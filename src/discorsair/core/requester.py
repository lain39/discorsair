"""Request engine: curl_cffi primary, FlareSolverr fallback on challenge."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional
import threading
from urllib.parse import urlencode, urljoin, urlparse
import time

from curl_cffi import requests

from discorsair.core.cookies import cookies_to_header, merge_cookies, parse_cookie_header
from discorsair.core.session import SessionState
from discorsair.utils.ua_map import get_default_ua

_LOG = logging.getLogger(__name__)


@dataclass
class ResponseData:
    status: int
    headers: Dict[str, str]
    text: str

    def json(self) -> Any:
        return json.loads(self.text)


_CF_CHALLENGE_RE = re.compile(
    r"/cdn-cgi/.*?(challenge|l/chk_jschl|l/chk_captcha|l/orchestrate)"
    r"|__cf_chl_(?:opt|f_tk|rtk|cpt|jschl|captcha)"
    r"|challenges\.cloudflare\.com"
    r"|just a moment|checking your browser"
    r"|error code: 1020",
    re.IGNORECASE,
)
_HTML_HINT_RE = re.compile(r"<\s*(html|head|title)\b", re.IGNORECASE)
_JSON_ERR_RE = re.compile(r'"error_type"\s*:|bad csrf', re.IGNORECASE)
_LEADING_WS_RE = re.compile(r"^\s*")
_CF_REDIRECT_RE = re.compile(r"/cdn-cgi/|challenges\.cloudflare\.com", re.IGNORECASE)

def _is_cloudflare_challenge(status: int, headers: Dict[str, str], text: str) -> bool:
    if status == 302:
        location = ""
        for key, value in headers.items():
            if str(key).lower() == "location":
                location = str(value)
                break
        if location and _CF_REDIRECT_RE.search(location):
            return True
        return False
    if status not in (403, 503, 429):
        return False

    content_type = ""
    for key, value in headers.items():
        if str(key).lower() == "content-type":
            content_type = str(value)
            break

    body = text or ""
    ct_l = content_type.lower()

    if "application/json" in ct_l or "text/json" in ct_l:
        return False

    m = _LEADING_WS_RE.match(body)
    start = m.end() if m else 0

    if start < len(body) and body[start] == "{":
        if _JSON_ERR_RE.search(body[start : start + 2048]):
            return False

    if "text/html" not in ct_l and _HTML_HINT_RE.search(body[start : start + 1024]) is None:
        return False

    return _CF_CHALLENGE_RE.search(body) is not None


def _proxy_key(proxy: str | None) -> str:
    if not proxy:
        return "direct"
    parsed = urlparse(proxy)
    host = parsed.hostname or ""
    port = parsed.port or ""
    return f"{host}:{port}".strip(":")


def _translate_proxy_for_flaresolverr(proxy: str | None) -> str | None:
    if not proxy:
        return None
    parsed = urlparse(proxy)
    host = parsed.hostname or ""
    if host in {"127.0.0.1", "localhost", "0.0.0.0"}:
        host = "host.docker.internal"
        rebuilt = parsed._replace(netloc=f"{host}:{parsed.port}")
        return rebuilt.geturl()
    return proxy


class Requester:
    def __init__(
        self,
        session: SessionState,
        flaresolverr_base_url: str | None,
        flaresolverr_timeout_secs: int,
        ua_probe_url: str | None,
        debug: bool = False,
        min_interval_secs: float = 0.0,
        max_retries: int = 2,
        timeout_secs: float = 30,
    ):
        self._session = session
        self._flaresolverr_base_url = flaresolverr_base_url
        self._flaresolverr_timeout_secs = flaresolverr_timeout_secs
        self._ua_probe_url = ua_probe_url
        self._debug = debug
        self._min_interval_secs = max(float(min_interval_secs), 0.0)
        self._max_retries = max(int(max_retries), 1)
        self._timeout_secs = max(float(timeout_secs), 1.0)
        self._lock = threading.Lock()
        if not self._session.cookies:
            self._session.cookies = parse_cookie_header(self._session.cookie_header)

    def _ensure_user_agent(self, ua_probe_url: str | None) -> str:
        if self._session.user_agent:
            return self._session.user_agent
        default_ua = get_default_ua(self._session.impersonate_target)
        if default_ua:
            self._session.user_agent = default_ua
            return default_ua
        probe_url = ua_probe_url or "data:,"
        if probe_url and self._flaresolverr_base_url:
            solution = self._flaresolverr_request("get", probe_url, headers={})
            ua = solution.get("userAgent", "")
            if ua:
                self._session.user_agent = ua
                return ua
        return ""

    def request(
        self,
        method: str,
        path_or_url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, str]] = None,
        data: Optional[str] = None,
        json_body: Optional[Dict[str, Any]] = None,
        ua_probe_url: Optional[str] = None,
        allow_fallback: bool = True,
    ) -> ResponseData:
        url = path_or_url
        if not url.startswith("http"):
            url = urljoin(self._session.base_url, path_or_url)

        attempts = self._max_retries
        last_exc: Exception | None = None
        response: ResponseData | None = None
        with self._lock:
            for attempt in range(attempts):
                self._throttle()

                user_agent = self._ensure_user_agent(ua_probe_url or self._ua_probe_url)
                req_headers = dict(headers or {})
                if user_agent and "User-Agent" not in req_headers:
                    req_headers["User-Agent"] = user_agent

                cookies = dict(self._session.cookies)
                cf_key = _proxy_key(self._session.proxy)
                cached_cf = self._session.cf_clearance_cache.get(cf_key)
                if cached_cf and "cf_clearance" not in cookies:
                    cookies["cf_clearance"] = cached_cf

                proxies = None
                if self._session.proxy:
                    proxies = {"http": self._session.proxy, "https": self._session.proxy}

                _LOG.info("request: %s %s via curl_cffi", method.upper(), url)
                if self._debug:
                    _LOG.debug("request headers: %s", _redact_headers(req_headers))
                    if params:
                        _LOG.debug("request params: %s", params)
                try:
                    resp = requests.request(
                        method=method,
                        url=url,
                        headers=req_headers,
                        params=params,
                        data=data,
                        json=json_body,
                        cookies=cookies,
                        impersonate=self._session.impersonate_target,
                        proxies=proxies,
                        timeout=self._timeout_secs,
                    )
                    response = ResponseData(status=resp.status_code, headers=dict(resp.headers), text=resp.text)
                    if resp.cookies:
                        merged = dict(self._session.cookies)
                        merged.update(resp.cookies)
                        self._session.cookies = merged
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    self._session.last_response_ok = False
                    _LOG.warning("request failed (%s/%s): %s", attempt + 1, attempts, exc)
                    self._backoff(attempt)
                    continue

                if self._debug:
                    _LOG.debug("response status: %s", response.status)
                    _LOG.debug("response headers: %s", _redact_headers(response.headers))
                    _LOG.debug("response body (first 500 chars): %s", response.text[:500])

                if allow_fallback and _is_cloudflare_challenge(response.status, response.headers, response.text):
                    if not self._flaresolverr_base_url:
                        return response
                    _LOG.info("challenge detected, solving via FlareSolverr and retrying")
                    self._solve_challenge()
                    resp = requests.request(
                        method=method,
                        url=url,
                        headers=req_headers,
                        params=params,
                        data=data,
                        json=json_body,
                        cookies=self._session.cookies,
                        impersonate=self._session.impersonate_target,
                        proxies=proxies,
                        timeout=self._timeout_secs,
                    )
                    response = ResponseData(status=resp.status_code, headers=dict(resp.headers), text=resp.text)
                    if resp.cookies:
                        merged = dict(self._session.cookies)
                        merged.update(resp.cookies)
                        self._session.cookies = merged
                    if self._debug:
                        _LOG.debug("retry status: %s", response.status)
                        _LOG.debug("retry headers: %s", _redact_headers(response.headers))
                        _LOG.debug("retry body (first 500 chars): %s", response.text[:500])
                    if _is_cloudflare_challenge(response.status, response.headers, response.text):
                        _LOG.warning("challenge still present after solve")
                        self._backoff(attempt)
                        continue

                if response.status >= 500 or response.status == 429:
                    _LOG.warning("retryable status (%s/%s): %s", attempt + 1, attempts, response.status)
                    self._session.last_response_ok = False
                    self._backoff(attempt)
                    continue
                self._session.last_response_ok = response.status < 400
                return response

            if last_exc:
                raise last_exc
            if response is None:
                raise RuntimeError("request failed without response")
            return response

    def _backoff(self, attempt: int) -> None:
        delay = min(2 ** attempt, 10)
        time.sleep(delay)

    def get_cookie_header(self) -> str:
        return cookies_to_header(self._session.cookies)

    def last_response_ok(self) -> bool | None:
        return self._session.last_response_ok

    def _throttle(self) -> None:
        if self._min_interval_secs <= 0:
            return
        now = time.monotonic()
        elapsed = now - self._session.last_request_ts
        if elapsed < self._min_interval_secs:
            time.sleep(self._min_interval_secs - elapsed)
        self._session.last_request_ts = time.monotonic()

    def _flaresolverr_request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        params: Optional[Dict[str, str]] = None,
        data: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self._flaresolverr_base_url:
            raise RuntimeError("FlareSolverr not configured")

        flaresolverr_proxy = _translate_proxy_for_flaresolverr(self._session.proxy)

        payload: Dict[str, Any] = {
            "cmd": f"request.{method.lower()}",
            "url": url,
            "maxTimeout": int(self._flaresolverr_timeout_secs * 1000),
            "headers": headers,
        }
        if self._session.cookies:
            payload["cookies"] = [{"name": k, "value": v} for k, v in self._session.cookies.items()]
        if params:
            payload["url"] = url + "?" + urlencode(params, doseq=True)
        if data is not None:
            payload["postData"] = data
        if flaresolverr_proxy:
            payload["proxy"] = flaresolverr_proxy
        if self._session.user_agent:
            payload["userAgent"] = self._session.user_agent

        fs_url = urljoin(self._flaresolverr_base_url.rstrip("/") + "/", "v1")
        _LOG.info("request: %s via FlareSolverr", fs_url)
        if self._debug:
            _LOG.debug("flaresolverr payload (redacted): %s", _redact_headers(payload))
        fs_resp = requests.post(fs_url, json=payload, timeout=self._flaresolverr_timeout_secs)
        data_json = fs_resp.json()
        if data_json.get("status") != "ok":
            raise RuntimeError(f"FlareSolverr error: {data_json}")

        solution = data_json.get("solution", {})
        cookies_list = solution.get("cookies", [])
        merged = dict(self._session.cookies)
        for item in cookies_list:
            name = item.get("name")
            value = item.get("value")
            if name and value:
                merged[name] = value
                if name == "cf_clearance":
                    self._session.cf_clearance_cache[_proxy_key(self._session.proxy)] = value
        self._session.cookies = merged
        return solution

    def _solve_challenge(self) -> None:
        if not self._flaresolverr_base_url:
            return
        base = self._session.base_url
        if not base:
            raise RuntimeError("base_url is required to solve challenge")
        self._flaresolverr_request("get", base, headers={})


def _redact_headers(headers: Dict[str, Any]) -> Dict[str, Any]:
    redacted = {}
    for key, value in headers.items():
        lower = str(key).lower()
        if lower in {"cookie", "set-cookie", "x-csrf-token", "authorization"}:
            redacted[key] = "***"
        else:
            redacted[key] = value
    return redacted
