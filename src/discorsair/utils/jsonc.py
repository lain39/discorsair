"""JSONC helpers."""

from __future__ import annotations

import json
from typing import Any


def loads(text: str) -> dict[str, Any]:
    return json.loads(strip_comments(text))


def strip_comments(text: str) -> str:
    out: list[str] = []
    i = 0
    in_string = False
    escaped = False
    while i < len(text):
        char = text[i]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            i += 1
            continue
        if char == "/" and i + 1 < len(text):
            marker = text[i + 1]
            if marker == "/":
                i = _consume_line_comment(text, i)
                continue
            if marker == "*":
                i = _consume_block_comment(text, i)
                continue
        out.append(char)
        i += 1
    return "".join(out)


def _consume_line_comment(text: str, start: int) -> int:
    i = start + 2
    while i < len(text) and text[i] != "\n":
        i += 1
    return i


def _consume_block_comment(text: str, start: int) -> int:
    i = start + 2
    while i < len(text):
        if i + 1 < len(text) and text[i] == "*" and text[i + 1] == "/":
            return i + 2
        i += 1
    raise ValueError("unterminated block comment")
