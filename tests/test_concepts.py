"""Extração de conceitos: dedup, frequência e prompt — sem LLM."""

from types import SimpleNamespace

from sqlalchemy import select

from lelia.agents import concepts
from lelia.storage.db import get_project_session
from lelia.storage.models_project import Concept, Paper, PaperAnalysis


def _mk_paper(slug, title="Artigo X", doi="10.1/c"):
    with get_project_session(slug) as s:
        p = Paper(title=title, doi=doi, url="", state="incluido",
                  authors="[]")
        s.add(p)
        s.commit()
        return p.id


def _result(*terms):
    return concepts.ConceptsResult(concepts=[
        concepts.ConceptItem(term=t, description=f"desc {t}",
                             related_terms=[f"{t}-syn"])
        for t in terms])


def test_persist_cria_conceitos_e_registra_analise(slug):
    pid = _mk_paper(slug)
    created = concepts.persist(slug, pid, _result("F1-score", "LoRA"),
                               iteration=1, model_ref="ollama:gemma")
    assert created == 2
    with get_project_session(slug) as s:
        rows = list(s.scalars(select(Concept).order_by(Concept.id)))
        assert [c.term for c in rows] == ["F1-score", "LoRA"]
        assert all(c.origin == "agente" and c.status == "proposto"
                   and c.frequency == 1 for c in rows)
        assert rows[0].related_terms == '["F1-score-syn"]'
        ana = s.scalar(select(PaperAnalysis).where(
            PaperAnalysis.paper_id == pid,
            PaperAnalysis.kind == "conceitos"))
        assert ana is not None and "F1-score" in ana.result
        assert '"model": "ollama:gemma"' in ana.result


def test_persist_dedup_case_insensitive_e_incrementa_frequencia(slug):
    pid = _mk_paper(slug)
    concepts.persist(slug, pid, _result("LoRA"), iteration=1)
    created = concepts.persist(slug, pid, _result("lora", "LoRA", "PEFT"),
                               iteration=2)
    assert created == 1  # só PEFT é novo; "lora"/"LoRA" deduplicam
    with get_project_session(slug) as s:
        lora = s.scalar(select(Concept).where(Concept.term == "LoRA"))
        peft = s.scalar(select(Concept).where(Concept.term == "PEFT"))
        assert lora.frequency == 2
        assert peft is not None and peft.frequency == 1


def test_persist_ignora_termos_vazios_e_duplicados_na_resposta(slug):
    pid = _mk_paper(slug)
    res = concepts.ConceptsResult(concepts=[
        concepts.ConceptItem(term="  "),
        concepts.ConceptItem(term="X"),
        concepts.ConceptItem(term="x"),
    ])
    assert concepts.persist(slug, pid, res, iteration=1) == 1


def test_persist_vincula_conceitos_ao_paper(slug):
    from lelia.catalog import service as catalog
    from lelia.storage.models_project import ConceptPaper

    pid = _mk_paper(slug)
    pid2 = _mk_paper(slug, "Outro", doi="10.1/d")
    concepts.persist(slug, pid, _result("LoRA", "PEFT"), iteration=1)
    # reprocessa o mesmo paper: vínculo não duplica
    concepts.persist(slug, pid, _result("LoRA"), iteration=2)
    # outro paper com termo já existente: vínculo novo, sem conceito novo
    assert concepts.persist(slug, pid2, _result("LoRA"), iteration=2) == 0

    pcs = catalog.paper_concepts(slug, pid)
    assert {pc["concept"].term for pc in pcs} == {"LoRA", "PEFT"}
    assert all(pc["origin"] == "agente" for pc in pcs)
    pcs2 = catalog.paper_concepts(slug, pid2)
    assert [pc["concept"].term for pc in pcs2] == ["LoRA"]
    with get_project_session(slug) as s:
        n = len(list(s.scalars(select(ConceptPaper))))
        assert n == 3  # LoRA×2 papers + PEFT×1


def test_paper_concepts_fallback_analises_antigas(slug):
    """Análises 'conceitos' gravadas antes do vínculo ainda listam os
    conceitos pelo termo."""
    import json as _json
    from lelia.catalog import service as catalog

    pid = _mk_paper(slug)
    with get_project_session(slug) as s:
        c = Concept(term="Antigo", description="", origin="agente",
                    frequency=1, status="proposto")
        s.add(c)
        s.flush()
        s.add(PaperAnalysis(
            paper_id=pid, kind="conceitos",
            result=_json.dumps({"terms": ["antigo"], "model": "x"})))
        s.commit()
    pcs = catalog.paper_concepts(slug, pid)
    assert [pc["concept"].term for pc in pcs] == ["Antigo"]


