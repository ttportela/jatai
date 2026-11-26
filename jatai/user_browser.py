"""Jataí Browser — Chromium visível controlado pelo usuário.

Uma janela do Chromium (Playwright, headless=False) com perfil
persistente compartilhado com a aquisição autenticada
(`~/.jatai/auth-profile`). O usuário navega livremente — inclusive
faz login em portais institucionais uma única vez — e o serviço
monitora downloads e respostas `application/pdf` para anexar o PDF
detectado ao paper que originou a sessão.

A Sync API do Playwright não roda dentro do event loop do FastAPI,
então todo o trabalho de browser acontece numa thread dedicada que
drena uma fila de pedidos (`open_for_paper` / `open_blank`).
"""

import logging
import os
import queue
import re
import signal
import subprocess
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

log = logging.getLogger("jatai.user_browser")

_queue: "queue.Queue[dict]" = queue.Queue()
_state = {
    "thread": None,
    "ctx": None,
    "page": None,
    "target": None,        # (slug, paper_id) do paper de origem
    "current_url": "",
    "error": "",
}
_HANDLED_URLS: set[str] = set()
_MAX_HANDLED = 500
_lock = threading.Lock()


_INIT_JS = r"""
(() => {
  if (window.__jataiUI) return;
  window.__jataiUI = true;
  const host = document.createElement('div');
  host.id = 'jatai-ui-host';
  const root = host.attachShadow({ mode: 'open' });
  root.innerHTML = `
    <style>
      .wrap{position:fixed;bottom:16px;right:16px;z-index:2147483647;
        font:13px system-ui,sans-serif;display:flex;flex-direction:column;
        gap:6px;align-items:flex-end}
      .status{background:#1f2937;color:#fff;padding:6px 10px;border-radius:8px;
        box-shadow:0 2px 8px rgba(0,0,0,.35);max-width:340px;
        word-break:break-word}
      button{background:#2f5fd0;color:#fff;border:0;border-radius:8px;
        padding:8px 14px;font:600 13px system-ui,sans-serif;cursor:pointer;
        box-shadow:0 2px 8px rgba(0,0,0,.35)}
      button:hover{background:#274bb0}
    </style>
    <div class="wrap">
      <div class="status" id="jatai-st">Jataí Browser</div>
      <button id="jatai-skip" type="button">Pular ⏭ próximo</button>
    </div>`;
  const attach = () => {
    if (document.documentElement) {
      document.documentElement.appendChild(host);
    } else {
      setTimeout(attach, 50);
    }
  };
  attach();
  root.getElementById('jatai-skip').addEventListener('click', () => {
    if (window.__jataiSkip) window.__jataiSkip();
  });
  window.__jataiSetStatus = (t) => {
    const el = root.getElementById('jatai-st');
    if (el) el.textContent = String(t);
  };
})();
"""


def _handled(url: str) -> bool:
    with _lock:
        if url in _HANDLED_URLS:
            return True
        _HANDLED_URLS.add(url)
        if len(_HANDLED_URLS) > _MAX_HANDLED:
            _HANDLED_URLS.pop()
        return False


def _profile_dir() -> str:
    from jatai.documents.acquisition.browser import auth_profile_dir
    return str(auth_profile_dir())


