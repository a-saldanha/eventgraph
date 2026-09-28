"""Tests for Phase 5 parameterized query types."""
from __future__ import annotations

import pytest
from app.graph_model import Entity, EntityType, EventGraph, Mention, RelevanceVerdict
from app.pipeline.build import Bundle
from app.ingest.dedup import DedupResult
from app import queries as Q


def _make_bundle(entities=None, edges=None, items=None, timeline=None):
    graph = EventGraph(
        entities=entities or [],
        edges=edges or [],
        merges=[],
        relevance=[RelevanceVerdict(item_id="i1", relevant=True, score=1.0, rationale="test")],
    )
    dedup = DedupResult()
    return Bundle(
        items=items or [],
        dedup=dedup,
        graph=graph,
        timeline=timeline or [],
        stats={},
        review_queue=[],
        item_topics={},
    )


def _money_entity(eid, amount, currency=None, needs_review=False, item_ids=None):
    attrs = {"amount": amount, "needs_review": needs_review}
    if currency:
        attrs["currency"] = currency
    mentions = [Mention(item_id=iid, text=str(amount)) for iid in (item_ids or ["i1"])]
    return Entity(id=eid, type=EntityType.MONEY, label=str(amount), attrs=attrs, mentions=mentions)


def _person_entity(eid, label, owner=False, mention_count=1):
    mentions = [Mention(item_id=f"i{j}", text=label) for j in range(mention_count)]
    attrs = {}
    if owner:
        attrs["owner"] = True
    return Entity(id=eid, type=EntityType.PERSON, label=label, attrs=attrs, mentions=mentions)


# ----- money() tests -----

def test_money_splits_resolved_vs_flagged():
    """money() splits resolved amounts from those with no currency marker."""
    resolved = _money_entity("m1", 1000.00, currency="USD")
    flagged = _money_entity("m2", 500.00, needs_review=True)
    b = _make_bundle(entities=[resolved, flagged])
    result = Q.money(b)
    assert "table" in result
    rows = result["table"]
    flagged_rows = [r for r in rows if r["flagged"]]
    resolved_rows = [r for r in rows if not r["flagged"]]
    assert len(flagged_rows) == 1
    assert len(resolved_rows) == 1


def test_money_excludes_unflagged_amounts():
    """Flagged amounts appear in caveats and are marked flagged=True, NOT summed."""
    resolved = _money_entity("m1", 200.00, currency="USD")
    no_currency = _money_entity("m2", 99.99, needs_review=True)  # no currency marker
    b = _make_bundle(entities=[resolved, no_currency])
    result = Q.money(b)
    # The no-currency amount must be flagged
    flagged_rows = [r for r in result["table"] if r["flagged"]]
    assert len(flagged_rows) == 1
    assert flagged_rows[0]["currency"] == "?"
    # Caveats must mention the issue
    assert result["caveats"], "should have at least one caveat for unflagged amounts"


def test_money_no_amounts():
    b = _make_bundle()
    result = Q.money(b)
    assert result["answer"] == "No financial amounts found."
    assert result["table"] == []


# ----- who() tests -----

def test_who_never_returns_owner():
    """who() must exclude the entity with attrs.owner=True."""
    owner = _person_entity("p0", "Me", owner=True, mention_count=100)
    alice = _person_entity("p1", "Alice", mention_count=5)
    bob = _person_entity("p2", "Bob", mention_count=3)
    b = _make_bundle(entities=[owner, alice, bob])
    result = Q.who(b)
    names = [r["name"] for r in result["table"]]
    assert "Me" not in names, "owner must not appear in who() results"
    assert "Alice" in names or "Bob" in names


def test_who_returns_required_keys():
    alice = _person_entity("p1", "Alice", mention_count=2)
    b = _make_bundle(entities=[alice])
    result = Q.who(b)
    assert "answer" in result
    assert "table" in result
    assert "subgraph" in result


# ----- entity() tests -----

def test_entity_query_wellformed():
    """entity() returns required keys when entity exists."""
    e = _person_entity("p1", "Alice")
    b = _make_bundle(entities=[e])
    result = Q.entity("p1", b)
    for key in ("answer", "answer_parts", "entity", "citations", "subgraph", "connected_edges"):
        assert key in result, f"missing key: {key}"


def test_entity_query_missing():
    """entity() returns error dict when entity not found."""
    b = _make_bundle()
    result = Q.entity("nonexistent", b)
    assert "error" in result


# ----- starter_questions() tests -----

def test_starter_questions_includes_all_query_types():
    b = _make_bundle()
    qs = Q.starter_questions(b)
    ids = [q["id"] for q in qs]
    assert "money" in ids
    assert "timeline" in ids
    assert "who" in ids


def test_starter_questions_adds_entity_questions_for_top_people():
    alice = _person_entity("p1", "Alice", mention_count=10)
    b = _make_bundle(entities=[alice])
    qs = Q.starter_questions(b)
    # Should have at least the base QUERY_TYPES + something about Alice
    assert len(qs) >= len(Q.QUERY_TYPES)
