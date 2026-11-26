"""Conector Scopus (Elsevier Search API /content/search/scopus).

Autenticação via header ``X-ELS-APIKey`` (e ``X-ELS-Insttoken`` opcional
para acesso institucional completo).

Comportamento das views da API Scopus:
- ``view=COMPLETE`` retorna abstract (``dc:description``), lista completa de
  autores (``author``) e palavras-chave (``authkeywords``), mas requer
  entitlement institucional (IP ou ``X-ELS-Insttoken``).
- Chaves gratuitas do portal Elsevier Developer funcionam com
  ``view=STANDARD`` (título, primeiro autor ``dc:creator``, DOI, veículo e
  data). O conector inicia em ``COMPLETE`` e faz fallback automático para
  ``STANDARD`` se receber HTTP 401/403 de permissão de view.
- Quando não há resultados, a API retorna HTTP 200 com um único item
  contendo ``{"@_fa": "true", "error": "Result set was empty"}``.
"""

import random
import re
import threading
import time

import httpx

from jatai.sources.base import ProgressCallback, RawRecord, SourceConnector

API_URL = "https://api.elsevier.com/content/search/scopus"
MAX_PAGE_SIZE = 25
MAX_DEEP_OFFSET = 5000

_UA = {"User-Agent": "jatai/0.1 (research agent)"}

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


def _parse_cover_date(cover_date: str) -> tuple[int | None, int | None]:
    """Extrai (ano, mês) de `prism:coverDate` (YYYY-MM-DD)."""
    if not cover_date:
        return None, None
    parts = cover_date.split("-")
    year = int(parts[0]) if len(parts) >= 1 and parts[0].isdigit() else None
    month = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None
    return year, month


def _year_filter_clause(year_spec: str) -> str:
    """Converte '2020', '2018-2026', '2015-', '-2019' para filtro PUBYEAR."""
    spec = year_spec.strip()
    if not spec:
        return ""
    if re.fullmatch(r"\d{4}", spec):
        return f"PUBYEAR IS {spec}"
    m_range = re.fullmatch(r"(\d{4})\s*-\s*(\d{4})", spec)
    if m_range:
        return f"PUBYEAR > {int(m_range.group(1)) - 1} AND PUBYEAR < {int(m_range.group(2)) + 1}"
    m_from = re.fullmatch(r"(\d{4})\s*-", spec)
    if m_from:
        return f"PUBYEAR > {int(m_from.group(1)) - 1}"
    m_to = re.fullmatch(r"-\s*(\d{4})", spec)
    if m_to:
        return f"PUBYEAR < {int(m_to.group(1)) + 1}"
    return ""


def _extract_authors(entry: dict) -> list[str]:
    raw_authors = entry.get("author")
    if isinstance(raw_authors, list) and raw_authors:
        names: list[str] = []
        for a in raw_authors:
            if not isinstance(a, dict):
                continue
            authname = (a.get("authname") or "").strip()
            if authname:
                names.append(authname)
                continue
            surname = (a.get("surname") or "").strip()
            given = (a.get("given-name") or a.get("initials") or "").strip()
            full = f"{surname}, {given}".strip(", ")
            if full:
                names.append(full)
        if names:
            return names

    creator = (entry.get("dc:creator") or "").strip()
    return [creator] if creator else []


def _extract_keywords(entry: dict) -> list[str]:
    raw_kw = entry.get("authkeywords")
    if not raw_kw or not isinstance(raw_kw, str):
        return []
    return [k.strip() for k in raw_kw.split("|") if k.strip()]


def _extract_url(entry: dict, doi: str) -> str:
    for link in entry.get("link") or []:
        if isinstance(link, dict) and link.get("@ref") == "scopus":
            href = (link.get("@href") or "").strip()
            if href:
                return href
    if doi:
        return f"https://doi.org/{doi}"
    return ""


def _entry_to_raw(entry: dict) -> RawRecord:
    dc_id = str(entry.get("dc:identifier") or "")
    scopus_id = dc_id.replace("SCOPUS_ID:", "").strip() or str(entry.get("eid") or "")
    doi = str(entry.get("prism:doi") or "").strip()
    year, month = _parse_cover_date(str(entry.get("prism:coverDate") or ""))

    pub_type = str(
        entry.get("subtypeDescription")
        or entry.get("prism:aggregationType")
        or ""
    ).strip()

    return RawRecord(
        title=str(entry.get("dc:title") or "").strip(),
        authors=_extract_authors(entry),
        year=year,
        month=month,
        doi=doi,
        abstract=str(entry.get("dc:description") or "").strip(),
        url=_extract_url(entry, doi),
        venue=str(entry.get("prism:publicationName") or "").strip(),
        pub_type=pub_type,
        external_id=scopus_id,
        keywords=_extract_keywords(entry),
        raw={
            "scopus_id": scopus_id,
            "eid": entry.get("eid"),
            "citedby_count": entry.get("citedby-count"),
            "openaccess": entry.get("openaccess"),
            "issn": entry.get("prism:issn"),
            "eissn": entry.get("prism:eIssn"),
        },
    )


