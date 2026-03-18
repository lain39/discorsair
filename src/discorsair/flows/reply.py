"""Auto-reply flow."""

from __future__ import annotations

from typing import Any

from discorsair.discourse.client import DiscourseClient


def reply(client: DiscourseClient, topic_id: int, raw: str, category: int | None) -> dict[str, Any]:
    result = client.reply(topic_id=topic_id, raw=raw, category=category)
    post = result.get("post", {})
    return {
        "ok": True,
        "topic_id": topic_id,
        "post_id": post.get("id"),
        "category": category,
        "result": result,
    }
