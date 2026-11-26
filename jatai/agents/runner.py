"""Orquestrador de execuções do agente: Run → Iteration → tasks.

Executa as tarefas habilitadas em sequência por iteração, até convergir
(0 artigos novos) ou atingir os limites configurados. O controle de
pausa/cancelamento é cooperativo entre tarefas.
"""

import json
import threading
from datetime import datetime, timezone

from sqlalchemy import func, select

from jatai.agents import config_service, llm
from jatai.audit.log import record_event
from jatai.events import bus
from jatai.catalog import service as catalog
from jatai.research import service as research
from jatai.storage.db import get_project_session
from jatai.storage.models_project import (
    AgentTaskConfig,
    Iteration,
    Notification,
    Paper,
    Query,
    Run,
    RunEvent,
)

ACTIVE_STATUSES = {"pendente", "executando", "pausado"}
RUNNING = {}  # slug -> threading.Event de cancelamento


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _agent_name(slug: str, agent_id: int | None) -> str:
    if not agent_id:
        return ""
    for a in config_service.list_agents(slug):
        if a.id == agent_id:
            return a.name
    return ""


def _run_label(slug: str, run) -> str:
    """Rótulo exibido no monitor/histórico: execuções avulsas ('single')
    mostram 'Tarefa · #paperId'; demais, o nome do agente."""
    agent_id = getattr(run, "agent_id", None)
    try:
        cfg = json.loads(getattr(run, "config_snapshot", "") or "{}")
        single = cfg.get("single") if isinstance(cfg, dict) else None
        if single:
            return _single_task(slug, agent_id, single).name
    except Exception:  # noqa: BLE001 — rótulo nunca deve quebrar o run
        pass
    return _agent_name(slug, agent_id)


# ---------- Ciclo de vida do Run ----------

def create_run(slug: str, max_iterations: int = 1, max_papers: int = 0,
               max_cost_usd: float | None = None,
               agent_id: int | None = None,
               single: dict | None = None) -> int:
    if agent_id is None:
        agent_id = config_service.default_agent_id(slug)
    snapshot = {"max_iterations": max_iterations, "max_papers": max_papers,
                "max_cost_usd": max_cost_usd, "agent_id": agent_id}
    if single:
        snapshot["single"] = single
    with get_project_session(slug) as s:
        r = Run(status="pendente", agent_id=agent_id,
                config_snapshot=json.dumps(snapshot))
        s.add(r)
        s.commit()
        rid = r.id
    record_event("run.created", entity=f"run:{rid}", operation="create",
                 project_slug=slug, payload=snapshot)
    return rid


# Tarefas que podem ser executadas avulsas sobre uma única publicação
# (botão "Nova análise" no detalhe do artigo).
SINGLE_TASK_TYPES = {"deep_analysis", "classification", "concept_extraction",
                     "query_expansion", "snowballing"}


def create_analysis_run(slug: str, paper_id: int, task_type: str,
                        model_ref: str = "") -> int:
    """Cria uma run de iteração única que executa `task_type` somente para
    `paper_id` (params.paper_id/model_ref orientam a task)."""
    return create_run(
        slug, max_iterations=1, max_papers=0, max_cost_usd=None,
        single={"task_type": task_type, "paper_id": paper_id,
                "model_ref": model_ref or ""})


def _single_task(slug: str, agent_id: int | None, single: dict):
    """AgentTaskConfig sintético para execução avulsa: copia a config real
    da tarefa (se existir) e injeta paper_id/model_ref nos params."""
    from types import SimpleNamespace

    ttype = str(single.get("task_type") or "")
    info = config_service.task_type_info(ttype) or {}
    base = config_service.get_task_config(slug, ttype, agent_id=agent_id)
    params = {}
    if base is not None:
        try:
            params = json.loads(base.params or "{}")
        except json.JSONDecodeError:
            params = {}
    params["paper_id"] = int(single.get("paper_id") or 0)
    if single.get("model_ref"):
        params["model_ref"] = str(single["model_ref"])
    pid = int(single.get("paper_id") or 0)
    base_name = (base.name if base and base.name else
                 info.get("name") or ttype)
    return SimpleNamespace(
        id=base.id if base else None,
        task_type=ttype,
        agent_id=agent_id,
        name=f"{base_name} · #{pid}" if pid else base_name,
        enabled=True,
        llm_model=(base.llm_model if base else ""),
        prompt_template=(base.prompt_template if base else ""),
        prompt_version=(base.prompt_version if base else ""),
        max_tokens=(base.max_tokens if base else 0),
        max_attempts=(base.max_attempts if base else 3),
        review_policy=(base.review_policy if base else "automatica"),
        conditions=(base.conditions if base else "{}"),
        params=json.dumps(params),
        position=0)


ACTIVE_RUN_STATUSES = ("pendente", "executando", "pausado")
FINISHED_RUN_STATUSES = ("concluida", "falha", "cancelada")


def list_runs(slug: str, limit: int = 50, offset: int = 0,
              statuses: list[str] | tuple[str, ...] | None = None) -> list[Run]:
    with get_project_session(slug) as s:
        q = select(Run).order_by(Run.id.desc())
        if statuses:
            q = q.where(Run.status.in_(statuses))
        return list(s.scalars(q.limit(limit).offset(offset)))


def count_runs(slug: str, statuses: list[str] | tuple[str, ...] | None = None) -> int:
    with get_project_session(slug) as s:
        q = select(func.count(Run.id))
        if statuses:
            q = q.where(Run.status.in_(statuses))
        return s.scalar(q) or 0


def get_run(slug: str, run_id: int) -> Run | None:
    with get_project_session(slug) as s:
        return s.get(Run, run_id)


def run_iterations(slug: str, run_id: int) -> list[Iteration]:
    with get_project_session(slug) as s:
        return list(s.scalars(
            select(Iteration).where(Iteration.run_id == run_id)
            .order_by(Iteration.number)))


EVENT_LABELS = {
    "run.started": "execução iniciada",
    "run.status": "status",
    "run.error": "erro fatal",
    "iteration.started": "iteração iniciada",
    "iteration.finished": "iteração concluída",
    "task.started": "tarefa iniciada",
    "task.finished": "tarefa concluída",
    "task.error": "erro na tarefa",
    "task.skipped": "tarefa ignorada",
    "task.progress": "progresso",
    "query.started": "consulta iniciada",
    "query.progress": "busca em andamento",
    "query.retry": "nova tentativa",
    "query.finished": "consulta concluída",
    "llm.in": "chamada LLM — entrada",
    "llm.out": "chamada LLM — saída",
}


def run_events(slug: str, run_id: int, limit: int = 2000) -> list[dict]:
    """Histórico persistido de eventos do run (mais antigo → mais novo)."""
    with get_project_session(slug) as s:
        rows = list(s.scalars(
            select(RunEvent).where(RunEvent.run_id == run_id)
            .order_by(RunEvent.id.desc()).limit(limit)))
    out = []
    for r in reversed(rows):
        try:
            ev = json.loads(r.payload or "{}")
        except ValueError:
            ev = {}
        ev["id"] = r.id
        ev["iteration"] = r.iteration
        ev["label"] = EVENT_LABELS.get(r.type, r.type)
        ev["created_at"] = r.created_at
        out.append(ev)
    return out


def run_event(slug: str, run_id: int, event_id: int) -> dict | None:
    """Evento persistido completo (payload integral) para o modal de detalhe."""
    with get_project_session(slug) as s:
        r = s.get(RunEvent, event_id)
        if not r or r.run_id != run_id:
            return None
        try:
            ev = json.loads(r.payload or "{}")
        except ValueError:
            ev = {}
    ev["id"] = r.id
    ev["iteration"] = r.iteration
    ev["type"] = r.type
    ev["label"] = EVENT_LABELS.get(r.type, r.type)
    ev["created_at"] = r.created_at.isoformat() if r.created_at else None
    return ev


def describe_run_event(ev: dict) -> str:
    """Mensagem detalhada de um evento do run (sem truncamento)."""
    t = ev.get("type", "")
    if t == "run.started":
        return str(ev.get("agent") or "")
    if t == "run.status":
        return f"{ev.get('status', '')} · {ev.get('agent') or ''}".strip(" ·")
    if t == "run.error":
        return str(ev.get("error") or "")
    if t == "iteration.started":
        return f"iteração {ev.get('number')}/{ev.get('max_iterations')}"
    if t == "iteration.finished":
        st = ev.get("stats") or {}
        parts = [f"{st.get('new_papers', 0)} artigos novos",
                 f"{st.get('queries', 0)} consultas",
                 f"{st.get('new_terms', 0)} termos novos"]
        if st.get("duplicates"):
            parts.append(f"{st['duplicates']} duplicados")
        if st.get("screened"):
            parts.append(f"{st['screened']} triados")
        errs = st.get("errors") or []
        if errs:
            parts.append(f"{len(errs)} erro(s): " + "; ".join(
                str(e)[:200] for e in errs[:3]))
        return " · ".join(parts)
    if t == "task.started":
        return str(ev.get("task_name") or ev.get("task_type") or "")
    if t in ("task.finished", "task.error", "task.skipped"):
        name = str(ev.get("task_name") or ev.get("task_type") or "")
        extra = str(ev.get("error") or ev.get("reason") or "")
        return f"{name}: {extra}" if extra else name
    if t == "task.progress":
        return str(ev.get("message") or "")
    if t == "query.started":
        src = ", ".join(ev.get("sources") or [])
        return f"#{ev.get('query_id')} · {ev.get('expression') or ''}" + \
               (f" · {src}" if src else "")
    if t == "query.progress":
        total = ev.get("total")
        return (f"{ev.get('source') or ''} · pág. {ev.get('page')}"
                + (f" · {ev.get('fetched')}/{total} registros" if total
                   else f" · {ev.get('fetched')} registros"))
    if t == "query.retry":
        return (f"{ev.get('source') or ''} · tentativa "
                f"{ev.get('attempt')} — aguardando {ev.get('wait_s')}s")
    if t == "query.finished":
        errs = ev.get("errors") or []
        return (f"{ev.get('expression') or ''} · +{ev.get('new', 0)} novos"
                f" · +{ev.get('duplicates', 0)} dup"
                + (f" · {len(errs)} erro(s)" if errs else "")
                + f" · {(ev.get('duration_ms') or 0) / 1000:.1f}s")
    if t == "llm.in":
        head = " · ".join(p for p in (
            f"#{ev['paper_id']}" if ev.get("paper_id") else "",
            str(ev.get("model") or ""), str(ev.get("task") or "")) if p)
        prompt = str(ev.get("prompt") or "")
        prev = " ".join(prompt.split())[:400]
        return head + (f" — prompt: {prev}{'…' if len(prompt) > 400 else ''}"
                       if prompt else "")
    if t == "llm.out":
        head = " · ".join(p for p in (
            f"#{ev['paper_id']}" if ev.get("paper_id") else "",
            str(ev.get("model") or ""), str(ev.get("task") or "")) if p)
        meta = (f"{ev.get('tokens_in', 0)}→{ev.get('tokens_out', 0)} tok"
                f" · US$ {ev.get('cost_usd', 0):.4f}")
        err = str(ev.get("error") or "")
        if err:
            return f"{head} · {meta} — ERRO: {err}"
        out = " ".join(str(ev.get("response") or "").split())[:400]
        return head + f" · {meta}" + (f" — {out}{'…' if len(str(ev.get('response') or '')) > 400 else ''}" if out else "")
    return ""


