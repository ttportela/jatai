"""Ingestão, deduplicação e consulta do catálogo de publicações."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.orm import selectinload

from jatai.audit.log import record_event
from jatai.dedup.normalize import dedup_key, normalize_doi, titles_similar
from jatai.sources.base import RawRecord, get_connector
from jatai.storage.db import get_project_session
from jatai.storage.models_project import (
    DedupGroup,
    HumanIntervention,
    Paper,
    PaperFile,
    PaperSighting,
    Query,
    QualityScore,
)

PAGE_SIZE = 25


def _existing_keys(session) -> dict[tuple[str, str], int]:
    """Mapa (tipo, valor_normalizado) -> paper.id para dedup."""
    keys: dict[tuple[str, str], int] = {}
    for p in session.scalars(select(Paper)):
        if p.doi:
            keys[("doi", normalize_doi(p.doi))] = p.id
        keys[("title", f"{re.sub(r'[^a-z0-9 ]', '', p.title.lower())}|{p.year or ''}")] = p.id
    return keys


def _find_duplicate(session, rec: RawRecord) -> Paper | None:
    kind, value = dedup_key(rec.doi, rec.title, rec.year)
    if kind == "doi":
        row = session.scalar(select(Paper).where(Paper.doi != "").where(
            func.lower(Paper.doi) == value))
        # compara normalizado (strip de prefixo) via python para simplicidade
        for p in session.scalars(select(Paper).where(Paper.doi != "")):
            if normalize_doi(p.doi) == value:
                return p
        return None
    # fallback fuzzy por título+ano
    for p in session.scalars(select(Paper)):
        if p.year == rec.year and titles_similar(p.title, rec.title):
            return p
    return None


def _ensure_fts(s) -> None:
    s.execute(text(
        "CREATE VIRTUAL TABLE IF NOT EXISTS paper_fts USING fts5("
        "title, authors, abstract)"))


def _fts_upsert(s, paper: Paper) -> None:
    _ensure_fts(s)
    s.execute(text("DELETE FROM paper_fts WHERE rowid = :rid"), {"rid": paper.id})
    s.execute(text(
        "INSERT INTO paper_fts(rowid, title, authors, abstract) VALUES(:rid,:t,:a,:b)"),
        {"rid": paper.id, "t": paper.title or "", "a": paper.authors or "",
         "b": (paper.abstract or "")[:4000]})


def fts_backfill(slug: str) -> int:
    """Garante tabela FTS e indexa papers ainda não indexados."""
    with get_project_session(slug) as s:
        _ensure_fts(s)
        indexed = {r[0] for r in s.execute(text("SELECT rowid FROM paper_fts"))}
        missing = [p for p in s.scalars(select(Paper)) if p.id not in indexed]
        for p in missing:
            _fts_upsert(s, p)
        s.commit()
        return len(missing)


def ingest_records(slug: str, source_type: str, query_id: int | None,
                   records: list[RawRecord],
                   seed: dict | None = None) -> tuple[int, int]:
    """Insere registros novos e vincula sightings. Retorna (novos, duplicados).

    `seed` (snowballing): {"id", "title", "doi"} do artigo semente — cada
    publicação importada ganha um PaperSighting extra com source="snowball"
    registrando de qual artigo ela veio."""
    new_count = dup_count = 0
    seed_raw = ""
    if seed:
        seed_raw = json.dumps(
            {"seed_paper_id": seed.get("id"),
             "seed_title": seed.get("title", ""),
             "seed_doi": seed.get("doi", "")},
            ensure_ascii=False)[:4000]
    with get_project_session(slug) as s:
        for rec in records:
            paper = _find_duplicate(s, rec)
            if paper is None:
                paper = Paper(
                    title=rec.title,
                    authors=json.dumps(rec.authors, ensure_ascii=False),
                    year=rec.year,
                    doi=normalize_doi(rec.doi),
                    abstract=rec.abstract,
                    source=source_type,
                    url=rec.url,
                    venue=rec.venue,
                    pub_type=rec.pub_type,
                    keywords=", ".join(rec.keywords),
                    state="novo",
                )
                s.add(paper)
                s.flush()
                _fts_upsert(s, paper)
                new_count += 1
            else:
                dup_count += 1
            s.add(PaperSighting(
                paper_id=paper.id,
                query_id=query_id,
                source=source_type,
                external_id=rec.external_id,
                raw=json.dumps(rec.raw, ensure_ascii=False, default=str)[:8000],
            ))
            if seed:
                s.add(PaperSighting(
                    paper_id=paper.id,
                    query_id=None,
                    source="snowball",
                    external_id=f"#{seed.get('id', '')}",
                    raw=seed_raw,
                ))
        s.commit()
    if new_count or dup_count:
        record_event("catalog.ingest", entity="paper", operation="ingest", project_slug=slug,
                     payload={"source": source_type, "query_id": query_id,
                              "new": new_count, "duplicates": dup_count})
    return new_count, dup_count


def create_manual_paper(slug: str, fields: dict) -> int | None:
    """Cria publicação manual. Retorna o id do paper (novo ou duplicado)."""
    title = str(fields.get("title", "")).strip()
    if not title:
        return None
    year_raw = str(fields.get("year", "")).strip()
    try:
        year = int(year_raw) if year_raw else None
    except ValueError:
        year = None
    authors_raw = str(fields.get("authors", "")).strip()
    authors = [a.strip() for a in re.split(r"[,;]|\band\b", authors_raw, flags=re.I)
               if a.strip()] if authors_raw else []
    rec = RawRecord(
        title=title,
        authors=authors,
        year=year,
        doi=str(fields.get("doi", "")).strip(),
        abstract=str(fields.get("abstract", "")).strip(),
        url=str(fields.get("url", "")).strip(),
        venue=str(fields.get("venue", "")).strip(),
        pub_type=str(fields.get("pub_type", "")).strip(),
        keywords=[k.strip() for k in str(fields.get("keywords", "")).split(",")
                  if k.strip()],
        raw={"origin": "manual"},
    )
    ingest_records(slug, "manual", None, [rec])
    # o paper criado (ou o duplicado encontrado pela dedup) casa com a mesma chave
    doi = normalize_doi(rec.doi)
    with get_project_session(slug) as s:
        papers = list(s.scalars(select(Paper)))
    if doi:
        for p in papers:
            if normalize_doi(p.doi) == doi:
                return p.id
    for p in papers:
        if titles_similar(p.title, title):
            return p.id
    return None


def _coverage_bound(value: str, default_month: int) -> tuple[int, int] | None:
    """'YYYY', 'YYYY-MM' -> (ano, mês). Mês ausente usa o default
    (1 para início, 12 para fim)."""
    s = str(value or "").strip()
    m = re.match(r"^(\d{4})(?:-(\d{1,2}))?$", s)
    if not m:
        return None
    year = int(m.group(1))
    month = int(m.group(2)) if m.group(2) else default_month
    if not 1 <= month <= 12:
        return None
    return year, month


def _coverage_years(slug: str) -> str:
    """Período de cobertura como range de anos para APIs que filtram
    por ano ('2010-2020', '2010-', '-2020')."""
    from jatai.research import service as research

    cfg = research.get_config(slug)
    s = _coverage_bound(cfg.get("coverage_start", ""), 1)
    e = _coverage_bound(cfg.get("coverage_end", ""), 12)
    if not s and not e:
        return ""
    sy = s[0] if s else None
    ey = e[0] if e else None
    if sy and ey:
        return str(sy) if sy == ey else f"{sy}-{ey}"
    return f"{sy}-" if sy else f"-{ey}"


def _filter_by_coverage(slug: str, records: list) -> list:
    """Restringe registros ao período de cobertura (coverage_start/end,
    'AAAA' ou 'AAAA-MM') do projeto."""
    from jatai.research import service as research

    cfg = research.get_config(slug)
    start = _coverage_bound(cfg.get("coverage_start", ""), 1)
    end = _coverage_bound(cfg.get("coverage_end", ""), 12)
    if not start and not end:
        return records
    out = []
    for rec in records:
        y = rec.year
        if y is None:
            out.append(rec)  # sem ano: mantém (não dá para descartar com segurança)
            continue
        m = rec.month if 1 <= (rec.month or 0) <= 12 else None
        if m is not None:
            ym = (y, m)
            if start and ym < start:
                continue
            if end and ym > end:
                continue
        else:
            # sem mês: compara só o ano — mantém os anos de fronteira
            if start and y < start[0]:
                continue
            if end and y > end[0]:
                continue
        out.append(rec)
    return out


def _record_execution(slug: str, query_id: int, source: str, expression: str,
                      connector, status: str, t0: float, fetched: int,
                      filtered: int, new: int, dup: int, pages: int,
                      error: str, run_id: int | None, iteration: int | None) -> None:
    """Persiste um QueryExecution com a trilha completa da chamada à fonte."""
    import time as _time

    from jatai.storage.models_project import QueryExecution

    req = getattr(connector, "last_request", {}) or {}
    try:
        req_json = json.dumps({"url": req.get("url", ""),
                               "params": req.get("params", {}),
                               "pages": req.get("pages", pages),
                               "partial": req.get("partial", False),
                               "warning": req.get("warning", "")},
                              ensure_ascii=False)[:4000]
    except (TypeError, ValueError):
        req_json = ""
    with get_project_session(slug) as s:
        s.add(QueryExecution(
            query_id=query_id, run_id=run_id, iteration=iteration,
            source=source, expression=expression[:2000], request=req_json,
            status=status,
            duration_ms=int((_time.time() - t0) * 1000),
            results_count=fetched, new_count=new, duplicate_count=dup,
            filtered_count=max(0, fetched - filtered), pages=pages,
            error=error[:1000]))
        s.commit()


def query_executions(slug: str, query_id: int, limit: int = 10) -> list:
    """Últimas execuções registradas de uma consulta (mais recente primeiro)."""
    from jatai.storage.models_project import QueryExecution

    with get_project_session(slug) as s:
        return list(s.scalars(
            select(QueryExecution)
            .where(QueryExecution.query_id == query_id)
            .order_by(QueryExecution.id.desc()).limit(limit)))


def executions_by_query(slug: str, per_query: int = 5) -> dict[int, list]:
    """Mapa query_id -> últimas execuções (para a tela de consultas)."""
    from jatai.storage.models_project import QueryExecution

    with get_project_session(slug) as s:
        rows = list(s.scalars(
            select(QueryExecution).order_by(QueryExecution.id.desc()).limit(500)))
    out: dict[int, list] = {}
    for r in rows:
        if len(out.setdefault(r.query_id, [])) < per_query:
            out[r.query_id].append(r)
    return out


def execute_query(slug: str, query_id: int, max_results: int = 200,
                  run_id: int | None = None, iteration: int | None = None,
                  should_stop=None) -> dict:
    """Executa uma Query nas fontes habilitadas (conectores disponíveis).

    Registra um QueryExecution por fonte (expressão enviada, parâmetros,
    duração, páginas, novos/duplicados/filtrados, erros) e publica eventos
    query.started/progress/finished no barramento do projeto.
    """
    import time as _time

    from jatai.events import bus
    from jatai.research import service as research

    with get_project_session(slug) as s:
        q = s.get(Query, query_id)
        if q is None:
            return {"error": "consulta não encontrada"}
        expression = q.expression
        q_source = q.source

    src_objs = [src for src in research.list_sources(slug) if src.enabled]
    if q_source:
        src_objs = [src for src in src_objs if src.source_type == q_source]
    sources = [src.source_type for src in src_objs]

    bus.publish(slug, "query.started", query_id=query_id, run_id=run_id,
                iteration=iteration, expression=expression,
                sources=sources)

    t_start = _time.time()
    total_new = total_dup = 0
    errors: list[str] = []
    executions: list[dict] = []
    for src in src_objs:
        if should_stop and should_stop():
            break
        stype = src.source_type
        config = dict(src.config or {})
        # período de cobertura do projeto -> filtro de ano da fonte
        # (conectores que não suportam `year` ignoram a chave)
        year_range = _coverage_years(slug)
        if year_range:
            config["year"] = year_range
        connector = get_connector(stype, config=config)
        if connector is None:
            errors.append(f"{stype}: conector não implementado")
            _record_execution(slug, query_id, stype, expression, None,
                              "erro", t_start, 0, 0, 0, 0, 0,
                              f"{stype}: conector não implementado", run_id, iteration)
            continue

        def _progress(page: int, fetched: int, total: int | None,
                      _st=stype) -> None:
            bus.publish(slug, "query.progress", query_id=query_id,
                        run_id=run_id, iteration=iteration, source=_st,
                        page=page, fetched=fetched, total=total)

        def _retry(attempt: int, wait_s: float, _st=stype) -> None:
            bus.publish(slug, "query.retry", query_id=query_id,
                        run_id=run_id, iteration=iteration, source=_st,
                        attempt=attempt, wait_s=wait_s)

        # ingestão incremental: cada página é persistida assim que chega,
        # preservando resultados parciais se a paginação falhar depois
        ingested: set[int] = set()
        acc = {"new": 0, "dup": 0, "filtered": 0}

        def _on_page(page: int, batch: list, _st=stype) -> None:
            kept = _filter_by_coverage(slug, batch)
            acc["filtered"] += len(kept)
            n, d = ingest_records(slug, _st, query_id, kept)
            acc["new"] += n
            acc["dup"] += d
            ingested.update(id(r) for r in batch)

        t0 = _time.time()
        try:
            records = connector.search(expression, max_results=max_results,
                                       on_progress=_progress,
                                       on_retry=_retry,
                                       on_page=_on_page,
                                       should_stop=should_stop)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{stype}: {exc}")
            _record_execution(slug, query_id, stype, expression, connector,
                              "erro", t0, 0, 0, 0, 0,
                              getattr(connector, "last_request", {}).get("pages", 0),
                              str(exc), run_id, iteration)
            _save_search_results(
                slug, query_id, stype, expression, [],
                getattr(connector, "last_request", {}) or {},
                {"fetched": 0, "filtered": 0, "new": 0, "duplicates": 0},
                status="erro", error=str(exc),
                responses=getattr(connector, "last_payloads", None) or [])
            executions.append({"source": stype, "status": "erro", "error": str(exc)})
            close = getattr(connector, "close", None)
            if callable(close):
                close()
            continue
        close = getattr(connector, "close", None)
        if callable(close):
            close()

        fetched = len(records)
        # registros que o conector não entregou via on_page (ex.: dublês
        # antigos) são ingeridos agora — ingest_records deduplica por chave
        remaining = [r for r in records if id(r) not in ingested]
        kept_remaining = _filter_by_coverage(slug, remaining)
        new_r, dup_r = ingest_records(slug, stype, query_id, kept_remaining)
        new = acc["new"] + new_r
        dup = acc["dup"] + dup_r
        filtered_count = acc["filtered"] + len(kept_remaining)
        total_new += new
        total_dup += dup
        partial = bool((getattr(connector, "last_request", {}) or {})
                       .get("partial"))
        status = ("parcial" if partial else ("ok" if fetched else "vazio"))
        pages = int(getattr(connector, "last_request", {}).get("pages", 0) or 0)
        warning = (getattr(connector, "last_request", {}) or {}).get("warning", "")
        if warning:
            bus.publish(slug, "query.partial", query_id=query_id,
                        run_id=run_id, iteration=iteration, source=stype,
                        warning=warning)
        _record_execution(slug, query_id, stype, expression, connector,
                          status, t0, fetched, filtered_count, new, dup,
                          pages, warning, run_id, iteration)
        saved = _save_search_results(
            slug, query_id, stype, expression, records,
            getattr(connector, "last_request", {}) or {},
            {"fetched": fetched, "filtered": filtered_count,
             "new": new, "duplicates": dup},
            status=status,
            responses=getattr(connector, "last_payloads", None) or [])
        executions.append({"source": stype, "status": status,
                           "results": fetched, "new": new, "duplicates": dup,
                           "saved": saved})

    if not sources:
        errors.append("nenhuma fonte habilitada para esta consulta")

    status = "executada" if not errors else ("erro" if total_new == 0 and total_dup == 0 else "executada")
    research.set_query_status(slug, query_id, status)
    with get_project_session(slug) as s:
        q = s.get(Query, query_id)
        q.results_count = total_new + total_dup
        s.commit()
    duration_ms = int((_time.time() - t_start) * 1000)
    record_event("query.executed", entity=f"query:{query_id}", operation="execute",
                 project_slug=slug,
                 payload={"new": total_new, "duplicates": total_dup, "errors": errors})
    bus.publish(slug, "query.finished", query_id=query_id, run_id=run_id,
                iteration=iteration, expression=expression,
                new=total_new, duplicates=total_dup, errors=errors,
                duration_ms=duration_ms, status=status)
    return {"new": total_new, "duplicates": total_dup, "errors": errors,
            "executions": executions}


def _fts_ids(slug: str, q: str) -> list[int] | None:
    """IDs via FTS5; None se a busca textual deve usar LIKE."""
    terms = [t.strip('"') for t in q.replace("+", " ").split() if t.strip('"')]
    if not terms:
        return None
    match = " OR ".join(f'"{t}"*' for t in terms[:8])
    try:
        with get_project_session(slug) as s:
            _ensure_fts(s)
            rows = s.execute(text("SELECT rowid FROM paper_fts WHERE paper_fts MATCH :m"),
                             {"m": match})
            return [r[0] for r in rows]
    except Exception:  # noqa: BLE001
        return None


def _paper_filters(stmt, slug: str, q, year, source, state, has_pdf, states=""):
    if q:
        fts_ids = _fts_ids(slug, q)
        if fts_ids is not None:
            stmt = stmt.where(Paper.id.in_(fts_ids)) if fts_ids else stmt.where(False)
        else:
            like = f"%{q}%"
            stmt = stmt.where(or_(Paper.title.ilike(like), Paper.abstract.ilike(like),
                                  Paper.authors.ilike(like)))
    if year:
        stmt = stmt.where(Paper.year == int(year))
    if source:
        stmt = stmt.where(Paper.source == source)
    if state:
        stmt = stmt.where(Paper.state == state)
    if states:
        wanted = [x for x in states.split(",") if x]
        if wanted:
            stmt = stmt.where(Paper.state.in_(wanted))
    if has_pdf == "sim":
        stmt = stmt.where(Paper.pdf_path != "")
    elif has_pdf == "nao":
        stmt = stmt.where(Paper.pdf_path == "")
    return stmt


PAPER_SORTS = {
    "titulo": Paper.title, "autores": Paper.authors, "ano": Paper.year,
    "fonte": Paper.source, "tipo": Paper.pub_type, "estado": Paper.state,
}


def list_papers(slug: str, *, q: str = "", year: str = "", source: str = "",
                state: str = "", has_pdf: str = "", page: int = 1,
                sort: str = "ano", direction: str = "desc",
                states: str = "") -> tuple[list[Paper], int]:
    with get_project_session(slug) as s:
        fts_backfill(slug)
        stmt = _paper_filters(select(Paper), slug, q, year, source, state, has_pdf, states)
        count_stmt = _paper_filters(select(func.count(Paper.id)), slug, q, year, source, state, has_pdf, states)
        total = s.scalar(count_stmt) or 0
        col = PAPER_SORTS.get(sort, Paper.year)
        primary = col.desc() if direction != "asc" else col.asc()
        # desempate estável: ano nulo por último, depois id
        rows = list(s.scalars(
            stmt.order_by(primary.nulls_last(), Paper.id.desc())
            .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)))
        return rows, total


def get_paper(slug: str, paper_id: int) -> Paper | None:
    with get_project_session(slug) as s:
        return s.scalar(
            select(Paper).where(Paper.id == paper_id)
            .options(selectinload(Paper.sightings)))


def set_paper_state(slug: str, paper_id: int, state: str, comment: str = "") -> None:
    valid = {"novo", "incluido", "excluido", "talvez", "pendente"}
    if state not in valid:
        return
    with get_project_session(slug) as s:
        p = s.get(Paper, paper_id)
        if not p:
            return
        old = p.state
        p.state = state
        s.add(HumanIntervention(entity=f"paper:{paper_id}", field="state",
                                old_value=old, new_value=state, comment=comment))
        s.commit()
    record_event("paper.state", entity=f"paper:{paper_id}", operation="update",
                 project_slug=slug, payload={"old": old, "new": state})


def delete_paper(slug: str, paper_id: int) -> None:
    """Remove a publicação e seu PDF do workspace."""
    with get_project_session(slug) as s:
        p = s.get(Paper, paper_id)
        if not p:
            return
        pdf = p.pdf_path
        s.delete(p)
        s.commit()
    if pdf and Path(pdf).exists():
        try:
            Path(pdf).unlink()
        except OSError:
            pass
    record_event("paper.deleted", entity=f"paper:{paper_id}", operation="delete",
                 project_slug=slug)


def attach_pdf(slug: str, paper_id: int, filename: str, data: bytes,
                origin: str = "manual") -> str:
    ws = Path(projects_workspace(slug)) / "pdfs"
    ws.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or f"paper-{paper_id}.pdf"
    dest = ws / f"{paper_id}-{safe}"
    dest.write_bytes(data)
    with get_project_session(slug) as s:
        p = s.get(Paper, paper_id)
        if p:
            p.pdf_path = str(dest)
            exists = s.scalar(select(PaperFile).where(
                PaperFile.paper_id == paper_id, PaperFile.path == str(dest)))
            if exists is None:
                s.add(PaperFile(paper_id=paper_id, path=str(dest),
                                kind="pdf", origin=origin))
            s.commit()
    record_event("paper.pdf", entity=f"paper:{paper_id}", operation="attach",
                 project_slug=slug, payload={"file": dest.name,
                                             "origin": origin})
    return str(dest)


def projects_workspace(slug: str) -> str:
    from jatai.projects import service as projects

    p = projects.get_project(slug)
    return p.workspace if p else ""


def _save_search_results(slug: str, query_id: int, stype: str,
                         expression: str, records: list[RawRecord],
                         request: dict, counts: dict,
                         status: str = "ok", error: str = "",
                         responses: list | None = None) -> str | None:
    """Grava os resultados de uma busca em
    <workspace>/searches/q<id>_<fonte>_<timestamp>.json — inclusive
    execuções vazias e com erro. `responses` guarda o JSON bruto de
    cada página retornado pela API, conforme recebido. Falhas de
    escrita nunca interrompem a busca."""
    ws = projects_workspace(slug)
    if not ws:
        return None
    try:
        from dataclasses import asdict

        d = Path(ws) / "searches"
        d.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = d / f"q{query_id}_{stype}_{ts}.json"
        payload = {
            "query_id": query_id,
            "source": stype,
            "expression": expression,
            "saved_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
            "status": status,
            "error": error,
            "request": request,
            "counts": counts,
            "responses": responses or [],
            "results": [asdict(r) for r in records],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return str(path)
    except Exception:  # noqa: BLE001
        return None


def paper_years(slug: str) -> list[int]:
    with get_project_session(slug) as s:
        rows = s.scalars(select(Paper.year).where(Paper.year.is_not(None)).distinct())
        return sorted((y for y in rows if y), reverse=True)


def paper_sources(slug: str) -> list[str]:
    with get_project_session(slug) as s:
        return sorted(r for r in s.scalars(select(Paper.source).distinct()) if r)


def paper_states(slug: str) -> list[dict]:
    return [
        {"value": "novo", "label": "Novo"},
        {"value": "incluido", "label": "Incluído"},
        {"value": "excluido", "label": "Excluído"},
        {"value": "talvez", "label": "Talvez"},
        {"value": "pendente", "label": "Pendente"},
    ]


def paper_decisions(slug: str, paper_id: int):
    from jatai.storage.models_project import Decision

    with get_project_session(slug) as s:
        return list(s.scalars(select(Decision).where(Decision.paper_id == paper_id)
                              .order_by(Decision.created_at.desc())))


def paper_analyses(slug: str, paper_id: int):
    from jatai.storage.models_project import PaperAnalysis

    with get_project_session(slug) as s:
        rows = list(s.scalars(select(PaperAnalysis).where(PaperAnalysis.paper_id == paper_id)
                              .order_by(PaperAnalysis.created_at.desc())))
    for a in rows:
        a.model_name = ""
        try:
            data = json.loads(a.result or "{}")
            if isinstance(data, dict):
                a.model_name = data.get("model") or ""
        except (ValueError, TypeError):
            pass
    return rows


def _ensure_evidence_cols(s) -> None:
    cols = {r[1] for r in s.execute(text("PRAGMA table_info(evidence)"))}
    if "origin" not in cols:
        s.execute(text(
            "ALTER TABLE evidence ADD COLUMN origin VARCHAR(20) DEFAULT 'agente'"))


def paper_evidences(slug: str, paper_id: int):
    from jatai.storage.models_project import Evidence

    with get_project_session(slug) as s:
        _ensure_evidence_cols(s)
        return list(s.scalars(select(Evidence).where(Evidence.paper_id == paper_id)
                              .order_by(Evidence.created_at.desc())))


def get_evidence(slug: str, evidence_id: int, paper_id: int | None = None):
    from jatai.storage.models_project import Evidence

    with get_project_session(slug) as s:
        _ensure_evidence_cols(s)
        e = s.get(Evidence, evidence_id)
        if e is None or (paper_id is not None and e.paper_id != paper_id):
            return None
        return e


def add_evidence(slug: str, paper_id: int, excerpt: str,
                 page: int | None = None, origin: str = "manual") -> int:
    from jatai.storage.models_project import Evidence

    excerpt = (excerpt or "").strip()
    if not excerpt:
        return -1
    with get_project_session(slug) as s:
        _ensure_evidence_cols(s)
        e = Evidence(paper_id=paper_id, excerpt=excerpt[:4000],
                     page=page, origin=origin)
        s.add(e)
        s.commit()
        eid = e.id
    record_event("evidence.added", entity=f"evidence:{eid}", operation="create",
                 project_slug=slug, payload={"paper_id": paper_id, "origin": origin})
    return eid


def update_evidence(slug: str, evidence_id: int, excerpt: str,
                    page: int | None = None) -> None:
    from jatai.storage.models_project import Evidence

    excerpt = (excerpt or "").strip()
    if not excerpt:
        return
    with get_project_session(slug) as s:
        e = s.get(Evidence, evidence_id)
        if e is None:
            return
        e.excerpt, e.page = excerpt[:4000], page
        s.commit()
    record_event("evidence.updated", entity=f"evidence:{evidence_id}",
                 operation="update", project_slug=slug)


def delete_evidence(slug: str, evidence_id: int) -> None:
    from jatai.storage.models_project import Evidence

    with get_project_session(slug) as s:
        e = s.get(Evidence, evidence_id)
        if e is None:
            return
        s.delete(e)
        s.commit()
    record_event("evidence.deleted", entity=f"evidence:{evidence_id}",
                 operation="delete", project_slug=slug)


def paper_tags(slug: str, paper_id: int):
    from jatai.storage.models_project import PaperTag, Tag

    with get_project_session(slug) as s:
        return list(s.scalars(
            select(Tag).join(PaperTag, PaperTag.tag_id == Tag.id)
            .where(PaperTag.paper_id == paper_id)))


def paper_count(slug: str) -> int:
    with get_project_session(slug) as s:
        return s.scalar(select(func.count(Paper.id))) or 0


def state_counts(slug: str) -> dict[str, int]:
    with get_project_session(slug) as s:
        rows = s.execute(select(Paper.state, func.count()).group_by(Paper.state)).all()
        return {state: n for state, n in rows}


def sighting_queries(slug: str, paper_id: int):
    """Retorna sightings com a Query associada para a aba Proveniência."""
    from jatai.storage.models_project import Query as Q

    with get_project_session(slug) as s:
        sightings = list(s.scalars(
            select(PaperSighting).where(PaperSighting.paper_id == paper_id)
            .order_by(PaperSighting.created_at.desc())))
        result = []
        for sg in sightings:
            q = s.get(Q, sg.query_id) if sg.query_id else None
            seed_title = ""
            if sg.source == "snowball" and sg.raw:
                try:
                    seed_title = str(json.loads(sg.raw).get("seed_title") or "")
                except ValueError:
                    seed_title = ""
            result.append({"sighting": sg, "query": q,
                           "seed_title": seed_title})
        return result


# ---------- Tela 11: Conceitos ----------

def list_concepts(slug: str, status: str = ""):
    from jatai.storage.models_project import Concept

    with get_project_session(slug) as s:
        stmt = select(Concept).order_by(Concept.frequency.desc(), Concept.term)
        if status:
            stmt = stmt.where(Concept.status == status)
        return list(s.scalars(stmt))


def set_concept_status(slug: str, concept_id: int, status: str) -> None:
    from jatai.storage.models_project import Concept

    if status not in ("proposto", "aceito", "rejeitado"):
        return
    with get_project_session(slug) as s:
        c = s.get(Concept, concept_id)
        if c:
            c.status = status
            s.commit()
    record_event("concept.status", entity=f"concept:{concept_id}", operation="update",
                 project_slug=slug, payload={"status": status})


def concept_count(slug: str, status: str = "") -> int:
    from jatai.storage.models_project import Concept

    with get_project_session(slug) as s:
        stmt = select(func.count(Concept.id))
        if status:
            stmt = stmt.where(Concept.status == status)
        return s.scalar(stmt) or 0


def delete_concept(slug: str, concept_id: int) -> bool:
    """Remove o conceito e seus vínculos com publicações."""
    from jatai.storage.models_project import Concept, ConceptPaper

    with get_project_session(slug) as s:
        c = s.get(Concept, concept_id)
        if c is None:
            return False
        s.execute(delete(ConceptPaper).where(
            ConceptPaper.concept_id == concept_id))
        s.delete(c)
        s.commit()
    record_event("concept.delete", entity=f"concept:{concept_id}",
                 operation="delete", project_slug=slug)
    return True


# ---------- Conceitos de uma publicação ----------

def paper_concepts(slug: str, paper_id: int) -> list[dict]:
    """Conceitos vinculados ao artigo: associação concept_paper + fallback
    para análises 'conceitos' antigas, que só gravavam os termos."""
    import json as _json
    from jatai.storage.models_project import (
        Concept, ConceptPaper, PaperAnalysis)

    with get_project_session(slug) as s:
        origins = {r.concept_id: r.origin for r in s.scalars(
            select(ConceptPaper).where(ConceptPaper.paper_id == paper_id))}
        terms: set[str] = set()
        for a in s.scalars(select(PaperAnalysis).where(
                PaperAnalysis.paper_id == paper_id,
                PaperAnalysis.kind == "conceitos")):
            try:
                payload = _json.loads(a.result or "{}")
            except ValueError:
                continue
            for t in payload.get("terms") or []:
                t = str(t).strip().lower()
                if t:
                    terms.add(t)
        ids = set(origins)
        if terms:
            for c in s.scalars(select(Concept)):
                if (c.term or "").strip().lower() in terms:
                    ids.add(c.id)
        if not ids:
            return []
        rows = s.scalars(select(Concept).where(Concept.id.in_(ids))
                         .order_by(Concept.term))
        out = []
        for c in rows:
            try:
                rel = _json.loads(c.related_terms or "[]")
            except ValueError:
                rel = []
            out.append({"concept": c, "origin": origins.get(c.id, "agente"),
                        "related": rel if isinstance(rel, list) else []})
        return out


def get_concept(slug: str, concept_id: int):
    from jatai.storage.models_project import Concept

    with get_project_session(slug) as s:
        return s.get(Concept, concept_id)


def _link_paper_concept(s, concept_id: int, paper_id: int,
                        origin: str = "manual") -> None:
    from jatai.storage.models_project import ConceptPaper

    link = s.get(ConceptPaper, (concept_id, paper_id))
    if link is None:
        s.add(ConceptPaper(concept_id=concept_id, paper_id=paper_id,
                           origin=origin))


def save_paper_concept(slug: str, paper_id: int, term: str,
                       description: str = "", related_terms: str = "",
                       concept_id: int | None = None,
                       status: str = "") -> int | None:
    """Inclui/edita um conceito vinculado à publicação.

    Com concept_id, edita o Concept existente; sem, reutiliza um Concept
    de mesmo termo (case-insensitive) ou cria um novo com origin='manual'.
    Retorna o id do conceito ou None se o termo for vazio."""
    import json as _json
    from jatai.storage.models_project import Concept, ConceptPaper

    term = (term or "").strip()[:300]
    if not term:
        return None
    related = [t.strip() for t in re.split(r"[,;]", related_terms or "")
               if t.strip()]
    with get_project_session(slug) as s:
        if concept_id:
            c = s.get(Concept, concept_id)
            if c is None:
                return None
        else:
            c = s.scalar(select(Concept).where(
                func.lower(Concept.term) == term.lower()))
        if c is None:
            c = Concept(term=term, origin="manual",
                        status=status or "aceito")
            s.add(c)
            s.flush()
        else:
            c.term = term
            if status in ("proposto", "aceito", "rejeitado"):
                c.status = status
        if description.strip():
            c.description = description.strip()[:2000]
        if related:
            c.related_terms = _json.dumps(related, ensure_ascii=False)[:4000]
        link = s.get(ConceptPaper, (c.id, paper_id))
        if link is None:
            s.add(ConceptPaper(concept_id=c.id, paper_id=paper_id,
                               origin="manual"))
        record_event("concept.paper", entity=f"concept:{c.id}",
                     operation="link", project_slug=slug,
                     payload={"paper_id": paper_id, "term": c.term})
        s.commit()
        return c.id


def save_concept(slug: str, term: str, description: str = "",
                 related_terms: str = "", status: str = "",
                 concept_id: int | None = None) -> int | None:
    """Cria ou edita um conceito global (tela Conceitos).

    Com concept_id edita o registro; sem, reutiliza um conceito de mesmo
    termo (case-insensitive) ou cria um novo com origin='manual'."""
    import json as _json
    from jatai.storage.models_project import Concept

    term = (term or "").strip()[:300]
    if not term:
        return None
    related = [t.strip() for t in re.split(r"[,;]", related_terms or "")
               if t.strip()]
    with get_project_session(slug) as s:
        if concept_id:
            c = s.get(Concept, concept_id)
            if c is None:
                return None
        else:
            c = s.scalar(select(Concept).where(
                func.lower(Concept.term) == term.lower()))
        if c is None:
            c = Concept(term=term, origin="manual",
                        status=status or "aceito")
            s.add(c)
            s.flush()
        else:
            c.term = term
            if status in ("proposto", "aceito", "rejeitado"):
                c.status = status
            if not c.origin:
                c.origin = "manual"
        if description.strip():
            c.description = description.strip()[:2000]
        if related:
            c.related_terms = _json.dumps(related, ensure_ascii=False)[:4000]
        record_event("concept.save", entity=f"concept:{c.id}",
                     operation="update" if concept_id else "create",
                     project_slug=slug, payload={"term": c.term})
        s.commit()
        return c.id


# ---------- Triagem ----------

def review_papers(slug: str, limit: int = 50):
    """Papers que precisam de decisão humana (talvez/pendente/novo com sinal do agente)."""
    with get_project_session(slug) as s:
        return list(s.scalars(
            select(Paper).where(Paper.state.in_(["talvez", "pendente", "novo"]))
            .order_by(Paper.updated_at.desc()).limit(limit)))


def pending_agent_queries(slug: str):
    with get_project_session(slug) as s:
        return list(s.scalars(
            select(Query).where(Query.origin.in_(["agent", "expanded"]),
                                Query.status.in_(["pendente", "aprovada"]))))


# ---------- Tela 14: Dashboard ----------

def dashboard_stats(slug: str) -> dict:
    from jatai.storage.models_project import Concept, LLMCall, Query, Run

    with get_project_session(slug) as s:
        state_rows = s.execute(
            select(Paper.state, func.count()).group_by(Paper.state)).all()
        return {
            "papers_total": s.scalar(select(func.count(Paper.id))) or 0,
            "states": {st: n for st, n in state_rows},
            "queries_total": s.scalar(select(func.count(Query.id))) or 0,
            "queries_pending": s.scalar(select(func.count(Query.id)).where(
                Query.status.in_(["pendente", "aprovada"]))) or 0,
            "runs_total": s.scalar(select(func.count(Run.id))) or 0,
            "runs_active": s.scalar(select(func.count(Run.id)).where(
                Run.status.in_(["executando", "pausado", "pendente"]))) or 0,
            "concepts": s.scalar(select(func.count(Concept.id))) or 0,
            "llm_calls": s.scalar(select(func.count(LLMCall.id))) or 0,
            "llm_cost": s.scalar(select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0))) or 0.0,
        }


def recent_runs(slug: str, limit: int = 5):
    from jatai.storage.models_project import Run

    with get_project_session(slug) as s:
        return list(s.scalars(select(Run).order_by(Run.id.desc()).limit(limit)))


# ---------- Tela 02: Dashboard ----------

def papers_by_year(slug: str) -> list[tuple[int, int]]:
    with get_project_session(slug) as s:
        return [(y, n) for y, n in s.execute(
            select(Paper.year, func.count()).where(Paper.year.is_not(None))
            .group_by(Paper.year).order_by(Paper.year)).all()]


def papers_by_source(slug: str) -> list[tuple[str, int]]:
    with get_project_session(slug) as s:
        return [(src, n) for src, n in s.execute(
            select(Paper.source, func.count()).group_by(Paper.source)
            .order_by(func.count().desc())).all() if src]


def iterations_progress(slug: str, days: int | None = None) -> list[dict]:
    """Artigos novos por iteração de cada run (evolução da pesquisa).

    `days` limita às iterações criadas nos últimos N dias."""
    from datetime import datetime, timedelta, timezone
    from jatai.storage.models_project import Iteration, Run

    with get_project_session(slug) as s:
        stmt = select(Iteration.run_id, Iteration.number, Iteration.new_papers,
                      Iteration.new_terms, Iteration.status,
                      Iteration.created_at)
        if days:
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) \
                - timedelta(days=days)
            stmt = stmt.where(Iteration.created_at >= cutoff)
        rows = s.execute(
            stmt.order_by(Iteration.run_id, Iteration.number)).all()
        return [{"run_id": r, "number": n, "new_papers": p, "new_terms": t,
                 "status": st, "created_at": ts} for r, n, p, t, st, ts in rows]


def concepts_by_iteration(slug: str) -> list[tuple[int, int]]:
    from jatai.storage.models_project import Concept

    with get_project_session(slug) as s:
        return [(it, n) for it, n in s.execute(
            select(Concept.iteration, func.count()).group_by(Concept.iteration)
            .order_by(Concept.iteration)).all()]


def latest_papers(slug: str, limit: int = 8):
    with get_project_session(slug) as s:
        return list(s.scalars(select(Paper).order_by(Paper.id.desc()).limit(limit)))


def recent_interventions(slug: str, limit: int = 8):
    with get_project_session(slug) as s:
        return list(s.scalars(
            select(HumanIntervention).order_by(HumanIntervention.id.desc()).limit(limit)))


def failed_tasks(slug: str, limit: int = 10):
    """Tarefas com erros na última iteração de cada run."""
    from jatai.storage.models_project import Iteration

    with get_project_session(slug) as s:
        iters = list(s.scalars(select(Iteration).where(Iteration.status == "falha")
                               .order_by(Iteration.id.desc()).limit(limit)))
        return iters


def next_run_estimate(slug: str) -> str:
    sched = None
    from jatai.agents import config_service

    sched = config_service.get_schedule(slug)
    if not sched or not sched.enabled:
        return ""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    last = sched.last_run_at
    if last and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    base = last or now
    if sched.frequency == "diaria":
        nxt = base + timedelta(hours=24)
    elif sched.frequency == "semanal":
        nxt = base + timedelta(days=7)
    else:
        return ""
    return nxt.strftime("%d/%m/%Y %H:%M")


# ---------- Avaliação da Qualidade (notas 0.0–1.0) ----------

def quality_scores(slug: str, paper_id: int) -> dict[int, "QualityScore"]:
    from jatai.storage.models_project import QualityScore

    with get_project_session(slug) as s:
        return {q.criterion_id: q for q in s.scalars(
            select(QualityScore).where(QualityScore.paper_id == paper_id))}


def set_quality_score(slug: str, paper_id: int, criterion_id: int,
                      score: float, comment: str = "") -> None:
    from jatai.storage.models_project import QualityScore

    score = max(0.0, min(1.0, score))
    with get_project_session(slug) as s:
        q = s.scalar(select(QualityScore).where(
            QualityScore.paper_id == paper_id,
            QualityScore.criterion_id == criterion_id))
        if q is None:
            q = QualityScore(paper_id=paper_id, criterion_id=criterion_id)
            s.add(q)
        q.score, q.comment = score, comment
        s.commit()
    record_event("quality.score", entity=f"paper:{paper_id}", operation="update",
                 project_slug=slug,
                 payload={"criterion_id": criterion_id, "score": score})


def quality_total(slug: str, paper_id: int) -> float | None:
    """Média ponderada das notas; None se nenhuma nota."""
    from jatai.storage.models_project import QualityCriterion, QualityScore

    with get_project_session(slug) as s:
        rows = s.execute(
            select(QualityScore.score, QualityCriterion.weight)
            .join(QualityCriterion, QualityScore.criterion_id == QualityCriterion.id)
            .where(QualityScore.paper_id == paper_id,
                   QualityCriterion.active.is_(True))).all()
        if not rows:
            return None
        num = sum(sc * w for sc, w in rows)
        den = sum(w for _, w in rows) or 1.0
        return round(num / den, 3)


def quality_avgs(slug: str) -> dict[int, float]:
    """Média de qualidade por paper (para listagens)."""
    from jatai.storage.models_project import QualityCriterion, QualityScore

    with get_project_session(slug) as s:
        rows = s.execute(
            select(QualityScore.paper_id, QualityScore.score, QualityCriterion.weight)
            .join(QualityCriterion, QualityScore.criterion_id == QualityCriterion.id)
            .where(QualityCriterion.active.is_(True))).all()
    num: dict[int, float] = {}
    den: dict[int, float] = {}
    for pid, sc, w in rows:
        num[pid] = num.get(pid, 0.0) + sc * w
        den[pid] = den.get(pid, 0.0) + w
    return {pid: round(num[pid] / den[pid], 3) for pid in num if den[pid]}


# ---------- Análise agregada (Qualidade > Análise) ----------

def _latest_analyses(slug: str, kind: str) -> dict[int, "PaperAnalysis"]:
    """Última análise de um kind por paper."""
    import json as _json  # noqa: F401
    from jatai.storage.models_project import PaperAnalysis

    with get_project_session(slug) as s:
        rows = list(s.scalars(
            select(PaperAnalysis)
            .where(PaperAnalysis.kind == kind)
            .order_by(PaperAnalysis.paper_id, PaperAnalysis.created_at.desc())))
    latest: dict[int, "PaperAnalysis"] = {}
    for a in rows:
        latest.setdefault(a.paper_id, a)
    return latest


def analysis_overview(slug: str) -> dict:
    """Resumo por paper das análises do agente (todos os kinds)."""
    from jatai.storage.models_project import Paper, PaperAnalysis

    with get_project_session(slug) as s:
        rows = list(s.execute(
            select(PaperAnalysis.paper_id, PaperAnalysis.kind,
                   func.max(PaperAnalysis.created_at))
            .group_by(PaperAnalysis.paper_id, PaperAnalysis.kind)))
        papers = {p.id: p for p in s.scalars(select(Paper))}
    per_paper: dict[int, dict] = {}
    kind_counts: dict[str, int] = {}
    for pid, kind, ts in rows:
        if pid not in papers:
            continue
        entry = per_paper.setdefault(pid, {"kinds": {}, "last": None})
        entry["kinds"][kind] = ts
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        if entry["last"] is None or (ts and ts > entry["last"]):
            entry["last"] = ts
    items = []
    triage = triagem_results(slug, [pid for pid in per_paper])
    for pid, info in per_paper.items():
        p = papers[pid]
        items.append({"paper": p, "kinds": info["kinds"], "last": info["last"],
                      "triagens": triage.get(pid, [])})
    items.sort(key=lambda x: x["last"] or x["paper"].id, reverse=True)
    return {"items": items, "kind_counts": kind_counts}


TRIAGEM_KINDS = {"triagem": "titulo_resumo",
                 "triagem_profunda": "texto_completo"}

_STAGE_LABELS = {"titulo_resumo": "título/resumo",
                 "texto_completo": "texto completo"}


def review_flow(slug: str) -> dict:
    """Matriz fonte × fase da revisão para o dashboard (estilo PRISMA).

    Linhas: uma por fonte bibliográfica (fontes habilitadas primeiro,
    depois fontes com registros não configuradas, "outros" por último) +
    linha de total. Colunas: Total de artigos originados da fonte,
    um grupo por nível de triagem executado (na ordem de registro,
    com uma sub-coluna por critério de inclusão/exclusão contando os
    artigos removidos) e, sempre por último, Artigos incluídos.

    Um artigo encontrado em N fontes conta em cada uma delas (semântica
    de "registros identificados por base"); a linha de total soma as
    colunas e pode exceder o número de artigos únicos."""
    import json as _json
    from jatai.storage.models_project import (
        Criterion, Paper, PaperAnalysis, PaperSighting, ProjectSource,
        AgentTaskConfig)

    with get_project_session(slug) as s:
        cfg_sources = list(s.scalars(
            select(ProjectSource).order_by(ProjectSource.id)))
        papers = list(s.scalars(select(Paper)))
        sight_rows = list(s.execute(
            select(PaperSighting.paper_id, PaperSighting.source)
            .order_by(PaperSighting.id)))
        crit_meta = {c.code.strip().upper(): c
                     for c in s.scalars(select(Criterion))}
        task_rows = list(s.scalars(
            select(AgentTaskConfig)
            .where(AgentTaskConfig.enabled.is_(True))
            .order_by(AgentTaskConfig.agent_id,
                      AgentTaskConfig.position, AgentTaskConfig.id)))
        kind_rows = s.execute(
            select(PaperAnalysis.kind, func.min(PaperAnalysis.created_at))
            .where(PaperAnalysis.kind.like("triagem%"))
            .group_by(PaperAnalysis.kind)
            .order_by(func.min(PaperAnalysis.created_at))).all()

    names = {}
    for c in cfg_sources:
        names.setdefault(c.source_type, c.name or c.source_type)

    # fontes de cada artigo (sightings + fonte primária; "snowball" é
    # proveniência, não base de dados). `first_source` guarda a primeira
    # fonte da ordem de proveniência (sighting mais antigo) — usada para
    # contabilizar duplicados: o artigo só "pertence" à primeira fonte.
    paper_sources: dict[int, list[str]] = {}
    first_source: dict[int, str] = {}
    for pid, src in sight_rows:
        if src and src != "snowball":
            if pid not in first_source:
                first_source[pid] = src
            paper_sources.setdefault(pid, [])
            if src not in paper_sources[pid]:
                paper_sources[pid].append(src)
    paper_state = {p.id: p.state for p in papers}
    for p in papers:
        src = (p.source or "").strip()
        keys = paper_sources.setdefault(p.id, [])
        if src and src != "snowball" and src not in keys:
            keys.append(src)
        if p.id not in first_source:
            first_source[p.id] = src if (src and src != "snowball") else "outros"
        if not keys:
            keys.append("outros")

    # ---- níveis de triagem ----
    # Ordem: tarefas de triagem habilitadas na ordem do pipeline
    # ("ordem de triagem registrada"), depois kinds executados sem tarefa
    # configurada, por data da primeira execução.
    TASK_LEVELS = {"screener": ("triagem", "titulo_resumo"),
                   "deep_screener": ("triagem_profunda", "texto_completo")}

    def _crit_sort(code: str):
        c = crit_meta.get(code)
        return (c.position if c else 9999, code)

    level_specs: list[tuple[str, str]] = []
    seen_kinds: set[str] = set()
    for t in task_rows:
        mapped = TASK_LEVELS.get(t.task_type)
        if mapped and mapped[0] not in seen_kinds:
            seen_kinds.add(mapped[0])
            level_specs.append(mapped)
    for kind, _first in kind_rows:
        if kind.endswith("_erro") or kind in seen_kinds:
            continue
        seen_kinds.add(kind)
        level_specs.append((kind, TRIAGEM_KINDS.get(kind, "")))

    levels = []
    for idx, (kind, stage) in enumerate(level_specs, start=1):
        latest = _latest_analyses(slug, kind)
        verdicts: dict[int, tuple[str, set[str]]] = {}
        seen_codes: dict[str, None] = {}
        for pid, a in latest.items():
            try:
                data = _json.loads(a.result or "{}")
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            applied = str(data.get("applied_verdict")
                          or data.get("overall_verdict") or "")
            flagged: set[str] = set()
            for e in data.get("evaluations", []):
                if not isinstance(e, dict):
                    continue
                code = str(e.get("criterion_id", "")).strip().upper()
                if not code:
                    continue
                seen_codes.setdefault(code)
                if str(e.get("verdict", "")) == "excluir":
                    flagged.add(code)
            verdicts[pid] = (applied, flagged)

        # critérios da coluna: os configurados aplicáveis ao estágio
        # (triagem profunda reavalia todos) ∪ os vistos nas análises
        if stage == "titulo_resumo":
            applicable = {c.code.strip().upper() for c in crit_meta.values()
                          if c.applies_to in ("titulo_resumo", "ambos")}
        elif stage == "texto_completo":
            applicable = set(crit_meta)
        else:
            applicable = set()
        all_codes = applicable | set(seen_codes)

        criteria = sorted(all_codes, key=_crit_sort)
        has_other = any(app == "excluir" and not flagged
                        for app, flagged in verdicts.values())
        cols = [{"key": c, "label": c,
                 "hint": (crit_meta[c].description[:120]
                          if c in crit_meta else "")}
                for c in criteria]
        if has_other:
            cols.append({"key": "_outros", "label": "Outros motivos",
                         "hint": "Excluídos sem critério IC/EC específico"})
        levels.append({
            "key": f"level|{kind}", "kind": kind,
            "label": f"{idx}º nível — {_STAGE_LABELS.get(stage, stage or kind)}",
            "cols": cols,
            "verdicts": verdicts,
        })

    # ---- linhas: uma por fonte ----
    enabled = []
    for c in cfg_sources:
        if c.enabled and c.source_type not in enabled:
            enabled.append(c.source_type)
    used = sorted({s for keys in paper_sources.values() for s in keys} - {"outros"})
    row_keys = enabled + [k for k in used if k not in enabled]
    if "outros" in {s for keys in paper_sources.values() for s in keys}:
        row_keys.append("outros")

    rows = []
    totals = {"total": 0, "duplicates": 0, "pending": 0, "included": 0}
    for lv in levels:
        for c in lv["cols"]:
            totals[f"{lv['key']}|{c['key']}"] = 0
    for key in row_keys:
        member_papers = [pid for pid, ks in paper_sources.items()
                         if key in ks]
        # artigos únicos (1ª fonte da proveniência) — usados nas colunas
        # de fase para não contabilizar duplicados em fontes posteriores
        own_papers = [pid for pid in member_papers
                      if first_source.get(pid) == key]
        cells: dict[str, int] = {}
        included_n = pending_n = 0
        for pid in own_papers:
            st = paper_state.get(pid)
            if st == "incluido":
                included_n += 1
            elif st != "excluido":
                pending_n += 1
        for pid in member_papers:
            for lv in levels:
                applied, flagged = lv["verdicts"].get(pid, ("", set()))
                if applied != "excluir":
                    continue
                hit = False
                for c in lv["cols"]:
                    ck = c["key"]
                    if ck == "_outros":
                        continue
                    if ck in flagged:
                        cells[f"{lv['key']}|{ck}"] = \
                            cells.get(f"{lv['key']}|{ck}", 0) + 1
                        hit = True
                if not hit and "_outros" in {c["key"] for c in lv["cols"]}:
                    cells[f"{lv['key']}|_outros"] = \
                        cells.get(f"{lv['key']}|_outros", 0) + 1
        dup_n = sum(1 for pid in member_papers
                    if first_source.get(pid) != key)
        rows.append({"key": key, "label": names.get(key, key),
                     "total": len(member_papers), "duplicates": dup_n,
                     "pending": pending_n,
                     "included": included_n,
                     "cells": cells})
        totals["total"] += len(member_papers)
        totals["duplicates"] += dup_n
        totals["pending"] += pending_n
        totals["included"] += included_n
        for k, v in cells.items():
            totals[k] = totals.get(k, 0) + v

    total_row = {"key": "_total", "label": "Total",
                 "total": totals["total"],
                 "duplicates": totals["duplicates"],
                 "pending": totals["pending"],
                 "included": totals["included"],
                 "cells": {k: v for k, v in totals.items()
                           if "|" in k}}

    return {"levels": [{k: v for k, v in lv.items() if k != "verdicts"}
                       for lv in levels],
            "rows": rows, "total_row": total_row,
            "papers_total": len(papers),
            "included_total": sum(1 for st in paper_state.values()
                                  if st == "incluido")}


def triagem_results(slug: str, paper_ids: list[int] | None = None) -> dict[int, list]:
    """Análises de triagem por paper (título/resumo e texto completo),
    parseadas: avaliação por critério com evidência e origem
    (decision.origin) de cada critério. Retorna {paper_id: [triagem,...]}
    ordenadas por data."""
    import json as _json
    from jatai.storage.models_project import Criterion, Decision

    per_kind = {kind: _latest_analyses(slug, kind) for kind in TRIAGEM_KINDS}
    if paper_ids is not None:
        wanted = set(paper_ids)
        per_kind = {k: {pid: a for pid, a in m.items() if pid in wanted}
                    for k, m in per_kind.items()}
    analyses: dict[int, list] = {}
    for kind, latest in per_kind.items():
        for pid, a in latest.items():
            analyses.setdefault(pid, []).append((kind, a))
    flat = [(pid, kind, a) for pid, lst in analyses.items()
            for kind, a in lst]
    if not flat:
        return {}
    pids = list(analyses)
    with get_project_session(slug) as s:
        crit_meta = {c.code.strip().upper(): c for c in s.scalars(select(Criterion))}
        decisions = list(s.scalars(
            select(Decision).where(Decision.paper_id.in_(pids),
                                   Decision.criterion_code != "")
            .order_by(Decision.id)))
    # origem por (paper, critério) ao longo do tempo — para cada triagem
    # usa a decisão mais recente até a data da análise
    hist: dict[tuple[int, str], list] = {}
    for d in decisions:
        hist.setdefault((d.paper_id, d.criterion_code.strip().upper()),
                        []).append((d.created_at, d.origin))

    def _origin(pid: int, code: str, ts) -> str:
        entries = hist.get((pid, code.upper()), [])
        if not entries:
            return "agente"
        eligible = [o for t, o in entries if ts is None or t <= ts]
        return eligible[-1] if eligible else entries[0][1]

    out: dict[int, list] = {}
    for pid, kind, a in flat:
        try:
            data = _json.loads(a.result or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        evals = []
        for e in data.get("evaluations", []):
            if not isinstance(e, dict):
                continue
            code = str(e.get("criterion_id", "")).strip()
            c = crit_meta.get(code.upper())
            evals.append({
                "code": code,
                "kind": "IC" if c and c.type == "inclusao" else
                        "EC" if c else "?",
                "required": bool(c.required) if c else False,
                "met": e.get("met"),
                "verdict": str(e.get("verdict", "")),
                "justification": str(e.get("justification", "")),
                "evidence": str(e.get("evidence_quote") or ""),
                "origin": _origin(pid, code, a.created_at),
            })
        out.setdefault(pid, []).append({
            "analysis_id": a.id,
            "stage": str(data.get("stage") or TRIAGEM_KINDS.get(kind, "")),
            "kind": kind,
            "verdict": str(data.get("applied_verdict") or data.get("overall_verdict", "")),
            "confidence": data.get("confidence"),
            "rule_notes": data.get("rule_notes", []),
            "evaluations": evals,
            "created_at": a.created_at,
        })
    for lst in out.values():
        lst.sort(key=lambda t: t["created_at"] or datetime.min.replace(tzinfo=timezone.utc))
    return out


def deep_results(slug: str, paper_ids: list[int] | None = None) -> dict[int, list]:
    """Análises profundas (resumo + respostas às RQs) parseadas por paper.
    Retorna {paper_id: [analise,...]} ordenadas por data."""
    import json as _json
    latest = _latest_analyses(slug, "profunda")
    if paper_ids is not None:
        wanted = set(paper_ids)
        latest = {pid: a for pid, a in latest.items() if pid in wanted}
    out: dict[int, list] = {}
    for pid, a in latest.items():
        try:
            data = _json.loads(a.result or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        answers = []
        for ans in data.get("answers", []):
            if not isinstance(ans, dict):
                continue
            answers.append({
                "code": str(ans.get("code", "")),
                "addressed": ans.get("addressed"),
                "answer": str(ans.get("answer", "")),
                "evidence": str(ans.get("evidence_quote") or ""),
            })
        out.setdefault(pid, []).append({
            "analysis_id": a.id,
            "summary": str(data.get("summary", "")),
            "confidence": data.get("confidence"),
            "rule_notes": data.get("rule_notes", []),
            "answers": answers,
            "created_at": a.created_at,
        })
    for lst in out.values():
        lst.sort(key=lambda t: t["created_at"] or datetime.min.replace(tzinfo=timezone.utc))
    return out


def rq_answers_matrix(slug: str, limit: int = 300) -> dict:
    """Respostas às RQs extraídas das análises 'profunda'."""
    import json as _json
    from jatai.storage.models_project import Paper
    from jatai.research import service as research

    questions = research.list_questions(slug)
    latest = _latest_analyses(slug, "profunda")
    with get_project_session(slug) as s:
        papers = {p.id: p for p in s.scalars(
            select(Paper).where(Paper.id.in_(list(latest) or [0]))
            .order_by(Paper.id.desc()).limit(limit))}
    rows = []
    for pid, paper in papers.items():
        try:
            data = _json.loads(latest[pid].result or "{}")
        except (ValueError, TypeError):
            data = {}
        answers = {}
        for ans in data.get("rq_answers", []) if isinstance(data, dict) else []:
            if isinstance(ans, dict) and ans.get("code"):
                answers[str(ans["code"])] = {
                    "answer": str(ans.get("answer", "")),
                    "evidence": str(ans.get("evidence", "")),
                }
        rows.append({"paper": paper, "summary": str(data.get("summary", ""))
                     if isinstance(data, dict) else "", "answers": answers})
    return {"questions": questions, "rows": rows}


def classification_matrix(slug: str, limit: int = 300) -> dict:
    """Valores de CatalogFields extraídos das análises 'classificacao'."""
    import json as _json
    from jatai.storage.models_project import Paper
    from jatai.research import service as research

    fields = [f for f in research.list_catalog_fields(slug) if f.active]
    latest = _latest_analyses(slug, "classificacao")
    with get_project_session(slug) as s:
        papers = {p.id: p for p in s.scalars(
            select(Paper).where(Paper.id.in_(list(latest) or [0]))
            .order_by(Paper.id.desc()).limit(limit))}
    rows = []
    for pid, paper in papers.items():
        try:
            data = _json.loads(latest[pid].result or "{}")
        except (ValueError, TypeError):
            data = {}
        values = data.get("values", {}) if isinstance(data, dict) else {}
        rows.append({"paper": paper,
                     "vals": {f.identifier: values.get(f.identifier)
                              for f in fields}})
    return {"fields": fields, "rows": rows}


def _latest_classification(slug: str, paper_id: int):
    from jatai.storage.models_project import PaperAnalysis
    with get_project_session(slug) as s:
        return s.scalar(select(PaperAnalysis).where(
            PaperAnalysis.paper_id == paper_id,
            PaperAnalysis.kind == "classificacao")
            .order_by(PaperAnalysis.id.desc()))


def classification_values(slug: str, paper_id: int) -> dict:
    """Mapa identifier -> valor da última classificação do paper."""
    a = _latest_classification(slug, paper_id)
    if a is None:
        return {}
    try:
        data = json.loads(a.result or "{}")
    except (ValueError, TypeError):
        return {}
    values = data.get("values", {})
    return values if isinstance(values, dict) else {}


def classification_meta(slug: str, paper_id: int) -> dict | None:
    """Metadados da última classificação: origem, modelo e datas."""
    a = _latest_classification(slug, paper_id)
    if a is None:
        return None
    try:
        data = json.loads(a.result or "{}")
    except (ValueError, TypeError):
        data = {}
    return {
        "origin": data.get("origin") or "agente",
        "model": data.get("model") or "",
        "classified_at": data.get("classified_at") or "",
        "edited_at": data.get("edited_at") or "",
        "created_at": a.created_at.isoformat() if a.created_at else "",
    }


def _value_to_input(v) -> str:
    """Serializa um valor de classificação para exibição em input de texto."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "verdadeiro" if v else "falso"
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def update_classification(slug: str, paper_id: int, raw_values: dict) -> bool:
    """Atualiza os valores da última análise 'classificacao' do paper.

    raw_values: {identifier: string do formulário}; os valores são
    coagidos conforme o data_type do CatalogField. Retorna False se o
    paper não tiver classificação.
    """
    from jatai.agents.classify import _coerce_value
    from jatai.research import service as research
    from jatai.storage.models_project import PaperAnalysis

    a = _latest_classification(slug, paper_id)
    if a is None:
        return False
    types = {f.identifier: f.data_type
             for f in research.list_catalog_fields(slug)}
    coerced = {k: _coerce_value(str(v).strip() or None, types.get(k, "texto"))
               for k, v in raw_values.items()}
    try:
        data = json.loads(a.result or "{}")
    except (ValueError, TypeError):
        data = {}
    data.setdefault("values", {}).update(coerced)
    for ev in data.get("evaluations", []):
        if isinstance(ev, dict) and ev.get("identifier") in coerced:
            ev["value"] = coerced[ev["identifier"]]
    data["edited_at"] = datetime.now(timezone.utc).isoformat()
    data["origin"] = "manual"
    with get_project_session(slug) as s:
        row = s.get(PaperAnalysis, a.id)
        row.result = json.dumps(data, ensure_ascii=False)[:16000]
        s.commit()
    record_event("classification.updated", entity=f"paper:{paper_id}",
                 operation="update", project_slug=slug,
                 payload={"fields": list(coerced)})
    return True


