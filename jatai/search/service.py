"""Serviço de geração de consultas: configuração -> QueryDefinition -> fontes."""

import json

from sqlalchemy import select

from jatai.research import service as research
from jatai.search import builder
from jatai.search.adapters.base import get_adapter
from jatai.search.domain import QueryDefinition
from jatai.storage.db import get_project_session
from jatai.storage.models_project import Query

DEFAULT_MAX_PER_SOURCE = 25


def build_definition(slug: str) -> QueryDefinition | None:
    return builder.definition_from_areas(slug)


def compile_for_source(definition: QueryDefinition, source: str,
                       limit: int = DEFAULT_MAX_PER_SOURCE) -> list[str]:
    adapter = get_adapter(source)
    if adapter is None:
        return []
    return adapter.compile(definition, limit=limit)


def preview(slug: str, sources: list[str] | None = None,
            limit: int = DEFAULT_MAX_PER_SOURCE) -> dict:
    """Gera consultas sem persistir — para visualização na UI."""
    definition = build_definition(slug)
    if definition is None:
        return {"definition": None, "logical": "", "queries": {}}
    if sources is None:
        sources = [s.source_type for s in research.list_sources(slug) if s.enabled]
    queries = {}
    for src in sources:
        queries[src] = compile_for_source(definition, src, limit)
    return {
        "definition": definition.to_expression(),
        "logical": definition.to_expression(),
        "queries": queries,
    }


def _existing_keys(slug: str) -> set[tuple[str, str]]:
    with get_project_session(slug) as s:
        rows = s.execute(select(Query.source, Query.expression)).all()
    return {(src or "", expr.strip().lower()) for src, expr in rows}


def generate(slug: str, sources: list[str] | None = None,
             limit: int = DEFAULT_MAX_PER_SOURCE,
             origin: str = "agent") -> dict:
    """Gera e persiste consultas traduzidas por fonte (dedup contra existentes)."""
    definition = build_definition(slug)
    if definition is None:
        return {"created": 0, "per_source": {}, "logical": ""}
    if sources is None:
        sources = [s.source_type for s in research.list_sources(slug) if s.enabled]
    logical = definition.to_expression()
    existing = _existing_keys(slug)
    per_source: dict[str, int] = {}
    created = 0
    for src in sources:
        exprs = compile_for_source(definition, src, limit)
        n = 0
        for expr in exprs:
            if (src, expr.lower()) in existing:
                continue
            research.create_query(
                slug, expression=expr, source=src, origin=origin,
                related_terms=logical, status="pendente")
            existing.add((src, expr.lower()))
            n += 1
        per_source[src] = n
        created += n
    return {"created": created, "per_source": per_source, "logical": logical}
