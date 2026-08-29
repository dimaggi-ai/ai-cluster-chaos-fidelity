# Chaos Fidelity for AI Clusters: a machine-checkable standard for which fault injection tests which layer

**Most chaos experiments on AI clusters test the wrong thing, and nobody notices.** A pod kill is not an XID. Overlay `NetworkChaos` is not an InfiniBand flap — and neither is `tc`/`netem` on the RDMA NIC, because RDMA bypasses the kernel the way `tc` works on. A DCGM field injection tests your telemetry pipeline, not your GPU. Get the layer wrong and you certify a recovery behavior that will not fire on the real fault — you retire the risk on paper. **Certifying the wrong injection is worse than certifying nothing.**

This repository is a **standard** that makes fidelity mechanical: an eight-layer fault model, a spec schema for a chaos experiment, a **linter that fails a wrong-layer experiment in CI with the reason**, and a vetted catalog of 20 reference experiments that all pass. It is deliberately narrow, literature-independent, and adoptable — bring your own experiments, run them through the linter, and know that what you certified is what you think you certified.

*Part of a series on turning GPU capital into usable compute (see [Series](#series)). Its companion, the reliability-economics study, prices what these experiments certify in GPU-hours. All factual claims trace to [REFERENCES.md](REFERENCES.md).*

---

## The problem, in one table

An AI cluster fails on eight layers that each emit a different signal and heal a different way. An injection is only as good as the layer it actually hits:

| Wanted fault | The one honest injection | What people reach for instead (the linter rejects it) |
|---|---|---|
| Uncorrectable ECC / XID | DCGM field injection [6] — certifies the *telemetry → cordon* loop, **not** device physics | overlay packet loss, pod-kill |
| Rank death | `pod-kill` / SIGKILL a worker [1] | `NetworkChaos` on eth0 |
| **InfiniBand / rail flap** | **a real link fault** — switch port bounce, `ibportstate`, vendor link-disable | `tc`/`netem` on the NIC (RDMA is kernel-bypass — it never reaches the collective) |
| CPU desync timeout | stall one rank's CPU, GPU healthy [12] | GPU reset / device inject |
| Bad checkpoint | `IOChaos` fault/mistake on the ckpt path [1] | pod memory stress |
| Wrong model served | mount the wrong digest | kill the router |

The full reasoning — including why `tc` on an RDMA NIC is the overlay-vs-fabric mistake one level deeper, and why DCGM injection certifies the telemetry plane rather than the silicon — is in [docs/fidelity-taxonomy.md](docs/fidelity-taxonomy.md). The operational how-to (the actual `dcgmi` commands, the XID→heal map, the Chaos Mesh forms) is the runbook in [docs/injection-methods.md](docs/injection-methods.md).

**The two clocks** are why the fabric experiment matters: when a rail drops, the InfiniBand transport gives up in ~30 s (`NCCL_IB_TIMEOUT`, default 20, ×7 retries [11]) while the PyTorch NCCL watchdog default is **600 s** [10]. Tune only the watchdog and you tuned the wrong timer.

## The standard

Three parts, all in this repo:

1. **A spec schema** — the contract for a chaos experiment ([SPEC.md](SPEC.md)): its layer, the wanted fault, the injection technique, blast radius, duration cap, abort condition, automated undo, steady-state contract, what it certifies, and (for the governed-autonomy sequel) how far it may be auto-promoted.
2. **A linter** ([`catalog/validate.py`](catalog/validate.py)) that enforces two things and rejects anything that fails:
   - **Fidelity** — the taxonomy above as machine rules. An IB flap injected on the overlay *or via `tc`/`netem`*, an XID faked with a pod kill, a desync injected with a GPU reset: **each fails validation with the reason.**
   - **Completeness + safety** — every spec needs an owner, a duration cap, an abort condition, and an automated undo (missing any one, it is a demo, not an experiment), and physically-stressing techniques (`ib-link-down`, `gpu-burn`, `chaosd-nic`) must be flagged `agent_forbidden` so no autonomous planner can fire them.
3. **A reference catalog** — 20 experiment specs across all eight layers ([`catalog/`](catalog/)) that all pass, plus the taxonomy and runbook docs. Copy from it, or lint your own against the same rules.

```
pip install pyyaml
make test                       # lint the catalog + prove wrong specs are rejected
python3 catalog/validate.py your-experiment.yaml   # lint one of your own
```

The linter is the point: a prose taxonomy gets nodded at and violated under deadline; a machine rule fails the pull request. The [validator tests](catalog/test_validate.py) prove that hand-written wrong specs — including the subtle `tc`-on-RDMA case — fail for the stated reason.

The standard is tool-agnostic on purpose: the specs name a *technique*, not a vendor. Chaos Mesh [1] supplies the Kubernetes-layer forms here, but the same experiments run under Litmus [2] or a cloud fault-injection service [4] when the cluster is managed; ChaosEater [3] is the current precedent for generating specs like these with an LLM — exactly the loop the governed-autonomy sequel governs.

## Why a standard, not a checklist

A checklist is advice; a standard is enforcement with an ecosystem. This repo is versioned ([SPEC.md](SPEC.md) carries the version), the schema is stable, and the catalog is meant to grow by contribution — see [CONTRIBUTING.md](CONTRIBUTING.md). The honest scope is stated up front, including the failure classes the catalog does **not** yet cover (SDC, correlated power/cooling, stragglers, checkpoint-store outages) in [catalog/README.md](catalog/README.md#coverage--and-what-is-deliberately-out-of-scope) — listing them is the alternative to a coverage matrix that looks complete because it is small.

## Reproduce

```
pip install pyyaml
make test                    # validator (20 specs valid) + rejection tests
python3 catalog/validate.py  # lint the whole catalog
python3 catalog/test_validate.py
```

Python 3.11+, `pyyaml`. The checks run on every push via [CI](.github/workflows/ci.yml) — a machine-checkable standard must machine-check itself.

## Series — turning GPU capital into usable compute

- **GPU Cluster Networking** — how interconnect bandwidth caps AI training throughput at scale ([network-vs-more-gpus](https://github.com/dimaggi-ai/network-vs-more-gpus))
- **GPU Cluster Scheduling** — reclaiming stranded capacity across Slurm, Kubernetes, and multi-cloud ([scheduler-vs-more-gpus](https://github.com/dimaggi-ai/scheduler-vs-more-gpus))
- **Chaos Fidelity for AI Clusters** (this work) — the standard for testing recovery so a certified experiment certifies what it claims
- **Reliability Economics** (companion, in progress) — prices what these experiments certify: MTTF/MTTR/ETTR and which recovery policy wins in which failure regime
- **Governed Autonomy for GPU Clusters and Networks** (next) — the controller these experiments certify, from human intent to nanosecond in-ASIC reflexes

---

*Margaret (Maggie) Nanyonga — Founder & Principal Architect, [DIMAGGI AI](https://dimaggi.ai). Governed AI infrastructure: the control, reliability, and audit layer for autonomous systems operating production networks and compute.*