def save_classification(slug: str, paper_id: int, raw_values: dict,
                        manual: bool = True) -> bool:
    """Upsert da classificação do paper: atualiza a análise existente ou
    cria uma nova (origem 'manual' quando preenchida por humano)."""
    from jatai.storage.models_project import PaperAnalysis
    if _latest_classification(slug, paper_id) is not None:
        return update_classification(slug, paper_id, raw_values)
    from jatai.agents.classify import _coerce_value
    from jatai.research import service as research
    types = {f.identifier: f.data_type
             for f in research.list_catalog_fields(slug)}
    coerced = {k: _coerce_value(str(v).strip() or None, types.get(k, "texto"))
               for k, v in raw_values.items()}
    payload = {"values": coerced, "origin": "manual" if manual else "agente",
               "confidence": None, "rule_notes": [],
               "edited_at": datetime.now(timezone.utc).isoformat()}
    with get_project_session(slug) as s:
        s.add(PaperAnalysis(
            paper_id=paper_id, kind="classificacao",
            result=json.dumps(payload, ensure_ascii=False)[:16000]))
        s.commit()
    record_event("classification.created", entity=f"paper:{paper_id}",
                 operation="create", project_slug=slug,
                 payload={"fields": list(coerced)})
    return True


def delete_classification(slug: str, paper_id: int) -> bool:
    """Remove a(s) análise(s) 'classificacao' do paper."""
    from jatai.storage.models_project import PaperAnalysis
    with get_project_session(slug) as s:
        n = s.execute(delete(PaperAnalysis).where(
            PaperAnalysis.paper_id == paper_id,
            PaperAnalysis.kind == "classificacao")).rowcount
        s.commit()
    if n:
        record_event("classification.deleted", entity=f"paper:{paper_id}",
                     operation="delete", project_slug=slug,
                     payload={"analyses": n})
    return bool(n)


