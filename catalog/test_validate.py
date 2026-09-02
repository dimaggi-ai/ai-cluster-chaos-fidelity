"""Validator tests: the catalog passes; wrong specs fail for the right reason.
Run: python3 test_validate.py"""

from pathlib import Path

import yaml

try:
    from .validate import LAYERS, TECHNIQUE_LAYERS, TECHNIQUES, validate_spec
except ImportError:                      # run as a script from catalog/
    from validate import LAYERS, TECHNIQUE_LAYERS, TECHNIQUES, validate_spec

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
    assert len(exps) >= 25, "catalog shrank unexpectedly"
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


# ------------------------------------------------------- scale-across layers
# metro cut != leaf flap != OCS retune != PFS stall != cold-plate trip. Each of
# these used to be describable with any technique at all, because no rule
# covered them; the point of the five below is that the wrong one now fails.


def test_metro_cut_via_ib_link_down_rejected():
    """A cut between halls is not a port bounce inside one."""
    s = _spec(
        layer="inter-hall-circuit",
        wanted_fault="metro fibre cut between halls",
        injection={"tool": "switch CLI", "technique": "ib-link-down"},
        agent_forbidden=True,
    )
    errs = validate_spec(s, "t")
    assert errs and "FIDELITY" in errs[0] and "BETWEEN halls" in errs[0], errs


def test_ocs_retune_via_circuit_disable_rejected():
    """A retune comes back by itself; a disabled circuit does not. Two faults."""
    s = _spec(
        layer="inter-hall-circuit",
        wanted_fault="OCS circuit retune under a live collective",
        injection={"tool": "OCS API", "technique": "circuit-disable"},
        agent_forbidden=True,
    )
    errs = validate_spec(s, "t")
    assert errs and "FIDELITY" in errs[0] and "window" in errs[0].lower(), errs


def test_pfs_stall_via_ib_link_down_rejected():
    """The rule this catalogue was extended for: a PFS stall is not an ICI break."""
    s = _spec(
        layer="shared-storage",
        wanted_fault="shared parallel filesystem metadata stall (Lustre MDS)",
        injection={"tool": "switch CLI", "technique": "ib-link-down"},
        agent_forbidden=True,
    )
    errs = validate_spec(s, "t")
    assert errs and "FIDELITY" in errs[0] and "not an ICI break" in errs[0], errs


def test_pfs_stall_via_iochaos_rejected():
    """One pod's volume is not the mount every rank blocks on at once."""
    s = _spec(
        layer="shared-storage",
        wanted_fault="Lustre metadata stall across every rank",
        injection={"tool": "Chaos Mesh", "technique": "iochaos"},
    )
    errs = validate_spec(s, "t")
    assert errs and "FIDELITY" in errs[0], errs


def test_hall_power_loss_via_pod_kill_rejected():
    """Killing pods one at a time reproduces neither simultaneity nor order."""
    s = _spec(
        layer="facility",
        wanted_fault="utility feed lost to one hall (breaker open on the feeder)",
        injection={"tool": "kubectl", "technique": "pod-kill"},
    )
    errs = validate_spec(s, "t")
    assert errs and "FIDELITY" in errs[0] and "simultaneity" in errs[0], errs


def test_coolant_loss_via_gpu_burn_rejected():
    """A burn changes the heat, not the cooling: no ride-through window to miss."""
    s = _spec(
        layer="facility",
        wanted_fault="coolant loop loss - the CDU stops under load",
        injection={"tool": "gpu_burn", "technique": "gpu-burn"},
        agent_forbidden=True,
    )
    errs = validate_spec(s, "t")
    assert errs and "FIDELITY" in errs[0] and "ride-through" in errs[0], errs


def test_a_fault_that_reads_as_two_layers_cannot_be_certified():
    """'Each maps to one layer or CI fails' has a sharp edge: a wanted_fault
    worded as two layers ('inter-hall fabric cut') is refused whichever
    technique you pick, because both rules fire. The fix is to say which fault
    you mean, which is the whole standard in one sentence."""
    for tech in ("ib-link-down", "circuit-disable"):
        s = _spec(
            layer="inter-hall-circuit",
            wanted_fault="inter-hall fabric cut",
            injection={"tool": "x", "technique": tech},
            agent_forbidden=True,
        )
        errs = validate_spec(s, "t")
        assert errs and any("FIDELITY" in e for e in errs), (tech, errs)


def test_technique_must_live_on_its_declared_layer():
    """A real injection filed under the wrong layer would inherit that layer's
    coverage claim without testing it."""
    s = _spec(
        layer="accelerator",
        wanted_fault="a link event we would rather not describe",
        injection={"tool": "switch CLI", "technique": "ib-link-down"},
        agent_forbidden=True,
    )
    errs = validate_spec(s, "t")
    assert errs and "LAYER" in errs[0] and "fabric" in errs[0], errs
    assert validate_spec(_spec(**{**s, "layer": "fabric"}), "t") == []


def test_every_technique_has_a_home_layer():
    """A technique added to TECHNIQUES but not to TECHNIQUE_LAYERS would slip
    past the layer check silently."""
    assert set(TECHNIQUE_LAYERS) == set(TECHNIQUES)
    for tech, homes in TECHNIQUE_LAYERS.items():
        assert homes, tech
        assert homes <= set(LAYERS), (tech, homes - set(LAYERS))


def test_new_physical_techniques_require_agent_forbidden():
    """Opening a breaker, stopping a coolant loop, cutting a circuit and moving
    a mirror under a live collective are all things a planner must not do."""
    cases = [
        ("facility", "utility feed lost to one hall", "feeder-open"),
        ("facility", "coolant loop loss under load", "cdu-stop"),
        ("inter-hall-circuit", "metro fibre cut between halls", "circuit-disable"),
        ("inter-hall-circuit", "OCS circuit retune mid-job", "ocs-port-retune"),
    ]
    for layer, fault, tech in cases:
        s = _spec(layer=layer, wanted_fault=fault,
                  injection={"tool": "x", "technique": tech})
        errs = validate_spec(s, "t")
        assert errs and "agent_forbidden" in errs[0], (tech, errs)
        s["agent_forbidden"] = True
        assert validate_spec(s, "t") == [], (tech, validate_spec(s, "t"))


def test_leaf_flap_is_not_a_metro_cut():
    """The fabric rule was widened to cover leaf/spine wording, so an in-hall
    switch flap can no longer be certified with a transport-layer injection."""
    s = _spec(
        layer="inter-hall-circuit",
        wanted_fault="leaf switch flap in one hall",
        injection={"tool": "line-system NMS", "technique": "circuit-disable"},
        agent_forbidden=True,
    )
    errs = validate_spec(s, "t")
    assert errs and "FIDELITY" in errs[0], errs


if __name__ == "__main__":
    for fn in sorted(k for k in dir() if k.startswith("test_")):
        globals()[fn]()
        print(f"ok {fn}")
    print("all validator tests passed")
