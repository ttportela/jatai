"""Criação manual de publicação + parser BibTeX."""

from sqlalchemy import func, select

from jatai.catalog import service as catalog
from jatai.catalog.bibtex import parse_bibtex
from jatai.storage.db import get_project_session
from jatai.storage.models_project import Paper, PaperSighting

BIB = """
@article{silva2024,
  title = {Aprendizado de Máquina em {Saúde} Pública},
  author = {Silva, Ana and Souza, Bruno and Costa, Carla},
  year = {2024},
  doi = {10.1234/abc.2024},
  journal = {Revista Brasileira de Informática em Saúde},
  abstract = {Estudo sobre predição de doenças.},
  keywords = {machine learning, saúde, predição},
  url = {https://example.org/paper}
}

@inproceedings{lee2023,
  title = {Graph Neural Networks for Mobility},
  author = {Lee, Kim},
  booktitle = {Proc. KDD},
  year = {2023},
  pages = {1--10}
}
"""


def test_parse_bibtex_article():
    entries = parse_bibtex(BIB)
    assert len(entries) == 2
    e = entries[0]
    assert e["title"] == "Aprendizado de Máquina em Saúde Pública"
    assert e["authors"] == ["Silva, Ana", "Souza, Bruno", "Costa, Carla"]
    assert e["year"] == 2024
    assert e["doi"] == "10.1234/abc.2024"
    assert e["venue"] == "Revista Brasileira de Informática em Saúde"
    assert "predição" in e["abstract"]
    assert e["pub_type"] == "artigo"
    assert e["url"] == "https://example.org/paper"


def test_parse_bibtex_inproceedings_booktitle():
    e = parse_bibtex(BIB)[1]
    assert e["venue"] == "Proc. KDD"
    assert e["pub_type"] == "conferência"
    assert e["year"] == 2023


def test_parse_bibtex_sem_entrada():
    assert parse_bibtex("texto qualquer sem @") == []
    assert parse_bibtex("") == []


def test_parse_bibtex_ignora_sem_titulo():
    assert parse_bibtex("@article{x, author={A}}") == []


def test_create_manual_paper(slug):
    pid = catalog.create_manual_paper(slug, {
        "title": "Artigo manual",
        "authors": "Ana Silva, Bruno Souza",
        "year": "2024",
        "doi": "10.9999/manual",
        "venue": "Journal X",
        "pub_type": "artigo",
        "keywords": "ia, revisão",
        "abstract": "Resumo do artigo.",
    })
    assert pid
    with get_project_session(slug) as s:
        p = s.get(Paper, pid)
        assert p.title == "Artigo manual"
        assert p.state == "novo"
        assert p.source == "manual"
        assert p.year == 2024
        assert p.venue == "Journal X"
        assert '"Ana Silva"' in p.authors or "Ana Silva" in p.authors
        sight = s.scalar(select(PaperSighting).where(
            PaperSighting.paper_id == pid, PaperSighting.source == "manual"))
        assert sight is not None


def test_create_manual_paper_sem_titulo(slug):
    assert catalog.create_manual_paper(slug, {"title": "  "}) is None
    with get_project_session(slug) as s:
        n = s.scalar(select(func.count()).select_from(Paper))
        assert n == 0


def test_create_manual_paper_duplicado_doi(slug):
    fields = {"title": "Primeiro", "doi": "10.1111/dup"}
    pid1 = catalog.create_manual_paper(slug, fields)
    pid2 = catalog.create_manual_paper(slug, {**fields, "title": "Primeiro v2"})
    assert pid1 == pid2
    with get_project_session(slug) as s:
        n = s.scalar(select(func.count()).select_from(Paper))
        assert n == 1
