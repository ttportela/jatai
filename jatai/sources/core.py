"""Conector CORE (REST API v3 — https://api.core.ac.uk/v3/search/works).

Notas da API CORE v3:
- Autenticação via header ``Authorization: Bearer <api_key>`` (chave gratuita
  obtida em ``core.ac.uk/services/api``).
- Paginação baseada em ``offset`` (0-based) e ``limit`` (até 100 resultados
  por página).
- Limite padrão para chaves gratuitas em buscas de obras: ~10 requisições a
  cada 10 segundos (``min_interval = 1.05`` por padrão), informando
  ``X-RateLimit-Retry-After`` ou ``Retry-After`` em respostas HTTP 429.
"""

import random
import re
import threading
import time

import httpx

from jatai.sources.base import ProgressCallback, RawRecord, SourceConnector

API_URL = "https://api.core.ac.uk/v3/search/works"
WORK_URL = "https://api.core.ac.uk/v3/works"
MAX_PAGE_SIZE = 100
MAX_OFFSET = 10000

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


def _year_query_clause(year_spec: str) -> str:
    """Converte '2024', '2020-2026', '2022-', '-2021' em filtro `yearPublished`."""
    spec = year_spec.strip()
    if not spec:
        return ""
    if re.fullmatch(r"\d{4}", spec):
        return f"yearPublished:{spec}"
    m_range = re.fullmatch(r"(\d{4})\s*-\s*(\d{4})", spec)
    if m_range:
        return f"(yearPublished>={m_range.group(1)} AND yearPublished<={m_range.group(2)})"
    m_from = re.fullmatch(r"(\d{4})\s*-", spec)
    if m_from:
        return f"yearPublished>={m_from.group(1)}"
    m_to = re.fullmatch(r"-\s*(\d{4})", spec)
    if m_to:
        return f"yearPublished<={m_to.group(1)}"
    return ""


def _parse_published_date(raw_date: str, fallback_year) -> tuple[int | None, int | None]:
    """Extrai (ano, mês) de `publishedDate` (ISO-8601) e `yearPublished`."""
    year: int | None = None
    month: int | None = None

    if fallback_year is not None and str(fallback_year).isdigit():
        year = int(fallback_year)

    s = (raw_date or "").strip()
    if len(s) >= 4 and s[:4].isdigit():
        year = year or int(s[:4])
        if len(s) >= 7 and s[4] == "-" and s[5:7].isdigit():
            m_val = int(s[5:7])
            if 1 <= m_val <= 12:
                month = m_val

    return year, month


def _extract_authors(work: dict) -> list[str]:
    names: list[str] = []
    for item in work.get("authors") or []:
        if isinstance(item, dict):
            name = (item.get("name") or "").strip()
            if name:
                names.append(name)
        elif isinstance(item, str):
            name = item.strip()
            if name:
                names.append(name)
    return names


def _extract_venue(work: dict) -> str:
    for journal in work.get("journals") or []:
        if isinstance(journal, dict):
            title = (journal.get("title") or "").strip()
            if title:
                return title
        elif isinstance(journal, str) and journal.strip():
            return journal.strip()
    return (work.get("publisher") or "").strip()


def _extract_doi(work: dict) -> str:
    raw_doi = (work.get("doi") or "").strip()
    if not raw_doi:
        for ident in work.get("identifiers") or []:
            if isinstance(ident, dict) and (ident.get("type") or "").upper() == "DOI":
                raw_doi = (ident.get("identifier") or "").strip()
                if raw_doi:
                    break
    return re.sub(r"^https?://doi\.org/", "", raw_doi, flags=re.IGNORECASE).strip()


