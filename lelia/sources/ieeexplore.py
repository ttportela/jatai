"""Conector IEEE Xplore (Metadata Search API /api/v1/search/articles).

Notas da API IEEE Xplore:
- Autenticação via parâmetro de query ``apikey``.
- Paginação baseada em ``start_record`` (indexado a partir de **1**, não 0)
  e ``max_records`` (máximo de 200 registros por chamada).
- Filtro temporal nativo via ``start_year`` e ``end_year``.
- Limite padrão de cota: ~10 req/s e 200 req/dia no plano Developer; quando
  excedido, retorna HTTP 429 ou HTTP 403 com cabeçalho/mensagem de rate limit.
"""

import random
import re
import threading
import time

import httpx

from lelia.sources.base import ProgressCallback, RawRecord, SourceConnector

API_URL = "https://ieeexploreapi.ieee.org/api/v1/search/articles"
MAX_PAGE_SIZE = 200

_UA = {"User-Agent": "lelia/0.1 (research agent)"}

_RATE_LOCK = threading.Lock()
_GLOBAL_LAST_CALL = 0.0

_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


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


def _parse_year_bounds(year_spec: str) -> tuple[int | None, int | None]:
    """Converte '2020', '2018-2026', '2015-', '-2019' em (start_year, end_year)."""
    spec = year_spec.strip()
    if not spec:
        return None, None
    if re.fullmatch(r"\d{4}", spec):
        y = int(spec)
        return y, y
    m_range = re.fullmatch(r"(\d{4})\s*-\s*(\d{4})", spec)
    if m_range:
        return int(m_range.group(1)), int(m_range.group(2))
    m_from = re.fullmatch(r"(\d{4})\s*-", spec)
    if m_from:
        return int(m_from.group(1)), None
    m_to = re.fullmatch(r"-\s*(\d{4})", spec)
    if m_to:
        return None, int(m_to.group(1))
    return None, None


def _parse_month(pub_date: str) -> int | None:
    """Extrai mês (1-12) de strings como '15 March 2024', 'June 2023' ou '2024-03-15'."""
    s = (pub_date or "").strip()
    if not s:
        return None
    if len(s) >= 7 and s[4] == "-":
        try:
            m = int(s[5:7])
            return m if 1 <= m <= 12 else None
        except ValueError:
            pass
    for token in re.findall(r"[A-Za-z]+", s.lower()):
        if token in _MONTHS:
            return _MONTHS[token]
    return None


def _extract_authors(article: dict) -> list[str]:
    authors_obj = article.get("authors")
    if not isinstance(authors_obj, dict):
        return []
    raw_list = authors_obj.get("authors") or []
    names: list[str] = []
    for a in raw_list:
        if not isinstance(a, dict):
            continue
        name = (a.get("full_name") or "").strip()
        if name:
            names.append(name)
    return names


def _extract_keywords(article: dict) -> list[str]:
    index_terms = article.get("index_terms")
    if not isinstance(index_terms, dict):
        return []
    seen: set[str] = set()
    keywords: list[str] = []
    for group_key in ("author_terms", "ieee_terms", "mesh_terms"):
        group = index_terms.get(group_key)
        if not isinstance(group, dict):
            continue
        for term in group.get("terms") or []:
            t = str(term or "").strip()
            low = t.lower()
            if t and low not in seen:
                seen.add(low)
                keywords.append(t)
    return keywords


def _article_to_raw(art: dict) -> RawRecord:
    article_number = str(art.get("article_number") or "").strip()
    doi = str(art.get("doi") or "").strip()
    raw_year = art.get("publication_year")
    year = int(raw_year) if raw_year is not None and str(raw_year).isdigit() else None
    month = _parse_month(str(art.get("publication_date") or ""))

    url = (
        str(art.get("html_url") or "").strip()
        or str(art.get("pdf_url") or "").strip()
        or (f"https://doi.org/{doi}" if doi else "")
    )

    return RawRecord(
        title=str(art.get("title") or "").strip(),
        authors=_extract_authors(art),
        year=year,
        month=month,
        doi=doi,
        abstract=str(art.get("abstract") or "").strip(),
        url=url,
        venue=str(art.get("publication_title") or "").strip(),
        pub_type=str(art.get("content_type") or "").strip(),
        external_id=article_number,
        keywords=_extract_keywords(art),
        raw={
            "article_number": article_number,
            "citing_paper_count": art.get("citing_paper_count"),
            "citing_patent_count": art.get("citing_patent_count"),
            "access_type": art.get("access_type"),
            "is_number": art.get("is_number"),
            "pdf_url": art.get("pdf_url"),
        },
    )


