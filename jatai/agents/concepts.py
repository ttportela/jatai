"""Extração de conceitos/termos a partir do texto completo do artigo.

Usado pela task `concept_extraction` do agente: saída estruturada
Pydantic, prompt Jinja2 configurável via PromptTemplate e deduplicação
contra os conceitos já existentes no projeto.
"""

import json

from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel, Field
from sqlalchemy import select

from jatai.storage.db import get_project_session
from jatai.storage.models_project import (
    Concept, ConceptPaper, Paper, PaperAnalysis, PromptTemplate)


class ConceptItem(BaseModel):
    term: str = Field(description="Conceito, técnica ou métrica — forma curta e canônica")
    description: str = Field(default="", description="Descrição fiel ao texto do artigo")
    related_terms: list[str] = Field(default_factory=list)


class ConceptsResult(BaseModel):
    concepts: list[ConceptItem] = Field(default_factory=list, max_length=20)


DEFAULT_TEMPLATE = """\
Você é um analista de revisões sistemáticas. Extraia os principais conceitos,
técnicas, métodos e métricas mencionados no TEXTO COMPLETO do artigo abaixo.

Regras:
- Retorne apenas conceitos relevantes para a área da revisão — nada genérico
  (ex.: "estudo", "análise", "resultado"). Priorize os ligados às áreas de
  pesquisa configuradas abaixo.
- term deve ser a forma curta e canônica (ex.: "regressão logística",
  "F1-score", "fine-tuning").
- description deve ser fiel ao texto — nunca invente conceitos que o artigo
  não discute.
- related_terms lista sinônimos/variantes úteis para busca, se houver.
- Não repita conceitos já listados em "Conceitos já catalogados".
{% if max_concepts -%}
- Retorne no máximo {{ max_concepts }} conceitos — apenas os mais relevantes
  e específicos do artigo.
{% else -%}
- Prefira qualidade a quantidade: tipicamente 3 a 10 conceitos por artigo.
{% endif -%}

Artigo:
- Título: {{ paper.title }}
- Ano: {{ paper.year if paper.year is not none else 'desconhecido' }}
- Periódico/evento: {{ paper.venue or 'desconhecido' }}
- Fonte: {{ paper.source or 'desconhecida' }}
- Palavras-chave: {{ paper.keywords or '—' }}
- Autores: {{ paper.authors | join(', ') if paper.authors else '—' }}
- Resumo (metadados): {{ paper.abstract if paper.abstract else '(indisponível)' }}

Conceitos já catalogados no projeto (não repita):
{% for t in existing_terms -%}
- {{ t }}
{% else -%}
(nenhum)
{% endfor %}

{% if areas -%}
Áreas de pesquisa da revisão — priorize conceitos relevantes a elas:
{% for a in areas -%}
- {{ a.name }}{% if a.keywords %} (palavras-chave: {{ a.keywords | join(', ') }}){% endif %}
{% endfor %}

{% endif -%}
Texto completo do artigo (extraído do PDF):
{{ full_text }}
"""


def _paper_context(paper: Paper) -> dict:
    try:
        authors = json.loads(paper.authors or "[]")
    except json.JSONDecodeError:
        authors = []
    return {
        "title": paper.title,
        "abstract": paper.abstract or "",
        "year": paper.year,
        "venue": paper.venue or "",
        "source": paper.source or "",
        "keywords": paper.keywords or "",
        "authors": authors if isinstance(authors, list) else [],
    }


def build_prompt(slug: str, task, paper: Paper, full_text: str,
                 existing_terms: list[str], max_concepts: int = 0,
                 areas: list[dict] | None = None) -> tuple[str, str, str]:
    """Renderiza o PromptTemplate configurado na tarefa (ou o padrão
    embutido). Retorna (prompt_renderizado, nome_template, versao)."""
    tpl = None
    raw_id = str(getattr(task, "prompt_template", "") or "").strip()
    if raw_id.isdigit():
        with get_project_session(slug) as s:
            tpl = s.get(PromptTemplate, int(raw_id))
    name = tpl.name if tpl else "concept-extraction-default"
    version = tpl.version if tpl else "1"
    source = tpl.content if tpl and tpl.content else DEFAULT_TEMPLATE

    env = SandboxedEnvironment(autoescape=False, trim_blocks=True, lstrip_blocks=True)
    content = env.from_string(source).render(
        paper=_paper_context(paper),
        existing_terms=existing_terms[:300],
        full_text=full_text,
        max_concepts=max_concepts or 0,
        areas=areas or [],
    )
    return content, name, version


def persist(slug: str, paper_id: int, result: ConceptsResult,
            iteration: int, model_ref: str = "",
            existing: dict[str, int] | None = None) -> int:
    """Cria Concepts novos e incrementa a frequência dos já existentes.

    `existing`: mapa term.lower() -> concept.id já carregado (evita query
    por termo e sobrevive entre sessões). Retorna o número de conceitos
    novos criados; o dict é atualizado com os novos ids.
    """
    created = 0
    seen: set[str] = set()
    touched: list[int] = []  # ids de conceitos extraídos deste artigo
    with get_project_session(slug) as s:
        if existing is None:
            existing = {c.term.lower(): c.id
                        for c in s.scalars(select(Concept))}
        linked = set(s.scalars(
            select(ConceptPaper.concept_id).where(
                ConceptPaper.paper_id == paper_id)))
        for item in result.concepts:
            term = (item.term or "").strip()
            key = term.lower()
            if not term or key in seen:
                continue
            seen.add(key)
            if key in existing:
                cid = existing[key]
                row = s.get(Concept, cid)
                if row is not None:
                    row.frequency = (row.frequency or 1) + 1
                touched.append(cid)
                continue
            c = Concept(
                term=term[:300],
                description=(item.description or "")[:2000],
                origin="agente",
                frequency=1,
                iteration=iteration,
                status="proposto",
                related_terms=json.dumps(item.related_terms or [],
                                         ensure_ascii=False)[:4000],
            )
            s.add(c)
            s.flush()
            existing[key] = c.id
            touched.append(c.id)
            created += 1
        for cid in dict.fromkeys(touched):
            if cid not in linked:
                s.add(ConceptPaper(concept_id=cid, paper_id=paper_id,
                                   origin="agente"))
        s.add(PaperAnalysis(
            paper_id=paper_id, kind="conceitos",
            result=json.dumps({
                "terms": [i.term.strip() for i in result.concepts
                          if (i.term or "").strip()],
                "concept_ids": sorted(set(touched)),
                "model": model_ref,
            }, ensure_ascii=False)[:8000]))
        s.commit()
    return created
