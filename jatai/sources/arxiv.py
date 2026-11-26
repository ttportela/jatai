"""Conector arXiv (Atom 1.0 API — https://export.arxiv.org/api/query)."""

import random
import re
import threading
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlencode

import httpx

from jatai.sources.base import ProgressCallback, RawRecord, SourceConnector

API_URL = "https://export.arxiv.org/api/query"
MAX_PAGE_SIZE = 200
MAX_OFFSET = 30000

_UA = {"User-Agent": "jatai/0.1 (research agent; mailto:jatai@localhost)"}

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
    "arxiv": "http://arxiv.org/schemas/atom",
}

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


def _submitted_date_clause(year_spec: str) -> str:
    """Converte '2024', '2020-2026', '2022-', '-2021' em filtro `submittedDate`."""
    spec = year_spec.strip()
    if not spec:
        return ""
    if re.fullmatch(r"\d{4}", spec):
        return f"submittedDate:[{spec}01010000 TO {spec}12312359]"
    m_range = re.fullmatch(r"(\d{4})\s*-\s*(\d{4})", spec)
    if m_range:
        return f"submittedDate:[{m_range.group(1)}01010000 TO {m_range.group(2)}12312359]"
    m_from = re.fullmatch(r"(\d{4})\s*-", spec)
    if m_from:
        return f"submittedDate:[{m_from.group(1)}01010000 TO 209912312359]"
    m_to = re.fullmatch(r"-\s*(\d{4})", spec)
    if m_to:
        return f"submittedDate:[199101010000 TO {m_to.group(1)}12312359]"
    return ""


def _build_query_url(params: dict) -> str:
    """Monta a URL no formato CGI esperado pelo arXiv (`+` para espaços e `[]` literais)."""
    parts: list[str] = []
    for k, v in params.items():
        if v is None:
            continue
        if k == "search_query":
            encoded_q = quote(str(v), safe=" :()[]*._-").replace(" ", "+")
            parts.append(f"search_query={encoded_q}")
        else:
            parts.append(urlencode({k: v}))
    return f"{API_URL}?{'&'.join(parts)}"


