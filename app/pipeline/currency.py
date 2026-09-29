"""Currency detection for extracted amounts.

Amounts without an explicit symbol/code/word are flagged `needs_review`.
Supports US format (1,234.56), European format (1.234,56), and Indian
lakh format (1,00,000). Never assumes a currency.
"""
from __future__ import annotations

import re
from collections import Counter

# symbol → ISO code (symbols usually precede the number: $970, ₹5,000)
_SYMBOLS = [("$", "USD"), ("€", "EUR"), ("£", "GBP"), ("₹", "INR"), ("₩", "KRW"), ("¥", "JPY")]
# word/code → ISO code (usually follow the number: "26.6 eur", "5000 Rs")
_WORDS = [
    (r"\beur(?:o|os)?\b", "EUR"), (r"\beuros?\b", "EUR"),
    (r"\busd\b", "USD"), (r"\bdollars?\b", "USD"),
    (r"\binr\b", "INR"), (r"\brs\.?\b", "INR"), (r"\brupees?\b", "INR"),
    (r"\bnok\b", "NOK"), (r"\bkr\b", "NOK"), (r"\bkroner?\b", "NOK"),
    (r"\bgbp\b", "GBP"), (r"\bpounds?\b", "GBP"),
    (r"\bjpy\b", "JPY"), (r"\byen\b", "JPY"),
    (r"\bkrw\b", "KRW"), (r"\bwon\b", "KRW"),
    (r"\bcad\b", "CAD"), (r"\baud\b", "AUD"), (r"\bchf\b", "CHF"),
]
SYMBOL_OF = {
    "USD": "$", "EUR": "€", "GBP": "£", "INR": "₹",
    "KRW": "₩", "JPY": "¥", "NOK": "kr",
    "CAD": "CA$", "AUD": "A$", "CHF": "CHF ",
}
KNOWN = list(SYMBOL_OF.keys())


def normalize_amount(text: str) -> float | None:
    """Parse amount from US (1,234.56), European (1.234,56) or plain text."""
    t = text.strip().replace(" ", "")
    # European: ends with ,dd or has pattern .ddd,dd
    if re.search(r",\d{2}$", t) and "." in t:
        # 1.234,56 → 1234.56
        t = t.replace(".", "").replace(",", ".")
    elif re.search(r",\d{2}$", t):
        t = t.replace(",", ".")
    else:
        t = t.replace(",", "")
    try:
        return float(t)
    except ValueError:
        return None


def detect_currency(text: str) -> tuple[str | None, str]:
    """Return (ISO code, evidence-token) if a currency indicator is in `text`, else (None, '')."""
    for sym, code in _SYMBOLS:
        if sym in text:
            return code, sym
    low = text.lower()
    for pat, code in _WORDS:
        m = re.search(pat, low)
        if m:
            return code, m.group(0)
    return None, ""


def _amount_forms(amount: float) -> list[str]:
    two, twoc = f"{amount:.2f}", f"{amount:,.2f}"
    forms = {two, twoc, two.rstrip("0").rstrip("."), twoc.rstrip("0").rstrip(".")}
    if amount == int(amount):
        forms |= {f"{int(amount):,}", str(int(amount))}
    return sorted(forms, key=len, reverse=True)


def resolve_amount_currency(amount: float, item_ids, items_by_id, surfaces=None) -> dict:
    """Scan every occurrence of `amount` (in the given items + any LLM surfaces) for an
    adjacent currency indicator. Returns the currency attrs to attach to a money entity."""
    votes: Counter = Counter()
    evidence, snippets = set(), []

    for s in (surfaces or []):
        code, ev = detect_currency(s)
        if code:
            votes[code] += 1
            evidence.add(ev)
        snippets.append(s.strip()[:80])

    # boundary guard: don't match inside a larger number ("20" in "20340" / "8,480.53"),
    # but DO allow trailing sentence punctuation ("8480.53.").
    pat = re.compile(r"(?<![\d.,])(?:%s)(?!\d)(?![.,]\d)"
                     % "|".join(re.escape(f) for f in _amount_forms(amount)))
    for iid in item_ids:
        body = (items_by_id.get(iid) or {}).get("body", "") if isinstance(items_by_id.get(iid), dict) \
            else getattr(items_by_id.get(iid), "body", "")
        for m in pat.finditer(body):
            win = body[max(0, m.start() - 16): m.end() + 10]
            code, ev = detect_currency(win)
            if code:
                votes[code] += 1
                evidence.add(ev)
            snippets.append(win.strip().replace("\n", " ")[:80])

    currency = votes.most_common(1)[0][0] if votes else None
    ambiguous = len(votes) > 1  # different currencies seen for the same amount
    return {
        "amount": amount,
        "currency": currency,
        "currency_resolved": currency is not None and not ambiguous,
        "currency_evidence": sorted(evidence),
        "currency_ambiguous": ambiguous,
        "needs_review": currency is None or ambiguous,
        "snippets": snippets[:4],
    }


def label(amount: float, attrs: dict) -> str:
    """Money label: symbol only when the currency is resolved; a ⚠ flag otherwise."""
    cur = attrs.get("currency")
    if attrs.get("currency_resolved") and cur:
        return f"{SYMBOL_OF.get(cur, cur + ' ')}{amount:,.2f}" if cur != "NOK" else f"{amount:,.2f} kr"
    if attrs.get("currency_ambiguous"):
        return f"{amount:,.2f} ⚠ mixed-currency"
    return f"{amount:,.2f} ⚠ currency?"
