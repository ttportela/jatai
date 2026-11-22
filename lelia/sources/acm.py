"""Conector ACM Digital Library (https://dl.acm.org/action/doSearch).

Estratégia de acesso:
1. Tenta consultar ``https://dl.acm.org/action/doSearch`` via ``httpx.Client``.
2. Caso o portal exija execução de JavaScript/sessão de navegador, abre o
   Chrome via ``lelia.browser`` e passa a reutilizar diretamente essa sessão
   nas páginas seguintes, evitando requisições duplicadas.
3. Em caso de timeout em páginas profundas, executa retentativas com backoff
   e preserva todos os registros já coletados nas páginas anteriores.
"""

import html
import random
import re
import threading
import time
from collections.abc import Callable
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin

import httpx

from lelia.sources.base import ProgressCallback, RawRecord, SourceConnector

BASE_URL = "https://dl.acm.org"
SEARCH_URL = f"{BASE_URL}/action/doSearch"
MAX_PAGE_SIZE = 50
MAX_OFFSET = 5000

_DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

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
        jitter = random.uniform(0.2, 0.9)
        espera = (min_interval + jitter) - (agora - _GLOBAL_LAST_CALL)
        if espera > 0:
            time.sleep(espera)
        _GLOBAL_LAST_CALL = time.monotonic()


def _mark_call_finished() -> None:
    global _GLOBAL_LAST_CALL
    with _RATE_LOCK:
        _GLOBAL_LAST_CALL = time.monotonic()


def _parse_year_bounds(year_spec: str) -> tuple[int | None, int | None]:
    """Converte '2020', '2018-2026', '2015-', '-2019' em (AfterYear, BeforeYear)."""
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


def _parse_acm_date(date_text: str) -> tuple[int | None, int | None]:
    """Extrai (ano, mês) de strings como 'April 2024', '19 May 2023' ou '2022'."""
    s = (date_text or "").strip()
    if not s:
        return None, None

    year: int | None = None
    month: int | None = None

    m_year = re.search(r"\b(19\d{2}|20\d{2})\b", s)
    if m_year:
        year = int(m_year.group(1))

    for token in re.findall(r"[A-Za-z]+", s.lower()):
        if token in _MONTHS:
            month = _MONTHS[token]
            break

    return year, month


