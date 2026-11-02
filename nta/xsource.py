"""X source registry (sources.yaml) and the per-post source metadata the agent needs.

Point-in-time quarantine: each stored post carries the author's handle and display name as
X returned them with the post. If either differs from the registry, the post's weight is 0
and it is flagged; re-approval is a human editing sources.yaml.
"""

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from nta.schema import EventType

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCES_PATH = ROOT / "sources.yaml"
_CASHTAG_RE = re.compile(r"(?<![\w$])\$([A-Za-z]{1,5}(?:[./][A-Za-z])?)\b")


class XSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
    author_id: str = Field(pattern=r"^[0-9]{1,20}$")
    handle: str
    display_name: str
    source_class: Literal["primary", "aggregator", "commentator"] = Field(alias="class")
    weight: Decimal = Field(ge=0, le=1)
    event_types: frozenset[EventType]

    @model_validator(mode="after")
    def _weight_cap(self) -> "XSource":
        if self.source_class != "primary" and self.weight > Decimal("0.5"):
            raise ValueError(f"weight of non-primary source {self.author_id} must be <= 0.5")
        return self


class Registry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sources: list[XSource] = []

    def by_id(self) -> dict[str, XSource]:
        return {s.author_id: s for s in self.sources}


def load_registry(path: Path = DEFAULT_SOURCES_PATH) -> Registry:
    data = yaml.safe_load(Path(path).read_text()) or {}
    reg = Registry.model_validate(data)
    ids = [s.author_id for s in reg.sources]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate author_id in sources.yaml")
    return reg


def post_fields(raw: str | None) -> dict:
    try:
        d = json.loads(raw or "{}")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def quarantined(src: XSource, handle: str | None, name: str | None) -> bool:
    return (handle or "").lower() != src.handle.lower() or (name or "") != src.display_name


def cashtags(text: str) -> set[str]:
    return {m.group(1).upper().replace("/", ".") for m in _CASHTAG_RE.finditer(text)}


def map_instrument(text: str, tickers: dict[str, str]) -> str | None:
    """Exactly one allowlisted cashtag -> that instrument id; none or several -> None."""
    hits = {tickers[t] for t in cashtags(text) if t in tickers}
    return hits.pop() if len(hits) == 1 else None
