from datetime import UTC, datetime, timedelta
from decimal import Decimal

from nta.cluster import (
    Candidate,
    Member,
    assign,
    cited_accessions,
    edgar_headline,
    load_members,
    minhash,
    normalize_headline,
    similarity,
)
from nta.label import ITEM_TITLES
from tests.factories import one_of_each

T = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
W = timedelta(hours=72)
KW = dict(window=W, threshold=0.5, min_origins=2, min_weight=Decimal(1))


def mh(text: str) -> tuple[int, ...]:
    return tuple(minhash(normalize_headline(text)))


def member(sid: str, text: str, *, origin="edgar", cls="primary", weight="1", acc="0000000001-26-000001",
           at=T - timedelta(hours=1), event="guidance", inst="I1", cluster=None) -> Member:
    return Member(sid, cluster or sid, event, inst, mh(text), origin, cls, Decimal(weight), acc, at)


def cand(text: str, *, origin="x:42", cls="commentator", weight="0.3", cites=(), event="guidance",
         inst="I1", sid="C") -> Candidate:
    return Candidate(sid, event, inst, mh(text), origin, cls, Decimal(weight), frozenset(cites), T)


def test_normalize_and_minhash_are_deterministic() -> None:
    assert normalize_headline("Apple: Q3 RESULTS!  https://x.com/a  beat.") == "apple q3 results beat"
    assert minhash("apple q3 results") == minhash("apple q3 results")
    assert similarity(list(mh("Apple raises guidance for Q4")), list(mh("apple raises guidance for q4!"))) == 1.0
    assert similarity(list(mh("Apple raises guidance for Q4")), list(mh("Exxon names new CFO"))) < 0.2


def test_cited_accessions_from_text_and_urls() -> None:
    text = ("see 0000320193-26-000011 and "
            "https://www.sec.gov/Archives/edgar/data/320193/000032019326000012/x.htm")
    assert cited_accessions(text) == {"0000320193-26-000011", "0000320193-26-000012"}


def test_edgar_amendment_is_a_repeat_of_the_original() -> None:
    first = member("A", edgar_headline("Apple Inc.", "8-K", ["5.02"], ITEM_TITLES))
    amend = cand(edgar_headline("Apple Inc.", "8-K/A", ["5.02"], ITEM_TITLES),
                 origin="edgar", cls="primary", weight="1")
    a = assign(amend, [first], **KW)
    assert a.cluster_id == "A" and a.is_repeat and a.corroborated


def test_first_filing_starts_its_own_corroborated_cluster() -> None:
    a = assign(cand("Apple 8-K items 2.02", origin="edgar", cls="primary", weight="1"), [], **KW)
    assert a.cluster_id == "C" and not a.is_repeat and a.corroboration == 1 and a.corroborated


def test_lone_commentator_post_is_uncorroborated() -> None:
    a = assign(cand("Apple beats on revenue"), [], **KW)
    assert not a.corroborated and a.corroboration == 1


def test_post_citing_an_accession_joins_that_filing_cluster() -> None:
    filing = member("A", "apple 8-k items 2.02 results", acc="0000320193-26-000011")
    post = cand("totally different words here", cites={"0000320193-26-000011"}, event="other", inst=None)
    a = assign(post, [filing], **KW)
    assert a.cluster_id == "A" and a.is_repeat and a.corroboration == 2 and a.corroborated


def test_two_weighted_non_primary_origins_corroborate() -> None:
    p1 = member("P1", "Apple raises full year guidance", origin="x:1", cls="aggregator", weight="0.5")
    lone = assign(cand("Apple raises full-year guidance", weight="0.4"), [p1], **KW)
    assert lone.is_repeat and lone.corroboration == 2 and not lone.corroborated  # 0.9 < 1.0
    ok = assign(cand("Apple raises full-year guidance", weight="0.5"), [p1], **KW)
    assert ok.corroborated
    same_origin = assign(cand("Apple raises full-year guidance", origin="x:1", weight="0.5"), [p1], **KW)
    assert same_origin.corroboration == 1 and not same_origin.corroborated


def test_window_event_type_and_instrument_separate_clusters() -> None:
    text = "Apple raises full year guidance"
    old = member("OLD", text, at=T - W - timedelta(seconds=1))
    assert not assign(cand(text), [old], **KW).is_repeat
    assert not assign(cand(text, event="legal"), [member("A", text)], **KW).is_repeat
    assert not assign(cand(text, inst="I2"), [member("A", text)], **KW).is_repeat


def test_load_members_finds_same_instrument_and_cited_filings(session) -> None:
    rows = one_of_each(session)
    ev, inst, sig = rows["source_events"], rows["instruments"], rows["signals"]
    by_instrument = load_members(session, inst.id, [], sig.ready_at - W, sig.ready_at)
    assert [m.signal_id for m in by_instrument] == [sig.id]
    assert by_instrument[0].accession == ev.external_id and by_instrument[0].minhash == (1, 2)
    by_citation = load_members(session, None, [ev.external_id], sig.ready_at - W, sig.ready_at)
    assert [m.signal_id for m in by_citation] == [sig.id]
    assert load_members(session, "OTHER", [], sig.ready_at - W, sig.ready_at) == []
    assert load_members(session, None, [], sig.ready_at - W, sig.ready_at) == []
    assert load_members(session, inst.id, [], sig.ready_at + timedelta(seconds=1), sig.ready_at + W) == []
