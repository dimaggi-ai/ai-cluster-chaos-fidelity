#!/usr/bin/env python3
"""Completeness and plan binding for a human-filled drill evidence record.

It reads one validated drill plan and one structured evidence record
(``drill-record/v1``) and answers one question: does the record declare every
piece of evidence the plan asks for, bound to that exact plan? It never runs
anything, opens references, contacts a network or writes a file.

The strongest status is ``evidence_complete_not_judged``. Complete means every
required declaration is present and consistent. It does not mean the drill
passed, that an SLO held, that anything was isolated or restored, that anyone's
identity was confirmed, or that anything is compliant or accepted. An aborted
drill, or a recovery check recorded as not verified, can be complete.

Loading, bounds, file reading, time parsing and plan validation are imported
from validate_plan.py; this file adds only the record's meaning.

Usage:
    python3 drills/validate_record.py PLAN.yaml RECORD.yaml
Prints one JSON report. Exit 0 = evidence_complete_not_judged;
1 = invalid, mismatch or incomplete; 2 = usage error.
"""

from __future__ import annotations

from pathlib import Path
import hashlib
import json
import re
import sys

import validate_plan as vp

RECORD_VERSION = "drill-record/v1"
REPORT_VERSION = "drill-record-report/v1"

INVALID = "invalid"
MISMATCH = "mismatch"
INCOMPLETE = "incomplete"
COMPLETE = "evidence_complete_not_judged"
# Most severe first. The report status is the most severe class among its reasons.
PRECEDENCE = (INVALID, MISMATCH, INCOMPLETE)

NOT_OBSERVED = "not_observed"
OUTCOMES = ("completed_as_planned", "aborted", "not_run")
TIMELINE_EVENTS = ("preconditions_confirmed", "links_severed", "ended", "restore_started", "restore_finished")
HEX64 = re.compile(r"[0-9a-f]{64}")
GIT_OBJECT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")  # SHA-1 or SHA-256 repositories

# Codes whose class is not "invalid". Every other code (loader, bounds, shape) is invalid.
MISMATCH_CODES = frozenset({
    "plan_id_mismatch", "plan_sha256_mismatch", "file_sha256_mismatch", "window_mismatch",
    "unknown_reference", "duplicate_reference", "role_undeclared", "role_reference_mismatch",
    "check_role_mismatch", "check_not_independent", "outside_window", "timeline_order",
    "check_before_restore", "outcome_conflict",
})
INCOMPLETE_CODES = frozenset({"missing_evidence", "not_observed"})

LIMITATIONS = [
    "evidence_complete_not_judged means every required declaration is present and bound to this plan; nothing is judged.",
    "It is not a pass or fail, an SLO result, proof of isolation or restoration, or a compliance or acceptance claim.",
    "Everything in the record is a human declaration; references are labels and are never opened or verified.",
    "Role references are compared as text; no identity is authenticated.",
    "A complete coverage_gaps or deviations entry is a declaration, not proof that nothing was missed.",
    "Missing values are reported as missing; nothing is defaulted or interpolated.",
    "validator_source_commit is a declaration of which validate_plan.py source produced the digests; it is not checked against any repository.",
]


def _class(code: str) -> str:
    if code in MISMATCH_CODES:
        return MISMATCH
    if code in INCOMPLETE_CODES:
        return INCOMPLETE
    return INVALID


