"""Matriz fonte × fase da revisão (dashboard PRISMA) — review_flow."""

import json as _json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from lelia.catalog import service as catalog
from lelia.storage.db import get_project_session
from lelia.storage.models_project import (
    Criterion, Paper, PaperAnalysis, PaperSighting, ProjectSource)


def _mk(slug, title, state="novo", source_sightings=(), triagem=None,
        deep=None, created_at=None):
    """Cria paper + sightings + análises de triagem."""
    with get_project_session(slug) as s:
        p = Paper(title=title, doi="", url="", state=state, authors="[]",
                  source=source_sightings[0] if source_sightings else "")
        s.add(p)
        s.flush()
        pid = p.id
        for i, src in enumerate(source_sightings):
            s.add(PaperSighting(
                paper_id=pid, source=src, external_id=f"{src}-{pid}-{i}",
                raw="{}"))
        base = created_at or datetime(2026, 1, 1, tzinfo=timezone.utc)
        for kind, payload, offset in (
                ("triagem", triagem, 0), ("triagem_profunda", deep, 1)):
            if payload is not None:
                s.add(PaperAnalysis(
                    paper_id=pid, kind=kind,
                    result=_json.dumps(payload),
                    created_at=base + timedelta(minutes=offset)))
        s.commit()
        return pid


def _payload(verdict, evals):
    return {"applied_verdict": verdict,
            "evaluations": [
                {"criterion_id": c, "met": True, "verdict": v,
                 "justification": "", "evidence_quote": ""}
                for c, v in evals]}


def _mk_criteria(slug):
    with get_project_session(slug) as s:
        for code, typ, pos, stage in (
                ("IC1", "inclusao", 1, "titulo_resumo"),
                ("IC2", "inclusao", 2, "titulo_resumo"),
                ("EC1", "exclusao", 3, "titulo_resumo"),
                ("EC9", "exclusao", 4, "texto_completo")):
            s.add(Criterion(code=code, type=typ, description=f"desc {code}",
                            applies_to=stage, required=True, position=pos))
        s.commit()


def _row(flow, key):
    return next(r for r in flow["rows"] if r["key"] == key)


def _cell(row, level_key, col_key):
    return row["cells"].get(f"{level_key}|{col_key}", 0)


def test_review_flow_linhas_por_fonte_e_total(slug):
    _mk_criteria(slug)
    with get_project_session(slug) as s:
        s.add(ProjectSource(source_type="dblp", name="DBLP", enabled=True,
                            config={}))
        s.commit()
    # p1 em 2 fontes; p2 fake; p3 dblp; p4 sem fonte; p5 dblp; p6 2 fontes
    _mk(slug, "p1", "excluido", ("fake", "dblp"))
    _mk(slug, "p2", "incluido", ("fake",))
    _mk(slug, "p3", "novo", ("dblp",))
    _mk(slug, "p4", "incluido")
    _mk(slug, "p5", "excluido", ("dblp",))
    _mk(slug, "p6", "excluido", ("fake", "dblp"))

    flow = catalog.review_flow(slug)
    labels = [r["key"] for r in flow["rows"]]
    # habilitadas primeiro (fake do conftest + dblp), "outros" por último
    assert labels[0] == "fake" and "dblp" in labels
    assert labels[-1] == "outros"

    fake = _row(flow, "fake")
    dblp = _row(flow, "dblp")
    outros = _row(flow, "outros")
    # p1 e p6 contam nas duas fontes
    assert fake["total"] == 3   # p1, p2, p6
    assert dblp["total"] == 4   # p1, p3, p5, p6
    assert outros["total"] == 1  # p4
    assert fake["included"] == 1  # p2
    assert dblp["included"] == 0
    assert outros["included"] == 1  # p4

    tr = flow["total_row"]
    assert tr["total"] == 8  # 3+4+1 (p1 e p6 contam 2x)
    assert tr["included"] == 2
    assert flow["papers_total"] == 6
    assert flow["included_total"] == 2


