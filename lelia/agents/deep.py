"""Análise profunda (deep_analysis): resumo + respostas às questões de
pesquisa a partir do texto completo do PDF.

Mesma arquitetura da triagem (screening.py): saída estruturada Pydantic
validada pelo Ollama, prompt Jinja2 configurável via PromptTemplate,
regras determinísticas de pós-processamento e persistência auditável
(PaperAnalysis kind="profunda" + Evidence por trecho literal).
"""

import json

from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel, Field

from lelia.storage.db import get_project_session
from lelia.storage.models_project import Evidence, Paper, PaperAnalysis, PromptTemplate


class RQAnswer(BaseModel):
    code: str = Field(description="Código da questão respondida (ex.: RQ1)")
    addressed: bool | None = Field(
        default=None,
        description="true se o texto do artigo responde à questão; "
                    "false se o artigo não a aborda; null se inconclusivo")
    answer: str = Field(description="Resposta baseada no texto do artigo")
    evidence_quote: str | None = Field(
        default=None,
        description="Trecho literal do texto que fundamenta a resposta; "
                    "null quando não houver trecho correspondente")


class DeepResult(BaseModel):
    summary: str = Field(description="Resumo do artigo em 3-5 frases")
    answers: list[RQAnswer] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


DEFAULT_TEMPLATE = """\
Você é um analista de revisões sistemáticas. Analise o TEXTO COMPLETO do
artigo abaixo e responda a cada questão de pesquisa com base no conteúdo,
citando evidências literais.

Regras:
- Responda cada questão (RQ) individualmente, na ordem apresentada.
- addressed=true quando o artigo efetivamente responde à questão;
  addressed=false quando o tema não é abordado; null quando inconclusivo.
- answer deve ser fiel ao texto — nunca invente resultados, números ou
  conclusões que não estejam no artigo.
- evidence_quote deve ser um trecho literal do texto do artigo; use null
  quando não houver trecho que fundamente a resposta.
- Em caso de dúvida, prefira addressed=null e confidence baixa.

Artigo:
- Título: {{ paper.title }}
- Ano: {{ paper.year if paper.year is not none else 'desconhecido' }}
- Periódico/evento: {{ paper.venue or 'desconhecido' }}
- Fonte: {{ paper.source or 'desconhecida' }}
- Palavras-chave: {{ paper.keywords or '—' }}
- Autores: {{ paper.authors | join(', ') if paper.authors else '—' }}
- Resumo (metadados): {{ paper.abstract if paper.abstract else '(indisponível)' }}

Questões de pesquisa:
{% for q in questions -%}
- {{ q.code }}{% if q.required %} (obrigatória){% endif %}: {{ q.question }}
{% endfor %}

Texto completo do artigo (extraído do PDF):
{{ full_text }}

Responda com o resumo e uma entrada em "answers" para cada questão listada.
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


def build_prompt(slug: str, task, paper: Paper, questions: list,
                 full_text: str) -> tuple[str, str, str]:
    """Renderiza o PromptTemplate configurado na tarefa (ou o padrão embutido).

    Retorna (prompt_renderizado, nome_template, versao_template).
    """
    tpl = None
    raw_id = str(getattr(task, "prompt_template", "") or "").strip()
    if raw_id.isdigit():
        with get_project_session(slug) as s:
            tpl = s.get(PromptTemplate, int(raw_id))
    name = tpl.name if tpl else "deep-analysis-default"
    version = tpl.version if tpl else "1"
    source = tpl.content if tpl and tpl.content else DEFAULT_TEMPLATE

    env = SandboxedEnvironment(autoescape=False, trim_blocks=True, lstrip_blocks=True)
    content = env.from_string(source).render(
        paper=_paper_context(paper),
        questions=[{"code": q.code, "question": q.question,
                    "required": q.required} for q in questions],
        full_text=full_text,
    )
    return content, name, version


def _answers_by_code(result: DeepResult) -> dict[str, RQAnswer]:
    return {a.code.strip().upper(): a for a in result.answers}


def apply_rules(result: DeepResult, questions: list,
                min_confidence: float) -> list[str]:
    """Regras determinísticas — geram notas de qualidade (não alteram
    estado do paper, diferente da triagem).

    - questão obrigatória ausente ou inconclusiva (addressed=null)
    - resposta sem evidence_quote literal
    - confidence abaixo do limiar configurado
    """
    by_code = _answers_by_code(result)
    notes: list[str] = []
    for q in questions:
        ans = by_code.get(q.code.strip().upper())
        if ans is None:
            if q.required:
                notes.append(f"{q.code}: questão obrigatória não respondida")
            continue
        if ans.addressed is None and q.required:
            notes.append(f"{q.code}: resposta inconclusiva")
        if not (ans.evidence_quote or "").strip():
            notes.append(f"{q.code}: resposta sem evidência literal")
    if result.confidence < min_confidence:
        notes.append(
            f"confidence {result.confidence:.2f} abaixo do limiar {min_confidence:.2f}")
    return notes


def persist(slug: str, paper_id: int, result: DeepResult,
            notes: list[str], iteration: int,
            model: str = "") -> None:
    """Grava a análise completa (JSON) e as evidências literais por RQ."""
    payload = result.model_dump(mode="json")
    payload["rule_notes"] = notes
    payload["model"] = model
    with get_project_session(slug) as s:
        s.add(PaperAnalysis(paper_id=paper_id, kind="profunda",
                            result=json.dumps(payload, ensure_ascii=False)[:16000]))
        for ans in result.answers:
            quote = (ans.evidence_quote or "").strip()
            if quote:
                s.add(Evidence(paper_id=paper_id, excerpt=quote[:2000],
                               origin="agente"))
        s.commit()
