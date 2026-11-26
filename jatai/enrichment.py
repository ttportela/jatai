"""Enriquecimento de metadados via scraping com Chromium (Playwright).

Busca abstract e palavras-chave de artigos que vieram das fontes sem
esses campos, navegando até a URL do artigo (ou doi.org) e extraindo
meta tags e trechos de texto comuns.
"""

import json
import re
import urllib.parse
import urllib.request

from sqlalchemy import or_, select

from jatai.storage.db import get_project_session
from jatai.storage.models_project import Paper

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# seletores/tags tentados em ordem para abstract e keywords
_ABSTRACT_SELECTORS = [
    'meta[name="citation_abstract"]',
    'meta[name="description"]',
    'meta[name="DC.description"]',
    'meta[property="og:description"]',
    "#abstract", ".abstract", "section.abstract", ".article-abstract",
    "div.abstract", "[class*='abstract']",
]
_KEYWORDS_SELECTORS = [
    'meta[name="citation_keywords"]',
    'meta[name="keywords"]',
    'meta[name="DC.subject"]',
    ".keywords", "#keywords", "[class*='keyword']",
]

_JS_EXTRACT = """
(cfg) => {
  function meta(name) {
    const el = document.querySelector('meta[name="' + name + '"], meta[property="' + name + '"]');
    return el ? (el.getAttribute('content') || '').trim() : '';
  }
  function pick(selectors) {
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el) {
        const txt = (el.getAttribute('content') || el.innerText || '').trim();
        if (txt.length > 20) return txt.slice(0, 4000);
      }
    }
    return '';
  }
  return {
    abstract: meta('citation_abstract') || meta('description') || meta('DC.description')
              || meta('og:description') || pick(cfg.abstractSelectors),
    keywords: meta('citation_keywords') || meta('keywords') || meta('DC.subject')
              || pick(cfg.keywordSelectors),
    title: document.title || '',
  };
}
"""


def _launch():
    """Retorna (playwright compartilhado da thread, browser)."""
    from jatai import browser as _browser
    return _browser.get_playwright(), _browser.launch_browser()


def _target_url(paper: Paper) -> str:
    if paper.url:
        return paper.url
    if paper.doi:
        return f"https://doi.org/{paper.doi}"
    return ""


def _clean_keywords(raw: str) -> str:
    parts = [p.strip() for p in re.split(r"[,;]|\band\b", raw, flags=re.IGNORECASE) if p.strip()]
    # remove frases longas que provavelmente são descrição, não keywords
    parts = [p for p in parts if len(p) <= 60]
    return ", ".join(dict.fromkeys(parts))[:1000]


_OPENALEX_MAILTO = "jatai@localhost"


def _http_json(url: str, timeout: int = 20) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _openalex_abstract(inv: dict) -> str:
    if not inv:
        return ""
    pos = {}
    for word, positions in inv.items():
        for p in positions:
            pos[p] = word
    return " ".join(pos[i] for i in sorted(pos))


def _clean_crossref_abstract(raw: str) -> str:
    # Crossref devolve abstract em JATS/XML — extrai só o texto
    txt = re.sub(r"<[^>]+>", " ", raw or "")
    return re.sub(r"\s+", " ", txt).strip()


def enrich_via_api(paper: Paper) -> tuple[dict, str]:
    """Busca abstract/keywords via APIs de metadados (OpenAlex, Crossref).

    Muito mais confiável que scraping para DOIs de publishers que bloqueiam
    bots (IEEE, Springer, ACM). Retorna (campos, status) como enrich_paper.
    """
    if not paper.doi:
        return {}, "sem_url"
    doi = urllib.parse.quote(paper.doi.strip(), safe="")
    out: dict = {}

    oa = _http_json(f"https://api.openalex.org/works/doi:{doi}?mailto={_OPENALEX_MAILTO}")
    if oa:
        if not paper.abstract:
            abs_text = _openalex_abstract(oa.get("abstract_inverted_index") or {})
            if abs_text:
                out["abstract"] = abs_text[:8000]
        if not paper.keywords:
            kws = [k.get("display_name", "") for k in oa.get("keywords", [])]
            if not kws:
                kws = [t.get("display_name", "") for t in oa.get("topics", [])[:5]]
            kws = [k for k in kws if k]
            if kws:
                out["keywords"] = ", ".join(dict.fromkeys(kws))[:1000]

    # Crossref cobre abstracts que o OpenAlex não tem (JATS)
    missing_abs = "abstract" not in out
    missing_kw = "keywords" not in out
    if missing_abs or missing_kw:
        cr = _http_json(f"https://api.crossref.org/works/{doi}")
        msg = (cr or {}).get("message") or {}
        if msg:
            if missing_abs:
                abs_text = _clean_crossref_abstract(msg.get("abstract") or "")
                if len(abs_text) > 40:
                    out["abstract"] = abs_text[:8000]
            if missing_kw:
                subs = [s for s in (msg.get("subject") or []) if isinstance(s, str)]
                if subs:
                    out["keywords"] = _clean_keywords("; ".join(subs))

    if not out:
        return {}, "vazio"
    return out, ("ok" if len(out) == 2 else "parcial")


