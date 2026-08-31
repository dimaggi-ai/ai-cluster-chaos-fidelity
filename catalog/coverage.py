#!/usr/bin/env python3
"""Coverage validation: the catalog vs a published failure taxonomy.

The question a buyer should ask of any chaos catalog: *does it rehearse
the failures that actually interrupt production training runs, at the
right layer?* This module answers it against the most-cited public
root-cause table for a frontier training run — Meta's 54-day Llama 3
405B snapshot (419 unexpected interruptions on 16,384 H100s) [17].

For every published failure class, the registry names either the catalog
experiments that rehearse it (and machine-checks that each one exists,
sits on the right layer, and passes the fidelity linter) or an honest
NOT-COVERED entry with the reason. Coverage claims that cannot be
machine-checked are marketing; uncovered classes that are not named are
worse.

Run: python3 coverage.py   (exit 1 if any mapping is broken)
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import yaml

try:
    from .validate import validate_spec
except ImportError:                      # run as a script from catalog/
    from validate import validate_spec

EXPERIMENTS = Path(__file__).parent / "experiments"

# Llama 3 405B pre-training, 54 days: 419 unexpected interruptions [17].
# The published table itemizes 18 rows; the seven largest are itemized
# HERE (the source's eleven further rows, each at or below 1.7%, total
# 42 interruptions and are held as one long-tail row below). Counts and
# shares are quoted verbatim and NOT arithmetically reconciled: the
# source's own top row is internally inconsistent (148/419 = 35.3%
# against its published 30.1% — the share column is what makes the
# paper's 58.7% GPU-issues total work), so the published shares sum to
# ~94.9%, not 100%. We quote the share column as printed and leave the
# ~5pp residual where the source left it, rather than absorbing it into
# the long tail.
TOTAL_INTERRUPTIONS = 419


@dataclasses.dataclass(frozen=True)
class FailureClass:
    name: str
    count: int                   # published count (long-tail row: sum of the
                                 # source's eleven small published rows)
    share: float                 # published share of interruptions [17]
    layer: str | None            # taxonomy layer the rehearsal must sit on
    covered_by: tuple[str, ...]  # catalog experiments; empty = NOT COVERED
    note: str


TAXONOMY = (
    FailureClass(
        "faulty-gpu", 148, 0.301, "accelerator",
        ("dcgm-ecc-dbe-inject.yaml", "dcgm-pcie-replay-inject.yaml"),
        "XID-class GPU faults, rehearsed by DCGM field injection at the "
        "detection->cordon loop — the reaction to the reported fault, not "
        "survival of the physical event (the distinction "
        "docs/fidelity-taxonomy.md enforces; a real XID 79 is explicitly "
        "out of what these specs certify).",
    ),
    FailureClass(
        "gpu-hbm3-memory", 72, 0.172, "accelerator",
        ("dcgm-ecc-dbe-inject.yaml",),
        "HBM3 failures surface as uncorrectable ECC; DCGM ECC-DBE "
        "injection rehearses the telemetry->cordon reaction to that "
        "report, not survival of a real HBM event.",
    ),
    FailureClass(
        "software-bug", 54, 0.129, "training-graph",
        ("rank-kill-training.yaml", "cpu-desync-sleep.yaml"),
        "Crashes and hangs land on the training graph as a dead rank or a "
        "desynchronized collective — the two dominant symptoms; bugs also "
        "surface as NaNs, corrupt checkpoints, and deadlocks, not "
        "rehearsed here.",
    ),
    FailureClass(
        "network-switch-cable", 35, 0.084, "fabric",
        ("ib-link-flap.yaml",),
        "Fabric faults need a real link event; overlay tc/netem does not "
        "reach the verbs path (the repo's core rule).",
    ),
    FailureClass(
        "unplanned-host-maintenance", 32, 0.076, None,
        (),
        "NOT COVERED yet: injectable in principle (Chaos Mesh and Litmus "
        "both ship node-drain/cordon experiments) and rehearsed "
        "operationally via cordon/drain game days; an orchestration-layer "
        "node-drain spec is a planned catalog addition.",
    ),
    FailureClass(
        "gpu-sram-memory", 19, 0.045, "accelerator",
        ("dcgm-ecc-dbe-inject.yaml",),
        "SRAM ECC surfaces through the same detection->cordon loop, but "
        "DCGM draws the field-family distinction: the rehearsal injects "
        "the device/frame-buffer (HBM-class) ECC field, while SRAM has "
        "its own L1/L2/register-file counters — the reaction path is "
        "shared, the injected field is not SRAM's.",
    ),
    FailureClass(
        "gpu-system-processor", 17, 0.041, None,
        (),
        "NOT COVERED: GSP hangs have no faithful software injection today; "
        "certifying them requires vendor tooling. Named, not papered over.",
    ),
    FailureClass(
        "published-long-tail", 42, 0.101, None,
        (),
        "The source's eleven remaining published rows, each at or below "
        "1.7%: NIC 7, NCCL watchdog timeouts 7 (arguably the desync class "
        "cpu-desync-sleep rehearses, but not counted as covered), SDC 6 "
        "(explicitly out-of-scope per [16]), GPU thermal 6, SSD 3, PSU 3, "
        "and five rows of 2. Count and share are sums of published rows, "
        "not derived from an assumed 100% total.",
    ),
)


def check() -> tuple[list[str], float, float, int]:
    """Return (integrity errors, covered share, accounted share,
    covered count). A class counts as covered only if every one of its
    mapped experiments exists, sits on the required layer, and passes
    the linter."""
    errors: list[str] = []
    covered = 0.0
    covered_count = 0
    accounted = 0.0
    for fc in TAXONOMY:
        accounted += fc.share
        if not fc.covered_by:
            continue
        row_errors: list[str] = []
        for exp in fc.covered_by:
            path = EXPERIMENTS / exp
            if not path.exists():
                row_errors.append(f"{fc.name}: mapped experiment {exp} missing")
                continue
            spec = yaml.safe_load(path.read_text())
            if fc.layer and spec.get("layer") != fc.layer:
                row_errors.append(
                    f"{fc.name}: {exp} sits on layer {spec.get('layer')!r}, "
                    f"taxonomy requires {fc.layer!r}"
                )
            lint = validate_spec(spec, exp)
            if lint:
                row_errors.append(f"{fc.name}: {exp} fails the linter: {lint}")
        if row_errors:
            errors.extend(row_errors)
        else:
            covered += fc.share
            covered_count += fc.count
    return errors, covered, accounted, covered_count


def main() -> int:
    errors, covered, accounted, covered_count = check()
    w = max(len(fc.name) for fc in TAXONOMY)
    print(f"{'failure class [17]':<{w}}  {'count':>5}  {'share':>6}  rehearsed by")
    for fc in TAXONOMY:
        cov = ", ".join(e.removesuffix(".yaml") for e in fc.covered_by) \
            or "NOT COVERED"
        print(f"{fc.name:<{w}}  {fc.count:>5}  {fc.share:>6.1%}  {cov}")
    print()
    print(f"covered by a passing, right-layer experiment: {covered:.1%} by "
          f"published share ({covered_count}/{TOTAL_INTERRUPTIONS} = "
          f"{covered_count / TOTAL_INTERRUPTIONS:.1%} by published count)")
    print(f"named but not covered (with reasons above):   "
          f"{accounted - covered:.1%} by published share")
    print(f"published shares sum to {accounted:.1%} — the source's own "
          f"~5pp residual is left unreconciled, as published")
    if errors:
        print("\nINTEGRITY FAILURES:")
        for e in errors:
            print(f"  - {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
