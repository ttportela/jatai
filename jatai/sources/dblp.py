"""Conector DBLP via API JSON oficial, usando Chromium headless.

O dblp.org está protegido por um desafio anti-bot (Anubis) que exige JS.
Usamos Playwright com o Chrome real do sistema (channel="chrome") para
resolver o desafio e obter as respostas JSON da API de busca.
"""

import json
import random
import threading
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

from jatai.sources.base import ProgressCallback, RawRecord, SourceConnector

API_URL = "https://dblp.org/search/publ/api"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
MAX_OFFSET = 10000

_RATE_LOCK = threading.Lock()
_GLOBAL_LAST_CALL = 0.0


def _throttle(min_interval: float) -> None:
    global _GLOBAL_LAST_CALL
    with _RATE_LOCK:
        agora = time.monotonic()
        espera = min_interval - (agora - _GLOBAL_LAST_CALL)
        if espera > 0:
            time.sleep(espera)
        _GLOBAL_LAST_CALL = time.monotonic()


def _mark_call_finished() -> None:
    global _GLOBAL_LAST_CALL
    with _RATE_LOCK:
        _GLOBAL_LAST_CALL = time.monotonic()


def _first_str(value) -> str:
    """Normaliza campos do DBLP que podem vir como str, list ou dict."""
    if isinstance(value, list):
        for item in value:
            s = _first_str(item)
            if s:
                return s
        return ""
    if isinstance(value, dict):
        return str(value.get("text") or value.get("#text") or "").strip()
    return str(value or "").strip()


def _authors(info: dict) -> list[str]:
    authors = info.get("authors", {}).get("author", [])
    if isinstance(authors, (dict, str)):
        authors = [authors]
    out: list[str] = []
    for a in authors:
        name = _first_str(a)
        if name:
            out.append(name)
    return out


def _text(el) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ],
        1,
    )
}


def _month_num(value) -> int | None:
    """Converte '6', '06' ou 'June' em 1-12; None se não identificar."""
    s = _first_str(value).lower()
    if not s:
        return None
    if s.isdigit():
        n = int(s)
        return n if 1 <= n <= 12 else None
    return _MONTHS.get(s[:9])


def _parse_record_xml(record: str) -> dict | None:
    """Extrai metadados do campo <record> XML retornado pela API do DBLP."""
    try:
        root = ET.fromstring(record)
    except ET.ParseError:
        return None
    authors = [_text(a) for a in root.findall(".//author")]
    venue_el = root.find("journal") or root.find("booktitle")
    return {
        "title": _text(root.find("title")),
        "authors": [a for a in authors if a],
        "year": _text(root.find("year")),
        "month": _text(root.find("month")),
        "doi": _text(root.find("doi")),
        "abstract": "",
        "ee": _text(root.find("ee")),
        "url": _text(root.find("url")),
        "venue": _text(venue_el),
        "pub_type": root.tag,
        "key": root.attrib.get("key", ""),
        "keywords": [_text(k) for k in root.findall(".//keyword")],
    }


def _keywords(info: dict) -> list[str]:
    kw = info.get("keywords")
    if not isinstance(kw, dict):
        return []
    items = kw.get("keyword", [])
    if isinstance(items, str):
        items = [items]
    return [s for item in items if (s := _first_str(item))]


class _RateLimitedError(RuntimeError):
    def __init__(self, retry_after: float | None = None):
        super().__init__("DBLP: HTTP 429 Too Many Requests")
        self.retry_after = retry_after


class _Browser:
    """Encapsula um contexto/página Playwright reutilizável."""

    def __init__(self, timeout_ms: int = 60000):
        from jatai import browser as _browser

        self._pw = _browser.get_playwright()
        self._browser = _browser.launch_browser()
        self._context = self._browser.new_context(user_agent=USER_AGENT)
        self._page = self._context.new_page()
        self._timeout_ms = timeout_ms

    def fetch_json(self, url: str) -> dict:
        """Navega até a URL uma vez e aguarda o desafio JS (Anubis) concluir."""
        response = self._page.goto(url, timeout=30000, wait_until="domcontentloaded")
        if response is not None and response.status == 429:
            ra = response.headers.get("retry-after")
            retry_after = float(ra) if ra and ra.isdigit() else None
            raise _RateLimitedError(retry_after=retry_after)

        deadline = time.time() + self._timeout_ms / 1000
        while time.time() < deadline:
            try:
                title = (self._page.title() or "").lower()
                # Se ainda está na tela de desafio Anubis, apenas aguarda o redirect JS
                if "bot" in title or "anubis" in title or "making sure" in title:
                    self._page.wait_for_timeout(500)
                    continue

                pre = self._page.locator("body pre").first
                if pre.count() > 0:
                    raw_text = (pre.text_content() or "").strip()
                    if raw_text.startswith("{"):
                        return json.loads(raw_text)

                body_text = (self._page.locator("body").inner_text() or "").strip()
                if body_text.startswith("{"):
                    return json.loads(body_text)
            except Exception:
                pass

            self._page.wait_for_timeout(500)

        raise TimeoutError("DBLP: desafio anti-bot não resolvido no tempo limite")

    def close(self) -> None:
        try:
            self._page.close()
        except Exception:
            pass
        try:
            self._context.close()
        except Exception:
            pass


