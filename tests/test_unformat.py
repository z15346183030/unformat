#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""unformat 的回归测试。

很多用例是按 format() 的输出反推出来的：先用同一段 spec 把值写出去，
再要求读回来还是那个值。这类用例专门盯住「format 写得出来、却读不回来」
的分叉。
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unformat
from unformat import FormatError, compile, findall, parse, search

ROOT = Path(__file__).resolve().parents[1]


class TestBasics(unittest.TestCase):
    def test_anonymous_field(self):
        r = parse("It's {}, I love it!", "It's spam, I love it!")
        self.assertEqual(r[0], "spam")

    def test_named_field(self):
        r = parse("Age: {age:d}", "Age: 42")
        self.assertEqual(r["age"], 42)
        self.assertEqual(r.fixed, ())

    def test_mixed_fields_keep_positions(self):
        r = parse("{} = {name}", "7 = seven")
        self.assertEqual(r[0], "7")
        self.assertEqual(r["name"], "seven")

    def test_result_spans(self):
        r = parse("Age: {age:d}", "Age: 42")
        self.assertEqual(r.spans["age"], (5, 7))

    def test_repeated_named_field_must_agree(self):
        self.assertEqual(parse("{a:d}-{a:d}", "5-5")["a"], 5)
        self.assertIsNone(parse("{a:d}-{a:d}", "5-6"))

    def test_escaped_braces(self):
        r = parse("{{{v}}}", "{ok}")
        self.assertEqual(r["v"], "ok")

    def test_no_match_returns_none_not_exception(self):
        self.assertIsNone(parse("{:d}", "abc"))

    def test_ignore_case_on_literals(self):
        self.assertIsNotNone(parse("Name: {}", "NAME: Ada", ignore_case=True))
        self.assertIsNone(parse("Name: {}", "NAME: Ada"))

    def test_compile_once_reuse(self):
        p = compile("{n:d}")
        self.assertEqual([p.parse(str(i))["n"] for i in range(5)], [0, 1, 2, 3, 4])

    def test_search_and_findall(self):
        r = search("[{n:d}]", "noise [7] noise")
        self.assertEqual(r["n"], 7)
        hits = list(findall("[{n:d}]", "[1] [22] [333]"))
        self.assertEqual([h["n"] for h in hits], [1, 22, 333])


class TestIssueDriven(unittest.TestCase):
    """每条都对应一个公开讨论过的实际毛病。"""

    def test_issue_142_zero_padded_width_smaller_than_value(self):
        # "{c:02d}".format(c=100) == "100"；宽度是下限，不该截断。
        self.assertEqual(parse("{c:02d}", "100")["c"], 100)
        self.assertEqual(parse("{c:02d}", "04")["c"], 4)

    def test_issue_149_fixed_width_hex_split(self):
        # 两个连续的定宽十六进制字段，不能被最短的 "0" 抢走。
        r = parse("{x:03x}{y:03x}", "001002")
        self.assertEqual((r["x"], r["y"]), (1, 2))

    def test_issue_149_fixed_width_decimal_still_works(self):
        r = parse("{x:03d}{y:03d}", "001002")
        self.assertEqual((r["x"], r["y"]), (1, 2))

    def test_issue_116_explicit_string_type(self):
        self.assertEqual(parse("test_{name:s}", "test_hello")["name"], "hello")

    def test_issue_215_fill_character_not_part_of_value(self):
        self.assertEqual(
            parse("{name:.>16.16}", ".............Joe")["name"], "Joe"
        )

    def test_issue_217_empty_string_field(self):
        r = parse("Hello {}!", "Hello !")
        self.assertEqual(r[0], "")

    def test_issue_254_width_and_precision_together(self):
        # {:10.3} 是合法的 format spec：截到 3 个字符再补到 10 宽。
        r = parse("{:10.3}", "hel       ")
        self.assertEqual(r[0], "hel")

    def test_issue_269_grouped_float(self):
        self.assertAlmostEqual(parse("{:,f}", "-3,494.230000")[0], -3494.23)
        self.assertAlmostEqual(parse("{:,.2f}", "1,234,567.89")[0], 1234567.89)

    def test_issue_269_grouped_percent(self):
        self.assertAlmostEqual(parse("{:,.1%}", "1,234.5%")[0], 12.345)
        self.assertAlmostEqual(parse("{:.1%}", "12.3%")[0], 0.123)

    def test_issue_269_underscore_grouping(self):
        self.assertAlmostEqual(parse("{:_f}", "6_448.400000")[0], 6448.4)

    def test_issue_271_calendar_invalid_date_returns_none(self):
        self.assertIsNone(parse("{:%Y-%m-%d}", "2024-02-30"))

    def test_issue_13_optional_field_present(self):
        r = parse("{honorific?} {given} {sur}", "Mr Richard Jones")
        self.assertEqual(
            (r["honorific"], r["given"], r["sur"]),
            ("Mr", "Richard", "Jones"),
        )

    def test_issue_13_optional_field_absent(self):
        r = parse("{honorific?} {given} {sur}", "Jens Engels")
        self.assertEqual((r["given"], r["sur"]), ("Jens", "Engels"))
        self.assertEqual(r["honorific"], "")

    def test_issue_200_batch_reuses_one_compiled_parser(self):
        # 逐行处理时只编译一次；这里用 200 行验证循环路径。
        p = compile("id={id:d} level={level}")
        rows = [f"id={i} level=INFO" for i in range(200)]
        self.assertEqual([p.parse(s)["id"] for s in rows], list(range(200)))

    def test_contiguous_padded_numbers_from_readme_example(self):
        self.assertEqual(parse("{:2d}{:2d}", " 440").fixed, (4, 40))

    def test_word_type(self):
        r = parse("{:d} {:w}", "42 spam_here")
        self.assertEqual(r.fixed, (42, "spam_here"))
        self.assertIsNone(parse("{:w}", "not-a-word!"))

    def test_datetime_date_and_time(self):
        self.assertEqual(parse("{:%Y-%m-%d}", "2023-11-23")[0], dt.date(2023, 11, 23))
        self.assertEqual(parse("{:%H:%M}", "10:26")[0], dt.time(10, 26))

    def test_datetime_datetime(self):
        r = parse("{:%Y-%m-%d %H:%M}", "2023-11-23 10:26")
        self.assertEqual(r[0], dt.datetime(2023, 11, 23, 10, 26))


