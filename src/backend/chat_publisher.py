#############################################################################
# chat_publisher.py
#
# Producer side of the distributed chat pipeline. send_message() in
# chat_handler.py calls publish_message() here instead of writing to
# Supabase directly — persist_worker.py (src/worker/persist_worker.py) is
# the only thing that writes to the `messages` table, so ingestion and
# durable storage are decoupled across a Pub/Sub topic.
#
# Local dev / tests: set PUBSUB_EMULATOR_HOST (e.g. "localhost:8085") before
# import and the client library talks to the emulator instead of real GCP —
# no credentials or live topic needed. See scripts/setup_pubsub.sh for
# provisioning the real topic/subscriptions when you're ready to deploy.
#############################################################################

import os
import uuid

from google.cloud import pubsub_v1

PROJECT_ID = os.getenv("GCP_PROJECT_ID", "daniel-reyes-uprm")
TOPIC_ID = os.getenv("CHAT_PUBSUB_TOPIC", "chat-messages")

# Ordering keys (used below to keep each group's messages in order) require
# message ordering to be turned on explicitly on the publisher client.
_publisher = pubsub_v1.PublisherClient(
    publisher_options=pubsub_v1.types.PublisherOptions(enable_message_ordering=True)
)
_topic_path = _publisher.topic_path(PROJECT_ID, TOPIC_ID)


def publish_message(group_id: str, sender_id: str, content: str) -> str:
    """Publish a chat message onto the chat-messages topic.

    Uses `group_id` as the Pub/Sub ordering key so messages within a single
    group are delivered to subscribers in the order they were published,
    even though Pub/Sub doesn't guarantee ordering across different keys.

    `client_msg_id` is a fresh uuid4 attached as a message attribute — it's
    the idempotency key persist_worker.py upserts on, since Pub/Sub only
    guarantees at-least-once delivery (a message can be redelivered after a
    worker crash, a slow ack, etc).

    Returns the generated client_msg_id.
    """
    client_msg_id = str(uuid.uuid4())

    future = _publisher.publish(
        _topic_path,
        content.encode("utf-8"),
        ordering_key=group_id,
        group_id=group_id,
        sender_id=sender_id,
        client_msg_id=client_msg_id,
    )
    # Blocks until Pub/Sub acks the publish (or raises on failure) so the
    # caller knows the message was actually accepted onto the bus before
    # send_message() returns.
    future.result()

    return client_msg_id
