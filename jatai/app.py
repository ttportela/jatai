import asyncio
import json as _json
import threading
import urllib.parse
import queue as _queue
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                             RedirectResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from jatai import __version__
from jatai.audit.log import list_events, record_event
from jatai.agents import config_service as agent_cfg
from jatai.agents import llm as agent_llm
from jatai.agents import runner as agent_runner
from jatai.catalog import service as catalog
from jatai.events import bus as event_bus
from jatai.exports import service as exporter
from jatai import scheduler
from jatai.config import global_settings
from jatai.projects import service as projects
from jatai import notifications
from jatai import proposals as proposals_service
from jatai.research import service as research
from jatai.search import service as search_service

WEB_DIR = Path(__file__).parent / "web"
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))

# Telas do UX.md -> (rótulo, descrição)
SCREENS = [
    ("/projetos", "Projetos", "Gerenciar projetos de pesquisa"),
    ("/", "Dashboard", "Visão geral do projeto"),
    ("/pesquisa/configuracao", "Configuração", "Escopo e questões"),
    ("/pesquisa/fontes", "Fontes", "Bases bibliográficas"),
    ("/pesquisa/consultas", "Consultas", "Expressões de busca"),
    ("/agente/pipeline", "Pipeline", "Etapas e configurações"),
    ("/agente/modelos", "Modelos IA", "Provedores e modelos"),
    ("/agente/execucoes", "Execuções", "Runs e iterações"),
    ("/biblioteca/publicacoes", "Publicações", "Catálogo de artigos"),
    ("/biblioteca/conceitos", "Conceitos", "Termos e expansão"),
    ("/biblioteca/propostas", "Propostas", "Novos estudos sugeridos"),
    ("/qualidade/revisoes", "Triagem", "Revisão humana de publicações"),
    ("/qualidade/analise", "Análise", "Análises e resultados do agente"),
    ("/qualidade/avaliacao", "Avaliação", "Notas de qualidade dos estudos"),
    ("/sistema/historico", "Histórico", "Auditoria"),
    ("/sistema/exportacoes", "Exportações", "Resultados e relatórios"),
    ("/sistema/configuracoes", "Configurações", "Preferências globais"),
]

NAV_GROUPS = [
    ("Visão Geral", ["/projetos", "/"]),
    ("Pesquisa", ["/pesquisa/configuracao", "/pesquisa/fontes", "/pesquisa/consultas"]),
    ("Agente", ["/agente/pipeline", "/agente/modelos", "/agente/execucoes"]),
    ("Biblioteca", ["/biblioteca/publicacoes", "/biblioteca/conceitos", "/biblioteca/propostas"]),
    ("Qualidade", ["/qualidade/revisoes", "/qualidade/analise", "/qualidade/avaliacao"]),
    ("Sistema", ["/sistema/historico", "/sistema/exportacoes", "/sistema/configuracoes"]),
]


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# Mapa rota global da sidebar -> rota equivalente por projeto
PROJECT_ROUTES = {
    "/pesquisa/configuracao": "configuracao",
    "/pesquisa/fontes": "fontes",
    "/pesquisa/consultas": "consultas",
    "/agente/pipeline": "agente/pipeline",
    "/agente/modelos": "agente/modelos",
    "/agente/execucoes": "agente/execucoes",
    "/biblioteca/publicacoes": "publicacoes",
    "/biblioteca/conceitos": "conceitos",
    "/biblioteca/propostas": "propostas",
    "/qualidade/revisoes": "revisoes",
    "/qualidade/analise": "qualidade/analise",
    "/qualidade/avaliacao": "qualidade/avaliacao",
    "/sistema/exportacoes": "exportacoes",
}

# rota por projeto -> href global equivalente (para destacar nav)
PROJECT_TO_GLOBAL = {v: k for k, v in PROJECT_ROUTES.items()}
PROJECT_TO_GLOBAL.update({
    "": "", "dashboard": "/", "configuracao": "/pesquisa/configuracao",
})


def _active_nav(path: str) -> str:
    """Retorna o href global da sidebar correspondente ao path atual."""
    if path == "/projetos" or path.startswith("/projetos/") and len(path.strip("/").split("/")) == 2:
        return "/projetos"
    if path in PROJECT_TO_GLOBAL.values():
        return path
    parts = path.strip("/").split("/")
    if len(parts) >= 3 and parts[0] == "projetos":
        sub = "/".join(parts[2:])
        if sub in PROJECT_TO_GLOBAL:
            return PROJECT_TO_GLOBAL[sub]
        if sub == "dashboard" or sub == "":
            return "/"
    return ""


def _current_slug() -> str:
    return global_settings.load().get("current_project", "")


def _build_params(slug: str, task_type: str, agent_id: int, form) -> str:
    """Monta params JSON a partir dos campos do schema do tipo de tarefa."""
    import json as _json
    info = agent_cfg.task_type_info(task_type) or {}
    out = {}
    for fld in info.get("fields", []):
        key = fld["key"]
        raw = form.get(f"param_{key}")
        if raw is None:
            continue
        raw = str(raw).strip()
        if fld["type"] == "checkbox":
            out[key] = raw == "on" or raw == "1"
        elif fld["type"] == "number":
            try:
                out[key] = float(raw.replace(",", "."))
            except ValueError:
                pass
        else:
            out[key] = raw
    # preserva chaves de params que não estão no schema
    existing = agent_cfg.get_task_config(slug, task_type, agent_id=agent_id)
    if existing:
        try:
            old = _json.loads(existing.params or "{}")
            for k, v in old.items():
                out.setdefault(k, v)
        except _json.JSONDecodeError:
            pass
    return _json.dumps(out, ensure_ascii=False)


# ---- Visualizador de PDF / página da publicação ----

_PDF_INTERCEPT_JS = r"""
<script>
(function () {
  function pdfish(h) {
    return /\.pdf($|[?#])/i.test(h) || /\/pdf\//i.test(h);
  }
  function offer(url) {
    try { parent.postMessage({ jataiPdf: 1, url: url }, "*"); } catch (e) {}
  }
  document.addEventListener("click", function (e) {
    var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!a) return;
    var h = a.href || "";
    if (a.hasAttribute("download") || pdfish(h)) {
      e.preventDefault();
      e.stopPropagation();
      offer(h);
    }
  }, true);
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(
      'embed[type="application/pdf"], object[type="application/pdf"],'
      + ' iframe[src*=".pdf"], a[download]').forEach(function (el) {
      var u = el.src || el.href || el.data || "";
      if (u) offer(u);
    });
  });
})();
</script>
"""


def _proxied_page(html_text: str, base_url: str) -> str:
    """Reembrulha o HTML remoto: <base> para recursos relativos e
    script que detecta navegação para PDF e avisa a página-mãe."""
    import re as _re
    import urllib.parse as _up
    parts = _up.urlsplit(base_url)
    base_href = f"{parts.scheme}://{parts.netloc}/"
    base_tag = f'<base href="{_up.quote(base_href, safe=":/?#[]@!$&\'()*+,;=%")}">'
    head_m = _re.search(r"<head[^>]*>", html_text, _re.I)
    if head_m:
        html_text = (html_text[:head_m.end()] + base_tag +
                     html_text[head_m.end():])
    else:
        html_text = f"<head>{base_tag}</head>" + html_text
    body_m = _re.search(r"</body>", html_text, _re.I)
    if body_m:
        html_text = (html_text[:body_m.start()] + _PDF_INTERCEPT_JS +
                     html_text[body_m.start():])
    else:
        html_text += _PDF_INTERCEPT_JS
    return html_text