def _extract_url(work: dict, core_id: str, doi: str) -> str:
    download_url = (work.get("downloadUrl") or "").strip()
    if download_url.startswith("http"):
        return download_url

    for src_url in work.get("sourceFulltextUrls") or []:
        if isinstance(src_url, str) and src_url.strip().startswith("http"):
            return src_url.strip()

    for link in work.get("links") or []:
        if isinstance(link, dict):
            href = (link.get("url") or "").strip()
            if href.startswith("http") and link.get("type") in ("download", "display"):
                return href

    if core_id:
        return f"https://core.ac.uk/works/{core_id}"
    if doi:
        return f"https://doi.org/{doi}"
    return ""


def _extract_keywords(work: dict) -> list[str]:
    seen: set[str] = set()
    keywords: list[str] = []

    fos = work.get("fieldOfStudy")
    if isinstance(fos, str) and fos.strip():
        keywords.append(fos.strip())
        seen.add(fos.strip().lower())
    elif isinstance(fos, list):
        for item in fos:
            s = str(item or "").strip()
            if s and s.lower() not in seen:
                seen.add(s.lower())
                keywords.append(s)

    for subj in work.get("subjects") or []:
        s = str(subj or "").strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            keywords.append(s)

    return keywords


def _work_to_raw(w: dict) -> RawRecord | None:
    title = (w.get("title") or "").strip()
    if not title:
        return None

    raw_id = w.get("id")
    core_id = str(raw_id).strip() if raw_id is not None else ""
    doi = _extract_doi(w)
    year, month = _parse_published_date(
        str(w.get("publishedDate") or ""),
        w.get("yearPublished"),
    )

    lang_obj = w.get("language")
    lang_code = (
        lang_obj.get("code")
        if isinstance(lang_obj, dict)
        else (str(lang_obj) if lang_obj else "")
    )

    return RawRecord(
        title=title,
        authors=_extract_authors(w),
        year=year,
        month=month,
        doi=doi,
        abstract=(w.get("abstract") or "").strip(),
        url=_extract_url(w, core_id=core_id, doi=doi),
        venue=_extract_venue(w),
        pub_type=(w.get("documentType") or "").strip(),
        external_id=core_id or doi,
        keywords=_extract_keywords(w),
        raw={
            "core_id": core_id,
            "doi": doi,
            "download_url": w.get("downloadUrl"),
            "source_fulltext_urls": w.get("sourceFulltextUrls"),
            "citation_count": w.get("citationCount"),
            "language": lang_code,
            "publisher": w.get("publisher"),
            "has_fulltext": bool(w.get("fullText")),
        },
    )


