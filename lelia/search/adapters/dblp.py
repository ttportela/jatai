"""Adaptador DBLP.

Sintaxe DBLP (documentada):
- AND  = espaço entre termos (`graph network`)
- palavra exata = `palavra$`; busca é por prefixo por padrão
- case-insensitive, ignora acentos
- NÃO há NOT, frase (`.`) nem agrupamento com parênteses documentado;
  o operador `|` existe mas a precedência com o AND implícito não é
  garantida — por segurança SEMPRE usamos DNF.

Estratégia: converte a QueryDefinition para forma normal disjuntiva
e cada cláusula AND vira uma consulta independente. Termos negados são
omitidos da consulta remota (devem ser filtrados localmente na ingestão).
"""

import re

from lelia.search.adapters.base import QueryAdapter
from lelia.search.builder import dnf
from lelia.search.domain import QueryDefinition, Term
from lelia.search.parser import parse_expression

MAX_TERMS_PER_QUERY = 8
_VALID_DBLP_FIELDS = {"author", "venue", "year"}


def _clean_expression(expression: str) -> str:
    """Fallback simples quando a expressão não é parseável como booleanas."""
    expr = expression.strip()
    # Converte author:"John Doe" -> author:John_Doe antes de remover aspas
    expr = re.sub(
        r'\bauthor\s*:\s*"([^"]+)"',
        lambda m: "author:" + "_".join(m.group(1).split()),
        expr,
        flags=re.IGNORECASE,
    )
    expr = re.sub(r"\bAND\b", " ", expr, flags=re.IGNORECASE)
    expr = re.sub(r"\bOR\b", "|", expr, flags=re.IGNORECASE)
    # Remove operador NOT e negações prefixadas (-termo)
    expr = re.sub(r"\bNOT\s+\S+", " ", expr, flags=re.IGNORECASE)
    expr = re.sub(r"(?:^|\s)-\S+", " ", expr)
    expr = expr.replace("||", "|")
    expr = re.sub(r"""["'()]""", " ", expr)
    expr = re.sub(r"(?<=[A-Za-z0-9])\.(?=[A-Za-z0-9])", " ", expr)
    expr = re.sub(
        r"\b(?!author\b|venue\b|year\b)[A-Za-z_][A-Za-z0-9_]*\s*:",
        "",
        expr,
        flags=re.IGNORECASE,
    )
    expr = re.sub(r"\s{2,}", " ", expr).strip(" |")
    return re.sub(r"\|{2,}", "|", expr).strip()


def _format_db_term(term: Term) -> str:
    """Term da AST -> texto DBLP. Omite termos negados e sanitiza campos."""
    if getattr(term, "negated", False):
        return ""

    v = term.value.strip()
    if not v:
        return ""

    field_prefix = ""
    if ":" in v:
        prefix, _, rest = v.partition(":")
        prefix, rest = prefix.strip().lower(), rest.strip()
        if prefix in _VALID_DBLP_FIELDS:
            if prefix == "author" and " " in rest:
                return f"author:{'_'.join(rest.split())}"
            field_prefix = f"{prefix}:"
            v = rest
        else:
            v = rest  # title:, doi:, p1:... não existem no DBLP

    if field_prefix:
        return f"{field_prefix}{v}" if v else ""

    # '.' entre palavras era operador de frase -> espaço (AND)
    v = re.sub(r"(?<=[A-Za-z0-9])\.(?=[A-Za-z0-9])", " ", v)

    if term.match == "exact":
        # [^\W_]+ preserva letras acentuadas (ex.: Müller$) e separa hífens
        tokens = re.findall(r"[^\W_]+", v)
        return " ".join(f"{tok}$" for tok in tokens)

    return v


def _compile_clauses(clauses: list[list[Term]], max_clauses: int) -> list[str]:
    """Converte cláusulas DNF em lista deduplicada de queries DBLP."""
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
            for p in (_format_db_term(t) for t in uniq[:MAX_TERMS_PER_QUERY])
            if re.search(r"[A-Za-z0-9]", p)
        ]
        q = re.sub(r"\s{2,}", " ", " ".join(parts)).strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)

    return out


def compile_expression(expression: str, max_clauses: int = 40) -> list[str]:
    """Expressão de busca -> cláusulas prontas para a API/scraper do DBLP."""
    node = parse_expression(expression)
    if node is None:
        cleaned = _clean_expression(expression)
        return [cleaned] if cleaned else []

    clauses = dnf(node, limit=max_clauses * 4)
    out = _compile_clauses(clauses, max_clauses=max_clauses)
    if not out:
        cleaned = _clean_expression(expression)
        return [cleaned] if cleaned else []
    return out


class DBLPQueryAdapter(QueryAdapter):
    source_type = "dblp"

    def compile(self, definition: QueryDefinition, limit: int = 25) -> list[str]:
        errs = definition.validate()
        if errs:
            raise ValueError(f"QueryDefinition inválida: {errs}")
        clauses = dnf(definition.root, limit=limit * 4)
        return _compile_clauses(clauses, max_clauses=limit)