def _is_rate_limit_response(resp: httpx.Response) -> bool:
    if resp.status_code == 429:
        return True
    if resp.status_code == 403:
        body = (resp.text or "").lower()
        if "over rate limit" in body or "over qps" in body:
            return True
    return False


class IEEEXploreConnector(SourceConnector):
    source_type = "ieeexplore"
    name = "IEEE Xplore"
    requires_api_key = True

    def __init__(
        self,
        timeout: float = 30.0,
        api_key: str = "",
        year: str = "",
        sort_field: str = "",
        sort_order: str = "desc",
        content_type: str = "",
        min_interval: float = 0.25,
    ):
        self.timeout = timeout
        self.api_key = api_key.strip()
        self.year = year.strip()
        # Valores válidos: 'article_number', 'article_title', 'author',
        # 'publication_title', 'publication_year' (vazio = relevância)
        self.sort_field = sort_field.strip()
        self.sort_order = sort_order.strip().lower() or "desc"
        # Ex.: 'Journals', 'Conferences', 'Early Access Articles', 'Standards', 'Books'
        self.content_type = content_type.strip()
        self.min_interval = min_interval
        self.last_request: dict = {}
        self.last_payloads: list[dict] = []
        self._client = httpx.Client(
            headers={**_UA, "Accept": "application/json"},
            timeout=self.timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _compiled_query(self, expression: str) -> str:
        from lelia.search.adapters.ieeexplore import compile_expression

        return compile_expression(expression)

    def _request_with_retry(self, params: dict, on_retry=None) -> httpx.Response:
        if not self.api_key:
            raise ValueError("IEEE Xplore: api_key é obrigatória.")

        req_params = {**params, "apikey": self.api_key}

        for tentativa in range(1, 6):
            _throttle(self.min_interval)
            try:
                resp = self._client.get(API_URL, params=req_params)
            finally:
                _mark_call_finished()

            if _is_rate_limit_response(resp):
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
            "IEEE Xplore: limite de requisições excedido após 5 tentativas."
        )

    def is_available(self) -> bool:
        if not self.api_key:
            return False
        try:
            resp = self._request_with_retry(
                params={
                    "querytext": "test",
                    "max_records": 1,
                    "start_record": 1,
                }
            )
            return resp.status_code == 200
        except (httpx.HTTPError, RuntimeError, ValueError):
            return False

    def _search_page(
        self,
        query: str,
        start_record: int,
        max_records: int,
        on_retry=None,
    ) -> dict:
        params: dict = {
            "querytext": query,
            "start_record": start_record,
            "max_records": min(max_records, MAX_PAGE_SIZE),
        }

        start_year, end_year = _parse_year_bounds(self.year)
        if start_year is not None:
            params["start_year"] = start_year
        if end_year is not None:
            params["end_year"] = end_year
        if self.content_type:
            params["content_type"] = self.content_type
        if self.sort_field:
            params["sort_field"] = self.sort_field
            params["sort_order"] = self.sort_order

        self.last_request = {
            "method": "GET",
            "url": API_URL,
            "params": {**params, "apikey": "***"},
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

        # Paginação do IEEE Xplore é 1-based (start_record=1 é o primeiro registro)
        start_record = 1
        page = 0
        total: int | None = None

        while len(records) < max_results:
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
                    start_record=start_record,
                    max_records=page_size,
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

            raw_total = data.get("total_records")
            if raw_total is not None and str(raw_total).isdigit():
                total = int(raw_total)

            articles = data.get("articles") or []
            if not articles:
                break

            page += 1
            page_records: list[RawRecord] = []
            for art in articles:
                if not isinstance(art, dict):
                    continue
                rec = _article_to_raw(art)
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

            start_record += len(articles)
            if total is not None and start_record > total:
                break

        self.last_request["pages"] = page
        self.last_request["compiled_query"] = query[:300]
        return records