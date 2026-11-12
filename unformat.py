#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把格式化写出去的字符串，按同一段格式描述读回成值。

    >>> from unformat import parse
    >>> parse("It's {}, I love it!", "It's spam, I love it!")[0]
    'spam'
    >>> parse("{c:02d}", "100")["c"]
    100

与「用正则猜每个字段长什么样」的做法不同，这里只认一条规则：

    读到的值，重新交给 format() 用同一段 spec 写回去，
    必须能把捕获到的原文（去掉填充字符后）复原出来。

这样宽度、对齐、填充、千分位、精度、日期都由 format() 自己定义，
工具不需要为每种类型维护一套正则，也不会出现「format 写得出来、
却读不回来」的分叉。匹配失败时给出逐字段的判定表，而不是一个 None。

只用标准库，单文件，零配置。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import decimal as _decimal
import json
import re
import sys
from dataclasses import dataclass, field as _dc_field
from typing import Any, Iterable, Iterator

__version__ = "1.0.0"
__all__ = [
    "parse", "search", "findall", "compile", "Parser", "Result",
    "FormatError", "Format", "Field",
]

_MAX_ATTEMPTS = 20000  # 防止病态格式串导致候选组合爆炸


class FormatError(ValueError):
    """格式串本身有问题。消息里会指出位置和改法。"""


# --------------------------------------------------------------------------
# 格式串解析
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Format:
    """一条格式描述 [:spec] 的解析结果。

    保留 raw 以便用 format(value, raw) 原样写回；其余字段只用来决定
    「哪些字符是填充、该从哪一侧剥掉」。
    """

    raw: str = ""
    fill: str = " "
    align: str = ""
    sign: str = ""
    alt: bool = False
    zero: bool = False
    width: int | None = None
    grouping: str = ""
    precision: int | None = None
    type: str = ""
    is_datetime: bool = False
    has_padding: bool = False

    @property
    def numeric(self) -> bool:
        return self.type in {"d", "b", "o", "x", "X", "n", "f", "F",
                             "e", "E", "g", "G", "%"}

    @property
    def default_align(self) -> str:
        # format() 的约定：数字右对齐，字符串左对齐。
        return ">" if self.numeric else "<"

    @property
    def effective_align(self) -> str:
        if self.align:
            return self.align
        if self.zero and self.numeric:
            return "="
        return self.default_align if self.has_padding else ""

    @property
    def effective_fill(self) -> str:
        if self.zero and self.numeric and not self.align and self.fill == " ":
            return "0"
        return self.fill

    @property
    def write_spec(self) -> str:
        """交给 format() 的描述。w 是读字符串用的，写回去等价成 s。"""
        if self.type == "w" and self.raw.endswith("w"):
            return self.raw[:-1] + "s"
        return self.raw


_INT_TYPES = {"d", "b", "o", "x", "X", "n"}
_FLOAT_TYPES = {"f", "F", "e", "E", "g", "G", "%"}
_ALLOWED_TYPES = _INT_TYPES | _FLOAT_TYPES | {"", "s", "w"}

# strftime 指令 -> 正则片段。只收 format()/strptime 都认得的常用指令。
_DT_PARTS = {
    "Y": r"\d{4}", "y": r"\d{2}", "m": r"\d{1,2}", "d": r"\d{1,2}",
    "H": r"\d{1,2}", "I": r"\d{1,2}", "M": r"\d{1,2}", "S": r"\d{1,2}",
    "f": r"\d{1,6}", "j": r"\d{1,3}",
    "b": r"[A-Za-z]{3}", "B": r"[A-Za-z]+",
    "a": r"[A-Za-z]{3}", "A": r"[A-Za-z]+",
    "p": r"(?:AM|PM|am|pm)", "z": r"(?:Z|[+-]\d{2}:?\d{2})",
    "Z": r"[A-Za-z/_+-]+",
}
_DT_TIME_CODES = set("HIMSfpXrTzZ")
_DT_DATE_CODES = set("YymdbBaAjUWwGDVxu")


