"""Conector SciELO (Portal de Busca iAHx — https://search.scielo.org/).

Estratégia de acesso:
1. Consulta ``https://search.scielo.org/`` com ``output=site``, ``format=summary``
   e tamanho de página aceito pelo iAHx (``15``, ``30`` ou ``50``).
2. Caso o Bunny Shield (``/.bunny-shield/bd/bunnyprint.js``) exija execução
   de JavaScript, resolve o desafio via Playwright (``jatai.browser``),
   sincroniza os cookies e ``User-Agent`` com o ``httpx.Client`` e extrai os
   resultados HTML (mantendo parser XML caso a fonte responda ``<response>``).
"""

import html
import random
import re
import threading
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin

import httpx

from jatai.sources.base import ProgressCallback, RawRecord, SourceConnector

BASE_URL = "https://search.scielo.org"
SEARCH_URL = f"{BASE_URL}/"
VALID_PAGE_SIZES = (15, 30, 50)
MAX_PAGE_SIZE = 50
MAX_OFFSET = 10000

_DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img",
    "input", "link", "meta", "param", "source", "track", "wbr",
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


def _choose_page_size(remaining: int) -> int:
    """iAHx aceita apenas 15, 30 ou 50 itens por página."""
    for size in VALID_PAGE_SIZES:
        if remaining <= size:
            return size
    return MAX_PAGE_SIZE


def _year_query_clause(year_spec: str) -> str:
    """Converte '2020', '2018-2026', '2015-', '-2019' em filtro `year_cluster`."""
    spec = year_spec.strip()
    if not spec:
        return ""
    if re.fullmatch(r"\d{4}", spec):
        return f"year_cluster:{spec}"
    m_range = re.fullmatch(r"(\d{4})\s*-\s*(\d{4})", spec)
    if m_range:
        return f"year_cluster:[{m_range.group(1)} TO {m_range.group(2)}]"
    m_from = re.fullmatch(r"(\d{4})\s*-", spec)
    if m_from:
        return f"year_cluster:[{m_from.group(1)} TO *]"
    m_to = re.fullmatch(r"-\s*(\d{4})", spec)
    if m_to:
        return f"year_cluster:[* TO {m_to.group(1)}]"
    return ""


def _parse_date_fields(raw_da: str, raw_year: str) -> tuple[int | None, int | None]:
    """Extrai (ano, mês) dos campos `da` (ex.: '2024-05', '20240500') e `year_cluster`."""
    year: int | None = None
    month: int | None = None

    y_clean = (raw_year or "").strip()
    if re.fullmatch(r"\d{4}", y_clean):
        year = int(y_clean)

    da = (raw_da or "").strip()
    if da:
        m_iso = re.match(r"^(\d{4})-(\d{2})", da)
        if m_iso:
            year = year or int(m_iso.group(1))
            m_val = int(m_iso.group(2))
            if 1 <= m_val <= 12:
                month = m_val
        elif re.match(r"^\d{6,8}$", da):
            year = year or int(da[:4])
            m_val = int(da[4:6])
            if 1 <= m_val <= 12:
                month = m_val
        elif re.match(r"^\d{4}$", da) and year is None:
            year = int(da)

    return year, month


def _doc_fields(doc_el: ET.Element) -> dict[str, list[str]]:
    """Extrai campos de um nó `<doc>` do XML Solr/iAHx."""
    fields: dict[str, list[str]] = {}
    for child in doc_el:
        name = child.attrib.get("name", "")
        if not name:
            continue
        if child.tag == "arr":
            vals = [
                "".join(item.itertext()).strip()
                for item in child
                if "".join(item.itertext()).strip()
            ]
            if vals:
                fields.setdefault(name, []).extend(vals)
        else:
            val = "".join(child.itertext()).strip()
            if val:
                fields.setdefault(name, []).append(val)
    return fields


