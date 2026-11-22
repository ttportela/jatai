"""Lelia Browser: detecção e anexo de PDFs + rotas."""

import pymupdf
import pytest

from lelia import notifications, user_browser
from lelia.catalog import service as catalog
from lelia.storage.db import get_project_session
from lelia.storage.models_project import Paper, PaperFile
from sqlalchemy import select


def _real_pdf_bytes(text: str) -> bytes:
    doc = pymupdf.open()
    for _ in range(3):
        page = doc.new_page()
        page.insert_text((72, 72), text * 30)
    data = doc.tobytes()
    doc.close()
    return data


def _mk(slug, title="Artigo X", **kw):
    defaults = dict(title=title, doi="10.1/x", url="https://ex.com/p",
                    state="novo", pdf_path="", authors="[]", year=2020)
    defaults.update(kw)
    with get_project_session(slug) as s:
        p = Paper(**defaults)
        s.add(p)
        s.commit()
        return p.id


def _paper(slug, pid):
    with get_project_session(slug) as s:
        return s.get(Paper, pid)


@pytest.fixture(autouse=True)
def _clean_state():
    user_browser._state.update(ctx=None, page=None, target=None,
                               current_url="", error="")
    user_browser._HANDLED_URLS.clear()
    yield
    user_browser._state.update(ctx=None, page=None, target=None,
                               current_url="", error="")
    user_browser._HANDLED_URLS.clear()


class _FakeDownload:
    def __init__(self, data, filename="paper.pdf", url="https://x/dl"):
        self._data = data
        self.suggested_filename = filename
        self.url = url

    def save_as(self, path):
        with open(path, "wb") as f:
            f.write(self._data)


class _FakeReq:
    def __init__(self, rtype="document"):
        self.resource_type = rtype


class _FakeResp:
    def __init__(self, body=b"", ctype="application/pdf", url="https://x/p.pdf",
                 rtype="document"):
        self._body = body
        self.url = url
        self.request = _FakeReq(rtype)
        self.headers = {"content-type": ctype}

    def body(self):
        return self._body


def test_attach_pdf_success(slug):
    pid = _mk(slug, "Deep Learning for X" * 3)
    data = _real_pdf_bytes("Deep Learning for X")
    user_browser._attach(slug, pid, data, "paper.pdf", "https://x/p.pdf")
    p = _paper(slug, pid)
    assert p.pdf_path and p.pdf_path.endswith(".pdf")
    assert Path_exists(p.pdf_path)
    with get_project_session(slug) as s:
        files = list(s.scalars(select(PaperFile).where(
            PaperFile.paper_id == pid)))
    assert len(files) == 1 and files[0].origin == "browser"
    assert notifications.unread_count(slug) == 1


def test_attach_rejects_non_pdf(slug):
    pid = _mk(slug)
    user_browser._attach(slug, pid, b"<html>not a pdf</html>" * 100,
                         "x.pdf", "https://x/p")
    assert not _paper(slug, pid).pdf_path
    assert notifications.unread_count(slug) == 1  # aviso de inválido


def test_attach_skips_when_pdf_exists(slug):
    pid = _mk(slug, pdf_path="/tmp/existing.pdf")
    user_browser._attach(slug, pid, _real_pdf_bytes("Qualquer" * 30),
                         "x.pdf", "https://x/p")
    assert _paper(slug, pid).pdf_path == "/tmp/existing.pdf"


def test_on_download_attaches(slug):
    pid = _mk(slug, "Titulo do Paper " * 3)
    user_browser._state["target"] = (slug, pid)
    dl = _FakeDownload(_real_pdf_bytes("Titulo do Paper"),
                       url="https://x/dl-1")
    user_browser._on_download(dl)
    assert _paper(slug, pid).pdf_path
    # mesma URL não anexa de novo
    user_browser._on_download(dl)
    with get_project_session(slug) as s:
        assert len(list(s.scalars(select(PaperFile)))) == 1


def test_on_response_pdf_content_type(slug):
    pid = _mk(slug, "Paper Y " * 3)
    user_browser._state["target"] = (slug, pid)
    resp = _FakeResp(body=_real_pdf_bytes("Paper Y"),
                     url="https://x/viewer.pdf")
    user_browser._on_response(resp)
    assert _paper(slug, pid).pdf_path


def test_on_response_ignores_non_pdf_and_non_http(slug):
    pid = _mk(slug)
    user_browser._state["target"] = (slug, pid)
    user_browser._on_response(_FakeResp(ctype="text/html",
                                        body=b"<html></html>"))
    # URL não-http (ex.: chrome-extension do viewer) é ignorada
    user_browser._on_response(_FakeResp(
        body=_real_pdf_bytes("x" * 30), url="chrome-extension://x/y.pdf"))
    assert not _paper(slug, pid).pdf_path