def _datetime_regex(spec: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(spec):
        if spec[i] == "%" and i + 1 < len(spec):
            code = spec[i + 1]
            if code == "%":
                out.append("%")
            elif code in _DT_PARTS:
                out.append("(?:" + _DT_PARTS[code] + ")")
            else:
                raise FormatError(
                    f"spec {spec!r} 里的日期指令 %{code} 暂不支持；"
                    f"可用指令：{''.join(sorted(_DT_PARTS))}"
                )
            i += 2
        else:
            out.append(re.escape(spec[i]))
            i += 1
    return re.compile("".join(out), re.ASCII)


def _parse_format(spec: str, where: str) -> Format:
    """拆开 [[fill]align][sign][#][0][width][grouping][.precision][type]。

    也接受整段 strftime 描述（含 % 就直接走日期分支）。
    """
    # 只有出现 %X 这样的 strftime 指令才算日期；结尾那个单独的 % 是
    # 「百分数」类型符（例如 ',.1%'），不能误判成日期描述。
    if re.search(r"%[A-Za-z%]", spec):
        _datetime_regex(spec)  # 早一点报出不支持的指令
        return Format(raw=spec, type=spec, is_datetime=True, has_padding=False)

    rest = spec
    fill, align, sign, alt, zero = " ", "", "", False, False
    width, grouping, precision = None, "", None

    if len(rest) >= 2 and rest[1] in "<>=^":
        fill, align, rest = rest[0], rest[1], rest[2:]
    elif rest and rest[0] in "<>=^":
        align, rest = rest[0], rest[1:]

    if rest and rest[0] in "+- ":
        sign, rest = rest[0], rest[1:]
    if rest.startswith("#"):
        alt, rest = True, rest[1:]
    # 前导 0 只有后面还跟着内容时才是「零填充」旗标，单独的 "0" 当宽度处理。
    if rest.startswith("0") and len(rest) > 1:
        zero, rest = True, rest[1:]

    m = re.match(r"\d+", rest)
    if m:
        width = int(m.group())
        rest = rest[m.end():]

    if rest[:1] in (",", "_"):
        grouping, rest = rest[0], rest[1:]

    if rest.startswith("."):
        m = re.match(r"\.(\d+)", rest)
        if not m:
            raise FormatError(
                f"{where}: 精度 . 后面要跟数字（例如 .2 或 .2f），收到 {spec!r}"
            )
        precision = int(m.group(1))
        rest = rest[m.end():]

    typ = rest
    if typ not in _ALLOWED_TYPES:
        raise FormatError(
            f"{where}: 不认识的类型 {typ!r}（spec={spec!r}）。"
            f"支持：空字符串或 s/w/d/b/o/x/X/n/f/F/e/E/g/G/%，以及 %Y-%m-%d 这类日期描述。"
        )

    has_padding = any((align, fill != " ", zero, width is not None))
    return Format(
        raw=spec, fill=fill, align=align, sign=sign, alt=alt, zero=zero,
        width=width, grouping=grouping, precision=precision, type=typ,
        has_padding=has_padding,
    )


@dataclass(frozen=True)
class Field:
    name: str            # 空串表示匿名（按位置收集）
    spec: Format
    optional: bool
    source: str          # 原始 {..} 文本，仅用于展示
    index: int


@dataclass(frozen=True)
class _Lit:
    text: str


_Segment = Field | _Lit


def _split_format(fmt: str) -> list[_Segment]:
    """把格式串切成 字面量 / 字段 交替的片段。"""
    segs: list[_Segment] = []
    buf: list[str] = []
    anon = 0

    def flush() -> None:
        if buf:
            segs.append(_Lit("".join(buf)))
            buf.clear()

    i, n = 0, len(fmt)
    while i < n:
        ch = fmt[i]
        if ch == "{":
            if i + 1 < n and fmt[i + 1] == "{":
                buf.append("{")
                i += 2
                continue
            j = fmt.find("}", i + 1)
            if j < 0:
                raise FormatError(
                    f"格式串第 {i} 个字符的 {{ 没有配对的 }}；"
                    f"要输出字面量花括号请写成 {{{{ 和 }}}}"
                )
            inner = fmt[i + 1:j]
            if "{" in inner:
                raise FormatError(f"字段 {{{inner}}} 里不能再出现 {{")
            if ":" in inner:
                name, spec = inner.split(":", 1)
            else:
                name, spec = inner, ""
            optional = name.endswith("?")
            if optional:
                name = name[:-1]
            if name and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.\-\[\]]*", name):
                raise FormatError(
                    f"字段名 {name!r} 不合法；用字母/数字/下划线，"
                    f"点号和方括号也可以（会作为完整键名保留）"
                )
            where = f"字段 {{{inner}}}"
            f = _parse_format(spec, where)
            flush()
            if name:
                idx = -1
            else:
                idx = anon
                anon += 1
            segs.append(Field(name=name, spec=f, optional=optional,
                              source="{" + inner + "}", index=idx))
            i = j + 1
            continue
        if ch == "}":
            if i + 1 < n and fmt[i + 1] == "}":
                buf.append("}")
                i += 2
                continue
            raise FormatError(
                f"格式串第 {i} 个字符有一个落单的 }}；"
                f"要输出字面量右花括号请写成 }}}}"
            )
        buf.append(ch)
        i += 1
    flush()
    if not any(isinstance(s, Field) for s in segs):
        raise FormatError("格式串里至少要有一个 {} 字段")
    return segs


