# Drill evidence record `drill-record/v1`

A structured, human-filled record of one drill, checked against the exact plan it claims to follow. [`validate_record.py`](validate_record.py) checks that the record is **complete and bound to that plan**. It never judges whether the drill went well.

[evidence-template.md](evidence-template.md) stays the human guide to what to write down. It is never parsed. This file is the machine-readable form of the same sections.

```
python3 drills/validate_record.py PLAN.yaml RECORD.yaml
```

Exit codes: 0 = `evidence_complete_not_judged`; 1 = `invalid`, `mismatch` or `incomplete`; 2 = usage error.

## Compatibility

- The version is frozen at `drill-record/v1`. Any other `schema_version` is `invalid`.
- Unknown fields are refused at every level.
- A field may be added only under a new version.
- The record is checked only against a `drill-plan/v1` plan that is `plan_valid_not_run`.
- Loading, size limits, file reading and time parsing are those of `validate_plan.py`, imported rather than copied. The limits are the same: 64 KiB, 32 items, depth 8, 2000-character strings, 64-character keys, integers up to 10^15, UTC years 2000 to 2100. So are the loader rules: no duplicate keys, anchors, aliases, `.inf` or `.nan`; one UTF-8 document; regular files only. The refusal codes are shared too, and are listed in [schema.md](schema.md).
- A field whose name would make the record executable (`command`, `script`, `url`, `host`, `action`, and so on) is refused as `executable_field`. Field names that look like results (`outcome`) are allowed here, unlike in a plan.

## Fields

`not_observed` is a literal value. It means a person declares that this was not observed. It always makes the record `incomplete`, and it is never replaced by a default.