def test_open_for_paper_enqueues(slug):
    # não inicia a thread de browser de verdade: drena a fila na mão
    user_browser._state["thread"] = None
    called = []
    user_browser._queue.put  # noqa: B018 — sanity
    import lelia.user_browser as ub
    orig_start = ub._start
    ub._start = lambda: called.append(1)
    try:
        ub.open_for_paper(slug, 1, "https://ex.com")
    finally:
        ub._start = orig_start
    req = ub._queue.get_nowait()
    assert req == {"slug": slug, "paper_id": 1, "url": "https://ex.com"}
    assert called == [1]


def test_status_shape():
    st = user_browser.status()
    assert set(st) == {"running", "current_url", "has_target", "error"}


def test_attach_pdf_origin_dedup(slug):
    pid = _mk(slug)
    data = _real_pdf_bytes("Dedup " * 30)
    path = catalog.attach_pdf(slug, pid, "a.pdf", data, origin="browser")
    catalog.attach_pdf(slug, pid, "a.pdf", data, origin="browser")
    with get_project_session(slug) as s:
        files = list(s.scalars(select(PaperFile).where(
            PaperFile.paper_id == pid)))
    assert len(files) == 1 and files[0].path == path


def Path_exists(p):
    from pathlib import Path
    return Path(p).exists()


def test_route_opens_browser(slug, monkeypatch):
    from fastapi.testclient import TestClient
    from lelia.app import create_app
    pid = _mk(slug, url="https://publisher.com/article/1")
    opened = []
    monkeypatch.setattr(user_browser, "open_for_paper",
                        lambda s, p, u: opened.append((s, p, u)))
    client = TestClient(create_app())
    r = client.post(f"/projetos/{slug}/publicacoes/{pid}/browser",
                    follow_redirects=False)
    assert r.status_code == 303
    assert opened == [(slug, pid, "https://publisher.com/article/1")]


def test_route_doi_fallback(slug, monkeypatch):
    from fastapi.testclient import TestClient
    from lelia.app import create_app
    pid = _mk(slug, url="", doi="10.1234/abc")
    opened = []
    monkeypatch.setattr(user_browser, "open_for_paper",
                        lambda s, p, u: opened.append(u))
    client = TestClient(create_app())
    r = client.post(f"/projetos/{slug}/publicacoes/{pid}/browser",
                    follow_redirects=False)
    assert r.status_code == 303
    assert opened == ["https://doi.org/10.1234/abc"]


def test_publications_column_button(slug):
    from fastapi.testclient import TestClient
    from lelia.app import create_app
    _mk(slug, title="Com PDF", pdf_path="/tmp/x.pdf")
    _mk(slug, title="Sem PDF", url="https://ex.com/a")
    client = TestClient(create_app())
    html = client.get(f"/projetos/{slug}/publicacoes").text
    assert "/pdf-view" in html
    assert "/browser" in html
    assert "Lelia Browser" in html
    assert html.count("🌐") >= 2  # botão por linha + toolbar


if __name__ == "__main__":
    pytest.main([__file__, "-x", "-q"])


class _FakePage:
    def __init__(self, closed=False):
        self._closed = closed
        self.handlers = {}
        self.gotos = []

    def is_closed(self):
        return self._closed

    def on(self, ev, fn):
        self.handlers[ev] = fn

    def goto(self, url, timeout=None):
        self.gotos.append(url)


class _FakeCtx:
    def __init__(self, pages):
        self.pages = pages
        self.handlers = {}

    def on(self, ev, fn):
        self.handlers[ev] = fn

    def new_page(self):
        p = _FakePage()
        self.pages.append(p)
        return p

    def close(self):
        pass


def test_ensure_page_recovers_after_window_closed(monkeypatch):
    dead = _FakePage(closed=True)
    fresh = _FakePage()
    ctx = _FakeCtx([fresh])
    user_browser._state.update(ctx=None, page=None)
    user_browser._state["page"] = dead
    monkeypatch.setattr(user_browser, "_launch_context",
                        lambda: ctx)
    page = user_browser._ensure_page()
    assert page is fresh
    assert user_browser._state["ctx"] is ctx


def test_ensure_page_reuses_live_context(monkeypatch):
    live = _FakePage()
    ctx = _FakeCtx([live])
    user_browser._state.update(ctx=ctx, page=None)
    monkeypatch.setattr(user_browser, "_launch_context",
                        lambda: pytest.fail("não devia relançar"))
    assert user_browser._ensure_page() is live


