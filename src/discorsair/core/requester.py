"""Request engine: curl_cffi primary, FlareSolverr fallback on challenge."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional
import threading
from urllib.parse import unquote, urlencode, urljoin, urlparse
import time

from curl_cffi import requests

from discorsair.core.cookies import cookies_to_header, parse_cookie_header
from discorsair.core.session import SessionState
from discorsair.utils.ua_map import get_default_ua, infer_impersonate_target_from_ua

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
_CSRF_META_RE = re.compile(
    r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_MAX_RETRY_DELAY_SECS = 300


def _normalize_header_key(name: str) -> str:
    return str(name).strip().lower()


def _merge_headers(defaults: Dict[str, str], override: Optional[Dict[str, str]]) -> Dict[str, str]:
    if not override:
        return dict(defaults)
    out = dict(defaults)
    for key, value in override.items():
        norm = _normalize_header_key(key)
        for existing in list(out.keys()):
            if _normalize_header_key(existing) == norm:
                del out[existing]
        out[key] = value
    return out


def _has_header(headers: Dict[str, str], name: str) -> bool:
    target = _normalize_header_key(name)
    return any(_normalize_header_key(key) == target for key in headers)


def _mask_secret(value: str, *, keep_prefix: int = 8, keep_suffix: int = 6) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep_prefix + keep_suffix + 3:
        return "***"
    return f"{text[:keep_prefix]}...{text[-keep_suffix:]}"


def _summarize_cookies(cookies: Dict[str, str]) -> Dict[str, Any]:
    names = sorted(cookies.keys())
    summary: Dict[str, Any] = {
        "count": len(cookies),
        "names": names,
    }
    cf_clearance = cookies.get("cf_clearance")
    if cf_clearance:
        summary["cf_clearance"] = _mask_secret(cf_clearance)
    return summary


def _redact_cookie_pairs(cookies: Dict[str, str]) -> Dict[str, str]:
    return {name: _mask_secret(value) for name, value in cookies.items()}


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
    translated_host = "host.docker.internal" if host in {"127.0.0.1", "localhost", "0.0.0.0"} else host
    if not translated_host:
        return proxy
    port = f":{parsed.port}" if parsed.port else ""
    rebuilt = parsed._replace(netloc=f"{translated_host}{port}")
    return rebuilt.geturl()


def _build_flaresolverr_proxy(proxy: str | None) -> Dict[str, str] | None:
    translated_url = _translate_proxy_for_flaresolverr(proxy)
    if not translated_url:
        return None

    parsed = urlparse(proxy)
    payload = {"url": translated_url}
    if parsed.username:
        payload["username"] = unquote(parsed.username)
    if parsed.password:
        payload["password"] = unquote(parsed.password)
    return payload


def _normalized_port(parsed) -> int | None:
    if parsed.port is not None:
        return parsed.port
    if parsed.scheme == "http":
        return 80
    if parsed.scheme == "https":
        return 443
    return None


def _is_same_origin(base_url: str | None, target_url: str) -> bool:
    if not base_url:
        return True
    base = urlparse(base_url)
    target = urlparse(target_url)
    if not base.hostname or not target.hostname:
        return True
    return (
        base.scheme == target.scheme
        and base.hostname == target.hostname
        and _normalized_port(base) == _normalized_port(target)
    )


class Requester:
    def __init__(
        self,
        session: SessionState,
        flaresolverr_base_url: str | None,
        flaresolverr_timeout_secs: int,
        ua_probe_url: str | None,
        debug: bool = False,
        min_interval_secs: float = 0.0,
        max_retries: int = 1,
        timeout_secs: float = 30,
    ):
        self._session = session
        self._flaresolverr_base_url = flaresolverr_base_url
        self._flaresolverr_timeout_secs = flaresolverr_timeout_secs
        self._ua_probe_url = ua_probe_url
        self._debug = debug
        self._min_interval_secs = max(float(min_interval_secs), 0.0)
        self._max_retries = max(int(max_retries), 0)
        self._timeout_secs = max(float(timeout_secs), 1.0)
        self._lock = threading.Lock()
        self._csrf_token_hint = ""
        if not self._session.cookies:
            self._session.cookies = parse_cookie_header(self._session.cookie_header)

    def _default_headers(self, *, include_referer: bool = True) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        base = (self._session.base_url or "").rstrip("/")
        if include_referer and base:
            headers["Referer"] = base + "/"
        return headers

    def _ensure_user_agent(self, ua_probe_url: str | None) -> str:
        if self._session.user_agent:
            return self._session.user_agent
        default_ua = get_default_ua(self._session.impersonate_target)
        if default_ua:
            self._session.user_agent = default_ua
            return default_ua
        probe_url = ua_probe_url or "data:,"
        if probe_url and self._flaresolverr_base_url:
            solution = self._flaresolverr_request(
                "get",
                probe_url,
                include_cookies=False,
                persist_solution_cookies=False,
            )
            ua = solution.get("userAgent", "")
            if ua:
                self._session.user_agent = ua
                return ua
        return ""

    def _ensure_impersonate_target(self, user_agent: str) -> str:
        if self._session.impersonate_target:
            return self._session.impersonate_target
        inferred = infer_impersonate_target_from_ua(user_agent)
        if inferred:
            self._session.impersonate_target = inferred
            return inferred
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
        same_origin = _is_same_origin(self._session.base_url, url)

        response: ResponseData | None = None
        with self._lock:
            attempt = 0
            while True:
                self._throttle()

                user_agent = self._ensure_user_agent(ua_probe_url or self._ua_probe_url)
                impersonate_target = self._ensure_impersonate_target(user_agent)
                if self._debug and user_agent:
                    if impersonate_target:
                        _LOG.debug(
                            "ua probe aligned runtime identity: user_agent=%s impersonate_target=%s",
                            user_agent,
                            impersonate_target,
                        )
                    else:
                        _LOG.warning(
                            "ua probe could not align impersonate target: user_agent=%s impersonate_target=%s",
                            user_agent,
                            self._session.impersonate_target,
                        )
                req_headers = _merge_headers(self._default_headers(include_referer=same_origin), headers)
                if user_agent and not _has_header(req_headers, "User-Agent"):
                    req_headers["User-Agent"] = user_agent

                cookies: Dict[str, str] = {}
                if same_origin:
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
                    _LOG.debug("request cookies: %s", _summarize_cookies(cookies))
                    if params:
                        _LOG.debug("request params: %s", params)
                try:
                    response = self._perform_request(
                        method=method,
                        url=url,
                        headers=req_headers,
                        params=params,
                        data=data,
                        json_body=json_body,
                        cookies=cookies,
                        proxies=proxies,
                        impersonate_target=impersonate_target,
                        persist_response_cookies=same_origin,
                    )
                except Exception as exc:  # noqa: BLE001
                    self._session.last_response_ok = False
                    if not self._can_retry(attempt):
                        raise
                    _LOG.warning("request failed (%s/%s): %s", attempt + 1, self._attempt_limit_label(), exc)
                    self._backoff(attempt)
                    attempt += 1
                    continue

                if self._debug:
                    _LOG.debug("response status: %s", response.status)
                    _LOG.debug("response headers: %s", _redact_headers(response.headers))
                    _LOG.debug("response body (first 500 chars): %s", response.text[:500])

                if allow_fallback and same_origin and _is_cloudflare_challenge(response.status, response.headers, response.text):
                    if not self._flaresolverr_base_url:
                        self._session.last_response_ok = response.status < 400
                        return response
                    _LOG.info("challenge detected, solving via FlareSolverr and retrying")
                    try:
                        self._solve_challenge()
                        self._apply_csrf_token_hint(req_headers)
                        if self._debug:
                            _LOG.debug("retry request headers: %s", _redact_headers(req_headers))
                            _LOG.debug("retry request cookies: %s", _summarize_cookies(dict(self._session.cookies)))
                        response = self._perform_request(
                            method=method,
                            url=url,
                            headers=req_headers,
                            params=params,
                            data=data,
                            json_body=json_body,
                            cookies=dict(self._session.cookies),
                            proxies=proxies,
                            impersonate_target=self._session.impersonate_target,
                            persist_response_cookies=True,
                        )
                    except Exception as exc:  # noqa: BLE001
                        self._session.last_response_ok = False
                        if not self._can_retry(attempt):
                            raise
                        _LOG.warning(
                            "request failed after challenge solve (%s/%s): %s",
                            attempt + 1,
                            self._attempt_limit_label(),
                            exc,
                        )
                        self._backoff(attempt)
                        attempt += 1
                        continue
                    if self._debug:
                        _LOG.debug("retry status: %s", response.status)
                        _LOG.debug("retry headers: %s", _redact_headers(response.headers))
                        _LOG.debug("retry body (first 500 chars): %s", response.text[:500])
                    if _is_cloudflare_challenge(response.status, response.headers, response.text):
                        self._session.last_response_ok = False
                        if not self._can_retry(attempt):
                            return response
                        _LOG.warning("challenge still present after solve")
                        self._backoff(attempt)
                        attempt += 1
                        continue

                if response.status >= 500 or response.status == 429:
                    self._session.last_response_ok = False
                    if not self._can_retry(attempt):
                        return response
                    _LOG.warning(
                        "retryable status (%s/%s): %s",
                        attempt + 1,
                        self._attempt_limit_label(),
                        response.status,
                    )
                    self._backoff(attempt)
                    attempt += 1
                    continue
                self._session.last_response_ok = response.status < 400
                return response

            if response is None:
                raise RuntimeError("request failed without response")
            return response

    def _can_retry(self, attempt: int) -> bool:
        return self._max_retries == 0 or attempt < self._max_retries

    def _attempt_limit_label(self) -> str:
        if self._max_retries == 0:
            return "unlimited"
        return str(self._max_retries + 1)

    def _perform_request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        params: Optional[Dict[str, str]],
        data: Optional[str],
        json_body: Optional[Dict[str, Any]],
        cookies: Dict[str, str],
        proxies: Dict[str, str] | None,
        impersonate_target: str,
        persist_response_cookies: bool,
    ) -> ResponseData:
        if self._debug and cookies:
            _LOG.debug("curl_cffi cookie header: %s", _redact_cookie_pairs(cookies))
        resp = requests.request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            data=data,
            json=json_body,
            cookies=cookies,
            impersonate=impersonate_target or None,
            proxies=proxies,
            timeout=self._timeout_secs,
        )
        response = ResponseData(status=resp.status_code, headers=dict(resp.headers), text=resp.text)
        if persist_response_cookies and resp.cookies:
            merged = dict(self._session.cookies)
            merged.update(resp.cookies)
            self._session.cookies = merged
        return response

    def _backoff(self, attempt: int) -> None:
        delay = _retry_delay_secs(attempt)
        _LOG.info("retry backoff: sleeping %ss before next attempt", delay)
        time.sleep(delay)

    def get_cookie_header(self) -> str:
        return cookies_to_header(self._session.cookies)

    def get_csrf_token_hint(self) -> str:
        return self._csrf_token_hint

    def consume_csrf_token_hint(self) -> str:
        hint = self._csrf_token_hint
        self._csrf_token_hint = ""
        return hint

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
        params: Optional[Dict[str, str]] = None,
        data: Optional[str] = None,
        include_cookies: bool = True,
        persist_solution_cookies: bool = True,
    ) -> Dict[str, Any]:
        if not self._flaresolverr_base_url:
            raise RuntimeError("FlareSolverr not configured")

        flaresolverr_proxy = _build_flaresolverr_proxy(self._session.proxy)

        payload: Dict[str, Any] = {
            "cmd": f"request.{method.lower()}",
            "url": url,
            "maxTimeout": int(self._flaresolverr_timeout_secs * 1000),
        }
        if include_cookies and self._session.cookies:
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
        if self._debug:
            solution_cookies = {
                item.get("name"): item.get("value")
                for item in solution.get("cookies", [])
                if item.get("name") and item.get("value")
            }
            _LOG.debug(
                "flaresolverr solution: userAgent=%s cookies=%s",
                solution.get("userAgent", ""),
                _summarize_cookies(solution_cookies),
            )
        csrf_token = ""
        if include_cookies and persist_solution_cookies:
            csrf_token = _extract_csrf_token_from_html(
                str(solution.get("response") or solution.get("html") or "")
            )
        if csrf_token:
            self._csrf_token_hint = csrf_token
            _LOG.info("flaresolverr extracted csrf token: %s", _mask_secret(csrf_token))
        if not persist_solution_cookies:
            return solution
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
        self._flaresolverr_request("get", base)

    def _apply_csrf_token_hint(self, headers: Dict[str, str]) -> None:
        if not self._csrf_token_hint:
            return
        for key in list(headers.keys()):
            if _normalize_header_key(key) == "x-csrf-token":
                headers[key] = self._csrf_token_hint
                _LOG.info("request: updated x-csrf-token from FlareSolverr solution")
                return


def _redact_headers(headers: Dict[str, Any]) -> Dict[str, Any]:
    redacted = {}
    for key, value in headers.items():
        lower = str(key).lower()
        if lower in {"cookie", "set-cookie", "x-csrf-token", "authorization"}:
            redacted[key] = "***"
        else:
            redacted[key] = value
    return redacted


def _retry_delay_secs(attempt: int) -> int:
    safe_attempt = max(int(attempt), 0)
    return min(2 ** safe_attempt, _MAX_RETRY_DELAY_SECS)


def _extract_csrf_token_from_html(html: str) -> str:
    if not html:
        return ""
    match = _CSRF_META_RE.search(html)
    if not match:
        return ""
    return match.group(1).strip()
