"""Tests for the plan-only drill validator. Run: python3 -m unittest discover -s drills -p test_validate_plan.py"""

import ast
from contextlib import redirect_stderr, redirect_stdout
import os
import stat
import tempfile
import threading
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import validate_plan as v  # noqa: E402

EXAMPLE = HERE / "plans" / "vendor-severance-72h.yaml"
INVALID_DIR = HERE / "plans" / "invalid"

EXPECTED_FIXTURE_CODES = {
    "agent-operator.yaml": "role_not_human",
    "data-plane-severed.yaml": "plane",
    "duplicate-key.yaml": "duplicate_key",
    "duration-mismatch.yaml": "duration_mismatch",
    "executable-command.yaml": "executable_field",
    "execute-mode.yaml": "mode",
    "naive-start-time.yaml": "time",
    "nonfinite-threshold.yaml": "nonfinite",
    "operator-self-check.yaml": "check_not_independent",
    "production-environment.yaml": "production_refused",
    "result-imported.yaml": "result_field",
    "same-person-two-roles.yaml": "roles_not_distinct",
}


def example_plan():
    return yaml.safe_load(EXAMPLE.read_text())


def run(plan):
    return v.validate_bytes(yaml.safe_dump(plan, sort_keys=False).encode())


def codes(report):
    return [r["code"] for r in report["reasons"]]


