# Injection methods: a per-technique runbook

Reference numbers point to [REFERENCES.md](../REFERENCES.md). This is the operational companion to the [fidelity taxonomy](fidelity-taxonomy.md): the taxonomy says *which* technique is legitimate for a given fault (and the [validator](../catalog/validate.py) enforces it); this runbook says *how* to run each technique, what it certifies, and what it does not. Every technique here is the `injection.technique` of one or more specs in [`catalog/experiments/`](../catalog/experiments/).

Ground rules for all of it: one GPU or one rank first; a duration cap; an abort on temperature or a stuck link; an automated undo; a metrics file as ground truth. DCGM injection is software. GPU burn and host-NIC loss are not — keep them out of any autonomous planner's reach.

## 1. DCGM field injection (accelerator health path)

The right first GPU technique, and it does **not** stress the card: it writes a value into a DCGM field so health watches and policies fire exactly as they would on a real ECC or PCIe event [6]. NVSentinel-class controllers are built to consume this signal, and NVIDIA ships a no-GPU demo that drives an InfoROM-corruption injection through a fake DCGM to verify the cordon loop end to end [8].

```bash
# 1. host engine running
# 2. watch policies
dcgmi policy --set 0,0 -e      # ECC monitoring
dcgmi policy --reg

# 3. inject (NVIDIA's documented examples [6])
dcgmi test --inject --gpuid 0 -f 319 -v 4      # DCGM_FI_DEV_ECC_DBE_VOL_DEV
dcgmi test --inject --gpuid 0 -f 202 -v 99999  # DCGM_FI_DEV_PCIE_REPLAY_TOTAL
```

Field 319 is the *device-memory* volatile double-bit ECC counter (`DCGM_FI_DEV_ECC_DBE_VOL_DEV`); the *total* counter is field 311 (`DCGM_FI_DEV_ECC_DBE_VOL_TOTAL`) [6]. Field IDs drift across DCGM versions — look them up in `dcgm_fields.h` on the box under test, not in this document.

**Certifies:** the detector → cordon/drain loop on a *reported* hardware fault.
**Does not certify:** survival of a real uncorrectable HBM event, a real XID 79 (device gone from PCIe), or a stuck CUDA context. After injection, health should trip while the job keeps running until the controller acts — that gap *is* the experiment. Specs: [`dcgm-ecc-dbe-inject`](../catalog/experiments/dcgm-ecc-dbe-inject.yaml), [`dcgm-pcie-replay-inject`](../catalog/experiments/dcgm-pcie-replay-inject.yaml), [`inforom-corrupt-inject`](../catalog/experiments/inforom-corrupt-inject.yaml).

