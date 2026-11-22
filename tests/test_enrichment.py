"""Testes do enriquecimento de metadados com browser fake."""

import pytest
from sqlalchemy import select

from lelia import enrichment
from lelia.storage.db import get_project_session
from lelia.storage.models_project import Paper


class _FakePage:
    def __init__(self, data):
        self._data = data
        self.closed = False

    def goto(self, *a, **k):
        pass

    def wait_for_timeout(self, *a):
        pass

    def evaluate(self, js, cfg):
        return self._data

    def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self, data):
        self._data = data
        self.pages = []

    def new_page(self, user_agent=""):
        p = _FakePage(self._data)
        self.pages.append(p)
        return p


def _mk_paper(slug, title, abstract="", keywords="", url="https://x.com/a",
              doi="", state="novo"):
    with get_project_session(slug) as s:
        p = Paper(title=title, abstract=abstract, keywords=keywords,
                  url=url, doi=doi, state=state, authors="[]")
        s.add(p)
        s.commit()
        return p.id


@pytest.fixture()
def fake_browser(monkeypatch):
    browser = _FakeBrowser({"abstract": "Resumo extraído da página. " * 3,
                            "keywords": "trajetória, mineração, big data",
                            "title": "T"})
    monkeypatch.setattr(enrichment, "_launch", lambda: (None, browser))
    monkeypatch.setattr(enrichment, "_http_json", lambda *a, **k: None)
    return browser


def test_enrich_fills_missing_fields(slug, fake_browser):
    pid = _mk_paper(slug, "Paper X")
    progress = []
    out = enrichment.enrich_missing(
        slug, limit=5,
        on_progress=lambda *a: progress.append(a))
    assert out["processed"] == 1 and out["updated"] == 1
    with get_project_session(slug) as s:
        p = s.get(Paper, pid)
        assert "Resumo extraído" in p.abstract
        assert "trajetória" in p.keywords
    assert len(progress) == 1
    assert progress[0][0] == 1 and progress[0][1] == 1
    assert progress[0][4] == "ok"


def test_enrich_skips_excluded_and_complete(slug, fake_browser):
    _mk_paper(slug, "Completo", abstract="já tem", keywords="kw")
    _mk_paper(slug, "Excluído", state="excluido")
    pid = _mk_paper(slug, "Pendente")
    out = enrichment.enrich_missing(slug, limit=10)
    assert out["processed"] == 1
    with get_project_session(slug) as s:
        assert s.get(Paper, pid).abstract


def test_enrich_progress_reports_empty(slug, monkeypatch):
    browser = _FakeBrowser({"abstract": "", "keywords": "", "title": "T"})
    monkeypatch.setattr(enrichment, "_launch", lambda: (None, browser))
    _mk_paper(slug, "Sem dados")
    progress = []
    out = enrichment.enrich_missing(
        slug, limit=5, on_progress=lambda *a: progress.append(a))
    assert out["updated"] == 0
    assert progress[0][4] == "vazio"


def test_enrich_sem_url(slug):
    _mk_paper(slug, "Sem link", url="", doi="")
    out = enrichment.enrich_missing(slug, limit=5)
    # sem URL nem DOI não entra na fila
    assert out["processed"] == 0


def test_enrich_doi_browser_fallback(slug, fake_browser):
    """DOI sem dados nas APIs cai no scraping quando há URL real (não doi.org)."""
    _mk_paper(slug, "Só DOI", url="https://publisher.com/paper/1",
              doi="10.1000/xyz")
    out = enrichment.enrich_missing(slug, limit=5)
    assert out["updated"] == 1


def test_enrich_doi_api_success_skips_browser(slug, monkeypatch):
    """DOI resolvido via OpenAlex não usa o browser."""
    browser = _FakeBrowser({"abstract": "x", "keywords": "", "title": "T"})
    monkeypatch.setattr(enrichment, "_launch", lambda: (None, browser))

    def fake_http(url, timeout=20):
        if "openalex" in url:
            return {"abstract_inverted_index": {"Hello": [0], "world": [1]},
                    "keywords": [{"display_name": "trajetória"}]}
        return None
    monkeypatch.setattr(enrichment, "_http_json", fake_http)
    pid = _mk_paper(slug, "API", url="https://doi.org/10.1/x", doi="10.1/x")
    out = enrichment.enrich_missing(slug, limit=5)
    assert out["updated"] == 1
    assert not browser.pages  # browser nem chegou a abrir página
    with get_project_session(slug) as s:
        p = s.get(Paper, pid)
        assert p.abstract == "Hello world"
        assert p.keywords == "trajetória"


def test_enrich_doi_no_data_via_api(slug, fake_browser):
    """DOI só com url doi.org e APIs vazias -> não raspa doi.org (status vazio)."""
    progress = []
    _mk_paper(slug, "DOI puro", url="https://doi.org/10.9/z", doi="10.9/z")
    out = enrichment.enrich_missing(
        slug, limit=5, on_progress=lambda *a: progress.append(a))
    assert out["updated"] == 0
    assert progress[0][4] == "vazio"
    assert not fake_browser.pages


def test_clean_keywords():
    assert enrichment._clean_keywords("a, b; c and d") == "a, b, c, d"
    assert enrichment._clean_keywords("x" * 80) == ""
    assert enrichment._clean_keywords("a, a, b") == "a, b"


def test_missing_count_excludes_excluded(slug):
    _mk_paper(slug, "Exc", state="excluido")
    _mk_paper(slug, "Ok")
    assert enrichment.missing_metadata_count(slug) == 1
    # com url conta; sem url/doi não conta
    _mk_paper(slug, "Sem nada", url="", doi="")
    assert enrichment.missing_metadata_count(slug) == 1
    select(Paper)  # noqa
    with get_project_session(slug) as s:
        p = s.scalar(select(Paper).where(Paper.title == "Sem nada"))
        p.url = "https://x.com"
        s.commit()
    assert enrichment.missing_metadata_count(slug) == 2


def test_enrich_no_limit_processes_all(slug, fake_browser):
    for i in range(3):
        _mk_paper(slug, f"P{i}")
    out = enrichment.enrich_missing(slug, limit=None)
    assert out["processed"] == 3 and out["updated"] == 3


def test_enrich_limit_caps(slug, fake_browser):
    for i in range(3):
        _mk_paper(slug, f"P{i}")
    out = enrichment.enrich_missing(slug, limit=2)
    assert out["processed"] == 2
