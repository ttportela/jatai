"""Testes unitários de lelia.documents.acquisition."""

import pytest

from lelia.documents import acquisition as acq


def _real_pdf_bytes() -> bytes:
    """Gera um PDF mínimo válido via pymupdf para os testes."""
    import pymupdf
    doc = pymupdf.open()
    for _ in range(3):
        page = doc.new_page()
        page.insert_text((72, 72), "Artigo de teste para validação " * 10)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def paper(slug):
    from lelia.storage.db import get_project_session
    from lelia.storage.models_project import Paper
    with get_project_session(slug) as s:
        p = Paper(title="Artigo teste", doi="10.1/test",
                  url="https://example.org/paper", state="incluido",
                  pdf_path="", authors="[]")
        s.add(p)
        s.commit()
        return p.id


def test_looks_like_pdf():
    assert acq._looks_like_pdf("https://x.org/a.pdf")
    assert acq._looks_like_pdf("https://x.org/content/pdf/10.1/a")
    assert acq._looks_like_pdf("https://x.org/a.pdf?x=1")
    assert not acq._looks_like_pdf("https://x.org/article/123")
    assert not acq._looks_like_pdf("https://doi.org/10.1/x")


def test_is_pdf_bytes():
    assert acq._is_pdf_bytes(b"%PDF-1.4" + b"0" * 2000)
    assert acq._is_pdf_bytes(b"junk" * 100 + b"%PDF-1.7" + b"0" * 900)
    assert not acq._is_pdf_bytes(b"<html>" + b"x" * 2000, "text/html")
    assert not acq._is_pdf_bytes(b"%PDF-1.4")  # pequeno demais
    # ctype pdf mas conteúdo HTML -> rejeita
    assert not acq._is_pdf_bytes(b"<html>body</html>" + b" " * 2000,
                                 "application/pdf")


def test_find_oa_pdf_no_doi():
    assert acq.find_oa_pdf("") == []
    assert acq.find_oa_pdf(None) == []


def test_find_oa_pdf_sources_filter(monkeypatch):
    monkeypatch.setitem(acq._SOURCES, "unpaywall",
                        lambda doi, email="x": ["https://u.org/a.pdf"])
    monkeypatch.setitem(acq._SOURCES, "openalex",
                        lambda doi: ["https://oa.org/b.pdf"])
    # só unpaywall
    assert acq.find_oa_pdf("10.1/x", sources="unpaywall") == \
        ["https://u.org/a.pdf"]
    # ambos, pdf-like primeiro já garantido; dedup preserva ordem
    both = acq.find_oa_pdf("10.1/x", sources=["unpaywall", "openalex"])
    assert both == ["https://u.org/a.pdf", "https://oa.org/b.pdf"]
    # fonte desconhecida ignorada
    assert acq.find_oa_pdf("10.1/x", sources="foo") == []


def test_download_pdf_rejects_html(tmp_path, monkeypatch):
    class FakeResp:
        status_code = 200
        headers = {"content-type": "text/html"}

        def iter_bytes(self, n):
            yield b"<html>" + b"x" * 3000

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(acq.httpx, "stream",
                        lambda *a, **kw: FakeResp())
    dest = tmp_path / "x.pdf"
    assert not acq.download_pdf("https://x.org/a.pdf", dest)
    assert not dest.exists()


def test_download_pdf_accepts_pdf(tmp_path, monkeypatch):
    payload = b"%PDF-1.4\n" + b"0" * 3000

    class FakeResp:
        status_code = 200
        headers = {"content-type": "application/pdf"}

        def iter_bytes(self, n):
            yield payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(acq.httpx, "stream",
                        lambda *a, **kw: FakeResp())
    dest = tmp_path / "x.pdf"
    assert acq.download_pdf("https://x.org/a.pdf", dest)
    assert dest.read_bytes() == payload


def test_acquire_direct_pdf_url(slug, paper, tmp_path, monkeypatch):
    """URL do paper já é PDF e não tem DOI -> baixa direto, sem APIs."""
    from lelia.storage.db import get_project_session
    from lelia.storage.models_project import Paper
    with get_project_session(slug) as s:
        p = s.get(Paper, paper)
        p.doi = ""
        p.url = "https://ceur-ws.org/Vol-1/x.pdf"
        s.commit()

    monkeypatch.setattr(acq, "find_oa_pdf", lambda *a, **kw: [])
    dest = tmp_path / "ws"
    monkeypatch.setattr(acq, "download_pdf",
                        lambda url, d: (d.parent.mkdir(parents=True,
                                                       exist_ok=True),
                                        d.write_bytes(_real_pdf_bytes()),
                                        True)[-1])
    path = acq.acquire_for_paper(slug, paper, str(dest))
    assert path and path.endswith("x.pdf")
    with get_project_session(slug) as s:
        assert s.get(Paper, paper).pdf_path == path


