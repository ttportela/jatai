"""Testes da tarefa search / execute_query com conector fake."""

import pytest
from sqlalchemy import func, select

from jatai.catalog import service as catalog
from jatai.research import service as research
from jatai.sources.base import get_connector
from tests.fake_source import FakeConnector, rec


@pytest.fixture(autouse=True)
def fake_connector(monkeypatch):
    FakeConnector.reset()
    monkeypatch.setattr("jatai.catalog.service.get_connector",
                        lambda stype, config=None: FakeConnector() if stype == "fake" else get_connector(stype, config))
    return FakeConnector


def _query(slug, expr="alpha beta", source="fake", status="pendente", enabled=True):
    qid = research.create_query(slug, expr, source, status=status)
    research.set_query_enabled(slug, qid, enabled)
    return qid


def test_records_ingested_and_execution_logged(slug, fake_connector):
    fake_connector.results = {"alpha beta": [rec("Paper A", 2021, "10.1/a"),
                                             rec("Paper B", 2022, "10.1/b")]}
    qid = _query(slug)
    out = catalog.execute_query(slug, qid)
    assert out["new"] == 2 and out["duplicates"] == 0 and out["errors"] == []

    from jatai.storage.db import get_project_session
    from jatai.storage.models_project import Paper, Query, QueryExecution
    with get_project_session(slug) as s:
        assert s.scalar(select(func.count(Paper.id))) == 2
        assert s.get(Query, qid).status == "executada"
        ex = s.scalar(select(QueryExecution))
        assert ex.source == "fake" and ex.status == "ok"
        assert ex.results_count == 2 and ex.new_count == 2
        assert ex.expression == "alpha beta" and ex.pages == 1
        assert "fake://search" in ex.request


def test_search_results_salvos_em_json(slug, fake_connector):
    """Cada execução de consulta grava os resultados em
    <workspace>/searches/*.json."""
    import json as _json
    from pathlib import Path

    fake_connector.results = {"alpha beta": [rec("Paper A", 2021, "10.1/a")]}
    qid = _query(slug)
    catalog.execute_query(slug, qid)

    from jatai.catalog.service import projects_workspace
    d = Path(projects_workspace(slug)) / "searches"
    files = list(d.glob("*.json"))
    assert len(files) == 1
    data = _json.loads(files[0].read_text(encoding="utf-8"))
    assert data["query_id"] == qid and data["source"] == "fake"
    assert data["expression"] == "alpha beta"
    assert data["counts"]["fetched"] == 1 and data["counts"]["new"] == 1
    assert data["status"] == "ok"
    assert data["results"][0]["title"] == "Paper A"
    assert data["results"][0]["doi"] == "10.1/a"
    assert "fake://search" in data["request"]["url"]


def _search_files(slug):
    import json as _json
    from pathlib import Path
    from jatai.catalog.service import projects_workspace
    d = Path(projects_workspace(slug)) / "searches"
    return [_json.loads(f.read_text(encoding="utf-8"))
            for f in sorted(d.glob("*.json"))]


def test_search_vazio_tambem_salva(slug, fake_connector):
    fake_connector.results = {}
    qid = _query(slug)
    catalog.execute_query(slug, qid)
    files = _search_files(slug)
    assert len(files) == 1
    assert files[0]["status"] == "vazio" and files[0]["results"] == []


def test_search_erro_tambem_salva(slug, fake_connector):
    fake_connector.error = ConnectionError("fonte fora do ar")
    qid = _query(slug)
    catalog.execute_query(slug, qid)
    files = _search_files(slug)
    assert len(files) == 1
    assert files[0]["status"] == "erro"
    assert "fora do ar" in files[0]["error"]


def test_save_search_results_grava_json_bruto(slug):
    """`responses` preserva o JSON cru de cada página da API."""
    from jatai.catalog.service import _save_search_results
    raw = [{"total": 2, "token": "t2", "data": [{"paperId": "p1"}]},
           {"total": 2, "data": [{"paperId": "p2"}]}]
    path = _save_search_results(slug, 7, "semantic_scholar", "mobility",
                                [], {"url": "x"}, {"fetched": 0},
                                status="ok", responses=raw)
    assert path
    import json as _json
    data = _json.loads(open(path, encoding="utf-8").read())
    assert data["responses"] == raw
    assert data["source"] == "semantic_scholar"


def test_empty_results_marks_vazio(slug, fake_connector):
    fake_connector.results = {}
    qid = _query(slug)
    out = catalog.execute_query(slug, qid)
    assert out["new"] == 0
    from jatai.storage.db import get_project_session
    from jatai.storage.models_project import QueryExecution
    with get_project_session(slug) as s:
        ex = s.scalar(select(QueryExecution))
        assert ex.status == "vazio" and ex.results_count == 0