class COREConnector(SourceConnector):
    source_type = "core"
    name = "CORE"
    requires_api_key = True

    def __init__(
        self,
        timeout: float = 30.0,
        api_key: str = "",
        year: str = "",
        sort: str = "",
        document_type: str = "",
        language: str = "",
        min_interval: float = 1.05,
    ):
        self.timeout = timeout
        self.api_key = api_key.strip()
        self.year = year.strip()
        # Valores suportados: 'relevance:desc', 'yearPublished:desc',
        # 'yearPublished:asc', 'citationCount:desc'
        self.sort = sort.strip()
        # Ex.: 'research', 'thesis', 'preprint', 'conference' (vazio = todos)
        self.document_type = document_type.strip()
        # Código ISO-639-1 (ex.: 'en', 'pt', 'es')
        self.language = language.strip().lower()
        self.min_interval = min_interval
        self.last_request: dict = {}
        self.last_payloads: list[dict] = []
        self._client = httpx.Client(headers=self._headers(), timeout=self.timeout)

    def _headers(self) -> dict:
        h = {**_UA, "Accept": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _compiled_query(self, expression: str) -> str:
        from jatai.search.adapters.core import compile_expression

        compiled = compile_expression(expression)
        clauses = [f"({compiled})"] if compiled else []

        year_clause = _year_query_clause(self.year)
        if year_clause:
            clauses.append(year_clause)

        if self.document_type:
            clauses.append(f'documentType:"{self.document_type}"')

        if self.language:
            clauses.append(f'language.code:"{self.language}"')

        if not clauses:
            return ""
        if len(clauses) == 1:
            return compiled if compiled else clauses[0]
        return " AND ".join(clauses)

    def _request_json(
        self,
        url: str,
        params: dict | None = None,
        allow_404: bool = False,
        on_retry=None,
    ) -> dict | None:
        if not self.api_key:
            raise ValueError("CORE: api_key é obrigatória para a API v3.")

        clean_params = {k: v for k, v in (params or {}).items() if v is not None}

        for tentativa in range(1, 6):
            _throttle(self.min_interval)
            try:
                resp = self._client.get(url, params=clean_params)
            finally:
                _mark_call_finished()

            if allow_404 and resp.status_code == 404:
                return None

            if resp.status_code in (429, 500, 502, 503, 504):
                retry_after = (
                    resp.headers.get("X-RateLimit-Retry-After")
                    or resp.headers.get("Retry-After")
                )
                if retry_after and retry_after.isdigit():
                    espera = float(retry_after) + random.uniform(0.2, 0.8)
                else:
                    espera = (2 ** tentativa) + random.uniform(0.3, 1.0)

                self.last_request["retry_429"] = tentativa
                if on_retry:
                    on_retry(tentativa, espera)
                time.sleep(espera)
                continue

            resp.raise_for_status()
            try:
                return resp.json()
            except ValueError as exc:
                # corpo truncado/corrompido — falha transitória, retenta
                if tentativa >= 5:
                    raise RuntimeError(
                        f"CORE: resposta JSON inválida após 5 tentativas: {exc}"
                    ) from exc
                espera = (2 ** tentativa) + random.uniform(0.3, 1.0)
                if on_retry:
                    on_retry(tentativa, espera)
                time.sleep(espera)
                continue

        raise RuntimeError(
            "CORE: limite de requisições ou instabilidade após 5 tentativas."
        )

    def is_available(self) -> bool:
        if not self.api_key:
            return False
        try:
            data = self._request_json(
                API_URL,
                params={"q": 'title:"science"', "limit": 1, "offset": 0},
            )
            return isinstance(data, dict) and "results" in data
        except (httpx.HTTPError, RuntimeError, ValueError):
            return False

    def work_by_id(self, core_id: str | int) -> RawRecord | None:
        """Recupera uma obra específica pelo identificador numérico do CORE."""
        clean_id = re.sub(r"\D", "", str(core_id or ""))
        if not clean_id:
            return None
        url = f"{WORK_URL}/{clean_id}"
        self.last_request = {"method": "GET", "url": url, "params": {}}
        data = self._request_json(url, allow_404=True)
        if not isinstance(data, dict):
            return None
        return _work_to_raw(data)

    def _search_page(
        self,
        query: str,
        offset: int,
        limit: int,
        on_retry=None,
    ) -> dict:
        params: dict = {
            "q": query,
            "offset": offset,
            "limit": min(limit, MAX_PAGE_SIZE),
        }
        if self.sort:
            params["sort"] = self.sort

        self.last_request = {
            "method": "GET",
            "url": API_URL,
            "params": dict(params),
        }
        return self._request_json(API_URL, params=params, on_retry=on_retry) or {}

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

        offset = 0
        page = 0
        total: int | None = None

        while len(records) < max_results and offset < MAX_OFFSET:
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
                    offset=offset,
                    limit=page_size,
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

            raw_total = data.get("totalHits")
            if isinstance(raw_total, int):
                total = raw_total

            results = data.get("results") or []
            if not results:
                break

            page += 1
            page_records: list[RawRecord] = []
            for item in results:
                if not isinstance(item, dict):
                    continue
                rec = _work_to_raw(item)
                if rec is None:
                    continue
                key = (
                    rec.external_id
                    or rec.doi
                    or f"{rec.title}|{rec.year or ''}"
                ).lower()
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

            offset += len(results)
            if total is not None and offset >= total:
                break

        self.last_request["pages"] = page
        self.last_request["compiled_query"] = query[:300]
        return records