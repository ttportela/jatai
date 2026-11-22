import json
from datetime import datetime, timezone
from typing import Any

from lelia.storage.db import get_global_session
from lelia.storage.models_global import Event


def record_event(
    type_: str,
    *,
    entity: str = "",
    operation: str = "",
    origin: str = "user",
    project_slug: str = "",
    payload: dict[str, Any] | None = None,
) -> Event:
    """Append-only: nunca atualiza nem remove eventos."""
    with get_global_session() as session:
        event = Event(
            ts=datetime.now(timezone.utc),
            type=type_,
            entity=entity,
            operation=operation,
            origin=origin,
            project_slug=project_slug,
            payload=json.dumps(payload or {}, ensure_ascii=False, default=str),
        )
        session.add(event)
        session.commit()
        session.refresh(event)
        return event


def list_events(project_slug: str | None = None, limit: int = 200) -> list[Event]:
    from sqlalchemy import select

    with get_global_session() as session:
        stmt = select(Event).order_by(Event.ts.desc(), Event.id.desc()).limit(limit)
        if project_slug:
            stmt = stmt.where(Event.project_slug == project_slug)
        return list(session.scalars(stmt))


def get_event(event_id: int) -> Event | None:
    from sqlalchemy import select

    with get_global_session() as session:
        return session.scalar(select(Event).where(Event.id == event_id))