def _pdf_interstitial(slug: str, paper_id: int, pdf_url: str) -> str:
    import html as _html
    e = _html.escape
    action = (f"/projetos/{slug}/publicacoes/{paper_id}/pdf-url")
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>PDF detectado</title>
<style>
body{{font-family:system-ui,sans-serif;display:flex;align-items:center;
justify-content:center;min-height:80vh;margin:0;background:#f5f4ef;color:#222}}
.card{{background:#fff;border:1px solid #ddd;border-radius:12px;
padding:32px;max-width:540px}}
button,.btn{{display:inline-block;padding:10px 18px;border-radius:8px;
border:1px solid #999;background:#fff;cursor:pointer;font-size:14px}}
button.primary{{background:#2f5fd0;border-color:#2f5fd0;color:#fff}}
a{{color:#2f5fd0}}
code{{word-break:break-all;font-size:12px}}
</style></head><body><div class="card">
<h2>Este endereço é um PDF</h2>
<p>O conteúdo apontado é um arquivo PDF.</p>
<p><code>{e(pdf_url)}</code></p>
<form method="post" action="{action}">
<input type="hidden" name="url" value="{e(pdf_url)}">
<button class="primary" type="submit">Salvar PDF no projeto</button>
</form>
<p><a href="{e(pdf_url)}" target="_blank" rel="noopener">
Abrir PDF original</a></p>
</div></body></html>"""


def _visible_text_len(html_text: str) -> int:
    import re as _re
    t = _re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html_text)
    t = _re.sub(r"(?s)<[^>]+>", " ", t)
    return len(t.strip())


def _browser_render(url: str, ua: str) -> str | None:
    """Renderiza a página com Chromium (SPA/JS) e devolve o HTML."""
    from jatai import browser as _bm
    br = None
    try:
        br = _bm.launch_browser()
        ctx = br.new_context(user_agent=ua,
                             viewport={"width": 1280, "height": 900})
        try:
            page = ctx.new_page()
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:  # noqa: BLE001
                pass
            return page.content()
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        return None
    finally:
        if br is not None:
            try:
                br.close()
            except Exception:  # noqa: BLE001
                pass
        _bm.stop_playwright()


def create_app() -> FastAPI:
    app = FastAPI(title="Jataí", version=__version__)

    static_dir = WEB_DIR / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    scheduler.start_scheduler()

    def render(request: Request, template: str, context: dict | None = None) -> HTMLResponse:
        ctx = {
            "request": request,
            "version": __version__,
            "nav_groups": NAV_GROUPS,
            "screens": SCREENS,
            "settings": global_settings.load(),
            "current_project": global_settings.load().get("current_project", ""),
            "current_project_name": (
                (projects.get_project(global_settings.load().get("current_project", "")) or None)
                and projects.get_project(global_settings.load().get("current_project", "")).name) or "",
            "active_nav": _active_nav(request.url.path),
            "notif_unread": (
                notifications.unread_count(global_settings.load().get("current_project", ""))
                if global_settings.load().get("current_project") else 0),
        }
        ctx.update(context or {})
        return TEMPLATES.TemplateResponse(request, template, ctx)

    @app.get("/api/health")
    def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        slug = _current_slug()
        if slug and projects.get_project(slug):
            return RedirectResponse(f"/projetos/{slug}/dashboard")
        return RedirectResponse("/projetos")

    # ---- Projetos (Tela 01) ----

    @app.get("/projetos", response_class=HTMLResponse)
    def projects_list(request: Request):
        items = [
            {**{c: getattr(p, c) for c in ("slug", "name", "description", "status", "workspace")},
             "created_at": p.created_at.isoformat() if p.created_at else "",
             "publications": projects.count_publications(p.slug)}
            for p in projects.list_projects()
        ]
        return render(request, "projects.html", {"projects": items})

    @app.post("/projetos", response_class=HTMLResponse)
    async def projects_create(request: Request):
        form = await request.form()
        name = str(form.get("name", "")).strip()
        if name:
            projects.create_project(name, str(form.get("description", "")).strip())
        return RedirectResponse("/projetos", status_code=303)

    @app.get("/projetos/{slug}", response_class=HTMLResponse)
    def project_detail(request: Request, slug: str):
        project = projects.get_project(slug)
        if not project:
            return render(request, "placeholder.html", {"title": "Projeto não encontrado", "description": ""})
        settings = global_settings.load()
        if settings.get("current_project") != slug:
            settings["current_project"] = slug
            global_settings.save(settings)
        events = list_events(project_slug=slug, limit=20)
        return render(request, "project.html", {
            "project": project,
            "publications": projects.count_publications(slug),
            "events": events,
            "stats": catalog.dashboard_stats(slug),
            "recent_runs": catalog.recent_runs(slug),
        })

    @app.post("/projetos/{slug}/status", response_class=HTMLResponse)
    async def project_status(request: Request, slug: str):
        form = await request.form()
        projects.set_status(slug, str(form.get("status", "ativo")))
        return RedirectResponse(f"/projetos/{slug}", status_code=303)

    @app.post("/projetos/{slug}/update", response_class=HTMLResponse)
    async def project_update(request: Request, slug: str):
        form = await request.form()
        projects.update_project(slug, str(form.get("name", "")).strip() or None,
                                str(form.get("description", "")).strip())
        return RedirectResponse(f"/projetos/{slug}", status_code=303)

    @app.post("/projetos/{slug}/duplicate", response_class=HTMLResponse)
    async def project_duplicate(request: Request, slug: str):
        await request.form()
        try:
            copy = projects.duplicate_project(slug)
        except ValueError:
            pass
        else:
            return RedirectResponse(f"/projetos/{copy.slug}", status_code=303)
        return RedirectResponse("/projetos", status_code=303)

    @app.post("/projetos/{slug}/delete", response_class=HTMLResponse)
    async def project_delete(request: Request, slug: str):
        form = await request.form()
        if str(form.get("confirm", "")) == slug:
            try:
                projects.delete_project(slug)
            except ValueError:
                pass
        return RedirectResponse("/projetos", status_code=303)

    # ---- Notificações ----

    @app.get("/projetos/{slug}/notificacoes", response_class=HTMLResponse)
    def notifications_page(request: Request, slug: str):
        project = projects.get_project(slug)
        if not project:
            return render(request, "placeholder.html", {"title": "Projeto não encontrado", "description": ""})
        return render(request, "notificacoes.html", {
            "project": project,
            "notifications": notifications.list_notifications(slug),
        })

    @app.post("/projetos/{slug}/notificacoes/{notif_id}/ler", response_class=HTMLResponse)
    async def notification_read(request: Request, slug: str, notif_id: int):
        notifications.mark_read(slug, notif_id)
        return RedirectResponse(f"/projetos/{slug}/notificacoes", status_code=303)

    @app.post("/projetos/{slug}/notificacoes/ler-todas", response_class=HTMLResponse)
    async def notifications_read_all(request: Request, slug: str):
        notifications.mark_all_read(slug)
        return RedirectResponse(f"/projetos/{slug}/notificacoes", status_code=303)

    # ---- Histórico (Tela 13, base) ----

    @app.get("/sistema/historico", response_class=HTMLResponse)
    def history(request: Request):
        return render(request, "historico.html", {"events": list_events(limit=300)})

    @app.get("/sistema/historico/{event_id}")
    def historico_event(event_id: int):
        """Detalhe JSON de um evento de auditoria (modal da tela de histórico)."""
        from jatai.audit.log import get_event
        e = get_event(event_id)
        if not e:
            return JSONResponse({"error": "evento não encontrado"}, status_code=404)
        payload = {}
        try:
            payload = _json.loads(e.payload or "{}")
        except ValueError:
            payload = {"raw": e.payload}
        return JSONResponse({
            "id": e.id,
            "ts": e.ts.isoformat() if e.ts else None,
            "type": e.type, "entity": e.entity, "operation": e.operation,
            "origin": e.origin, "project_slug": e.project_slug,
            "payload": payload,
        })

    # ---- Configurações (Tela 15, base) ----

    @app.get("/sistema/configuracoes", response_class=HTMLResponse)
    def settings_page(request: Request):
        return render(request, "configuracoes.html")

    @app.post("/sistema/configuracoes", response_class=HTMLResponse)
    async def settings_save(request: Request):
        form = await request.form()
        global_settings.save({
            "language": str(form.get("language", "pt-BR")),
            "theme": str(form.get("theme", "light")),
            "port": int(str(form.get("port", "8000"))),
            "core_api_key": str(form.get("core_api_key", "")),
        })
        return RedirectResponse("/sistema/configuracoes", status_code=303)

    # ---- Pesquisa: Configuração (Tela 03) ----

    @app.get("/projetos/{slug}/configuracao", response_class=HTMLResponse)
    def research_config(request: Request, slug: str):
        project = projects.get_project(slug)
        if not project:
            return render(request, "placeholder.html", {"title": "Projeto não encontrado", "description": ""})
        return render(request, "configuracao.html", {
            "project": project,
            "config": research.get_config(slug),
            "areas": research.list_areas(slug),
            "questions": research.list_questions(slug),
            "criteria": research.list_criteria(slug),
            "catalog_fields": research.list_catalog_fields(slug),
            "quality_criteria": research.list_quality_criteria(slug),
            "next_aq": research.next_quality_code(slug),
        })

    @app.post("/projetos/{slug}/configuracao/identificacao", response_class=HTMLResponse)
    async def config_ident(request: Request, slug: str):
        form = await request.form()

        def _coverage(key: str) -> str:
            year = str(form.get(f"{key}_year", "")).strip()
            month = str(form.get(f"{key}_month", "")).strip()
            if not year:
                return ""
            return f"{year}-{month}" if month else year

        research.set_config(slug, {
            "research_name": str(form.get("research_name", "")),
            "coverage_start": _coverage("coverage_start"),
            "coverage_end": _coverage("coverage_end"),
        })
        return RedirectResponse(f"/projetos/{slug}/configuracao", status_code=303)

    @app.post("/projetos/{slug}/configuracao/areas/save", response_class=HTMLResponse)
    async def area_save(request: Request, slug: str):
        form = await request.form()
        # uma linha = palavra-chave; sinônimos da mesma linha unidos por vírgula
        # (o gerador de consultas separa por vírgula -> OR entre sinônimos)
        raw = str(form.get("keywords", "")).replace(";", "\n")
        keywords = []
        for line in raw.splitlines():
            parts = [t.strip() for t in line.split(",") if t.strip()]
            if parts:
                keywords.append(", ".join(dict.fromkeys(parts)))
        research.save_area(slug, _int(form.get("area_id")), str(form.get("name", "")).strip(),
                           str(form.get("status", "ativa")), keywords)
        return RedirectResponse(f"/projetos/{slug}/configuracao#areas", status_code=303)

    @app.post("/projetos/{slug}/configuracao/areas/{area_id}/delete", response_class=HTMLResponse)
    async def area_delete(request: Request, slug: str, area_id: int):
        research.delete_area(slug, area_id)
        return RedirectResponse(f"/projetos/{slug}/configuracao#areas", status_code=303)

    @app.post("/projetos/{slug}/configuracao/questions/save", response_class=HTMLResponse)
    async def question_save(request: Request, slug: str):
        form = await request.form()
        research.save_question(slug, _int(form.get("rq_id")), str(form.get("code", "")).strip(),
                               str(form.get("question", "")).strip(), str(form.get("answer_type", "texto")),
                               form.get("required") == "on", _int(form.get("position"), 0) or 0)
        return RedirectResponse(f"/projetos/{slug}/configuracao#questoes", status_code=303)

    @app.post("/projetos/{slug}/configuracao/questions/{rq_id}/delete", response_class=HTMLResponse)
    async def question_delete(request: Request, slug: str, rq_id: int):
        research.delete_question(slug, rq_id)
        return RedirectResponse(f"/projetos/{slug}/configuracao#questoes", status_code=303)

    @app.post("/projetos/{slug}/configuracao/criteria/save", response_class=HTMLResponse)
    async def criterion_save(request: Request, slug: str):
        form = await request.form()
        research.save_criterion(slug, _int(form.get("criterion_id")), str(form.get("code", "")).strip(),
                                str(form.get("type", "inclusao")), str(form.get("description", "")).strip(),
                                str(form.get("applies_to", "titulo_resumo")),
                                form.get("required") == "on", _int(form.get("position"), 0) or 0)
        return RedirectResponse(f"/projetos/{slug}/configuracao#criterios", status_code=303)

    @app.post("/projetos/{slug}/configuracao/criteria/{criterion_id}/delete", response_class=HTMLResponse)
    async def criterion_delete(request: Request, slug: str, criterion_id: int):
        research.delete_criterion(slug, criterion_id)
        return RedirectResponse(f"/projetos/{slug}/configuracao#criterios", status_code=303)

    @app.post("/projetos/{slug}/configuracao/fields/save", response_class=HTMLResponse)
    async def field_save(request: Request, slug: str):
        form = await request.form()
        research.save_catalog_field(
            slug, _int(form.get("field_id")),
            identifier=str(form.get("identifier", "")).strip(),
            label=str(form.get("label", "")).strip(),
            description=str(form.get("description", "")).strip(),
            data_type=str(form.get("data_type", "texto")),
            origin=str(form.get("origin", "agente")),
            required=form.get("required") == "on",
            active=form.get("active") == "on",
            llm_instruction=str(form.get("llm_instruction", "")).strip(),
            rq_code=str(form.get("rq_code", "")).strip(),
            position=_int(form.get("position"), 0) or 0,
        )
        return RedirectResponse(f"/projetos/{slug}/configuracao#caracterizacao", status_code=303)

    @app.post("/projetos/{slug}/configuracao/fields/{field_id}/delete", response_class=HTMLResponse)
    async def field_delete(request: Request, slug: str, field_id: int):
        research.delete_catalog_field(slug, field_id)
        return RedirectResponse(f"/projetos/{slug}/configuracao#caracterizacao", status_code=303)

    @app.post("/projetos/{slug}/configuracao/qualidade/save", response_class=HTMLResponse)
    async def quality_criterion_save(request: Request, slug: str):
        form = await request.form()
        research.save_quality_criterion(
            slug, _int(form.get("criterion_id")),
            str(form.get("code", "")).strip() or research.next_quality_code(slug),
            str(form.get("description", "")).strip(),
            float(str(form.get("weight", "1") or "1")),
            form.get("required") == "on",
            form.get("active") == "on",
            _int(form.get("position"), 0) or 0)
        return RedirectResponse(f"/projetos/{slug}/configuracao#qualidade", status_code=303)

    @app.post("/projetos/{slug}/configuracao/qualidade/{criterion_id}/delete", response_class=HTMLResponse)
    async def quality_criterion_delete(request: Request, slug: str, criterion_id: int):
        research.delete_quality_criterion(slug, criterion_id)
        return RedirectResponse(f"/projetos/{slug}/configuracao#qualidade", status_code=303)

    # ---- Pesquisa: Fontes (Tela 04) ----

    @app.get("/projetos/{slug}/fontes", response_class=HTMLResponse)
    def sources_page(request: Request, slug: str):
        project = projects.get_project(slug)
        if not project:
            return render(request, "placeholder.html", {"title": "Projeto não encontrado", "description": ""})
        from jatai.sources import base as sources_base
        return render(request, "fontes.html", {
            "project": project,
            "sources": research.list_sources(slug),
            "implemented": sources_base.implemented_source_types(),
            "api_key_required": {
                t for t, c in sources_base.connector_classes().items()
                if c.requires_api_key},
            "homepages": research.SOURCE_HOMEPAGES,
        })

    @app.post("/projetos/{slug}/fontes/{source_id}/toggle", response_class=HTMLResponse)
    async def source_toggle(request: Request, slug: str, source_id: int):
        form = await request.form()
        research.set_source_enabled(slug, source_id, form.get("enabled") == "on")
        return RedirectResponse(f"/projetos/{slug}/fontes", status_code=303)

    @app.post("/projetos/{slug}/fontes/{source_id}/config", response_class=HTMLResponse)
    async def source_config(request: Request, slug: str, source_id: int):
        form = await request.form()
        research.update_source_config(slug, source_id, {
            "api_key": str(form.get("api_key") or ""),
        })
        return RedirectResponse(f"/projetos/{slug}/fontes", status_code=303)

    @app.post("/projetos/{slug}/fontes/import", response_class=HTMLResponse)
    async def sources_import(request: Request, slug: str):
        form = await request.form()
        upload = form.get("file")
        filename = getattr(upload, "filename", "") or ""
        if filename and filename.lower().endswith((".bib", ".ris")):
            import_dir = Path(projects.get_project(slug).workspace) / "imports"  # type: ignore[union-attr]
            import_dir.mkdir(parents=True, exist_ok=True)
            dest = import_dir / Path(filename).name
            dest.write_bytes(await upload.read())
            record_event("source.import", entity="bibtex_ris", operation="upload",
                         project_slug=slug, payload={"file": dest.name})
        return RedirectResponse(f"/projetos/{slug}/fontes", status_code=303)

    # ---- Pesquisa: Consultas (Tela 05) ----

    @app.get("/projetos/{slug}/consultas", response_class=HTMLResponse)
    def queries_page(request: Request, slug: str, geradas: int = 0):
        project = projects.get_project(slug)
        if not project:
            return render(request, "placeholder.html", {"title": "Projeto não encontrado", "description": ""})
        return render(request, "consultas.html", {
            "project": project,
            "queries": research.list_queries(slug),
            "sources": [s for s in research.list_sources(slug) if s.enabled],
            "preview": search_service.preview(slug),
            "execucoes": catalog.executions_by_query(slug),
            "geradas": geradas,
        })

    @app.post("/projetos/{slug}/consultas/gerar", response_class=HTMLResponse)
    async def query_generate(request: Request, slug: str):
        form = await request.form()
        limit = int(str(form.get("max_per_source", "25")) or 25)
        result = search_service.generate(slug, limit=limit, origin="manual")
        return RedirectResponse(f"/projetos/{slug}/consultas?geradas={result['created']}",
                                status_code=303)

    @app.post("/projetos/{slug}/consultas/create", response_class=HTMLResponse)
    async def query_create(request: Request, slug: str):
        form = await request.form()
        expression = str(form.get("expression", "")).strip()
        source = str(form.get("source", ""))
        qid = _int(form.get("query_id"))
        if qid:
            research.update_query(slug, qid, expression, source)
        elif expression:
            research.create_query(slug, expression, source)
        return RedirectResponse(f"/projetos/{slug}/consultas", status_code=303)

    @app.post("/projetos/{slug}/consultas/{query_id}/toggle", response_class=HTMLResponse)
    async def query_toggle(request: Request, slug: str, query_id: int):
        queries = {q.id: q for q in research.list_queries(slug)}
        q = queries.get(query_id)
        if q:
            research.set_query_enabled(slug, query_id, not q.enabled)
        return RedirectResponse(f"/projetos/{slug}/consultas", status_code=303)

    @app.post("/projetos/{slug}/consultas/{query_id}/move", response_class=HTMLResponse)
    async def query_move(request: Request, slug: str, query_id: int):
        form = await request.form()
        research.move_query(slug, query_id, str(form.get("dir", "")))
        return RedirectResponse(f"/projetos/{slug}/consultas", status_code=303)

    @app.post("/projetos/{slug}/consultas/{query_id}/delete", response_class=HTMLResponse)
    async def query_delete(request: Request, slug: str, query_id: int):
        research.delete_query(slug, query_id)
        return RedirectResponse(f"/projetos/{slug}/consultas", status_code=303)

    @app.post("/projetos/{slug}/consultas/{query_id}/approve", response_class=HTMLResponse)
    async def query_approve(request: Request, slug: str, query_id: int):
        research.set_query_status(slug, query_id, "aprovada")
        return RedirectResponse(f"/projetos/{slug}/consultas", status_code=303)

    @app.post("/projetos/{slug}/consultas/{query_id}/status", response_class=HTMLResponse)
    async def query_status(request: Request, slug: str, query_id: int):
        form = await request.form()
        research.set_query_status(slug, query_id, str(form.get("status", "")))
        return RedirectResponse("/projetos/" + slug + "/revisoes", status_code=303)



    # ---- Exportações ----

    @app.get("/projetos/{slug}/exportar")
    def export_catalog(request: Request, slug: str, formato: str = "csv", estado: str = ""):
        from fastapi.responses import Response
        project = projects.get_project(slug)
        result = exporter.export(slug, formato, estado)
        if not project or result is None:
            return render(request, "placeholder.html", {"title": "Exportação indisponível", "description": "Formatos: csv, bibtex, ris, prisma"})
        content, media, fname = result
        return Response(content, media_type=media, headers={
            "Content-Disposition": f'attachment; filename="{fname}"'})

    # ---- Biblioteca: Publicações (Tela 09) e Detalhes (Tela 10) ----

    @app.post("/projetos/{slug}/consultas/{query_id}/executar", response_class=HTMLResponse)
    def query_execute(request: Request, slug: str, query_id: int):
        catalog.execute_query(slug, query_id)
        return RedirectResponse(f"/projetos/{slug}/consultas", status_code=303)

    @app.get("/projetos/{slug}/publicacoes", response_class=HTMLResponse)
    def publications(request: Request, slug: str, q: str = "", year: str = "",
                     fonte: str = "", estado: str = "", pdf: str = "", page: int = 1,
                     sort: str = "ano", dir: str = "desc"):
        project = projects.get_project(slug)
        if not project:
            return render(request, "placeholder.html", {"title": "Projeto não encontrado", "description": ""})
        papers, total = catalog.list_papers(
            slug, q=q, year=year, source=fonte, state=estado, has_pdf=pdf,
            page=page, sort=sort, direction=dir)
        pages = max(1, (total + catalog.PAGE_SIZE - 1) // catalog.PAGE_SIZE)
        return render(request, "publicacoes.html", {
            "project": project,
            "papers": papers,
            "total": total,
            "page": page,
            "pages": pages,
            "filters": {"q": q, "year": year, "fonte": fonte, "estado": estado, "pdf": pdf},
            "sort": sort, "dir": dir,
            "years": catalog.paper_years(slug),
            "sources": catalog.paper_sources(slug),
            "states": catalog.paper_states(slug),
            "qavgs": catalog.quality_avgs(slug),
        })

    @app.post("/projetos/{slug}/publicacoes/bibtex")
    async def publication_bibtex_parse(request: Request, slug: str):
        form = await request.form()
        from jatai.catalog.bibtex import parse_bibtex
        entries = parse_bibtex(str(form.get("bibtex", "")))
        return JSONResponse({"entries": entries, "count": len(entries)})

    @app.post("/projetos/{slug}/publicacoes", response_class=HTMLResponse)
    async def publication_create(request: Request, slug: str):
        form = await request.form()
        fields = {k: str(form.get(k, "")) for k in (
            "title", "authors", "year", "doi", "abstract", "url",
            "venue", "pub_type", "keywords")}
        pid = catalog.create_manual_paper(slug, fields)
        if pid:
            return RedirectResponse(
                f"/projetos/{slug}/publicacoes/{pid}", status_code=303)
        return RedirectResponse(f"/projetos/{slug}/publicacoes", status_code=303)

    @app.get("/projetos/{slug}/publicacoes/{paper_id}", response_class=HTMLResponse)
    def publication_detail(request: Request, slug: str, paper_id: int,
                           evidencia: int = 0, conceito: int = 0):
        project = projects.get_project(slug)
        paper = catalog.get_paper(slug, paper_id)
        if not project or paper is None:
            return render(request, "placeholder.html", {"title": "Publicação não encontrada", "description": ""})
        import json as _json
        provider_names = {p.id: p.name for p in agent_cfg.list_providers(slug)}
        pconcepts = catalog.paper_concepts(slug, paper_id)
        return render(request, "publicacao.html", {
            "project": project,
            "paper": paper,
            "authors": _json.loads(paper.authors or "[]"),
            "decisions": catalog.paper_decisions(slug, paper_id),
            "analyses": catalog.paper_analyses(slug, paper_id),
            "triagens": catalog.triagem_results(slug, [paper_id]).get(paper_id, []),
            "deep": catalog.deep_results(slug, [paper_id]).get(paper_id, []),
            "rq_questions": {q.code: q.question
                             for q in research.list_questions(slug)},
            "evidences": catalog.paper_evidences(slug, paper_id),
            "edit_evidence": catalog.get_evidence(slug, evidencia, paper_id) if evidencia else None,
            "paper_concepts": pconcepts,
            "edit_concept": next(
                (pc for pc in pconcepts if pc["concept"].id == conceito),
                None) if conceito else None,
            "tags": catalog.paper_tags(slug, paper_id),
            "catalog_fields": [f for f in research.list_catalog_fields(slug)
                               if f.active],
            "classif_values": {k: catalog._value_to_input(v) for k, v in
                               catalog.classification_values(slug, paper_id).items()},
            "classif_meta": catalog.classification_meta(slug, paper_id),
            "provenance": catalog.sighting_queries(slug, paper_id),
            "states": catalog.paper_states(slug),
            "quality_criteria": [c for c in research.list_quality_criteria(slug) if c.active],
            "quality_scores": catalog.quality_scores(slug, paper_id),
            "quality_total": catalog.quality_total(slug, paper_id),
            "screening_criteria": [{
                "code": c.code, "type": c.type, "required": c.required,
                "description": c.description, "applies_to": c.applies_to,
            } for c in research.list_criteria(slug)],
            "analysis_task_types": [
                {"type": t, "name": (agent_cfg.task_type_info(t) or {}).get("name", t)}
                for t in sorted(agent_runner.SINGLE_TASK_TYPES)],
            "model_options": [{
                "ref": f"{m.provider_id}:{m.id}",
                "identifier": m.identifier,
                "provider": provider_names.get(m.provider_id, "?"),
                "provider_id": m.provider_id,
                "cost_in": m.cost_in_per_mtok,
                "cost_out": m.cost_out_per_mtok,
                "is_default": bool(m.is_default),
            } for m in agent_cfg.list_models(slug)],
            "provider_names": provider_names,
        })

    @app.post("/projetos/{slug}/publicacoes/{paper_id}/estado", response_class=HTMLResponse)
    async def publication_state(request: Request, slug: str, paper_id: int):
        form = await request.form()
        catalog.set_paper_state(slug, paper_id, str(form.get("state", "")),
                                str(form.get("comment", "")))
        nxt = str(form.get("next", ""))
        if nxt == "list":
            qs = urllib.parse.urlencode(
                {k: v for k in ("q", "year", "fonte", "estado", "pdf", "page")
                 if (v := str(form.get(k, "")).strip())})
            return RedirectResponse(f"/projetos/{slug}/publicacoes?{qs}", status_code=303)
        if nxt == "triagem":
            data = {k: str(form.get(k, "")).strip()
                    for k in ("q", "sort", "dir", "page")}
            tstate = str(form.get("triagem_state", "")).strip()
            if tstate:
                data["state"] = tstate
            qs = urllib.parse.urlencode({k: v for k, v in data.items() if v})
            return RedirectResponse(f"/projetos/{slug}/revisoes?{qs}", status_code=303)
        return RedirectResponse(f"/projetos/{slug}/publicacoes/{paper_id}", status_code=303)

    @app.post("/projetos/{slug}/publicacoes/{paper_id}/delete", response_class=HTMLResponse)
    async def publication_delete(request: Request, slug: str, paper_id: int):
        form = await request.form()
        catalog.delete_paper(slug, paper_id)
        nxt = str(form.get("next", ""))
        if nxt == "list":
            qs = urllib.parse.urlencode(
                {k: v for k in ("q", "year", "fonte", "estado", "pdf", "page")
                 if (v := str(form.get(k, "")).strip())})
            return RedirectResponse(f"/projetos/{slug}/publicacoes?{qs}", status_code=303)
        if nxt == "triagem":
            data = {k: str(form.get(k, "")).strip()
                    for k in ("q", "sort", "dir", "page")}
            tstate = str(form.get("triagem_state", "")).strip()
            if tstate:
                data["state"] = tstate
            qs = urllib.parse.urlencode({k: v for k, v in data.items() if v})
            return RedirectResponse(f"/projetos/{slug}/revisoes?{qs}", status_code=303)
        return RedirectResponse(f"/projetos/{slug}/publicacoes", status_code=303)

    @app.post("/projetos/{slug}/publicacoes/{paper_id}/qualidade", response_class=HTMLResponse)
    async def publication_quality(request: Request, slug: str, paper_id: int):
        form = await request.form()
        for c in research.list_quality_criteria(slug):
            raw = form.get(f"score_{c.id}")
            if raw is None or str(raw).strip() == "":
                continue
            try:
                score = float(str(raw).replace(",", "."))
            except ValueError:
                continue
            catalog.set_quality_score(
                slug, paper_id, c.id, score,
                str(form.get(f"comment_{c.id}", "")).strip())
        return RedirectResponse(f"/projetos/{slug}/publicacoes/{paper_id}#qualidade", status_code=303)

    @app.post("/projetos/{slug}/publicacoes/{paper_id}/evidencias", response_class=HTMLResponse)
    async def evidence_add(request: Request, slug: str, paper_id: int):
        form = await request.form()
        page_raw = str(form.get("page", "")).strip()
        catalog.add_evidence(
            slug, paper_id, str(form.get("excerpt", "")),
            page=int(page_raw) if page_raw.isdigit() else None,
            origin="manual")
        return RedirectResponse(f"/projetos/{slug}/publicacoes/{paper_id}#evidencias", status_code=303)

    @app.post("/projetos/{slug}/publicacoes/{paper_id}/evidencias/{evidence_id}/editar", response_class=HTMLResponse)
    async def evidence_edit(request: Request, slug: str, paper_id: int, evidence_id: int):
        form = await request.form()
        page_raw = str(form.get("page", "")).strip()
        catalog.update_evidence(
            slug, evidence_id, str(form.get("excerpt", "")),
            page=int(page_raw) if page_raw.isdigit() else None)
        return RedirectResponse(f"/projetos/{slug}/publicacoes/{paper_id}#evidencias", status_code=303)

    @app.post("/projetos/{slug}/publicacoes/{paper_id}/analise/{analysis_id}/excluir", response_class=HTMLResponse)
    async def analysis_delete(request: Request, slug: str, paper_id: int, analysis_id: int):
        catalog.delete_analysis(slug, analysis_id)
        return RedirectResponse(f"/projetos/{slug}/publicacoes/{paper_id}#analise", status_code=303)

    @app.post("/projetos/{slug}/publicacoes/{paper_id}/analise/nova", response_class=HTMLResponse)
    async def analysis_run_new(request: Request, slug: str, paper_id: int):
        """Executa uma tarefa de análise avulsa sobre esta publicação."""
        form = await request.form()
        ttype = str(form.get("task_type", ""))
        model_ref = str(form.get("model_ref", "")).strip()
        if ttype not in agent_runner.SINGLE_TASK_TYPES:
            return RedirectResponse(
                f"/projetos/{slug}/publicacoes/{paper_id}#analise", status_code=303)
        if catalog.get_paper(slug, paper_id) is None:
            return RedirectResponse(f"/projetos/{slug}/publicacoes", status_code=303)
        rid = agent_runner.create_analysis_run(slug, paper_id, ttype, model_ref)
        agent_runner.control_run(slug, rid, "iniciar")
        return RedirectResponse(
            f"/projetos/{slug}/agente/execucoes?run={rid}", status_code=303)

    def _screening_form_evals(form, crit_types: dict) -> tuple[str, list]:
        """Extrai veredito geral e avaliações do formulário de triagem
        manual. `res_<CODE>` = incluir|excluir|inconclusivo; o `met` é
        derivado do tipo do critério (IC/EC)."""
        verdict = str(form.get("verdict", "incluir"))
        evals = []
        for key, raw in form.items():
            if not key.startswith("res_"):
                continue
            code = key[4:].strip().upper()
            v = str(raw).strip()
            if not code or v not in ("incluir", "excluir", "inconclusivo"):
                continue
            ctype = crit_types.get(code, "inclusao")
            if v == "inconclusivo":
                met = None
            else:
                is_exclusion = ctype == "exclusao"
                met = (v == "excluir") == is_exclusion
            evals.append({
                "code": code, "met": met, "verdict": v,
                "justification": str(form.get(f"just_{code}", "")),
                "evidence": str(form.get(f"evid_{code}", "")),
            })
        return verdict, evals

    @app.post("/projetos/{slug}/publicacoes/{paper_id}/analise/triagem",
              response_class=HTMLResponse)
    async def screening_manual_add(request: Request, slug: str, paper_id: int):
        """Adiciona uma triagem manual (sem LLM) ao artigo."""
        form = await request.form()
        stage = str(form.get("stage", "titulo_resumo"))
        kind = ("triagem_profunda" if stage == "texto_completo"
                else "triagem")
        crit_types = {c.code.strip().upper(): c.type
                      for c in research.list_criteria(slug)}
        verdict, evals = _screening_form_evals(form, crit_types)
        if catalog.get_paper(slug, paper_id) is None:
            return RedirectResponse(f"/projetos/{slug}/publicacoes",
                                    status_code=303)
        catalog.save_manual_screening(
            slug, paper_id, kind=kind, stage=stage, verdict=verdict,
            evals=evals)
        return RedirectResponse(
            f"/projetos/{slug}/publicacoes/{paper_id}#analise", status_code=303)

    @app.post("/projetos/{slug}/publicacoes/{paper_id}/analise/{analysis_id}/triagem",
              response_class=HTMLResponse)
    async def screening_manual_edit(request: Request, slug: str, paper_id: int,
                                    analysis_id: int):
        """Edita uma triagem existente (agente ou manual) com vereditos
        informados manualmente."""
        import json as _json
        from jatai.storage.models_project import PaperAnalysis
        from jatai.storage.db import get_project_session
        form = await request.form()
        with get_project_session(slug) as s:
            a = s.get(PaperAnalysis, analysis_id)
            if (a is None or a.paper_id != paper_id
                    or not str(a.kind).startswith("triagem")):
                return RedirectResponse(
                    f"/projetos/{slug}/publicacoes/{paper_id}#analise",
                    status_code=303)
            kind, stage = a.kind, "titulo_resumo"
            try:
                stage = str(_json.loads(a.result or "{}").get("stage")
                            or stage)
            except (ValueError, TypeError):
                pass
        crit_types = {c.code.strip().upper(): c.type
                      for c in research.list_criteria(slug)}
        verdict, evals = _screening_form_evals(form, crit_types)
        catalog.save_manual_screening(
            slug, paper_id, kind=kind, stage=stage, verdict=verdict,
            evals=evals, analysis_id=analysis_id)
        return RedirectResponse(
            f"/projetos/{slug}/publicacoes/{paper_id}#analise", status_code=303)

    @app.post("/projetos/{slug}/publicacoes/{paper_id}/evidencias/{evidence_id}/excluir", response_class=HTMLResponse)
    async def evidence_delete(request: Request, slug: str, paper_id: int, evidence_id: int):
        catalog.delete_evidence(slug, evidence_id)
        return RedirectResponse(f"/projetos/{slug}/publicacoes/{paper_id}#evidencias", status_code=303)

    @app.post("/projetos/{slug}/publicacoes/{paper_id}/conceitos", response_class=HTMLResponse)
    async def paper_concept_add(request: Request, slug: str, paper_id: int):
        form = await request.form()
        catalog.save_paper_concept(
            slug, paper_id, str(form.get("term", "")),
            description=str(form.get("description", "")),
            related_terms=str(form.get("related_terms", "")),
            status=str(form.get("status", "")))
        return RedirectResponse(f"/projetos/{slug}/publicacoes/{paper_id}#conceitos", status_code=303)

    @app.post("/projetos/{slug}/publicacoes/{paper_id}/conceitos/{concept_id}/editar", response_class=HTMLResponse)
    async def paper_concept_edit(request: Request, slug: str, paper_id: int, concept_id: int):
        form = await request.form()
        catalog.save_paper_concept(
            slug, paper_id, str(form.get("term", "")),
            description=str(form.get("description", "")),
            related_terms=str(form.get("related_terms", "")),
            concept_id=concept_id,
            status=str(form.get("status", "")))
        return RedirectResponse(f"/projetos/{slug}/publicacoes/{paper_id}#conceitos", status_code=303)

    @app.post("/projetos/{slug}/publicacoes/{paper_id}/classificacao", response_class=HTMLResponse)
    async def publication_classificacao_save(request: Request, slug: str, paper_id: int):
        form = await request.form()
        fields = [f for f in research.list_catalog_fields(slug) if f.active]
        values = {f.identifier: str(form.get(f"v_{f.identifier}", ""))
                  for f in fields}
        catalog.save_classification(slug, paper_id, values, manual=True)
        return RedirectResponse(f"/projetos/{slug}/publicacoes/{paper_id}#classificacao", status_code=303)

    @app.post("/projetos/{slug}/publicacoes/{paper_id}/pdf", response_class=HTMLResponse)
    async def publication_pdf(request: Request, slug: str, paper_id: int):
        form = await request.form()
        upload = form.get("file")
        filename = getattr(upload, "filename", "") or ""
        if filename.lower().endswith(".pdf"):
            catalog.attach_pdf(slug, paper_id, filename, await upload.read())
        return RedirectResponse(f"/projetos/{slug}/publicacoes/{paper_id}#informacoes", status_code=303)

    @app.get("/projetos/{slug}/publicacoes/{paper_id}/arquivo")
    def publication_pdf_file(slug: str, paper_id: int):
        """Serve o PDF local do artigo para o visualizador."""
        paper = catalog.get_paper(slug, paper_id)
        if paper is None or not paper.pdf_path:
            return JSONResponse({"error": "sem pdf"}, status_code=404)
        path = Path(paper.pdf_path)
        try:
            path.resolve().relative_to(
                Path(catalog.projects_workspace(slug)).resolve())
        except ValueError:
            return JSONResponse({"error": "caminho inválido"},
                                status_code=403)
        if not path.exists():
            return JSONResponse({"error": "arquivo não encontrado"},
                                status_code=404)
        return FileResponse(path, media_type="application/pdf",
                            filename=path.name,
                            content_disposition_type="inline")

    @app.get("/projetos/{slug}/publicacoes/{paper_id}/pdf-view",
             response_class=HTMLResponse)
    def publication_pdf_viewer(slug: str, paper_id: int):
        """Página visualizadora que embute o PDF — garante exibição
        inline mesmo quando o navegador está configurado para
        baixar arquivos PDF."""
        paper = catalog.get_paper(slug, paper_id)
        if paper is None or not paper.pdf_path:
            return HTMLResponse("PDF não encontrado", status_code=404)
        return HTMLResponse(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>PDF</title>
<style>html,body{{margin:0;height:100%;overflow:hidden}}
embed{{width:100%;height:100%;border:0}}</style></head>
<body><embed src="/projetos/{slug}/publicacoes/{paper_id}/arquivo"
 type="application/pdf" title="PDF"></body></html>""")

    @app.post("/projetos/{slug}/publicacoes/{paper_id}/browser",
              response_class=HTMLResponse)
    async def publication_open_browser(request: Request, slug: str,
                                       paper_id: int):
        """Abre o Jataí Browser (Chromium visível) na página do paper."""
        from jatai import user_browser
        paper = catalog.get_paper(slug, paper_id)
        back = f"/projetos/{slug}/publicacoes"
        if paper is None:
            return RedirectResponse(back, status_code=303)
        url = (paper.url or "").strip()
        if not url.startswith(("http://", "https://")):
            url = (f"https://doi.org/{paper.doi}" if paper.doi else "")
        if not url.startswith(("http://", "https://")):
            return RedirectResponse(back, status_code=303)
        user_browser.open_for_paper(slug, paper_id, url)
        return RedirectResponse(back, status_code=303)

    @app.post("/projetos/{slug}/browser", response_class=HTMLResponse)
    async def project_open_browser(request: Request, slug: str):
        """Abre o Jataí Browser em branco (ex.: login em portal)."""
        from jatai import user_browser
        user_browser.open_blank(slug)
        return RedirectResponse(f"/projetos/{slug}/publicacoes",
                                status_code=303)

    @app.post("/projetos/{slug}/browser/pipeline",
              response_class=HTMLResponse)
    async def project_browser_pipeline(request: Request, slug: str):
        """Abre o Jataí Browser em modo pipeline: percorre os artigos
        sem PDF aguardando o usuário encontrar cada PDF."""
        from jatai import user_browser
        user_browser.open_pipeline(slug)
        return RedirectResponse(f"/projetos/{slug}/publicacoes",
                                status_code=303)

    _VIEWER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0.0.0 Safari/537.36")

    @app.get("/projetos/{slug}/publicacoes/{paper_id}/ver")
    def publication_viewer(slug: str, paper_id: int, u: str = ""):
        """Proxy local da página da publicação: se o destino for PDF,
        mostra intersticial de salvamento; senão reembrulha o HTML com
        detector de links para PDF (postMessage à página-mãe)."""
        import html as _html
        import httpx

        paper = catalog.get_paper(slug, paper_id)
        if paper is None:
            return HTMLResponse("Publicação não encontrada",
                                status_code=404)
        target = (u or "").strip()
        if not target.startswith(("http://", "https://")):
            target = paper.url or (
                f"https://doi.org/{paper.doi}" if paper.doi else "")
        if not target.startswith(("http://", "https://")):
            return HTMLResponse("<p>Esta publicação não tem URL "
                                "para visualizar.</p>")
        try:
            with httpx.stream("GET", target, timeout=25,
                              headers={"User-Agent": _VIEWER_UA},
                              follow_redirects=True) as resp:
                ctype = (resp.headers.get("content-type") or "").lower()
                chunks, size = [], 0
                for chunk in resp.iter_bytes(65536):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > 8 * 1024 * 1024:
                        break
                data = b"".join(chunks)
                final_url = str(resp.url)
                encoding = resp.encoding or "utf-8"
            head = data[:1024]
            if "pdf" in ctype or head.lstrip().startswith(b"%PDF"):
                return HTMLResponse(_pdf_interstitial(slug, paper_id,
                                                      final_url))
            text = data.decode(encoding, errors="replace")
            # SPA/JS shell (IEEE, ACM...): httpx não renderiza —
            # renderiza com Chromium quando há pouco texto visível
            if _visible_text_len(text) < 400:
                rendered = _browser_render(final_url, _VIEWER_UA)
                if rendered:
                    text = rendered
            return HTMLResponse(_proxied_page(text, final_url))
        except Exception as exc:  # noqa: BLE001
            e = _html.escape(str(exc)[:200])
            eu = _html.escape(target)
            return HTMLResponse(
                f'<div style="font-family:sans-serif;padding:2rem">'
                f"<p>Não foi possível carregar a página remota:</p>"
                f"<p><code>{e}</code></p>"
                f'<p><a href="{eu}" target="_blank" rel="noopener">'
                f"Abrir diretamente</a></p></div>")

    @app.post("/projetos/{slug}/publicacoes/{paper_id}/pdf-url")
    async def publication_pdf_from_url(request: Request, slug: str,
                                       paper_id: int):
        """Baixa um PDF a partir de uma URL descoberta no visualizador
        (link clicado no iframe ou intersticial de PDF direto)."""
        import re as _re
        form = await request.form()
        url = str(form.get("url", "")).strip()
        ajax = str(form.get("ajax", "")) == "1"
        paper = catalog.get_paper(slug, paper_id)
        ok, msg = False, "URL inválida"
        if paper is not None and url.startswith(("http://", "https://")):
            from jatai.documents import acquisition as acq
            from jatai.documents.acquisition.validate import (
                validate_pdf_file)
            tmp = (Path(catalog.projects_workspace(slug)) / "pdfs"
                   / f".tmp-{paper_id}.pdf")
            try:
                if acq.download_pdf(url, tmp):
                    chk = validate_pdf_file(tmp, paper.title)
                    if chk["ok"]:
                        safe = _re.sub(
                            r"[^A-Za-z0-9._-]", "_",
                            url.split("?")[0].split("#")[0]
                            .split("/")[-1]) or f"{paper_id}.pdf"
                        if not safe.lower().endswith(".pdf"):
                            safe += ".pdf"
                        catalog.attach_pdf(slug, paper_id, safe,
                                           tmp.read_bytes())
                        ok, msg = True, "ok"
                    else:
                        msg = chk["reason"]
                else:
                    msg = "download falhou ou conteúdo não é PDF"
            finally:
                tmp.unlink(missing_ok=True)
        if ajax:
            return JSONResponse({"ok": ok, "error": "" if ok else msg})
        return RedirectResponse(
            f"/projetos/{slug}/publicacoes/{paper_id}#informacoes",
            status_code=303)


    # ---- Agente: Pipeline (Tela 06) ----

    @app.get("/projetos/{slug}/agente/pipeline", response_class=HTMLResponse)
    def pipeline(request: Request, slug: str):
        project = projects.get_project(slug)
        if not project:
            return render(request, "placeholder.html", {"title": "Projeto não encontrado", "description": ""})
        import json as _json
        agents = agent_cfg.list_agents(slug)
        for a in agents:
            a.tasks = agent_cfg.agent_tasks(slug, a.id)
            for t in a.tasks:
                try:
                    t.params_dict = _json.loads(t.params or "{}")
                except _json.JSONDecodeError:
                    t.params_dict = {}
        models = agent_cfg.list_models(slug)
        templates = agent_cfg.list_templates(slug)
        provider_names = {p.id: p.name for p in agent_cfg.list_providers(slug)}
        return render(request, "pipeline.html", {
            "project": project, "agents": agents, "models": models,
            "provider_names": provider_names,
            "model_labels": {f"{m.provider_id}:{m.id}":
                             f"{m.identifier} ({provider_names.get(m.provider_id, '?')})"
                             for m in models},
            "model_options": [{
                "ref": f"{m.provider_id}:{m.id}",
                "identifier": m.identifier,
                "provider": provider_names.get(m.provider_id, "?"),
                "provider_id": m.provider_id,
                "cost_in": m.cost_in_per_mtok,
                "cost_out": m.cost_out_per_mtok,
                "is_default": bool(m.is_default),
            } for m in models],
            "templates": templates, "policies": agent_cfg.REVIEW_POLICIES,
            "task_types": agent_cfg.TASK_TYPES,
            "tt_map": {t["type"]: t for t in agent_cfg.TASK_TYPES},
        })

    @app.post("/projetos/{slug}/agente/agentes/{agent_id}/tarefas/add", response_class=HTMLResponse)
    async def agent_task_add(request: Request, slug: str, agent_id: int):
        form = await request.form()
        agent_cfg.add_agent_task(slug, agent_id, str(form.get("task_type", "")))
        return RedirectResponse(f"/projetos/{slug}/agente/pipeline#agente-{agent_id}", status_code=303)

    @app.post("/projetos/{slug}/agente/agentes/{agent_id}/tarefas/{task_id}/remover", response_class=HTMLResponse)
    async def agent_task_remove(request: Request, slug: str, agent_id: int, task_id: int):
        agent_cfg.remove_agent_task(slug, task_id)
        return RedirectResponse(f"/projetos/{slug}/agente/pipeline#agente-{agent_id}", status_code=303)

    @app.post("/projetos/{slug}/agente/agentes/{agent_id}/tarefas/{task_id}/mover", response_class=HTMLResponse)
    async def agent_task_move(request: Request, slug: str, agent_id: int, task_id: int):
        form = await request.form()
        agent_cfg.move_agent_task(slug, task_id, str(form.get("dir", "")))
        return RedirectResponse(f"/projetos/{slug}/agente/pipeline#agente-{agent_id}", status_code=303)

    @app.post("/projetos/{slug}/agente/agentes/{agent_id}/tarefas/{task_type}", response_class=HTMLResponse)
    async def pipeline_task_save(request: Request, slug: str, agent_id: int, task_type: str):
        form = await request.form()
        def f(k, d=""):
            v = form.get(k, d)
            return str(v).strip()
        agent_cfg.save_task_config(
            slug, task_type, agent_id=agent_id,
            enabled=form.get("enabled") == "on" or f("enabled") == "1",
            name=f("name") or None,
            llm_model=f("llm_model") or "",
            prompt_template=f("prompt_template"),
            prompt_version=f("prompt_version") or "1",
            max_tokens=int(f("max_tokens", "0") or 0),
            max_attempts=int(f("max_attempts", "3") or 3),
            review_policy=f("review_policy", "automatica"),
            conditions=f("conditions", "{}"),
            params=_build_params(slug, task_type, agent_id, form),
        )
        return RedirectResponse(f"/projetos/{slug}/agente/pipeline", status_code=303)

    @app.post("/projetos/{slug}/agente/agentes/save", response_class=HTMLResponse)
    async def agent_save(request: Request, slug: str):
        form = await request.form()
        aid = _int(form.get("agent_id"))
        name = str(form.get("name", "")).strip()
        desc = str(form.get("description", "")).strip()
        enabled = form.get("enabled") == "on"
        if aid:
            agent_cfg.save_agent(slug, aid, name, desc, enabled)
        elif name:
            agent_cfg.create_agent(slug, name, desc, enabled=form.get("enabled") == "on")
        return RedirectResponse(f"/projetos/{slug}/agente/pipeline", status_code=303)

    @app.post("/projetos/{slug}/agente/agentes/{agent_id}/delete", response_class=HTMLResponse)
    async def agent_delete(request: Request, slug: str, agent_id: int):
        agent_cfg.delete_agent(slug, agent_id)
        return RedirectResponse(f"/projetos/{slug}/agente/pipeline", status_code=303)

    # ---- Agente: Modelos LLM (Tela 07) ----

    @app.get("/projetos/{slug}/agente/modelos", response_class=HTMLResponse)
    def modelos(request: Request, slug: str):
        project = projects.get_project(slug)
        if not project:
            return render(request, "placeholder.html", {"title": "Projeto não encontrado", "description": ""})
        providers = agent_cfg.list_providers(slug)
        # Auto-sync em segundo plano: provedores nunca sincronizados ou com
        # sync há mais de 10 min têm a lista de modelos atualizada sozinhos.
        now = datetime.now(timezone.utc)
        stale = []
        for p in providers:
            ls = p.last_sync
            if ls is not None and ls.tzinfo is None:
                ls = ls.replace(tzinfo=timezone.utc)
            if ls is None or now - ls > timedelta(minutes=10):
                stale.append(p.id)
        for pid_bg in stale:
            threading.Thread(
                target=agent_cfg.sync_provider, args=(slug, pid_bg),
                daemon=True).start()
        provider_id = request.query_params.get("provider", "")
        q = request.query_params.get("q", "").strip()
        custo = request.query_params.get("custo", "gratis")
        if provider_id == "todos" or not provider_id:
            pid = None  # sem seleção explícita: lista todos os provedores
        elif provider_id.isdigit():
            pid = int(provider_id)
        else:
            pid = providers[0].id if providers else None
        models = agent_cfg.list_models(slug, pid, include_removed=True)
        if q:
            models = [m for m in models
                      if q.lower() in (m.identifier or "").lower()]
        if custo == "gratis":
            models = [m for m in models
                      if not m.cost_in_per_mtok and not m.cost_out_per_mtok]
        provider_names = {p.id: p.name for p in providers}
        edit_id = request.query_params.get("edit", "")
        return render(request, "modelos.html", {
            "project": project, "providers": providers, "models": models,
            "provider_id": pid,
            "provider_sel": provider_id or (str(pid) if pid is not None else "todos"),
            "provider_names": provider_names,
            "q": q, "custo": custo,
            "usage": agent_llm.llm_usage(slug),
            "templates": agent_cfg.list_templates(slug),
            "provider_types": agent_cfg.PROVIDER_TYPES,
            "syncing": bool(stale),
            "edit_provider": agent_cfg.get_provider(slug, int(edit_id))
                            if edit_id.isdigit() else None,
        })

    @app.post("/projetos/{slug}/agente/modelos/provedor", response_class=HTMLResponse)
    async def provider_save(request: Request, slug: str):
        form = await request.form()
        pid = form.get("provider_id")
        api_key = str(form.get("api_key", "")).strip()
        api_key_ref = str(form.get("api_key_ref", "")).strip()
        agent_cfg.save_provider(
            slug, int(pid) if pid and str(pid).isdigit() else None,
            name=str(form.get("name", "")).strip() or "Provedor",
            provider=str(form.get("provider", "ollama")).strip(),
            endpoint=str(form.get("endpoint", "")).strip(),
            api_key_ref=api_key or api_key_ref,
        )
        return RedirectResponse(f"/projetos/{slug}/agente/modelos", status_code=303)

    @app.post("/projetos/{slug}/agente/modelos/provedor/{provider_id}/testar", response_class=HTMLResponse)
    def provider_test(request: Request, slug: str, provider_id: int):
        status = agent_cfg.test_provider(slug, provider_id)
        return RedirectResponse(f"/projetos/{slug}/agente/modelos?status={status}", status_code=303)

    @app.post("/projetos/{slug}/agente/modelos/provedor/{provider_id}/sincronizar", response_class=HTMLResponse)
    def provider_sync(request: Request, slug: str, provider_id: int):
        from urllib.parse import quote
        msg = agent_cfg.sync_provider(slug, provider_id)
        return RedirectResponse(
            f"/projetos/{slug}/agente/modelos?provider={provider_id}&sync={quote(msg)}",
            status_code=303)

    @app.post("/projetos/{slug}/agente/modelos/modelo", response_class=HTMLResponse)
    async def model_save(request: Request, slug: str):
        form = await request.form()
        mid = form.get("model_id")
        agent_cfg.save_model(
            slug, int(mid) if mid and str(mid).isdigit() else None,
            provider_id=int(str(form.get("provider_id", "0")) or 0),
            identifier=str(form.get("identifier", "")).strip(),
            context_window=int(str(form.get("context_window", "0")) or 0),
            max_output=int(str(form.get("max_output", "0")) or 0),
            default_temperature=float(str(form.get("default_temperature", "0") or 0)),
            cost_in=float(str(form.get("cost_in", "0") or 0)),
            cost_out=float(str(form.get("cost_out", "0") or 0)),
            multimodal=form.get("multimodal") == "on",
            is_default=form.get("is_default") == "on",
        )
        return RedirectResponse(f"/projetos/{slug}/agente/modelos", status_code=303)

    @app.post("/projetos/{slug}/agente/modelos/modelo/{model_id}/padrao", response_class=HTMLResponse)
    def model_set_default(request: Request, slug: str, model_id: int):
        provider_id = request.query_params.get("provider", "")
        agent_cfg.set_default_model(slug, model_id)
        qs = (f"provider={provider_id}"
              if provider_id == "todos" or provider_id.isdigit() else "")
        return RedirectResponse(
            f"/projetos/{slug}/agente/modelos?{qs}", status_code=303)

    @app.post("/projetos/{slug}/agente/modelos/template", response_class=HTMLResponse)
    async def template_save(request: Request, slug: str):
        form = await request.form()
        tid = form.get("template_id")
        agent_cfg.save_template(
            slug, int(tid) if tid and str(tid).isdigit() else None,
            name=str(form.get("name", "")).strip() or "template",
            version=str(form.get("version", "1")).strip() or "1",
            task_type=str(form.get("task_type", "")).strip(),
            content=str(form.get("content", "")),
        )
        return RedirectResponse(f"/projetos/{slug}/agente/modelos#templates", status_code=303)

    # ---- Agente: Execuções (Tela 08) ----

    @app.get("/projetos/{slug}/agente/execucoes", response_class=HTMLResponse)
    def execucoes(request: Request, slug: str, run: str = "", page: int = 1):
        project = projects.get_project(slug)
        if not project:
            return render(request, "placeholder.html", {"title": "Projeto não encontrado", "description": ""})
        HIST_PAGE = 15
        page = max(1, page)
        active_runs = agent_runner.list_runs(
            slug, limit=20, statuses=agent_runner.ACTIVE_RUN_STATUSES)
        hist_total = agent_runner.count_runs(
            slug, statuses=agent_runner.FINISHED_RUN_STATUSES)
        hist_pages = max(1, (hist_total + HIST_PAGE - 1) // HIST_PAGE)
        page = min(page, hist_pages)
        runs = agent_runner.list_runs(
            slug, limit=HIST_PAGE, offset=(page - 1) * HIST_PAGE,
            statuses=agent_runner.FINISHED_RUN_STATUSES)
        selected = None
        iterations = []
        run_events = []
        run_totals = None
        if run.isdigit():
            selected = agent_runner.get_run(slug, int(run))
            if selected:
                iterations = agent_runner.run_iterations(slug, selected.id)
                run_events = [
                    dict(ev, detail=agent_runner.describe_run_event(ev))
                    for ev in agent_runner.run_events(slug, selected.id)]
                run_totals = agent_runner.run_totals(slug, selected.id)
        sched = agent_cfg.get_schedule(slug)
        all_agents = agent_cfg.list_agents(slug)
        agent_names = {a.id: a.name for a in all_agents}
        return render(request, "execucoes.html", {
            "project": project, "runs": runs, "active_runs": active_runs,
            "hist_page": page, "hist_pages": hist_pages,
            "hist_total": hist_total,
            "selected": selected,
            "iterations": iterations, "schedule": sched,
            "run_events": run_events, "run_totals": run_totals,
            "agents": [a for a in all_agents if a.enabled],
            "agent_names": agent_names,
        })

    @app.get("/projetos/{slug}/agente/execucoes/{run_id}/evento/{event_id}")
    def run_event_detail(slug: str, run_id: int, event_id: int):
        """Payload integral de um evento do run (modal de detalhe)."""
        ev = agent_runner.run_event(slug, run_id, event_id)
        if not ev:
            return JSONResponse({"error": "evento não encontrado"}, status_code=404)
        return JSONResponse(_json.loads(_json.dumps(ev, default=str)))

    @app.get("/projetos/{slug}/agente/eventos")
    async def agent_events(request: Request, slug: str):
        """Server-Sent Events: stream de eventos do agente em tempo real."""
        q = event_bus.subscribe(slug)

        async def stream():
            try:
                # replay do histórico para dar contexto ao conectar
                for ev in event_bus.history(slug):
                    yield f"data: {_json.dumps(ev, ensure_ascii=False)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.to_thread(q.get, True, 15)
                    except _queue.Empty:
                        yield ": ping\n\n"
                        continue
                    yield f"data: {_json.dumps(ev, ensure_ascii=False)}\n\n"
            finally:
                event_bus.unsubscribe(slug, q)

        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/projetos/{slug}/agente/execucoes/nova", response_class=HTMLResponse)
    async def run_create(request: Request, slug: str):
        form = await request.form()
        kwargs = dict(
            max_iterations=int(str(form.get("max_iterations", "1")) or 1),
            max_papers=int(str(form.get("max_papers", "") or "0") or 0),
            max_cost_usd=(float(str(form.get("max_cost_usd", "")).replace(",", "."))
                          if str(form.get("max_cost_usd", "")).strip() else None),
        )
        agent_sel = str(form.get("agent_id", ""))
        if agent_sel == "todos":
            ids = [a.id for a in agent_cfg.list_agents(slug) if a.enabled]
        elif agent_sel.isdigit():
            ids = [int(agent_sel)]
        else:
            ids = [agent_runner.config_service.default_agent_id(slug)]
        last = None
        for aid in ids:
            if aid is None:
                continue
            rid = agent_runner.create_run(slug, agent_id=aid, **kwargs)
            agent_runner.control_run(slug, rid, "iniciar")
            last = rid
        if last is None:
            return RedirectResponse(f"/projetos/{slug}/agente/execucoes", status_code=303)
        return RedirectResponse(f"/projetos/{slug}/agente/execucoes?run={last}", status_code=303)

    @app.post("/projetos/{slug}/agente/execucoes/{run_id}/{action}", response_class=HTMLResponse)
    def run_control(request: Request, slug: str, run_id: int, action: str):
        agent_runner.control_run(slug, run_id, action)
        return RedirectResponse(f"/projetos/{slug}/agente/execucoes?run={run_id}", status_code=303)

    @app.post("/projetos/{slug}/agente/execucoes/agendamento", response_class=HTMLResponse)
    async def schedule_save(request: Request, slug: str):
        form = await request.form()
        agent_cfg.save_schedule(
            slug,
            frequency=str(form.get("frequency", "manual")),
            enabled=form.get("enabled") == "on",
        )
        return RedirectResponse(f"/projetos/{slug}/agente/execucoes", status_code=303)


    # ---- Biblioteca: Conceitos (Tela 11) ----

    @app.get("/projetos/{slug}/conceitos", response_class=HTMLResponse)
    def conceitos(request: Request, slug: str, status: str = ""):
        project = projects.get_project(slug)
        if not project:
            return render(request, "placeholder.html", {"title": "Projeto não encontrado", "description": ""})
        return render(request, "conceitos.html", {
            "project": project,
            "concepts": catalog.list_concepts(slug, status),
            "status": status,
            "counts": {st: catalog.concept_count(slug, st) for st in ("", "proposto", "aceito", "rejeitado")},
        })

    @app.post("/projetos/{slug}/conceitos/{concept_id}/status", response_class=HTMLResponse)
    async def conceito_status(request: Request, slug: str, concept_id: int):
        form = await request.form()
        status = str(form.get("status", ""))
        catalog.set_concept_status(slug, concept_id, status)
        return RedirectResponse(f"/projetos/{slug}/conceitos?status={status}", status_code=303)

    def _concept_form_ctx(slug: str, concept):
        import json as _json
        related = ""
        if concept is not None:
            try:
                rel = _json.loads(concept.related_terms or "[]")
                if isinstance(rel, list):
                    related = ", ".join(str(t) for t in rel)
            except ValueError:
                related = ""
        return {
            "project": projects.get_project(slug),
            "concept": concept,
            "related_text": related,
            "statuses": ("proposto", "aceito", "rejeitado"),
        }

    @app.get("/projetos/{slug}/conceitos/novo", response_class=HTMLResponse)
    def conceito_novo(request: Request, slug: str):
        project = projects.get_project(slug)
        if not project:
            return render(request, "placeholder.html", {"title": "Projeto não encontrado", "description": ""})
        ctx = _concept_form_ctx(slug, None)
        ctx["action"] = f"/projetos/{slug}/conceitos"
        return render(request, "conceito_form.html", ctx)

    @app.get("/projetos/{slug}/conceitos/{concept_id}/editar", response_class=HTMLResponse)
    def conceito_editar(request: Request, slug: str, concept_id: int):
        project = projects.get_project(slug)
        concept = catalog.get_concept(slug, concept_id)
        if not project or concept is None:
            return render(request, "placeholder.html", {"title": "Conceito não encontrado", "description": ""})
        ctx = _concept_form_ctx(slug, concept)
        ctx["action"] = f"/projetos/{slug}/conceitos/{concept_id}/editar"
        return render(request, "conceito_form.html", ctx)

    @app.post("/projetos/{slug}/conceitos", response_class=HTMLResponse)
    async def conceito_create(request: Request, slug: str):
        form = await request.form()
        catalog.save_concept(
            slug, str(form.get("term", "")),
            description=str(form.get("description", "")),
            related_terms=str(form.get("related_terms", "")),
            status=str(form.get("status", "")))
        return RedirectResponse(f"/projetos/{slug}/conceitos", status_code=303)

    @app.post("/projetos/{slug}/conceitos/{concept_id}/editar", response_class=HTMLResponse)
    async def conceito_update(request: Request, slug: str, concept_id: int):
        form = await request.form()
        catalog.save_concept(
            slug, str(form.get("term", "")),
            description=str(form.get("description", "")),
            related_terms=str(form.get("related_terms", "")),
            status=str(form.get("status", "")),
            concept_id=concept_id)
        return RedirectResponse(f"/projetos/{slug}/conceitos", status_code=303)

    @app.post("/projetos/{slug}/conceitos/{concept_id}/excluir", response_class=HTMLResponse)
    async def conceito_excluir(request: Request, slug: str, concept_id: int):
        form = await request.form()
        catalog.delete_concept(slug, concept_id)
        status = str(form.get("status", ""))
        qs = f"?status={status}" if status else ""
        return RedirectResponse(f"/projetos/{slug}/conceitos{qs}", status_code=303)

    # ---- Biblioteca: Propostas ----

    @app.get("/projetos/{slug}/propostas", response_class=HTMLResponse)
    def propostas_page(request: Request, slug: str, status: str = "",
                       kind: str = ""):
        project = projects.get_project(slug)
        if not project:
            return render(request, "placeholder.html", {"title": "Projeto não encontrado", "description": ""})
        return render(request, "propostas.html", {
            "project": project,
            "proposals": proposals_service.list_proposals(slug, status, kind),
            "counts": proposals_service.counts(slug),
            "kind_counts": proposals_service.kind_counts(slug),
            "status": status, "kind": kind,
            "kinds": proposals_service.KINDS,
            "statuses": proposals_service.STATUSES,
            "priorities": proposals_service.PRIORITIES,
        })

    @app.post("/projetos/{slug}/propostas/gerar", response_class=HTMLResponse)
    def propostas_gerar(request: Request, slug: str):
        """Dispara uma execução avulsa da task 'proposals' (LLM)."""
        rid = agent_runner.create_run(
            slug, max_iterations=1, max_papers=0,
            single={"task_type": "proposals"})
        agent_runner.control_run(slug, rid, "iniciar")
        return RedirectResponse(
            f"/projetos/{slug}/agente/execucoes?run={rid}", status_code=303)

    @app.get("/projetos/{slug}/propostas/novo", response_class=HTMLResponse)
    def proposta_nova(request: Request, slug: str):
        project = projects.get_project(slug)
        if not project:
            return render(request, "placeholder.html", {"title": "Projeto não encontrado", "description": ""})
        return render(request, "proposta_form.html", {
            "project": project, "proposal": None, "action": "",
            "kinds": proposals_service.KINDS,
            "priorities": proposals_service.PRIORITIES,
        })

    @app.post("/projetos/{slug}/propostas", response_class=HTMLResponse)
    async def proposta_create(request: Request, slug: str):
        form = await request.form()
        questions = [q.strip() for q in
                     str(form.get("questions", "")).splitlines() if q.strip()]
        proposals_service.create_proposal(
            slug, title=str(form.get("title", "")),
            kind=str(form.get("kind", "oportunidade")),
            summary=str(form.get("summary", "")),
            rationale=str(form.get("rationale", "")),
            questions=questions,
            priority=str(form.get("priority", "media")),
            notes=str(form.get("notes", "")))
        return RedirectResponse(f"/projetos/{slug}/propostas", status_code=303)

    @app.get("/projetos/{slug}/propostas/{proposal_id}/editar", response_class=HTMLResponse)
    def proposta_editar(request: Request, slug: str, proposal_id: int):
        project = projects.get_project(slug)
        p = proposals_service.get_proposal(slug, proposal_id)
        if not project or p is None:
            return render(request, "placeholder.html", {"title": "Proposta não encontrada", "description": ""})
        return render(request, "proposta_form.html", {
            "project": project, "proposal": p,
            "action": f"/projetos/{slug}/propostas/{proposal_id}/editar",
            "kinds": proposals_service.KINDS,
            "priorities": proposals_service.PRIORITIES,
        })

    @app.post("/projetos/{slug}/propostas/{proposal_id}/editar", response_class=HTMLResponse)
    async def proposta_update(request: Request, slug: str, proposal_id: int):
        form = await request.form()
        questions = [q.strip() for q in
                     str(form.get("questions", "")).splitlines() if q.strip()]
        proposals_service.update_proposal(
            slug, proposal_id,
            title=str(form.get("title", "")),
            kind=str(form.get("kind", "oportunidade")),
            summary=str(form.get("summary", "")),
            rationale=str(form.get("rationale", "")),
            questions=questions,
            priority=str(form.get("priority", "media")),
            notes=str(form.get("notes", "")))
        return RedirectResponse(f"/projetos/{slug}/propostas", status_code=303)

    @app.post("/projetos/{slug}/propostas/{proposal_id}/status", response_class=HTMLResponse)
    async def proposta_status(request: Request, slug: str, proposal_id: int):
        form = await request.form()
        proposals_service.set_status(slug, proposal_id,
                                     str(form.get("status", "")))
        status = str(form.get("filter_status", ""))
        qs = f"?status={status}" if status else ""
        return RedirectResponse(f"/projetos/{slug}/propostas{qs}", status_code=303)

    @app.post("/projetos/{slug}/propostas/{proposal_id}/converter", response_class=HTMLResponse)
    async def proposta_converter(request: Request, slug: str, proposal_id: int):
        form = await request.form()
        destino = proposals_service.convert_proposal(
            slug, proposal_id, str(form.get("destino", "projeto")))
        if destino.startswith("projeto:"):
            return RedirectResponse(f"/projetos/{destino.split(':',1)[1]}",
                                    status_code=303)
        return RedirectResponse(f"/projetos/{slug}/propostas", status_code=303)

    @app.post("/projetos/{slug}/propostas/{proposal_id}/excluir", response_class=HTMLResponse)
    async def proposta_excluir(request: Request, slug: str, proposal_id: int):
        form = await request.form()
        proposals_service.delete_proposal(slug, proposal_id)
        status = str(form.get("status", ""))
        qs = f"?status={status}" if status else ""
        return RedirectResponse(f"/projetos/{slug}/propostas{qs}", status_code=303)

    # ---- Qualidade: Triagem ----

    @app.get("/projetos/{slug}/revisoes", response_class=HTMLResponse)
    def triagem(request: Request, slug: str, q: str = "", page: int = 1,
                sort: str = "ano", dir: str = "desc", state: str = "",
                aba: str = "selecao"):
        project = projects.get_project(slug)
        if not project:
            return render(request, "placeholder.html", {"title": "Projeto não encontrado", "description": ""})
        ctx = {"project": project, "aba": aba, "sort": sort, "dir": dir,
               "q": q, "state": state}
        if aba == "dedup":
            ctx["dedup_groups"] = catalog.list_dedup_groups(slug)
        else:
            papers, total = catalog.list_papers(
                slug, q=q, page=page, sort=sort, direction=dir, state=state,
                states="novo,talvez,pendente")
            ctx.update({"papers": papers, "total": total, "page": page,
                        "pages": max(1, (total + catalog.PAGE_SIZE - 1)
                                     // catalog.PAGE_SIZE),
                        "queries": catalog.pending_agent_queries(slug)})
        return render(request, "revisoes.html", ctx)

    @app.post("/projetos/{slug}/revisoes/dedup/{gid}/mesclar")
    async def dedup_mesclar(request: Request, slug: str, gid: int):
        form = await request.form()
        keep = str(form.get("keep_id", "")).strip()
        catalog.merge_dedup_group(slug, gid,
                                  keep_id=int(keep) if keep.isdigit() else None)
        return RedirectResponse(f"/projetos/{slug}/revisoes?aba=dedup",
                                status_code=303)

    @app.post("/projetos/{slug}/revisoes/dedup/{gid}/ignorar")
    async def dedup_ignorar(request: Request, slug: str, gid: int):
        catalog.dismiss_dedup_group(slug, gid)
        return RedirectResponse(f"/projetos/{slug}/revisoes?aba=dedup",
                                status_code=303)

    # ---- Qualidade: Análise (4 abas) ----

    @app.get("/projetos/{slug}/qualidade/analise", response_class=HTMLResponse)
    def qualidade_analise(request: Request, slug: str, aba: str = "agente",
                         page: int = 1, editar: int = 0):
        project = projects.get_project(slug)
        if not project:
            return render(request, "placeholder.html", {"title": "Projeto não encontrado", "description": ""})
        ctx = {"project": project, "aba": aba, "page": page}
        if aba == "questoes":
            ctx.update(catalog.rq_answers_matrix(slug))
        elif aba == "evidencias":
            evidences, etotal = catalog.list_evidences(slug, page)
            ctx.update({"evidences": evidences, "etotal": etotal,
                        "epages": max(1, (etotal + catalog.PAGE_SIZE - 1)
                                      // catalog.PAGE_SIZE)})
        elif aba == "classificacao":
            ctx.update(catalog.classification_matrix(slug))
            if editar:
                edit_paper = catalog.get_paper(slug, editar)
                if edit_paper is not None:
                    raw_vals = catalog.classification_values(slug, editar)
                    ctx["edit_paper"] = edit_paper
                    ctx["edit_vals"] = {k: catalog._value_to_input(v)
                                        for k, v in raw_vals.items()}
        else:
            aba = "agente"
            ctx["aba"] = aba
            ctx.update(catalog.analysis_overview(slug))
        return render(request, "analise.html", ctx)

    @app.post("/projetos/{slug}/qualidade/analise/classificacao/{paper_id}")
    async def qualidade_classificacao_save(request: Request, slug: str, paper_id: int):
        form = await request.form()
        fields = [f for f in research.list_catalog_fields(slug) if f.active]
        values = {f.identifier: str(form.get(f"v_{f.identifier}", ""))
                  for f in fields}
        catalog.update_classification(slug, paper_id, values)
        return RedirectResponse(
            f"/projetos/{slug}/qualidade/analise?aba=classificacao", status_code=303)

    @app.post("/projetos/{slug}/qualidade/analise/classificacao/{paper_id}/excluir")
    def qualidade_classificacao_delete(request: Request, slug: str, paper_id: int):
        catalog.delete_classification(slug, paper_id)
        return RedirectResponse(
            f"/projetos/{slug}/qualidade/analise?aba=classificacao", status_code=303)

    # ---- Qualidade: Avaliação (grades AQ editáveis) ----

    @app.get("/projetos/{slug}/qualidade/avaliacao", response_class=HTMLResponse)
    def qualidade_avaliacao(request: Request, slug: str, page: int = 1,
                            states: str = "incluido"):
        project = projects.get_project(slug)
        if not project:
            return render(request, "placeholder.html", {"title": "Projeto não encontrado", "description": ""})
        criteria = [c for c in research.list_quality_criteria(slug) if c.active]
        if states == "todos":
            states = ""
        papers, total = catalog.list_papers(slug, page=page, states=states)
        pages = max(1, (total + catalog.PAGE_SIZE - 1) // catalog.PAGE_SIZE)
        pids = [p.id for p in papers]
        weights = {c.id: c.weight for c in criteria}
        ctx = {
            "project": project,
            "criteria": criteria,
            "papers": papers,
            "scores": catalog.quality_scores_bulk(slug, pids),
            "totals": catalog.quality_totals_bulk(slug, pids, weights),
            "total": total, "page": page, "pages": pages, "states": states,
        }
        return render(request, "avaliacao.html", ctx)

    @app.post("/projetos/{slug}/qualidade/avaliacao", response_class=HTMLResponse)
    async def qualidade_avaliacao_save(request: Request, slug: str):
        form = await request.form()
        states = str(form.get("states", "incluido"))
        page = _int(form.get("page"), 1) or 1
        for key, raw in form.items():
            if not key.startswith("s_"):
                continue
            try:
                _, pid_s, cid_s = key.split("_", 2)
                pid, cid = int(pid_s), int(cid_s)
            except (ValueError, TypeError):
                continue
            val = str(raw).strip().replace(",", ".")
            if not val:
                continue
            try:
                score = float(val)
            except ValueError:
                continue
            catalog.set_quality_score(slug, pid, cid, score)
        qs = urllib.parse.urlencode({"states": states, "page": page})
        return RedirectResponse(
            f"/projetos/{slug}/qualidade/avaliacao?{qs}", status_code=303)


    # ---- Tela 02: Dashboard do projeto ----

    @app.get("/projetos/{slug}/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request, slug: str, periodo: str = "semana"):
        project = projects.get_project(slug)
        if not project:
            return render(request, "placeholder.html", {"title": "Projeto não encontrado", "description": ""})
        from jatai.agents import runner as agent_runner
        from jatai.agents import config_service as agent_cfg

        period_days = {"dia": 1, "semana": 7, "mes": 30}.get(periodo, 7)
        stats = catalog.dashboard_stats(slug)
        stats["iterations_total"] = sum(
            len(agent_runner.run_iterations(slug, r.id))
            for r in agent_runner.list_runs(slug))
        from jatai.storage.db import get_project_session
        from jatai.storage.models_project import Paper
        from sqlalchemy import func, select
        with get_project_session(slug) as s:
            stats["with_pdf"] = s.scalar(
                select(func.count(Paper.id)).where(Paper.pdf_path != "")) or 0
        sched = agent_cfg.get_schedule(slug)
        return render(request, "dashboard.html", {
            "project": project,
            "stats": stats,
            "by_year": catalog.papers_by_year(slug),
            "by_source": catalog.papers_by_source(slug),
            "iterations": catalog.iterations_progress(slug, days=period_days),
            "periodo": periodo if periodo in ("dia", "semana", "mes") else "semana",
            "concepts_by_iter": catalog.concepts_by_iteration(slug),
            "latest": catalog.latest_papers(slug),
            "interventions": catalog.recent_interventions(slug),
            "failed_iters": catalog.failed_tasks(slug),
            "flow": catalog.review_flow(slug),
            "usage": agent_llm.llm_usage(slug),
            "next_run": catalog.next_run_estimate(slug),
            "schedule": sched,
        })

    # ---- Tela 14: Exportações e relatórios ----

    @app.get("/projetos/{slug}/exportacoes", response_class=HTMLResponse)
    def exportacoes(request: Request, slug: str):
        project = projects.get_project(slug)
        if not project:
            return render(request, "placeholder.html", {"title": "Projeto não encontrado", "description": ""})
        stats = catalog.dashboard_stats(slug)
        return render(request, "exportacoes.html", {
            "project": project, "stats": stats,
            "formats": exporter.FORMATS,
        })

    # Rotas globais da sidebar -> tela equivalente do projeto atual
    for _global, _sub in list(PROJECT_ROUTES.items()):
        def _make_redirect(sub):
            def _redir(request: Request):
                slug = _current_slug()
                if slug and projects.get_project(slug):
                    return RedirectResponse(f"/projetos/{slug}/{sub}", status_code=303)
                return RedirectResponse("/projetos", status_code=303)
            return _redir
        app.get(_global, response_class=HTMLResponse)(_make_redirect(_sub))

    # ---- Demais telas: placeholder ----

    @app.get("/{path:path}", response_class=HTMLResponse)
    def placeholder(request: Request, path: str):
        for href, label, desc in SCREENS:
            if href == f"/{path}":
                return render(request, "placeholder.html", {"title": label, "description": desc})
        return render(request, "placeholder.html", {"title": "Página não encontrada", "description": ""})

    return app
