"""Worker de agendamento: verifica periodicamente os agendamentos
ativos dos projetos e dispara novas execuções do agente."""

import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from jatai.agents import config_service, runner
from jatai.projects import service as projects
from jatai.storage.db import get_project_session
from jatai.storage.models_project import Schedule

_STARTED = False
_LOCK = threading.Lock()


def _due(schedule, now: datetime) -> bool:
    if not schedule or not schedule.enabled:
        return False
    last = schedule.last_run_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if schedule.frequency == "diaria":
        return last is None or last < now - timedelta(hours=23)
    if schedule.frequency == "semanal":
        return last is None or last < now - timedelta(days=6, hours=1)
    return False


def _tick() -> None:
    now = datetime.now(timezone.utc)
    for project in projects.list_projects():
        if project.status != "ativo":
            continue
        slug = project.slug
        try:
            sched = config_service.get_schedule(slug)
            if not _due(sched, now):
                continue
            agents = [a for a in config_service.list_agents(slug) if a.enabled]
            if not agents:
                continue
            for agent in agents:
                run_id = runner.create_run(slug, agent_id=agent.id)
                runner.control_run(slug, run_id, "iniciar")
            with get_project_session(slug) as s:
                row = s.scalar(select(Schedule).order_by(Schedule.id.desc()).limit(1))
                if row:
                    row.last_run_at = now
                    s.commit()
        except Exception:
            continue


def _loop(interval: int = 60) -> None:
    while True:
        try:
            _tick()
        except Exception:
            pass
        time.sleep(interval)


def start_scheduler(interval: int = 60) -> None:
    global _STARTED
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
    threading.Thread(target=_loop, args=(interval,), daemon=True,
                     name="jatai-scheduler").start()