class _RecordChecker(vp._Checker):
    """The plan checker's field rules, minus the plan-only refusal of result-shaped names.

    A record exists to hold what was observed, so ``outcome`` is allowed here.
    Executable field names are still refused.
    """

    def fields(self, obj, path, required, optional=()):
        if obj is vp._ABSENT:
            return None
        if not isinstance(obj, dict):
            self.fail(path or "(root)", "shape", "expected a mapping")
            return None
        allowed = set(required) | set(optional)
        for key in obj:
            sub = f"{path}.{key}" if path else key
            if key.lower() in vp.EXECUTABLE_FIELDS and key not in allowed:
                self.fail(sub, "executable_field", f"{key!r} would make the record executable; records are inert")
            elif key not in allowed:
                self.fail(sub, "unknown_field", f"unknown field {key!r}")
        for key in required:
            if key not in obj:
                self.fail(f"{path}.{key}" if path else key, "missing", "required field is missing")
        return obj

    def gap(self, path, message):
        self.fail(path, "missing_evidence", message)

    def enum(self, obj, key, path, choices):
        if key in (obj or {}) and obj[key] not in choices:
            self.fail(path, "value", f"must be one of {list(choices)}")
            return None
        return (obj or {}).get(key)

    # Evidence fields. Absent evidence is a gap (incomplete), the exact literal
    # not_observed is a declared gap (incomplete), and anything else must have the
    # field's type (invalid otherwise). No other text is given a meaning.
    # Each returns the usable value, NOT_OBSERVED, or None (absent or malformed).

    def ev(self, obj, key, path):
        if obj is None:
            return None
        if key not in obj:
            self.gap(path, "required evidence is missing")
            return None
        value = obj[key]
        if isinstance(value, str) and value == NOT_OBSERVED:
            self.fail(path, "not_observed", "declared not observed")
            return NOT_OBSERVED
        return value

    def ev_text(self, obj, key, path):
        value = self.ev(obj, key, path)
        if value is None or value == NOT_OBSERVED:
            return value
        if not isinstance(value, str) or not value.strip():
            self.fail(path, "text", "must be non-empty text or not_observed")
            return None
        return value

    def ev_enum(self, obj, key, path, choices):
        value = self.ev(obj, key, path)
        if value is None or value == NOT_OBSERVED:
            return value
        if value not in choices:
            self.fail(path, "value", f"must be one of {list(choices) + [NOT_OBSERVED]}")
            return None
        return value

    def ev_time(self, obj, key, path):
        value = self.ev(obj, key, path)
        if value is None or value == NOT_OBSERVED:
            return value
        parsed = vp._utc(value)
        if isinstance(parsed, tuple):
            self.fail(path, *parsed)
            return None
        return parsed

    def section(self, top, key, optional, path=None):
        """A mapping of evidence: {} when absent (its fields then report the gaps),
        None when declared not_observed or malformed."""
        path = path or key
        if key not in top:
            return {}
        raw = top[key]
        if isinstance(raw, str) and raw == NOT_OBSERVED:
            self.fail(path, "not_observed", "declared not observed")
            return None
        return self.fields(raw, path, (), optional)

    def entries(self, top, key):
        """(entries, expect_plan_ids). Absent or empty: every plan id is a gap."""
        if key not in top:
            return [], True
        raw = top[key]
        if isinstance(raw, str) and raw == NOT_OBSERVED:
            self.fail(key, "not_observed", "declared not observed")
            return [], False
        if not isinstance(raw, list):
            self.fail(key, "list", "must be a list of entries")
            return [], False
        return raw, True


def _norm(ref):
    return ref.strip().lower() if isinstance(ref, str) else None


def _index(c: _RecordChecker, entries, path):
    """id -> (position, entry). Duplicate ids are a conflicting reference."""
    out = {}
    for i, entry in enumerate(entries):
        ident = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(ident, str):
            continue
        if ident in out:
            c.fail(f"{path}[{i}].id", "duplicate_reference", f"id {ident!r} appears more than once")
        else:
            out[ident] = (i, entry)
    return out


