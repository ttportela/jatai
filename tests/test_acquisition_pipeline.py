"""Testes do pipeline de aquisição: providers, validação e orquestração."""

from pathlib import Path

import pymupdf
import pytest

from jatai.documents import acquisition as acq
from jatai.documents.acquisition import providers as prov
from jatai.documents.acquisition.validate import validate_pdf_file


def _real_pdf_bytes(text: str = "Artigo de teste " * 20) -> bytes:
    doc = pymupdf.open()
    for _ in range(3):
        page = doc.new_page()
        page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def _paper(slug, **kw):
    from jatai.storage.db import get_project_session
    from jatai.storage.models_project import Paper
    defaults = dict(title="Artigo X", doi="10.1/x", url="",
                    state="incluido", pdf_path="", authors="[]", year=2020)
    defaults.update(kw)
    with get_project_session(slug) as s:
        p = Paper(**defaults)
        s.add(p)
        s.commit()
        return p.id


class _Resp:
    def __init__(self, data=None, status=200, text="", ctype="application/json"):
        self._data = data
        self.status_code = status
        self.text = text
        self.headers = {"content-type": ctype}

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


# ---------------- Providers por DOI ----------------

def test_unpaywall_candidates(monkeypatch):
    payload = {"oa_locations": [
        {"url_for_pdf": "https://repo.org/a.pdf",
         "url": "https://repo.org/landing"},
        {"url": "https://other.org/b"},
    ], "best_oa_location": {"url_for_pdf": "https://best.org/c.pdf"}}
    monkeypatch.setattr(prov.httpx, "get",
                        lambda *a, **kw: _Resp(payload))
    cands = prov.doi_unpaywall("10.1/x")
    urls = [c.url for c in cands]
    assert "https://best.org/c.pdf" in urls
    assert "https://repo.org/a.pdf" in urls
    assert all(c.source == "unpaywall" for c in cands)
    # url_for_pdf deve ser marcada como pdf mesmo sem .pdf na URL
    forced = [c for c in cands if c.url == "https://repo.org/a.pdf"]
    assert forced and forced[0].kind == "pdf"


def test_unpaywall_http_error_returns_empty(monkeypatch):
    def boom(*a, **kw):
        raise prov.httpx.ConnectError("down")
    monkeypatch.setattr(prov.httpx, "get", boom)
    assert prov.doi_unpaywall("10.1/x") == []


def test_openalex_doi_candidates(monkeypatch):
    payload = {"best_oa_location": {"pdf_url": "https://oa.org/w.pdf",
                                    "landing_page_url": "https://oa.org/w"},
               "locations": [{"pdf_url": "https://repo.org/w.pdf"}],
               "open_access": {"oa_url": "https://oa.org/w"}}
    monkeypatch.setattr(prov.httpx, "get", lambda *a, **kw: _Resp(payload))
    urls = [c.url for c in prov.doi_openalex("10.1/x")]
    assert "https://oa.org/w.pdf" in urls
    assert "https://repo.org/w.pdf" in urls


def test_crossref_pdf_links(monkeypatch):
    payload = {"message": {
        "link": [{"content-type": "application/pdf", "URL": "https://cr.org/a.pdf"},
                 {"content-type": "text/html", "URL": "https://cr.org/page"}],
        "URL": "https://doi.org/10.1/x"}}
    monkeypatch.setattr(prov.httpx, "get", lambda *a, **kw: _Resp(payload))
    cands = prov.doi_crossref("10.1/x")
    pdfs = [c for c in cands if c.kind == "pdf"]
    assert any(c.url == "https://cr.org/a.pdf" for c in pdfs)
    # link text/html não deve virar candidato pdf
    assert not any(c.url == "https://cr.org/page" and c.kind == "pdf"
                   for c in cands)


def test_core_requires_key(monkeypatch):
    monkeypatch.setattr(prov, "_core_key", lambda k: "")
    assert prov.doi_core("10.1/x") == []
    assert prov.meta_core(prov.Query(title="t")) == []