def _profile_pids() -> list[int]:
    """PIDs de processos Chrome usando o perfil do Jataí Browser."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", f"user-data-dir={_profile_dir()}"],
            capture_output=True, text=True, timeout=5)
        return [int(x) for x in out.stdout.split()]
    except Exception:  # noqa: BLE001
        return []


def _auth_ctx_alive() -> bool:
    try:
        from jatai.documents.acquisition import browser as _ab
        return _ab._AUTH.get("ctx") is not None
    except Exception:  # noqa: BLE001
        return False


def _kill_orphaned_profile_processes() -> None:
    """Encerra Chrome órfãos que seguram o lock do perfil (ex.: servidor
    anterior morreu e o browser ficou para trás). Não mata se o
    contexto autenticado headless estiver vivo neste processo.
    Espera os processos morrerem; escala para SIGKILL se preciso."""
    if _state["ctx"] is not None or _auth_ctx_alive():
        return
    pids = _profile_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:  # noqa: BLE001
            pass
    deadline = time.time() + 8
    while time.time() < deadline and _profile_pids():
        time.sleep(0.4)
    for pid in _profile_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:  # noqa: BLE001
            pass
    time.sleep(0.5)


def _launch_context():
    """Contexto persistente visível; prefere o Chrome instalado.
    Se o perfil estiver travado por um processo órfão, limpa e tenta
    de novo."""
    from jatai import browser as _bm
    last_exc = None
    for attempt in range(3):
        for channel in ("chrome", None):
            try:
                kw = dict(user_data_dir=_profile_dir(), headless=False,
                          accept_downloads=True, chromium_sandbox=True)
                if channel:
                    kw["channel"] = channel
                return _bm.new_persistent_context(**kw)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        if attempt < 2:
            _kill_orphaned_profile_processes()
            time.sleep(1.0)
    raise last_exc


def _close_ctx(ctx) -> None:
    try:
        if ctx is not None:
            ctx.close()
    except Exception:  # noqa: BLE001
        pass


def _reset_browser() -> None:
    ctx = _state["ctx"]
    _state["ctx"] = None
    _state["page"] = None
    _state["current_url"] = ""
    _close_ctx(ctx)


def _on_ctx_closed(*_) -> None:
    """Contexto já fechado pelo usuário — só limpa as referências."""
    _state["ctx"] = None
    _state["page"] = None
    _state["current_url"] = ""


def _ensure_page():
    page = _state["page"]
    if page is not None:
        try:
            if not page.is_closed():
                return page
        except Exception:  # noqa: BLE001
            pass
        _state["page"] = None
    ctx = _state["ctx"]
    if ctx is not None:
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            _wire_page(page)
            _state["page"] = page
            return page
        except Exception:  # noqa: BLE001
            _reset_browser()
    _state["ctx"] = _launch_context()
    try:
        _state["ctx"].add_init_script(_INIT_JS)
    except Exception:  # noqa: BLE001
        pass
    _state["ctx"].on("close", _on_ctx_closed)
    _state["ctx"].on("page", lambda pg: _wire_page(pg))
    page = _state["ctx"].pages[0] if _state["ctx"].pages \
        else _state["ctx"].new_page()
    _wire_page(page)
    _state["page"] = page
    return page


def _on_skip() -> None:
    ev = _state.get("skip_event")
    if ev is not None:
        ev.set()


def _wire_page(page) -> None:
    page.on("download", _on_download)
    page.on("response", lambda r, p=page: _on_response(r, p))
    try:
        page.expose_function("__jataiSkip", _on_skip)
    except Exception:  # noqa: BLE001
        pass

    def _on_close(*_):
        if _state["page"] is page:
            _state["page"] = None
    page.on("close", _on_close)


def _normalize_url(url: str) -> str:
    if url == "about:blank":
        return url
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _browser_loop() -> None:
    while True:
        try:
            req = _queue.get(timeout=2)
        except queue.Empty:
            _check_open_tabs()
            continue
        if req.get("kind") == "pipeline":
            _run_pipeline(req)
            continue
        slug, paper_id = req.get("slug"), req.get("paper_id")
        try:
            page = _ensure_page()
            _state["target"] = (slug, paper_id)
            url = _normalize_url(req.get("url") or "about:blank")
            _state["current_url"] = url
            try:
                page.goto(url, timeout=45000,
                          wait_until="domcontentloaded")
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "closed" in msg.lower() or "target" in msg.lower():
                    raise
                # PDF direto / viewer: o Chrome aborta o goto ao assumir
                # a navegação — não é erro, segue o monitoramento
                log.info("goto interrompido pelo browser: %s",
                         msg[:120])
            _state["error"] = ""
            _post_goto_meta(page, slug, paper_id)
        except Exception as exc:  # noqa: BLE001
            _reset_browser()
            _state["error"] = str(exc)[:300]
            _notify_error(slug, exc)


def _post_goto_meta(page, slug, paper_id) -> None:
    """Metadado citation_pdf_url (repositórios OA) — mesma verificação
    do baixar_artigo.py após o carregamento da página."""
    if not slug or not paper_id:
        return
    try:
        page.wait_for_timeout(2000)
        meta = page.evaluate(
            "() => { const m = document.querySelector("
            "'meta[name=\"citation_pdf_url\"]');"
            " return m ? m.content : null; }")
    except Exception:  # noqa: BLE001
        return
    if not meta:
        return
    abs_url = urllib.parse.urljoin(page.url, meta)
    if not abs_url.startswith(("http://", "https://")):
        return
    if abs_url in _HANDLED_URLS:
        return
    body = _fetch_pdf_via_context(page, abs_url)
    if body:
        _handled(abs_url)
        _attach(slug, paper_id, body,
                _filename({}, abs_url, "paper.pdf"), abs_url)


def _check_open_tabs() -> None:
    """Fallback periódico: se alguma aba está exibindo um PDF nativo
    (document.contentType == application/pdf), baixa pela sessão."""
    ctx = _state["ctx"]
    target = _state["target"]
    if ctx is None or not target or not target[1]:
        return
    slug, paper_id = target
    for pg in list(getattr(ctx, "pages", []) or []):
        try:
            if pg.is_closed():
                continue
            if pg.evaluate("document.contentType") != "application/pdf":
                continue
            url = pg.url
            if not url.startswith(("http://", "https://"))                     or url in _HANDLED_URLS:
                continue
            body = _fetch_pdf_via_context(pg, url)
            if body:
                _handled(url)
                _attach(slug, paper_id, body,
                        _filename({}, url, "paper.pdf"), url)
        except Exception:  # noqa: BLE001
            continue


def _reset_signals() -> None:
    _state["found_event"] = threading.Event()
    _state["skip_event"] = threading.Event()


def _ctx_alive() -> bool:
    return _state["ctx"] is not None


def _set_status(page, text: str) -> None:
    try:
        page.evaluate(
            "t => window.__jataiSetStatus && window.__jataiSetStatus(t)",
            text)
    except Exception:  # noqa: BLE001
        pass


def _wait_for_outcome(page, total: int, idx: int) -> str:
    """Espera PDF detectado, skip do usuário, novo pedido ou fim do
    browser. Retorna 'found' | 'skip' | 'abort'."""
    ticks = 0
    while True:
        try:
            page.wait_for_timeout(300)  # bombeia eventos do Playwright
        except Exception:  # noqa: BLE001
            return "abort"
        ticks += 1
        if ticks % 20 == 0:
            _check_open_tabs()  # fallback: aba exibindo PDF no viewer
        if _state["found_event"].is_set():
            return "found"
        if _state["skip_event"].is_set():
            return "skip"
        if not _ctx_alive() or not _queue.empty():
            return "abort"


def _run_pipeline(req: dict) -> None:
    """Percorre os artigos sem PDF: abre o link, aguarda detecção do
    PDF (download/viewer/meta) ou skip, e avança. Termina quando o
    usuário fecha o browser ou chega ao fim da fila."""
    slug = req.get("slug")
    items = req.get("items") or []
    total = len(items)
    _state["pipeline"] = {"active": True, "total": total, "slug": slug}
    page = None
    try:
        for idx, (paper_id, url) in enumerate(items):
            try:
                page = _ensure_page()
            except Exception as exc:  # noqa: BLE001
                _state["error"] = str(exc)[:300]
                _notify_error(slug, exc)
                break
            _state["target"] = (slug, paper_id)
            _reset_signals()
            url = _normalize_url(url)
            _state["current_url"] = url
            try:
                page.goto(url, timeout=45000,
                          wait_until="domcontentloaded")
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "closed" in msg.lower() or "target" in msg.lower():
                    raise
                log.info("pipeline goto interrompido: %s", msg[:120])
            _set_status(page, f"Artigo {idx + 1}/{total} — "
                              f"aguardando PDF…")
            outcome = _wait_for_outcome(page, total, idx)
            if outcome == "found":
                _set_status(page, "PDF detectado e salvo ✓ — "
                                  "avançando…")
            elif outcome == "skip":
                _set_status(page, f"Pulando artigo {idx + 1}/{total}…")
            else:
                break
            if not _ctx_alive():
                break
        else:
            pass
        if page is not None and _ctx_alive():
            _set_status(page, "Pipeline concluído — nenhum artigo "
                              "pendente ou browser fechado.")
    finally:
        _state["pipeline"]["active"] = False


def _notify_error(slug, exc) -> None:
    if not slug:
        return
    try:
        from jatai import notifications
        notifications.notify(
            slug, "browser",
            "Jataí Browser: erro ao abrir",
            str(exc)[:300],
            link="/notificacoes")
    except Exception:  # noqa: BLE001
        pass


def _start() -> None:
    with _lock:
        if _state["thread"] is None or not _state["thread"].is_alive():
            t = threading.Thread(target=_browser_loop, daemon=True,
                                 name="jatai-browser")
            _state["thread"] = t
            t.start()


def open_for_paper(slug: str, paper_id: int, url: str) -> None:
    """Abre o Jataí Browser navegando para a URL do paper."""
    _start()
    _queue.put({"slug": slug, "paper_id": paper_id, "url": url})


def open_blank(slug: str) -> None:
    """Abre o Jataí Browser sem destino (ex.: login em portal)."""
    _start()
    _queue.put({"slug": slug, "paper_id": None, "url": "about:blank"})


def open_pipeline(slug: str) -> int:
    """Abre o Jataí Browser em modo pipeline: percorre todos os
    artigos sem PDF (com URL ou DOI), aguardando o usuário encontrar
    cada PDF. Retorna quantos artigos entraram na fila."""
    from jatai.catalog import service as catalog  # noqa: F401
    from jatai.storage.db import get_project_session
    from jatai.storage.models_project import Paper
    from sqlalchemy import select
    with get_project_session(slug) as s:
        papers = list(s.scalars(
            select(Paper).where(Paper.pdf_path == "")
            .order_by(Paper.id)))
    items = []
    for p in papers:
        u = (p.url or "").strip()
        if not u.startswith(("http://", "https://")):
            u = (f"https://doi.org/{p.doi}" if p.doi else "")
        if u.startswith(("http://", "https://")):
            items.append((p.id, u))
    _start()
    _queue.put({"kind": "pipeline", "slug": slug, "items": items})
    return len(items)


def status() -> dict:
    return {
        "running": bool(_state["thread"] and _state["thread"].is_alive()),
        "current_url": _state["current_url"],
        "has_target": _state["target"] is not None,
        "error": _state["error"],
    }


# ---- detecção de PDF -------------------------------------------------

def _filename(headers: dict, url: str, fallback: str = "paper.pdf") -> str:
    """Nome do arquivo: Content-Disposition → path da URL → fallback
    (mesma heurística do baixar_artigo.py)."""
    name = ""
    cd = (headers or {}).get("content-disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?",
                  cd, re.I)
    if m:
        name = urllib.parse.unquote(m.group(1))
    if not name:
        name = Path(urllib.parse.urlparse(url).path).name or fallback
    name = re.sub(r"[\\/*?:\"<>|\r\n]+", "_", name).strip(" ._")
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name[:180]


def _on_download(download) -> None:
    try:
        slug, paper_id = _state["target"] or (None, None)
        if not slug or not paper_id:
            return
        url = getattr(download, "url", "") or ""
        if url and url in _HANDLED_URLS:
            return
        with tempfile.NamedTemporaryFile(
                suffix=".pdf", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            download.save_as(str(tmp_path))
            data = tmp_path.read_bytes()
            if url and _handled(url):
                return
            _attach(slug, paper_id, data,
                    _filename({}, url or "",
                              getattr(download, "suggested_filename", None)
                              or "download.pdf"),
                    url or "download")
        finally:
            tmp_path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        log.exception("user_browser: falha ao tratar download")


def _fetch_pdf_via_context(page, url: str) -> bytes | None:
    """Rebaixa a URL via httpx carregando os cookies do perfil
    persistente (preserva login institucional). Usado quando o viewer
    interno do Chrome consome a resposta e o Playwright não entrega
    o body."""
    import httpx
    try:
        headers = {"User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36")}
        try:
            cookies = page.context.cookies(url)
            if cookies:
                headers["Cookie"] = "; ".join(
                    f"{c['name']}={c['value']}" for c in cookies)
        except Exception:  # noqa: BLE001
            pass
        with httpx.stream("GET", url, headers=headers, timeout=90,
                          follow_redirects=True) as resp:
            if resp.status_code != 200:
                return None
            chunks, size = [], 0
            for chunk in resp.iter_bytes(65536):
                chunks.append(chunk)
                size += len(chunk)
                if size > 50 * 1024 * 1024:
                    return None
            body = b"".join(chunks)
        return body if body.lstrip().startswith(b"%PDF") else None
    except Exception:  # noqa: BLE001
        log.exception("user_browser: refetch PDF falhou para %s", url)
        return None


def _on_response(response, page=None) -> None:
    """Captura PDFs servidos inline. O viewer interno do Chrome busca
    os bytes via requisição chrome-extension que o Playwright não
    expõe — nesse caso rebaixamos a URL pela sessão do contexto."""
    try:
        url = response.url
        if not url.startswith(("http://", "https://")):
            return
        if getattr(response, "status", 200) != 200:
            return
        ctype = (response.headers.get("content-type") or "").lower()
        if "application/pdf" not in ctype:
            return
        if url in _HANDLED_URLS:
            return
        slug, paper_id = _state["target"] or (None, None)
        if not slug or not paper_id:
            return
        body = None
        try:
            body = response.body()
        except Exception:  # noqa: BLE001
            pass
        if not body or not body.lstrip().startswith(b"%PDF"):
            body = (_fetch_pdf_via_context(page, url)
                    if page is not None else None)
        if not body or not body.lstrip().startswith(b"%PDF"):
            return
        if _handled(url):
            return
        _attach(slug, paper_id, body,
                _filename(response.headers, url), url)
    except Exception:  # noqa: BLE001
        log.exception("user_browser: falha ao tratar resposta PDF")


def _attach(slug: str, paper_id: int, data: bytes,
            filename: str, source_url: str) -> None:
    """Valida e anexa um PDF detectado ao paper de origem."""
    from jatai import notifications
    from jatai.catalog import service as catalog
    from jatai.documents.acquisition.validate import validate_pdf_file

    paper = catalog.get_paper(slug, paper_id)
    if paper is None:
        return
    if paper.pdf_path:
        return  # já tem PDF — detecções seguintes são ignoradas
    with tempfile.NamedTemporaryFile(suffix=".pdf",
                                     delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        res = validate_pdf_file(tmp_path, expected_title=paper.title)
        diverged = "diverge" in res["reason"]
        if res["ok"] or diverged:
            catalog.attach_pdf(slug, paper_id, filename, data,
                               origin="browser")
            ev = _state.get("found_event")
            if ev is not None:
                ev.set()
            if diverged:
                notifications.notify(
                    slug, "pdf_browser",
                    f"PDF anexado ao paper #{paper_id} — verificar conteúdo",
                    f"{paper.title[:150]}\nO texto do PDF diverge do "
                    f"título do artigo ({res.get('title_match') or 0:.0f}% "
                    f"de similaridade). Fonte: {source_url[:200]}",
                    link=f"/projetos/{slug}/publicacoes/{paper_id}")
            else:
                notifications.notify(
                    slug, "pdf_browser",
                    f"PDF anexado ao paper #{paper_id}",
                    f"{paper.title[:200]}\nFonte: {source_url[:200]}",
                    link=f"/projetos/{slug}/publicacoes/{paper_id}")
        else:
            notifications.notify(
                slug, "pdf_browser",
                f"PDF inválido ignorado (paper #{paper_id})",
                f"{paper.title[:150]}\nMotivo: {res['reason']}\n"
                f"Fonte: {source_url[:200]}",
                link=f"/projetos/{slug}/publicacoes/{paper_id}")
    finally:
        tmp_path.unlink(missing_ok=True)


__all__ = ["open_for_paper", "open_blank", "open_pipeline",
           "status"]
