"""Geração de propostas de pesquisa a partir do catálogo.

Identifica lacunas, tendências, limitações e oportunidades nos artigos
catalogados (agregados + amostra de títulos/resumos) e usa o LLM para
sugerir novos estudos. As propostas ficam na tela Propostas para
revisão, priorização e conversão em projeto ou questão de pesquisa.
"""

import re
from collections import Counter

from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel, Field
from rapidfuzz import fuzz
from sqlalchemy import select

from jatai.agents import llm
from jatai.research import service as research
from jatai.storage.db import get_project_session
from jatai.storage.models_project import (
    Concept, Paper, PaperAnalysis, Proposal, ResearchArea)

KINDS = ("lacuna", "tendencia", "limitacao", "oportunidade")


class ProposalItem(BaseModel):
    kind: str = Field(description="lacuna|tendencia|limitacao|oportunidade")
    title: str = Field(description="Título curto e específico da proposta")
    summary: str = Field(default="", description="O que seria estudado")
    rationale: str = Field(default="",
                            description="Por que o catálogo sugere isso")
    evidence: list[dict] = Field(
        default_factory=list,
        description="[{title, detail}] — artigos do catálogo que fundamentam")
    questions: list[str] = Field(
        default_factory=list,
        description="Perguntas de pesquisa sugeridas para o novo estudo")


class ProposalsResult(BaseModel):
    proposals: list[ProposalItem] = Field(default_factory=list, max_length=10)


DEFAULT_TEMPLATE = """\
Você é um metodologista de revisões sistemáticas. Analise o panorama do
catálogo abaixo e identifique oportunidades de novas pesquisas.

Tipos de proposta (kind):
- lacuna: tema/pergunta sem cobertura suficiente no catálogo
- tendencia: linha de pesquisa em ascensão (concentração recente de artigos)
- limitacao: limitações recorrentes declaradas nos artigos que um novo
  estudo poderia superar
- oportunidade: sinergia entre conceitos/métodos que sugere estudo inédito

Regras:
- Baseie-se APENAS nos dados do catálogo — nunca invente artigos.
- Em evidence, cite o título exato de artigos listados em "Amostra do
  catálogo" e explique em detail o que cada um evidencia.
- questions: 1 a 4 perguntas de pesquisa objetivas para o novo estudo.
- Evite propostas genéricas ("mais estudos sobre X"); seja específico
  sobre método, população ou desfecho.
- Não repita propostas já existentes listadas em "Propostas já registradas".
- Retorne no máximo {{ max_proposals }} propostas, ordenadas por relevância.

Contexto do projeto de pesquisa atual:
- Título/escopo: {{ scope }}
{% if questions_cfg -%}
Perguntas de pesquisa atuais:
{% for q in questions_cfg -%}
- {{ q }}
{% endfor %}
{% endif -%}
{% if areas -%}
Áreas e palavras-chave:
{% for a in areas -%}
- {{ a.name }}{% if a.keywords %} ({{ a.keywords }}){% endif %}
{% endfor %}
{% endif -%}

Estatísticas do catálogo:
{{ stats_text }}

Conceitos mais frequentes:
{% for t, f in top_concepts -%}
- {{ t }} ({{ f }} artigos)
{% else -%}
(nenhum)
{% endfor %}

Amostra do catálogo (título — ano — veículo — resumo):
{% for p in sample -%}
- {{ p.title }} — {{ p.year or '?' }} — {{ p.venue or '?' }} — {{ p.abstract[:400] }}
{% endfor %}

Propostas já registradas (não repita):
{% for t in existing_titles -%}
- {{ t }}
{% else -%}
(nenhuma)
{% endfor %}
"""


def _norm_title(t: str) -> str:
    return re.sub(r"\W+", " ", (t or "").lower()).strip()


