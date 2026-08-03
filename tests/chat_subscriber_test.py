#############################################################################
# chat_subscriber_test.py
#
# Unit tests for backend/chat_subscriber.py (receiver-side Pub/Sub fan-out).
# Mocks pubsub_v1.SubscriberClient directly — same approach as
# chat_publisher_test.py — rather than depending on a live Pub/Sub emulator
# process.
#############################################################################

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))

os.environ.setdefault("PUBSUB_EMULATOR_HOST", "localhost:8085")

import backend.chat_subscriber as chat_subscriber  # noqa: E402


def make_message(attributes: dict, data: bytes = b"hello") -> MagicMock:
    msg = MagicMock()
    msg.attributes = attributes
    msg.data = data
    return msg


class TestChatSubscriber(unittest.TestCase):

    def setUp(self):
        self.mock_subscriber_client = MagicMock()
        self.mock_subscriber_client.topic_path.return_value = "projects/test/topics/chat-messages"
        self.mock_subscriber_client.subscription_path.side_effect = (
            lambda project, sub_id: f"projects/{project}/subscriptions/{sub_id}"
        )
        patcher = patch.object(chat_subscriber.pubsub_v1, "SubscriberClient", return_value=self.mock_subscriber_client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_creates_a_uniquely_named_subscription_on_the_chat_topic(self):
        chat_subscriber.ChatSubscriber()

        self.mock_subscriber_client.create_subscription.assert_called_once()
        _, kwargs = self.mock_subscriber_client.create_subscription.call_args
        request = kwargs["request"]
        self.assertTrue(request["name"].startswith("projects/daniel-reyes-uprm/subscriptions/chat-messages-fanout-"))
        self.assertEqual(request["topic"], "projects/test/topics/chat-messages")

    def test_two_instances_get_different_subscription_names(self):
        """This is what makes it fan-out instead of competing consumption —
        every running app instance needs its own subscription."""
        chat_subscriber.ChatSubscriber()
        chat_subscriber.ChatSubscriber()

        calls = self.mock_subscriber_client.create_subscription.call_args_list
        first_name = calls[0][1]["request"]["name"]
        second_name = calls[1][1]["request"]["name"]
        self.assertNotEqual(first_name, second_name)

    def test_starts_a_streaming_pull_with_a_callback(self):
        chat_subscriber.ChatSubscriber()
        self.mock_subscriber_client.subscribe.assert_called_once()
        _, kwargs = self.mock_subscriber_client.subscribe.call_args
        self.assertTrue(callable(kwargs["callback"]))

    def test_well_formed_message_is_buffered_and_acked(self):
        sub = chat_subscriber.ChatSubscriber()
        msg = make_message(
            {"group_id": "group-1", "sender_id": "user-1", "client_msg_id": "cmid-1"},
            data=b"hello there",
        )

        sub._handle_message(msg)

        buffered = sub.drain("group-1")
        self.assertEqual(len(buffered), 1)
        self.assertEqual(buffered[0]["sender_id"], "user-1")
        self.assertEqual(buffered[0]["content"], "hello there")
        self.assertEqual(buffered[0]["client_msg_id"], "cmid-1")
        self.assertIsNone(buffered[0]["id"])
        msg.ack.assert_called_once()

    def test_malformed_message_is_dropped_but_still_acked(self):
        sub = chat_subscriber.ChatSubscriber()
        msg = make_message({"group_id": "group-1"})  # missing sender_id, client_msg_id

        sub._handle_message(msg)

        self.assertEqual(sub.drain("group-1"), [])
        msg.ack.assert_called_once()

    def test_drain_clears_the_buffer(self):
        sub = chat_subscriber.ChatSubscriber()
        sub._handle_message(make_message({"group_id": "group-1", "sender_id": "u", "client_msg_id": "c1"}))

        first_drain = sub.drain("group-1")
        second_drain = sub.drain("group-1")

        self.assertEqual(len(first_drain), 1)
        self.assertEqual(second_drain, [])

    def test_drain_is_scoped_per_group(self):
        sub = chat_subscriber.ChatSubscriber()
        sub._handle_message(make_message({"group_id": "group-A", "sender_id": "u", "client_msg_id": "c1"}))
        sub._handle_message(make_message({"group_id": "group-B", "sender_id": "u", "client_msg_id": "c2"}))

        self.assertEqual(len(sub.drain("group-A")), 1)
        self.assertEqual(len(sub.drain("group-B")), 1)

    def test_drain_on_a_group_with_nothing_buffered_returns_empty(self):
        sub = chat_subscriber.ChatSubscriber()
        self.assertEqual(sub.drain("never-seen-group"), [])

    def test_shutdown_cancels_pull_and_deletes_the_subscription(self):
        sub = chat_subscriber.ChatSubscriber()
        sub._shutdown()

        sub._pull_future.cancel.assert_called_once()
        self.mock_subscriber_client.delete_subscription.assert_called_once()

    def test_shutdown_does_not_raise_if_cleanup_fails(self):
        sub = chat_subscriber.ChatSubscriber()
        self.mock_subscriber_client.delete_subscription.side_effect = RuntimeError("already gone")
        sub._shutdown()  # should not raise


class TestGetChatSubscriberFallback(unittest.TestCase):

    def test_returns_none_instead_of_raising_when_broker_unreachable(self):
        chat_subscriber.get_chat_subscriber.clear()
        with patch.object(chat_subscriber, "ChatSubscriber", side_effect=RuntimeError("no broker")):
            result = chat_subscriber.get_chat_subscriber()
        self.assertIsNone(result)
        chat_subscriber.get_chat_subscriber.clear()


if __name__ == "__main__":
    unittest.main()
