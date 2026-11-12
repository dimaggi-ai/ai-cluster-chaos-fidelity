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
PRECONDITION_STATES = ("confirmed", "not_confirmed", NOT_OBSERVED)
CHECK_STATES = ("verified", "not_verified", NOT_OBSERVED)
TIMELINE_EVENTS = ("preconditions_confirmed", "links_severed", "ended", "restore_started", "restore_finished")
HEX64 = re.compile(r"[0-9a-f]{64}")

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

    def when(self, obj, key, path):
        """A UTC time, the literal not_observed, or None if absent or malformed."""
        if key not in (obj or {}):
            return None
        value = obj[key]
        if value == NOT_OBSERVED:
            self.fail(path, "not_observed", "declared not observed")
            return NOT_OBSERVED
        parsed = vp._utc(value)
        if isinstance(parsed, tuple):
            self.fail(path, *parsed)
            return None
        return parsed


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
    top = c.fields(record, "", ("schema_version", "record_id", "plan", "window", "sign_offs", "timeline",
                                "preconditions", "abort_criteria", "recovery_checks", "deviations",
                                "coverage_gaps", "outcome"), ("synthetic", "notes"))
    if top is None:
        return c.reasons, derived
    if "schema_version" in top and top["schema_version"] != RECORD_VERSION:
        c.fail("schema_version", "schema_version", f"must be {RECORD_VERSION!r}")
    c.text(top, "record_id", "record_id")
    c.text(top, "notes", "notes")
    if "synthetic" in top and not isinstance(top["synthetic"], bool):
        c.fail("synthetic", "boolean", "must be true or false")

    # ---- identity: exact plan, exact bytes, exact window
    ident = c.fields(top.get("plan", vp._ABSENT), "plan", ("plan_id", "plan_sha256", "file_sha256", "validator_status"))
    if ident is not None:
        if "plan_id" in ident and c.text(ident, "plan_id", "plan.plan_id") is not None \
                and ident["plan_id"] != plan.get("plan_id"):
            c.fail("plan.plan_id", "plan_id_mismatch", f"the plan's plan_id is {plan.get('plan_id')!r}")
        for key in ("plan_sha256", "file_sha256"):
            if key in ident and not (isinstance(ident[key], str) and HEX64.fullmatch(ident[key])):
                c.fail(f"plan.{key}", "digest", "must be 64 lowercase hexadecimal characters")
        c.enum(ident, "validator_status", "plan.validator_status", (vp.VALID,))
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
    signs = c.fields(top.get("sign_offs", vp._ABSENT), "sign_offs", (), vp.ROLE_NAMES)
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
            entry = c.fields(signs[name], p, ("reference", "confirmed_how", "at"))
            if entry is None:
                continue
            ref = c.text(entry, "reference", f"{p}.reference")
            if ref is not None and _norm(ref) != plan_roles[name]:
                c.fail(f"{p}.reference", "role_reference_mismatch", f"does not match the plan's {name} reference")
            c.text(entry, "confirmed_how", f"{p}.confirmed_how")
            c.when(entry, "at", f"{p}.at")

    # ---- timeline
    tl = c.fields(top.get("timeline", vp._ABSENT), "timeline", TIMELINE_EVENTS)
    t = {}
    if tl is not None:
        for key in TIMELINE_EVENTS:
            value = c.when(tl, key, f"timeline.{key}")
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
    pre = c.items(top, "preconditions", "preconditions")
    for i, item in enumerate(pre):
        p = f"preconditions[{i}]"
        entry = c.fields(item, p, ("id", "state"), ("reference",))
        if entry is None:
            continue
        c.text(entry, "id", f"{p}.id")
        c.text(entry, "reference", f"{p}.reference")
        if c.enum(entry, "state", f"{p}.state", PRECONDITION_STATES) == NOT_OBSERVED:
            c.fail(f"{p}.state", "not_observed", "declared not observed")
    seen_pre = _index(c, pre, "preconditions")
    if "preconditions" in top:
        for ident in sorted(set(seen_pre) - plan_pre):
            c.fail(f"preconditions[{seen_pre[ident][0]}].id", "unknown_reference", f"the plan has no precondition {ident!r}")
        for ident in sorted(plan_pre - set(seen_pre)):
            c.gap("preconditions", f"no entry for the plan's precondition {ident!r}")

    # ---- abort criteria: one entry per plan criterion
    plan_abort = {e["id"] for e in plan["abort_criteria"]}
    aborts = c.items(top, "abort_criteria", "abort_criteria")
    for i, item in enumerate(aborts):
        p = f"abort_criteria[{i}]"
        entry = c.fields(item, p, ("id", "observed_extreme", "crossed", "response"))
        if entry is None:
            continue
        c.text(entry, "id", f"{p}.id")
        c.text(entry, "response", f"{p}.response")
        if "observed_extreme" in entry:
            value = entry["observed_extreme"]
            if value == NOT_OBSERVED:
                c.fail(f"{p}.observed_extreme", "not_observed", "declared not observed")
            elif isinstance(value, bool) or not isinstance(value, (int, float)):
                c.fail(f"{p}.observed_extreme", "number", "must be a number or not_observed")
        if "crossed" in entry:
            if entry["crossed"] == NOT_OBSERVED:
                c.fail(f"{p}.crossed", "not_observed", "declared not observed")
            elif not isinstance(entry["crossed"], bool):
                c.fail(f"{p}.crossed", "boolean", "must be true, false or not_observed")
    seen_abort = _index(c, aborts, "abort_criteria")
    if "abort_criteria" in top:
        for ident in sorted(set(seen_abort) - plan_abort):
            c.fail(f"abort_criteria[{seen_abort[ident][0]}].id", "unknown_reference", f"the plan has no abort criterion {ident!r}")
        for ident in sorted(plan_abort - set(seen_abort)):
            c.gap("abort_criteria", f"no entry for the plan's abort criterion {ident!r}")

    # ---- recovery checks: plan role, that role's person, never the operator; after restore starts
    plan_checks = {e["id"]: e["checked_by_role"] for e in plan["recovery_checks"]}
    checks = c.items(top, "recovery_checks", "recovery_checks")
    for i, item in enumerate(checks):
        p = f"recovery_checks[{i}]"
        entry = c.fields(item, p, ("id", "checked_by_role", "checked_by_reference", "state", "at", "reference"))
        if entry is None:
            continue
        c.text(entry, "id", f"{p}.id")
        c.text(entry, "reference", f"{p}.reference")
        state = c.enum(entry, "state", f"{p}.state", CHECK_STATES)
        if state == NOT_OBSERVED:
            c.fail(f"{p}.state", "not_observed", "declared not observed")
        when = c.when(entry, "at", f"{p}.at")
        role = c.enum(entry, "checked_by_role", f"{p}.checked_by_role", vp.ROLE_NAMES)
        ref = c.text(entry, "checked_by_reference", f"{p}.checked_by_reference")
        ident = entry.get("id")
        if isinstance(ident, str) and ident in plan_checks and role and role != plan_checks[ident]:
            c.fail(f"{p}.checked_by_role", "check_role_mismatch", f"the plan assigns this check to the {plan_checks[ident]}")
        if ref is not None:
            if _norm(ref) == plan_roles.get("operator"):
                c.fail(f"{p}.checked_by_reference", "check_not_independent", "the operator may not record a recovery check")
            elif role in plan_roles and _norm(ref) != plan_roles[role]:
                c.fail(f"{p}.checked_by_reference", "role_reference_mismatch", f"does not match the plan's {role} reference")
        floor = t.get("restore_started") or t.get("ended")
        if when not in (None, NOT_OBSERVED) and floor and when < floor:
            c.fail(f"{p}.at", "check_before_restore", "a recovery check cannot precede the restore (or the drill's end)")
    seen_check = _index(c, checks, "recovery_checks")
    if "recovery_checks" in top:
        for ident in sorted(set(seen_check) - set(plan_checks)):
            c.fail(f"recovery_checks[{seen_check[ident][0]}].id", "unknown_reference", f"the plan has no recovery check {ident!r}")
        for ident in sorted(set(plan_checks) - set(seen_check)):
            c.gap("recovery_checks", f"no entry for the plan's recovery check {ident!r}")

    # ---- deviations and coverage gaps: explicit declarations, possibly "none"
    deviations_none = False
    if "deviations" in top:
        dev = top["deviations"]
        if dev == "none":
            deviations_none = True
        elif isinstance(dev, list) and dev:
            for i, item in enumerate(dev):
                entry = c.fields(item, f"deviations[{i}]", ("description",))
                if entry is not None:
                    c.text(entry, "description", f"deviations[{i}].description")
        else:
            c.fail("deviations", "value", "must be the word none or a non-empty list of {description}")
    if "coverage_gaps" in top:
        gaps = top["coverage_gaps"]
        if gaps != "none_declared":
            if isinstance(gaps, list) and gaps:
                for i, item in enumerate(gaps):
                    if not isinstance(item, str) or not item.strip():
                        c.fail(f"coverage_gaps[{i}]", "text", "must be non-empty text")
            else:
                c.fail("coverage_gaps", "value", "must be none_declared or a non-empty list of text")

    # ---- outcome: declared, never inferred, never upgraded
    out = c.fields(top.get("outcome", vp._ABSENT), "outcome", ("declared",), ("abort_criterion_id",))
    if out is not None:
        declared = c.enum(out, "declared", "outcome.declared", OUTCOMES)
        named = out.get("abort_criterion_id")
        if declared == "aborted":
            if "abort_criterion_id" not in out:
                c.gap("outcome.abort_criterion_id", "an aborted drill names the criterion that stopped it")
            elif c.text(out, "abort_criterion_id", "outcome.abort_criterion_id") is not None:
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
                 "plan_sha256": plan_report["plan_sha256"], "status": plan_report["status"]},
        "record": {"file": record_name, "file_sha256": hashlib.sha256(record_bytes).hexdigest(), "record_sha256": None},
        "derived": {"restore_minutes": None, "plan_max_restore_minutes": None, "restore_exceeds_plan_max": None},
        "reasons": [], "limitations": list(LIMITATIONS),
    }
    reasons = [_reason("plan", r) for r in plan_report["reasons"]]
    try:
        record = vp._load(record_bytes)
        vp._bounds(record)
        report["record"]["record_sha256"] = vp.plan_digest(record)
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
