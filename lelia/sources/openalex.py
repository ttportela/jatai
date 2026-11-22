"""Conector OpenAlex (API REST /works)."""

import random
import re
import threading
import time

import httpx

from lelia.sources.base import ProgressCallback, RawRecord, SourceConnector

API_URL = "https://api.openalex.org/works"
MAX_PAGE_SIZE = 100
MAX_BASIC_PAGING_RESULTS = 10000

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


def _abstract(inverted: dict | None) -> str:
    if not inverted or not isinstance(inverted, dict):
        return ""
    pos: dict[int, str] = {}
    for word, indexes in inverted.items():
        if not indexes:
            continue
        for i in indexes:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def _authors(work: dict) -> list[str]:
    out: list[str] = []
    for a in work.get("authorships") or []:
        if not isinstance(a, dict):
            continue
        name = (a.get("author") or {}).get("display_name")
        if name:
            out.append(name)
    return out


def _work_to_raw(w: dict) -> RawRecord:
    raw_doi = w.get("doi") or ""
    doi = re.sub(r"^https?://doi\.org/", "", raw_doi, flags=re.IGNORECASE).strip()
    venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
    keywords = [
        c.get("display_name") or ""
        for c in (w.get("keywords") or [])
        if isinstance(c, dict)
    ]
    month = None
    pub_date = str(w.get("publication_date") or "")
    if len(pub_date) >= 7 and pub_date[4] == "-":
        try:
            month = int(pub_date[5:7])
        except ValueError:
            month = None
    work_id = w.get("id") or ""
    return RawRecord(
        title=w.get("display_name") or "",
        authors=_authors(w),
        year=w.get("publication_year"),
        month=month,
        doi=doi,
        abstract=_abstract(w.get("abstract_inverted_index")),
        url=work_id,
        venue=venue,
        pub_type=w.get("type") or "",
        external_id=work_id,
        keywords=[k for k in keywords if k],
        raw={"id": work_id, "doi": w.get("doi"), "type": w.get("type")},
    )


def _year_date_filter(year_spec: str) -> str:
    """Converte '2024', '2020-2026', '2022-', '-2021' no filtro OpenAlex
    `from_publication_date`/`to_publication_date`."""
    spec = (year_spec or "").strip()
    if not spec:
        return ""
    parts: list[str] = []
    if re.fullmatch(r"\d{4}", spec):
        parts = [f"from_publication_date:{spec}-01-01",
                 f"to_publication_date:{spec}-12-31"]
    else:
        m = re.fullmatch(r"(\d{4})\s*-\s*(\d{4})", spec)
        if m:
            parts = [f"from_publication_date:{m.group(1)}-01-01",
                     f"to_publication_date:{m.group(2)}-12-31"]
        else:
            m = re.fullmatch(r"(\d{4})\s*-", spec)
            if m:
                parts = [f"from_publication_date:{m.group(1)}-01-01"]
            else:
                m = re.fullmatch(r"-\s*(\d{4})", spec)
                if m:
                    parts = [f"to_publication_date:{m.group(1)}-12-31"]
    return ",".join(parts)


def _work_short_id(work_id: str) -> str:
    """'https://openalex.org/W123' -> 'W123'."""
    return (work_id or "").rstrip("/").rsplit("/", 1)[-1]