def test_connector_error_recorded(slug, fake_connector):
    fake_connector.error = ConnectionError("fonte fora do ar")
    qid = _query(slug)
    out = catalog.execute_query(slug, qid)
    assert out["new"] == 0 and any("fake" in e for e in out["errors"])
    from jatai.storage.db import get_project_session
    from jatai.storage.models_project import Query, QueryExecution
    with get_project_session(slug) as s:
        assert s.get(Query, qid).status == "erro"
        ex = s.scalar(select(QueryExecution))
        assert ex.status == "erro" and "fora do ar" in ex.error


def test_duplicates_counted_on_rerun(slug, fake_connector):
    fake_connector.results = {"alpha beta": [rec("Paper A", 2021, "10.1/a")]}
    qid = _query(slug)
    catalog.execute_query(slug, qid)
    qid2 = _query(slug, expr="alpha beta 2")
    fake_connector.results["alpha beta 2"] = [rec("Paper A", 2021, "10.1/a")]
    out = catalog.execute_query(slug, qid2)
    assert out["new"] == 0 and out["duplicates"] == 1


def test_coverage_filter_drops_out_of_range(slug, fake_connector):
    research.set_config(slug, {"coverage_start": "2020", "coverage_end": "2021"})
    fake_connector.results = {"alpha beta": [
        rec("Dentro", 2021, "10.1/in"),
        rec("Fora", 2010, "10.1/out"),
        rec("Sem ano", None, "10.1/na"),
    ]}
    qid = _query(slug)
    out = catalog.execute_query(slug, qid)
    assert out["new"] == 2  # 2010 descartado; sem ano mantido
    from jatai.storage.db import get_project_session
    from jatai.storage.models_project import QueryExecution
    with get_project_session(slug) as s:
        ex = s.scalar(select(QueryExecution))
        assert ex.filtered_count == 1


def test_query_not_found(slug):
    assert catalog.execute_query(slug, 99999)["error"] == "consulta não encontrada"


def test_source_restricted_to_query_source(slug, fake_connector, monkeypatch):
    """Consulta com source='fake' não deve chamar outros conectores."""
    called = []
    monkeypatch.setattr("jatai.catalog.service.get_connector",
                        lambda stype, config=None: called.append(stype) or FakeConnector())
    fake_connector.results = {"x": [rec("P1")]}
    qid = _query(slug, expr="x", source="fake")
    catalog.execute_query(slug, qid)
    assert called == ["fake"]


def test_unimplemented_source_records_error(slug):
    """Fonte habilitada sem conector gera erro auditado, não exceção."""
    from jatai.storage.db import get_project_session
    from jatai.storage.models_project import ProjectSource
    with get_project_session(slug) as s:
        s.add(ProjectSource(source_type="inexistente", name="X", enabled=True, config={}))
        s.commit()
    qid = _query(slug, source="")  # todas as fontes habilitadas
    research.set_query_enabled(slug, qid, True)
    # desativa fake para isolar
    from jatai.storage.models_project import ProjectSource as PS
    from sqlalchemy import select
    with get_project_session(slug) as s:
        for src in s.scalars(select(PS).where(PS.source_type == "fake")):
            src.enabled = False
        s.commit()
    out = catalog.execute_query(slug, qid)
    assert any("não implementado" in e for e in out["errors"])


def test_coverage_filter_com_mes(slug, fake_connector):
    """Cobertura 'AAAA-MM': registros com mês são comparados por (ano, mês);
    sem mês, só o ano decide (anos de fronteira são mantidos)."""
    from jatai.sources.base import RawRecord
    research.set_config(slug, {"coverage_start": "2020-06",
                               "coverage_end": "2021-03"})

    def r(title, year, month=None, doi=""):
        return RawRecord(title=title, year=year, month=month, doi=doi,
                         authors=[], keywords=[])

    fake_connector.results = {"q": [
        r("Antes", 2020, 5, "10.1/a"),     # fora: maio < junho
        r("Limite ini", 2020, 6, "10.1/b"),  # dentro
        r("Sem mes 2020", 2020, None, "10.1/c"),  # fronteira: mantém
        r("Limite fim", 2021, 3, "10.1/d"),   # dentro
        r("Depois", 2021, 4, "10.1/e"),    # fora: abril > março
        r("Fora ano", 2019, 12, "10.1/f"), # fora
    ]}
    qid = _query(slug, expr="q")
    out = catalog.execute_query(slug, qid)
    assert out["new"] == 3