Map reported XIDs to expected controller actions, or you will quarantine application bugs (official meanings from NVIDIA's XID documentation [5]):

| XID | Official meaning [5] | Chaos-worthy heal |
|---|---|---|
| 48 | Double Bit ECC Error (uncorrectable) | drain, reset GPU, no new gang admission |
| 63 / 64 | GPU memory remapping event / failure | reset window vs RMA path |
| 79 | GPU has fallen off the bus | drain node; a software reset is often impossible |
| 94 / 95 | contained / uncontained memory error | restart app (94) vs reset GPU before restart (95) |
| 13 / 31 | Graphics Engine Exception / GPU memory page fault | usually application-level; must **not** auto-RMA |

## 2. Kill a rank or a replica (workload graph)

The cheapest high-fidelity training/inference fault. `kill -9` the rank process, or `kubectl delete pod --force --grace-period=0`, or Chaos Mesh `PodChaos` with `action: pod-kill` [1]. Note that `pod-failure` (pause; duration-based; the replica stays in the Service endpoints) is a *different experiment* from `pod-kill` (one-shot restart) [1] — use failure when you want a frozen-but-present replica.

**The two clocks.** Watch the NCCL watchdog and the InfiniBand transport timeout separately. PyTorch's watchdog defaults to **600 s** for NCCL (30 min for other backends) [10]; the IB transport gives up in ~30 s (`NCCL_IB_TIMEOUT` default 20, ×7 retries [11]). If you tune only the watchdog, you tuned the wrong clock — and the companion reliability-economics study prices that gap in GPU-hours.

**Desync without touching the GPU.** Meta's fleet data says most NCCL watchdog timeouts are CPU-side desync, not a slow collective [12]. Inject the desync class by stalling one rank's CPU (`SIGSTOP`, or a sleep before the next collective — a dataloader stall or checkpoint fsync). A healer that cordons on every timeout will quarantine healthy hardware; the [`cpu-desync-sleep`](../catalog/experiments/cpu-desync-sleep.yaml) spec certifies the classifier that tells the two apart. **Certifies:** checkpoint/restart policy and the desync classifier. Specs: [`rank-kill-training`](../catalog/experiments/rank-kill-training.yaml), [`vllm-replica-kill`](../catalog/experiments/vllm-replica-kill.yaml), [`decode-replica-kill`](../catalog/experiments/decode-replica-kill.yaml).

## 3. Overlay network chaos (and its lie)

Chaos Mesh `NetworkChaos` applies tc/netem in the pod's network namespace [1]: `delay`, `loss`, `duplicate`, `corrupt`, `partition`, `bandwidth`, `reorder`, `rate`. It is the right tool for an HTTP router, an object-store client, or etcd — and the **wrong** tool for a collective. Partitioning two training workers on the overlay tests bootstrap and socket setup (`NCCL_SOCKET_IFNAME`), not AllReduce on the RDMA NIC. Label the experiment honestly: it certifies the *client path*, nothing about RDMA or the store itself. Spec: [`object-store-overlay-delay`](../catalog/experiments/object-store-overlay-delay.yaml).

## 4. Fabric chaos (the layer people skip — and get wrong twice)

To fake an IB flap you must fault the path the collective actually uses. The tempting shortcut — Chaosd `tc`/`netem` on the RDMA NIC — **does not work**, and this is the standard's sharpest catch: RDMA is kernel-bypass, so `tc`/`netem` (which lives in the kernel network stack) never touches the verbs path an AllReduce runs on. It injects an *IP-path* fault; the collective sails through it. Using it for an "IB flap" is the overlay-vs-fabric mistake one level deeper, and the [validator](../catalog/validate.py) rejects it.

The honest fabric fault is a **real link fault**: a switch port bounce, `ibportstate` disable, or a re-enable-able vendor link-disable, toward one HCA/port for 10–30 s, one node, abort if the link stays down. That needs hardware and switch access, so it stays **L0/L1** and `agent_forbidden`. **Certifies:** fail-closed collective behavior and rail-vs-GPU classification. Spec: [`ib-link-flap`](../catalog/experiments/ib-link-flap.yaml). If what you actually want is to test the *bootstrap/rendezvous* IP path, that is a legitimate — and different — experiment: [`ip-bootstrap-delay`](../catalog/experiments/ip-bootstrap-delay.yaml) uses Chaosd `tc`/`netem` and says so.

## 5. Checkpoint and artifact I/O

`IOChaos` hooks filesystem calls on a volume [1]: `action: fault` with `errno: 5` (EIO) for save/load errors; `action: latency` for slow `async_save` (and watchdog risk if the CPU stalls on fsync); `action: mistake` for bit flips — an *integrity*, not availability, fault. Point it at the checkpoint mount (`volumePath`), not the whole node. The contract is that a corrupt image is rejected by hash and the job resumes from the last good digest, with no "success" log for a partial write. Specs: [`ckpt-eio`](../catalog/experiments/ckpt-eio.yaml), [`ckpt-bitflip`](../catalog/experiments/ckpt-bitflip.yaml).

## 6. Stress and burn (capacity and isolation)

`StressChaos` pressures the *container* (CPU/memory) [1] — right for noisy-neighbor and tenancy isolation, wrong for HBM. For the device, `gpu-burn` runs a saturating GEMM [9] in a **labeled lab job**, time-boxed, thermal abort armed — you are testing neighbor isolation, MIG, PCIe contention, and throttle attribution, not damaging the card. DCGM's own `dcgmi diag -r pulse_test` is a level-4 commissioning test [7]; do not fire it as random chaos on a live gang. Both `gpu-burn` and Chaosd NIC faults are `agent_forbidden`. Specs: [`noisy-neighbor-stress`](../catalog/experiments/noisy-neighbor-stress.yaml), [`gpu-burn-isolation`](../catalog/experiments/gpu-burn-isolation.yaml).

## 7. Edges: time, DNS, HTTP

`TimeChaos` (clock skew — breaks leases, JWTs, some checkpoint naming; lab only), `DNSChaos` (miss the artifact registry or tokenizer service), `HTTPChaos` (5xx or delay on the router / OpenAI-compatible gateway) [1]. These certify the serving edge and control plane, not NCCL.

## 8. Quality injection (the fault latency SLOs miss)

No CRD required. Mount a previous adapter digest on one replica (the inverse of `pin_artifact`); point RAG at an empty or lagged index; drop tool responses for an agent worker. Watch the frozen eval slice, not QPS — a 200 OK on the wrong weights is exactly the fault an availability SLO cannot see. Specs: [`wrong-adapter-mount`](../catalog/experiments/wrong-adapter-mount.yaml), [`rag-index-lag`](../catalog/experiments/rag-index-lag.yaml).

## 9. Control-plane injection (the healer becomes the fault)

Delete the healer pod mid-drain; block the referee RPC; have the planner emit a non-allow-listed tool; race two agents with a simultaneous DCGM fault and overlay delay. The contract: fail closed, one drain, undo runs, an audit line exists. This is where the [governed-autonomy sequel](../README.md#series) meets chaos — the referee that denies an illegal remediation is a Tool Guard-class policy firewall, and it is tested here, not assumed. Specs: [`healer-illegal-proposal`](../catalog/experiments/healer-illegal-proposal.yaml), [`planner-kill-mid-heal`](../catalog/experiments/planner-kill-mid-heal.yaml).

## Fidelity and safety, in one table

| Wanted fault | Acceptable injection | Wrong injection |
|---|---|---|
| Uncorrectable ECC reported | `dcgm-inject` ECC field | overlay packet loss |
| GPU off bus (XID 79) | lab/physical only | field inject that leaves NVML up |
| Rank death | `pod-kill` / SIGKILL one worker | `NetworkChaos` on eth0 |
| IB flap | Chaosd on the RDMA `device` | `NetworkChaos` delay 200 ms |
| CPU desync timeout | `cpu-sleep` on one rank | GPU reset |
| Bad checkpoint | `IOChaos` fault/mistake on ckpt path | pod memory stress |
| Wrong model | mount the wrong digest | kill the router |

The [validator](../catalog/validate.py) rejects every entry in the right-hand column with its reason — which is why the taxonomy survives a deadline instead of being quietly ignored under one.
