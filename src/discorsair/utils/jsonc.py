"""JSONC loader (supports // comments)."""

from __future__ import annotations

import json
import re
from typing import Any


_COMMENT_RE = re.compile(r"(^|\s)//.*$", re.MULTILINE)
_BLOCK_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def loads(text: str) -> dict[str, Any]:
    stripped = re.sub(_BLOCK_RE, "", text)
    stripped = re.sub(_COMMENT_RE, "", stripped)
    return json.loads(stripped)