def _clean_whitespace(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _parse_iso_date(raw_date: str) -> tuple[int | None, int | None]:
    """Extrai (ano, mês) de timestamps ISO-8601 como '2024-05-20T17:59:59Z'."""
    s = (raw_date or "").strip()
    if len(s) >= 4 and s[:4].isdigit():
        year = int(s[:4])
        month = None
        if len(s) >= 7 and s[4] == "-" and s[5:7].isdigit():
            m_val = int(s[5:7])
            if 1 <= m_val <= 12:
                month = m_val
        return year, month
    return None, None


def _extract_arxiv_id(raw_id: str) -> tuple[str, str]:
    """'http://arxiv.org/abs/2305.12345v2' -> ('2305.12345v2', '2305.12345')."""
    clean = re.sub(r"^https?://arxiv\.org/(?:abs|pdf)/", "", (raw_id or "").strip())
    clean = re.sub(r"\.pdf$", "", clean)
    base = re.sub(r"v\d+$", "", clean)
    return clean, base


def _entry_to_raw(entry: ET.Element) -> RawRecord | None:
    raw_id = _clean_whitespace(entry.findtext("atom:id", default="", namespaces=_NS))
    if "/api/errors#" in raw_id:
        summary = _clean_whitespace(
            entry.findtext("atom:summary", default="Erro na consulta arXiv", namespaces=_NS)
        )
        raise ValueError(f"arXiv API: {summary}")

    arxiv_id, base_id = _extract_arxiv_id(raw_id)
    title = _clean_whitespace(entry.findtext("atom:title", default="", namespaces=_NS))
    if not title:
        return None

    abstract = _clean_whitespace(entry.findtext("atom:summary", default="", namespaces=_NS))

    authors: list[str] = []
    for author_el in entry.findall("atom:author", namespaces=_NS):
        name = _clean_whitespace(author_el.findtext("atom:name", default="", namespaces=_NS))
        if name:
            authors.append(name)

    published = _clean_whitespace(entry.findtext("atom:published", default="", namespaces=_NS))
    updated = _clean_whitespace(entry.findtext("atom:updated", default="", namespaces=_NS))
    year, month = _parse_iso_date(published or updated)

    doi = _clean_whitespace(entry.findtext("arxiv:doi", default="", namespaces=_NS))
    doi = re.sub(r"^https?://doi\.org/", "", doi, flags=re.IGNORECASE).strip()

    journal_ref = _clean_whitespace(
        entry.findtext("arxiv:journal_ref", default="", namespaces=_NS)
    )
    comment = _clean_whitespace(entry.findtext("arxiv:comment", default="", namespaces=_NS))

    primary_cat_el = entry.find("arxiv:primary_category", namespaces=_NS)
    primary_cat = (
        (primary_cat_el.attrib.get("term") or "").strip()
        if primary_cat_el is not None
        else ""
    )

    categories: list[str] = []
    seen_cats: set[str] = set()
    if primary_cat:
        categories.append(primary_cat)
        seen_cats.add(primary_cat.lower())
    for cat_el in entry.findall("atom:category", namespaces=_NS):
        term = (cat_el.attrib.get("term") or "").strip()
        if term and term.lower() not in seen_cats:
            seen_cats.add(term.lower())
            categories.append(term)

    abs_url = ""
    pdf_url = ""
    for link_el in entry.findall("atom:link", namespaces=_NS):
        href = (link_el.attrib.get("href") or "").strip()
        rel = (link_el.attrib.get("rel") or "").strip()
        link_title = (link_el.attrib.get("title") or "").strip().lower()
        link_type = (link_el.attrib.get("type") or "").strip().lower()
        if not href:
            continue
        href_https = re.sub(r"^http://arxiv\.org/", "https://arxiv.org/", href)
        if link_title == "pdf" or link_type == "application/pdf":
            pdf_url = href_https
        elif rel == "alternate" or link_type == "text/html":
            abs_url = href_https

    canonical_url = (
        abs_url
        or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else "")
        or pdf_url
    )

    return RawRecord(
        title=title,
        authors=authors,
        year=year,
        month=month,
        doi=doi,
        abstract=abstract,
        url=canonical_url,
        venue=journal_ref or "arXiv",
        pub_type="preprint",
        external_id=base_id or arxiv_id,
        keywords=categories,
        raw={
            "arxiv_id": arxiv_id,
            "arxiv_id_base": base_id,
            "primary_category": primary_cat,
            "categories": categories,
            "journal_ref": journal_ref,
            "comment": comment,
            "published": published,
            "updated": updated,
            "pdf_url": pdf_url,
        },
    )


def _parse_atom_feed(xml_text: str) -> tuple[list[RawRecord], int | None]:
    if not xml_text or "<feed" not in xml_text:
        return [], None
    root = ET.fromstring(xml_text)
    total_text = _clean_whitespace(
        root.findtext("opensearch:totalResults", default="", namespaces=_NS)
    )
    total = int(total_text) if total_text.isdigit() else None

    records: list[RawRecord] = []
    for entry in root.findall("atom:entry", namespaces=_NS):
        rec = _entry_to_raw(entry)
        if rec is not None:
            records.append(rec)

    return records, total