def delete_analysis(slug: str, analysis_id: int) -> bool:
    """Remove uma análise do agente (triagem, triagem profunda, profunda,
    classificação etc.) pelo id — usada para descartar processos com erro
    ou refazer uma etapa."""
    from jatai.storage.models_project import PaperAnalysis
    with get_project_session(slug) as s:
        a = s.get(PaperAnalysis, analysis_id)
        if a is None:
            return False
        paper_id, kind = a.paper_id, a.kind
        s.delete(a)
        s.commit()
    record_event("analysis.deleted", entity=f"paper:{paper_id}",
                 operation="delete", project_slug=slug,
                 payload={"kind": kind, "analysis_id": analysis_id})
    return True


def save_manual_screening(slug: str, paper_id: int, *, kind: str = "triagem",
                          stage: str = "titulo_resumo", verdict: str = "incluir",
                          evals: list | None = None,
                          analysis_id: int | None = None) -> int | None:
    """Cria ou edita uma triagem manual (sem LLM) de um artigo.

    `evals`: [{code, met (True/False/None), verdict, justification,
    evidence}]. O payload gravado tem o mesmo formato das triagens do
    agente, com origin/model="manual" — alimenta a ficha da publicação e
    o quadro de fluxo do dashboard. Retorna o id da análise ou None."""
    import json as _json
    from jatai.storage.models_project import (
        Criterion, Decision, Evidence, Paper, PaperAnalysis)

    verdict = (verdict or "").strip()
    state_map = {"incluir": "incluido", "excluir": "excluido",
                 "talvez": "talvez"}
    if verdict not in state_map or kind not in ("triagem", "triagem_profunda"):
        return None

    payload_evals = []
    for e in evals or []:
        code = str(e.get("code", "")).strip().upper()
        if not code:
            continue
        v = str(e.get("verdict", "inconclusivo"))
        if v not in ("incluir", "excluir", "inconclusivo"):
            v = "inconclusivo"
        met = e.get("met")
        payload_evals.append({
            "criterion_id": code,
            "met": met if isinstance(met, bool) else None,
            "verdict": v,
            "justification": str(e.get("justification", ""))[:2000],
            "evidence_quote": str(e.get("evidence", ""))[:2000],
        })

    payload = {
        "evaluations": payload_evals,
        "overall_verdict": verdict,
        "applied_verdict": verdict,
        "confidence": None,
        "stage": stage,
        "rule_notes": ["triagem manual"],
        "model": "manual",
        "origin": "manual",
    }
    result_json = _json.dumps(payload, ensure_ascii=False)[:8000]

    with get_project_session(slug) as s:
        p = s.get(Paper, paper_id)
        if p is None:
            return None
        if analysis_id:
            a = s.get(PaperAnalysis, analysis_id)
            if (a is None or a.paper_id != paper_id
                    or not str(a.kind).startswith("triagem")):
                return None
            a.result = result_json
            op = "update"
        else:
            a = PaperAnalysis(paper_id=paper_id, kind=kind, result=result_json)
            s.add(a)
            s.flush()
            op = "create"
        p.state = state_map[verdict]

        crit_meta = {c.code.strip().upper(): c
                     for c in s.scalars(select(Criterion))}
        for e in payload_evals:
            c = crit_meta.get(e["criterion_id"])
            met = e["met"]
            if met is None:
                dverdict = "talvez"
            elif c is not None and c.type == "exclusao":
                dverdict = "excluir" if met else "incluir"
            elif c is not None:
                dverdict = "incluir" if met else "excluir"
            else:
                dverdict = e["verdict"] if e["verdict"] in (
                    "incluir", "excluir") else "talvez"
            d = Decision(paper_id=paper_id,
                         criterion_code=e["criterion_id"],
                         verdict=dverdict,
                         justification=e["justification"],
                         origin="human", iteration=0)
            s.add(d)
            s.flush()
            if e["evidence_quote"]:
                s.add(Evidence(paper_id=paper_id, decision_id=d.id,
                               excerpt=e["evidence_quote"], origin="manual"))
        s.add(Decision(paper_id=paper_id, criterion_code="", verdict=verdict,
                       justification="triagem manual", origin="human",
                       iteration=0))
        s.commit()

    record_event("screening.manual", entity=f"paper:{paper_id}",
                 operation=op, project_slug=slug,
                 payload={"kind": kind, "stage": stage, "verdict": verdict,
                          "analysis_id": a.id})
    return a.id