# --------------------------------------------------------------------------
# 取值与写回校验
# --------------------------------------------------------------------------

def _strip_padding(raw: str, f: Format) -> str:
    """按 spec 声明的填充方向，把填充字符从原文两端剥掉。"""
    if not f.has_padding:
        return raw
    fill = f.effective_fill
    align = f.effective_align
    if align == "^":
        out = raw.strip(fill)
    elif align in (">", "="):
        out = raw.lstrip(fill)
    elif align == "<":
        out = raw.rstrip(fill)
    else:
        out = raw
    if out == "" and raw:
        # "000" + 03d 应当读作 0，而不是解析失败。
        if fill == "0" and f.numeric:
            return "0"
        return raw
    return out


def _clean_number(text: str, f: Format) -> str:
    text = text.strip()
    if f.grouping:
        text = text.replace(f.grouping, "")
    text = text.replace(",", "").replace("_", "")
    return text


def _convert(clean: str, f: Format) -> tuple[bool, Any, str]:
    """把去掉填充的文本转成 Python 值。失败时给原因，绝不抛给调用方。"""
    if f.is_datetime:
        try:
            dt = _dt.datetime.strptime(clean, f.raw)
        except ValueError as exc:
            return False, None, f"日期不合法（{exc}）"
        codes = {m.group(1) for m in re.finditer(r"%([A-Za-z%])", f.raw)}
        has_t = bool(codes & _DT_TIME_CODES)
        has_d = bool(codes & _DT_DATE_CODES)
        if has_d and has_t:
            return True, dt, ""
        if has_d:
            return True, dt.date(), ""
        if has_t:
            return True, dt.time(), ""
        return True, dt, ""

    t = f.type
    if t in ("", "s"):
        return True, clean, ""

    if t == "w":
        if not clean or not re.fullmatch(r"\w+", clean):
            return False, None, f"{clean!r} 不是字母/数字/下划线组成的词"
        return True, clean, ""

    if t == "%":
        body = clean.strip()
        if not body.endswith("%"):
            return False, None, "百分数字段需要以 % 结尾"
        try:
            # 先按十进制除，再转 float：12.3 / 100 用二进制直接算会写成
            # 0.12300000000000001，写回校验时容易让人以为读错了。
            v = float(_decimal.Decimal(_clean_number(body[:-1], f)) / 100)
        except (ValueError, _decimal.InvalidOperation):
            return False, None, f"{clean!r} 不是百分数"
        return True, v, ""

    if t in _FLOAT_TYPES:
        try:
            v = float(_clean_number(clean, f))
        except ValueError:
            return False, None, f"{clean!r} 不是 {t} 类型的数字"
        return True, v, ""

    base = {"d": 10, "b": 2, "o": 8, "x": 16, "X": 16, "n": 10}[t]
    body = _clean_number(clean, f)
    if not body:
        return False, None, "空的整数字段"
    # format(31, '#x') -> '0x1f'，int(...,16) 本身就认这个前缀。
    try:
        if t == "n":
            body = body.replace(".", "")
        v = int(body, base)
    except ValueError:
        return False, None, f"{clean!r} 不是 {base} 进制整数"
    return True, v, ""


