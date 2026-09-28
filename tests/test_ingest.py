"""Unit tests for the deterministic ingestion spine (parser + dedup)."""
import textwrap
from datetime import timedelta

from app.ingest.dedup import find_duplicates
from app.ingest.markdown import _norm_subject, parse_batch_file, parse_timestamp
from app.schema import SourceType


def _write(tmp_path, text):
    p = tmp_path / "batchX_redacted.md"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return p


def test_parses_email_with_multiline_fromto_and_fenced_body(tmp_path):
    p = _write(tmp_path, """
        # BATCH X
        --- ITEM 1 ---
        source_type:   email
        channel:       gmail
        timestamp:     Wed, 10 Dec 2025 05:16:50 +0000
        from / to:     from: Matteo Rinaldi <matteo.rinaldi@kuni.eu>
                       to:   Rohan Menezes <rohan0707.menezes@gmail.com>
        subject:       [RESEND] Link to rebuttal form
        body_format:   plain
        body:
        ```
        Dear Rohan,
        The code is 715903264.
        ```
        notes:         Resend; kept as its own item.
    """)
    items = parse_batch_file(p)
    assert len(items) == 1
    it = items[0]
    assert it.source_type == SourceType.EMAIL
    assert it.sender == "Matteo Rinaldi <matteo.rinaldi@kuni.eu>"
    assert "rohan0707.menezes@gmail.com" in it.recipients
    assert it.subject == "[RESEND] Link to rebuttal form"
    assert "code is 715903264" in it.body
    assert it.notes.startswith("Resend")  # captured but eval-only
    assert it.timestamp is not None and it.timestamp.year == 2025


def test_whatsapp_section_becomes_conversation_id(tmp_path):
    p = _write(tmp_path, """
        # BATCH X
        ## Chat: `WhatsApp Chat - IT Aravali PhD ICVSP Srinivasa` (459 messages)
        --- ITEM 1 ---
        source_type:   whatsapp
        channel:       whatsapp:dm: WhatsApp Chat - IT Aravali PhD ICVSP Srinivasa
        timestamp:     [18/03/26, 12:04:52 PM]
        from / to:     from: Srinivasa
                       to:   R0h@n
        body:
        ```
        Hi Rohin, congratulations on your ICVSP paper acceptance.
        ```
        notes:         —
    """)
    it = parse_batch_file(p)[0]
    assert it.source_type == SourceType.WHATSAPP
    assert "IT Aravali PhD ICVSP Srinivasa" in it.conversation_id
    assert it.timestamp is not None and it.timestamp.month == 3


def test_email_thread_grouping_strips_reply_prefixes():
    assert _norm_subject("Re: Fwd: Registration confirmed") == "registration confirmed"
    assert _norm_subject("[RESEND] Link to form") == "link to form"


def test_timestamp_parsing_per_source():
    assert parse_timestamp(SourceType.EMAIL, "Fri, 22 May 2026 03:03:36 -0400").year == 2026
    assert parse_timestamp(SourceType.WHATSAPP, "[18/03/26, 9:31:24 PM]").hour == 21
    assert parse_timestamp(SourceType.PDF, "n/a (undated certificate)") is None


def test_timestamp_resolves_named_timezone_abbreviation():
    # dateutil can't resolve bare "EET" without a tzinfos map; we supply one so the
    # result is timezone-aware (+2h) instead of silently dropping the zone.
    ts = parse_timestamp(SourceType.PDF, "2 Mar 2024 14:22 EET")
    assert ts is not None and ts.utcoffset() == timedelta(hours=2)
    ts_eest = parse_timestamp(SourceType.PDF, "2 Jul 2024 14:22 EEST")
    assert ts_eest is not None and ts_eest.utcoffset() == timedelta(hours=3)


def test_exact_and_near_duplicates(tmp_path):
    p = _write(tmp_path, """
        # BATCH X
        --- ITEM 1 ---
        source_type:   email
        channel:       gmail
        timestamp:     Wed, 10 Dec 2025 05:16:50 +0000
        from / to:     from: A
        subject:       Registration confirmed for the conference
        body:
        ```
        Your registration for the conference is confirmed. Reference 7HT2K91WPQD. See you there.
        ```
        notes:         —
        --- ITEM 2 ---
        source_type:   email
        channel:       gmail
        timestamp:     Wed, 10 Dec 2025 06:00:00 +0000
        from / to:     from: A
        subject:       Registration confirmed for the conference
        body:
        ```
        Your registration for the conference is confirmed. Reference 7HT2K91WPQD. See you there.
        ```
        notes:         identical copy from a second export
        --- ITEM 3 ---
        source_type:   email
        channel:       gmail
        timestamp:     Wed, 10 Dec 2025 07:00:00 +0000
        from / to:     from: A
        subject:       Fwd: Registration confirmed for the conference
        body:
        ```
        Forwarding for your records. Your registration for the conference is confirmed. Reference 7HT2K91WPQD. Regards.
        ```
        notes:         forward with extra text
    """)
    items = parse_batch_file(p)
    res = find_duplicates(items, near_threshold=0.5)
    # ITEM 1 and 2 are byte-identical bodies -> exact
    assert any(len(g) == 2 for g in res.exact_groups)
    # ITEM 3 quotes the same text with extra words -> near-dup with the others
    assert res.n_near_dupes >= 1
