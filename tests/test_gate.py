"""Tests for the deterministic verification gate. Every check is pinned in BOTH directions - the
wrong claim must FAIL and the right claim must PASS - so a gate that silently stopped checking would
be caught. Values here are invented; only the arithmetic is under test."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gate  # noqa: E402


def _fails(text, corpus="", check=None):
    ok, defects = gate.run_gate(text, corpus)
    f = [d for d in defects if d["severity"] == "FAIL"]
    if check:
        f = [d for d in f if d["check"] == check]
    return (not ok) and bool(f), f


# --- bit_delta: single-bit claim at the wrong bit (0x450000 ^ 0x440000 = 0x10000 = bit 16) --------
def test_bit_delta_wrong_bit_fails():
    bad = "REG_A stock 0x00440000. The 0x00450000 write differs from stock only at bit 19."
    failed, defects = _fails(bad, check="bit_delta")
    assert failed
    assert "bit 16" in defects[0]["detail"]   # it names the correct single-bit delta


def test_bit_delta_right_bit_passes():
    good = "REG_A stock 0x00440000. The 0x00450000 write differs from stock only at bit 16."
    ok, defects = gate.run_gate(good)
    assert ok
    assert not [d for d in defects if d["check"] == "bit_delta"]


def test_bit_delta_multibit_only_claim_fails():
    # 0x4C0008 ^ 0x440000 = 0x80008 = bits 3 and 19; "only at bit 19" is false
    bad = "REG_A stock 0x00440000. The 0x004C0008 write differs from stock only at bit 19."
    failed, defects = _fails(bad, check="bit_delta")
    assert failed
    assert "multiple bits" in defects[0]["detail"]


def test_bit_delta_ignores_unrelated_bit_mentions():
    ok, _ = gate.run_gate("FLAG_X is bit 0 of the control word.")
    assert ok


# --- fraction ------------------------------------------------------------------------------------
def test_fraction_wrong_ratio_fails():
    failed, _ = _fails("capacity drops 512 to 480 = 7/8 of the total", check="fraction")
    assert failed


def test_fraction_right_ratio_passes():
    ok, _ = gate.run_gate("capacity drops 512 to 480 = 15/16 of the total")
    assert ok


def test_fraction_bare_range_is_not_a_drop():
    # a hyphenated range is not an "A to B" transition and must not be read as one
    ok, _ = gate.run_gate("open ports 22-80, roughly 1/2 of them idle")
    assert ok


# --- grounding -----------------------------------------------------------------------------------
CORPUS = "0x00001000 CONFIG_WORD: stock `0x000000ff`, reset value of the config word.\n"


def test_grounding_contradicting_value_fails():
    failed, _ = _fails("The register 0x00001000 = 0xdeadbeef at boot.", CORPUS, check="grounding")
    assert failed


def test_grounding_correct_value_passes():
    ok, _ = gate.run_gate("The register 0x00001000 = 0x000000ff at boot.", CORPUS)
    assert ok


def test_grounding_inference_tag_exempt_from_warn():
    ok, defects = gate.run_gate("0x00009999 = 0x1234 is the likely slot [INFER, not in corpus].", CORPUS)
    assert ok
    assert not [d for d in defects if d["check"] == "grounding"]
