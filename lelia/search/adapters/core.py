"""Adaptador CORE (REST API v3 — /v3/search/works).

Sintaxe CORE (`q` — CORE Query Language / Lucene):
- Operadores booleanos: AND, OR, NOT (em maiúsculas)
- Agrupamento: parênteses (...)
- Frase exata: "..."
- Wildcards: * (0+ caracteres) e ? (1 caractere)
- Regra crítica de campos no CORE v3: termos escopados a um campo
  **devem** estar entre aspas ou parênteses (ex.: ``title:"cancer"`` ou
  ``title:(cancer)``); a forma sem delimitador (``title:cancer``) não
  retorna correspondências na API v3.
- Campos suportados: ``title``, ``abstract``, ``authors``, ``fullText``,
  ``doi``, ``issn``, ``publisher``, ``journals.title``, ``yearPublished``,
  ``documentType``, ``language.code``, ``fieldOfStudy``, ``subjects``.
"""

import re

from lelia.search.adapters.base import QueryAdapter
from lelia.search.domain import Group, QueryDefinition, Term
from lelia.search.parser import parse_expression

_RESERVED_WORDS = {"AND", "OR", "NOT", "TO"}

_FIELD_MAP = {
    "title": "title",
    "ti": "title",
    "abs": "abstract",
    "abstract": "abstract",
    "ab": "abstract",
    "fulltext": "fullText",
    "ft": "fullText",
    "author": "authors",
    "auth": "authors",
    "au": "authors",
    "venue": "journals.title",
    "journal": "journals.title",
    "srctitle": "journals.title",
    "publisher": "publisher",
    "doi": "doi",
    "issn": "issn",
    "key": "subjects",
    "keyword": "subjects",
    "keywords": "subjects",
    "subject": "subjects",
    "subjects": "subjects",
    "field": "fieldOfStudy",
    "fieldofstudy": "fieldOfStudy",
    "doctype": "documentType",
    "type": "documentType",
    "lang": "language.code",
    "language": "language.code",
}


def _strip_outer_delimiters(value: str) -> str:
    if len(value) >= 2 and (
        (value[0] == '"' and value[-1] == '"')
        or (value[0] == "'" and value[-1] == "'")
        or (value[0] == "{" and value[-1] == "}")
    ):
        return value[1:-1].strip()
    return value


def _format_year_filter(spec: str) -> str:
    """Converte '2024', '2020-2026', '2022-', '-2021' em filtro `yearPublished`."""
    s = spec.strip()
    if re.fullmatch(r"\d{4}", s):
        return f"yearPublished:{s}"
    m_range = re.fullmatch(r"(\d{4})\s*-\s*(\d{4})", s)
    if m_range:
        return f"(yearPublished>={m_range.group(1)} AND yearPublished<={m_range.group(2)})"
    m_from = re.fullmatch(r"(\d{4})\s*-", s)
    if m_from:
        return f"yearPublished>={m_from.group(1)}"
    m_to = re.fullmatch(r"-\s*(\d{4})", s)
    if m_to:
        return f"yearPublished<={m_to.group(1)}"
    return ""


def _format_value(raw_value: str, match: str, scoped: bool) -> str:
    v = _strip_outer_delimiters(raw_value.strip())
    if not v:
        return ""

    v_clean = v.replace('"', "").lstrip("*?").strip()
    if not v_clean:
        return ""

    if match == "prefix" and " " not in v_clean and "-" not in v_clean:
        if not v_clean.endswith("*"):
            v_clean = f"{v_clean}*"
        return f"({v_clean})" if scoped else v_clean

    needs_quotes = (
        match in ("phrase", "exact")
        or " " in v_clean
        or "-" in v_clean
        or v_clean.upper() in _RESERVED_WORDS
        or any(ch in v_clean for ch in ("(", ")", ":", "/", "[", "]"))
    )
    if needs_quotes:
        v_quoted = v_clean.replace("*", "").replace("?", "").strip()
        return f'"{v_quoted}"' if v_quoted else ""

    # No CORE v3, termos ligados a um campo exigem parênteses ou aspas
    return f"({v_clean})" if scoped else v_clean


def _format_term(term: Term, default_field: str = "") -> str:
    v = term.value.strip()
    if not v:
        return ""

    field = default_field
    if ":" in v:
        prefix, _, rest = v.partition(":")
        prefix_clean = prefix.strip().lower()
        rest_clean = rest.strip()

        if prefix_clean in ("year", "pubyear", "yearpublished"):
            year_expr = _format_year_filter(rest_clean)
            if year_expr:
                return year_expr

        if prefix_clean in _FIELD_MAP:
            field = _FIELD_MAP[prefix_clean]
            v = rest_clean
        elif prefix_clean == "all":
            field = ""
            v = rest_clean

    formatted = _format_value(v, term.match, scoped=bool(field))
    if not formatted:
        return ""
    return f"{field}:{formatted}" if field else formatted


def _is_negated(node) -> bool:
    return bool(getattr(node, "negated", False))


def _compile_node(node, default_field: str = "") -> tuple[str, bool]:
    """Retorna `(expressao_compilada, esta_negada)`."""
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

        inner = (
            negatives[0]
            if len(negatives) == 1
            else "(" + f" {op} ".join(negatives) + ")"
        )
        return inner, not _is_negated(node)

    return "", False


def compile_expression(expression: str, default_field: str = "") -> str:
    """Expressão booleana Lelia -> sintaxe de consulta CORE v3."""
    raw = (expression or "").strip()
    if not raw:
        return ""

    # Mantém intacta caso já contenha campos nativos da API CORE v3
    if re.search(
        r"\b(?:title|abstract|authors|fullText|doi|issn|publisher|journals\.title|yearPublished|documentType|language\.code|fieldOfStudy|subjects)\s*(?::|>=|<=|>|<)",
        raw,
    ):
        return raw

    node = parse_expression(raw)
    if node is None:
        cleaned = _format_value(
            raw,
            "phrase" if (" " in raw or "-" in raw) else "term",
            scoped=bool(default_field),
        )
        if not cleaned:
            return raw
        return f"{default_field}:{cleaned}" if default_field else cleaned

    try:
        expr, negated = _compile_node(node, default_field=default_field)
    except Exception:  # noqa: BLE001
        return f'{default_field}:"{raw}"' if default_field else f'"{raw}"'

    if not expr:
        return ""
    if negated:
        return f"NOT {expr}"
    return expr


class COREQueryAdapter(QueryAdapter):
    source_type = "core"

    def compile(self, definition: QueryDefinition, limit: int = 25) -> list[str]:
        errs = definition.validate()
        if errs:
            raise ValueError(f"QueryDefinition inválida: {errs}")
        expr, negated = _compile_node(definition.root)
        if not expr:
            return []
        if negated:
            expr = f"NOT {expr}"
        return [expr]