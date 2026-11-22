"""CRUD da configuração de pesquisa (banco por-projeto)."""

from sqlalchemy import delete, func, select, text

from lelia.audit.log import record_event
from lelia.storage.db import get_project_session
from lelia.storage.models_project import (
    CatalogField,
    Criterion,
    Keyword,
    ProjectConfig,
    ProjectSource,
    Query,
    ResearchArea,
    ResearchQuestion,
)


# ---------- Identificação (ProjectConfig) ----------

def get_config(slug: str) -> dict[str, str]:
    with get_project_session(slug) as s:
        return {r.key: r.value for r in s.scalars(select(ProjectConfig))}


def set_config(slug: str, values: dict[str, str]) -> None:
    with get_project_session(slug) as s:
        for k, v in values.items():
            row = s.get(ProjectConfig, k)
            if row:
                row.value = v
            else:
                s.add(ProjectConfig(key=k, value=v))
        s.commit()
    record_event("config.updated", entity="project_config", operation="update", project_slug=slug)


# ---------- Áreas e palavras-chave ----------

def list_areas(slug: str) -> list[ResearchArea]:
    from sqlalchemy.orm import selectinload

    with get_project_session(slug) as s:
        return list(s.scalars(
            select(ResearchArea).options(selectinload(ResearchArea.keywords))
            .order_by(ResearchArea.position, ResearchArea.id)))


def save_area(slug: str, area_id: int | None, name: str, status: str,
              keywords: list[str]) -> int:
    with get_project_session(slug) as s:
        if area_id:
            area = s.get(ResearchArea, area_id)
        else:
            area = ResearchArea(name=name, status=status,
                                position=len(s.scalars(select(ResearchArea)).all()))
            s.add(area)
        if area is None:
            return -1
        area.name = name
        area.status = status
        s.flush()
        s.execute(delete(Keyword).where(Keyword.area_id == area.id))
        for term in keywords:
            term = term.strip()
            if term:
                s.add(Keyword(area_id=area.id, term=term))
        s.commit()
        aid = area.id
    record_event("area.saved", entity=f"research_area:{aid}", operation="upsert", project_slug=slug)
    return aid


def delete_area(slug: str, area_id: int) -> None:
    with get_project_session(slug) as s:
        area = s.get(ResearchArea, area_id)
        if area:
            s.delete(area)
            s.commit()
    record_event("area.deleted", entity=f"research_area:{area_id}", operation="delete", project_slug=slug)


# ---------- Questões de pesquisa ----------

def list_questions(slug: str) -> list[ResearchQuestion]:
    with get_project_session(slug) as s:
        return list(s.scalars(select(ResearchQuestion).order_by(ResearchQuestion.position, ResearchQuestion.id)))


def save_question(slug: str, rq_id: int | None, code: str, question: str,
                  answer_type: str, required: bool, position: int) -> int:
    with get_project_session(slug) as s:
        if rq_id:
            rq = s.get(ResearchQuestion, rq_id)
        else:
            rq = ResearchQuestion(code=code, question=question)
        if rq is None:
            return -1
        rq.code, rq.question = code, question
        rq.answer_type, rq.required, rq.position = answer_type, required, position
        s.add(rq)
        s.commit()
        rid = rq.id
    record_event("rq.saved", entity=f"research_question:{rid}", operation="upsert", project_slug=slug)
    return rid


def delete_question(slug: str, rq_id: int) -> None:
    with get_project_session(slug) as s:
        rq = s.get(ResearchQuestion, rq_id)
        if rq:
            s.delete(rq)
            s.commit()
    record_event("rq.deleted", entity=f"research_question:{rq_id}", operation="delete", project_slug=slug)


# ---------- Critérios ----------

def list_criteria(slug: str) -> list[Criterion]:
    with get_project_session(slug) as s:
        return list(s.scalars(select(Criterion).order_by(Criterion.position, Criterion.id)))


def save_criterion(slug: str, crit_id: int | None, code: str, type_: str,
                   description: str, applies_to: str, required: bool, position: int) -> int:
    with get_project_session(slug) as s:
        if crit_id:
            c = s.get(Criterion, crit_id)
        else:
            c = Criterion(code=code, type=type_, description=description)
        if c is None:
            return -1
        c.code, c.type, c.description = code, type_, description
        c.applies_to, c.required, c.position = applies_to, required, position
        s.add(c)
        s.commit()
        cid = c.id
    record_event("criterion.saved", entity=f"criterion:{cid}", operation="upsert", project_slug=slug)
    return cid


def delete_criterion(slug: str, crit_id: int) -> None:
    with get_project_session(slug) as s:
        c = s.get(Criterion, crit_id)
        if c:
            s.delete(c)
            s.commit()
    record_event("criterion.deleted", entity=f"criterion:{crit_id}", operation="delete", project_slug=slug)


# ---------- Caracterização científica ----------