def test_core_with_key(monkeypatch):
    monkeypatch.setattr(prov, "_core_key", lambda k: "KEY")
    payload = {"results": [{"downloadUrl": "https://core.org/dl.pdf"}]}
    seen = {}

    def fake_get(url, **kw):
        seen["auth"] = kw.get("headers", {}).get("Authorization", "")
        return _Resp(payload)
    monkeypatch.setattr(prov.httpx, "get", fake_get)
    cands = prov.doi_core("10.1/x")
    assert seen["auth"] == "Bearer KEY"
    assert cands[0].url == "https://core.org/dl.pdf"


# ---------------- Providers por metadados ----------------

def test_meta_arxiv_parses_xml(monkeypatch):
    xml = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry><id>http://arxiv.org/abs/2101.00001v2</id></entry>
      <entry><id>http://arxiv.org/abs/2101.00002</id></entry>
    </feed>"""
    monkeypatch.setattr(prov.httpx, "get",
                        lambda *a, **kw: _Resp(text=xml, ctype="text/xml"))
    cands = prov.meta_arxiv(prov.Query(title="movimento de trajetórias"))
    urls = [c.url for c in cands]
    assert "https://arxiv.org/pdf/2101.00001v2" in urls
    assert "https://arxiv.org/pdf/2101.00002" in urls
    assert all(c.kind == "pdf" and c.via == "meta" for c in cands)


def test_meta_openalex_uses_year_filter(monkeypatch):
    captured = {}

    def fake_get(url, params=None, **kw):
        captured.update(params or {})
        return _Resp({"results": [{"best_oa_location":
                                   {"pdf_url": "https://oa.org/m.pdf"}}]})
    monkeypatch.setattr(prov.httpx, "get", fake_get)
    cands = prov.meta_openalex(prov.Query(title="titulo", year=2020))
    assert "from_publication_date:2019-01-01" in captured.get("filter", "")
    assert cands[0].url == "https://oa.org/m.pdf"


# ---------------- find_candidates / find_meta_candidates ----------------

def test_find_candidates_doi_phase_skips_meta_when_pdf(monkeypatch):
    monkeypatch.setitem(prov.DOI_PROVIDERS, "unpaywall",
                        lambda doi, **kw: [prov.Candidate(
                            "https://u.org/a.pdf", "unpaywall", "pdf")])
    monkeypatch.setitem(prov.META_PROVIDERS, "openalex",
                        lambda q, **kw: pytest.fail("meta não devia rodar"))
    out = prov.find_candidates(prov.Query(doi="10.1/x", title="t"),
                               sources="unpaywall,openalex")
    assert [c.url for c in out] == ["https://u.org/a.pdf"]


def test_find_candidates_falls_back_to_meta(monkeypatch):
    monkeypatch.setitem(prov.DOI_PROVIDERS, "unpaywall",
                        lambda doi, **kw: [])
    monkeypatch.setitem(prov.META_PROVIDERS, "arxiv",
                        lambda q, **kw: [prov.Candidate(
                            "https://arxiv.org/pdf/1", "arxiv", "pdf",
                            "meta")])
    out = prov.find_candidates(prov.Query(doi="10.1/x", title="t"),
                               sources="unpaywall,arxiv")
    assert out[0].url == "https://arxiv.org/pdf/1"
    assert out[0].via == "meta"


def test_find_candidates_dedup_and_pdf_first(monkeypatch):
    monkeypatch.setitem(prov.DOI_PROVIDERS, "unpaywall",
                        lambda doi, **kw: [
                            prov.Candidate("https://x.org/landing",
                                           "unpaywall", "landing"),
                            prov.Candidate("https://x.org/a.pdf",
                                           "unpaywall", "pdf")])
    out = prov.find_candidates(prov.Query(doi="10.1/x"),
                               sources="unpaywall")
    assert out[0].url == "https://x.org/a.pdf"
    assert len(out) == 2


def test_find_oa_pdf_parallel_sources(monkeypatch):
    monkeypatch.setitem(acq._SOURCES, "unpaywall",
                        lambda doi, email="x": ["https://u.org/a.pdf"])
    monkeypatch.setitem(acq._SOURCES, "crossref",
                        lambda doi: ["https://c.org/b.pdf"])
    out = acq.find_oa_pdf("10.1/x", sources="unpaywall,crossref")
    assert set(out) == {"https://u.org/a.pdf", "https://c.org/b.pdf"}


# ---------------- Validação ----------------

def test_validate_real_pdf(tmp_path):
    p = tmp_path / "a.pdf"
    p.write_bytes(_real_pdf_bytes())
    r = validate_pdf_file(p)
    assert r["ok"] and r["pages"] >= 1


def test_validate_rejects_fake_pdf(tmp_path):
    p = tmp_path / "fake.pdf"
    p.write_bytes(b"%PDF-1.4" + b"0" * 3000)
    r = validate_pdf_file(p)
    assert not r["ok"]


def test_validate_rejects_html(tmp_path):
    p = tmp_path / "page.pdf"
    p.write_bytes(b"<html>" + b"x" * 3000)
    assert not validate_pdf_file(p)["ok"]


def test_validate_title_mismatch(tmp_path):
    """PDF de outro artigo (texto diferente) deve ser rejeitado."""
    doc = pymupdf.open()
    for _ in range(2):
        page = doc.new_page()
        for i in range(10):
            page.insert_text(
                (72, 72 + i * 14),
                "Completely unrelated quantum chromodynamics lattice "
                "gauge theory results section " + str(i))
    p = tmp_path / "other.pdf"
    p.write_bytes(doc.tobytes())
    doc.close()
    r = validate_pdf_file(p, expected_title="Estudo sobre migração de "
                                          "bactérias marinhas")
    assert not r["ok"]
    assert "diverge" in r["reason"]


def test_validate_missing_file(tmp_path):
    assert not validate_pdf_file(tmp_path / "nope.pdf")["ok"]


# ---------------- Orquestração / pipeline ----------------

def test_acquire_uses_meta_fallback(slug, tmp_path, monkeypatch):
    """DOI não retorna PDF -> fallback por metadados baixa via arXiv."""
    pid = _paper(slug, doi="10.1/x",
                 title="trajetórias e movimento de objetos",
                 url="https://doi.org/10.1/x")
    monkeypatch.setattr(acq, "find_oa_pdf", lambda *a, **kw: [])
    monkeypatch.setattr(acq, "find_meta_candidates",
                        lambda q, **kw: [prov.Candidate(
                            "https://arxiv.org/pdf/1234.5678", "arxiv",
                            "pdf", "meta")])
    monkeypatch.setattr(acq, "download_pdf",
                        lambda url, d: (d.parent.mkdir(parents=True,
                                                       exist_ok=True),
                                        d.write_bytes(_real_pdf_bytes(
                                            "trajetórias e movimento " * 15)),
                                        True)[-1])
    path = acq.acquire_for_paper(slug, pid, str(tmp_path / "ws"))
    assert path and "meta" in Path(path).name


def test_acquire_authenticated_last_resort(slug, tmp_path, monkeypatch):
    """Com authenticated=True e todos os downloads normais falhando,
    tenta a sessão autenticada."""
    pid = _paper(slug, doi="10.1/x", title="t",
                 url="https://doi.org/10.1/x")
    monkeypatch.setattr(acq, "find_oa_pdf",
                        lambda *a, **kw: ["https://pub.org/a.pdf"])
    monkeypatch.setattr(acq, "find_meta_candidates",
                        lambda *a, **kw: [])
    monkeypatch.setattr(acq, "download_pdf", lambda url, d: False)
    monkeypatch.setattr(acq, "download_pdf_browser",
                        lambda *a, **kw: False)
    calls = []

    def fake_ctx(ctx, url, dest):
        calls.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_real_pdf_bytes())
        return True
    monkeypatch.setattr(acq, "download_pdf_context", fake_ctx)
    path = acq.acquire_for_paper(slug, pid, str(tmp_path / "ws"),
                                 browser=object(), authenticated=True,
                                 auth_context=object())
    assert path and "auth" in Path(path).name
    assert calls == ["https://pub.org/a.pdf"]


def test_acquire_no_auth_without_flag(slug, tmp_path, monkeypatch):
    """Sem authenticated=True, sessão autenticada não é tentada."""
    pid = _paper(slug, doi="10.1/x", title="t")
    monkeypatch.setattr(acq, "find_oa_pdf",
                        lambda *a, **kw: ["https://pub.org/a.pdf"])
    monkeypatch.setattr(acq, "find_meta_candidates",
                        lambda *a, **kw: [])
    monkeypatch.setattr(acq, "download_pdf", lambda url, d: False)
    monkeypatch.setattr(acq, "download_pdf_browser",
                        lambda *a, **kw: pytest.fail("sem browser"))
    monkeypatch.setattr(acq, "download_pdf_context",
                        lambda *a, **kw: pytest.fail("sem auth"))
    assert acq.acquire_for_paper(slug, pid, str(tmp_path / "ws"),
                                 browser=None) is None


def test_save_recovers_existing_valid_file(slug, tmp_path, monkeypatch):
    """Se o arquivo já existe e é válido, não baixa de novo."""
    pid = _paper(slug, doi="10.1/x", title="Artigo teste",
                 url="https://x.org/a.pdf")
    monkeypatch.setattr(acq, "find_oa_pdf", lambda *a, **kw: [])
    ws = tmp_path / "ws"
    dest = ws / "pdfs" / f"{pid}-direct-a.pdf"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(_real_pdf_bytes())
    monkeypatch.setattr(acq, "download_pdf",
                        lambda *a, **kw: pytest.fail("não devia baixar"))
    path = acq.acquire_for_paper(slug, pid, str(ws))
    assert path == str(dest)


def test_save_rejects_invalid_existing_file(slug, tmp_path, monkeypatch):
    """Arquivo existente mas inválido é removido e re-baixado."""
    pid = _paper(slug, doi="10.1/x", title="Artigo teste",
                 url="https://x.org/b.pdf")
    monkeypatch.setattr(acq, "find_oa_pdf", lambda *a, **kw: [])
    ws = tmp_path / "ws"
    dest = ws / "pdfs" / f"{pid}-direct-b.pdf"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"lixo" * 1000)
    monkeypatch.setattr(acq, "download_pdf",
                        lambda url, d: (d.write_bytes(_real_pdf_bytes()),
                                        True)[-1])
    path = acq.acquire_for_paper(slug, pid, str(ws))
    assert path == str(dest)


def test_acquire_skips_paper_with_pdf(slug, tmp_path, monkeypatch):
    pid = _paper(slug, doi="10.1/x", title="t", pdf_path="/tmp/x.pdf")
    monkeypatch.setattr(acq, "find_oa_pdf",
                        lambda *a, **kw: pytest.fail("não devia consultar"))
    assert acq.acquire_for_paper(slug, pid, str(tmp_path)) is None


def test_pdf_hash_and_paperfile_dedup(slug, tmp_path, monkeypatch):
    """Segunda aquisição do mesmo paper não duplica PaperFile."""
    pid = _paper(slug, doi="10.1/x", title="Artigo teste",
                 url="https://x.org/c.pdf")
    monkeypatch.setattr(acq, "find_oa_pdf", lambda *a, **kw: [])
    monkeypatch.setattr(acq, "download_pdf",
                        lambda url, d: (d.parent.mkdir(parents=True,
                                                       exist_ok=True),
                                        d.write_bytes(_real_pdf_bytes()),
                                        True)[-1])
    ws = str(tmp_path / "ws")
    p1 = acq.acquire_for_paper(slug, pid, ws)
    # segunda chamada retorna cedo por pdf_path preenchido
    assert acq.acquire_for_paper(slug, pid, ws) is None
    from jatai.storage.db import get_project_session
    from jatai.storage.models_project import PaperFile
    with get_project_session(slug) as s:
        files = s.query(PaperFile).filter_by(paper_id=pid).all()
        assert len(files) == 1
        assert files[0].path == p1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
