"""Auto-reply flow."""

from __future__ import annotations

from discorsair.discourse.client import DiscourseClient


def reply(client: DiscourseClient, topic_id: int, raw: str, category: int | None) -> None:
    result = client.reply(topic_id=topic_id, raw=raw, category=category)
    post = result.get("post", {})
    print(f"reply ok: topic_id={topic_id} post_id={post.get('id')}")
