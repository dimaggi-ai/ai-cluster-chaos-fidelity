#!/usr/bin/env python3
"""Plan-only validator for disconnection drills.

It reads a drill plan (YAML) and answers one question: is this plan
complete and well-formed enough for people to review? It never runs a
drill, contacts a network, touches a cluster or writes a file.

The only passing status is ``plan_valid_not_run``. A valid plan is not a
drill result, not proof that a host is isolated, not a compliance claim and
not an authorisation to run anything. Role declarations are names written in
a file; they do not authenticate anyone.

Usage:
    python3 drills/validate_plan.py PLAN.yaml [PLAN.yaml ...]
Prints one JSON report. Exit 0 = every plan is plan_valid_not_run;
1 = at least one plan is invalid; 2 = usage error.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

import yaml

SCHEMA_VERSION = "drill-plan/v1"
REPORT_VERSION = "drill-plan-report/v1"
EVIDENCE_TEMPLATE = "drill-evidence/v1"
VALID = "plan_valid_not_run"
INVALID = "invalid"

MAX_FILE_BYTES = 64 * 1024
MAX_ITEMS = 32          # any list or mapping
MAX_DEPTH = 8
MAX_STRING = 2000
MAX_DURATION_HOURS = 744  # 31 days: a plan, not a survival claim
DURATION_TOLERANCE_HOURS = 1e-9

ENVIRONMENTS = ("staging", "isolated-lab")
SCENARIOS = ("vendor-control-plane-disconnection",)
COMPARATORS = (">", ">=", "<", "<=")
ROLE_NAMES = ("approver", "operator", "verifier")
CHECK_ROLES = ("approver", "verifier")  # never the operator who ran it
NON_HUMAN_PREFIXES = ("agent:", "bot:", "service:", "svc:", "system:", "automation:")

# Field names that would make a plan executable. Refused wherever they appear,
# with their own reason, before the general unknown-field rule.
EXECUTABLE_FIELDS = frozenset({
    "command", "commands", "cmd", "exec", "execute", "script", "scripts", "run", "shell",
    "endpoint", "endpoints", "url", "urls", "host", "hosts", "address", "target", "targets",
    "inject", "injection", "injector", "technique", "action", "actions", "kubectl", "api",
    "apply", "manifest", "playbook", "hook", "hooks",
})
# Observed results belong in a separate evidence record. A plan never carries them,
# and this validator has no result importer.
RESULT_FIELDS = frozenset({
    "result", "results", "outcome", "outcomes", "passed", "status", "success", "verdict", "observed",
})

LIMITATIONS = [
    "plan_valid_not_run means the plan is well-formed for review; no drill was run.",
    "This is not evidence of continuity, isolation, restoration or survival for any period.",
    "Role names are declarations in a file; they are not authenticated identities or approvals.",
    "Free-text procedures are not evaluated or executed.",
    "Not a compliance, certification or qualification claim.",
]


class _Reject(Exception):
    def __init__(self, path: str, code: str, message: str):
        super().__init__(message)
        self.path, self.code, self.message = path, code, message


# ---------------------------------------------------------------- strict YAML
class _StrictLoader(yaml.SafeLoader):
    """SafeLoader with no anchors/aliases, no duplicate keys, no implicit timestamps."""


# Timestamps stay strings, so the validator parses them itself (aware, UTC).
_StrictLoader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _compose_node(self, parent, index):
    if self.check_event(yaml.events.AliasEvent):
        event = self.peek_event()
        raise _Reject("", "yaml_alias", f"YAML aliases are not allowed (line {event.start_mark.line + 1})")
    event = self.peek_event()
    anchor = getattr(event, "anchor", None)
    if anchor is not None:
        raise _Reject("", "yaml_anchor", f"YAML anchors are not allowed (line {event.start_mark.line + 1})")
    return yaml.composer.Composer.compose_node(self, parent, index)


def _construct_mapping(self, node, deep=False):
    if not isinstance(node, yaml.MappingNode):
        raise _Reject("", "yaml_shape", "expected a mapping")
    seen = set()
    for key_node, _ in node.value:
        key = self.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            raise _Reject("", "key_type", f"mapping keys must be strings (line {key_node.start_mark.line + 1})")
        if key in seen:
            raise _Reject("", "duplicate_key", f"duplicate key {key!r} (line {key_node.start_mark.line + 1})")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(self, node, deep=deep)


_StrictLoader.compose_node = _compose_node
_StrictLoader.construct_mapping = _construct_mapping
_StrictLoader.add_constructor("tag:yaml.org,2002:map", lambda loader, node: _construct_mapping(loader, node, deep=True))


def _load(data: bytes):
    if len(data) > MAX_FILE_BYTES:
        raise _Reject("", "file_too_large", f"plan is {len(data)} bytes; the limit is {MAX_FILE_BYTES}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _Reject("", "encoding", "plan is not UTF-8") from exc
    try:
        docs = list(yaml.load_all(text, Loader=_StrictLoader))
    except _Reject:
        raise
    except yaml.YAMLError as exc:
        raise _Reject("", "yaml_syntax", f"not valid YAML: {exc.__class__.__name__}") from exc
    except RecursionError as exc:
        raise _Reject("", "too_deep", "plan nests too deeply") from exc
    if len(docs) != 1:
        raise _Reject("", "yaml_documents", f"expected exactly one YAML document, found {len(docs)}")
    return docs[0]


def _bounds(value, path="", depth=0):
    """Size, depth, type and finiteness limits, before any meaning is read."""
    where = path or "(root)"
    if depth > MAX_DEPTH:
        raise _Reject(where, "too_deep", f"nesting deeper than {MAX_DEPTH}")
    if isinstance(value, dict):
        if len(value) > MAX_ITEMS:
            raise _Reject(where, "too_many_items", f"{len(value)} fields; the limit is {MAX_ITEMS}")
        for key, item in value.items():
            _bounds(item, f"{path}.{key}" if path else key, depth + 1)
    elif isinstance(value, list):
        if len(value) > MAX_ITEMS:
            raise _Reject(where, "too_many_items", f"{len(value)} items; the limit is {MAX_ITEMS}")
        for i, item in enumerate(value):
            _bounds(item, f"{path}[{i}]", depth + 1)
    elif isinstance(value, str):
        if len(value) > MAX_STRING:
            raise _Reject(where, "string_too_long", f"{len(value)} characters; the limit is {MAX_STRING}")
    elif isinstance(value, bool) or value is None:
        pass
    elif isinstance(value, int):
        if abs(value) > 10 ** 15:
            raise _Reject(where, "number_out_of_range", "integer out of range")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise _Reject(where, "nonfinite", "numbers must be finite (no .inf or .nan)")
    else:
        raise _Reject(where, "value_type", f"unsupported value type {type(value).__name__}")


# ---------------------------------------------------------------- meaning
_ABSENT = object()  # a missing block is reported once, as missing, not field by field


class _Checker:
    def __init__(self):
        self.reasons: list[dict] = []

    def fail(self, path, code, message):
        self.reasons.append({"path": path, "code": code, "message": message})

    def fields(self, obj, path, required, optional=()):
        """Check a mapping's keys. Returns the mapping, or None if it is absent or not one."""
        if obj is _ABSENT:
            return None
        if not isinstance(obj, dict):
            self.fail(path or "(root)", "shape", "expected a mapping")
            return None
        allowed = set(required) | set(optional)
        for key in obj:
            sub = f"{path}.{key}" if path else key
            if key.lower() in EXECUTABLE_FIELDS:
                self.fail(sub, "executable_field",
                          f"{key!r} would make the plan executable; drill plans are plan-only")
            elif key.lower() in RESULT_FIELDS:
                self.fail(sub, "result_field",
                          f"{key!r} looks like a drill result; results are never read from a plan")
            elif key not in allowed:
                self.fail(sub, "unknown_field", f"unknown field {key!r}")
        for key in required:
            if key not in obj:
                self.fail(f"{path}.{key}" if path else key, "missing", "required field is missing")
        return obj

    def text(self, obj, key, path):
        value = obj.get(key) if isinstance(obj, dict) else None
        if key in (obj or {}) and (not isinstance(value, str) or not value.strip()):
            self.fail(path, "text", "must be non-empty text")
            return None
        return value

    def number(self, obj, key, path, *, positive=True):
        if key not in (obj or {}):
            return None
        value = obj[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.fail(path, "number", "must be a number")
            return None
        if positive and not value > 0:
            self.fail(path, "not_positive", "must be greater than zero")
            return None
        return float(value)

    def items(self, obj, key, path):
        if key not in (obj or {}):
            return []
        value = obj[key]
        if not isinstance(value, list) or not value:
            self.fail(path, "list", "must be a non-empty list")
            return []
        return value

    def ids(self, entries, path):
        seen = set()
        for i, entry in enumerate(entries):
            ident = entry.get("id") if isinstance(entry, dict) else None
            if isinstance(ident, str):
                if ident in seen:
                    self.fail(f"{path}[{i}].id", "duplicate_id", f"id {ident!r} is used twice")
                seen.add(ident)


def _utc(value):
    """Aware ISO 8601 time -> UTC datetime; None if naive or malformed."""
    if not isinstance(value, str) or "T" not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00") if value.endswith("Z") else value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _check(plan) -> list[dict]:
    c = _Checker()
    top = c.fields(plan, "", ("schema_version", "mode", "plan_id", "title", "environment", "scenario",
                              "window", "preconditions", "abort_criteria", "restore", "recovery_checks",
                              "roles", "evidence"), ("synthetic", "notes"))
    if top is None:
        return c.reasons
    if "schema_version" in top and top["schema_version"] != SCHEMA_VERSION:
        c.fail("schema_version", "schema_version", f"must be {SCHEMA_VERSION!r}")
    if "mode" in top and top["mode"] != "plan-only":
        c.fail("mode", "mode", "must be 'plan-only'; this validator never accepts an executable mode")
    for key in ("plan_id", "title", "notes"):
        c.text(top, key, key)
    if "synthetic" in top and not isinstance(top["synthetic"], bool):
        c.fail("synthetic", "boolean", "must be true or false")

    env = c.fields(top.get("environment", _ABSENT), "environment", ("class", "description"))
    if env is not None:
        cls = env.get("class")
        if "class" in env and cls not in ENVIRONMENTS:
            code = "production_refused" if isinstance(cls, str) and "prod" in cls.lower() else "environment"
            c.fail("environment.class", code, f"must be one of {list(ENVIRONMENTS)}; production is refused")
        c.text(env, "description", "environment.description")

    scen = c.fields(top.get("scenario", _ABSENT), "scenario", ("kind", "description", "severed_links", "excluded_links"))
    if scen is not None:
        if "kind" in scen and scen["kind"] not in SCENARIOS:
            c.fail("scenario.kind", "scenario", f"must be one of {list(SCENARIOS)}")
        c.text(scen, "description", "scenario.description")
        severed, excluded = [], []
        for key, bucket in (("severed_links", severed), ("excluded_links", excluded)):
            for i, link in enumerate(c.items(scen, key, f"scenario.{key}")):
                entry = c.fields(link, f"scenario.{key}[{i}]", ("name", "plane", "description"))
                if entry is None:
                    continue
                c.text(entry, "name", f"scenario.{key}[{i}].name")
                c.text(entry, "description", f"scenario.{key}[{i}].description")
                plane = entry.get("plane")
                want = "control" if key == "severed_links" else "data"
                if "plane" in entry and plane != want:
                    c.fail(f"scenario.{key}[{i}].plane", "plane",
                           f"{key} must all be {want}-plane links; this scenario never severs the data plane"
                           if key == "severed_links" else f"{key} must be data-plane links kept up")
                if isinstance(entry.get("name"), str):
                    bucket.append(entry["name"])
        for name in sorted(set(severed) & set(excluded)):
            c.fail("scenario.excluded_links", "link_conflict", f"link {name!r} is both severed and excluded")

    win = c.fields(top.get("window", _ABSENT), "window", ("start", "end", "duration_hours"))
    if win is not None:
        start = _utc(win.get("start")) if "start" in win else None
        end = _utc(win.get("end")) if "end" in win else None
        if "start" in win and start is None:
            c.fail("window.start", "time", "must be an ISO 8601 time with a UTC offset (for example 2026-12-01T09:00:00Z)")
        if "end" in win and end is None:
            c.fail("window.end", "time", "must be an ISO 8601 time with a UTC offset")
        hours = c.number(win, "duration_hours", "window.duration_hours")
        if start and end:
            span = (end - start).total_seconds() / 3600
            if span <= 0:
                c.fail("window.end", "window_order", "end must be after start")
            elif hours is not None and abs(span - hours) > DURATION_TOLERANCE_HOURS:
                c.fail("window.duration_hours", "duration_mismatch",
                       f"duration_hours is {hours:g} but the window spans {span:g} hours in UTC")
        if hours is not None and hours > MAX_DURATION_HOURS:
            c.fail("window.duration_hours", "duration_too_long", f"at most {MAX_DURATION_HOURS} hours per plan")

    pre = c.items(top, "preconditions", "preconditions")
    for i, item in enumerate(pre):
        entry = c.fields(item, f"preconditions[{i}]", ("id", "description"))
        if entry is not None:
            c.text(entry, "id", f"preconditions[{i}].id")
            c.text(entry, "description", f"preconditions[{i}].description")
    c.ids(pre, "preconditions")

    aborts = c.items(top, "abort_criteria", "abort_criteria")
    for i, item in enumerate(aborts):
        p = f"abort_criteria[{i}]"
        entry = c.fields(item, p, ("id", "signal", "comparator", "threshold", "unit", "sustained_minutes"),
                         ("description",))
        if entry is None:
            continue
        for key in ("id", "signal", "unit", "description"):
            c.text(entry, key, f"{p}.{key}")
        if "comparator" in entry and entry["comparator"] not in COMPARATORS:
            c.fail(f"{p}.comparator", "comparator", f"must be one of {list(COMPARATORS)}")
        c.number(entry, "threshold", f"{p}.threshold", positive=False)
        c.number(entry, "sustained_minutes", f"{p}.sustained_minutes")
    c.ids(aborts, "abort_criteria")

    rest = c.fields(top.get("restore", _ABSENT), "restore", ("procedure", "max_restore_minutes"))
    if rest is not None:
        c.text(rest, "procedure", "restore.procedure")
        c.number(rest, "max_restore_minutes", "restore.max_restore_minutes")

    roles = c.fields(top.get("roles", _ABSENT), "roles", ("approver", "operator"), ("verifier",))
    declared = {}
    if roles is not None:
        for name in ROLE_NAMES:
            if name not in roles:
                continue
            p = f"roles.{name}"
            entry = c.fields(roles[name], p, ("kind", "name", "reference"))
            if entry is None:
                continue
            if "kind" in entry and entry["kind"] != "human":
                c.fail(f"{p}.kind", "role_not_human", "drill roles must be declared human; agents may not hold them")
            for key in ("name", "reference"):
                value = c.text(entry, key, f"{p}.{key}")
                if isinstance(value, str) and value.strip().lower().startswith(NON_HUMAN_PREFIXES):
                    c.fail(f"{p}.{key}", "role_not_human", "looks like an agent or service identity")
            if isinstance(entry.get("reference"), str):
                declared[name] = entry["reference"].strip().lower()
        refs = list(declared.values())
        for ref in sorted({r for r in refs if refs.count(r) > 1}):
            c.fail("roles", "roles_not_distinct", f"one person ({ref!r}) is declared in more than one role")

    checks = c.items(top, "recovery_checks", "recovery_checks")
    for i, item in enumerate(checks):
        p = f"recovery_checks[{i}]"
        entry = c.fields(item, p, ("id", "check", "checked_by_role"))
        if entry is None:
            continue
        c.text(entry, "id", f"{p}.id")
        c.text(entry, "check", f"{p}.check")
        role = entry.get("checked_by_role")
        if "checked_by_role" in entry:
            if role not in CHECK_ROLES:
                c.fail(f"{p}.checked_by_role", "check_not_independent",
                       f"must be one of {list(CHECK_ROLES)}: the operator does not verify their own recovery")
            elif roles is not None and role not in roles:
                c.fail(f"{p}.checked_by_role", "role_undeclared", f"role {role!r} is not declared in roles")
    c.ids(checks, "recovery_checks")

    ev = c.fields(top.get("evidence", _ABSENT), "evidence", ("template",))
    if ev is not None and "template" in ev and ev["template"] != EVIDENCE_TEMPLATE:
        c.fail("evidence.template", "evidence_template", f"must be {EVIDENCE_TEMPLATE!r}")
    return c.reasons


# ---------------------------------------------------------------- public API
def plan_digest(plan) -> str:
    """Stable digest of the parsed plan: key order, quoting and comments do not change it."""
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def validate_bytes(data: bytes, name: str = "<input>") -> dict:
    report = {"report_version": REPORT_VERSION, "file": name,
              "file_sha256": hashlib.sha256(data).hexdigest(), "plan_sha256": None,
              "status": INVALID, "reasons": [], "limitations": list(LIMITATIONS)}
    try:
        plan = _load(data)
        _bounds(plan)
    except _Reject as exc:
        report["reasons"] = [{"path": exc.path or "(file)", "code": exc.code, "message": exc.message}]
        return report
    report["plan_sha256"] = plan_digest(plan)
    reasons = _check(plan)
    report["reasons"] = sorted(reasons, key=lambda r: (r["path"], r["code"], r["message"]))
    report["status"] = INVALID if reasons else VALID
    return report


def validate_path(path) -> dict:
    path = Path(path)
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            return {"report_version": REPORT_VERSION, "file": path.name, "file_sha256": None,
                    "plan_sha256": None, "status": INVALID, "limitations": list(LIMITATIONS),
                    "reasons": [{"path": "(file)", "code": "file_too_large",
                                 "message": f"plan is {size} bytes; the limit is {MAX_FILE_BYTES}"}]}
        data = path.read_bytes()
    except OSError as exc:
        return {"report_version": REPORT_VERSION, "file": path.name, "file_sha256": None,
                "plan_sha256": None, "status": INVALID, "limitations": list(LIMITATIONS),
                "reasons": [{"path": "(file)", "code": "unreadable", "message": exc.__class__.__name__}]}
    return validate_bytes(data, path.name)


def main(argv: list[str]) -> int:
    if not argv or any(a in ("-h", "--help") for a in argv):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    reports = [validate_path(a) for a in argv]
    print(json.dumps({"report_version": REPORT_VERSION, "reports": reports}, indent=2, sort_keys=True))
    return 0 if all(r["status"] == VALID for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