def _verify(raw: str, value: Any, f: Format,
            ignore_case: bool) -> tuple[bool, str, bool, str]:
    """核心规则：把值用同一段 spec 写回去，看能不能复原原文。

    返回 (是否通过, 写回结果, 是否逐字节复原, 原因)。「逐字节复原」用于
    候选排序：能原样写回的切分点优先于只能「去掉填充后一致」的切分点，
    否则 "001002" + {:03x}{:03x} 会被先出现的 "0" 抢走。
    """
    try:
        reproduced = format(value, f.write_spec)
    except (ValueError, TypeError, OverflowError) as exc:
        return False, "", False, f"format() 无法用 {f.write_spec!r} 写回这个值（{exc}）"

    def eq(a: str, b: str) -> bool:
        return a.casefold() == b.casefold() if ignore_case else a == b

    if eq(raw, reproduced):
        return True, reproduced, True, ""
    a = _strip_padding(raw, f)
    b = _strip_padding(reproduced, f)
    if eq(a, b):
        return True, reproduced, False, ""
    return (
        False,
        reproduced,
        False,
        f"写回得到 {reproduced!r}，去掉填充后是 {b!r}，"
        f"与原文去掉填充后的 {a!r} 不一致",
    )


# --------------------------------------------------------------------------
# 结果对象
# --------------------------------------------------------------------------

@dataclass
class Result:
    fixed: tuple[Any, ...]
    named: dict[str, Any]
    spans: dict[str, tuple[int, int]]
    text: str
    start: int = 0
    end: int = 0
    trace: list[dict[str, Any]] = _dc_field(default_factory=list)

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, int):
            return self.fixed[key]
        return self.named[key]

    def __contains__(self, name: str) -> bool:
        return name in self.named

    def __repr__(self) -> str:
        return f"<Result {self.fixed!r} {self.named!r}>"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "fixed": [_jsonable(v) for v in self.fixed],
            "named": {k: _jsonable(v) for k, v in self.named.items()},
            "spans": {k: list(v) for k, v in self.spans.items()},
            "text": self.text[self.start:self.end],
        }


def _jsonable(v: Any) -> Any:
    if isinstance(v, (_dt.date, _dt.time, _dt.datetime)):
        return v.isoformat()
    if isinstance(v, float) and v != v:
        return "nan"
    return v


@dataclass
class Failure:
    reason: str
    segment: int
    position: int
    trace: list[dict[str, Any]] = _dc_field(default_factory=list)


# --------------------------------------------------------------------------
# 匹配器
# --------------------------------------------------------------------------

_SHAPE: dict[str, str] = {
    "": r"[\s\S]", "s": r"[\s\S]", "w": r"[\w]",
    "d": r"[0-9+\-_,.]", "n": r"[0-9+\-_,.]",
    "b": r"[01bB+\-_0x]", "o": r"[0-7oO+\-_0x]",
    "x": r"[0-9a-fA-FxX+\-_]", "X": r"[0-9a-fA-FxX+\-_]",
    "f": r"[0-9+\-.,_eEiInNaA]", "F": r"[0-9+\-.,_eEiInNaA]",
    "e": r"[0-9+\-.,_eEiInNaA]", "E": r"[0-9+\-.,_eEiInNaA]",
    "g": r"[0-9+\-.,_eEiInNaA]", "G": r"[0-9+\-.,_eEiInNaA]",
    "%": r"[0-9+\-.,_eEiInNaA%]",
}


