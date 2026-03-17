"""Auto-like flow."""

from __future__ import annotations

from discorsair.discourse.client import DiscourseClient


def like(client: DiscourseClient, post_id: int, emoji: str) -> None:
    result = client.toggle_reaction(post_id, emoji)
    print(f"like ok: post_id={post_id} emoji={emoji} status={result.get('current_user_reaction')}")
