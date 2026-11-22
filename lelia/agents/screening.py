"""Triagem título+resumo (screener): prompt Jinja2, saída estruturada
Pydantic e regras determinísticas de decisão.

O modelo avalia cada critério IC/EC individualmente; as regras locais
forçam `excluir`/`talvez` conforme obrigatoriedade, inconclusivos e
confiança mínima — a decisão final nunca depende só do LLM.
"""

import json
from typing import Literal

from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel, Field

from lelia.storage.db import get_project_session
from lelia.storage.models_project import (
    Decision,
    Evidence,
    Paper,
    PaperAnalysis,
    PromptTemplate,
)

CRITERIA_STAGES = ("titulo_resumo", "ambos")
VERDICT_STATE = {"incluir": "incluido", "excluir": "excluido", "talvez": "talvez"}


class CriterionEval(BaseModel):
    criterion_id: str = Field(description="Código do critério avaliado (ex.: IC1, EC2)")
    met: bool | None = Field(
        default=None,
        description="true se a condição do critério se aplica ao texto; "
                    "false se violada; null se não houver informação suficiente")
    verdict: Literal["incluir", "excluir", "inconclusivo"] = Field(
        description="Efeito do critério sobre a decisão de inclusão do artigo: "
                    "incluir = favorece incluir (IC atendido ou EC não se aplica); "
                    "excluir = favorece excluir (IC violado ou EC se aplica); "
                    "inconclusivo = evidência insuficiente")
    justification: str = ""
    evidence_quote: str | None = Field(
        default=None,
        description="Trecho literal do título/resumo que fundamenta; null se ausente")


class ScreeningResult(BaseModel):
    evaluations: list[CriterionEval] = Field(default_factory=list)
    overall_verdict: Literal["incluir", "excluir", "talvez"]
    confidence: float = Field(ge=0.0, le=1.0)


DEFAULT_TEMPLATE = """\
Você é um revisor de triagem de uma revisão sistemática de literatura.
Avalie o artigo abaixo contra CADA critério de inclusão (IC) e exclusão (EC).

Regras:
- Avalie cada critério individualmente, na ordem apresentada.
- Para IC, "met"=true significa que o artigo atende ao critério de inclusão.
- Para EC, "met"=true significa que o motivo de exclusão SE APLICA ao artigo.
- "verdict" é o efeito do critério sobre a inclusão: "incluir" quando o
  critério favorece incluir (IC atendido ou EC que não se aplica),
  "excluir" quando favorece excluir (IC violado ou EC que se aplica).
- Use met=null e verdict="inconclusivo" quando o texto não trouxer informação
  suficiente — nunca invente dados (ex.: resumo ausente).
- evidence_quote deve ser um trecho literal do título ou resumo; use null
  quando não houver trecho que fundamente a avaliação.
- Em caso de dúvida, prefira "inconclusivo"/"talvez".

Artigo:
- Título: {{ paper.title }}
- Ano: {{ paper.year if paper.year is not none else 'desconhecido' }}
- Periódico/evento: {{ paper.venue or 'desconhecido' }}
- Fonte: {{ paper.source or 'desconhecida' }}
- Palavras-chave: {{ paper.keywords or '—' }}
- Autores: {{ paper.authors | join(', ') if paper.authors else '—' }}
- Resumo: {{ paper.abstract if paper.abstract else '(resumo não disponível)' }}

Critérios:
{% for c in criteria -%}
- {{ c.code }} ({{ c.kind }}{{ ', obrigatório' if c.required else '' }}): {{ c.description }}
{% endfor %}
{% if questions -%}
Questões de pesquisa (contexto da revisão):
{% for q in questions -%}
- {{ q.code }}: {{ q.question }}
{% endfor %}
{% endif -%}
Responda avaliando todos os critérios listados acima.
"""


def screening_criteria(criteria) -> list:
    """Critérios aplicáveis à triagem por título/resumo, na ordem configurada."""
    return [c for c in criteria if c.applies_to in CRITERIA_STAGES]


def _paper_context(paper: Paper) -> dict:
    try:
        authors = json.loads(paper.authors or "[]")
    except json.JSONDecodeError:
        authors = []
    return {
        "id": paper.id,
        "title": paper.title,
        "abstract": paper.abstract or "",
        "year": paper.year,
        "venue": paper.venue or "",
        "source": paper.source or "",
        "keywords": paper.keywords or "",
        "authors": authors if isinstance(authors, list) else [],
    }


