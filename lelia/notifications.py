"""Notificações in-app por projeto (sino na barra superior)."""

from datetime import datetime, timezone

from sqlalchemy import func, select

from lelia.storage.db import get_project_session
from lelia.storage.models_project import Notification


def notify(slug: str, kind: str, title: str, body: str = "",
           link: str = "") -> int:
    with get_project_session(slug) as s:
        n = Notification(kind=kind, title=title[:300], body=body,
                         link=link, read=0)
        s.add(n)
        s.commit()
        return n.id


def list_notifications(slug: str, limit: int = 50) -> list[Notification]:
    with get_project_session(slug) as s:
        return list(s.scalars(select(Notification)
                              .order_by(Notification.id.desc()).limit(limit)))


def unread_count(slug: str) -> int:
    with get_project_session(slug) as s:
        return s.scalar(select(func.count(Notification.id)).where(
            Notification.read == 0)) or 0


def mark_read(slug: str, notif_id: int) -> None:
    with get_project_session(slug) as s:
        n = s.get(Notification, notif_id)
        if n:
            n.read = 1
            s.commit()


def mark_all_read(slug: str) -> None:
    with get_project_session(slug) as s:
        for n in s.scalars(select(Notification).where(Notification.read == 0)):
            n.read = 1
        s.commit()
