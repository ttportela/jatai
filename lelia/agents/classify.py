"""Classificação/catalogação (classification): preenche os campos de
caracterização científica (CatalogField) a partir do texto completo do PDF.

Mesma arquitetura da triagem/análise profunda: saída estruturada Pydantic
validada pelo Ollama, prompt Jinja2 configurável via PromptTemplate,
regras determinísticas de pós-processamento e persistência auditável.
O modelo de resultado é gerado dinamicamente a partir dos campos ativos,
então os identificadores são impostos pelo JSON Schema — o LLM não pode
inventar nomes de campo.

O JSON gravado mantém o contrato {"values": {identifier: valor}} usado
pela matriz de classificação, acrescido do detalhamento por campo.
"""

import json
from datetime import datetime, timezone

from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel, Field, create_model

from lelia.storage.db import get_project_session
from lelia.storage.models_project import Evidence, Paper, PaperAnalysis, PromptTemplate


class FieldEntry(BaseModel):
    value: str | None = Field(
        default=None,
        description="Valor do campo serializado conforme o tipo de dado "
                    "(texto livre; número como dígito; booleano como "
                    "verdadeiro/falso; lista separada por vírgulas); "
                    "null quando a informação não estiver no texto")
    justification: str = ""
    evidence_quote: str | None = Field(
        default=None,
        description="Trecho literal do texto que fundamenta o valor; "
                    "null quando não houver trecho correspondente")


def result_model(fields: list):
    """Gera o modelo Pydantic da resposta com uma propriedade por campo
    de caracterização — o JSON Schema impõe os identifiers corretos."""
    props = {
        f.identifier: (FieldEntry | None, Field(
            default=None,
            description=f"{f.label} ({f.data_type})"
                        + (", obrigatório" if f.required else "")))
        for f in fields
    }
    values_model = create_model("FieldValues", **props)
    return create_model(
        "ClassificationResult",
        values=(values_model, Field(default_factory=values_model)),
        confidence=(float, Field(ge=0.0, le=1.0)),
    )


