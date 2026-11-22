"""Adaptador OpenAlex.

O parâmetro global `search` do OpenAlex aceita expressões booleanas
com AND/OR/NOT (em maiúsculas), parênteses e frases entre aspas.
Compilamos a árvore preservando a estrutura lógica.
"""

import re

from lelia.search.adapters.base import QueryAdapter
from lelia.search.domain import Group, QueryDefinition, Term
from lelia.search.parser import parse_expression

_RESERVED_WORDS = {"AND", "OR", "NOT"}


def _format_term(term: Term) -> str:
    prefix = "NOT " if getattr(term, "negated", False) else ""
    v = term.value.strip()
    if not v:
        return ""

    # Evita aspas duplas duplicadas caso o parser tenha preservado aspas externas
    if len(v) >= 2 and v.startswith('"') and v.endswith('"'):
        v = v[1:-1].strip()
    if not v:
        return ""

    # Escapa aspas internas residuais
    v_escaped = v.replace('"', '\\"')

    needs_quotes = (
        term.match == "phrase"
        or " " in v_escaped
        or v_escaped.upper() in _RESERVED_WORDS
        or any(ch in v_escaped for ch in ("(", ")", ":"))
    )
    if needs_quotes:
        return f'{prefix}"{v_escaped}"'
    return f"{prefix}{v_escaped}"


def _node(node) -> str:
    if isinstance(node, Term):
        return _format_term(node)

    if isinstance(node, Group):
        prefix = "NOT " if getattr(node, "negated", False) else ""
        parts = [compiled for c in node.children if (compiled := _node(c))]
        if not parts:
            return ""
        if len(parts) == 1:
            return f"{prefix}{parts[0]}"
        op = (node.op or "AND").strip().upper()
        return f"{prefix}({f' {op} '.join(parts)})"

    return ""


def compile_expression(expression: str) -> str:
    """Expressão de busca -> string booleana canônica para o parâmetro
    `search` do OpenAlex (AND/OR/NOT, frases entre aspas). Inválida ou
    texto simples: retorna como veio."""
    raw = (expression or "").strip()
    if not raw:
        return expression

    node = parse_expression(raw)
    if node is None:
        return expression

    try:
        out = _node(node).strip()
    except Exception:  # noqa: BLE001
        return expression

    return out or expression


class OpenAlexQueryAdapter(QueryAdapter):
    source_type = "openalex"

    def compile(self, definition: QueryDefinition, limit: int = 25) -> list[str]:
        errs = definition.validate()
        if errs:
            raise ValueError(f"QueryDefinition inválida: {errs}")
        expr = _node(definition.root).strip()
        return [expr] if expr else []