"""Testes da tarefa search do runner: seleção de consultas e stats."""

import json

import pytest
from sqlalchemy import select

from jatai.agents import runner
from jatai.research import service as research
from jatai.storage.db import get_project_session
from jatai.storage.models_project import AgentTaskConfig, Query
from tests.fake_source import FakeConnector, rec


@pytest.fixture(autouse=True)
def fake_connector(monkeypatch):
    FakeConnector.reset()
    monkeypatch.setattr("jatai.catalog.service.get_connector",
                        lambda stype, config=None: FakeConnector() if stype == "fake" else None)
    return FakeConnector


def _mk_run(slug, **snap):
    cfg = {"max_iterations": 1, "max_papers": 0, "max_cost_usd": None, **snap}
    with get_project_session(slug) as s:
        from jatai.storage.models_project import Run
        r = Run(status="executando", agent_id=None,
                config_snapshot=json.dumps(cfg))
        s.add(r)
        s.commit()
        return r.id


def _mk_task(**params):
    t = AgentTaskConfig(task_type="search", name="Busca", enabled=True,
                        params=json.dumps(params))
    return t


def _mk_query(slug, expr, source="fake", status="pendente", enabled=True):
    qid = research.create_query(slug, expr, source, status=status)
    research.set_query_enabled(slug, qid, enabled)
    return qid


def test_search_executes_all_enabled_queries(slug, fake_connector):
    """Toda consulta habilitada executa, independente do status."""
    fake_connector.results = {e: [rec(f"P-{e}")] for e in
                              ("q1", "q2", "q3", "q4")}
    _mk_query(slug, "q1", status="pendente")
    _mk_query(slug, "q2", status="aprovada")
    _mk_query(slug, "q3", status="executada")   # re-executa mesmo assim
    _mk_query(slug, "q4", status="pendente", enabled=False)  # desativada: pula
    run_id = _mk_run(slug)
    stats = {"queries": 0, "new_papers": 0, "new_terms": 0, "errors": []}
    runner._search(slug, run_id, 1, _mk_task(), stats)
    assert sorted(fake_connector.calls) == ["q1", "q2", "q3"]
    assert stats["queries"] == 3 and stats["new_papers"] == 3


def test_search_retries_erro_status(slug, fake_connector):
    fake_connector.results = {"qx": [rec("P")]}
    _mk_query(slug, "qx", status="erro")
    run_id = _mk_run(slug)
    stats = {"queries": 0, "new_papers": 0, "new_terms": 0, "errors": []}
    runner._search(slug, run_id, 1, _mk_task(), stats)
    assert fake_connector.calls == ["qx"]


def test_search_respects_max_papers_cap(slug, fake_connector):
    fake_connector.results = {"qm": [rec(f"P{i}") for i in range(10)]}
    _mk_query(slug, "qm")
    run_id = _mk_run(slug, max_papers=3)
    stats = {"queries": 0, "new_papers": 0, "new_terms": 0, "errors": []}
    runner._search(slug, run_id, 1, _mk_task(), stats)
    # FakeConnector já fatia por max_results; verifica via ingest
    from jatai.catalog.service import paper_count
    assert paper_count(slug) == 3


def test_search_accumulates_duplicates(slug, fake_connector):
    fake_connector.results = {"qd": [rec("Dup", 2020, "10.9/dup")]}
    _mk_query(slug, "qd")
    run_id = _mk_run(slug)
    stats = {"queries": 0, "new_papers": 0, "new_terms": 0, "errors": []}
    runner._search(slug, run_id, 1, _mk_task(), stats)
    # segunda execução da mesma consulta (status volta a pendente via update)
    with get_project_session(slug) as s:
        q = s.scalar(select(Query).where(Query.expression == "qd"))
        q.status = "pendente"
        s.commit()
    stats2 = {"queries": 0, "new_papers": 0, "new_terms": 0, "errors": []}
    runner._search(slug, run_id, 2, _mk_task(), stats2)
    assert stats2["new_papers"] == 0
    assert stats2.get("duplicates", 0) == 1


def test_search_errors_collected_not_raised(slug, fake_connector):
    fake_connector.error = TimeoutError("timeout dbLP")
    _mk_query(slug, "qe")
    run_id = _mk_run(slug)
    stats = {"queries": 0, "new_papers": 0, "new_terms": 0, "errors": []}
    runner._search(slug, run_id, 1, _mk_task(), stats)
    assert stats["queries"] == 1 and stats["errors"]


def test_queries_listadas_na_ordem_de_execucao(slug):
    q1 = _mk_query(slug, "qa")
    q2 = _mk_query(slug, "qb")
    q3 = _mk_query(slug, "qc")
    assert [q.id for q in research.list_queries(slug)] == [q1, q2, q3]
    # move qb para o topo -> passa a executar primeiro
    assert research.move_query(slug, q2, "up")
    assert [q.id for q in research.list_queries(slug)] == [q2, q1, q3]
    assert research.move_query(slug, q2, "up") is False  # já no topo
    assert research.move_query(slug, q3, "up")
    assert research.move_query(slug, q3, "up")
    assert research.move_query(slug, q3, "up") is False  # fundo
    assert [q.id for q in research.list_queries(slug)] == [q3, q2, q1]
    assert research.move_query(slug, 9999, "up") is False  # inexistente


def test_search_respeita_ordem_definida_pelo_usuario(slug, fake_connector):
    fake_connector.results = {e: [rec(f"P-{e}")] for e in ("qa", "qb", "qc")}
    qa = _mk_query(slug, "qa")
    qb = _mk_query(slug, "qb")
    qc = _mk_query(slug, "qc")
    research.move_query(slug, qc, "up")
    research.move_query(slug, qc, "up")
    run_id = _mk_run(slug)
    stats = {"queries": 0, "new_papers": 0, "new_terms": 0, "errors": []}
    runner._search(slug, run_id, 1, _mk_task(), stats)
    # qc subiu duas posições: qc, qa? não — qc estava em 3º, up->2º, up->1º
    assert fake_connector.calls == ["qc", "qa", "qb"]