def test_acquire_falls_back_to_browser(slug, paper, tmp_path, monkeypatch):
    """httpx falha em todos os candidatos -> tenta via browser."""
    monkeypatch.setattr(acq, "find_oa_pdf",
                        lambda doi, sources=None: ["https://oa.org/paper"])
    monkeypatch.setattr(acq, "find_meta_candidates",
                        lambda *a, **kw: [])
    monkeypatch.setattr(acq, "download_pdf", lambda url, d: False)
    calls = []

    def fake_browser_dl(browser, url, dest):
        calls.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_real_pdf_bytes())
        return True

    monkeypatch.setattr(acq, "download_pdf_browser", fake_browser_dl)
    path = acq.acquire_for_paper(slug, paper, str(tmp_path / "ws"),
                                 browser=object())
    assert path
    assert calls == ["https://oa.org/paper"]


def test_acquire_no_browser_no_fallback(slug, paper, tmp_path, monkeypatch):
    """Sem browser disponível, não tenta fallback."""
    monkeypatch.setattr(acq, "find_oa_pdf",
                        lambda doi, sources=None: ["https://oa.org/paper"])
    monkeypatch.setattr(acq, "find_meta_candidates",
                        lambda *a, **kw: [])
    monkeypatch.setattr(acq, "download_pdf", lambda url, d: False)
    monkeypatch.setattr(acq, "download_pdf_browser",
                        lambda *a, **kw: pytest.fail("não devia chamar browser"))
    assert acq.acquire_for_paper(slug, paper, str(tmp_path / "ws")) is None


def test_acquire_skips_existing_pdf(slug, paper, monkeypatch):
    from lelia.storage.db import get_project_session
    from lelia.storage.models_project import Paper
    with get_project_session(slug) as s:
        s.get(Paper, paper).pdf_path = "/tmp/já.pdf"
        s.commit()
    monkeypatch.setattr(acq, "find_oa_pdf",
                        lambda *a, **kw: pytest.fail("não devia consultar"))
    assert acq.acquire_for_paper(slug, paper, "/tmp") is None


def test_download_pdf_browser_extracts_pdf_link(tmp_path):
    """Landing page com citation_pdf_url -> segue o link e baixa."""
    saved = {}

    class FakeDownload:
        def save_as(self, path):
            saved["path"] = path
            import pathlib
            pathlib.Path(path).write_bytes(_real_pdf_bytes())

    class FakePage:
        def __init__(self):
            self.calls = 0
            self.last_url = ""

        def goto(self, url, **kw):
            self.calls += 1
            self.last_url = url

        def evaluate(self, js):
            return ["https://x.org/real.pdf"] if self.calls else []

        def expect_download(self, timeout=None):
            page = self

            class Ctx:
                def __enter__(self):
                    class Info:
                        value = FakeDownload()
                    return Info()

                def __exit__(self, *a):
                    # simula TimeoutError do Playwright quando a página
                    # navegada não disparou download (landing HTML)
                    if "landing" in page.last_url:
                        raise TimeoutError("no download")
                    return False
            return Ctx()

    class FakeCtx:
        def new_page(self):
            return FakePage()

        def close(self):
            pass

    class FakeBrowser:
        def new_context(self, **kw):
            return FakeCtx()

    dest = tmp_path / "x.pdf"
    assert acq.download_pdf_browser(FakeBrowser(), "https://x.org/landing",
                                    dest, timeout_ms=100)
    assert saved["path"] == str(dest)
    assert dest.exists()
    assert b"%PDF" in dest.read_bytes()[:10]


def test_download_pdf_browser_no_download(tmp_path):
    """Página sem PDF e sem links -> retorna False."""
    class FakePage:
        def goto(self, url, **kw):
            pass

        def evaluate(self, js):
            return []

        def expect_download(self, timeout=None):
            class Ctx:
                def __enter__(self):
                    raise TimeoutError("no download")

                def __exit__(self, *a):
                    return False
            return Ctx()

    class FakeCtx:
        def new_page(self):
            return FakePage()

        def close(self):
            pass

    class FakeBrowser:
        def new_context(self, **kw):
            return FakeCtx()

    assert not acq.download_pdf_browser(FakeBrowser(),
                                        "https://x.org/landing",
                                        tmp_path / "x.pdf",
                                        timeout_ms=100)
    assert not (tmp_path / "x.pdf").exists()
