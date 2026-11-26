"""Regras determinísticas da triagem (screener) — sem LLM."""

from types import SimpleNamespace

from jatai.agents import screening


def _criteria():
    return [
        SimpleNamespace(code="EC1", type="exclusao", required=True,
                        applies_to="titulo_resumo", description="dado sensível"),
        SimpleNamespace(code="IC1", type="inclusao", required=True,
                        applies_to="titulo_resumo", description="estudo empírico"),
        SimpleNamespace(code="IC2", type="inclusao", required=False,
                        applies_to="titulo_resumo", description="open access"),
        SimpleNamespace(code="EC9", type="exclusao", required=True,
                        applies_to="texto_completo", description="fora do escopo"),
    ]


def _result(evals, overall="incluir", confidence=0.9):
    return screening.ScreeningResult(
        evaluations=[screening.CriterionEval(**e) for e in evals],
        overall_verdict=overall, confidence=confidence)


def test_screening_criteria_filters_applies_to():
    codes = [c.code for c in screening.screening_criteria(_criteria())]
    assert codes == ["EC1", "IC1", "IC2"]


def test_ec_obrigatorio_atendido_forca_excluir():
    r = _result([
        {"criterion_id": "EC1", "met": True, "verdict": "excluir"},
        {"criterion_id": "IC1", "met": True, "verdict": "incluir"},
    ])
    verdict, reasons = screening.apply_rules(r, _criteria(), 0.6)
    assert verdict == "excluir"
    assert any("EC1" in x for x in reasons)


def test_ic_obrigatorio_violado_forca_excluir():
    r = _result([
        {"criterion_id": "EC1", "met": False, "verdict": "incluir"},
        {"criterion_id": "IC1", "met": False, "verdict": "excluir"},
    ])
    verdict, _ = screening.apply_rules(r, _criteria(), 0.6)
    assert verdict == "excluir"


def test_criterio_obrigatorio_inconclusivo_rebaixa_para_talvez():
    r = _result([
        {"criterion_id": "EC1", "met": None, "verdict": "inconclusivo"},
        {"criterion_id": "IC1", "met": True, "verdict": "incluir"},
    ])
    verdict, reasons = screening.apply_rules(r, _criteria(), 0.6)
    assert verdict == "talvez"
    assert any("inconclusivo" in x for x in reasons)


def test_criterio_obrigatorio_ausente_rebaixa_para_talvez():
    r = _result([
        {"criterion_id": "EC1", "met": False, "verdict": "incluir"},
        # IC1 não avaliado pelo modelo
    ])
    verdict, _ = screening.apply_rules(r, _criteria(), 0.6)
    assert verdict == "talvez"


def test_confidence_baixa_rebaixa_para_talvez():
    r = _result([
        {"criterion_id": "EC1", "met": False, "verdict": "incluir"},
        {"criterion_id": "IC1", "met": True, "verdict": "incluir"},
    ], confidence=0.3)
    verdict, reasons = screening.apply_rules(r, _criteria(), 0.6)
    assert verdict == "talvez"
    assert any("confidence" in x for x in reasons)


def test_tudo_ok_mantem_incluir():
    r = _result([
        {"criterion_id": "EC1", "met": False, "verdict": "incluir"},
        {"criterion_id": "IC1", "met": True, "verdict": "incluir"},
        {"criterion_id": "IC2", "met": None, "verdict": "inconclusivo"},
    ])
    verdict, reasons = screening.apply_rules(r, _criteria(), 0.6)
    assert verdict == "incluir"
    assert reasons == []


def test_excluir_do_llm_permanece_excluir():
    r = _result([
        {"criterion_id": "EC1", "met": False, "verdict": "incluir"},
        {"criterion_id": "IC1", "met": True, "verdict": "incluir"},
    ], overall="excluir", confidence=0.4)
    verdict, _ = screening.apply_rules(r, _criteria(), 0.6)
    assert verdict == "excluir"
