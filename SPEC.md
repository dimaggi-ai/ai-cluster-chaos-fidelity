# Chaos experiment spec — v1.0

The contract for a single AI-cluster chaos experiment. A conforming spec is one
YAML file that the [linter](catalog/validate.py) accepts. The linter is the
normative reference; this document explains it.

## Version

`SPEC v1.0`. The schema below is stable within a major version. Additive fields
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

**`layer`** — `accelerator`, `fabric`, `overlay-network`, `orchestration`,
`training-graph`, `inference-graph`, `data-artifacts`, `control-plane`.

**`injection.technique`** — `dcgm-inject`, `pod-kill`, `pod-failure`,
`process-kill`, `cpu-sleep`, `networkchaos-overlay`, `chaosd-nic`,
`ib-link-down`, `iochaos`, `stresschaos`, `gpu-burn`, `timechaos`, `dnschaos`,
`httpchaos`, `artifact-swap`, `rag-degrade`, `agent-proposal`.

## The three rules the linter enforces

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
   `artifact-swap`.

3. **Agent safety.** Physically-stressing techniques (`ib-link-down`,
   `gpu-burn`, `chaosd-nic`) must set `agent_forbidden: true` — an autonomous
   planner may fire a software DCGM injection, never a saturating burn or a
   real link-down on production fabric.

## Fidelity caveat, normative

A spec's `certifies` field must describe what the injection *actually* exercises,
not what a reader might assume. In particular: `dcgm-inject` certifies the
telemetry → remediation loop on a *reported* fault, not device physics or silent
corruption; `chaosd-nic`/`networkchaos-overlay` certify the IP path, not the
RDMA collective path. Overclaiming in `certifies` defeats the standard even when
the schema validates.

## Conformance

A file conforms to SPEC v1.0 iff `python3 catalog/validate.py <file>` exits 0.
The [reference catalog](catalog/experiments/) is the set of conforming examples;
the [rejection tests](catalog/test_validate.py) are the conformance tests for the
linter itself.