class ValidPlan(unittest.TestCase):
    def test_example_is_valid_not_run(self):
        report = v.validate_path(EXAMPLE)
        self.assertEqual(report["status"], v.VALID, report["reasons"])
        self.assertEqual(report["status"], "plan_valid_not_run")
        self.assertEqual(report["reasons"], [])
        self.assertRegex(report["plan_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(any("no drill was run" in s for s in report["limitations"]))
        self.assertTrue(any("not authenticated" in s for s in report["limitations"]))

    def test_offset_times_are_converted_to_utc(self):
        plan = example_plan()
        plan["window"]["start"] = "2026-12-01T11:00:00+02:00"
        plan["window"]["end"] = "2026-12-04T01:00:00-08:00"
        self.assertEqual(run(plan)["status"], v.VALID)

    def test_verifier_role_is_optional_when_no_check_uses_it(self):
        plan = example_plan()
        del plan["roles"]["verifier"]
        for check in plan["recovery_checks"]:
            check["checked_by_role"] = "approver"
        self.assertEqual(run(plan)["status"], v.VALID)


class Fixtures(unittest.TestCase):
    def test_every_fixture_fails_for_its_own_reason_only(self):
        found = sorted(p.name for p in INVALID_DIR.glob("*.yaml"))
        self.assertEqual(found, sorted(EXPECTED_FIXTURE_CODES))
        for name, code in EXPECTED_FIXTURE_CODES.items():
            with self.subTest(name=name):
                report = v.validate_path(INVALID_DIR / name)
                self.assertEqual(report["status"], v.INVALID)
                self.assertEqual(codes(report), [code], report["reasons"])


class StrictInput(unittest.TestCase):
    def test_malformed_yaml(self):
        report = v.validate_bytes(b"schema_version: [unclosed\n")
        self.assertEqual((report["status"], codes(report)), (v.INVALID, ["yaml_syntax"]))
        self.assertIsNone(report["plan_sha256"])

    def test_not_utf8(self):
        self.assertEqual(codes(v.validate_bytes(b"\xff\xfe\x00")), ["encoding"])

    def test_two_documents(self):
        self.assertEqual(codes(v.validate_bytes(b"a: 1\n---\nb: 2\n")), ["yaml_documents"])

    def test_empty_file(self):
        self.assertEqual(codes(v.validate_bytes(b"")), ["yaml_documents"])

    def test_nested_duplicate_key(self):
        data = EXAMPLE.read_bytes().replace(b"  max_restore_minutes: 60\n",
                                            b"  max_restore_minutes: 60\n  max_restore_minutes: 5\n")
        self.assertEqual(codes(v.validate_bytes(data)), ["duplicate_key"])

    def test_anchor_and_alias_refused(self):
        self.assertEqual(codes(v.validate_bytes(b"a: &x 1\nb: *x\n")), ["yaml_anchor"])
        self.assertIn(codes(v.validate_bytes(b"a: [1]\nb: *x\n"))[0], ("yaml_alias", "yaml_syntax"))

    def test_python_tags_refused(self):
        report = v.validate_bytes(b"a: !!python/object/apply:os.system ['true']\n")
        self.assertEqual(codes(report), ["yaml_syntax"])

    def test_nan_and_negative_inf(self):
        for literal in (b".nan", b"-.inf", b".Inf"):
            with self.subTest(literal=literal):
                data = EXAMPLE.read_bytes().replace(b"threshold: 5\n", b"threshold: " + literal + b"\n")
                self.assertEqual(codes(v.validate_bytes(data)), ["nonfinite"])

    def test_non_string_key(self):
        self.assertEqual(codes(v.validate_bytes(b"1: a\n")), ["key_type"])

    def test_file_too_large(self):
        data = b"# " + b"x" * v.MAX_FILE_BYTES + b"\n"
        self.assertEqual(codes(v.validate_bytes(data)), ["file_too_large"])

    def test_too_many_items(self):
        plan = example_plan()
        plan["preconditions"] = [{"id": f"p{i}", "description": "d"} for i in range(v.MAX_ITEMS + 1)]
        self.assertEqual(codes(run(plan)), ["too_many_items"])

    def test_too_deep(self):
        nested = "x"
        for _ in range(v.MAX_DEPTH + 2):
            nested = [nested]
        plan = example_plan()
        plan["notes"] = nested
        self.assertEqual(codes(run(plan)), ["too_deep"])

    def test_string_too_long(self):
        plan = example_plan()
        plan["title"] = "t" * (v.MAX_STRING + 1)
        self.assertEqual(codes(run(plan)), ["string_too_long"])

    def test_huge_integer(self):
        plan = example_plan()
        plan["restore"]["max_restore_minutes"] = 10 ** 30
        self.assertEqual(codes(run(plan)), ["number_out_of_range"])

    def test_boolean_is_not_a_number(self):
        plan = example_plan()
        plan["restore"]["max_restore_minutes"] = True
        self.assertEqual(codes(run(plan)), ["number"])

    def test_unquoted_timestamp_stays_text_and_needs_offset(self):
        data = EXAMPLE.read_bytes().replace(b'start: "2026-12-01T09:00:00Z"', b"start: 2026-12-01 09:00:00")
        self.assertEqual(codes(v.validate_bytes(data)), ["time"])


class Rules(unittest.TestCase):
    def mutate(self, change):
        plan = example_plan()
        change(plan)
        return codes(run(plan))

    def test_unknown_field(self):
        self.assertEqual(self.mutate(lambda p: p.update(owner="x")), ["unknown_field"])

    def test_missing_required(self):
        self.assertEqual(self.mutate(lambda p: p.pop("restore")), ["missing"])

    def test_wrong_schema_version(self):
        self.assertEqual(self.mutate(lambda p: p.update(schema_version="drill-plan/v2")), ["schema_version"])

    def test_isolated_lab_allowed_other_environment_refused(self):
        self.assertEqual(self.mutate(lambda p: p["environment"].update({"class": "isolated-lab"})), [])
        self.assertEqual(self.mutate(lambda p: p["environment"].update({"class": "dev"})), ["environment"])
        self.assertEqual(self.mutate(lambda p: p["environment"].update({"class": "Prod-EU"})), ["production_refused"])

    def test_other_scenario_refused(self):
        self.assertEqual(self.mutate(lambda p: p["scenario"].update(kind="revoked-keys")), ["scenario"])

    def test_link_both_severed_and_excluded(self):
        def change(p):
            p["scenario"]["excluded_links"][0]["name"] = "vendor-management-api"
        self.assertEqual(self.mutate(change), ["link_conflict"])

    def test_window_end_before_start(self):
        def change(p):
            p["window"]["end"] = "2026-11-30T09:00:00Z"
        self.assertEqual(self.mutate(change), ["window_order"])

    def test_zero_and_negative_duration(self):
        for hours in (0, -72):
            with self.subTest(hours=hours):
                self.assertEqual(self.mutate(lambda p: p["window"].update(duration_hours=hours)), ["not_positive"])

    def test_duration_too_long(self):
        def change(p):
            p["window"]["end"] = "2027-01-02T09:00:00Z"
            p["window"]["duration_hours"] = 768
        self.assertEqual(self.mutate(change), ["duration_too_long"])

    def test_date_only_time_refused(self):
        self.assertEqual(self.mutate(lambda p: p["window"].update(start="2026-12-01")), ["time"])

    def test_bad_comparator(self):
        self.assertEqual(self.mutate(lambda p: p["abort_criteria"][0].update(comparator="==")), ["comparator"])

    def test_abort_criteria_must_be_measurable(self):
        def change(p):
            del p["abort_criteria"][0]["threshold"]
        self.assertEqual(self.mutate(change), ["missing"])
        self.assertEqual(self.mutate(lambda p: p["abort_criteria"][0].update(threshold="high")), ["number"])
        self.assertEqual(self.mutate(lambda p: p.update(abort_criteria=[])), ["list"])

    def test_duplicate_ids(self):
        self.assertEqual(self.mutate(lambda p: p["preconditions"][1].update(id="pre-backup")), ["duplicate_id"])

    def test_role_reference_with_agent_prefix(self):
        def change(p):
            p["roles"]["operator"]["reference"] = "agent:claude-code"
        self.assertEqual(self.mutate(change), ["role_not_human"])

    def test_role_comparison_ignores_case_and_spaces(self):
        def change(p):
            p["roles"]["verifier"]["reference"] = "  Approver@Example.invalid "
        self.assertEqual(self.mutate(change), ["roles_not_distinct"])

    def test_check_by_undeclared_role(self):
        def change(p):
            del p["roles"]["verifier"]
        self.assertEqual(self.mutate(change), ["role_undeclared", "role_undeclared"])

    def test_executable_field_anywhere(self):
        for where, change in {
            "top": lambda p: p.update(script="restore.sh"),
            "scenario": lambda p: p["scenario"].update(inject="iptables"),
            "link": lambda p: p["scenario"]["severed_links"][0].update(endpoint="https://vendor.example"),
            "check": lambda p: p["recovery_checks"][0].update(URL="https://x.example"),
        }.items():
            with self.subTest(where=where):
                self.assertEqual(self.mutate(change), ["executable_field"])

    def test_wrong_evidence_template(self):
        self.assertEqual(self.mutate(lambda p: p["evidence"].update(template="other")), ["evidence_template"])

    def test_reasons_are_field_level_and_sorted(self):
        def change(p):
            p["mode"] = "execute"
            p["environment"]["class"] = "production"
            p["roles"]["operator"]["kind"] = "agent"
        plan = example_plan()
        change(plan)
        report = run(plan)
        paths = [r["path"] for r in report["reasons"]]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(set(paths), {"mode", "environment.class", "roles.operator.kind"})


class Identity(unittest.TestCase):
    def test_digest_ignores_key_order_quoting_and_comments(self):
        plan = example_plan()
        reordered = dict(reversed(list(plan.items())))
        text = "# a comment\n" + yaml.safe_dump(reordered, sort_keys=False, default_style='"')
        self.assertEqual(v.validate_bytes(text.encode())["plan_sha256"], v.validate_path(EXAMPLE)["plan_sha256"])

    def test_digest_changes_with_content(self):
        plan = example_plan()
        before = run(plan)["plan_sha256"]
        plan["restore"]["max_restore_minutes"] = 61
        self.assertNotEqual(run(plan)["plan_sha256"], before)

    def test_digest_is_sha256_of_canonical_json(self):
        plan = example_plan()
        canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        self.assertEqual(v.plan_digest(plan), hashlib.sha256(canonical.encode()).hexdigest())

    def test_report_is_deterministic(self):
        first = v.validate_path(INVALID_DIR / "production-environment.yaml")
        second = v.validate_path(INVALID_DIR / "production-environment.yaml")
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_invalid_plan_still_gets_identity(self):
        report = v.validate_path(INVALID_DIR / "production-environment.yaml")
        self.assertRegex(report["plan_sha256"], r"^[0-9a-f]{64}$")


class Safety(unittest.TestCase):
    def test_recovery_from_invalid_to_corrected(self):
        bad = (INVALID_DIR / "production-environment.yaml").read_bytes()
        self.assertEqual(v.validate_bytes(bad)["status"], v.INVALID)
        fixed = bad.replace(b"  class: production\n", b"  class: staging\n")
        self.assertEqual(v.validate_bytes(fixed)["status"], v.VALID)

    def test_plan_bytes_unchanged(self):
        paths = [EXAMPLE, *sorted(INVALID_DIR.glob("*.yaml"))]
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
        for p in paths:
            v.validate_path(p)
        with redirect_stdout(io.StringIO()):
            v.main([str(p) for p in paths])
        for p in paths:
            self.assertEqual((p.read_bytes(), p.stat().st_mtime_ns), before[p])

    def test_parsed_plan_is_not_mutated(self):
        plan = example_plan()
        snapshot = copy.deepcopy(plan)
        v._check(plan)
        self.assertEqual(plan, snapshot)

    def test_no_successful_execution_status_is_possible(self):
        statuses = {v.validate_path(p)["status"] for p in [EXAMPLE, *INVALID_DIR.glob("*.yaml")]}
        self.assertEqual(statuses, {v.VALID, v.INVALID})
        for field in ("status", "result", "passed", "outcome", "verdict"):
            with self.subTest(field=field):
                plan = example_plan()
                plan[field] = "passed"
                report = run(plan)
                self.assertEqual(report["status"], v.INVALID)
                self.assertEqual(codes(report), ["result_field"])

    def test_no_execution_or_network_imports(self):
        tree = ast.parse((HERE / "validate_plan.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(imported, {"__future__", "datetime", "hashlib", "json", "math", "os", "pathlib",
                                    "stat", "sys", "yaml"})
        called = {node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
                  for node in ast.walk(tree) if isinstance(node, ast.Call)}
        for name in ("eval", "exec", "compile", "system", "popen", "spawnv", "execv", "fork", "__import__",
                     "unsafe_load", "full_load"):
            self.assertNotIn(name, called)
        os_calls = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute) and getattr(node.func.value, "id", None) == "os"}
        self.assertEqual(os_calls, {"stat", "open", "fstat", "read", "close"})

    def test_unreadable_path(self):
        report = v.validate_path(HERE / "plans" / "does-not-exist.yaml")
        self.assertEqual((report["status"], codes(report)), (v.INVALID, ["unreadable"]))


