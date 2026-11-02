import hashlib
from pathlib import Path

from nta.edgar import (
    ACCEPTED_FORMS,
    cap_raw,
    classify,
    group_filings,
    parse_feed,
    parse_index,
)

FIX = Path(__file__).parent / "fixtures" / "edgar"


def test_parse_real_feed_sample() -> None:
    entries, bad = parse_feed((FIX / "feed_real_sample.atom").read_bytes())
    assert bad == 0
    assert len(entries) == 7
    qvc = next(e for e in entries if e.cik == "0001254699")
    assert qvc.accession == "0001254699-26-000028"
    assert qvc.form_type == "8-K"
    assert qvc.items == ("7.01", "9.01")
    assert qvc.published_at.isoformat() == "2026-10-30T17:29:06-04:00"
    assert qvc.index_url.startswith("https://www.sec.gov/Archives/edgar/data/1254699/")
    assert {e.form_type for e in entries} == {"8-K", "8-K/A"}


def test_group_co_registrants() -> None:
    entries, _ = parse_feed((FIX / "feed_real_sample.atom").read_bytes())
    filings = group_filings(entries)
    assert len(filings) == 6  # Digital Realty Inc. and L.P. share one accession
    dlr = next(f for f in filings if f.accession == "0001193125-26-441892")
    assert list(dlr.index_url_by_cik) == ["0001494877", "0001297996"]


def test_classify() -> None:
    entries, _ = parse_feed((FIX / "feed_real_sample.atom").read_bytes())
    filings = group_filings(entries)
    allow = {"0001297996": ("inst-dlr", "DLR")}  # the second co-registrant
    c = classify(filings, seen={"0001254699-26-000028"}, allowlisted=allow)
    assert [(n.filing.accession, n.cik, n.ticker) for n in c.new_allowlisted] == [
        ("0001193125-26-441892", "0001297996", "DLR")]
    assert [f.accession for f in c.duplicates] == ["0001254699-26-000028"]
    assert len(c.new_other) == 4
    assert c.other_form == []
    assert "8-K/A" in ACCEPTED_FORMS


def test_malformed_entries_are_counted_not_fatal() -> None:
    xml = (b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
           b"<entry><title>garbage</title></entry></feed>")
    entries, bad = parse_feed(xml)
    assert entries == [] and bad == 1


def test_parse_real_index_sample() -> None:
    info = parse_index((FIX / "index_real_sample.htm").read_text(), "8-K")
    assert info.primary_doc_url == (
        "https://www.sec.gov/Archives/edgar/data/1254699/000125469926000028/qvc-20261030.htm")
    assert info.items == ("7.01", "9.01")


def test_parse_index_without_form_match_falls_back() -> None:
    from tests.edgar_fakes import index_html
    info = parse_index(index_html("/Archives/a/b.htm", form="8-K/A", ixbrl=False), "8-K")
    assert info.primary_doc_url == "https://www.sec.gov/Archives/a/b.htm"


def test_parse_index_garbage() -> None:
    assert parse_index("<html>nothing here", "8-K").primary_doc_url is None


def test_cap_raw() -> None:
    text, truncated, digest = cap_raw("héllo\x00 world", max_bytes=1000)
    assert (text, truncated) == ("héllo world", False)
    assert digest == hashlib.sha256("héllo world".encode()).hexdigest()
    # 'é' is 2 bytes; cutting at 2 bytes must not split it.
    text, truncated, digest = cap_raw("hé" * 10, max_bytes=2)
    assert (text, truncated) == ("h", True)
    assert digest == hashlib.sha256(b"h").hexdigest()
