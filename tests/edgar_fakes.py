"""A fake sec.gov for poller tests: Atom feed pages, filing index pages, documents."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape
from urllib.parse import parse_qs, urlparse

import httpx

from tests.fakes import FakeClock


@dataclass
class E:
    accession: str
    cik: str
    updated: datetime
    form: str = "8-K"
    company: str = "Some Co"
    items: tuple[str, ...] = ("8.01", "9.01")


def atom(entries: list[E]) -> bytes:
    parts = ['<?xml version="1.0" encoding="ISO-8859-1" ?>\n',
             '<feed xmlns="http://www.w3.org/2005/Atom">\n<title>Latest Filings</title>\n',
             '<updated>2026-10-01T10:00:00-04:00</updated>\n']
    for e in entries:
        cik_int = int(e.cik)
        acc_nodash = e.accession.replace("-", "")
        summary = (f" &lt;b&gt;Filed:&lt;/b&gt; {e.updated.date()} &lt;b&gt;AccNo:&lt;/b&gt; "
                   f"{e.accession} &lt;b&gt;Size:&lt;/b&gt; 100 KB\n"
                   + "".join(f"&lt;br&gt;Item {i}: Something\n" for i in e.items))
        parts.append(
            "<entry>\n"
            f"<title>{escape(e.form)} - {escape(e.company)} ({e.cik}) (Filer)</title>\n"
            f'<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/'
            f'{cik_int}/{acc_nodash}/{e.accession}-index.htm"/>\n'
            f'<summary type="html">\n{summary}</summary>\n'
            f"<updated>{e.updated.isoformat()}</updated>\n"
            f'<category scheme="https://www.sec.gov/" label="form type" term="{escape(e.form)}"/>\n'
            f"<id>urn:tag:sec.gov,2008:accession-number={e.accession}</id>\n"
            "</entry>\n")
    parts.append("</feed>\n")
    return "".join(parts).encode("iso-8859-1")


def index_html(doc_path: str, form: str = "8-K", items: tuple[str, ...] = ("8.01", "9.01"),
               ixbrl: bool = True) -> str:
    href = f"/ix?doc={doc_path}" if ixbrl else doc_path
    items_html = "".join(f"Item {i}: Something<br />" for i in items)
    return f"""<html><body>
<div class="formGrouping"><div class="infoHead">Filing Date</div><div class="info">2026-10-01</div>
<div class="infoHead">Items</div><div class="info">{items_html}</div></div>
<table class="tableFile" summary="Document Format Files">
<tr><th scope="col">Seq</th><th scope="col">Description</th><th scope="col">Document</th><th scope="col">Type</th><th scope="col">Size</th></tr>
<tr><td scope="row">1</td><td scope="row">{form}</td><td scope="row"><a href="{href}">doc.htm</a> iXBRL</td><td scope="row">{form}</td><td scope="row">100</td></tr>
<tr class="evenRow"><td scope="row">2</td><td scope="row">EX-99.1</td><td scope="row"><a href="/Archives/x/ex99.htm">ex99.htm</a></td><td scope="row">EX-99.1</td><td scope="row">50</td></tr>
</table></body></html>"""


def doc_html(accession: str) -> str:
    return f"<html><body><p>FORM 8-K</p><p>Accession {accession}</p><script>alert(1)</script></body></html>"


@dataclass
class FakeSec:
    clock: FakeClock
    pages: dict[int, bytes] = field(default_factory=dict)  # start offset -> feed bytes
    fail: dict[str, list[int]] = field(default_factory=dict)  # url substring -> statuses to return first
    requests: list[tuple[str, float]] = field(default_factory=list)

    def set_feed(self, entries: list[E], start: int = 0) -> None:
        self.pages[start] = atom(entries)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        assert request.headers.get("User-Agent") == "Test Runner test@example.com", "missing SEC UA"
        assert request.url.host == "www.sec.gov"
        self.requests.append((url, self.clock.monotonic()))
        for key, statuses in self.fail.items():
            if key in url and statuses:
                return httpx.Response(statuses.pop(0))
        path = request.url.path
        if path == "/cgi-bin/browse-edgar":
            start = int(parse_qs(urlparse(url).query).get("start", ["0"])[0])
            return httpx.Response(200, content=self.pages.get(start, atom([])),
                                  headers={"Content-Type": "application/atom+xml"})
        if path.endswith("-index.htm"):
            accession = path.rsplit("/", 1)[1].removesuffix("-index.htm")
            folder = path.rsplit("/", 1)[0]
            return httpx.Response(200, text=index_html(f"{folder}/{accession}-doc.htm"))
        if path.endswith("-doc.htm"):
            accession = path.rsplit("/", 1)[1].removesuffix("-doc.htm")
            return httpx.Response(200, text=doc_html(accession))
        return httpx.Response(404)

    def count(self, substring: str) -> int:
        return sum(1 for u, _ in self.requests if substring in u)


def acc(n: int) -> str:
    return f"0000000000-26-{n:06d}"


def minutes_before(t: datetime, m: float) -> datetime:
    return t - timedelta(minutes=m)
