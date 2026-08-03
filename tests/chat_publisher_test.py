#############################################################################
# chat_publisher_test.py
#
# Unit tests for backend/chat_publisher.py (producer side of the
# distributed chat pipeline). Mocks pubsub_v1.PublisherClient directly —
# same approach the rest of this suite uses for BigQuery
# (see data_fetcher_test.py's @patch("data_fetcher._run_query") /
# @patch('google.cloud.bigquery.Client')) — rather than depending on a
# live Pub/Sub emulator process, so these run anywhere with no external
# process required.
#
# sys.path is set up explicitly (rather than assumed) so this file runs the
# same way regardless of how/where it's invoked from.
#############################################################################

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))

# PublisherClient() resolves real GCP credentials at construction time
# (module import time, here) unless PUBSUB_EMULATOR_HOST is set — without
# this, importing chat_publisher fails with DefaultCredentialsError in any
# environment without real GCP auth configured, emulator or not running.
os.environ.setdefault("PUBSUB_EMULATOR_HOST", "localhost:8085")

import backend.chat_publisher as chat_publisher  # noqa: E402


class TestPublishMessage(unittest.TestCase):

    def setUp(self):
        self.mock_future = MagicMock()
        self.mock_future.result.return_value = None
        self.mock_publisher = MagicMock()
        self.mock_publisher.publish.return_value = self.mock_future

        publisher_patcher = patch.object(chat_publisher, "_publisher", self.mock_publisher)
        topic_patcher = patch.object(chat_publisher, "_topic_path", "projects/test-project/topics/chat-messages")
        publisher_patcher.start()
        topic_patcher.start()
        self.addCleanup(publisher_patcher.stop)
        self.addCleanup(topic_patcher.stop)

    def test_returns_a_client_msg_id(self):
        client_msg_id = chat_publisher.publish_message("group-1", "user-1", "hello")
        self.assertTrue(client_msg_id)
        self.assertIsInstance(client_msg_id, str)

    def test_uses_group_id_as_the_ordering_key(self):
        """Ordering keys are what make per-group message order deterministic
        even though Pub/Sub doesn't guarantee ordering across different keys."""
        chat_publisher.publish_message("group-42", "user-1", "hello")
        _, kwargs = self.mock_publisher.publish.call_args
        self.assertEqual(kwargs["ordering_key"], "group-42")

    def test_attaches_group_and_sender_attributes(self):
        chat_publisher.publish_message("group-1", "user-9", "hi there")
        _, kwargs = self.mock_publisher.publish.call_args
        self.assertEqual(kwargs["group_id"], "group-1")
        self.assertEqual(kwargs["sender_id"], "user-9")

    def test_attached_client_msg_id_matches_the_returned_value(self):
        """persist_worker.py's idempotency depends on the id that comes back
        from publish_message matching the one attached to the wire message."""
        client_msg_id = chat_publisher.publish_message("group-1", "user-9", "hi there")
        _, kwargs = self.mock_publisher.publish.call_args
        self.assertEqual(kwargs["client_msg_id"], client_msg_id)

    def test_encodes_content_as_the_message_payload(self):
        chat_publisher.publish_message("group-1", "user-1", "hello world")
        args, _ = self.mock_publisher.publish.call_args
        self.assertEqual(args[1], b"hello world")

    def test_publishes_to_the_topic_path(self):
        chat_publisher.publish_message("group-1", "user-1", "hello")
        args, _ = self.mock_publisher.publish.call_args
        self.assertEqual(args[0], "projects/test-project/topics/chat-messages")

    def test_waits_for_publish_confirmation_before_returning(self):
        """future.result() blocks until Pub/Sub acks the publish (or raises
        on failure) — publish_message shouldn't return before that."""
        chat_publisher.publish_message("group-1", "user-1", "hello")
        self.mock_future.result.assert_called_once()

    def test_propagates_publish_failures(self):
        self.mock_future.result.side_effect = RuntimeError("broker unavailable")
        with self.assertRaises(RuntimeError):
            chat_publisher.publish_message("group-1", "user-1", "hello")

    def test_generates_a_unique_id_per_call(self):
        first = chat_publisher.publish_message("group-1", "user-1", "one")
        second = chat_publisher.publish_message("group-1", "user-1", "two")
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
