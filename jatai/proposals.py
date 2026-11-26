"""CRUD e fluxo de revisão das Propostas de pesquisa.

As propostas são geradas pelo agente (task `proposals`) ou criadas
manualmente; o pesquisador revisa, refina, prioriza e converte em
novo projeto ou em questões de pesquisa do projeto atual.
"""

import json
from datetime import datetime, timezone

from sqlalchemy import func, select

from jatai.audit.log import record_event
from jatai.storage.db import get_project_session
from jatai.storage.models_project import Proposal, ResearchQuestion

STATUSES = ("proposto", "em_analise", "priorizado", "convertido", "descartado")
KINDS = ("lacuna", "tendencia", "limitacao", "oportunidade")
PRIORITIES = ("baixa", "media", "alta")

_PRIORITY_ORDER = {"alta": 0, "media": 1, "baixa": 2}
_STATUS_ORDER = {"priorizado": 0, "em_analise": 1, "proposto": 2,
                 "convertido": 3, "descartado": 4}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def list_proposals(slug: str, status: str = "", kind: str = "") -> list[Proposal]:
    with get_project_session(slug) as s:
        q = select(Proposal)
        if status:
            q = q.where(Proposal.status == status)
        if kind:
            q = q.where(Proposal.kind == kind)
        return list(s.scalars(q.order_by(
            Proposal.status, Proposal.priority, Proposal.id.desc())))


def counts(slug: str) -> dict:
    """Contagem por status para os filtros da tela."""
    with get_project_session(slug) as s:
        rows = s.execute(select(Proposal.status, func.count())
                         .group_by(Proposal.status)).all()
    out = {st: 0 for st in STATUSES}
    out[""] = sum(n for _, n in rows)
    for st, n in rows:
        out[st] = n
    return out


def kind_counts(slug: str) -> dict:
    with get_project_session(slug) as s:
        rows = s.execute(select(Proposal.kind, func.count())
                         .group_by(Proposal.kind)).all()
    out = {k: 0 for k in KINDS}
    for k, n in rows:
        out[k] = n
    return out


def get_proposal(slug: str, proposal_id: int) -> Proposal | None:
    with get_project_session(slug) as s:
        p = s.get(Proposal, proposal_id)
        if p is None:
            return None
        s.expunge(p)
        return p


def create_proposal(slug: str, *, title: str, kind: str = "oportunidade",
                    summary: str = "", rationale: str = "",
                    questions: list | None = None, priority: str = "media",
                    notes: str = "", origin: str = "manual") -> int:
    if kind not in KINDS:
        kind = "oportunidade"
    if priority not in PRIORITIES:
        priority = "media"
    with get_project_session(slug) as s:
        p = Proposal(kind=kind, title=title.strip()[:300],
                     summary=summary, rationale=rationale,
                     questions=questions or [], priority=priority,
                     notes=notes, origin=origin, status="proposto")
        s.add(p)
        s.commit()
        pid = p.id
    record_event("proposal.created", entity=f"proposal:{pid}",
                 operation="create", project_slug=slug,
                 payload={"title": title, "kind": kind})
    return pid


def update_proposal(slug: str, proposal_id: int, **fields) -> bool:
    allowed = {"title", "kind", "summary", "rationale", "questions",
               "priority", "notes", "status", "converted_to"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if "kind" in fields and fields["kind"] not in KINDS:
        fields["kind"] = "oportunidade"
    if "priority" in fields and fields["priority"] not in PRIORITIES:
        fields["priority"] = "media"
    if "status" in fields and fields["status"] not in STATUSES:
        fields.pop("status")
    if "questions" in fields and not isinstance(fields["questions"], list):
        fields["questions"] = [str(fields["questions"])]
    with get_project_session(slug) as s:
        p = s.get(Proposal, proposal_id)
        if p is None:
            return False
        for k, v in fields.items():
            setattr(p, k, v)
        p.updated_at = _utcnow()
        s.commit()
    record_event("proposal.updated", entity=f"proposal:{proposal_id}",
                 operation="update", project_slug=slug,
                 payload={"fields": sorted(fields)})
    return True


def set_status(slug: str, proposal_id: int, status: str) -> bool:
    if status not in STATUSES:
        return False
    return update_proposal(slug, proposal_id, status=status)


def delete_proposal(slug: str, proposal_id: int) -> bool:
    with get_project_session(slug) as s:
        p = s.get(Proposal, proposal_id)
        if p is None:
            return False
        s.delete(p)
        s.commit()
    record_event("proposal.deleted", entity=f"proposal:{proposal_id}",
                 operation="delete", project_slug=slug)
    return True


def convert_proposal(slug: str, proposal_id: int, destino: str) -> str:
    """Converte a proposta em novo projeto ('projeto') ou em questões de
    pesquisa do projeto atual ('questao'). Retorna descrição do destino
    ou '' em caso de falha."""
    p = get_proposal(slug, proposal_id)
    if p is None or p.status == "convertido":
        return ""
    if destino == "projeto":
        from jatai.projects import service as projects
        desc = p.summary or p.rationale
        if p.questions:
            desc = (desc + "\n\nPerguntas sugeridas:\n" +
                    "\n".join(f"- {q}" for q in p.questions)).strip()
        project = projects.create_project(p.title, desc,
                                           origin="proposal")
        update_proposal(slug, proposal_id, status="convertido",
                        converted_to=f"projeto:{project.slug}")
        record_event("proposal.converted", entity=f"proposal:{proposal_id}",
                     operation="update", project_slug=slug,
                     payload={"destino": "projeto", "slug": project.slug})
        return f"projeto:{project.slug}"
    if destino == "questao":
        from jatai.research import service as research
        qs = [q for q in (p.questions or []) if str(q).strip()] or [p.title]
        first_id = None
        with get_project_session(slug) as s:
            existing = list(s.scalars(select(ResearchQuestion)))
            pos = max((r.position or 0 for r in existing), default=0)
            codes = [r.code for r in existing]
        for i, qtext in enumerate(qs):
            code = _next_code(codes)
            codes.append(code)
            rid = research.save_question(
                slug, None, code, str(qtext).strip()[:500],
                answer_type="texto", required=False,
                position=pos + i + 1)
            if first_id is None:
                first_id = rid
        update_proposal(slug, proposal_id, status="convertido",
                        converted_to=f"questao:{first_id}")
        record_event("proposal.converted", entity=f"proposal:{proposal_id}",
                     operation="update", project_slug=slug,
                     payload={"destino": "questao", "questions": len(qs)})
        return f"questao:{first_id}"
    return ""


def _next_code(codes: list[str]) -> str:
    """Próximo código RQn livre (RQ1, RQ2...)."""
    used = set()
    for c in codes:
        m = c.strip().upper().lstrip("RQ")
        if m.isdigit():
            used.add(int(m))
    n = 1
    while n in used:
        n += 1
    return f"RQ{n}"
