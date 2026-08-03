#############################################################################
# distributed_chat_integration_test.py
#
# chat_publisher_test.py and persist_worker_test.py each verify their own
# side of the pipeline in isolation, mocking the other side away. Neither
# proves the two independently-deployable services actually agree on the
# wire format — that's exactly the kind of bug that shows up for real only
# once two separately-written services try to talk to each other.
#
# This test chains the REAL, unmodified send_message() -> publish_message()
# call to the REAL, unmodified handle_push() -> persist() call. The only
# thing intercepted is the transport hop itself (there's no live Pub/Sub
# broker in this environment) — everything on either side of that hop is
# production code, unmodified.
#############################################################################

import base64
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(SRC_DIR / "worker"))

os.environ.setdefault("PUBSUB_EMULATOR_HOST", "localhost:8085")
os.environ.setdefault("SUPABASE_URL", "https://fake-project.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "fake-anon-key")

import backend.chat_handler as chat_handler  # noqa: E402
import backend.chat_publisher as chat_publisher  # noqa: E402
import persist_worker  # noqa: E402


def capture_published_message(mock_publisher_publish):
    """Turns a captured PublisherClient.publish(...) call into the exact
    envelope shape Pub/Sub would POST to a push endpoint — the same
    translation the real Pub/Sub service performs, done here explicitly so
    the test can feed it to the real handle_push().
    """
    _, kwargs = mock_publisher_publish.call_args
    args, _ = mock_publisher_publish.call_args
    data = args[1]
    return {
        "message": {
            "data": base64.b64encode(data).decode("utf-8"),
            "attributes": {
                "group_id": kwargs["group_id"],
                "sender_id": kwargs["sender_id"],
                "client_msg_id": kwargs["client_msg_id"],
            },
            "messageId": "integration-test-message-id",
        },
        "subscription": "projects/test-project/subscriptions/chat-messages-persist-sub",
    }


class TestPublisherConsumerAgreeOnWireFormat(unittest.TestCase):

    def setUp(self):
        # Producer side: real chat_handler.send_message() / publish_message(),
        # only the actual network publish call is stubbed (no live broker
        # here) — it still runs through the real ordering-key/attribute
        # logic in chat_publisher.py.
        self.mock_future = MagicMock()
        self.mock_future.result.return_value = None
        pub_patcher = patch.object(chat_publisher, "_publisher")
        self.mock_pub_client = pub_patcher.start()
        self.mock_pub_client.publish.return_value = self.mock_future
        self.addCleanup(pub_patcher.stop)

        # Consumer side: real persist_worker.handle_push() / persist(),
        # only the actual Supabase network call is mocked.
        self.mock_supabase = MagicMock()
        supabase_patcher = patch.object(persist_worker, "supabase", self.mock_supabase)
        supabase_patcher.start()
        self.addCleanup(supabase_patcher.stop)
        service_account_patcher = patch.object(persist_worker, "PUBSUB_PUSH_SERVICE_ACCOUNT", None)
        service_account_patcher.start()
        self.addCleanup(service_account_patcher.stop)

        persist_worker.app.testing = True
        self.flask_client = persist_worker.app.test_client()

    def test_a_sent_message_round_trips_to_an_identical_persisted_row(self):
        # 1. Real producer call, exactly as group_chat.py's send handler makes it.
        client_msg_id = chat_handler.send_message("group-42", "user-7", "hello from the real pipeline")

        # 2. Translate the captured publish() call into what Pub/Sub would
        #    actually push (this step stands in for the live broker).
        envelope = capture_published_message(self.mock_pub_client.publish)

        # 3. Feed it to the real consumer endpoint, exactly as Pub/Sub would.
        resp = self.flask_client.post("/", json=envelope)

        self.assertEqual(resp.status_code, 200)
        self.mock_supabase.table.return_value.upsert.assert_called_once()
        (payload,), kwargs = self.mock_supabase.table.return_value.upsert.call_args

        # The row persist_worker.py wrote matches exactly what send_message()
        # was called with — no field lost, renamed, or mismatched crossing
        # the producer -> consumer boundary.
        self.assertEqual(payload["group_id"], "group-42")
        self.assertEqual(payload["sender_id"], "user-7")
        self.assertEqual(payload["content"], "hello from the real pipeline")
        self.assertEqual(payload["client_msg_id"], client_msg_id)
        self.assertEqual(kwargs["on_conflict"], "client_msg_id")
        self.assertTrue(kwargs["ignore_duplicates"])

    def test_redelivery_of_the_same_published_message_reuses_the_conflict_key(self):
        """Models Pub/Sub's at-least-once redelivery: the same envelope
        arrives at the consumer twice. Actual duplicate-prevention happens
        in Postgres via the UNIQUE constraint (migrations/001_add_client_msg_id.sql)
        — not verifiable without a real database — but this proves the
        consumer issues the *same* idempotent upsert both times rather than,
        say, generating a fresh id or skipping on_conflict on redelivery.
        """
        chat_handler.send_message("group-1", "user-1", "will be delivered twice")
        envelope = capture_published_message(self.mock_pub_client.publish)

        self.flask_client.post("/", json=envelope)
        self.flask_client.post("/", json=envelope)

        upsert_call = self.mock_supabase.table.return_value.upsert
        self.assertEqual(upsert_call.call_count, 2)
        first_payload = upsert_call.call_args_list[0][0][0]
        second_payload = upsert_call.call_args_list[1][0][0]
        self.assertEqual(first_payload["client_msg_id"], second_payload["client_msg_id"])
        for call in upsert_call.call_args_list:
            self.assertEqual(call.kwargs["on_conflict"], "client_msg_id")
            self.assertTrue(call.kwargs["ignore_duplicates"])

    def test_two_different_messages_in_the_same_group_keep_distinct_ids(self):
        chat_handler.send_message("group-1", "user-1", "first")
        first_envelope = capture_published_message(self.mock_pub_client.publish)

        chat_handler.send_message("group-1", "user-1", "second")
        second_envelope = capture_published_message(self.mock_pub_client.publish)

        self.assertNotEqual(
            first_envelope["message"]["attributes"]["client_msg_id"],
            second_envelope["message"]["attributes"]["client_msg_id"],
        )


if __name__ == "__main__":
    unittest.main()
