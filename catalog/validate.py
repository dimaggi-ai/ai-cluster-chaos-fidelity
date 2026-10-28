#!/usr/bin/env python3
"""Validator for chaos experiment specs.

Three layers of checking:

1. COMPLETENESS — a spec without an owner, a duration cap, an abort
   condition, and an automated undo is not an experiment, it is a demo.
   The validator refuses it.

2. FIDELITY LINT — the repo's core taxonomy ("a pod kill is not an XID;
   overlay NetworkChaos is not an InfiniBand flap") encoded as machine
   rules: if the wanted fault and the injection technique live on
   different layers, the spec is rejected with the reason. Certifying the
   wrong technique is worse than certifying nothing.

3. LAYER MAP — the technique must live on the layer the spec declares.
   A real injection filed under the wrong layer inherits that layer's
   coverage claim without ever testing it.

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

# The fault model, read from the plant upward. A fault lives on exactly one of
# these; the whole standard is the claim that an injection on the wrong one
# certifies nothing.
LAYERS = (
    "facility",             # utility feed, coolant loop — the plant the hall sits on
    "accelerator",
    "fabric",               # the RDMA fabric INSIDE one hall
    "inter-hall-circuit",   # optical transport BETWEEN halls (OCS, metro, DWDM)
    "overlay-network",
    "shared-storage",       # the parallel filesystem every rank mounts at once
    "orchestration",
    "training-graph",
    "inference-graph",
    "data-artifacts",       # one job's own checkpoint files and model digests
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
    "ocs-port-retune",      # command the optical circuit switch to move a circuit:
                            # the fault is the reconfiguration WINDOW, not lost light
    "circuit-disable",      # take a whole inter-hall/metro circuit down at the
                            # transport layer (line-system or client port disable)
    "feeder-open",          # open a breaker on a hall's utility feed / PDU branch
    "cdu-stop",             # stop the coolant distribution unit or close the loop
    "pfs-degrade",          # degrade the SHARED parallel filesystem server-side
                            # (stop an OST/MDS, throttle metadata) — every rank at once
    "load-replay",          # GENERATE load: replay a synthetic or recorded request
                            # trace at a multiple of the baseline rate. It adds
                            # requests; it does not fault the ones in flight
)

# techniques that physically stress hardware or the fabric: an autonomous
# planner must never be allowed to fire these (repo rule)
PHYSICAL_TECHNIQUES = {
    "chaosd-nic", "gpu-burn", "ib-link-down",
    "ocs-port-retune",   # moves a mirror under a live collective; the companion
                         # governed-autonomy gate gives retune-live-collective no
                         # autonomy level at all, and this is the same act
    "circuit-disable", "feeder-open", "cdu-stop",
}

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
        r"(rdma|infiniband|\bib\b|rail|nvlink|fabric|leaf|spine|\btor\b)",
        {"ib-link-down"},
        "an RDMA/fabric fault must hit the collective's actual data path with a "
        "real link fault (ib-link-down: switch/port/ibportstate). tc/netem "
        "(chaosd-nic, networkchaos-overlay) runs on the kernel IP path, which "
        "RDMA bypasses — using it for an IB flap is the overlay-vs-fabric "
        "mistake one level deeper. A leaf/spine flap is this rule too: it is "
        "inside one hall, and it is not a metro cut",
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
    (
        r"(metro|long.?haul|dark.?fib|dwdm|fib(re|er).?cut|inter.?hall)",
        {"circuit-disable"},
        "a metro / inter-hall cut takes a whole transport circuit down and "
        "leaves the intra-hall fabric perfectly healthy. ib-link-down bounces "
        "one port inside one hall, and tc/netem runs on an IP path the "
        "collective does not use: neither reproduces the loss of the circuit "
        "BETWEEN halls, which is a different topology, a different recovery, "
        "and a different owner",
    ),
    (
        r"(\bocs\b|optical.?(circuit|switch)|circuit.?re.?tune|wavelength|lambda.?switch)",
        {"ocs-port-retune"},
        "an OCS retune is a reconfiguration WINDOW on a circuit that comes "
        "back by itself; the fault is the gap and the re-establishment. "
        "Disabling the circuit certifies a loss that never recovers on its "
        "own, and bouncing an IB port certifies neither — a retune and a cut "
        "are two experiments, not one",
    ),
    (
        r"(hall.?power|feeder|utility.?feed|breaker|\bpdu\b|power.?cut|brown.?out)",
        {"feeder-open"},
        "losing a hall's feed is a facility fault: it drops racks that are "
        "individually healthy, all at the same instant, and what it certifies "
        "is the restart ORDER on the way back. Killing pods one at a time "
        "reproduces neither the simultaneity nor the order",
    ),
    (
        r"(coolant|cold.?plate|\bcdu\b|loop.?loss|chilled.?water|\bcrac\b|liquid.?cool|pump)",
        {"cdu-stop"},
        "a coolant-loop loss is a facility fault with a clock on it: the hall "
        "has only its thermal buffer, and what the experiment certifies is "
        "whether load sheds inside the ride-through window. A device burn or "
        "a power cap changes the heat, not the cooling, and hands you no "
        "window to miss",
    ),
    (
        r"(\bpfs\b|lustre|gpfs|weka|parallel.?file|shared.?(file|mount|storage)|metadata.?storm|\bmds\b|\bost\b)",
        {"pfs-degrade"},
        "a parallel-filesystem stall is a SHARED-STORAGE fault: the collective "
        "is healthy and every rank blocks on the same mount at once. IOChaos "
        "faults one pod's volume, and an ib-link-down breaks the compute "
        "fabric — a PFS stall is not an ICI break, and certifying it as one "
        "retires the wrong risk in the wrong quarter",
    ),
    (
        r"(\b(load|traffic|requests?|qps|rps|demand)[\s_-]?(surge|spike|burst|flood)"
        r"|\b(surge|spike|burst|flood)s? (of|in) (load|traffic|requests|demand)\b"
        r"|flash.?crowd|thundering.?herd)",
        {"load-replay"},
        "a load surge is a DEMAND fault: every component stays healthy and "
        "more requests arrive than the system was sized for. What it certifies "
        "is admission, queueing and shedding under that demand. HTTPChaos "
        "delays or aborts the requests already there, and a pod kill removes "
        "capacity; both test degraded supply, not excess demand, so neither "
        "proves the shed path fires",
    ),
]

# --------------------------------------------------- technique -> layer map
# "Each maps to one layer or CI fails." The fidelity rules above check the
# wanted fault against the technique; this checks the technique against the
# declared layer, so a spec cannot file a real injection under the wrong layer
# and quietly inherit that layer's coverage claim.
#
# Written from first principles, then run against the catalog: no existing spec
# violated it. It is a guard against the next spec, not a bug it found.
# The physical techniques map one-to-one; the software ones do not, and saying
# so is more honest than pretending pod-kill belongs to a single layer.
TECHNIQUE_LAYERS = {
    "dcgm-inject": {"accelerator"},
    "gpu-burn": {"accelerator"},
    "ib-link-down": {"fabric"},
    "ocs-port-retune": {"inter-hall-circuit"},
    "circuit-disable": {"inter-hall-circuit"},
    "feeder-open": {"facility"},
    "cdu-stop": {"facility"},
    "pfs-degrade": {"shared-storage"},
    "networkchaos-overlay": {"overlay-network"},
    "chaosd-nic": {"overlay-network"},
    "dnschaos": {"overlay-network", "orchestration"},
    "httpchaos": {"overlay-network", "orchestration", "inference-graph",
                  "control-plane"},
    "iochaos": {"data-artifacts"},
    "artifact-swap": {"data-artifacts", "inference-graph"},
    "rag-degrade": {"data-artifacts", "inference-graph"},
    "pod-kill": {"orchestration", "training-graph", "inference-graph",
                 "control-plane"},
    "pod-failure": {"orchestration", "training-graph", "inference-graph",
                    "control-plane"},
    "process-kill": {"training-graph", "inference-graph", "control-plane"},
    "cpu-sleep": {"training-graph"},
    "stresschaos": {"orchestration", "training-graph", "inference-graph"},
    "timechaos": {"orchestration", "training-graph", "control-plane"},
    "agent-proposal": {"control-plane"},
    "load-replay": {"inference-graph", "control-plane"},
}

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

    # each fault maps to one layer: the technique must live on the declared one
    home = TECHNIQUE_LAYERS.get(technique)
    if home and spec["layer"] in LAYERS and spec["layer"] not in home:
        _err(errors, name,
             f"LAYER: technique '{technique}' does not live on layer "
             f"'{spec['layer']}' (it belongs on {sorted(home)}) — a fault maps "
             "to one layer, and filing it under another inherits that layer's "
             "coverage claim without testing it")

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
    if any(a in ("-h", "--help") for a in argv[1:]):
        print(
            "chaos-fidelity — validate AI-cluster chaos experiment specs against the "
            "fidelity standard.\n\n"
            "Usage:\n"
            "  chaos-fidelity [SPEC.yaml | DIR ...]\n\n"
            "With no arguments, validates the bundled reference catalog.\n"
            "Exit status: 0 if all specs are valid, 1 otherwise."
        )
        return 0
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