class ScopusConnector(SourceConnector):
    source_type = "scopus"
    name = "Scopus"
    requires_api_key = True

    def __init__(
        self,
        timeout: float = 30.0,
        api_key: str = "",
        inst_token: str = "",
        year: str = "",
        sort: str = "-citedby-count",
        view: str = "COMPLETE",
        min_interval: float = 0.5,
    ):
        self.timeout = timeout
        self.api_key = api_key.strip()
        self.inst_token = inst_token.strip()
        self.year = year.strip()
        # Valores suportados: '-citedby-count', '-coverdate', '+coverdate', 'relevancy'
        self.sort = sort.strip()
        self._preferred_view = view.strip().upper() or "COMPLETE"
        self.min_interval = min_interval
        self.last_request: dict = {}
        self.last_payloads: list[dict] = []
        self._client = httpx.Client(headers=self._headers(), timeout=self.timeout)

    def _headers(self) -> dict:
        h = {**_UA, "Accept": "application/json"}
        if self.api_key:
            h["X-ELS-APIKey"] = self.api_key
        if self.inst_token:
            h["X-ELS-Insttoken"] = self.inst_token
        return h

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _compiled_query(self, expression: str) -> str:
        from jatai.search.adapters.scopus import compile_expression

        compiled = compile_expression(expression)
        year_clause = _year_filter_clause(self.year)
        if compiled and year_clause:
            return f"({compiled}) AND {year_clause}"
        return compiled

    def _request_with_retry(self, params: dict, on_retry=None) -> httpx.Response:
        if not self.api_key:
            raise ValueError("Scopus: api_key (X-ELS-APIKey) é obrigatória.")

        for tentativa in range(1, 6):
            _throttle(self.min_interval)
            try:
                resp = self._client.get(API_URL, params=params)
            finally:
                _mark_call_finished()

            # Fallback automático se a chave não tiver entitlement para view=COMPLETE
            if (
                resp.status_code in (401, 403)
                and params.get("view") == "COMPLETE"
            ):
                self._preferred_view = "STANDARD"
                params["view"] = "STANDARD"
                continue

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                rate_reset = resp.headers.get("X-RateLimit-Reset")
                if retry_after and retry_after.isdigit():
                    espera = float(retry_after) + random.uniform(0.2, 0.8)
                elif rate_reset and rate_reset.isdigit():
                    delta = max(1.0, float(rate_reset) - time.time())
                    espera = min(delta, 30.0) + random.uniform(0.2, 0.8)
                else:
                    espera = (2 ** tentativa) + random.uniform(0.2, 1.0)

                self.last_request["retry_429"] = tentativa
                if on_retry:
                    on_retry(tentativa, espera)
                time.sleep(espera)
                continue

            resp.raise_for_status()
            return resp

        raise RuntimeError("Scopus: limite de requisições (429) após 5 tentativas.")

    def is_available(self) -> bool:
        if not self.api_key:
            return False
        try:
            resp = self._request_with_retry(
                params={
                    "query": "TITLE(test)",
                    "count": 1,
                    "start": 0,
                    "view": "STANDARD",
                }
            )
            return resp.status_code == 200
        except (httpx.HTTPError, RuntimeError, ValueError):
            return False

    def _search_page(
        self,
        query: str,
        start: int,
        count: int,
        on_retry=None,
    ) -> dict:
        params: dict = {
            "query": query,
            "start": start,
            "count": min(count, MAX_PAGE_SIZE),
            "view": self._preferred_view,
        }
        if self.sort:
            params["sort"] = self.sort

        self.last_request = {
            "method": "GET",
            "url": API_URL,
            "params": dict(params),
        }
        for tentativa in range(1, 4):
            resp = self._request_with_retry(params=params, on_retry=on_retry)
            try:
                return resp.json()
            except ValueError:
                if tentativa == 3:
                    raise RuntimeError(
                        f"{self.source_type}: resposta JSON inválida após 3 tentativas."
                    )
                time.sleep(2 ** tentativa + random.uniform(0.2, 0.8))

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

        while len(records) < max_results and start < MAX_DEEP_OFFSET:
            if should_stop and should_stop():
                if records:
                    self.last_request["partial"] = True
                    self.last_request["warning"] = (
                        f"Busca interrompida na página {page + 1}"
                    )
                break
            remaining = max_results - len(records)
            page_size = min(MAX_PAGE_SIZE, remaining)

            try:
                data = self._search_page(
                    query=query,
                    start=start,
                    count=page_size,
                    on_retry=on_retry,
                )
            except Exception as exc:
                if records:
                    self.last_request["partial"] = True
                    self.last_request["warning"] = (
                        f"Paginação interrompida na página {page + 1}: {exc}"
                    )
                    break
                raise
            self.last_payloads.append(data)
            sr = data.get("search-results") or {}

            raw_total = sr.get("opensearch:totalResults")
            if raw_total is not None and str(raw_total).isdigit():
                total = int(raw_total)

            entries = sr.get("entry") or []
            if not entries or (
                len(entries) == 1
                and isinstance(entries[0], dict)
                and entries[0].get("error")
            ):
                break

            page += 1
            page_records: list[RawRecord] = []
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("error"):
                    continue
                rec = _entry_to_raw(entry)
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

            start += len(entries)
            if total is not None and start >= total:
                break

        self.last_request["pages"] = page
        self.last_request["view_used"] = self._preferred_view
        self.last_request["compiled_query"] = query[:300]
        return records