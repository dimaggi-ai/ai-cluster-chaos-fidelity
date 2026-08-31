"""Validator tests: the catalog passes; wrong specs fail for the right reason.
Run: python3 test_validate.py"""

from pathlib import Path

import yaml

try:
    from .validate import validate_spec
except ImportError:                      # run as a script from catalog/
    from validate import validate_spec

GOOD = {
    "name": "t",
    "layer": "inference-graph",
    "wanted_fault": "single serving replica death",
    "injection": {"tool": "Chaos Mesh", "technique": "pod-kill"},
    "blast_radius": "one replica, staging",
    "duration": "3m",
    "steady_state": ["TTFT p99 < 2s"],
    "abort": "TTFT p99 > 8s for 60s",
    "undo": "delete chaos resource",
    "owner": "serving-oncall",
    "evidence": "metrics.json",
    "hardware_required": True,
    "certifies": "restart path",
    "autonomy_class": "L2",
}


def _spec(**over):
    s = {**GOOD}
    s.update(over)
    return s


def test_catalog_all_valid():
    exps = sorted((Path(__file__).parent / "experiments").glob("*.yaml"))
    assert len(exps) >= 15, "catalog shrank unexpectedly"
    for p in exps:
        errs = validate_spec(yaml.safe_load(p.read_text()), p.name)
        assert not errs, (p.name, errs)


def test_good_spec_passes():
    assert validate_spec(_spec(), "t") == []


def test_demo_rule():
    """Missing owner/duration/abort/undo => 'it is a demo', refused."""
    for field in ("owner", "duration", "abort", "undo"):
        s = _spec()
        del s[field]
        errs = validate_spec(s, "t")
        assert errs and "demo" in errs[0], (field, errs)


def test_fidelity_rdma_via_overlay_rejected():
    """The repo's headline rule: overlay NetworkChaos is not an IB flap."""
    s = _spec(
        layer="fabric",
        wanted_fault="RDMA rail degradation (IB flap)",
        injection={"tool": "Chaos Mesh", "technique": "networkchaos-overlay"},
    )
    errs = validate_spec(s, "t")
    assert errs and "FIDELITY" in errs[0] and "overlay" in errs[0], errs


def test_fidelity_xid_via_podkill_rejected():
    s = _spec(
        layer="accelerator",
        wanted_fault="reported ECC DBE (XID 48)",
        injection={"tool": "Chaos Mesh", "technique": "pod-kill"},
    )
    errs = validate_spec(s, "t")
    assert errs and "FIDELITY" in errs[0], errs


def test_fidelity_fabric_via_process_kill_rejected():
    """An IB flap is only legitimately injected with a real link fault, never a
    process kill (regression guard for the fabric-rule allow-set)."""
    s = _spec(
        layer="fabric",
        wanted_fault="InfiniBand rail flap on the compute fabric",
        injection={"tool": "kubectl", "technique": "process-kill"},
    )
    errs = validate_spec(s, "t")
    assert errs and "FIDELITY" in errs[0], errs


def test_fidelity_ib_flap_via_tc_netem_rejected():
    """The standard's sharpest catch: tc/netem (chaosd-nic) does NOT reach the
    kernel-bypass RDMA path, so it cannot certify an IB flap."""
    for tech in ("chaosd-nic", "networkchaos-overlay"):
        s = _spec(
            layer="fabric",
            wanted_fault="RDMA rail loss / InfiniBand link flap",
            injection={"tool": "Chaosd", "technique": tech},
            agent_forbidden=True,
        )
        errs = validate_spec(s, "t")
        assert errs and "FIDELITY" in errs[0] and "bypass" in errs[0], (tech, errs)


def test_fidelity_ib_flap_via_link_down_passes():
    s = _spec(
        layer="fabric",
        wanted_fault="RDMA rail loss / InfiniBand link flap",
        injection={"tool": "switch CLI", "technique": "ib-link-down"},
        agent_forbidden=True,
    )
    assert validate_spec(s, "t") == []


def test_fidelity_rank_death_via_network_rejected():
    s = _spec(
        layer="training-graph",
        wanted_fault="rank death mid-collective",
        injection={"tool": "Chaos Mesh", "technique": "networkchaos-overlay"},
    )
    errs = validate_spec(s, "t")
    assert errs and "FIDELITY" in errs[0], errs


def test_fidelity_desync_requires_healthy_gpu():
    s = _spec(
        layer="training-graph",
        wanted_fault="CPU desync straggler",
        injection={"tool": "dcgmi", "technique": "dcgm-inject"},
    )
    errs = validate_spec(s, "t")
    assert errs and "healthy" in errs[0], errs


def test_physical_technique_requires_agent_forbidden():
    # use a non-fabric wanted_fault so only the agent_forbidden rule fires
    # (a fabric fault with chaosd-nic would also trip the fidelity rule)
    s = _spec(
        layer="overlay-network",
        wanted_fault="degraded IP bootstrap path",
        injection={"tool": "Chaosd", "technique": "chaosd-nic"},
    )
    errs = validate_spec(s, "t")
    assert errs and "agent_forbidden" in errs[0], errs
    s["agent_forbidden"] = True
    assert validate_spec(s, "t") == []


def test_duration_needs_numeric_cap():
    errs = validate_spec(_spec(duration="until it breaks"), "t")
    assert errs and "numeric cap" in errs[0], errs


def test_unknown_layer_and_technique():
    assert validate_spec(_spec(layer="vibes"), "t")
    assert validate_spec(
        _spec(injection={"tool": "x", "technique": "chaos-monkey"}), "t"
    )


if __name__ == "__main__":
    for fn in sorted(k for k in dir() if k.startswith("test_")):
        globals()[fn]()
        print(f"ok {fn}")
    print("all validator tests passed")