class Parser:
    """编译一次，反复匹配。线程不安全（内部有 trace/尝试计数）。"""

    def __init__(self, fmt: str, ignore_case: bool = False):
        self.fmt = fmt
        self.segments = _split_format(fmt)
        self.ignore_case = ignore_case
        self._shape: dict[int, re.Pattern[str] | None] = {}
        for i, s in enumerate(self.segments):
            if isinstance(s, Field):
                if s.spec.is_datetime:
                    self._shape[i] = None
                else:
                    pattern = _SHAPE.get(s.spec.type, r"[\s\S]")
                    chars = pattern[1:-1]  # 去掉方括号
                    # 数字字段可能带空格 / 自定义填充的对齐前缀，这些字符
                    # 也要算进「候选区间」，否则 ' 4' 会被截断成 '4'。
                    for extra in (" ", s.spec.fill, "0" if s.spec.zero else ""):
                        if extra and len(extra) == 1:
                            chars += re.escape(extra)
                    self._shape[i] = re.compile("[" + chars + "]*", re.ASCII)
        self._lit_first = self.segments[0] if isinstance(self.segments[0], _Lit) else _Lit("")
        self._names: list[str] = []
        seen: set[str] = set()
        for s in self.segments:
            if isinstance(s, Field) and s.name and s.name not in seen:
                seen.add(s.name)
                self._names.append(s.name)

    # -- 对外 -------------------------------------------------------------
    def parse(self, text: str) -> Result | None:
        r = self._run(text, 0, require_end=True, explain=False)
        return r if isinstance(r, Result) else None

    def parse_ex(self, text: str) -> tuple[Result | None, list[dict[str, Any]], Failure]:
        r = self._run(text, 0, require_end=True, explain=True)
        if isinstance(r, Result):
            return r, r.trace, Failure("", -1, -1)
        return None, r.trace, Failure(r.reason, r.segment, r.position, r.trace)

    def search(self, text: str, start: int = 0) -> Result | None:
        for s in range(max(0, start), len(text) + 1):
            r = self._run(text, s, require_end=False, explain=False)
            if isinstance(r, Result):
                return r
        return None

    def findall(self, text: str) -> Iterator[Result]:
        pos = 0
        while pos <= len(text):
            r = self.search(text, pos)
            if r is None:
                return
            yield r
            pos = max(r.end, pos + 1)

    # -- 内部 -------------------------------------------------------------
    def _eq_lit(self, text: str, lit: str, pos: int) -> bool:
        piece = text[pos:pos + len(lit)]
        if len(piece) != len(lit):
            return False
        return piece.casefold() == lit.casefold() if self.ignore_case else piece == lit

    def _run(self, text: str, start: int, require_end: bool,
             explain: bool) -> Result | Failure:
        state = _State(text, start, require_end, explain)
        segs = self.segments
        ok = self._walk(segs, 0, start, {}, {}, {}, state)
        if ok:
            fixed = tuple(state.assign_fixed[i] for i in sorted(state.assign_fixed))
            return Result(
                fixed=fixed, named=dict(state.assign_named),
                spans=dict(state.assign_spans), text=text,
                start=start, end=state.end_pos, trace=state.trace,
            )
        return Failure(state.best_reason, state.best_segment,
                       state.best_pos, state.trace)

    def _walk(self, segs: list[_Segment], si: int, pos: int,
              fixed: dict[int, Any], named: dict[str, Any],
              spans: dict[str, tuple[int, int]], state: "_State") -> bool:
        state.attempts += 1
        if state.attempts > _MAX_ATTEMPTS:
            return False
        if si == len(segs):
            if state.require_end and pos != len(state.text):
                state.note(si, pos, f"字段都读完了，但还剩 {len(state.text) - pos} 个字符 "
                                    f"{state.text[pos:pos + 20]!r} 没被消费")
                return False
            state.end_pos = pos
            state.assign_fixed, state.assign_named = dict(fixed), dict(named)
            state.assign_spans = dict(spans)
            return True

        seg = segs[si]
        if isinstance(seg, _Lit):
            if self._eq_lit(state.text, seg.text, pos):
                if state.explain:
                    state.trace.append({
                        "step": si, "kind": "literal", "at": [pos, pos + len(seg.text)],
                        "text": seg.text, "ok": True,
                    })
                return self._walk(segs, si + 1, pos + len(seg.text),
                                  fixed, named, spans, state)
            state.note(si, pos, f"这里应该是字面量 {seg.text!r}，实际是 "
                                f"{state.text[pos:pos + len(seg.text) + 8]!r}")
            return False

        # --- 字段 ---
        # 先把这个字段所有可能的切分点都算一遍，再按「能不能逐字节写回」
        # 排序。否则 "001002" + {:03x}{:03x} 会被最短的 "0" 抢走，
        # 后面的字段只能把 "01002" 硬读成一个数。
        tried: list[dict[str, Any]] = []
        passed: list[tuple[bool, int, Any, str, str]] = []
        f = seg.spec
        for e in self._candidate_ends(segs, si, pos, state):
            raw = state.text[pos:e]
            clean = _strip_padding(raw, f)
            ok, value, why = _convert(clean, f)
            verified, reproduced, exact, verify_why = False, "", False, why
            if ok:
                verified, reproduced, exact, verify_why = _verify(
                    raw, value, f, self.ignore_case
                )
            tried.append({
                "step": si, "kind": "field", "field": seg.source,
                "at": [pos, e], "raw": raw, "clean": clean,
                "value": _jsonable(value) if ok else None,
                "reproduced": reproduced, "ok": verified,
                "exact": exact, "why": "" if verified else verify_why,
            })
            if not verified:
                state.note(si, e, f"{seg.source} 取 {raw!r}：{verify_why}")
                continue
            passed.append((exact, e, value, raw, clean))
        if not tried:
            why = (f"{seg.source} 从这里开始没有一个字符符合 "
                   f"{f.type or '字符串'} 字段的形状")
            state.note(si, pos, why)
            if state.explain:
                state.trace.append({
                    "step": si, "kind": "field", "field": seg.source,
                    "at": [pos, pos], "raw": "", "clean": "", "value": None,
                    "reproduced": "", "ok": False, "exact": False, "why": why,
                })
        if state.explain:
            state.trace.extend(tried)

        # 可选字段优先当成「出现了」；实在读不通才当它缺失。
        # 其余情况按「能不能逐字节写回」分组，组内从左到右（短到长）试。
        passed.sort(key=lambda c: (seg.optional and c[3] == "", not c[0], c[1]))
        for exact, e, value, raw, _clean in passed:
            key = seg.name if seg.name else str(seg.index)
            spans2 = dict(spans)
            spans2[key] = (pos, e)
            named2, fixed2 = dict(named), dict(fixed)
            if seg.name:
                if seg.name in named2 and named2[seg.name] != value:
                    state.note(si, e, f"重复字段 {seg.name} 前后取值不一致")
                    continue
                named2[seg.name] = value
            else:
                fixed2[seg.index] = value

            # 可选字段取空时，允许把紧跟其后的空白分隔符一起跳过。
            branches: list[tuple[int, int]] = [(si + 1, e)]
            if seg.optional and raw == "" and si + 1 < len(segs):
                nxt = segs[si + 1]
                if isinstance(nxt, _Lit) and nxt.text and nxt.text.isspace():
                    branches.append((si + 2, e))
            for nsi, npos in branches:
                if self._walk(segs, nsi, npos, fixed2, named2, spans2, state):
                    return True
        return False

    def _candidate_ends(self, segs: list[_Segment], si: int, pos: int,
                        state: "_State") -> Iterable[int]:
        text = state.text
        seg = segs[si]
        assert isinstance(seg, Field)
        f = seg.spec
        minlen = 0 if (seg.optional or f.type in ("", "s")) else 1

        # 后面紧跟字面量：字段只能结束在该字面量出现的位置。
        if si + 1 < len(segs) and isinstance(segs[si + 1], _Lit):
            lit = segs[si + 1].text
            if lit:
                found: list[int] = []
                at = pos + minlen
                while True:
                    k = self._find_lit(text, lit, at)
                    if k < 0:
                        break
                    found.append(k)
                    at = k + 1
                    if len(found) > 512:
                        break
                # 可选字段允许「整个字段没出现」：补一个空的切分点，
                # 放在最后试，这样 "{title?} {name}" 读 "Ada" 时才会
                # 先把 title 当作缺失，而不是把 Ada 抢给 title。
                if seg.optional and pos not in found:
                    found.append(pos)
                return found

        hi = len(text)
        shape = self._shape[si]
        if shape is not None:
            m = shape.match(text, pos)
            if m:
                hi = min(hi, m.end())
        # 给后续片段留出最短空间
        reserve = 0
        for k in range(si + 1, len(segs)):
            if isinstance(segs[k], _Lit):
                reserve += len(segs[k].text)
            elif not segs[k].optional and segs[k].spec.type not in ("", "s"):
                reserve += 1
        hi = min(hi, len(text) - reserve if state.require_end else hi)
        lo = pos + minlen
        lo = min(lo, hi + 1)
        ends = range(lo, hi + 1)
        if seg.optional and hi >= pos and lo <= pos:
            ends = [k for k in ends if k > pos] + [pos]
        return ends

    def _find_lit(self, text: str, lit: str, at: int) -> int:
        if not self.ignore_case:
            return text.find(lit, at)
        low_t, low_l = text.casefold(), lit.casefold()
        # casefold 可能改变长度，这里只用于纯 ASCII 字面量；退化时用逐位比较。
        if len(low_t) == len(text):
            return low_t.find(low_l, at)
        for i in range(at, len(text) - len(lit) + 1):
            if text[i:i + len(lit)].casefold() == low_l:
                return i
        return -1