# ---------- pipeline ----------

class _PipePage(_FakePage):
    def __init__(self, on_wait=None):
        super().__init__()
        self.url = "about:blank"
        self.waits = 0
        self._on_wait = on_wait
        self.statuses = []

    def wait_for_timeout(self, ms):
        self.waits += 1
        if self._on_wait:
            self._on_wait(self.waits)

    def evaluate(self, expr, arg=None):
        if "__leliaSetStatus" in expr:
            self.statuses.append(arg)
        return None


def _drain_queue():
    while True:
        try:
            user_browser._queue.get_nowait()
        except Exception:
            break


def test_open_pipeline_enqueues_items(slug):
    _mk(slug, title="Sem PDF url", url="https://ex.com/a")
    _mk(slug, title="Sem PDF doi", url="", doi="10.1/z")
    _mk(slug, title="Com PDF", pdf_path="/tmp/x.pdf", url="https://ex.com/b")
    _mk(slug, title="Sem nada", url="", doi="")
    _drain_queue()
    n = user_browser.open_pipeline(slug)
    assert n == 2
    req = user_browser._queue.get_nowait()
    assert req["kind"] == "pipeline" and req["slug"] == slug
    assert req["items"] == [(1, "https://ex.com/a"),
                            (2, "https://doi.org/10.1/z")] or \
        req["items"][0][1] == "https://ex.com/a"
    assert req["items"][1] == (2, "https://doi.org/10.1/z")


def test_attach_sets_found_event(slug):
    pid = _mk(slug, "Found Event " * 3)
    import threading as _t
    user_browser._state["found_event"] = _t.Event()
    user_browser._attach(slug, pid, _real_pdf_bytes("Found Event"),
                         "x.pdf", "https://x/p")
    assert user_browser._state["found_event"].is_set()


def test_run_pipeline_advances_on_found(slug, monkeypatch):
    _drain_queue()
    gotos = []

    def on_wait(w):
        user_browser._state["found_event"].set()

    page = _PipePage(on_wait=on_wait)
    page.goto = lambda url, **kw: (gotos.append(url),
                                   setattr(page, "url", url))
    monkeypatch.setattr(user_browser, "_ensure_page", lambda: page)
    monkeypatch.setattr(user_browser, "_ctx_alive", lambda: True)
    req = {"kind": "pipeline", "slug": slug,
           "items": [(1, "https://a"), (2, "https://b")]}
    user_browser._run_pipeline(req)
    assert gotos == ["https://a", "https://b"]
    assert any("PDF detectado" in str(s) for s in page.statuses)
    assert user_browser._state["pipeline"]["active"] is False


def test_run_pipeline_skip_advances(slug, monkeypatch):
    _drain_queue()
    gotos = []

    def on_wait(w):
        user_browser._state["skip_event"].set()

    page = _PipePage(on_wait=on_wait)
    page.goto = lambda url, **kw: (gotos.append(url),
                                   setattr(page, "url", url))
    monkeypatch.setattr(user_browser, "_ensure_page", lambda: page)
    monkeypatch.setattr(user_browser, "_ctx_alive", lambda: True)
    user_browser._run_pipeline({"kind": "pipeline", "slug": slug,
                                "items": [(1, "https://a"),
                                          (2, "https://b"),
                                          (3, "https://c")]})
    assert gotos == ["https://a", "https://b", "https://c"]


def test_run_pipeline_aborts_when_browser_closed(slug, monkeypatch):
    _drain_queue()
    gotos = []
    page = _PipePage()
    page.goto = lambda url, **kw: gotos.append(url)
    monkeypatch.setattr(user_browser, "_ensure_page", lambda: page)
    alive = {"v": True}
    monkeypatch.setattr(user_browser, "_ctx_alive",
                        lambda: alive["v"])
    orig_wait = page.wait_for_timeout

    def wait_then_close(ms):
        orig_wait(ms)
        alive["v"] = False
    page.wait_for_timeout = wait_then_close
    user_browser._run_pipeline({"kind": "pipeline", "slug": slug,
                                "items": [(1, "https://a"),
                                          (2, "https://b")]})
    assert gotos == ["https://a"]


def test_pipeline_route(slug, monkeypatch):
    from fastapi.testclient import TestClient
    from lelia.app import create_app
    opened = []
    monkeypatch.setattr(user_browser, "open_pipeline",
                        lambda s: opened.append(s) or 3)
    client = TestClient(create_app())
    r = client.post(f"/projetos/{slug}/browser/pipeline",
                    follow_redirects=False)
    assert r.status_code == 303
    assert opened == [slug]
