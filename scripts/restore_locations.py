"""Restore real place names (geography cluster) in this project's artifacts.

Authoritative map (IDENTITY_MAP.private.md, Geography): all distinctive invented
tokens, so whole-word case-insensitive replacement to the canonical real name is safe.
Institution-linked 'Aravali' and unmapped venue/street names (Paja, Vasterby,
Kalevankatu, Nord Plaza) are intentionally left alone.
"""
import json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# order: longer / typo variants before the base token; regex, case-insensitive, word-boundary
RULES = [
    (r"\bNorvannia\b", "Finland"),
    (r"\bNorvania\b", "Finland"),
    (r"\bEstvaria\b", "Estonia"),
    (r"\bTallvik\b", "Tallinn"),
    (r"\bKaldera\b", "Tampere"),
    (r"\bKaldra\b", "Tampere"),
    (r"\bNorvik\b", "Helsinki"),
]


def apply_rules(text):
    counts = {}
    for pat, rep in RULES:
        text, n = re.subn(pat, rep, text, flags=re.IGNORECASE)
        counts[pat] = n
    return text, counts


def contexts(text, pat, k=4):
    out = []
    for i, m in enumerate(re.finditer(pat, text, flags=re.IGNORECASE)):
        if i >= k:
            break
        s = max(0, m.start() - 25)
        out.append(text[s:m.end() + 18].replace("\n", " "))
    return out


def main(write):
    targets = [ROOT / ".cache" / "bundle.json"] + sorted(
        (ROOT / "processed_data").glob("batch*_redacted.md"))
    grand = {}
    for p in targets:
        raw = p.read_text()
        new, counts = apply_rules(raw)
        for k, v in counts.items():
            grand[k] = grand.get(k, 0) + v
        if p.suffix == ".json":
            json.loads(new)
        if write:
            p.write_text(new)
        print(f"{'WROTE' if write else 'dry'}: {p.name}  (+{sum(counts.values())} repl)")

    print("\n=== replacements per rule (all files) ===")
    for pat, rep in RULES:
        print(f"  {grand.get(pat,0):5}  {pat:16} -> {rep}")

    if not write:
        wa = (ROOT / "processed_data" / "batch3_whatsapp_redacted.md").read_text()
        for pat, _ in RULES:
            cs = contexts(wa, pat)
            if cs:
                print(f"\n=== sample {pat} ===")
                for c in cs:
                    print("   …", c)


if __name__ == "__main__":
    main(write="--write" in sys.argv)
