#############################################################################
# persist_worker_test.py
#
# Unit tests for worker/persist_worker.py — the Pub/Sub PUSH endpoint that
# durably persists chat messages. Uses Flask's test client to POST
# Pub/Sub-shaped envelopes directly, and mocks the Supabase client, so
# these run anywhere with no live Pub/Sub or Cloud Run needed.
#############################################################################

import base64
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR / "worker"))

# create_client() resolves at import time — needs a syntactically valid URL
# even though nothing here talks to a real Supabase project.
os.environ.setdefault("SUPABASE_URL", "https://fake-project.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "fake-anon-key")

import persist_worker  # noqa: E402


def push_envelope(attributes: dict, data: str = "hello") -> dict:
    """Shape Pub/Sub actually POSTs to a push endpoint: base64-encoded data,
    attributes alongside it, wrapped in a `message` envelope."""
    return {
        "message": {
            "data": base64.b64encode(data.encode("utf-8")).decode("utf-8"),
            "attributes": attributes,
            "messageId": "test-message-id",
        },
        "subscription": "projects/test-project/subscriptions/chat-messages-persist-sub",
    }


class TestPersist(unittest.TestCase):

    def setUp(self):
        self.mock_supabase = MagicMock()
        patcher = patch.object(persist_worker, "supabase", self.mock_supabase)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_upserts_on_the_client_msg_id_conflict_target(self):
        """This is what actually makes redelivery idempotent — without the
        matching UNIQUE constraint from migrations/001_add_client_msg_id.sql,
        this upsert call has nothing to deduplicate against."""
        persist_worker.persist("group-1", "user-1", "hello", "cmid-123")

        self.mock_supabase.table.assert_called_with("messages")
        upsert_call = self.mock_supabase.table.return_value.upsert
        _, kwargs = upsert_call.call_args
        self.assertEqual(kwargs["on_conflict"], "client_msg_id")
        self.assertTrue(kwargs["ignore_duplicates"])

    def test_upsert_payload_contains_all_fields(self):
        persist_worker.persist("group-1", "user-9", "hi there", "cmid-abc")

        upsert_call = self.mock_supabase.table.return_value.upsert
        (payload,), _ = upsert_call.call_args
        self.assertEqual(payload["group_id"], "group-1")
        self.assertEqual(payload["sender_id"], "user-9")
        self.assertEqual(payload["content"], "hi there")
        self.assertEqual(payload["client_msg_id"], "cmid-abc")


class TestHandlePush(unittest.TestCase):
    """PUBSUB_PUSH_SERVICE_ACCOUNT is left unset for these (matches local/
    dev mode) so verify_push_request short-circuits to True — auth
    verification itself is covered separately in TestVerifyPushRequest.
    """

    def setUp(self):
        self.mock_supabase = MagicMock()
        supabase_patcher = patch.object(persist_worker, "supabase", self.mock_supabase)
        supabase_patcher.start()
        self.addCleanup(supabase_patcher.stop)

        service_account_patcher = patch.object(persist_worker, "PUBSUB_PUSH_SERVICE_ACCOUNT", None)
        service_account_patcher.start()
        self.addCleanup(service_account_patcher.stop)

        persist_worker.app.testing = True
        self.client = persist_worker.app.test_client()

    def test_persists_a_well_formed_push_and_returns_200(self):
        envelope = push_envelope(
            {"group_id": "group-1", "sender_id": "user-1", "client_msg_id": "cmid-1"},
            data="hello",
        )
        resp = self.client.post("/", json=envelope)

        self.assertEqual(resp.status_code, 200)
        self.mock_supabase.table.return_value.upsert.assert_called_once()

    def test_decodes_base64_message_data_as_the_content(self):
        envelope = push_envelope(
            {"group_id": "group-1", "sender_id": "user-1", "client_msg_id": "cmid-1"},
            data="hello world",
        )
        self.client.post("/", json=envelope)

        upsert_call = self.mock_supabase.table.return_value.upsert
        (payload,), _ = upsert_call.call_args
        self.assertEqual(payload["content"], "hello world")

    def test_drops_a_message_missing_required_attributes_but_still_acks(self):
        """Retrying can't fix a missing attribute — returning 200 tells
        Pub/Sub not to redeliver, instead of looping it toward the
        dead-letter topic for a bug that redelivery will never resolve."""
        envelope = push_envelope({"group_id": "group-1"})  # missing sender_id, client_msg_id
        resp = self.client.post("/", json=envelope)

        self.assertEqual(resp.status_code, 200)
        self.mock_supabase.table.return_value.upsert.assert_not_called()

    def test_returns_500_on_a_transient_persistence_failure(self):
        """Non-2xx is what makes Pub/Sub redeliver this push later."""
        self.mock_supabase.table.return_value.upsert.return_value.execute.side_effect = RuntimeError("db down")
        envelope = push_envelope({"group_id": "group-1", "sender_id": "user-1", "client_msg_id": "cmid-1"})

        resp = self.client.post("/", json=envelope)

        self.assertEqual(resp.status_code, 500)

    def test_rejects_a_request_with_no_message_field(self):
        """Not a real Pub/Sub push — 400, not 200, so nothing gets treated
        as successfully delivered."""
        resp = self.client.post("/", json={"not": "a pubsub envelope"})
        self.assertEqual(resp.status_code, 400)


class TestVerifyPushRequest(unittest.TestCase):

    def test_skips_verification_when_no_service_account_configured(self):
        with patch.object(persist_worker, "PUBSUB_PUSH_SERVICE_ACCOUNT", None):
            fake_request = MagicMock()
            fake_request.headers = {}
            self.assertTrue(persist_worker.verify_push_request(fake_request))

    def test_rejects_missing_authorization_header(self):
        with patch.object(persist_worker, "PUBSUB_PUSH_SERVICE_ACCOUNT", "pubsub-push@test-project.iam.gserviceaccount.com"):
            fake_request = MagicMock()
            fake_request.headers = {}
            self.assertFalse(persist_worker.verify_push_request(fake_request))

    def test_accepts_a_token_whose_email_claim_matches(self):
        with patch.object(persist_worker, "PUBSUB_PUSH_SERVICE_ACCOUNT", "pubsub-push@test-project.iam.gserviceaccount.com"):
            fake_request = MagicMock()
            fake_request.headers = {"Authorization": "Bearer faketoken"}
            with patch.object(
                persist_worker.id_token, "verify_oauth2_token",
                return_value={"email": "pubsub-push@test-project.iam.gserviceaccount.com"},
            ):
                self.assertTrue(persist_worker.verify_push_request(fake_request))

    def test_rejects_a_token_whose_email_claim_does_not_match(self):
        """This is the actual security property: without this check, any
        validly-signed Google token — not just the one this subscription
        uses — would be accepted."""
        with patch.object(persist_worker, "PUBSUB_PUSH_SERVICE_ACCOUNT", "pubsub-push@test-project.iam.gserviceaccount.com"):
            fake_request = MagicMock()
            fake_request.headers = {"Authorization": "Bearer faketoken"}
            with patch.object(
                persist_worker.id_token, "verify_oauth2_token",
                return_value={"email": "someone-else@another-project.iam.gserviceaccount.com"},
            ):
                self.assertFalse(persist_worker.verify_push_request(fake_request))

    def test_rejects_an_invalid_token(self):
        with patch.object(persist_worker, "PUBSUB_PUSH_SERVICE_ACCOUNT", "pubsub-push@test-project.iam.gserviceaccount.com"):
            fake_request = MagicMock()
            fake_request.headers = {"Authorization": "Bearer garbage"}
            with patch.object(
                persist_worker.id_token, "verify_oauth2_token",
                side_effect=ValueError("invalid token"),
            ):
                self.assertFalse(persist_worker.verify_push_request(fake_request))


if __name__ == "__main__":
    unittest.main()
