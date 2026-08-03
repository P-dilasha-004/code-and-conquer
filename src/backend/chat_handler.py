import os
import streamlit as st
from supabase import create_client, Client

from backend.chat_publisher import publish_message

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Missing SUPABASE_URL or SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def create_group_chat(group_id: str, group_name: str, user_id: str) -> None:
    """Create a Supabase chat room for a group and add the creator as the
    first member. Safe to call more than once for the same `group_id` — any
    pre-existing chat row or membership row is left untouched.
    """
    try:
        existing_chat = (
            supabase.table("group_chats")
            .select("group_id")
            .eq("group_id", group_id)
            .limit(1)
            .execute()
        )

        if not existing_chat.data:
            supabase.table("group_chats").insert({
                "group_id": group_id,
                "group_name": group_name,
            }).execute()

        add_user_to_chat(group_id, user_id)

    except Exception as e:
        print(f"Supabase create_group_chat failed: {e}")
        raise e


def add_user_to_chat(group_id: str, user_id: str) -> None:
    """Add a member to an existing group chat. No-op if the user is already
    a member, so calling this from `join_group` is always safe.
    """
    try:
        existing_member = (
            supabase.table("group_chat_members")
            .select("group_id, user_id")
            .eq("group_id", group_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )

        if existing_member.data:
            return

        supabase.table("group_chat_members").insert({
            "group_id": group_id,
            "user_id": user_id,
        }).execute()

    except Exception as e:
        print(f"Supabase add_user_to_chat failed: {e}")
        raise e


def send_message(group_id: str, sender_id: str, content: str) -> str:
    """Publish a new message onto the chat-messages Pub/Sub topic instead of
    writing to Supabase directly. persist_worker.py (src/worker/) is the
    only thing that writes to the `messages` table now — this function just
    hands the message to the broker and returns.

    Returns the generated client_msg_id (useful for correlating this send
    with the row persist_worker eventually writes).
    """
    try:
        return publish_message(group_id, sender_id, content)

    except Exception as e:
        print(f"Pub/Sub publish_message failed: {e}")
        raise e


def get_messages(group_id: str) -> list:
    """Fetch every message for a group, ordered oldest → newest."""
    try:
        res = (
            supabase.table("messages")
            .select("*")
            .eq("group_id", group_id)
            .order("created_at", desc=False)
            .execute()
        )
        return res.data or []

    except Exception as e:
        print(f"Supabase get_messages failed: {e}")
        return []


def get_new_messages(group_id: str, after_created_at: str | None) -> list:
    """Fetch only messages newer than `after_created_at` (ISO timestamp from
    Supabase). Used by the group-chat page to poll for new messages without
    re-downloading the whole history on every tick. Pass `None` to get the
    full history.
    """
    try:
        query = (
            supabase.table("messages")
            .select("*")
            .eq("group_id", group_id)
        )

        if after_created_at:
            query = query.gt("created_at", after_created_at)

        res = query.order("created_at", desc=False).execute()
        return res.data or []

    except Exception as e:
        print(f"Supabase get_new_messages failed: {e}")
        return []
