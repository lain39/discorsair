"""Discourse endpoints registry."""

from __future__ import annotations


def latest() -> str:
    return "/latest.json"


def unseen() -> str:
    return "/unseen.json"


def notifications() -> str:
    return "/notifications"


def csrf() -> str:
    return "/session/csrf"


def timings() -> str:
    return "/topics/timings"


def reactions(post_id: int, emoji: str) -> str:
    return f"/discourse-reactions/posts/{post_id}/custom-reactions/{emoji}/toggle.json"


def create_post() -> str:
    return "/posts"


def topic_json(topic_id: int) -> str:
    return f"/t/{topic_id}.json"


def topic_posts(topic_id: int) -> str:
    return f"/t/{topic_id}/posts.json"