def enrich_paper(browser, paper: Paper) -> tuple[dict, str]:
    """Navega até o artigo e extrai abstract/keywords.

    Retorna (campos_atualizados, status):
    ok | parcial | vazio | sem_url | erro.
    """
    url = _target_url(paper)
    if not url:
        return {}, "sem_url"
    page = browser.new_page(user_agent=USER_AGENT)
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        data = page.evaluate(_JS_EXTRACT, {
            "abstractSelectors": _ABSTRACT_SELECTORS,
            "keywordSelectors": _KEYWORDS_SELECTORS,
        })
    except Exception:
        return {}, "erro"
    finally:
        page.close()

    out = {}
    abstract = (data.get("abstract") or "").strip()
    if abstract and not paper.abstract:
        out["abstract"] = abstract[:8000]
    keywords = _clean_keywords(data.get("keywords") or "")
    if keywords and not paper.keywords:
        out["keywords"] = keywords
    if not out:
        return {}, "vazio"
    return out, ("ok" if len(out) == 2 else "parcial")


def enrich_missing(slug: str, limit: int | None = 20, on_progress=None,
                   should_stop=None) -> dict:
    """Enriquece até `limit` artigos sem abstract ou keywords.

    Ignora artigos excluídos. `on_progress(idx, total, paper_id, title,
    status, found_fields)` é chamado a cada artigo processado.
    """
    with get_project_session(slug) as s:
        base = [
            or_(Paper.abstract == "", Paper.keywords == ""),
            Paper.state != "excluido",
        ]
        q1 = (select(Paper).where(*base, Paper.url != "")
              .order_by(Paper.id.desc()))
        if limit:
            q1 = q1.limit(limit)
        papers = list(s.scalars(q1))
        # inclui papers com DOI mas sem URL
        if limit is None or len(papers) < limit:
            have = {p.id for p in papers}
            q2 = (select(Paper)
                  .where(*base, Paper.url == "", Paper.doi != "")
                  .order_by(Paper.id.desc()))
            if limit is not None:
                q2 = q2.limit(limit - len(papers))
            papers += [p for p in s.scalars(q2) if p.id not in have]
        ids = [p.id for p in papers]
        snap = {p.id: p for p in papers}

    if not ids:
        return {"processed": 0, "updated": 0}

    pw, browser = _launch()
    updated = errors = 0
    try:
        for idx, pid in enumerate(ids, 1):
            if should_stop is not None and should_stop():
                break
            paper = snap[pid]
            if paper.doi:
                # DOI: APIs de metadados primeiro (rápido e confiável);
                # scraping só se houver URL real (não doi.org) como fallback
                fields, status = enrich_via_api(paper)
                if not fields and paper.url and "doi.org" not in paper.url:
                    bfields, bstatus = enrich_paper(browser, paper)
                    if bfields:
                        fields = {**fields, **{k: v for k, v in bfields.items()
                                               if k not in fields}}
                        status = "ok" if len(fields) == 2 else "parcial"
                    elif status == "vazio":
                        status = bstatus
            elif paper.url:
                fields, status = enrich_paper(browser, paper)
            else:
                fields, status = {}, "sem_url"
            found = [k for k in ("abstract", "keywords") if k in fields]
            if status == "erro":
                errors += 1
            if fields:
                with get_project_session(slug) as s:
                    p = s.get(Paper, pid)
                    if p:
                        for k, v in fields.items():
                            setattr(p, k, v)
                        s.commit()
                        updated += 1
            if on_progress:
                on_progress(idx, len(ids), pid, paper.title, status, found)
    finally:
        try:
            browser.close()
        except Exception:
            pass
    return {"processed": len(ids), "updated": updated, "errors": errors}


def missing_metadata_count(slug: str) -> int:
    with get_project_session(slug) as s:
        return len(list(s.scalars(
            select(Paper.id)
            .where(or_(Paper.abstract == "", Paper.keywords == ""),
                   Paper.state != "excluido",
                   or_(Paper.url != "", Paper.doi != "")))))


__all__ = ["enrich_missing", "missing_metadata_count", "enrich_paper"]
