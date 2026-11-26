"""Providers de descoberta de PDFs open-access.

Cada provider pode resolver por DOI (modo "doi") ou por metadados
título/autores/ano (modo "meta"). Retornam Candidate(url, source, kind)
sem fazer download — a validação acontece no pipeline.
"""

import json
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

UNPAYWALL = "https://api.unpaywall.org/v2/{doi}"
OPENALEX = "https://api.openalex.org/works/doi:{doi}"
OPENALEX_SEARCH = "https://api.openalex.org/works"
CROSSREF = "https://api.crossref.org/works/{doi}"
CROSSREF_SEARCH = "https://api.crossref.org/works"
S2 = ("https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
      "?fields=title,year,openAccessPdf,externalIds")
S2_SEARCH = ("https://api.semanticscholar.org/graph/v1/paper/search"
             "?query={q}&limit={limit}&fields=title,year,openAccessPdf")
ARXIV = "http://export.arxiv.org/api/query"
CORE = "https://api.core.ac.uk/v3/search/works"

DEFAULT_SOURCES = ("unpaywall", "openalex", "crossref", "semanticscholar",
                   "core", "arxiv")

_UA = {"User-Agent": "jatai/0.1 (research agent; mailto:jatai@localhost)"}


@dataclass
class Query:
    doi: str = ""
    title: str = ""
    authors: tuple[str, ...] = field(default_factory=tuple)
    year: int | None = None


@dataclass
class Candidate:
    url: str
    source: str
    kind: str = "unknown"      # "pdf" | "landing" | "unknown"
    via: str = "doi"           # "doi" | "meta" | "direct"


def _get_json(url: str, params: dict | None = None,
              headers: dict | None = None, timeout: float = 20.0):
    try:
        resp = httpx.get(url, params=params, timeout=timeout,
                         headers={**_UA, **(headers or {})})
        if resp.status_code != 200:
            return None
        return resp.json()
    except (httpx.HTTPError, json.JSONDecodeError, ValueError):
        return None


def _pdf(url: str) -> str:
    """kind 'pdf' se a URL indica claramente um PDF."""
    path = url.split("?")[0].split("#")[0].lower()
    return "pdf" if (".pdf" in path or "/pdf" in path) else "landing"


def _cand(url: str, source: str, via: str = "doi",
          force_pdf: bool = False) -> Candidate | None:
    u = (url or "").strip()
    if not u.startswith("http"):
        return None
    return Candidate(u, source, "pdf" if force_pdf else _pdf(u), via)


# ---------------- Providers por DOI ----------------

def doi_unpaywall(doi: str, email: str = "jatai@localhost") -> list[Candidate]:
    data = _get_json(UNPAYWALL.format(doi=quote(doi, safe="")),
                     params={"email": email})
    if not data:
        return []
    out = []
    locs = list(data.get("oa_locations") or [])
    best = data.get("best_oa_location")
    if best:
        locs.insert(0, best)
    for loc in locs:
        for key, forced in (("url_for_pdf", True), ("url", False)):
            c = _cand(loc.get(key), "unpaywall", force_pdf=forced)
            if c:
                out.append(c)
    return out


def doi_openalex(doi: str) -> list[Candidate]:
    data = _get_json(OPENALEX.format(doi=quote(doi, safe="")))
    if not data:
        return []
    out = []
    best = data.get("best_oa_location") or {}
    for key, forced in (("pdf_url", True), ("landing_page_url", False)):
        c = _cand(best.get(key), "openalex", force_pdf=forced)
        if c:
            out.append(c)
    for loc in data.get("locations") or []:
        c = _cand(loc.get("pdf_url"), "openalex", force_pdf=True)
        if c:
            out.append(c)
    c = _cand((data.get("open_access") or {}).get("oa_url"), "openalex")
    if c:
        out.append(c)
    return out


def doi_crossref(doi: str) -> list[Candidate]:
    data = _get_json(CROSSREF.format(doi=quote(doi, safe="")))
    msg = (data or {}).get("message") or {}
    out = []
    for link in msg.get("link") or []:
        if "pdf" in (link.get("content-type") or "").lower():
            c = _cand(link.get("URL"), "crossref", force_pdf=True)
            if c:
                out.append(c)
    c = _cand(msg.get("URL"), "crossref")
    if c:
        out.append(c)
    return out


def doi_semanticscholar(doi: str) -> list[Candidate]:
    data = _get_json(S2.format(doi=quote(doi, safe="")))
    pdf = ((data or {}).get("openAccessPdf") or {}).get("url")
    c = _cand(pdf, "semanticscholar", force_pdf=True)
    return [c] if c else []