def run_totals(slug: str, run_id: int) -> dict:
    """Totalizadores de uma execução a partir dos eventos persistidos.

    Agrega todos os contadores numéricos publicados nos stats das
    iterações (artigos novos, triados, analisados, classificados,
    adquiridos, consultas, termos, duplicados...)."""
    events = run_events(slug, run_id)
    totals = {"events": len(events), "tasks_ok": 0, "tasks_error": 0,
              "iterations": 0, "errors": 0, "stats": {}}
    for ev in events:
        t = ev.get("type")
        if t == "task.finished":
            totals["tasks_ok"] += 1
        elif t == "task.error":
            totals["tasks_error"] += 1
            totals["errors"] += 1
        elif t == "run.error":
            totals["errors"] += 1
        elif t == "iteration.finished":
            totals["iterations"] += 1
            st = ev.get("stats") or {}
            for k, v in st.items():
                if k == "errors":
                    totals["errors"] += len(v or [])
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    totals["stats"][k] = totals["stats"].get(k, 0) + v
    return totals


def set_run_status(slug: str, run_id: int, status: str) -> None:
    with get_project_session(slug) as s:
        r = s.get(Run, run_id)
        if r:
            r.status = status
            if status in ("concluida", "falha", "cancelada") and not r.finished_at:
                r.finished_at = utcnow()
            s.commit()
    record_event("run.status", entity=f"run:{run_id}", operation="status",
                 project_slug=slug, payload={"status": status})
    with get_project_session(slug) as s:
        r = s.get(Run, run_id)
        agent_id = r.agent_id if r else None
        label = _run_label(slug, r) if r else ""
    bus.publish(slug, "run.status", run_id=run_id, status=status,
                agent=label or _agent_name(slug, agent_id), agent_id=agent_id)


def control_run(slug: str, run_id: int, action: str) -> str:
    """iniciar | pausar | cancelar | reexecutar"""
    run = get_run(slug, run_id)
    if run is None:
        return "erro"
    if action == "iniciar" and run.status == "pendente":
        threading.Thread(target=_execute_run, args=(slug, run_id), daemon=True).start()
        return "ok"
    if action == "iniciar" and run.status == "pausado":
        # a thread original continua viva aguardando em _should_stop;
        # basta voltar o status — criar outra thread duplicaria a execução
        set_run_status(slug, run_id, "executando")
        return "ok"
    if action == "pausar" and run.status == "executando":
        set_run_status(slug, run_id, "pausado")
        return "ok"
    if action == "cancelar" and run.status in ACTIVE_STATUSES:
        set_run_status(slug, run_id, "cancelada")
        return "ok"
    if action == "reexecutar":
        snap = json.loads(run.config_snapshot or "{}")
        snap.pop("agent_id", None)
        new_id = create_run(slug, agent_id=run.agent_id, **snap)
        threading.Thread(target=_execute_run, args=(slug, new_id), daemon=True).start()
        return "ok"
    return "ignorado"


def _should_stop(slug: str, run_id: int) -> str | None:
    """Retorna motivo de parada ou None para continuar."""
    run = get_run(slug, run_id)
    if run is None or run.status == "cancelada":
        return "cancelada"
    if run.status == "pausado":
        while True:
            import time
            time.sleep(2)
            run = get_run(slug, run_id)
            if run is None or run.status == "cancelada":
                return "cancelada"
            if run.status == "executando":
                return None
    return None


def _execute_run(slug: str, run_id: int) -> None:
    try:
        run = get_run(slug, run_id)
        if run is None or run.status == "cancelada":
            return  # cancelada antes do início não deve ser retomada
        set_run_status(slug, run_id, "executando")
        bus.publish(slug, "run.started", run_id=run_id,
                    agent=_run_label(slug, run), agent_id=run.agent_id)
        cfg = json.loads(run.config_snapshot or "{}")
        max_iter = int(cfg.get("max_iterations", 1))
        # None = sem limite; valor numérico (inclusive 0) = teto exato em USD
        _mc = cfg.get("max_cost_usd")
        max_cost = float(_mc) if _mc is not None else None

        single = cfg.get("single")
        if single:
            enabled = [_single_task(slug, run.agent_id, single)]
        else:
            tasks = config_service.list_task_configs(slug, agent_id=run.agent_id)
            enabled = [t for t in tasks if t.enabled]
        total_new = 0
        task_totals: dict[int, dict] = {}   # task.id -> {name, type, delta, errors}
        skipped_tasks: list[str] = []

        for number in range(1, max_iter + 1):
            reason = _should_stop(slug, run_id)
            if reason:
                if reason == "cancelada":
                    set_run_status(slug, run_id, "cancelada")
                return
            iter_id = _create_iteration(slug, run_id, number)
            bus.publish(slug, "iteration.started", run_id=run_id, iteration=iter_id,
                        number=number, max_iterations=max_iter)
            stats = {"queries": 0, "new_papers": 0, "new_terms": 0, "errors": []}
            for task in enabled:
                reason = _should_stop(slug, run_id)
                if reason:
                    break
                # O limite de custo vale apenas para tarefas que usam LLM;
                # tarefas gratuitas (search, dedup...) sempre executam.
                info = config_service.task_type_info(task.task_type) or {}
                if (info.get("uses_llm") and max_cost is not None
                        and llm.llm_usage(slug)["cost_usd"] >= max_cost):
                    reason = f"limite de custo atingido (${max_cost:.2f})"
                    bus.publish(slug, "task.skipped", run_id=run_id, iteration=iter_id,
                                task_type=task.task_type, task_name=task.name,
                                reason=reason)
                    skipped_tasks.append(task.name or task.task_type)
                    continue
                try:
                    summary = _run_task(slug, run_id, iter_id, task, stats)
                    if summary is not None:
                        acc = task_totals.setdefault(task.id, {
                            "name": summary["name"], "type": summary["task_type"],
                            "delta": {}, "errors": 0})
                        for k, v in summary["delta"].items():
                            acc["delta"][k] = acc["delta"].get(k, 0) + v
                        acc["errors"] += summary["errors"]
                except Exception as exc:  # noqa: BLE001
                    stats["errors"].append(f"{task.task_type}: {exc}")
                    bus.publish(slug, "task.error", run_id=run_id, iteration=iter_id,
                                task_type=task.task_type, task_name=task.name,
                                error=str(exc)[:300])
            _finish_iteration(slug, iter_id, stats)
            total_new += stats["new_papers"]
            bus.publish(slug, "iteration.finished", run_id=run_id, iteration=iter_id,
                        number=number, stats=dict(stats))
            if stats["new_papers"] == 0:
                break  # convergência: nenhum artigo novo
        final = get_run(slug, run_id)
        if final and final.status == "executando":
            set_run_status(slug, run_id, "concluida")
            from jatai import notifications
            agent = _run_label(slug, final) or _agent_name(slug, run.agent_id)
            lines = [f"{total_new} novo(s) artigo(s) descoberto(s) nesta execução."]
            for acc in task_totals.values():
                lines.append(_task_done_line(acc["type"], acc["name"],
                                             acc["delta"], acc["errors"]))
            for name in dict.fromkeys(skipped_tasks):
                lines.append(f"• {name}: ignorada (limite de custo)")
            notifications.notify(
                slug, "run",
                f"Execução #{run_id} concluída" + (f" — agente {agent}" if agent else ""),
                "\n".join(lines),
                link=f"/projetos/{slug}/agente/execucoes?run={run_id}")
        elif final and final.status == "falha":
            from jatai import notifications
            notifications.notify(slug, "run", f"Execução #{run_id} falhou",
                                 "O agente encerrou com erro. Veja o monitor de execuções.",
                                 link=f"/projetos/{slug}/agente/execucoes?run={run_id}")
    except Exception as exc:  # noqa: BLE001
        set_run_status(slug, run_id, "falha")
        record_event("run.error", entity=f"run:{run_id}", operation="error",
                     project_slug=slug, payload={"error": str(exc)})
        bus.publish(slug, "run.error", run_id=run_id, error=str(exc)[:300])
        try:
            from jatai import notifications
            notifications.notify(slug, "run", f"Execução #{run_id} falhou",
                                 str(exc)[:300],
                                 link=f"/projetos/{slug}/agente/execucoes?run={run_id}")
        except Exception:
            pass
    finally:
        # iterações interrompidas (cancelamento/parada no meio) não ficam
        # eternamente como "executando"
        try:
            with get_project_session(slug) as s:
                for it in s.scalars(select(Iteration).where(
                        Iteration.run_id == run_id,
                        Iteration.status == "executando")):
                    it.status = "interrompida"
                s.commit()
        except Exception:  # noqa: BLE001
            pass
        try:
            from jatai import browser as _pw
            _pw.stop_playwright()
        except Exception:  # noqa: BLE001
            pass


def _create_iteration(slug: str, run_id: int, number: int) -> int:
    with get_project_session(slug) as s:
        it = Iteration(run_id=run_id, number=number, status="executando")
        s.add(it)
        s.commit()
        return it.id


