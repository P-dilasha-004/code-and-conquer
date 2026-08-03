#!/bin/bash
# Provisions the real Pub/Sub topic + push subscription for the distributed
# chat pipeline (chat_publisher.py -> topic -> persist_worker.py, pushed to
# over HTTP rather than pulled).
#
# NOT run automatically by anything — this is a manual, one-time step you
# run yourself once you've decided which GCP project this should live in.
# Local development and the test suite never touch real cloud resources —
# the tests mock the Pub/Sub client and POST directly to persist_worker.py's
# Flask test client instead.
#
# ORDER MATTERS: a push subscription needs a URL to push to, so
# persist_worker.py must already be deployed to Cloud Run (see
# PERSONAL_DEPLOY.md) before you can run PART 2 below. Run PART 1 any time;
# come back for PART 2 once you have the worker's Cloud Run URL.
#
# Make sure you have Owner/Editor (or at least pubsub.admin + iam.admin) on
# PROJECT_ID before running this.

### VARIABLES TO CHANGE - START
PROJECT_ID=your-gcp-project-id
TOPIC_ID=chat-messages
PERSIST_SUBSCRIPTION_ID=chat-messages-persist-sub
DEAD_LETTER_TOPIC_ID=chat-messages-dlq
DEAD_LETTER_SUBSCRIPTION_ID=chat-messages-dlq-sub
MAX_DELIVERY_ATTEMPTS=5

# PART 2 only — fill these in after `gcloud run deploy` for persist_worker
# has printed the service's URL.
WORKER_URL=https://TODO-fill-in-after-deploying-persist-worker.run.app
WORKER_SERVICE_NAME=persist-worker
WORKER_REGION=us-central1
PUSH_AUTH_SERVICE_ACCOUNT=TODO-fill-in-project-number-compute@developer.gserviceaccount.com
### VARIABLES TO CHANGE - END

gcloud config set project "${PROJECT_ID}"

if [ $? != 0 ]; then
    echo "setting GCP project failed! did you set the PROJECT_ID bash variable?"
    exit 1
fi

gcloud services enable pubsub.googleapis.com

if [ $? != 0 ]; then
    echo "enabling the Pub/Sub API failed!"
    exit 1
fi

# ================= PART 1: topics (run any time) ================= #

gcloud pubsub topics create "${TOPIC_ID}"
gcloud pubsub topics create "${DEAD_LETTER_TOPIC_ID}"

echo "
PART 1 done: topics '${TOPIC_ID}' and '${DEAD_LETTER_TOPIC_ID}' created.

Now deploy persist_worker.py to Cloud Run (see PERSONAL_DEPLOY.md), copy
its service URL into WORKER_URL above, then re-run this script for PART 2.
"

# ================= PART 2: push subscription (after worker is deployed) ================= #
# Skip this section (comment it out or just don't re-run past PART 1) until
# WORKER_URL is a real deployed URL, not the TODO placeholder.

if [[ "${WORKER_URL}" == *TODO* ]]; then
    echo "WORKER_URL is still a placeholder — stopping before PART 2. Deploy persist_worker.py first."
    exit 0
fi

# --enable-message-ordering: required for chat_publisher.py's
#   ordering_key=group_id to actually guarantee per-group delivery order.
# --push-endpoint / --push-auth-service-account: Pub/Sub calls
#   persist_worker.py's HTTP endpoint directly, authenticating as this
#   service account (an OIDC token persist_worker.py verifies — see
#   verify_push_request() in persist_worker.py).
# --dead-letter-topic / --max-delivery-attempts: after this many failed
#   deliveries (persist_worker.py returning non-2xx on DB errors), Pub/Sub
#   stops retrying and diverts the message here instead of forever.

gcloud pubsub subscriptions create "${PERSIST_SUBSCRIPTION_ID}" \
    --topic="${TOPIC_ID}" \
    --enable-message-ordering \
    --push-endpoint="${WORKER_URL}" \
    --push-auth-service-account="${PUSH_AUTH_SERVICE_ACCOUNT}" \
    --dead-letter-topic="${DEAD_LETTER_TOPIC_ID}" \
    --max-delivery-attempts="${MAX_DELIVERY_ATTEMPTS}"

if [ $? != 0 ]; then
    echo "creating the persist subscription failed!"
    exit 1
fi

# A pull subscription on the DLQ so dead-lettered messages are inspectable
# (e.g. `gcloud pubsub subscriptions pull ...`) instead of silently dropped.
gcloud pubsub subscriptions create "${DEAD_LETTER_SUBSCRIPTION_ID}" \
    --topic="${DEAD_LETTER_TOPIC_ID}"

# The Pub/Sub service account needs explicit permission to publish to the
# dead-letter topic on your behalf — required for dead-lettering to work.
PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')
PUBSUB_SERVICE_AGENT="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"

gcloud pubsub topics add-iam-policy-binding "${DEAD_LETTER_TOPIC_ID}" \
    --member="serviceAccount:${PUBSUB_SERVICE_AGENT}" \
    --role="roles/pubsub.publisher"

gcloud pubsub subscriptions add-iam-policy-binding "${PERSIST_SUBSCRIPTION_ID}" \
    --member="serviceAccount:${PUBSUB_SERVICE_AGENT}" \
    --role="roles/pubsub.subscriber"

# PUSH_AUTH_SERVICE_ACCOUNT needs permission to actually invoke the
# persist_worker Cloud Run service — Cloud Run services are private by
# default, this is what lets the push subscription's authenticated
# requests through.
gcloud run services add-iam-policy-binding "${WORKER_SERVICE_NAME}" \
    --region="${WORKER_REGION}" \
    --member="serviceAccount:${PUSH_AUTH_SERVICE_ACCOUNT}" \
    --role="roles/run.invoker"

echo "
Pub/Sub provisioning complete.

Set these on the main app (publisher side):
  GCP_PROJECT_ID='${PROJECT_ID}'
  CHAT_PUBSUB_TOPIC='${TOPIC_ID}'

Set this on persist_worker.py's Cloud Run service:
  PUBSUB_PUSH_SERVICE_ACCOUNT='${PUSH_AUTH_SERVICE_ACCOUNT}'

Don't forget: run migrations/001_add_client_msg_id.sql against the Supabase
project before sending any real traffic — persist_worker.py's upsert
depends on the UNIQUE constraint that migration adds.
"