def _check(plan: dict, plan_report: dict, record) -> tuple[list[dict], dict]:
    c = _RecordChecker()
    derived = {"restore_minutes": None, "plan_max_restore_minutes": None, "restore_exceeds_plan_max": None}
    # Structure (identity, window, entry ids) is required: absent is invalid.
    # Evidence sections are checked below: absent is incomplete.
    top = c.fields(record, "", ("schema_version", "record_id", "plan", "window"),
                   ("synthetic", "notes", "sign_offs", "timeline", "preconditions", "abort_criteria",
                    "recovery_checks", "deviations", "coverage_gaps", "outcome"))
    if top is None:
        return c.reasons, derived
    if "schema_version" in top and top["schema_version"] != RECORD_VERSION:
        c.fail("schema_version", "schema_version", f"must be {RECORD_VERSION!r}")
    c.text(top, "record_id", "record_id")
    c.text(top, "notes", "notes")
    if "synthetic" in top and not isinstance(top["synthetic"], bool):
        c.fail("synthetic", "boolean", "must be true or false")

    # ---- identity: exact plan, exact bytes, exact window
    ident = c.fields(top.get("plan", vp._ABSENT), "plan", ("plan_id", "plan_sha256", "file_sha256", "validator_status",
                                                                   "validator_source_commit"))
    if ident is not None:
        if "plan_id" in ident and c.text(ident, "plan_id", "plan.plan_id") is not None \
                and ident["plan_id"] != plan.get("plan_id"):
            c.fail("plan.plan_id", "plan_id_mismatch", f"the plan's plan_id is {plan.get('plan_id')!r}")
        for key in ("plan_sha256", "file_sha256"):
            if key in ident and not (isinstance(ident[key], str) and HEX64.fullmatch(ident[key])):
                c.fail(f"plan.{key}", "digest", "must be 64 lowercase hexadecimal characters")
        c.enum(ident, "validator_status", "plan.validator_status", (vp.VALID,))
        if "validator_source_commit" in ident and not (isinstance(ident["validator_source_commit"], str)
                                                       and GIT_OBJECT.fullmatch(ident["validator_source_commit"])):
            c.fail("plan.validator_source_commit", "commit",
                   "must be a full git commit id: 40 or 64 lowercase hexadecimal characters")
        same_plan = ident.get("plan_sha256") == plan_report["plan_sha256"]
        if isinstance(ident.get("plan_sha256"), str) and HEX64.fullmatch(ident["plan_sha256"]) and not same_plan:
            c.fail("plan.plan_sha256", "plan_sha256_mismatch", "the record names a different plan (canonical content differs)")
        if isinstance(ident.get("file_sha256"), str) and HEX64.fullmatch(ident["file_sha256"]) \
                and ident["file_sha256"] != plan_report["file_sha256"]:
            c.fail("plan.file_sha256", "file_sha256_mismatch",
                   "the plan file's bytes differ from the ones recorded"
                   + (" although its canonical content matches (reformatted, re-commented or re-quoted)" if same_plan else ""))

    start = end = None
    win = c.fields(top.get("window", vp._ABSENT), "window", ("start", "end"))
    if win is not None:
        times = {}
        for key in ("start", "end"):
            if key in win:
                parsed = vp._utc(win[key])
                if isinstance(parsed, tuple):
                    c.fail(f"window.{key}", *parsed)
                else:
                    times[key] = parsed
        pstart, pend = vp._utc(plan["window"]["start"]), vp._utc(plan["window"]["end"])
        for key, want in (("start", pstart), ("end", pend)):
            if key in times and times[key] != want:
                c.fail(f"window.{key}", "window_mismatch", f"the plan's window {key} is {want.isoformat()} UTC")
        start, end = pstart, pend  # observations are always bound to the plan's own window

    # ---- sign-offs: one per role the plan declares, same reference
    plan_roles = {name: _norm(entry.get("reference")) for name, entry in plan["roles"].items()}
    signs = c.section(top, "sign_offs", vp.ROLE_NAMES)
    if signs is not None:
        for name in vp.ROLE_NAMES:
            p = f"sign_offs.{name}"
            if name not in signs:
                if name in plan_roles:
                    c.gap(p, f"the plan declares a {name}; their sign-off is missing")
                continue
            if name not in plan_roles:
                c.fail(p, "role_undeclared", f"the plan declares no {name}")
                continue
            entry = c.section(signs, name, ("reference", "confirmed_how", "at"), p)
            if entry is None:
                continue
            ref = c.ev_text(entry, "reference", f"{p}.reference")
            if ref not in (None, NOT_OBSERVED) and _norm(ref) != plan_roles[name]:
                c.fail(f"{p}.reference", "role_reference_mismatch", f"does not match the plan's {name} reference")
            c.ev_text(entry, "confirmed_how", f"{p}.confirmed_how")
            c.ev_time(entry, "at", f"{p}.at")

    # ---- timeline
    tl = c.section(top, "timeline", TIMELINE_EVENTS)
    t = {}
    if tl is not None:
        for key in TIMELINE_EVENTS:
            value = c.ev_time(tl, key, f"timeline.{key}")
            if value not in (None, NOT_OBSERVED):
                t[key] = value
        # Observations of the drill itself lie inside the plan's window.
        for key in ("links_severed", "ended"):
            if key in t and start and not start <= t[key] <= end:
                c.fail(f"timeline.{key}", "outside_window", "must lie within the plan's window (UTC)")
        # Preconditions may be confirmed before the window opens, never after the links are cut.
        order = [("preconditions_confirmed", "links_severed"), ("links_severed", "ended"),
                 ("ended", "restore_started"), ("restore_started", "restore_finished")]
        for first, second in order:
            if first in t and second in t and t[first] > t[second]:
                c.fail(f"timeline.{second}", "timeline_order", f"{second} is earlier than {first}")
        if "preconditions_confirmed" in t and end and t["preconditions_confirmed"] > end:
            c.fail("timeline.preconditions_confirmed", "outside_window", "is after the plan's window closed")
        # Restoration may legitimately run past the window's end; it is measured, not judged.
        limit = plan["restore"]["max_restore_minutes"]
        derived["plan_max_restore_minutes"] = limit
        if "restore_started" in t and "restore_finished" in t and t["restore_finished"] >= t["restore_started"]:
            minutes = (t["restore_finished"] - t["restore_started"]).total_seconds() / 60
            derived["restore_minutes"] = minutes
            derived["restore_exceeds_plan_max"] = minutes > limit

    # ---- preconditions
    plan_pre = {e["id"] for e in plan["preconditions"]}
    pre, expect_pre = c.entries(top, "preconditions")
    for i, item in enumerate(pre):
        p = f"preconditions[{i}]"
        entry = c.fields(item, p, ("id",), ("state", "reference"))
        if entry is None:
            continue
        c.text(entry, "id", f"{p}.id")
        c.ev_enum(entry, "state", f"{p}.state", ("confirmed", "not_confirmed"))
        if "reference" in entry:  # optional; when given it is evidence like any other
            c.ev_text(entry, "reference", f"{p}.reference")
    seen_pre = _index(c, pre, "preconditions")
    for ident in sorted(set(seen_pre) - plan_pre):
        c.fail(f"preconditions[{seen_pre[ident][0]}].id", "unknown_reference", f"the plan has no precondition {ident!r}")
    if expect_pre:
        for ident in sorted(plan_pre - set(seen_pre)):
            c.gap("preconditions", f"no entry for the plan's precondition {ident!r}")

    # ---- abort criteria: one entry per plan criterion
    plan_abort = {e["id"] for e in plan["abort_criteria"]}
    aborts, expect_abort = c.entries(top, "abort_criteria")
    for i, item in enumerate(aborts):
        p = f"abort_criteria[{i}]"
        entry = c.fields(item, p, ("id",), ("observed_extreme", "crossed", "response"))
        if entry is None:
            continue
        c.text(entry, "id", f"{p}.id")
        c.ev_text(entry, "response", f"{p}.response")
        value = c.ev(entry, "observed_extreme", f"{p}.observed_extreme")
        if value not in (None, NOT_OBSERVED) and (isinstance(value, bool) or not isinstance(value, (int, float))):
            c.fail(f"{p}.observed_extreme", "number", "must be a number or not_observed")
        crossed = c.ev(entry, "crossed", f"{p}.crossed")
        if crossed not in (None, NOT_OBSERVED) and not isinstance(crossed, bool):
            c.fail(f"{p}.crossed", "boolean", "must be true, false or not_observed")
    seen_abort = _index(c, aborts, "abort_criteria")
    for ident in sorted(set(seen_abort) - plan_abort):
        c.fail(f"abort_criteria[{seen_abort[ident][0]}].id", "unknown_reference", f"the plan has no abort criterion {ident!r}")
    if expect_abort:
        for ident in sorted(plan_abort - set(seen_abort)):
            c.gap("abort_criteria", f"no entry for the plan's abort criterion {ident!r}")

    # ---- recovery checks: plan role, that role's person, never the operator; after restore starts
    plan_checks = {e["id"]: e["checked_by_role"] for e in plan["recovery_checks"]}
    checks, expect_check = c.entries(top, "recovery_checks")
    for i, item in enumerate(checks):
        p = f"recovery_checks[{i}]"
        entry = c.fields(item, p, ("id",), ("checked_by_role", "checked_by_reference", "state", "at", "reference"))
        if entry is None:
            continue
        c.text(entry, "id", f"{p}.id")
        c.ev_text(entry, "reference", f"{p}.reference")
        c.ev_enum(entry, "state", f"{p}.state", ("verified", "not_verified"))
        when = c.ev_time(entry, "at", f"{p}.at")
        role = c.ev_enum(entry, "checked_by_role", f"{p}.checked_by_role", vp.ROLE_NAMES)
        ref = c.ev_text(entry, "checked_by_reference", f"{p}.checked_by_reference")
        ident = entry.get("id")
        if isinstance(ident, str) and ident in plan_checks and role not in (None, NOT_OBSERVED) \
                and role != plan_checks[ident]:
            c.fail(f"{p}.checked_by_role", "check_role_mismatch", f"the plan assigns this check to the {plan_checks[ident]}")
        if ref not in (None, NOT_OBSERVED):
            if _norm(ref) == plan_roles.get("operator"):
                c.fail(f"{p}.checked_by_reference", "check_not_independent", "the operator may not record a recovery check")
            elif role in plan_roles and _norm(ref) != plan_roles[role]:
                c.fail(f"{p}.checked_by_reference", "role_reference_mismatch", f"does not match the plan's {role} reference")
        floor = t.get("restore_started") or t.get("ended")
        if when not in (None, NOT_OBSERVED) and floor and when < floor:
            c.fail(f"{p}.at", "check_before_restore", "a recovery check cannot precede the restore (or the drill's end)")
    seen_check = _index(c, checks, "recovery_checks")
    for ident in sorted(set(seen_check) - set(plan_checks)):
        c.fail(f"recovery_checks[{seen_check[ident][0]}].id", "unknown_reference", f"the plan has no recovery check {ident!r}")
    if expect_check:
        for ident in sorted(set(plan_checks) - set(seen_check)):
            c.gap("recovery_checks", f"no entry for the plan's recovery check {ident!r}")

    # ---- deviations and coverage gaps: explicit declarations, possibly "none"
    deviations_none = False
    dev = c.ev(top, "deviations", "deviations")
    if dev == "none":
        deviations_none = True
    elif isinstance(dev, list) and not dev:
        c.gap("deviations", "an empty list declares nothing; write none or list the deviations")
    elif isinstance(dev, list):
        for i, item in enumerate(dev):
            entry = c.fields(item, f"deviations[{i}]", (), ("description",))
            c.ev_text(entry, "description", f"deviations[{i}].description")
    elif dev not in (None, NOT_OBSERVED):
        c.fail("deviations", "value", "must be the word none, not_observed or a list of {description}")
    gaps = c.ev(top, "coverage_gaps", "coverage_gaps")
    if isinstance(gaps, list) and not gaps:
        c.gap("coverage_gaps", "an empty list declares nothing; write none_declared or list the gaps")
    elif isinstance(gaps, list):
        for i, item in enumerate(gaps):
            if isinstance(item, str) and item == NOT_OBSERVED:
                c.fail(f"coverage_gaps[{i}]", "not_observed", "declared not observed")
            elif not isinstance(item, str) or not item.strip():
                c.fail(f"coverage_gaps[{i}]", "text", "must be non-empty text")
    elif gaps not in (None, NOT_OBSERVED, "none_declared"):
        c.fail("coverage_gaps", "value", "must be none_declared, not_observed or a list of text")

    # ---- outcome: declared, never inferred, never upgraded
    out = c.section(top, "outcome", ("declared", "abort_criterion_id"))
    if out is not None:
        declared = c.ev_enum(out, "declared", "outcome.declared", OUTCOMES)
        if declared == "aborted":
            if "abort_criterion_id" not in out:
                c.gap("outcome.abort_criterion_id", "an aborted drill names the criterion that stopped it")
            else:
                named = c.ev_text(out, "abort_criterion_id", "outcome.abort_criterion_id")
                if named not in (None, NOT_OBSERVED):
                    if named not in plan_abort:
                        c.fail("outcome.abort_criterion_id", "unknown_reference", f"the plan has no abort criterion {named!r}")
                    elif named in seen_abort and seen_abort[named][1].get("crossed") is False:
                        c.fail("outcome.abort_criterion_id", "outcome_conflict", "the record says this criterion was not crossed")
        elif declared in OUTCOMES and "abort_criterion_id" in out:
            c.fail("outcome.abort_criterion_id", "value", "only an aborted outcome names an abort criterion")
        if declared == "not_run":
            for key in ("links_severed", "ended", "restore_started", "restore_finished"):
                if key in t:
                    c.fail(f"timeline.{key}", "outcome_conflict", "the record says the drill was not run")
        if declared == "completed_as_planned" and deviations_none:
            for ident, (i, entry) in sorted(seen_abort.items()):
                if entry.get("crossed") is True:
                    c.fail(f"abort_criteria[{i}].crossed", "outcome_conflict",
                           "a crossed criterion with completed_as_planned needs a recorded deviation")
    return c.reasons, derived


