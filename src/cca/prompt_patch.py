"""按规则改写 claude 可执行文件里的系统提示：只碰命中的那几行，其余字节不动。

为什么改文件而不是改请求：Claude Code 没有"只删系统提示里某一句"的开关。能改请求体的
只有 `CLAUDE_CODE_EXTRA_BODY`，而它是整段替换 `system`，会把 27k 的 harness 说明一起换掉，
还得手动维护一份抓来的提示模板。等长改写只动命中的字符串内容，动态内容（工作目录、记忆
路径、日期）仍由 cc 自己现算。

安全约束：
- 只替换引号之间的内容，引号和 `var X=` 之类的外壳保留，语法不会坏。
- 命中 0 就不改文件，返回"未命中"，由调用方提示。
- 每处改动连同原文一起记进缓存文件，`revert()` 能精确写回。
- 文件指纹（路径 + 大小 + mtime）没变就跳过，不读 366MB。
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

# 默认规则：每条是一组"必须同时出现在同一行"的字节串。
# 命中的行里，只改写特征词所在的那个字符串字面量。
DEFAULT_RULES: tuple[tuple[bytes, ...], ...] = (
    (b"security testing", b"CTF"),
    (b"Dual-use",),
)

if TYPE_CHECKING:
    from pathlib import Path

QUOTES = (b'"', b"`")   # 单引号太容易误判（don't 这类），不认

# bun 打包后同一段源码有两份：一份 UTF-8 明文（带换行），一份在字符串表里按 UTF-16LE 存
# （每个字符后面跟一个 \x00，不分行）。只改明文那份没用——运行时读的是字符串表。
UTF16_UNIT = 2


class Rule:
    """规则：一组必须同行的特征词。anchor 取最长那个，找它更快也更准。"""

    def __init__(self, needles: tuple[bytes, ...]) -> None:
        self.needles = needles
        self.anchor = max(needles, key=len)


def load_rules(rules_file: Path) -> tuple[tuple[bytes, ...], ...]:
    """读规则文件；没有或坏了就用默认规则（坏文件只影响自定义，不该拦住启动）。"""
    try:
        raw = json.loads(rules_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_RULES
    rules = []
    for item in raw if isinstance(raw, list) else []:
        needles = tuple(str(n).encode("utf-8") for n in item if str(n).strip())
        if needles:
            rules.append(needles)
    return tuple(rules) or DEFAULT_RULES


def _utf16(needle: bytes) -> bytes:
    """ASCII 特征词的 UTF-16LE 形式：每个字节后补一个 \x00。"""
    return b"".join(bytes((c, 0)) for c in needle)


def _line_bounds(data: bytes, i: int, unit: int = 1) -> tuple[int, int]:
    """特征词所在的"行"。UTF-16 表里没有换行，整表算一行。"""
    if unit > 1:
        start = max(0, i - 4096)
        return start, min(len(data), i + 4096)
    start = data.rfind(b"\n", 0, i) + 1
    end = data.find(b"\n", i)
    return start, len(data) if end < 0 else end


def _literal_bounds_utf16(data: bytes, i: int, lo: int, hi: int) -> tuple[int, int] | None:
    """UTF-16 表里的字符串边界：引号也是两字节。"""
    quote = b'"\x00'
    start = data.rfind(quote, lo, i)
    if start < 0 or (start - lo) % UTF16_UNIT:
        return None
    end = data.find(quote, i, hi)
    return (start + UTF16_UNIT, end) if end > i else None


def _literal_bounds(data: bytes, i: int, lo: int, hi: int) -> tuple[int, int] | None:
    """特征词所在的字符串字面量内容区间（不含引号）。

    取特征词左边最近的引号当开头，右边最近的同种引号当结尾；两者之间再出现同种引号
    就说明没成对，放弃这处，免得把半个字符串抹掉。
    """
    found = [(data.rfind(q, lo, i), q) for q in QUOTES]
    for start, quote in sorted(found, reverse=True):
        if start < 0 or data.find(quote, start + 1, i) >= 0:
            continue
        end = data.find(quote, i, hi)
        if end > i:
            return start + 1, end
    # 兜底：bun 的字符串表里不带引号，靠 \x00 分隔。左右各扫到最近的 \x00。
    start = data.rfind(b"\x00", lo, i) + 1
    end = data.find(b"\x00", i, hi)
    return (start, end) if start <= i < end else None


def find_spans(data: bytes, rules: tuple[tuple[bytes, ...], ...]) -> list[tuple[int, int, int]]:
    """找出要改写的内容区间：(起点, 终点, 字节单位)。明文和 UTF-16 表各扫一遍。"""
    spans: list[tuple[int, int, int]] = []
    for needles in rules:
        for unit, encoded in ((1, needles), (UTF16_UNIT, tuple(_utf16(n) for n in needles))):
            rule = Rule(encoded)
            pos = 0
            while True:
                hit = data.find(rule.anchor, pos)
                if hit < 0:
                    break
                pos = hit + 1
                if unit > 1 and hit % UTF16_UNIT:
                    continue
                lo, hi = _line_bounds(data, hit, unit)
                if not all(n in data[lo:hi] for n in rule.needles):
                    continue
                bounds = (_literal_bounds_utf16(data, hit, lo, hi) if unit > 1
                          else _literal_bounds(data, hit, lo, hi))
                if bounds is None:
                    continue
                span = (bounds[0], bounds[1], unit)
                if span not in spans:
                    spans.append(span)
    return spans


def _blank(data: bytearray, start: int, end: int, unit: int) -> None:
    """把区间内容换成等长空白：长度不变，打包偏移不会坏。UTF-16 用两字节的空格。"""
    fill = b" " if unit == 1 else b" \x00"
    data[start:end] = fill * ((end - start) // unit)


def read_cache(cache_file: Path) -> dict:
    try:
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def write_cache(cache_file: Path, cache: dict) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(cache, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    cache_file.chmod(0o600)


def _fingerprint(path: Path) -> dict:
    st = path.stat()
    return {"path": str(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _record(cache: dict, path: Path, entries: list[dict], hits: int) -> None:
    cache[str(path)] = {**_fingerprint(path), "hits": hits, "entries": entries}


ENABLED_KEY = "_enabled"


def enabled(cache_file: Path) -> bool:
    """默认打开：装上就管。`cca prompt off` 关掉自检并还原。"""
    return bool(read_cache(cache_file).get(ENABLED_KEY, True))


def set_enabled(cache_file: Path, *, value: bool) -> None:
    cache = read_cache(cache_file)
    cache[ENABLED_KEY] = value
    write_cache(cache_file, cache)


def currently_patched(path: Path) -> bool:
    """文件里还找得到特征词吗：还找得到 = 没改过。"""
    data = path.read_bytes()
    return not find_spans(data, DEFAULT_RULES)


def apply_to(path: Path, rules: tuple[tuple[bytes, ...], ...], cache_file: Path) -> dict:
    """改写一次。返回 {status: patched|already|nomatch|locked, hits, entries}。"""
    path = path.resolve()
    if not path.is_file():
        return {"status": "missing", "hits": 0}
    cache = read_cache(cache_file)
    st = path.stat()
    known = cache.get(str(path)) or {}
    if (known.get("size"), known.get("mtime_ns")) == (st.st_size, st.st_mtime_ns):
        return {"status": "already", "hits": int(known.get("hits") or 0), "entries": known.get("entries") or []}

    data = bytearray(path.read_bytes())
    spans = find_spans(bytes(data), rules)
    if not spans:
        _record(cache, path, [], 0)
        write_cache(cache_file, cache)
        return {"status": "nomatch", "hits": 0, "entries": []}

    entries = []
    for start, end, unit in spans:
        original = bytes(data[start:end])
        entries.append({"start": start, "end": end, "unit": unit,
                        "original": base64.b64encode(original).decode("ascii")})
        _blank(data, start, end, unit)

    mode = path.stat().st_mode
    tmp = path.with_suffix(path.suffix + ".cca-tmp")
    tmp.write_bytes(bytes(data))
    tmp.chmod(mode)
    tmp.replace(path)
    _record(cache, path, entries, len(entries))
    write_cache(cache_file, cache)
    return {"status": "patched", "hits": len(entries), "entries": entries}


def revert(path: Path, cache_file: Path) -> dict:
    """按记录把原文写回去。窗口对不上（文件换过版本）就拒绝，绝不猜。"""
    path = path.resolve()
    cache = read_cache(cache_file)
    known = cache.get(str(path))
    if not known or not known.get("entries"):
        return {"status": "nothing", "hits": 0}
    data = bytearray(path.read_bytes())
    restored = 0
    for entry in known["entries"]:
        start, end = int(entry["start"]), int(entry["end"])
        original = base64.b64decode(entry["original"])
        if end - start != len(original):
            return {"status": "mismatch", "hits": restored}
        fill = b" " * (end - start) if int(entry.get("unit") or 1) == 1 else b" \x00" * ((end - start) // 2)
        if bytes(data[start:end]) != fill:
            return {"status": "mismatch", "hits": restored}
        data[start:end] = original
        restored += 1
    mode = path.stat().st_mode
    tmp = path.with_suffix(path.suffix + ".cca-tmp")
    tmp.write_bytes(bytes(data))
    tmp.chmod(mode)
    tmp.replace(path)
    cache.pop(str(path), None)
    write_cache(cache_file, cache)
    return {"status": "reverted", "hits": restored}


def needs_work(path: Path, cache_file: Path) -> bool:
    """启动前的快速判断：只看指纹，不读文件。"""
    try:
        st = path.stat()
    except OSError:
        return False
    known = (read_cache(cache_file).get(str(path.resolve())) or {})
    return (known.get("size"), known.get("mtime_ns")) != (st.st_size, st.st_mtime_ns)
