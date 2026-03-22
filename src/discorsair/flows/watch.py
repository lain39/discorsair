"""Watch topics flow."""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Callable, Iterable
from concurrent.futures import TimeoutError

from discorsair.storage.sqlite_store import SQLiteStore

from discorsair.discourse.client import DiscourseClient
from discorsair.utils.notify import Notifier, format_notification


def _iter_topics(latest: dict[str, Any]) -> Iterable[dict[str, Any]]:
    topic_list = latest.get("topic_list", {})
    return topic_list.get("topics", [])


def watch(
    client: DiscourseClient,
    store: SQLiteStore | None,
    interval_secs: int,
    once: bool,
    max_posts_per_interval: int | None = None,
    crawl_enabled: bool = True,
    use_unseen: bool = False,
    notifier: Notifier | None = None,
    notify_interval_secs: int = 600,
    notify_auto_mark_read: bool = False,
    timings_per_topic: int = 30,
    on_success: Callable[[], None] | None = None,
    stop_event: Any | None = None,
    schedule_windows: list[str] | None = None,
    timezone_name: str = "Asia/Shanghai",
    sent_notification_ids_mem: set[int] | None = None,
) -> None:
    last_notify_ts = 0.0
    sent_notification_ids_mem = sent_notification_ids_mem if sent_notification_ids_mem is not None else set()
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        if schedule_windows:
            delay = _schedule_delay_secs(schedule_windows, timezone_name)
            if delay > 0:
                logging.getLogger(__name__).info("watch: outside schedule, sleep=%ss", delay)
                _sleep_interruptible(delay, stop_event)
                continue
        remaining = max_posts_per_interval
        now = time.monotonic()
        if notifier and (now - last_notify_ts) >= notify_interval_secs:
            try:
                _poll_notifications(
                    client,
                    store,
                    notifier,
                    sent_notification_ids_mem=sent_notification_ids_mem,
                    auto_mark_read=notify_auto_mark_read,
                )
            except TimeoutError:
                logging.getLogger(__name__).warning("watch: notifications timeout")
            last_notify_ts = time.monotonic()
        try:
            if use_unseen:
                latest = client.get_unseen()
                if not _iter_topics(latest):
                    logging.getLogger(__name__).info("no unseen topics, fallback to latest")
                    latest = client.get_latest()
            else:
                latest = client.get_latest()
        except TimeoutError:
            logging.getLogger(__name__).warning("watch: latest/unseen timeout")
            _sleep_interruptible(max(interval_secs, 1), stop_event)
            continue
        if on_success:
            on_success()
        topics = list(_iter_topics(latest))
        logging.getLogger(__name__).info("watch: topics=%s remaining_posts=%s", len(topics), remaining)
        for topic in topics:
            topic_id = int(topic.get("id", 0))
            if topic_id:
                title = topic.get("title", "")
                replies = topic.get("reply_count", 0)
                views = topic.get("views", 0)
                unseen = topic.get("unseen", False)
                logging.getLogger(__name__).info(
                    "topic=%s replies=%s views=%s unseen=%s title=%s",
                    topic_id,
                    replies,
                    views,
                    unseen,
                    title,
                )
                try:
                    remaining = _touch_topic(client, store, topic, topic_id, remaining, crawl_enabled, timings_per_topic)
                    if store is not None:
                        store.inc_stat("topics_seen", 1)
                except TimeoutError:
                    logging.getLogger(__name__).warning("watch: request timeout, skip topic=%s", topic_id)
                    continue
                if remaining is not None and remaining <= 0:
                    break
        if once:
            _log_watch_summary(store)
            return
        _sleep_interruptible(max(interval_secs, 1), stop_event)
        if stop_event is not None and stop_event.is_set():
            _log_watch_summary(store)
            return


def _touch_topic(
    client: DiscourseClient,
    store: SQLiteStore | None,
    topic_summary: dict[str, Any],
    topic_id: int,
    remaining_posts: int | None,
    crawl_enabled: bool,
    timings_per_topic: int,
) -> int | None:
    topic = client.get_topic(topic_id, track_visit=True, force_load=True)
    post_stream = topic.get("post_stream", {})
    posts = post_stream.get("posts", [])
    stream_ids = post_stream.get("stream", [])

    if posts and crawl_enabled and store is not None:
        store.insert_posts(topic_id, posts)
        store.inc_stat("posts_fetched", len(posts))

    _post_timings(client, store, topic_id, topic_summary, topic, timings_per_topic)

    highest = int(topic.get("highest_post_number", 0) or 0)
    if not crawl_enabled or store is None:
        return remaining_posts

    last_synced_post_number = store.get_last_synced_post_number(topic_id)
    if highest > 0 and highest <= last_synced_post_number:
        store.upsert_topic(
            topic_id=topic_id,
            last_synced_post_number=last_synced_post_number,
            last_stream_len=len(stream_ids) if stream_ids else 0,
            last_seen_at=topic.get("last_posted_at", "") or "",
        )
        return remaining_posts

    if not stream_ids:
        return remaining_posts
    stream_ids = [int(x) for x in stream_ids if isinstance(x, int) or str(x).isdigit()]
    existing = store.get_existing_post_ids(topic_id, stream_ids)
    missing_all = [pid for pid in stream_ids if pid not in existing]
    missing = list(missing_all)
    if remaining_posts is not None:
        missing = missing[: max(remaining_posts, 0)]
    if not missing_all:
        store.upsert_topic(
            topic_id=topic_id,
            last_synced_post_number=highest,
            last_stream_len=len(stream_ids),
            last_seen_at=topic.get("last_posted_at", "") or "",
        )
        return remaining_posts
    if not missing:
        return remaining_posts

    # Fetch missing posts in batches of 20
    batch_size = 20
    for i in range(0, len(missing), batch_size):
        batch = missing[i : i + batch_size]
        data = client.get_posts_by_ids(topic_id, batch)
        posts_data = data.get("post_stream", {}).get("posts", [])
        store.insert_posts(topic_id, posts_data)
        store.inc_stat("posts_fetched", len(posts_data))
        if remaining_posts is not None:
            remaining_posts -= len(batch)

    last_synced_post_number = highest
    store.upsert_topic(
        topic_id=topic_id,
        last_synced_post_number=last_synced_post_number,
        last_stream_len=len(stream_ids),
        last_seen_at=topic.get("last_posted_at", "") or "",
    )
    return remaining_posts


