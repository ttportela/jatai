"""Testes da tela/serviço de Propostas e da task `proposals`."""

import json

import pytest
from sqlalchemy import select

from jatai import proposals as prop
from jatai.agents import proposals as gen
from jatai.storage.db import get_project_session
from jatai.storage.models_project import (
    Paper, Proposal, ResearchQuestion)


def _mk_paper(slug, title, year=2024, state="incluido", **kw):
    with get_project_session(slug) as s:
        p = Paper(title=title, year=year, state=state,
                  venue=kw.get("venue", "Journal X"),
                  abstract=kw.get("abstract", "resumo"))
        s.add(p)
        s.commit()
        return p.id


def test_proposal_crud_e_filtros(slug):
    p1 = prop.create_proposal(slug, title="Estudo A", kind="lacuna",
                              priority="alta", questions=["RQ?"])
    p2 = prop.create_proposal(slug, title="Estudo B", kind="tendencia")
    assert {p.id for p in prop.list_proposals(slug)} == {p1, p2}
    assert len(prop.list_proposals(slug, status="proposto")) == 2
    assert prop.counts(slug)[""] == 2
    assert prop.kind_counts(slug)["lacuna"] == 1
    assert prop.set_status(slug, p1, "priorizado")
    assert prop.get_proposal(slug, p1).priority == "alta"
    assert prop.update_proposal(slug, p2, kind="bogus", priority="x",
                                status="descartado", notes="anotação")
    p = prop.get_proposal(slug, p2)
    assert p.kind == "oportunidade" and p.priority == "media"
    assert p.status == "descartado" and p.notes == "anotação"
    assert prop.delete_proposal(slug, p1)
    assert prop.counts(slug)[""] == 1


def test_convert_proposal_em_questoes(slug):
    pid = prop.create_proposal(
        slug, title="Novo estudo", questions=["Como X afeta Y?", "E Z?"])
    assert prop.convert_proposal(slug, pid, "questao").startswith("questao:")
    p = prop.get_proposal(slug, pid)
    assert p.status == "convertido" and p.converted_to
    with get_project_session(slug) as s:
        qs = list(s.scalars(select(ResearchQuestion)))
    assert len(qs) == 2 and all(q.question for q in qs)
    # segunda conversão é no-op
    assert prop.convert_proposal(slug, pid, "questao") == ""


def test_convert_proposal_em_projeto(slug):
    pid = prop.create_proposal(slug, title="Projeto derivado",
                               summary="resumo", questions=["P1?"])
    destino = prop.convert_proposal(slug, pid, "projeto")
    assert destino.startswith("projeto:")
    new_slug = destino.split(":", 1)[1]
    from jatai.projects import service as projects
    assert projects.get_project(new_slug).name == "Projeto derivado"


def test_persist_dedup_por_titulo_similar(slug):
    _mk_paper(slug, "Artigo sobre mobilidade urbana")
    res = gen.ProposalsResult(proposals=[
        gen.ProposalItem(kind="lacuna", title="Mobilidade urbana em cidades médias",
                         evidence=[{"title": "Artigo sobre mobilidade urbana",
                                    "detail": "único do tema"}],
                         questions=["Como medir?"]),
        gen.ProposalItem(kind="tendencia", title="Estudo de sensores IoT"),
        gen.ProposalItem(kind="x", title=""),          # kind inválido + vazio
        gen.ProposalItem(kind="oportunidade",
                         title="Mobilidade urbana em cidades médias"),  # dup
    ])
    n = gen.persist(slug, res)
    assert n == 2
    with get_project_session(slug) as s:
        props = list(s.scalars(select(Proposal)))
    assert len(props) == 2
    lacuna = next(p for p in props if p.kind == "lacuna")
    assert lacuna.evidence[0]["paper_id"] is not None
    assert lacuna.questions == ["Como medir?"]
    # kind inválido caiu em oportunidade? não — o item vazio foi pulado;
    # o 4º era duplicado do 1º
    assert {p.kind for p in props} == {"lacuna", "tendencia"}


def test_generate_usa_llm_e_persiste(slug, monkeypatch):
    _mk_paper(slug, "Paper A", state="incluido")
    fake = gen.ProposalsResult(proposals=[
        gen.ProposalItem(kind="oportunidade", title="Proposta X")])
    monkeypatch.setattr("jatai.agents.llm.chat_structured",
                        lambda s, **kw: fake)
    n = gen.generate(slug, model_ref="m", max_proposals=5)
    assert n == 1
    p = prop.get_proposal(slug, prop.list_proposals(slug)[0].id)
    assert p.origin == "agente" and p.status == "proposto"
