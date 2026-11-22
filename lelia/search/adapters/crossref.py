"""Adaptador Crossref (REST API /works).

Sintaxe Crossref:
- A API REST do Crossref utiliza ranqueamento por relevância (Solr/DisMax)
  e **não** suporta operadores booleanos estritos (``AND``, ``OR``, ``NOT``),
  parênteses, wildcards (``*``, ``?``) nem frases exatas entre aspas em
  ``query`` / ``query.bibliographic``.
- Além disso, o Crossref exige que pelo menos ~20% dos termos da consulta
  estejam presentes no documento; consultas com muitos sinônimos unidos por
  ``OR`` diluem a relevância.
- Estratégia (mesma do DBLP): converte a árvore booleana para Forma Normal
  Disjuntiva (DNF), gerando uma consulta enxuta por cláusula ``AND``.
  Termos negados (``NOT``) são omitidos da consulta remota (devem ser
  filtrados localmente na ingestão).
- Campos suportados pelo adaptador (extraídos pelo conector para parâmetros
  nativos ``query.*`` e ``filter``): ``title:``, ``author:``, ``venue:``,
  ``affil:``, ``doi:``, ``issn:``, ``year:``.
"""

import re

from lelia.search.adapters.base import QueryAdapter
from lelia.search.builder import dnf
from lelia.search.domain import QueryDefinition, Term
from lelia.search.parser import parse_expression

MAX_TERMS_PER_QUERY = 10

_FIELD_MAP = {
    "title": "title",
    "ti": "title",
    "author": "author",
    "auth": "author",
    "au": "author",
    "venue": "container-title",
    "journal": "container-title",
    "srctitle": "container-title",
    "container": "container-title",
    "affil": "affiliation",
    "affiliation": "affiliation",
    "doi": "doi",
    "issn": "issn",
    "year": "year",
    "pubyear": "year",
}

# Campos que no Crossref vão para `query.bibliographic` (busca geral)
_BIBLIOGRAPHIC_FIELDS = {"all", "abs", "abstract", "ab", "key", "keyword", "keywords", "kw"}


def _strip_outer_delimiters(value: str) -> str:
    if len(value) >= 2 and (
        (value[0] == '"' and value[-1] == '"')
        or (value[0] == "'" and value[-1] == "'")
        or (value[0] == "{" and value[-1] == "}")
    ):
        return value[1:-1].strip()
    return value


def _sanitize_text(raw_value: str) -> str:
    v = _strip_outer_delimiters(raw_value.strip())
    # Remove caracteres de sintaxe booleana/wildcard não suportados pelo Crossref
    v = re.sub(r'["\'(){}*?]', " ", v)
    return re.sub(r"\s{2,}", " ", v).strip()


def _clean_expression(expression: str) -> str:
    """Fallback quando a expressão não é parseável como booleana."""
    expr = (expression or "").strip()
    expr = re.sub(r"\b(?:AND|OR)\b", " ", expr, flags=re.IGNORECASE)
    expr = re.sub(r"\bNOT\s+\S+", " ", expr, flags=re.IGNORECASE)
    expr = re.sub(r"(?:^|\s)-\S+", " ", expr)
    expr = re.sub(r'["\'(){}*?]', " ", expr)
    return re.sub(r"\s{2,}", " ", expr).strip()


def _format_crossref_term(term: Term) -> str:
    """Term da AST -> token limpo ou `campo:valor` canônico para o conector."""
    if getattr(term, "negated", False):
        return ""

    v = term.value.strip()
    if not v:
        return ""

    if ":" in v:
        prefix, _, rest = v.partition(":")
        prefix_clean = prefix.strip().lower()
        rest_clean = rest.strip()

        if prefix_clean in ("doi", "issn", "year", "pubyear"):
            canonical_key = "year" if prefix_clean == "pubyear" else prefix_clean
            val = _strip_outer_delimiters(rest_clean).replace(" ", "")
            return f"{canonical_key}:{val}" if val else ""

        if prefix_clean in _FIELD_MAP:
            canonical_key = _FIELD_MAP[prefix_clean]
            val = _sanitize_text(rest_clean)
            if not val:
                return ""
            return f'{canonical_key}:"{val}"' if " " in val else f"{canonical_key}:{val}"

        if prefix_clean in _BIBLIOGRAPHIC_FIELDS:
            return _sanitize_text(rest_clean)

    return _sanitize_text(v)


def _compile_clauses(clauses: list[list[Term]], max_clauses: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    for clause in clauses[:max_clauses]:
        seen_t: set[str] = set()
        uniq: list[Term] = []
        for t in clause:
            if getattr(t, "negated", False):
                continue
            key = t.value.strip().lower()
            if key and key not in seen_t:
                seen_t.add(key)
                uniq.append(t)

        parts = [
            p
            for p in (_format_crossref_term(t) for t in uniq[:MAX_TERMS_PER_QUERY])
            if p
        ]
        q = re.sub(r"\s{2,}", " ", " ".join(parts)).strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)

    return out


def compile_expression(expression: str, max_clauses: int = 25) -> list[str]:
    """Expressão booleana Lelia -> lista de consultas DNF prontas para Crossref."""
    raw = (expression or "").strip()
    if not raw:
        return []

    node = parse_expression(raw)
    if node is None:
        cleaned = _clean_expression(raw)
        return [cleaned] if cleaned else []

    clauses = dnf(node, limit=max_clauses * 4)
    out = _compile_clauses(clauses, max_clauses=max_clauses)
    if not out:
        cleaned = _clean_expression(raw)
        return [cleaned] if cleaned else []
    return out


class CrossrefQueryAdapter(QueryAdapter):
    source_type = "crossref"

    def compile(self, definition: QueryDefinition, limit: int = 25) -> list[str]:
        errs = definition.validate()
        if errs:
            raise ValueError(f"QueryDefinition inválida: {errs}")
        clauses = dnf(definition.root, limit=limit * 4)
        return _compile_clauses(clauses, max_clauses=limit)