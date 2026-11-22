"""Constrói QueryDefinition a partir da configuração científica do projeto."""

from lelia.research import service as research
from lelia.search.domain import Group, QueryDefinition, Term


def _term_from_keyword(term: str) -> Term:
    """Termo com múltiplas palavras vira phrase; palavra única vira prefix."""
    t = term.strip()
    return Term(value=t, match="phrase" if " " in t else "prefix")


def definition_from_areas(slug: str, name: str = "configuração da pesquisa") -> QueryDefinition | None:
    """AND entre áreas; OR entre palavras-chave dentro de cada área."""
    areas = research.list_areas(slug)
    groups = []
    for area in areas:
        # defesa: termos antigos podem conter vírgulas não separadas
        raw_terms = [p.strip() for k in area.keywords for p in k.term.split(",")]
        terms = [_term_from_keyword(t) for t in raw_terms if t]
        if not terms:
            continue
        node = terms[0] if len(terms) == 1 else Group("OR", terms)
        groups.append(node)
    if not groups:
        return None
    root = groups[0] if len(groups) == 1 else Group("AND", groups)
    return QueryDefinition(name=name, root=root)


def dnf(node, limit: int = 200) -> list[list[Term]]:
    """Converte a árvore para forma normal disjuntiva (lista de cláusulas AND).

    Retorna no máximo `limit` cláusulas para evitar explosão combinatória.
    Nós negados (NOT) não são expressáveis em cláusulas de termos
    positivos e são ignorados — fontes baseadas em DNF não suportam
    negação (limitação documentada).
    """
    if getattr(node, "negated", False):
        return []
    if isinstance(node, Term):
        return [[node]]
    if isinstance(node, Group):
        children = [c for c in node.children
                    if not getattr(c, "negated", False)]
        if node.op == "OR":
            out: list[list[Term]] = []
            for child in children:
                out.extend(dnf(child, limit))
                if len(out) >= limit:
                    return out[:limit]
            return out
        # AND: produto cartesiano das cláusulas filhas
        result: list[list[Term]] = [[]]
        for child in children:
            child_clauses = dnf(child, limit)
            if not child_clauses:
                continue  # filho só-negado: não restringe a cláusula
            result = [left + right for left in result for right in child_clauses]
            if len(result) >= limit:
                return result[:limit]
        return result
    return []
