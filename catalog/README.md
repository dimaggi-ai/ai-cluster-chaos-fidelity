# Experiment catalog

Machine-checkable chaos experiment specs, one YAML per experiment, plus a
validator that enforces the repo's [fidelity taxonomy](../docs/fidelity-taxonomy.md).

## Run

```
pip install pyyaml
python3 validate.py                       # validate every spec in experiments/
python3 validate.py experiments/decode-replica-kill.yaml   # one spec
python3 test_validate.py                  # prove wrong specs are rejected
```

## What the validator enforces

1. **Completeness (the demo rule).** Every spec must carry an `owner`, a
   `duration` cap, an `abort` condition, and an automated `undo`. Missing any
   one, the spec is refused — it is a demo, not an experiment.
2. **Fidelity lint.** If the `wanted_fault` and the `injection.technique` live
   on different layers, the spec is rejected with the reason (an IB flap on the
   overlay, an XID via pod-kill, a desync via GPU reset all fail).
3. **Agent safety.** Physically-stressing techniques (`chaosd-nic`, `gpu-burn`,
   `ib-link-down`, `ocs-port-retune`, `circuit-disable`, `feeder-open`,
   `cdu-stop`) must set `agent_forbidden: true` — an autonomous planner may
   never fire them.
4. **Layer map.** The technique must live on the layer the spec declares — a
   fault maps to one layer, and filing a real injection under the wrong one
   inherits that layer's coverage claim without testing it.

## Spec fields

| Field | Meaning |
|---|---|
| `name` | unique id (matches filename) |
| `layer` | one of the eleven cluster layers |
| `wanted_fault` | the real fault this experiment stands in for |
| `injection.tool` / `.technique` | how it is injected; technique from the known set |
| `blast_radius` | scope — one GPU / one rank / one replica first |
| `duration` | time cap (numeric) |
| `steady_state` | list of contracts that must hold |
| `abort` | the kill switch condition |
| `undo` | automated restore path |
| `owner` | who runs it |
| `evidence` | the metrics/log files that are ground truth |
| `hardware_required` | true if it needs real accelerators/fabric |
| `certifies` | the recovery behavior this promotes |
| `autonomy_class` | L0–L3: how far this may be auto-promoted (see the governed-autonomy sequel) |
| `agent_forbidden` | required true for physically-stressing techniques |

The `hardware_required` flag is honest bookkeeping: most accelerator and fabric
experiments need NVIDIA hardware and a real fabric. The specs, the taxonomy, and
the validator run anywhere; the injections that touch silicon do not.

## Coverage — and what is deliberately out of scope

The 25 specs span all eleven layers, but not evenly: accelerator (4), orchestration
(3), inference-graph (3), data-artifacts (3), training-graph (2), control-plane (2),
overlay-network (2), facility (2), inter-hall-circuit (2), fabric (1),
shared-storage (1). Coverage of a layer is a starting point, not a claim of
completeness. Two deliberate pairs demonstrate the standard's sharpest rules:
[`ib-link-flap`](experiments/ib-link-flap.yaml) faults the real RDMA path while
[`ip-bootstrap-delay`](experiments/ip-bootstrap-delay.yaml) uses `tc`/`netem`
and certifies only the IP path; and [`metro-circuit-cut`](experiments/metro-circuit-cut.yaml)
takes a circuit *between* halls away while
[`ocs-retune-window`](experiments/ocs-retune-window.yaml) only moves one — a cut
and a retune are two experiments, and so is
[`ib-link-flap`](experiments/ib-link-flap.yaml) versus either of them (inside a
hall versus between halls). [`pfs-metadata-stall`](experiments/pfs-metadata-stall.yaml)
carries the rule the v1.1 extension exists for: **a PFS stall is not an ICI
break** — every rank blocks at once on a healthy fabric, and a spec that
certifies it with a fabric injection is rejected.

Failure classes **not yet catalogued**, named here so the omission is deliberate
rather than implied:

- **Computational silent data corruption (SDC)** — a device silently producing wrong
  matmul results that poison gradients with *no* error signal ([16] in ../REFERENCES.md).
  `ckpt-bitflip` covers checkpoint *file* bit-rot, which is detectable by checksum;
  computational SDC is the harder, undetectable-at-the-checkpoint case and deserves its
  own spec with explicit fidelity limits.
- **Persistent slow node (gray failure)** — a thermally-throttled or slow-NIC node that
  never *fails* but drags the whole collective to its speed. `cpu-desync-sleep` is a hard
  stop, not a persistent degradation.
- **Sustained power-cap throttle** — `hall-feeder-open` covers the feed going away;
  a hall running *derated* for hours is a gray degradation, closer to the slow-node
  case than to an outage, and needs its own steady-state contract.
- **Restoration-scale events** — `metro-circuit-cut` certifies the re-pin onto the
  surviving circuits, not the hours-later restoration; `hall-feeder-open` certifies
  the restart order, not a grid event taking several sites at once.

Three gaps named in v1.0 are now catalogued rather than listed: correlated
multi-node failure ([`hall-feeder-open`](experiments/hall-feeder-open.yaml) —
plural by construction), the shared-store outage
([`pfs-metadata-stall`](experiments/pfs-metadata-stall.yaml) — server-side, every
rank at once, where `ckpt-eio` is per-pod), and the cooling event
([`coolant-loop-loss`](experiments/coolant-loop-loss.yaml) — the ride-through
clock).

These are tracked for future catalog versions (and modelled in the companion
reliability-economics study); listing them here is the honest alternative to a
coverage matrix that looks complete because it is small.

## What the fidelity lint does and does not catch

The fidelity rules match on keywords in `wanted_fault`, not on meaning. That is
enough to stop the mistakes that actually happen — an IB flap injected on the
overlay, an XID faked with a pod kill — because those specs describe the real
fault in the obvious words. It is not a proof: a spec that hides its intent
("silent file corruption on save" with no `checkpoint`/`ckpt`/`artifact` in the
text) can slip a wrong technique through, and a spec that names a layer only to
disclaim it ("object-store latency on the overlay, not the fabric") can trip a
false positive. Write `wanted_fault` in plain terms and the lint does its job;
treat it as a fast guardrail, not a theorem prover. The reviewer of a spec is
still the reviewer.