def build_prompt(slug: str, task, paper: Paper, criteria: list,
                 questions: list) -> tuple[str, str, str]:
    """Renderiza o PromptTemplate configurado na tarefa (ou o padrão embutido).

    Retorna (prompt_renderizado, nome_template, versao_template).
    """
    tpl = None
    raw_id = str(getattr(task, "prompt_template", "") or "").strip()
    if raw_id.isdigit():
        with get_project_session(slug) as s:
            tpl = s.get(PromptTemplate, int(raw_id))
    name = tpl.name if tpl else "screener-default"
    version = tpl.version if tpl else "1"
    source = tpl.content if tpl and tpl.content else DEFAULT_TEMPLATE

    env = SandboxedEnvironment(autoescape=False, trim_blocks=True, lstrip_blocks=True)
    content = env.from_string(source).render(
        paper=_paper_context(paper),
        criteria=[{"code": c.code, "kind": "IC" if c.type == "inclusao" else "EC",
                   "required": c.required, "description": c.description}
                  for c in criteria],
        questions=[{"code": q.code, "question": q.question, "required": q.required}
                   for q in questions],
    )
    return content, name, version


def _evals_by_code(result: ScreeningResult) -> dict[str, CriterionEval]:
    return {e.criterion_id.strip().upper(): e for e in result.evaluations}


def apply_rules(result: ScreeningResult, criteria: list,
                min_confidence: float,
                stages: tuple[str, ...] | None = CRITERIA_STAGES
                ) -> tuple[str, list[str]]:
    """Regras determinísticas sobre o veredito do LLM.

    - EC obrigatório atendido (met=true) ou IC obrigatório violado
      (met=false)  -> excluir
    - critério obrigatório inconclusivo (met=null ou ausente) ou
      confidence < min_confidence -> rebaixa incluir para talvez

    `stages` filtra os critérios por applies_to; None = todos.
    """
    by_code = _evals_by_code(result)
    reasons: list[str] = []
    inconclusive = False
    applicable = ([c for c in criteria if c.applies_to in stages]
                  if stages is not None else list(criteria))
    for c in applicable:
        if not c.required:
            continue
        ev = by_code.get(c.code.strip().upper())
        met = ev.met if ev else None
        if met is None:
            inconclusive = True
        elif c.type == "exclusao" and met is True:
            reasons.append(f"{c.code}: critério de exclusão obrigatório atendido")
            return "excluir", reasons
        elif c.type == "inclusao" and met is False:
            reasons.append(f"{c.code}: critério de inclusão obrigatório violado")
            return "excluir", reasons
    if inconclusive:
        reasons.append("critério obrigatório inconclusivo")
    if result.confidence < min_confidence:
        reasons.append(f"confidence {result.confidence:.2f} abaixo do limiar {min_confidence:.2f}")
    verdict = result.overall_verdict
    if verdict == "incluir" and reasons:
        verdict = "talvez"
    return verdict, reasons


def _criterion_decision_verdict(criterion, met: bool | None) -> str:
    """Mapeia met do critério para o veredito registrado na decisão."""
    if met is None:
        return "talvez"
    if criterion.type == "inclusao":
        return "incluir" if met else "excluir"
    return "excluir" if met else "incluir"


def persist(slug: str, paper_id: int, criteria: list, result: ScreeningResult,
            verdict: str, reasons: list[str], iteration: int,
            *, kind: str = "triagem", stage: str = "titulo_resumo",
            stages: tuple[str, ...] | None = CRITERIA_STAGES,
            model: str = "") -> None:
    """Grava estado do paper, decisão por critério (origin=agent),
    evidências e a análise completa para a tela de análise."""
    by_code = _evals_by_code(result)
    state = VERDICT_STATE.get(verdict, "talvez")
    applicable = ([c for c in criteria if c.applies_to in stages]
                  if stages is not None else list(criteria))
    with get_project_session(slug) as s:
        p = s.get(Paper, paper_id)
        if p is not None:
            p.state = state
        for c in applicable:
            ev = by_code.get(c.code.strip().upper())
            if ev is None:
                met, justification = None, "critério não avaliado pelo modelo"
            else:
                met, justification = ev.met, ev.justification or ""
            d = Decision(paper_id=paper_id, criterion_code=c.code,
                         verdict=_criterion_decision_verdict(c, met),
                         justification=justification[:2000],
                         origin="agent", iteration=iteration)
            s.add(d)
            s.flush()
            quote = (ev.evidence_quote if ev else None) or ""
            quote = quote.strip()
            if quote:
                s.add(Evidence(paper_id=paper_id, decision_id=d.id,
                               excerpt=quote[:2000]))
        note = "; ".join(reasons)
        conf = f"confidence={result.confidence:.2f}"
        s.add(Decision(
            paper_id=paper_id, criterion_code="", verdict=verdict,
            justification=(f"{conf}" + (f" — {note}" if note else ""))[:2000],
            origin="agent", iteration=iteration))
        payload = result.model_dump(mode="json")
        payload["stage"] = stage
        payload["applied_verdict"] = verdict
        payload["rule_notes"] = reasons
        payload["model"] = model
        s.add(PaperAnalysis(paper_id=paper_id, kind=kind,
                            result=json.dumps(payload, ensure_ascii=False)[:8000]))
        s.commit()