def _catalog_context(slug: str, sample_size: int = 60) -> dict:
    """Agregados baratos do catálogo para o prompt."""
    with get_project_session(slug) as s:
        cfg = research.get_config(slug)
        areas = [{"name": a.name, "keywords": a.keywords or ""}
                 for a in s.scalars(select(ResearchArea))]
        questions = [q.question for q in research.list_questions(slug)]
        criteria = [f"{c.code} {c.type}: {c.description[:120]}"
                    for c in research.list_criteria(slug)]

        papers = list(s.scalars(select(Paper)))
        by_state = Counter(p.state or "novo" for p in papers)
        by_source = Counter(p.source or "?" for p in papers)
        by_year = Counter(p.year for p in papers if p.year)
        kinds_count = Counter(
            a.kind for a in s.scalars(select(PaperAnalysis)))

        concepts = [(c.term, c.frequency or 1)
                    for c in s.scalars(
                        select(Concept).order_by(Concept.frequency.desc()).limit(40))]

        # amostra: incluídos mais recentes primeiro, depois demais estados
        ordered = sorted(
            papers,
            key=lambda p: (p.state == "incluido", p.year or 0),
            reverse=True)
        sample = [{
            "title": p.title,
            "year": p.year,
            "venue": p.venue or "",
            "abstract": (p.abstract or "")[:400],
        } for p in ordered[:sample_size]]

        stats = {
            "total_papers": len(papers),
            "by_state": dict(by_state),
            "by_source": dict(by_source.most_common(15)),
            "by_year": dict(sorted(by_year.items())),
            "analyses": dict(kinds_count),
            "criteria": criteria,
        }
        stats_text = "\n".join(f"- {k}: {v}" for k, v in stats.items())
        return {
            "cfg": cfg,
            "areas": areas,
            "questions": questions,
            "stats_text": stats_text,
            "top_concepts": concepts,
            "sample": sample,
            "title_to_id": {_norm_title(p.title): p.id for p in papers},
        }


def build_prompt(ctx: dict, max_proposals: int) -> str:
    scope = (ctx["cfg"].get("title") or ctx["cfg"].get("research_title")
             or "revisão científica")
    env = SandboxedEnvironment(autoescape=False, trim_blocks=True,
                                lstrip_blocks=True)
    return env.from_string(DEFAULT_TEMPLATE).render(
        max_proposals=max_proposals,
        scope=scope,
        questions_cfg=ctx["questions"],
        areas=ctx["areas"],
        stats_text=ctx["stats_text"],
        top_concepts=ctx["top_concepts"],
        sample=ctx["sample"],
        existing_titles=[p.title for p in ctx.get("_existing", [])],
    )


def generate(slug: str, *, model_ref: str = "", max_proposals: int = 8,
             run_id: int | None = None, iteration: int = 0,
             timeout: float | None = None) -> int:
    """Gera propostas via LLM e persiste as novas. Retorna quantas criou."""
    ctx = _catalog_context(slug)
    with get_project_session(slug) as s:
        ctx["_existing"] = list(s.scalars(select(Proposal.title)))
    prompt = build_prompt(ctx, max_proposals)
    result = llm.chat_structured(
        slug, model_ref=model_ref, messages=[{"role": "user", "content": prompt}],
        response_model=ProposalsResult, task="proposals",
        timeout=timeout, run_id=run_id, iteration=iteration)
    created = persist(slug, result, run_id=run_id, iteration=iteration,
                      title_to_id=ctx["title_to_id"])
    return created


def _similar(a: str, b: str) -> bool:
    na, nb = _norm_title(a), _norm_title(b)
    return na == nb or (na and nb and fuzz.token_sort_ratio(na, nb) >= 88)


def persist(slug: str, result: ProposalsResult, *, run_id=None,
            iteration=0, title_to_id=None) -> int:
    """Insere propostas novas; ignora títulos muito similares aos já
    existentes. Retorna o número criado."""
    created = 0
    with get_project_session(slug) as s:
        existing = [p.title for p in s.scalars(select(Proposal))]
        if title_to_id is None:
            title_to_id = {_norm_title(p.title): p.id
                           for p in s.scalars(select(Paper))}
        for item in result.proposals:
            kind = (item.kind or "").strip().lower()
            if kind not in KINDS:
                kind = "oportunidade"
            title = (item.title or "").strip()
            if not title or any(_similar(title, e) for e in existing):
                continue
            evidence = []
            for ev in item.evidence or []:
                pid = title_to_id.get(_norm_title(str(ev.get("title", ""))))
                evidence.append({
                    "paper_id": pid,
                    "title": str(ev.get("title", ""))[:300],
                    "detail": str(ev.get("detail", ""))[:600],
                })
            p = Proposal(
                kind=kind, title=title[:300],
                summary=(item.summary or "")[:4000],
                rationale=(item.rationale or "")[:4000],
                evidence=evidence[:20],
                questions=[str(q)[:500] for q in (item.questions or [])][:8],
                origin="agente", run_id=run_id, iteration=iteration,
                status="proposto")
            s.add(p)
            existing.append(title)
            created += 1
        s.commit()
    return created
