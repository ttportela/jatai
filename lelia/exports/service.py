"""Exportação do catálogo: CSV, BibTeX, RIS e fluxo PRISMA."""

import csv
import io
import json
import re
from datetime import datetime

from sqlalchemy import func, select

from lelia.storage.db import get_project_session
from lelia.storage.models_project import Paper, Query


def _papers(slug: str, state: str = "") -> list[Paper]:
    with get_project_session(slug) as s:
        stmt = select(Paper).order_by(Paper.id)
        if state:
            stmt = stmt.where(Paper.state == state)
        return list(s.scalars(stmt))


def _authors(p: Paper) -> list[str]:
    try:
        return json.loads(p.authors or "[]")
    except json.JSONDecodeError:
        return []


def _bibtex_type(p: Paper) -> str:
    t = (p.pub_type or "").lower()
    if "article" in t or "journal" in t:
        return "article"
    if "proceeding" in t or "conference" in t or "inproceed" in t:
        return "inproceedings"
    if "book" in t:
        return "book"
    if "thesis" in t:
        return "mastersthesis"
    return "misc"


def _citekey(p: Paper) -> str:
    authors = _authors(p)
    last = re.sub(r"[^A-Za-z]", "", authors[0].split()[-1]) if authors else "anon"
    year = p.year or "nd"
    first = re.sub(r"[^A-Za-z0-9]", "", p.title.split()[0]) if p.title.split() else "x"
    return f"{last.lower()}{year}{first.lower()}"


def _bibtex_escape(s: str) -> str:
    return s.replace("{", "").replace("}", "")


def to_bibtex(slug: str, state: str = "") -> str:
    out = ["% Exportado do Lelia"]
    for p in _papers(slug, state):
        authors = " and ".join(_authors(p))
        fields = [
            ("title", _bibtex_escape(p.title)),
            ("author", authors),
            ("year", str(p.year or "")),
            ("journal", p.venue),
            ("doi", p.doi),
            ("url", p.url),
            ("abstract", _bibtex_escape(p.abstract[:2000])),
        ]
        body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields if v)
        out.append(f"@{_bibtex_type(p)}{{{_citekey(p)},\n{body}\n}}")
    return "\n\n".join(out) + "\n"


def to_ris(slug: str, state: str = "") -> str:
    lines = []
    for p in _papers(slug, state):
        tmap = {"article": "JOUR", "inproceedings": "CONF", "book": "BOOK"}
        lines.append("TY  - " + tmap.get(_bibtex_type(p), "GEN"))
        lines.append(f"TI  - {p.title}")
        for a in _authors(p):
            parts = a.split()
            last = parts[-1] if parts else a
            first = " ".join(parts[:-1])
            lines.append(f"AU  - {last}, {first}")
        if p.year:
            lines.append(f"PY  - {p.year}")
        if p.venue:
            lines.append(f"JO  - {p.venue}" if _bibtex_type(p) == "article" else f"BT  - {p.venue}")
        if p.doi:
            lines.append(f"DO  - {p.doi}")
        if p.url:
            lines.append(f"UR  - {p.url}")
        if p.abstract:
            lines.append(f"AB  - {p.abstract[:1900]}")
        lines.append("ER  - ")
    return "\n".join(lines) + "\n"


def to_csv(slug: str, state: str = "") -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "title", "authors", "year", "doi", "venue", "type", "source",
                "state", "url", "pdf_path", "abstract"])
    for p in _papers(slug, state):
        w.writerow([p.id, p.title, "; ".join(_authors(p)), p.year or "", p.doi,
                    p.venue, p.pub_type, p.source, p.state, p.url, p.pdf_path,
                    p.abstract[:500]])
    return buf.getvalue()