def _pick_lang_field(
    fields: dict[str, list[str]],
    base: str,
    preferred_lang: str = "pt",
) -> str:
    """Seleciona campo multilíngue (`ti_pt`, `ti_en`, `ti_es`, `ti`)."""
    order = [f"{base}_{preferred_lang}", base, f"{base}_en", f"{base}_pt", f"{base}_es"]
    for key in order:
        vals = fields.get(key)
        if vals:
            return vals[0]
    for key, vals in fields.items():
        if key.startswith(f"{base}_") and vals:
            return vals[0]
    return ""


def _build_article_url(
    fields: dict[str, list[str]],
    pid: str,
    doi: str,
    lang: str,
) -> str:
    for key in ("ur", "fulltext_html_pt", "fulltext_html_en", "fulltext_html_es"):
        vals = fields.get(key)
        if vals and vals[0].startswith("http"):
            return vals[0]

    clean_pid = pid
    collection = "scl"
    m = re.match(r"^(?:art-)?(S\d{4}-\d{4}\d+)-([a-z]{3})$", pid, flags=re.IGNORECASE)
    if m:
        clean_pid = m.group(1)
        collection = m.group(2).lower()
    elif fields.get("in"):
        collection = fields["in"][0].lower()

    if clean_pid.startswith("S"):
        domain = "www.scielo.br" if collection == "scl" else f"www.scielo.{collection}"
        return f"https://{domain}/scielo.php?script=sci_arttext&pid={clean_pid}&tlng={lang}"

    if doi:
        return f"https://doi.org/{doi}"
    return ""


def _xml_doc_to_raw(doc_el: ET.Element, lang: str = "pt") -> RawRecord | None:
    fields = _doc_fields(doc_el)
    title = _pick_lang_field(fields, "ti", preferred_lang=lang)
    if not title:
        return None

    abstract = _pick_lang_field(fields, "ab", preferred_lang=lang)
    authors = fields.get("au") or []
    doi = (fields.get("doi") or [""])[0].replace("https://doi.org/", "").strip()
    pid = (fields.get("pid") or fields.get("id") or [""])[0].strip()
    venue = (fields.get("ta") or fields.get("fo") or [""])[0].strip()
    pub_type = (fields.get("type") or fields.get("la") or [""])[0].strip()
    raw_da = (fields.get("da") or [""])[0]
    raw_year = (fields.get("year_cluster") or [""])[0]
    year, month = _parse_date_fields(raw_da, raw_year)

    keywords: list[str] = []
    seen_kw: set[str] = set()
    for kw_key in (f"kw_{lang}", "kw", "de", "kw_en", "kw_pt", "kw_es"):
        for kw in fields.get(kw_key) or []:
            low = kw.lower()
            if kw and low not in seen_kw:
                seen_kw.add(low)
                keywords.append(kw)

    url = _build_article_url(fields, pid=pid, doi=doi, lang=lang)

    return RawRecord(
        title=title,
        authors=authors,
        year=year,
        month=month,
        doi=doi,
        abstract=abstract,
        url=url,
        venue=venue,
        pub_type=pub_type,
        external_id=pid,
        keywords=keywords,
        raw={k: (v[0] if len(v) == 1 else v) for k, v in fields.items()},
    )


def _parse_scielo_xml(
    xml_text: str,
    lang: str = "pt",
) -> tuple[list[RawRecord], int | None]:
    """Faz o parsing caso a resposta venha no formato XML Solr/iAHx."""
    cleaned = xml_text.strip()
    if "<response" in cleaned and not cleaned.startswith("<?xml") and not cleaned.startswith("<response"):
        start_idx = cleaned.find("<response")
        end_idx = cleaned.rfind("</response>")
        if start_idx != -1 and end_idx != -1:
            cleaned = cleaned[start_idx : end_idx + len("</response>")]

    root = ET.fromstring(cleaned)
    result_el = root.find(".//result")
    total: int | None = None
    if result_el is not None:
        num_found = result_el.attrib.get("numFound")
        if num_found and num_found.isdigit():
            total = int(num_found)

    records: list[RawRecord] = []
    for doc_el in root.findall(".//doc"):
        rec = _xml_doc_to_raw(doc_el, lang=lang)
        if rec is not None:
            records.append(rec)

    return records, total