def delete_error_analyses(slug: str) -> int:
    """Remove todas as análises com erro (kind terminado em '_erro'),
    tornando os papers elegíveis para reprocessamento."""
    from jatai.storage.models_project import PaperAnalysis
    with get_project_session(slug) as s:
        n = s.execute(delete(PaperAnalysis).where(
            PaperAnalysis.kind.like("%_erro"))).rowcount
        s.commit()
    if n:
        record_event("analysis.errors_deleted", operation="delete",
                     project_slug=slug, payload={"analyses": n})
    return n


def list_evidences(slug: str, page: int = 1) -> tuple[list, int]:
    """Evidências paginadas com título do paper."""
    from jatai.storage.models_project import Evidence, Paper

    with get_project_session(slug) as s:
        count_stmt = select(func.count(Evidence.id))
        total = s.scalar(count_stmt) or 0
        rows = list(s.execute(
            select(Evidence, Paper.title)
            .join(Paper, Evidence.paper_id == Paper.id)
            .order_by(Evidence.created_at.desc(), Evidence.id.desc())
            .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)))
        return rows, total


# ---------- Avaliação (grades de notas AQ) ----------

def quality_scores_bulk(slug: str, paper_ids: list[int]) -> dict[tuple[int, int], float]:
    from jatai.storage.models_project import QualityScore

    if not paper_ids:
        return {}
    with get_project_session(slug) as s:
        rows = s.execute(
            select(QualityScore.paper_id, QualityScore.criterion_id, QualityScore.score)
            .where(QualityScore.paper_id.in_(paper_ids))).all()
        return {(pid, cid): sc for pid, cid, sc in rows}


