# Contributing to the Chaos Fidelity Standard

The standard grows by contribution. Two kinds are welcome: **new experiment
specs** (extending catalog coverage) and **new fidelity rules** (catching a
wrong-layer mistake the linter misses).

## Adding an experiment

1. Write a YAML spec under `catalog/experiments/` following [SPEC.md](SPEC.md).
2. Be honest in `certifies`: state what the injection actually exercises and
   what it does not. Overclaiming defeats the standard even if the schema
   validates.
3. Run `make test`. Your spec must pass `validate.py`, and you must not break
   the rejection tests.
4. If your experiment needs real hardware, set `hardware_required: true`; if the
   technique physically stresses hardware or the fabric, set
   `agent_forbidden: true`.

The bar for the catalog is *reference quality*: a spec should be something
another operator can copy and trust. Prefer closing a named coverage gap (see
[catalog/README.md](catalog/README.md#coverage--and-what-is-deliberately-out-of-scope))
— SDC, correlated power/cooling, stragglers, checkpoint-store outage.

## Adding a fidelity rule

A good rule catches a mistake that *looks* right. Add a `(pattern, allowed
techniques, reason)` entry to `FIDELITY_RULES` in `catalog/validate.py`, add a
rejection test to `catalog/test_validate.py` proving a wrong spec fails for the
stated reason, and update [docs/fidelity-taxonomy.md](docs/fidelity-taxonomy.md).
The linter is normative — every rule must have a test.

## Scope

This standard covers *fidelity* — which injection tests which layer. It does not
prescribe how often to run experiments, or price their outcomes; the companion
reliability-economics study does the latter. Keep PRs within scope.