class _State:
    def __init__(self, text: str, start: int, require_end: bool, explain: bool):
        self.text = text
        self.start = start
        self.require_end = require_end
        self.explain = explain
        self.trace: list[dict[str, Any]] = []
        self.attempts = 0
        self.best_reason = "没有匹配"
        self.best_segment = -1
        self.best_pos = -1
        self.end_pos = start
        self.assign_fixed: dict[int, Any] = {}
        self.assign_named: dict[str, Any] = {}
        self.assign_spans: dict[str, tuple[int, int]] = {}

    def note(self, segment: int, pos: int, reason: str) -> None:
        if segment > self.best_segment or (segment == self.best_segment and pos > self.best_pos):
            self.best_segment = segment
            self.best_pos = pos
            self.best_reason = reason


# --------------------------------------------------------------------------
# 模块级便捷函数
# --------------------------------------------------------------------------

def compile(fmt: str, ignore_case: bool = False) -> Parser:  # noqa: A001
    return Parser(fmt, ignore_case=ignore_case)


def parse(fmt: str, text: str, ignore_case: bool = False) -> Result | None:
    return compile(fmt, ignore_case).parse(text)


def search(fmt: str, text: str, ignore_case: bool = False) -> Result | None:
    return compile(fmt, ignore_case).search(text)