def list_catalog_fields(slug: str) -> list[CatalogField]:
    with get_project_session(slug) as s:
        return list(s.scalars(select(CatalogField).order_by(CatalogField.position, CatalogField.id)))


def save_catalog_field(slug: str, field_id: int | None, **kw) -> int:
    with get_project_session(slug) as s:
        if field_id:
            f = s.get(CatalogField, field_id)
        else:
            f = CatalogField(identifier=kw["identifier"], label=kw["label"])
        if f is None:
            return -1
        for k in ("identifier", "label", "description", "data_type", "origin",
                  "required", "active", "llm_instruction", "rq_code", "position"):
            if k in kw:
                setattr(f, k, kw[k])
        s.add(f)
        s.commit()
        fid = f.id
    record_event("catalog_field.saved", entity=f"catalog_field:{fid}", operation="upsert", project_slug=slug)
    return fid


def delete_catalog_field(slug: str, field_id: int) -> None:
    with get_project_session(slug) as s:
        f = s.get(CatalogField, field_id)
        if f:
            s.delete(f)
            s.commit()
    record_event("catalog_field.deleted", entity=f"catalog_field:{field_id}", operation="delete", project_slug=slug)


# ---------- Fontes ----------

AVAILABLE_SOURCES = [
    ("dblp", "DBLP"),
    ("openalex", "OpenAlex"),
    ("crossref", "Crossref"),
    ("arxiv", "arXiv"),
    ("pubmed", "PubMed"),
    ("semantic_scholar", "Semantic Scholar"),
    ("core", "CORE"),
    ("scopus", "Scopus"),
    ("ieee", "IEEE"),
    ("sciencedirect", "ScienceDirect"),
    ("scielo", "SciELO"),
    ("acm", "ACM"),
]

# Página oficial de cada fonte (aberta em nova aba na tela Fontes)
SOURCE_HOMEPAGES = {
    "dblp": "https://dblp.org",
    "openalex": "https://openalex.org",
    "crossref": "https://www.crossref.org",
    "arxiv": "https://arxiv.org",
    "pubmed": "https://pubmed.ncbi.nlm.nih.gov",
    "semantic_scholar": "https://www.semanticscholar.org",
    "core": "https://core.ac.uk",
    "scopus": "https://www.scopus.com",
    "ieee": "https://ieeexplore.ieee.org",
    "sciencedirect": "https://www.sciencedirect.com",
    "scielo": "https://search.scielo.org",
    "acm": "https://dl.acm.org",
}


def ensure_default_sources(slug: str) -> None:
    with get_project_session(slug) as s:
        existing = {r.source_type for r in s.scalars(select(ProjectSource))}
        for i, (stype, name) in enumerate(AVAILABLE_SOURCES):
            if stype not in existing:
                s.add(ProjectSource(source_type=stype, name=name,
                                    enabled=(stype == "dblp"), config={}))
        s.commit()


def list_sources(slug: str) -> list[ProjectSource]:
    ensure_default_sources(slug)
    with get_project_session(slug) as s:
        return list(s.scalars(select(ProjectSource).order_by(ProjectSource.id)))


def set_source_enabled(slug: str, source_id: int, enabled: bool) -> None:
    with get_project_session(slug) as s:
        src = s.get(ProjectSource, source_id)
        if src:
            src.enabled = enabled
            s.commit()
    record_event("source.toggled", entity=f"project_source:{source_id}", operation="update",
                 project_slug=slug, payload={"enabled": enabled})


def update_source_config(slug: str, source_id: int,
                         values: dict[str, str]) -> None:
    """Mescla chaves na config da fonte (ex.: api_key). String vazia
    remove a chave."""
    with get_project_session(slug) as s:
        src = s.get(ProjectSource, source_id)
        if not src:
            return
        cfg = dict(src.config or {})
        for k, v in values.items():
            v = (v or "").strip()
            if v:
                cfg[k] = v
            else:
                cfg.pop(k, None)
        src.config = cfg
        s.commit()
    record_event("source.config", entity=f"project_source:{source_id}",
                 operation="update", project_slug=slug,
                 payload={"keys": sorted(values)})


# ---------- Consultas ----------

def _ensure_query_schema(s) -> None:
    """Garante colunas adicionadas após a criação inicial do banco."""
    cols = {r[1] for r in s.execute(text("PRAGMA table_info(query)"))}
    if "enabled" not in cols:
        s.execute(text("ALTER TABLE query ADD COLUMN enabled BOOLEAN DEFAULT 1"))
        s.commit()
        cols = {r[1] for r in s.execute(text("PRAGMA table_info(query)"))}
    if "sort_order" not in cols:
        s.execute(text("ALTER TABLE query ADD COLUMN sort_order INTEGER"))
        # backfill: ordem de criação como ordem inicial de execução
        s.execute(text("UPDATE query SET sort_order = id"))
        s.commit()


def list_queries(slug: str) -> list[Query]:
    """Consultas na ordem de execução definida pelo usuário
    (sort_order; desempate por criação)."""
    with get_project_session(slug) as s:
        _ensure_query_schema(s)
        return list(s.scalars(select(Query)
                              .order_by(Query.sort_order.asc().nulls_last(),
                                        Query.id.asc())))


