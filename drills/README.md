# Disconnection drills (plan-only)

This directory checks **drill plans**. It does not run drills.

A disconnection drill asks: if the links from a cluster to its vendor's control plane are cut for a set time, does the cluster keep serving, and can people put the links back safely? Before anyone runs such a drill, the plan should say exactly what is cut, what stays up, when it stops, how it is undone and who checks the result. `validate_plan.py` checks that the plan says all of that, in a form people can review.

```bash
python3 drills/validate_plan.py drills/plans/vendor-severance-72h.yaml
python3 -m unittest discover -s drills -p test_validate_plan.py
```

There is no `make` target: the `Makefile` is a hash-pinned artifact in `evidence.json`, so CI runs these two commands directly.

| Path | What it is |
|---|---|
| [validate_plan.py](validate_plan.py) | The validator. Reads YAML, prints a JSON report, exits 0 (valid) or 1 (invalid). Standard library plus PyYAML. |
| [test_validate_plan.py](test_validate_plan.py) | Tests for every rule, every fixture, and the safety properties. |
| [schema.md](schema.md) | The plan format, field by field, with every reason code. |
| [plans/vendor-severance-72h.yaml](plans/vendor-severance-72h.yaml) | A synthetic 72-hour example on staging. It validates. |
| [plans/invalid/](plans/invalid/) | Twelve plans that fail, each for one stated reason. |
| [evidence-template.md](evidence-template.md) | What a person records if a drill is later run. Not read by any tool. |
| [../docs/disconnection-drills.md](../docs/disconnection-drills.md) | Why plan-only first, and the limits. |

## What a valid result means, and what it does not

The only passing status is **`plan_valid_not_run`**. It means the plan is complete and well-formed for people to review. It does **not** mean:

- that a drill ran, passed or was approved;
- that the cluster can survive a disconnection for any period;
- that anything is isolated;
- that the people named in `roles` exist, agreed or were authenticated (role entries are declarations in a file);
- any compliance, certification or qualification.

The validator has no execution path and no result importer. It does not open network connections, call a cluster, read keys, write files or evaluate free text. Fields that would make a plan executable (`command`, `endpoint`, `inject`, `script` and similar) are refused, and so are fields that look like results (`result`, `status`, `passed` and similar).

The validator checks plans before anything happens. It cannot block a real outage, and it is not meant to: nothing here sits in the path of real traffic or real incident response.
