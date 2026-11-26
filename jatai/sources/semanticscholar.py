"""Conector Semantic Scholar (Graph API /paper/search/bulk)."""

import random
import threading
import time

import httpx

from jatai.sources.base import ProgressCallback, RawRecord, SourceConnector

API_URL = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
SEARCH_PING_URL = "https://api.semanticscholar.org/graph/v1/paper/search"

FIELDS = (
    "paperId,title,abstract,year,publicationDate,venue,publicationTypes,"
    "journal,externalIds,authors.name,openAccessPdf,url"
)

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


def _month(pub_date: str) -> int | None:
    if len(pub_date) >= 7 and pub_date[4] == "-":
        try:
            return int(pub_date[5:7])
        except ValueError:
            return None
    return None


def _paper_to_raw(p: dict) -> RawRecord:
    ext = p.get("externalIds") or {}
    doi = ext.get("DOI") or ext.get("doi") or ""
    journal = p.get("journal") or {}
    venue = (
        (journal.get("name") if isinstance(journal, dict) else "")
        or p.get("venue")
        or ""
    )
    authors = [
        a.get("name", "")
        for a in (p.get("authors") or [])
        if isinstance(a, dict) and a.get("name")
    ]
    pdf = p.get("openAccessPdf") or {}
    pub_types = p.get("publicationTypes") or []
    pub_date = str(p.get("publicationDate") or "")
    paper_id = p.get("paperId") or ""

    return RawRecord(
        title=p.get("title") or "",
        authors=authors,
        year=p.get("year"),
        month=_month(pub_date),
        doi=doi,
        abstract=p.get("abstract") or "",
        url=(pdf.get("url") if isinstance(pdf, dict) else "") or p.get("url") or "",
        venue=venue,
        pub_type=pub_types[0] if pub_types else "",
        external_id=paper_id,
        raw={"paperId": paper_id, "externalIds": ext},
    )


class SemanticScholarConnector(SourceConnector):
    source_type = "semantic_scholar"
    name = "Semantic Scholar"
    requires_api_key = True

    def __init__(
        self,
        timeout: float = 30.0,
        api_key: str = "",
        year: str = "",
        sort: str = "citationCount:desc",
        min_interval: float | None = None,
    ):
        self.timeout = timeout
        self.api_key = api_key.strip()
        self.year = year.strip()
        self.sort = sort.strip()
        self.last_request: dict = {}
        self.last_payloads: list = []
        self.min_interval = (
            min_interval
            if min_interval is not None
            else (1.15 if self.api_key else 1.5)
        )
        self._client = httpx.Client(headers=self._headers(), timeout=self.timeout)

    def _headers(self) -> dict:
        h = dict(_UA)
        if self.api_key:
            h["x-api-key"] = self.api_key
        return h

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _get_throttled(self, url: str, params: dict, on_retry=None) -> httpx.Response:
        if not self.api_key:
            raise ValueError(
                "Semantic Scholar: api_key é obrigatória para evitar bloqueio."
            )

        for tentativa in range(1, 6):
            _throttle(self.min_interval)
            try:
                resp = self._client.get(url, params=params)
            finally:
                _mark_call_finished()

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    espera = float(retry_after) + random.uniform(0.2, 0.8)
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
            "Semantic Scholar: limite de requisições (429) após 5 tentativas"
        )

    def is_available(self) -> bool:
        if not self.api_key:
            return False
        try:
            r = self._get_throttled(
                SEARCH_PING_URL,
                params={"query": "test", "limit": 1, "fields": "title"},
            )
            return r.status_code == 200
        except (httpx.HTTPError, RuntimeError, ValueError):
            return False

    def _compiled_query(self, expression: str) -> str:
        from jatai.search.adapters.semanticscholar import compile_expression

        return compile_expression(expression)

    def _search_page(self, query: str, token: str | None, on_retry=None) -> dict:
        params = {"query": query, "fields": FIELDS}
        if self.sort:
            params["sort"] = self.sort
        if self.year:
            params["year"] = self.year
        if token:
            params["token"] = token

        self.last_request = {
            "method": "GET",
            "url": API_URL,
            "params": {**params, "token": "…" if token else None},
        }
        resp = self._get_throttled(API_URL, params=params, on_retry=on_retry)
        for tentativa in range(1, 4):
            try:
                return resp.json()
            except ValueError:
                if tentativa == 3:
                    raise RuntimeError(
                        "Semantic Scholar: resposta JSON inválida após 3 tentativas."
                    )
                time.sleep(2 ** tentativa + random.uniform(0.2, 0.8))
                resp = self._get_throttled(API_URL, params=params,
                                            on_retry=on_retry)

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
        token = None
        page = 0
        total = None

        while len(records) < max_results:
            if should_stop and should_stop():
                if records:
                    self.last_request["partial"] = True
                    self.last_request["warning"] = (
                        f"Busca interrompida na página {page + 1}"
                    )
                break
            try:
                data = self._search_page(query, token, on_retry=on_retry)
            except Exception as exc:
                if records:
                    self.last_request["partial"] = True
                    self.last_request["warning"] = (
                        f"Paginação interrompida na página {page + 1}: {exc}"
                    )
                    break
                raise
            self.last_payloads.append(data)
            results = data.get("data") or []
            if data.get("total") is not None:
                total = data.get("total")
            if not results and not data.get("token"):
                break

            page += 1
            page_records: list[RawRecord] = []
            for p in results:
                rec = _paper_to_raw(p)
                key = rec.external_id or rec.doi or rec.title.lower()
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                records.append(rec)
                page_records.append(rec)
                if len(records) >= max_results:
                    break

            if on_page and page_records:
                on_page(page, page_records)

            if on_progress:
                on_progress(page, len(records), total)

            token = data.get("token")
            if not token or not results:
                break

        self.last_request["pages"] = page
        self.last_request["compiled_query"] = query[:300]
        return records