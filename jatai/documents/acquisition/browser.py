"""Download de PDFs via Chromium: fallback para desafios JS/bot dos
publishers e sessão autenticada persistente (login institucional)."""

from pathlib import Path

from jatai.config.settings import base_dir, ensure_dirs
from jatai.documents.acquisition.validate import (
    MAX_BYTES,
    is_pdf_bytes,
    looks_like_pdf,
    validate_pdf_file,
)

_BA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " \
      "AppleWebKit/537.36 (KHTML, like Gecko) " \
      "Chrome/126.0.0.0 Safari/537.36"


def _pdf_links_from_page(page) -> list[str]:
    """Extrai links de PDF de uma landing page (meta citation_pdf_url,
    hrefs)."""
    try:
        links = page.evaluate(
            """() => {
                const out = [];
                const m = document.querySelector(
                    'meta[name="citation_pdf_url"]');
                if (m && m.content) out.push(m.content);
                document.querySelectorAll('a[href]').forEach(a => {
                    const h = a.href || "";
                    if (/\\.pdf($|\\?)/i.test(h) || /\\/pdf\\//i.test(h))
                        out.push(h);
                });
                return out.slice(0, 10);
            }""")
        return [u for u in (links or []) if str(u).startswith("http")]
    except Exception:  # noqa: BLE001
        return []


def _save_response_pdf(resp, dest: Path) -> bool:
    """Salva bytes de uma resposta cujo content-type é PDF."""
    try:
        data = resp.body()
        if len(data) > MAX_BYTES or not is_pdf_bytes(
                data, "application/pdf"):
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True
    except Exception:  # noqa: BLE001
        return False


def download_pdf_context(context, url: str, dest: Path,
                         timeout_ms: int = 60000,
                         expected_title: str | None = None) -> bool:
    """Tenta baixar um PDF usando um BrowserContext do Playwright.

    Cobre três casos: (1) evento de download do Chromium; (2) resposta
    com content-type PDF aberta no viewer interno; (3) landing page
    open-access com links para PDF (até 4 candidatos).
    """
    try:
        page = context.new_page()
    except Exception:  # noqa: BLE001
        return False
    try:
        queue = [url]
        seen: set[str] = set()
        attempts = 0
        while queue and attempts < 4:
            cand = queue.pop(0)
            if cand in seen:
                continue
            seen.add(cand)
            attempts += 1
            resp = None
            try:
                with page.expect_download(timeout=timeout_ms) as info:
                    try:
                        resp = page.goto(cand, timeout=timeout_ms,
                                         wait_until="domcontentloaded")
                    except Exception:  # noqa: BLE001
                        pass  # o download interrompe a navegação
                info.value.save_as(str(dest))
            except Exception:  # noqa: BLE001
                if resp is not None and "pdf" in (
                        resp.headers.get("content-type") or "").lower():
                    if _save_response_pdf(resp, dest) and \
                            validate_pdf_file(dest, expected_title)["ok"]:
                        return True
                    dest.unlink(missing_ok=True)
                for u in _pdf_links_from_page(page):
                    if u not in seen:
                        queue.append(u)
                continue
            if validate_pdf_file(dest, expected_title)["ok"]:
                return True
            dest.unlink(missing_ok=True)
        return False
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass


def download_pdf_browser(browser, url: str, dest: Path,
                         timeout_ms: int = 60000,
                         expected_title: str | None = None) -> bool:
    """Wrapper sobre download_pdf_context para um Browser (não
    persistente). Mantido para compatibilidade."""
    try:
        context = browser.new_context(accept_downloads=True,
                                      user_agent=_BA)
    except Exception:  # noqa: BLE001
        return False
    try:
        return download_pdf_context(context, url, dest, timeout_ms,
                                    expected_title)
    finally:
        try:
            context.close()
        except Exception:  # noqa: BLE001
            pass


# ---------- Sessão autenticada persistente ----------

_AUTH = {"ctx": None}


def auth_profile_dir() -> Path:
    """Perfil Chromium persistente para login institucional."""
    d = Path(base_dir()) / "auth-profile"
    ensure_dirs()
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_auth_context():
    """Retorna (pw, context) persistente e autenticado, criando sob
    demanda a partir do playwright compartilhado da thread.
    O login é feito uma vez via `jatai pdf-login`."""
    from jatai import browser as _browser
    if _AUTH["ctx"] is not None:
        return _browser.get_playwright(), _AUTH["ctx"]
    try:
        ctx = _browser.new_persistent_context(
            user_data_dir=str(auth_profile_dir()),
            headless=True, accept_downloads=True, user_agent=_BA)
    except Exception:  # noqa: BLE001
        return None, None
    _AUTH["ctx"] = ctx
    return _browser.get_playwright(), ctx


def close_auth_context() -> None:
    """Fecha o contexto autenticado (o pw compartilhado permanece —
    é encerrado por stop_playwright() ao fim do run)."""
    ctx = _AUTH.get("ctx")
    _AUTH["ctx"] = None
    if ctx is not None:
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass


def open_login_window(start_url: str = "https://www.google.com") -> None:
    """Abre uma janela visível do Chromium no perfil autenticado para o
    usuário fazer login manual na(s) plataforma(s) institucional(is).
    Bloqueia até a janela ser fechada."""
    from jatai import browser as _browser
    pw = _browser.get_playwright()
    try:
        ctx = _browser.new_persistent_context(
            user_data_dir=str(auth_profile_dir()),
            headless=False, accept_downloads=True)
        try:
            page = ctx.new_page()
            page.goto(start_url, wait_until="domcontentloaded",
                      timeout=60000)
            page.wait_for_event("close", timeout=0)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
    finally:
        _browser.stop_playwright()


__all__ = ["download_pdf_browser", "download_pdf_context",
           "get_auth_context", "close_auth_context", "open_login_window",
           "auth_profile_dir", "looks_like_pdf"]
