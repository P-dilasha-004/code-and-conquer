#############################################################################
# group_chat_test.py
#
# Unit tests for pages/group_chat.py's message cache/reconciliation logic
# (_add_optimistic_message, _merge_new_messages). These are pure
# session-state manipulation, no network — Streamlit's st.session_state
# works as a plain dict-like object outside a real app run ("bare mode").
#############################################################################

import os
import sys
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))

# chat_handler.py (imported transitively) and chat_publisher.py both
# resolve credentials/URLs at import time — see chat_publisher_test.py.
os.environ.setdefault("SUPABASE_URL", "https://fake-project.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "fake-anon-key")
os.environ.setdefault("PUBSUB_EMULATOR_HOST", "localhost:8085")

import pages.group_chat as group_chat  # noqa: E402
import streamlit as st  # noqa: E402


class TestMergeAndReconciliation(unittest.TestCase):

    def setUp(self):
        st.session_state.clear()
        group_chat._init_group_chat_state()
        self.group_id = "group-1"

    def test_sender_optimistic_echo_appears_immediately(self):
        group_chat._add_optimistic_message(self.group_id, "user-A", "hi", "cmid-1")

        messages = st.session_state.chat_messages_by_group[self.group_id]
        self.assertEqual(len(messages), 1)
        self.assertIsNone(messages[0]["id"])
        self.assertEqual(messages[0]["content"], "hi")

    def test_durable_row_reconciles_the_senders_own_echo_in_place(self):
        group_chat._add_optimistic_message(self.group_id, "user-A", "hi", "cmid-1")

        durable_row = {"id": 501, "client_msg_id": "cmid-1", "sender_id": "user-A",
                        "content": "hi", "created_at": "2026-01-01T00:00:05+00:00"}
        added = group_chat._merge_new_messages(self.group_id, [durable_row])

        messages = st.session_state.chat_messages_by_group[self.group_id]
        self.assertEqual(len(messages), 1, "the durable row must replace the placeholder, not duplicate it")
        self.assertEqual(messages[0]["id"], 501)
        self.assertEqual(added, 0, "a reconciliation isn't a net-new message")

    def test_fanout_delivered_message_from_another_user_is_shown_without_an_id(self):
        """Models chat_subscriber delivering someone else's message before
        persist_worker.py has written it."""
        fanout_msg = {"id": None, "client_msg_id": "cmid-2", "sender_id": "user-B",
                      "content": "hey", "created_at": "2026-01-01T00:00:01+00:00"}

        added = group_chat._merge_new_messages(self.group_id, [fanout_msg])

        messages = st.session_state.chat_messages_by_group[self.group_id]
        self.assertEqual(added, 1)
        self.assertEqual(len(messages), 1)
        self.assertIsNone(messages[0]["id"])

    def test_durable_row_reconciles_another_users_fanout_message_in_place(self):
        """This is the bug the generalized `pending` tracking fixes: without
        it, the durable Postgres reconciliation poll would re-append this
        message a second time."""
        fanout_msg = {"id": None, "client_msg_id": "cmid-2", "sender_id": "user-B",
                      "content": "hey", "created_at": "2026-01-01T00:00:01+00:00"}
        group_chat._merge_new_messages(self.group_id, [fanout_msg])

        durable_row = {"id": 777, "client_msg_id": "cmid-2", "sender_id": "user-B",
                        "content": "hey", "created_at": "2026-01-01T00:00:01+00:00"}
        added = group_chat._merge_new_messages(self.group_id, [durable_row])

        messages = st.session_state.chat_messages_by_group[self.group_id]
        self.assertEqual(len(messages), 1, "must not be duplicated once the durable row arrives")
        self.assertEqual(messages[0]["id"], 777)
        self.assertEqual(added, 0)

    def test_a_message_already_seen_by_id_is_not_reappended(self):
        durable_row = {"id": 42, "client_msg_id": "cmid-3", "sender_id": "user-C",
                        "content": "once", "created_at": "2026-01-01T00:00:02+00:00"}
        group_chat._merge_new_messages(self.group_id, [durable_row])

        added_again = group_chat._merge_new_messages(self.group_id, [dict(durable_row)])

        messages = st.session_state.chat_messages_by_group[self.group_id]
        self.assertEqual(len(messages), 1)
        self.assertEqual(added_again, 0)

    def test_two_different_groups_do_not_share_pending_state(self):
        group_chat._add_optimistic_message("group-A", "user-A", "a", "cmid-a")
        group_chat._add_optimistic_message("group-B", "user-B", "b", "cmid-b")

        self.assertEqual(len(st.session_state.chat_messages_by_group["group-A"]), 1)
        self.assertEqual(len(st.session_state.chat_messages_by_group["group-B"]), 1)
        self.assertIn("cmid-a", st.session_state.chat_pending_by_group["group-A"])
        self.assertNotIn("cmid-a", st.session_state.chat_pending_by_group.get("group-B", {}))


if __name__ == "__main__":
    unittest.main()