def _finish_iteration(slug: str, iter_id: int, stats: dict) -> None:
    with get_project_session(slug) as s:
        it = s.get(Iteration, iter_id)
        if it:
            it.queries_count = stats["queries"]
            it.new_papers = stats["new_papers"]
            it.new_terms = stats["new_terms"]
            it.status = "concluida" if not stats["errors"] else "falha"
            s.commit()
    if stats["errors"]:
        record_event("iteration.errors", entity=f"iteration:{iter_id}", operation="error",
                     project_slug=slug, payload={"errors": stats["errors"]})


# ---------- Tasks ----------

def _task_done_line(task_type: str, name: str, delta: dict, errors: int) -> str:
    """Linha de conclusão de uma tarefa para a notificação de fim de run."""
    def n(k: str) -> int:
        return int(delta.get(k, 0))
    parts: list[str] = []
    if task_type == "query_builder":
        parts.append(f"{n('queries_created')} consulta(s) gerada(s)")
    elif task_type == "query_expansion":
        parts.append(f"{n('terms_suggested')} termo(s) sugerido(s)")
        if n("queries_created"):
            parts.append(f"{n('queries_created')} consulta(s) criada(s)")
    elif task_type == "search":
        parts.append(f"{n('queries')} consulta(s) executada(s)")
        parts.append(f"{n('results')} resultado(s)")
        parts.append(f"{n('new_papers')} artigo(s) novo(s)")
        if n("duplicates"):
            parts.append(f"{n('duplicates')} duplicado(s)")
    elif task_type == "screener":
        parts.append(f"{n('screened')} artigo(s) triado(s)")
    elif task_type == "deep_screener":
        parts.append(f"{n('deep_screened')} artigo(s) triado(s) em profundidade")
    elif task_type == "metadata_enrichment":
        parts.append(f"{n('enriched')} artigo(s) enriquecidos")
    elif task_type == "acquisition":
        parts.append(f"{n('acquired')} PDF(s) adquirido(s)")
    elif task_type == "dedup":
        parts.append(f"{n('groups')} grupo(s) de possíveis duplicatas")
        if n("dup_alerts"):
            parts.append(f"{n('dup_alerts')} alerta(s) de PDF duplicado")
    elif task_type == "deep_analysis":
        parts.append(f"{n('analyzed')} artigo(s) analisados em profundidade")
    elif task_type == "classification":
        parts.append(f"{n('classified')} artigo(s) classificados")
    elif task_type == "concept_extraction":
        parts.append(f"{n('concepts')} conceito(s) extraído(s)")
    elif n("new_terms"):
        parts.append(f"{n('new_terms')} item(ns) novo(s)")
    if errors:
        parts.append(f"{errors} erro(s)")
    detail = " · ".join(parts) if parts else "concluída sem alterações"
    return f"• {name}: {detail}"


def _task_enabled(task: AgentTaskConfig, stats: dict) -> bool:
    return True


def _model_for(slug: str, task) -> str:
    with get_project_session(slug) as s:
        t = s.get(AgentTaskConfig, task.id) if getattr(task, "id", None) else None
        if t is None:
            t = s.scalar(select(AgentTaskConfig).where(
                AgentTaskConfig.task_type == task.task_type,
                AgentTaskConfig.agent_id == getattr(task, "agent_id", None)))
        if t is not None:
            return llm.task_model_ref(s, t)
        return llm.default_model_ref(s)


def _task_params(task: AgentTaskConfig) -> dict:
    try:
        return json.loads(task.params or "{}")
    except json.JSONDecodeError:
        return {}


def _llm_timeout(task: AgentTaskConfig) -> float | None:
    """Timeout por chamada LLM (s) configurado na tarefa; None = padrão (300s).

    Lê `timeout_s` dos params; se ausente, usa o default declarado em
    TASK_TYPES para o tipo de tarefa.
    """
    val = _task_params(task).get("timeout_s")
    if val:
        return float(val)
    info = config_service.task_type_info(task.task_type) or {}
    for f in info.get("fields", []):
        if f.get("key") == "timeout_s" and f.get("default"):
            return float(f["default"])
    return None


def _run_task(slug: str, run_id: int, iter_id: int, task: AgentTaskConfig, stats: dict) -> dict | None:
    """Executa a tarefa e retorna {task_type, name, delta, errors} com as
    métricas produzidas nesta execução (para o resumo da notificação)."""
    handler = TASK_HANDLERS.get(task.task_type)
    if handler is None:
        bus.publish(slug, "task.skipped", run_id=run_id, iteration=iter_id,
                    task_type=task.task_type, task_name=task.name,
                    reason="sem handler implementado")
        return None
    bus.publish(slug, "task.started", run_id=run_id, iteration=iter_id,
                task_type=task.task_type, task_name=task.name or task.task_type)
    before = {k: v for k, v in stats.items()
              if isinstance(v, (int, float)) and not isinstance(v, bool)}
    err_before = len(stats.get("errors", []))
    t0 = utcnow()
    handler(slug, run_id, iter_id, task, stats)
    bus.publish(slug, "task.finished", run_id=run_id, iteration=iter_id,
                task_type=task.task_type, task_name=task.name or task.task_type,
                duration_ms=int((utcnow() - t0).total_seconds() * 1000),
                stats=dict(stats))
    delta = {k: stats[k] - before.get(k, 0)
             for k in stats
             if isinstance(stats[k], (int, float)) and not isinstance(stats[k], bool)}
    return {"task_type": task.task_type, "name": task.name or task.task_type,
            "delta": {k: v for k, v in delta.items() if v},
            "errors": len(stats.get("errors", [])) - err_before}


TASK_HANDLERS = {}


def task(name):
    def deco(fn):
        TASK_HANDLERS[name] = fn
        return fn
    return deco


def _clean_query_expression(expr: str) -> str:
    """Remove prefixos de campo/variáveis inválidos para DBLP/OpenAlex."""
    import re
    expr = re.sub(r"\bp\d+\s*:\s*", "", expr)          # p1:, p2: etc.
    expr = re.sub(r"\b(?:title|author|venue|year|doi|abstract|keyword)s?\s*:\s*",
                  "", expr, flags=re.IGNORECASE)
    expr = re.sub(r"\s{2,}", " ", expr).strip()
    return expr


@task("query_builder")
def _query_builder(slug, run_id, iter_id, task, stats):
    """Gera consultas determinísticas a partir da configuração da pesquisa.

    Constrói uma QueryDefinition lógica (AND entre áreas, OR entre
    palavras-chave) e a traduz por adaptador de fonte — sem LLM.
    """
    try:
        _params = json.loads(task.params or "{}")
    except json.JSONDecodeError:
        _params = {}
    max_q = max(1, int(_params.get("max_queries", 5) or 5))
    sources = [x.strip() for x in str(_params.get("sources", "") or "").split(",") if x.strip()]
    from jatai.search import service as search_service
    result = search_service.generate(slug, sources=sources or None,
                                     limit=max_q, origin="agent")
    stats["queries"] += result["created"]
    stats["new_terms"] += result["created"]
    stats["queries_created"] = stats.get("queries_created", 0) + result["created"]


@task("search")
def _search(slug, run_id, iter_id, task, stats):
    """Executa consultas pendentes/aprovadas e habilitadas nas fontes.

    O teto de artigos por consulta vem do snapshot da run (max_papers);
    0/em branco = sem teto (busca todos os resultados dentro do período
    de cobertura configurado no projeto).
    """
    run = get_run(slug, run_id)
    try:
        snap = json.loads(run.config_snapshot or "{}") if run else {}
    except json.JSONDecodeError:
        snap = {}
    cap = int(snap.get("max_papers", 0) or 0)
    max_results = cap if cap > 0 else 10000
    # Toda consulta habilitada é executada a cada run — duplicatas são
    # contabilizadas como "dup" na ingestão, não como novos artigos.
    pending = [q for q in research.list_queries(slug) if q.enabled]

    def _interrupted() -> bool:
        r = get_run(slug, run_id)
        return r is None or r.status in ("cancelada", "pausado")

    for q in pending:
        reason = _should_stop(slug, run_id)
        if reason:
            break
        result = catalog.execute_query(slug, q.id, max_results=max_results,
                                       run_id=run_id, iteration=iter_id,
                                       should_stop=_interrupted)
        stats["queries"] += 1
        stats["new_papers"] += result.get("new", 0)
        stats["results"] = stats.get("results", 0) + sum(
            int(e.get("results", 0) or 0) for e in result.get("executions", []))
        stats["duplicates"] = stats.get("duplicates", 0) + result.get("duplicates", 0)
        for err in result.get("errors", []):
            stats["errors"].append(err)


