"""Adaptador arXiv (Atom API /api/query).

Sintaxe arXiv (`search_query`):
- Operadores booleanos: AND, OR, ANDNOT (em maiúsculas; ``ANDNOT`` é uma
  palavra única e operador binário — não existe ``NOT`` unário isolado)
- Agrupamento: parênteses (...)
- Frase exata: "..."
- Wildcards: suportados apenas em categorias (ex.: ``cat:cs*``). Nos campos
  textuais (``all``, ``ti``, ``abs``, ``au``), a API não aceita ``*``/``?``
  e já aplica stemming automaticamente nos termos simples.
- Campos suportados: ``all:``, ``ti:``, ``au:``, ``abs:``, ``co:``,
  ``jr:``, ``cat:``, ``rn:``, ``submittedDate:[YYYYMMDDHHMM TO YYYYMMDDHHMM]``
"""

import re

from jatai.search.adapters.base import QueryAdapter
from jatai.search.domain import Group, QueryDefinition, Term
from jatai.search.parser import parse_expression

_RESERVED_WORDS = {"AND", "OR", "NOT", "ANDNOT", "TO"}

_FIELD_MAP = {
    "all": "all",
    "allfield": "all",
    "title": "ti",
    "ti": "ti",
    "abs": "abs",
    "abstract": "abs",
    "ab": "abs",
    "author": "au",
    "auth": "au",
    "au": "au",
    "cat": "cat",
    "category": "cat",
    "categories": "cat",
    "subjarea": "cat",
    "comment": "co",
    "co": "co",
    "venue": "jr",
    "journal": "jr",
    "srctitle": "jr",
    "jr": "jr",
    "rn": "rn",
    "report": "rn",
}


def _strip_outer_delimiters(value: str) -> str:
    if len(value) >= 2 and (
        (value[0] == '"' and value[-1] == '"')
        or (value[0] == "'" and value[-1] == "'")
        or (value[0] == "{" and value[-1] == "}")
    ):
        return value[1:-1].strip()
    return value


def _format_submitted_date(spec: str) -> str:
    """Converte '2024', '2020-2026', '2022-', '-2021' em filtro `submittedDate`."""
    s = spec.strip()
    if re.fullmatch(r"\d{4}", s):
        return f"submittedDate:[{s}01010000 TO {s}12312359]"
    m_range = re.fullmatch(r"(\d{4})\s*-\s*(\d{4})", s)
    if m_range:
        return f"submittedDate:[{m_range.group(1)}01010000 TO {m_range.group(2)}12312359]"
    m_from = re.fullmatch(r"(\d{4})\s*-", s)
    if m_from:
        return f"submittedDate:[{m_from.group(1)}01010000 TO 209912312359]"
    m_to = re.fullmatch(r"-\s*(\d{4})", s)
    if m_to:
        return f"submittedDate:[199101010000 TO {m_to.group(1)}12312359]"
    return ""


def _format_value(raw_value: str, match: str, field: str) -> str:
    v = _strip_outer_delimiters(raw_value.strip())
    if not v:
        return ""

    v_clean = v.replace('"', "").strip()
    if not v_clean:
        return ""

    # Categorias arXiv (ex.: cs.LG, cs*) não levam aspas e aceitam '*' no sufixo
    if field == "cat":
        cat_val = v_clean.replace(" ", "").lstrip("*?")
        if match == "prefix" and cat_val and not cat_val.endswith("*"):
            cat_val = f"{cat_val}*"
        return cat_val

    # Nos demais campos, o arXiv não suporta wildcards (* / ?) e já faz stemming
    v_clean = v_clean.replace("*", "").replace("?", "").strip()
    if not v_clean:
        return ""

    needs_quotes = (
        match in ("phrase", "exact")
        or " " in v_clean
        or "-" in v_clean
        or v_clean.upper() in _RESERVED_WORDS
        or any(ch in v_clean for ch in ("(", ")", ":", "[", "]"))
    )
    if needs_quotes:
        return f'"{v_clean}"'

    return v_clean


def _format_term(term: Term, default_field: str = "all") -> str:
    v = term.value.strip()
    if not v:
        return ""

    field = default_field
    if ":" in v:
        prefix, _, rest = v.partition(":")
        prefix_clean = prefix.strip().lower()
        rest_clean = rest.strip()

        if prefix_clean in ("year", "pubyear", "submitteddate"):
            date_expr = _format_submitted_date(rest_clean)
            if date_expr:
                return date_expr

        if prefix_clean in _FIELD_MAP:
            field = _FIELD_MAP[prefix_clean]
            v = rest_clean

    formatted = _format_value(v, term.match, field=field)
    if not formatted:
        return ""
    return f"{field}:{formatted}" if field else formatted


def _is_negated(node) -> bool:
    return bool(getattr(node, "negated", False))


def _compile_node(node, default_field: str = "all") -> tuple[str, bool]:
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
                neg_joined = " ANDNOT ".join(negatives)
                expr = f"({base} ANDNOT {neg_joined})"
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


def compile_expression(expression: str, default_field: str = "all") -> str:
    """Expressão booleana Jataí -> sintaxe `search_query` do arXiv."""
    raw = (expression or "").strip()
    if not raw:
        return ""

    if re.search(
        r"\b(?:all|ti|au|abs|co|jr|cat|rn|submittedDate)\s*:|\bANDNOT\b",
        raw,
    ):
        return raw

    node = parse_expression(raw)
    if node is None:
        cleaned = _format_value(
            raw,
            "phrase" if (" " in raw or "-" in raw) else "term",
            field=default_field,
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
        return f"submittedDate:[199101010000 TO 209912312359] ANDNOT {expr}"
    return expr


class ArxivQueryAdapter(QueryAdapter):
    source_type = "arxiv"

    def compile(self, definition: QueryDefinition, limit: int = 25) -> list[str]:
        errs = definition.validate()
        if errs:
            raise ValueError(f"QueryDefinition inválida: {errs}")
        expr, negated = _compile_node(definition.root)
        if not expr:
            return []
        if negated:
            expr = f"submittedDate:[199101010000 TO 209912312359] ANDNOT {expr}"
        return [expr]