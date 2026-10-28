# Chaos experiment spec — v1.1

The contract for a single AI-cluster chaos experiment. A conforming spec is one
YAML file that the [linter](catalog/validate.py) accepts. The linter is the
normative reference; this document explains it.

## Version

`SPEC v1.1` (v1.0 plus the scale-across layers, techniques, fidelity rules, and the technique→layer check — all additive). The schema below is stable within a major version. Additive fields
(new optional keys, new enum members) may appear in minor versions; removing or
repurposing a field is a major-version change.

## Required fields

| Field | Type | Meaning |
|---|---|---|
| `name` | string | unique id, matches the filename |
| `layer` | enum | the cluster layer the fault lives on (see below) |
| `wanted_fault` | string | the real fault this experiment stands in for, in plain words |
| `injection.tool` | string | the tool used |
| `injection.technique` | enum | how it is injected (see below) |
| `injection.detail` | string | the concrete invocation (optional but expected) |
| `blast_radius` | string | scope — one GPU / one rank / one replica first |
| `duration` | string | a time cap carrying a number (e.g. `3m`) |
| `steady_state` | list | one or more contracts that must hold |
| `abort` | string | the kill-switch condition |
| `undo` | string | the automated restore path |
| `owner` | string | who runs it |
| `evidence` | string | the metrics/log files that are ground truth |
| `hardware_required` | bool | true if it needs real accelerators/fabric |
| `certifies` | string | the recovery behavior this promotes — and, honestly, what it does **not** |

## Optional fields

| Field | Type | Meaning |
|---|---|---|
| `autonomy_class` | `L0`–`L3` | how far this may be auto-promoted (governed-autonomy sequel) |
| `agent_forbidden` | bool | required `true` for physically-stressing techniques |
| `notes` | string | anything else |

## Enumerations

**`layer`** — `facility`, `accelerator`, `fabric`, `inter-hall-circuit`,
`overlay-network`, `shared-storage`, `orchestration`, `training-graph`,
`inference-graph`, `data-artifacts`, `control-plane`. Three distinctions carry
the v1.1 additions: `fabric` is the RDMA fabric *inside* one hall while
`inter-hall-circuit` is the optical transport *between* halls; `data-artifacts`
is one job's own files while `shared-storage` is the parallel filesystem every
rank mounts at once; and `facility` is the plant — power and cooling — under
all of it.

**`injection.technique`** — `dcgm-inject`, `pod-kill`, `pod-failure`,
`process-kill`, `cpu-sleep`, `networkchaos-overlay`, `chaosd-nic`,
`ib-link-down`, `iochaos`, `stresschaos`, `gpu-burn`, `timechaos`, `dnschaos`,
`httpchaos`, `artifact-swap`, `rag-degrade`, `agent-proposal`,
`ocs-port-retune`, `circuit-disable`, `feeder-open`, `cdu-stop`, `pfs-degrade`,
`load-replay`.

## The four rules the linter enforces

1. **Completeness (the demo rule).** A spec missing `owner`, `duration`,
   `abort`, or `undo` is a demo, not an experiment, and is rejected.

2. **Fidelity.** If the `wanted_fault` and the `injection.technique` live on
   different layers, the spec is rejected with the reason. The rules encode the
   [taxonomy](docs/fidelity-taxonomy.md): an RDMA/fabric fault requires a real
   link fault (`ib-link-down`), never `tc`/`netem` (`chaosd-nic`,
   `networkchaos-overlay`) — RDMA is kernel-bypass; an accelerator health fault
   requires `dcgm-inject` or `gpu-burn`; a rank death is a `pod-kill`/
   `process-kill`; a CPU desync must leave the GPU healthy (`cpu-sleep`); a
   checkpoint fault is `iochaos`/`artifact-swap`; serving the wrong model is
   `artifact-swap`. The v1.1 rules extend the same idea across the plant:
   a metro / inter-hall cut requires `circuit-disable` (a whole transport
   circuit, both halls' fabrics healthy); an OCS retune requires
   `ocs-port-retune` (the fault is the reconfiguration *window* — a cut and a
   retune are two experiments); a hall power loss requires `feeder-open`
   (simultaneity plus restart order, which pod kills reproduce neither of); a
   coolant-loop loss requires `cdu-stop` (the ride-through clock is the
   experiment); and a parallel-filesystem stall requires `pfs-degrade` —
   **a PFS stall is not an ICI break**, and certifying it as one retires the
   wrong risk.
   A load or traffic surge requires `load-replay` (generated demand against
   healthy components): `httpchaos` delays the requests already there and
   `pod-kill` removes capacity, so neither proves the shed path fires.

4. **Layer map.** The technique must live on the layer the spec declares
   (`ib-link-down` on `fabric`, `feeder-open` on `facility`, …). Each fault
   maps to one layer or CI fails: a real injection filed under the wrong layer
   would inherit that layer's coverage claim without ever testing it. Software
   techniques that genuinely serve several layers (`pod-kill`, `httpchaos`)
   carry a set of homes rather than a pretended single one.

3. **Agent safety.** Physically-stressing techniques (`ib-link-down`,
   `gpu-burn`, `chaosd-nic`, and the v1.1 plant set: `ocs-port-retune`,
   `circuit-disable`, `feeder-open`, `cdu-stop`) must set
   `agent_forbidden: true` — an autonomous planner may fire a software DCGM
   injection, never a saturating burn, a breaker, a coolant valve, or a mirror
   moved under a live collective. (`ocs-port-retune` is on the list even though
   a retune is routine: the companion governed-autonomy gate gives
   retune-live-collective no autonomy level at all, and this is the same act.)

## Fidelity caveat, normative

A spec's `certifies` field must describe what the injection *actually* exercises,
not what a reader might assume. In particular: `dcgm-inject` certifies the
telemetry → remediation loop on a *reported* fault, not device physics or silent
corruption; `chaosd-nic`/`networkchaos-overlay` certify the IP path, not the
RDMA collective path. Overclaiming in `certifies` defeats the standard even when
the schema validates.

## Conformance

A file conforms to SPEC v1.1 iff `python3 catalog/validate.py <file>` exits 0.
The [reference catalog](catalog/experiments/) is the set of conforming examples;
the [rejection tests](catalog/test_validate.py) are the conformance tests for the
linter itself.
