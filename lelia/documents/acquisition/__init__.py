"""Aquisição de texto completo respeitando copyright.

Pipeline: descoberta (providers OA) → ordenação/classificação →
download httpx → fallback Chromium → fallback autenticado.
Nunca contorna paywalls; a sessão autenticada usa o login manual do
próprio usuário (`lelia pdf-login`).
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from lelia.audit.log import record_event
from lelia.documents.acquisition import providers as _prov
from lelia.documents.acquisition.browser import (
    auth_profile_dir,
    close_auth_context,
    download_pdf_browser,
    download_pdf_context,
    get_auth_context,
    looks_like_pdf,
    open_login_window,
)
from lelia.documents.acquisition.providers import (
    Candidate,
    Query,
    find_candidates,
    find_meta_candidates,
)
from lelia.documents.acquisition.validate import (
    is_pdf_bytes,
    looks_like_pdf as _looks_like_pdf_url,
    validate_pdf_file,
)

_UA = {"User-Agent": "lelia/0.1 (research agent; mailto:lelia@localhost)"}

# Registry legado mantido para compatibilidade/testes: nome -> fn que
# recebe DOI (e email p/ unpaywall) e retorna URLs candidatas.
_SOURCES = {
    "unpaywall": lambda doi, email="lelia@localhost":
        [c.url for c in _prov.doi_unpaywall(doi, email=email)],
    "openalex": lambda doi: [c.url for c in _prov.doi_openalex(doi)],
    "crossref": lambda doi: [c.url for c in _prov.doi_crossref(doi)],
    "semanticscholar": lambda doi:
        [c.url for c in _prov.doi_semanticscholar(doi)],
    "core": lambda doi: [c.url for c in _prov.doi_core(doi)],
}


def _looks_like_pdf(url: str) -> bool:
    return _looks_like_pdf_url(url)


_is_pdf_bytes = is_pdf_bytes


def find_oa_pdf(doi: str, sources: str | list[str] | None = None,
                email: str = "lelia@localhost") -> list[str]:
    """Retorna URLs candidatas a PDF open-access para o DOI, em ordem.

    Compat: delega aos providers por DOI em paralelo e achata em URLs.
    """
    if not doi:
        return []
    if sources is None:
        names = list(_prov.DEFAULT_SOURCES)
    elif isinstance(sources, str):
        names = [s.strip().lower() for s in sources.split(",") if s.strip()]
    else:
        names = [s.strip().lower() for s in sources if s.strip()]

    def call(name):
        fn = _SOURCES.get(name)
        if fn is None:
            return []
        try:
            return fn(doi, email) if name == "unpaywall" else fn(doi)
        except Exception:  # noqa: BLE001
            return []

    results: list[list[str]] = [[] for _ in names]
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(names)))) as ex:
        futs = {ex.submit(call, n): i for i, n in enumerate(names)}
        for fut, i in futs.items():
            try:
                results[i] = list(fut.result() or [])
            except Exception:  # noqa: BLE001
                results[i] = []

    out: list[str] = []
    for urls in results:
        for u in urls:
            if u and u not in out:
                out.append(u)
    out.sort(key=lambda u: 0 if _looks_like_pdf(u) else 1)
    return out


def download_pdf(url: str, dest: Path, timeout: float = 60.0) -> bool:
    """Baixa um PDF verificando o content-type/tamanho. Retorna sucesso."""
    try:
        with httpx.stream("GET", url, timeout=timeout,
                          headers=_UA,
                          follow_redirects=True) as resp:
            if resp.status_code != 200:
                return False
            ctype = resp.headers.get("content-type", "")
            chunks = []
            size = 0
            for chunk in resp.iter_bytes(65536):
                chunks.append(chunk)
                size += len(chunk)
                if size > 50 * 1024 * 1024:
                    return False
            data = b"".join(chunks)
        if not is_pdf_bytes(data, ctype):
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True
    except httpx.HTTPError:
        return False


def _save(slug: str, paper_id: int, workspace: str, url: str,
          origin: str, downloader=None,
          expected_title: str | None = None) -> str | None:
    """Baixa (ou reutiliza arquivo já válido) e persiste pdf_path +
    PaperFile. Retorna o caminho ou None."""
    from lelia.storage.db import get_project_session
    from lelia.storage.models_project import Paper, PaperFile

    safe = re.sub(r"[^A-Za-z0-9._-]", "_",
                  url.split("?")[0].split("#")[0].split("/")[-1]) \
        or f"{paper_id}.pdf"
    if not safe.lower().endswith(".pdf"):
        safe += ".pdf"
    dest = Path(workspace) / "pdfs" / f"{paper_id}-{origin}-{safe}"

    # recuperação: arquivo já baixado e válido -> só vincula
    if dest.exists() and validate_pdf_file(dest, expected_title)["ok"]:
        pass
    else:
        dest.unlink(missing_ok=True)
        if not (downloader or download_pdf)(url, dest):
            return None
        check = validate_pdf_file(dest, expected_title)
        if not check["ok"]:
            dest.unlink(missing_ok=True)
            return None

    with get_project_session(slug) as s:
        p = s.get(Paper, paper_id)
        if p:
            p.pdf_path = str(dest)
            exists = s.query(PaperFile).filter_by(
                paper_id=paper_id, path=str(dest)).first()
            if not exists:
                s.add(PaperFile(paper_id=paper_id, path=str(dest),
                                kind="pdf", origin=origin))
            s.commit()
    record_event("paper.pdf_oa", entity=f"paper:{paper_id}", operation="acquire",
                 project_slug=slug, payload={"url": url, "file": dest.name,
                                             "origin": origin})
    return str(dest)


def _paper_query(slug: str, paper_id: int):
    from lelia.storage.db import get_project_session
    from lelia.storage.models_project import Paper
    from sqlalchemy import select

    with get_project_session(slug) as s:
        p = s.scalar(select(Paper).where(Paper.id == paper_id))
        if p is None:
            return None
        try:
            authors = tuple(json.loads(p.authors or "[]"))
        except (json.JSONDecodeError, TypeError):
            authors = ()
        return _prov.Query(doi=(p.doi or "").strip(), title=p.title or "",
                           authors=authors, year=p.year), \
            (p.url or "").strip(), bool(p.pdf_path)


def acquire_for_paper(slug: str, paper_id: int, workspace: str,
                      sources: str | list[str] | None = None,
                      browser=None,
                      browser_factory=None,
                      authenticated: bool = False,
                      auth_context=None) -> str | None:
    """Tenta obter o PDF do artigo. Retorna path ou None.

    Ordem: (1) URL do paper se já for PDF; (2) candidatos OA por DOI
    (fontes configuráveis, em paralelo); (3) fallback por metadados
    título/autores/ano quando o DOI não retorna PDF; (4) Chromium para
    desafios JS; (5) sessão autenticada persistente, se habilitada.
    """
    loaded = _paper_query(slug, paper_id)
    if loaded is None:
        return None
    query, paper_url, has_pdf = loaded
    if has_pdf:
        return None

    candidates: list[_prov.Candidate] = []
    if paper_url and _looks_like_pdf(paper_url):
        candidates.append(_prov.Candidate(paper_url, "direct",
                                          "pdf", "direct"))
    for u in find_oa_pdf(query.doi, sources=sources):
        candidates.append(_prov.Candidate(
            u, "oa", "pdf" if _looks_like_pdf(u) else "landing", "doi"))
    # fallback por metadados quando o DOI não rendeu PDF
    if not any(c.kind == "pdf" for c in candidates) and query.title:
        for c in find_meta_candidates(query, sources=sources):
            candidates.append(c)

    # dedup por URL preservando ordem
    seen, uniq = set(), []
    for c in candidates:
        if c.url not in seen:
            seen.add(c.url)
            uniq.append(c)
    candidates = uniq[:14]

    def origin_of(c: _prov.Candidate) -> str:
        if c.via == "meta":
            return "meta"
        return "direct" if c.source == "direct" else "oa"

    for c in candidates:
        path = _save(slug, paper_id, workspace, c.url, origin_of(c),
                     expected_title=query.title)
        if path:
            return path

    # fallback Chromium (lazy)
    if browser is None and browser_factory is not None:
        try:
            browser = browser_factory()
        except Exception:  # noqa: BLE001
            browser = None
    if browser is not None:
        for c in candidates[:6]:
            path = _save(
                slug, paper_id, workspace, c.url, origin_of(c),
                downloader=lambda u, d: download_pdf_browser(
                    browser, u, d),
                expected_title=query.title)
            if path:
                return path

    # última tentativa: sessão autenticada persistente
    if authenticated:
        ctx = auth_context
        own_pw = None
        if ctx is None:
            own_pw, ctx = get_auth_context()
        if ctx is not None:
            try:
                for c in candidates[:6]:
                    path = _save(
                        slug, paper_id, workspace, c.url,
                        origin_of(c) + "-auth",
                        downloader=lambda u, d: download_pdf_context(
                            ctx, u, d),
                        expected_title=query.title)
                    if path:
                        return path
            finally:
                if own_pw is not None:
                    close_auth_context()
    return None


# re-exports de compatibilidade
__all__ = ["acquire_for_paper", "find_oa_pdf", "find_candidates",
           "download_pdf", "download_pdf_browser", "download_pdf_context",
           "get_auth_context", "close_auth_context", "open_login_window",
           "auth_profile_dir", "looks_like_pdf", "is_pdf_bytes",
           "validate_pdf_file", "Query", "Candidate",
           "DEFAULT_SOURCES"]
DEFAULT_SOURCES = _prov.DEFAULT_SOURCES
_SOURCES = _SOURCES  # noqa: F811 — registry usado por find_oa_pdf/testes
