"""Minimal row builders for tests. One valid row per table."""

from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from nta.schema import (
    Direction,
    EventType,
    Instrument,
    Label,
    OpenFigiResponse,
    Quote,
    QuoteKind,
    QuoteSchedule,
    Run,
    RunStop,
    SourceEvent,
)

T0 = datetime(2026, 10, 1, 14, 0, tzinfo=UTC)


def one_of_each(s: Session) -> dict[str, object]:
    inst = Instrument(ticker="AAPL", figi="BBG000B9XRY4", cik="0000320193", name="Apple Inc.",
                      aliases=["Apple"], added_on=date(2026, 10, 1))
    run = Run(process="poll_edgar", code_sha="abc", config_hash="def", started_at=T0)
    s.add_all([inst, run])
    s.flush()
    ev = SourceEvent(source_id="edgar", external_id="0000320193-26-000001", form_type="8-K",
                     cik="0000320193", instrument_id=inst.id, published_at=T0,
                     first_observed_at=T0, ingested_at=T0, url="https://www.sec.gov/x",
                     items=["2.02"], raw="hello", raw_hash="0" * 64, run_id=run.id)
    figi = OpenFigiResponse(request_key='{"idType":"TICKER"}', response={"data": []},
                            first_observed_at=T0)
    stop = RunStop(run_id=run.id, stopped_at=T0, reason="test")
    s.add_all([ev, figi, stop])
    s.flush()
    sched = QuoteSchedule(source_event_id=ev.id, instrument_id=inst.id, kind=QuoteKind.mark_1h,
                          due_at=T0)
    quote = Quote(instrument_id=inst.id, source_event_id=ev.id, ts=T0, first_observed_at=T0,
                  bid=1, ask=2, last=1.5, source="alpaca_iex", kind=QuoteKind.observe)
    label = Label(source_event_id=ev.id, material=True, direction=Direction.up,
                  event_type=EventType.guidance, labeled_at=T0)
    s.add_all([sched, quote, label])
    s.flush()
    return {
        "instruments": inst, "runs": run, "source_events": ev, "openfigi_responses": figi,
        "run_stops": stop, "quote_schedule": sched, "quotes": quote, "labels": label,
    }