def _core_key(api_key: str | None) -> str:
    if api_key is not None:
        return api_key
    try:
        from jatai.config import global_settings
        return str(global_settings.load().get("core_api_key", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def doi_core(doi: str, api_key: str | None = None) -> list[Candidate]:
    api_key = _core_key(api_key)
    if not api_key:
        return []
    data = _get_json(CORE, params={"q": f'doi:"{doi}"', "limit": 3},
                     headers={"Authorization": f"Bearer {api_key}"})
    out = []
    for r in (data or {}).get("results") or []:
        for key in ("downloadUrl", "fullTextIdentifier"):
            c = _cand(r.get(key), "core", force_pdf=True)
            if c:
                out.append(c)
    return out


# ---------------- Providers por metadados ----------------

def _title_query(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip())[:200]


def meta_openalex(q: Query) -> list[Candidate]:
    params = {"search": _title_query(q.title), "per-page": 3,
              "select": "doi,title,publication_year,open_access,"
                        "best_oa_location,locations"}
    if q.year:
        params["filter"] = (f"from_publication_date:{q.year - 1}-01-01,"
                            f"to_publication_date:{q.year + 1}-12-31")
    data = _get_json(OPENALEX_SEARCH, params=params)
    out = []
    for w in (data or {}).get("results") or []:
        best = w.get("best_oa_location") or {}
        for key, forced in (("pdf_url", True), ("landing_page_url", False)):
            c = _cand(best.get(key), "openalex", via="meta",
                      force_pdf=forced)
            if c:
                out.append(c)
        for loc in w.get("locations") or []:
            c = _cand(loc.get("pdf_url"), "openalex", via="meta",
                      force_pdf=True)
            if c:
                out.append(c)
    return out


def meta_crossref(q: Query) -> list[Candidate]:
    query = _title_query(q.title)
    if q.authors:
        query += " " + " ".join(q.authors[:3])
    data = _get_json(CROSSREF_SEARCH,
                     params={"query.bibliographic": query, "rows": 3,
                             "select": "DOI,link,URL,title"})
    out = []
    for item in ((data or {}).get("message") or {}).get("items") or []:
        for link in item.get("link") or []:
            if "pdf" in (link.get("content-type") or "").lower():
                c = _cand(link.get("URL"), "crossref", via="meta",
                          force_pdf=True)
                if c:
                    out.append(c)
        c = _cand(item.get("URL"), "crossref", via="meta")
        if c:
            out.append(c)
    return out


def meta_semanticscholar(q: Query) -> list[Candidate]:
    data = _get_json(S2_SEARCH.format(q=quote(_title_query(q.title)),
                                    limit=3))
    out = []
    for p in (data or {}).get("data") or []:
        c = _cand((p.get("openAccessPdf") or {}).get("url"),
                  "semanticscholar", via="meta", force_pdf=True)
        if c:
            out.append(c)
    return out


def meta_arxiv(q: Query) -> list[Candidate]:
    """Busca no arXiv por título; extrai o id e monta a URL do PDF."""
    title = _title_query(q.title)
    if not title:
        return []
    try:
        resp = httpx.get(ARXIV, params={
            "search_query": f'ti:"{title}"', "start": 0,
            "max_results": 3, "sortBy": "relevance",
            "sortOrder": "descending"}, timeout=25, headers=_UA)
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.text)
    except Exception:  # noqa: BLE001
        return []
    out = []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for entry in root.findall("a:entry", ns):
        id_el = entry.find("a:id", ns)
        if id_el is None or id_el.text is None:
            continue
        arxiv_id = id_el.text.rstrip("/").rsplit("/", 1)[-1]
        c = _cand(f"https://arxiv.org/pdf/{arxiv_id}", "arxiv",
                  via="meta", force_pdf=True)
        if c:
            out.append(c)
    return out


def meta_core(q: Query, api_key: str | None = None) -> list[Candidate]:
    api_key = _core_key(api_key)
    if not api_key:
        return []
    query = f'title:"{_title_query(q.title)}"'
    data = _get_json(CORE, params={"q": query, "limit": 3},
                     headers={"Authorization": f"Bearer {api_key}"})
    out = []
    for r in (data or {}).get("results") or []:
        for key in ("downloadUrl", "fullTextIdentifier"):
            c = _cand(r.get(key), "core", via="meta", force_pdf=True)
            if c:
                out.append(c)
    return out


# ---------------- Registry e coleta paralela ----------------

# nome -> {"doi": fn|None, "meta": fn|None}
DOI_PROVIDERS = {
    "unpaywall": lambda doi, email="jatai@localhost", **kw:
        doi_unpaywall(doi, email=email),
    "openalex": lambda doi, **kw: doi_openalex(doi),
    "crossref": lambda doi, **kw: doi_crossref(doi),
    "semanticscholar": lambda doi, **kw: doi_semanticscholar(doi),
    "core": lambda doi, **kw: doi_core(doi),
}
META_PROVIDERS = {
    "openalex": lambda q, **kw: meta_openalex(q),
    "crossref": lambda q, **kw: meta_crossref(q),
    "semanticscholar": lambda q, **kw: meta_semanticscholar(q),
    "arxiv": lambda q, **kw: meta_arxiv(q),
    "core": lambda q, **kw: meta_core(q),
}

# ordem de preferência para desempate de candidatos
PROVIDER_ORDER = ["direct", "unpaywall", "openalex", "crossref",
                  "semanticscholar", "core", "arxiv"]


def _resolve_sources(sources) -> list[str]:
    if sources is None:
        return list(DEFAULT_SOURCES)
    if isinstance(sources, str):
        return [s.strip().lower() for s in sources.split(",") if s.strip()]
    return [s.strip().lower() for s in sources if s.strip()]


def _run_parallel(fns: list) -> list:
    """Executa callables sem argumentos em paralelo; falhas -> []."""
    if not fns:
        return []
    out = []
    with ThreadPoolExecutor(max_workers=min(6, len(fns))) as ex:
        for res in ex.map(lambda f: _safe(f), fns):
            if res:
                out.extend(res)
    return out


def _safe(fn) -> list:
    try:
        return list(fn() or [])
    except Exception:  # noqa: BLE001
        return []


def find_candidates(query: Query, sources=None,
                    email: str = "jatai@localhost") -> list[Candidate]:
    """Descobre candidatos a PDF. Fase DOI primeiro; se nenhum candidato
    for claramente PDF, cai para busca por metadados (título/autores/ano)."""
    names = _resolve_sources(sources)
    kw = {"email": email}

    doi_cands: list[Candidate] = []
    if query.doi:
        fns = []
        for n in names:
            fn = DOI_PROVIDERS.get(n)
            if fn:
                fns.append(lambda f=fn: f(query.doi, **kw))
        doi_cands = _run_parallel(fns)

    meta_cands: list[Candidate] = []
    needs_meta = (not doi_cands) or not any(c.kind == "pdf"
                                            for c in doi_cands)
    if needs_meta and query.title:
        fns = []
        for n in names:
            fn = META_PROVIDERS.get(n)
            if fn:
                fns.append(lambda f=fn: f(query, **kw))
        meta_cands = _run_parallel(fns)

    # dedup por URL (guarda a primeira ocorrência = provider de maior
    # prioridade, já que names segue DEFAULT_SOURCES)
    seen, out = set(), []
    for c in doi_cands + meta_cands:
        if c.url in seen:
            continue
        seen.add(c.url)
        out.append(c)

    def rank(c: Candidate):
        kind_rank = 0 if c.kind == "pdf" else (2 if c.kind == "landing" else 1)
        prov_rank = (PROVIDER_ORDER.index(c.source)
                     if c.source in PROVIDER_ORDER else 99)
        via_rank = 0 if c.via == "doi" else 1
        return (kind_rank, via_rank, prov_rank, c.url)

    out.sort(key=rank)
    return out


def find_meta_candidates(query: Query, sources=None,
                         email: str = "jatai@localhost") -> list[Candidate]:
    """Apenas a fase de metadados (título/autores/ano)."""
    names = _resolve_sources(sources)
    fns = []
    for n in names:
        fn = META_PROVIDERS.get(n)
        if fn:
            fns.append(lambda f=fn: f(query, email=email))
    out = _run_parallel(fns)
    seen, dedup = set(), []
    for c in out:
        if c.url not in seen:
            seen.add(c.url)
            dedup.append(c)
    dedup.sort(key=lambda c: (0 if c.kind == "pdf" else 1,
                              PROVIDER_ORDER.index(c.source)
                              if c.source in PROVIDER_ORDER else 99,
                              c.url))
    return dedup


__all__ = ["Query", "Candidate", "find_candidates",
           "find_meta_candidates", "doi_unpaywall",
           "doi_openalex", "doi_crossref", "doi_semanticscholar",
           "doi_core", "meta_openalex", "meta_crossref",
           "meta_semanticscholar", "meta_arxiv", "meta_core",
           "DOI_PROVIDERS", "META_PROVIDERS", "DEFAULT_SOURCES"]
