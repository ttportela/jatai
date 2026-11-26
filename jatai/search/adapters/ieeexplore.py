"""Adaptador IEEE Xplore (Metadata Search API).

Sintaxe IEEE Xplore (`querytext` / Command Search):
- Operadores booleanos: AND, OR, NOT (em maiúsculas)
- Agrupamento: parênteses (...)
- Frase exata: "..."
- Wildcards: * (0+ caracteres; exige no mínimo 3 caracteres antes do * e
  a API impõe limite máximo de 2 wildcards por consulta) e ? (1 caractere)
- Campos suportados (Command Search): "Document Title":, "Abstract":,
  "Author Keywords":, "IEEE Terms":, "Index Terms":, "Authors":,
  "Publication Title":, "DOI":, "Affiliation":, "ISSN":, "ISBN":,
  "Publication Year":
"""

import re

from jatai.search.adapters.base import QueryAdapter
from jatai.search.domain import Group, QueryDefinition, Term
from jatai.search.parser import parse_expression

MAX_WILDCARDS_PER_QUERY = 2

_RESERVED_WORDS = {"AND", "OR", "NOT", "NEAR", "ONEAR"}

_FIELD_MAP = {
    "title": '"Document Title"',
    "abs": '"Abstract"',
    "abstract": '"Abstract"',
    "key": '"Author Keywords"',
    "keyword": '"Author Keywords"',
    "keywords": '"Author Keywords"',
    "authkey": '"Author Keywords"',
    "ieeeterms": '"IEEE Terms"',
    "index_terms": '"Index Terms"',
    "author": '"Authors"',
    "auth": '"Authors"',
    "venue": '"Publication Title"',
    "journal": '"Publication Title"',
    "srctitle": '"Publication Title"',
    "doi": '"DOI"',
    "affil": '"Affiliation"',
    "affiliation": '"Affiliation"',
    "issn": '"ISSN"',
    "isbn": '"ISBN"',
    "year": '"Publication Year"',
    "pubyear": '"Publication Year"',
}


def _strip_outer_quotes(value: str) -> str:
    if len(value) >= 2 and (
        (value[0] == '"' and value[-1] == '"')
        or (value[0] == "'" and value[-1] == "'")
    ):
        return value[1:-1].strip()
    return value


def _format_value(raw_value: str, match: str, wildcard_budget: list[int]) -> str:
    v = _strip_outer_quotes(raw_value.strip())
    if not v:
        return ""

    v_clean = v.replace('"', "")

    # IEEE Xplore permite no máximo 2 termos com wildcard (*) por querytext
    # e exige pelo menos 3 caracteres antes do '*'
    if match == "prefix" and " " not in v_clean and not v_clean.endswith("*"):
        if len(v_clean) >= 3 and wildcard_budget[0] > 0:
            v_clean = f"{v_clean}*"
            wildcard_budget[0] -= 1
    elif "*" in v_clean:
        if wildcard_budget[0] > 0:
            wildcard_budget[0] -= 1
        else:
            v_clean = v_clean.replace("*", "")

    needs_quotes = (
        match in ("phrase", "exact")
        or " " in v_clean
        or v_clean.upper() in _RESERVED_WORDS
        or any(ch in v_clean for ch in ("(", ")", ":"))
    )
    if needs_quotes:
        return f'"{v_clean}"'
    return v_clean


def _format_term(term: Term, wildcard_budget: list[int]) -> str:
    v = term.value.strip()
    if not v:
        return ""

    field_prefix = ""
    if ":" in v:
        prefix, _, rest = v.partition(":")
        prefix_clean = prefix.strip().lower()
        rest_clean = rest.strip()

        if prefix_clean in _FIELD_MAP:
            field_prefix = f"{_FIELD_MAP[prefix_clean]}:"
            v = rest_clean
        elif prefix_clean == "all":
            v = rest_clean

    formatted = _format_value(v, term.match, wildcard_budget)
    if not formatted:
        return ""
    return f"{field_prefix}{formatted}"


def _is_negated(node) -> bool:
    return bool(getattr(node, "negated", False))


def _compile_node(node, wildcard_budget: list[int]) -> tuple[str, bool]:
    """Retorna `(expressao_compilada, esta_negada)`.

    Na sintaxe booleana do IEEE Xplore, o operador preferencial de exclusão
    entre cláusulas é `AND NOT` (ou `NOT` binário: `x NOT y`).
    """
    if isinstance(node, Term):
        return _format_term(node, wildcard_budget), _is_negated(node)

    if isinstance(node, Group):
        op = (node.op or "AND").strip().upper()
        if op not in ("AND", "OR"):
            op = "AND"

        positives: list[str] = []
        negatives: list[str] = []

        for child in node.children:
            compiled, child_neg = _compile_node(child, wildcard_budget)
            if not compiled:
                continue
            if child_neg:
                negatives.append(compiled)
            else:
                positives.append(compiled)

        if not positives and not negatives:
            return "", False

        if len(positives) == 1 and not negatives:
            return positives[0], _is_negated(node)

        if positives:
            base = (
                positives[0]
                if len(positives) == 1
                else "(" + f" {op} ".join(positives) + ")"
            )
            if negatives:
                neg_joined = " AND NOT ".join(negatives)
                expr = f"({base} AND NOT {neg_joined})"
            else:
                expr = base
            return expr, _is_negated(node)

        inner = (
            negatives[0]
            if len(negatives) == 1
            else "(" + f" {op} ".join(negatives) + ")"
        )
        return inner, not _is_negated(node)

    return "", False


def compile_expression(expression: str) -> str:
    """Expressão booleana Jataí -> sintaxe `querytext` do IEEE Xplore."""
    raw = (expression or "").strip()
    if not raw:
        return ""

    # Preserva caso já seja uma consulta nativa com campos Command Search do IEEE
    if re.search(
        r'"(?:Document Title|Abstract|Author Keywords|IEEE Terms|Index Terms|Authors|Publication Title|DOI|Affiliation|ISSN|ISBN|Publication Year)"\s*:',
        raw,
    ):
        return raw

    node = parse_expression(raw)
    if node is None:
        cleaned = _format_value(
            raw,
            "phrase" if " " in raw else "term",
            [MAX_WILDCARDS_PER_QUERY],
        )
        return cleaned or raw

    try:
        expr, negated = _compile_node(node, [MAX_WILDCARDS_PER_QUERY])
    except Exception:  # noqa: BLE001
        return f'"{raw}"'

    if not expr:
        return ""
    if negated:
        return f"NOT {expr}"
    return expr


class IEEEXploreQueryAdapter(QueryAdapter):
    source_type = "ieeexplore"

    def compile(self, definition: QueryDefinition, limit: int = 25) -> list[str]:
        errs = definition.validate()
        if errs:
            raise ValueError(f"QueryDefinition inválida: {errs}")
        expr, negated = _compile_node(definition.root, [MAX_WILDCARDS_PER_QUERY])
        if not expr:
            return []
        if negated:
            expr = f"NOT {expr}"
        return [expr]