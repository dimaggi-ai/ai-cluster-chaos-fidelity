#!/usr/bin/env python3
"""Validator for chaos experiment specs.

Two layers of checking:

1. COMPLETENESS — a spec without an owner, a duration cap, an abort
   condition, and an automated undo is not an experiment, it is a demo.
   The validator refuses it.

2. FIDELITY LINT — the repo's core taxonomy ("a pod kill is not an XID;
   overlay NetworkChaos is not an InfiniBand flap") encoded as machine
   rules: if the wanted fault and the injection technique live on
   different layers, the spec is rejected with the reason. Certifying the
   wrong technique is worse than certifying nothing.

Usage:
    python3 validate.py                 # validate every spec in experiments/
    python3 validate.py path/to/spec.yaml
Exit code 0 = all valid; 1 = at least one invalid.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

LAYERS = (
    "accelerator",
    "fabric",
    "overlay-network",
    "orchestration",
    "training-graph",
    "inference-graph",
    "data-artifacts",
    "control-plane",
)

TECHNIQUES = (
    "dcgm-inject",          # DCGM field injection (software; no GPU harm)
    "pod-kill",             # Chaos Mesh PodChaos action: pod-kill
    "pod-failure",          # Chaos Mesh PodChaos action: pod-failure (pause)
    "process-kill",         # SIGKILL a rank/worker process
    "cpu-sleep",            # stall the CPU on one rank (desync, GPU healthy)
    "networkchaos-overlay", # Chaos Mesh NetworkChaos (tc/netem on pod veth)
    "chaosd-nic",           # Chaosd tc/netem on a host NIC — IP path ONLY.
                            # RDMA is kernel-bypass, so this does NOT reach the
                            # collective's verbs path (see fabric rule below).
    "ib-link-down",         # real RDMA-fabric link fault: switch port bounce,
                            # ibportstate disable, or vendor link-disable
    "iochaos",              # Chaos Mesh IOChaos on a volume path
    "stresschaos",          # Chaos Mesh StressChaos (container CPU/memory)
    "gpu-burn",             # device-level saturating load (lab only)
    "timechaos",
    "dnschaos",
    "httpchaos",
    "artifact-swap",        # mount a wrong/previous model or adapter digest
    "rag-degrade",          # empty or lagged retrieval index
    "agent-proposal",       # feed the control plane an illegal remediation
)

# techniques that physically stress hardware or the fabric: an autonomous
# planner must never be allowed to fire these (repo rule)
PHYSICAL_TECHNIQUES = {"chaosd-nic", "gpu-burn", "ib-link-down"}

REQUIRED_FIELDS = (
    "name", "layer", "wanted_fault", "injection", "blast_radius",
    "duration", "steady_state", "abort", "undo", "owner", "evidence",
    "hardware_required", "certifies",
)
REQUIRED_INJECTION_FIELDS = ("tool", "technique")

# The demo rule: these four make an experiment an experiment.
DEMO_FIELDS = ("owner", "duration", "abort", "undo")

# ------------------------------------------------------------- fidelity lint
# (wanted_fault regex, allowed techniques, message when violated)
FIDELITY_RULES = [
    (
        r"(rdma|infiniband|\bib\b|rail|nvlink|fabric)",
        {"ib-link-down"},
        "an RDMA/fabric fault must hit the collective's actual data path with a "
        "real link fault (ib-link-down: switch/port/ibportstate). tc/netem "
        "(chaosd-nic, networkchaos-overlay) runs on the kernel IP path, which "
        "RDMA bypasses — using it for an IB flap is the overlay-vs-fabric "
        "mistake one level deeper",
    ),
    (
        r"(ecc|xid|hbm|inforom|pcie|thermal|power.?cap)",
        {"dcgm-inject", "gpu-burn"},
        "accelerator health faults are injected at the DCGM/device layer, "
        "not via network or pod faults",
    ),
    (
        r"(rank.?death|rank.?kill|worker.?death)",
        {"pod-kill", "process-kill"},
        "rank death is a process/pod kill; a network fault is a different "
        "experiment",
    ),
    (
        r"(desync|cpu.?diverg|straggler)",
        {"cpu-sleep"},
        "CPU desync must leave the GPU healthy — sleep/stall the rank, "
        "do not reset or kill the device",
    ),
    (
        r"(checkpoint|ckpt|artifact|safetensors)",
        {"iochaos", "artifact-swap"},
        "checkpoint/artifact faults are file-IO or digest faults, not "
        "pod or memory stress",
    ),
    (
        r"(wrong.?(model|weights|adapter)|weight.?skew)",
        {"artifact-swap"},
        "serving the wrong model is a digest fault; killing the router "
        "tests something else",
    ),
]

VALID_AUTONOMY = ("L0", "L1", "L2", "L3")


def _err(errors: list, spec_name: str, msg: str) -> None:
    errors.append(f"  {spec_name}: {msg}")


def validate_spec(spec: dict, fname: str) -> list[str]:
    errors: list = []
    name = spec.get("name", fname)

    for f in REQUIRED_FIELDS:
        if f not in spec or spec[f] in (None, "", []):
            _err(errors, name, f"missing required field '{f}'"
                 + (" — without owner/duration/abort/undo it is a demo, "
                    "not an experiment" if f in DEMO_FIELDS else ""))
    if errors:
        return errors

    if spec["layer"] not in LAYERS:
        _err(errors, name, f"unknown layer '{spec['layer']}' (choose from {LAYERS})")

    inj = spec["injection"]
    for f in REQUIRED_INJECTION_FIELDS:
        if f not in inj or not inj[f]:
            _err(errors, name, f"injection is missing '{f}'")
    technique = inj.get("technique", "")
    if technique and technique not in TECHNIQUES:
        _err(errors, name, f"unknown technique '{technique}' (choose from {TECHNIQUES})")

    if not isinstance(spec["steady_state"], list) or not spec["steady_state"]:
        _err(errors, name, "steady_state must be a non-empty list of contracts")

    if not isinstance(spec["hardware_required"], bool):
        _err(errors, name, "hardware_required must be true or false")

    if not re.search(r"\d", str(spec["duration"])):
        _err(errors, name, "duration must carry a numeric cap (e.g. '3m')")

    ac = spec.get("autonomy_class")
    if ac is not None and ac not in VALID_AUTONOMY:
        _err(errors, name, f"autonomy_class must be one of {VALID_AUTONOMY}")

    # fidelity lint
    wanted = str(spec["wanted_fault"]).lower()
    for pattern, allowed, msg in FIDELITY_RULES:
        if re.search(pattern, wanted) and technique not in allowed:
            _err(errors, name,
                 f"FIDELITY: wanted_fault '{spec['wanted_fault']}' with "
                 f"technique '{technique}' — {msg}")

    # physical techniques must be explicitly barred from autonomous planners
    if technique in PHYSICAL_TECHNIQUES and spec.get("agent_forbidden") is not True:
        _err(errors, name,
             f"technique '{technique}' physically stresses hardware or the "
             "host network: set 'agent_forbidden: true' — an autonomous "
             "planner must never fire this class")

    return errors


def _collect(args: list[str]) -> list[Path]:
    """Expand file/dir arguments to a flat list of YAML spec paths. With no
    args, fall back to the bundled reference catalog (so the installed CLI
    validates something useful out of the box)."""
    here = Path(__file__).resolve().parent
    raw = [Path(a) for a in args] if args else [here / "experiments"]
    out: list[Path] = []
    for p in raw:
        if p.is_dir():
            out.extend(sorted(p.glob("*.yaml")))
        else:
            out.append(p)
    return out


def main(argv: list[str]) -> int:
    paths = _collect(argv[1:])
    if not paths:
        print("no specs found (pass a .yaml file or a directory of specs)")
        return 1
    all_errors: list[str] = []
    for p in paths:
        try:
            with p.open() as f:
                spec = yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as e:
            print(f"FAIL {p.name}")
            all_errors.append(f"  {p.name}: could not read/parse ({e})")
            continue
        errs = validate_spec(spec, p.name)
        marker = "FAIL" if errs else "ok  "
        print(f"{marker} {p.name}")
        all_errors.extend(errs)
    if all_errors:
        print(f"\n{len(all_errors)} problem(s):")
        print("\n".join(all_errors))
        return 1
    print(f"\nall {len(paths)} specs valid")
    return 0


def cli() -> None:
    """Console entry point (`chaos-fidelity [spec.yaml | dir ...]`)."""
    raise SystemExit(main(sys.argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