@task("screener")
def _screener(slug, run_id, iter_id, task, stats):
    """Triagem título+resumo: avaliação por critério via LLM estruturado
    (Pydantic) + regras determinísticas. Uma chamada por artigo."""
    from jatai.agents import screening
    from jatai.storage.models_project import PaperAnalysis

    _params = _task_params(task)
    batch_size = max(1, int(_params.get("batch_size", 5) or 5))
    max_papers = int(_params.get("max_papers", 0) or 0) or None
    min_conf = float(_params.get("min_confidence", 0.6) or 0.6)
    llm_timeout = _llm_timeout(task)

    criteria = screening.screening_criteria(research.list_criteria(slug))
    if not criteria:
        return
    questions = research.list_questions(slug)
    model_ref = _model_for(slug, task)
    model_label = llm.model_label(slug, model_ref)

    done = 0
    failed_ids: set[int] = set()  # papers que falharam não são re-tentados na mesma execução
    while True:
        if _should_stop(slug, run_id):
            break
        if max_papers and done >= max_papers:
            break
        with get_project_session(slug) as s:
            pending_q = select(Paper).where(Paper.state == "novo")
            if failed_ids:
                pending_q = pending_q.where(~Paper.id.in_(failed_ids))
            pending_q = pending_q.order_by(Paper.id)
            if max_papers:
                pending_q = pending_q.limit(min(batch_size, max_papers - done))
            else:
                pending_q = pending_q.limit(batch_size)
            papers = list(s.scalars(pending_q))
            count_q = select(func.count()).select_from(Paper) \
                .where(Paper.state == "novo")
            if failed_ids:
                count_q = count_q.where(~Paper.id.in_(failed_ids))
            total_pending = s.scalar(count_q) or 0
        if not papers:
            break
        remaining = (max_papers - done) if max_papers else total_pending
        total = done + min(total_pending, remaining)  # posição global na fila

        for paper in papers:
            if _should_stop(slug, run_id):
                break
            if max_papers and done >= max_papers:
                break
            try:
                prompt, tpl_name, tpl_ver = screening.build_prompt(
                    slug, task, paper, criteria, questions)
                curto = (f"{paper.title[:55]}{'…' if len(paper.title) > 55 else ''}")
                bus.publish(slug, "task.progress", run_id=run_id, iteration=iter_id,
                            task_type=task.task_type,
                            task_name=task.name or task.task_type,
                            message=(f"{done + 1}/{total} · #{paper.id} · {curto}"
                                     f" — avaliando ({len(criteria)} critérios)…"))
                result = llm.chat_structured(
                    slug, model_ref=model_ref, task=task.task_type,
                    messages=[{"role": "user", "content": prompt}],
                    response_model=screening.ScreeningResult,
                    max_tokens=task.max_tokens or 2048,
                    prompt_template=tpl_name, prompt_version=tpl_ver,
                    timeout=llm_timeout,
                    run_id=run_id, iteration=iter_id, paper_id=paper.id)
                verdict, reasons = screening.apply_rules(result, criteria, min_conf)
                screening.persist(slug, paper.id, criteria, result, verdict,
                                  reasons, iter_id, model=model_label)
                stats["screened"] = stats.get("screened", 0) + 1
                done += 1
                bus.publish(slug, "task.progress", run_id=run_id, iteration=iter_id,
                            task_type=task.task_type,
                            task_name=task.name or task.task_type,
                            message=(f"{done}/{total} · #{paper.id} · "
                                     f"{paper.title[:55]}{'…' if len(paper.title) > 55 else ''}"
                                     f" — {verdict}"))
            except Exception as exc:  # noqa: BLE001
                stats["errors"].append(f"screener paper {paper.id}: {exc}")
                failed_ids.add(paper.id)
                with get_project_session(slug) as s:
                    s.add(PaperAnalysis(paper_id=paper.id, kind="triagem_erro",
                                        result=json.dumps({"error": str(exc)[:500]})))
                    s.commit()
                done += 1
                bus.publish(slug, "task.progress", run_id=run_id, iteration=iter_id,
                            task_type=task.task_type,
                            task_name=task.name or task.task_type,
                            message=(f"{done}/{total} · #{paper.id} — "
                                     f"erro: {str(exc)[:80]}"))


@task("metadata_enrichment")
def _metadata_enrichment(slug, run_id, iter_id, task, stats):
    """Busca abstract/keywords via Chromium para artigos incompletos."""
    try:
        _params = json.loads(task.params or "{}")
    except json.JSONDecodeError:
        _params = {}
    # 0 ou vazio = sem limite (processa todos os artigos incompletos)
    limit = int(_params.get("max_papers", 0) or 0) or None
    from jatai import enrichment

    def _progress(idx, total, pid, title, status, found):
        curto = f"#{pid} · " + title[:55] + ("…" if len(title) > 55 else "")
        if status == "erro":
            msg = f"{idx}/{total} · {curto} — erro ao acessar"
        elif status == "sem_url":
            msg = f"{idx}/{total} · {curto} — sem URL/DOI"
        elif status == "vazio":
            msg = f"{idx}/{total} · {curto} — sem dados encontrados"
        else:
            msg = f"{idx}/{total} · {curto} — {'+'.join(found) or '—'}"
        bus.publish(slug, "task.progress", run_id=run_id, iteration=iter_id,
                    task_type=task.task_type, task_name=task.name or task.task_type,
                    message=msg)

    result = enrichment.enrich_missing(
        slug, limit=limit, on_progress=_progress,
        should_stop=lambda: _should_stop(slug, run_id) is not None)
    stats["enriched"] = stats.get("enriched", 0) + result["updated"]
    stats["new_terms"] += result["updated"]


@task("query_expansion")
def _query_expansion(slug, run_id, iter_id, task, stats):
    """Extrai termos frequentes dos artigos incluídos e propõe novas consultas.
    Com params.paper_id (análise avulsa), usa apenas esse artigo."""
    try:
        _params = json.loads(task.params or "{}")
    except json.JSONDecodeError:
        _params = {}
    single_pid = int(_params.get("paper_id", 0) or 0)
    with get_project_session(slug) as s:
        if single_pid:
            papers = list(s.scalars(select(Paper).where(Paper.id == single_pid)))
        else:
            papers = list(s.scalars(select(Paper).where(Paper.state == "incluido")
                                    .order_by(Paper.id.desc()).limit(20)))
    if len(papers) < (1 if single_pid else 3):
        return
    terms = []
    for p in papers:
        terms.extend(t.strip() for t in (p.keywords or "").split(",") if t.strip())
    if not terms:
        return
    prompt = (
        "Com base nos termos abaixo extraídos de artigos incluídos numa revisão, "
        "sugira até 3 novas palavras-chave ou subtemas para expandir a busca.\n\n"
        f"Termos: {', '.join(sorted(set(terms))[:50])}\n\n"
        'Responda APENAS com JSON: {"terms": ["..."], "queries": [{"source": "dblp", "expression": "..."}]}'
    )
    try:
        data = llm.chat_json(slug,
                             model_ref=_params.get("model_ref") or _model_for(slug, task),
                             task=task.task_type,
                             messages=[{"role": "user", "content": prompt}],
                             max_tokens=task.max_tokens or 1024,
                             timeout=_llm_timeout(task),
                             run_id=run_id, iteration=iter_id)
    except Exception:  # noqa: BLE001
        return
    n_terms = len(data.get("terms", [])) if isinstance(data, dict) else 0
    stats["terms_suggested"] = stats.get("terms_suggested", 0) + n_terms
    stats["new_terms"] = stats.get("new_terms", 0) + n_terms
    for item in (data.get("queries", []) if isinstance(data, dict) else [])[:3]:
        expr = str(item.get("expression", "")).strip()
        if expr:
            research.create_query(slug, expression=expr,
                                  source=str(item.get("source", "dblp")),
                                  origin="expanded", status="pendente")
            stats["queries"] += 1
            stats["queries_created"] = stats.get("queries_created", 0) + 1


# ---------- Fase 5: tasks de texto completo, classificação e conceitos ----------

def _truncate_head_tail(text: str, max_chars: int) -> str:
    """Corta texto longo preservando início (intro/métodos) e fim
    (discussão, limitações, ameaças à validade) — as seções finais são
    essenciais para campos como 'Validade' da classificação.
    max_chars=0 desativa o corte (texto completo)."""
    if not max_chars or len(text) <= max_chars:
        return text
    head = int(max_chars * 0.6)
    tail = max_chars - head
    return (text[:head]
            + "\n\n[... trechos intermediários do artigo omitidos ...]\n\n"
            + text[-tail:])


def _paper_full_text(slug: str, paper: Paper, max_chars: int = 48000) -> str:
    """Extrai e cacheia o texto do PDF em <workspace>/extracted/<id>.txt.
    Textos longos são cortados preservando início e fim."""
    from pathlib import Path

    from jatai.documents import pdf as pdfdoc

    ws = catalog.projects_workspace(slug)
    out = Path(ws) / "extracted" / f"{paper.id}.txt"
    if out.exists():
        return _truncate_head_tail(
            out.read_text(encoding="utf-8", errors="replace"), max_chars)
    try:
        txt = pdfdoc.extract_text(paper.pdf_path, max_pages=120)
    except Exception:  # noqa: BLE001
        return ""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(txt, encoding="utf-8")
    return _truncate_head_tail(txt, max_chars)


def _has_analysis(slug: str, paper_id: int, kind: str) -> bool:
    kinds = [kind] if not kind.endswith("_erro") else [kind]
    if kind == "profunda":
        kinds = ["profunda", "profunda_erro"]
    elif kind == "classificacao":
        kinds = ["classificacao", "classificacao_erro"]
    with get_project_session(slug) as s:
        from jatai.storage.models_project import PaperAnalysis
        return s.scalar(select(func.count(PaperAnalysis.id)).where(
            PaperAnalysis.paper_id == paper_id,
            PaperAnalysis.kind.in_(kinds))) > 0


@task("acquisition")
def _acquisition(slug, run_id, iter_id, task, stats):
    """Pipeline de aquisição de PDFs: providers OA por DOI, fallback
    por metadados, download httpx/Chromium e sessão autenticada
    opcional. Papers já com pdf_path nunca são reprocessados.
    max_pdfs: 0 ou vazio = sem limite. Progresso por artigo no monitor.
    """
    try:
        _params = json.loads(task.params or "{}")
    except json.JSONDecodeError:
        _params = {}
    max_pdfs = int(_params.get("max_pdfs", 0) or 0) or None
    sources = str(_params.get("sources", "") or "").strip() or None
    authenticated = str(_params.get("authenticated", "")).lower() in \
        ("1", "true", "on", "sim", "yes")
    from jatai.documents import acquisition as acq
    from sqlalchemy import and_, or_

    ws = catalog.projects_workspace(slug)
    with get_project_session(slug) as s:
        # elegível: sem pdf e com referência (DOI ou URL de PDF).
        # estados triados com qualquer DOI/URL; papers "novo" só quando
        # a URL já é um PDF direto — evita baixar não triados.
        q = (select(Paper).where(
            or_(Paper.pdf_path == "", Paper.pdf_path.is_(None)),
            or_(Paper.state.in_(["incluido", "talvez", "pendente"]),
                and_(Paper.state == "novo", Paper.url.like("%pdf%"))),
            or_(and_(Paper.doi.isnot(None), Paper.doi != ""),
                Paper.url.like("%pdf%")))
            .order_by(Paper.id))
        if max_pdfs:
            q = q.limit(max_pdfs)
        papers = [(p.id, p.title) for p in s.scalars(q)]

    # Chromium sob demanda: publishers com desafio JS (PMC, Springer)
    # só entregam PDF a um browser real; abre só se o httpx falhar
    _browser = {"pw": None, "browser": None}

    def _browser_factory():
        if _browser["browser"] is None:
            from jatai.enrichment import _launch
            _browser["pw"], _browser["browser"] = _launch()
        return _browser["browser"]

    acquired = errors = 0
    try:
        for idx, (pid, title) in enumerate(papers, 1):
            if _should_stop(slug, run_id):
                break
            curto = f"#{pid} · " + title[:55] + ("…" if len(title) > 55 else "")
            try:
                path = acq.acquire_for_paper(slug, pid, ws, sources=sources,
                                             browser_factory=_browser_factory,
                                             authenticated=authenticated)
            except Exception as exc:  # noqa: BLE001
                errors += 1
                bus.publish(slug, "task.progress", run_id=run_id, iteration=iter_id,
                            task_type=task.task_type, task_name=task.name or task.task_type,
                            message=f"{idx}/{len(papers)} · {curto} — erro: {str(exc)[:80]}")
                continue
            if path:
                acquired += 1
                msg = f"{idx}/{len(papers)} · {curto} — PDF baixado"
            else:
                msg = f"{idx}/{len(papers)} · {curto} — sem PDF open-access"
            bus.publish(slug, "task.progress", run_id=run_id, iteration=iter_id,
                        task_type=task.task_type, task_name=task.name or task.task_type,
                        message=msg)
    finally:
        if _browser["browser"] is not None:
            try:
                _browser["browser"].close()
            except Exception:  # noqa: BLE001
                pass
        if authenticated:
            acq.close_auth_context()
    stats["acquired"] = stats.get("acquired", 0) + acquired
    stats["new_terms"] += acquired


