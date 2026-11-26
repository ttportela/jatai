"""Triagem manual (sem LLM): criação, edição e reflexos em estado/decisões."""

import json as _json

from sqlalchemy import select

from jatai.catalog import service as catalog
from jatai.storage.db import get_project_session
from jatai.storage.models_project import (
    Criterion, Decision, Evidence, Paper, PaperAnalysis)


def _mk_paper(slug, state="novo"):
    with get_project_session(slug) as s:
        p = Paper(title="Artigo", doi="", url="", state=state, authors="[]")
        s.add(p)
        s.commit()
        return p.id


def _mk_criteria(slug):
    with get_project_session(slug) as s:
        s.add(Criterion(code="IC1", type="inclusao", description="empírico",
                        applies_to="titulo_resumo", required=True, position=1))
        s.add(Criterion(code="EC1", type="exclusao", description="sensível",
                        applies_to="titulo_resumo", required=True, position=2))
        s.commit()


def _state(slug, pid):
    with get_project_session(slug) as s:
        return s.get(Paper, pid).state


def test_add_manual_screening_cria_analise_estado_e_decisoes(slug):
    _mk_criteria(slug)
    pid = _mk_paper(slug)
    aid = catalog.save_manual_screening(
        slug, pid, kind="triagem", stage="titulo_resumo", verdict="excluir",
        evals=[
            {"code": "IC1", "met": True, "verdict": "incluir",
             "justification": "estudo empírico", "evidence": "survey de 500"},
            {"code": "EC1", "met": True, "verdict": "excluir",
             "justification": "dados sensíveis", "evidence": "prontuários"},
        ])
    assert aid
    assert _state(slug, pid) == "excluido"

    with get_project_session(slug) as s:
        a = s.get(PaperAnalysis, aid)
        data = _json.loads(a.result)
        assert a.kind == "triagem" and a.paper_id == pid
        assert data["applied_verdict"] == "excluir"
        assert data["stage"] == "titulo_resumo"
        assert data["origin"] == "manual" and data["model"] == "manual"
        assert len(data["evaluations"]) == 2

        decs = list(s.scalars(select(Decision).where(
            Decision.paper_id == pid).order_by(Decision.id)))
        # 2 por critério + 1 geral
        assert len(decs) == 3
        assert all(d.origin == "human" for d in decs)
        by_code = {d.criterion_code: d.verdict for d in decs}
        # IC1 met=True -> incluir; EC1 met=True -> excluir
        assert by_code["IC1"] == "incluir"
        assert by_code["EC1"] == "excluir"
        assert by_code[""] == "excluir"

        ev = list(s.scalars(select(Evidence).where(Evidence.paper_id == pid)))
        assert len(ev) == 2 and all(e.origin == "manual" for e in ev)


def test_edit_manual_screening_atualiza_analise(slug):
    _mk_criteria(slug)
    pid = _mk_paper(slug)
    aid = catalog.save_manual_screening(
        slug, pid, verdict="incluir",
        evals=[{"code": "IC1", "met": True, "verdict": "incluir"}])
    assert _state(slug, pid) == "incluido"

    # edição: muda veredito geral e critério
    aid2 = catalog.save_manual_screening(
        slug, pid, verdict="talvez", analysis_id=aid,
        evals=[{"code": "IC1", "met": None, "verdict": "inconclusivo",
                "justification": "sem resumo", "evidence": ""}])
    assert aid2 == aid
    assert _state(slug, pid) == "talvez"
    with get_project_session(slug) as s:
        assert s.scalar(select(PaperAnalysis).where(
            PaperAnalysis.id == aid)).result is not None
        data = _json.loads(s.get(PaperAnalysis, aid).result)
        assert data["applied_verdict"] == "talvez"
        assert data["evaluations"][0]["verdict"] == "inconclusivo"
        # decisões humanas se acumulam (histórico)
        decs = list(s.scalars(select(Decision).where(
            Decision.paper_id == pid, Decision.criterion_code == "IC1")))
        assert len(decs) == 2


def test_save_manual_screening_validacoes(slug):
    pid = _mk_paper(slug)
    # veredito inválido
    assert catalog.save_manual_screening(slug, pid, verdict="xyz") is None
    # kind inválido
    assert catalog.save_manual_screening(slug, pid, kind="profunda") is None
    # paper inexistente
    assert catalog.save_manual_screening(slug, 99999, verdict="incluir") is None
    # analysis_id de outra triagem/paper
    aid = catalog.save_manual_screening(slug, pid, verdict="incluir")
    pid2 = _mk_paper(slug)
    assert catalog.save_manual_screening(
        slug, pid2, verdict="incluir", analysis_id=aid) is None


def test_manual_screening_alimenta_review_flow(slug):
    """Triagem manual conta no quadro fonte × fase do dashboard."""
    _mk_criteria(slug)
    from jatai.storage.models_project import PaperSighting
    pid = _mk_paper(slug)
    with get_project_session(slug) as s:
        s.add(PaperSighting(paper_id=pid, source="fake", external_id="x",
                            raw="{}"))
        s.commit()
    catalog.save_manual_screening(
        slug, pid, verdict="excluir",
        evals=[{"code": "EC1", "met": True, "verdict": "excluir",
                "justification": "", "evidence": ""}])
    flow = catalog.review_flow(slug)
    fake = next(r for r in flow["rows"] if r["key"] == "fake")
    # a análise manual gera o nível e conta a exclusão por EC1
    assert fake["cells"].get("level|triagem|EC1") == 1
    assert fake["included"] == 0