def _flow_counts(slug: str) -> dict:
    with get_project_session(slug) as s:
        total_q = s.scalar(select(func.count(Query.id))) or 0
        executed = s.scalar(select(func.count(Query.id)).where(Query.status == "executada")) or 0
        identified = s.scalar(select(func.count(Paper.id))) or 0
        screened = s.scalar(select(func.count(Paper.id)).where(
            Paper.state.in_(["incluido", "excluido", "talvez"]))) or 0
        included = s.scalar(select(func.count(Paper.id)).where(Paper.state == "incluido")) or 0
        with_pdf = s.scalar(select(func.count(Paper.id)).where(Paper.pdf_path != "")) or 0
    return {"queries": total_q, "executed_queries": executed,
            "identified": identified, "screened": screened,
            "included": included, "with_pdf": with_pdf}


def to_prisma(slug: str, title: str = "") -> str:
    c = _flow_counts(slug)
    lines = [
        "# Fluxo PRISMA",
        "",
        f"**Projeto:** {title or slug}",
        f"**Gerado em:** {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        "",
        "## Identificação",
        f"- Registros identificados nas fontes: **{c['identified']}**",
        f"- Consultas executadas: {c['executed_queries']} de {c['queries']}",
        "",
        "## Triagem",
        f"- Registros avaliados (título/resumo): **{c['screened']}**",
        f"- Incluídos: **{c['included']}**",
        f"- Com texto completo obtido: {c['with_pdf']}",
        "",
        "## Fluxo",
        "```",
        f"Consultas ({c['queries']}) → Resultados brutos ({c['identified']})",
        f"  → Triagem título/resumo ({c['screened']}) → Incluídos ({c['included']})",
        "```",
    ]
    return "\n".join(lines) + "\n"



def to_json(slug: str, state: str = "") -> str:
    """Exportação estruturada completa: papers + metadados + classificações."""
    from lelia.storage.models_project import PaperAnalysis, Decision, Evidence

    papers = []
    with get_project_session(slug) as s:
        stmt = select(Paper).order_by(Paper.id)
        if state:
            stmt = stmt.where(Paper.state == state)
        rows = list(s.scalars(stmt))
        for p in rows:
            analyses = list(s.scalars(select(PaperAnalysis).where(
                PaperAnalysis.paper_id == p.id)))
            decisions = list(s.scalars(select(Decision).where(
                Decision.paper_id == p.id).order_by(Decision.id)))
            evidences = list(s.scalars(select(Evidence).where(
                Evidence.paper_id == p.id)))
            papers.append({
                "id": p.id, "title": p.title, "authors": _authors(p),
                "year": p.year, "doi": p.doi, "venue": p.venue,
                "type": p.pub_type, "source": p.source, "state": p.state,
                "url": p.url, "keywords": p.keywords, "abstract": p.abstract,
                "has_pdf": bool(p.pdf_path),
                "analyses": [{"kind": a.kind, "result": a.result} for a in analyses],
                "decisions": [{"verdict": d.verdict, "justification": d.justification,
                               "origin": d.origin, "criterion": d.criterion_code}
                              for d in decisions],
                "evidences": [e.excerpt for e in evidences],
            })
    return json.dumps({"project": slug, "exported_at": datetime.now().isoformat(),
                       "count": len(papers), "papers": papers},
                      ensure_ascii=False, indent=2)

FORMATS = {
    "bibtex": ("text/plain", ".bib", to_bibtex),
    "ris": ("text/plain", ".ris", to_ris),
    "csv": ("text/csv", ".csv", to_csv),
    "prisma": ("text/markdown", ".md", to_prisma),
    "json": ("application/json", ".json", to_json),
}


def export(slug: str, fmt: str, state: str = "") -> tuple[str, str, str] | None:
    """Retorna (conteúdo, media_type, filename) ou None."""
    if fmt not in FORMATS:
        return None
    media, ext, fn = FORMATS[fmt]
    content = fn(slug, state) if fmt != "prisma" else fn(slug, "")
    fname = f"lelia-{slug}-{fmt}-{datetime.now():%Y%m%d}.{ext.lstrip('.')}"
    return content, media, fname

