from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from nta.label import (
    choose_sample,
    html_to_lines,
    preview_lines,
    parse_event_type,
    run_session,
    sanitize,
)
from nta.schema import Direction, EventType, Instrument, Label, SourceEvent
from tests.fakes import FakeClock


def test_html_to_lines_drops_markup_scripts_and_hidden_xbrl() -> None:
    raw = """<html><head><title>T</title><script>alert('x')</script></head><body>
      <ix:header><div style="display:none">hidden facts 12345</div></ix:header>
      <p>UNITED STATES&nbsp;SECURITIES AND EXCHANGE COMMISSION</p>
      <div>Item 2.02   Results of
         Operations</div><table><tr><td>a</td><td>b</td></tr></table></body></html>"""
    assert html_to_lines(raw) == [
        "UNITED STATES SECURITIES AND EXCHANGE COMMISSION",
        "Item 2.02 Results of Operations",
        "a b",
    ]


def test_html_to_lines_limits_and_wraps() -> None:
    raw = "<html><body>" + "".join(f"<p>line {i}</p>" for i in range(200)) + "</body></html>"
    assert len(html_to_lines(raw)) == 60
    assert html_to_lines("x" * 250, width=100) == ["x" * 100, "x" * 100, "x" * 50]
    assert html_to_lines(None) == []


def test_terminal_escapes_are_stripped() -> None:
    evil = "ok\x1b[2J\x1b]0;pwned\x07 ‮gnp.exe\x00 done"
    assert sanitize(evil) == "ok[2J]0;pwned gnp.exe done"
    assert "\x1b" not in "".join(html_to_lines(f"<html><body><p>{evil}</p></body></html>"))


def test_sample_is_seeded_and_stable_under_growth() -> None:
    accs = [f"0000000000-26-{i:06d}" for i in range(500)]
    a = choose_sample(accs, seed=7, target=100)
    assert a == choose_sample(list(reversed(accs)), seed=7, target=100)
    assert a != choose_sample(accs, seed=8, target=100)
    # New filings may enter the sample, but existing ones keep their relative order.
    grown = choose_sample(accs + [f"0000000000-26-{i:06d}" for i in range(500, 600)], 7, 100)
    kept = [x for x in grown if x in set(a)]
    assert kept == [x for x in a if x in set(kept)]


def test_parse_event_type() -> None:
    assert parse_event_type("3") == EventType.m_and_a
    assert parse_event_type("guidance") == EventType.guidance
    assert parse_event_type("9") is None and parse_event_type("nope") is None


@pytest.fixture
def events(session: Session) -> list[SourceEvent]:
    inst = Instrument(ticker="AAPL", figi="BBG000B9XRY4", cik="0000320193", name="Apple",
                      added_on=date(2026, 1, 1))
    session.add(inst)
    session.flush()
    t = datetime(2026, 10, 1, 14, tzinfo=UTC)
    evs = [SourceEvent(source_id="edgar", external_id=f"0000000000-26-{i:06d}", form_type="8-K",
                       cik=inst.cik, instrument_id=inst.id, published_at=t + timedelta(hours=i),
                       first_observed_at=t + timedelta(hours=i), ingested_at=t, items=["2.02"],
                       url="https://www.sec.gov/x", raw=f"<html><body><p>doc {i}</p></body></html>")
           for i in range(5)]
    session.add_all(evs)
    session.commit()
    return evs


def scripted(answers: list[str]):
    it = iter(answers)

    def input_fn(prompt: str) -> str:
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None
    return input_fn


def labels(session: Session) -> list[Label]:
    session.expire_all()
    return list(session.scalars(select(Label).order_by(Label.id)))


def test_session_writes_labels_and_resumes(session_factory: sessionmaker[Session], session: Session, events):
    out: list[str] = []
    # 1st: material up guidance with note; 2nd: invalid then not material, other, no note; then quit.
    n = run_session(session_factory, FakeClock(), seed=7, target=3,
                    input_fn=scripted(["y", "u", "2", "beat and raise", "maybe", "n", "7", "", "q"]),
                    print_fn=out.append)
    assert n == 2
    first, second = labels(session)
    assert (first.material, first.direction, first.event_type, first.note, first.sample_seed) == \
        (True, Direction.up, EventType.guidance, "beat and raise", 7)
    assert (second.material, second.direction, second.event_type, second.note) == \
        (False, Direction.unclear, EventType.other, None)
    assert any("not understood" in line for line in out)

    # Resume: only the third sampled filing remains, in the same order.
    expected_third = choose_sample([e.external_id for e in events], 7, 3)[2]
    out.clear()
    run_session(session_factory, FakeClock(), seed=7, target=3,
                input_fn=scripted(["y", "d", "legal", "x"]), print_fn=out.append)
    third = labels(session)[2]
    assert session.get(SourceEvent, third.source_event_id).external_id == expected_third
    assert "2 already labeled, 1 to go" in out[0]


def test_skip_and_end_of_input(session_factory: sessionmaker[Session], session: Session, events):
    out: list[str] = []
    assert run_session(session_factory, FakeClock(), seed=1, target=5,
                       input_fn=scripted(["s", "s"]), print_fn=out.append) == 0
    assert labels(session) == []
    assert "doc " in "\n".join(out)  # the preview was shown


def test_preview_skips_cover_page() -> None:
    raw = ("<html><body><p>UNITED STATES</p><p>FORM 8-K</p><p>Check the appropriate box</p>"
           "<p>Item 2.02 Results of Operations and Financial Condition.</p><p>On Oct 30 we reported.</p>"
           "</body></html>")
    lines, skipped = preview_lines(raw)
    assert skipped == 3
    assert lines[0].startswith("Item 2.02") and lines[1] == "On Oct 30 we reported."
    assert preview_lines("<html><body><p>no items here</p></body></html>") == (["no items here"], 0)