class ArxivConnector(SourceConnector):
    source_type = "arxiv"
    name = "arXiv"
    requires_api_key = False

    def __init__(
        self,
        timeout: float = 30.0,
        year: str = "",
        category: str = "",
        sort_by: str = "relevance",
        sort_order: str = "descending",
        min_interval: float = 3.1,
    ):
        self.timeout = timeout
        self.year = year.strip()
        self.category = category.strip()
        self.sort_by = sort_by.strip() or "relevance"
        self.sort_order = sort_order.strip() or "descending"
        self.min_interval = min_interval
        self.last_request: dict = {}
        self.last_payloads: list[str] = []
        self._client = httpx.Client(
            headers={**_UA, "Accept": "application/atom+xml,application/xml;q=0.9,*/*;q=0.8"},
            timeout=self.timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _compiled_query(self, expression: str) -> str:
        from jatai.search.adapters.arxiv import compile_expression

        compiled = compile_expression(expression)
        clauses = [f"({compiled})"] if compiled else []

        if self.category:
            cats = [c.strip() for c in re.split(r"[,|]+", self.category) if c.strip()]
            if len(cats) == 1:
                clauses.append(f"cat:{cats[0]}")
            elif len(cats) > 1:
                clauses.append("(" + " OR ".join(f"cat:{c}" for c in cats) + ")")

        date_clause = _submitted_date_clause(self.year)
        if date_clause:
            clauses.append(date_clause)

        if not clauses:
            return ""
        if len(clauses) == 1:
            return compiled if compiled else clauses[0]
        return " AND ".join(clauses)

    def _request_xml(self, params: dict, on_retry=None) -> str:
        url = _build_query_url(params)
        self.last_request["final_url"] = url

        for tentativa in range(1, 5):
            _throttle(self.min_interval)
            try:
                resp = self._client.get(url)
            finally:
                _mark_call_finished()

            body = resp.text or ""
            is_transient_blank = resp.status_code == 200 and "<feed" not in body

            if resp.status_code in (429, 500, 502, 503, 504) or is_transient_blank:
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    espera = float(retry_after) + random.uniform(0.3, 1.0)
                else:
                    espera = max(self.min_interval, 2 ** tentativa) + random.uniform(0.3, 1.2)

                self.last_request["retry_429"] = tentativa
                if on_retry:
                    on_retry(tentativa, espera)
                time.sleep(espera)
                continue

            resp.raise_for_status()
            return body

        raise RuntimeError(
            "arXiv: limite de requisições ou resposta vazia após 4 tentativas."
        )

    def is_available(self) -> bool:
        try:
            xml_text = self._request_xml(
                params={
                    "search_query": "all:electron",
                    "start": 0,
                    "max_results": 1,
                }
            )
            return "<feed" in xml_text
        except (httpx.HTTPError, RuntimeError):
            return False

    def work_by_id(self, arxiv_id: str) -> RawRecord | None:
        """Busca um artigo específico pelo identificador arXiv (ex.: '2305.12345')."""
        _, base_id = _extract_arxiv_id(arxiv_id)
        if not base_id:
            return None
        params = {"id_list": base_id, "max_results": 1}
        self.last_request = {"method": "GET", "url": API_URL, "params": dict(params)}
        xml_text = self._request_xml(params=params)
        records, _ = _parse_atom_feed(xml_text)
        return records[0] if records else None

    def search(
        self,
        expression: str,
        max_results: int = 100,
        on_progress: ProgressCallback | None = None,
        on_retry=None,
        on_page=None,
        should_stop=None,
    ) -> list[RawRecord]:
        records: list[RawRecord] = []
        seen: set[str] = set()
        self.last_payloads = []
        query = self._compiled_query(expression)
        if not query:
            return records

        start = 0
        page = 0
        total: int | None = None

        while len(records) < max_results and start < MAX_OFFSET:
            if should_stop and should_stop():
                if records:
                    self.last_request["partial"] = True
                    self.last_request["warning"] = (
                        f"Busca interrompida na página {page + 1}"
                    )
                break
            remaining = max_results - len(records)
            page_size = min(MAX_PAGE_SIZE, remaining)

            params: dict = {
                "search_query": query,
                "start": start,
                "max_results": page_size,
                "sortBy": self.sort_by,
                "sortOrder": self.sort_order,
            }

            self.last_request = {
                "method": "GET",
                "url": API_URL,
                "params": dict(params),
            }

            try:
                xml_text = self._request_xml(params=params, on_retry=on_retry)
            except Exception as exc:
                if records:
                    self.last_request["partial"] = True
                    self.last_request["warning"] = (
                        f"Paginação interrompida na página {page + 1}: {exc}"
                    )
                    break
                raise
            self.last_payloads.append(xml_text)

            batch, reported_total = _parse_atom_feed(xml_text)
            if reported_total is not None:
                total = reported_total

            if not batch:
                break

            page += 1
            page_records: list[RawRecord] = []
            for rec in batch:
                key = rec.external_id or rec.doi or rec.title.lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                records.append(rec)
                page_records.append(rec)
                if len(records) >= max_results:
                    break

            if on_page and page_records:
                on_page(page, page_records)

            if on_progress:
                on_progress(page, len(records), total)

            start += len(batch)
            if total is not None and start >= total:
                break

        self.last_request["pages"] = page
        self.last_request["compiled_query"] = query[:300]
        return records