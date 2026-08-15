#!/usr/bin/env python3
"""Deterministic verification gate for grounded agent output.

Before a bridge agent promotes a claim to a channel, the *checkable* parts of it are verified in
Python - numeric/arithmetic consistency and the grounding of cited values against the source corpus.
A confidently worded wrong number therefore cannot pass, no matter how fluent the prose around it.

Why deterministic, and not just a second model grading the first: two language models share blind
spots and both fumble arithmetic. A model can assert "X differs from Y only at bit 19" when
X ^ Y = 0x10000 = bit 16, and a reviewing model may wave it through. Arithmetic is not a matter of
opinion, so it is checked with arithmetic. This is the deterministic HALF of a two-stage gate; an
LLM adversarial judge (see harness_agent.py) handles only what determinism cannot - soundness of
reasoning, relevance, novelty.

Checks (each returns a list of defect dicts; severity FAIL blocks, WARN informs):
  1. bit_delta   - "A differs from B only at bit N" -> N must equal the bit index of A ^ B. FAIL.
  2. fraction    - "A to B = p/q" -> B/A or (A-B)/A must equal p/q. FAIL.
  3. grounding   - "<id> ... stock <value>" -> the corpus must carry that id, and a contradicting
                   value on the corpus's own line for that id is a FAIL; an absent one is a WARN.
                   A line the author tagged [INFER]/[gap]/[UNRESOLVED] is exempt from the WARN.

run_gate(text, corpus) -> (passed, defects). passed is True iff no FAIL is present.
"""
import re

HEX = re.compile(r'0x[0-9a-fA-F]{2,}')
INFER_TAG = re.compile(r'\[(INFER|gap|UNRESOLVED)', re.I)
_DELTA_WORDS = re.compile(r'\bdiffers?\b|\bdelta\b|only at|\bxor\b|differ from', re.I)


def _single_bit_index(x):
    """Bit index if x has exactly one bit set, else None."""
    return x.bit_length() - 1 if x and (x & (x - 1)) == 0 else None


def _set_bits(x):
    return [k for k in range(x.bit_length()) if (x >> k) & 1]


def check_bit_deltas(text):
    """Flag 'differs only at bit N' claims that the values do not bear out. Two failure modes:
    a value pair differs at a single bit M != N (wrong bit), or the pair differs at MULTIPLE bits
    while the text asserts 'only' bit N (a multi-bit delta cannot be 'only' one bit)."""
    defects = []
    for m in re.finditer(r'bit\s+(\d+)', text):
        n = int(m.group(1))
        lo, hi = max(0, m.start() - 260), min(len(text), m.end() + 60)
        window = text[lo:hi]
        if not _DELTA_WORDS.search(window):
            continue
        vals = sorted({int(h, 16) for h in HEX.findall(window)})
        if len(vals) < 2:
            continue
        single = {}   # bit -> (a, b) for pairs differing at exactly one bit
        multi = []    # (a, b, [bits]) for pairs differing at more than one bit
        for i in range(len(vals)):
            for j in range(i + 1, len(vals)):
                x = vals[i] ^ vals[j]
                if x == 0:
                    continue
                b = _single_bit_index(x)
                if b is not None:
                    single[b] = (vals[i], vals[j])
                else:
                    multi.append((vals[i], vals[j], _set_bits(x)))
        if n in single:
            continue   # the claim matches a real single-bit delta
        asserts_only = bool(re.search(r'\bonly\b', window, re.I))
        if single:
            real = ", ".join(f"0x{a:X}^0x{b:X}=bit {bb}" for bb, (a, b) in sorted(single.items()))
            defects.append({"severity": "FAIL", "check": "bit_delta",
                            "detail": f"claims 'bit {n}' but the single-bit delta in context is at "
                                      f"a different bit; actual: {real}",
                            "context": window.strip()[:200]})
        elif multi and asserts_only:
            a, b, bits = min(multi, key=lambda t: len(t[2]))
            if len(vals) == 2 or n in bits:
                defects.append({"severity": "FAIL", "check": "bit_delta",
                                "detail": f"claims 'only at bit {n}' but 0x{a:X}^0x{b:X} differs at "
                                          f"multiple bits: {bits}",
                                "context": window.strip()[:200]})
    return defects


