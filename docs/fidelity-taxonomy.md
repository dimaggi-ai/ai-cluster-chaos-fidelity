# The fidelity taxonomy: which injection actually hits which layer

Reference numbers point to [REFERENCES.md](../REFERENCES.md).

A chaos experiment certifies a recovery behavior. If the injection and the fault you care about live on different layers, you have certified the wrong behavior — and you will trust it in production. **Certifying the wrong technique is worse than certifying nothing**, because it retires the risk on paper.

This is the repository's central claim, and it is mechanical enough to enforce in software (see [`catalog/validate.py`](../catalog/validate.py)). The rest of this document is the reasoning behind the lint rules.

## An AI cluster fails on eleven layers, not one

| Layer | What fails | The signal it emits |
|---|---|---|
| Accelerator | XID, ECC/HBM, thermal, power, device drop | DCGM fields, XID log [5, 6] |
| Fabric | NVLink, InfiniBand/RoCE, optical, NCCL | link counters, NCCL transport errors [11] |
| Overlay network | pod-to-pod TCP, object store, control APIs | HTTP/gRPC errors, latency |
| Orchestration | gang placement, GPU DRA, topology, requeue | scheduler events, pending GPUs [13] |
| Training graph | rank death, desync, checkpoint stall | watchdog trips, loss/step drift [12, 10] |
| Inference graph | replica/role death, KV eviction, weight skew | TTFT/ITL, router state [14, 15] |
| Data & artifacts | wrong checkpoint, truncation, RAG lag | checksum failures, retrieval hit-rate |
| Control plane | the healer itself: bad cordon, retry storm | referee decisions, audit log |
| Inter-hall circuit | metro cut, OCS retune window, DWDM line fault | transport alarms, per-circuit counters — while both halls' fabrics stay green |
| Shared storage | PFS metadata stall, OST loss — every rank at once | MDS/OST latency, uniform stall across jobs |
| Facility | feeder open, coolant loop loss | switchgear and BMS telemetry, inlet temperature, the ride-through clock |

(The last three arrived in SPEC v1.1: they are where a cluster stops being one
hall. `fabric` is the RDMA fabric *inside* a hall; `inter-hall-circuit` is the
optical transport *between* halls. `data-artifacts` is one job's own files;
`shared-storage` is the mount every rank shares. `facility` is the plant under
all of it.)

Each layer emits a different signal and heals a different way. An injection is only as good as the layer it actually hits.

## The three canonical fidelity errors

### A pod kill is not an XID

Deleting a pod tests the orchestrator's restart path. It does **not** test the accelerator health path: no DCGM field moves, no XID is logged [5, 6], the cordon/drain controller never sees the signal it is built to consume. To certify "we cordon a lemon GPU before the next gang lands," you must inject at the device layer — a DCGM field injection [6], or the InfoROM-corruption pattern NVSentinel ships as a demo [8]. Both are software-only; neither harms the card. What they exercise is the *reaction to a reported fault*, which is exactly the loop you are promoting to autonomy.

### Overlay `NetworkChaos` is not an InfiniBand flap — and neither is `tc` on the RDMA NIC

Chaos Mesh `NetworkChaos` applies tc/netem inside the pod's network namespace [1]. That is the right tool for an HTTP router, an object-store client, or etcd. It is the **wrong** tool for a collective: NCCL runs over RDMA on the IB/RoCE NIC, not the pod overlay. Delaying the overlay tests bootstrap and socket setup (`NCCL_SOCKET_IFNAME`), never the AllReduce path.