def create_query(slug: str, expression: str, source: str, origin: str = "manual",
                 related_terms: str = "", status: str = "pendente") -> int:
    with get_project_session(slug) as s:
        _ensure_query_schema(s)
        nxt = s.scalar(select(func.max(Query.sort_order))) or 0
        q = Query(expression=expression, source=source, origin=origin,
                  related_terms=related_terms, status=status,
                  sort_order=nxt + 1)
        s.add(q)
        s.commit()
        qid = q.id
    record_event("query.created", entity=f"query:{qid}", operation="create",
                 project_slug=slug, payload={"expression": expression, "source": source})
    return qid


def update_query(slug: str, query_id: int, expression: str,
                 source: str) -> None:
    with get_project_session(slug) as s:
        _ensure_query_schema(s)
        q = s.get(Query, query_id)
        if q:
            q.expression = expression
            q.source = source
            s.commit()
    record_event("query.updated", entity=f"query:{query_id}", operation="update",
                 project_slug=slug, payload={"expression": expression})


def set_query_enabled(slug: str, query_id: int, enabled: bool) -> None:
    with get_project_session(slug) as s:
        _ensure_query_schema(s)
        q = s.get(Query, query_id)
        if q:
            q.enabled = enabled
            s.commit()
    record_event("query.enabled", entity=f"query:{query_id}", operation="update",
                 project_slug=slug, payload={"enabled": enabled})


def delete_query(slug: str, query_id: int) -> None:
    with get_project_session(slug) as s:
        q = s.get(Query, query_id)
        if q:
            s.delete(q)
            s.commit()
    record_event("query.deleted", entity=f"query:{query_id}", operation="delete", project_slug=slug)


def set_query_status(slug: str, query_id: int, status: str) -> None:
    with get_project_session(slug) as s:
        q = s.get(Query, query_id)
        if q:
            q.status = status
            s.commit()
    record_event("query.status", entity=f"query:{query_id}", operation="update",
                 project_slug=slug, payload={"status": status})


def move_query(slug: str, query_id: int, direction: str) -> bool:
    """Move a consulta um passo na ordem de execução ('up'|'down').

    Regrava sort_order como sequência 1..N para manter a ordem estável
    mesmo após inserções/remoções. Retorna False se não havia para onde
    mover."""
    direction = (direction or "").strip().lower()
    if direction not in ("up", "down"):
        return False
    with get_project_session(slug) as s:
        _ensure_query_schema(s)
        queries = list(s.scalars(select(Query)
                                 .order_by(Query.sort_order.asc().nulls_last(),
                                           Query.id.asc())))
        idx = next((i for i, q in enumerate(queries) if q.id == query_id), None)
        if idx is None:
            return False
        j = idx - 1 if direction == "up" else idx + 1
        if j < 0 or j >= len(queries):
            return False
        queries[idx], queries[j] = queries[j], queries[idx]
        for i, q in enumerate(queries, start=1):
            q.sort_order = i
        s.commit()
    record_event("query.reorder", entity=f"query:{query_id}", operation="update",
                 project_slug=slug, payload={"direction": direction})
    return True


# ---------- Avaliação da Qualidade dos Estudos (AQ) ----------

def list_quality_criteria(slug: str) -> list:
    from lelia.storage.models_project import QualityCriterion

    with get_project_session(slug) as s:
        return list(s.scalars(select(QualityCriterion)
                              .order_by(QualityCriterion.position, QualityCriterion.id)))


def save_quality_criterion(slug: str, crit_id: int | None, code: str,
                           description: str, weight: float, required: bool,
                           active: bool, position: int) -> int:
    from lelia.storage.models_project import QualityCriterion

    with get_project_session(slug) as s:
        if crit_id:
            c = s.get(QualityCriterion, crit_id)
        else:
            c = QualityCriterion(code=code)
        if c is None:
            return -1
        c.code, c.description = code, description
        c.weight, c.required, c.active, c.position = weight, required, active, position
        s.add(c)
        s.commit()
        cid = c.id
    record_event("quality_criterion.saved", entity=f"quality_criterion:{cid}",
                 operation="upsert", project_slug=slug)
    return cid


def delete_quality_criterion(slug: str, crit_id: int) -> None:
    from lelia.storage.models_project import QualityCriterion, QualityScore

    with get_project_session(slug) as s:
        c = s.get(QualityCriterion, crit_id)
        if c:
            s.execute(delete(QualityScore).where(QualityScore.criterion_id == crit_id))
            s.delete(c)
            s.commit()
    record_event("quality_criterion.deleted", entity=f"quality_criterion:{crit_id}",
                 operation="delete", project_slug=slug)


def next_quality_code(slug: str) -> str:
    criteria = list_quality_criteria(slug)
    n = len(criteria) + 1
    return f"AQ{n:02d}"
