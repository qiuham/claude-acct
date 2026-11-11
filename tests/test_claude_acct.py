"""claude-acct 测试：全部使用临时 HOME 与假 claude，不访问网络。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unicodedata
import unittest
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# cca 的终端显示固定上海（见 env.DISPLAY_TZ），测子进程输出时也得同一个基准，
# 否则本机时区一换，期望时间就错一小时。
os.environ["TZ"] = "Asia/Shanghai"
time.tzset()

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: E402,F401 - 先于 cca 导入：HOME 指到临时目录，测试不碰真实 ~/.claude-accounts

from cca.api import identity  # noqa: E402
from cca.config import API_DISALLOWED_TOOLS  # noqa: E402
from cca.report import format_remaining  # noqa: E402

# 测试侧固定的 DeepSeek 预设变量（与实现解耦：实现改错这里就能发现）
BUILD_ENV_CODE = """
import sys
from cca.env import build_env
env = build_env(sys.argv[2])
for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
            "HTTPS_PROXY", "NO_PROXY", "CLAUDE_CONFIG_DIR",
            "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
            "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_MESSAGING_SOCKET",
            "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_ENTRYPOINT",
            "CLAUDE_CODE_NO_FLICKER"):
    print(f"{key} = {env.get(key)}")
"""

LAST_MODEL_PROBE = """
import sys
from cca.session.push import last_session_model
print(last_session_model(__import__("pathlib").Path(sys.argv[2])))
"""

WINDOW_PROBE = """
import json, sys
from cca.env import compact_window_for
catalog = json.loads(sys.argv[2])
for slug in ("openrouter/free", "qwen/qwen3.8-27b:free", "anthropic/claude-opus-4-8",
             "nobody/unknown-model"):
    print(f"{slug}={compact_window_for(slug, catalog)}")
"""

LAUNCH_ENV_PROBE = """
import sys
from cca.env import launch_env, model_flag_value
args = sys.argv[3:]
env = launch_env(sys.argv[2], args)
print(f"MODEL_FLAG = {model_flag_value(args)}")
for key in ("ANTHROPIC_MODEL", "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "ANTHROPIC_BASE_URL",
            "_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL"):
    print(f"{key} = {env.get(key)}")
"""

PUSH_CLASSIFY_PROBE = """
import sys, types
from cca.session.push import push_session_command, push_session_once

class Result:
    def __init__(self, code, out, err=""):
        self.returncode, self.stdout, self.stderr = code, out, err

case = sys.argv[2]
def fake_run(argv, **kwargs):
    if case == "running":
        return Result(1, "", "Error: That session is running in the background (abcde123). Run `claude attach abcde123` to open it, or `claude stop abcde123` first to resume it here.")
    if case == "limit":
        return Result(1, "You've hit your weekly limit", "")
    if case == "failed":
        return Result(2, "", "boom")
    if case == "env-blocked":
        return Result(1, "", "错误: 没有配置代理：出口不明，先给订阅号配代理（--proxy http://host:port）")
    if case == "copy":
        return Result(0, "note: background session abcde123 keeps its own saved options, so the "
                         "flags you passed started a copy as feedface-1111-2222-3333-444455556666. "
                         "Without flags, the same command continues abcde123 itself.\\n"
                         "backgrounded \u00b7 feedface", "")
    if case == "verify-limit":
        return Result(0, "backgrounded \u00b7 abcde123", "")
    if case == "verify-quiet":
        return Result(0, "backgrounded \u00b7 abcde123", "")
    return Result(0, "backgrounded \u00b7 abcde123", "")

if case == "verify-limit":
    verify = lambda profile, sid, **kw: ("retry", "额度还没放开（挂回面板后立刻又撞上）")
elif case == "verify-quiet":
    verify = lambda profile, sid, **kw: ("", "")
else:
    verify = lambda profile, sid, **kw: ("sent", "已挂回面板并在跑（attach 可见）")

calls = []
def recording_run(argv, **kwargs):
    calls.append(list(argv))
    return fake_run(argv, **kwargs)

state, note = push_session_once("001", sys.argv[3], "继续", "/tmp", run=recording_run,
                                       verify=verify)
print("state=%s" % state)
print("note=%s" % note)
print("cmd=%s" % " ".join(push_session_command("001", "abcde123", "继续")))
for call in calls:
    print("call=%s" % " ".join(call))
"""

PUSH_JOB_PROBE = """
import sys
from cca.session import takeover
from cca.session.push import push_session_once, wait_job_id

takeover.deliver_after_resume = lambda account, session, text, **kw: calls.append(["deliver", text]) or ""
moving = sys.argv[2] == "moving"
full = sys.argv[3]
calls = []

class Result:
    def __init__(self):
        self.returncode, self.stdout, self.stderr = 0, "backgrounded \u00b7 abcde123", ""

def fake_run(argv, **kwargs):
    calls.append(list(argv))
    return Result()

state, note = push_session_once("001", full, "继续", "/tmp", run=fake_run,
                                       motion=lambda profile, job: moving,
                                       verify=lambda profile, sid, **kw: ("sent", "挂回面板"))
print("state=%s" % state)
print("note=%s" % note)
print("job=%s" % wait_job_id("001", full))
for call in calls:
    print("call=%s" % " ".join(call))
"""

API_ARGS_PROBE = """
import sys
from cca.api.client import api_launch_args
for name in sys.argv[2:]:
    print("%s -> %s" % (name, " ".join(api_launch_args(name, ["--bg", "--resume", "abc"]))))
"""

AGENTS_PROBE = """
import sys
import subprocess
import shutil
import cca.session.push as session_push
from cca.session.push import _agents_sessions
import json, pathlib, types
payload = sys.argv[2]
subprocess.run = lambda *a, **kw: types.SimpleNamespace(
    returncode=0, stdout=payload, stderr="")
shutil.which = lambda name: "/usr/bin/true"
session_push.profile_dir = lambda name: pathlib.Path("/tmp/fake-profile")
pathlib.Path("/tmp/fake-profile/daemon").mkdir(parents=True, exist_ok=True)
for item in _agents_sessions("ds"):
    print("id=%s job=%s state=%s name=%s" % (item["id"], item["job"], item["state"], item["name"]))
"""

FOLLOW_PROBE = """
import sys
from cca.api.client import follow_candidates, follow_plan
available = ["gpt-6-sol", "gpt-6.1-sol", "gpt-6.1-sol-preview", "gpt-6-astra", "gpt-5.6-sol",
             "deepseek-v4-flash", "deepseek-v4-pro", "weird-name", "gpt-6-luna"]
configured = {"main": "gpt-6-astra", "sub": "deepseek-v4-flash",
              "opus": "gpt-6-sol", "sonnet": "gpt-5.6-sol", "haiku": "gpt-6-luna",
              "broken": "weird-name"}
for key, old, new in follow_plan(configured, available):
    print("plan %s %s -> %s" % (key, old, new))
print("candidates gpt-6-sol:", follow_candidates("gpt-6-sol", available))
print("candidates weird-name:", follow_candidates("weird-name", available))
"""













CATALOG_PROBE = """
import sys
from cca.api.client import load_model_catalog
print([m["id"] for m in load_model_catalog()])
"""

PROVIDERS_ENV = {
    "deepseek": {
        "ANTHROPIC_MODEL": "deepseek-flash[1m]",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-flash[1m]",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-flash[1m]",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-flash",
        "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-flash",
        "CLAUDE_CODE_EFFORT_LEVEL": "xhigh",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "786432",
    }
}
SCRIPT = REPO / "bin" / "claude-acct"
SRC = REPO / "src"

def display_width_of(line: str) -> int:
    """测试侧算终端显示宽度（中文/全角按 2 列），不复用被测代码以免同错同对。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in line)


FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
out = os.environ.get("FAKE_CLAUDE_OUT")
if out:
    with open(out, "w") as fh:
        json.dump({
            "argv": sys.argv[1:],
            "config_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
            "https_proxy": os.environ.get("HTTPS_PROXY"),
            "http_proxy": os.environ.get("HTTP_PROXY"),
            "browser": os.environ.get("BROWSER"),
        }, fh)
log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(" ".join(sys.argv[1:]) + "\\n")
cwd_log = os.environ.get("FAKE_CLAUDE_CWD_LOG")
if cwd_log:
    with open(cwd_log, "a") as fh:
        fh.write(" ".join(sys.argv[1:]) + "\\t" + os.getcwd() + "\\n")
if sys.argv[1:3] == ["agents", "--json"]:
    payload = os.environ.get("FAKE_CLAUDE_AGENTS")
    if payload is not None:
        sys.stdout.write(payload)
