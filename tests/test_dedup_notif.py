"""Dedup registra grupos p/ revisão; merge/dismiss via catálogo."""

import json

import pytest
from sqlalchemy import select

from jatai import notifications
from jatai.catalog import service as catalog
from jatai.agents import runner
from jatai.storage.db import get_project_session
from jatai.storage.models_project import DedupGroup, Notification, Paper, PaperSighting
from types import SimpleNamespace


def _mk(slug, title, doi="", url="", pdf="", **kw):
    kw.setdefault("authors", "[]")
    kw.setdefault("state", "novo")
    with get_project_session(slug) as s:
        p = Paper(title=title, doi=doi, url=url, pdf_path=pdf, **kw)
        s.add(p)
        s.commit()
        return p.id


def _sighting(slug, paper_id, source, ext_id=""):
    with get_project_session(slug) as s:
        s.add(PaperSighting(paper_id=paper_id, source=source,
                            external_id=ext_id))
        s.commit()


def _count(slug):
    with get_project_session(slug) as s:
        return len(list(s.scalars(select(Paper))))


def _groups(slug):
    with get_project_session(slug) as s:
        return list(s.scalars(select(DedupGroup)))


def _run_dedup(slug, params=None):
    stats = {"queries": 0, "new_papers": 0, "new_terms": 0, "errors": []}
    task = SimpleNamespace(task_type="dedup",
                           params=json.dumps(params or {}))
    runner._dedup(slug, 0, 0, task, stats)
    return stats


def test_dedup_registers_group_by_url(slug, tmp_path):
    f = tmp_path / "a.pdf"; f.write_bytes(b"%PDF-1.4 fake")
    _mk(slug, "Paper A", url="https://example.com/p/1", pdf=str(f))
    _mk(slug, "Paper A dup", url="HTTPS://www.example.com/p/1/")
    _mk(slug, "Outro", url="https://example.com/p/2")
    stats = _run_dedup(slug)
    # nada é mesclado automaticamente
    assert _count(slug) == 3
    groups = _groups(slug)
    assert len(groups) == 1
    assert groups[0].status == "pendente"
    assert "identificador" in groups[0].reason
    assert stats["groups"] == 1


def test_dedup_registers_group_by_title_similarity(slug):
    _mk(slug, "Mobility trajectories in urban systems",
        url="https://a/1")
    _mk(slug, "Mobility trajectories in urban system",
        url="https://b/2")  # DOI/URL distintos
    stats = _run_dedup(slug)
    groups = _groups(slug)
    assert len(groups) == 1
    assert "título" in groups[0].reason
    assert stats["groups"] == 1


def test_dedup_threshold_blocks_weak_similarity(slug):
    _mk(slug, "Alpha beta gamma delta epsilon", url="https://a/1")
    _mk(slug, "Completely different words here now", url="https://b/2")
    stats = _run_dedup(slug)
    assert stats["groups"] == 0
    assert _groups(slug) == []


def test_dedup_rerun_replaces_pending_groups(slug):
    _mk(slug, "Paper A", doi="10.1/x", url="https://a")
    _mk(slug, "Paper A dup", url="https://doi.org/10.1/x")
    _run_dedup(slug)
    _mk(slug, "Paper B", doi="10.2/y", url="https://b")
    _mk(slug, "Paper B dup", url="https://doi.org/10.2/y")
    _run_dedup(slug)
    groups = _groups(slug)
    pendentes = [g for g in groups if g.status == "pendente"]
    # A-pair continua duplicada + novo grupo B-pair
    assert len(pendentes) == 2
    sizes = sorted(len(json.loads(g.paper_ids)) for g in pendentes)
    assert sizes == [2, 2]


def test_dedup_does_not_resurrect_dismissed_group(slug):
    _mk(slug, "Paper A", doi="10.1/x")
    _mk(slug, "Paper A dup", doi="10.1/x")
    _run_dedup(slug)
    catalog.dismiss_dedup_group(slug, _groups(slug)[0].id)
    stats = _run_dedup(slug)
    assert stats["groups"] == 0
    assert catalog.list_dedup_groups(slug) == []