class DBLPConnector(SourceConnector):
    source_type = "dblp"
    name = "DBLP"

    def __init__(self, max_results: int = 10000, min_interval: float = 1.0):
        self.max_results = max_results
        self.min_interval = min_interval
        self._browser: _Browser | None = None
        self.last_request: dict = {}
        self.last_payloads: list[dict] = []

    def _get_browser(self) -> _Browser:
        if self._browser is None:
            self._browser = _Browser()
        return self._browser

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None

    def is_available(self) -> bool:
        return True

    @staticmethod
    def _record_key(rec: RawRecord) -> str:
        return (
            rec.external_id
            or rec.doi
            or f"{rec.title}|{rec.year or ''}"
        ).lower()

    def _fetch_with_retry(self, browser: _Browser, url: str, on_retry=None) -> dict:
        for tentativa in range(1, 5):
            _throttle(self.min_interval)
            try:
                return browser.fetch_json(url)
            except _RateLimitedError as exc:
                espera = (
                    exc.retry_after + random.uniform(0.3, 1.0)
                    if exc.retry_after is not None
                    else (2 ** tentativa) + random.uniform(0.5, 1.5)
                )
                self.last_request["retry_429"] = tentativa
                if on_retry:
                    on_retry(tentativa, espera)
                time.sleep(espera)
            finally:
                _mark_call_finished()
        raise RuntimeError("DBLP: limite de requisições (429) após 4 tentativas")

    def search(
        self,
        expression: str,
        max_results: int = 200,
        on_progress: ProgressCallback | None = None,
        on_retry=None,
        on_page=None,
        should_stop=None,
    ) -> list[RawRecord]:
        limit = min(max_results, self.max_results)
        browser = self._get_browser()
        from jatai.search.adapters.dblp import compile_expression

        clauses = compile_expression(expression)
        records: list[RawRecord] = []
        self.last_payloads = []
        seen: set[str] = set()
        pages = 0
        reported_total = 0
        hits_per_page = min(limit, 1000)
        page_records: list[RawRecord] = []

        for clause in clauses:
            if should_stop and should_stop():
                break
            offset = 0
            clause_scanned = 0
            clause_total: int | None = None
            while len(records) < limit and offset < MAX_OFFSET:
                if should_stop and should_stop():
                    if records:
                        self.last_request["partial"] = True
                        self.last_request["warning"] = (
                            f"Busca interrompida na página {pages + 1}"
                        )
                    break
                params = {
                    "q": clause,
                    "format": "json",
                    "h": hits_per_page,
                    "f": offset,
                }
                url = f"{API_URL}?{urlencode(params)}"
                self.last_request = {"method": "GET", "url": url, "params": params}
                try:
                    data = self._fetch_with_retry(browser, url, on_retry=on_retry)
                except Exception as exc:
                    if records:
                        self.last_request["partial"] = True
                        self.last_request["warning"] = (
                            f"Paginação interrompida na cláusula '{clause[:60]}' "
                            f"(página {pages + 1}): {exc}"
                        )
                        break
                    raise
                self.last_payloads.append(data)
                pages += 1

                hits = data.get("result", {}).get("hits", {})
                items = hits.get("hit") or []
                if isinstance(items, dict):
                    items = [items]

                total = int(hits.get("@total", 0) or 0)
                if clause_total is None:
                    clause_total = total
                    reported_total += total
                if not items:
                    break

                clause_scanned += len(items)
                page_records = []
                for item in items:
                    rec = self._parse_hit(item)
                    if rec is None:
                        continue
                    key = self._record_key(rec)
                    if key in seen:
                        continue
                    seen.add(key)
                    records.append(rec)
                    page_records.append(rec)
                    if len(records) >= limit:
                        break

                if on_page and page_records:
                    on_page(pages, page_records)

                if on_progress:
                    on_progress(pages, len(records), reported_total or None)

                if clause_total and clause_scanned >= clause_total:
                    break
                offset += len(items)

            if len(records) >= limit:
                break

        self.last_request["pages"] = pages
        self.last_request["clauses"] = clauses
        self.last_request["total_reported"] = reported_total
        return records

    @staticmethod
    def _parse_hit(item: dict) -> RawRecord | None:
        info = item.get("info", {})
        parsed = _parse_record_xml(info["record"]) if info.get("record") else None
        if parsed:
            year = int(parsed["year"]) if parsed["year"].isdigit() else None
            month = _month_num(parsed.get("month"))
            return RawRecord(
                title=parsed["title"],
                authors=parsed["authors"],
                year=year,
                month=month,
                doi=parsed["doi"],
                abstract=parsed["abstract"],
                url=parsed["ee"] or parsed["url"],
                venue=parsed["venue"],
                pub_type=parsed["pub_type"],
                external_id=parsed["key"],
                keywords=[k for k in parsed["keywords"] if k],
                raw=info,
            )

        year_str = _first_str(info.get("year"))
        return RawRecord(
            title=_first_str(info.get("title")),
            authors=_authors(info),
            year=int(year_str) if year_str.isdigit() else None,
            month=_month_num(info.get("month")),
            doi=_first_str(info.get("doi")),
            abstract=_first_str(info.get("abstract")),
            url=_first_str(info.get("ee")) or _first_str(info.get("url")),
            venue=_first_str(info.get("venue")),
            pub_type=_first_str(info.get("type")),
            external_id=_first_str(info.get("key")),
            keywords=_keywords(info),
            raw=info,
        )