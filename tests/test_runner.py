"""Testes do ciclo de execução _execute_run: limites, gating de custo e convergência."""

import json

import pytest
from sqlalchemy import func, select

from lelia.agents import config_service, runner
from lelia.research import service as research
from lelia.storage.db import get_project_session
from lelia.storage.models_project import Iteration, Run
from tests.fake_source import FakeConnector, rec


@pytest.fixture(autouse=True)
def fake_connector(monkeypatch):
    FakeConnector.reset()
    monkeypatch.setattr("lelia.catalog.service.get_connector",
                        lambda stype, config=None: FakeConnector() if stype == "fake" else None)
    return FakeConnector


def _mk_agent_with_tasks(slug, task_types):
    aid = config_service.create_agent(slug, "Agente teste")
    for tt in task_types:
        config_service.add_agent_task(slug, aid, tt)
    return aid


def _run(slug, agent_id, **snap):
    cfg = {"max_iterations": 1, "max_papers": 0, "max_cost_usd": None, **snap}
    with get_project_session(slug) as s:
        r = Run(status="pendente", agent_id=agent_id,
                config_snapshot=json.dumps(cfg))
        s.add(r)
        s.commit()
        return r.id


def _iters(slug, run_id):
    with get_project_session(slug) as s:
        return list(s.scalars(select(Iteration).where(Iteration.run_id == run_id)))


def test_zero_cost_limit_still_runs_free_tasks(slug, fake_connector):
    """Regressão: max_cost_usd=0.0 não pode impedir tarefas sem LLM (search)."""
    fake_connector.results = {"q1": [rec("P1")]}
    research.create_query(slug, "q1", "fake", status="pendente")
    aid = _mk_agent_with_tasks(slug, ["search"])
    rid = _run(slug, aid, max_cost_usd=0.0)
    runner._execute_run(slug, rid)
    iters = _iters(slug, rid)
    assert len(iters) == 1
    assert fake_connector.calls == ["q1"]
    assert iters[0].new_papers == 1


def test_cost_limit_skips_llm_tasks(slug, fake_connector, monkeypatch):
    """Com custo >= limite, tarefas uses_llm são puladas; livres executam."""
    monkeypatch.setattr("lelia.agents.llm.llm_usage",
                        lambda s: {"cost_usd": 5.0})
    fake_connector.results = {"q1": [rec("P1")]}
    research.create_query(slug, "q1", "fake", status="pendente")
    aid = _mk_agent_with_tasks(slug, ["search", "screener"])
    rid = _run(slug, aid, max_cost_usd=1.0)
    runner._execute_run(slug, rid)
    iters = _iters(slug, rid)
    assert len(iters) == 1 and iters[0].new_papers == 1  # search rodou


def test_convergence_stops_after_first_iteration(slug, fake_connector):
    """0 artigos novos -> converge e não itera de novo (max_iterations=3)."""
    aid = _mk_agent_with_tasks(slug, ["search"])
    rid = _run(slug, aid, max_iterations=3)
    runner._execute_run(slug, rid)
    assert len(_iters(slug, rid)) == 1


def test_run_cancelled_marks_status(slug, fake_connector):
    aid = _mk_agent_with_tasks(slug, ["search"])
    rid = _run(slug, aid)
    runner.set_run_status(slug, rid, "cancelada")
    runner._execute_run(slug, rid)
    with get_project_session(slug) as s:
        assert s.get(Run, rid).status == "cancelada"
    assert _iters(slug, rid) == []


def test_resume_paused_run_does_not_spawn_thread(slug, monkeypatch):
    """Retomar run pausada só muda o status — a thread original continua."""
    aid = config_service.create_agent(slug, "Agente")
    rid = _run(slug, aid)
    runner.set_run_status(slug, rid, "pausado")
    spawned = []
    monkeypatch.setattr(runner.threading, "Thread",
                        lambda *a, **k: spawned.append(1) or type("T", (), {"start": lambda s: None})())
    assert runner.control_run(slug, rid, "iniciar") == "ok"
    assert spawned == []
    with get_project_session(slug) as s:
        assert s.get(Run, rid).status == "executando"


def test_control_cancel_paused_run(slug):
    aid = config_service.create_agent(slug, "Agente")
    rid = _run(slug, aid)
    runner.set_run_status(slug, rid, "pausado")
    assert runner.control_run(slug, rid, "cancelar") == "ok"
    with get_project_session(slug) as s:
        assert s.get(Run, rid).status == "cancelada"