class TestExplainAndErrors(unittest.TestCase):
    def test_explain_shows_per_field_trace(self):
        p = compile("{x:03x}{y:03x}")
        res, trace, failure = p.parse_ex("001002")
        self.assertIsNotNone(res)
        fields = [t for t in trace if t["kind"] == "field"]
        self.assertTrue(fields)
        self.assertTrue(any(t["ok"] and t["exact"] for t in fields))

    def test_explain_on_failure_has_reason(self):
        p = compile("{:d}")
        res, trace, failure = p.parse_ex("abc")
        self.assertIsNone(res)
        self.assertTrue(failure.reason)
        self.assertEqual(trace[-1]["ok"], False)

    def test_unclosed_brace(self):
        with self.assertRaises(FormatError):
            compile("oops {name")

    def test_stray_closing_brace(self):
        with self.assertRaises(FormatError):
            compile("oops }")

    def test_bad_precision(self):
        with self.assertRaises(FormatError):
            compile("{:.f}")

    def test_unknown_type(self):
        with self.assertRaises(FormatError):
            compile("{:q}")

    def test_format_without_any_field(self):
        with self.assertRaises(FormatError):
            compile("just text")

    def test_jsonable_date(self):
        r = parse("{:%Y-%m-%d}", "2023-11-23")
        self.assertEqual(r.as_dict()["fixed"], ["2023-11-23"])