class CodexReview5bcd360(unittest.TestCase):
    """Regressions for Codex's isolated review of 5bcd360: every input gets a JSON refusal, never a traceback or a hang."""

    def assert_refused(self, report, code):
        self.assertEqual((report["status"], codes(report)), (v.INVALID, [code]), report["reasons"])
        json.dumps(report, allow_nan=False)  # always serialisable
        self.assertLess(len(json.dumps(report)), 4096)  # bounded: no input is echoed at length

    def test_integer_past_python_conversion_limit(self):
        data = EXAMPLE.read_bytes().replace(b"threshold: 5\n", b"threshold: " + b"9" * 5000 + b"\n")
        self.assertLess(len(data), v.MAX_FILE_BYTES)
        self.assert_refused(v.validate_bytes(data), "yaml_value")

    def test_integer_under_conversion_limit_still_out_of_range(self):
        data = EXAMPLE.read_bytes().replace(b"threshold: 5\n", b"threshold: " + b"9" * 4000 + b"\n")
        self.assert_refused(v.validate_bytes(data), "number_out_of_range")

    def test_huge_integer_via_cli_gives_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "huge.yaml"
            path.write_bytes(EXAMPLE.read_bytes().replace(b"threshold: 5\n", b"threshold: " + b"9" * 5000 + b"\n"))
            code, out = Cli.call(self, [str(path)])
        self.assertEqual(code, 1)
        self.assertEqual(codes(json.loads(out)["reports"][0]), ["yaml_value"])

    def test_times_that_overflow_when_converted_to_utc(self):
        for key, value in (("start", "0001-01-01T00:30:00+23:00"), ("end", "9999-12-31T23:30:00-23:00")):
            with self.subTest(value=value):
                plan = example_plan()
                plan["window"][key] = value
                self.assert_refused(run(plan), "time_out_of_range")

    def test_times_outside_supported_years(self):
        for start, end in (("1999-12-01T09:00:00Z", "1999-12-04T09:00:00Z"),
                           ("2101-12-01T09:00:00Z", "2101-12-04T09:00:00Z")):
            with self.subTest(start=start):
                plan = example_plan()
                plan["window"].update(start=start, end=end)
                report = run(plan)
                self.assertEqual(codes(report), ["time_out_of_range", "time_out_of_range"])

    def test_named_pipe_is_refused_without_blocking(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("no FIFOs on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            fifo = Path(tmp) / "plan.yaml"
            os.mkfifo(fifo)
            box = {}
            worker = threading.Thread(target=lambda: box.update(report=v.validate_path(fifo)), daemon=True)
            worker.start()
            worker.join(timeout=2)
            if worker.is_alive():  # unblock the reader so the test process can still exit
                fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                os.close(fd)
                self.fail("validate_path blocked on a FIFO")
        self.assert_refused(box["report"], "not_regular_file")

    def test_devices_and_directories_are_not_opened(self):
        for path in (Path(os.devnull), HERE / "plans"):
            with self.subTest(path=str(path)):
                self.assert_refused(v.validate_path(path), "not_regular_file")

    def test_file_that_grows_after_the_size_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "growing.yaml"
            path.write_bytes(b"#" * (v.MAX_FILE_BYTES + 100))
            real_stat = os.stat

            def stale_stat(p, *args, **kwargs):  # the size seen before the file grew
                fields = list(real_stat(p, *args, **kwargs))
                fields[stat.ST_SIZE] = 10
                return os.stat_result(fields)

            original = v.os.stat
            v.os.stat = stale_stat
            try:
                report = v.validate_path(path)
            finally:
                v.os.stat = original
        self.assert_refused(report, "file_too_large")

    def test_file_swapped_between_check_and_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp) / "other.yaml"
            other.write_bytes(b"a: 1\n")
            original = v.os.stat
            v.os.stat = lambda p, *a, **k: original(other)
            try:
                report = v.validate_path(EXAMPLE)
            finally:
                v.os.stat = original
        self.assert_refused(report, "file_changed")

    def test_long_explicit_key(self):
        data = b"? " + b"k" * 3000 + b"\n: value\n"
        self.assert_refused(v.validate_bytes(data), "key_too_long")

    def test_key_just_over_limit(self):
        plan = example_plan()
        plan["k" * (v.MAX_KEY + 1)] = "x"
        self.assert_refused(run(plan), "key_too_long")
        plan = example_plan()
        plan["k" * v.MAX_KEY] = "x"
        self.assertEqual(codes(run(plan)), ["unknown_field"])

    def test_corrected_inputs_recover_to_valid(self):
        good = EXAMPLE.read_bytes()
        broken = {
            "huge integer": good.replace(b"threshold: 5\n", b"threshold: " + b"9" * 5000 + b"\n"),
            "overflowing time": good.replace(b'start: "2026-12-01T09:00:00Z"', b'start: "0001-01-01T00:30:00+23:00"'),
            "long key": good.replace(b"evidence:\n", b"? " + b"k" * 3000 + b"\n: x\nevidence:\n"),
        }
        for name, data in broken.items():
            with self.subTest(name=name):
                self.assertEqual(v.validate_bytes(data)["status"], v.INVALID)
        self.assertEqual(v.validate_bytes(good)["status"], v.VALID)

    def test_no_regression_reaches_the_internal_error_fallback(self):
        inputs = [b"9" * 5000, b"a: " + b"9" * 5000, b"? " + b"k" * 3000 + b"\n: v\n",
                  b"window: {start: '0001-01-01T00:30:00+23:00'}\n"]
        for data in inputs:
            with self.subTest(data=data[:20]):
                self.assertNotIn("internal_error", codes(v.validate_bytes(data)))


class Cli(unittest.TestCase):
    def call(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = v.main(args)
        return code, out.getvalue()

    def test_valid_exits_zero(self):
        code, out = self.call([str(EXAMPLE)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["reports"][0]["status"], v.VALID)

    def test_any_invalid_exits_one(self):
        code, out = self.call([str(EXAMPLE), str(INVALID_DIR / "agent-operator.yaml")])
        self.assertEqual(code, 1)
        self.assertEqual([r["status"] for r in json.loads(out)["reports"]], [v.VALID, v.INVALID])

    def test_usage_exits_two(self):
        self.assertEqual(self.call([])[0], 2)


if __name__ == "__main__":
    unittest.main()
