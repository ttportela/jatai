"""Conector Crossref (REST API /works).

Notas da API Crossref:
- Aberta e gratuita, sem chave de API. Informar ``mailto`` coloca as
  requisições no *Polite Pool*, com limites de taxa maiores e mais estáveis.
- Paginação profunda via ``cursor=*`` e ``next-cursor`` com ``rows`` até 1000.
- Campos bibliográficos são buscados via ``query.bibliographic`` (título,
  resumo, autores, periódico e ano) e campos específicos via ``query.title``,
  ``query.author``, ``query.container-title`` e ``query.affiliation``.
- Resumos no Crossref frequentemente contêm marcação JATS XML
  (``<jats:p>``, ``<jats:italic>``), que são limpos automaticamente.
"""

import html
import random
import re
import threading
import time
from urllib.parse import quote

import httpx

from jatai.sources.base import ProgressCallback, RawRecord, SourceConnector

API_URL = "https://api.crossref.org/works"
MAX_PAGE_SIZE = 200

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


def _year_filters(year_spec: str) -> list[str]:
    """Converte '2020', '2018-2026', '2015-', '-2019' em filtros Crossref."""
    spec = year_spec.strip()
    if not spec:
        return []
    if re.fullmatch(r"\d{4}", spec):
        return [f"from-pub-date:{spec}", f"until-pub-date:{spec}"]
    m_range = re.fullmatch(r"(\d{4})\s*-\s*(\d{4})", spec)
    if m_range:
        return [
            f"from-pub-date:{m_range.group(1)}",
            f"until-pub-date:{m_range.group(2)}",
        ]
    m_from = re.fullmatch(r"(\d{4})\s*-", spec)
    if m_from:
        return [f"from-pub-date:{m_from.group(1)}"]
    m_to = re.fullmatch(r"-\s*(\d{4})", spec)
    if m_to:
        return [f"until-pub-date:{m_to.group(1)}"]
    return []


