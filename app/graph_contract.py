"""Graph contract: validate and repair a Bundle.

validate(bundle, strict=False) -> ContractReport
    Check structural invariants. strict=True raises on the first violation
    (for tests that want to catch bugs before repair hides them).

repair(bundle) -> (bundle, ContractReport)
    Fix every violation in-place and record what was changed.

Both are called at the end of build_graph and on every bundle load.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Canonical edge kind vocabulary — shared with the frontend legend via /api/meta.
EDGE_KINDS: frozenset[str] = frozenset({
    "corresponded_with",
    "affiliated_with",
    "paid_to",
    "held_at",
    "document_for",
    "on_date",
    "paid",
})

ENTITY_TYPES: frozenset[str] = frozenset({
    "person", "org", "location", "money", "document", "subevent",
})

PROVENANCE_VALUES: frozenset[str] = frozenset({
    "structural", "llm_verified", "fallback", "name_rule",
})

CONFIDENCE_VALUES: frozenset[str] = frozenset({
    "high", "medium", "low",
})

_MAX_LABEL = 120


@dataclass
class Violation:
    rule: str
    element_id: str
    detail: str


@dataclass
class ContractReport:
    violations: list[Violation] = field(default_factory=list)
    repairs: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.violations) == 0

    def add(self, rule: str, eid: str, detail: str) -> None:
        self.violations.append(Violation(rule, eid, detail))

    def model_dump(self) -> dict:
        return {
            "ok": self.ok,
            "violation_count": len(self.violations),
            "repair_count": len(self.repairs),
            "violations": [{"rule": v.rule, "id": v.element_id, "detail": v.detail}
                           for v in self.violations],
            "repairs": self.repairs,
        }


class ContractError(Exception):
    pass


def validate(bundle, strict: bool = False) -> ContractReport:
    """Check all contract rules. strict=True raises on first violation."""
    report = ContractReport()
    from .pipeline.build import Bundle
    _check(bundle, report, strict)
    return report


def repair(bundle) -> tuple[object, ContractReport]:
    """Fix all violations in-place. Returns (bundle, report)."""
    report = ContractReport()
    _check(bundle, report, strict=False)
    if not report.ok:
        _repair(bundle, report)
    return bundle, report


def _check(bundle, report: ContractReport, strict: bool) -> None:
    graph = bundle.graph
    item_ids = {it.id for it in bundle.items}
    entity_ids: set[str] = set()

    def fail(rule, eid, detail):
        report.add(rule, eid, detail)
        if strict:
            raise ContractError(f"{rule} [{eid}]: {detail}")

    # ── entity rules ─────────────────────────────────────────────────────────

    seen_ids: set[str] = set()
    owner_count = 0

    for e in graph.entities:
        eid = e.id

        # R1: unique IDs
        if eid in seen_ids:
            fail("R1_duplicate_id", eid, "duplicate entity ID")
        seen_ids.add(eid)

        # R2: valid type
        type_val = e.type.value if hasattr(e.type, "value") else str(e.type)
        if type_val not in ENTITY_TYPES:
            fail("R2_invalid_type", eid, f"unknown type {type_val!r}")

        # R3: non-empty label, length limit
        if not e.label or not e.label.strip():
            fail("R3_empty_label", eid, "empty label")
        elif len(e.label) > _MAX_LABEL:
            fail("R3_label_too_long", eid, f"label length {len(e.label)} > {_MAX_LABEL}")

        # R4: at least 1 mention whose item_id exists
        if not e.mentions:
            fail("R4_no_mentions", eid, "entity has no mentions")
        else:
            bad = [m.item_id for m in e.mentions if m.item_id not in item_ids]
            if bad:
                fail("R4_unknown_mention_item", eid, f"mention item_ids not in corpus: {bad[:3]}")

        # R5: owner count
        if e.attrs.get("owner"):
            owner_count += 1

        entity_ids.add(eid)

    # R5 continued: 0 or 1 owner
    if owner_count > 1:
        fail("R5_multiple_owners", "graph", f"{owner_count} entities marked owner")

    stats_confident = bundle.stats.get("owner_confident", False)
    if owner_count == 0 and stats_confident:
        fail("R5_owner_missing", "graph",
             "stats.owner_confident=True but no owner entity found")

    # ── edge rules ────────────────────────────────────────────────────────────

    edge_keys: set[tuple] = set()

    for ed in graph.edges:
        eid = ed.id

        # R6: endpoints exist, no self-loops
        if ed.source not in entity_ids:
            fail("R6_dangling_source", eid, f"source {ed.source!r} not in entities")
        if ed.target not in entity_ids:
            fail("R6_dangling_target", eid, f"target {ed.target!r} not in entities")
        if ed.source == ed.target:
            fail("R6_self_loop", eid, "self-loop edge")

        # R7: valid edge kind
        if ed.kind not in EDGE_KINDS:
            fail("R7_invalid_kind", eid, f"unknown edge kind {ed.kind!r}")

        # R8: at least 1 evidence item that exists
        if not ed.evidence_item_ids:
            fail("R8_no_evidence", eid, "edge has no evidence items")
        else:
            bad = [i for i in ed.evidence_item_ids if i not in item_ids]
            if bad:
                fail("R8_unknown_evidence", eid, f"evidence item_ids not in corpus: {bad[:3]}")

        # R9: (source, target, kind) unique
        key = (ed.source, ed.target, ed.kind)
        if key in edge_keys:
            fail("R9_duplicate_edge", eid, f"duplicate (source,target,kind): {key}")
        edge_keys.add(key)

    # ── merge records ─────────────────────────────────────────────────────────

    for mr in graph.merges:
        if mr.canonical_id not in entity_ids:
            fail("R10_merge_bad_id", mr.canonical_id,
                 f"merge record references non-existent entity {mr.canonical_id!r}")

    # ── money entities ────────────────────────────────────────────────────────

    for e in graph.entities:
        type_val = e.type.value if hasattr(e.type, "value") else str(e.type)
        if type_val != "money":
            continue
        amt = e.attrs.get("amount")
        if amt is None or not isinstance(amt, (int, float)):
            fail("R11_money_no_amount", e.id, "money entity missing numeric amount")
        if not e.attrs.get("currency") and not e.attrs.get("needs_review"):
            fail("R11_money_no_currency", e.id,
                 "money entity has no currency and needs_review is not True")


def _repair(bundle, report: ContractReport) -> None:
    graph = bundle.graph
    item_ids = {it.id for it in bundle.items}

    # Build valid entity id set for edge repair.
    seen: set[str] = set()
    keep_entities = []
    for e in graph.entities:
        if e.id in seen:
            report.repairs.append(f"removed duplicate entity {e.id!r}")
            continue
        seen.add(e.id)

        # Fix empty/long labels
        if not e.label or not e.label.strip():
            e.label = e.id
            report.repairs.append(f"set empty label on {e.id!r} to its ID")
        elif len(e.label) > _MAX_LABEL:
            full = e.label
            e.attrs["full_label"] = full
            e.label = full[:_MAX_LABEL - 1] + "…"
            report.repairs.append(f"truncated label on {e.id!r}")

        # Drop mentions with unknown item_ids
        before = len(e.mentions)
        e.mentions = [m for m in e.mentions if m.item_id in item_ids]
        if len(e.mentions) < before:
            dropped = before - len(e.mentions)
            report.repairs.append(
                f"dropped {dropped} unknown-item mention(s) from {e.id!r}"
            )

        if e.mentions:  # only keep entities with at least one valid mention
            keep_entities.append(e)
        else:
            report.repairs.append(f"removed entity {e.id!r} with no valid mentions")

    graph.entities = keep_entities
    valid_ids = {e.id for e in graph.entities}

    # Fix edge violations
    edge_keys: set[tuple] = set()
    keep_edges = []
    for ed in graph.edges:
        if ed.source not in valid_ids or ed.target not in valid_ids:
            report.repairs.append(
                f"removed edge {ed.id!r} with dangling endpoint(s)"
            )
            continue
        if ed.source == ed.target:
            report.repairs.append(f"removed self-loop edge {ed.id!r}")
            continue
        if ed.kind not in EDGE_KINDS:
            report.repairs.append(
                f"removed edge {ed.id!r} with unknown kind {ed.kind!r}"
            )
            continue
        ed.evidence_item_ids = [i for i in ed.evidence_item_ids if i in item_ids]
        if not ed.evidence_item_ids:
            report.repairs.append(f"removed edge {ed.id!r} with no valid evidence")
            continue
        key = (ed.source, ed.target, ed.kind)
        if key in edge_keys:
            report.repairs.append(f"removed duplicate edge {ed.id!r}")
            continue
        edge_keys.add(key)
        keep_edges.append(ed)

    graph.edges = keep_edges

    # Fix merge records
    graph.merges = [
        mr for mr in graph.merges if mr.canonical_id in valid_ids
    ]

    log.info(
        "graph_contract repair: %d violation(s), %d fix(es)",
        len(report.violations), len(report.repairs),
    )
