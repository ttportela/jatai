"""Playwright compartilhado por thread.

A Sync API do Playwright não permite duas instâncias concorrentes na
mesma thread (levanta "using Sync API inside the asyncio loop"). Todas
as tarefas do agente rodam na mesma thread de execução, então o
playwright, browsers e contextos devem derivar de uma única instância
thread-local criada sob demanda e encerrada ao fim do run.
"""

import threading

_local = threading.local()


def get_playwright():
    """Retorna a instância sync_playwright da thread atual, criando-a
    sob demanda. Não chamar .stop() diretamente — use stop_playwright()."""
    pw = getattr(_local, "pw", None)
    if pw is None:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        _local.pw = pw
    return pw


def launch_browser(headless: bool = True):
    """Lança um Chromium (channel=chrome com fallback para chromium)."""
    pw = get_playwright()
    try:
        return pw.chromium.launch(channel="chrome", headless=headless)
    except Exception:  # noqa: BLE001
        return pw.chromium.launch(headless=headless)


def new_persistent_context(user_data_dir: str, headless: bool = True,
                           **kwargs):
    """BrowserContext persistente (perfil em disco) a partir do pw
    compartilhado."""
    return get_playwright().chromium.launch_persistent_context(
        user_data_dir=user_data_dir, headless=headless, **kwargs)


def stop_playwright() -> None:
    """Encerra a instância compartilhada da thread, se houver."""
    pw = getattr(_local, "pw", None)
    _local.pw = None
    if pw is not None:
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass


__all__ = ["get_playwright", "launch_browser",
           "new_persistent_context", "stop_playwright"]