def _clean_jats_abstract(raw_abstract: str) -> str:
    """Remove tags JATS XML/HTML e cabeçalho redundante 'Abstract'."""
    if not raw_abstract:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw_abstract)
    text = html.unescape(text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    text = re.sub(r"^(?:abstract|resumo)\s*[:.-]?\s*", "", text, flags=re.IGNORECASE)
    return text.strip()


def _extract_date_parts(item: dict) -> tuple[int | None, int | None]:
    """Extrai (ano, mês) priorizando data de publicação sobre depósito/indexação."""
    for key in ("published", "published-print", "published-online", "issued", "created"):
        obj = item.get(key)
        if not isinstance(obj, dict):
            continue
        date_parts = obj.get("date-parts")
        if (
            isinstance(date_parts, list)
            and date_parts
            and isinstance(date_parts[0], list)
            and date_parts[0]
        ):
            first = date_parts[0]
            year = int(first[0]) if len(first) >= 1 and isinstance(first[0], int) else None
            month = (
                int(first[1])
                if len(first) >= 2 and isinstance(first[1], int) and 1 <= first[1] <= 12
                else None
            )
            if year is not None:
                return year, month
    return None, None


def _extract_authors(item: dict) -> list[str]:
    authors: list[str] = []
    for a in item.get("author") or []:
        if not isinstance(a, dict):
            continue
        given = (a.get("given") or "").strip()
        family = (a.get("family") or "").strip()
        if given or family:
            authors.append(f"{given} {family}".strip())
        elif a.get("name"):
            authors.append(str(a["name"]).strip())
    return authors


def _first_str(val) -> str:
    if isinstance(val, list):
        for x in val:
            s = str(x or "").strip()
            if s:
                return s
        return ""
    return str(val or "").strip()


def _item_to_raw(item: dict) -> RawRecord:
    doi = str(item.get("DOI") or "").strip()
    title = _first_str(item.get("title"))
    venue = _first_str(item.get("container-title")) or _first_str(item.get("short-container-title"))
    pub_type = str(item.get("type") or "").strip()
    year, month = _extract_date_parts(item)
    abstract = _clean_jats_abstract(str(item.get("abstract") or ""))

    resource_url = (
        ((item.get("resource") or {}).get("primary") or {}).get("URL")
        if isinstance(item.get("resource"), dict)
        else ""
    )
    url = str(resource_url or item.get("URL") or (f"https://doi.org/{doi}" if doi else "")).strip()

    raw_subjects = item.get("subject") or []
    keywords = [str(s).strip() for s in raw_subjects if str(s or "").strip()]

    return RawRecord(
        title=title,
        authors=_extract_authors(item),
        year=year,
        month=month,
        doi=doi,
        abstract=abstract,
        url=url,
        venue=venue,
        pub_type=pub_type,
        external_id=doi,
        keywords=keywords,
        raw={
            "doi": doi,
            "type": pub_type,
            "publisher": item.get("publisher"),
            "issn": item.get("ISSN"),
            "is_referenced_by_count": item.get("is-referenced-by-count"),
            "references_count": item.get("references-count"),
            "score": item.get("score"),
        },
    )


def _decompose_clause(clause: str) -> tuple[str, dict[str, str], list[str]]:
    """Separa tokens `campo:valor` emitidos pelo adaptador em parâmetros Crossref.

    Retorna `(texto_bibliografico, query_fields, filters)`.
    """
    query_buckets: dict[str, list[str]] = {
        "title": [],
        "author": [],
        "container-title": [],
        "affiliation": [],
    }
    filters: list[str] = []

    def _consume(match: re.Match) -> str:
        field = match.group(1).lower()
        val = (match.group(2) or match.group(3) or "").strip()
        if not val:
            return ""
        if field == "doi":
            filters.append(f"doi:{val}")
        elif field == "issn":
            filters.append(f"issn:{val}")
        elif field == "year":
            filters.extend(_year_filters(val))
        elif field in query_buckets:
            query_buckets[field].append(val)
        return " "

    remaining = re.sub(
        r'\b(title|author|container-title|affiliation|doi|issn|year):(?:"([^"]+)"|(\S+))',
        _consume,
        clause,
        flags=re.IGNORECASE,
    )
    bib_text = re.sub(r"\s{2,}", " ", remaining).strip()
    query_fields = {
        f"query.{k}": " ".join(vals)
        for k, vals in query_buckets.items()
        if vals
    }
    return bib_text, query_fields, filters


class CrossrefConnector(SourceConnector):
    source_type = "crossref"
    name = "Crossref"
    requires_api_key = False

    def __init__(
        self,
        timeout: float = 30.0,
        mailto: str = "",
        year: str = "",
        work_type: str = "",
        sort: str = "relevance",
        min_interval: float | None = None,
    ):
        self.timeout = timeout
        self.mailto = mailto.strip()
        self.year = year.strip()
        # Ex.: 'journal-article', 'proceedings-article', 'book-chapter' (vazio = todos)
        self.work_type = work_type.strip()
        # Ex.: 'relevance', '-published', '+published', '-is-referenced-by-count'
        self.sort = sort.strip()
        self.min_interval = (
            min_interval
            if min_interval is not None
            else (0.25 if self.mailto else 0.6)
        )
        self.last_request: dict = {}
        self.last_payloads: list[dict] = []
        self._client = httpx.Client(headers=self._headers(), timeout=self.timeout)

    def _headers(self) -> dict:
        contact = f"mailto:{self.mailto}" if self.mailto else "mailto:jatai@localhost"
        return {
            "User-Agent": f"jatai/0.1 (research agent; {contact})",
            "Accept": "application/json",
        }

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _sort_params(self) -> dict[str, str]:
        s = self.sort
        if not s:
            return {}
        if s.startswith("-"):
            return {"sort": s[1:], "order": "desc"}
        if s.startswith("+"):
            return {"sort": s[1:], "order": "asc"}
        return {"sort": s, "order": "desc"}

    def _request_json(
        self,
        url: str,
        params: dict | None = None,
        allow_404: bool = False,
        on_retry=None,
    ) -> dict | None:
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        if self.mailto and "mailto" not in clean_params:
            clean_params["mailto"] = self.mailto

        for tentativa in range(1, 5):
            _throttle(self.min_interval)
            try:
                resp = self._client.get(url, params=clean_params)
            finally:
                _mark_call_finished()

            if allow_404 and resp.status_code == 404:
                return None

            if resp.status_code in (429, 502, 503, 504):
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    espera = float(retry_after) + random.uniform(0.2, 0.8)
                else:
                    espera = (2 ** tentativa) + random.uniform(0.2, 0.8)

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
                if tentativa >= 4:
                    raise RuntimeError(
                        f"Crossref: resposta JSON inválida após 4 tentativas: {exc}"
                    ) from exc
                espera = (2 ** tentativa) + random.uniform(0.2, 0.8)
                if on_retry:
                    on_retry(tentativa, espera)
                time.sleep(espera)
                continue

        raise RuntimeError(
            "Crossref: limite de requisições ou instabilidade após 4 tentativas."
        )

    def is_available(self) -> bool:
        try:
            data = self._request_json(API_URL, params={"rows": 1})
            return isinstance(data, dict) and data.get("status") == "ok"
        except (httpx.HTTPError, RuntimeError):
            return False

    def work_by_doi(self, doi: str) -> dict | None:
        """Recupera os metadados brutos de uma obra pelo DOI (ou None se 404)."""
        clean_doi = re.sub(
            r"^https?://doi\.org/", "", (doi or "").strip(), flags=re.IGNORECASE
        )
        if not clean_doi:
            return None
        encoded = quote(clean_doi, safe="")
        self.last_request = {"method": "GET", "url": f"{API_URL}/{encoded}", "params": {}}
        data = self._request_json(f"{API_URL}/{encoded}", allow_404=True)
        return (data or {}).get("message")

    def search(
        self,
        expression: str,
        max_results: int = 100,
        on_progress: ProgressCallback | None = None,
        on_retry=None,
        on_page=None,
        should_stop=None,
    ) -> list[RawRecord]:
        from jatai.search.adapters.crossref import compile_expression

        clauses = compile_expression(expression)
        if not clauses:
            return []

        records: list[RawRecord] = []
        seen: set[str] = set()
        self.last_payloads = []
        pages = 0
        reported_total = 0

        base_filters = _year_filters(self.year)
        if self.work_type:
            base_filters.append(f"type:{self.work_type}")
        sort_params = self._sort_params()

        for clause in clauses:
            if should_stop and should_stop():
                break
            bib_text, query_fields, clause_filters = _decompose_clause(clause)
            all_filters = list(dict.fromkeys(base_filters + clause_filters))

            if not bib_text and not query_fields and not all_filters:
                continue

            cursor: str | None = "*"
            clause_total: int | None = None

            while len(records) < max_results and cursor:
                if should_stop and should_stop():
                    if records:
                        self.last_request["partial"] = True
                        self.last_request["warning"] = (
                            f"Busca interrompida na página {pages + 1}"
                        )
                    break
                rows = min(MAX_PAGE_SIZE, max_results - len(records))
                params: dict = {
                    "rows": rows,
                    "cursor": cursor,
                }
                if bib_text:
                    params["query.bibliographic"] = bib_text
                params.update(query_fields)
                if all_filters:
                    params["filter"] = ",".join(all_filters)
                params.update(sort_params)

                log_params = {**params, "cursor": "…" if cursor != "*" else "*"}
                self.last_request = {"method": "GET", "url": API_URL, "params": log_params}

                try:
                    data = self._request_json(API_URL, params=params,
                                               on_retry=on_retry) or {}
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

                msg = data.get("message") or {}
                raw_total = msg.get("total-results")
                if clause_total is None and isinstance(raw_total, int):
                    clause_total = raw_total
                    reported_total += raw_total

                items = msg.get("items") or []
                if not items:
                    break

                pages += 1
                page_records: list[RawRecord] = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    rec = _item_to_raw(item)
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
                    on_page(pages, page_records)

                if on_progress:
                    on_progress(pages, len(records), reported_total or None)

                next_cursor = msg.get("next-cursor")
                if not next_cursor or len(items) < rows:
                    break
                cursor = next_cursor

            if len(records) >= max_results:
                break

        self.last_request["pages"] = pages
        self.last_request["clauses"] = clauses
        self.last_request["total_reported"] = reported_total
        return records