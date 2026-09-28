# Baseline (Phase 0)

Captured with `scripts/graph_report.py` before any Phase 0 behaviour change.
Same 1,482-item corpus, two builds.

## (A) Current saved bundle — LLM mode (`.cache/bundle.json`)

```
mode: llm
items: 1482  unique after dedup: 1044 (exact 419, near 22)

entities by type: person=295, org=52, location=33, money=11, document=7, topic=0, subevent=7
persons: 295  unknown/empty: 256
owner (heuristic: most-mentioned person): 'unknown'  mentions=457  channels=['pdf', 'whatsapp']

top correspondents (person, by mentions):
    396  'IT Aravali PhD? ICIP Srinivasa'  ['whatsapp']
    254  '(self — personal planning sheet)'  ['excel_row']
     43  'Alan Saldanha'  ['email', 'pdf']
     39  'IT Hooghly Rajiv  Bansal'  ['whatsapp']
     20  'IT Coromandel PhD RAG Kavitha'  ['whatsapp']
      8  'IT Coromandel Professor Civil Ganesh Ramaswamy'  ['whatsapp']
      8  'RAG_chatbot'  ['whatsapp']
      8  'Revathi Sundaram'  ['whatsapp']
      7  'Matteo Rinaldi'  ['email']
      7  'Milica Petrovic from ConfDesk'  ['email']

duplicate ORG label groups: 2
  [confdesk] ['ConfDesk', 'Confdesk']
  [rail nv] ['Rail NV', 'Rail-NV']
duplicate LOCATION label groups: 0

isolated entities: 329 / 405 (81.2%)
merges: 7   review queue: 0
```

## (B) Fresh heuristic build (`processed_data/`)

```
mode: heuristic
items: 1482  unique after dedup: 1044 (exact 419, near 22)

entities by type: person=90, org=17, location=2, money=5, document=7, topic=0, subevent=7
persons: 90  unknown/empty: 52
owner (heuristic: most-mentioned person): 'unknown'  mentions=73  channels=['pdf', 'whatsapp']

top correspondents (person, by mentions):
     53  'IT Aravali PhD? ICIP Srinivasa'  ['whatsapp']
     50  '(self — personal planning sheet)'  ['excel_row']
     42  'Alan Saldanha'  ['email', 'pdf']
     10  'IT Coromandel PhD RAG Kavitha'  ['whatsapp']
      8  'IT Hooghly Rajiv  Bansal'  ['whatsapp']
      7  'Matteo Rinaldi'  ['email']
      7  'Milica Petrovic from ConfDesk'  ['email']
      6  'icip2026'  ['email']
      5  'Kabir Thakur'  ['email']
      4  'Vikram Sood'  ['email']

duplicate ORG label groups: 1
  [confdesk] ['ConfDesk', 'Confdesk']
duplicate LOCATION label groups: 0

isolated entities: 64 / 128 (50.0%)
merges: 7   review queue: 0
```

## What this exposes (regression targets for later phases)

- **Owner split.** The most-mentioned "person" is `unknown` (457 mentions, whatsapp+pdf), separate from `Alan Saldanha` (43, email+pdf) and the calendar `(self …)` marker (254). One person, three entities.
- **Non-people at the top.** Top correspondents are WhatsApp chat titles and a planning-sheet self-marker, not individuals.
- **Unknown people.** 256 of 295 person entities are labelled `unknown` or empty (LLM bundle).
- **Duplicate org labels.** ConfDesk/Confdesk and Rail NV/Rail-NV are separate entities.
- **Connectivity.** 329 of 405 entities (81%) have no edges.
- **Timezone.** With blanket `warnings.filterwarnings("ignore")` removed, dateutil warns it can't parse `EET`. Phase 0 fixes tz parsing.
