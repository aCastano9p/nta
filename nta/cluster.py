"""Clustering of signals into news events.

Cluster key = (event_type, instrument_id, MinHash of the normalized headline). A signal
joins an earlier cluster when, within the repeat window (72 h by default), it either
cites that cluster's origin (an accession number or SEC URL) or has the same event type
and instrument and a MinHash similarity at or above the threshold. Joining marks it a
repeat. Corroboration counts distinct origins in the cluster; a cluster containing a
primary source (every EDGAR filing) is corroborated by definition.

Members are identified by their source_event id, and cluster_id is the source_event id of
the cluster's first member: source events are inputs, so ids stay the same in replay.

Everything here is pure except load_candidates, which reads prior signals.
"""

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from nta.schema import Signal, SourceEvent

NUM_PERM = 64
SHINGLE = 4
_URL_RE = re.compile(r"https?://\S+")
_NON_WORD_RE = re.compile(r"[^a-z0-9.]+")
_ACCESSION_RE = re.compile(r"\b(\d{10})-(\d{2})-(\d{6})\b")
# EDGAR archive URLs carry the accession without dashes: /Archives/edgar/data/<cik>/<18 digits>/
_ARCHIVE_RE = re.compile(r"sec\.gov/Archives/edgar/data/\d+/(\d{10})(\d{2})(\d{6})", re.I)


def normalize_headline(text: str) -> str:
    t = _URL_RE.sub(" ", text.lower())
    t = _NON_WORD_RE.sub(" ", t)
    return " ".join(w.strip(".") for w in t.split() if w.strip("."))


def shingles(text: str, k: int = SHINGLE) -> set[str]:
    if len(text) <= k:
        return {text} if text else set()
    return {text[i:i + k] for i in range(len(text) - k + 1)}


def _h(seed: int, value: str) -> int:
    return int.from_bytes(hashlib.blake2b(f"{seed}:{value}".encode(), digest_size=8).digest(), "big")


def minhash(normalized: str, num_perm: int = NUM_PERM) -> list[int]:
    """Deterministic MinHash signature (blake2b with a per-permutation seed)."""
    sh = shingles(normalized)
    if not sh:
        return [0] * num_perm
    return [min(_h(i, s) for s in sh) for i in range(num_perm)]


def similarity(a: list[int], b: list[int]) -> float:
    if not a or len(a) != len(b):
        return 0.0
    return sum(x == y for x, y in zip(a, b)) / len(a)


def cited_accessions(text: str) -> set[str]:
    """Accession numbers cited in free text, as plain numbers or inside SEC archive URLs."""
    found = {"-".join(m.groups()) for m in _ACCESSION_RE.finditer(text)}
    found |= {"-".join(m.groups()) for m in _ARCHIVE_RE.finditer(text)}
    return found


_INJECTION_RES = [re.compile(p, re.I) for p in (
    r"\b(ignore|disregard|forget)\b.{0,30}\b(previous|prior|above|earlier|all)\b.{0,20}\b(instructions?|prompts?|rules)\b",
    r"\byou are now\b",
    r"\b(system|developer) (prompt|message|instructions?)\b",
    r"\bnew instructions?\b",
    r"\bas an ai\b",
    r"</?\s*untrusted_item",
)]


def injection_suspected(text: str) -> bool:
    """Instruction-like text addressed to a model. Deterministic, so live and replay agree;
    a flagged signal never trades, whatever the model answered."""
    return any(r.search(text) for r in _INJECTION_RES)


def edgar_headline(company: str, form_type: str, items: Iterable[str], titles: dict[str, str]) -> str:
    items = list(items)
    parts = [company, form_type, "items"] + [f"{i} {titles.get(i, '')}".strip() for i in items]
    return " ".join(parts)


@dataclass(frozen=True)
class Member:
    """A prior signal, as clustering sees it."""
    event_id: str           # source_events.id
    cluster_id: str
    event_type: str
    instrument_id: str | None
    minhash: tuple[int, ...]
    origin: str
    source_class: str
    weight: Decimal
    accession: str          # the member's source_events.external_id
    ready_at: datetime


@dataclass(frozen=True)
class Candidate:
    event_id: str           # source_events.id
    event_type: str
    instrument_id: str | None
    minhash: tuple[int, ...]
    origin: str
    source_class: str
    weight: Decimal
    cites: frozenset[str]   # accession numbers this item cites
    ready_at: datetime


@dataclass(frozen=True)
class Assignment:
    cluster_id: str
    is_repeat: bool
    corroboration: int
    corroborated: bool


def assign(c: Candidate, prior: Iterable[Member], *, window: timedelta, threshold: float,
           min_origins: int, min_weight: Decimal) -> Assignment:
    recent = sorted((m for m in prior if c.ready_at - window <= m.ready_at <= c.ready_at),
                    key=lambda m: (m.ready_at, m.event_id))
    cluster_id = None
    for m in recent:  # citing an origin wins over text similarity
        if m.accession in c.cites:
            cluster_id = m.cluster_id
            break
    if cluster_id is None:
        for m in recent:
            if (m.event_type == c.event_type and m.instrument_id == c.instrument_id
                    and m.instrument_id is not None
                    and similarity(list(m.minhash), list(c.minhash)) >= threshold):
                cluster_id = m.cluster_id
                break
    is_repeat = cluster_id is not None
    cluster_id = cluster_id or c.event_id
    members = [m for m in recent if m.cluster_id == cluster_id]
    weights: dict[str, Decimal] = {c.origin: c.weight}
    classes = {c.source_class}
    for m in members:
        weights[m.origin] = max(weights.get(m.origin, Decimal(0)), m.weight)
        classes.add(m.source_class)
    corroborated = "primary" in classes or (
        len(weights) >= min_origins and sum(weights.values()) >= min_weight)
    return Assignment(cluster_id, is_repeat, len(weights), corroborated)


def load_members(s: Session, instrument_id: str | None, cites: Iterable[str], since: datetime,
                 until: datetime) -> list[Member]:
    """Prior signals that could share a cluster: same instrument, or the cited filings."""
    conds = []
    if instrument_id is not None:
        conds.append(Signal.instrument_id == instrument_id)
    cites = list(cites)
    if cites:
        conds.append(SourceEvent.external_id.in_(cites))
    if not conds:
        return []
    rows = s.execute(
        select(Signal.source_event_id, Signal.cluster_id, Signal.event_type, Signal.instrument_id, Signal.minhash,
               Signal.origin, Signal.source_class, Signal.source_weight, SourceEvent.external_id,
               Signal.ready_at)
        .join(SourceEvent, SourceEvent.id == Signal.source_event_id)
        .where(or_(*conds), Signal.ready_at >= since, Signal.ready_at <= until)
        .order_by(Signal.ready_at, Signal.id)).all()
    return [Member(r[0], r[1], r[2], r[3], tuple(r[4]), r[5], r[6], r[7], r[8], r[9]) for r in rows]
