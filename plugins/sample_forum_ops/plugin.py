"""Example plugin for topic prioritization, reply, and like automation."""

from __future__ import annotations


def _text(value) -> str:
    return str(value or "").strip()


def _contains_any(text: str, keywords: list[str]) -> bool:
    haystack = _text(text).lower()
    if not haystack:
        return False
    for keyword in keywords:
        needle = _text(keyword).lower()
        if needle and needle in haystack:
            return True
    return False


class Plugin:
    def on_topics_fetched(self, ctx, event) -> None:
        priority_keywords = list(ctx.config.get("priority_title_keywords", []))
        skip_keywords = list(ctx.config.get("skip_title_keywords", []))
        for topic in event.topics:
            topic_id = int(topic.get("id", 0) or 0)
            title = _text(topic.get("title"))
            if topic_id <= 0:
                continue
            if _contains_any(title, skip_keywords):
                ctx.skip_topic(topic_id, "matched skip_title_keywords")
                continue
            if _contains_any(title, priority_keywords):
                ctx.prioritize_topic(topic_id, 100)

    def on_topic_after_enter(self, ctx, event) -> None:
        if _text(ctx.config.get("reply_mode", "after_crawl")) != "after_enter":
            return
        self._reply_if_matched(ctx, event.topic_summary, posts=[])

    def on_topic_after_crawl(self, ctx, event) -> None:
        if _text(ctx.config.get("reply_mode", "after_crawl")) != "after_crawl":
            return
        self._reply_if_matched(ctx, event.topic_summary, posts=list(event.posts))

    def on_post_fetched(self, ctx, event) -> None:
        post = event.post
        cooked = _text(post.get("cooked"))
        if not cooked:
            return
        min_like_count = int(ctx.config.get("min_like_count", 10) or 0)
        if int(post.get("like_count", 0) or 0) < min_like_count:
            return
        ctx.like(post)

    def _reply_if_matched(self, ctx, topic_summary, posts: list[dict]) -> None:
        topic_id = int(topic_summary.get("id", 0) or 0)
        if topic_id <= 0:
            return
        reply_text = _text(ctx.config.get("reply_text"))
        if not reply_text:
            return
        title = _text(topic_summary.get("title"))
        title_keywords = list(ctx.config.get("reply_title_keywords", []))
        if title_keywords and not _contains_any(title, title_keywords):
            return
        post_keywords = list(ctx.config.get("reply_post_keywords", []))
        if posts and post_keywords:
            post_text = "\n".join(_text(post.get("cooked")) for post in posts)
            if not _contains_any(post_text, post_keywords):
                return
        once_key = f"reply:{topic_id}:{_text(ctx.config.get('reply_mode', 'after_crawl'))}"
        ctx.reply(topic_id, reply_text, once_key=once_key)


def create_plugin():
    return Plugin()
