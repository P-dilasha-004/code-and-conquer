#############################################################################
# chat_subscriber.py
#
# Receiver-side push delivery for the distributed chat pipeline. Each
# Streamlit server process runs ONE background Pub/Sub subscriber (via
# get_chat_subscriber(), cached with st.cache_resource so every session on
# this process shares it) that receives a live copy of every published
# chat message and buffers it in memory, keyed by group_id.
#
# This is a fan-out subscription: a fresh, uniquely-named subscription on
# the chat-messages topic, separate from persist_worker.py's own
# subscription. Pub/Sub delivers an independent copy of every message to
# every subscription on a topic — this doesn't compete with, interfere
# with, or depend on the durable-persistence path at all. It's purely an
# additional, low-latency read path; Postgres (written by persist_worker.py)
# remains the system of record.
#
# Streamlit constraint worth being upfront about: this makes message
# DELIVERY push-based, but Streamlit's rendering model still needs *some*
# timer to redraw a session's browser — there's no supported way to force
# another session's rerun from a background thread. See group_chat.py's
# much-shortened run_every. The actual win is that the redraw now reads a
# fast in-memory buffer instead of hitting Postgres on every tick, so the
# interval can safely shrink from 2s to a fraction of a second without
# adding real load.
#
# Degrades gracefully: if no Pub/Sub broker is reachable (no emulator
# running, no real GCP credentials), get_chat_subscriber() returns None
# instead of crashing the page — group_chat.py falls back to
# Postgres-only polling in that case, same as before this file existed.
#############################################################################

import atexit
import logging
import os
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import streamlit as st
from google.cloud import pubsub_v1

logger = logging.getLogger("chat_subscriber")

PROJECT_ID = os.getenv("GCP_PROJECT_ID", "daniel-reyes-uprm")
TOPIC_ID = os.getenv("CHAT_PUBSUB_TOPIC", "chat-messages")


class ChatSubscriber:
    def __init__(self):
        self._buffers: dict[str, list[dict]] = defaultdict(list)
        self._lock = threading.Lock()

        self._subscriber = pubsub_v1.SubscriberClient()
        topic_path = self._subscriber.topic_path(PROJECT_ID, TOPIC_ID)

        # Unique per process on purpose — every running instance of this
        # app needs its OWN copy of every message (fan-out), not to compete
        # with other instances over a shared queue (that's what
        # persist_worker.py's subscription is for, and it's a different one).
        sub_id = f"chat-messages-fanout-{uuid.uuid4().hex[:12]}"
        self._subscription_path = self._subscriber.subscription_path(PROJECT_ID, sub_id)

        # Short timeout AND retry=None on purpose: this runs on a Streamlit
        # page load, and the client's default retry policy has its own ~60s
        # total deadline that `timeout` alone doesn't override — if no
        # broker is reachable, get_chat_subscriber()'s fallback needs to
        # kick in fast (one attempt, small deadline), not hang the page for
        # a minute first.
        self._subscriber.create_subscription(
            request={"name": self._subscription_path, "topic": topic_path},
            retry=None,
            timeout=5.0,
        )

        self._pull_future = self._subscriber.subscribe(
            self._subscription_path, callback=self._handle_message
        )

        atexit.register(self._shutdown)

    def _handle_message(self, message) -> None:
        group_id = message.attributes.get("group_id")
        sender_id = message.attributes.get("sender_id")
        client_msg_id = message.attributes.get("client_msg_id")

        if group_id and sender_id and client_msg_id:
            with self._lock:
                self._buffers[group_id].append({
                    "id": None,  # not durable yet — persist_worker.py hasn't written the row
                    "client_msg_id": client_msg_id,
                    "sender_id": sender_id,
                    "content": message.data.decode("utf-8"),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
        # Ack regardless: this is a best-effort low-latency read path, not
        # the durable one. A dropped/malformed message here still gets
        # written for real by persist_worker.py's separate subscription.
        message.ack()

    def drain(self, group_id: str) -> list[dict]:
        """Returns and clears everything buffered for this group since the
        last drain call for it."""
        with self._lock:
            pending = self._buffers.get(group_id, [])
            self._buffers[group_id] = []
        return pending

    def _shutdown(self) -> None:
        try:
            self._pull_future.cancel()
        except Exception:
            pass
        try:
            self._subscriber.delete_subscription(request={"subscription": self._subscription_path})
        except Exception:
            pass


@st.cache_resource(show_spinner=False)
def get_chat_subscriber() -> Optional[ChatSubscriber]:
    try:
        return ChatSubscriber()
    except Exception as e:
        logger.warning("Chat fan-out subscriber unavailable, falling back to polling only: %s", e)
        return None
