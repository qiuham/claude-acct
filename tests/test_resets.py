"""订阅号额度只走 HTTP（/api/oauth/usage，失败不退回 claude -p）+ 重置卡（cedar_ember）并进 `cca ls` 的一列。

全部用桩：请求本身替换掉 `oauth_usage._open`，CLI 用 CCA_OAUTH_USAGE_FILE 桩文件；不联网、不起真的 claude。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: F401 - 先于 cca 导入：HOME 指到临时目录，测试不碰真实 ~/.claude-accounts

from cca import report
from cca.api import oauth_usage, usage
from cca.paths import reset_cache_file, usage_cache_file

REPO = Path(__file__).resolve().parent.parent
TOKEN = "sk-ant-oat01-SECRET-TOKEN-VALUE"  # noqa: S105 - 假令牌，只为断言它不出现在输出里
NEW_TOKEN = "sk-ant-oat01-REFRESHED-BY-CLAUDE"  # noqa: S105 - 假令牌
HOUR = 3600
SHANGHAI = ZoneInfo("Asia/Shanghai")
GRANT_IDS = ("grant_abc", "grant_def", "grant_ghi", "grant_next")


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def grant(gid="grant_abc", **over):
    base = {"id": gid, "label": "", "resets_total": 3, "resets_left": 2, "starts_at": iso(time.time() - HOUR),
            "ends_at": iso(time.time() + 48 * HOUR), "clears": ["five_hour"], "paused": False,
            "usable_now": True, "use_requires_limit": False, "type": "session", "gives_back_weekly": False,
            "percent_used": 0, "blocking": []}
    base.update(over)
    return base


def body(grants=None, *, cedar=True, eligible=True, reason=None):
    out = {"five_hour": {"utilization": 41, "resets_at": iso(time.time() + 2 * HOUR)},
           "seven_day": {"utilization": 12.0, "resets_at": iso(time.time() + 72 * HOUR)}}
    if cedar is None:
        out["cedar_ember"] = None
    elif cedar:
        out["cedar_ember"] = {"eligible": eligible, "ineligible_reason": reason, "at_limit": False,
                              "next_grant_id": "grant_next", "cooldown_until": None,
                              "weekly_resets_at": iso(time.time() + 72 * HOUR),
                              "grants": [grant()] if grants is None else grants}
    return out


class HomeCase(unittest.TestCase):
    """每个用例一个临时 HOME：号目录、凭据、缓存都在里面。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "CCA_PROXY": "http://localhost:7897"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CCA_OAUTH_USAGE_FILE", None)
        os.environ.pop("CCA_USAGE_LIVE_FILE", None)
        oauth_usage.read_claude_version.cache_clear()
        self.addCleanup(oauth_usage.read_claude_version.cache_clear)
        self.addCleanup(self._tmp.cleanup)

    def seed(self, name="b001", *, token=TOKEN, scopes=("user:inference", "user:profile"), expires_in=HOUR):
        d = self.home / ".claude-accounts" / name
        d.mkdir(parents=True, exist_ok=True)
        oauth = {"accessToken": token, "refreshToken": "sk-ant-ort01-REFRESH", "subscriptionType": "max",
                 "expiresAt": int((time.time() + expires_in) * 1000)}
        if scopes is not None:
            oauth["scopes"] = list(scopes)
        (d / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")
        return d

    def fake_open(self, payload=None, *, error=None):
        """替换真正发请求的那一个函数：记下每次请求，返回桩数据或抛 HTTP 错。"""
        calls = []

        def _open(request, proxy):
            calls.append(request)
            if error is not None:
                raise urllib.error.HTTPError(request.full_url, error, "x", {}, None)
            return json.dumps(payload).encode()
        return calls, mock.patch.object(oauth_usage, "_open", side_effect=_open)

    def version(self, v="2.1.327"):
        return mock.patch.object(oauth_usage, "cli_version", return_value=v)


class HttpRequestTest(HomeCase):
    def test_headers_match_claude_code(self):
        self.seed()
        calls, opener = self.fake_open(body())
        with opener, self.version():
            got = oauth_usage.fetch_oauth_usage("b001", with_resets=True)
        self.assertEqual(got["status"], 200)
        req = calls[0]
        self.assertEqual(req.full_url, "https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1")
        headers = {k.lower(): v for k, v in req.header_items()}
        self.assertEqual(headers["user-agent"], "claude-cli/2.1.327 (external, cli)")
        self.assertNotIn("mozilla", headers["user-agent"].lower())
        self.assertEqual(headers["authorization"], f"Bearer {TOKEN}")
        self.assertEqual(headers["anthropic-beta"], "oauth-2025-04-20")
        self.assertEqual(headers["x-app"], "cli")
        self.assertEqual(headers["accept"], "application/json")
        self.assertEqual(headers["content-type"], "application/json")

    def test_version_is_read_once(self):
        self.seed("b001")
        self.seed("b002")
        _calls, opener = self.fake_open(body())
        with opener, self.version() as ver:
            oauth_usage.fetch_oauth_usage("b001", with_resets=True)
            oauth_usage.fetch_oauth_usage("b002", with_resets=True)
        self.assertEqual(ver.call_count, 1)

    def test_no_proxy_sends_nothing(self):
        """订阅号没配代理（也没写直连）：不带令牌从本机出口直连，_open 一次也不调。"""
        self.seed()
        calls, opener = self.fake_open(body())
        with opener, self.version(), mock.patch.dict(os.environ, {"CCA_PROXY": ""}):
            got = oauth_usage.fetch_oauth_usage("b001", with_resets=True)
        self.assertEqual(calls, [])
        self.assertEqual((got["body"], got["reason"]), (None, "没有配置代理"))

    def test_redirect_is_not_followed(self):
        """遇到 3xx 不跟随：urllib 跟跳转会把 Authorization 带到新主机。"""
        handler = oauth_usage._NoRedirect()  # noqa: SLF001 - 测的就是这个处理器
        req = oauth_usage.urllib.request.Request("https://api.anthropic.com/x", headers={"Authorization": "Bearer t"})
        self.assertIsNone(handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example/"))
        built = []

        class FakeOpener:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(request.full_url, 302, "Found", {}, None)

        def build_opener(*handlers):
            built.extend(handlers)
            return FakeOpener()
        self.seed()
        with mock.patch.object(oauth_usage.urllib.request, "build_opener", side_effect=build_opener), \
                self.version():
            got = oauth_usage.fetch_oauth_usage("b001", with_resets=True)
        self.assertTrue(any(isinstance(h, oauth_usage._NoRedirect) for h in built))  # noqa: SLF001
        self.assertEqual((got["body"], got["status"]), (None, 302))

    def test_errors_never_carry_token(self):
        self.seed()
        with mock.patch.object(oauth_usage, "_open", side_effect=OSError(f"boom {TOKEN}")), self.version():
            got = oauth_usage.fetch_oauth_usage("b001", with_resets=True)
        self.assertNotIn(TOKEN, got["reason"])
        self.assertIn("网络失败", got["reason"])


class UsageViaHttpTest(HomeCase):
    def test_normal_writes_usage_and_resets(self):
        self.seed()
        _calls, opener = self.fake_open(body())
        with opener, self.version(), mock.patch.object(usage, "_run_claude") as cli:
            windows, why = usage.fetch_usage_live("b001")
        cli.assert_not_called()
        self.assertEqual(why, "")
        self.assertEqual((windows["five_hour"]["percent"], windows["seven_day"]["percent"]), (41, 12))
        cached = json.loads(reset_cache_file().read_text(encoding="utf-8"))["b001"]
        self.assertTrue(cached["available"])
        self.assertEqual(cached["cards"][0]["resets_left"], 2)
        raw = reset_cache_file().read_text(encoding="utf-8")
        self.assertNotIn("grant_abc", raw)
        self.assertNotIn("grant_next", raw)
        self.assertNotIn(TOKEN, raw)

    def test_live_usage_keeps_cache_format(self):
        self.seed()
        _calls, opener = self.fake_open(body())
        with opener, self.version():
            result, failures = usage.live_usage(["b001"], force=True)
        self.assertEqual(failures, {})
        entry = json.loads(usage_cache_file().read_text(encoding="utf-8"))["b001"]
        self.assertEqual(set(entry), {"windows", "fetched_at"})
        self.assertEqual(entry["windows"]["five_hour"]["label"], "5 小时")
        self.assertEqual(result["b001"]["source"], "live")

    def test_401_does_not_fall_back_and_never_refreshes(self):
        self.seed()
        calls, opener = self.fake_open(error=401)
        with opener, self.version(), mock.patch.object(usage, "_run_claude") as cli, \
                mock.patch.object(usage.subprocess, "run") as run:
            windows, why = usage.fetch_usage_live("b001")
        cli.assert_not_called()
        run.assert_not_called()
        self.assertEqual((windows, why), ({}, "令牌过期或失效（HTTP 401）"))
        # 只请求过额度接口：没有任何刷新令牌的请求
        self.assertEqual(len(calls), 1)
        for req in calls:
            self.assertTrue(req.full_url.startswith("https://api.anthropic.com/api/oauth/usage?"))
            self.assertNotIn("token", req.full_url)
            self.assertIsNone(req.data)

    def test_expired_token_skips_http(self):
        self.seed(expires_in=-60)
        calls, opener = self.fake_open(body())
        with opener, self.version(), mock.patch.object(usage, "_run_claude") as cli:
            windows, why = usage.fetch_usage_live("b001")
        self.assertEqual(calls, [])
        cli.assert_not_called()
        self.assertEqual((windows, why), ({}, "令牌过期"))

    def test_bad_body_is_a_failure(self):
        self.seed()
        with mock.patch.object(oauth_usage, "_open", return_value=b"<html>"), self.version(), \
                mock.patch.object(usage, "_run_claude") as cli:
            windows, why = usage.fetch_usage_live("b001")
        cli.assert_not_called()
        self.assertEqual((windows, why), ({}, "返回体解析不了"))

    def test_no_profile_scope_queries_usage_only(self):
        self.seed(scopes=("user:inference",))
        calls, opener = self.fake_open(body())
        with opener, self.version():
            windows, _why = usage.fetch_usage_live("b001")
        self.assertTrue(windows)
        self.assertNotIn("cedar_ember", calls[0].full_url)
        self.assertEqual(len(calls), 1)
        self.assertFalse(reset_cache_file().exists())
        info = report.reset_cards_payload("b001", "oauth")
        self.assertEqual(info["status"], "unknown")
        self.assertIn("user:profile", info["reason"])

    def test_401_keeps_cached_cards_and_sends_no_extra_request(self):
        """令牌过期：这次算没取到（记进 failures），不为卡另发请求，1 小时内缓存里的旧卡照用。"""
        self.seed()
        _calls, opener = self.fake_open(body())
        with opener, self.version():
            usage.fetch_usage_live("b001")
        before = reset_cache_file().read_text(encoding="utf-8")
        calls, opener = self.fake_open(error=401)
        with opener, self.version(), mock.patch.object(usage, "_run_claude") as cli:
            result, failures = usage.live_usage(["b001"], force=True)
        cli.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertEqual(failures, {"b001": "令牌过期或失效（HTTP 401）"})
        self.assertNotIn("b001", result)
        self.assertEqual(reset_cache_file().read_text(encoding="utf-8"), before)
        self.assertEqual(report.reset_cards_payload("b001", "oauth")["count"], 2)

    def test_no_refresh_endpoint_anywhere(self):
        """cca 绝不自己刷新令牌：代码里没有刷新接口，也没有 refresh_token 授权。"""
        for mod in (oauth_usage, usage):
            src = Path(mod.__file__).read_text(encoding="utf-8")
            self.assertNotIn("oauth/token", src)
            self.assertNotIn("grant_type", src)
            self.assertNotIn("refreshToken", src)

    def test_snapshot_newer_still_fetches_when_cards_stale(self):
        """claude 的本地快照比缓存新时本来不查额度；但卡的缓存旧了就还是查一次（卡只从这个请求来）。"""
        d = self.seed()
        snap = {"cachedUsageUtilization": {"fetchedAtMs": int(time.time() * 1000),
                                           "utilization": body(cedar=False)}}
        (d / ".claude.json").write_text(json.dumps(snap), encoding="utf-8")
        calls, opener = self.fake_open(body())
        with opener, self.version():
            usage.live_usage(["b001"], force=False)
        self.assertEqual(len(calls), 1)
        (d / ".claude.json").write_text(json.dumps(snap), encoding="utf-8")
        calls, opener = self.fake_open(body())
        with opener, self.version():
            usage.live_usage(["b001"], force=False)   # 卡刚查过：照旧用快照，不发请求
        self.assertEqual(calls, [])


    def write_snapshot(self, d):
        snap = {"cachedUsageUtilization": {"fetchedAtMs": int(time.time() * 1000),
                                           "utilization": body(cedar=False)}}
        (d / ".claude.json").write_text(json.dumps(snap), encoding="utf-8")

    def test_snapshot_newer_cards_query_fails_returns_no_stale_cache(self):
        """快照比缓存新、只为补卡去查：HTTP 失败就不放进返回值（调用方读快照），也不算失败。"""
        d = self.seed()
        old = time.time() - 60   # 额度缓存还在 5 分钟内，但比快照旧
        stale = {"five_hour": {"label": "5 小时", "percent": 99, "resets_at": None, "reset_passed": False}}
        usage.merge_cache(usage_cache_file(), {"b001": {"windows": stale, "fetched_at": old}})
        self.write_snapshot(d)
        calls, opener = self.fake_open(error=401)
        with opener, self.version(), mock.patch.object(usage, "_run_claude") as cli:
            result, failures = usage.live_usage(["b001"], force=False)
        self.assertEqual(len(calls), 1)
        cli.assert_not_called()
        self.assertNotIn("b001", result)
        self.assertEqual(failures, {})

    def test_snapshot_newer_cards_query_ok_returns_live(self):
        d = self.seed()
        self.write_snapshot(d)
        _calls, opener = self.fake_open(body())
        with opener, self.version(), mock.patch.object(usage, "_run_claude") as cli:
            result, _failures = usage.live_usage(["b001"], force=False)
        cli.assert_not_called()
        self.assertEqual(result["b001"]["source"], "live")
        self.assertEqual(report.reset_cards_payload("b001", "oauth")["count"], 2)

    def test_failure_reason_has_no_fallback_part(self):
        self.seed()
        _calls, opener = self.fake_open(error=403)
        with opener, self.version():
            _windows, why = usage.fetch_usage_live("b001")
        self.assertEqual(why, "令牌过期或失效（HTTP 403）")
        self.assertNotIn(TOKEN, why)


class ParseResetsTest(unittest.TestCase):
    def test_filters_invalid_cards(self):
        now = time.time()
        grants = [
            grant("ok_one", ends_at=None, label="", clears=["seven_day"], type="weekly", resets_left=1),
            grant("ok_two", resets_left=3),
            grant("expired", ends_at=iso(now - 60)),
            grant("future", starts_at=iso(now + HOUR)),
            grant("paused", paused=True),
            grant("used_up", resets_left=0),
            grant("BAD ID!"),
            grant("no_clears", clears=[]),
        ]
        entry = oauth_usage.parse_resets(body(grants), now)
        # 编号/停用/用完/clears 在解析时就丢；时间在显示时判
        self.assertEqual(len(entry["cards"]), 4)
        active = oauth_usage.active_cards(entry, now)
        self.assertEqual(sorted(c["resets_left"] for c in active), [1, 3])
        self.assertIsNone(next(c for c in active if c["resets_left"] == 1)["ends_at"])
        for card in entry["cards"]:
            self.assertEqual(set(card), set(oauth_usage.CARD_FIELDS))

    def test_cedar_null_or_missing(self):
        for b in (body(cedar=None), body(cedar=False)):
            entry = oauth_usage.parse_resets(b, time.time())
            self.assertFalse(entry["available"])
            self.assertEqual(entry["cards"], [])

    def test_ineligible(self):
        entry = oauth_usage.parse_resets(body([], eligible=False, reason="not_max_plan"), time.time())
        self.assertEqual((entry["eligible"], entry["ineligible_reason"]), (False, "not_max_plan"))


CJK = __import__("re").compile(r"[\u4e00-\u9fff]")


class ResetCellTest(HomeCase):
    """cca ls 的「重置卡」一格和 --json 的 reset_cards（进程内）。"""

    def cache(self, name, grants=None, **kw):
        usage.merge_cache(reset_cache_file(), {name: oauth_usage.parse_resets(body(grants, **kw), time.time())})

    def cell(self, name="b001", **kw):
        text = report.reset_cell_text(report.reset_cards_payload(name, "oauth"), **kw).plain
        self.assertIsNone(CJK.search(text), text)     # 格子里没有中文
        return text

    def test_count_and_remaining(self):
        """`张数 · 最早一张还剩多久`：8 天以上只写天、1～8 天 `3d 04h`、不到 1 天 `5h 23m`。"""
        cases = (
            (22 * 24 * HOUR + 30 * 60, "2 · 22d"),
            (3 * 24 * HOUR + 4 * HOUR + 30 * 60, "2 · 3d 04h"),
            (5 * HOUR + 23 * 60 + 30, "2 · 5h 23m"),
            (8 * 24 * HOUR + 60, "2 · 8d"),
            (7 * 24 * HOUR + 23 * HOUR + 30 * 60, "2 · 7d 23h"),
        )
        for left, want in cases:
            with self.subTest(want):
                self.seed()
                self.cache("b001", [grant(ends_at=iso(time.time() + left), resets_left=2)])
                self.assertEqual(self.cell(), want)

    def test_payload_and_narrow_date(self):
        self.seed()
        ends = time.time() + 30 * HOUR
        self.cache("b001", [grant(ends_at=iso(ends), resets_left=2)])
        info = report.reset_cards_payload("b001", "oauth")
        self.assertEqual((info["status"], info["count"]), ("cards", 2))
        want = datetime.fromtimestamp(int(ends), SHANGHAI)
        self.assertEqual(info["first_expires_at"], want.isoformat(timespec="seconds"))
        self.assertTrue(info["first_expires_at"].endswith("+08:00"))
        self.assertEqual(self.cell(countdown=False), f"2 · {want:%m-%d}")
        card = info["cards"][0]
        self.assertEqual(card["kind"], "5 小时")
        self.assertNotIn("id", card)

    def test_total_over_kinds_uses_earliest(self):
        self.seed()
        soon = time.time() + 5 * HOUR + 10 * 60 + 30
        self.cache("b001", [grant("grant_abc", label="周末卡", ends_at=None, resets_left=2),
                            grant("grant_def", clears=["five_hour", "seven_day"], resets_left=1,
                                  ends_at=iso(soon))])
        info = report.reset_cards_payload("b001", "oauth")
        self.assertEqual(info["count"], 3)
        self.assertEqual(self.cell(), "3 · 5h 10m")
        self.assertEqual(sorted(c["kind"] for c in info["cards"]), ["5 小时 + 7 天", "周末卡"])

    def test_never_expiring_cards(self):
        self.seed()
        self.cache("b001", [grant(ends_at=None, resets_left=2)])
        self.assertEqual(self.cell(), "2 · -")

    def test_status_detail_one_line_per_card(self):
        self.seed()
        soon = time.time() + 5 * HOUR
        self.cache("b001", [grant("grant_abc", label="周末卡", ends_at=None, resets_left=2),
                            grant("grant_def", clears=["five_hour", "seven_day"], resets_left=1,
                                  ends_at=iso(soon))])
        lines = report.reset_detail_text(report.reset_cards_payload("b001", "oauth")).splitlines()
        want = datetime.fromtimestamp(int(soon), SHANGHAI)
        self.assertEqual(lines[0], "共 3 张")
        self.assertIn("周末卡 ×2 · 不过期 · 清 5 小时", lines)
        self.assertIn(f"session ×1 · {want:%m-%d %H:%M} 到期 · 清 5 小时 + 7 天", lines)

    def test_status_detail_year_when_not_this_year(self):
        far = datetime(datetime.now(SHANGHAI).year + 1, 1, 2, 3, 4, tzinfo=SHANGHAI)
        self.assertEqual(report._card_when(far.isoformat()), f"{far:%Y-%m-%d %H:%M} 到期")  # noqa: SLF001

    def test_narrow_terminal_drops_resets_before_tier(self):
        """窄终端：新加的重置卡列先让位，原有的订阅、档位照旧。"""
        fixed = {"profile": 7, "five": 16, "seven": 16, "resets": 16, "tier": 6, "substate": 4}
        cols, with_email, _w = report._fit_list_columns(  # noqa: SLF001
            ["five", "seven", "resets", "tier", "substate"], fixed, 65)
        self.assertEqual(cols, ["five", "seven", "tier", "substate"])
        self.assertFalse(with_email)

    def test_no_cards_ineligible_unknown_api(self):
        self.seed("b001")
        self.seed("b002")
        self.seed("b003")
        self.cache("b001", cedar=None)
        self.cache("b002", [], eligible=False, reason="tenure")
        cells = {}
        for name in ("b001", "b002", "b003"):
            info = report.reset_cards_payload(name, "oauth")
            cells[name] = (info["status"], self.cell(name))
        self.assertEqual(cells, {"b001": ("none", "0"), "b002": ("ineligible", "0"),
                                 "b003": ("unknown", "?")})
        info = report.reset_cards_payload("b002", "oauth")
        self.assertEqual(info["ineligible_reason"], "tenure")
        self.assertEqual(report.reset_detail_text(info), "0（没资格：tenure（开通时间不够））")
        self.assertTrue(report.reset_detail_text(report.reset_cards_payload("b003", "oauth")).startswith("?"))
        api = report.reset_cards_payload("ds", "api")
        self.assertEqual((api["status"], report.reset_cell_text(api, is_api=True).plain), ("unknown", "-"))

    def test_cards_expire_while_cached(self):
        """缓存放 1 小时：这期间到期的卡显示时就不算了；缓存旧过 1 小时 = 查不了。"""
        self.seed()
        self.cache("b001", [grant(ends_at=iso(time.time() + 60))])
        self.assertEqual(report.reset_cards_payload("b001", "oauth", time.time() + 120)["status"], "none")
        self.assertEqual(report.reset_cards_payload("b001", "oauth", time.time() + 2 * HOUR)["status"],
                         "unknown")


class LsResetsCliTest(unittest.TestCase):
    """`cca ls` 的「重置卡」列走 CCA_OAUTH_USAGE_FILE 桩文件：不联网、不起真的 claude。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.stub = self.home / "stub-bin"
        self.stub.mkdir()
        fake = self.stub / "claude"
        fake.write_text('#!/bin/sh\necho "$@" >> "$HOME/claude-called"\nexit 1\n', encoding="utf-8")
        fake.chmod(0o755)
        for name, scopes in (("b001", ["user:profile"]), ("b002", ["user:profile"]),
                             ("b003", ["user:profile"]), ("b004", ["user:inference"]),
                             ("b005", ["user:profile"])):
            d = self.home / ".claude-accounts" / name
            d.mkdir(parents=True)
            (d / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
                "accessToken": TOKEN, "scopes": scopes, "subscriptionType": "max",
                "expiresAt": int((time.time() + HOUR) * 1000)}}), encoding="utf-8")
        self.ends = time.time() + 30 * HOUR
        self.fixture = self.home / "oauth-usage.json"
        self.fixture.write_text(json.dumps({
            "b001": body([grant("grant_abc", label="周末送的卡", ends_at=None),
                          grant("grant_def", clears=["five_hour", "seven_day"], resets_left=1)]),
            "b002": body(cedar=None),
            "b003": body([], eligible=False, reason="not_max_plan"),
            "b004": body(),
            "b005": body([grant("grant_ghi", ends_at=iso(self.ends), resets_left=2),
                          grant("grant_old", ends_at=iso(time.time() - 60))]),
        }), encoding="utf-8")

    def run_cli(self, *args, fixture=None, columns=200):
        env = {"HOME": str(self.home), "PATH": f"{self.stub}:{os.environ.get('PATH', '')}",
               "CCA_OFFLINE": "1", "CCA_WATCHDOG_AUTOSTART": "0", "CCA_AUTO_PLUGIN": "0",
               "COLUMNS": str(columns),
               "CCA_OAUTH_USAGE_FILE": str(fixture or self.fixture),
               "CCA_SUBSCRIPTION_LIVE_FILE": str(self.home / "no-sub.json"),
               "CCA_PROXY": "http://localhost:7897", "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, str(REPO / "bin" / "claude-acct"), *args],
                              env=env, capture_output=True, text=True, timeout=60, check=False)

    def assert_no_secrets(self, text):
        for secret in (TOKEN, *GRANT_IDS, "grant_old"):
            self.assertNotIn(secret, text)

    def test_json_reset_cards(self):
        r = self.run_cli("ls", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = {row["profile"]: row["reset_cards"] for row in json.loads(r.stdout)["profiles"]}
        self.assertEqual({k: v["status"] for k, v in rows.items()},
                         {"b001": "cards", "b002": "none", "b003": "ineligible", "b004": "unknown",
                          "b005": "cards"})
        self.assertEqual((rows["b001"]["count"], rows["b005"]["count"]), (3, 2))
        self.assertEqual(rows["b003"]["ineligible_reason"], "not_max_plan")
        self.assertIn("user:profile", rows["b004"]["reason"])
        self.assertIsNone(rows["b004"]["count"])
        weekend = next(c for c in rows["b001"]["cards"] if c["kind"] == "周末送的卡")
        self.assertIsNone(weekend["ends_at"])
        self.assertEqual(set(weekend), {"kind", "label", "type", "resets_left", "resets_total",
                                        "starts_at", "ends_at", "clears"})
        want = datetime.fromtimestamp(int(self.ends), SHANGHAI).isoformat(timespec="seconds")
        self.assertEqual(rows["b005"]["first_expires_at"], want)
        self.assert_no_secrets(r.stdout + r.stderr)
        for cache in (self.home / ".claude-accounts").glob("_*.json"):
            self.assert_no_secrets(cache.read_text(encoding="utf-8"))
        # 额度全走 HTTP 桩：一次也没起 claude -p /usage
        called = self.home / "claude-called"
        self.assertNotIn("/usage", called.read_text(encoding="utf-8") if called.exists() else "")

    def test_table_column(self):
        r = self.run_cli("ls")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("reset 卡", r.stdout)
        header = next(line for line in r.stdout.splitlines() if line.startswith("PROFILE"))
        self.assertLess(header.index("7 天(已用)"), header.index("重置卡"))
        lines = {line.split()[0]: line for line in r.stdout.splitlines() if line.startswith("b00")}
        self.assertRegex(lines["b001"], r" 3 · (1d 23h|2d 00h) ")   # 不过期的卡 + 48 小时后到期的卡
        self.assertRegex(lines["b005"], r" 2 · 1d 0[56]h ")
        self.assertRegex(lines["b002"], r" 0 ")
        self.assertRegex(lines["b003"], r" 0 ")
        self.assertIn(" ? ", lines["b004"])
        for line in lines.values():
            self.assertNotIn("张", line)
            self.assertNotIn("无资格", line)
        self.assert_no_secrets(r.stdout + r.stderr)
        self.assertNotIn("claude -p", r.stdout + r.stderr)
        self.assertNotIn("30 秒", r.stderr)

    def test_notes_only_when_needed(self):
        """表尾的重置卡说明：? 的部分只在出现 ? 时打；额度走 HTTP 的说明不再每次打。"""
        full = self.run_cli("ls")
        self.assertIn("重置卡 = 张数 · 最早一张还剩多久；? = 没查到", full.stdout)
        only = self.run_cli("ls", "b001")
        self.assertEqual(only.returncode, 0, only.stderr)
        self.assertIn("重置卡 = 张数 · 最早一张还剩多久", only.stdout)
        self.assertNotIn("? = 没查到", only.stdout)
        self.assertNotIn("额度由 cca 直接请求", full.stdout + only.stdout)

    def test_notes_follow_narrow_and_dropped_column(self):
        """窄表格子写到期日，说明跟着写到期日；重置卡列被省掉时说明行也不打。"""
        narrow = self.run_cli("ls", "b001", columns=80)
        self.assertEqual(narrow.returncode, 0, narrow.stderr)
        header = next(line for line in narrow.stdout.splitlines() if line.startswith("PROFILE"))
        self.assertIn("重置卡", header)
        self.assertRegex(narrow.stdout, r" 3 · \d\d-\d\d ")
        self.assertIn("重置卡 = 张数 · 最早一张的到期日", narrow.stdout)
        self.assertNotIn("最早一张还剩多久", narrow.stdout)
        tiny = self.run_cli("ls", columns=40)
        self.assertEqual(tiny.returncode, 0, tiny.stderr)
        header = next(line for line in tiny.stdout.splitlines() if line.startswith("PROFILE"))
        self.assertNotIn("重置卡", header)
        self.assertNotIn("重置卡 =", tiny.stdout)

    def test_failed_profile_shows_cache_and_reason(self):
        """HTTP 429：不起 claude，表尾写「实时额度没取到: b001（HTTP 429 限流）」。"""
        failing = self.home / "fail.json"
        failing.write_text(json.dumps({"b001": {"_http_status": 429}}), encoding="utf-8")
        r = self.run_cli("ls", "b001", "--refresh", fixture=failing)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("实时额度没取到: b001（HTTP 429 限流）", r.stdout)
        called = self.home / "claude-called"
        self.assertNotIn("/usage", called.read_text(encoding="utf-8") if called.exists() else "")

    def test_status_lists_each_card(self):
        self.run_cli("ls", "b001")
        r = self.run_cli("status", "b001")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("重置卡", r.stdout)
        self.assertIn("周末送的卡 ×2 · 不过期 · 清 5 小时", r.stdout)
        self.assertIn("session ×1", r.stdout)
        self.assertIn("清 5 小时 + 7 天", r.stdout)
        self.assert_no_secrets(r.stdout + r.stderr)

    def test_cache_then_refresh(self):
        self.run_cli("ls", "b001", "--json")
        empty = self.home / "empty.json"
        empty.write_text("{}", encoding="utf-8")
        cached = json.loads(self.run_cli("ls", "b001", "--json", fixture=empty).stdout)["profiles"][0]
        self.assertEqual(cached["reset_cards"]["count"], 3)        # 1 小时内用缓存里的卡
        fresh = json.loads(self.run_cli("ls", "b001", "--json", "--refresh", fixture=empty).stdout)
        self.assertEqual(fresh["profiles"][0]["reset_cards"]["count"], 3)   # 这次没查到：旧卡照用

    def test_no_resets_command(self):
        r = self.run_cli("resets")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("cca resets", self.run_cli("--help").stdout)
        self.assertNotIn("cca resets", (REPO / "README.md").read_text(encoding="utf-8"))
        self.assertFalse(hasattr(report, "cmd_resets"))
        self.assertFalse(hasattr(usage, "live_resets"))


if __name__ == "__main__":
    unittest.main()