def test_review_flow_niveis_e_criterios_por_fonte(slug):
    _mk_criteria(slug)
    # nível 1: p1 excluído por EC1; p6 por EC1+IC2; ambos da fonte fake
    _mk(slug, "p1", "excluido", ("fake",),
        triagem=_payload("excluir", [("EC1", "excluir"), ("IC1", "incluir")]))
    _mk(slug, "p2", "incluido", ("fake",),
        triagem=_payload("incluir", [("EC1", "incluir"), ("IC1", "incluir")]))
    _mk(slug, "p5", "talvez", ("dblp",),
        triagem=_payload("talvez", [("EC1", "incluir"), ("IC1", "inconclusivo")]))
    _mk(slug, "p6", "excluido", ("fake", "dblp"),
        triagem=_payload("excluir", [("IC2", "excluir"), ("EC1", "excluir")]))
    # nível 2 (deep): p2b incluído, p5b excluído por EC9 — deep usa outro kind
    _mk(slug, "p2b", "incluido", ("dblp",),
        deep=_payload("incluir", [("EC9", "incluir")]))
    _mk(slug, "p5b", "excluido", ("dblp",),
        deep=_payload("excluir", [("EC9", "excluir")]))

    flow = catalog.review_flow(slug)
    levels = flow["levels"]
    assert [lv["key"] for lv in levels] == ["level|triagem",
                                            "level|triagem_profunda"]
    assert "1º nível" in levels[0]["label"]
    assert "2º nível" in levels[1]["label"]

    lvl1, lvl2 = levels[0], levels[1]
    # nível 1: só critérios de título/resumo; nível 2 (deep) reavalia todos
    assert [c["key"] for c in lvl1["cols"]] == ["IC1", "IC2", "EC1"]
    assert [c["key"] for c in lvl2["cols"]] == ["IC1", "IC2", "EC1", "EC9"]

    fake = _row(flow, "fake")
    dblp = _row(flow, "dblp")
    # nível 1: fake tem p1+p6 (p6 conta nos 2 critérios)
    assert _cell(fake, "level|triagem", "EC1") == 2
    assert _cell(fake, "level|triagem", "IC2") == 1
    assert _cell(fake, "level|triagem", "IC1") == 0
    # p6 está em fake e dblp -> conta nas duas linhas
    assert _cell(dblp, "level|triagem", "EC1") == 1
    assert _cell(dblp, "level|triagem", "IC2") == 1
    # nível 2: p5b excluído por EC9, origem dblp
    assert _cell(dblp, "level|triagem_profunda", "EC9") == 1
    assert _cell(fake, "level|triagem_profunda", "EC9") == 0

    tr = flow["total_row"]
    assert _cell(tr, "level|triagem", "EC1") == 3
    assert _cell(tr, "level|triagem_profunda", "EC9") == 1


def test_review_flow_exclusao_sem_criterio_vira_outros(slug):
    _mk(slug, "px", "excluido", ("fake",),
        triagem={"applied_verdict": "excluir",
                 "evaluations": [{"criterion_id": "EC1", "met": False,
                                  "verdict": "incluir"}]})
    flow = catalog.review_flow(slug)
    lv = flow["levels"][0]
    assert any(c["key"] == "_outros" for c in lv["cols"])
    fake = _row(flow, "fake")
    assert _cell(fake, "level|triagem", "_outros") == 1
    assert _cell(fake, "level|triagem", "EC1") == 0


def test_review_flow_niveis_mesmo_sem_analises(slug):
    """Tarefas de triagem habilitadas no pipeline geram os níveis mesmo
    antes da primeira execução (contagens zeradas)."""
    _mk_criteria(slug)
    _mk(slug, "p1", "novo", ("fake",))
    with get_project_session(slug) as s:
        from lelia.storage.models_project import Agent, AgentTaskConfig
        agent = s.scalar(select(Agent))
        if agent is None:
            agent = Agent(name="Agente", enabled=True, is_default=True)
            s.add(agent)
            s.flush()
        for i, ttype in enumerate(("screener", "deep_screener")):
            s.add(AgentTaskConfig(task_type=ttype, agent_id=agent.id,
                                  enabled=True, position=i))
        s.commit()

    flow = catalog.review_flow(slug)
    levels = flow["levels"]
    assert [lv["key"] for lv in levels] == ["level|triagem",
                                            "level|triagem_profunda"]
    assert "1º nível" in levels[0]["label"]
    assert "2º nível" in levels[1]["label"]
    fake = _row(flow, "fake")
    assert _cell(fake, "level|triagem", "EC1") == 0
    assert fake["total"] == 1


def test_review_flow_duplicados_por_proveniencia(slug):
    """Duplicados = artigos da fonte que já foram encontrados antes
    (ordem dos sightings); cada artigo conta só na 1ª fonte."""
    _mk(slug, "p1", "novo", ("fake", "dblp"))   # 1ª fonte: fake
    _mk(slug, "p2", "excluido", ("dblp", "fake"))  # 1ª fonte: dblp
    _mk(slug, "p3", "incluido", ("fake",))
    _mk(slug, "p4", "talvez", ("dblp",))

    flow = catalog.review_flow(slug)
    fake = _row(flow, "fake")
    dblp = _row(flow, "dblp")
    assert fake["total"] == 3 and fake["duplicates"] == 1  # p2 é dup aqui
    assert dblp["total"] == 3 and dblp["duplicates"] == 1  # p1 é dup aqui
    assert flow["total_row"]["total"] == 6
    assert flow["total_row"]["duplicates"] == 2
    # "em triagem"/incluídos contam só na 1ª fonte (sem duplicados):
    # fake: p1(novo) -> 1 ; dblp: p4(talvez) -> 1 (p1 é dup, não conta)
    assert fake["pending"] == 1 and dblp["pending"] == 1
    assert fake["included"] == 1 and dblp["included"] == 0
    assert flow["total_row"]["pending"] == 2
    assert flow["total_row"]["included"] == 1


def test_review_flow_vazio(slug):
    flow = catalog.review_flow(slug)
    assert flow["levels"] == []
    # fonte 'fake' do conftest aparece habilitada mesmo sem artigos
    assert [r["key"] for r in flow["rows"]] == ["fake"]
    assert flow["rows"][0]["total"] == 0
    assert flow["total_row"]["total"] == 0
    assert flow["papers_total"] == 0