def test_coverage_years(slug):
    from jatai.catalog.service import _coverage_years
    research.set_config(slug, {})
    assert _coverage_years(slug) == ""
    research.set_config(slug, {"coverage_start": "2020",
                               "coverage_end": "2024"})
    assert _coverage_years(slug) == "2020-2024"
    research.set_config(slug, {"coverage_start": "2020",
                               "coverage_end": "2020"})
    assert _coverage_years(slug) == "2020"
    research.set_config(slug, {"coverage_start": "2019-06",
                               "coverage_end": ""})
    assert _coverage_years(slug) == "2019-"
    research.set_config(slug, {"coverage_start": "",
                               "coverage_end": "2025-03"})
    assert _coverage_years(slug) == "-2025"


def test_update_source_config(slug):
    src = research.list_sources(slug)[0]
    research.update_source_config(slug, src.id, {"api_key": "sk-123"})
    src = next(s for s in research.list_sources(slug) if s.id == src.id)
    assert src.config["api_key"] == "sk-123"
    # string vazia remove a chave; demais chaves preservadas
    research.update_source_config(slug, src.id, {"api_key": "",
                                                 "extra": "x"})
    src = next(s for s in research.list_sources(slug) if s.id == src.id)
    assert "api_key" not in src.config and src.config["extra"] == "x"


def test_coverage_bound_formatos():
    from jatai.catalog.service import _coverage_bound
    assert _coverage_bound("2020", 1) == (2020, 1)
    assert _coverage_bound("2020", 12) == (2020, 12)
    assert _coverage_bound("2020-06", 1) == (2020, 6)
    assert _coverage_bound("2020-13", 1) is None
    assert _coverage_bound("abc", 1) is None
    assert _coverage_bound("", 1) is None


def test_on_page_ingest_sem_duplicar(slug, fake_connector):
    """FakeConnector chama on_page com todos os registros E retorna a lista;
    execute_query não pode ingerir duas vezes (sightings únicos)."""
    from jatai.storage.db import get_project_session
    from jatai.storage.models_project import Paper, PaperSighting
    fake_connector.results = {"alpha beta": [rec("Paper A", 2021, "10.1/a"),
                                             rec("Paper B", 2022, "10.1/b")]}
    qid = _query(slug)
    catalog.execute_query(slug, qid)
    with get_project_session(slug) as s:
        assert s.scalar(select(func.count(Paper.id))) == 2
        assert s.scalar(select(func.count(PaperSighting.id))) == 2


def test_partial_search_preserva_paginas_ingestadas(slug, monkeypatch):
    """Conector que falha após entregar páginas via on_page: o que já foi
    persistido é mantido e a execução registra o erro."""
    from jatai.sources.base import RawRecord, SourceConnector
    from jatai.storage.db import get_project_session
    from jatai.storage.models_project import Paper, QueryExecution

    class PartialThenFail(SourceConnector):
        source_type = "fake"
        name = "Fake"

        def is_available(self):
            return True

        def search(self, expression, max_results=100, on_progress=None,
                   on_retry=None, on_page=None, should_stop=None):
            batches = [[RawRecord(title="P1", year=2020, doi="10.9/1",
                                  authors=[], external_id="e1"),
                        RawRecord(title="P2", year=2020, doi="10.9/2",
                                  authors=[], external_id="e2")],
                       [RawRecord(title="P3", year=2020, doi="10.9/3",
                                  authors=[], external_id="e3")]]
            for i, b in enumerate(batches, 1):
                if on_page:
                    on_page(i, b)
            self.last_request = {"pages": 2}
            raise ConnectionError("caiu na página 3")

    monkeypatch.setattr("jatai.catalog.service.get_connector",
                        lambda stype, config=None: PartialThenFail())
    qid = _query(slug)
    out = catalog.execute_query(slug, qid)
    assert out["errors"]
    with get_project_session(slug) as s:
        # páginas entregues via on_page foram persistidas antes da falha
        assert s.scalar(select(func.count(Paper.id))) == 3
        ex = s.scalar(select(QueryExecution))
        assert ex.status == "erro"


def test_execute_query_injeta_ano_cobertura_todas_fontes(slug, monkeypatch):
    """coverage_start/end do projeto viram config['year'] para qualquer
    fonte (não só semantic_scholar)."""
    seen = {}
    real = get_connector

    def spy(stype, config=None):
        seen[stype] = dict(config or {})
        return real(stype, config)

    monkeypatch.setattr("jatai.catalog.service.get_connector", spy)
    research.set_config(slug, {"coverage_start": "2020", "coverage_end": "2024"})
    qid = _query(slug)
    catalog.execute_query(slug, qid)
    assert seen.get("fake", {}).get("year") == "2020-2024"


def test_execute_query_sem_cobertura_nao_injeta_ano(slug, monkeypatch):
    seen = {}
    real = get_connector

    def spy(stype, config=None):
        seen[stype] = dict(config or {})
        return real(stype, config)

    monkeypatch.setattr("jatai.catalog.service.get_connector", spy)
    qid = _query(slug)
    catalog.execute_query(slug, qid)
    assert "year" not in seen.get("fake", {})