There is a subtler version of this same error, and the standard catches it: **`tc`/`netem` on the host RDMA NIC still does not reach the collective.** RDMA is kernel-bypass — the verbs path goes straight from the HCA to user memory, around the kernel network stack that `tc`/`netem` (whether via `NetworkChaos` or Chaosd) operates on. So "Chaosd on the RDMA NIC" injects an *IP-path* fault, not a fabric fault; an AllReduce sails through it. To actually fault the collective's path you need a real link fault — a switch port bounce, `ibportstate` disable, or a vendor link-disable (`ib-link-down` in the catalog). That requires hardware and switch access, which is why it stays hardware-only and `agent_forbidden`. The [`ip-bootstrap-delay`](../catalog/experiments/ip-bootstrap-delay.yaml) and [`ib-link-flap`](../catalog/experiments/ib-link-flap.yaml) specs are deliberately paired to make the distinction concrete. This is the experiment people skip and then lose a week of training to, and it is the one where getting the *technique* wrong silently certifies nothing.

### A DCGM field injection is not a physical fault

DCGM error injection [6] writes a value into a DCGM field so the health watches and policies fire exactly as they would on a real ECC, PCIe, or thermal event. That is genuinely useful — it certifies the **telemetry → remediation control loop** (does the cordon/drain fire on a *reported* fault?), which is the loop you promote to autonomy. But be precise about what it does *not* establish: it does not create a real uncorrectable HBM error, a real XID 79, a stuck CUDA context, or — critically — a silent computational error that emits no telemetry at all. The catalog's DCGM specs say this in their `certifies` field ("the reaction to a *reported* fault, not survival of a real one"), and the taxonomy states the principle: **an injection certifies the layer it touches, and DCGM injection touches the telemetry plane, not device physics.** Certifying "we cordon on a reported ECC" is real and valuable; claiming it certifies "we survive HBM failure" or "we catch SDC" is the same category error in a lab coat.

### A GPU reset is not a CPU desync

Meta's fleet data is unambiguous: **most NCCL watchdog timeouts are collective desync from CPU-side divergence, not a slow or hung collective** — over 60% across all training frameworks [12]. A healer that responds to every watchdog timeout by cordoning the node will quarantine healthy hardware the majority of the time. To certify the classifier that tells these apart, you must inject the desync *without* touching the GPU: stall or `SIGSTOP` one rank's CPU before its next collective (a dataloader stall or a checkpoint fsync). Resetting the GPU injects a different fault and certifies a different — wrong — response.

## The scale-across errors (the same mistake, between halls)

The three canonical errors above all live inside one hall. Stretch the cluster
across halls and the identical category error reappears wearing new words —
"the link went down" now means five different faults:

- **A metro cut is not a leaf flap.** A cut takes a whole transport circuit
  away and leaves the intra-hall fabric on *both* sides perfectly healthy;
  a port bounce inside a hall does the opposite. Different topology, different
  owner (transport, not fabric), different recovery (re-pin onto surviving
  circuits, not a rail cordon). The linter requires `circuit-disable` for one
  and `ib-link-down` for the other, and a `wanted_fault` worded as both
  ("inter-hall fabric cut") is refused with either technique — say which fault
  you mean.
- **An OCS retune is not a cut.** A retune is a reconfiguration *window*: the
  circuit comes back by itself, and the experiment is the gap and the
  re-establishment against the two clocks. A disabled circuit certifies a loss
  that never self-recovers. One command, two different experiments
  ([`ocs-retune-window`](../catalog/experiments/ocs-retune-window.yaml) vs
  [`metro-circuit-cut`](../catalog/experiments/metro-circuit-cut.yaml)).
- **A PFS stall is not an ICI break.** When the shared parallel filesystem
  stalls, every rank blocks at the same instant on a *healthy* compute fabric —
  the signal that distinguishes it from a fabric fault is precisely that the
  fabric counters do not move. Certify it with a fabric injection and you have
  retired the wrong risk in the wrong quarter. Server-side degradation
  (`pfs-degrade`) is the only faithful injection; `IOChaos` on one pod's volume
  is the per-job checkpoint experiment, which already exists on
  `data-artifacts`.
- **A hall power loss is not N pod kills.** Opening a feeder drops many
  individually healthy nodes at the same instant, and what recovery certifies
  is the restart *order* on the way back (storage and control plane before
  trainers). Killing pods one at a time reproduces neither the simultaneity nor
  the order.