def _clean_text(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s{2,}", " ", html.unescape(text)).strip()


class _ACMItemParser(HTMLParser):
    """Extrai os cartões `<li class="search__item ...">` de uma página do ACM DL."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self._in_item = False
        self._item_depth = 0
        self._current: dict = {}

        self._capture_stack: list[tuple[str, int]] = []
        self._tag_depth = 0
        self._buf: dict[str, list[str]] = {}
        self._authors: list[str] = []
        self._in_author_link = False
        self._author_buf: list[str] = []

    @staticmethod
    def _has_class(attrs_dict: dict[str, str | None], cls_name: str) -> bool:
        raw = attrs_dict.get("class") or ""
        return cls_name in raw.split()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = dict(attrs)

        if tag == "li" and not self._in_item and self._has_class(attrs_d, "search__item"):
            self._in_item = True
            self._item_depth = 1
            self._tag_depth = 1
            self._current = {
                "title": "",
                "href": "",
                "doi": attrs_d.get("data-doi") or "",
                "authors": [],
                "venue": "",
                "date": "",
                "abstract": "",
                "pub_type": "",
            }
            self._capture_stack = []
            self._buf = {"title": [], "venue": [], "date": [], "abstract": [], "pub_type": []}
            self._authors = []
            self._in_author_link = False
            return

        if not self._in_item:
            return

        if tag == "li":
            self._item_depth += 1
        self._tag_depth += 1

        if self._has_class(attrs_d, "issue-item__title") or self._has_class(attrs_d, "hlFld-Title"):
            self._capture_stack.append(("title", self._tag_depth))
        elif self._has_class(attrs_d, "issue-item__content-authors"):
            self._capture_stack.append(("authors_box", self._tag_depth))
        elif self._has_class(attrs_d, "epub-section__title"):
            self._capture_stack.append(("venue", self._tag_depth))
        elif self._has_class(attrs_d, "bookPubDate"):
            self._capture_stack.append(("date", self._tag_depth))
        elif self._has_class(attrs_d, "issue-item__abstract"):
            self._capture_stack.append(("abstract", self._tag_depth))
        elif self._has_class(attrs_d, "issue-heading"):
            self._capture_stack.append(("pub_type", self._tag_depth))

        active_roles = {role for role, _ in self._capture_stack}

        if tag == "a":
            href = attrs_d.get("href") or ""
            if "title" in active_roles and href and not self._current["href"]:
                self._current["href"] = href
            if "authors_box" in active_roles:
                self._in_author_link = True
                self._author_buf = []

    def handle_data(self, data: str) -> None:
        if not self._in_item:
            return

        if self._in_author_link:
            self._author_buf.append(data)

        for role, _ in self._capture_stack:
            if role in self._buf:
                self._buf[role].append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._in_item:
            return

        if tag == "a" and self._in_author_link:
            name = re.sub(r"\s{2,}", " ", "".join(self._author_buf)).strip(" ,;")
            if name and name.lower() not in ("view all", "show all"):
                self._authors.append(name)
            self._in_author_link = False
            self._author_buf = []

        while self._capture_stack and self._capture_stack[-1][1] == self._tag_depth:
            self._capture_stack.pop()

        self._tag_depth -= 1

        if tag == "li":
            self._item_depth -= 1
            if self._item_depth <= 0:
                self._current["title"] = re.sub(r"\s{2,}", " ", "".join(self._buf["title"])).strip()
                self._current["venue"] = re.sub(r"\s{2,}", " ", "".join(self._buf["venue"])).strip()
                self._current["date"] = re.sub(r"\s{2,}", " ", "".join(self._buf["date"])).strip()
                self._current["abstract"] = re.sub(r"\s{2,}", " ", "".join(self._buf["abstract"])).strip()
                self._current["pub_type"] = re.sub(r"\s{2,}", " ", "".join(self._buf["pub_type"])).strip()
                self._current["authors"] = list(dict.fromkeys(self._authors))
                if self._current["title"]:
                    self.items.append(self._current)
                self._in_item = False


def _parse_acm_html(html_text: str) -> tuple[list[RawRecord], int | None]:
    """Extrai registros e total de resultados do HTML da busca do ACM DL."""
    total: int | None = None
    m_total = re.search(
        r'class=["\'][^"\']*\b(?:hitsLength|result__count)\b[^"\']*["\'][^>]*>(.*?)</',
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m_total:
        digits = re.sub(r"\D", "", _clean_text(m_total.group(1)))
        if digits.isdigit():
            total = int(digits)

    parser = _ACMItemParser()
    parser.feed(html_text)

    records: list[RawRecord] = []
    for item in parser.items:
        href = item["href"]
        full_url = urljoin(BASE_URL, href) if href else ""

        doi = item["doi"].strip()
        if not doi and href:
            m_doi = re.search(r"/doi/(?:abs/|full/|pdf/|epdf/)?(10\.\d{4,9}/[^\s?#]+)", href)
            if m_doi:
                doi = m_doi.group(1).rstrip("/")

        year, month = _parse_acm_date(item["date"])
        if doi and not full_url:
            full_url = f"{BASE_URL}/doi/{doi}"

        records.append(
            RawRecord(
                title=item["title"],
                authors=item["authors"],
                year=year,
                month=month,
                doi=doi,
                abstract=item["abstract"],
                url=full_url,
                venue=item["venue"],
                pub_type=item["pub_type"],
                external_id=doi or full_url,
                raw={
                    "date_text": item["date"],
                    "href": href,
                },
            )
        )

    return records, total


def _is_interstitial_page(status_code: int, html_text: str) -> bool:
    if status_code in (403, 503):
        return True
    low = (html_text or "")[:6000].lower()
    return any(
        marker in low
        for marker in (
            "just a moment",
            "attention required",
            "cf-browser-verification",
            "cf_chl_opt",
            "challenge-platform",
            "checking your browser",
            "enable javascript and cookies to continue",
        )
    )


class _BrowserSession:
    """Sessão Playwright reutilizável para carregar páginas HTML da ACM."""

    def __init__(self, timeout_ms: int = 90000, headless: bool = False):
        from lelia import browser as _browser

        self._pw = _browser.get_playwright()
        self._owns_browser = False

        try:
            self._browser = self._pw.chromium.launch(
                channel="chrome",
                headless=headless,
                args=["--no-first-run", "--no-default-browser-check"],
            )
            self._owns_browser = True
        except Exception:
            self._browser = _browser.launch_browser()
            self._owns_browser = False

        self._context = self._browser.new_context(
            viewport={"width": 1366, "height": 768},
        )
        self._page = self._context.new_page()
        self._timeout_ms = timeout_ms
        self._session_ready = False

    def fetch(self, url: str) -> str:
        deadline = time.time() + self._timeout_ms / 1000
        # Após a primeira página carregar, páginas seguintes usam timeout menor
        nav_timeout = 60000 if self._session_ready else 45000

        try:
            self._page.goto(url, timeout=nav_timeout, wait_until="domcontentloaded")
        except Exception as exc:
            # Se a página já começou a renderizar resultados apesar do timeout de rede, aproveita o DOM
            html_partial = ""
            try:
                html_partial = self._page.content() or ""
            except Exception:
                pass
            if "issue-item__title" in html_partial or "hitsLength" in html_partial:
                self._session_ready = True
                return html_partial
            raise TimeoutError(f"ACM Digital Library: timeout ao carregar {url}") from exc

        last_title = ""
        last_url = ""

        while time.time() < deadline:
            try:
                last_title = self._page.title() or ""
                last_url = self._page.url or ""
                ready = self._page.evaluate("() => document.readyState")
                html_now = self._page.content() or ""

                if not _is_interstitial_page(200, html_now) and ready in ("interactive", "complete"):
                    if (
                        "issue-item__title" not in html_now
                        and "hitsLength" not in html_now
                        and "noResults" not in html_now
                        and "search-result__no-result" not in html_now
                    ):
                        try:
                            self._page.wait_for_selector(
                                ".search__item, .hitsLength, .noResults, .search-result__no-result",
                                timeout=8000,
                            )
                            html_now = self._page.content() or html_now
                        except Exception:
                            pass

                    self._session_ready = True
                    return html_now
            except Exception:
                pass

            self._page.wait_for_timeout(1000)

        raise TimeoutError(
            f"ACM Digital Library: página não carregou no tempo limite "
            f"({self._timeout_ms // 1000}s, title={last_title!r}, url={last_url!r})."
        )

    def close(self) -> None:
        try:
            self._page.close()
        except Exception:
            pass
        try:
            self._context.close()
        except Exception:
            pass
        if self._owns_browser:
            try:
                self._browser.close()
            except Exception:
                pass


class ACMConnector(SourceConnector):
    source_type = "acm"
    name = "ACM Digital Library"
    requires_api_key = False

    def __init__(
        self,
        timeout: float = 45.0,
        year: str = "",
        sort: str = "relevancy",
        expand: str = "all",
        min_interval: float = 3.0,
        use_browser_fallback: bool = True,
        browser_timeout_ms: int = 90000,
        headless: bool = False,
    ):
        self.timeout = timeout
        self.year = year.strip()
        # Valores suportados: 'relevancy', 'Latest', 'Earliest', 'cited'
        self.sort = sort.strip()
        # 'all' = ACM Guide to Computing Literature; 'dl' = ACM Full-Text Collection
        self.expand = expand.strip() or "all"
        self.min_interval = min_interval
        self.use_browser_fallback = use_browser_fallback
        self.browser_timeout_ms = browser_timeout_ms
        self.headless = headless
        self.last_request: dict = {}
        self.last_payloads: list[str] = []
        self._browser: _BrowserSession | None = None
        self._client = httpx.Client(
            headers={
                "User-Agent": _DEFAULT_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=self.timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()
        if self._browser is not None:
            self._browser.close()
            self._browser = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _get_browser(self) -> _BrowserSession:
        if self._browser is None:
            self._browser = _BrowserSession(
                timeout_ms=self.browser_timeout_ms,
                headless=self.headless,
            )
        return self._browser

    def _compiled_query(self, expression: str) -> str:
        from lelia.search.adapters.acm import compile_expression

        return compile_expression(expression)

    def _fetch_html(self, params: dict, on_retry=None) -> str:
        url = f"{SEARCH_URL}?{urlencode(params)}"
        last_exc: Exception | None = None

        for tentativa in range(1, 4):
            _throttle(self.min_interval)
            try:
                # Se a sessão do navegador já foi aberta, usa-a diretamente
                # sem duplicar requisições no httpx
                if self._browser is not None:
                    html_text = self._browser.fetch(url)
                    self.last_request["transport"] = "playwright"
                    return html_text

                resp = self._client.get(SEARCH_URL, params=params)
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    espera = (
                        float(retry_after) + random.uniform(0.5, 1.5)
                        if retry_after and retry_after.isdigit()
                        else (3 ** tentativa) + random.uniform(0.5, 1.5)
                    )
                    self.last_request["retry_429"] = tentativa
                    if on_retry:
                        on_retry(tentativa, espera)
                    time.sleep(espera)
                    continue

                body = resp.text or ""
                if resp.status_code == 200 and not _is_interstitial_page(resp.status_code, body):
                    self.last_request["transport"] = "httpx"
                    return body

                if self.use_browser_fallback and _is_interstitial_page(resp.status_code, body):
                    browser = self._get_browser()
                    html_text = browser.fetch(url)
                    self.last_request["transport"] = "playwright"
                    return html_text

                resp.raise_for_status()
                return body

            except (TimeoutError, httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if tentativa >= 3:
                    break
                espera = (3 ** tentativa) + random.uniform(0.5, 1.5)
                self.last_request["retry_timeout"] = tentativa
                if on_retry:
                    on_retry(tentativa, espera)
                time.sleep(espera)
            finally:
                _mark_call_finished()

        raise TimeoutError(
            f"ACM Digital Library: falha após 3 tentativas ({last_exc})."
        ) from last_exc

    def is_available(self) -> bool:
        try:
            html_text = self._fetch_html(
                params={
                    "AllField": "computing",
                    "startPage": 0,
                    "pageSize": 20,
                    "expand": self.expand,
                }
            )
            return "issue-item__title" in html_text or "hitsLength" in html_text
        except Exception:  # noqa: BLE001
            return False

    def search(
        self,
        expression: str,
        max_results: int = 100,
        on_progress: ProgressCallback | None = None,
        on_retry=None,
        on_page: Callable[[int, list[RawRecord]], None] | None = None,
        should_stop=None,
    ) -> list[RawRecord]:
        records: list[RawRecord] = []
        seen: set[str] = set()
        self.last_payloads = []
        query = self._compiled_query(expression)
        if not query:
            return records

        after_year, before_year = _parse_year_bounds(self.year)
        start_page = 0  # Paginação 0-based no ACM DL
        page = 0
        total: int | None = None

        while len(records) < max_results and (start_page * MAX_PAGE_SIZE) < MAX_OFFSET:
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
                "AllField": query,
                "startPage": start_page,
                "pageSize": page_size,
                "expand": self.expand,
            }
            if after_year is not None:
                params["AfterYear"] = after_year
            if before_year is not None:
                params["BeforeYear"] = before_year
            if self.sort:
                params["sortBy"] = self.sort

            self.last_request = {
                "method": "GET",
                "url": SEARCH_URL,
                "params": dict(params),
            }

            try:
                html_text = self._fetch_html(params=params, on_retry=on_retry)
            except Exception as exc:
                # Se já coletamos páginas anteriores, retorna os resultados parciais
                # em vez de descartar toda a busca
                if records:
                    self.last_request["partial"] = True
                    self.last_request["warning"] = (
                        f"Paginação interrompida na página {page + 1}: {exc}"
                    )
                    break
                raise

            self.last_payloads.append(html_text)

            batch, reported_total = _parse_acm_html(html_text)
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

            start_page += 1
            if total is not None and (start_page * page_size) >= total:
                break

        self.last_request["pages"] = page
        self.last_request["compiled_query"] = query[:300]
        return records