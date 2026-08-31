"""Coverage-registry tests. Run: python3 test_coverage.py"""

try:
    from .coverage import TAXONOMY, TOTAL_INTERRUPTIONS, check
except ImportError:                      # run as a script from catalog/
    from coverage import TAXONOMY, TOTAL_INTERRUPTIONS, check


def test_mapping_integrity():
    errors, _, _, _ = check()
    assert not errors, errors


def test_majority_of_interruptions_rehearsed():
    _, covered, _, covered_count = check()
    assert covered >= 0.70, covered
    assert covered_count / TOTAL_INTERRUPTIONS >= 0.75, covered_count


def test_uncovered_classes_are_named_with_reasons():
    uncovered = {fc.name: fc for fc in TAXONOMY if not fc.covered_by}
    assert set(uncovered) == {
        "unplanned-host-maintenance",
        "gpu-system-processor",
        "published-long-tail",
    }, set(uncovered)
    for fc in uncovered.values():
        assert len(fc.note) > 40, f"{fc.name}: reason too thin"
    assert "NOT COVERED" in uncovered["unplanned-host-maintenance"].note
    assert "NOT COVERED" in uncovered["gpu-system-processor"].note


def test_published_numbers_quoted_not_reconciled():
    # the seven largest rows of the published 18-row table, itemized here
    itemized = [fc for fc in TAXONOMY if fc.name != "published-long-tail"]
    assert len(itemized) == 7
    assert abs(sum(fc.share for fc in itemized) - 0.848) < 0.005
    # counts sum to the published 419 exactly (148+72+54+35+32+19+17+42)
    assert sum(fc.count for fc in TAXONOMY) == TOTAL_INTERRUPTIONS
    # published shares do NOT sum to 1.0 — the source's top row is
    # internally inconsistent (148/419 = 35.3% vs its printed 30.1%),
    # and we refuse to absorb that residual anywhere
    assert abs(sum(fc.share for fc in TAXONOMY) - 0.949) < 0.005


if __name__ == "__main__":
    for fn in sorted(k for k in dir() if k.startswith("test_")):
        globals()[fn]()
        print(f"ok {fn}")
    print("all coverage tests passed")