- **A coolant-loop loss is a clock, not a temperature.** The hall has only its
  thermal buffer — seconds at direct-to-chip density, minutes on legacy air
  (the companion cooling study measures ~11 s vs ~250 s at each rung's own
  ceiling). The experiment is whether load sheds *inside* that window. A
  `gpu-burn` changes the heat, not the cooling, and hands you no window to
  miss.

## The two clocks (why the fabric experiment matters)

When a rank dies or a rail drops, two independent timers race:

- The **PyTorch NCCL watchdog** default is **600 s** (10 minutes) [10].
- The **InfiniBand transport** gives up far sooner: `NCCL_IB_TIMEOUT` computes as 4.096 µs × 2^timeout, default 20 since NCCL 2.23, with 7 retries — the QP errors out in roughly **30 seconds** [11].

If you only tune the watchdog, you are tuning the slower clock and testing the wrong timer. The [`ib-link-flap`](../catalog/experiments/ib-link-flap.yaml) experiment exists to prove the transport clock fires first and the controller marks a *rail*, not a *GPU*. That detection gap — roughly nine minutes of frozen fleet per event at the default — is what the companion reliability-economics study prices in GPU-hours.

## The rule, as the validator enforces it

```
wanted fault           acceptable injection            rejected injection
uncorrectable ECC       dcgm-inject / gpu-burn          overlay packet loss, pod-kill
GPU off bus (XID 79)    dcgm-inject (reported) — real   any inject that leaves NVML up and calls it "off bus"
                        one is lab/physical
rank death              pod-kill / process-kill         NetworkChaos on eth0
IB / rail flap          ib-link-down (switch/port/      tc/netem on the NIC (chaosd-nic
                        ibportstate) — real link fault  or overlay) — RDMA bypasses it
CPU desync timeout      cpu-sleep (GPU stays healthy)   gpu reset / device inject
bad checkpoint          iochaos fault/mistake on ckpt   pod memory stress
wrong model served      artifact-swap (wrong digest)    kill the router
metro / inter-hall cut  circuit-disable (transport)     ib-link-down (wrong hall scope),
                                                        tc/netem (wrong path entirely)
OCS retune window       ocs-port-retune                 circuit-disable (a cut is not
                                                        a window)
hall power loss         feeder-open (window, operator)  pod-kill xN
coolant loop loss       cdu-stop (vs the ride-through   gpu-burn (heat is not cooling)
                        clock)
PFS stall               pfs-degrade (server-side)       iochaos (one pod), ib-link-down
                                                        (a PFS stall is not an ICI break)
```

A fourth rule arrived with v1.1: **the technique must live on the layer the
spec declares.** `ib-link-down` filed under `accelerator` fails even when no
fidelity keyword matches — a real injection under the wrong layer inherits that
layer's coverage claim without testing it.

Two more rules the validator carries, because they are safety, not fidelity:

- **Every experiment needs an owner, a duration cap, an abort condition, and an automated undo.** Missing any one of the four, it is a demo, and the validator refuses it. Unbounded GPU burn on a training fabric is not an experiment.
- **Physically-stressing techniques** (`chaosd-nic`, `gpu-burn`, `ib-link-down`, `ocs-port-retune`, `circuit-disable`, `feeder-open`, `cdu-stop`) must be flagged `agent_forbidden: true`. An autonomous planner may schedule a DCGM field injection; it must never fire host-NIC loss, a saturating burn, a breaker, a coolant valve, or a mirror moved under a live collective. The distinction is exactly the one the [governed-autonomy companion](../README.md#series) is built on: DCGM inject is software; host-NIC loss and GPU burn are not.

## Why encode it as a linter

A prose taxonomy gets nodded at and then violated under deadline. A machine rule fails the pull request. The 25 specs in [`catalog/experiments/`](../catalog/experiments/) all pass; the [validator tests](../catalog/test_validate.py) prove that hand-written wrong specs — an IB flap injected on the overlay *or via `tc`/`netem`*, an XID faked with a pod kill, a desync injected with a GPU reset — fail for the stated reason. That is the taxonomy doing work rather than decorating a slide.
