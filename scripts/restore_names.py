"""One-off: restore the real IEEE/ICIP/SPS org names in this project's artifacts.

Scoped to the public org cluster only (IEEE / ICIP / SPS). People, universities,
geography and the paper title stay pseudonymized. Case-sensitive ordered replaces,
plus whole-word regex for the short ambiguous tokens ISPA / ISS.
"""
import json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (pattern, replacement, is_regex)
RULES = [
    ("ISPA International Conference on Visual Signal Processing",
     "IEEE International Conference on Image Processing", False),
    ("ISPA Signal Society", "IEEE Signal Processing Society", False),
    ("ispasignalsociety.org", "signalprocessingsociety.org", False),
    ("ISPA International LLC", "IEEE", False),
    ("ISS Travel Grants", "SPS Travel Grants", False),
    ("isstravelgrants@confworkshops.net", "spstravelgrants@cmsworkshops.com", False),
    ("ISPA-ICVSP", "IEEE-ICIP", False),
    ("ICVSP2026", "ICIP2026", False),
    ("ICVSP 2026", "ICIP 2026", False),
    ("ICVSP", "ICIP", False),
    ("icvsp", "icip", False),                       # email local-parts (icvspreg, icvsp2026)
    ("ispaconfreg.org", "ieeeicassp.org", False),
    ("ispa.org", "ieee.org", False),                # org domain; local-parts stay pseudonymous
    ("Ispaconfreg", "IeeeConfReg", False),
    ("Ispasignalsociety", "SignalProcessingSociety", False),
    (r"\bISPA\b", "IEEE", True),
    (r"\bISS\b", "SPS", True),
]


def apply_rules(text):
    counts = {}
    for pat, rep, is_rx in RULES:
        if is_rx:
            new, n = re.subn(pat, rep, text)
        else:
            n = text.count(pat)
            new = text.replace(pat, rep)
        counts[pat] = n
        text = new
    return text, counts


def contexts(text, pat, is_rx, k=4):
    out = []
    it = (re.finditer(pat, text) if is_rx else
          (m for m in re.finditer(re.escape(pat), text)))
    for i, m in enumerate(it):
        if i >= k:
            break
        s = max(0, m.start() - 30)
        out.append(text[s:m.end() + 20].replace("\n", " "))
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
            json.loads(new)  # validate
        if write:
            p.write_text(new)
        print(f"{'WROTE' if write else 'dry'}: {p.name}  (+{sum(counts.values())} repl)")

    print("\n=== replacements per rule (all files) ===")
    for pat, _, _ in RULES:
        print(f"  {grand.get(pat,0):5}  {pat}")

    # eyeball the two ambiguous whole-word rules on the source
    if not write:
        wa = (ROOT / "processed_data" / "batch3_whatsapp_redacted.md").read_text()
        for pat in (r"\bISS\b", r"\bISPA\b"):
            print(f"\n=== contexts for {pat} in whatsapp source ===")
            for c in contexts(wa, pat, True):
                print("   …", c)


if __name__ == "__main__":
    main(write="--write" in sys.argv)
