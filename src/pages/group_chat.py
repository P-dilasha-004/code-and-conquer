import time
from datetime import datetime, timezone

import streamlit as st

from backend.chat_handler import get_messages, get_new_messages, send_message
from backend.chat_subscriber import get_chat_subscriber


# How often the fragment redraws. This used to be the same number as "how
# often we check Postgres for new messages" — now it's just the UI redraw
# cadence, decoupled from the DB: most ticks only drain an in-memory buffer
# fed by the Pub/Sub fan-out subscriber (see chat_subscriber.py), which is
# cheap enough to do this often. Streamlit itself is the reason this can't
# be push-triggered instead of timer-triggered — there's no supported way
# to force another session's rerun from a background thread, so a fast
# timer reading a fast source is the best available approximation of push.
FAST_POLL_INTERVAL_SECONDS = 0.3

# Independent, much slower cadence for the durable Postgres reconciliation
# check — defense in depth in case the fan-out subscriber missed a message
# (dropped, process restarted, etc) or isn't available at all (see
# chat_subscriber.get_chat_subscriber()'s graceful-fallback behavior, in
# which case this is the ONLY path new messages arrive through, same as
# the original design).
RECONCILE_INTERVAL_SECONDS = 10

# Used instead of RECONCILE_INTERVAL_SECONDS when chat_subscriber is
# unavailable (see get_chat_subscriber()'s fallback) — in that case
# Postgres is the ONLY source of new messages, so this needs to behave
# like the original design's cadence, not the sparse defense-in-depth
# cadence. Checking Postgres every FAST_POLL_INTERVAL_SECONDS (0.3s) in
# that situation would be a real, needless increase in DB load.
FALLBACK_POLL_INTERVAL_SECONDS = 2


def _init_group_chat_state() -> None:
    if "chat_messages_by_group" not in st.session_state:
        st.session_state.chat_messages_by_group = {}
    if "chat_last_ts_by_group" not in st.session_state:
        st.session_state.chat_last_ts_by_group = {}
    if "chat_seen_ids_by_group" not in st.session_state:
        st.session_state.chat_seen_ids_by_group = {}
    if "chat_pending_by_group" not in st.session_state:
        # client_msg_id -> index into chat_messages_by_group[group_id],
        # for messages shown optimistically before persist_worker.py has
        # durably written them. See _add_optimistic_message.
        st.session_state.chat_pending_by_group = {}


def _prime_cache(group_id: str) -> None:
    """Load the full message history once per session per group."""
    if group_id in st.session_state.chat_messages_by_group:
        return

    history = get_messages(group_id)
    st.session_state.chat_messages_by_group[group_id] = list(history)
    st.session_state.chat_seen_ids_by_group[group_id] = {
        m.get("id") for m in history if m.get("id") is not None
    }
    if history:
        latest = max((m.get("created_at", "") for m in history), default="")
        if latest:
            st.session_state.chat_last_ts_by_group[group_id] = latest


