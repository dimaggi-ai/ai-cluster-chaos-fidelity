# Disconnection drills: plan first, inject later

## Why

Operators increasingly claim that a cluster keeps working when it loses its connection to the vendor's control plane: no management API, no update channel, no licence check. A claim like that is only as good as the drill that tested it. A drill that cuts the wrong link, or cuts it in production, can cause the outage it was meant to rehearse.

This standard already says that an injection is only as good as the layer it actually hits. The same applies here. A disconnection drill must say exactly which links it cuts (control plane), which it keeps (data plane), when it stops, how it is undone and who checks the result. Otherwise, whatever result it produces cannot be trusted.

## What ships now

Only the plan check (see [drills/README.md](../drills/README.md)). A plan is a YAML file. `drills/validate_plan.py` refuses it unless:

- it is plan-only and targets staging or an isolated lab, never production;
- it cuts control-plane links only and lists the data-plane links that stay up;
- its window is in UTC and its stated duration matches the window;
- every abort criterion is measurable: a signal, a comparator, a number, a unit and how long it must hold;
- it has a restore procedure with a time limit, and recovery checks done by someone other than the operator;
- it declares distinct human approver and operator roles;
- it carries no executable fields and no results.

The report gives a stable plan digest, so reviewers and a later evidence record can refer to the exact plan.

## What does not ship, on purpose

- **No fault injection, and no execution path of any kind.** Injection comes only after plans have been reviewed by the people who would run them.
- **No result importer.** Observed results go in a human-filled [evidence record](../drills/evidence-template.md), which no tool here reads.
- **No identity checks.** Role entries are names in a file. Confirming who people are and that they approved belongs to the operator's own access controls.
- **No link to live systems.** Nothing here can block, delay or gate a real outage response.

## Limits

- `plan_valid_not_run` is a statement about a document. It is not evidence that a cluster survives disconnection, that anything is isolated, or that any period (72 hours or otherwise) is safe.
- Free text (descriptions, the restore procedure, checks) is not evaluated. A plan can be well-formed and still wrong; that is what human review is for.
- One scenario in v1: vendor control-plane disconnection. Others (revoked keys, blocked updates, zone loss) would need their own reviewed schema versions.
- Not a compliance, certification or qualification claim.

## Reproduce

```bash
pip install pyyaml
python3 -m unittest discover -s drills -p test_validate_plan.py
python3 drills/validate_plan.py drills/plans/vendor-severance-72h.yaml          # exit 0
python3 drills/validate_plan.py drills/plans/invalid/production-environment.yaml # exit 1
```