class TestRoundTrip(unittest.TestCase):
    """核心不变量：format() 写出去的，用同一段 spec 必须读得回来。

    CASES 里存的是「花括号里面的内容」。写出去用 format(value, spec)，
    读回来用 parse('{' + spec + '}', text)，两边共用同一段描述。
    """

    CASES = [
        ("", "hello"),
        (":s", "hello"),
        (":>12s", "hello"),
        (":<12s", "hello"),
        (":^12s", "hello"),
        (":10.3", "abcdef"),
        (":.3", "abcdef"),
        (":*^12", "hello"),
        (":d", 42),
        (":5d", 42),
        (":05d", 42),
        (":+d", 42),
        (":,d", 1234567),
        (":_d", 1234567),
        (":x", 255),
        (":06x", 255),
        (":#x", 255),
        (":b", 13),
        (":08b", 13),
        (":o", 64),
        (":f", 3.5),
        (":.2f", 3.14159),
        (":08.2f", 3.14159),
        (":,f", -3494.23),
        (":.2e", 1234.5),
        (":g", 7113.95),
        (":g", 0.00012),
        (":.1%", 0.123),
        (":,.1%", 12.345),
        (":%Y-%m-%d", dt.date(2023, 11, 23)),
        (":%Y%m%d", dt.date(2023, 11, 23)),
        (":%H:%M", dt.time(10, 26)),
        (":%Y-%m-%d %H:%M", dt.datetime(2023, 11, 23, 10, 26)),
    ]

    # 有些 spec 本身就是有损的：':.2f' 把 3.14159 写成 '3.14'，
    # ':10.3' 把 'abcdef' 截成 'abc'。这时候要求「读回原值」是不讲道理的，
    # 真正该保证的是「读到的值再按同一段 spec 写回去，还是原来那串文本」。
    LOSSLESS = [
        ("", "hello"), (":s", "hello"), (":>12s", "hello"),
        (":<12s", "hello"), (":^12s", "hello"), (":*^12", "hello"),
        (":d", 42), (":5d", 42), (":05d", 42), (":+d", 42),
        (":,d", 1234567), (":_d", 1234567), (":x", 255),
        (":06x", 255), (":#x", 255), (":b", 13), (":08b", 13),
        (":o", 64), (":f", 3.5), (":,f", -3494.23),
        (":g", 7113.95), (":g", 0.00012),
        (":.1%", 0.123), (":,.1%", 12.345),
        (":%Y-%m-%d", dt.date(2023, 11, 23)),
        (":%H:%M", dt.time(10, 26)),
        (":%Y-%m-%d %H:%M", dt.datetime(2023, 11, 23, 10, 26)),
    ]

    def test_every_case_reproduces_the_same_text(self):
        failures = []
        for spec, value in self.CASES:
            # 花括号里的 ":s" 交给 format() 时要去掉那个冒号。
            writable = spec[1:] if spec.startswith(":") else spec
            text = format(value, writable)
            r = parse("{" + spec + "}", text)
            if r is None:
                failures.append((spec, value, text, "None"))
                continue
            again = format(r[0], writable)
            if again != text:
                failures.append((spec, value, text, again))
        self.assertEqual(failures, [], f"不能复原原文的用例: {failures}")

    def test_lossless_specs_return_the_original_value(self):
        failures = []
        for spec, value in self.LOSSLESS:
            writable = spec[1:] if spec.startswith(":") else spec
            r = parse("{" + spec + "}", format(value, writable))
            if r is None or not _same(r[0], value):
                failures.append((spec, value, None if r is None else r[0]))
        self.assertEqual(failures, [], f"无损 spec 读回的值不对: {failures}")

    def test_case_count_is_meaningful(self):
        self.assertGreaterEqual(len(self.CASES), 30)


def _same(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) < 1e-9
    return type(a) is type(b) and a == b


class TestCLI(unittest.TestCase):
    def _run(self, *args, stdin=None):
        return subprocess.run(
            [sys.executable, str(ROOT / "unformat.py"), *args],
            input=stdin, text=True, capture_output=True, encoding="utf-8",
        )

    def test_simple(self):
        p = self._run("Age: {age:d}", "Age: 42")
        self.assertEqual(p.returncode, 0)
        self.assertIn("age = 42", p.stdout)

    def test_issue_142_cli(self):
        p = self._run("{c:02d}", "100")
        self.assertEqual(p.returncode, 0)
        self.assertIn("c = 100", p.stdout)

    def test_json_output(self):
        p = self._run("{n:d}", "7", "--json")
        self.assertEqual(json.loads(p.stdout)["named"]["n"], 7)

    def test_json_on_failure(self):
        p = self._run("{n:d}", "abc", "--json")
        self.assertEqual(json.loads(p.stdout)["ok"], False)
        self.assertEqual(p.returncode, 1)

    def test_explain_output(self):
        p = self._run("{name:.>16.16}", ".............Joe", "--explain")
        self.assertEqual(p.returncode, 0)
        self.assertIn("逐段候选判定", p.stdout)
        self.assertIn("匹配成功", p.stdout)

    def test_stdin_batch(self):
        p = self._run("{n:d}", stdin="1\n22\n333\n")
        lines = p.stdout.strip().splitlines()
        self.assertEqual([s.split(" = ")[1] for s in lines], ["1", "22", "333"])

    def test_stdin_batch_json(self):
        p = self._run("--json", "{n:d}", stdin="1\n22\n")
        self.assertEqual([json.loads(s)["named"]["n"] for s in p.stdout.splitlines()],
                         [1, 22])

    def test_bad_format_exits_2(self):
        p = self._run("{:q}", "x")
        self.assertEqual(p.returncode, 2)
        self.assertIn("格式串有误", p.stderr)

    def test_version(self):
        p = self._run("--version")
        self.assertIn(unformat.__version__, p.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
