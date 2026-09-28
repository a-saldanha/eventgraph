"""Header/contact parsing into typed participants. Invented data only."""
from app.ingest.adapters import UnsupportedUpload, parse_upload
from app.ingest.participants import build_participants, normalize_phone
from app.schema import SourceType

EML = SourceType.EMAIL
WA = SourceType.WHATSAPP


def one(parts):
    assert len(parts) == 1, [p.model_dump() for p in parts]
    return parts[0]


def test_org_with_parenthesised_comma_is_one_org():
    p = one(build_participants(EML, to_val="Globex Logistics (North Wing, Springfield)"))
    assert p.kind == "org"
    assert p.display_name == "Globex Logistics"


def test_dash_is_a_placeholder():
    p = one(build_participants(EML, to_val="—"))
    assert p.kind == "placeholder"


def test_role_mailbox_is_system():
    p = one(build_participants(EML, from_val="support@example.com"))
    assert p.kind == "system"
    assert p.id_value == "support@example.com"


def test_quoted_display_name_with_comma_is_one_person():
    p = one(build_participants(EML, to_val='"Doe, Jane" <jane@example.com>'))
    assert p.kind == "person"
    assert p.id_value == "jane@example.com"


def test_bidi_marks_stripped_for_matching_but_kept_in_raw():
    raw_in = "‎Jane Doe"
    p = one(build_participants(WA, from_val=raw_in))
    assert "‎" in p.raw           # provenance keeps the original mark
    assert p.display_name == "Jane Doe"  # matching uses the cleaned text


def test_phone_variants_normalize_to_same_e164():
    # International and national formats of one US number (default region) normalize
    # identically, whether via phonenumbers or the deterministic fallback.
    a = normalize_phone("+1 (212) 736-5000")
    b = normalize_phone("212-736-5000")
    assert a == b == "+12127365000"


def test_whatsapp_group_title_is_not_a_person():
    p = one(build_participants(WA, from_val="Trip Planning Group",
                               group_title="Trip Planning Group"))
    assert p.kind == "group"
    assert p.kind != "person"


def test_descriptor_prefixed_label_splits_name_and_descriptors():
    p = one(build_participants(WA, from_val="ACME PhD Advisor Jane Doe"))
    assert p.kind == "person"
    assert p.display_name == "Jane Doe"
    assert "ACME" in p.descriptors and "Advisor" in p.descriptors


def test_corrupt_file_in_batch_does_not_block_the_others():
    files = [("good.eml", b"From: Jane Doe <jane@example.com>\nSubject: Hi\n\nHello."),
             ("photo.png", b"\x89PNG\r\n\x1a\n\x00bytes")]
    parsed, errors = [], []
    for name, data in files:
        try:
            parsed += parse_upload(name, data)
        except UnsupportedUpload as e:
            errors.append((name, str(e)))
    assert len(parsed) == 1 and parsed[0].sender_display
    assert len(errors) == 1 and errors[0][0] == "photo.png"
