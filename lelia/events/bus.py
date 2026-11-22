"""Event Bus em processo + Observer para notificações em tempo real.

O agente (runner) publica eventos de execução; a interface assina o
tópico do projeto via SSE. Cada assinante recebe uma fila própria e
um histórico limitado é mantido para contexto ao conectar.
"""

import json
import queue
import threading
import time
from collections import deque

_lock = threading.Lock()
_subscribers: dict[str, set[queue.Queue]] = {}
_history: dict[str, deque] = {}
HISTORY_SIZE = 100


def subscribe(slug: str) -> queue.Queue:
    """Registra um observador e retorna sua fila de eventos."""
    q: queue.Queue = queue.Queue(maxsize=500)
    with _lock:
        _subscribers.setdefault(slug, set()).add(q)
    return q


def unsubscribe(slug: str, q: queue.Queue) -> None:
    with _lock:
        subs = _subscribers.get(slug)
        if subs:
            subs.discard(q)
            if not subs:
                _subscribers.pop(slug, None)


def history(slug: str) -> list[dict]:
    with _lock:
        return list(_history.get(slug, ()))


def publish(slug: str, event_type: str, **payload) -> dict:
    """Publica um evento para todos os observadores do projeto.

    Eventos com run_id também são persistidos na tabela run_event,
    formando o histórico de ações de cada execução."""
    event = {"type": event_type, "ts": time.time(), **payload}
    line = json.dumps(event, ensure_ascii=False)
    run_id = payload.get("run_id")
    if run_id:
        try:
            from lelia.storage.db import get_project_session
            from lelia.storage.models_project import RunEvent
            with get_project_session(slug) as s:
                s.add(RunEvent(
                    run_id=int(run_id),
                    iteration=int(payload.get("iteration") or 0),
                    type=event_type, payload=line))
                s.commit()
        except Exception:  # noqa: BLE001 — persistência nunca trava o agente
            pass
    with _lock:
        _history.setdefault(slug, deque(maxlen=HISTORY_SIZE)).append(event)
        subs = list(_subscribers.get(slug, ()))
    for q in subs:
        try:
            q.put_nowait(event)
        except queue.Full:
            pass  # observador lento: descarta para não travar o agente
    return event
