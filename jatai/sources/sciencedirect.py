"""Conector ScienceDirect (Elsevier Search API /content/search/sciencedirect).

Autenticação via header ``X-ELS-APIKey`` (e ``X-ELS-Insttoken`` opcional
para acesso institucional).

Notas da API ScienceDirect:
- O endpoint ``/content/search/sciencedirect`` utiliza ``view=STANDARD``,
  ``start`` (offset baseado em 0) e ``count`` (até 100 registros por página).
- O filtro de ano é passado nativamente via parâmetro ``date`` (ex.: ``"2022"``
  ou ``"2018-2026"``).
- Quando não há resultados, a API retorna HTTP 200 com um único item
  contendo ``{"@_fa": "true", "error": "Result set was empty"}``.
"""

import datetime
import random
import re
import threading
import time

import httpx

from jatai.sources.base import ProgressCallback, RawRecord, SourceConnector

API_URL = "https://api.elsevier.com/content/search/sciencedirect"
MAX_PAGE_SIZE = 100
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


def _format_date_param(year_spec: str) -> str:
    """Converte '2020', '2018-2026', '2015-', '-2019' para o parâmetro `date`."""
    spec = year_spec.strip()
    if not spec:
        return ""
    if re.fullmatch(r"\d{4}", spec):
        return spec
    m_range = re.fullmatch(r"(\d{4})\s*-\s*(\d{4})", spec)
    if m_range:
        return f"{m_range.group(1)}-{m_range.group(2)}"
    m_from = re.fullmatch(r"(\d{4})\s*-", spec)
    if m_from:
        current_year = datetime.date.today().year + 1
        return f"{m_from.group(1)}-{current_year}"
    m_to = re.fullmatch(r"-\s*(\d{4})", spec)
    if m_to:
        return f"1900-{m_to.group(1)}"
    return ""


def _extract_authors(entry: dict) -> list[str]:
    """Normaliza autores do ScienceDirect (`authors.author` pode ser list[dict], list[str] ou dict)."""
    authors_field = entry.get("authors")
    raw_list = []
    if isinstance(authors_field, dict):
        raw_list = authors_field.get("author") or []
    elif isinstance(authors_field, list):
        raw_list = authors_field

    if isinstance(raw_list, (dict, str)):
        raw_list = [raw_list]

    names: list[str] = []
    for item in raw_list:
        if isinstance(item, str):
            name = item.strip()
            if name:
                names.append(name)
        elif isinstance(item, dict):
            name = (
                item.get("$")
                or item.get("authname")
                or item.get("name")
                or ""
            ).strip()
            if not name:
                surname = (item.get("surname") or "").strip()
                given = (item.get("given-name") or item.get("initials") or "").strip()
                name = f"{given} {surname}".strip()
            if name:
                names.append(name)

    if names:
        return names

    creator = (entry.get("dc:creator") or "").strip()
    return [creator] if creator else []


def _extract_url(entry: dict, pii: str, doi: str) -> str:
    for link in entry.get("link") or []:
        if isinstance(link, dict) and link.get("@ref") == "scidir":
            href = (link.get("@href") or "").strip()
            if href:
                return href
    if pii:
        return f"https://www.sciencedirect.com/science/article/pii/{pii}"
    if doi:
        return f"https://doi.org/{doi}"
    return ""


def _entry_to_raw(entry: dict) -> RawRecord:
    pii = str(entry.get("pii") or "").strip()
    dc_id = str(entry.get("dc:identifier") or "").strip()
    eid = str(entry.get("eid") or "").strip()
    doi = str(entry.get("prism:doi") or "").strip()

    external_id = pii or eid or dc_id.replace("DOI:", "").replace("PII:", "").strip()
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
        url=_extract_url(entry, pii, doi),
        venue=str(entry.get("prism:publicationName") or "").strip(),
        pub_type=pub_type,
        external_id=external_id,
        raw={
            "pii": pii,
            "eid": eid,
            "dc_identifier": dc_id,
            "openaccess": entry.get("openaccess"),
            "volume": entry.get("prism:volume"),
            "issue": entry.get("prism:issueIdentifier"),
            "starting_page": entry.get("prism:startingPage"),
        },
    )


class ScienceDirectConnector(SourceConnector):
    source_type = "sciencedirect"
    name = "ScienceDirect"
    requires_api_key = True

    def __init__(
        self,
        timeout: float = 30.0,
        api_key: str = "",
        inst_token: str = "",
        year: str = "",
        sort: str = "",
        view: str = "STANDARD",
        min_interval: float = 0.5,
    ):
        self.timeout = timeout
        self.api_key = api_key.strip()
        self.inst_token = inst_token.strip()
        self.year = year.strip()
        # Valores suportados: 'relevancy' (padrão se vazio), '-coverDate', '+coverDate'
        self.sort = sort.strip()
        self.view = view.strip().upper() or "STANDARD"
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
        from jatai.search.adapters.sciencedirect import compile_expression

        return compile_expression(expression)

    def _request_with_retry(self, params: dict, on_retry=None) -> httpx.Response:
        if not self.api_key:
            raise ValueError("ScienceDirect: api_key (X-ELS-APIKey) é obrigatória.")

        for tentativa in range(1, 6):
            _throttle(self.min_interval)
            try:
                resp = self._client.get(API_URL, params=params)
            finally:
                _mark_call_finished()

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

        raise RuntimeError(
            "ScienceDirect: limite de requisições (429) após 5 tentativas."
        )

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
            "view": self.view,
        }
        date_filter = _format_date_param(self.year)
        if date_filter:
            params["date"] = date_filter
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
        self.last_request["compiled_query"] = query[:300]
        return records