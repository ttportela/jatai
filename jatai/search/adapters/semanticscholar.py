"""Adaptador Semantic Scholar.

Duas traduções:
1. ``compile(definition)`` — QueryDefinition -> cláusulas DNF para /paper/search
   (termos negados são omitidos e filtrados localmente na ingestão).
2. ``compile_expression(expression)`` — expressão booleana -> sintaxe MATCH
   do endpoint /paper/search/bulk (+ = AND, | = OR, - = NOT, "..." = frase).
"""

import re

from jatai.search.adapters.base import QueryAdapter
from jatai.search.builder import dnf
from jatai.search.domain import Group, QueryDefinition, Term
from jatai.search.parser import parse_expression


def _strip_outer_quotes(value: str) -> str:
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1].strip()
    return value


def _format_term(term: Term) -> str:
    v = _strip_outer_quotes(term.value.strip())
    if not v:
        return ""
    if term.match in ("phrase", "exact") or " " in v:
        return f'"{v}"'
    return v


def _bulk_node(node) -> str:
    """AST -> sintaxe MATCH do bulk: OR = ' | ', AND = ' + ', NOT = '-'."""
    if isinstance(node, Term):
        s = _format_term(node)
        if not s:
            return ""
        return f"-{s}" if getattr(node, "negated", False) else s

    if isinstance(node, Group):
        parts = [compiled for c in node.children if (compiled := _bulk_node(c))]
        if not parts:
            return ""
        negated = getattr(node, "negated", False)
        if len(parts) == 1 and not negated:
            return parts[0]
        joiner = " | " if (node.op or "").upper() == "OR" else " + "
        body = "(" + joiner.join(parts) + ")"
        return f"-{body}" if negated else body

    return ""


def _normalize_bulk_plus(expression: str) -> str:
    """Converte '+' como operador AND apenas fora de trechos entre aspas."""
    segments = re.split(r'("[^"]*")', expression)
    for i in range(0, len(segments), 2):
        segments[i] = re.sub(
            r"(?:(?<=^)|(?<=[\s\)\"]))\+(?=(?:$|[\s\(\"-]))",
            " AND ",
            segments[i],
        )
    return "".join(segments)


def compile_expression(expression: str) -> str:
    """Expressão de busca -> uma consulta booleana na sintaxe bulk."""
    raw = (expression or "").strip()
    if not raw:
        return expression

    expr_in = _normalize_bulk_plus(raw)
    node = parse_expression(expr_in)
    if node is None:
        return expression

    try:
        return _bulk_node(node) or expression
    except Exception:  # noqa: BLE001
        return expression


class SemanticScholarQueryAdapter(QueryAdapter):
    source_type = "semantic_scholar"

    def compile(self, definition: QueryDefinition, limit: int = 25) -> list[str]:
        errs = definition.validate()
        if errs:
            raise ValueError(f"QueryDefinition inválida: {errs}")

        out: list[str] = []
        seen: set[str] = set()
        for clause in dnf(definition.root, limit=limit * 4):
            terms = [
                _format_term(t)
                for t in clause
                if not getattr(t, "negated", False)
            ]
            expr = " ".join(t for t in terms if t).strip()
            if expr and expr not in seen:
                seen.add(expr)
                out.append(expr)
                if len(out) >= limit:
                    break
        return out