"""Daily activity flow."""

from __future__ import annotations

from typing import Any

from discorsair.discourse.client import DiscourseClient


def _pick_unseen_topic(latest: dict[str, Any]) -> int | None:
    topics = latest.get("topic_list", {}).get("topics", [])
    for topic in topics:
        if topic.get("unseen") is True:
            return int(topic.get("id", 0)) or None
    if topics:
        return int(topics[0].get("id", 0)) or None
    return None


def daily(client: DiscourseClient, topic_id: int | None) -> dict[str, Any]:
    if topic_id is None:
        latest = client.get_latest()
        topic_id = _pick_unseen_topic(latest)
    if not topic_id:
        return {"ok": False, "topic_id": None, "reason": "no_topic_found"}
    client.post_timings(topic_id=topic_id, timings={1: 1000}, topic_time=1000)
    return {"ok": True, "topic_id": topic_id}