DEFAULT_TEMPLATE = """\
Você é um catalogador de uma revisão sistemáticas. Analise o
TEXTO COMPLETO do artigo e preencha CADA campo de caracterização
científica listado abaixo.

Regras:
- Preencha cada campo individualmente, usando exatamente a chave
  (identifier) indicada — nunca invente outras chaves.
- value=null quando a informação não estiver no texto — nunca invente.
- Serialize value conforme o tipo de dado do campo:
  texto = texto curto; numero = apenas o número; booleano = verdadeiro/falso;
  lista = itens separados por vírgula; estruturado = JSON compacto.
- Prefira o valor mais específico e fiel ao texto; use a instrução do campo
  como guia semântico.
- evidence_quote deve ser um trecho literal do texto do artigo; use null
  quando não houver trecho que fundamente o valor.
- Em caso de dúvida, prefira value=null e confidence baixa.

Artigo:
- Título: {{ paper.title }}
- Ano: {{ paper.year if paper.year is not none else 'desconhecido' }}
- Periódico/evento: {{ paper.venue or 'desconhecido' }}
- Fonte: {{ paper.source or 'desconhecida' }}
- Palavras-chave: {{ paper.keywords or '—' }}
- Autores: {{ paper.authors | join(', ') if paper.authors else '—' }}
- Resumo (metadados): {{ paper.abstract if paper.abstract else '(indisponível)' }}

Campos de caracterização:
{% for f in fields -%}
- chave "{{ f.identifier }}" — {{ f.label }} (tipo {{ f.data_type }}{{ ', obrigatório' if f.required else '' }}{{ ', RQ ' + f.rq_code if f.rq_code else '' }})
  descrição: {{ f.description }}
{% if f.llm_instruction %}  INSTRUÇÃO OBRIGATÓRIA para este campo (siga exatamente, inclusive formato e exemplos): {{ f.llm_instruction }}
{% endif %}{% endfor %}

Texto completo do artigo (extraído do PDF):
{{ full_text }}

Responda preenchendo "values" com uma entrada para cada chave listada acima.
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


def build_prompt(slug: str, task, paper: Paper, fields: list,
                 full_text: str) -> tuple[str, str, str]:
    """Renderiza o PromptTemplate configurado na tarefa (ou o padrão embutido).

    Retorna (prompt_renderizado, nome_template, versao_template).
    """
    tpl = None
    raw_id = str(getattr(task, "prompt_template", "") or "").strip()
    if raw_id.isdigit():
        with get_project_session(slug) as s:
            tpl = s.get(PromptTemplate, int(raw_id))
    name = tpl.name if tpl else "classification-default"
    version = tpl.version if tpl else "1"
    source = tpl.content if tpl and tpl.content else DEFAULT_TEMPLATE

    env = SandboxedEnvironment(autoescape=False, trim_blocks=True, lstrip_blocks=True)
    content = env.from_string(source).render(
        paper=_paper_context(paper),
        fields=[{"identifier": f.identifier, "label": f.label,
                 "data_type": f.data_type, "required": f.required,
                 "description": f.description,
                 "llm_instruction": f.llm_instruction,
                 "rq_code": f.rq_code} for f in fields],
        full_text=full_text,
    )
    return content, name, version


def _entries(result, fields: list) -> dict[str, FieldEntry | None]:
    """Mapeia identifier -> FieldEntry da resposta estruturada."""
    out = {}
    values = getattr(result, "values", None)
    for f in fields:
        entry = getattr(values, f.identifier, None) if values is not None else None
        out[f.identifier] = entry if isinstance(entry, FieldEntry) else None
    return out


def _coerce_value(raw: str | None, data_type: str):
    """Converte o valor serializado para o tipo do campo quando possível."""
    if raw is None:
        return None
    v = str(raw).strip()
    if not v:
        return None
    if data_type == "numero":
        try:
            return float(v) if "." in v else int(v)
        except ValueError:
            return v
    if data_type == "booleano":
        low = v.lower()
        if low in ("verdadeiro", "true", "sim", "yes", "1"):
            return True
        if low in ("falso", "false", "não", "nao", "no", "0"):
            return False
        return v
    if data_type == "lista":
        return [p.strip() for p in v.split(",") if p.strip()]
    return v


def apply_rules(result, fields: list, min_confidence: float) -> list[str]:
    """Regras determinísticas — geram notas de qualidade.

    - campo obrigatório sem valor ou sem entrada
    - valor sem evidence_quote literal
    - confidence abaixo do limiar configurado
    """
    entries = _entries(result, fields)
    notes: list[str] = []
    for f in fields:
        ev = entries.get(f.identifier)
        if ev is None:
            if f.required:
                notes.append(f"{f.identifier}: campo obrigatório não avaliado")
            continue
        if ev.value is None and f.required:
            notes.append(f"{f.identifier}: campo obrigatório sem valor")
        if not (ev.evidence_quote or "").strip():
            notes.append(f"{f.identifier}: valor sem evidência literal")
    if result.confidence < min_confidence:
        notes.append(
            f"confidence {result.confidence:.2f} abaixo do limiar {min_confidence:.2f}")
    return notes


def persist(slug: str, paper_id: int, fields: list, result,
            notes: list[str], iteration: int, model_ref: str = "") -> None:
    """Grava a análise 'classificacao' (contrato {"values": {...}} +
    detalhamento por campo) e as evidências literais."""
    entries = _entries(result, fields)
    type_by_id = {f.identifier: f.data_type for f in fields}
    values: dict[str, object] = {}
    evaluations = []
    for f in fields:
        ev = entries.get(f.identifier)
        raw = ev.value if ev else None
        values[f.identifier] = _coerce_value(raw, type_by_id.get(f.identifier, "texto"))
        evaluations.append({
            "identifier": f.identifier,
            "value": raw,
            "justification": (ev.justification if ev else "")[:2000],
            "evidence_quote": (ev.evidence_quote if ev else None),
        })
    payload = {
        "values": values,
        "evaluations": evaluations,
        "confidence": result.confidence,
        "rule_notes": notes,
        "origin": "agente",
        "model": model_ref,
        "classified_at": datetime.now(timezone.utc).isoformat(),
    }
    with get_project_session(slug) as s:
        s.add(PaperAnalysis(paper_id=paper_id, kind="classificacao",
                            result=json.dumps(payload, ensure_ascii=False)[:16000]))
        for f in fields:
            ev = entries.get(f.identifier)
            quote = ((ev.evidence_quote if ev else "") or "").strip()
            if quote:
                s.add(Evidence(paper_id=paper_id, excerpt=quote[:2000],
                               origin="agente"))
        s.commit()