def findall(fmt: str, text: str, ignore_case: bool = False) -> Iterator[Result]:
    return compile(fmt, ignore_case).findall(text)


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------

def _print_explain(p: Parser, text: str, res: Result | None,
                   trace: list[dict[str, Any]], failure: Failure) -> None:
    print(f"格式串 : {p.fmt}")
    print(f"输入   : {text!r}")
    print()
    print("逐段候选判定（能逐字节写回的切分点会优先采用）")
    print("-" * 78)
    for t in trace:
        if t["kind"] == "literal":
            print(f"  [{t['step']:>2}] 字面量 {t['text']!r:<28} @{t['at'][0]:>3}  命中")
        else:
            verdict = "通过" if t["ok"] else "不通过"
            tag = "可原样写回" if t.get("exact") else "仅去填充后一致"
            val = repr(t["value"])
            print(f"  [{t['step']:>2}] 字段 {t['field']:<14} @{t['at'][0]:>3}:{t['at'][1]:<3} "
                  f"取 {t['raw']!r}")
            print(f"       去掉填充 {t['clean']!r} -> 值 {val} -> 写回 {t['reproduced']!r}  "
                  f"{verdict}（{tag}）")
            if not t["ok"]:
                print(f"       原因：{t['why']}")
    print("-" * 78)
    if res is not None:
        print("结论   : 匹配成功")
        print(f"  采用切分 : {res.spans}")
        if res.fixed:
            print(f"  按位置 : {res.fixed!r}")
        if res.named:
            print(f"  按名字 : {res.named!r}")
    else:
        print(f"结论   : 没有匹配")
        print(f"  停在第 {failure.segment} 段、第 {failure.position} 个字符：{failure.reason}")
        print("  提示：格式串里的宽度/填充要和写出去时用的那段完全一致；")
        print("        也可以试试 --ignore-case，或用 --explain 看上面每一步的写回结果。")


