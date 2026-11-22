import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from lelia.audit.log import record_event
from lelia.config.settings import base_dir, ensure_dirs
from lelia.storage.db import get_global_session, get_project_session
from lelia.storage.models_global import Project


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "projeto"


def _unique_slug(base: str) -> str:
    slug = base
    n = 2
    while get_project(slug) is not None:
        slug = f"{base}-{n}"
        n += 1
    return slug


def _workspace(slug: str) -> Path:
    ws = base_dir() / "projects" / slug
    for sub in ("pdfs", "index", "exports"):
        (ws / sub).mkdir(parents=True, exist_ok=True)
    return ws


def create_project(name: str, description: str = "", origin: str = "user", slug: str | None = None) -> Project:
    ensure_dirs()
    slug = _unique_slug(slug or slugify(name))
    workspace = _workspace(slug)
    with get_global_session() as session:
        project = Project(
            slug=slug,
            name=name,
            description=description,
            status="ativo",
            created_at=datetime.now(timezone.utc),
            workspace=str(workspace),
        )
        session.add(project)
        session.commit()
        session.refresh(project)
    get_project_session(slug).close()  # garante db.sqlite do projeto
    record_event("project.created", entity=f"project:{slug}", operation="create",
                 origin=origin, project_slug=slug, payload={"name": name})
    return project


def list_projects() -> list[Project]:
    with get_global_session() as session:
        return list(session.scalars(select(Project).order_by(Project.created_at.desc())))


def get_project(slug: str) -> Project | None:
    with get_global_session() as session:
        return session.get(Project, slug)


def set_status(slug: str, status: str) -> None:
    assert status in ("ativo", "pausado", "arquivado")
    with get_global_session() as session:
        project = session.get(Project, slug)
        if project:
            project.status = status
            session.commit()
    record_event("project.status", entity=f"project:{slug}", operation="update",
                 project_slug=slug, payload={"status": status})


def update_project(slug: str, name: str | None = None, description: str | None = None) -> None:
    with get_global_session() as session:
        project = session.get(Project, slug)
        if not project:
            return
        if name is not None:
            project.name = name
        if description is not None:
            project.description = description
        session.commit()
    record_event("project.updated", entity=f"project:{slug}", operation="update",
                 project_slug=slug, payload={"name": name, "description": description})


def duplicate_project(slug: str, new_name: str | None = None) -> Project:
    """Duplica configurações (metadados por ora; dados de pesquisa na Fase 2+)."""
    src = get_project(slug)
    if not src:
        raise ValueError(f"Projeto não encontrado: {slug}")
    copy = create_project(new_name or f"{src.name} (cópia)", src.description,
                          slug=_unique_slug(f"{src.slug}-copia"))
    record_event("project.duplicated", entity=f"project:{copy.slug}", operation="create",
                 project_slug=copy.slug, payload={"source": slug})
    return copy


def delete_project(slug: str) -> None:
    """Remove o projeto e seu workspace. Irreversível — exige confirmação na rota."""
    project = get_project(slug)
    if not project:
        raise ValueError(f"Projeto não encontrado: {slug}")
    with get_global_session() as session:
        session.delete(session.get(Project, slug))
        session.commit()
    ws = Path(project.workspace)
    if ws.exists() and ws.parent == base_dir() / "projects":
        shutil.rmtree(ws)
    record_event("project.deleted", entity=f"project:{slug}", operation="delete",
                 payload={"name": project.name})


def count_publications(slug: str) -> int:
    try:
        from lelia.catalog.service import paper_count
        return paper_count(slug)
    except Exception:
        return 0