def _paper_keys(p: Paper) -> set[str]:
    """Chaves de deduplicação: DOI normalizado e URL normalizada.

    DOI extraído de urls doi.org/ também vira chave 'doi:'.
    """
    import re
    from jatai.dedup.normalize import normalize_doi

    keys = set()
    doi = normalize_doi(p.doi) if p.doi else ""
    if doi:
        keys.add(f"doi:{doi}")
    url = (p.url or "").strip().lower()
    if url:
        m = re.search(r"doi\.org/(10\.\S+)", url)
        if m:
            keys.add(f"doi:{normalize_doi(m.group(1).rstrip('.,;)'))}")
        u = re.sub(r"^https?://", "", url)
        u = re.sub(r"^www\.", "", u).split("#")[0].rstrip("/")
        if u:
            keys.add(f"url:{u}")
    return keys


def _pdf_hash(path: str) -> str:
    import hashlib
    from pathlib import Path
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _title_score(a: str, b: str) -> float:
    """Similaridade 0-100 entre títulos normalizados (rapidfuzz)."""
    from jatai.dedup.normalize import normalize_title
    from rapidfuzz import fuzz
    na, nb = normalize_title(a or ""), normalize_title(b or "")
    if not na or not nb:
        return 0.0
    return float(max(fuzz.ratio(na, nb), fuzz.token_sort_ratio(na, nb)))


@task("dedup")
def _dedup(slug, run_id, iter_id, task, stats):
    """Registra grupos de possíveis duplicatas para revisão humana.

    Não mescla automaticamente: une papers que compartilham DOI/URL ou
    têm títulos muito similares (limiar `title_threshold`) e grava cada
    grupo na tabela `dedup_group` (status pendente) para o usuário
    avaliar na aba Deduplicação. PDFs com hash idêntico continuam gerando
    notificação."""
    import json as _json
    from jatai import notifications
    from jatai.storage.models_project import DedupGroup

    params = _task_params(task)
    try:
        threshold = float(params.get("title_threshold", 92) or 92)
    except (TypeError, ValueError):
        threshold = 92.0

    with get_project_session(slug) as s:
        papers = list(s.scalars(select(Paper).order_by(Paper.id)))

        # 1) calcula/preenche hash dos PDFs (uma vez por arquivo)
        for p in papers:
            if p.pdf_path and not p.pdf_hash:
                p.pdf_hash = _pdf_hash(p.pdf_path)

        # 2) união por chaves compartilhadas (DOI ou URL) e título similar
        parent = {p.id: p.id for p in papers}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

        why: dict[int, set[str]] = {}

        def mark(pid, reason):
            why.setdefault(pid, set()).add(reason)

        keymap: dict[str, int] = {}
        for p in papers:
            for k in _paper_keys(p):
                if k in keymap:
                    union(p.id, keymap[k])
                    mark(p.id, "identificador")
                    mark(keymap[k], "identificador")
                else:
                    keymap[k] = p.id

        # similaridade de título (par a par; O(n²) com rapidfuzz é viável
        # para o volume de uma revisão)
        titled = [(p.id, p.title or "") for p in papers if (p.title or "").strip()]
        best_pair: dict[int, float] = {}
        for i in range(len(titled)):
            pid_i, ti = titled[i]
            for j in range(i + 1, len(titled)):
                pid_j, tj = titled[j]
                if find(pid_i) == find(pid_j):
                    continue  # já unidos por chave exata
                score = _title_score(ti, tj)
                if score >= threshold:
                    union(pid_i, pid_j)
                    mark(pid_i, "título")
                    mark(pid_j, "título")
                    for pid in (pid_i, pid_j):
                        best_pair[pid] = max(best_pair.get(pid, 0.0), score)

        groups: dict[int, list[Paper]] = {}
        for p in papers:
            groups.setdefault(find(p.id), []).append(p)

        # 3) substitui os grupos pendentes pelos encontrados nesta execução;
        #    grupos já resolvidos (mesclado/ignorado) com os mesmos papers
        #    não voltam a aparecer
        resolved = {frozenset(json.loads(g.paper_ids or "[]"))
                    for g in s.scalars(select(DedupGroup)
                                       .where(DedupGroup.status != "pendente"))}
        for old in list(s.scalars(select(DedupGroup)
                                  .where(DedupGroup.status == "pendente"))):
            s.delete(old)

        created = 0
        for members in groups.values():
            if len(members) < 2:
                continue
            ids = sorted(p.id for p in members)
            if frozenset(ids) in resolved:
                continue
            reasons = sorted({r for pid in ids for r in why.get(pid, set())})
            score = max((best_pair.get(pid, 0.0) for pid in ids), default=0.0)
            s.add(DedupGroup(
                paper_ids=_json.dumps(ids),
                reason=" + ".join(reasons) or "possível duplicata",
                score=round(score, 1),
                status="pendente"))
            created += 1

        # 4) PDFs com hash idêntico entre papers distintos -> notifica
        by_hash: dict[str, list[Paper]] = {}
        for p in papers:
            if p.pdf_hash and p.pdf_path:
                by_hash.setdefault(p.pdf_hash, []).append(p)
        already = [n.body for n in s.scalars(
            select(Notification).where(Notification.kind == "pdf_dup"))]
        notified = 0
        for h, members in by_hash.items():
            if len({p.id for p in members}) < 2:
                continue
            desc = ", ".join(f"#{p.id} '{p.title[:50]}'" for p in members[:6])
            body = f"PDFs idênticos (SHA-256 {h[:12]}…): {desc}. " \
                   "Possível duplicata — verifique antes de mesclar."
            if any(h[:12] in b for b in already):
                continue
            s.add(Notification(kind="pdf_dup",
                               title="Possível PDF duplicado",
                               body=body,
                               link="/projetos/" + slug + "/revisoes?aba=dedup"))
            notified += 1
        s.commit()

    stats["groups"] = stats.get("groups", 0) + created
    stats["dup_alerts"] = stats.get("dup_alerts", 0) + notified
    if created or notified:
        bus.publish(slug, "task.progress", run_id=run_id, iteration=iter_id,
                    task_type="dedup", task_name="Deduplicação",
                    message=f"{created} grupo(s) de possíveis duplicatas, "
                            f"{notified} alerta(s) de PDF duplicado")


@task("deep_screener")
def _deep_screener(slug, run_id, iter_id, task, stats):
    """Triagem profunda (2ª fase): reavalia todos os critérios IC/EC
    contra o texto completo do PDF. Elegíveis: papers 'incluido' com PDF
    sem análise 'triagem_profunda'. O veredito pode reverter a inclusão
    (incluido -> excluido/talvez)."""
    from jatai.agents import deep_screening, screening
    from jatai.storage.models_project import PaperAnalysis

    _params = _task_params(task)
    batch_size = max(1, int(_params.get("batch_size", 10) or 10))
    max_papers = int(_params.get("max_papers", 0) or 0) or None
    min_conf = float(_params.get("min_confidence", 0.6) or 0.6)
    llm_timeout = _llm_timeout(task)

    criteria = deep_screening.deep_criteria(research.list_criteria(slug))
    if not criteria:
        return
    questions = research.list_questions(slug)
    model_ref = _model_for(slug, task)
    model_label = llm.model_label(slug, model_ref)

    while True:
        if _should_stop(slug, run_id):
            break
        done_q = select(PaperAnalysis.paper_id).where(
            PaperAnalysis.kind.in_(["triagem_profunda", "triagem_profunda_erro"]))
        with get_project_session(slug) as s:
            q = (select(Paper)
                 .where(Paper.state == "incluido",
                        Paper.pdf_path.isnot(None), Paper.pdf_path != "",
                        ~Paper.id.in_(done_q))
                 .order_by(Paper.id).limit(batch_size))
            if max_papers:
                q = q.limit(min(batch_size,
                                max(1, max_papers - stats.get("deep_screened", 0))))
            papers = list(s.scalars(q))
            total_pending = s.scalar(
                select(func.count()).select_from(Paper)
                .where(Paper.state == "incluido",
                       Paper.pdf_path.isnot(None), Paper.pdf_path != "",
                       ~Paper.id.in_(done_q))) or 0
        if not papers:
            break
        if max_papers and stats.get("deep_screened", 0) >= max_papers:
            break
        total = stats.get("deep_screened", 0) + total_pending

        for paper in papers:
            if _should_stop(slug, run_id):
                break
            if max_papers and stats.get("deep_screened", 0) >= max_papers:
                break
            try:
                text = _paper_full_text(slug, paper)
                if not text:
                    with get_project_session(slug) as s:
                        s.add(PaperAnalysis(
                            paper_id=paper.id, kind="triagem_profunda_erro",
                            result=json.dumps({"error": "texto não extraído do PDF"})))
                        s.commit()
                    continue
                prompt, tpl_name, tpl_ver = deep_screening.build_prompt(
                    slug, task, paper, criteria, questions, text)
                curto = (f"{paper.title[:55]}{'…' if len(paper.title) > 55 else ''}")
                bus.publish(slug, "task.progress", run_id=run_id, iteration=iter_id,
                            task_type=task.task_type,
                            task_name=task.name or task.task_type,
                            message=(f"{stats.get('deep_screened', 0) + 1}/{total} · "
                                     f"#{paper.id} · {curto} — triagem profunda "
                                     f"({len(criteria)} critérios, PDF)…"))
                result = llm.chat_structured(
                    slug, model_ref=model_ref, task=task.task_type,
                    messages=[{"role": "user", "content": prompt}],
                    response_model=screening.ScreeningResult,
                    max_tokens=task.max_tokens or 2048,
                    prompt_template=tpl_name, prompt_version=tpl_ver,
                    timeout=llm_timeout,
                    run_id=run_id, iteration=iter_id, paper_id=paper.id)
                verdict, reasons = deep_screening.apply_rules(
                    result, criteria, min_conf)
                deep_screening.persist(slug, paper.id, criteria, result,
                                       verdict, reasons, iter_id,
                                       model=model_label)
                stats["deep_screened"] = stats.get("deep_screened", 0) + 1
                bus.publish(slug, "task.progress", run_id=run_id, iteration=iter_id,
                            task_type=task.task_type,
                            task_name=task.name or task.task_type,
                            message=(f"{stats.get('deep_screened', 0)}/{total} · "
                                     f"#{paper.id} · {curto} — {verdict}"))
            except Exception as exc:  # noqa: BLE001
                stats["errors"].append(f"deep_screener paper {paper.id}: {exc}")
                with get_project_session(slug) as s:
                    s.add(PaperAnalysis(
                        paper_id=paper.id, kind="triagem_profunda_erro",
                        result=json.dumps({"error": str(exc)[:500]})))
                    s.commit()
                bus.publish(slug, "task.progress", run_id=run_id, iteration=iter_id,
                            task_type=task.task_type,
                            task_name=task.name or task.task_type,
                            message=f"#{paper.id} — erro: {str(exc)[:80]}")


