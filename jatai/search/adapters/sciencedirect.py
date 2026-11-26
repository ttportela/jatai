"""Adaptador ScienceDirect (Elsevier Search API).

Sintaxe ScienceDirect (`query` no endpoint /content/search/sciencedirect):
- Operadores booleanos: AND, OR, AND NOT (sempre binário; não aceita NOT unário)
- Agrupamento: parênteses (...)
- Frase exata (sem stemming): "..."
- Wildcards (* e ?): não são suportados na Search API V2 e são removidos
  na compilação para evitar rejeição pela API.
- Campos suportados: TITLE(...), TAK(...) [Title, Abstract, Keywords],
  AUTHORS(...), SRCTITLE(...), AFFIL(...), DOI(...), ISSN(...), ISBN(...),
  PUB-DATE
"""

import re

from jatai.search.adapters.base import QueryAdapter
from jatai.search.domain import Group, QueryDefinition, Term
from jatai.search.parser import parse_expression

_FIELD_MAP = {
    "title": "TITLE",
    "tak": "TAK",
    "abs": "TAK",
    "abstract": "TAK",
    "key": "TAK",
    "keyword": "TAK",
    "keywords": "TAK",
    "authkey": "TAK",
    "author": "AUTHORS",
    "auth": "AUTHORS",
    "venue": "SRCTITLE",
    "journal": "SRCTITLE",
    "srctitle": "SRCTITLE",
    "doi": "DOI",
    "affil": "AFFIL",
    "affiliation": "AFFIL",
    "issn": "ISSN",
    "isbn": "ISBN",
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

    # A Search API V2 do ScienceDirect não suporta wildcards (* e ?)
    v_clean = v.replace('"', "").replace("*", "").replace("?", "").strip()
    if not v_clean:
        return ""

    # Aspas garantem correspondência exata e evitam que hífens internos
    # sejam interpretados como negação pelo parser booleano da Elsevier
    if match in ("phrase", "exact") or " " in v_clean or "-" in v_clean:
        return f'"{v_clean}"'

    return v_clean


def _format_pub_date(spec: str) -> str:
    s = spec.strip()
    if re.fullmatch(r"\d{4}", s):
        return f"PUB-DATE IS {s}"
    m_range = re.fullmatch(r"(\d{4})\s*-\s*(\d{4})", s)
    if m_range:
        return f"(PUB-DATE > {int(m_range.group(1)) - 1} AND PUB-DATE < {int(m_range.group(2)) + 1})"
    m_from = re.fullmatch(r"(\d{4})\s*-", s)
    if m_from:
        return f"PUB-DATE > {int(m_from.group(1)) - 1}"
    m_to = re.fullmatch(r"-\s*(\d{4})", s)
    if m_to:
        return f"PUB-DATE < {int(m_to.group(1)) + 1}"
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

        if prefix_clean in ("year", "pubyear", "date"):
            date_expr = _format_pub_date(rest_clean)
            if date_expr:
                return date_expr

        if prefix_clean in _FIELD_MAP:
            field = _FIELD_MAP[prefix_clean]
            v = rest_clean
        elif prefix_clean == "all":
            field = ""
            v = rest_clean

    formatted = _format_value(v, term.match)
    if not formatted:
        return ""
    return f"{field}({formatted})" if field else formatted


def _is_negated(node) -> bool:
    return bool(getattr(node, "negated", False))


def _compile_node(node, default_field: str = "") -> tuple[str, bool]:
    """Retorna `(expressao_compilada, esta_negada)`.

    O ScienceDirect exige `AND NOT` como operador binário (não aceita `NOT X`
    unário). Propagamos a flag `negated` até o grupo pai para montar `A AND NOT B`.
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
    """Expressão booleana Jataí -> sintaxe de consulta ScienceDirect."""
    raw = (expression or "").strip()
    if not raw:
        return ""

    # Mantém intacta caso já seja uma consulta nativa com campos ScienceDirect
    if re.search(
        r"\b(?:TITLE|TAK|AUTHORS|SRCTITLE|AFFIL|DOI|ISSN|ISBN|PUB-DATE)\s*\(",
        raw,
    ):
        return raw

    node = parse_expression(raw)
    if node is None:
        cleaned = _format_value(raw, "phrase" if " " in raw else "term")
        if not cleaned:
            return raw
        return f"{default_field}({cleaned})" if default_field else cleaned

    try:
        expr, negated = _compile_node(node, default_field=default_field)
    except Exception:  # noqa: BLE001
        return f'"{raw}"'

    if not expr:
        return ""
    if negated:
        # Expressão raiz puramente negada: ancore com filtro amplo de ano
        return f"(PUB-DATE > 1900) AND NOT {expr}"
    return expr


class ScienceDirectQueryAdapter(QueryAdapter):
    source_type = "sciencedirect"

    def compile(self, definition: QueryDefinition, limit: int = 25) -> list[str]:
        errs = definition.validate()
        if errs:
            raise ValueError(f"QueryDefinition inválida: {errs}")
        expr, negated = _compile_node(definition.root)
        if not expr:
            return []
        if negated:
            expr = f"(PUB-DATE > 1900) AND NOT {expr}"
        return [expr]