def quality_totals_bulk(slug: str, paper_ids: list[int],
                        weights: dict[int, float]) -> dict[int, float | None]:
    """Nota final ponderada por paper (só critérios ativos)."""
    scores = quality_scores_bulk(slug, paper_ids)
    out: dict[int, float | None] = {}
    for pid in paper_ids:
        num = den = 0.0
        for (p, cid), sc in scores.items():
            if p != pid or cid not in weights:
                continue
            w = weights[cid]
            num += sc * w
            den += w
        out[pid] = round(num / den, 3) if den else None
    return out


# ---------------- deduplicação (revisão humana) ----------------


def _paper_detail(p: Paper) -> dict:
    """Resumo de um paper para exibição/merge de grupos de duplicatas."""
    return {
        "id": p.id,
        "title": p.title,
        "doi": p.doi or "",
        "url": p.url or "",
        "year": p.year,
        "venue": p.venue or "",
        "pub_type": p.pub_type or "",
        "state": p.state,
        "pdf_path": p.pdf_path or "",
        "authors": (p.authors or "[]"),
        "abstract_len": len(p.abstract or ""),
        "sources": sorted({sg.source for sg in p.sightings}),
    }


def list_dedup_groups(slug: str) -> list[dict]:
    """Grupos de possíveis duplicatas pendentes de revisão."""
    with get_project_session(slug) as s:
        rows = list(s.scalars(select(DedupGroup)
                              .where(DedupGroup.status == "pendente")
                              .order_by(DedupGroup.score.desc(), DedupGroup.id)))
        out = []
        for g in rows:
            ids = json.loads(g.paper_ids or "[]")
            papers = list(s.scalars(select(Paper).where(Paper.id.in_(ids))
                                    .options(selectinload(Paper.sightings))))
            papers.sort(key=lambda p: p.id)
            out.append({
                "id": g.id,
                "reason": g.reason,
                "score": g.score,
                "created_at": g.created_at,
                "papers": [_paper_detail(p) for p in papers],
            })
        return out


