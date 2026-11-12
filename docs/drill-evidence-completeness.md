# Drill evidence completeness

A disconnection drill produces two documents: the plan reviewed beforehand ([disconnection-drills.md](disconnection-drills.md)), and the record people write afterwards. This step checks the second against the first. It answers a narrow question: **does the record declare everything the plan asked for, and is it bound to that exact plan?**

It deliberately does not answer whether the drill went well.

## Why completeness comes before judgement

A drill record is usually reviewed by people who were not in the room. Before anyone can judge it, they need to know two things. Which plan, byte for byte, the record describes. And whether every abort criterion, recovery check and sign-off the plan named was recorded, or was explicitly declared not observed. Gaps that are found late are expensive. Silently filled gaps are worse, because they look like evidence.

So the checker:

- **Binds the record to the plan.** It checks the canonical `plan_sha256` and the exact `file_sha256`, and reports the difference. If a plan was only reformatted, its canonical digest still matches but its byte digest does not. The checker reports that as a mismatch for a person to resolve, not as a silent pass.
- **Requires one entry per plan item:** each abort criterion, recovery check, precondition and declared role. Every missing entry is reported, not just the first.
- **Treats `not_observed` as an honest answer that still leaves a gap.** The record is then `incomplete`. Nothing is defaulted or interpolated.
- **Keeps observations of the drill inside the plan's window.** Restoration and recovery checks may legitimately happen after the window closes. Restore time is reported against the plan's `max_restore_minutes` as a fact, not a verdict.
- **Echoes the declared outcome and never upgrades it.** An aborted drill, or a recovery check recorded as not verified, can be a complete record.

## Statuses

`invalid` → `mismatch` → `incomplete` → `evidence_complete_not_judged`, most severe first. All reasons are kept, each tagged with its class, so one pass shows every problem. [drills/record-schema.md](../drills/record-schema.md) has the full contract.

## Limits

- Everything in a record is a human declaration. References are labels and are never opened. Role references are compared as text and do not authenticate anyone.
- `evidence_complete_not_judged` is not a pass or fail. It is not an SLO result, not proof of isolation or restoration, and not a compliance, certification or acceptance claim.
- There is no telemetry import, no fault execution, no network, no signing and no subprocess. Records live in files that people write.
- An agent can read the reasons, propose a corrected record and check it again. It cannot change the rules, the plan or the status, and it cannot promote a record.

## Try it

```
python3 drills/validate_record.py drills/plans/vendor-severance-72h.yaml drills/records/vendor-severance-72h-complete.yaml
python3 drills/validate_record.py drills/plans/vendor-severance-72h.yaml drills/records/vendor-severance-72h-aborted.yaml
python3 drills/validate_record.py drills/plans/vendor-severance-72h.yaml drills/records/vendor-severance-72h-not-observed.yaml
python3 drills/validate_record.py drills/plans/vendor-severance-72h.yaml drills/records/vendor-severance-72h-not-run.yaml
python3 -m unittest discover -s drills -p test_validate_record.py
```

The first two records are `evidence_complete_not_judged`; the second is an aborted drill with a failed recovery check. The last two are `incomplete`. All four are synthetic. CI does not run these yet: this change adds new files only, and adding a CI step would edit the shared workflow (see the pull request).
