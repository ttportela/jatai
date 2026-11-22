"""Adaptador SciELO (Portal de Busca iAHx / Solr — search.scielo.org).

Sintaxe SciELO (`q` em https://search.scielo.org/):
- Operadores booleanos: AND, OR, AND NOT (sempre binário; evita NOT unário)
- Agrupamento: parênteses (...)
- Frase exata: "..."
- Truncamento / prefixo: * (truncamento à direita, ex.: tecnol*)
- Campos suportados: ti:(...), ab:(...), au:(...), kw:(...), ta:(...),
  doi:(...), pid:(...), year_cluster:(...), la:(...), in:(...)
"""

import re

from lelia.search.adapters.base import QueryAdapter
from lelia.search.domain import Group, QueryDefinition, Term
from lelia.search.parser import parse_expression

_FIELD_MAP = {
    "title": "ti",
    "ti": "ti",
    "abs": "ab",
    "abstract": "ab",
    "ab": "ab",
    "key": "kw",
    "keyword": "kw",
    "keywords": "kw",
    "subject": "kw",
    "kw": "kw",
    "mh": "mh",
    "author": "au",
    "auth": "au",
    "au": "au",
    "venue": "ta",
    "journal": "ta",
    "srctitle": "ta",
    "ta": "ta",
    "doi": "doi",
    "pid": "pid",
    "lang": "la",
    "language": "la",
    "la": "la",
    "collection": "in",
    "in": "in",
}


def _strip_outer_delimiters(value: str) -> str:
    if len(value) >= 2 and (
        (value[0] == '"' and value[-1] == '"')
        or (value[0] == "'" and value[-1] == "'")
        or (value[0] == "{" and value[-1] == "}")
    ):
        return value[1:-1].strip()
    return value


def _format_value(raw_value: str, match: str) -> str:
    v = _strip_outer_delimiters(raw_value.strip())
    if not v:
        return ""

    v_clean = v.replace('"', "").strip()
    if not v_clean:
        return ""

    if match == "prefix" and " " not in v_clean and not v_clean.endswith("*"):
        v_clean = f"{v_clean}*"

    if match in ("phrase", "exact") or " " in v_clean or "-" in v_clean:
        return f'"{v_clean}"'

    return v_clean


def _format_year_filter(spec: str) -> str:
    s = spec.strip()
    if re.fullmatch(r"\d{4}", s):
        return f"year_cluster:{s}"
    m_range = re.fullmatch(r"(\d{4})\s*-\s*(\d{4})", s)
    if m_range:
        return f"year_cluster:[{m_range.group(1)} TO {m_range.group(2)}]"
    m_from = re.fullmatch(r"(\d{4})\s*-", s)
    if m_from:
        return f"year_cluster:[{m_from.group(1)} TO *]"
    m_to = re.fullmatch(r"-\s*(\d{4})", s)
    if m_to:
        return f"year_cluster:[* TO {m_to.group(1)}]"
    return ""


def _format_term(term: Term, default_field: str = "") -> str:
    v = term.value.strip()
    if not v:
        return ""

    field = default_field
    if ":" in v:
        prefix, _, rest = v.partition(":")
        prefix_clean = prefix.strip().lower()
        rest_clean = rest.strip()

        if prefix_clean in ("year", "pubyear", "year_cluster"):
            year_expr = _format_year_filter(rest_clean)
            if year_expr:
                return year_expr

        if prefix_clean in _FIELD_MAP:
            field = _FIELD_MAP[prefix_clean]
            v = rest_clean
        elif prefix_clean == "all":
            field = ""
            v = rest_clean

    formatted = _format_value(v, term.match)
    if not formatted:
        return ""
    return f"{field}:({formatted})" if field else formatted


def _is_negated(node) -> bool:
    return bool(getattr(node, "negated", False))


def _compile_node(node, default_field: str = "") -> tuple[str, bool]:
    """Retorna `(expressao_compilada, esta_negada)`.

    No iAHx/SciELO, `AND NOT` é o operador binário seguro de exclusão.
    Propagamos `negated` até o grupo pai para gerar `A AND NOT B`.
    """
    if isinstance(node, Term):
        return _format_term(node, default_field=default_field), _is_negated(node)

    if isinstance(node, Group):
        op = (node.op or "AND").strip().upper()
        if op not in ("AND", "OR"):
            op = "AND"

        positives: list[str] = []
        negatives: list[str] = []

        for child in node.children:
            compiled, child_neg = _compile_node(child, default_field=default_field)
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

        # Grupo composto apenas por termos negados: NOT (A OR B)
        inner = (
            negatives[0]
            if len(negatives) == 1
            else "(" + f" {op} ".join(negatives) + ")"
        )
        return inner, not _is_negated(node)

    return "", False


def compile_expression(expression: str, default_field: str = "") -> str:
    """Expressão booleana Lelia -> sintaxe de consulta SciELO."""
    raw = (expression or "").strip()
    if not raw:
        return ""

    # Mantém intacta caso já contenha prefixos nativos de campo do SciELO
    if re.search(
        r"\b(?:ti|ab|au|kw|mh|ta|doi|pid|year_cluster|la|in)\s*:\s*\(",
        raw,
    ):
        return raw

    node = parse_expression(raw)
    if node is None:
        cleaned = _format_value(raw, "phrase" if " " in raw else "term")
        if not cleaned:
            return raw
        return f"{default_field}:({cleaned})" if default_field else cleaned

    try:
        expr, negated = _compile_node(node, default_field=default_field)
    except Exception:  # noqa: BLE001
        return f'"{raw}"'

    if not expr:
        return ""
    if negated:
        return f"* AND NOT {expr}"
    return expr


class SciELOQueryAdapter(QueryAdapter):
    source_type = "scielo"

    def compile(self, definition: QueryDefinition, limit: int = 25) -> list[str]:
        errs = definition.validate()
        if errs:
            raise ValueError(f"QueryDefinition inválida: {errs}")
        expr, negated = _compile_node(definition.root)
        if not expr:
            return []
        if negated:
            expr = f"* AND NOT {expr}"
        return [expr]