| Field | Required | Rule |
|---|---|---|
| `schema_version` | yes | Exactly `drill-record/v1`. |
| `record_id` | yes | Non-empty text. |
| `synthetic`, `notes` | no | Boolean; text. |
| `plan.plan_id` | yes | Must equal the plan's `plan_id`. |
| `plan.plan_sha256`, `plan.file_sha256` | yes | 64 lowercase hex characters, copied from the plan validator's report. |
| `plan.validator_status` | yes | Exactly `plan_valid_not_run`. |
| `window.start`, `window.end` | yes | Aware ISO 8601 times naming the same instants as the plan's window. Any UTC offset is accepted. |
| `sign_offs.<role>` | one per role the plan declares | `reference` (must equal the plan's reference for that role, ignoring case and surrounding spaces), `confirmed_how` (text), `at` (time or `not_observed`). |
| `timeline.<event>` | yes, all five | `preconditions_confirmed`, `links_severed`, `ended`, `restore_started`, `restore_finished`. Each is a time or `not_observed`. |
| `preconditions[]` | one per plan precondition | `id`, `state` (`confirmed`, `not_confirmed` or `not_observed`), optional `reference`. |
| `abort_criteria[]` | one per plan criterion | `id`, `observed_extreme` (number or `not_observed`), `crossed` (`true`, `false` or `not_observed`), `response` (text; `none` is allowed). |
| `recovery_checks[]` | one per plan check | `id`, `checked_by_role` (as the plan assigns it), `checked_by_reference` (that role's plan reference, never the operator's), `state` (`verified`, `not_verified` or `not_observed`), `at` (time or `not_observed`), `reference` (text label). |
| `deviations` | yes | The word `none`, or a non-empty list of `{description}`. |
| `coverage_gaps` | yes | A non-empty list of text, or `none_declared`. |
| `outcome.declared` | yes | `completed_as_planned`, `aborted` or `not_run`. |
| `outcome.abort_criterion_id` | only when `aborted` | The plan criterion that stopped the drill. |

All `reference` values are inert labels. They are never opened, fetched or resolved, even when they look like paths or URLs.

## Time rules

All comparisons are made in UTC.

- **Inside the plan's window, inclusive:** `links_severed` and `ended`. These are observations of the drill itself.
- **May come before the window:** `preconditions_confirmed`. It must not come after `links_severed`, or after the window's end.
- **Order:** `preconditions_confirmed` ≤ `links_severed` ≤ `ended` ≤ `restore_started` ≤ `restore_finished`. Pairs where either side is `not_observed` are not compared.
- **May run past the window's end:**
  - Restoration. This is ordinary and is not a mismatch.
  - Recovery checks. Each `at` must not precede `restore_started`, or `ended` if no restore start was observed.
- **Sign-off times:** must be valid times. They are not ordered against the drill.

`max_restore_minutes` is compared, never judged. When both restore times are observed, the report's `derived` block gives `restore_minutes`, `plan_max_restore_minutes` and `restore_exceeds_plan_max`. Exceeding the plan's limit does not change the status. It is a fact for reviewers.

## Outcome rules

The declared outcome is echoed as `declared_outcome`. It is never inferred, changed or upgraded.

- **`aborted`:**
  - It must name an `abort_criterion_id`. If none is named, the record is `incomplete` (`missing_evidence`).
  - The criterion must exist in the plan (`unknown_reference`).
  - The criterion must not be recorded as `crossed: false` (`outcome_conflict`).
  - A complete aborted record, or a complete record whose recovery check is `not_verified`, is still `evidence_complete_not_judged`.
- **`not_run`:** no drill time (`links_severed`, `ended`, `restore_started`, `restore_finished`) may be observed (`outcome_conflict`). Every observation is then `not_observed`, so a not-run record is always `incomplete`, and says so.
- **`completed_as_planned`:** any criterion with `crossed: true` needs a recorded deviation; with `deviations: none` it is `outcome_conflict`.

## Statuses and precedence

Every reason found is kept. Each reason has a class. The status is the most severe class present, in this order:

1. **`invalid`:** the input cannot be read or does not follow this schema. This covers:
   - loader and limit refusals;
   - `unknown_field`, `missing`, `shape`, `value`, `text`, `number`, `boolean`, `digest`, `time`, `time_out_of_range`, `schema_version` and `executable_field`;
   - an invalid plan (`plan_not_valid`, with the plan's own reasons);
   - unreadable or non-regular files;
   - `internal_error`.
2. **`mismatch`:** the record conflicts with the plan or with itself:
   - identity: `plan_id_mismatch`, `plan_sha256_mismatch`, `file_sha256_mismatch`, `window_mismatch`;
   - references: `unknown_reference`, `duplicate_reference`, `role_undeclared`, `role_reference_mismatch`, `check_role_mismatch`, `check_not_independent`;
   - timeline: `outside_window`, `timeline_order`, `check_before_restore`;
   - outcome: `outcome_conflict`.
3. **`incomplete`:** required evidence is absent (`missing_evidence`) or declared `not_observed` (`not_observed`).
4. **`evidence_complete_not_judged`:** there are no reasons.

`plan_sha256_mismatch` means the record names a different plan. `file_sha256_mismatch` alone, with the message "canonical content matches", means the plan file was reformatted, re-commented or re-quoted after the record was written. Its meaning is the same, but its bytes are not. Both are mismatches, and re-recording the digests is a human decision.

Reasons are sorted by class (in the order above), then by input (`plan` before `record`), path, code and message. The same inputs always give the same report. `derived` is filled only when the status is not `invalid`.

## Report

```json
{
  "report_version": "drill-record-report/v1",
  "status": "evidence_complete_not_judged",
  "declared_outcome": "completed_as_planned",
  "plan":   {"file": "...", "file_sha256": "...", "plan_sha256": "...", "status": "plan_valid_not_run"},
  "record": {"file": "...", "file_sha256": "...", "record_sha256": "..."},
  "derived": {"restore_minutes": 42.0, "plan_max_restore_minutes": 60, "restore_exceeds_plan_max": false},
  "reasons": [{"class": "...", "input": "plan|record", "path": "...", "code": "...", "message": "..."}],
  "limitations": ["..."]
}
```

`record_sha256` is the canonical-JSON digest of the parsed record, computed the same way as a plan's `plan_sha256`. `file_sha256` covers the exact bytes.

## What completeness is not

Completeness is not a pass or a fail. It is not an SLO result, and not proof of isolation or restoration. It authenticates no one, and it is not a compliance or acceptance claim. A `coverage_gaps` or `deviations` entry is a declaration, not proof that nothing else was missed. An agent may read the reasons, propose a corrected record and check it again. It may not change these rules or promote a record.
