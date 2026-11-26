"""Adaptadores de consulta por fonte (QueryDefinition -> expressões)."""

from jatai.search.adapters import semanticscholar
from jatai.search.adapters.base import get_adapter
from jatai.search.domain import Group, QueryDefinition, Term


def _def(root) -> QueryDefinition:
    return QueryDefinition(name="t", root=root)


def test_s2_adapter_dnf_e_or():
    """(a OR b) AND (c OR d) -> 4 consultas, uma por combinação."""
    root = Group("AND", [
        Group("OR", [Term("a"), Term("b")]),
        Group("OR", [Term("c"), Term("d")]),
    ])
    out = semanticscholar.SemanticScholarQueryAdapter().compile(_def(root))
    assert out == ["a c", "a d", "b c", "b d"]


def test_s2_adapter_frase_e_exata_entre_aspas():
    root = Group("AND", [
        Term("data mining", match="phrase"),
        Term("graph", match="exact"),
        Term("clas", match="prefix"),
    ])
    out = semanticscholar.SemanticScholarQueryAdapter().compile(_def(root))
    assert out == ['"data mining" "graph" clas']


def test_s2_adapter_termo_simples():
    out = semanticscholar.SemanticScholarQueryAdapter().compile(
        _def(Term("mobility")))
    assert out == ["mobility"]


def test_s2_adapter_respeita_limit_e_dedup():
    root = Group("OR", [Term("x"), Term("x"), Term("y")])
    out = semanticscholar.SemanticScholarQueryAdapter().compile(
        _def(root), limit=1)
    assert out == ["x"]


def test_s2_adapter_registrado():
    adapter = get_adapter("semantic_scholar")
    assert isinstance(adapter, semanticscholar.SemanticScholarQueryAdapter)