def _post_timings(
    client: DiscourseClient,
    store: SQLiteStore | None,
    topic_id: int,
    topic_summary: dict[str, Any],
    topic: dict[str, Any],
    timings_per_topic: int,
) -> None:
    highest = int(topic.get("highest_post_number", 0) or 0)
    if highest <= 0:
        return
    last_read = int(topic_summary.get("last_read_post_number", 0) or 0)
    if last_read >= highest:
        logging.getLogger(__name__).info("timings: topic=%s already_read=%s", topic_id, last_read)
        return

    remaining = max(1, timings_per_topic)
    while remaining > 0:
        next_posts: list[int] = []
        start = last_read + 1
        if start > highest:
            return
        batch_count = min(5, remaining)
        end = min(highest, start + batch_count - 1)
        for post_number in range(start, end + 1):
            next_posts.append(post_number)
        if not next_posts:
            return

        ms = random.randint(2000, 5000)
        logging.getLogger(__name__).info("timings: topic=%s posts=%s-%s ms=%s", topic_id, start, end, ms)
        timings = {post_number: ms for post_number in next_posts}
        client.post_timings(topic_id=topic_id, timings=timings, topic_time=ms)
        if store is not None:
            store.inc_stat("timings_sent", 1)
        last_read = next_posts[-1]
        remaining -= len(next_posts)


def _poll_notifications(
    client: DiscourseClient,
    store: SQLiteStore | None,
    notifier: Notifier,
    *,
    sent_notification_ids_mem: set[int] | None = None,
    auto_mark_read: bool = False,
) -> None:
    data = client.get_notifications(limit=30, recent=True)
    items = data.get("notifications", [])
    if not items:
        return
    unread = [item for item in items if item.get("read") is False] 
    if not unread:
        return
    ids = [int(item.get("id", 0) or 0) for item in unread]
    if store is not None:
        sent = store.get_sent_notification_ids(ids)
    else:
        sent = {notification_id for notification_id in ids if sent_notification_ids_mem and notification_id in sent_notification_ids_mem}
    to_send = [item for item in unread if int(item.get("id", 0) or 0) not in sent]
    sent_items: list[dict[str, Any]] = []
    for item in to_send:
        msg = format_notification(item)
        if notifier.send(msg):
            sent_items.append(item)
    if store is not None:
        if sent_items:
            store.mark_notifications_sent(sent_items)
            store.inc_stat("notifications_sent", len(sent_items))
    elif sent_notification_ids_mem is not None:
        if sent_items:
            sent_notification_ids_mem.update(int(item.get("id", 0) or 0) for item in sent_items)
    if not auto_mark_read:
        return
    sent_after = set(sent)
    sent_after.update(int(item.get("id", 0) or 0) for item in sent_items)
    if all(notification_id in sent_after for notification_id in ids):
        client.mark_notifications_read()


def _sleep_interruptible(seconds: float, stop_event: Any | None) -> None:
    if seconds <= 0:
        return
    if stop_event is None:
        time.sleep(seconds)
        return
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if stop_event.is_set():
            return
        time.sleep(1)


def _log_watch_summary(store: SQLiteStore | None) -> None:
    if store is None:
        return
    stats = store.get_stats_today()
    logging.getLogger(__name__).info(
        "watch summary: topics=%s posts=%s timings=%s notifications=%s",
        stats.get("topics_seen", 0),
        stats.get("posts_fetched", 0),
        stats.get("timings_sent", 0),
        stats.get("notifications_sent", 0),
    )


def _schedule_delay_secs(windows: list[str], timezone_name: str) -> int:
    now = datetime.now(ZoneInfo(timezone_name))
    mins_now = now.hour * 60 + now.minute
    ranges: list[tuple[int, int]] = []
    for w in windows:
        if "-" not in w:
            continue
        start_s, end_s = w.split("-", 1)
        try:
            sh, sm = [int(x) for x in start_s.split(":")]
            eh, em = [int(x) for x in end_s.split(":")]
        except ValueError:
            continue
        start = sh * 60 + sm
        end = eh * 60 + em
        ranges.append((start, end))
    # Normalize cross-midnight windows (e.g., 23:00-02:00) into two ranges
    normalized: list[tuple[int, int]] = []
    for start, end in ranges:
        if end >= start:
            normalized.append((start, end))
        else:
            normalized.append((start, 24 * 60 - 1))
            normalized.append((0, end))
    ranges = normalized
    for start, end in ranges:
        if start <= mins_now <= end:
            return 0
    future_starts = [start for start, _ in ranges if start > mins_now]
    if future_starts:
        next_start = min(future_starts)
        return (next_start - mins_now) * 60
    if ranges:
        next_start = min(r[0] for r in ranges)
        return ((24 * 60 - mins_now) + next_start) * 60
    return 0
