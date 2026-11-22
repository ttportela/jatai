"""Adaptador Scopus (Elsevier Search API).

Sintaxe Scopus:
- Operadores booleanos: AND, OR, AND NOT (sempre binário; não existe NOT unário isolado)
- Agrupamento: parênteses (...)
- Frase com stemming/lematização: "..."
- Frase/termo exato (sem stemming): {...}
- Wildcards: * (múltiplos caracteres), ? (1 caractere)
- Campos suportados: TITLE-ABS-KEY(...), TITLE(...), ABS(...), KEY(...),
  AUTH(...), SRCTITLE(...), DOI(...), ISSN(...), AFFIL(...), SUBJAREA(...),
  DOCTYPE(...), LANGUAGE(...), PUBYEAR
"""

import re

from lelia.search.adapters.base import QueryAdapter
from lelia.search.domain import Group, QueryDefinition, Term
from lelia.search.parser import parse_expression

_FIELD_MAP = {
    "title": "TITLE",
    "abs": "ABS",
    "abstract": "ABS",
    "key": "KEY",
    "keyword": "KEY",
    "keywords": "KEY",
    "authkey": "AUTHKEY",
    "author": "AUTH",
    "auth": "AUTH",
    "venue": "SRCTITLE",
    "journal": "SRCTITLE",
    "srctitle": "SRCTITLE",
    "doi": "DOI",
    "issn": "ISSN",
    "affil": "AFFIL",
    "affiliation": "AFFIL",
    "subjarea": "SUBJAREA",
    "doctype": "DOCTYPE",
    "lang": "LANGUAGE",
    "language": "LANGUAGE",
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

    if match == "exact":
        v_clean = v.replace("{", "").replace("}", "")
        return f"{{{v_clean}}}"

    v_clean = v.replace('"', "")
    if match == "prefix" and " " not in v_clean and not v_clean.endswith("*"):
        v_clean = f"{v_clean}*"

    # Aspas evitam que hífens internos sejam interpretados como AND NOT pelo Scopus
    if match == "phrase" or " " in v_clean or "-" in v_clean:
        return f'"{v_clean}"'

    return v_clean


def _format_year_filter(spec: str) -> str:
    s = spec.strip()
    if re.fullmatch(r"\d{4}", s):
        return f"PUBYEAR IS {s}"
    m_range = re.fullmatch(r"(\d{4})\s*-\s*(\d{4})", s)
    if m_range:
        return f"(PUBYEAR > {int(m_range.group(1)) - 1} AND PUBYEAR < {int(m_range.group(2)) + 1})"
    m_from = re.fullmatch(r"(\d{4})\s*-", s)
    if m_from:
        return f"PUBYEAR > {int(m_from.group(1)) - 1}"
    m_to = re.fullmatch(r"-\s*(\d{4})", s)
    if m_to:
        return f"PUBYEAR < {int(m_to.group(1)) + 1}"
    return ""


def _format_term(term: Term, default_field: str = "TITLE-ABS-KEY") -> str:
    v = term.value.strip()
    if not v:
        return ""

    field = default_field
    if ":" in v:
        prefix, _, rest = v.partition(":")
        prefix_clean = prefix.strip().lower()
        rest_clean = rest.strip()

        if prefix_clean in ("year", "pubyear"):
            year_expr = _format_year_filter(rest_clean)
            if year_expr:
                return year_expr

        if prefix_clean in _FIELD_MAP:
            field = _FIELD_MAP[prefix_clean]
            v = rest_clean
        elif prefix_clean == "all":
            field = "ALL"
            v = rest_clean

    formatted = _format_value(v, term.match)
    if not formatted:
        return ""
    return f"{field}({formatted})"


def _is_negated(node) -> bool:
    return bool(getattr(node, "negated", False))


def _compile_node(node, default_field: str = "TITLE-ABS-KEY") -> tuple[str, bool]:
    """Retorna `(expressao_compilada, esta_negada)`.

    O Scopus exige `AND NOT` como operador binário (não aceita `NOT X` unário).
    Propagamos a flag `negated` até o grupo pai para emitir `A AND NOT B`.
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


def compile_expression(expression: str, default_field: str = "TITLE-ABS-KEY") -> str:
    """Expressão booleana Lelia -> sintaxe de consulta Scopus."""
    raw = (expression or "").strip()
    if not raw:
        return ""

    # Mantém intacta caso já seja uma consulta nativa com códigos de campo Scopus
    if re.search(
        r"\b(?:TITLE-ABS-KEY|TITLE|ABS|KEY|AUTHKEY|AUTH|SRCTITLE|DOI|ISSN|AFFIL|SUBJAREA|DOCTYPE|LANGUAGE|PUBYEAR|ALL)\s*\(",
        raw,
    ):
        return raw

    node = parse_expression(raw)
    if node is None:
        cleaned = _format_value(raw, "phrase" if " " in raw else "term")
        return f"{default_field}({cleaned})" if cleaned else raw

    try:
        expr, negated = _compile_node(node, default_field=default_field)
    except Exception:  # noqa: BLE001
        return f'{default_field}("{raw}")'

    if not expr:
        return ""
    if negated:
        return f"ALL(*) AND NOT {expr}"
    return expr


class ScopusQueryAdapter(QueryAdapter):
    source_type = "scopus"

    def compile(self, definition: QueryDefinition, limit: int = 25) -> list[str]:
        errs = definition.validate()
        if errs:
            raise ValueError(f"QueryDefinition inválida: {errs}")
        expr, negated = _compile_node(definition.root)
        if not expr:
            return []
        if negated:
            expr = f"ALL(*) AND NOT {expr}"
        return [expr]