def test_save_paper_concept_inclui_edita_e_reutiliza(slug):
    from lelia.catalog import service as catalog

    pid = _mk_paper(slug)
    cid = catalog.save_paper_concept(
        slug, pid, "Fine-tuning", description="desc",
        related_terms="LoRA, PEFT", status="aceito")
    assert cid
    pcs = catalog.paper_concepts(slug, pid)
    assert len(pcs) == 1 and pcs[0]["origin"] == "manual"
    c = pcs[0]["concept"]
    assert c.description == "desc" and c.status == "aceito"
    assert pcs[0]["related"] == ["LoRA", "PEFT"]

    # termo repetido (case-insensitive) reutiliza o conceito, não duplica
    cid2 = catalog.save_paper_concept(slug, pid, "fine-tuning")
    assert cid2 == cid
    assert len(catalog.paper_concepts(slug, pid)) == 1

    # edição de conceito do agente via ficha manual
    with get_project_session(slug) as s:
        ag = Concept(term="RAG", description="", origin="agente",
                     frequency=1, status="proposto")
        s.add(ag)
        s.commit()
        agid = ag.id
    catalog.save_paper_concept(slug, pid, "RAG v2", description="nova",
                               concept_id=agid, status="rejeitado")
    c2 = catalog.get_concept(slug, agid)
    assert c2.term == "RAG v2" and c2.status == "rejeitado"
    assert catalog.get_concept(slug, 99999) is None


def test_build_prompt_renderiza_contexto_e_termos_existentes(slug):
    task = SimpleNamespace(prompt_template="")
    paper = SimpleNamespace(
        title="Título do artigo", abstract="Resumo aqui", year=2024,
        venue="Journal X", source="openalex", keywords="ia, llm",
        authors='["Ana", "Beto"]')
    prompt, name, ver = concepts.build_prompt(
        slug, task, paper, "TEXTO COMPLETO DO PDF", ["LoRA", "RAG"])
    assert name == "concept-extraction-default" and ver == "1"
    assert "Título do artigo" in prompt
    assert "TEXTO COMPLETO DO PDF" in prompt
    assert "- LoRA" in prompt and "- RAG" in prompt


def test_build_prompt_respeita_max_concepts(slug):
    task = SimpleNamespace(prompt_template="")
    paper = SimpleNamespace(
        title="T", abstract="", year=None, venue="", source="",
        keywords="", authors="[]")
    prompt, _, _ = concepts.build_prompt(
        slug, task, paper, "texto", [], max_concepts=3)
    assert "no máximo 3 conceitos" in prompt
    prompt2, _, _ = concepts.build_prompt(
        slug, task, paper, "texto", [], max_concepts=0)
    assert "tipicamente 3 a 10 conceitos" in prompt2


def test_build_prompt_orienta_por_areas(slug):
    task = SimpleNamespace(prompt_template="")
    paper = SimpleNamespace(
        title="T", abstract="", year=None, venue="", source="",
        keywords="", authors="[]")
    areas = [{"name": "LLMs em saúde", "keywords": ["LLM", "clinical NLP"]}]
    prompt, _, _ = concepts.build_prompt(
        slug, task, paper, "texto", [], areas=areas)
    assert "Áreas de pesquisa da revisão" in prompt
    assert "LLMs em saúde" in prompt
    assert "clinical NLP" in prompt
    prompt2, _, _ = concepts.build_prompt(slug, task, paper, "texto", [])
    assert "Áreas de pesquisa" not in prompt2


def test_save_concept_cria_e_edita(slug):
    from lelia.catalog import service as catalog

    cid = catalog.save_concept(
        slug, "Interpretação de modelos", "Métodos de XAI",
        related_terms="XAI, LIME, SHAP", status="aceito")
    assert cid
    with get_project_session(slug) as s:
        c = s.get(Concept, cid)
        assert c.term == "Interpretação de modelos"
        assert c.origin == "manual"
        assert c.status == "aceito"
        assert c.related_terms == '["XAI", "LIME", "SHAP"]'

    cid2 = catalog.save_concept(
        slug, "XAI", "Explainable AI", status="proposto",
        concept_id=cid)
    assert cid2 == cid
    with get_project_session(slug) as s:
        c = s.get(Concept, cid)
        assert c.term == "XAI"
        assert c.status == "proposto"
        assert c.description == "Explainable AI"


def test_save_concept_reutiliza_mesmo_termo(slug):
    from lelia.catalog import service as catalog

    cid1 = catalog.save_concept(slug, "LoRA", status="aceito")
    cid2 = catalog.save_concept(slug, "lora", "Low-Rank Adaptation")
    assert cid1 == cid2
    with get_project_session(slug) as s:
        assert len(list(s.scalars(select(Concept)))) == 1


def test_save_concept_sem_termo(slug):
    from lelia.catalog import service as catalog

    assert catalog.save_concept(slug, "  ") is None
    assert catalog.save_concept(slug, "X", concept_id=99999) is None


def test_delete_concept_remove_vinculos(slug):
    from lelia.catalog import service as catalog
    from lelia.storage.models_project import ConceptPaper

    pid = _mk_paper(slug)
    cid = catalog.save_paper_concept(slug, pid, "XAI", "desc")
    assert catalog.delete_concept(slug, cid)
    with get_project_session(slug) as s:
        assert s.get(Concept, cid) is None
        assert s.scalar(select(ConceptPaper).where(
            ConceptPaper.concept_id == cid)) is None
    assert catalog.delete_concept(slug, cid) is False