def _detail_count(p: Paper) -> int:
    """Heurística: nº de campos preenchidos — usado p/ escolher o registro
    mais completo num merge."""
    fields = ("doi", "url", "abstract", "venue", "pub_type",
              "pdf_path", "pdf_hash", "keywords", "authors")
    n = sum(1 for f in fields if getattr(p, f))
    if (p.authors or "") in ("", "[]"):
        n -= 1
    return n + (1 if p.year else 0) + (1 if p.title else 0)


def merge_dedup_group(slug: str, group_id: int, keep_id: int | None = None) -> bool:
    """Mescla os papers de um grupo dedup.

    Mantém o registro com mais detalhes (ou `keep_id` se informado),
    completa campos vazios a partir dos demais, move as proveniências
    (sightings) e remove os registros mesclados. Marca o grupo como
    'mesclado'. Retorna False se o grupo/papers não existirem."""
    with get_project_session(slug) as s:
        g = s.get(DedupGroup, group_id)
        if not g or g.status != "pendente":
            return False
        ids = json.loads(g.paper_ids or "[]")
        papers = list(s.scalars(select(Paper).where(Paper.id.in_(ids))
                                .options(selectinload(Paper.sightings))))
        if len(papers) < 2:
            g.status = "mesclado" if len(papers) <= 1 else g.status
            s.commit()
            return len(papers) >= 1
        keep = next((p for p in papers if p.id == keep_id), None) \
            or max(papers, key=_detail_count)
        for p in papers:
            if p.id == keep.id:
                continue
            for fld in ("doi", "url", "abstract", "keywords", "venue",
                        "pub_type", "authors", "pdf_path", "pdf_hash"):
                if not getattr(keep, fld) and getattr(p, fld):
                    setattr(keep, fld, getattr(p, fld))
            if not keep.year and p.year:
                keep.year = p.year
            # proveniências: move sightings que o keeper ainda não tem
            have = {(sg.source, sg.external_id) for sg in keep.sightings}
            for sg in list(p.sightings):
                if (sg.source, sg.external_id) not in have:
                    keep.sightings.append(sg)  # move via relação (cascade)
                    have.add((sg.source, sg.external_id))
                # duplicadas: cascade do delete do paper já as remove
            # notas de qualidade referenciam paper_id — migra p/ keeper;
            # se o keeper já tem nota p/ o mesmo critério, descarta a do dup
            have_crit = {qs.criterion_id for qs in s.scalars(
                select(QualityScore).where(QualityScore.paper_id == keep.id))}
            for qs in list(s.scalars(select(QualityScore)
                                     .where(QualityScore.paper_id == p.id))):
                if qs.criterion_id in have_crit:
                    s.delete(qs)
                else:
                    qs.paper_id = keep.id
                    have_crit.add(qs.criterion_id)
            s.delete(p)
        g.status = "mesclado"
        record_event("dedup.merge", entity=f"dedup_group:{g.id}",
                     operation="merge", project_slug=slug,
                     payload={"kept": keep.id,
                              "removed": [p.id for p in papers
                                          if p.id != keep.id]})
        s.commit()
        return True


def dismiss_dedup_group(slug: str, group_id: int) -> bool:
    """Marca o grupo como 'ignorado' (artigos não são duplicatas)."""
    with get_project_session(slug) as s:
        g = s.get(DedupGroup, group_id)
        if not g or g.status != "pendente":
            return False
        g.status = "ignorado"
        record_event("dedup.dismiss", entity=f"dedup_group:{g.id}",
                     operation="dismiss", project_slug=slug)
        s.commit()
        return True