def test_dedup_pdf_hash_notifies_without_group(slug, tmp_path):
    f1 = tmp_path / "a.pdf"; f1.write_bytes(b"identical-bytes")
    f2 = tmp_path / "b.pdf"; f2.write_bytes(b"identical-bytes")
    _mk(slug, "PDF A totally different title x", pdf=str(f1))
    _mk(slug, "Another unrelated paper y", pdf=str(f2))
    stats = _run_dedup(slug)
    assert _count(slug) == 2
    # sem DOI/URL/título similar -> nenhum grupo
    assert stats["groups"] == 0
    with get_project_session(slug) as s:
        notifs = list(s.scalars(select(Notification)))
    assert len(notifs) == 1
    assert notifs[0].kind == "pdf_dup"
    assert "dedup" in (notifs[0].link or "")
    with get_project_session(slug) as s:
        hashes = {p.pdf_hash for p in s.scalars(select(Paper))}
    assert all(len(h) == 64 for h in hashes)
    # segunda execução não duplica a notificação
    _run_dedup(slug)
    assert notifications.unread_count(slug) == 1


# ---------- merge / dismiss ----------


def test_merge_keeps_richest_and_combines_provenance(slug):
    p1 = _mk(slug, "Artigo X", doi="10.1/x", url="https://a/1",
             abstract="resumo longo " * 20, authors="[\"Ana\"]",
             venue="Journal A", year=2020)
    p2 = _mk(slug, "Artigo X (duplicata)", doi="10.1/x", url="https://a/1")
    _sighting(slug, p1, "dblp", "key1")
    _sighting(slug, p2, "openalex", "W2")
    _sighting(slug, p2, "dblp", "key1")  # duplicada -> deve sumir
    _run_dedup(slug)
    g = _groups(slug)[0]
    assert catalog.merge_dedup_group(slug, g.id)
    assert _count(slug) == 1
    kept = catalog.get_paper(slug, p1)
    assert kept is not None
    assert kept.abstract and len(kept.abstract) > 50  # registro rico
    sources = sorted(sg.source for sg in kept.sightings)
    assert sources == ["dblp", "openalex"]
    assert len(kept.sightings) == 2  # proveniência combinada sem duplicar
    g2 = _groups(slug)[0]
    assert g2.status == "mesclado"


def test_merge_respects_keep_id(slug):
    p1 = _mk(slug, "Simples", doi="10.1/x")
    p2 = _mk(slug, "Detalhado", doi="10.1/x", abstract="abc " * 30,
             url="https://x")
    _run_dedup(slug)
    g = _groups(slug)[0]
    # força manter o mais simples
    assert catalog.merge_dedup_group(slug, g.id, keep_id=p1)
    kept = catalog.get_paper(slug, p1)
    assert kept.doi == "10.1/x"
    assert kept.abstract  # completou campo vazio a partir do outro
    assert catalog.get_paper(slug, p2) is None


def test_dismiss_group_keeps_papers(slug):
    _mk(slug, "A", doi="10.1/x")
    _mk(slug, "B dup", doi="10.1/x")
    _run_dedup(slug)
    g = _groups(slug)[0]
    assert catalog.dismiss_dedup_group(slug, g.id)
    assert _count(slug) == 2
    assert _groups(slug)[0].status == "ignorado"
    assert catalog.list_dedup_groups(slug) == []


def test_merge_already_resolved_returns_false(slug):
    _mk(slug, "A", doi="10.1/x")
    _mk(slug, "B", doi="10.1/x")
    _run_dedup(slug)
    g = _groups(slug)[0]
    catalog.dismiss_dedup_group(slug, g.id)
    assert not catalog.merge_dedup_group(slug, g.id)


def test_normalization_removed():
    from jatai.agents.config_service import TASK_TYPES
    assert "normalization" not in [t["type"] for t in TASK_TYPES]


def test_notification_service(slug):
    nid = notifications.notify(slug, "run", "Teste", "corpo", "/x")
    assert notifications.unread_count(slug) == 1
    notifications.mark_read(slug, nid)
    assert notifications.unread_count(slug) == 0
    notifications.notify(slug, "info", "Outra")
    notifications.mark_all_read(slug)
    assert notifications.unread_count(slug) == 0
