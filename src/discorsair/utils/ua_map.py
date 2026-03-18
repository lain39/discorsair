"""UA mapping for impersonate targets."""

from __future__ import annotations

import re
from typing import Dict


_UA_MAP: Dict[str, str] = {
    "chrome110": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
    "chrome120": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


def get_default_ua(impersonate_target: str) -> str:
    return _UA_MAP.get(impersonate_target, "")


def _available_impersonate_targets() -> set[str]:
    targets = set(_UA_MAP.keys())
    try:
        from curl_cffi.requests import BrowserType

        targets.update(member.value for member in BrowserType)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        pass
    return targets


def _pick_best_version(available: set[str], pattern: str, target_version: int) -> str:
    matches: list[tuple[int, str]] = []
    compiled = re.compile(pattern)
    for value in available:
        match = compiled.match(value)
        if not match:
            continue
        matches.append((int(match.group(1)), value))
    if not matches:
        return ""
    exact = [value for version, value in matches if version == target_version]
    if exact:
        return sorted(exact, key=len)[0]
    lower_or_equal = [item for item in matches if item[0] <= target_version]
    if lower_or_equal:
        return max(lower_or_equal, key=lambda item: item[0])[1]
    return min(matches, key=lambda item: item[0])[1]


def infer_impersonate_target_from_ua(user_agent: str) -> str:
    ua = (user_agent or "").strip()
    if not ua:
        return ""

    available = _available_impersonate_targets()
    if not available:
        return ""

    if " Edg/" in ua:
        match = re.search(r"Edg/(\d+)", ua)
        if match:
            return _pick_best_version(available, r"^edge(\d+)$", int(match.group(1)))

    is_android = " Android " in ua
    if " Chrome/" in ua and " Edg/" not in ua:
        match = re.search(r"Chrome/(\d+)", ua)
        if match:
            if is_android:
                return _pick_best_version(available, r"^chrome(\d+)_android$", int(match.group(1)))
            return _pick_best_version(available, r"^chrome(\d+)[a-z]*$", int(match.group(1)))

    if " Firefox/" in ua:
        match = re.search(r"Firefox/(\d+)", ua)
        if match:
            return _pick_best_version(available, r"^firefox(\d+)$", int(match.group(1)))

    if "Safari/" in ua and "Chrome/" not in ua and "Chromium/" not in ua and "Edg/" not in ua:
        match = re.search(r"Version/(\d+)(?:\.(\d+))?", ua)
        if match:
            major = int(match.group(1))
            minor = int(match.group(2) or "0")
            condensed = int(f"{major}{minor}")
            ios = " iPhone " in ua or " iPad " in ua
            if ios:
                return _pick_best_version(available, r"^safari(\d+)(?:_ios)?$", condensed)
            return _pick_best_version(available, r"^safari(\d+)$", condensed)

    return ""
