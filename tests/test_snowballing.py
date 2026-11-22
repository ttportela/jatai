"""Task snowballing: referências do OpenAlex viram novas publicações."""

import json
from types import SimpleNamespace

from sqlalchemy import select

from lelia.agents import runner
from lelia.sources import openalex as openalex_mod
from lelia.sources.base import RawRecord
from lelia.storage.db import get_project_session
from lelia.storage.models_project import (
    Paper, PaperAnalysis, PaperSighting, ProjectSource)


class FakeOpenAlex(openalex_mod.OpenAlexConnector):
    def __init__(self):
        super().__init__()
        self.works = {}       # doi -> work dict
        self.by_title = {}    # title -> work dict
        self.refs = {}        # work id -> [ref ids]
        self.records = {}     # work id -> RawRecord

    def is_available(self):
        return True

    def work_by_doi(self, doi):
        return self.works.get(doi)

    def work_by_title(self, title):
        return self.by_title.get(title)

    def referenced_works(self, work):
        return list(self.refs.get(work.get("id"), []))

    def works_by_ids(self, work_ids):
        return [self.records[i] for i in work_ids if i in self.records]


def _mk_paper(slug, title, doi="", state="incluido"):
    with get_project_session(slug) as s:
        p = Paper(title=title, doi=doi, url="", state=state, authors="[]")
        s.add(p)
        s.commit()
        return p.id


def _enable_openalex(slug):
    with get_project_session(slug) as s:
        s.add(ProjectSource(source_type="openalex", name="OpenAlex",
                            enabled=True, config={}))
        s.commit()


def _task(params="{}"):
    return SimpleNamespace(id=1, task_type="snowballing", name="Snowballing",
                           params=params)


def _stats():
    return {"errors": []}


def _run(slug):
    return runner.create_run(slug)


def _setup(slug, monkeypatch):
    _enable_openalex(slug)
    fake = FakeOpenAlex()
    monkeypatch.setattr("lelia.sources.base.get_connector",
                        lambda stype, config=None: fake)
    return fake


def test_snowballing_importa_referencias_e_conta(slug, monkeypatch):
    fake = _setup(slug, monkeypatch)
    seed = _mk_paper(slug, "Artigo semente", doi="10.1/seed")
    known = _mk_paper(slug, "Já conhecido", doi="10.1/known", state="novo")
    fake.works["10.1/seed"] = {"id": "https://openalex.org/W1"}
    fake.refs["https://openalex.org/W1"] = ["W2", "W3"]
    fake.records["W2"] = RawRecord(title="Conhecido", doi="10.1/known",
                                    year=2020)
    fake.records["W3"] = RawRecord(title="Novo achado", doi="10.1/new",
                                    year=2021)

    stats = _stats()
    runner._snowballing(slug, _run(slug), 1, _task(), stats)

    assert stats["snowball_papers"] == 1
    assert stats["snowball_refs"] == 2
    assert stats["snowball_known"] == 1   # W2 casou com paper existente
    assert stats["snowball_new"] == 1     # W3 virou publicação nova
    assert stats["new_papers"] == 1

    with get_project_session(slug) as s:
        newp = s.scalar(select(Paper).where(Paper.doi == "10.1/new"))
        assert newp is not None and newp.state == "novo"
        assert newp.source == "openalex"
        # proveniência: sighting da fonte + sighting snowball com a semente
        sights = list(s.scalars(select(PaperSighting).where(
            PaperSighting.paper_id == newp.id)))
        by_src = {sg.source for sg in sights}
        assert by_src == {"openalex", "snowball"}
        sb = next(sg for sg in sights if sg.source == "snowball")
        raw = json.loads(sb.raw)
        assert raw["seed_paper_id"] == seed and "Artigo semente" in raw["seed_title"]
        ana = s.scalar(select(PaperAnalysis).where(
            PaperAnalysis.paper_id == seed,
            PaperAnalysis.kind == "snowball"))
        payload = json.loads(ana.result)
        assert payload["refs_listadas"] == 2
        assert payload["refs_novas"] == 1 and payload["refs_conhecidas"] == 1
        # artigo já processado não é reselecionado
        stats2 = _stats()
        runner._snowballing(slug, _run(slug), 1, _task(), stats2)
        assert stats2.get("snowball_papers", 0) == 0
        assert known is not None


def test_snowballing_sem_openalex_habilitada(slug, monkeypatch):
    fake = FakeOpenAlex()
    monkeypatch.setattr("lelia.sources.base.get_connector",
                        lambda stype, config=None: fake)
    _mk_paper(slug, "Semente", doi="10.1/x")
    stats = _stats()
    runner._snowballing(slug, _run(slug), 1, _task(), stats)
    assert stats.get("snowball_papers", 0) == 0