def check_fractions(text):
    """Verify 'A to B = p/q' ratio claims (either the remaining fraction B/A or the drop (A-B)/A)."""
    defects = []
    # require an explicit transition ("->" or "to"); a bare hyphen is a numeric range, not a drop
    for m in re.finditer(r'(\d{2,})\s*(?:->|to)\s*(\d{2,}).{0,20}?(\d+)\s*/\s*(\d+)', text):
        a, b, p, q = map(int, m.groups())
        if a == 0 or q == 0:
            continue
        claimed = p / q
        if abs(b / a - claimed) > 1e-6 and abs((a - b) / a - claimed) > 1e-6:
            defects.append({"severity": "FAIL", "check": "fraction",
                            "detail": f"'{a} to {b} = {p}/{q}' inconsistent: {b}/{a}={b/a:.4f}, "
                                      f"({a}-{b})/{a}={(a-b)/a:.4f}, {p}/{q}={claimed:.4f}",
                            "context": m.group(0)})
    return defects


def check_grounding(text, corpus):
    """For an asserted '<id> ... stock <value>', require the corpus to carry that id, and flag a
    value that contradicts the corpus's own line for the id. Lines tagged as inference are exempt."""
    defects = []
    if not corpus:
        return defects
    corpus_l = corpus.lower()
    corpus_lines = corpus.splitlines()
    for line in text.splitlines():
        if INFER_TAG.search(line):
            continue
        m = re.search(r'(0x[0-9a-fA-F]{5,})\b.{0,50}?(?:stock|=)\s*`?(0x[0-9a-fA-F]{4,})', line)
        if not m:
            continue
        ident, val = m.group(1).lower(), m.group(2).lower()
        if ident not in corpus_l:
            defects.append({"severity": "WARN", "check": "grounding",
                            "detail": f"identifier {ident} not found in corpus (asserted value {val})",
                            "context": line.strip()[:200]})
            continue
        idx = corpus_l.find(ident)
        near = corpus_l[max(0, idx - 100):idx + 300]
        if val not in near:
            id_line = next((cl for cl in corpus_lines if ident in cl.lower()), "")
            other = [v.lower() for v in HEX.findall(id_line) if v.lower() != ident]
            if other and val not in other:
                defects.append({"severity": "FAIL", "check": "grounding",
                                "detail": f"{ident} asserted value {val} but corpus line shows {other}",
                                "context": id_line.strip()[:200]})
            else:
                defects.append({"severity": "WARN", "check": "grounding",
                                "detail": f"{ident}={val} not corroborated near the id in corpus",
                                "context": line.strip()[:160]})
    return defects


def run_gate(text, corpus=""):
    """Run every deterministic check. Returns (passed, defects); passed is True iff no FAIL."""
    defects = check_bit_deltas(text) + check_fractions(text) + check_grounding(text, corpus)
    passed = not any(d["severity"] == "FAIL" for d in defects)
    return passed, defects


if __name__ == "__main__":
    import sys
    text = open(sys.argv[1]).read()
    corpus = open(sys.argv[2]).read() if len(sys.argv) > 2 else ""
    ok, defects = run_gate(text, corpus)
    fails = [d for d in defects if d["severity"] == "FAIL"]
    warns = [d for d in defects if d["severity"] == "WARN"]
    print(f"GATE: {'PASS' if ok else 'FAIL'}  ({len(fails)} FAIL, {len(warns)} WARN)")
    for d in fails + warns:
        print(f"\n[{d['severity']}] {d['check']}: {d['detail']}")
        print(f"   ctx: {d['context']}")
    sys.exit(0 if ok else 1)