@task("deep_analysis")
def _deep_analysis(slug, run_id, iter_id, task, stats):
    """Análise profunda: resumo + respostas às RQs a partir do texto
    completo do PDF. Mesma arquitetura da triagem: saída estruturada
    Pydantic, regras determinísticas e auditoria por artigo.
    Processa todos os papers elegíveis (incluído + PDF sem análise),
    em lotes — max_papers=0/vazio = sem limite."""
    from jatai.agents import deep
    from jatai.storage.models_project import PaperAnalysis

    try:
        _params = json.loads(task.params or "{}")
    except json.JSONDecodeError:
        _params = {}
    max_papers = int(_params.get("max_papers", 0) or 0) or None
    batch_size = max(1, int(_params.get("batch_size", 10) or 10))
    min_conf = float(_params.get("min_confidence", 0.6) or 0.6)
    single_pid = int(_params.get("paper_id", 0) or 0)

    questions = research.list_questions(slug)
    if not questions:
        return
    model_ref = _params.get("model_ref") or _model_for(slug, task)
    model_label = llm.model_label(slug, model_ref)
    done_ids: set[int] = set()

    while True:
        if _should_stop(slug, run_id):
            break
        if single_pid:
            # análise avulsa: apenas este artigo, ignora estado/marca de
            # processado (re-análise explícita do usuário)
            with get_project_session(slug) as s:
                papers = list(s.scalars(
                    select(Paper).where(Paper.id == single_pid)))
                total_pending = len(papers)
        else:
            done_q = select(PaperAnalysis.paper_id).where(
                PaperAnalysis.kind.in_(["profunda", "profunda_erro"]))
            with get_project_session(slug) as s:
                q = (select(Paper)
                     .where(Paper.state == "incluido",
                            Paper.pdf_path.isnot(None), Paper.pdf_path != "",
                            ~Paper.id.in_(done_q))
                     .order_by(Paper.id).limit(batch_size))
                if max_papers:
                    q = q.limit(min(batch_size, max(1, max_papers - stats.get("analyzed", 0))))
                papers = list(s.scalars(q))
                total_pending = s.scalar(
                    select(func.count()).select_from(Paper)
                    .where(Paper.state == "incluido",
                           Paper.pdf_path.isnot(None), Paper.pdf_path != "",
                           ~Paper.id.in_(done_q))) or 0
        if not papers:
            break
        if max_papers and stats.get("analyzed", 0) >= max_papers:
            break
        total = stats.get("analyzed", 0) + total_pending

        for paper in papers:
            if _should_stop(slug, run_id):
                break
            if max_papers and stats.get("analyzed", 0) >= max_papers:
                break
            try:
                # análise profunda recebe o artigo completo, sem corte
                text = _paper_full_text(slug, paper, max_chars=0)
                if not text:
                    # sem texto extraível: registra para não re-tentar
                    # para sempre (mesma convenção dos demais erros)
                    with get_project_session(slug) as s:
                        s.add(PaperAnalysis(
                            paper_id=paper.id, kind="profunda_erro",
                            result=json.dumps({"error": "texto não extraído do PDF"})))
                        s.commit()
                    continue
                prompt, tpl_name, tpl_ver = deep.build_prompt(
                    slug, task, paper, questions, text)
                curto = (f"{paper.title[:55]}{'…' if len(paper.title) > 55 else ''}")
                bus.publish(slug, "task.progress", run_id=run_id, iteration=iter_id,
                            task_type=task.task_type,
                            task_name=task.name or task.task_type,
                            message=(f"{stats.get('analyzed', 0) + 1}/{total} · "
                                     f"#{paper.id} · {curto} — analisando "
                                     f"({len(questions)} RQs, PDF)…"))
                result = llm.chat_structured(
                    slug, model_ref=model_ref, task=task.task_type,
                    messages=[{"role": "user", "content": prompt}],
                    response_model=deep.DeepResult,
                    max_tokens=task.max_tokens or 4096,
                    timeout=_llm_timeout(task),
                    prompt_template=tpl_name, prompt_version=tpl_ver,
                    run_id=run_id, iteration=iter_id, paper_id=paper.id)
                notes = deep.apply_rules(result, questions, min_conf)
                deep.persist(slug, paper.id, result, notes, iter_id,
                             model=model_label)
                stats["analyzed"] = stats.get("analyzed", 0) + 1
                bus.publish(slug, "task.progress", run_id=run_id, iteration=iter_id,
                            task_type=task.task_type,
                            task_name=task.name or task.task_type,
                            message=(f"{stats.get('analyzed', 0)}/{total} · "
                                     f"#{paper.id} · {curto} — "
                                     f"conf={result.confidence:.2f}"))
            except Exception as exc:  # noqa: BLE001
                stats["errors"].append(f"deep_analysis paper {paper.id}: {exc}")
                with get_project_session(slug) as s:
                    s.add(PaperAnalysis(paper_id=paper.id, kind="profunda_erro",
                                        result=json.dumps({"error": str(exc)[:500]})))
                    s.commit()
                bus.publish(slug, "task.progress", run_id=run_id, iteration=iter_id,
                            task_type=task.task_type,
                            task_name=task.name or task.task_type,
                            message=f"#{paper.id} — erro: {str(exc)[:80]}")
        if single_pid:
            break  # análise avulsa: um único artigo por execução


@task("classification")
def _classification(slug, run_id, iter_id, task, stats):
    """Classificação/catalogação: preenche os CatalogFields ativos a
    partir do texto completo do PDF. Mesma arquitetura da análise
    profunda: saída estruturada Pydantic, regras determinísticas e
    auditoria por artigo. Processa todos os elegíveis (incluído + PDF
    sem classificação) — max_papers=0/vazio = sem limite."""
    from jatai.agents import classify
    from jatai.storage.models_project import PaperAnalysis

    try:
        _params = json.loads(task.params or "{}")
    except json.JSONDecodeError:
        _params = {}
    max_papers = int(_params.get("max_papers", 0) or 0) or None
    batch_size = max(1, int(_params.get("batch_size", 10) or 10))
    min_conf = float(_params.get("min_confidence", 0.6) or 0.6)
    single_pid = int(_params.get("paper_id", 0) or 0)

    fields = [f for f in research.list_catalog_fields(slug) if f.active]
    if not fields:
        return
    response_model = classify.result_model(fields)
    model_ref = _params.get("model_ref") or _model_for(slug, task)
    model_label = llm.model_label(slug, model_ref)
    if single_pid:
        eligible = (Paper.id == single_pid,)
    else:
        done_q = select(PaperAnalysis.paper_id).where(
            PaperAnalysis.kind.in_(["classificacao", "classificacao_erro"]))
        eligible = (Paper.state == "incluido",
                    Paper.pdf_path.isnot(None), Paper.pdf_path != "",
                    ~Paper.id.in_(done_q))

    while True:
        if _should_stop(slug, run_id):
            break
        with get_project_session(slug) as s:
            q = select(Paper).where(*eligible).order_by(Paper.id) \
                .limit(batch_size)
            if max_papers:
                q = q.limit(min(batch_size,
                                max(1, max_papers - stats.get("classified", 0))))
            papers = list(s.scalars(q))
            total_pending = s.scalar(
                select(func.count()).select_from(Paper).where(*eligible)) or 0
        if not papers:
            break
        if max_papers and stats.get("classified", 0) >= max_papers:
            break
        total = stats.get("classified", 0) + total_pending

        for paper in papers:
            if _should_stop(slug, run_id):
                break
            if max_papers and stats.get("classified", 0) >= max_papers:
                break
            try:
                text = _paper_full_text(slug, paper)
                if not text:
                    with get_project_session(slug) as s:
                        s.add(PaperAnalysis(
                            paper_id=paper.id, kind="classificacao_erro",
                            result=json.dumps({"error": "texto não extraído do PDF"})))
                        s.commit()
                    continue
                prompt, tpl_name, tpl_ver = classify.build_prompt(
                    slug, task, paper, fields, text)
                curto = (f"{paper.title[:55]}{'…' if len(paper.title) > 55 else ''}")
                bus.publish(slug, "task.progress", run_id=run_id, iteration=iter_id,
                            task_type=task.task_type,
                            task_name=task.name or task.task_type,
                            message=(f"{stats.get('classified', 0) + 1}/{total} · "
                                     f"#{paper.id} · {curto} — classificando "
                                     f"({len(fields)} campos, PDF)…"))
                result = llm.chat_structured(
                    slug, model_ref=model_ref, task=task.task_type,
                    messages=[{"role": "user", "content": prompt}],
                    response_model=response_model,
                    max_tokens=task.max_tokens or 2048,
                    prompt_template=tpl_name, prompt_version=tpl_ver,
                    timeout=_llm_timeout(task),
                    run_id=run_id, iteration=iter_id, paper_id=paper.id)
                notes = classify.apply_rules(result, fields, min_conf)
                classify.persist(slug, paper.id, fields, result, notes, iter_id,
                                  model_ref=model_label)
                stats["classified"] = stats.get("classified", 0) + 1
                bus.publish(slug, "task.progress", run_id=run_id, iteration=iter_id,
                            task_type=task.task_type,
                            task_name=task.name or task.task_type,
                            message=(f"{stats.get('classified', 0)}/{total} · "
                                     f"#{paper.id} · {curto} — "
                                     f"conf={result.confidence:.2f}"))
            except Exception as exc:  # noqa: BLE001
                stats["errors"].append(f"classification paper {paper.id}: {exc}")
                with get_project_session(slug) as s:
                    s.add(PaperAnalysis(paper_id=paper.id, kind="classificacao_erro",
                                        result=json.dumps({"error": str(exc)[:500]})))
                    s.commit()
                bus.publish(slug, "task.progress", run_id=run_id, iteration=iter_id,
                            task_type=task.task_type,
                            task_name=task.name or task.task_type,
                            message=f"#{paper.id} — erro: {str(exc)[:80]}")
        if single_pid:
            break  # análise avulsa: um único artigo por execução