class _SciELOItemParser(HTMLParser):
    """Extrai os cartões `<div class="item" id="art-...">` da página HTML do SciELO."""

    def __init__(self, preferred_lang: str = "pt") -> None:
        super().__init__(convert_charrefs=True)
        self.preferred_lang = preferred_lang
        self.items: list[dict] = []

        self._in_item = False
        self._item_depth = 0
        self._tag_depth = 0
        self._current: dict = {}
        self._capture_stack: list[tuple[str, int]] = []
        self._buf: dict[str, list[str]] = {}
        self._abstracts: dict[str, list[str]] = {}
        self._authors: list[str] = []
        self._in_author = False
        self._author_buf: list[str] = []

    @staticmethod
    def _classes(attrs_d: dict[str, str | None]) -> set[str]:
        return set((attrs_d.get("class") or "").split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = dict(attrs)
        classes = self._classes(attrs_d)

        if tag == "div" and not self._in_item and "item" in classes:
            self._in_item = True
            self._item_depth = 1
            self._tag_depth = 1
            self._current = {
                "item_id": attrs_d.get("id") or "",
                "title": "",
                "href": "",
                "authors": [],
                "source": "",
                "metadata": "",
                "abstract": "",
            }
            self._capture_stack = []
            self._buf = {"title": [], "source": [], "metadata": []}
            self._abstracts = {}
            self._authors = []
            self._in_author = False
            self._author_buf = []
            return

        if not self._in_item:
            return

        if tag == "div":
            self._item_depth += 1
        if tag not in _VOID_TAGS:
            self._tag_depth += 1

        el_id = attrs_d.get("id") or ""
        if tag == "strong" and "title" in classes:
            self._capture_stack.append(("title", self._tag_depth))
        elif "source" in classes:
            self._capture_stack.append(("source", self._tag_depth))
        elif "metadata" in classes or "DOIResults" in classes:
            self._capture_stack.append(("metadata", self._tag_depth))
        elif "abstract" in classes or "abstract_" in el_id:
            m_lang = re.search(r"abstract_([a-z]{2})\b", el_id)
            abs_key = m_lang.group(1) if m_lang else "default"
            self._abstracts.setdefault(abs_key, [])
            self._capture_stack.append((f"abs:{abs_key}", self._tag_depth))

        if tag == "a":
            href = (attrs_d.get("href") or "").strip()
            if "author" in classes:
                self._in_author = True
                self._author_buf = []
            elif href and not self._current["href"] and (
                "sci_arttext" in href or "/a/" in href or "doi.org/" in href
            ):
                self._current["href"] = href

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Tags auto-fechadas (<br/>, <img/>) não alteram a profundidade da pilha
        if tag == "a":
            attrs_d = dict(attrs)
            href = (attrs_d.get("href") or "").strip()
            if self._in_item and href and not self._current["href"] and (
                "sci_arttext" in href or "/a/" in href or "doi.org/" in href
            ):
                self._current["href"] = href

    def handle_data(self, data: str) -> None:
        if not self._in_item:
            return

        if self._in_author:
            self._author_buf.append(data)

        for role, _ in self._capture_stack:
            if role in self._buf:
                self._buf[role].append(data)
            elif role.startswith("abs:"):
                key = role.split(":", 1)[1]
                self._abstracts.setdefault(key, []).append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._in_item:
            return

        if tag == "a" and self._in_author:
            name = re.sub(r"\s{2,}", " ", "".join(self._author_buf)).strip(" ,;")
            if name:
                self._authors.append(name)
            self._in_author = False
            self._author_buf = []

        if tag not in _VOID_TAGS:
            while self._capture_stack and self._capture_stack[-1][1] == self._tag_depth:
                self._capture_stack.pop()
            self._tag_depth -= 1

        if tag == "div":
            self._item_depth -= 1
            if self._item_depth <= 0:
                self._current["title"] = re.sub(
                    r"\s{2,}", " ", "".join(self._buf["title"])
                ).strip()
                self._current["source"] = re.sub(
                    r"\s{2,}", " ", "".join(self._buf["source"])
                ).strip()
                self._current["metadata"] = re.sub(
                    r"\s{2,}", " ", "".join(self._buf["metadata"])
                ).strip()
                self._current["authors"] = list(dict.fromkeys(self._authors))

                chosen_abs = ""
                for lang_key in (self.preferred_lang, "en", "pt", "es", "default"):
                    if self._abstracts.get(lang_key):
                        text = re.sub(
                            r"\s{2,}", " ", "".join(self._abstracts[lang_key])
                        ).strip()
                        if text:
                            chosen_abs = text
                            break
                self._current["abstract"] = chosen_abs

                if self._current["title"]:
                    self.items.append(self._current)
                self._in_item = False


def _parse_scielo_html(
    html_text: str,
    preferred_lang: str = "pt",
) -> tuple[list[RawRecord], int | None]:
    """Parser HTML estrutural para `output=site`."""
    total: int | None = None
    m_total = re.search(
        r'id=["\']TotalHits["\'][^>]*>\s*([\d\.,]+)',
        html_text,
        flags=re.IGNORECASE,
    )
    if m_total:
        digits = re.sub(r"\D", "", m_total.group(1))
        if digits.isdigit():
            total = int(digits)

    parser = _SciELOItemParser(preferred_lang=preferred_lang)
    parser.feed(html_text)

    records: list[RawRecord] = []
    for item in parser.items:
        href = item["href"]
        full_url = urljoin(BASE_URL, href) if href else ""

        pid = ""
        m_item_pid = re.match(
            r"^(?:art-)?(S\d{4}-\d{4}\d+)(?:-[a-z]{3})?$",
            item["item_id"],
            flags=re.IGNORECASE,
        )
        if m_item_pid:
            pid = m_item_pid.group(1)
        elif full_url:
            m_url_pid = re.search(r"pid=(S\d{4}-\d{4}\d+)", full_url, flags=re.IGNORECASE)
            if m_url_pid:
                pid = m_url_pid.group(1)

        meta_and_src = f"{item['source']} {item['metadata']}"
        m_doi = re.search(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", meta_and_src)
        doi = m_doi.group(0).rstrip(".,;)") if m_doi else ""

        m_year = re.search(r"\b(19\d{2}|20\d{2})\b", item["source"])
        year = int(m_year.group(1)) if m_year else None

        venue = item["source"]
        if m_year:
            venue = item["source"][: m_year.start()].strip(" ,.-;")

        records.append(
            RawRecord(
                title=item["title"],
                authors=item["authors"],
                year=year,
                doi=doi,
                abstract=item["abstract"],
                url=full_url or (f"https://doi.org/{doi}" if doi else ""),
                venue=venue,
                external_id=pid or doi or full_url,
                raw={
                    "source_format": "html",
                    "item_id": item["item_id"],
                    "source_line": item["source"],
                },
            )
        )

    return records, total


def _is_challenge_page(status_code: int, html_text: str) -> bool:
    if status_code in (202, 403, 503):
        return True
    low = (html_text or "")[:6000].lower()
    if "<response" in low:
        return False
    if "pesquisa | scielo" not in low and "totalhits" not in low:
        return True
    return any(
        marker in low
        for marker in (
            "just a moment",
            "checking your browser",
            "please enable javascript",
        )
    )


class _PlaywrightFallback:
    """Sessão Playwright reutilizada apenas se o portal exigir desafio anti-bot JS."""

    def __init__(self, timeout_ms: int = 45000):
        from jatai import browser as _browser

        self._pw = _browser.get_playwright()
        self._browser = _browser.launch_browser()
        # Preserva o User-Agent nativo do Chrome para não divergir no bunnyprint.js
        self._context = self._browser.new_context(
            locale="pt-BR",
            viewport={"width": 1366, "height": 768},
        )
        self._page = self._context.new_page()
        self._timeout_ms = timeout_ms
        self.user_agent: str = ""

    def fetch(self, url: str) -> str:
        self._page.goto(url, timeout=30000, wait_until="domcontentloaded")
        deadline = time.time() + self._timeout_ms / 1000

        while time.time() < deadline:
            try:
                title = (self._page.title() or "").lower()
                if any(k in title for k in ("bot", "anubis", "making sure", "just a moment")):
                    self._page.wait_for_timeout(500)
                    continue

                content = self._page.content() or ""
                if not _is_challenge_page(200, content):
                    has_hits = re.search(
                        r'id=["\']TotalHits["\'][^>]*>\s*\d+',
                        content,
                        flags=re.IGNORECASE,
                    )
                    no_results = re.search(
                        r'id=["\']NoResults["\'][^>]*class=["\'][^"\']*\brow\b(?![^"\']*\bhide\b)',
                        content,
                        flags=re.IGNORECASE,
                    )
                    if (
                        "<response" in content
                        or has_hits
                        or no_results
                        or 'class="item"' in content
                    ):
                        if not self.user_agent:
                            try:
                                self.user_agent = (
                                    self._page.evaluate("() => navigator.userAgent") or ""
                                )
                            except Exception:
                                pass
                        return content
            except Exception:
                pass
            self._page.wait_for_timeout(500)

        content = self._page.content() or ""
        if not _is_challenge_page(200, content):
            return content
        raise TimeoutError("SciELO: desafio anti-bot JS não resolvido no tempo limite.")

    def sync_to_client(self, client: httpx.Client) -> None:
        try:
            for c in self._context.cookies(BASE_URL):
                name = c.get("name")
                val = c.get("value")
                if name and val:
                    client.cookies.set(
                        name,
                        val,
                        domain=c.get("domain") or ".scielo.org",
                        path=c.get("path") or "/",
                    )
            if self.user_agent:
                client.headers["User-Agent"] = self.user_agent
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._page.close()
        except Exception:
            pass
        try:
            self._context.close()
        except Exception:
            pass


class SciELOConnector(SourceConnector):
    source_type = "scielo"
    name = "SciELO"
    requires_api_key = False

    def __init__(
        self,
        timeout: float = 30.0,
        lang: str = "pt",
        year: str = "",
        collection: str = "",
        sort: str = "RELEVANCE",
        min_interval: float = 0.8,
        use_browser_fallback: bool = True,
    ):
        self.timeout = timeout
        self.lang = lang.strip().lower() or "pt"
        self.year = year.strip()
        # Ex.: 'scl' (Brasil), 'arg', 'chl', 'col', 'esp', 'mex' (vazio = todas)
        self.collection = collection.strip().lower()
        # Valores suportados: 'RELEVANCE', 'YEAR_DESC', 'YEAR_ASC', 'CITED_DESC'
        self.sort = sort.strip()
        self.min_interval = min_interval
        self.use_browser_fallback = use_browser_fallback
        self.last_request: dict = {}
        self.last_payloads: list[str] = []
        self._browser: _PlaywrightFallback | None = None
        self._client = httpx.Client(
            headers={
                "User-Agent": _DEFAULT_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
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

    def _get_browser(self) -> _PlaywrightFallback:
        if self._browser is None:
            self._browser = _PlaywrightFallback()
        return self._browser

    def _compiled_query(self, expression: str) -> str:
        from jatai.search.adapters.scielo import compile_expression

        compiled = compile_expression(expression)
        clauses = [compiled] if compiled else []

        year_clause = _year_query_clause(self.year)
        if year_clause:
            clauses.append(year_clause)

        if self.collection:
            clauses.append(f"in:{self.collection}")

        if not clauses:
            return ""
        if len(clauses) == 1:
            return clauses[0]

        first = clauses[0]
        if not (first.startswith("(") and first.endswith(")")):
            first = f"({first})"
        return f"{first} AND " + " AND ".join(clauses[1:])

    def _fetch_page_payload(self, params: dict, on_retry=None) -> str:
        url = f"{SEARCH_URL}?{urlencode(params)}"

        for tentativa in range(1, 5):
            _throttle(self.min_interval)
            try:
                resp = self._client.get(SEARCH_URL, params=params)
            finally:
                _mark_call_finished()

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    espera = float(retry_after) + random.uniform(0.2, 0.8)
                else:
                    espera = (2 ** tentativa) + random.uniform(0.3, 1.0)

                self.last_request["retry_429"] = tentativa
                if on_retry:
                    on_retry(tentativa, espera)
                time.sleep(espera)
                continue

            body = resp.text or ""
            if resp.status_code == 200 and not _is_challenge_page(resp.status_code, body):
                self.last_request["transport"] = "httpx"
                return body

            # Desafio anti-bot JS ou bloqueio HTTP: recorre ao Chromium
            if self.use_browser_fallback and _is_challenge_page(resp.status_code, body):
                _throttle(self.min_interval)
                try:
                    browser = self._get_browser()
                    rendered = browser.fetch(url)
                    browser.sync_to_client(self._client)
                    self.last_request["transport"] = "playwright"
                    return rendered
                finally:
                    _mark_call_finished()

            resp.raise_for_status()
            return body

        raise RuntimeError("SciELO: limite de requisições (429) após 4 tentativas.")

    def is_available(self) -> bool:
        try:
            payload = self._fetch_page_payload(
                params={
                    "q": "ti:(science)",
                    "lang": self.lang,
                    "count": 15,
                    "from": 1,
                    "page": 1,
                    "output": "site",
                }
            )
            return "<response" in payload or "TotalHits" in payload
        except Exception:  # noqa: BLE001
            return False

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

        offset = 1  # iAHx utiliza paginação 1-based (`from=1`)
        page = 0
        total: int | None = None

        while len(records) < max_results and offset <= MAX_OFFSET:
            if should_stop and should_stop():
                if records:
                    self.last_request["partial"] = True
                    self.last_request["warning"] = (
                        f"Busca interrompida na página {page + 1}"
                    )
                break

            remaining = max_results - len(records)
            page_size = _choose_page_size(remaining)
            next_page = page + 1

            params: dict = {
                "q": query,
                "lang": self.lang,
                "count": page_size,
                "from": offset,
                "page": next_page,
                "output": "site",
                "format": "summary",
            }
            if self.sort:
                params["sort"] = self.sort

            self.last_request = {
                "method": "GET",
                "url": SEARCH_URL,
                "params": dict(params),
            }

            try:
                payload = self._fetch_page_payload(params=params, on_retry=on_retry)
            except Exception as exc:
                if records:
                    self.last_request["partial"] = True
                    self.last_request["warning"] = (
                        f"Paginação interrompida na página {page + 1}: {exc}"
                    )
                    break
                raise

            self.last_payloads.append(payload)

            if "<response" in payload:
                batch, reported_total = _parse_scielo_xml(payload, lang=self.lang)
            else:
                batch, reported_total = _parse_scielo_html(
                    payload,
                    preferred_lang=self.lang,
                )

            if reported_total is not None:
                total = reported_total

            if not batch:
                break

            page = next_page
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

            offset += len(batch)
            if total is not None and offset > total:
                break

        self.last_request["pages"] = page
        self.last_request["compiled_query"] = query[:300]
        return records