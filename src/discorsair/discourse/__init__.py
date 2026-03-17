"""Discourse API client."""

from discorsair.discourse.client import DiscourseClient
from discorsair.discourse.queued_client import QueuedDiscourseClient

__all__ = [
    "DiscourseClient",
    "QueuedDiscourseClient",
]