@task("concept_extraction")
def _concept_extraction(slug, run_id, iter_id, task, stats):
    """Extrai conceitos/termos do artigo completo (PDF) de cada paper
    incluído ainda não processado. Saída estruturada Pydantic, dedup por
    termo e marcação por artigo — max_papers=0/vazio = sem limite."""
    from jatai.agents import concepts
    from jatai.storage.models_project import Concept, PaperAnalysis

    try:
        _params = json.loads(task.params or "{}")
    except json.JSONDecodeError:
        _params = {}
    max_papers = int(_params.get("max_papers", 0) or 0) or None
    batch_size = max(1, int(_params.get("batch_size", 5) or 5))
    max_concepts = int(_params.get("max_concepts", 3) or 0)
    single_pid = int(_params.get("paper_id", 0) or 0)

    model_ref = _params.get("model_ref") or _model_for(slug, task)
    model_label = llm.model_label(slug, model_ref)
    areas = [
        {"name": a.name,
         "keywords": [t.strip() for k in a.keywords
                      for t in k.term.split(",") if t.strip()]}
        for a in research.list_areas(slug) if a.status == "ativa"
    ]

    while True:
        if _should_stop(slug, run_id):
            break
        if single_pid:
            # análise avulsa: apenas este artigo, ignora estado/marca de
            # processado (re-análise explícita do usuário)
            with get_project_session(slug) as s:
                papers = list(s.scalars(
                    select(Paper).where(Paper.id == single_pid)
                    .limit(batch_size)))
                total_pending = len(papers)
        else:
            done_q = select(PaperAnalysis.paper_id).where(
                PaperAnalysis.kind.in_(["conceitos", "conceitos_erro"]))
            with get_project_session(slug) as s:
                q = (select(Paper)
                     .where(Paper.state == "incluido",
                            ~Paper.id.in_(done_q))
                     .order_by(Paper.id).limit(batch_size))
                if max_papers:
                    q = q.limit(min(batch_size,
                                    max(1, max_papers - stats.get("concepts_papers", 0))))
                papers = list(s.scalars(q))
                total_pending = s.scalar(
                    select(func.count()).select_from(Paper)
                    .where(Paper.state == "incluido",
                           ~Paper.id.in_(done_q))) or 0
        # conceitos já catalogados (dedup na persistência + contexto do prompt)
        with get_project_session(slug) as s:
            existing = {}
            existing_terms = []
            for c in s.scalars(select(Concept)):
                existing[c.term.lower()] = c.id
                existing_terms.append(c.term)
            existing_terms.sort()
        if not papers:
            break
        if max_papers and stats.get("concepts_papers", 0) >= max_papers:
            break
        total = stats.get("concepts_papers", 0) + total_pending

        for paper in papers:
            if _should_stop(slug, run_id):
                break
            if max_papers and stats.get("concepts_papers", 0) >= max_papers:
                break
            try:
                # artigo completo via PDF; sem PDF usa metadados
                text = ""
                if paper.pdf_path:
                    text = _paper_full_text(slug, paper, max_chars=0)
                if not text:
                    text = (f"Título: {paper.title}\n"
                            f"Resumo: {paper.abstract or '(indisponível)'}\n"
                            f"Palavras-chave: {paper.keywords or '—'}")
                prompt, tpl_name, tpl_ver = concepts.build_prompt(
                    slug, task, paper, text, existing_terms,
                    max_concepts=max_concepts, areas=areas)
                curto = (f"{paper.title[:55]}{'…' if len(paper.title) > 55 else ''}")
                bus.publish(slug, "task.progress", run_id=run_id, iteration=iter_id,
                            task_type=task.task_type,
                            task_name=task.name or task.task_type,
                            message=(f"{stats.get('concepts_papers', 0) + 1}/{total} · "
                                     f"#{paper.id} · {curto} — extraindo conceitos…"))
                result = llm.chat_structured(
                    slug, model_ref=model_ref, task=task.task_type,
                    messages=[{"role": "user", "content": prompt}],
                    response_model=concepts.ConceptsResult,
                    max_tokens=task.max_tokens or 2048,
                    prompt_template=tpl_name, prompt_version=tpl_ver,
                    timeout=_llm_timeout(task),
                    run_id=run_id, iteration=iter_id, paper_id=paper.id)
                if max_concepts:
                    result.concepts = result.concepts[:max_concepts]
                created = concepts.persist(slug, paper.id, result, iter_id,
                                           model_ref=model_label,
                                           existing=existing)
                stats["new_terms"] = stats.get("new_terms", 0) + created
                stats["concepts"] = stats.get("concepts", 0) + created
                stats["concepts_papers"] = stats.get("concepts_papers", 0) + 1
                bus.publish(slug, "task.progress", run_id=run_id, iteration=iter_id,
                            task_type=task.task_type,
                            task_name=task.name or task.task_type,
                            message=(f"{stats['concepts_papers']}/{total} · "
                                     f"#{paper.id} · {curto} — "
                                     f"+{created} conceito(s)"))
            except Exception as exc:  # noqa: BLE001
                stats["errors"].append(f"concept_extraction paper {paper.id}: {exc}")
                with get_project_session(slug) as s:
                    s.add(PaperAnalysis(
                        paper_id=paper.id, kind="conceitos_erro",
                        result=json.dumps({"error": str(exc)[:500]})))
                    s.commit()
                stats["concepts_papers"] = stats.get("concepts_papers", 0) + 1
                bus.publish(slug, "task.progress", run_id=run_id, iteration=iter_id,
                            task_type=task.task_type,
                            task_name=task.name or task.task_type,
                            message=f"#{paper.id} — erro: {str(exc)[:80]}")
        if single_pid:
            break  # análise avulsa: um único artigo por execução


def _snowball_pdf_records(slug, run_id, iter_id, task, paper, openalex_conn,
                          max_refs: int | None) -> list[tuple[str, object]]:
    """Fallback do snowballing: extrai a lista de referências do texto do
    PDF via LLM e resolve cada uma nas fontes habilitadas com conector.

    Retorna [(source_type, RawRecord)] — uma entrada por fonte onde a
    referência foi encontrada (uma referência pode aparecer em várias
    fontes, gerando um sighting em cada uma)."""
    import re as _re

    from jatai.agents import references as refs_mod
    from jatai.dedup.normalize import normalize_doi, titles_similar
    from jatai.sources import openalex as openalex_mod
    from jatai.sources.base import get_connector

    text = _paper_full_text(slug, paper, max_chars=0)
    if len(text) < 400:
        return []
    prompt, tn, tv = refs_mod.build_prompt(slug, task, paper, text,
                                           max_refs=max_refs or 0)
    _params = _task_params(task)
    model_ref = _params.get("model_ref") or _model_for(slug, task)
    result = llm.chat_structured(
        slug, model_ref=model_ref, task=task.task_type,
        messages=[{"role": "user", "content": prompt}],
        response_model=refs_mod.ReferencesResult,
        max_tokens=getattr(task, "max_tokens", None) or 4096,
        prompt_template=tn, prompt_version=tv,
        timeout=_llm_timeout(task), run_id=run_id, iteration=iter_id,
        paper_id=paper.id)

    # dedupe por título normalizado + limite
    seen: set[str] = set()
    items = []
    for r in result.references:
        t = (r.title or "").strip()
        key = _re.sub(r"\s+", " ", t.lower())
        if t and key not in seen:
            seen.add(key)
            items.append(r)
    if max_refs:
        items = items[:max_refs]

    # fontes habilitadas com conector implementado
    connectors: dict[str, object] = {"openalex": openalex_conn}
    dblp = next((s for s in research.list_sources(slug)
                 if s.enabled and s.source_type == "dblp"), None)
    if dblp is not None:
        conn = get_connector("dblp", config=(dblp.config or {}))
        if conn is not None:
            connectors["dblp"] = conn

    out: list[tuple[str, object]] = []

    def _title_match(query: str, candidate: str) -> bool:
        """Match de referência: similaridade de título (limiar mais folgado)
        ou todos os termos relevantes da consulta presentes no candidato."""
        if titles_similar(query, candidate, threshold=70):
            return True
        qt = set(_re.findall(r"[a-z0-9]{3,}", query.lower()))
        ct = set(_re.findall(r"[a-z0-9]{3,}", candidate.lower()))
        return bool(qt) and qt <= ct

    for item in items:
        title = item.title.strip()
        doi = normalize_doi(item.doi or "")
        for stype, conn in connectors.items():
            try:
                if stype == "openalex":
                    work = conn.work_by_doi(doi) if doi else None
                    if work is None and title:
                        work = conn.work_by_title(title)
                    rec = openalex_mod._work_to_raw(work) if work else None
                else:  # dblp: busca por título, aceita só match plausível
                    hits = conn.search(title, max_results=5) or []
                    rec = next((h for h in hits
                                if _title_match(title, h.title)), None)
            except Exception:  # noqa: BLE001 — fonte instável não derruba o lote
                rec = None
            if rec is not None and rec.title:
                out.append((stype, rec))
    return out


