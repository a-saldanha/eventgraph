"""Phase 1 tests: graph contract validate() and repair().

All fixtures use invented data; no network calls are made.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.graph_contract import (
    ContractError,
    ContractReport,
    repair,
    validate,
    EDGE_KINDS,
)
from app.graph_model import Edge, Entity, EntityType, EventGraph, Mention, MergeRecord
from app.schema import SourceItem, SourceType, Provenance


# ── helpers ─────────────────────────────────────────────────────────────────

def _prov():
    return Provenance(batch_file="test.md", item_index=0)


def _item(iid="i0", body="hello"):
    return SourceItem(
        id=iid, source_type=SourceType.EMAIL, body=body,
        channel="email", timestamp=datetime(2024, 1, 1), provenance=_prov(),
    )


def _entity(eid, label, etype=EntityType.PERSON, item_id="i0", **attrs):
    return Entity(
        id=eid, type=etype, label=label,
        mentions=[Mention(item_id=item_id, text=label)],
        attrs=attrs,
    )


def _edge(eid, src, tgt, kind="corresponded_with", item_id="i0"):
    return Edge(id=eid, source=src, target=tgt, kind=kind,
                weight=1.0, evidence_item_ids=[item_id])


def _bundle(entities=None, edges=None, merges=None, items=None, stats=None):
    graph = EventGraph(entities=entities or [], edges=edges or [], merges=merges or [])
    items = items or [_item()]

    class _B:
        pass
    b = _B()
    b.graph = graph
    b.items = items
    b.stats = stats or {}
    b.review_queue = []
    return b


# ── R1: unique entity IDs ─────────────────────────────────────────────────────

def test_r1_duplicate_entity_id_raises_in_strict():
    e1 = _entity("p0", "Alice")
    e2 = _entity("p0", "Bob")
    bundle = _bundle(entities=[e1, e2])
    with pytest.raises(ContractError, match="R1"):
        validate(bundle, strict=True)


def test_r1_repair_removes_duplicate():
    e1 = _entity("p0", "Alice")
    e2 = _entity("p0", "Bob")
    bundle = _bundle(entities=[e1, e2])
    bundle, report = repair(bundle)
    ids = [e.id for e in bundle.graph.entities]
    assert ids.count("p0") == 1
    assert any("duplicate" in r for r in report.repairs)


# ── R2: valid entity type ─────────────────────────────────────────────────────

def test_r2_invalid_type_is_flagged():
    e = Entity.model_construct(
        id="p0", type="alien", label="Alice",
        mentions=[Mention(item_id="i0", text="Alice")],
        aliases=[], attrs={},
    )
    bundle = _bundle(entities=[e])
    report = validate(bundle)
    assert any(v.rule.startswith("R2") for v in report.violations)


# ── R3: label non-empty ───────────────────────────────────────────────────────

def test_r3_empty_label_flagged():
    e = _entity("p0", "")
    bundle = _bundle(entities=[e])
    report = validate(bundle)
    assert any(v.rule.startswith("R3") for v in report.violations)


def test_r3_repair_sets_id_as_label():
    e = _entity("p0", "")
    bundle = _bundle(entities=[e])
    bundle, report = repair(bundle)
    assert bundle.graph.entities[0].label == "p0"


def test_r3_label_too_long_flagged():
    e = _entity("p0", "A" * 200)
    bundle = _bundle(entities=[e])
    report = validate(bundle)
    assert any(v.rule == "R3_label_too_long" for v in report.violations)


# ── R4: mentions exist in corpus ─────────────────────────────────────────────

def test_r4_no_mentions_flagged():
    e = Entity(id="p0", type=EntityType.PERSON, label="Ghost", mentions=[])
    bundle = _bundle(entities=[e])
    report = validate(bundle)
    assert any(v.rule == "R4_no_mentions" for v in report.violations)


def test_r4_unknown_mention_item_flagged():
    e = _entity("p0", "Alice", item_id="nonexistent")
    bundle = _bundle(entities=[e], items=[_item("i0")])
    report = validate(bundle)
    assert any(v.rule == "R4_unknown_mention_item" for v in report.violations)


def test_r4_repair_drops_bad_mention_and_removes_orphan_entity():
    e = _entity("p0", "Alice", item_id="nonexistent")
    bundle = _bundle(entities=[e], items=[_item("i0")])
    bundle, report = repair(bundle)
    # Entity has no valid mentions → removed
    assert len(bundle.graph.entities) == 0
    assert any("no valid mentions" in r for r in report.repairs)


# ── R5: owner count ───────────────────────────────────────────────────────────

def test_r5_two_owners_flagged():
    e1 = _entity("p0", "Alice", owner=True)
    e2 = _entity("p1", "Bob", owner=True)
    bundle = _bundle(entities=[e1, e2])
    report = validate(bundle)
    assert any(v.rule == "R5_multiple_owners" for v in report.violations)


def test_r5_zero_owners_no_violation_when_not_confident():
    e = _entity("p0", "Alice")
    bundle = _bundle(entities=[e], stats={"owner_confident": False})
    report = validate(bundle)
    assert not any(v.rule.startswith("R5") for v in report.violations)


def test_r5_zero_owners_violation_when_confident():
    e = _entity("p0", "Alice")
    bundle = _bundle(entities=[e], stats={"owner_confident": True})
    report = validate(bundle)
    assert any(v.rule == "R5_owner_missing" for v in report.violations)


# ── R6: edge endpoints ────────────────────────────────────────────────────────

def test_r6_dangling_source_flagged():
    e = _entity("p0", "Alice")
    ed = _edge("e0", "nonexistent", "p0")
    bundle = _bundle(entities=[e], edges=[ed])
    report = validate(bundle)
    assert any(v.rule == "R6_dangling_source" for v in report.violations)


def test_r6_dangling_target_flagged():
    e = _entity("p0", "Alice")
    ed = _edge("e0", "p0", "nonexistent")
    bundle = _bundle(entities=[e], edges=[ed])
    report = validate(bundle)
    assert any(v.rule == "R6_dangling_target" for v in report.violations)


def test_r6_self_loop_flagged():
    e = _entity("p0", "Alice")
    ed = _edge("e0", "p0", "p0")
    bundle = _bundle(entities=[e], edges=[ed])
    report = validate(bundle)
    assert any(v.rule == "R6_self_loop" for v in report.violations)


def test_r6_repair_removes_dangling_edges():
    e = _entity("p0", "Alice")
    ed = _edge("e0", "nonexistent", "p0")
    bundle = _bundle(entities=[e], edges=[ed])
    bundle, report = repair(bundle)
    assert len(bundle.graph.edges) == 0
    assert any("dangling" in r for r in report.repairs)


# ── R7: valid edge kind ───────────────────────────────────────────────────────

def test_r7_unknown_edge_kind_flagged():
    e1 = _entity("p0", "Alice")
    e2 = _entity("p1", "Bob")
    ed = _edge("e0", "p0", "p1", kind="invented_relation")
    bundle = _bundle(entities=[e1, e2], edges=[ed])
    report = validate(bundle)
    assert any(v.rule == "R7_invalid_kind" for v in report.violations)


def test_r7_all_valid_kinds_pass():
    items = [_item(f"i{k}") for k in range(len(EDGE_KINDS))]
    entities = [_entity(f"e{k}", f"Entity{k}", item_id=items[k].id)
                for k in range(len(EDGE_KINDS))]
    edges = [
        _edge(f"edge{k}", entities[k].id, entities[(k + 1) % len(entities)].id,
              kind=kind, item_id=items[k].id)
        for k, kind in enumerate(sorted(EDGE_KINDS))
    ]
    # Remove self-loops (happens when len(entities)==1)
    edges = [e for e in edges if e.source != e.target]
    bundle = _bundle(entities=entities, edges=edges, items=items)
    report = validate(bundle)
    kind_violations = [v for v in report.violations if v.rule.startswith("R7")]
    assert not kind_violations, kind_violations


# ── R8: edge evidence ─────────────────────────────────────────────────────────

def test_r8_no_evidence_flagged():
    e1 = _entity("p0", "Alice")
    e2 = _entity("p1", "Bob")
    ed = Edge(id="e0", source="p0", target="p1", kind="corresponded_with",
              evidence_item_ids=[])
    bundle = _bundle(entities=[e1, e2], edges=[ed])
    report = validate(bundle)
    assert any(v.rule == "R8_no_evidence" for v in report.violations)


def test_r8_unknown_evidence_item_flagged():
    e1 = _entity("p0", "Alice")
    e2 = _entity("p1", "Bob")
    ed = _edge("e0", "p0", "p1", item_id="nonexistent")
    bundle = _bundle(entities=[e1, e2], edges=[ed], items=[_item("i0")])
    report = validate(bundle)
    assert any(v.rule == "R8_unknown_evidence" for v in report.violations)


def test_r8_repair_removes_bad_evidence_and_edge():
    e1 = _entity("p0", "Alice")
    e2 = _entity("p1", "Bob")
    ed = _edge("e0", "p0", "p1", item_id="nonexistent")
    bundle = _bundle(entities=[e1, e2], edges=[ed], items=[_item("i0")])
    bundle, report = repair(bundle)
    assert len(bundle.graph.edges) == 0


# ── R9: duplicate edges ───────────────────────────────────────────────────────

def test_r9_duplicate_edge_flagged():
    e1 = _entity("p0", "Alice")
    e2 = _entity("p1", "Bob")
    ed1 = _edge("e0", "p0", "p1")
    ed2 = _edge("e1", "p0", "p1")  # same src/tgt/kind
    bundle = _bundle(entities=[e1, e2], edges=[ed1, ed2])
    report = validate(bundle)
    assert any(v.rule == "R9_duplicate_edge" for v in report.violations)


def test_r9_repair_keeps_first_duplicate():
    e1 = _entity("p0", "Alice")
    e2 = _entity("p1", "Bob")
    ed1 = _edge("e0", "p0", "p1")
    ed2 = _edge("e1", "p0", "p1")
    bundle = _bundle(entities=[e1, e2], edges=[ed1, ed2])
    bundle, report = repair(bundle)
    assert len(bundle.graph.edges) == 1


# ── R10: merge record references ─────────────────────────────────────────────

def test_r10_merge_bad_id_flagged():
    e = _entity("p0", "Alice")
    mr = MergeRecord(canonical_id="nonexistent", merged_forms=["Alice"], rationale="test")
    bundle = _bundle(entities=[e], merges=[mr])
    report = validate(bundle)
    assert any(v.rule == "R10_merge_bad_id" for v in report.violations)


def test_r10_repair_removes_orphan_merge():
    e = _entity("p0", "Alice")
    mr = MergeRecord(canonical_id="nonexistent", merged_forms=["Alice"], rationale="test")
    bundle = _bundle(entities=[e], merges=[mr])
    bundle, report = repair(bundle)
    assert len(bundle.graph.merges) == 0


# ── R11: money entity fields ──────────────────────────────────────────────────

def test_r11_money_no_amount_flagged():
    e = _entity("m0", "$100", etype=EntityType.MONEY)
    e.attrs = {}  # no amount
    bundle = _bundle(entities=[e])
    report = validate(bundle)
    assert any(v.rule == "R11_money_no_amount" for v in report.violations)


def test_r11_money_no_currency_flagged():
    e = _entity("m0", "$100", etype=EntityType.MONEY)
    e.attrs = {"amount": 100.0}  # no currency, no needs_review
    bundle = _bundle(entities=[e])
    report = validate(bundle)
    assert any(v.rule == "R11_money_no_currency" for v in report.violations)


def test_r11_money_needs_review_suppresses_currency_check():
    e = _entity("m0", "$100", etype=EntityType.MONEY)
    e.attrs = {"amount": 100.0, "needs_review": True}
    bundle = _bundle(entities=[e])
    report = validate(bundle)
    assert not any(v.rule == "R11_money_no_currency" for v in report.violations)


# ── contract passes clean graph ───────────────────────────────────────────────

def test_clean_graph_has_no_violations():
    e1 = _entity("p0", "Alice")
    e2 = _entity("p1", "Bob")
    ed = _edge("e0", "p0", "p1")
    bundle = _bundle(entities=[e1, e2], edges=[ed])
    report = validate(bundle)
    assert report.ok, report.violations


def test_model_dump_is_json_serializable():
    import json
    report = ContractReport()
    report.add("R1", "x", "test")
    report.repairs.append("fixed something")
    d = report.model_dump()
    json.dumps(d)  # must not raise