class OpenAlexConnector(SourceConnector):
    source_type = "openalex"
    name = "OpenAlex"

    def __init__(
        self,
        timeout: float = 30.0,
        mailto: str = "",
        api_key: str = "",
        year: str = "",
        min_interval: float | None = None,
    ):
        self.timeout = timeout
        self.mailto = mailto.strip()
        self.api_key = api_key.strip()
        self.year = year.strip()
        # Buscas booleanas complexas têm limite de 2 req/s com chave e 1 req/s sem chave
        self.min_interval = (
            min_interval
            if min_interval is not None
            else (0.55 if self.api_key else 1.05)
        )
        self.last_request: dict = {}
        self.last_payloads: list[dict] = []
        self._client = httpx.Client(headers=self._headers(), timeout=self.timeout)

    def _headers(self) -> dict:
        ua_contact = f"mailto:{self.mailto}" if self.mailto else "mailto:lelia@localhost"
        headers = {"User-Agent": f"lelia/0.1 (research agent; {ua_contact})"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

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

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        espera = float(retry_after) + random.uniform(0.1, 0.5)
                    except ValueError:
                        espera = (2 ** tentativa) + random.uniform(0.2, 0.8)
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
                # corpo truncado/corrompido (conexão caiu no meio) —
                # falha transitória: retenta com backoff como no 429
                if tentativa < 4:
                    espera = (2 ** tentativa) + random.uniform(0.2, 0.8)
                    if on_retry:
                        on_retry(tentativa, espera)
                    time.sleep(espera)
                    continue
                raise RuntimeError(
                    f"OpenAlex: resposta JSON inválida após 4 tentativas: {exc}"
                ) from exc

        raise RuntimeError("OpenAlex: limite de requisições (429) após 4 tentativas.")

    def is_available(self) -> bool:
        try:
            data = self._request_json(API_URL, params={"per-page": 1})
            return isinstance(data, dict)
        except (httpx.HTTPError, RuntimeError):
            return False

    def search(
        self,
        expression: str,
        max_results: int = 200,
        on_progress: ProgressCallback | None = None,
        on_retry=None,
        on_page=None,
        should_stop=None,
    ) -> list[RawRecord]:
        from lelia.search.adapters.openalex import compile_expression

        compiled = compile_expression(expression)
        records: list[RawRecord] = []
        seen: set[str] = set()
        self.last_payloads = []

        # Cursor paging requer api_key a partir da 2ª página; sem chave usa paginação básica
        use_cursor = bool(self.api_key)
        cursor: str | None = "*"
        page = 0

        while len(records) < max_results:
            if should_stop and should_stop():
                if records:
                    self.last_request["partial"] = True
                    self.last_request["warning"] = (
                        f"Busca interrompida na página {page + 1}"
                    )
                break
            if not use_cursor and page * MAX_PAGE_SIZE >= MAX_BASIC_PAGING_RESULTS:
                break

            per_page = min(MAX_PAGE_SIZE, max_results - len(records))
            params: dict = {
                "search": compiled,
                "per-page": per_page,
            }
            year_filter = _year_date_filter(self.year)
            if year_filter:
                params["filter"] = year_filter
            if use_cursor:
                params["cursor"] = cursor
                log_params = {**params, "cursor": "…" if cursor != "*" else "*"}
            else:
                params["page"] = page + 1
                log_params = dict(params)

            self.last_request = {"method": "GET", "url": API_URL, "params": log_params}
            try:
                data = self._request_json(API_URL, params=params,
                                           on_retry=on_retry) or {}
            except Exception as exc:
                if records:
                    self.last_request["partial"] = True
                    self.last_request["warning"] = (
                        f"Paginação interrompida na página {page + 1}: {exc}"
                    )
                    break
                raise
            self.last_payloads.append(data)

            results = data.get("results") or []
            if not results:
                break

            page += 1
            page_records: list[RawRecord] = []
            for w in results:
                rec = _work_to_raw(w)
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

            meta = data.get("meta") or {}
            if on_progress:
                on_progress(page, len(records), meta.get("count"))

            if use_cursor:
                cursor = meta.get("next_cursor")
                if not cursor:
                    break
            elif len(results) < per_page:
                break

        self.last_request["pages"] = page
        return records

    # ---------- Snowballing (lista de referências) ----------

    def _get(self, url: str, params: dict | None = None):
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        self.last_request = {"method": "GET", "url": url, "params": clean_params}
        return self._request_json(url, params=clean_params, allow_404=True)

    def work_by_doi(self, doi: str) -> dict | None:
        """Obra única pelo DOI (retorna o JSON bruto ou None se não existir)."""
        doi = re.sub(r"^https?://doi\.org/", "", (doi or "").strip(), flags=re.IGNORECASE)
        if not doi:
            return None
        return self._get(f"{API_URL}/doi:{doi}")

    def work_by_title(self, title: str) -> dict | None:
        """Melhor obra correspondente ao título (busca em title.search)."""
        clean_title = re.sub(r"[,:|]+", " ", (title or "")).strip()
        if not clean_title:
            return None
        from lelia.dedup.normalize import titles_similar

        data = self._get(
            API_URL,
            {
                "filter": f"title.search:{clean_title}",
                "per-page": 5,
            },
        )
        results = (data or {}).get("results") or []
        for w in results:
            if titles_similar(w.get("display_name") or "", title):
                return w
        return results[0] if results else None

    @staticmethod
    def referenced_works(work: dict) -> list[str]:
        """IDs OpenAlex das obras referenciadas (forma curta 'W123')."""
        return [_work_short_id(w) for w in (work.get("referenced_works") or []) if w]

    def works_by_ids(self, work_ids: list[str]) -> list[RawRecord]:
        """Busca metadados de obras por IDs OpenAlex (lotes de 50)."""
        records: list[RawRecord] = []
        ids = [_work_short_id(i) for i in work_ids if i]
        for i in range(0, len(ids), 50):
            chunk = ids[i : i + 50]
            data = self._get(
                API_URL,
                {
                    "filter": "ids.openalex:" + "|".join(chunk),
                    "per-page": MAX_PAGE_SIZE,
                },
            )
            for w in (data or {}).get("results") or []:
                records.append(_work_to_raw(w))
        return records