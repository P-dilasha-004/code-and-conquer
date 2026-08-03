#############################################################################
# persist_worker.py
#
# Consumer/worker side of the distributed chat pipeline — a standalone
# service, independent of the Streamlit app, that receives messages
# published by chat_publisher.publish_message() (src/backend/chat_publisher.py)
# via a Pub/Sub PUSH subscription and durably persists them into Supabase's
# `messages` table.
#
# Push, not pull: Cloud Run bills/scales around HTTP requests and scales to
# zero when idle. A pull-loop worker doesn't fit that model without forcing
# min-instances=1 (a small but constant cost even when no one's chatting).
# A push subscription instead has Pub/Sub call THIS endpoint over HTTP —
# genuinely serverless, no idle cost. See scripts/setup_pubsub.sh for how
# the subscription is provisioned once this service has a deployed URL.
#
# This is the only thing that writes to `messages` now — ingestion
# (publish) and durable storage (this service) are separate deployable
# units, communicating only through the broker.
#
# Pub/Sub push is at-least-once, same as pull: a request can be redelivered
# (this endpoint returning a non-2xx, a slow response, a Cloud Run cold
# start timing out, etc), so persistence here is an idempotent upsert keyed
# on `client_msg_id` (added via migrations/001_add_client_msg_id.sql, with
# a UNIQUE constraint that backs the upsert's conflict target). Messages
# that fail repeatedly are diverted to the dead-letter topic once Pub/Sub's
# configured max delivery attempts are exhausted, instead of retrying
# forever.
#
# Run locally:
#   SUPABASE_URL=... SUPABASE_KEY=... python3 src/worker/persist_worker.py
# (then point a push subscription's --push-endpoint at it via a tunnel, or
# just POST a Pub/Sub-shaped envelope at it directly for manual testing —
# see tests/persist_worker_test.py for the exact envelope shape.)
#############################################################################

import base64
import logging
import os

from flask import Flask, request
from google.auth.transport import requests as google_auth_requests
from google.oauth2 import id_token
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("persist_worker")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Missing SUPABASE_URL or SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# The service account email the push subscription authenticates as (set via
# --push-auth-service-account in scripts/setup_pubsub.sh). Every push
# request carries a signed OIDC token in its Authorization header; verifying
# it here is what stops anyone who finds this public Cloud Run URL from
# POSTing fake chat messages directly. Left unset for local/manual testing,
# where there's no real Pub/Sub in front of this endpoint to sign anything.
PUBSUB_PUSH_SERVICE_ACCOUNT = os.getenv("PUBSUB_PUSH_SERVICE_ACCOUNT")

app = Flask(__name__)


def verify_push_request(flask_request) -> bool:
    if not PUBSUB_PUSH_SERVICE_ACCOUNT:
        logger.warning("PUBSUB_PUSH_SERVICE_ACCOUNT not set — skipping push auth verification")
        return True

    auth_header = flask_request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False

    token = auth_header.split(" ", 1)[1]
    try:
        claims = id_token.verify_oauth2_token(token, google_auth_requests.Request())
    except ValueError as e:
        logger.error("Push request token verification failed: %s", e)
        return False

    return claims.get("email") == PUBSUB_PUSH_SERVICE_ACCOUNT


def persist(group_id: str, sender_id: str, content: str, client_msg_id: str) -> None:
    """Idempotent write: upsert on the `client_msg_id` unique constraint so
    a redelivered push never creates a second row.
    """
    supabase.table("messages").upsert(
        {
            "group_id": group_id,
            "sender_id": sender_id,
            "content": content,
            "client_msg_id": client_msg_id,
        },
        on_conflict="client_msg_id",
        ignore_duplicates=True,
    ).execute()


@app.route("/", methods=["POST"])
def handle_push():
    if not verify_push_request(request):
        return "unauthorized", 401

    envelope = request.get_json(silent=True)
    if not envelope or "message" not in envelope:
        logger.error("Malformed push envelope: %s", envelope)
        # 400 (not 2xx) — this isn't a real Pub/Sub push, don't ack-and-move-on.
        return "bad request", 400

    message = envelope["message"]
    attributes = message.get("attributes", {})
    group_id = attributes.get("group_id")
    sender_id = attributes.get("sender_id")
    client_msg_id = attributes.get("client_msg_id")

    if not (group_id and sender_id and client_msg_id):
        # Retrying can't fix a missing attribute — return 2xx so Pub/Sub
        # treats it as delivered instead of looping it into the DLQ for a
        # bug that redelivery will never resolve.
        logger.error("Dropping malformed message, missing attributes: %s", attributes)
        return "", 200

    try:
        content = base64.b64decode(message.get("data", "")).decode("utf-8")
        persist(group_id, sender_id, content, client_msg_id)
        logger.info("Persisted client_msg_id=%s group_id=%s", client_msg_id, group_id)
        return "", 200
    except Exception as e:
        # Transient failure (DB hiccup, etc) — non-2xx tells Pub/Sub to
        # redeliver. persist() is idempotent, so redelivery is safe.
        logger.error("Failed to persist client_msg_id=%s, will be retried: %s", client_msg_id, e)
        return "internal error", 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8081)))