@task("snowballing")
def _snowballing(slug, run_id, iter_id, task, stats):
    """Snowballing: para cada artigo incluído ainda não processado, busca no
    OpenAlex a lista de referências (referenced_works) e importa os registros
    como novas publicações (state="novo"). Requer a fonte 'openalex' habilitada.

    Fallback (param pdf_fallback, default ligado): quando a obra não é
    encontrada ou o OpenAlex não tem referenced_works, extrai a lista de
    referências do texto do PDF via LLM (agents/references.py) e resolve cada
    uma nas fontes habilitadas com conector (OpenAlex, DBLP...).

    Proveniência: cada publicação importada ganha um PaperSighting com a
    fonte onde foi encontrada e um sighting extra source="snowball" com o
    artigo semente. Registra por artigo (PaperAnalysis kind="snowball")
    quantas referências já existiam na base e quantas foram adicionadas;
    os totais vão para os stats da iteração (snowball_refs, snowball_known,
    new_papers)."""
    from jatai.sources import openalex as openalex_mod
    from jatai.sources.base import get_connector
    from jatai.storage.models_project import PaperAnalysis

    _params = _task_params(task)
    batch_size = max(1, int(_params.get("batch_size", 5) or 5))
    max_papers = int(_params.get("max_papers", 0) or 0) or None
    max_refs = int(_params.get("max_refs", 0) or 0) or None
    pdf_fallback = str(_params.get("pdf_fallback", "1")).strip().lower() \
        not in ("0", "false", "nao", "não", "off")
    max_pdf_refs = int(_params.get("max_pdf_refs", 40) or 0) or None
    single_pid = int(_params.get("paper_id", 0) or 0)

    src = next((s for s in research.list_sources(slug)
                if s.enabled and s.source_type == "openalex"), None)
    connector = get_connector("openalex", config=(src.config or {}) if src else {}) \
        if src else None
    if connector is None or not isinstance(connector, openalex_mod.OpenAlexConnector):
        stats["errors"].append("snowballing: conector OpenAlex indisponível")
        bus.publish(slug, "task.progress", run_id=run_id, iteration=iter_id,
                    task_type=task.task_type, task_name=task.name or task.task_type,
                    message="snowballing: fonte 'openalex' não habilitada — tarefa ignorada")
        return
    try:
        if not connector.is_available():
            raise RuntimeError("OpenAlex inacessível")
    except Exception as exc:  # noqa: BLE001
        stats["errors"].append(f"snowballing: {exc}")
        bus.publish(slug, "task.progress", run_id=run_id, iteration=iter_id,
                    task_type=task.task_type, task_name=task.name or task.task_type,
                    message=f"snowballing: {exc} — tarefa ignorada")
        return

    done_q = select(PaperAnalysis.paper_id).where(
        PaperAnalysis.kind.in_(["snowball", "snowball_erro"]))
    processed = 0
    while True:
        if _should_stop(slug, run_id):
            break
        if single_pid:
            # análise avulsa: apenas este artigo, ignora estado/marca de
            # processado (re-execução explícita do usuário)
            with get_project_session(slug) as s:
                papers = list(s.scalars(
                    select(Paper).where(Paper.id == single_pid)
                    .limit(batch_size)))
                total_pending = len(papers)
        else:
            with get_project_session(slug) as s:
                q = (select(Paper)
                     .where(Paper.state == "incluido",
                            ~Paper.id.in_(done_q))
                     .order_by(Paper.id))
                if max_papers:
                    q = q.limit(min(batch_size,
                                    max(1, max_papers - stats.get("snowball_papers", 0))))
                else:
                    q = q.limit(batch_size)
                papers = list(s.scalars(q))
                total_pending = s.scalar(
                    select(func.count()).select_from(Paper)
                    .where(Paper.state == "incluido",
                           ~Paper.id.in_(done_q))) or 0
        if not papers:
            break
        if max_papers and stats.get("snowball_papers", 0) >= max_papers:
            break
        total = stats.get("snowball_papers", 0) + total_pending

        for paper in papers:
            if _should_stop(slug, run_id):
                break
            if max_papers and stats.get("snowball_papers", 0) >= max_papers:
                break
            curto = (f"{paper.title[:55]}{'…' if len(paper.title) > 55 else ''}")
            try:
                work = connector.work_by_doi(paper.doi) if paper.doi else None
                if work is None:
                    work = connector.work_by_title(paper.title)
                ref_ids = connector.referenced_works(work) if work else []
                if max_refs:
                    ref_ids = ref_ids[:max_refs]
                seed = {"id": paper.id, "title": paper.title,
                        "doi": paper.doi or ""}
                by_source: dict[str, list] = {}
                notes: list[str] = []
                if ref_ids:
                    by_source["openalex"] = connector.works_by_ids(ref_ids)
                else:
                    notes.append("obra não encontrada no OpenAlex"
                                 if work is None else
                                 "sem referências indexadas no OpenAlex")
                    if pdf_fallback:
                        caps = [c for c in (max_pdf_refs, max_refs) if c]
                        fb = _snowball_pdf_records(
                            slug, run_id, iter_id, task, paper, connector,
                            min(caps) if caps else None)
                        for stype, rec in fb:
                            by_source.setdefault(stype, []).append(rec)
                        if fb:
                            notes.append(
                                f"{len(fb)} referência(s) extraídas do PDF "
                                "e resolvidas nas fontes")
                        else:
                            notes.append("fallback de PDF não retornou referências")

                new = dup = 0
                for stype, recs in by_source.items():
                    n, d = catalog.ingest_records(
                        slug, stype, None, recs, seed=seed)
                    new += n
                    dup += d
                payload = {
                    "work_id": work.get("id") if work else None,
                    "refs_listadas": len(ref_ids),
                    "refs_resolvidas": sum(len(v) for v in by_source.values()),
                    "refs_conhecidas": dup,
                    "refs_novas": new,
                }
                if notes:
                    payload["note"] = "; ".join(notes)
                with get_project_session(slug) as s:
                    s.add(PaperAnalysis(
                        paper_id=paper.id, kind="snowball",
                        result=json.dumps(payload, ensure_ascii=False)))
                    s.commit()
                stats["snowball_papers"] = stats.get("snowball_papers", 0) + 1
                stats["snowball_refs"] = stats.get("snowball_refs", 0) + len(ref_ids)
                stats["snowball_known"] = stats.get("snowball_known", 0) + dup
                stats["snowball_new"] = stats.get("snowball_new", 0) + new
                stats["new_papers"] = stats.get("new_papers", 0) + new
                if ref_ids:
                    suffix = f"{len(ref_ids)} refs: {dup} já na base, {new} novas"
                elif by_source:
                    suffix = (f"PDF: {payload['refs_resolvidas']} refs resolvidas — "
                              f"{dup} já na base, {new} novas")
                else:
                    suffix = "sem referências encontradas"
                bus.publish(slug, "task.progress", run_id=run_id, iteration=iter_id,
                            task_type=task.task_type,
                            task_name=task.name or task.task_type,
                            message=(f"{stats['snowball_papers']}/{total} · "
                                     f"#{paper.id} · {curto} — {suffix}"))
            except Exception as exc:  # noqa: BLE001
                stats["errors"].append(f"snowballing paper {paper.id}: {exc}")
                with get_project_session(slug) as s:
                    s.add(PaperAnalysis(
                        paper_id=paper.id, kind="snowball_erro",
                        result=json.dumps({"error": str(exc)[:500]})))
                    s.commit()
                stats["snowball_papers"] = stats.get("snowball_papers", 0) + 1
                bus.publish(slug, "task.progress", run_id=run_id, iteration=iter_id,
                            task_type=task.task_type,
                            task_name=task.name or task.task_type,
                            message=f"#{paper.id} — erro: {str(exc)[:80]}")
        if single_pid:
            break  # análise avulsa: um único artigo por execução

    # resumo final no histórico da execução
    if stats.get("snowball_papers"):
        bus.publish(slug, "task.progress", run_id=run_id, iteration=iter_id,
                    task_type=task.task_type, task_name=task.name or task.task_type,
                    message=(f"snowballing concluído: {stats.get('snowball_papers', 0)} "
                             f"artigo(s) — {stats.get('snowball_refs', 0)} referências "
                             f"listadas, {stats.get('snowball_known', 0)} já conhecidas, "
                             f"{stats.get('snowball_new', 0)} nova(s) publicação(ões)"))


@task("proposals")
def _proposals(slug, run_id, iter_id, task, stats):
    """Gera propostas de pesquisa (lacunas, tendências, limitações,
    oportunidades) a partir dos agregados do catálogo — uma chamada LLM
    por execução; as propostas aparecem na tela Propostas."""
    from jatai.agents import proposals as proposals_mod

    _params = _task_params(task)
    max_p = max(1, int(_params.get("max_proposals", 8) or 8))
    created = proposals_mod.generate(
        slug, model_ref=_model_for(slug, task), max_proposals=max_p,
        run_id=run_id, iteration=iter_id, timeout=_llm_timeout(task))
    stats["proposals"] = stats.get("proposals", 0) + created


def run_cost(slug: str, run_id: int) -> float:
    "n/a — custo é global por projeto no momento"
    return 0.0


def papers_total(slug: str) -> int:
    with get_project_session(slug) as s:
        return s.scalar(select(func.count(Paper.id))) or 0