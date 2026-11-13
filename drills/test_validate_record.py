"""Tests for validate_record.py. Synthetic inputs only; no network, no cluster."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

import validate_plan as vp
import validate_record as vr

HERE = Path(__file__).resolve().parent
PLAN = HERE / "plans" / "vendor-severance-72h.yaml"
RECORDS = HERE / "records"
COMPLETE = RECORDS / "vendor-severance-72h-complete.yaml"
ABORTED = RECORDS / "vendor-severance-72h-aborted.yaml"
NOT_OBSERVED = RECORDS / "vendor-severance-72h-not-observed.yaml"
NOT_RUN = RECORDS / "vendor-severance-72h-not-run.yaml"
PLAN_BYTES = PLAN.read_bytes()


def base():
    return yaml.safe_load(COMPLETE.read_bytes())


def dump(obj) -> bytes:
    return yaml.safe_dump(obj, sort_keys=False).encode()


def check(record, plan_bytes=PLAN_BYTES):
    data = record if isinstance(record, bytes) else dump(record)
    return vr.validate(plan_bytes, data, "plan.yaml", "record.yaml")


def codes(report, cls=None):
    return sorted((r["path"], r["code"]) for r in report["reasons"] if cls is None or r["class"] == cls)


class Fixtures(unittest.TestCase):
    def test_complete_record_is_complete_not_judged(self):
        r = vr.validate_paths(PLAN, COMPLETE)
        self.assertEqual(r["status"], vr.COMPLETE)
        self.assertEqual(r["reasons"], [])
        self.assertEqual(r["declared_outcome"], "completed_as_planned")
        self.assertEqual(r["derived"], {"restore_minutes": 42.0, "plan_max_restore_minutes": 60,
                                        "restore_exceeds_plan_max": False})
        self.assertEqual(r["plan"]["plan_sha256"], "c299b94ce2326a174a1ab561ec57bc4fdc8a504e5e1ce531ee897f4ef76a8914")

    def test_aborted_and_failed_check_is_complete_and_never_upgraded(self):
        r = vr.validate_paths(PLAN, ABORTED)
        self.assertEqual(r["status"], vr.COMPLETE)
        self.assertEqual(r["declared_outcome"], "aborted")
        self.assertTrue(r["derived"]["restore_exceeds_plan_max"])  # a fact, not a status
        text = json.dumps(r).lower()
        for word in ("passed", "success", "succeeded", "compliant", "accepted"):
            self.assertNotIn(word, text)

    def test_explicitly_not_observed_and_missing_entries_are_incomplete(self):
        r = vr.validate_paths(PLAN, NOT_OBSERVED)
        self.assertEqual(r["status"], vr.INCOMPLETE)
        self.assertEqual(codes(r), [
            ("abort_criteria[2].crossed", "not_observed"),
            ("abort_criteria[2].observed_extreme", "not_observed"),
            ("preconditions", "missing_evidence"),
            ("recovery_checks[1].at", "not_observed"),
            ("recovery_checks[1].state", "not_observed"),
        ])

    def test_not_run_reports_every_missing_observation_and_derives_nothing(self):
        r = vr.validate_paths(PLAN, NOT_RUN)
        self.assertEqual(r["status"], vr.INCOMPLETE)
        self.assertEqual(r["declared_outcome"], "not_run")
        self.assertEqual({x["class"] for x in r["reasons"]}, {vr.INCOMPLETE})
        self.assertEqual(len(r["reasons"]), 20)
        self.assertIsNone(r["derived"]["restore_minutes"])
        self.assertIsNone(r["derived"]["restore_exceeds_plan_max"])


class Binding(unittest.TestCase):
    def test_other_plan_hash_is_mismatch(self):
        rec = base()
        rec["plan"]["plan_sha256"] = "0" * 64
        r = check(rec)
        self.assertEqual(r["status"], vr.MISMATCH)
        self.assertEqual(codes(r), [("plan.plan_sha256", "plan_sha256_mismatch")])

    def test_reformatted_plan_same_canonical_content_still_mismatches_bytes(self):
        reformatted = b"# a comment changes the bytes, not the plan\n" + PLAN_BYTES
        self.assertEqual(vp.validate_bytes(reformatted)["plan_sha256"], vp.validate_bytes(PLAN_BYTES)["plan_sha256"])
        r = check(base(), reformatted)
        self.assertEqual(r["status"], vr.MISMATCH)
        self.assertEqual(codes(r), [("plan.file_sha256", "file_sha256_mismatch")])
        self.assertIn("canonical content matches", r["reasons"][0]["message"])

    def test_changed_plan_values_mismatch_both_hashes(self):
        changed = PLAN_BYTES.replace(b"max_restore_minutes: 60", b"max_restore_minutes: 90")
        r = check(base(), changed)
        self.assertEqual(codes(r), [("plan.file_sha256", "file_sha256_mismatch"), ("plan.plan_sha256", "plan_sha256_mismatch")])
        self.assertNotIn("canonical content matches", json.dumps(r))

    def test_window_and_plan_id_mismatch(self):
        rec = base()
        rec["window"]["end"] = "2026-12-04T10:00:00Z"
        rec["plan"]["plan_id"] = "another-plan"
        self.assertEqual(codes(check(rec)), [("plan.plan_id", "plan_id_mismatch"), ("window.end", "window_mismatch")])

    def test_window_in_another_offset_naming_the_same_instant_matches(self):
        rec = base()
        rec["window"]["start"] = "2026-12-01T10:00:00+01:00"
        self.assertEqual(check(rec)["status"], vr.COMPLETE)

    def test_invalid_plan_makes_the_report_invalid(self):
        bad = PLAN_BYTES.replace(b"class: staging", b"class: production")
        r = check(base(), bad)
        self.assertEqual(r["status"], vr.INVALID)
        self.assertIn(("(file)", "plan_not_valid"), codes(r))
        self.assertIn(("environment.class", "production_refused"), codes(r))
        self.assertTrue(all(x["input"] == "plan" for x in r["reasons"]))


class References(unittest.TestCase):
    def test_unknown_criterion_and_check_ids_are_mismatch(self):
        rec = base()
        rec["abort_criteria"][0]["id"] = "abort-made-up"
        rec["recovery_checks"][0]["id"] = "check-made-up"
        r = check(rec)
        self.assertEqual(r["status"], vr.MISMATCH)
        self.assertEqual(codes(r, vr.MISMATCH), [("abort_criteria[0].id", "unknown_reference"),
                                                 ("recovery_checks[0].id", "unknown_reference")])
        # The plan's own ids are then missing too, and both kinds are retained.
        self.assertEqual(codes(r, vr.INCOMPLETE), [("abort_criteria", "missing_evidence"), ("recovery_checks", "missing_evidence")])

    def test_duplicate_reference_is_mismatch(self):
        rec = base()
        rec["preconditions"].append(copy.deepcopy(rec["preconditions"][0]))
        self.assertEqual(codes(check(rec)), [("preconditions[3].id", "duplicate_reference")])

    def test_sign_off_reference_must_match_the_plan(self):
        rec = base()
        rec["sign_offs"]["approver"]["reference"] = "someone-else@example.invalid"
        self.assertEqual(codes(check(rec)), [("sign_offs.approver.reference", "role_reference_mismatch")])

    def test_sign_off_reference_ignores_case_and_spaces_like_the_plan(self):
        rec = base()
        rec["sign_offs"]["approver"]["reference"] = "  Approver@Example.invalid "
        self.assertEqual(check(rec)["status"], vr.COMPLETE)

    def test_missing_sign_off_is_incomplete(self):
        rec = base()
        del rec["sign_offs"]["verifier"]
        self.assertEqual(codes(check(rec)), [("sign_offs.verifier", "missing_evidence")])

    def test_role_the_plan_does_not_declare_is_mismatch(self):
        plan = yaml.safe_load(PLAN_BYTES)
        del plan["roles"]["verifier"]
        for chk in plan["recovery_checks"]:
            chk["checked_by_role"] = "approver"
        plan_bytes = dump(plan)
        self.assertEqual(vp.validate_bytes(plan_bytes)["status"], vp.VALID)
        rec = base()
        digest = vp.validate_bytes(plan_bytes)
        rec["plan"].update(plan_sha256=digest["plan_sha256"], file_sha256=digest["file_sha256"])
        r = check(rec, plan_bytes)
        self.assertIn(("sign_offs.verifier", "role_undeclared"), codes(r))
        self.assertIn(("recovery_checks[0].checked_by_role", "check_role_mismatch"), codes(r))

    def test_operator_may_not_record_a_recovery_check(self):
        rec = base()
        rec["recovery_checks"][0]["checked_by_reference"] = "operator@example.invalid"
        self.assertEqual(codes(check(rec)), [("recovery_checks[0].checked_by_reference", "check_not_independent")])

    def test_check_role_must_match_the_plan(self):
        rec = base()
        rec["recovery_checks"][2]["checked_by_role"] = "verifier"
        rec["recovery_checks"][2]["checked_by_reference"] = "verifier@example.invalid"
        self.assertEqual(codes(check(rec)), [("recovery_checks[2].checked_by_role", "check_role_mismatch")])


class Timeline(unittest.TestCase):
    def test_restore_and_recovery_after_the_window_closes_are_ordinary(self):
        rec = base()
        rec["timeline"]["restore_finished"] = "2026-12-04T11:30:00Z"   # past the window, past the plan's limit
        rec["recovery_checks"][2]["at"] = "2026-12-06T12:00:00Z"
        r = check(rec)
        self.assertEqual(r["status"], vr.COMPLETE)
        self.assertEqual(r["derived"]["restore_minutes"], 145.0)
        self.assertTrue(r["derived"]["restore_exceeds_plan_max"])

    def test_preconditions_may_be_confirmed_before_the_window(self):
        rec = base()
        rec["timeline"]["preconditions_confirmed"] = "2026-11-30T09:00:00Z"
        self.assertEqual(check(rec)["status"], vr.COMPLETE)

    def test_drill_observations_outside_the_window_are_mismatch(self):
        rec = base()
        rec["timeline"]["links_severed"] = "2026-12-01T08:00:00Z"
        rec["timeline"]["preconditions_confirmed"] = "2026-12-01T07:00:00Z"
        rec["timeline"]["ended"] = "2026-12-04T09:00:01Z"
        rec["timeline"]["restore_started"] = "2026-12-04T09:05:00Z"
        self.assertEqual(codes(check(rec)), [("timeline.ended", "outside_window"), ("timeline.links_severed", "outside_window")])

    def test_out_of_order_events_are_mismatch(self):
        rec = base()
        rec["timeline"]["restore_started"] = "2026-12-04T08:00:00Z"
        self.assertEqual(codes(check(rec)), [("timeline.restore_started", "timeline_order")])

    def test_recovery_check_before_restore_started_is_mismatch(self):
        rec = base()
        rec["recovery_checks"][0]["at"] = "2026-12-04T09:01:00Z"
        self.assertEqual(codes(check(rec)), [("recovery_checks[0].at", "check_before_restore")])

    def test_malformed_and_naive_times_are_invalid(self):
        rec = base()
        rec["timeline"]["ended"] = "2026-12-04T09:00:00"
        rec["sign_offs"]["operator"]["at"] = "yesterday"
        rec["recovery_checks"][0]["at"] = "9999-12-31T23:00:00-23:00"
        r = check(rec)
        self.assertEqual(r["status"], vr.INVALID)
        self.assertEqual(codes(r, vr.INVALID), [("recovery_checks[0].at", "time_out_of_range"),
                                                ("sign_offs.operator.at", "time"), ("timeline.ended", "time")])


class Outcome(unittest.TestCase):
    def test_aborted_without_naming_a_criterion_is_incomplete(self):
        rec = yaml.safe_load(ABORTED.read_bytes())
        del rec["outcome"]["abort_criterion_id"]
        self.assertEqual(codes(check(rec)), [("outcome.abort_criterion_id", "missing_evidence")])

    def test_aborted_on_a_criterion_recorded_as_not_crossed_is_mismatch(self):
        rec = yaml.safe_load(ABORTED.read_bytes())
        rec["outcome"]["abort_criterion_id"] = "abort-error-rate"
        self.assertEqual(codes(check(rec)), [("outcome.abort_criterion_id", "outcome_conflict")])

    def test_aborted_on_an_unknown_criterion_is_mismatch(self):
        rec = yaml.safe_load(ABORTED.read_bytes())
        rec["outcome"]["abort_criterion_id"] = "abort-made-up"
        self.assertEqual(codes(check(rec)), [("outcome.abort_criterion_id", "unknown_reference")])

    def test_not_run_with_observed_drill_times_is_mismatch(self):
        rec = base()
        rec["outcome"] = {"declared": "not_run"}
        self.assertEqual(codes(check(rec)), [(f"timeline.{k}", "outcome_conflict")
                                             for k in ("ended", "links_severed", "restore_finished", "restore_started")])

    def test_crossed_criterion_with_completed_and_no_deviation_is_mismatch(self):
        rec = base()
        rec["abort_criteria"][0]["crossed"] = True
        self.assertEqual(codes(check(rec)), [("abort_criteria[0].crossed", "outcome_conflict")])
        rec["deviations"] = [{"description": "The approver chose to continue past the threshold."}]
        self.assertEqual(check(rec)["status"], vr.COMPLETE)

    def test_only_aborted_names_a_criterion(self):
        rec = base()
        rec["outcome"]["abort_criterion_id"] = "abort-error-rate"
        self.assertEqual(codes(check(rec)), [("outcome.abort_criterion_id", "value")])

    def test_outcome_values_are_closed(self):
        rec = base()
        rec["outcome"]["declared"] = "passed"
        r = check(rec)
        self.assertEqual(codes(r), [("outcome.declared", "value")])
        self.assertIsNone(r["declared_outcome"])

    def test_deviations_and_coverage_need_an_explicit_declaration(self):
        for key, bad in (("deviations", ""), ("deviations", "None"), ("coverage_gaps", "none"), ("coverage_gaps", {})):
            rec = base()
            rec[key] = bad
            self.assertEqual(codes(check(rec)), [(key, "value")], (key, bad))
        for key in ("deviations", "coverage_gaps"):  # an empty list declares nothing: a gap, not a shape error
            rec = base()
            rec[key] = []
            self.assertEqual(codes(check(rec)), [(key, "missing_evidence")], key)
        rec = base()
        rec["coverage_gaps"] = "none_declared"
        self.assertEqual(check(rec)["status"], vr.COMPLETE)


def get(rec, path):
    obj = rec
    for part in path:
        obj = obj[part]
    return obj


# Every evidence field of one entry, as a path into the complete fixture.
EVIDENCE_FIELDS = [
    ("sign_offs", "approver", "reference"), ("sign_offs", "operator", "confirmed_how"), ("sign_offs", "verifier", "at"),
    ("timeline", "preconditions_confirmed"), ("timeline", "links_severed"), ("timeline", "ended"),
    ("timeline", "restore_started"), ("timeline", "restore_finished"),
    ("preconditions", 0, "state"),
    ("abort_criteria", 0, "observed_extreme"), ("abort_criteria", 1, "crossed"), ("abort_criteria", 2, "response"),
    ("recovery_checks", 0, "checked_by_role"), ("recovery_checks", 0, "checked_by_reference"),
    ("recovery_checks", 1, "state"), ("recovery_checks", 1, "at"), ("recovery_checks", 2, "reference"),
    ("deviations",), ("coverage_gaps",), ("outcome", "declared"),
]


def dotted(path):
    out = ""
    for part in path:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else part)
    return out


class EvidenceAbsence(unittest.TestCase):
    """Absent evidence is incomplete; malformed structure, type or discriminator is invalid."""

    def test_each_absent_evidence_field_is_missing_evidence(self):
        for path in EVIDENCE_FIELDS:
            rec = base()
            del get(rec, path[:-1])[path[-1]]
            r = check(rec)
            self.assertEqual(r["status"], vr.INCOMPLETE, path)
            self.assertIn((dotted(path), "missing_evidence"), codes(r), path)
            self.assertEqual(codes(r, vr.INVALID), [], path)

    def test_absent_sections_report_every_plan_item(self):
        expected = {
            "sign_offs": [(f"sign_offs.{n}", "missing_evidence") for n in ("approver", "operator", "verifier")],
            "timeline": [(f"timeline.{e}", "missing_evidence") for e in vr.TIMELINE_EVENTS],
            "outcome": [("outcome.declared", "missing_evidence")],
            "preconditions": [("preconditions", "missing_evidence")] * 3,
            "abort_criteria": [("abort_criteria", "missing_evidence")] * 3,
            "recovery_checks": [("recovery_checks", "missing_evidence")] * 3,
        }
        for key, want in expected.items():
            rec = base()
            del rec[key]
            r = check(rec)
            self.assertEqual(r["status"], vr.INCOMPLETE, key)
            self.assertEqual(codes(r), sorted(want), key)

    def test_empty_lists_and_mappings_are_gaps_not_shape_errors(self):
        for key in ("preconditions", "abort_criteria", "recovery_checks"):
            rec = base()
            rec[key] = []
            r = check(rec)
            self.assertEqual(r["status"], vr.INCOMPLETE, key)
            self.assertEqual(codes(r), [(key, "missing_evidence")] * 3, key)
        rec = base()
        rec["sign_offs"]["approver"] = {}
        self.assertEqual(codes(check(rec)), [(f"sign_offs.approver.{k}", "missing_evidence")
                                             for k in ("at", "confirmed_how", "reference")])
        rec = base()
        rec["sign_offs"] = {}
        self.assertEqual(check(rec)["status"], vr.INCOMPLETE)

    def test_structure_stays_invalid_when_absent(self):
        for path in (("schema_version",), ("record_id",), ("plan",), ("window",), ("plan", "plan_sha256"),
                     ("plan", "validator_source_commit"), ("window", "end"), ("abort_criteria", 0, "id"),
                     ("recovery_checks", 2, "id"), ("preconditions", 1, "id")):
            rec = base()
            del get(rec, path[:-1])[path[-1]]
            r = check(rec)
            self.assertEqual(r["status"], vr.INVALID, path)
            self.assertIn((dotted(path), "missing"), codes(r), path)

    def test_wrong_type_or_discriminator_stays_invalid(self):
        cases = [
            (("abort_criteria", 0, "observed_extreme"), "high", "number"),
            (("abort_criteria", 0, "observed_extreme"), True, "number"),
            (("abort_criteria", 1, "crossed"), "yes", "boolean"),
            (("abort_criteria", 2, "response"), 5, "text"),
            (("abort_criteria", 2, "response"), "  ", "text"),
            (("preconditions", 0, "state"), "done", "value"),
            (("recovery_checks", 0, "state"), "passed", "value"),
            (("recovery_checks", 0, "checked_by_role"), "auditor", "value"),
            (("sign_offs", "approver", "confirmed_how"), ["x"], "text"),
            (("timeline", "ended"), "yesterday", "time"),
            (("outcome", "declared"), "passed", "value"),
            (("abort_criteria",), {"id": "abort-error-rate"}, "list"),
            (("recovery_checks",), "none", "list"),
            (("sign_offs",), ["approver"], "shape"),
            (("timeline",), "2026-12-01T09:00:00Z", "shape"),
            (("sign_offs", "approver"), "yes", "shape"),
        ]
        for path, bad, code in cases:
            rec = base()
            get(rec, path[:-1])[path[-1]] = bad
            r = check(rec)
            self.assertEqual(r["status"], vr.INVALID, (path, bad))
            self.assertIn((dotted(path), code), codes(r), (path, bad))


class NotObservedLiteral(unittest.TestCase):
    """The exact literal not_observed is a declared gap in every evidence field; other prose is just text."""

    def test_literal_in_any_evidence_field_is_incomplete_and_never_compared(self):
        fields = EVIDENCE_FIELDS + [("preconditions", 0, "reference")]
        for path in fields:
            rec = base()
            get(rec, path[:-1])[path[-1]] = "not_observed"
            r = check(rec)
            self.assertEqual(r["status"], vr.INCOMPLETE, path)
            self.assertEqual(codes(r, vr.INCOMPLETE), [(dotted(path), "not_observed")], path)
            self.assertEqual(codes(r, vr.MISMATCH), [], path)
            self.assertEqual(codes(r, vr.INVALID), [], path)

    def test_codex_reported_fields_are_incomplete(self):
        for path in (("abort_criteria", 0, "response"), ("sign_offs", "approver", "confirmed_how"),
                     ("recovery_checks", 0, "reference"), ("recovery_checks", 0, "checked_by_reference")):
            rec = base()
            get(rec, path[:-1])[path[-1]] = "not_observed"
            self.assertEqual(check(rec)["status"], vr.INCOMPLETE, path)

    def test_whole_section_declared_not_observed(self):
        for key in ("sign_offs", "timeline", "preconditions", "abort_criteria", "recovery_checks",
                    "deviations", "coverage_gaps", "outcome"):
            rec = base()
            rec[key] = "not_observed"
            r = check(rec)
            self.assertEqual(r["status"], vr.INCOMPLETE, key)
            self.assertEqual(codes(r), [(key, "not_observed")], key)

    def test_list_items_and_outcome_details(self):
        rec = base()
        rec["coverage_gaps"].append("not_observed")
        self.assertEqual(codes(check(rec)), [("coverage_gaps[2]", "not_observed")])
        rec = base()
        rec["deviations"] = [{"description": "not_observed"}]
        self.assertEqual(codes(check(rec)), [("deviations[0].description", "not_observed")])
        rec = yaml.safe_load(ABORTED.read_bytes())
        rec["outcome"]["abort_criterion_id"] = "not_observed"
        self.assertEqual(codes(check(rec)), [("outcome.abort_criterion_id", "not_observed")])

    def test_other_prose_is_not_given_a_meaning(self):
        for text in ("Not observed", " not_observed", "not observed", "NOT_OBSERVED", "unknown", "n/a"):
            rec = base()
            rec["abort_criteria"][0]["response"] = text
            rec["sign_offs"]["operator"]["confirmed_how"] = text
            rec["recovery_checks"][0]["reference"] = text
            self.assertEqual(check(rec)["status"], vr.COMPLETE, text)
            rec = base()
            rec["sign_offs"]["approver"]["reference"] = text  # compared as a reference, like any text
            self.assertEqual(codes(check(rec)), [("sign_offs.approver.reference", "role_reference_mismatch")], text)


class ExplicitNull(unittest.TestCase):
    """A supplied null (a blank YAML value) has the wrong type: invalid, never complete (task123 R3).

    Absent evidence stays incomplete (missing_evidence); only an omitted key is absent.
    """

    TYPE_CODE = {"reference": "text", "confirmed_how": "text", "response": "text", "checked_by_reference": "text",
                 "at": "time", "state": "value", "checked_by_role": "value", "declared": "value",
                 "observed_extreme": "number", "crossed": "boolean", "deviations": "value", "coverage_gaps": "value"}
    CODEX_FIELDS = (("abort_criteria", 0, "response"), ("sign_offs", "approver", "confirmed_how"),
                    ("recovery_checks", 2, "reference"), ("recovery_checks", 0, "checked_by_reference"),
                    ("abort_criteria", 0, "observed_extreme"), ("timeline", "restore_finished"),
                    ("outcome", "declared"), ("coverage_gaps",))

    def code(self, path):
        return "time" if path[0] == "timeline" else self.TYPE_CODE[path[-1]]

    def test_null_in_every_evidence_field_is_invalid_with_its_type_code(self):
        for path in EVIDENCE_FIELDS + [("abort_criteria", 0, "crossed"), ("abort_criteria", 0, "response"),
                                       ("sign_offs", "approver", "confirmed_how")]:
            rec = base()
            get(rec, path[:-1])[path[-1]] = None
            r = check(rec)
            self.assertEqual(r["status"], vr.INVALID, path)
            self.assertEqual(codes(r), [(dotted(path), self.code(path))], path)

    def test_codex_reported_fields_null_absent_and_corrected(self):
        for path in self.CODEX_FIELDS:
            null, absent = base(), base()
            want = get(null, path[:-1])[path[-1]]
            get(null, path[:-1])[path[-1]] = None
            del get(absent, path[:-1])[path[-1]]
            r = check(null)
            self.assertEqual(r["status"], vr.INVALID, path)
            self.assertNotEqual(r["reasons"], [], path)
            self.assertEqual(check(absent)["status"], vr.INCOMPLETE, path)
            get(null, path[:-1])[path[-1]] = want
            self.assertEqual(check(null)["status"], vr.COMPLETE, path)

    def test_cli_blank_value_exits_one_absent_exits_one_corrected_exits_zero(self):
        def run(data):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "record.yaml"
                path.write_bytes(data)
                out = subprocess.run([sys.executable, str(HERE / "validate_record.py"), str(PLAN), str(path)],
                                     capture_output=True, text=True, timeout=60)
                self.assertEqual(path.read_bytes(), data)
            return out.returncode, json.loads(out.stdout)
        rec = base()
        rec["abort_criteria"][0]["response"] = None
        blank = dump(rec).replace(b"response: null", b"response:")
        self.assertIn(b"response:\n", blank)
        code, report = run(blank)
        self.assertEqual((code, report["status"]), (1, vr.INVALID))
        self.assertEqual(codes(report), [("abort_criteria[0].response", "text")])
        del rec["abort_criteria"][0]["response"]
        code, report = run(dump(rec))
        self.assertEqual((code, report["status"]), (1, vr.INCOMPLETE))
        self.assertEqual(codes(report), [("abort_criteria[0].response", "missing_evidence")])
        code, report = run(COMPLETE.read_bytes())
        self.assertEqual((code, report["status"], report["reasons"]), (0, vr.COMPLETE, []))

    def test_null_sections_and_lists_are_invalid_shape(self):
        for key, code in (("sign_offs", "shape"), ("timeline", "shape"), ("outcome", "shape"),
                          ("preconditions", "list"), ("abort_criteria", "list"), ("recovery_checks", "list")):
            rec = base()
            rec[key] = None
            r = check(rec)
            self.assertEqual(r["status"], vr.INVALID, key)
            self.assertIn((key, code), codes(r), key)
        rec = base()
        rec["sign_offs"]["approver"] = None
        self.assertEqual(codes(check(rec)), [("sign_offs.approver", "shape")])

    def test_null_structure_stays_invalid(self):
        for path in (("schema_version",), ("record_id",), ("plan",), ("window",), ("plan", "plan_id"),
                     ("plan", "plan_sha256"), ("plan", "validator_source_commit"), ("window", "end"),
                     ("abort_criteria", 0, "id"), ("recovery_checks", 2, "id")):
            rec = base()
            get(rec, path[:-1])[path[-1]] = None
            self.assertEqual(check(rec)["status"], vr.INVALID, path)

    def test_null_optional_fields_when_supplied_are_invalid(self):
        rec = base()
        rec["preconditions"][0]["reference"] = None
        self.assertEqual(codes(check(rec)), [("preconditions[0].reference", "text")])
        rec = yaml.safe_load(ABORTED.read_bytes())
        rec["outcome"]["abort_criterion_id"] = None
        self.assertEqual(codes(check(rec)), [("outcome.abort_criterion_id", "text")])
        rec = base()
        rec["deviations"] = [{"description": None}]
        self.assertEqual(codes(check(rec)), [("deviations[0].description", "text")])
        rec = base()
        rec["coverage_gaps"] = [None]
        self.assertEqual(codes(check(rec)), [("coverage_gaps[0]", "text")])

    def test_null_never_yields_complete_anywhere_in_the_fixture(self):
        # Every scalar leaf of the complete record, set to null, must leave the record not complete.
        def leaves(obj, path=()):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    yield from leaves(v, path + (k,))
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    yield from leaves(v, path + (i,))
            else:
                yield path
        rec0 = base()
        optional_free = set()  # notes and synthetic are typed too: null is invalid
        count = 0
        for path in leaves(rec0):
            if path in optional_free:
                continue
            rec = base()
            get(rec, path[:-1])[path[-1]] = None
            self.assertNotEqual(check(rec)["status"], vr.COMPLETE, path)
            count += 1
        self.assertGreater(count, 40)


class RecoveryControls(unittest.TestCase):
    """Controls: the absence and not_observed rules must not reject complete records."""

    def test_verified_and_not_verified_checks_are_both_complete(self):
        for state in ("verified", "not_verified"):
            rec = base()
            for entry in rec["recovery_checks"]:
                entry["state"] = state
            r = check(rec)
            self.assertEqual(r["status"], vr.COMPLETE, state)
            self.assertEqual(r["reasons"], [], state)

    def test_aborted_with_failed_check_is_complete(self):
        rec = yaml.safe_load(ABORTED.read_bytes())
        self.assertIn("not_verified", [e["state"] for e in rec["recovery_checks"]])
        r = check(rec)
        self.assertEqual((r["status"], r["declared_outcome"]), (vr.COMPLETE, "aborted"))

    def test_gap_then_declared_then_filled_recovers(self):
        rec = base()
        del rec["recovery_checks"][1]["at"]
        self.assertEqual(check(rec)["status"], vr.INCOMPLETE)
        rec["recovery_checks"][1]["at"] = "not_observed"
        self.assertEqual(codes(check(rec)), [("recovery_checks[1].at", "not_observed")])
        rec["recovery_checks"][1]["at"] = base()["recovery_checks"][1]["at"]
        r = check(rec)
        self.assertEqual(r["status"], vr.COMPLETE)
        self.assertEqual(r["record"]["record_sha256"], check(base())["record"]["record_sha256"])

    def test_optional_precondition_reference_may_be_omitted(self):
        rec = base()
        for entry in rec["preconditions"]:
            entry.pop("reference", None)
        self.assertEqual(check(rec)["status"], vr.COMPLETE)


class ValidatorSourceCommit(unittest.TestCase):
    """A declaration of which validate_plan.py source produced the digests; echoed, never verified."""

    def test_declared_commit_is_echoed(self):
        r = check(base())
        self.assertEqual(r["plan"]["declared_validator_source_commit"], "1b85c2e5e00a909989b7cfa81938c33e917a72c8")

    def test_sha256_object_ids_are_accepted(self):
        rec = base()
        rec["plan"]["validator_source_commit"] = "a" * 64
        r = check(rec)
        self.assertEqual(r["status"], vr.COMPLETE)
        self.assertEqual(r["plan"]["declared_validator_source_commit"], "a" * 64)

    def test_malformed_commit_is_invalid_and_not_echoed(self):
        for bad in ("1b85c2e", "1B85C2E5E00A909989B7CFA81938C33E917A72C8", "g" * 40, 123, "not_observed", "a" * 41):
            rec = base()
            rec["plan"]["validator_source_commit"] = bad
            r = check(rec)
            self.assertEqual(codes(r), [("plan.validator_source_commit", "commit")], bad)
            self.assertIsNone(r["plan"]["declared_validator_source_commit"], bad)

    def test_missing_commit_is_invalid(self):
        rec = base()
        del rec["plan"]["validator_source_commit"]
        r = check(rec)
        self.assertEqual(codes(r), [("plan.validator_source_commit", "missing")])
        self.assertIsNone(r["plan"]["declared_validator_source_commit"])

    def test_any_well_formed_commit_is_accepted_because_nothing_is_verified(self):
        rec = base()
        rec["plan"]["validator_source_commit"] = "0" * 40
        self.assertEqual(check(rec)["status"], vr.COMPLETE)


class Parsing(unittest.TestCase):
    def assertRefused(self, data: bytes, code: str):
        r = check(data)
        self.assertEqual(r["status"], vr.INVALID)
        self.assertIn(code, [x["code"] for x in r["reasons"]], r["reasons"])
        return r

    def test_duplicate_key(self):
        self.assertRefused(COMPLETE.read_bytes().replace(b"record_id:", b"record_id: a\nrecord_id:", 1), "duplicate_key")

    def test_anchor_and_alias(self):
        self.assertRefused(b"schema_version: &v drill-record/v1\nrecord_id: *v\n", "yaml_anchor")

    def test_nonfinite_number(self):
        self.assertRefused(COMPLETE.read_bytes().replace(b"observed_extreme: 1.8", b"observed_extreme: .nan"), "nonfinite")

    def test_huge_integer_literal(self):
        self.assertRefused(COMPLETE.read_bytes().replace(b"observed_extreme: 1.8", b"observed_extreme: " + b"9" * 5000), "yaml_value")

    def test_oversized_bytes(self):
        r = self.assertRefused(b"#" * (vp.MAX_FILE_BYTES + 1), "file_too_large")
        self.assertTrue(r["reasons"][0]["message"].startswith("record "))

    def test_two_documents_and_not_utf8(self):
        self.assertRefused(b"a: 1\n---\nb: 2\n", "yaml_documents")
        self.assertRefused(b"\xff\xfe", "encoding")

    def test_unknown_schema_version_and_fields(self):
        rec = base()
        rec["schema_version"] = "drill-record/v2"
        rec["extra"] = 1
        rec["timeline"]["vendor_called"] = "2026-12-02T09:00:00Z"
        r = check(rec)
        self.assertEqual(codes(r, vr.INVALID), [("extra", "unknown_field"), ("schema_version", "schema_version"),
                                                ("timeline.vendor_called", "unknown_field")])

    def test_executable_field_is_refused(self):
        rec = base()
        rec["recovery_checks"][0]["command"] = "kubectl get nodes"
        self.assertIn(("recovery_checks[0].command", "executable_field"), codes(check(rec)))

    def test_bad_digest_shape(self):
        rec = base()
        rec["plan"]["file_sha256"] = "C299"
        self.assertEqual(codes(check(rec)), [("plan.file_sha256", "digest")])

    def test_non_mapping_record(self):
        self.assertRefused(b"- just\n- a list\n", "shape")

    def test_unexpected_exception_is_a_structured_refusal(self):
        with mock.patch.object(vr, "_check", side_effect=RuntimeError("boom")):
            r = check(base())
        self.assertEqual(r["status"], vr.INVALID)
        self.assertEqual(codes(r), [("(file)", "internal_error")])


class Files(unittest.TestCase):
    def test_fifo_is_refused_without_opening(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("no FIFOs on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            fifo = Path(tmp) / "record.yaml"
            os.mkfifo(fifo)
            r = vr.validate_paths(PLAN, fifo)
        self.assertEqual(r["status"], vr.INVALID)
        self.assertEqual([(x["input"], x["code"]) for x in r["reasons"]], [("record", "not_regular_file")])
        self.assertIsNone(r["record"]["file_sha256"])

    def test_missing_plan_and_directory_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = vr.validate_paths(Path(tmp) / "absent.yaml", tmp)
        self.assertEqual(sorted((x["input"], x["code"]) for x in r["reasons"]),
                         [("plan", "unreadable"), ("record", "not_regular_file")])
        self.assertEqual(r["plan"]["status"], vp.INVALID)

    def test_inputs_are_not_modified(self):
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (PLAN, COMPLETE)}
        vr.validate_paths(PLAN, COMPLETE)
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (PLAN, COMPLETE)})


class Behaviour(unittest.TestCase):
    def test_deterministic_report(self):
        for rec in (COMPLETE, ABORTED, NOT_OBSERVED, NOT_RUN):
            self.assertEqual(json.dumps(vr.validate_paths(PLAN, rec), sort_keys=True),
                             json.dumps(vr.validate_paths(PLAN, rec), sort_keys=True))

    def test_key_order_and_quoting_do_not_change_record_identity(self):
        rec = base()
        reordered = dict(reversed(list(rec.items())))
        self.assertEqual(check(rec)["record"]["record_sha256"], check(reordered)["record"]["record_sha256"])

    def test_all_reasons_retained_and_most_severe_class_wins(self):
        rec = base()
        rec["extra"] = 1                                   # invalid
        rec["plan"]["plan_sha256"] = "0" * 64              # mismatch
        del rec["sign_offs"]["verifier"]                   # incomplete
        r = check(rec)
        self.assertEqual(r["status"], vr.INVALID)
        self.assertEqual([x["class"] for x in r["reasons"]], [vr.INVALID, vr.MISMATCH, vr.INCOMPLETE])
        self.assertIsNone(r["derived"]["restore_minutes"])  # nothing derived from an invalid record

    def test_mismatch_outranks_incomplete(self):
        rec = base()
        rec["plan"]["plan_sha256"] = "0" * 64
        del rec["sign_offs"]["verifier"]
        self.assertEqual(check(rec)["status"], vr.MISMATCH)

    def test_refusal_then_corrected_record_recovers(self):
        rec = base()
        rec["recovery_checks"][0]["checked_by_reference"] = "operator@example.invalid"
        rec["timeline"]["ended"] = "not observed"          # wrong literal: invalid, not a gap
        first = check(rec)
        self.assertEqual(first["status"], vr.INVALID)
        rec["timeline"]["ended"] = "2026-12-04T09:00:00Z"
        second = check(rec)
        self.assertEqual(second["status"], vr.MISMATCH)
        rec["recovery_checks"][0]["checked_by_reference"] = "verifier@example.invalid"
        third = check(rec)
        self.assertEqual(third["status"], vr.COMPLETE)
        self.assertEqual(third["record"]["record_sha256"], check(base())["record"]["record_sha256"])

    def test_every_code_has_a_documented_class(self):
        schema = (HERE / "record-schema.md").read_text()
        for code in sorted(vr.MISMATCH_CODES | vr.INCOMPLETE_CODES):
            self.assertIn(f"`{code}`", schema, code)


class Boundaries(unittest.TestCase):
    def test_imports_are_limited_and_shared_helpers_are_reused(self):
        tree = ast.parse((HERE / "validate_record.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(imported, {"__future__", "pathlib", "hashlib", "json", "re", "sys", "validate_plan"})
        source = (HERE / "validate_record.py").read_text()
        for name in ("vp._load", "vp._bounds", "vp._read_regular_file", "vp._utc", "vp.plan_digest", "vp.validate_bytes"):
            self.assertIn(name, source)
        self.assertNotIn("yaml.", source)       # no second parser
        self.assertNotIn("open(", source)       # all file access goes through the shared reader

    def test_references_are_never_opened(self):
        rec = base()
        rec["recovery_checks"][0]["reference"] = "/etc/passwd"
        with mock.patch("os.open", side_effect=AssertionError("opened")) as opened:
            r = check(rec)
        opened.assert_not_called()
        self.assertEqual(r["status"], vr.COMPLETE)


class Cli(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(HERE / "validate_record.py"), *map(str, args)],
                              capture_output=True, text=True, timeout=60)

    def test_complete_exits_zero(self):
        out = self.run_cli(PLAN, COMPLETE)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["status"], vr.COMPLETE)

    def test_incomplete_exits_one(self):
        out = self.run_cli(PLAN, NOT_RUN)
        self.assertEqual(out.returncode, 1)
        self.assertEqual(json.loads(out.stdout)["status"], vr.INCOMPLETE)

    def test_mismatch_from_files_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "plan.yaml"
            plan.write_bytes(b"# reformatted\n" + PLAN_BYTES)
            out = self.run_cli(plan, COMPLETE)
        self.assertEqual(out.returncode, 1)
        self.assertEqual(json.loads(out.stdout)["status"], vr.MISMATCH)

    def test_usage_error_exits_two(self):
        self.assertEqual(self.run_cli(PLAN).returncode, 2)
        self.assertEqual(self.run_cli().returncode, 2)


if __name__ == "__main__":
    unittest.main()