def _emit(res: Result | None, args: argparse.Namespace, p: Parser,
          text: str) -> int:
    if args.explain:
        res2, trace, failure = p.parse_ex(text)
        _print_explain(p, text, res2, trace, failure)
        return 0 if res2 is not None else 1
    if args.json:
        if res is None:
            print(json.dumps({"ok": False, "text": text}, ensure_ascii=False))
        else:
            print(json.dumps(res.as_dict(), ensure_ascii=False))
    else:
        if res is None:
            print("没有匹配（加 --explain 看原因）")
        else:
            parts: list[str] = []
            for i, v in enumerate(res.fixed):
                parts.append(f"[{i}] = {v!r}")
            for k, v in res.named.items():
                parts.append(f"{k} = {v!r}")
            print("\n".join(parts) if parts else "(没有字段)")
    return 0 if res is not None else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="unformat",
        description="把 format() 写出去的字符串，按同一段格式描述读回成值。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python unformat.py \"It's {}, I love it!\" \"It's spam, I love it!\"\n"
            "  python unformat.py \"{c:02d}\" \"100\"\n"
            "  python unformat.py \"{name:.>16.16}\" \".............Joe\" --explain\n"
            "  echo 'Age: 42' | python unformat.py \"Age: {age:d}\"\n"
        ),
    )
    ap.add_argument("format", help="格式串，例如 'Age: {age:d}'")
    ap.add_argument("text", nargs="?", help="要读的字符串；省略则从标准输入逐行读取")
    ap.add_argument("--explain", action="store_true", help="打印逐字段判定表")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--search", action="store_true", help="在文本里搜索而非整串匹配")
    ap.add_argument("--all", action="store_true", help="找出全部匹配（配合 --search）")
    ap.add_argument("--ignore-case", action="store_true", help="字面量忽略大小写")
    ap.add_argument("--version", action="version", version=f"unformat {__version__}")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    try:
        p = compile(args.format, ignore_case=args.ignore_case)
    except FormatError as exc:
        print(f"格式串有误：{exc}", file=sys.stderr)
        return 2

    # 批量模式：每行独立匹配，编译只做一次。
    if args.text is None:
        rc = 0
        for line in sys.stdin:
            line = line.rstrip("\r\n")
            if args.all:
                hits = list(p.findall(line))
                if args.json:
                    print(json.dumps({"ok": bool(hits),
                                      "matches": [h.as_dict() for h in hits]},
                                     ensure_ascii=False))
                else:
                    for h in hits:
                        print(f"{h.named or h.fixed}")
                if not hits:
                    rc = 1
                continue
            res = p.search(line) if args.search else p.parse(line)
            if args.explain:
                res2, trace, failure = p.parse_ex(line)
                _print_explain(p, line, res2, trace, failure)
                if res2 is None:
                    rc = 1
                continue
            if args.json:
                print(json.dumps(res.as_dict() if res else {"ok": False, "text": line},
                                 ensure_ascii=False))
            else:
                if res is None:
                    print("没有匹配（加 --explain 看原因）")
                else:
                    parts = [f"[{i}] = {v!r}" for i, v in enumerate(res.fixed)]
                    parts += [f"{k} = {v!r}" for k, v in res.named.items()]
                    print("\n".join(parts) if parts else "(没有字段)")
            if res is None:
                rc = 1
        return rc

    if args.all:
        hits = list(p.findall(args.text))
        if args.json:
            print(json.dumps({"ok": bool(hits),
                              "matches": [h.as_dict() for h in hits]},
                             ensure_ascii=False))
        elif args.explain:
            res2, trace, failure = p.parse_ex(args.text)
            _print_explain(p, args.text, res2, trace, failure)
        else:
            for h in hits:
                print(f"{h.named or h.fixed}")
        return 0 if hits else 1

    res = p.search(args.text) if args.search else p.parse(args.text)
    return _emit(res, args, p, args.text)


if __name__ == "__main__":
    sys.exit(main())