if sys.argv[1:2] == ["logs"]:
    stale = set(filter(None, os.environ.get("FAKE_CLAUDE_STALE_IDS", "").split(",")))
    if sys.argv[2] in stale:
        sys.stderr.write("Couldn't read logs for %s - job not found - it may have already exited\\n" % sys.argv[2])
        sys.exit(1)
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
"""


class CliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        # ~/.claude 只放共享资产；001 和其它号一样住在 ~/.claude-accounts 下。
        cdir = self.home / ".claude"
        cdir.mkdir()
        for item in ("skills", "projects", "plugins"):
            (cdir / item).mkdir()
        (cdir / "CLAUDE.md").write_text("", encoding="utf-8")
        acct = self.home / ".claude-accounts" / "001"
        acct.mkdir(parents=True)
        for item in ("projects", "skills", "plugins", "CLAUDE.md"):
            (acct / item).symlink_to(cdir / item)
        (acct / "settings.json").write_text('{"theme": "dark"}', encoding="utf-8")
        # 新号播种读的默认 settings（和 001 那份内容一样，方便沿用老断言）
        defaults = self.home / ".cca" / "defaults"
        defaults.mkdir(parents=True)
        (defaults / "settings.json").write_text('{"theme": "dark"}', encoding="utf-8")
        (acct / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "fake-token"}}), encoding="utf-8"
        )
        (acct / ".claude.json").write_text(
            json.dumps({
                "oauthAccount": {
                    "emailAddress": "claude001@example.com",
                    "organizationRateLimitTier": "default_claude_max_5x",
                }
            }),
            encoding="utf-8",
        )
        # handoff 把家目录的会话改到工作目录接着跑；测试里也备一个 ~/llm。
        (self.home / "llm").mkdir()
        self.stub = self.home / "stub-bin"
        self.stub.mkdir()
        fake = self.stub / "claude"
        fake.write_text(FAKE_CLAUDE, encoding="utf-8")
        fake.chmod(0o755)
        for alias in ("cca", "ccl"):
            (self.stub / alias).symlink_to(SCRIPT)

    def tearDown(self):
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def env(self, **extra):
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.stub}:{os.environ.get('PATH', '')}",
            "CCA_OFFLINE": "1",          # 测试不查出口信息，避免联网拖慢
            "CCA_AUTO_PLUGIN": "0",      # 建号时不自动装面板（那会真跑一次 claude）
            "CCA_WATCHDOG_AUTOSTART": "0",   # 不真的拉起换号常驻进程
            # 额度/订阅默认用不存在的 fixture：走本地快照，不起 claude、不联网
            "CCA_USAGE_LIVE_FILE": str(self.home / "no-usage.json"),
            "CCA_SUBSCRIPTION_LIVE_FILE": str(self.home / "no-sub.json"),
            "FAKE_CLAUDE_OUT": str(self.home / "claude-call.json"),
            "FAKE_CLAUDE_LOG": str(self.home / "claude-calls.log"),
            "FAKE_CLAUDE_CWD_LOG": str(self.home / "claude-cwd.log"),
            "PYTHONDONTWRITEBYTECODE": "1",
            # 非 tty 时 Rich 用 COLUMNS 决定表格宽度；固定成宽终端，断言才稳定。
            "COLUMNS": "200",
            # 测试环境用一个必定可解析的代理，doctor 的代理解析检查才不会随环境摇摆；
            # "未设置 CCA_PROXY 时注入内建代理"由 test_build_env_proxy_modes 单独覆盖。
            "CCA_PROXY": "http://localhost:7897",
        }
        env.update(extra)
        return env

    def calls(self):
        """假 claude 的调用流水（每次追加一行）。"""
        p = self.home / "claude-calls.log"
        return p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    def exe(self, argv0):
        return SCRIPT if argv0 == "claude-acct" else self.stub / argv0

    def run_cli(self, *args, argv0="claude-acct", stdin_data=None, **envextra):
        return subprocess.run(
            [str(self.exe(argv0)), *args],
            env=self.env(**envextra),
            capture_output=True,
            text=True,
            timeout=30,
            input=stdin_data,
            check=False,
        )

    def record(self):
        return json.loads((self.home / "claude-call.json").read_text(encoding="utf-8"))

    def acct_dir(self, name):
        return self.home / ".claude-accounts" / name

    def profile_base(self, name):
        return self.acct_dir(name)

    def seed_daemon_dir(self, name="001"):
        """daemon 目录存在才会去问 claude agents（不给从未跑过的配置目录制造副作用）。"""
        (self.profile_base(name) / "daemon").mkdir(parents=True, exist_ok=True)

    def seed_roster(self, name, pid):
        self.seed_daemon_dir(name)
        (self.profile_base(name) / "daemon" / "roster.json").write_text(
            json.dumps({"workers": {"w1": {"pid": pid, "sessionId": "s1", "cwd": "/x"}}}),
            encoding="utf-8",
        )

    def agents_json(self, *sessions):
        """构造 claude agents --json 输出；sessions 每项为 (state, name)。"""
        return json.dumps(
            [
                {
                    "id": f"{i:08d}",
                    "kind": "background",
                    "name": name,
                    "cwd": "/x",
                    "sessionId": f"{i:08d}-0000-0000-0000-000000000000",
                    "state": state,
                }
                for i, (state, name) in enumerate(sessions)
            ]
        )

    def seed_transcript(self, name, session, *, title=None, ai=None, prompt="", cwd="/proj/x",  # noqa: PLR0913 - 测试造数据，参数各自直白
                        project="-proj-x", age_hours=0.1, trailing_cwd=None):
        """造一条假 transcript（含 CLI 真实写入的标题类记录）。"""
        d = self.profile_base(name) / "projects" / project
        d.mkdir(parents=True, exist_ok=True)
        lines = [{"type": "user", "sessionId": session, "cwd": cwd, "gitBranch": "main"}]
        if ai:
            lines.append({"type": "ai-title", "aiTitle": ai, "sessionId": session})
        if title:
            lines.append({"type": "custom-title", "customTitle": title, "sessionId": session})
        if prompt:
            lines.append({"type": "last-prompt", "lastPrompt": prompt, "sessionId": session})
        if trailing_cwd:
            lines.append({"type": "attachment", "sessionId": session, "cwd": trailing_cwd})
        f = d / f"{session}.jsonl"
        f.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
        ts = time.time() - age_hours * 3600
        os.utime(f, (ts, ts))
        return f

    # -- 解析 ------------------------------------------------------------
    def test_flag_before_profile_guides(self):
        """`cca -c` 这种"参数写在号名前面"的手误：说清正确写法，而不是去找名为 -c 的号。"""
        self.run_cli("new", "002")
        r = self.run_cli("-c")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("跟在号名后面", r.stderr)
        self.assertIn("cca 002 -c", r.stderr)          # 例子写清"号在前"
        self.assertNotIn("claude-acct new -c", r.stderr)

    def test_per_command_help(self):
        """`cca <命令> --help` 不再是"未知参数"，而是该命令的用法（含闸门与例子）。"""
        r = self.run_cli("rm", "--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        for expect in ("cca rm <profile>", "--force", "闸门", "不删什么", "输入 profile 名"):
            self.assertIn(expect, r.stdout)
        r = self.run_cli("handoff", "--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--only", r.stdout)
        self.assertIn("--dry-run", r.stdout)
        self.assertIn("cca ds stop-bg", r.stdout)      # 手工等价的两步
        r = self.run_cli("add", "--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--provider deepseek --key", r.stdout)
        self.assertEqual(sorted(p.name for p in (self.home / ".claude-accounts").iterdir()), ["001"])

    def test_which_requires_profile(self):
        """不再有"当前号"：问问自己在哪个号，得把号写出来。"""
        r = self.run_cli("which")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("要写清哪个号", r.stderr)

    def test_which_prints_one_line_for_profile(self):
        self.run_cli("new", "002")
        r = self.run_cli("which", "002")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(r.stdout.strip().splitlines()), 1)
        self.assertIn("002", r.stdout)

    def test_bare_run_prints_help(self):
        """裸跑 = 没想好要干什么，不是错误：直接给帮助，退出码 0（与 kubectl 一致）。"""
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("cca <号> [claude 参数…]", r.stdout)
        self.assertEqual(r.stderr, "")
        self.assertFalse((self.home / "claude-call.json").exists())

    def test_bare_run_help_matches_dash_dash_help(self):
        self.assertEqual(self.run_cli().stdout, self.run_cli("--help").stdout)

    def test_every_subcommand_has_its_own_help(self):
        """每个子命令的 --help 都是它自己的用法，不退回总帮助。"""
        from cca.config import COMMAND_USAGE, SUBCOMMANDS  # noqa: PLC0415

        self.assertEqual(sorted(set(SUBCOMMANDS) - set(COMMAND_USAGE) - {"help"}), [])
        for command in ("sideagent", "mcp", "mv", "import", "wake", "ls"):
            r = self.run_cli(command, "--help")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotEqual(r.stdout, self.run_cli("--help").stdout, command)
            self.assertIn(f"cca {'ls' if command == 'ls' else command}", r.stdout)

    def test_top_level_help_lists_every_command(self):
        r = self.run_cli("--help")
        listed = set(re.findall(r"cca ([a-z][a-z0-9-]+)", r.stdout))
        from cca.config import SUBCOMMANDS  # noqa: PLC0415

        hidden = {"help", "list", "rename"}           # 别名，不单列
        self.assertEqual(sorted(set(SUBCOMMANDS) - hidden - listed), [])

    def test_flag_before_profile_points_at_correct_form(self):
        r = self.run_cli("-c")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cca <profile>", r.stderr)
        self.assertIn("cca --help", r.stderr)

    def test_claude_acct_env_no_longer_selects(self):
        """$CLAUDE_ACCT 这一层已取消：写谁就是谁。"""
        self.run_cli("new", "002")
        r = self.run_cli("which", "002", CLAUDE_ACCT="001")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("002", r.stdout)

    # -- 建号 ------------------------------------------------------------
    def test_new_creates_shared_links_and_seed(self):
        r = self.run_cli("new", "002")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = self.acct_dir("002")
        self.assertTrue((d / "projects").is_symlink())
        self.assertTrue((d / "skills").is_symlink())
        self.assertEqual((d / "settings.json").read_text(encoding="utf-8"), '{"theme": "dark"}')
        self.assertFalse((d / ".credentials.json").exists())

    def test_new_seeds_from_default_settings(self):
        """播种源是 ~/.cca/defaults/settings.json，跟任何账号无关。"""
        defaults = self.home / ".cca" / "defaults" / "settings.json"
        defaults.write_text('{"theme": "light", "ultracode": true}', encoding="utf-8")
        acct001 = self.acct_dir("001")
        (acct001 / "settings.json").write_text('{"theme": "solarized"}', encoding="utf-8")
        r = self.run_cli("new", "002")
        self.assertEqual(r.returncode, 0, r.stderr)
        seeded = json.loads((self.acct_dir("002") / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(seeded["theme"], "light")
        self.assertTrue(seeded["ultracode"])

    def test_new_without_default_settings_seeds_nothing(self):
        """没有默认文件就不播种，也不去 001 或 ~/.claude 找（兼容放在迁移脚本里）。"""
        (self.home / ".cca" / "defaults" / "settings.json").unlink()
        r = self.run_cli("new", "002")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.acct_dir("002") / "settings.json").exists())
        self.assertFalse((self.home / ".cca" / "defaults" / "settings.json").exists())

    def test_new_refuses_nonempty_dir(self):
        self.run_cli("new", "002")
        r = self.run_cli("new", "002")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("已存在", r.stderr)

    def test_new_rejects_reserved_name(self):
        self.assertNotEqual(self.run_cli("new", "doctor").returncode, 0)

    # -- 运行 ------------------------------------------------------------
    def test_run_sets_config_dir_for_non_default(self):
        self.run_cli("new", "002")
        r = self.run_cli("002", "-c", "--foo")
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = self.record()
        self.assertEqual(rec["argv"], ["-c", "--foo"])
        self.assertEqual(rec["config_dir"], str(self.acct_dir("002")))
        self.assertTrue(rec["https_proxy"])

    def test_run_sets_config_dir_for_default_profile(self):
        # 所有号一视同仁：001 也显式设 CLAUDE_CONFIG_DIR
        r = self.run_cli("001", CLAUDE_CONFIG_DIR="/tmp/elsewhere")  # noqa: S108 - 测试里只是一个路径字符串
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.record()["config_dir"], str(self.acct_dir("001")))

    def test_run_unknown_profile_fails(self):
        r = self.run_cli("009")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("不存在", r.stderr)

    def test_run_falls_back_to_roster_when_agents_unavailable(self):
        """agents 不可用（无输出/非 JSON）时，沿用 roster 的 pid 存活判据并保守提示。"""
        self.run_cli("new", "002")
        proc = subprocess.Popen(["sleep", "30"])
        try:
            self.seed_roster("001", proc.pid)
            r = self.run_cli("002")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("串行", r.stderr)
            self.assertIn("001", r.stderr)
        finally:
            proc.terminate()
            proc.wait()

    def test_run_ignores_stale_roster_when_agents_reports_none(self):
        """agents 报空时，roster 里还活着的 pid 不该再触发串行警告（旧实现的主要误报）。"""
        self.run_cli("new", "002")
        proc = subprocess.Popen(["sleep", "30"])
        try:
            self.seed_roster("001", proc.pid)
            r = self.run_cli("002", FAKE_CLAUDE_AGENTS="[]")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("串行", r.stderr)
            self.assertNotIn("警告", r.stderr)
        finally:
            proc.terminate()
            proc.wait()

    def test_run_warns_when_other_profile_is_running(self):
        self.run_cli("new", "002")
        self.seed_daemon_dir("001")
        r = self.run_cli("002", FAKE_CLAUDE_AGENTS=self.agents_json(("busy", "sam")))
        self.assertIn("串行", r.stderr)
        self.assertIn("sam", r.stderr)

    def test_run_only_notes_parked_sessions(self):
        self.run_cli("new", "002")
        self.seed_daemon_dir("001")
        payload = self.agents_json(("blocked", "awaits input"), ("done", "finished"))
        r = self.run_cli("002", FAKE_CLAUDE_AGENTS=payload)
        self.assertIn("提示", r.stderr)
        self.assertNotIn("警告", r.stderr)
        self.assertNotIn("串行", r.stderr)

    def test_run_treats_unrecognized_state_as_running(self):
        """出现没见过的状态词（如 working）时保守按"在跑"处理，守护不能静默失效。"""
        self.run_cli("new", "002")
        self.seed_daemon_dir("001")
        r = self.run_cli("002", FAKE_CLAUDE_AGENTS=self.agents_json(("working", "job")))
        self.assertIn("串行", r.stderr)

    def test_stop_bg_skips_unrecognized_running_state(self):
        self.run_cli("new", "002")
        self.seed_daemon_dir("001")
        r = self.run_cli("stop-bg", "001", FAKE_CLAUDE_AGENTS=self.agents_json(("working", "job")))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([c for c in self.calls() if c.startswith("stop ")], [])

    # -- 登录透传 --------------------------------------------------------
    def test_login_passes_args_through(self):
        self.run_cli("new", "002")
        r = self.run_cli("login", "002", "--email", "claude002@example.com")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.record()["argv"], ["auth", "login", "--email", "claude002@example.com"])

    # -- 展示 ------------------------------------------------------------
    def test_ccl_lists_identities(self):
        self.run_cli("new", "002")
        (self.acct_dir("002") / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "claude002@example.com"}}), encoding="utf-8"
        )
        r = self.run_cli(argv0="ccl")
        self.assertIn("claude001@example.com", r.stdout)
        self.assertIn("claude002@example.com", r.stdout)
        # 不再有"当前号"标记：账号全靠命令里写出来
        self.assertNotIn("*", r.stdout)

    def test_status_json(self):
        """点名一个号 = 一个对象（脚本照旧）；不带号才是数组。"""
        data = json.loads(self.run_cli("status", "001", "--json").stdout)
        self.assertEqual(data["profile"], "001")
        self.assertTrue(data["logged_in"])
        self.assertNotIn("binding", data)
        self.assertNotIn("current", data)

    def test_status_without_profile_summarizes_all(self):
        self.run_cli("new", "002")
        r = self.run_cli("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        for name in ("001", "002"):
            self.assertIn(name, r.stdout)
        self.assertNotIn("← 当前", r.stdout)

    def test_status_with_profile_still_single(self):
        self.run_cli("new", "002")
        r = self.run_cli("status", "001")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("002", r.stdout.split("后台会话")[0])

    def test_profile_kind_and_marker(self):
        """有清单 = API 号；没有 = 订阅号；清单坏了要报错而不是当订阅号混过去。"""
        self.run_cli("new", "002")
        self.assertEqual(self.kind_of("001"), "oauth")
        self.assertEqual(self.kind_of("002"), "oauth")
        marker = self.acct_dir("002") / ".cca-profile.json"
        marker.write_text(
            json.dumps({"kind": "api", "provider": "deepseek",
                        "base_url": "https://api.deepseek.com/anthropic",
                        "env": {"ANTHROPIC_MODEL": "deepseek-flash[1m]"}}),
            encoding="utf-8",
        )
        self.assertEqual(self.kind_of("002"), "api")
        marker.write_text("{ 坏 JSON", encoding="utf-8")
        r = self.run_cli("status", "002")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("无法解析", r.stderr)

    def test_api_key_file_and_mask(self):
        """密钥文件 0600；掩码只给前 6 后 4，完整 key 不出现在任何返回里。"""
        self.run_cli("new", "002")
        secret = "sk-ant-oat01-SUPERSECRET-VALUE"  # noqa: S105 - 测试用的假密钥
        out = self._run_python(
            """
            import sys
            from cca.paths import key_file
            from cca.account.store import mask_key, read_api_key, write_api_key
            write_api_key(sys.argv[2], sys.argv[3])
            print("READ", read_api_key(sys.argv[2]))
            print("MASK", mask_key(read_api_key(sys.argv[2])))
            print("MODE", oct(key_file(sys.argv[2]).stat().st_mode & 0o777))
            """,
            "002",
            secret,
        )
        lines = out.stdout.strip().splitlines()
        self.assertEqual(lines[0], f"READ {secret}")
        self.assertEqual(lines[1], "MASK sk-ant…ALUE")
        self.assertEqual(lines[2], "MODE 0o600")
        self.assertNotIn(secret, lines[1])

    def test_write_api_key_rejects_empty(self):
        """空 key 是最坏的"看起来成功"：直接拒绝，且不留空文件。"""
        self.run_cli("new", "002")
        out = self._run_python(
            """
            import sys
            from cca.account.store import write_api_key
            try:
                write_api_key(sys.argv[2], "   ")
            except SystemExit as exc:
                print("EXIT", exc.code)
            """,
            "002",
        )
        self.assertIn("EXIT", out.stdout)
        self.assertFalse((self.acct_dir("002") / ".api-key").exists())

    def test_render_settings_env_keeps_other_keys(self):
        """settings.json 是渲染产物：写端点/模型/apiKeyHelper，保留其它键，全文不含密钥。

        `modelSettings` / `permissions` 是**使用者手改**的（逐模型推理档、放行模式），重渲染必须
        原样留着 —— 它们不在清单里，丢了没人会知道。`model` 例外：它跟清单的 `ANTHROPIC_MODEL`
        对齐（API 号的主模型只认清单，留一个播种来的旧值就是第二个真相）。
        """
        self.seed_api_profile("002")
        (self.acct_dir("002") / "settings.json").write_text(
            json.dumps(
                {
                    "theme": "dark",
                    "model": "gpt-6-astra",
                    "modelSettings": {"gpt-6-astra": {"effortLevel": "medium"}},
                    "permissions": {"defaultMode": "bypassPermissions"},
                    "env": {"MY_FLAG": "1"},
                }
            ),
            encoding="utf-8",
        )
        out = self._run_python(
            """
            import json, sys
            from cca.paths import profile_dir
            from cca.account.store import read_marker, write_settings_env
            marker, error = read_marker(sys.argv[2])
            assert not error, error
            write_settings_env(sys.argv[2], marker)
            path = profile_dir(sys.argv[2]) / "settings.json"
            print(path.read_text(encoding="utf-8"))
            """,
            "002",
        )
        data = json.loads(out.stdout)
        self.assertEqual(data["theme"], "dark")
        # `model` 是唯一例外：API 号的主模型真相在清单 env，这里跟它对齐（避免"两个真相"）。
        self.assertEqual(data["model"], data["env"]["ANTHROPIC_MODEL"])
        self.assertEqual(data["modelSettings"], {"gpt-6-astra": {"effortLevel": "medium"}})
        self.assertEqual(data["permissions"], {"defaultMode": "bypassPermissions"})
        self.assertEqual(data["env"]["MY_FLAG"], "1")
        self.assertEqual(data["env"]["ANTHROPIC_BASE_URL"], "https://api.deepseek.com/anthropic")
        self.assertEqual(data["env"]["ANTHROPIC_MODEL"], "deepseek-flash[1m]")
        self.assertIn(str(self.acct_dir("002") / ".api-key"), data["apiKeyHelper"])
        self.assertNotIn("sk-ant", out.stdout)

    def test_parse_flags_collects_repeatable_values(self):
        """`cca handoff --only=a --only=b` 要按出现顺序收成列表（字典存值会互相覆盖）。"""
        out = self._run_python(
            """
            import sys
            from cca.cli import parse_flags
            rest, flags = parse_flags(
                ["a", "b", "--only=x", "--only=y", "--all"],
                bool_flags=("--all",), multi_flags=("--only",), usage="t",
            )
            print(rest, flags)
            """
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("['a', 'b']", out.stdout)
        self.assertIn("'--only': ['x', 'y']", out.stdout)
        self.assertIn("'--all': True", out.stdout)

    def test_api_env_injects_and_scrubs(self):
        """API 号：注入清单变量、清掉继承的 ANTHROPIC_*（防拿 A 家 key 打 B 家端点）；
        密钥不进环境（由 apiKeyHelper 提供）；默认不注入内建代理。"""
        self.seed_api_profile("002")
        env = self.build_env_of(
            "002",
            env_extra={
                "ANTHROPIC_API_KEY": "inherited-should-be-gone",
                "ANTHROPIC_BASE_URL": "https://wrong.example",
                "ANTHROPIC_MODEL": "claude-sonnet-5",
            },
        )
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.deepseek.com/anthropic")
        self.assertEqual(env["ANTHROPIC_MODEL"], "deepseek-flash[1m]")
        self.assertIsNone(env["ANTHROPIC_AUTH_TOKEN"], "密钥不该进环境")
        self.assertIsNone(env["ANTHROPIC_API_KEY"], "继承的 API key 要被清掉")
        self.assertIsNone(env["HTTPS_PROXY"])
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.acct_dir("002")))

    def test_nested_session_env_is_scrubbed(self):
        """从 claude 会话里启动新号：父会话的标记不能带进子进程。

        普通终端没有 CLAUDECODE，这些变量本来就不存在，行为不变。
        """
        self.seed_api_profile("002")
        env = self.build_env_of(
            "002",
            env_extra={
                "CLAUDECODE": "1",
                "CLAUDE_CODE_CHILD_SESSION": "1",
                "CLAUDE_CODE_SESSION_ID": "父会话编号",
                "CLAUDE_CODE_SESSION_ATTENDED": "1",
                "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/cc-socks/s.sock",  # noqa: S108 - 只是字符串
                "CLAUDE_CODE_MESSAGING_TOKEN": "secret",
                "CLAUDE_CODE_ENTRYPOINT": "cli",
            },
        )
        for key in ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
                    "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_MESSAGING_SOCKET",
                    "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_ENTRYPOINT"):
            self.assertIsNone(env[key], f"{key} 不该传给子进程")
        # 号自己的配置照常注入，不受清理影响
        self.assertEqual(env["ANTHROPIC_MODEL"], "deepseek-flash[1m]")

    def test_nested_session_env_kept_outside_claude(self):
        """普通终端里启动：没有 CLAUDECODE，就不去动这些变量。"""
        self.seed_api_profile("002")
        env = self.build_env_of("002", env_extra={"CLAUDE_CODE_ENTRYPOINT": "cli"})
        self.assertEqual(env["CLAUDE_CODE_ENTRYPOINT"], "cli")

    def test_api_env_default_no_proxy(self):
        """API 号默认不带代理：端点直连，也不注入任何代理变量。"""
        self.run_cli("new", "002")
        r = self.run_cli("add", "sub", "--url", "https://sub2api.example.com",
                         "--key", "sk-1234567890abcd")
        self.assertEqual(r.returncode, 0, r.stderr)
        marker = json.loads((self.acct_dir("sub") / ".cca-profile.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["proxy"], "")
        env = self.build_env_of("sub")
        self.assertIsNone(env["HTTPS_PROXY"])
        self.assertIsNone(env["NO_PROXY"])

    def test_api_env_proxy_none_and_inherit(self):
        self.run_cli("new", "002")
        self.run_cli("add", "sub", "--url", "https://gw.example", "--key", "sk-1234567890abcd")
        self.run_cli("add", "sub", "--proxy", "none")
        self.assertIsNone(self.build_env_of("sub")["HTTPS_PROXY"])
        self.run_cli("add", "sub", "--proxy", "inherit")
        self.assertIsNone(self.build_env_of("sub")["HTTPS_PROXY"])
        r = self.run_cli("add", "sub", "--proxy", "随便写的")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--proxy 取值", r.stderr)

    def test_doctor_warns_when_api_profile_has_no_proxy(self):
        """proxy=none 在无代理的 shell 里会让自动更新直连失败 —— doctor 要提示怎么改。"""
        self.run_cli("new", "002")
        self.run_cli("add", "sub", "--url", "https://gw.example", "--key", "sk-1234567890abcd",
                     "--proxy", "none")
        r = self.run_cli("doctor", CCA_PROXY="none")
        self.assertIn("Auto-update failed", r.stdout)
        self.assertIn("cca set proxy sub", r.stdout)

    def test_api_env_proxy_override(self):
        self.seed_api_profile("002", proxy="http://my-proxy:8080")
        env = self.build_env_of("002")
        self.assertEqual(env["HTTPS_PROXY"], "http://my-proxy:8080")

    def test_oauth_env_requires_proxy(self):
        """订阅号没有代理就拒绝启动；配了 CCA_PROXY 才注入。"""
        self.run_cli("new", "002")
        r = self.run_cli("002", CCA_PROXY="")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("没有配置代理", r.stderr)
        env = self.build_env_of("002", env_extra={"CCA_PROXY": "http://my-proxy:8080"})
        self.assertIsNone(env["ANTHROPIC_BASE_URL"])
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.acct_dir("002")))
        self.assertEqual(env["HTTPS_PROXY"], "http://my-proxy:8080")

    def test_add_api_profile_isolated(self):
        """`cca add … --isolated` 建出隔离 profile：有自己的对话池、不软链 CLAUDE.md。"""
        r = self.run_cli("add", "cx", "--provider", "deepseek", "--key", "sk-test", "--isolated")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        d = self.acct_dir("cx")
        self.assertTrue((d / ".cca-isolated.json").is_file())
        self.assertTrue((d / "settings.json").is_file())
        self.assertFalse((d / "projects").is_symlink(), "隔离号不该软链共享对话池")
        self.assertFalse((d / "CLAUDE.md").is_symlink(), "隔离号不该软链共享 CLAUDE.md")
        marker = json.loads((d / ".cca-profile.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["kind"], "api")

    def test_add_api_profile_happy_path(self):
        """cca add ds --provider deepseek --key … ：建号三件套齐全，密钥不进输出。"""
        secret = "sk-ant-oat01-SUPERSECRET-VALUE"  # noqa: S105 - 测试用的假密钥
        r = self.run_cli("add", "ds", "--provider", "deepseek", "--key", secret)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(secret, r.stdout + r.stderr)
        self.assertIn("--key-stdin", r.stderr)   # history 提示走 stderr
        marker = json.loads((self.acct_dir("ds") / ".cca-profile.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["provider"], "deepseek")
        self.assertEqual(marker["base_url"], "https://api.deepseek.com/anthropic")
        self.assertEqual(marker["proxy"], "")           # 默认不配代理，启动前要求配
        self.assertEqual(marker["env"], PROVIDERS_ENV["deepseek"])
        keyfile = self.acct_dir("ds") / ".api-key"
        self.assertEqual(keyfile.read_text(encoding="utf-8").strip(), secret)
        self.assertEqual(keyfile.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.acct_dir("ds") / ".cca-profile.json").stat().st_mode & 0o777, 0o600)
        settings = json.loads((self.acct_dir("ds") / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"], "https://api.deepseek.com/anthropic")
        self.assertIn(str(keyfile), settings["apiKeyHelper"])
        self.assertNotIn(secret, json.dumps(settings))
        self.assertTrue((self.acct_dir("ds") / "projects").is_symlink(), "共享软链要铺好")
        cfg = json.loads((self.acct_dir("ds") / ".claude.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["mcpServers"]["sideagents"]["args"], ["mcp", "serve"])

    def test_add_api_profile_creates_config_and_inherits_trust(self):
        """API 号没有 `claude auth login` 那一步：`.claude.json` 得由 add 建出来，
        否则引导标记与 001 的目录信任都无处落盘（交互启动会走向导、--bg 会因未信任起不来）。"""
        self.run_cli("new", "002")
        cfg = self.acct_dir("001") / ".claude.json"
        data = json.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
        data.setdefault("projects", {})["/proj/trusted"] = {"hasTrustDialogAccepted": True}
        cfg.write_text(json.dumps(data), encoding="utf-8")
        r = self.run_cli("add", "ds", "--provider", "deepseek", "--key", "sk-1234567890abcd")
        self.assertEqual(r.returncode, 0, r.stderr)
        profile_cfg = json.loads((self.acct_dir("ds") / ".claude.json").read_text(encoding="utf-8"))
        self.assertTrue(profile_cfg["hasCompletedOnboarding"])
        self.assertTrue(profile_cfg["projects"]["/proj/trusted"]["hasTrustDialogAccepted"])
        self.assertIn("hasCompletedOnboarding", r.stdout)
        self.assertIn("目录信任", r.stdout)

    def write_catalog_fixture(self, models=None):
        """写一份 OpenRouter /api/v1/models 的假响应（测试与离线用，绝不联网）。"""
        fixture = self.home / "or-models.json"
        fixture.write_text(json.dumps({"data": models or [
            {"id": "openrouter/free", "name": "Free router", "context_length": 200000,
             "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "qwen/qwen3.8-27b:free", "name": "Qwen", "context_length": 262144,
             "pricing": {"prompt": "0", "completion": "0"}},
        ]}), encoding="utf-8")
        return fixture

    def test_catalog_reads_fixture_without_network(self):
        fixture = self.write_catalog_fixture()
        out = self._run_python(CATALOG_PROBE, env_extra={"CCA_MODEL_CATALOG_FILE": str(fixture)})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("openrouter/free", out.stdout)

    def test_catalog_survives_broken_cache_and_network_failure(self):
        """坏缓存 + 拉不到 → 空列表，绝不抛异常（Review Focus 5/1）。"""
        cache = self.home / ".claude-accounts" / ".cache" / "openrouter-models.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("{ 这不是 JSON", encoding="utf-8")
        stale = time.time() - 7200
        os.utime(cache, (stale, stale))          # 过期 → 必须真的去联网（死 URL）
        out = self._run_python(CATALOG_PROBE, env_extra={"CCA_CATALOG_URL": "http://127.0.0.1:9/nope"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("[]", out.stdout)

    def test_last_session_model_ignores_synthetic(self):
        """取最后一条真实模型；`<synthetic>`（错误条目）不算。"""
        f = self.seed_transcript("001", "aaaa1111-0000-0000-0000-000000000000", title="甲")
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {"model": "qwen/qwen3.8-27b:free"}}) + "\n")
            fh.write(json.dumps({"type": "assistant", "message": {"model": "<synthetic>"}}) + "\n")
        out = self._run_python(LAST_MODEL_PROBE, str(f))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("qwen/qwen3.8-27b:free", out.stdout)

    def test_sessions_shows_model_column(self):
        f = self.seed_transcript("001", "bbbb2222-0000-0000-0000-000000000000", title="乙")
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {"model": "openrouter/free"}}) + "\n")
        r = self.run_cli("sessions")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("openrouter/free", r.stdout)

    def test_model_list_free_filter_and_sort(self):
        """`--list` 只读目录：免费过滤、按 ctx 排序、关键词搜索、价格换算成人话。"""
        self.seed_api_profile("or", provider="openrouter", base_url="https://openrouter.ai/api",
                              env={"ANTHROPIC_MODEL": "openrouter/free"}, render=True)
        fixture = self.write_catalog_fixture([
            {"id": "openrouter/free", "name": "Free router", "context_length": 200000,
             "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "qwen/qwen3.8-27b:free", "name": "Qwen", "context_length": 262144,
             "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "anthropic/claude-opus-4-8", "name": "Opus", "context_length": 1000000,
             "pricing": {"prompt": "0.000004", "completion": "0.00002"}},
        ])
        env = {"CCA_MODEL_CATALOG_FILE": str(fixture)}
        r = self.run_cli("model", "--list", "--free", **env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("openrouter/free", r.stdout)
        self.assertIn("qwen/qwen3.8-27b:free", r.stdout)
        self.assertNotIn("claude-opus-4-8", r.stdout)
        self.assertIn("FREE", r.stdout)
        r = self.run_cli("model", "--list", "--sort", "ctx", **env)
        self.assertLess(r.stdout.index("claude-opus-4-8"), r.stdout.index("qwen/qwen3.8-27b:free"))
        r = self.run_cli("model", "--list", "--search", "qwen", **env)
        self.assertIn("qwen/qwen3.8-27b:free", r.stdout)
        self.assertNotIn("claude-opus-4-8", r.stdout)
        r = self.run_cli("model", "--list", **env)
        self.assertIn("$4/$20 每 M", r.stdout)

    def test_model_summary_prefers_marker_over_seeded_settings_model(self):
        """001 有 `model: opus`，新号会把它整份播种进来 —— 但 API 号的真相是清单 env（C1）。"""
        (self.home / ".claude" / "settings.json").write_text(
            json.dumps({"model": "opus"}), encoding="utf-8")
        self.seed_api_profile("or", provider="openrouter", base_url="https://openrouter.ai/api",
                              env={"ANTHROPIC_MODEL": "openrouter/free"}, render=True)
        data = json.loads(self.run_cli("model", "--json").stdout)
        row = next(r for r in data if r["profile"] == "or")
        self.assertEqual(row["main"], "openrouter/free")
        self.assertEqual(row["source"], "marker")

    def test_model_effort_lands_on_the_real_main_model(self):
        """`--effort` 必须写在实际主模型名下，不能写到播种来的 `opus` 上（C1 的后果）。"""
        (self.home / ".claude" / "settings.json").write_text(
            json.dumps({"model": "opus"}), encoding="utf-8")
        self.seed_api_profile("or", provider="openrouter", base_url="https://openrouter.ai/api",
                              env={"ANTHROPIC_MODEL": "openrouter/free"}, render=True)
        r = self.run_cli("model", "or", "--effort", "high")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        st = json.loads((self.acct_dir("or") / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(st["modelSettings"], {"openrouter/free": {"effortLevel": "high"}})

    def test_model_main_writes_marker_and_keeps_settings_in_step(self):
        """`--main` 之后 settings.model 与清单 env 必须一致，`cca sync` 也不能让它们背离（I4）。"""
        (self.home / ".claude" / "settings.json").write_text(
            json.dumps({"model": "opus"}), encoding="utf-8")
        self.seed_api_profile("or", provider="openrouter", base_url="https://openrouter.ai/api",
                              env={"ANTHROPIC_MODEL": "openrouter/free"}, render=True)
        self.assertEqual(self.run_cli("model", "or", "--main", "qwen/qwen3.8-27b:free").returncode, 0)
        st = json.loads((self.acct_dir("or") / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(st["model"], "qwen/qwen3.8-27b:free")
        self.run_cli("sync", "or")
        st = json.loads((self.acct_dir("or") / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(st["model"], "qwen/qwen3.8-27b:free")
        self.assertEqual(st["env"]["ANTHROPIC_MODEL"], "qwen/qwen3.8-27b:free")

    def test_model_json_reports_what_changed(self):
        """`--json` 的 changes 必须说清改了哪几项（I5）。"""
        self.seed_api_profile("or", provider="openrouter", base_url="https://openrouter.ai/api",
                              env={"ANTHROPIC_MODEL": "openrouter/free"}, render=True)
        out = json.loads(self.run_cli("model", "or", "--main", "qwen/qwen3.8-27b:free",
                                      "--effort", "high", "--json").stdout)
        self.assertEqual(sorted(out[0]["changes"]), ["effort", "main"])

    def test_model_list_accepts_profile_argument(self):
        """spec 里写的是 `cca model or --list --free`；带号要能用（目录是全局的，号只被忽略）。"""
        fixture = self.write_catalog_fixture()
        r = self.run_cli("model", "or", "--list", "--free",
                         CCA_MODEL_CATALOG_FILE=str(fixture))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("openrouter/free", r.stdout)

    def test_add_openrouter_writes_account_window_from_catalog(self):
        """建号时按目录给号级默认写一份窗口；`--main` 改主模型时同步跟（I1）。"""
        fixture = self.write_catalog_fixture([
            {"id": "openrouter/free", "name": "Free", "context_length": 200000, "pricing": {}},
            {"id": "qwen/qwen3.8-27b:free", "name": "Qwen", "context_length": 262144, "pricing": {}},
        ])
        env = {"CCA_MODEL_CATALOG_FILE": str(fixture)}
        r = self.run_cli("add", "or", "--provider", "openrouter", "--key", "sk-or-v1-test", **env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        marker = json.loads((self.acct_dir("or") / ".cca-profile.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "180000")
        self.assertEqual(self.run_cli("model", "or", "--main", "qwen/qwen3.8-27b:free", **env).returncode, 0)
        marker = json.loads((self.acct_dir("or") / ".cca-profile.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "235000")

    def test_session_window_hints_when_catalog_unavailable(self):
        """目录拉不到时也要说一句，别让使用者分不清"模型不在目录里"和"目录没拉到"（I3）。"""
        self.seed_api_profile("or", provider="openrouter", base_url="https://openrouter.ai/api",
                              env={"ANTHROPIC_MODEL": "openrouter/free"}, render=True)
        out = self._run_python(LAUNCH_ENV_PROBE, "or", "--model", "qwen/qwen3.8-27b:free",
                               env_extra={"CCA_CATALOG_URL": "http://127.0.0.1:9/nope"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("拿不到 OpenRouter 目录", out.stderr)

    def test_model_summary_without_profile_is_read_only(self):
        self.seed_api_profile("or", provider="openrouter", base_url="https://openrouter.ai/api",
                              env={"ANTHROPIC_MODEL": "openrouter/free"}, render=True)
        before = (self.acct_dir("or") / "settings.json").read_bytes()
        r = self.run_cli("model")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("or", r.stdout)
        self.assertIn("openrouter/free", r.stdout)
        self.assertEqual((self.acct_dir("or") / "settings.json").read_bytes(), before)

    def test_model_write_requires_profile(self):
        r = self.run_cli("model", "--main", "openrouter/free")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("要写清哪个号", r.stderr)

    def test_model_sets_main_alias_and_effort(self):
        self.seed_api_profile("or", provider="openrouter", base_url="https://openrouter.ai/api",
                              env={"ANTHROPIC_MODEL": "openrouter/free"}, render=True)
        r = self.run_cli("model", "or", "--main", "qwen/qwen3.8-27b:free",
                         "--opus", "~anthropic/claude-opus-latest", "--effort", "high")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        st = json.loads((self.acct_dir("or") / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(st["model"], "qwen/qwen3.8-27b:free")
        self.assertEqual(st["env"]["ANTHROPIC_MODEL"], "qwen/qwen3.8-27b:free")
        self.assertEqual(st["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"], "~anthropic/claude-opus-latest")
        self.assertEqual(st["modelSettings"]["qwen/qwen3.8-27b:free"]["effortLevel"], "high")
        marker = json.loads((self.acct_dir("or") / ".cca-profile.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["env"]["ANTHROPIC_MODEL"], "qwen/qwen3.8-27b:free")  # 清单同步

    def test_model_all_writes_every_profile(self):
        for name in ("or", "or2"):
            self.seed_api_profile(name, provider="openrouter", base_url="https://openrouter.ai/api",
                                  env={"ANTHROPIC_MODEL": "openrouter/free"}, render=True)
        r = self.run_cli("model", "--all", "--sub", "openrouter/free")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for name in ("or", "or2"):
            st = json.loads((self.acct_dir(name) / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(st["env"]["CLAUDE_CODE_SUBAGENT_MODEL"], "openrouter/free")

    def test_compact_window_three_branches(self):
        """三分支：认得的 claude-* 不写、未知 slug 按目录写、目录里没有不写。"""
        self.seed_api_profile("or", provider="openrouter", base_url="https://openrouter.ai/api",
                              env={"ANTHROPIC_MODEL": "openrouter/free"}, render=True)
        catalog = [
            {"id": "openrouter/free", "name": "Free", "context_length": 200000, "pricing": {}},
            {"id": "qwen/qwen3.8-27b:free", "name": "Qwen", "context_length": 262144, "pricing": {}},
            {"id": "anthropic/claude-opus-4-8", "name": "Opus", "context_length": 1000000, "pricing": {}},
        ]
        out = self._run_python(WINDOW_PROBE, json.dumps(catalog))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("openrouter/free=180000", out.stdout)
        self.assertIn("qwen/qwen3.8-27b:free=235000", out.stdout)
        self.assertIn("anthropic/claude-opus-4-8=None", out.stdout)
        self.assertIn("nobody/unknown-model=None", out.stdout)

    def test_api_accounts_disallow_internal_tools(self):
        """API 号禁掉 Claude Code 的内部工具（Artifact* / SendFeedback）。

        第三方模型看到这些工具的描述就会调，但实现没开 → 真机上反复报
        `No such tool available: SendFeedback … it cannot run`。
        """
        joined = ",".join(API_DISALLOWED_TOOLS)
        self.assertIn("Artifact", API_DISALLOWED_TOOLS)
        self.assertIn("SendFeedback", API_DISALLOWED_TOOLS)
        self.seed_api_profile("ds", provider="deepseek")
        out = self._run_python(
            "import sys\nfrom cca.api.client import api_launch_args\n"
            "print('ARGS=', api_launch_args('ds', ['-p', 'hi']))\n")
        self.assertIn("SendFeedback", out.stdout)
        self.assertIn(f"--disallowedTools={joined}", out.stdout)

    def test_compact_window_ratio_can_be_overridden(self):
        """窗口系数默认 0.9（原来硬编码 0.75）；`CCA_COMPACT_WINDOW_RATIO` 可覆盖并夹到 0.5~0.98。"""
        self.run_cli("new", "002")
        self.write_catalog_fixture([
            {"id": "m", "name": "M", "context_length": 1000000, "pricing": {}},
        ])
        probe = (
            "import sys\n"
            "from cca.env import compact_window_for\n"
            "import json\n"
            "cat = json.loads(sys.argv[2])\n"
            "print('W=', compact_window_for('m', cat))\n"
        )
        catalog = json.dumps([{"id": "m", "name": "M", "context_length": 1000000}])
        out = self._run_python(probe, catalog)
        self.assertIn("W= 900000", out.stdout)
        out = self._run_python(probe, catalog, env_extra={"CCA_COMPACT_WINDOW_RATIO": "0.75"})
        self.assertIn("W= 750000", out.stdout)
        out = self._run_python(probe, catalog, env_extra={"CCA_COMPACT_WINDOW_RATIO": "5"})
        self.assertIn("W= 980000", out.stdout)

    def test_session_model_does_not_touch_profile_settings(self):
        """`--model` 只进本次会话 env：号级 settings.json 一个字节都不许改。"""
        self.seed_api_profile("or", provider="openrouter", base_url="https://openrouter.ai/api",
                              env={"ANTHROPIC_MODEL": "openrouter/free"}, render=True)
        fixture = self.write_catalog_fixture()
        before = (self.acct_dir("or") / "settings.json").read_bytes()
        out = self._run_python(LAUNCH_ENV_PROBE, "or", "--model", "qwen/qwen3.8-27b:free",
                               env_extra={"CCA_MODEL_CATALOG_FILE": str(fixture)})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("MODEL_FLAG = qwen/qwen3.8-27b:free", out.stdout)
        self.assertIn("CLAUDE_CODE_AUTO_COMPACT_WINDOW = 235000", out.stdout)
        self.assertIn("_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL = 1", out.stdout)
        self.assertEqual((self.acct_dir("or") / "settings.json").read_bytes(), before)

    @staticmethod
    def stamp(*, minutes_ago=0.0):
        """ISO 时间串（UTC，带 Z）：transcript 里的 `timestamp` 就是这个格式。"""
        moment = datetime.now(UTC) - timedelta(minutes=minutes_ago)
        return moment.isoformat().replace("+00:00", "Z")

    def turn(self, *, minutes_ago=0.0, text="我继续做下一步"):
        """一条正常对话记录。

        带时间戳是有意的：判定按**记录自己的时间**走，不看文件 mtime（mtime 会被记账记录顶新）。
        """
        return {"type": "assistant", "timestamp": self.stamp(minutes_ago=minutes_ago),
                "message": {"role": "assistant", "model": "claude-opus-5-5",
                            "content": [{"type": "text", "text": text}]}}

    def limit_error(self, *, minutes_ago=0.0):
        """一条额度/限流错误记录。"""
        return {"type": "assistant", "isApiErrorMessage": True,
                "timestamp": self.stamp(minutes_ago=minutes_ago),
                "message": {"role": "assistant",
                            "content": [{"type": "text",
                                         "text": "API Error: 429 usage limit reached"}]}}

    def queue_op(self, *, minutes_ago=0.0):
        """一条"只是记账"的记录：监控任务往会话队列里塞通知（被额度卡住后还会一直来）。"""
        return {"type": "queue-operation", "operation": "enqueue",
                "timestamp": self.stamp(minutes_ago=minutes_ago),
                "content": "<task-notification>\n<task-id>b89v7flgf</task-id>\n</task-notification>"}

    def test_push_session_once_classifies_output(self):
        for case, expect in (("running", "skipped"), ("limit", "retry"), ("failed", "failed"),
                             ("ok", "sent"), ("verify-limit", "retry"), ("verify-quiet", "sent"),
                             ("copy", "sent")):
            out = self._run_python(PUSH_CLASSIFY_PROBE, case, "abcde123")
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn(f"state={expect}", out.stdout, case)
        # 推的方式是 `--bg --resume`（会话留在面板里），提示走位置参数
        self.assertIn("cmd=cca 001 --bg --resume abcde123 继续", out.stdout)

    def seed_wait_session(self, short="abcde123", *, last=None, state="working", profile="001",  # noqa: PLR0913 - 测试造数据，参数各自直白
                          extra_history=(), idle_seconds=0):
        """造一条后台会话：transcript（可带历史记录与最后一条）+ daemon 面板登记。

        `idle_seconds` 是"最后写 transcript 距今多久"：0（默认）表示刚写过（真在跑的样子），
        给个大数就变成"进程还挂着但半天没动"（后台会话停在等输入时的样子）。
        """
        long_id = f"{short}-0000-0000-0000-000000000000"
        self.seed_daemon_dir(profile)
        f = self.seed_transcript(profile, long_id, title="job", prompt="继续做X", cwd=str(self.home))
        with f.open("a", encoding="utf-8") as fh:
            for entry in extra_history:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            if last is not None:
                fh.write(json.dumps(last, ensure_ascii=False) + "\n")
        ts = time.time() - idle_seconds
        os.utime(f, (ts, ts))
        return long_id

    def wait_env(self, sessions, *, stale=()):
        """面板 payload（FAKE_CLAUDE_AGENTS）+ 已退出的会话（FAKE_CLAUDE_STALE_IDS）。

        还要把假 `claude` 放回 PATH：`_run_python` 默认只带真实 PATH，那样 `active_workers()`
        会去调真的 claude（读不到面板，还会白白慢一截）。
        """
        payload = json.dumps([{"id": short, "kind": "background", "name": "job",
                               "cwd": str(self.home), "sessionId": long_id, "state": state}
                              for short, long_id, state in sessions])
        env = {"FAKE_CLAUDE_AGENTS": payload,
               "PATH": f"{self.stub}:{os.environ.get('PATH', '')}"}
        if stale:
            env["FAKE_CLAUDE_STALE_IDS"] = ",".join(stale)
        return env

    def test_push_stops_by_job_id_and_respects_log_motion(self):
        """推之前按 job id 停登记（`claude stop` 不认会话 UUID），日志还在动就不碰。"""
        long_id = self.seed_wait_session(last=self.limit_error(), state="blocked")
        env = self.wait_env([("abcde123", long_id, "blocked")])
        out = self._run_python(PUSH_JOB_PROBE, "moving", long_id, env_extra=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("state=skipped", out.stdout)
        self.assertIn("job=abcde123", out.stdout)                 # 面板短 id 才算 job id
        self.assertNotIn("stop", out.stdout)                      # 在跑就不 stop、不 resume
        self.assertNotIn("--resume", out.stdout)
        out = self._run_python(PUSH_JOB_PROBE, "still", long_id, env_extra=env)
        self.assertIn("state=sent", out.stdout)
        self.assertIn("call=cca 001 stop abcde123", out.stdout)
        # 会话有名字（job）：启动提示是 /rename，原提示续上后以消息投递
        self.assertIn(f"call=cca 001 --bg --resume {long_id} /rename job", out.stdout)
        self.assertIn("call=deliver 继续", out.stdout)
        self.assertNotIn("--name", out.stdout)

    def test_push_env_blocked_is_retryable(self):
        """环境不可用（代理/出口）要记 retry —— 判成终结 failed 会把整条等待记录废掉。

        真机：一次 DNS 抖动让 6 条推送全部 failed（`没有配置代理：出口不明`），到点的续跑一条没成。
        """
        out = self._run_python(PUSH_CLASSIFY_PROBE, "env-blocked", "abcde123")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("state=retry", out.stdout)
        self.assertIn("环境不可用", out.stdout)

    def test_model_effort_writes_marker_env_for_api(self):
        """API 号改 `--effort` 要同步写清单 env —— 否则预设里的 CLAUDE_CODE_EFFORT_LEVEL 会压过它。"""
        self.seed_api_profile("ds")
        r = self.run_cli("model", "ds", "--effort", "high")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        marker = json.loads((self.acct_dir("ds") / ".cca-profile.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["env"]["CLAUDE_CODE_EFFORT_LEVEL"], "high")
        settings = json.loads((self.acct_dir("ds") / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["env"]["CLAUDE_CODE_EFFORT_LEVEL"], "high")

    def test_deepseek_preset_defaults_to_xhigh_effort(self):
        """ds 预设的默认推理档是 xhigh（不是 max：max 又慢又费）。"""
        self.run_cli("add", "ds", "--provider", "deepseek", "--key", "sk-test")
        settings = json.loads((self.acct_dir("ds") / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["env"]["CLAUDE_CODE_EFFORT_LEVEL"], "xhigh")

    def test_add_openrouter_preset_renders_gateway_env(self):
        """OpenRouter 预设：端点、四档别名、网关发现的三个开关；工具搜索与子代理不设、密钥只进 .api-key。"""
        r = self.run_cli("add", "or", "--provider", "openrouter", "--key", "sk-or-v1-test")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        marker = json.loads((self.acct_dir("or") / ".cca-profile.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["base_url"], "https://openrouter.ai/api")
        env = marker["env"]
        self.assertEqual(env["ANTHROPIC_MODEL"], "openrouter/free")
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "~anthropic/claude-opus-latest")
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "~anthropic/claude-sonnet-latest")
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "~anthropic/claude-haiku-latest")
        self.assertEqual(env["ANTHROPIC_DEFAULT_FABLE_MODEL"], "~anthropic/claude-fable-latest")
        self.assertEqual(env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"], "1")
        self.assertEqual(env["CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT"], "1")
        self.assertEqual(env["CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK"], "1")
        self.assertNotIn("ENABLE_TOOL_SEARCH", env)
        self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", env)
        self.assertEqual(marker["proxy"], "")
        self.assertNotIn("sk-or-v1-test", json.dumps(marker))
        self.assertEqual(oct((self.acct_dir("or") / ".api-key").stat().st_mode)[-3:], "600")

    def test_add_url_only_and_host_inference(self):
        """`--url` + key 就够（中转/网关）；只给 URL 时命中已知预设会自动套用并说明。"""
        # 无预设：不设模型变量（网关自己映射模型名），面板显示端点主机
        r = self.run_cli("add", "sub", "--url", "https://sub2api.example.com",
                         "--key", "sk-sub-1234567890abcd")
        self.assertEqual(r.returncode, 0, r.stderr)
        marker = json.loads((self.acct_dir("sub") / ".cca-profile.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["provider"], "generic")
        self.assertEqual(marker["base_url"], "https://sub2api.example.com")
        self.assertEqual(marker["env"], {})
        ls = self.run_cli("ls")
        self.assertIn("api:sub2api.example.com", ls.stdout)
        # 命中预设主机：自动套用模型变量 + 明说一句
        r = self.run_cli("add", "ds2", "--url", "https://api.deepseek.com/anthropic",
                         "--key", "sk-ds-1234567890abcd")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("识别到", r.stderr)
        marker = json.loads((self.acct_dir("ds2") / ".cca-profile.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["provider"], "deepseek")
        self.assertEqual(marker["env"]["ANTHROPIC_MODEL"], "deepseek-flash[1m]")

    def test_add_url_without_model_hints(self):
        """不强制 --model（中转就该用 Claude 原生模型名），但要一句引导。"""
        r = self.run_cli("add", "sub", "--url", "https://gw.example", "--key", "sk-1234567890abcd")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("没给 --model", r.stderr)
        r = self.run_cli("rm", "sub", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_cli("add", "sub2", "--url", "https://gw.example", "--key", "sk-1234567890abcd",
                         "--model", "my-model")
        self.assertNotIn("没给 --model", r.stderr)
        r = self.run_cli("add", "ds", "--provider", "deepseek", "--key", "sk-1234567890abcd")
        self.assertNotIn("没给 --model", r.stderr)

    def test_add_updates_existing_api_profile(self):
        """`cca add <已存在的 API 号>` = 增量更新：只改这次给的字段，并列出改了什么。"""
        self.run_cli("add", "sub", "--url", "https://gw.example", "--key", "sk-old-1234567890")
        marker_path = self.acct_dir("sub") / ".cca-profile.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        self.assertEqual(marker["env"], {})
        # 改模型：五个变量一起设
        r = self.run_cli("add", "sub", "--model", "m1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("已更新 sub", r.stdout)
        self.assertIn("ANTHROPIC_MODEL (未设) → m1", r.stdout)
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        self.assertEqual(marker["env"]["CLAUDE_CODE_SUBAGENT_MODEL"], "m1")
        self.assertEqual(marker["base_url"], "https://gw.example")     # 没提的字段不动
        self.assertEqual((self.acct_dir("sub") / ".api-key").read_text(encoding="utf-8").strip(),
                         "sk-old-1234567890")                            # 密钥也没动
        # 加变量、删变量
        self.run_cli("add", "sub", "--env", "FOO=1")
        self.assertIn("FOO", json.loads(marker_path.read_text(encoding="utf-8"))["env"])
        r = self.run_cli("add", "sub", "--env", "FOO=")
        self.assertIn("(删除)", r.stdout)
        self.assertNotIn("FOO", json.loads(marker_path.read_text(encoding="utf-8"))["env"])
        # 换端点 + 换 key（add 里也能换）
        r = self.run_cli("add", "sub", "--url", "https://new.example/anthropic",
                         "--key", "sk-new-1234567890")
        self.assertIn("端点 https://gw.example → https://new.example/anthropic", r.stdout)
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        self.assertEqual(marker["base_url"], "https://new.example/anthropic")
        self.assertEqual((self.acct_dir("sub") / ".api-key").read_text(encoding="utf-8").strip(),
                         "sk-new-1234567890")
        self.assertEqual(marker["env"]["ANTHROPIC_MODEL"], "m1")         # 模型仍是上次设的
        settings = json.loads((self.acct_dir("sub") / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"], "https://new.example/anthropic")

    def test_add_existing_api_profile_without_flags_guides(self):
        self.run_cli("add", "sub", "--url", "https://gw.example", "--key", "sk-1234567890abcd")
        r = self.run_cli("add", "sub")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("已存在", r.stderr)
        self.assertIn("--model", r.stderr)

    def test_add_provider_and_url_are_mutually_exclusive(self):
        """端点二选一：混着给会让人以为"预设还能换端点"，直接报错并给替代写法。"""
        r = self.run_cli("add", "x", "--provider", "deepseek",
                         "--url", "https://mirror.example/anthropic", "--key", "sk-1234567890abcd")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("只能给一个", r.stderr)
        self.assertIn("--model deepseek-flash[1m]", r.stderr)
        self.assertFalse((self.acct_dir("x")).exists())

    def test_add_url_conflicts_with_base_url(self):
        r = self.run_cli("add", "x", "--url", "https://a.example", "--base-url", "https://b.example",
                         "--key", "sk-1234567890abcd")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("同一个参数", r.stderr)

    def test_add_provider_generic_is_explicit_no_preset(self):
        r = self.run_cli("add", "gw", "--provider", "generic", "--url", "https://gw.example/anthropic",
                         "--key", "sk-1234567890abcd")
        self.assertEqual(r.returncode, 0, r.stderr)
        marker = json.loads((self.acct_dir("gw") / ".cca-profile.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["provider"], "generic")
        self.assertEqual(marker["env"], {})

    def test_add_help_lists_recipes(self):
        r = self.run_cli("add", "--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        for expect in ("--provider deepseek --key", "--url https://", "--key-stdin",
                       "二选一", "cca login",
                       # 模型槽位表要说清楚（反馈："不知道 --model 映射到 cc 哪个模型上"）
                       "ANTHROPIC_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL",
                       "所有槽位", "不给 `--model`"):
            self.assertIn(expect, r.stdout)
        self.assertEqual(sorted(p.name for p in (self.home / ".claude-accounts").iterdir()), ["001"])

    def test_add_api_profile_does_not_say_login_next(self):
        """API 号建完即用：不能再提示"下一步: cca login"（那是订阅号路径），且不重复"提示:"前缀。"""
        r = self.run_cli("add", "ds", "--provider", "deepseek", "--key", "sk-1234567890abcd")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("cca login ds", r.stdout + r.stderr)
        self.assertIn("启动: cca ds", r.stdout)
        self.assertNotIn("提示: 提示:", r.stdout + r.stderr)
        # 订阅号路径仍要有那句
        r = self.run_cli("new", "plain")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("下一步: cca login plain", r.stdout)

    def test_add_api_missing_key_guides(self):
        r = self.run_cli("add", "ds", "--provider", "deepseek")
        self.assertNotEqual(r.returncode, 0)
        for hint in ("--key", "--key-stdin", "DEEPSEEK_API_KEY"):
            self.assertIn(hint, r.stderr)

    def test_add_unknown_provider_guides(self):
        r = self.run_cli("add", "ds", "--provider", "deepseekk", "--key", "x" * 20)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("deepseek", r.stderr)
        self.assertIn("--url", r.stderr)          # 提示用主写法（--base-url 仍是同义词，一并提到）

    def test_add_key_stdin_and_env_var(self):
        r = self.run_cli("add", "ds", "--provider", "deepseek", "--key-stdin",
                         stdin_data="sk-xyz-1234567890\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.acct_dir("ds") / ".api-key").read_text(encoding="utf-8").strip(),
                         "sk-xyz-1234567890")
        r = self.run_cli("add", "ds2", "--provider", "deepseek", DEEPSEEK_API_KEY="sk-env-1234567890")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.acct_dir("ds2") / ".api-key").read_text(encoding="utf-8").strip(),
                         "sk-env-1234567890")

    def test_add_key_stdin_empty_guides(self):
        r = self.run_cli("add", "ds", "--provider", "deepseek", "--key-stdin", stdin_data="")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("echo $KEY", r.stderr)
        self.assertFalse((self.acct_dir("ds") / ".api-key").exists())

    def test_add_keys_are_mutually_exclusive(self):
        r = self.run_cli("add", "ds", "--provider", "deepseek", "--key", "sk-1234567890abcd",
                         "--key-stdin", stdin_data="sk-other\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("只能给一个", r.stderr)

    def test_add_generic_endpoint_with_model(self):
        r = self.run_cli("add", "gtw", "--base-url", "https://gw.example/anthropic",
                         "--key", "sk-generic-1234567890", "--model", "my-model")
        self.assertEqual(r.returncode, 0, r.stderr)
        marker = json.loads((self.acct_dir("gtw") / ".cca-profile.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["provider"], "generic")
        self.assertEqual(marker["base_url"], "https://gw.example/anthropic")
        for var in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
            self.assertEqual(marker["env"][var], "my-model")

    def test_add_env_rejects_managed_keys(self):
        for bad in ("CLAUDE_CONFIG_DIR=/tmp/x", "ANTHROPIC_BASE_URL=https://x",
                    "HTTPS_PROXY=http://x", "ANTHROPIC_API_KEY=sk-x"):
            r = self.run_cli("add", "ds", "--provider", "deepseek", "--key", "sk-1234567890abcd",
                             "--env", bad)
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertIn("--env", r.stderr)

    def test_add_env_extra_var(self):
        r = self.run_cli("add", "ds", "--provider", "deepseek", "--key", "sk-1234567890abcd",
                         "--env", "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1")
        self.assertEqual(r.returncode, 0, r.stderr)
        marker = json.loads((self.acct_dir("ds") / ".cca-profile.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["env"]["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"], "1")
        self.assertEqual(marker["env"]["ANTHROPIC_MODEL"], "deepseek-flash[1m]")

    def test_add_without_provider_unchanged(self):
        """回归：不带 API 参数的 add 仍是 建号 + OAuth 登录（参数原样透传）。"""
        r = self.run_cli("add", "002", "--email", "claude002@example.com")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.record()["argv"][:2], ["auth", "login"])
        self.assertFalse((self.acct_dir("002") / ".cca-profile.json").exists())

    # -- API 号：换 key / 删 key / 启动校验 ---------------------------------
    def test_login_rotates_api_key(self):
        self.seed_api_profile("002", key="sk-old-1234567890abcd")
        r = self.run_cli("login", "002", "--key", "sk-new-1234567890wxyz")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.acct_dir("002") / ".api-key").read_text(encoding="utf-8").strip(),
                         "sk-new-1234567890wxyz")
        rendered = (self.acct_dir("002") / "settings.json").read_text(encoding="utf-8")
        self.assertIn("apiKeyHelper", rendered)            # 重渲染后 helper 还在
        self.assertNotIn("sk-old", r.stdout + r.stderr + rendered)
        self.assertNotIn("sk-new-1234567890wxyz", r.stdout + r.stderr)
        self.assertIn("sk-new…wxyz", r.stdout)

    def test_login_api_missing_key_guides(self):
        self.seed_api_profile("002", key=None)
        r = self.run_cli("login", "002", stdin_data="")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--key", r.stderr)
        self.assertIn("DEEPSEEK_API_KEY", r.stderr)

    def test_login_api_rejects_other_flags(self):
        self.seed_api_profile("002")
        r = self.run_cli("login", "002", "--email", "x@example.com")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("只支持 --key", r.stderr)

    def test_login_oauth_rejects_api_key_flag(self):
        """订阅号给 --key 是误操作：直接说清，而不是透传给 claude auth login 撞未知参数。"""
        self.run_cli("new", "003")
        r = self.run_cli("login", "003", "--key", "sk-1234567890abcd")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("API key 只对 API 号有意义", r.stderr)
        self.assertIn("cca add", r.stderr)

    def test_logout_removes_api_key_only(self):
        self.seed_api_profile("002")
        r = self.run_cli("logout", "002")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.acct_dir("002") / ".api-key").exists())
        self.assertTrue((self.acct_dir("002") / ".cca-profile.json").is_file())
        self.assertIn("对话与配置保留", r.stdout)
        self.run_cli("new", "003")
        self.run_cli("logout", "003")
        self.assertIn("auth logout", self.calls())          # 订阅号仍走 claude auth logout

    def test_run_api_profile_missing_key_fails_fast(self):
        self.seed_api_profile("002", key=None)
        r = self.run_cli("002", "-p", "hi")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("缺少 API key", r.stderr)
        self.assertIn("cca login 002 --key", r.stderr)
        self.assertEqual(self.calls(), [], "缺 key 时不该启动 claude")

    def test_run_api_profile_warns_on_loose_key_perms(self):
        self.seed_api_profile("002")
        (self.acct_dir("002") / ".api-key").chmod(0o644)
        r = self.run_cli("002", "-p", "hi")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("chmod 600", r.stderr)

    def test_run_api_profile_corrupt_marker_fails_before_launch(self):
        self.seed_api_profile("002")
        (self.acct_dir("002") / ".cca-profile.json").write_text("{ 坏", encoding="utf-8")
        r = self.run_cli("002", "-p", "hi")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("无法解析", r.stderr)
        self.assertEqual(self.calls(), [])

    # -- API 号：面板与详情 -------------------------------------------------
    def test_ls_shows_api_kind_and_missing_key_hint(self):
        self.seed_api_profile("002")
        r = self.run_cli("ls")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("api:deepseek", r.stdout)
        self.assertIn("api 号没有订阅额度缓存", r.stdout)
        self.assertNotIn("sk-ant", r.stdout)
        self.run_cli("logout", "002")
        r = self.run_cli("ls")
        self.assertIn("key 缺失", r.stdout)
        self.assertIn("cca login 002 --key", r.stdout)

    def test_status_api_profile(self):
        self.seed_api_profile("002")
        r = self.run_cli("status", "002")
        self.assertEqual(r.returncode, 0, r.stderr)
        for expect in ("api:deepseek", "https://api.deepseek.com/anthropic", "deepseek-flash[1m]",
                       "已配置 sk-ant…ALUE", "代理"):
            self.assertIn(expect, r.stdout)
        for oauth_only in ("档位", "凭据", "重新登录", "额外用量"):
            self.assertNotIn(oauth_only, r.stdout)
        self.assertNotIn("sk-ant-oat01-SUPERSECRET-VALUE", r.stdout)

    def test_status_api_profile_missing_key_guides(self):
        self.seed_api_profile("002", key=None)
        r = self.run_cli("status", "002")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("缺失", r.stdout)
        self.assertIn("cca login 002 --key", r.stdout)

    def test_ls_json_includes_kind(self):
        self.seed_api_profile("002")
        data = json.loads(self.run_cli("ls", "--json").stdout)
        by_profile = {p["profile"]: p for p in data["profiles"]}
        self.assertEqual(by_profile["002"]["kind"], "api")
        self.assertEqual(by_profile["002"]["provider"], "deepseek")
        self.assertEqual(by_profile["001"]["kind"], "oauth")

    # -- API 号：体检 / 播种 / 删号 / 切换引导 -------------------------------
    def test_doctor_api_checks(self):
        self.seed_api_profile("002")
        r = self.run_cli("doctor")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("api:deepseek", r.stdout)
        # 权限过宽 → WARN 带修复命令
        (self.acct_dir("002") / ".api-key").chmod(0o644)
        r = self.run_cli("doctor")
        self.assertIn("chmod 600", r.stdout)
        (self.acct_dir("002") / ".api-key").chmod(0o600)
        # settings 与清单漂移 → WARN + cca sync
        settings = json.loads((self.acct_dir("002") / "settings.json").read_text(encoding="utf-8"))
        settings["env"]["ANTHROPIC_BASE_URL"] = "https://stale.example"
        (self.acct_dir("002") / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        r = self.run_cli("doctor")
        self.assertIn("cca sync 002", r.stdout)
        # 缺 key → FAIL（退出码 1）
        self.run_cli("logout", "002")
        r = self.run_cli("doctor")
        self.assertEqual(r.returncode, 1)
        self.assertIn("cca login 002 --key", r.stdout)

    def test_doctor_flags_unrendered_helper(self):
        """清单在、settings 没渲染 → claude 拿不到凭据：FAIL 并给出 cca sync。"""
        self.seed_api_profile("002", render=False)
        r = self.run_cli("doctor")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("cca sync 002", r.stdout)

    def test_doctor_online_probes_api_endpoint(self):
        """`--online` 探活 API 端点：可达（含 401）算 OK，连不上才 WARN。"""
        class Unauthorized(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.send_response(401)
                self.send_header("content-length", "0")
                self.end_headers()

        server = HTTPServer(("127.0.0.1", 0), Unauthorized)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            self.seed_api_profile("002", base_url=f"http://127.0.0.1:{port}", provider="generic")
            # 探活走该号生效的代理（CCA_PROXY 优先），测试环境里那个桩代理没起，这里显式直连
            r = self.run_cli("doctor", "--online", CCA_PROXY="none")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("端点可达", r.stdout)
        finally:
            server.shutdown()
            server.server_close()          # 不然会留一个未关闭的 socket 警告

        self.run_cli("rm", "002", "--yes")
        self.seed_api_profile("003", base_url="http://127.0.0.1:9", provider="generic")
        r = self.run_cli("doctor", "--online", CCA_PROXY="none")
        self.assertIn("连不上", r.stdout)

    def test_status_flags_settings_drift(self):
        """清单是真相：settings 漂移时 status 直接给修复命令。"""
        self.seed_api_profile("002")
        settings = json.loads((self.acct_dir("002") / "settings.json").read_text(encoding="utf-8"))
        settings["env"]["ANTHROPIC_MODEL"] = "stale-model"
        (self.acct_dir("002") / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        r = self.run_cli("status", "002")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("不一致", r.stdout)
        self.assertIn("cca sync 002", r.stdout)

    def test_api_key_helper_output_is_key_only(self):
        """凭据通道自检：helper 命令的输出必须就是密钥本身（不能带 banner/日志）。"""
        secret = "sk-ant-oat01-SUPERSECRET-VALUE"  # noqa: S105 - 测试用的假密钥
        self.seed_api_profile("002", key=secret)
        out = self._run_python(
            """
            import json, subprocess, sys
            from cca.paths import profile_dir
            settings = json.loads((profile_dir(sys.argv[2]) / "settings.json").read_text(encoding="utf-8"))
            done = subprocess.run(settings["apiKeyHelper"], shell=True, capture_output=True, text=True)
            print("RC", done.returncode)
            print("OUT", done.stdout.strip())
            print("ERR", done.stderr.strip() or "<空>")
            """,
            "002",
        )
        lines = out.stdout.strip().splitlines()
        self.assertEqual(lines[0], "RC 0")
        self.assertEqual(lines[1], f"OUT {secret}")
        self.assertEqual(lines[2], "ERR <空>")   # 不能有 banner/日志混进 stderr

    def test_profile_proxy_overrides_global_env(self):
        """单号代理优先于全局 `CCA_PROXY`。"""
        self.seed_api_profile("002", proxy="http://from-profile:1")
        env = self.build_env_of("002", env_extra={"CCA_PROXY": "http://from-env:2"})
        self.assertEqual(env["HTTPS_PROXY"], "http://from-profile:1")

    def test_oauth_profile_does_not_inherit_api_env(self):
        """从别的号（或 sideagent 守护进程）启动时，别人的密钥不能带进来，否则 401。"""
        self.run_cli("new", "003")
        env = self.build_env_of("003", env_extra={
            "ANTHROPIC_AUTH_TOKEN": "sk-别人的",
            "ANTHROPIC_API_KEY": "sk-别人的",
            "ANTHROPIC_BASE_URL": "https://别人的.example",
            "ANTHROPIC_MODEL": "deepseek-flash[1m]",
        })
        self.assertIsNone(env["ANTHROPIC_AUTH_TOKEN"])
        self.assertIsNone(env["ANTHROPIC_API_KEY"])
        self.assertIsNone(env["ANTHROPIC_BASE_URL"])
        self.assertIsNone(env["ANTHROPIC_MODEL"])

    def test_global_env_used_when_profile_proxy_unset(self):
        """单号没配代理时，用全局 `CCA_PROXY`。"""
        self.seed_api_profile("002", proxy="builtin")
        env = self.build_env_of("002", env_extra={"CCA_PROXY": "http://from-env:2"})
        self.assertEqual(env["HTTPS_PROXY"], "http://from-env:2")

    def test_set_proxy_for_subscription_profile(self):
        """订阅号把代理存进 .cca-proxy，启动时优先于全局。"""
        self.run_cli("new", "003")
        r = self.run_cli("set", "proxy", "003", "http://127.0.0.1:7897")
        self.assertEqual(r.returncode, 0, r.stderr)
        proxy_file = self.home / ".claude-accounts" / "003" / ".cca-proxy"
        self.assertEqual(proxy_file.read_text(encoding="utf-8").strip(), "http://127.0.0.1:7897")
        self.assertEqual(proxy_file.stat().st_mode & 0o777, 0o600)
        env = self.build_env_of("003", env_extra={"CCA_PROXY": "http://from-env:2"})
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:7897")

    def test_set_proxy_for_api_profile_updates_marker(self):
        """API 号的代理写进清单，和 `cca add --proxy` 同一个位置。"""
        self.seed_api_profile("002", proxy="builtin")
        r = self.run_cli("set", "proxy", "002", "http://127.0.0.1:7897")
        self.assertEqual(r.returncode, 0, r.stderr)
        marker = json.loads((self.home / ".claude-accounts" / "002" / ".cca-profile.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["proxy"], "http://127.0.0.1:7897")

    def test_set_proxy_global_writes_shell_rc_once(self):
        """全局命令写进 shell 启动文件；重复执行只改同一段。"""
        r = self.run_cli("set", "proxy", "--global", "http://g:1", SHELL="/bin/bash")
        self.assertEqual(r.returncode, 0, r.stderr)
        rc = self.home / ".bashrc"
        self.assertEqual(rc.read_text(encoding="utf-8").count("claude-acct proxy"), 2)
        self.assertIn("export CCA_PROXY=http://g:1", rc.read_text(encoding="utf-8"))
        r = self.run_cli("set", "proxy", "--global", "http://g:2", SHELL="/bin/bash")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = rc.read_text(encoding="utf-8")
        self.assertEqual(text.count("claude-acct proxy"), 2)
        self.assertIn("export CCA_PROXY=http://g:2", text)
        self.assertNotIn("http://g:1", text)

    def test_tui_defaults_to_fullscreen(self):
        """默认用 alt-screen 渲染（面板里的鼠标点击只在它下面有效）。"""
        self.run_cli("new", "003")
        env = self.build_env_of("003")
        self.assertEqual(env["CLAUDE_CODE_NO_FLICKER"], "1")

    def test_set_tui_default_turns_renderer_back(self):
        """`cca set tui default` 回到经典渲染，并且不继承外面的 NO_FLICKER。"""
        self.run_cli("new", "003")
        r = self.run_cli("set", "tui", "default")
        self.assertEqual(r.returncode, 0, r.stderr)
        env = self.build_env_of("003", env_extra={"CLAUDE_CODE_NO_FLICKER": "1"})
        self.assertIsNone(env["CLAUDE_CODE_NO_FLICKER"])

    def test_set_tui_rejects_unknown_mode(self):
        r = self.run_cli("set", "tui", "huge")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("fullscreen", r.stderr + r.stdout)

    def test_doctor_probe_key_reports_validity(self):
        """`--probe-key` 才真的带密钥发一次最小请求：200 = 有效，401 = 被拒（FAIL + 修复命令）。"""
        seen = {}

        class Endpoint(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                seen["auth"] = self.headers.get("authorization") or ""
                seen["x_api_key"] = self.headers.get("x-api-key") or ""
                seen["model"] = body.get("model")
                code = 200 if seen["auth"].endswith("good-key-1234567890") else 401
                payload = b'{"id":"msg_1","type":"message","role":"assistant","content":[]}'
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = HTTPServer(("127.0.0.1", 0), Endpoint)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            self.seed_api_profile("002", base_url=f"http://127.0.0.1:{port}", provider="generic",
                                  env={"ANTHROPIC_MODEL": "probe-model"}, key="good-key-1234567890")
            r = self.run_cli("doctor", "--online", "--probe-key", CCA_PROXY="none")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("密钥有效", r.stdout)
            self.assertEqual(seen["auth"], "Bearer good-key-1234567890")
            self.assertEqual(seen["x_api_key"], "good-key-1234567890")   # 两个头都带
            self.assertEqual(seen["model"], "probe-model")

            self.run_cli("rm", "002", "--yes")
            self.seed_api_profile("003", base_url=f"http://127.0.0.1:{port}", provider="generic",
                                  key="bad-key-1234567890")
            r = self.run_cli("doctor", "--online", "--probe-key", CCA_PROXY="none")
            self.assertEqual(r.returncode, 1)
            self.assertIn("密钥被端点拒绝", r.stdout)
            self.assertIn("cca login 003 --key", r.stdout)
        finally:
            server.shutdown()
            server.server_close()

    def test_probe_reports_upstream_error_distinctly(self):
        """5xx 是网关/上游的问题，不能说成"密钥有效"；其它 4xx 只能说"可达但被拒"。"""
        state = {"code": 502}

        class Gateway(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = b'{"error":{"message":"Upstream request failed"}}'
                self.send_response(state["code"])
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = HTTPServer(("127.0.0.1", 0), Gateway)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            self.seed_api_profile("002", base_url=f"http://127.0.0.1:{port}", provider="generic")
            r = self.run_cli("doctor", "--online", "--probe-key", CCA_PROXY="none")
            self.assertEqual(r.returncode, 1)
            self.assertIn("端点/上游报错", r.stdout)
            self.assertNotIn("密钥有效", r.stdout)
            state["code"] = 404
            r = self.run_cli("doctor", "--online", "--probe-key", CCA_PROXY="none")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("请求被拒", r.stdout)
            self.assertNotIn("密钥有效", r.stdout)
        finally:
            server.shutdown()
            server.server_close()

    def test_trust_inherited_from_all_profiles(self):
        """新号该继承**所有其它号**已信任的目录（不只是 001）——否则在别人信任过的目录里 --bg 会被挡。"""
        self.run_cli("new", "002")
        cfg = self.acct_dir("001") / ".claude.json"
        data = json.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
        data.setdefault("projects", {})["/proj/from-001"] = {"hasTrustDialogAccepted": True}
        cfg.write_text(json.dumps(data), encoding="utf-8")
        other = self.acct_dir("002") / ".claude.json"
        other.write_text(json.dumps({"projects": {"/proj/from-002": {"hasTrustDialogAccepted": True}}}),
                         encoding="utf-8")
        r = self.run_cli("add", "codex", "--url", "https://gw.example", "--key", "sk-1234567890abcd")
        self.assertEqual(r.returncode, 0, r.stderr)
        projects = json.loads((self.acct_dir("codex") / ".claude.json").read_text(encoding="utf-8"))["projects"]
        self.assertTrue(projects["/proj/from-001"]["hasTrustDialogAccepted"])
        self.assertTrue(projects["/proj/from-002"]["hasTrustDialogAccepted"])

    def test_base_url_trailing_slash_normalized(self):
        self.run_cli("new", "002")
        self.run_cli("add", "gtw", "--url", "https://gw.example:8080/", "--key", "sk-1234567890abcd")
        marker = json.loads((self.acct_dir("gtw") / ".cca-profile.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["base_url"], "https://gw.example:8080")

    def test_doctor_probe_key_without_key_guides(self):
        self.seed_api_profile("002", key=None)
        r = self.run_cli("doctor", "--online", "--probe-key", CCA_PROXY="none")
        self.assertEqual(r.returncode, 1)                       # 缺 key 本身已是 FAIL
        self.assertIn("跳过密钥验证", r.stdout)                  # 不谎报"端点连不上"
        self.assertIn("cca login 002 --key", r.stdout)

    def test_doctor_warns_auth_conflict(self):
        """订阅登录与网关凭据同时存在是冲突状态：提示，但不 fail。"""
        self.seed_api_profile("002")
        self.seed_credentials("002")
        r = self.run_cli("doctor")
        self.assertIn("冲突", r.stdout)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_sync_api_profile_preserves_env(self):
        self.seed_api_profile("002")
        defaults = self.home / ".cca" / "defaults" / "settings.json"
        defaults.parent.mkdir(parents=True, exist_ok=True)
        defaults.write_text(
            json.dumps({"theme": "light", "env": {"SHARED": "1"}}), encoding="utf-8"
        )
        r = self.run_cli("sync", "002")
        self.assertEqual(r.returncode, 0, r.stderr)
        settings = json.loads((self.acct_dir("002") / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["theme"], "light")
        self.assertEqual(settings["env"]["SHARED"], "1")
        self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"],
                         "https://api.deepseek.com/anthropic")
        self.assertIn("apiKeyHelper", settings)

    def test_rm_api_profile_mentions_key(self):
        self.seed_api_profile("002")
        r = self.run_cli("rm", "002", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("API key", r.stdout)
        self.assertFalse((self.acct_dir("002") / ".api-key").exists())

    def test_which_reports_api_kind(self):
        self.seed_api_profile("002")
        r = self.run_cli("which", "002")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("api:deepseek", r.stdout)


    def test_rename_moves_profile_and_key_path(self):
        """`cca rename` 改目录、重写 apiKeyHelper 的旧路径；旧名消失、新名可用。"""
        self.run_cli("add", "cx", "--provider", "deepseek", "--key", "sk-test")
        before = json.loads((self.acct_dir("cx") / "settings.json").read_text(encoding="utf-8"))
        self.assertIn("/cx/", before["apiKeyHelper"])
        r = self.run_cli("mv", "cx", "gpt-ds")          # 命令名是 mv（rename 是别名）
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.acct_dir("cx").exists(), "旧目录应已消失")
        after = json.loads((self.acct_dir("gpt-ds") / "settings.json").read_text(encoding="utf-8"))
        self.assertIn("/gpt-ds/", after["apiKeyHelper"])
        self.assertTrue((self.acct_dir("gpt-ds") / ".api-key").is_file())
        again = self.run_cli("mv", "cx", "other")
        self.assertNotEqual(again.returncode, 0, "旧名已不存在，应报错")

    def test_ls_accepts_profile_filter(self):
        """`cca ls 006` 只看指定号；不认识的号名给明确报错（不是"多余参数"）。"""
        self.run_cli("new", "002")
        self.seed_credentials("002")
        self.seed_usage("002", five=10, seven=20)
        one = self.run_cli("ls", "002")
        self.assertEqual(one.returncode, 0, one.stdout + one.stderr)
        self.assertIn("002", one.stdout)
        self.assertNotIn("001", one.stdout.split("PROFILE", 1)[-1].splitlines()[1] if "PROFILE" in one.stdout else "")
        bad = self.run_cli("ls", "nosuch")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("没有这个号", bad.stdout + bad.stderr)

    def test_models_banner_shows_main_and_sub(self):
        """启动横幅补一句真实模型：/model 从 2.1.317 起不再显示映射后的模型名。"""
        self.seed_api_profile("cx", provider="deepseek",
                              env={"ANTHROPIC_MODEL": "gpt-6-astra",
                                   "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-flash"}, render=True)
        out = self._run_python(
            "import sys\nfrom cca.api.client import models_banner\n"
            "print(f'B={models_banner('cx')}')\n")
        self.assertIn("B= · 主 gpt-6-astra · 子 deepseek-flash", out.stdout)

    def test_identity_banner_by_kind(self):
        """启动横幅的身份摘要：订阅号给邮箱；API 号给「种类 · key 状态」，不能说未登录。"""
        self.run_cli("new", "002")
        (self.acct_dir("002") / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "claude002@example.com"}}), encoding="utf-8"
        )
        self.seed_api_profile("003")
        out = self._run_python(
            """
            import sys
            from cca.api.client import identity_banner
            for name in ("002", "003"):
                print(identity_banner(name))
            """,
        )
        lines = out.stdout.strip().splitlines()
        self.assertEqual(lines[0], "claude002@example.com")
        self.assertEqual(lines[1], "api:deepseek · key 已配置")
        # 缺 key 时横幅直接给修复入口
        self.run_cli("logout", "003")
        out = self._run_python(
            """
            import sys
            from cca.api.client import identity_banner
            print(identity_banner("003"))
            """,
        )
        self.assertEqual(out.stdout.strip(), "api:deepseek · key 缺失（cca login 003 --key <key>）")

    def test_handoff_notes_daemon_lifetime(self):
        """搬运"等输入"的会话时要说明：面板登记只在 CLI daemon 存活期间有效（实测）。"""
        self.run_cli("new", "002")
        long_id = self.seed_carriable("001", "abcde123", state="blocked", name="job")
        self.seed_daemon_dir("001")
        r = self.run_cli("handoff", "001", "002", "--all",
                         FAKE_CLAUDE_AGENTS=self.agents_payload("abcde123", long_id, state="blocked"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("daemon", r.stderr)
        self.assertIn("cca 002 --bg --resume", r.stderr)

    def test_serial_guard_ignores_api_profiles(self):
        """API 号不参与串行约定：ds 在跑时启动 002 不该挨警告；两个订阅号同时跑才警告。"""
        self.run_cli("new", "002")
        self.run_cli("new", "003")
        self.seed_api_profile("ds")
        self.seed_daemon_dir("ds")
        agents = self.agents_json(("busy", "在跑"))
        # ds 在跑 → 启动 001（订阅号）：不警告
        r = self.run_cli("001", "-p", "hi", FAKE_CLAUDE_AGENTS=agents)
        self.assertNotIn("建议串行使用", r.stderr)
        # 002 在跑 → 启动 003（都是订阅号）：警告
        self.seed_daemon_dir("002")
        r = self.run_cli("003", "-p", "hi", FAKE_CLAUDE_AGENTS=agents)
        self.assertIn("建议串行使用", r.stderr)
        # 从 API 号启动：也不提
        r = self.run_cli("ds", "-p", "hi", FAKE_CLAUDE_AGENTS=agents)
        self.assertNotIn("建议串行使用", r.stderr)

    def test_doctor_serial_check_ignores_api_profiles(self):
        self.run_cli("new", "002")
        self.seed_api_profile("ds")
        self.seed_daemon_dir("ds")
        r = self.run_cli("doctor", FAKE_CLAUDE_AGENTS=self.agents_json(("busy", "在跑")))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("建议串行使用", r.stdout)
        self.assertIn("不参与串行约定", r.stdout)


    def test_sync_reseeds_settings(self):
        self.run_cli("new", "002")
        defaults = self.home / ".cca" / "defaults" / "settings.json"
        defaults.parent.mkdir(parents=True, exist_ok=True)
        defaults.write_text('{"theme": "light"}', encoding="utf-8")
        r = self.run_cli("sync", "002")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            (self.acct_dir("002") / "settings.json").read_text(encoding="utf-8"), '{"theme": "light"}'
        )

    # -- 切号 ------------------------------------------------------------




    def test_reserved_profile_names_are_rejected(self):
        for name in ("switch", "stop-bg", "sessions", "resume", "add", "ls"):
            self.assertNotEqual(self.run_cli("new", name).returncode, 0, name)

    # -- 建号 + 登录一步到位 ----------------------------------------------
    def test_add_creates_profile_and_logs_in(self):
        r = self.run_cli("add", "003", "--email", "x@example.com")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.acct_dir("003").is_dir())
        self.assertTrue((self.acct_dir("003") / "projects").is_symlink())
        self.assertEqual(self.record()["argv"], ["auth", "login", "--email", "x@example.com"])

    def test_add_existing_profile_skips_new(self):
        self.run_cli("new", "002")
        r = self.run_cli("add", "002")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("已存在，跳过建号", r.stdout)
        self.assertEqual(self.record()["argv"], ["auth", "login"])

    # -- 接管会话（switch --carry）---------------------------------------
    def seed_carriable(self, profile="001", short="abcde123", *, state="blocked", name="job", cwd=None):
        """在 profile 上造一条后台会话 + transcript，并让源号信任它的目录。"""
        cwd = cwd or str(self.home)
        long_id = f"{short}-0000-0000-0000-000000000000"
        self.seed_transcript(profile, long_id, title=name, prompt="继续做X", cwd=cwd)
        self.seed_daemon_dir(profile)
        cfg = self.acct_dir("001") / ".claude.json" if profile == "001" else self.acct_dir(profile) / ".claude.json"
        data = json.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
        data.setdefault("projects", {})[cwd] = {"hasTrustDialogAccepted": True}
        cfg.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return long_id

    def seed_credentials(self, profile="002"):
        """造一个"已登录"的凭据文件（只为本地面板逻辑用；token 不会真发出去）。"""
        f = self.acct_dir(profile) / ".credentials.json"
        f.write_text(json.dumps({"claudeAiOauth": {"accessToken": "fake-token"}}), encoding="utf-8")
        return f

    def usage_live_fixture(self, values):
        """`{号: (5小时, 7天)}` → 实时额度的桩文件（CCA_USAGE_LIVE_FILE）。"""
        path = self.home / "usage-live.json"
        path.write_text(json.dumps({
            name: {"five_hour": {"utilization": five, "resets_at": None},
                   "seven_day": {"utilization": seven, "resets_at": None}}
            for name, (five, seven) in values.items()
        }), encoding="utf-8")
        return path

    def agents_payload(self, short, long_id, *, state="blocked", name="job", cwd=None):
        return json.dumps(
            [{"id": short, "kind": "background", "name": name, "cwd": cwd or str(self.home),
              "sessionId": long_id, "state": state}]
        )

    def seed_api_profile(self, name="002", *, base_url="https://api.deepseek.com/anthropic",  # noqa: PLR0913 - 测试造数据，参数各自直白
                         provider="deepseek", env=None, key="sk-ant-oat01-SUPERSECRET-VALUE",
                         proxy="none", render=True):
        """造一个 API 号：清单 + 密钥（不渲染 settings —— 那由被测代码做）。"""
        self.run_cli("new", name)
        marker = {
            "kind": "api",
            "provider": provider,
            "base_url": base_url,
            "env": dict(PROVIDERS_ENV.get(provider, PROVIDERS_ENV["deepseek"])) if env is None else env,
            "proxy": proxy,
            "created_at": int(time.time()),
        }
        marker_path = self.acct_dir(name) / ".cca-profile.json"
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        marker_path.chmod(0o600)
        if key:
            key_path = self.acct_dir(name) / ".api-key"
            key_path.write_text(key + "\n", encoding="utf-8")
            key_path.chmod(0o600)
        if render:
            self._run_python(
                """
                import sys
                from cca.account.store import read_marker, write_settings_env
                marker, error = read_marker(sys.argv[2])
                assert not error, error
                write_settings_env(sys.argv[2], marker)
                """,
                name,
            )
        return marker

    def build_env_of(self, name, *, env_extra=None):
        """跑 build_env(name) 并返回关心的键（未设置的键为 None）。"""
        out = self._run_python(BUILD_ENV_CODE, name, env_extra=env_extra)
        self.assertEqual(out.returncode, 0, out.stderr)
        result = {}
        for line in out.stdout.strip().splitlines():
            key, _, value = line.partition(" = ")
            result[key] = None if value == "None" else value
        return result

    def kind_of(self, name):
        """调被测代码里的 profile_kind()：断言"有清单=api / 没有=oauth"。"""
        return self._run_python(
            """
            import sys
            from cca.account.store import profile_kind
            print(profile_kind(sys.argv[2]))
            """,
            name,
        ).stdout.strip()

    def calls_with_cwd(self):
        p = self.home / "claude-cwd.log"
        return p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    def seed_usage(self, profile="001", *, five=10, seven=20, five_reset_hours=2.0,  # noqa: PLR0913 - 测试造数据，参数各自直白
                   seven_reset_days=5.0, age_minutes=5.0):
        """写一份 cachedUsageUtilization 快照；重置时间传负数就是"已经重置"。"""
        # 001 号的全局配置在 $HOME/.claude.json，不在 ~/.claude 下。
        cfg = self.acct_dir("001") / ".claude.json" if profile == "001" \
            else self.acct_dir(profile) / ".claude.json"
        data = json.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
        now = datetime.now(UTC)
        data["cachedUsageUtilization"] = {
            "fetchedAtMs": int((time.time() - age_minutes * 60) * 1000),
            "utilization": {
                "five_hour": {
                    "utilization": five,
                    "resets_at": (now + timedelta(hours=five_reset_hours)).isoformat(),
                },
                "seven_day": {
                    "utilization": seven,
                    "resets_at": (now + timedelta(days=seven_reset_days)).isoformat(),
                },
            },
        }
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps(data), encoding="utf-8")
        return cfg

    def test_handoff_moves_session_to_target(self):
        self.run_cli("new", "002")
        sid = self.seed_carriable()
        # 交接时改到 ~/llm 起；源号先信任它，copy_trust 才有东西可复制。
        cfg = self.acct_dir("001") / ".claude.json"
        data = json.loads(cfg.read_text(encoding="utf-8"))
        data["projects"][str(self.home / "llm")] = {"hasTrustDialogAccepted": True}
        cfg.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        (self.acct_dir("002") / ".claude.json").write_text("{}", encoding="utf-8")
        payload = self.agents_payload("abcde123", sid, cwd=str(self.home))
        r = self.run_cli("handoff", "001", "002", "--only=abcde123", FAKE_CLAUDE_AGENTS=payload)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.calls()
        self.assertIn("stop abcde123", calls)                      # 先停源号（否则只会 fork 副本）
        self.assertIn(f"--bg --resume {sid} /rename job", calls)   # 再在目标号续跑（保住原名）
        # 家目录的会话改到 ~/llm 里起（家目录不能长期信任）
        self.assertIn(f"--bg --resume {sid} /rename job\t{self.home / 'llm'}", self.calls_with_cwd())
        self.assertIn("已从 001 搬到 002：1 条", r.stdout)
        data = json.loads((self.acct_dir("002") / ".claude.json").read_text(encoding="utf-8"))
        self.assertTrue(data["projects"][str(self.home / "llm")]["hasTrustDialogAccepted"])  # 补了信任位

    def test_push_cleans_up_stray_copy_and_falls_back_to_print(self):
        """CLI 判成"要开副本"时：清掉副本，改用 -p 把提示推给原会话（原 id 不变）。

        真机踩到两次：`--bg --resume` 对 carry 过来、自带 saved options 的会话一律给副本
        （`started a copy as 2a7c67a9 / ba4035b1`），会让 cca wait 偷偷分叉用户正在用的会话。
        """
        out = self._run_python(PUSH_CLASSIFY_PROBE, "copy", "abcde123")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("state=sent", out.stdout)
        self.assertIn("已清掉并改用 -p", out.stdout)
        self.assertIn("call=cca 001 stop feedface-1111-2222-3333-444455556666", out.stdout)  # 副本 stop
        self.assertIn("call=cca 001 rm feedface-1111-2222-3333-444455556666", out.stdout)    # 副本删
        self.assertIn("call=cca 001 -p 继续 --resume abcde123", out.stdout)                  # 回退 -p

    def test_handoff_stops_only_the_selected_session(self):
        """只选一条时，源号其余会话不许被 stop —— 真机踩到：选了 1，别的也被连坐停了。"""
        self.run_cli("new", "002")
        moved = self.seed_carriable(short="abcde123", name="move me")
        keep = self.seed_carriable(short="beef0001", name="keep me")
        payload = json.dumps([
            {"id": "abcde123", "kind": "background", "name": "move me", "cwd": str(self.home),
             "sessionId": moved, "state": "blocked"},
            {"id": "beef0001", "kind": "background", "name": "keep me", "cwd": str(self.home),
             "sessionId": keep, "state": "blocked"},
        ])
        r = self.run_cli("handoff", "001", "002", "--only=abcde123", FAKE_CLAUDE_AGENTS=payload)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.calls()
        self.assertIn("stop abcde123", calls)                # 选中那条照停（否则只会 fork 副本）
        self.assertNotIn("stop beef0001", calls)             # 没选的不许停
        self.assertNotIn(keep, "\n".join(calls))             # 也不许被搬走
        self.assertIn("已从 001 搬到 002：1 条", r.stdout)

    def test_strip_tool_search_covers_subagent_transcripts(self):
        """剥离要连子代理的 transcript 一起（`<会话id>/subagents/agent-*.jsonl`）。

        真机踩到：只剥主文件，子代理照样 422 `unknown variant tool_definition`。
        """
        self.seed_api_profile("ds")
        sid = self.seed_carriable(short="abcde123", name="to ds")
        base = self.profile_base("001") / "projects" / "-proj-x"
        main = base / f"{sid}.jsonl"
        with main.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "attachment",
                                 "attachment": {"type": "deferred_tools_record"}}) + "\n")
        sub = base / sid / "subagents"
        sub.mkdir(parents=True, exist_ok=True)
        for name, n in (("agent-aaa.jsonl", 2), ("agent-bbb.jsonl", 1)):
            (sub / name).write_text(
                "".join(json.dumps({"type": "attachment",
                                    "attachment": {"type": "deferred_tools_record"}}) + "\n"
                        for _ in range(n)), encoding="utf-8")
        payload = self.agents_payload("abcde123", sid, cwd=str(self.home))
        # 不带开关：提醒里要算上子代理（1 + 2 + 1 = 4 条）
        r = self.run_cli("handoff", "001", "ds", "--only=abcde123", FAKE_CLAUDE_AGENTS=payload)
        self.assertIn("4 条", r.stdout + r.stderr)
        # 带开关：主文件与两个子代理文件都剥干净
        r = self.run_cli("handoff", "001", "ds", "--only=abcde123", "--strip-tool-search",
                         FAKE_CLAUDE_AGENTS=payload)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("剥掉 4 条", r.stdout)
        for f in (main, sub / "agent-aaa.jsonl", sub / "agent-bbb.jsonl"):
            self.assertNotIn("deferred_tools_record", f.read_text(encoding="utf-8"))
        backups = sorted((self.home / ".claude-accounts" / "_session-backups").glob("*.jsonl"))
        self.assertEqual(len(backups), 3, [b.name for b in backups])

    def test_api_launch_args_do_not_swallow_positional_args(self):
        """注入的排除参数不许吃掉后面的位置参数。

        真机踩到：`cca ds stop fe35821d` 被送成 `--disallowedTools Artifact,… stop fe35821d`，
        而它是**变参** → CLI 把 `stop`/`fe35821d` 当成工具名，回
        `Permission deny rule "stop" matches no known tool`，stop 根本没执行。
        """
        self.seed_api_profile("ds")
        out = self._run_python(API_ARGS_PROBE, "ds")
        line = out.stdout.strip()
        self.assertIn("--disallowedTools=Artifact,ArtifactComments,ArtifactData", line)
        self.assertTrue(line.endswith("--bg --resume abc"), line)      # 后面的参数原样保留

    def test_api_settings_env_disables_artifact_toolset(self):
        """API 号的 settings env 要带 `CLAUDE_CODE_ARTIFACT_TOOLSET=""`。

        为什么不是只靠 `--disallowedTools`：那是**启动参数**，CLI 自己重启会话（rewind / attach /
        用 saved options 续）时参数就丢了 → 又 400（真机 rewind 后复现）。env 跟着账号走，重启也生效。
        """
        self.seed_api_profile("ds")
        env = json.loads((self.acct_dir("ds") / "settings.json").read_text(encoding="utf-8"))["env"]
        self.assertEqual(env.get("CLAUDE_CODE_ARTIFACT_TOOLSET"), "")
        # 订阅号不许被动手
        self.run_cli("new", "002")
        settings = self.acct_dir("002") / "settings.json"
        text = settings.read_text(encoding="utf-8") if settings.exists() else "{}"
        self.assertNotIn("CLAUDE_CODE_ARTIFACT_TOOLSET", text)

    def test_api_launch_args_exclude_artifact_tools(self):
        """API 号启动默认排除第三方端点不认的第一方工具（Artifact 的 schema 会被判成非正则 → 400）。

        真机：`400 Invalid schema for function 'Artifact': "^[^\\0]*$" is not a "regex"`（handoff 到 ds 后）。
        订阅号不动；使用者自己写了 --disallowedTools 就不再叠加。
        """
        self.run_cli("new", "002")
        self.seed_api_profile("ds")
        out = self._run_python(API_ARGS_PROBE, "002", "ds")
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = dict(line.split(" -> ", 1) for line in out.stdout.strip().splitlines())
        self.assertNotIn("disallowedTools", lines["002"])            # 订阅号不加参数
        # 必须是 `=` 单 token 形式，而且**不能吃掉后面的位置参数**
        # DeepSeek 端点还额外拒绝 deferred tool search
        self.assertIn("--disallowedTools=Artifact,ArtifactComments,ArtifactData,"
                      "SendFeedback,ToolSearch,MCPSearch --bg --resume abc", lines["ds"])

    def test_handoff_to_api_target_passes_disallowed_tools(self):
        """handoff 接管时也要带上：它直接调 claude，不经过 cca <号> 那条路。"""
        self.seed_api_profile("ds")
        sid = self.seed_carriable(short="abcde123", name="to ds")
        payload = self.agents_payload("abcde123", sid, cwd=str(self.home))
        r = self.run_cli("handoff", "001", "ds", "--only=abcde123", FAKE_CLAUDE_AGENTS=payload)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        resume = [c for c in self.calls() if "--bg --resume" in c]
        self.assertTrue(resume, self.calls())
        self.assertIn("--disallowedTools=Artifact", resume[0])

    def test_handoff_strip_tool_search_for_api_target(self):
        """搬到 API 号（第三方端点）：有 tool search 记录就提醒，`--strip-tool-search` 才剥（先备份）。

        真机踩到：那条会话 resume 到 DeepSeek 报
        `422 … unknown variant tool_definition, expected tool_reference`；剥掉记录即可跑。
        """
        self.seed_api_profile("ds")
        sid = self.seed_carriable(short="abcde123", name="to ds")
        path = self.profile_base("001") / "projects" / "-proj-x" / f"{sid}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            for i in range(3):
                fh.write(json.dumps({"type": "attachment", "uuid": f"u{i}",
                                     "attachment": {"type": "deferred_tools_record",
                                                    "tools": ["WebFetch"]}}) + "\n")
        payload = self.agents_payload("abcde123", sid, cwd=str(self.home))
        # ① 不带开关：只提醒，不动文件
        r = self.run_cli("handoff", "001", "ds", "--only=abcde123", FAKE_CLAUDE_AGENTS=payload)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("tool search", r.stdout + r.stderr)
        self.assertIn("--strip-tool-search", r.stdout + r.stderr)
        self.assertEqual(path.read_text(encoding="utf-8").count("deferred_tools_record"), 3)
        # ② 带开关：剥掉 + 备份
        r = self.run_cli("handoff", "001", "ds", "--only=abcde123", "--strip-tool-search",
                         FAKE_CLAUDE_AGENTS=payload)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("剥掉 3 条", r.stdout)
        self.assertEqual(path.read_text(encoding="utf-8").count("deferred_tools_record"), 0)
        backups = list((self.home / ".claude-accounts" / "_session-backups").glob("*.jsonl"))
        self.assertTrue(backups, "要有备份")
        self.assertIn("deferred_tools_record", backups[0].read_text(encoding="utf-8"))

    def test_handoff_non_tty_without_only_fails(self):
        """管道里既没有 --only 也没有 --all 时不猜：明确报错，且一条会话都不动。"""
        self.run_cli("new", "002")
        sid = self.seed_carriable()
        payload = self.agents_payload("abcde123", sid, cwd=str(self.home))
        r = self.run_cli("handoff", "001", "002", FAKE_CLAUDE_AGENTS=payload, stdin_data="")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("非交互环境", r.stderr)
        self.assertEqual([c for c in self.calls() if c.startswith(("stop ", "--bg"))], [])

    def test_handoff_dry_run_selects_nothing_by_default(self):
        """默认一个都不勾：计划表里所有会话都标"不动"，没写的 --only 一条都不搬。"""
        self.run_cli("new", "002")
        parked = self.seed_carriable(short="parked01", name="daily")
        self.seed_carriable(short="running1", name="heavy", state="working")
        self.seed_daemon_dir("002")
        payload = json.dumps([
            {"id": "parked01", "kind": "background", "name": "daily", "cwd": str(self.home),
             "sessionId": parked, "state": "blocked"},
            {"id": "running1", "kind": "background", "name": "heavy", "cwd": str(self.home),
             "sessionId": "running1-0000-0000-0000-000000000000", "state": "working"},
        ])
        r = self.run_cli("handoff", "001", "002", "--dry-run", FAKE_CLAUDE_AGENTS=payload)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("接管计划 → 002", r.stdout)
        self.assertNotIn("等待", r.stdout)       # parked01 默认不勾
        self.assertIn("不动", r.stdout)          # running1 也不勾
        self.assertNotIn("会继续跑", r.stdout)
        self.assertEqual([c for c in self.calls() if c.startswith(("stop ", "--bg"))], [])

    def test_handoff_dry_run_touches_nothing(self):
        self.run_cli("new", "002")
        sid = self.seed_carriable()
        payload = self.agents_payload("abcde123", sid, cwd=str(self.home))
        r = self.run_cli("handoff", "001", "002", "--only=abcde123", "--dry-run",
                         FAKE_CLAUDE_AGENTS=payload)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("接管计划", r.stdout)
        self.assertIn("--dry-run", r.stdout)
        self.assertEqual([c for c in self.calls() if c.startswith(("stop ", "--bg"))], [])

    def test_handoff_explicit_running_is_taken(self):
        """显式点名一个正在跑的会话 = 明确要接管它（会继续跑）。"""
        self.run_cli("new", "002")
        sid = self.seed_carriable(state="working")
        payload = self.agents_payload("abcde123", sid, state="working", cwd=str(self.home))
        r = self.run_cli("handoff", "001", "002", "--only=abcde123", FAKE_CLAUDE_AGENTS=payload)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"--bg --resume {sid} /rename job", self.calls())

    def test_handoff_all_takes_running(self):
        self.run_cli("new", "002")
        parked = self.seed_carriable(short="parked01", name="daily")
        running = "running1-0000-0000-0000-000000000000"
        self.seed_transcript("001", running, title="heavy", cwd=str(self.home))
        payload = json.dumps([
            {"id": "parked01", "kind": "background", "name": "daily", "cwd": str(self.home),
             "sessionId": parked, "state": "blocked"},
            {"id": "running1", "kind": "background", "name": "heavy", "cwd": str(self.home),
             "sessionId": running, "state": "working"},
        ])
        r = self.run_cli("handoff", "001", "002", "--all", FAKE_CLAUDE_AGENTS=payload)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        bg = sorted(c for c in self.calls() if c.startswith("--bg"))
        self.assertEqual(bg, sorted([f"--bg --resume {parked} /rename daily", f"--bg --resume {running} /rename heavy"]))

    def test_handoff_unknown_prefix_fails(self):
        self.run_cli("new", "002")
        sid = self.seed_carriable()
        payload = self.agents_payload("abcde123", sid, cwd=str(self.home))
        r = self.run_cli("handoff", "001", "002", "--only=zzz", FAKE_CLAUDE_AGENTS=payload)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("匹配到 0 条", r.stderr)

    def test_handoff_rejects_same_profile(self):
        r = self.run_cli("handoff", "001", "001")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("同一个", r.stderr)

    def test_handoff_unknown_target_fails(self):
        r = self.run_cli("handoff", "001", "009")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("不存在", r.stderr)




    def test_ls_marks_reset_passed_and_absolute_times(self):
        """缓存里的百分比要配上重置时刻看：001 的 5 小时窗口已经重置，98% 是旧值。"""
        self.run_cli("new", "002")
        self.run_cli("new", "003")
        self.seed_usage("001", five=98, seven=95, five_reset_hours=-3, age_minutes=600)
        self.seed_usage("002", five=3, seven=59, five_reset_hours=5)
        self.seed_usage("003", five=94, seven=13, five_reset_hours=3)
        r = self.run_cli("ls")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("98%*", r.stdout)
        self.assertIn("重置时刻已过", r.stdout)
        self.assertIn("快照偏旧", r.stdout)
        # 面板不再有"最空闲"列（使用者反馈没必要；判断留在 --json 的 roomiest 里）。
        self.assertNotIn("最空闲", r.stdout)
        # 时间是绝对时刻，不再有"3 小时后"这种要用户做减法的相对说法。
        self.assertRegex(r.stdout, r"\d\d-\d\d \d\d:\d\d")
        for relative in ("小时后", "天后", "分钟前", "小时前"):
            self.assertNotIn(relative, r.stdout)

    def test_ls_json_includes_usage_and_roomiest(self):
        self.run_cli("new", "002")
        self.seed_usage("001", five=90, seven=90)
        self.seed_usage("002", five=1, seven=10, five_reset_hours=1)
        r = self.run_cli("ls", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["roomiest"], "002")
        by_profile = {p["profile"]: p for p in data["profiles"]}
        self.assertEqual(by_profile["002"]["usage"]["windows"]["five_hour"]["percent"], 1)
        self.assertFalse(by_profile["002"]["usage"]["windows"]["five_hour"]["reset_passed"])
        self.assertGreater(by_profile["001"]["usage"]["fetched_at"], 0)
        self.assertIn("background", by_profile["001"])

    def test_ls_without_snapshot_shows_dash(self):
        self.run_cli("new", "002")
        r = self.run_cli("ls")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("-", r.stdout)
        self.assertNotIn("最空闲", r.stdout)

    def test_ls_tilde_only_for_genuinely_stale_values(self):
        """`~` 只标"真的旧"（超过 5 分钟）的值；刚刷过的快照不算，否则每行都挂 `~` 变噪音。"""
        self.run_cli("new", "002")
        self.seed_credentials("002")
        self.seed_usage("002", five=7, seven=42, age_minutes=1)      # 1 分钟前的快照：算新鲜
        r = self.run_cli("ls")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("~ = 不是刚取的实时值", r.stdout)
        self.assertNotIn("7%~", r.stdout)

    def test_ls_marks_tilde_when_value_is_old(self):
        """超过 5 分钟才标 `~`（并解释来源）。"""
        self.run_cli("new", "002")
        self.seed_credentials("002")
        self.seed_usage("002", five=7, seven=42, age_minutes=30)
        r = self.run_cli("ls")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("~", r.stdout)

    def test_ls_skips_claude_when_snapshot_is_fresh(self):
        """本地快照比缓存新时，不去起 `claude -p /usage` —— 首刷那十几秒就是它造成的。

        真机：`cca ls` 第一次四个号全部 "claude 超时"（13 秒一刀切），面板只能端出旧数字。
        """
        self.run_cli("new", "002")
        self.seed_credentials("002")
        self.seed_usage("002", five=7, seven=42, age_minutes=0)     # 快照：刚刚刷新过
        cache = self.acct_dir("002").parent / "_usage-cache.json"
        cache.write_text(json.dumps({"002": {"windows": {
            "five_hour": {"label": "5 小时", "percent": 90, "resets_at": None, "reset_passed": False},
            "seven_day": {"label": "7 天", "percent": 80, "resets_at": None, "reset_passed": False},
        }, "fetched_at": time.time() - 3600}}), encoding="utf-8")   # 缓存：一小时前
        fixture = self.usage_live_fixture({"002": (99, 98)})        # 若真去取，会看到 99/98
        r = self.run_cli("ls", CCA_USAGE_LIVE_FILE=str(fixture))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("42%", r.stdout)          # 用快照（更新）
        self.assertNotIn("98%", r.stdout)

    def test_ls_refreshes_when_snapshot_is_old(self):
        """快照超过新鲜窗口（5 分钟）就不再当"当前额度"：起一次 /usage 取实时。

        真机：快照是 claude 在会话启动/结束时刷的，闲置的号能停在二三十分钟前；
        原来的 30 分钟窗口让 `cca ls` 一直端旧数字，和会话里看到的对不上。
        """
        self.run_cli("new", "002")
        self.seed_credentials("002")
        self.seed_usage("002", five=7, seven=42, age_minutes=20)    # 快照：20 分钟前，太旧
        cache = self.acct_dir("002").parent / "_usage-cache.json"
        cache.write_text(json.dumps({"002": {"windows": {
            "five_hour": {"label": "5 小时", "percent": 60, "resets_at": None, "reset_passed": False},
            "seven_day": {"label": "7 天", "percent": 55, "resets_at": None, "reset_passed": False},
        }, "fetched_at": time.time() - 3600}}), encoding="utf-8")   # 缓存：一小时前
        fixture = self.usage_live_fixture({"002": (99, 98)})
        r = self.run_cli("ls", CCA_USAGE_LIVE_FILE=str(fixture))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("99%", r.stdout)          # 用了实时值
        self.assertIn("98%", r.stdout)

    def test_ls_prefers_newer_snapshot_over_usage_cache(self):
        """缓存比 claude 的本地快照旧时，用快照 —— 并给这格标 `~`。

        真机：005 的实时接口 403，面板端着 4 分钟前的缓存（79%），
        而 claude 自己的快照已经写着 7 天 100% —— 和 CLI 里看到的不一致。
        """
        self.run_cli("new", "002")
        self.seed_credentials("002")
        self.seed_usage("002", five=10, seven=100, age_minutes=0)     # 快照：刚刷过（更新）
        fixture = self.usage_live_fixture({"002": (65, 79)})
        cache = self.acct_dir("002").parent / "_usage-cache.json"
        cache.write_text(json.dumps({"002": {"windows": {
            "five_hour": {"label": "5 小时", "percent": 65, "resets_at": None, "reset_passed": False},
            "seven_day": {"label": "7 天", "percent": 79, "resets_at": None, "reset_passed": False},
        }, "fetched_at": time.time() - 60}}), encoding="utf-8")        # 缓存：1 分钟前（新鲜，但比快照旧）
        r = self.run_cli("ls", CCA_USAGE_LIVE_FILE=str(fixture))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("100%", r.stdout)          # 用快照（更新）
        self.assertNotIn("79%", r.stdout)        # `~` 的规则见 test_ls_tilde_* 两条

    def test_ls_reports_live_usage_failure_reason(self):
        """实时取额度失败时，表尾要说清是哪个号、为什么（否则面板静默端着旧数字）。"""
        self.run_cli("new", "002")
        self.seed_credentials("002")
        self.seed_usage("002", five=10, seven=20)
        r = self.run_cli("ls", CCA_USAGE_LIVE_FILE=str(self.home / "nope.json"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("实时额度没取到", r.stdout)
        self.assertIn("002", r.stdout)

    def test_ls_prefers_live_usage_and_writes_cache(self):
        """额度默认实时取：live 值盖过快照，并把结果写进缓存。"""
        self.run_cli("new", "002")
        self.seed_credentials("002")
        self.seed_usage("002", five=88, seven=77, age_minutes=60)   # 快照旧了，才需要实时取
        fixture = self.usage_live_fixture({"002": (12, 34)})
        r = self.run_cli("ls", CCA_USAGE_LIVE_FILE=str(fixture))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("12%", r.stdout)
        self.assertIn("34%", r.stdout)
        self.assertNotIn("88%", r.stdout)
        cache = json.loads((self.acct_dir("002").parent / "_usage-cache.json").read_text(encoding="utf-8"))
        self.assertIn("002", cache)

    def test_ls_uses_usage_cache_within_ttl_and_refresh_forces(self):
        """TTL 内直接用缓存（不再请求）；`--refresh` 强制拿最新的。"""
        self.run_cli("new", "002")
        self.seed_credentials("002")
        self.seed_usage("002", five=88, seven=77, age_minutes=60)   # 快照旧了，才需要实时取
        fixture = self.usage_live_fixture({"002": (12, 34)})
        self.run_cli("ls", CCA_USAGE_LIVE_FILE=str(fixture))
        self.usage_live_fixture({"002": (99, 98)})        # 数值变了，但缓存还新鲜
        r = self.run_cli("ls", CCA_USAGE_LIVE_FILE=str(fixture))
        self.assertIn("12%", r.stdout)
        self.assertNotIn("99%", r.stdout)
        r = self.run_cli("ls", "--refresh", CCA_USAGE_LIVE_FILE=str(fixture))
        self.assertIn("99%", r.stdout)

    def test_ls_cached_flag_skips_network(self):
        """`--cached`：只看 claude 的本地快照，零网络。"""
        self.run_cli("new", "002")
        self.seed_credentials("002")
        self.seed_usage("002", five=88, seven=77)
        fixture = self.usage_live_fixture({"002": (12, 34)})
        r = self.run_cli("ls", "--cached", CCA_USAGE_LIVE_FILE=str(fixture))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("88%", r.stdout)
        self.assertNotIn("12%", r.stdout)
        self.assertFalse((self.acct_dir("002").parent / "_usage-cache.json").is_file())

    def test_ls_falls_back_to_snapshot_when_live_fails(self):
        """实时取数失败一律降级到快照 —— `cca ls` 不许因此报错或卡住。"""
        self.run_cli("new", "002")
        self.seed_credentials("002")
        self.seed_usage("002", five=88, seven=77)
        r = self.run_cli("ls", CCA_USAGE_LIVE_FILE=str(self.home / "nope.json"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("88%", r.stdout)

    def subscription_live_fixture(self, values):
        """`{号: 订阅字段}` → 实时订阅的桩文件（CCA_SUBSCRIPTION_LIVE_FILE）。

        `{"error": "restricted"}` 这类标记原样保留，用来模拟平台判定。
        """
        path = self.home / "subscription-live.json"
        payload = {name: (value if "error" in value else {"organization": value})
                   for name, value in values.items()}
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_ls_subscription_local_fallback(self):
        """取不到实时数据时，套餐和计费渠道仍要有：凭据 + .claude.json 就够了。"""
        self.run_cli("new", "002")
        self.seed_credentials("002")
        (self.acct_dir("002") / ".claude.json").write_text(json.dumps({"oauthAccount": {
            "emailAddress": "c@example.com", "billingType": "stripe_subscription"}}), encoding="utf-8")
        creds = json.loads((self.acct_dir("002") / ".credentials.json").read_text(encoding="utf-8"))
        creds["claudeAiOauth"]["subscriptionType"] = "max"
        (self.acct_dir("002") / ".credentials.json").write_text(json.dumps(creds), encoding="utf-8")
        r = self.run_cli("ls", "--json",
                         CCA_SUBSCRIPTION_LIVE_FILE=str(self.home / "nope.json"),
                         CCA_USAGE_LIVE_FILE=str(self.usage_live_fixture({"002": (1, 2)})))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        sub = next(x for x in json.loads(r.stdout)["profiles"] if x["profile"] == "002")["subscription"]
        self.assertEqual(sub["type"], "max")
        self.assertEqual(sub["source"], "local")

    def test_ls_subscription_live_merged_into_wide_column(self):
        """实时值进来后，宽表一眼看到 套餐·渠道·状态·开始日。"""
        self.run_cli("new", "002")
        self.seed_credentials("002")
        fixture = self.subscription_live_fixture({"002": {
            "organization_type": "claude_max",
            "billing_type": "apple_subscription",
            "subscription_status": "canceled",
            "subscription_created_at": "2026-09-23T10:42:31Z",
        }})
        r = self.run_cli("ls", "--wide",
                         CCA_SUBSCRIPTION_LIVE_FILE=str(fixture),
                         CCA_USAGE_LIVE_FILE=str(self.usage_live_fixture({"002": (1, 2)})))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # 计费和首订日已去掉：只剩档位和订阅状态
        self.assertIn("max 5x", r.stdout)
        self.assertIn("已取消", r.stdout)
        self.assertNotIn("计费", r.stdout)

    def test_subscription_with_null_status_is_not_discarded(self):
        """正常在用的订阅 `subscription_status` 是 null —— 不能因此把整条响应丢掉。"""
        self.run_cli("new", "002")
        self.seed_credentials("002")
        fixture = self.subscription_live_fixture({"002": {
            "organization_type": "claude_max",
            "billing_type": "apple_subscription",
            "subscription_status": None,
            "subscription_created_at": "2026-10-16T06:19:30Z",
        }})
        r = self.run_cli("ls", "--json",
                         CCA_SUBSCRIPTION_LIVE_FILE=str(fixture),
                         CCA_USAGE_LIVE_FILE=str(self.usage_live_fixture({"002": (1, 2)})))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        sub = next(x for x in json.loads(r.stdout)["profiles"] if x["profile"] == "002")["subscription"]
        self.assertEqual(sub["source"], "live")
        self.assertEqual(sub["type"], "claude_max")

    def test_status_shows_subscription_state(self):
        """`cca status` 要有订阅状态行；首订日已经去掉。"""
        self.run_cli("new", "002")
        self.seed_credentials("002")
        fixture = self.subscription_live_fixture({"002": {
            "organization_type": "claude_max",
            "billing_type": "apple_subscription",
            "subscription_status": "active",
            "subscription_created_at": "2026-09-23T10:42:31Z",
        }})
        r = self.run_cli("status", "002", CCA_SUBSCRIPTION_LIVE_FILE=str(fixture))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("订阅", r.stdout)
        self.assertNotIn("首订日", r.stdout)

    def test_ls_shows_current_time(self):
        """表头要有"现在几点"—— 重置时刻是绝对时间，没有当前时间就得自己做减法。"""
        self.run_cli("new", "002")
        r = self.run_cli("ls")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"现在 \d{2}-\d{2} \d{2}:\d{2}")
        payload = json.loads(self.run_cli("ls", "--json").stdout)
        self.assertRegex(payload["now"], r"^\d{2}-\d{2} \d{2}:\d{2}$")

    def test_ls_wide_adds_columns(self):
        """`--wide` 的列一个都不能少（曾因缩进把 wide 单元格塞进了别的分支）。"""
        self.run_cli("new", "002")
        r = self.run_cli("ls", "--wide")
        self.assertEqual(r.returncode, 0, r.stderr)
        for header in ("PROFILE", "邮箱", "5 小时(已用)", "7 天(已用)", "后台", "档位",
                       "订阅", "登录", "快照", "额外"):
            self.assertIn(header, r.stdout)
        self.assertIn("claude001@example.com", r.stdout)
        # 到期日没有接口可查（anthropics/claude-code#95712）：页脚写明不显示续费日。
        self.assertIn("不显示续费日", r.stdout)
        self.assertNotIn("会员到期", r.stdout)

    def test_remaining_is_strictly_aligned(self):
        """倒计时严格 7 列：单位位置必须一致，扫一列才能比较谁先放开。"""
        cases = [2 * 3600 + 11 * 60, 10 * 3600 + 4 * 60, 3 * 86400 + 3 * 3600, 43 * 60, 30]
        out = [format_remaining(c) for c in cases]
        self.assertEqual({len(v) for v in out}, {7}, out)
        # 第一段的单位固定在第 3 列（数字补到两位），分钟/天同理。
        self.assertEqual({v[2] for v in out[:2]}, {"h"})
        self.assertEqual(out[2][2], "d")

    def test_ls_shows_reset_countdown(self):
        """默认视图给倒计时（比绝对时刻短）；`--wide` 两个都给。"""
        self.run_cli("new", "002")
        self.seed_credentials("002")
        now = datetime.now(UTC)
        path = self.home / "usage-live-countdown.json"
        path.write_text(json.dumps({"002": {
            "five_hour": {"utilization": 40,
                          "resets_at": (now + timedelta(hours=2, minutes=13)).isoformat()},
            "seven_day": {"utilization": 10,
                          "resets_at": (now + timedelta(days=3, hours=4)).isoformat()},
        }}), encoding="utf-8")
        for args in (["ls"], ["ls", "--wide"]):
            r = self.run_cli(*args, CCA_USAGE_LIVE_FILE=str(path))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            # 绝对时刻和倒计时都在；12/13 分之差随执行耗时，正则放宽。
            self.assertRegex(r.stdout, r"40%[~*]? \d{2}-\d{2} \d{2}:\d{2} ·\s+2h 1[23]m")
            self.assertRegex(r.stdout, r"·\s+3d 0[0-9]h")
        # 窄终端放不下两个值，退回只给时刻（不然 Rich 会裁列）。
        narrow = self.run_cli("ls", COLUMNS="60", CCA_USAGE_LIVE_FILE=str(path))
        self.assertEqual(narrow.returncode, 0, narrow.stderr)
        self.assertNotRegex(narrow.stdout, r"\d{2}:\d{2} · ")

    def test_ls_background_column_shows_running_over_total(self):
        """只给"在跑"数会误导：挂起的会话（blocked，不消耗额度）看起来像没有会话。"""
        self.run_cli("new", "002")
        self.seed_daemon_dir("001")
        parked = self.agents_json(("blocked", "等输入"))
        # 后台列只在 --wide 里给，默认视图不再占位
        r = self.run_cli("ls", "--wide", FAKE_CLAUDE_AGENTS=parked)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("0/1", r.stdout)
        both = json.dumps(
            [
                {"id": "00000000", "kind": "background", "name": "等输入", "cwd": "/x",
                 "sessionId": "00000000-0000-0000-0000-000000000000", "state": "blocked"},
                {"id": "00000001", "kind": "background", "name": "在跑", "cwd": "/x",
                 "sessionId": "00000001-0000-0000-0000-000000000000", "state": "working"},
            ]
        )
        r = self.run_cli("ls", "--wide", FAKE_CLAUDE_AGENTS=both)
        self.assertIn("1/2", r.stdout)

    def test_ls_rejects_unknown_flag(self):
        self.run_cli("new", "002")
        r = self.run_cli("ls", "--wdie")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("未知参数 --wdie", r.stderr)
        self.assertIn("--wide", r.stderr)

    def test_ls_fits_narrow_terminal_without_cropping(self):
        """窄终端不许静默裁列：曾经 72~80 列下"登录/后台"整列消失。"""
        self.run_cli("new", "002")
        self.seed_usage("002", five=99, seven=71)
        for columns in ("60", "72", "80", "96", "100", "120"):
            r = self.run_cli("ls", COLUMNS=columns)
            self.assertEqual(r.returncode, 0, r.stderr)
            for header in ("PROFILE", "5 小时(已用)", "7 天(已用)"):
                self.assertIn(header, r.stdout, f"COLUMNS={columns} 下缺列 {header}")
            widest = max(display_width_of(line) for line in r.stdout.splitlines())
            self.assertLessEqual(widest, int(columns), f"COLUMNS={columns} 下表格超出终端宽度")

    def test_follow_plan_only_same_tier_and_forward(self):
        """跟随只认同前缀同档位、版本严格变大的候选；跨档/降级/拆不出结构一律跳过。

        真实场景：`gpt-6-sol` → `gpt-6.1-sol` 该跟；但 `-sol` 绝不能被换成 `-astra`，
        `gpt-6-astra`（端点还没有 6.1 版）也不该动，`weird-name` 拆不出结构更不许猜。
        """
        out = self._run_python(FOLLOW_PROBE)
        self.assertEqual(out.returncode, 0, out.stderr)
        plan = {}
        for line in out.stdout.splitlines():
            if line.startswith("plan "):
                _, key, rest = line.split(" ", 2)
                plan[key] = rest
        self.assertEqual(plan["opus"], "gpt-6-sol -> gpt-6.1-sol")     # 同档位升级 ✓
        self.assertEqual(plan["sonnet"], "gpt-5.6-sol -> gpt-6.1-sol")  # 同档位、版本更高 ✓
        self.assertNotIn("main", plan)                                  # astra 没有更新版，不动 ✓
        self.assertNotIn("sub", plan)                                   # 已是最新 ✓
        self.assertNotIn("haiku", plan)                                 # 没有 gpt-6.1-luna ✓
        self.assertNotIn("broken", plan)                                # 拆不出结构，不许猜 ✓
        self.assertIn("'gpt-6.1-sol'", out.stdout)

    def test_models_command_lists_and_marks_configured(self):
        """`cca models <号>`：探端点清单，并标出该号现在配的是哪几个。"""
        self.seed_api_profile("ds", env={"ANTHROPIC_MODEL": "gpt-6-astra",
                                         "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-v4-flash"})
        catalog = self.home / "models.json"
        catalog.write_text(json.dumps({"data": [{"id": "gpt-6-astra"}, {"id": "gpt-6.1-sol"},
                                                {"id": "deepseek-v4-flash"}]}), encoding="utf-8")
        r = self.run_cli("models", "ds", CCA_MODELS_FILE=str(catalog))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("gpt-6.1-sol", r.stdout)
        self.assertIn("gpt-6-astra  ← 该号在用", r.stdout)
        self.assertIn("deepseek-v4-flash  ← 该号在用", r.stdout)

    def test_model_follow_dry_run_then_write(self):
        """`--follow`：先给计划（dry-run 不写），去掉后按计划写进该号设置。"""
        self.seed_api_profile("ds", env={"ANTHROPIC_MODEL": "gpt-6-astra",
                                         "ANTHROPIC_DEFAULT_OPUS_MODEL": "gpt-6-sol",
                                         "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-v4-flash"})
        catalog = self.home / "models.json"
        catalog.write_text(json.dumps({"data": [{"id": "gpt-6-astra"}, {"id": "gpt-6.1-sol"},
                                                {"id": "deepseek-v4-flash"}]}), encoding="utf-8")
        before = (self.acct_dir("ds") / "settings.json").read_text(encoding="utf-8")
        r = self.run_cli("model", "ds", "--follow", "--dry-run", CCA_MODELS_FILE=str(catalog))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("gpt-6-sol", r.stdout)
        self.assertIn("gpt-6.1-sol", r.stdout)
        self.assertIn("--dry-run：没有写入", r.stdout)
        self.assertEqual(before, (self.acct_dir("ds") / "settings.json").read_text(encoding="utf-8"))
        r = self.run_cli("model", "ds", "--follow", CCA_MODELS_FILE=str(catalog))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("重启会话才生效", r.stdout)
        env = json.loads((self.acct_dir("ds") / "settings.json").read_text(encoding="utf-8"))["env"]
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "gpt-6.1-sol")
        self.assertEqual(env["ANTHROPIC_MODEL"], "gpt-6-astra")        # 没有更新版就不动 ✓
        self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL"], "deepseek-v4-flash")

    def test_model_follow_reports_when_up_to_date(self):
        """已经跟得上时说清"已是最新"，而不是打一张空表。"""
        self.seed_api_profile("ds", env={"ANTHROPIC_MODEL": "gpt-6-astra"})
        catalog = self.home / "models.json"
        catalog.write_text(json.dumps({"data": [{"id": "gpt-6-astra"}]}), encoding="utf-8")
        r = self.run_cli("model", "ds", "--follow", CCA_MODELS_FILE=str(catalog))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("已是最新", r.stdout)

    def test_ls_says_usage_is_used_not_remaining(self):
        """表头/表尾必须写明百分比是"已用"。

        背景：把"已用 5%"读成"只剩 5%"是真实发生过的误读（表头只写"5 小时"太含糊）。
        """
        self.run_cli("new", "002")
        self.seed_usage("002", five=12, seven=29)
        r = self.run_cli("ls")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("5 小时(已用)", r.stdout)
        self.assertIn("7 天(已用)", r.stdout)
        self.assertIn("表内百分比 = 已用，不是剩余".replace("表内", "表中"), r.stdout)

    def test_usage_command_is_gone(self):
        """额度并入 `cca ls`；`cca usage` 不再当子命令，只可能是 profile 名。"""
        r = self.run_cli("usage")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("profile usage 不存在", r.stderr)

    # -- 单号详情（status）------------------------------------------------

    def test_status_shows_background_session_details(self):
        self.run_cli("new", "002")
        self.seed_daemon_dir("001")
        payload = json.dumps(
            [{"id": "abcde123", "kind": "background", "name": "重建-canary", "cwd": "/x/y",
              "sessionId": "abcde123-0000-0000-0000-000000000000", "state": "blocked"}]
        )
        r = self.run_cli("status", "001", FAKE_CLAUDE_AGENTS=payload)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("abcde123", r.stdout)
        self.assertIn("重建-canary", r.stdout)
        self.assertIn("/x/y", r.stdout)
        self.assertIn("blocked", r.stdout)

    def test_status_shows_credential_expiry_without_leaking_token(self):
        """到期时间只取两个数字字段：token 本身既不能进输出，也不参与解析。"""
        self.run_cli("new", "002")
        secret = "sk-ant-oat01-SUPERSECRET-TOKEN-VALUE"  # noqa: S105 - 测试用的假密钥
        access = int((time.time() + 3600) * 1000)
        refresh = int((time.time() + 30 * 86400) * 1000)
        (self.acct_dir("002") / ".credentials.json").write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": secret,
                        "refreshToken": secret + "-refresh",
                        "expiresAt": access,
                        "refreshTokenExpiresAt": refresh,
                        "scopes": ["user:inference"],
                    }
                }
            ),
            encoding="utf-8",
        )
        r = self.run_cli("status", "002")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("凭据", r.stdout)
        self.assertIn("重新登录", r.stdout)
        expected_access = time.strftime("%m-%d %H:%M", time.localtime(access / 1000))
        expected_refresh = time.strftime("%m-%d %H:%M", time.localtime(refresh / 1000))
        self.assertIn(expected_access, r.stdout)
        self.assertIn(expected_refresh, r.stdout)
        self.assertNotIn(secret, r.stdout + r.stderr)
        self.assertNotIn("refresh", r.stdout.replace("刷新令牌", ""))

    def test_status_flags_expired_credential(self):
        self.run_cli("new", "002")
        stale = int((time.time() - 86400) * 1000)
        (self.acct_dir("002") / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "x", "expiresAt": stale}}), encoding="utf-8"
        )
        r = self.run_cli("status", "002")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("已过期", r.stdout)
        self.assertIn("自动续", r.stdout)

    def test_status_includes_quota_row(self):
        self.run_cli("new", "002")
        self.seed_usage("002", five=42, seven=13)
        r = self.run_cli("status", "002")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("额度", r.stdout)
        self.assertIn("5 小时 42%", r.stdout)


    def test_handoff_rejects_typo_flag(self):
        """`--olny` 这种拼错必须报错：静默忽略会让人以为只搬了这一条。"""
        self.run_cli("new", "002")
        r = self.run_cli("handoff", "001", "002", "--olny=abcde123")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("未知参数 --olny", r.stderr)
        self.assertIn("--only", r.stderr)
        self.assertEqual([c for c in self.calls() if c.startswith(("stop ", "--bg"))], [])


    def test_handoff_skips_interactive_sessions(self):
        """交互会话不进接管候选：`--bg --resume` 只会 fork 一份副本。"""
        self.run_cli("new", "002")
        long_id = self.seed_carriable("001", "abcde123", state="blocked", name="job")
        payload = json.dumps(
            [
                {"id": "abcde123", "kind": "background", "name": "job", "cwd": str(self.home),
                 "sessionId": long_id, "state": "blocked"},
                {"id": None, "kind": "interactive", "name": "ham-60", "cwd": str(self.home),
                 "sessionId": "076d0ea8-0000-0000-0000-000000000000", "state": None},
            ]
        )
        r = self.run_cli("handoff", "001", "002", "--all", "--dry-run", FAKE_CLAUDE_AGENTS=payload)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("abcde123", r.stdout)
        self.assertNotIn("ham-60", r.stdout)






    def test_doctor_flags_stale_background_session(self):
        """清单里进程已退出的会话要提示 rm，但不自动删；在跑的不查。"""
        self.run_cli("new", "002")
        self.seed_daemon_dir("001")
        payload = self.agents_json(("blocked", "awaits"), ("busy", "running"))
        r = self.run_cli("doctor", FAKE_CLAUDE_AGENTS=payload, FAKE_CLAUDE_STALE_IDS="00000000")
        self.assertIn("已退出但仍在后台会话清单里", r.stdout)
        self.assertIn("rm 00000000", r.stdout)
        self.assertNotIn("rm 00000001", r.stdout)

    def test_doctor_does_not_flag_live_sessions(self):
        self.run_cli("new", "002")
        self.seed_daemon_dir("001")
        payload = self.agents_json(("blocked", "awaits"))
        r = self.run_cli("doctor", FAKE_CLAUDE_AGENTS=payload)
        self.assertNotIn("已退出但仍在后台会话清单里", r.stdout)

    # -- 停后台会话 ------------------------------------------------------
    def test_stop_bg_stops_parked_and_skips_running(self):
        self.run_cli("new", "002")
        self.seed_daemon_dir("001")
        payload = self.agents_json(("blocked", "awaits"), ("done", "finished"), ("busy", "running"))
        r = self.run_cli("stop-bg", "001", FAKE_CLAUDE_AGENTS=payload)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        stops = [c for c in self.calls() if c.startswith("stop ")]
        self.assertIn("stop 00000000", stops)
        self.assertIn("stop 00000001", stops)
        self.assertNotIn("stop 00000002", stops)
        self.assertIn("跳过 1 个正在跑的会话", r.stderr)

    def test_stop_bg_force_stops_running(self):
        self.run_cli("new", "002")
        self.seed_daemon_dir("001")
        r = self.run_cli("stop-bg", "001", "--force", FAKE_CLAUDE_AGENTS=self.agents_json(("busy", "running")))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("stop 00000000", self.calls())

    def test_stop_bg_without_sessions_is_noop(self):
        self.run_cli("new", "002")
        r = self.run_cli("stop-bg", "001")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("没有后台会话", r.stdout)
        self.assertEqual([c for c in self.calls() if c.startswith("stop ")], [])

    def test_stop_bg_unknown_profile_fails(self):
        r = self.run_cli("stop-bg", "009")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("不存在", r.stderr)

    def test_stop_bg_skips_interactive_entries(self):
        """`claude agents --json` 里的交互会话没有 job id，stop 只会回 No job matching。"""
        self.run_cli("new", "002")
        self.seed_daemon_dir("001")
        payload = json.dumps(
            [
                # 前台交互会话：id / state 都是 null，只有 sessionId。
                {"id": None, "kind": "interactive", "name": "ham-60", "cwd": "/x",
                 "sessionId": "076d0ea8-0000-0000-0000-000000000000", "state": None},
                {"id": "7232a555", "kind": "background", "name": "ham-f3", "cwd": "/x",
                 "sessionId": "7232a555-0000-0000-0000-000000000000", "state": "done"},
            ]
        )
        r = self.run_cli("stop-bg", "001", "--force", FAKE_CLAUDE_AGENTS=payload)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([c for c in self.calls() if c.startswith("stop ")], ["stop 7232a555"])
        self.assertIn("非后台任务", r.stderr)
        self.assertIn("ham-60", r.stderr)

    def test_stop_bg_only_interactive_entries_is_noop(self):
        self.run_cli("new", "002")
        self.seed_daemon_dir("001")
        payload = json.dumps(
            [{"id": None, "kind": "interactive", "name": "ham-60", "cwd": "/x",
              "sessionId": "076d0ea8-0000-0000-0000-000000000000", "state": None}]
        )
        r = self.run_cli("stop-bg", "001", "--force", FAKE_CLAUDE_AGENTS=payload)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([c for c in self.calls() if c.startswith("stop ")], [])
        self.assertIn("没有可停的后台会话", r.stdout)
        self.assertIn("非后台任务", r.stderr)

    def test_stop_bg_rejects_unknown_flag(self):
        self.run_cli("new", "002")
        r = self.run_cli("stop-bg", "001", "--froce")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("未知参数 --froce", r.stderr)
        self.assertIn("--force", r.stderr)
        self.assertEqual([c for c in self.calls() if c.startswith("stop ")], [])

    def test_stop_bg_uses_roster_worker_key_as_job_id(self):
        """回退判据里 roster 的 dict key 才是 job id（sessionId 是另一个字段）。"""
        self.run_cli("new", "002")
        proc = subprocess.Popen(["sleep", "30"])
        try:
            self.seed_roster("001", proc.pid)
            r = self.run_cli("stop-bg", "001", "--force")
        finally:
            proc.terminate()
            proc.wait()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([c for c in self.calls() if c.startswith("stop ")], ["stop w1"])

    def test_stop_bg_all_covers_every_profile(self):
        """--all：每个有后台会话的号都停（换 workspace 前用）。"""
        self.run_cli("new", "002")
        self.seed_daemon_dir("001")
        self.seed_daemon_dir("002")
        payload = self.agents_json(("blocked", "awaits"), ("busy", "running"))
        r = self.run_cli("stop-bg", "--all", FAKE_CLAUDE_AGENTS=payload)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        stops = [c for c in self.calls() if c.startswith("stop ")]
        self.assertEqual(stops, ["stop 00000000", "stop 00000000"])   # 两个号各一条，跑着的跳过
        self.assertIn("001: 已停止", r.stdout)
        self.assertIn("002: 已停止", r.stdout)
        recorded = json.loads((self.home / ".cca" / "stopped-bg.json").read_text(encoding="utf-8"))
        self.assertEqual(set(recorded), {"00000000-0000-0000-0000-000000000000"})   # 跑着没停的不记
        self.assertIn("cca resume-bg", r.stdout)

    def test_stop_bg_all_stops_own_session_last(self):
        """在会话里敲：本会话不在中途停（否则命令跟着断），放到最后。"""
        self.run_cli("new", "002")
        self.seed_daemon_dir("001")
        self.seed_daemon_dir("002")
        payload = self.agents_json(("busy", "me"), ("busy", "other"))
        r = self.run_cli("stop-bg", "--all", "--force", FAKE_CLAUDE_AGENTS=payload,
                         CLAUDE_CODE_SESSION_ID="00000000-0000-0000-0000-000000000000",
                         CLAUDE_CONFIG_DIR=str(self.acct_dir("002")))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        stops = [c for c in self.calls() if c.startswith("stop ")]
        self.assertEqual(stops, ["stop 00000001", "stop 00000001", "stop 00000000"])
        self.assertIn("最后停本会话 002 00000000", r.stdout)
        recorded = json.loads((self.home / ".cca" / "stopped-bg.json").read_text(encoding="utf-8"))
        self.assertEqual(recorded["00000000-0000-0000-0000-000000000000"]["account"], "002")  # 本会话停前先记
        self.assertEqual(recorded["00000000-0000-0000-0000-000000000000"]["state"], "busy")

    def test_stop_bg_all_without_force_leaves_own_session(self):
        self.run_cli("new", "002")
        self.seed_daemon_dir("002")
        payload = self.agents_json(("busy", "me"))
        r = self.run_cli("stop-bg", "--all", FAKE_CLAUDE_AGENTS=payload,
                         CLAUDE_CODE_SESSION_ID="00000000-0000-0000-0000-000000000000",
                         CLAUDE_CONFIG_DIR=str(self.acct_dir("002")))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([c for c in self.calls() if c.startswith("stop ")], [])
        self.assertIn("本会话 002 00000000 正在跑，没停", r.stderr)

    def test_stop_bg_all_rejects_profile_name(self):
        r = self.run_cli("stop-bg", "001", "--all")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--all 不跟号名一起用", r.stderr)

    # -- 删号（cca rm）--------------------------------------------------
    def test_rm_requires_typed_confirmation(self):
        self.run_cli("new", "002")
        r = self.run_cli("rm", "002", stdin_data="yes\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.acct_dir("002").exists())
        self.assertIn("已取消", r.stdout)

    def test_rm_deletes_after_confirmation(self):
        self.run_cli("new", "002")
        r = self.run_cli("rm", "002", stdin_data="002\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.acct_dir("002").exists())
        self.assertIn("已删除", r.stdout)
        self.assertIn("共享池", r.stdout)

    def test_rm_yes_skips_confirmation(self):
        self.run_cli("new", "002")
        r = self.run_cli("rm", "002", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.acct_dir("002").exists())

    def test_rm_without_stdin_needs_yes(self):
        self.run_cli("new", "002")
        r = self.run_cli("rm", "002", stdin_data="")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--yes", r.stderr)
        self.assertTrue(self.acct_dir("002").exists())

    def test_rm_refuses_when_shared_entry_is_real_dir(self):
        """共享项被 CLI 换成实体目录时删号会连累共享池 —— 必须拒绝。"""
        self.run_cli("new", "002")
        shared = self.acct_dir("002") / "projects"
        shared.unlink()
        shared.mkdir()
        (shared / "keep.jsonl").write_text("{}", encoding="utf-8")
        r = self.run_cli("rm", "002", "--yes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("共享", r.stderr)
        self.assertTrue((shared / "keep.jsonl").exists())

    def test_rm_keeps_transcripts_in_shared_pool(self):
        self.run_cli("new", "002")
        long_id = "abcdefgh-0000-0000-0000-000000000000"
        self.seed_transcript("002", long_id, title="t", cwd=str(self.home))
        r = self.run_cli("rm", "002", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        kept = [p for p in (self.home / ".claude" / "projects").rglob("*.jsonl") if p.stem == long_id]
        self.assertEqual(len(kept), 1, "对话记录不该被删号带走")


    def test_rm_refuses_running_session_without_force(self):
        self.run_cli("new", "002")
        self.seed_daemon_dir("002")
        r = self.run_cli("rm", "002", "--yes", FAKE_CLAUDE_AGENTS=self.agents_json(("working", "在跑")))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("正在跑", r.stderr)
        self.assertTrue(self.acct_dir("002").exists())

    def test_rm_force_stops_then_deletes(self):
        self.run_cli("new", "002")
        self.seed_daemon_dir("002")
        r = self.run_cli("rm", "002", "--yes", "--force", FAKE_CLAUDE_AGENTS=self.agents_json(("working", "在跑")))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("stop 00000000", self.calls())
        self.assertFalse(self.acct_dir("002").exists())

    def test_rm_refuses_interactive_session_without_force(self):
        self.run_cli("new", "002")
        self.seed_daemon_dir("002")
        payload = json.dumps(
            [{"id": None, "kind": "interactive", "name": "repair", "cwd": "/x",
              "sessionId": "076d0ea8-0000-0000-0000-000000000000", "state": None}]
        )
        r = self.run_cli("rm", "002", "--yes", FAKE_CLAUDE_AGENTS=payload)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("交互会话", r.stderr)
        self.assertTrue(self.acct_dir("002").exists())

    def test_rm_is_idempotent_for_skeleton_dir(self):
        """daemon 尾巴可能在删号后重建一个只有 telemetry 的空目录：再跑一次 rm 就能清掉。"""
        self.run_cli("new", "002")
        self.run_cli("rm", "002", "--yes")
        skeleton = self.acct_dir("002") / "telemetry"
        skeleton.mkdir(parents=True)
        (skeleton / "x.json").write_text("{}", encoding="utf-8")
        r = self.run_cli("rm", "002", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.acct_dir("002").exists())

    def test_rm_notes_daemon_tail(self):
        self.run_cli("new", "002")
        self.seed_daemon_dir("002")
        r = self.run_cli("rm", "002", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("telemetry", r.stderr)

    def test_rm_unknown_profile_fails(self):
        r = self.run_cli("rm", "009", "--yes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("不存在", r.stderr)

    # -- 首次引导标记 ----------------------------------------------------
    def test_run_seeds_onboarding_flag(self):
        """新号第一次交互启动会走引导（甚至再要一次登录），启动前补上标记。"""
        self.run_cli("new", "002")
        cfg = self.acct_dir("002") / ".claude.json"
        cfg.write_text(
            json.dumps({"oauthAccount": {"emailAddress": "x@example.com"}, "numStartups": 1}), encoding="utf-8"
        )
        r = self.run_cli("002")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(cfg.read_text(encoding="utf-8"))
        self.assertIs(data["hasCompletedOnboarding"], True)  # noqa: FBT003 - unittest 断言只接受位置参数
        self.assertEqual(data["oauthAccount"]["emailAddress"], "x@example.com")  # 不碰账号字段
        self.assertIn("hasCompletedOnboarding", r.stderr)

    def test_run_leaves_existing_onboarding_flag_alone(self):
        self.run_cli("new", "002")
        cfg = self.acct_dir("002") / ".claude.json"
        cfg.write_text(json.dumps({"hasCompletedOnboarding": True, "numStartups": 7}), encoding="utf-8")
        before = cfg.read_text(encoding="utf-8")
        self.run_cli("002")
        self.assertEqual(cfg.read_text(encoding="utf-8"), before)

    def test_run_does_not_create_config_for_fresh_profile(self):
        """没跑过 claude 的配置目录不该被凭空造出 .claude.json。"""
        self.run_cli("new", "002")
        cfg = self.acct_dir("002") / ".claude.json"
        self.assertFalse(cfg.exists())
        self.run_cli("002")
        self.assertFalse(cfg.exists())

    # -- 可续会话清单 ----------------------------------------------------
    def test_sessions_lists_shared_pool_across_dirs(self):
        self.run_cli("new", "002")
        self.seed_transcript(
            "001", "aaaa1111-2222-3333-4444-555566667777",
            ai="自动标题", title="手设名字", prompt="继续做X", cwd="/proj/x",
        )
        r = self.run_cli("sessions")
        self.assertEqual(r.returncode, 0, r.stderr)
        for expect in ("aaaa1111", "手设名字", "继续做X", "/proj/x"):
            self.assertIn(expect, r.stdout)

    def test_sessions_falls_back_to_ai_title(self):
        self.run_cli("new", "002")
        self.seed_transcript("001", "bbbb2222-0000-0000-0000-000000000000", ai="AI 起的标题")
        self.assertIn("AI 起的标题", self.run_cli("sessions").stdout)

    def test_sessions_marks_live_owner(self):
        self.run_cli("new", "002")
        sid = "cccc3333-0000-0000-0000-000000000000"
        self.seed_transcript("001", sid, title="x")
        self.seed_daemon_dir("001")
        payload = json.dumps(
            [{"id": "cccc3333", "kind": "background", "name": "x", "cwd": "/x", "sessionId": sid, "state": "busy"}]
        )
        self.assertIn("001 busy", self.run_cli("sessions", FAKE_CLAUDE_AGENTS=payload).stdout)

    def test_sessions_window_and_all(self):
        self.run_cli("new", "002")
        self.seed_transcript("001", "dddd4444-0000-0000-0000-000000000000", title="新", age_hours=1)
        self.seed_transcript("001", "eeee5555-0000-0000-0000-000000000000", title="旧", age_hours=24 * 30)
        default_out = self.run_cli("sessions").stdout
        self.assertIn("dddd4444", default_out)
        self.assertNotIn("eeee5555", default_out)
        self.assertIn("eeee5555", self.run_cli("sessions", "--all").stdout)

    def test_sessions_caps_rows_per_project_but_counts_all(self):
        """每个项目默认只列最近 15 条，写明还有多少；--all 全列（总数照实报）。"""
        self.run_cli("new", "002")
        for i in range(25):
            self.seed_transcript(
                "001", f"{i:08d}-0000-0000-0000-000000000000", title=f"活{i}", age_hours=i + 1
            )
        r = self.run_cli("sessions")
        self.assertEqual(r.returncode, 0, r.stderr)
        for i in range(15):
            self.assertIn(f"{i:08d}", r.stdout, f"最近的第 {i} 条应出现")
        self.assertNotIn("00000024", r.stdout)          # 第 25 条太旧，默认不列
        self.assertIn("另有 10 条：cca sessions --all", r.stdout)
        self.assertIn("共 25 条（最近 14 天）", r.stdout)
        wide = self.run_cli("sessions", "--all").stdout
        for i in range(25):
            self.assertIn(f"{i:08d}", wide, f"第 {i} 条应出现")

    def test_sessions_footer_counts_older_ones(self):
        """表尾给真实总数，并说清窗口外还有多少条、怎么看到 —— 不再把截断当"全部"。"""
        self.run_cli("new", "002")
        self.seed_transcript("001", "aaaa1111-0000-0000-0000-000000000000", title="新", age_hours=1)
        self.seed_transcript("001", "bbbb2222-0000-0000-0000-000000000000", title="也新", age_hours=2)
        self.seed_transcript("001", "cccc3333-0000-0000-0000-000000000000", title="很旧", age_hours=24 * 40)
        default_out = self.run_cli("sessions").stdout
        self.assertIn("共 2 条（最近 14 天）", default_out)
        self.assertIn("另有 1 条更早的：cca sessions --all", default_out)
        self.assertNotIn("cccc3333", default_out)
        all_out = self.run_cli("sessions", "--all").stdout
        self.assertIn("共 3 条（全部）", all_out)
        self.assertIn("cccc3333", all_out)
        self.assertNotIn("另有", all_out)

    def test_sessions_json_reports_total_and_older(self):
        self.run_cli("new", "002")
        self.seed_transcript("001", "aaaa1111-0000-0000-0000-000000000000", title="新", age_hours=1)
        self.seed_transcript("001", "cccc3333-0000-0000-0000-000000000000", title="很旧", age_hours=24 * 40)
        payload = json.loads(self.run_cli("sessions", "--json").stdout)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["older_not_shown"], 1)
        self.assertEqual(len(payload["sessions"]), 1)

    def seed_big_transcript(self, session="ffff9999-0000-0000-0000-000000000000", *,
                            turns=40, chunk=4000, project="-proj-x"):
        """造一条"超限"会话：每轮 user/assistant 各一段大文本，末尾带真实 usage。

        usage 是裁剪时估算 token 的依据（input + cache_read + cache_creation ≈ 当时的上下文）。
        """
        d = self.profile_base("001") / "projects" / project
        d.mkdir(parents=True, exist_ok=True)
        lines = []
        for i in range(turns):
            body = f"第{i}轮 " + ("内容" * (chunk // 2))
            lines.append({"type": "user", "sessionId": session, "cwd": "/proj/x",
                          "timestamp": f"2026-09-28T00:{i:02d}:00Z",
                          "message": {"role": "user", "content": [{"type": "text", "text": body}]}})
            lines.append({"type": "assistant", "sessionId": session,
                          "timestamp": f"2026-09-28T00:{i:02d}:30Z",
                          "message": {"role": "assistant", "model": "claude-sonnet-5",
                                      "content": [{"type": "text", "text": body}],
                                      "usage": {"input_tokens": (i + 1) * 1000,
                                                "cache_read_input_tokens": 0,
                                                "cache_creation_input_tokens": 0,
                                                "output_tokens": 10}}})
        f = d / f"{session}.jsonl"
        f.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
        return f

    def test_trim_backs_up_and_shrinks(self):
        """裁剪：先备份、再按干净边界切、打印前后估算 —— 备份必须与原文件逐字节相同。"""
        f = self.seed_big_transcript()
        original = f.read_bytes()
        r = self.run_cli("trim", "ffff9999", "--keep=20000")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        backup = self.home / ".claude-accounts" / "_session-backups" / f.name
        self.assertTrue(backup.is_file(), "要先备份")
        self.assertEqual(backup.read_bytes(), original, "备份要与原文件一致")
        self.assertLess(f.stat().st_size, len(original), "原文件要变小")
        self.assertIn("备份", r.stdout)
        self.assertIn("裁后:", r.stdout)
        # 切点是干净的 user 边界（不是 tool_result 回填）
        first = json.loads(f.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(first["type"], "user")
        self.assertNotIn("tool_result", json.dumps(first))

    def test_trim_dry_run_touches_nothing(self):
        f = self.seed_big_transcript()
        original = f.read_bytes()
        r = self.run_cli("trim", "ffff9999", "--keep=20000", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("--dry-run", r.stdout)
        self.assertEqual(f.read_bytes(), original)
        self.assertFalse((self.home / ".claude-accounts" / "_session-backups").exists())

    def test_trim_refuses_running_session(self):
        """进程还在写这个文件，裁了会被它覆盖 —— 必须先停。"""
        self.seed_big_transcript(session="abcde123-0000-0000-0000-000000000000")
        self.seed_daemon_dir("001")
        payload = self.agents_payload("abcde123", "abcde123-0000-0000-0000-000000000000", state="working")
        r = self.run_cli("trim", "abcde123", "--keep=20000", FAKE_CLAUDE_AGENTS=payload)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("正在跑", r.stderr)
        self.assertFalse((self.home / ".claude-accounts" / "_session-backups").exists())

    def test_trim_refuses_registered_session_but_force_overrides(self):
        """挂起的登记也可能是活进程握着的会话：默认拒绝，--force 才裁。"""
        self.seed_big_transcript(session="abcde123-0000-0000-0000-000000000000")
        self.seed_daemon_dir("001")
        payload = self.agents_payload("abcde123", "abcde123-0000-0000-0000-000000000000", state="blocked")
        r = self.run_cli("trim", "abcde123", "--keep=20000", FAKE_CLAUDE_AGENTS=payload)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("还有后台登记", r.stderr)
        self.assertFalse((self.home / ".claude-accounts" / "_session-backups").exists())
        r = self.run_cli("trim", "abcde123", "--keep=20000", "--force", FAKE_CLAUDE_AGENTS=payload)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.home / ".claude-accounts" / "_session-backups").is_dir())

    def test_trim_unknown_prefix_fails(self):
        self.seed_big_transcript()
        r = self.run_cli("trim", "zzzz", "--keep=20000")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("没有匹配", r.stderr)

    def test_trim_ambiguous_prefix_fails(self):
        self.seed_big_transcript(session="abcde123-0000-0000-0000-000000000000")
        self.seed_big_transcript(session="abcde124-0000-0000-0000-000000000000")
        r = self.run_cli("trim", "abcde12", "--keep=20000")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("匹配到 2 条", r.stderr)
        self.assertFalse((self.home / ".claude-accounts" / "_session-backups").exists())

    def test_sessions_empty_pool(self):
        r = self.run_cli("sessions")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("没有可续会话", r.stdout)

    def test_sessions_uses_session_start_cwd(self):
        """中途记录会带上别的目录（memory 工具等），分组要按会话起始目录。"""
        self.run_cli("new", "002")
        self.seed_transcript(
            "001", "ffff6666-0000-0000-0000-000000000000", title="中途换过 cwd",
            cwd="/proj/start", trailing_cwd="/proj/start/memory",
        )
        out = self.run_cli("sessions").stdout
        self.assertIn("/proj/start   ", out)
        self.assertNotIn("/proj/start/memory", out)

    # -- 目录信任继承 ----------------------------------------------------
    def test_agents_json_accepts_new_field_names(self):
        """`claude agents --json` 2.1.296 起把 `id`/`state` 改叫 `sessionId`/`status`。

        不认新字段的后果很实在：`job` 变空 → 推送路径拿空 id 去 `claude stop`（等于没停）；
        `state` 全变 unknown → 等待器把"还在跑"的会话当成别的状态。
        """
        self.seed_api_profile("ds")
        payload = json.dumps([{
            "pid": 12345, "cwd": "/tmp/x", "kind": "interactive", "startedAt": 1,  # noqa: S108 - 测试里只是一个路径字符串
            "sessionId": "abcde123-1111-2222-3333-444455556666", "name": "factor-dc",
            "status": "busy",
        }])
        out = self._run_python(AGENTS_PROBE, payload)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("id=abcde123", out.stdout)
        self.assertIn("job=abcde123", out.stdout)     # stop 只认短 id，不能给完整 UUID
        self.assertIn("state=busy", out.stdout)
        self.assertIn("name=factor-dc", out.stdout)

    def test_login_on_missing_profile_points_at_add(self):
        """号不存在时要说清"login 不建号"，并给一步到位的 `cca add`（而不是老名字 claude-acct new）。"""
        r = self.run_cli("login", "003")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("login 只负责登录", r.stderr)
        self.assertIn("cca add 003", r.stderr)
        self.assertIn("cca new 003", r.stderr)
        self.assertNotIn("claude-acct new", r.stderr)
    def test_login_does_not_auto_open_browser(self):
        """登录只打链接、不自动开浏览器（`BROWSER=/bin/false`）—— 本机没有本地浏览器，open 只会卡住。"""
        self.run_cli("new", "002")
        r = self.run_cli("login", "002")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.record()["argv"], ["auth", "login"])
        self.assertEqual(self.record()["browser"], "/bin/false")

    def test_login_respects_explicit_browser(self):
        """使用者显式设了 BROWSER 就尊重他（想让它开就开）。"""
        self.run_cli("new", "002")
        r = self.run_cli("login", "002", BROWSER="firefox")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.record()["browser"], "firefox")

    def test_run_does_not_auto_open_browser(self):
        """交互启动也带上（界面里 /login 同样只打链接）。"""
        self.run_cli("new", "002")
        r = self.run_cli("002", "--version")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.record()["browser"], "/bin/false")

    def test_login_seeds_onboarding_and_trust(self):
        """登录后配置目录才存在全局配置：顺手把引导标记与 001 的信任带过去。"""
        self.run_cli("new", "002")
        (self.acct_dir("001") / ".claude.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {"emailAddress": "claude001@example.com"},
                    "projects": {"/a": {"hasTrustDialogAccepted": True}, "/b": {"hasTrustDialogAccepted": False}},
                }
            ),
            encoding="utf-8",
        )
        cfg = self.acct_dir("002") / ".claude.json"
        cfg.write_text(json.dumps({"oauthAccount": {"emailAddress": "claude002@example.com"}}), encoding="utf-8")
        r = self.run_cli("login", "002")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(cfg.read_text(encoding="utf-8"))
        self.assertTrue(data["projects"]["/a"]["hasTrustDialogAccepted"])
        self.assertNotIn("/b", data["projects"])  # 001 没信任的不继承
        self.assertIs(data["hasCompletedOnboarding"], True)  # noqa: FBT003 - unittest 断言只接受位置参数
        self.assertEqual(data["oauthAccount"]["emailAddress"], "claude002@example.com")
        self.assertIn("目录信任", r.stdout)

    def test_sync_merges_default_trust_without_losing_local(self):
        self.run_cli("new", "002")
        (self.acct_dir("001") / ".claude.json").write_text(
            json.dumps({"projects": {"/a": {"hasTrustDialogAccepted": True}}}), encoding="utf-8"
        )
        cfg = self.acct_dir("002") / ".claude.json"
        cfg.write_text(json.dumps({"projects": {"/mine": {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
        r = self.run_cli("sync", "002")
        self.assertEqual(r.returncode, 0, r.stderr)
        projects = json.loads(cfg.read_text(encoding="utf-8"))["projects"]
        self.assertIn("/a", projects)
        self.assertIn("/mine", projects)  # 自己的记录不被覆盖

    # -- 渲染 ------------------------------------------------------------
    def _run_python(self, code, *args, env_extra=None):
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "CCA_WATCHDOG_AUTOSTART": "0",
            # 探针直接 `from cca.xxx import ...`，不用再加载启动器。
            "PYTHONPATH": str(SRC),
            # 临时 HOME：被测模块里的 profile 路径要落在测试沙箱里（否则会读到真实 ~/.claude）
            "HOME": str(self.home),
        }
        env.update(env_extra or {})
        return subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code), str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )

    def test_rich_is_imported_lazily(self):
        """非展示命令必须能在没有 Rich 的环境里跑：import 本模块不得加载 rich。"""
        result = self._run_python(
            """
            import json, sys
            from cca.report import make_console
            before = sorted(n for n in sys.modules if n == "rich" or n.startswith("rich."))
            make_console(force_terminal=False)
            after = sorted(n for n in sys.modules if n == "rich" or n.startswith("rich."))
            print(json.dumps({"before": before, "after": after}))
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["before"], [], "import claude-acct 不应加载 rich")
        self.assertIn("rich", data["after"])

    def test_tables_align_wide_characters(self):
        """中文宽字符要按显示宽度排版 —— 这就是引入 Rich 要解决的问题。"""
        result = self._run_python(
            """
            import io, json, sys, unicodedata
            from cca.report import _plain, _table, make_console, state_text
            buffer = io.StringIO()
            console = make_console(file=buffer, width=80, force_terminal=False, color_system=None)
            table = _table(console, min_width=60)
            table.add_column("PROFILE")
            table.add_column("状态")
            table.add_column("后台", justify="right")
            table.add_row(_plain("001"), state_text("working"), _plain("3/5"))
            table.add_row(_plain("002"), state_text("blocked"), _plain("0/0"))
            console.print(table)
            lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
            widths = {sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in line) for line in lines}
            print(json.dumps({"lines": lines, "widths": sorted(widths)}))
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(len(data["widths"]), 1, data["lines"])
        self.assertIn("blocked", " ".join(data["lines"]))

    # -- 命令面（对齐 podjob：ls 别名 / --json / --wide）------------------
    def test_list_alias_ls(self):
        self.run_cli("new", "002")
        self.assertIn("claude001@example.com", self.run_cli("ls").stdout)

    def test_list_json(self):
        self.run_cli("new", "002")
        data = json.loads(self.run_cli("list", "--json").stdout)
        self.assertNotIn("current", data)
        self.assertEqual([p["profile"] for p in data["profiles"]], ["001", "002"])
        self.assertIn("background", data["profiles"][0])

    def test_sessions_rejects_profile_arg(self):
        """会话不绑账号：池子就是池子，写号不再是这条命令的参数。"""
        r = self.run_cli("sessions", "002")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("多余参数", r.stderr)

    def test_sessions_lists_pool_with_live_owner_per_profile(self):
        """一次列出共享池：每行标出当前挂在哪个号上（两个号各自的会话都要在）。"""
        self.run_cli("new", "002")
        self.seed_transcript("001", "aaaa1111-0000-0000-0000-000000000000", title="甲的活")
        self.seed_transcript("002", "bbbb2222-0000-0000-0000-000000000000", title="乙的活")
        self.seed_daemon_dir("001")
        self.seed_daemon_dir("002")
        payload_001 = self.agents_payload("aaaa1111", "aaaa1111-0000-0000-0000-000000000000",
                                          state="working", name="甲的活")
        r = self.run_cli("sessions", FAKE_CLAUDE_AGENTS=payload_001)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("甲的活", r.stdout)
        self.assertIn("乙的活", r.stdout)
        self.assertIn("共享池", r.stdout)

    def test_sessions_json_reports_live_owner(self):
        self.run_cli("new", "002")
        sid = "abcde123-0000-0000-0000-000000000000"
        self.seed_transcript("001", sid, title="sam", prompt="继续做X")
        self.seed_daemon_dir("001")
        payload = json.dumps(
            [{"id": "abcde123", "kind": "background", "name": "sam", "cwd": "/x", "sessionId": sid, "state": "busy"}]
        )
        data = json.loads(self.run_cli("sessions", "--json", FAKE_CLAUDE_AGENTS=payload).stdout)
        self.assertEqual(len(data["sessions"]), 1)
        session = data["sessions"][0]
        self.assertEqual(session["id"], sid)
        self.assertEqual(session["title"], "sam")
        self.assertEqual(session["prompt"], "继续做X")
        self.assertEqual(session["live"], [{"profile": "001", "state": "busy"}])

    def test_sessions_wide_keeps_full_title_and_prompt(self):
        self.run_cli("new", "002")
        long_title = "这是一个很长的标题" * 4
        long_prompt = "这是一条很长的提问" * 4
        self.seed_transcript("001", "ffff7777-0000-0000-0000-000000000000", title=long_title, prompt=long_prompt)
        self.assertNotIn(long_title, self.run_cli("sessions").stdout)  # 默认按列宽截断
        wide = self.run_cli("sessions", "--wide").stdout
        self.assertIn(long_title, wide)
        self.assertIn(long_prompt, wide)

    def test_doctor_json(self):
        self.run_cli("new", "002")
        data = json.loads(self.run_cli("doctor", "--json").stdout)
        self.assertEqual(data["profiles"], ["001", "002"])
        self.assertEqual(data["fails"], [])
        self.assertIn("warns", data)

    # -- 续会话 ----------------------------------------------------------






    def test_build_env_proxy_modes(self):
        """默认注入内建代理；CCA_PROXY=none 只"不注入"（继承值保留）；CCA_PROXY=<url> 换地址。"""
        result = self._run_python(
            """
            import json, os, sys
            from cca.env import build_env
            os.environ.pop("CCA_PROXY", None)
            os.environ["CCA_PROXY"] = "http://default:1"
            default = build_env("001")
            os.environ["CCA_PROXY"] = "none"
            off_clean = build_env("001")
            os.environ["HTTPS_PROXY"] = "http://inherited:1"
            os.environ["HTTP_PROXY"] = "http://inherited:1"
            off = build_env("001")
            os.environ["CCA_PROXY"] = "http://my-proxy:3128"
            custom = build_env("001")
            print(json.dumps({
                "default": "http://default:1",
                "default_https": default.get("HTTPS_PROXY"),
                "off_clean_https": off_clean.get("HTTPS_PROXY"),
                "off_https": off.get("HTTPS_PROXY"),
                "off_http": off.get("HTTP_PROXY"),
                "custom_https": custom.get("HTTPS_PROXY"),
                "custom_http": custom.get("HTTP_PROXY"),
                "custom_no_proxy": custom.get("NO_PROXY"),
            }))
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["default_https"], data["default"])
        self.assertIsNone(data["off_clean_https"], "none 不应注入内建代理")
        self.assertEqual(data["off_https"], "http://inherited:1", "none 应保留继承的代理变量")
        self.assertEqual(data["off_http"], "http://inherited:1")
        self.assertEqual(data["custom_https"], "http://my-proxy:3128")
        self.assertEqual(data["custom_http"], "http://my-proxy:3128")
        self.assertIn(".example.com", data["custom_no_proxy"])

    def test_run_proxy_none_keeps_inherited(self):
        self.run_cli("new", "002")
        self.run_cli("002", CCA_PROXY="none", HTTPS_PROXY="http://inherited:1")
        self.assertEqual(self.record()["https_proxy"], "http://inherited:1")

    def test_run_proxy_none_does_not_inject_builtin(self):
        self.run_cli("new", "002")
        self.run_cli("002", CCA_PROXY="none")
        self.assertIsNone(self.record()["https_proxy"])

    def test_doctor_reports_proxy_state(self):
        self.run_cli("new", "002")
        self.assertIn("代理:", self.run_cli("doctor").stdout)
        self.assertIn("未配置", self.run_cli("doctor", CCA_PROXY="").stdout)
        self.assertIn("CCA_PROXY=none", self.run_cli("doctor", CCA_PROXY="none").stdout)
        # none 只表示"直连"：把继承到的代理变量点名，避免读成"已经不用代理了"
        inherited = self.run_cli("doctor", CCA_PROXY="none", HTTPS_PROXY="http://mine:1").stdout
        self.assertIn("直连", inherited)
        self.assertIn("继承 HTTPS_PROXY", inherited)
        data = json.loads(self.run_cli("doctor", "--json", CCA_PROXY="none").stdout)
        self.assertEqual(data["proxy"], {"url": None, "unset": False})

    def test_doctor_warns_on_unresolvable_proxy(self):
        self.run_cli("new", "002")
        r = self.run_cli("doctor", CCA_PROXY="http://proxy.invalid:7897")
        self.assertIn("解析不了", r.stdout)

    # -- 体检 ------------------------------------------------------------
    def test_doctor_ok_on_healthy_tree(self):
        r = self.run_cli("doctor")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OK", r.stdout)

    def test_doctor_flags_broken_share(self):
        self.run_cli("new", "002")
        self.seed_credentials("002")
        link = self.acct_dir("002") / "projects"
        link.unlink()
        link.mkdir()
        r = self.run_cli("doctor")
        self.assertIn("WARN", r.stdout)
        self.assertIn("projects", r.stdout)

    def test_doctor_flags_multi_profile_workers(self):
        self.run_cli("new", "002")
        proc = subprocess.Popen(["sleep", "30"])
        try:
            for name in ("001", "002"):
                base = self.acct_dir(name)
                daemon = base / "daemon"
                daemon.mkdir(parents=True, exist_ok=True)
                (daemon / "roster.json").write_text(
                    json.dumps({"workers": {"w": {"pid": proc.pid, "sessionId": "s"}}}), encoding="utf-8"
                )
            r = self.run_cli("doctor")
            self.assertIn("串行", r.stdout)
        finally:
            proc.terminate()
            proc.wait()

    def test_removed_commands_explain_replacement(self):
        """四个已删命令要给替代写法，而不是"profile 不存在"或"未知命令"。"""
        for cmd in ("switch", "bind", "unbind", "resume"):
            r = self.run_cli(cmd)
            self.assertNotEqual(r.returncode, 0, cmd)
            self.assertIn("已移除", r.stderr, cmd)

    def test_removed_names_stay_reserved(self):
        for name in ("switch", "bind", "unbind", "resume"):
            self.assertNotEqual(self.run_cli("new", name).returncode, 0, name)

    def test_legacy_binding_files_are_ignored(self):
        """旧的 bind.tsv / .last-profile 不再参与任何决定，存在也不影响选号。"""
        self.run_cli("new", "002")
        root = self.home / ".claude-accounts"
        (root / "bind.tsv").write_text("# workspace_uuid\tprofile\nws-1\t002\n", encoding="utf-8")
        (root / ".last-profile").write_text("002\n", encoding="utf-8")
        r = self.run_cli("which", "001", CODER_WORKSPACE_ID="ws-1", CLAUDE_ACCT="002")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("001", r.stdout)

    def test_help_has_no_removed_commands(self):
        r = self.run_cli("--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        for word in ("cca switch", "cca bind", "cca unbind", "cca resume"):
            # 只认整词：cca resume-bg 是现役命令，不是已删掉的 cca resume
            self.assertNotRegex(r.stdout, re.escape(word) + r"(?![\w-])", word)
        self.assertIn("cca handoff", r.stdout)
        self.assertIn("没有默认号", r.stdout)

    def test_version_matches_version_file(self):
        expected = (REPO / "VERSION").read_text(encoding="utf-8").strip()
        out = self.run_cli("version").stdout.strip()
        self.assertEqual(out, f"claude-acct {expected}")
        self.assertTrue(re.match(r"^\d+\.\d+\.\d+$", expected))


if __name__ == "__main__":
    unittest.main()


class IdentityTests(unittest.TestCase):
    """请求头：版本从 claude 的软链读，读不到就不写 UA。"""

    def test_headers_with_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            version_file = Path(tmp) / "2.1.308"
            version_file.write_text("", encoding="utf-8")
            with mock.patch("cca.api.identity.shutil.which", return_value=str(version_file)):
                headers = identity.anthropic_headers("tok")
        self.assertEqual(headers["user-agent"], "claude-cli/2.1.308 (external, sdk-cli)")
        self.assertEqual(headers["x-app"], "cli")
        self.assertEqual(headers["authorization"], "Bearer tok")

    def test_headers_without_cli(self):
        with mock.patch("cca.api.identity.shutil.which", return_value=None):
            headers = identity.anthropic_headers()
        self.assertNotIn("user-agent", headers)
        self.assertNotIn("authorization", headers)



class EgressLocaleTests(unittest.TestCase):
    """语言固定美式英语；按 locale -a 的实际写法返回。"""

    def test_returns_installed_name(self):
        from cca.api import egress  # noqa: PLC0415
        with mock.patch.object(egress, "system_locales",
                               return_value={"C", "C.utf8", "en_US.utf8", "POSIX"}):
            self.assertEqual(egress.default_locale(), "en_US.utf8")

    def test_missing_returns_empty(self):
        from cca.api import egress  # noqa: PLC0415
        with mock.patch.object(egress, "system_locales", return_value={"C", "POSIX"}):
            self.assertEqual(egress.default_locale(), "en_US.UTF-8")


