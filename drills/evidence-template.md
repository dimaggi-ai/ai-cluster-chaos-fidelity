# Drill evidence record `drill-evidence/v1`

A template for people to fill in **if** a validated plan is later run under its own approval. No tool in this repository reads, imports or checks a filled-in record. What is recorded here is what people observed; it is not validated by `validate_plan.py`.

Copy this file next to the plan, fill every section, and mark anything not observed as **not observed** rather than leaving it blank.

## 1. Plan identity

| Field | Value |
|---|---|
| `plan_id` | |
| `plan_sha256` (from the validator report) | |
| `file_sha256` (from the validator report) | |
| Validator report status | `plan_valid_not_run` |

If the plan changed after validation, its digests change: re-validate and record the new ones.

## 2. Approvals

| Role | Person | How and when they approved (link or reference) |
|---|---|---|
| Approver | | |
| Operator | | |
| Verifier | | |

The plan's role entries are declarations only. Record here how each person's identity and approval were actually confirmed.

## 3. Timeline (UTC)

| Time (UTC) | Event | Who |
|---|---|---|
| | Preconditions confirmed | |
| | Links severed | |
| | Abort or planned end | |
| | Restore started | |
| | Restore finished | |

## 4. Abort criteria, observed

One row for every `abort_criteria` entry in the plan.

| `id` | Signal | Threshold | Highest or lowest value observed | Crossed for the sustained time? | Action taken |
|---|---|---|---|---|---|
| | | | | | |

## 5. Deviations from the plan

Anything done differently from the plan, and why. Write "none" if there were none.

## 6. Restoration verification

One row for every `recovery_checks` entry. The person checking must hold the role the plan names, and must not be the operator.

| `id` | Checked by | Result | Evidence (link or reference) |
|---|---|---|---|
| | | verified / not verified | |

## 7. Coverage gaps

What this drill did not test: links not severed, workloads not present on staging, durations not reached, signals not collected.

## 8. Outcome

One of: **completed as planned**, **aborted** (give the criterion `id`), **not run**. An outcome describes this one drill in this one environment. It is not a claim about production, about other periods, or about compliance.

## 9. Review

| Reviewer | State (not reviewed / reviewed with findings / accepted) | Date (UTC) |
|---|---|---|
| | | |
