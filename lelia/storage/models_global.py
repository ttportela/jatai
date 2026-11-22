from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from lelia.storage.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "project"

    slug: Mapped[str] = mapped_column(String(120), primary_key=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="ativo")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    workspace: Mapped[str] = mapped_column(String(500), nullable=False)


class Event(Base):
    """Trilha append-only de auditoria (nunca atualizar/apagar)."""

    __tablename__ = "event"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    type: Mapped[str] = mapped_column(String(80), index=True)
    entity: Mapped[str] = mapped_column(String(200), default="")
    operation: Mapped[str] = mapped_column(String(40), default="")
    origin: Mapped[str] = mapped_column(String(20), default="user")  # user | agent
    project_slug: Mapped[str] = mapped_column(String(120), default="", index=True)
    payload: Mapped[str] = mapped_column(Text, default="{}")