def _reason(source, r):
    message = r["message"]
    if source == "record" and message.startswith("plan "):
        message = "record " + message[len("plan "):]  # shared loader messages say "plan"
    return {"class": _class(r["code"]), "input": source, "path": r["path"] or "(file)", "code": r["code"], "message": message}


def _read(path):
    try:
        return vp._read_regular_file(Path(path)), None
    except vp._Reject as exc:
        return None, {"path": "(file)", "code": exc.code, "message": exc.message}
    except OSError as exc:
        return None, {"path": "(file)", "code": "unreadable", "message": exc.__class__.__name__}


def validate(plan_bytes: bytes, record_bytes: bytes, plan_name="<plan>", record_name="<record>") -> dict:
    plan_report = vp.validate_bytes(plan_bytes, plan_name)
    report = {
        "report_version": REPORT_VERSION, "status": INVALID, "declared_outcome": None,
        "plan": {"file": plan_name, "file_sha256": plan_report["file_sha256"],
                 "plan_sha256": plan_report["plan_sha256"], "status": plan_report["status"],
                 "declared_validator_source_commit": None},
        "record": {"file": record_name, "file_sha256": hashlib.sha256(record_bytes).hexdigest(), "record_sha256": None},
        "derived": {"restore_minutes": None, "plan_max_restore_minutes": None, "restore_exceeds_plan_max": None},
        "reasons": [], "limitations": list(LIMITATIONS),
    }
    reasons = [_reason("plan", r) for r in plan_report["reasons"]]
    try:
        record = vp._load(record_bytes)
        vp._bounds(record)
        report["record"]["record_sha256"] = vp.plan_digest(record)
        declared = record.get("plan", {}).get("validator_source_commit") if isinstance(record, dict) \
            and isinstance(record.get("plan"), dict) else None
        if isinstance(declared, str) and GIT_OBJECT.fullmatch(declared):
            report["plan"]["declared_validator_source_commit"] = declared  # echoed, never verified
        if plan_report["status"] == vp.VALID:
            plan = vp._load(plan_bytes)
            found, report["derived"] = _check(plan, plan_report, record)
            reasons += [_reason("record", r) for r in found]
            if isinstance(record, dict) and isinstance(record.get("outcome"), dict) \
                    and record["outcome"].get("declared") in OUTCOMES:
                report["declared_outcome"] = record["outcome"]["declared"]
        else:
            reasons.append(_reason("plan", {"path": "(file)", "code": "plan_not_valid",
                                            "message": "a record is checked only against a plan_valid_not_run plan"}))
    except vp._Reject as exc:
        reasons.append(_reason("record", {"path": exc.path, "code": exc.code, "message": exc.message}))
    except Exception as exc:  # last line of defence: a refusal, never a traceback
        reasons.append(_reason("record", {"path": "(file)", "code": "internal_error",
                                          "message": f"the checker could not check this input: {exc.__class__.__name__}"}))
    report["reasons"] = sorted(reasons, key=lambda r: (PRECEDENCE.index(r["class"]), r["input"], r["path"], r["code"], r["message"]))
    present = {r["class"] for r in reasons}
    report["status"] = next((cls for cls in PRECEDENCE if cls in present), COMPLETE)
    if report["status"] == INVALID:
        report["derived"] = {"restore_minutes": None, "plan_max_restore_minutes": None, "restore_exceeds_plan_max": None}
    return report


def validate_paths(plan_path, record_path) -> dict:
    plan_bytes, plan_err = _read(plan_path)
    record_bytes, record_err = _read(record_path)
    if plan_err or record_err:
        report = validate(plan_bytes or b"", record_bytes or b"", Path(plan_path).name, Path(record_path).name)
        reasons = [_reason(src, err) for src, err in (("plan", plan_err), ("record", record_err)) if err]
        report.update(status=INVALID, declared_outcome=None, reasons=reasons,
                      derived={"restore_minutes": None, "plan_max_restore_minutes": None, "restore_exceeds_plan_max": None})
        for src, err in (("plan", plan_err), ("record", record_err)):
            if err:
                report[src]["file_sha256"] = None
                report[src][f"{src}_sha256"] = None
                if src == "plan":
                    report["plan"]["status"] = vp.INVALID
        return report
    return validate(plan_bytes, record_bytes, Path(plan_path).name, Path(record_path).name)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or any(a in ("-h", "--help") for a in argv):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    report = validate_paths(argv[0], argv[1])
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == COMPLETE else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