def _add_optimistic_message(group_id: str, sender_id: str, content: str, client_msg_id: str) -> None:
    """Show the sender's own message immediately after send_message()
    returns. send_message() now only publishes to Pub/Sub (see
    backend/chat_publisher.py) — persist_worker.py writes the durable row
    asynchronously, so there's no longer a synchronous DB write to
    re-fetch. Without this, the sender would see nothing until the next
    poll tick happens to land after the worker has caught up.

    Keyed by client_msg_id so _merge_new_messages can replace this
    placeholder in place once the authoritative row arrives, instead of
    showing the message twice.
    """
    cached = st.session_state.chat_messages_by_group.setdefault(group_id, [])
    pending = st.session_state.chat_pending_by_group.setdefault(group_id, {})

    cached.append({
        "id": None,
        "client_msg_id": client_msg_id,
        "sender_id": sender_id,
        "content": content,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    pending[client_msg_id] = len(cached) - 1


def _merge_new_messages(group_id: str, new_messages: list) -> int:
    """Append messages we haven't displayed yet, keeping the cache ordered
    oldest → newest. Returns the number of freshly-added messages.

    `pending` tracks every message currently shown WITHOUT a durable id yet
    — that's not just the sender's own optimistic echo (_add_optimistic_message)
    anymore. chat_subscriber's fan-out delivers other users' messages the
    same way, before persist_worker.py has written them: no `id`, just a
    client_msg_id. Without tracking those too, the slow Postgres
    reconciliation poll would re-append them a second time once the
    durable row shows up — this function registers ANY id-less message it
    appends into `pending`, so that later reconciliation replaces it in
    place instead of duplicating it, regardless of who sent it.
    """
    if not new_messages:
        return 0

    cached = st.session_state.chat_messages_by_group.setdefault(group_id, [])
    seen = st.session_state.chat_seen_ids_by_group.setdefault(group_id, set())
    pending = st.session_state.chat_pending_by_group.setdefault(group_id, {})
    latest_ts = st.session_state.chat_last_ts_by_group.get(group_id, "")

    added = 0
    for msg in new_messages:
        msg_id = msg.get("id")
        if msg_id is not None and msg_id in seen:
            continue

        client_msg_id = msg.get("client_msg_id")
        if client_msg_id and client_msg_id in pending:
            # Already shown (our own echo, or an earlier fan-out delivery)
            # — replace the placeholder in place with the authoritative row.
            cached[pending.pop(client_msg_id)] = msg
        else:
            cached.append(msg)
            added += 1
            if client_msg_id and msg_id is None:
                # Not durable yet — remember where it is so a later,
                # now-durable copy of this same message reconciles here
                # instead of appending again.
                pending[client_msg_id] = len(cached) - 1

        if msg_id is not None:
            seen.add(msg_id)

        created_at = msg.get("created_at") or ""
        if created_at and created_at > latest_ts:
            latest_ts = created_at

    if latest_ts:
        st.session_state.chat_last_ts_by_group[group_id] = latest_ts

    return added


def _format_time(created_at: str) -> str:
    if not created_at:
        return ""
    return str(created_at)[11:16]


@st.fragment(run_every=f"{FAST_POLL_INTERVAL_SECONDS}s")
def _chat_stream_fragment(group_id: str, user_id: str) -> None:
    """Auto-refreshing slice of the page. Streamlit reruns only this
    fragment, every FAST_POLL_INTERVAL_SECONDS, so new messages appear
    without a full page reload and without disturbing the input box.

    Two sources feed it, at two different cadences:
      1. Every tick: drain chat_subscriber's in-memory buffer, fed by a
         Pub/Sub fan-out subscription — push-based, near-instant once a
         message is published, cheap enough to check this often.
      2. Every RECONCILE_INTERVAL_SECONDS: a real get_new_messages() call
         against Postgres — the durable source of truth, and the only
         source at all if the fan-out subscriber isn't available (see
         chat_subscriber.get_chat_subscriber()'s fallback behavior).
    """
    subscriber = get_chat_subscriber()
    if subscriber is not None:
        _merge_new_messages(group_id, subscriber.drain(group_id))

    reconcile_key = f"chat_last_reconcile_{group_id}"
    now = time.monotonic()
    last_reconcile = st.session_state.get(reconcile_key, 0.0)
    reconcile_interval = RECONCILE_INTERVAL_SECONDS if subscriber is not None else FALLBACK_POLL_INTERVAL_SECONDS
    if (now - last_reconcile) >= reconcile_interval:
        last_ts = st.session_state.chat_last_ts_by_group.get(group_id)
        _merge_new_messages(group_id, get_new_messages(group_id, last_ts))
        st.session_state[reconcile_key] = now

    messages = st.session_state.chat_messages_by_group.get(group_id, [])

    with st.container(height=450, border=True):
        if not messages:
            st.info("No messages yet. Start the conversation below.")
        else:
            for msg in messages:
                sender = msg.get("sender_id", "Unknown")
                content = msg.get("content", "")
                time_str = _format_time(msg.get("created_at", ""))
                is_me = sender == user_id

                role = "user" if is_me else "assistant"
                label = "You" if is_me else sender

                with st.chat_message(role):
                    st.markdown(
                        f"**{label}** · <span style='opacity:0.6'>{time_str}</span>",
                        unsafe_allow_html=True
                    )
                    st.write(content)


def display_group_chat_page() -> None:
    _init_group_chat_state()

    current_group = st.session_state.get("current_chat_group")
    user_id = (
        st.session_state.get("user_id")
        or st.session_state.get("current_user_id")
    )

    if not current_group:
        st.warning("No group selected.")
        return

    if not user_id:
        st.error("No logged-in user found.")
        return

    group_id = current_group.get("id")
    group_name = current_group.get("name", "Group Chat")

    if not group_id:
        st.error("Current group is missing an id.")
        return

    st.title(f"💬 {group_name}")
    st.caption("Live chat · push-delivered, with a periodic durability check")

    _prime_cache(group_id)

    col_a, col_b = st.columns([1, 1])
    with col_a:
        if st.button("Refresh", use_container_width=True, key=f"gc_refresh_{group_id}"):
            # Hard refresh: drop the cache and reload the whole history.
            st.session_state.chat_messages_by_group.pop(group_id, None)
            st.session_state.chat_seen_ids_by_group.pop(group_id, None)
            st.session_state.chat_last_ts_by_group.pop(group_id, None)
            st.session_state.chat_pending_by_group.pop(group_id, None)
            st.session_state.pop(f"chat_last_reconcile_{group_id}", None)
            st.rerun()

    with col_b:
        if st.button("Back to My Groups", use_container_width=True, key=f"gc_back_{group_id}"):
            st.session_state.page = "My Groups"
            st.rerun()

    _chat_stream_fragment(group_id, user_id)

    message_text = st.chat_input(
        "Type a message…",
        key=f"chat_input_{group_id}",
    )

    if message_text and message_text.strip():
        try:
            client_msg_id = send_message(group_id, user_id, message_text.strip())
            # send_message() now only publishes to Pub/Sub — persist_worker.py
            # writes the durable row asynchronously, so there's nothing in
            # Supabase to re-fetch yet. Show it locally right away; the next
            # poll tick reconciles this placeholder with the authoritative
            # row once the worker has caught up (see _merge_new_messages).
            _add_optimistic_message(group_id, user_id, message_text.strip(), client_msg_id)
            st.rerun()
        except Exception as e:
            st.error(f"Failed to send message: {e}")
