"""Auto-like flow."""

from __future__ import annotations

from typing import Any

from discorsair.discourse.client import DiscourseClient


def like(client: DiscourseClient, post_id: int, emoji: str) -> dict[str, Any]:
    result = client.toggle_reaction(post_id, emoji)
    return {
        "ok": True,
        "post_id": post_id,
        "emoji": emoji,
        "current_user_reaction": result.get("current_user_reaction"),
        "result": result,
    }
