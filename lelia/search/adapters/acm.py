"""Adaptador ACM Digital Library (dl.acm.org/action/doSearch).

Sintaxe ACM DL (plataforma Atypon Literatum):
- Operadores booleanos: AND, OR, NOT (em maiúsculas; AND NOT para exclusão)
- Agrupamento: parênteses (...)
- Frase exata: "..." (wildcards não funcionam dentro de aspas)
- Wildcards: * (0+ caracteres, mínimo 3 caracteres antes do *) e ? (1 caractere)
- Atenção a hífens: termos hifenizados sem aspas (ex.: `web-based`) são
  interpretados como `web NOT based` pelo motor da ACM e devem ir entre aspas.
- Campos suportados: AllField:(...), Title:(...), Abstract:(...),
  Keyword:(...), Author:(...), Affiliation:(...), PublicationTitle:(...),
  DOI:(...), ISSN:(...), ISBN:(...)
"""

import re

from lelia.search.adapters.base import QueryAdapter
from lelia.search.domain import Group, QueryDefinition, Term
from lelia.search.parser import parse_expression

_RESERVED_WORDS = {"AND", "OR", "NOT"}

_FIELD_MAP = {
    "all": "AllField",
    "allfield": "AllField",
    "title": "Title",
    "ti": "Title",
    "abs": "Abstract",
    "abstract": "Abstract",
    "ab": "Abstract",
    "key": "Keyword",
    "keyword": "Keyword",
    "keywords": "Keyword",
    "kw": "Keyword",
    "author": "Author",
    "auth": "Author",
    "au": "Author",
    "affil": "Affiliation",
    "affiliation": "Affiliation",
    "venue": "PublicationTitle",
    "journal": "PublicationTitle",
    "srctitle": "PublicationTitle",
    "publicationtitle": "PublicationTitle",
    "doi": "DOI",
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

    v_clean = v.replace('"', "").strip()
    if not v_clean:
        return ""

    if match == "prefix" and " " not in v_clean and "-" not in v_clean:
        if not v_clean.endswith("*") and len(v_clean) >= 3:
            v_clean = f"{v_clean}*"
        return v_clean

    # Aspas são obrigatórias para frases, termos com hífen (evita `-` como NOT)
    # e palavras reservadas; wildcards não são suportados dentro de aspas na ACM
    needs_quotes = (
        match in ("phrase", "exact")
        or " " in v_clean
        or "-" in v_clean
        or v_clean.upper() in _RESERVED_WORDS
        or any(ch in v_clean for ch in ("(", ")", ":", "/"))
    )
    if needs_quotes:
        v_quoted = v_clean.replace("*", "").replace("?", "").strip()
        return f'"{v_quoted}"' if v_quoted else ""

    return v_clean


def _format_term(term: Term, default_field: str = "") -> str:
    v = term.value.strip()
    if not v:
        return ""

    field = default_field
    if ":" in v:
        prefix, _, rest = v.partition(":")
        prefix_clean = prefix.strip().lower()
        rest_clean = rest.strip()

        if prefix_clean in _FIELD_MAP:
            field = _FIELD_MAP[prefix_clean]
            v = rest_clean

    formatted = _format_value(v, term.match)
    if not formatted:
        return ""
    return f"{field}:({formatted})" if field else formatted


def _is_negated(node) -> bool:
    return bool(getattr(node, "negated", False))


def _compile_node(node, default_field: str = "") -> tuple[str, bool]:
    """Retorna `(expressao_compilada, esta_negada)`.

    Propaga `negated` para o grupo pai emitir `A AND NOT B`.
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

        inner = (
            negatives[0]
            if len(negatives) == 1
            else "(" + f" {op} ".join(negatives) + ")"
        )
        return inner, not _is_negated(node)

    return "", False


def compile_expression(expression: str, default_field: str = "") -> str:
    """Expressão booleana Lelia -> sintaxe de consulta ACM Digital Library."""
    raw = (expression or "").strip()
    if not raw:
        return ""

    # Mantém intacta caso já contenha prefixos nativos da ACM DL
    if re.search(
        r"\b(?:AllField|Title|Abstract|Keyword|Author|Affiliation|PublicationTitle|DOI|ISSN|ISBN)\s*:\s*\(",
        raw,
    ):
        return raw

    node = parse_expression(raw)
    if node is None:
        cleaned = _format_value(raw, "phrase" if (" " in raw or "-" in raw) else "term")
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
        return f"NOT {expr}"
    return expr


class ACMQueryAdapter(QueryAdapter):
    source_type = "acm"

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