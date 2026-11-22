"""Extração da lista de referências bibliográficas do texto completo do
artigo (fallback do snowballing quando a fonte não tem referenced_works).

Saída estruturada Pydantic + PromptTemplate configurável, mesmo padrão
de concepts.py/classify.py.
"""

import json

from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel, Field

from lelia.storage.db import get_project_session
from lelia.storage.models_project import Paper, PromptTemplate


class ReferenceItem(BaseModel):
    title: str = Field(description="Título da obra referenciada")
    authors: list[str] = Field(default_factory=list)
    year: int | None = Field(default=None)
    doi: str = Field(default="")
    venue: str = Field(default="")


class ReferencesResult(BaseModel):
    references: list[ReferenceItem] = Field(default_factory=list, max_length=100)


DEFAULT_TEMPLATE = """\
Você é um analista de revisões sistemáticas. O texto abaixo é a extração
completa de um artigo científico (PDF). Localize a seção de REFERÊNCIAS
bibliográficas (pode aparecer como "References", "Bibliografia",
"Referências" etc., geralmente no final) e extraia cada entrada.

Regras:
- Retorne APENAS entradas que sejam referências bibliográficas de outras
  obras — nunca o próprio artigo, nunca notas de rodapé ou citações
  contextuais no corpo do texto.
- title é obrigatório e deve ser o título da obra citada.
- authors: lista de autores quando identificáveis.
- year: ano de publicação quando identificável.
- doi: DOI da referência, somente se estiver explicitamente escrito.
- venue: periódico/livro/evento quando identificável.
- Não invente dados: campos não identificáveis ficam vazios.
- Elimine duplicatas.
{% if max_refs -%}
- Retorne no máximo {{ max_refs }} referências — priorize as mais
  relevantes/citadas do artigo.
{% endif -%}

Artigo:
- Título: {{ paper.title }}
- Ano: {{ paper.year if paper.year is not none else 'desconhecido' }}
- Autores: {{ paper.authors | join(', ') if paper.authors else '—' }}

Texto completo do artigo:
{{ full_text }}
"""


def _paper_context(paper: Paper) -> dict:
    try:
        authors = json.loads(paper.authors or "[]")
    except json.JSONDecodeError:
        authors = []
    return {
        "title": paper.title,
        "year": paper.year,
        "authors": authors if isinstance(authors, list) else [],
    }


def build_prompt(slug: str, task, paper: Paper, full_text: str,
                 max_refs: int = 0) -> tuple[str, str, str]:
    """Renderiza o PromptTemplate configurado na tarefa (ou o padrão
    embutido). Retorna (prompt_renderizado, nome_template, versao)."""
    tpl = None
    raw_id = str(getattr(task, "prompt_template", "") or "").strip()
    if raw_id.isdigit():
        with get_project_session(slug) as s:
            tpl = s.get(PromptTemplate, int(raw_id))
    name = tpl.name if tpl else "references-default"
    version = tpl.version if tpl else "1"
    source = tpl.content if tpl and tpl.content else DEFAULT_TEMPLATE

    env = SandboxedEnvironment(autoescape=False, trim_blocks=True, lstrip_blocks=True)
    content = env.from_string(source).render(
        paper=_paper_context(paper),
        full_text=full_text,
        max_refs=max_refs or 0,
    )
    return content, name, version
