"""Execução avulsa de tarefa de análise sobre uma única publicação."""

import json
from types import SimpleNamespace

from sqlalchemy import select

from jatai.agents import concepts, runner
from jatai.storage.db import get_project_session
from jatai.storage.models_project import Paper, PaperAnalysis


def _mk_paper(slug, title, state="incluido"):
    with get_project_session(slug) as s:
        p = Paper(title=title, doi="", url="", state=state, authors="[]")
        s.add(p)
        s.commit()
        return p.id


def _task(params):
    return SimpleNamespace(id=None, task_type="concept_extraction",
                           name="Extração", params=json.dumps(params),
                           max_tokens=None, prompt_template="",
                           agent_id=None)


def test_single_task_injeta_paper_id_e_modelo(slug):
    t = runner._single_task(slug, None,
                            {"task_type": "concept_extraction",
                             "paper_id": 7, "model_ref": "1:2"})
    p = json.loads(t.params)
    assert p["paper_id"] == 7 and p["model_ref"] == "1:2"
    assert t.task_type == "concept_extraction" and "#7" in t.name


def test_concept_extraction_avulsa_ignora_estado_e_marca(slug, monkeypatch):
    """paper_id processa só aquele artigo, mesmo excluído e já marcado."""
    pid_a = _mk_paper(slug, "Alvo", state="excluido")
    pid_b = _mk_paper(slug, "Outro", state="incluido")
    # A já tem análise 'conceitos' — modo avulso deve reprocessar mesmo assim
    with get_project_session(slug) as s:
        s.add(PaperAnalysis(paper_id=pid_a, kind="conceitos",
                            result='{"terms": []}'))
        s.commit()

    fake_result = concepts.ConceptsResult(concepts=[
        concepts.ConceptItem(term="conceito-x")])
    monkeypatch.setattr(
        "jatai.agents.llm.chat_structured",
        lambda s, **kw: fake_result)

    stats = {"errors": []}
    runner._concept_extraction(slug, runner.create_run(slug), 1,
                               _task({"paper_id": pid_a}), stats)

    assert stats.get("concepts_papers") == 1
    assert stats.get("new_terms") == 1
    with get_project_session(slug) as s:
        kinds_a = [a.kind for a in s.scalars(
            select(PaperAnalysis).where(PaperAnalysis.paper_id == pid_a))]
        kinds_b = [a.kind for a in s.scalars(
            select(PaperAnalysis).where(PaperAnalysis.paper_id == pid_b))]
        assert kinds_a.count("conceitos") == 2  # antiga + nova
        assert "conceitos" not in kinds_b       # outro artigo intacto


def test_paper_analyses_expoe_model_name(slug):
    from jatai.catalog import service as catalog

    pid = _mk_paper(slug, "Com modelo")
    with get_project_session(slug) as s:
        s.add(PaperAnalysis(
            paper_id=pid, kind="profunda",
            result=json.dumps({"summary": "x", "model": "google/gemini-3.5"})))
        s.commit()
    rows = catalog.paper_analyses(slug, pid)
    assert rows[0].model_name == "google/gemini-3.5"


def test_run_label_single_usa_tarefa_e_paper(slug):
    """Monitor/histórico: run avulsa exibe 'Tarefa · #paperId'."""
    pid = _mk_paper(slug, "Rotulo")
    rid = runner.create_analysis_run(slug, pid, "classification")
    run = runner.get_run(slug, rid)
    label = runner._run_label(slug, run)
    assert "#" + str(pid) in label
    assert "Classifica" in label or "classifica" in label.lower()
    # run normal continua com nome do agente (ou vazio sem agente)
    rid2 = runner.create_run(slug)
    run2 = runner.get_run(slug, rid2)
    assert runner._run_label(slug, run2) == runner._agent_name(
        slug, run2.agent_id)


def test_analysis_run_snapshot_e_reexecucao(slug):
    pid = _mk_paper(slug, "Semente")
    rid = runner.create_analysis_run(slug, pid, "snowballing",
                                     model_ref="1:2")
    run = runner.get_run(slug, rid)
    snap = json.loads(run.config_snapshot)
    assert snap["single"]["task_type"] == "snowballing"
    assert snap["single"]["paper_id"] == pid
    assert snap["max_iterations"] == 1
    # reexecutar preserva o modo avulso
    assert runner.control_run(slug, rid, "reexecutar") == "ok"