def test_snowballing_fallback_pdf_quando_sem_refs(slug, monkeypatch):
    """OpenAlex sem referenced_works -> extrai refs do PDF via LLM e
    resolve nas fontes (openalex + dblp), com sighting por fonte."""
    from lelia.agents import references as refs_mod
    from lelia.sources import dblp as dblp_mod

    fake = _setup(slug, monkeypatch)
    seed = _mk_paper(slug, "Semente sem refs", doi="10.1/seed")
    fake.works["10.1/seed"] = {"id": "https://openalex.org/W9"}
    fake.refs["https://openalex.org/W9"] = []  # obra existe, sem refs

    # LLM devolve 2 referências
    llm_result = refs_mod.ReferencesResult(references=[
        refs_mod.ReferenceItem(title="Ref A", doi="10.1/refa", year=2020),
        refs_mod.ReferenceItem(title="Ref B", year=2021),
    ])
    monkeypatch.setattr("lelia.agents.llm.chat_structured",
                        lambda *a, **kw: llm_result)
    monkeypatch.setattr("lelia.agents.runner._paper_full_text",
                        lambda *a, **kw: "TEXTO " * 200)

    # openalex resolve Ref A (por DOI); Ref B não existe lá
    fake.works["10.1/refa"] = {"id": "https://openalex.org/W100",
                               "display_name": "Ref A paper", "doi": "https://doi.org/10.1/refa"}
    fake.by_title["Ref B"] = None

    # dblp resolve Ref B por título
    class FakeDBLP:
        def search(self, expression, max_results=200, on_progress=None):
            assert "Ref B" in expression
            return [RawRecord(title="Ref B: um estudo", doi="10.1/refb",
                              year=2021)]

    monkeypatch.setattr(
        "lelia.sources.base.get_connector",
        lambda stype, config=None: fake if stype == "openalex" else FakeDBLP())
    with get_project_session(slug) as s:
        s.add(ProjectSource(source_type="dblp", name="DBLP",
                            enabled=True, config={}))
        s.commit()

    stats = _stats()
    runner._snowballing(slug, _run(slug), 1, _task(), stats)

    assert stats["snowball_new"] == 2
    with get_project_session(slug) as s:
        pa = s.scalar(select(Paper).where(Paper.doi == "10.1/refa"))
        pb = s.scalar(select(Paper).where(Paper.doi == "10.1/refb"))
        assert pa is not None and pb is not None
        # Ref A veio do openalex; Ref B do dblp — cada um com sighting
        # da fonte + sighting snowball da semente
        for p, expected_src in ((pa, "openalex"), (pb, "dblp")):
            srcs = {sg.source for sg in s.scalars(
                select(PaperSighting).where(PaperSighting.paper_id == p.id))}
            assert expected_src in srcs and "snowball" in srcs
        ana = s.scalar(select(PaperAnalysis).where(
            PaperAnalysis.paper_id == seed,
            PaperAnalysis.kind == "snowball"))
        payload = json.loads(ana.result)
        assert "PDF" in payload["note"]


def test_snowballing_fallback_desativado_por_param(slug, monkeypatch):
    """pdf_fallback=0 -> não chama LLM nem resolve no PDF."""
    fake = _setup(slug, monkeypatch)
    _mk_paper(slug, "Semente", doi="10.1/seed")
    fake.works["10.1/seed"] = {"id": "https://openalex.org/W9"}
    fake.refs["https://openalex.org/W9"] = []

    def boom(*a, **kw):
        raise AssertionError("LLM não deveria ser chamado")
    monkeypatch.setattr("lelia.agents.llm.chat_structured", boom)

    stats = _stats()
    runner._snowballing(slug, _run(slug), 1,
                        _task('{"pdf_fallback": false}'), stats)
    assert stats["snowball_papers"] == 1
    assert stats.get("snowball_new", 0) == 0


def test_snowballing_obra_nao_encontrada_marca_e_segue(slug, monkeypatch):
    fake = _setup(slug, monkeypatch)
    _mk_paper(slug, "Sem obra", doi="")  # sem DOI e sem match por título
    stats = _stats()
    runner._snowballing(slug, _run(slug), 1, _task(), stats)
    assert stats["snowball_papers"] == 1
    with get_project_session(slug) as s:
        ana = s.scalar(select(PaperAnalysis).where(
            PaperAnalysis.kind == "snowball"))
        assert "não encontrada" in ana.result
