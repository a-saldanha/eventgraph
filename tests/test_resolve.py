"""Phase 2: identity must-links, owner inference, dedup application. Invented data."""
from app.ingest.participants import build_participants
from app.pipeline.build import build_graph
from app.resolve.identity import build_identity_index
from app.resolve.owner import infer_owner
from app.schema import Participant, Provenance, SourceItem, SourceType


def _item(iid, stype, body="", parts=None, conv="c", ts=None):
    return SourceItem(
        id=iid, source_type=stype, conversation_id=conv, body=body,
        participants=parts or [], content_hash=iid,
        provenance=Provenance(batch_file="t", item_index=1),
    )


def _p(email=None, phone=None, name=None, role="sender", kind="person"):
    if email:
        return Participant(display_name=name, id_type="email", id_value=email, raw=email,
                           role=role, kind=kind)
    if phone:
        return Participant(display_name=name, id_type="phone", id_value=phone, raw=phone,
                           role=role, kind=kind)
    return Participant(display_name=name, raw=name or "", role=role, kind=kind)


def test_emails_differing_only_in_case_share_one_identity():
    items = [_item("a", SourceType.EMAIL, parts=[_p(email="jane@x.com")]),
             _item("b", SourceType.EMAIL, parts=[_p(email="JANE@x.com")])]
    idx = build_identity_index(items)
    assert len(idx.clusters) == 1


def test_role_mailbox_is_not_an_identity():
    items = [_item("a", SourceType.EMAIL, parts=[_p(email="support@x.com", kind="system")])]
    idx = build_identity_index(items)
    assert idx.clusters == []


def test_owner_inference_picks_multichannel_identity_and_flags_uncertain():
    # One identity reaches two channels and is the inbox recipient -> owner.
    items = [
        _item("e1", SourceType.EMAIL, parts=[_p(email="me@x.com", role="recipient")]),
        _item("e2", SourceType.EMAIL, parts=[_p(email="me@x.com", role="recipient")], conv="c2"),
        _item("w1", SourceType.WHATSAPP, parts=[_p(email="me@x.com")], conv="w"),
        _item("o1", SourceType.EMAIL, parts=[_p(email="other@x.com")], conv="c3"),
    ]
    idx = build_identity_index(items)
    owner = infer_owner(items, idx)
    assert owner.primary_key == "email:me@x.com" and owner.confident

    # A dead tie is not confident.
    tie = [_item("t1", SourceType.EMAIL, parts=[_p(email="a@x.com")]),
           _item("t2", SourceType.EMAIL, parts=[_p(email="b@x.com")], conv="c2")]
    r = infer_owner(tie, build_identity_index(tie))
    assert not r.confident


def test_duplicate_is_marked_and_excluded_from_extraction():
    body = "Invoice total 100.00 USD paid to Acme."
    items = [_item("x1", SourceType.EMAIL, body=body, parts=[_p(email="a@x.com")]),
             _item("x2", SourceType.EMAIL, body=body, parts=[_p(email="a@x.com")])]
    items[0].content_hash = items[1].content_hash = "same"
    b = build_graph(items, mode="heuristic")
    assert b.stats["canonical_items"] == 1
    assert sum(1 for it in b.items if it.duplicate_of) == 1
