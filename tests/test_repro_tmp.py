import json, time
import pytest
from sqlalchemy import select
from jatai.agents import runner
from jatai.storage.db import get_project_session
from jatai.storage.models_project import Paper, PaperAnalysis

def _mk(slug, **kw):
    base = dict(title="T", doi="", url="", state="incluido", authors="[]")
    base.update(kw)
    with get_project_session(slug) as s:
        p = Paper(**base); s.add(p); s.commit(); return p.id

def test_execute_run_deep(slug, monkeypatch):
    from jatai.agents import deep
    from jatai.research import service as research
    pid = _mk(slug)
    research.save_question(slug, None, code="RQ1", question="Q?", answer_type="texto", required=True, position=1)
    fake = deep.DeepResult(summary="s", answers=[], confidence=0.9)
    monkeypatch.setattr("jatai.agents.llm.chat_structured", lambda s, **kw: fake)
    monkeypatch.setattr("jatai.agents.runner._paper_full_text", lambda *a, **k: "texto")
    rid = runner.create_analysis_run(slug, pid, "deep_analysis")
    runner.control_run(slug, rid, "iniciar")
    for _ in range(60):
        r = runner.get_run(slug, rid)
        if r and r.status in ("concluida","falha","cancelada"): break
        time.sleep(0.2)
    print("STATUS:", runner.get_run(slug, rid).status)
    for ev in runner.run_events(slug, rid):
        print(ev.get("type"), "|", str(ev.get("error") or ev.get("message") or "")[:120])

def test_execute_run_classif(slug, monkeypatch):
    from jatai.agents import classify
    from jatai.research import service as research
    pid = _mk(slug)
    research.save_catalog_field(slug, None, identifier="C1", label="L", data_type="texto", active=True)
    RM = classify.result_model(research.list_catalog_fields(slug))
    inst = RM(values={}, confidence=0.9)
    monkeypatch.setattr("jatai.agents.llm.chat_structured", lambda s, **kw: inst)
    monkeypatch.setattr("jatai.agents.runner._paper_full_text", lambda *a, **k: "texto")
    rid = runner.create_analysis_run(slug, pid, "classification")
    runner.control_run(slug, rid, "iniciar")
    for _ in range(60):
        r = runner.get_run(slug, rid)
        if r and r.status in ("concluida","falha","cancelada"): break
        time.sleep(0.2)
    print("STATUS:", runner.get_run(slug, rid).status)
    for ev in runner.run_events(slug, rid):
        print(ev.get("type"), "|", str(ev.get("error") or ev.get("message") or "")[:120])
