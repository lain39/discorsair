"""Requester tests."""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

fake_requests = types.SimpleNamespace(request=None, post=None)
fake_requests_exceptions = types.SimpleNamespace(RequestException=RuntimeError)
fake_requests.exceptions = fake_requests_exceptions
sys.modules.setdefault("curl_cffi", types.SimpleNamespace(requests=fake_requests))
sys.modules.setdefault("curl_cffi.requests", fake_requests)
sys.modules.setdefault("curl_cffi.requests.exceptions", fake_requests_exceptions)

from discorsair.core.requester import (
    ChallengeUnresolvedError,
    Requester,
    _build_flaresolverr_proxy,
    _build_flaresolverr_proxy_with_mode,
    _extract_csrf_token_from_html,
    _retry_delay_secs,
    _translate_proxy_for_flaresolverr,
    _translate_proxy_for_flaresolverr_with_mode,
)
from discorsair.core.session import SessionState
from discorsair.discourse.client import DiscourseAuthError


class RequesterTests(unittest.TestCase):
    def test_extract_csrf_token_from_html(self) -> None:
        html = '<html><head><meta name="csrf-token" content="csrf-123"></head></html>'
        self.assertEqual(_extract_csrf_token_from_html(html), "csrf-123")

    def test_retry_delay_grows_with_attempts(self) -> None:
        self.assertEqual(_retry_delay_secs(0), 1)
        self.assertEqual(_retry_delay_secs(1), 2)
        self.assertEqual(_retry_delay_secs(2), 4)
        self.assertEqual(_retry_delay_secs(3), 8)
        self.assertEqual(_retry_delay_secs(4), 16)

    def test_retry_delay_caps_at_longer_ceiling(self) -> None:
        self.assertEqual(_retry_delay_secs(8), 256)
        self.assertEqual(_retry_delay_secs(9), 300)
        self.assertEqual(_retry_delay_secs(12), 300)

    def test_translate_proxy_rewrites_loopback_without_auth(self) -> None:
        proxy = "http://user:pass@127.0.0.1:7890"
        self.assertEqual(
            _translate_proxy_for_flaresolverr(proxy),
            "http://host.docker.internal:7890",
        )

    def test_build_flaresolverr_proxy_decodes_auth(self) -> None:
        proxy = "http://proxy.user:p%40ss%26word@127.0.0.1:5352"
        self.assertEqual(
            _build_flaresolverr_proxy(proxy),
            {
                "url": "http://host.docker.internal:5352",
                "username": "proxy.user",
                "password": "p@ss&word",
            },
        )

    def test_translate_proxy_keeps_loopback_when_flaresolverr_not_in_docker(self) -> None:
        proxy = "http://user:pass@127.0.0.1:7890"
        self.assertEqual(
            _translate_proxy_for_flaresolverr_with_mode(proxy, running_in_docker=False),
            "http://127.0.0.1:7890",
        )

    def test_build_flaresolverr_proxy_keeps_loopback_when_not_in_docker(self) -> None:
        proxy = "http://proxy.user:p%40ss%26word@127.0.0.1:5352"
        self.assertEqual(
            _build_flaresolverr_proxy_with_mode(proxy, running_in_docker=False),
            {
                "url": "http://127.0.0.1:5352",
                "username": "proxy.user",
                "password": "p@ss&word",
            },
        )

    def test_ensure_user_agent_probe_does_not_send_cookies(self) -> None:
        requester = Requester(
            session=SessionState(
                base_url="https://forum.example",
                cookie_header="_t=1; cf_clearance=abc",
                impersonate_target="chrome110",
                user_agent="",
            ),
            flaresolverr_base_url="http://flaresolverr:8191",
            flaresolverr_timeout_secs=60,
            ua_probe_url="https://forum.example/latest.json",
        )

        class DummyFsResponse:
            def json(self) -> dict[str, object]:
                return {
                    "status": "ok",
                    "solution": {
                        "userAgent": "ua-from-flaresolverr",
                        "cookies": [
                            {"name": "probe_cookie", "value": "should-not-persist"},
                        ],
                    },
                }

        with patch("discorsair.core.requester.get_default_ua", return_value=""):
            with patch("discorsair.core.requester.requests.post", return_value=DummyFsResponse()) as post:
                user_agent = requester._ensure_user_agent(requester._ua_probe_url)

        self.assertEqual(user_agent, "ua-from-flaresolverr")
        self.assertEqual(requester._session.user_agent, "ua-from-flaresolverr")
        self.assertEqual(requester.get_csrf_token_hint(), "")
        payload = post.call_args.kwargs["json"]
        self.assertNotIn("cookies", payload)
        self.assertNotIn("probe_cookie", requester._session.cookies)
        self.assertEqual(requester._session.cookies["cf_clearance"], "abc")

    def test_request_infers_impersonate_target_from_flaresolverr_user_agent(self) -> None:
        requester = Requester(
            session=SessionState(
                base_url="https://forum.example",
                cookie_header="_t=1",
                impersonate_target="",
                user_agent="",
            ),
            flaresolverr_base_url="http://flaresolverr:8191",
            flaresolverr_timeout_secs=60,
            ua_probe_url="https://forum.example/latest.json",
        )

        class DummyFsResponse:
            def json(self) -> dict[str, object]:
                return {
                    "status": "ok",
                    "solution": {
                        "userAgent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/142.0.0.0 Safari/537.36"
                        ),
                    },
                }

        class DummyResponse:
            def __init__(self) -> None:
                self.status_code = 200
                self.text = "{\"ok\":true}"
                self.headers = {"Content-Type": "application/json"}
                self.cookies = {}

        with patch("discorsair.core.requester.get_default_ua", return_value=""):
            with patch("discorsair.utils.ua_map._available_impersonate_targets", return_value={"chrome110", "chrome120", "chrome142"}):
                with patch("discorsair.core.requester.requests.post", return_value=DummyFsResponse()):
                    with patch("discorsair.core.requester.requests.request", return_value=DummyResponse()) as request_call:
                        response = requester.request("get", "/latest.json", allow_fallback=False)

        self.assertEqual(response.status, 200)
        self.assertEqual(requester._session.user_agent, DummyFsResponse().json()["solution"]["userAgent"])
        self.assertEqual(requester._session.impersonate_target, "chrome142")
        self.assertEqual(request_call.call_args.kwargs["impersonate"], "chrome142")

    def test_retry_after_challenge_handles_retry_exception(self) -> None:
        requester = Requester(
            session=SessionState(
                base_url="https://forum.example",
                cookie_header="_t=1",
                impersonate_target="chrome110",
                user_agent="ua",
            ),
            flaresolverr_base_url="http://flaresolverr:8191",
            flaresolverr_timeout_secs=60,
            ua_probe_url=None,
            max_retries=2,
        )

        class DummyResponse:
            def __init__(self, status_code: int, text: str, headers: dict[str, str] | None = None) -> None:
                self.status_code = status_code
                self.text = text
                self.headers = headers or {}
                self.cookies = {}

        calls: list[str] = []

        def fake_request(**kwargs):
            calls.append(kwargs["url"])
            if len(calls) == 1:
                return DummyResponse(
                    403,
                    "<html>Just a moment</html>",
                    {"Content-Type": "text/html"},
                )
            if len(calls) == 2:
                raise RuntimeError("retry failed")
            return DummyResponse(200, "{\"ok\":true}", {"Content-Type": "application/json"})

        with patch("discorsair.core.requester.requests.request", side_effect=fake_request):
            with patch.object(requester, "_solve_challenge") as solve:
                with patch.object(requester, "_backoff") as backoff:
                    response = requester.request("get", "/latest.json")

        self.assertEqual(response.status, 200)
        self.assertEqual(calls.count("https://forum.example/latest.json"), 3)
        solve.assert_called_once()
        backoff.assert_called_once()

    def test_challenge_retry_replaces_csrf_header_from_flaresolverr_html(self) -> None:
        requester = Requester(
            session=SessionState(
                base_url="https://forum.example",
                cookie_header="_t=1",
                impersonate_target="chrome110",
                user_agent="ua",
            ),
            flaresolverr_base_url="http://flaresolverr:8191",
            flaresolverr_timeout_secs=60,
            ua_probe_url=None,
            max_retries=1,
        )

        class DummyResponse:
            def __init__(self, status_code: int, text: str, headers: dict[str, str] | None = None) -> None:
                self.status_code = status_code
                self.text = text
                self.headers = headers or {}
                self.cookies = {}

        class DummyFsResponse:
            def json(self) -> dict[str, object]:
                return {
                    "status": "ok",
                    "solution": {
                        "response": (
                            '<html><head><meta name="csrf-token" content="new-csrf"></head></html>'
                        ),
                        "cookies": [{"name": "cf_clearance", "value": "abc"}],
                    },
                }

        calls: list[dict[str, object]] = []

        def fake_request(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return DummyResponse(403, "<html>Just a moment</html>", {"Content-Type": "text/html"})
            return DummyResponse(200, "{\"ok\":true}", {"Content-Type": "application/json"})

        with patch("discorsair.core.requester.requests.post", return_value=DummyFsResponse()):
            with patch("discorsair.core.requester.requests.request", side_effect=fake_request):
                response = requester.request(
                    "post",
                    "/timings",
                    headers={"x-csrf-token": "old-csrf"},
                    data="topic_id=1",
                )

        self.assertEqual(response.status, 200)
        retry_call = calls[-1]
        self.assertEqual(retry_call["url"], "https://forum.example/timings")
        self.assertEqual(retry_call["headers"]["x-csrf-token"], "new-csrf")
        self.assertEqual(requester.get_csrf_token_hint(), "new-csrf")

    def test_max_retries_counts_additional_retries(self) -> None:
        requester = Requester(
            session=SessionState(
                base_url="https://forum.example",
                cookie_header="_t=1",
                impersonate_target="chrome110",
                user_agent="ua",
            ),
            flaresolverr_base_url=None,
            flaresolverr_timeout_secs=60,
            ua_probe_url=None,
            max_retries=1,
        )

        class DummyResponse:
            def __init__(self, status_code: int) -> None:
                self.status_code = status_code
                self.text = "{\"ok\":false}"
                self.headers = {"Content-Type": "application/json"}
                self.cookies = {}

        responses = [DummyResponse(500), DummyResponse(200)]

        with patch("discorsair.core.requester.requests.request", side_effect=responses) as request_call:
            with patch.object(requester, "_backoff") as backoff:
                response = requester.request("get", "/latest.json", allow_fallback=False)

        self.assertEqual(response.status, 200)
        self.assertEqual(request_call.call_count, 2)
        backoff.assert_called_once()

    def test_max_retries_zero_retries_until_success(self) -> None:
        requester = Requester(
            session=SessionState(
                base_url="https://forum.example",
                cookie_header="_t=1",
                impersonate_target="chrome110",
                user_agent="ua",
            ),
            flaresolverr_base_url=None,
            flaresolverr_timeout_secs=60,
            ua_probe_url=None,
            max_retries=0,
        )

        class DummyResponse:
            def __init__(self, status_code: int) -> None:
                self.status_code = status_code
                self.text = "{\"ok\":true}"
                self.headers = {"Content-Type": "application/json"}
                self.cookies = {}

        responses = [RuntimeError("network-1"), RuntimeError("network-2"), DummyResponse(200)]

        with patch("discorsair.core.requester.requests.request", side_effect=responses) as request_call:
            with patch.object(requester, "_backoff") as backoff:
                response = requester.request("get", "/latest.json", allow_fallback=False)

        self.assertEqual(response.status, 200)
        self.assertEqual(request_call.call_count, 3)
        self.assertEqual(backoff.call_count, 2)

    def test_cross_origin_request_does_not_send_or_persist_site_cookies(self) -> None:
        requester = Requester(
            session=SessionState(
                base_url="https://forum.example",
                cookie_header="_t=1; cf_clearance=abc",
                impersonate_target="chrome110",
                user_agent="ua",
            ),
            flaresolverr_base_url="http://flaresolverr:8191",
            flaresolverr_timeout_secs=60,
            ua_probe_url=None,
        )

        class DummyResponse:
            def __init__(self) -> None:
                self.status_code = 403
                self.text = "<html>Just a moment</html>"
                self.headers = {"Content-Type": "text/html"}
                self.cookies = {"external_cookie": "1"}

        with patch("discorsair.core.requester.requests.request", return_value=DummyResponse()) as request_call:
            with patch.object(requester, "_solve_challenge") as solve:
                response = requester.request("get", "https://other.example/ping")

        self.assertEqual(response.status, 403)
        solve.assert_not_called()
        kwargs = request_call.call_args.kwargs
        self.assertEqual(kwargs["cookies"], {})
        self.assertNotIn("Referer", kwargs["headers"])
        self.assertNotIn("external_cookie", requester._session.cookies)
        self.assertEqual(requester._session.cookies["cf_clearance"], "abc")

    def test_flaresolverr_request_uses_structured_proxy_payload(self) -> None:
        requester = Requester(
            session=SessionState(
                base_url="https://forum.example",
                cookie_header="_t=1",
                impersonate_target="chrome110",
                user_agent="ua",
                proxy="http://proxy.user:p%40ss%26word@127.0.0.1:5352",
            ),
            flaresolverr_base_url="http://flaresolverr:8191",
            flaresolverr_timeout_secs=60,
            ua_probe_url=None,
        )

        class DummyFsResponse:
            def json(self) -> dict[str, object]:
                return {"status": "ok", "solution": {"cookies": []}}

        with patch("discorsair.core.requester.requests.post", return_value=DummyFsResponse()) as post:
            requester._flaresolverr_request("get", "https://forum.example/latest.json")

        payload = post.call_args.kwargs["json"]
        self.assertEqual(
            payload["proxy"],
            {
                "url": "http://host.docker.internal:5352",
                "username": "proxy.user",
                "password": "p@ss&word",
            },
        )

    def test_flaresolverr_request_keeps_loopback_proxy_when_not_in_docker(self) -> None:
        requester = Requester(
            session=SessionState(
                base_url="https://forum.example",
                cookie_header="_t=1",
                impersonate_target="chrome110",
                user_agent="ua",
                proxy="http://proxy.user:p%40ss%26word@127.0.0.1:5352",
            ),
            flaresolverr_base_url="http://flaresolverr:8191",
            flaresolverr_timeout_secs=60,
            ua_probe_url=None,
            flaresolverr_in_docker=False,
        )

        class DummyFsResponse:
            def json(self) -> dict[str, object]:
                return {"status": "ok", "solution": {"cookies": []}}

        with patch("discorsair.core.requester.requests.post", return_value=DummyFsResponse()) as post:
            requester._flaresolverr_request("get", "https://forum.example/latest.json")

        payload = post.call_args.kwargs["json"]
        self.assertEqual(
            payload["proxy"],
            {
                "url": "http://127.0.0.1:5352",
                "username": "proxy.user",
                "password": "p@ss&word",
            },
        )

    def test_fetch_csrf_via_flaresolverr_aligns_user_agent(self) -> None:
        requester = Requester(
            session=SessionState(
                base_url="https://forum.example",
                cookie_header="_t=1",
                impersonate_target="chrome110",
                user_agent="",
            ),
            flaresolverr_base_url="http://flaresolverr:8191",
            flaresolverr_timeout_secs=60,
            ua_probe_url=None,
            flaresolverr_use_base_url_for_csrf=True,
        )

        class DummyFsResponse:
            def json(self) -> dict[str, object]:
                return {
                    "status": "ok",
                    "solution": {
                        "response": '<html><head><meta name="csrf-token" content="csrf-123"></head></html>',
                        "cookies": [],
                    },
                }

        with patch("discorsair.core.requester.get_default_ua", return_value="ua-110"):
            with patch("discorsair.core.requester.requests.post", return_value=DummyFsResponse()) as post:
                token = requester.fetch_csrf_token_via_flaresolverr()

        self.assertEqual(token, "csrf-123")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["userAgent"], "ua-110")
        self.assertTrue(requester.last_response_ok())

    def test_fetch_csrf_via_flaresolverr_retries_on_failure(self) -> None:
        requester = Requester(
            session=SessionState(
                base_url="https://forum.example",
                cookie_header="_t=1",
                impersonate_target="chrome110",
                user_agent="ua",
            ),
            flaresolverr_base_url="http://flaresolverr:8191",
            flaresolverr_timeout_secs=60,
            ua_probe_url=None,
            flaresolverr_use_base_url_for_csrf=True,
            max_retries=1,
        )

        class DummyFsResponse:
            def json(self) -> dict[str, object]:
                return {
                    "status": "ok",
                    "solution": {
                        "response": '<html><head><meta name="csrf-token" content="csrf-123"></head></html>',
                        "cookies": [],
                    },
                }

        with patch("discorsair.core.requester.requests.post", side_effect=[RuntimeError("fs down"), DummyFsResponse()]) as post:
            with patch.object(requester, "_backoff") as backoff:
                token = requester.fetch_csrf_token_via_flaresolverr()

        self.assertEqual(token, "csrf-123")
        self.assertEqual(post.call_count, 2)
        backoff.assert_called_once()
        self.assertTrue(requester.last_response_ok())

    def test_fetch_csrf_via_flaresolverr_marks_last_response_failed_on_error(self) -> None:
        requester = Requester(
            session=SessionState(
                base_url="https://forum.example",
                cookie_header="_t=1",
                impersonate_target="chrome110",
                user_agent="ua",
            ),
            flaresolverr_base_url="http://flaresolverr:8191",
            flaresolverr_timeout_secs=60,
            ua_probe_url=None,
            flaresolverr_use_base_url_for_csrf=True,
            max_retries=1,
        )

        with patch("discorsair.core.requester.requests.post", side_effect=RuntimeError("fs down")):
            with patch.object(requester, "_backoff"):
                with self.assertRaisesRegex(RuntimeError, "fs down"):
                    requester.fetch_csrf_token_via_flaresolverr()

        self.assertFalse(requester.last_response_ok())

    def test_fetch_csrf_via_flaresolverr_auth_error_is_not_retried(self) -> None:
        requester = Requester(
            session=SessionState(
                base_url="https://forum.example",
                cookie_header="_t=1",
                impersonate_target="chrome110",
                user_agent="ua",
            ),
            flaresolverr_base_url="http://flaresolverr:8191",
            flaresolverr_timeout_secs=60,
            ua_probe_url=None,
            flaresolverr_use_base_url_for_csrf=True,
            max_retries=3,
        )

        with patch.object(requester, "_flaresolverr_request", side_effect=DiscourseAuthError("not_logged_in")):
            with patch.object(requester, "_backoff") as backoff:
                with self.assertRaises(DiscourseAuthError):
                    requester.fetch_csrf_token_via_flaresolverr()

        backoff.assert_not_called()
        self.assertFalse(requester.last_response_ok())

    def test_flaresolverr_cookies_without_csrf_meta_raise_auth_error(self) -> None:
        requester = Requester(
            session=SessionState(
                base_url="https://forum.example",
                cookie_header="_t=1",
                impersonate_target="chrome110",
                user_agent="ua",
            ),
            flaresolverr_base_url="http://flaresolverr:8191",
            flaresolverr_timeout_secs=60,
            ua_probe_url=None,
        )

        class DummyFsResponse:
            def json(self) -> dict[str, object]:
                return {
                    "status": "ok",
                    "solution": {
                        "response": "<html><body>ok</body></html>",
                        "cookies": [{"name": "_forum_session", "value": "abc"}],
                    },
                }

        with patch("discorsair.core.requester.requests.post", return_value=DummyFsResponse()):
            with self.assertRaises(DiscourseAuthError):
                requester._solve_challenge()

    def test_challenge_solve_auth_error_is_not_retried(self) -> None:
        requester = Requester(
            session=SessionState(
                base_url="https://forum.example",
                cookie_header="_t=1",
                impersonate_target="chrome110",
                user_agent="ua",
            ),
            flaresolverr_base_url="http://flaresolverr:8191",
            flaresolverr_timeout_secs=60,
            ua_probe_url=None,
            max_retries=3,
        )

        class DummyResponse:
            def __init__(self) -> None:
                self.status_code = 403
                self.text = "<html>Just a moment</html>"
                self.headers = {"Content-Type": "text/html"}
                self.cookies = {}

        with patch("discorsair.core.requester.requests.request", return_value=DummyResponse()) as request_call:
            with patch.object(requester, "_solve_challenge", side_effect=DiscourseAuthError("not_logged_in")):
                with patch.object(requester, "_backoff") as backoff:
                    with self.assertRaises(DiscourseAuthError):
                        requester.request("get", "/latest.json")

        self.assertEqual(request_call.call_count, 1)
        backoff.assert_not_called()
        self.assertFalse(requester.last_response_ok())

    def test_challenge_still_present_after_solve_stops_even_with_unlimited_retries(self) -> None:
        requester = Requester(
            session=SessionState(
                base_url="https://forum.example",
                cookie_header="_t=1",
                impersonate_target="chrome110",
                user_agent="ua",
            ),
            flaresolverr_base_url="http://flaresolverr:8191",
            flaresolverr_timeout_secs=60,
            ua_probe_url=None,
            max_retries=0,
        )

        class DummyResponse:
            def __init__(self, status_code: int, text: str, headers: dict[str, str] | None = None) -> None:
                self.status_code = status_code
                self.text = text
                self.headers = headers or {}
                self.cookies = {}

        class DummyFsResponse:
            def json(self) -> dict[str, object]:
                return {
                    "status": "ok",
                    "solution": {
                        "response": '<html><head><meta name="csrf-token" content="new-csrf"></head></html>',
                        "cookies": [],
                    },
                }

        calls: list[dict[str, object]] = []

        def fake_request(**kwargs):
            calls.append(kwargs)
            return DummyResponse(403, "<html>Just a moment</html>", {"Content-Type": "text/html"})

        with patch("discorsair.core.requester.requests.post", return_value=DummyFsResponse()):
            with patch("discorsair.core.requester.requests.request", side_effect=fake_request):
                with patch.object(requester, "_backoff") as backoff:
                    with self.assertRaisesRegex(ChallengeUnresolvedError, "challenge still present after solve"):
                        requester.request("post", "/timings", headers={"x-csrf-token": "old-csrf"}, data="topic_id=1")

        self.assertGreaterEqual(len(calls), 4)
        self.assertEqual(backoff.call_count, 2)


if __name__ == "__main__":
    unittest.main()
