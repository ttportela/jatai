"""Triagem profunda (deep_screener): segunda fase da triagem.

Reavalia TODOS os critérios IC/EC contra o texto completo do PDF dos
artigos incluídos na triagem título/resumo. Reusa os schemas, regras
determinísticas e persistência de screening.py — só muda a fonte
(texto completo), o kind da análise ("triagem_profunda") e o stage
registrado no payload ("texto_completo").
"""

from jinja2.sandbox import SandboxedEnvironment

from jatai.agents import screening
from jatai.storage.db import get_project_session
from jatai.storage.models_project import Paper, PromptTemplate

# Etapa 2 avalia todos os critérios IC/EC (o full text cobre tanto os
# critérios de título/resumo quanto os exclusivos de texto completo).
DEEP_STAGES: tuple[str, ...] | None = None

DEFAULT_TEMPLATE = """\
Você é um revisor de triagem de uma revisão sistemática de literatura.
Esta é a SEGUNDA fase da triagem: o artigo já foi preliminarmente incluído
pela avaliação de título e resumo. Agora avalie o TEXTO COMPLETO do artigo
contra CADA critério de inclusão (IC) e exclusão (EC).

Regras:
- Avalie cada critério individualmente, na ordem apresentada.
- Para IC, "met"=true significa que o artigo atende ao critério de inclusão.
- Para EC, "met"=true significa que o motivo de exclusão SE APLICA ao artigo.
- "verdict" é o efeito do critério sobre a inclusão: "incluir" quando o
  critério favorece incluir (IC atendido ou EC que não se aplica),
  "excluir" quando favorece excluir (IC violado ou EC que se aplica).
- Use met=null e verdict="inconclusivo" quando o texto não trouxer informação
  suficiente — nunca invente dados.
- evidence_quote deve ser um trecho literal do texto do artigo; use null
  quando não houver trecho que fundamente a avaliação.
- Em caso de dúvida, prefira "inconclusivo"/"talvez" — a triagem profunda
  pode reverter a inclusão se o texto completo não sustentar os critérios.

Artigo:
- Título: {{ paper.title }}
- Ano: {{ paper.year if paper.year is not none else 'desconhecido' }}
- Periódico/evento: {{ paper.venue or 'desconhecido' }}
- Fonte: {{ paper.source or 'desconhecida' }}
- Palavras-chave: {{ paper.keywords or '—' }}
- Autores: {{ paper.authors | join(', ') if paper.authors else '—' }}
- Resumo (metadados): {{ paper.abstract if paper.abstract else '(indisponível)' }}

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
Texto completo do artigo (extraído do PDF):
{{ full_text }}

Responda avaliando todos os critérios listados acima.
"""


def deep_criteria(criteria) -> list:
    """Critérios da etapa 2: todos os IC/EC (o full text é fonte mais rica
    que título/resumo, então reavalia inclusive os de título/resumo)."""
    return list(criteria)


def build_prompt(slug: str, task, paper: Paper, criteria: list,
                 questions: list, full_text: str) -> tuple[str, str, str]:
    """Renderiza o PromptTemplate configurado na tarefa (ou o padrão).

    Retorna (prompt_renderizado, nome_template, versao_template).
    """
    tpl = None
    raw_id = str(getattr(task, "prompt_template", "") or "").strip()
    if raw_id.isdigit():
        with get_project_session(slug) as s:
            tpl = s.get(PromptTemplate, int(raw_id))
    name = tpl.name if tpl else "deep-screener-default"
    version = tpl.version if tpl else "1"
    source = tpl.content if tpl and tpl.content else DEFAULT_TEMPLATE

    env = SandboxedEnvironment(autoescape=False, trim_blocks=True, lstrip_blocks=True)
    content = env.from_string(source).render(
        paper=screening._paper_context(paper),
        criteria=[{"code": c.code, "kind": "IC" if c.type == "inclusao" else "EC",
                   "required": c.required, "description": c.description}
                  for c in criteria],
        questions=[{"code": q.code, "question": q.question, "required": q.required}
                   for q in questions],
        full_text=full_text,
    )
    return content, name, version


def apply_rules(result, criteria: list, min_confidence: float):
    """Mesmas regras determinísticas da triagem, sem filtro de etapa."""
    return screening.apply_rules(result, criteria, min_confidence,
                                 stages=DEEP_STAGES)


def persist(slug: str, paper_id: int, criteria: list, result,
            verdict: str, reasons: list[str], iteration: int,
            model: str = "") -> None:
    """Persiste como 'triagem_profunda' (stage=texto_completo)."""
    screening.persist(slug, paper_id, criteria, result, verdict, reasons,
                      iteration, kind="triagem_profunda",
                      stage="texto_completo", stages=DEEP_STAGES,
                      model=model)
