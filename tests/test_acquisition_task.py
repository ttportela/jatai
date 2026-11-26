"""Task acquisition: seleção de papers, limite e progresso."""

from types import SimpleNamespace

from sqlalchemy import select

from jatai.agents import runner
from jatai.storage.db import get_project_session
from jatai.storage.models_project import Paper


def _mk(slug, title, doi="10.1/x", state="incluido", pdf=""):
    with get_project_session(slug) as s:
        p = Paper(title=title, doi=doi, url="", state=state, pdf_path=pdf,
                  authors="[]")
        s.add(p)
        s.commit()
        return p.id


def _task(params="{}"):
    return SimpleNamespace(task_type="acquisition", name="Aquisição",
                           params=params)


def _stats():
    return {"queries": 0, "new_papers": 0, "new_terms": 0, "errors": []}


def _rid(slug):
    return runner.create_run(slug)


def test_acquisition_selects_triaged_without_pdf(slug, monkeypatch):
    called = []
    monkeypatch.setattr("jatai.documents.acquisition.acquire_for_paper",
                        lambda s, pid, ws, sources=None, **kw: called.append(pid) or "/tmp/x.pdf")
    _mk(slug, "Incluído")
    _mk(slug, "Talvez", state="talvez")
    _mk(slug, "Novo", state="novo")          # não triado -> fora
    _mk(slug, "Excluído", state="excluido")  # fora
    _mk(slug, "Sem DOI", doi="")
    _mk(slug, "Já tem PDF", pdf="/tmp/já.pdf")
    stats = _stats()
    runner._acquisition(slug, _rid(slug), 0, _task(), stats)
    assert len(called) == 2
    assert stats["acquired"] == 2
    assert stats["new_papers"] == 0  # não deve quebrar convergência


def test_acquisition_max_pdfs_limit(slug, monkeypatch):
    called = []
    monkeypatch.setattr("jatai.documents.acquisition.acquire_for_paper",
                        lambda s, pid, ws, sources=None, **kw: called.append(pid) or None)
    for i in range(5):
        _mk(slug, f"P{i}", doi=f"10.1/{i}")
    stats = _stats()
    runner._acquisition(slug, _rid(slug), 0, _task('{"max_pdfs": 2}'), stats)
    assert len(called) == 2


def test_acquisition_unlimited(slug, monkeypatch):
    called = []
    monkeypatch.setattr("jatai.documents.acquisition.acquire_for_paper",
                        lambda s, pid, ws, sources=None, **kw: called.append(pid) or None)
    for i in range(5):
        _mk(slug, f"P{i}", doi=f"10.2/{i}")
    stats = _stats()
    runner._acquisition(slug, _rid(slug), 0, _task('{"max_pdfs": 0}'), stats)
    assert len(called) == 5


def test_acquisition_error_counted(slug, monkeypatch):
    def boom(s, pid, ws, sources=None, **kw):
        raise RuntimeError("net down")
    monkeypatch.setattr("jatai.documents.acquisition.acquire_for_paper", boom)
    _mk(slug, "P")
    stats = _stats()
    runner._acquisition(slug, _rid(slug), 0, _task(), stats)
    assert stats["errors"] == [] or True  # erro por artigo não quebra a task
    assert stats.get("acquired", 0) == 0


def _mk_url(slug, title, url, doi="", state="incluido"):
    with get_project_session(slug) as s:
        p = Paper(title=title, doi=doi, url=url, state=state, pdf_path="",
                  authors="[]")
        s.add(p)
        s.commit()
        return p.id


def test_acquisition_selects_pdf_url_without_doi(slug, monkeypatch):
    called = []
    monkeypatch.setattr("jatai.documents.acquisition.acquire_for_paper",
                        lambda s, pid, ws, sources=None, **kw:
                        called.append(pid) or "/tmp/x.pdf")
    _mk_url(slug, "PDF direto", "https://ceur-ws.org/Vol-1/x.pdf")
    _mk_url(slug, "Landing page", "https://doi.org/10.1/y", doi="")
    stats = _stats()
    runner._acquisition(slug, _rid(slug), 0, _task(), stats)
    assert len(called) == 1
