"""CRUD de provedores, modelos, templates de prompt e configuração das 10 tarefas."""

import json

from sqlalchemy import select

from lelia.audit.log import record_event
from lelia.storage.db import get_project_session
from sqlalchemy import text

from lelia.storage.models_project import (
    Agent,
    AgentTaskConfig,
    LLMModel,
    LLMProvider,
    PromptTemplate,
    Run,
    Schedule,
)

# Registro de tipos de tarefa disponíveis (padrão Strategy).
# Para adicionar um novo tipo: inclua a entrada aqui e implemente o handler
# em lelia/agents/runner.py com @task("<tipo>").
TASK_TYPES = [
    {"type": "query_builder", "name": "Geração de consultas", "description": "Gera consultas determinísticas (QueryDefinition + adaptadores) a partir das áreas e palavras-chave", "uses_llm": False, "fields": [{"key": "max_queries", "type": "number", "label": "Máx. consultas por iteração", "default": 5}, {"key": "sources", "type": "text", "label": "Fontes (vírgula)", "default": "dblp,openalex"}]},
    {"type": "search", "name": "Busca nas fontes", "description": "Executa consultas pendentes/aprovadas nas fontes", "uses_llm": False, "fields": [{"key": "max_results", "type": "number", "label": "Máx. resultados por consulta", "default": 100}, {"key": "sources", "type": "text", "label": "Fontes (vírgula)", "default": ""}]},
    {"type": "metadata_enrichment", "name": "Enriquecimento de metadados",
     "description": "Baixa abstract e palavras-chave via Chromium para artigos incompletos",
     "uses_llm": False, "fields": [{"key": "max_papers", "type": "number", "label": "Máx. artigos por iteração (0 = sem limite)", "default": 0}]},
    {"type": "dedup", "name": "Deduplicação", "description": "Registra grupos de artigos possivelmente duplicados (DOI/URL ou título similar) para revisão e merge manual na aba Deduplicação; PDFs com hash idêntico geram notificação", "uses_llm": False, "fields": [{"key": "title_threshold", "type": "number", "label": "Limiar similaridade título (0-100)", "default": 92}]},
    {"type": "screener", "name": "Triagem de título/resumo", "description": "Avalia título e resumo contra critérios IC/EC", "uses_llm": True, "fields": [{"key": "batch_size", "type": "number", "label": "Tamanho do lote interno", "default": 5}, {"key": "max_papers", "type": "number", "label": "Máx. artigos por execução (0 = sem limite)", "default": 0}, {"key": "min_confidence", "type": "number", "label": "Confiança mínima p/ incluir (0-1)", "default": 0.6}, {"key": "timeout_s", "type": "number", "label": "Timeout por chamada LLM (s)", "default": 300}]},
    {"type": "acquisition", "name": "Aquisição de texto completo", "description": "Pipeline OA: DOI + fallback por metadados, download httpx/Chromium e sessão autenticada opcional; 0/vazio = sem limite", "uses_llm": False, "fields": [{"key": "max_pdfs", "type": "number", "label": "Máx. PDFs por iteração (0 = sem limite)", "default": 0}, {"key": "sources", "type": "text", "label": "Fontes OA (vírgula)", "default": "unpaywall,openalex,crossref,semanticscholar,core,arxiv"}, {"key": "authenticated", "type": "checkbox", "label": "Última tentativa: sessão autenticada (login via lelia pdf-login)", "default": False}]},
    {"type": "deep_screener", "name": "Triagem profunda (texto completo)", "description": "Segunda fase da triagem: reavalia todos os critérios IC/EC contra o texto completo do PDF dos artigos incluídos; pode reverter a inclusão", "uses_llm": True, "fields": [{"key": "batch_size", "type": "number", "label": "Tamanho do lote interno", "default": 10}, {"key": "max_papers", "type": "number", "label": "Máx. artigos por execução (0 = sem limite)", "default": 0}, {"key": "min_confidence", "type": "number", "label": "Confiança mínima p/ incluir (0-1)", "default": 0.6}, {"key": "timeout_s", "type": "number", "label": "Timeout por chamada LLM (s)", "default": 900}]},
    {"type": "deep_analysis", "name": "Análise profunda", "description": "Resumo, respostas às RQs e evidências a partir do texto completo do PDF", "uses_llm": True, "fields": [{"key": "max_papers", "type": "number", "label": "Máx. artigos por execução (0 = sem limite)", "default": 0}, {"key": "batch_size", "type": "number", "label": "Tamanho do lote interno", "default": 10}, {"key": "min_confidence", "type": "number", "label": "Confiança mínima (0-1)", "default": 0.6}, {"key": "timeout_s", "type": "number", "label": "Timeout por chamada LLM (s)", "default": 900}]},
    {"type": "classification", "name": "Classificação/catalogação", "description": "Preenche campos de caracterização a partir do texto completo do PDF", "uses_llm": True, "fields": [{"key": "max_papers", "type": "number", "label": "Máx. artigos por execução (0 = sem limite)", "default": 0}, {"key": "batch_size", "type": "number", "label": "Tamanho do lote interno", "default": 10}, {"key": "min_confidence", "type": "number", "label": "Confiança mínima (0-1)", "default": 0.6}, {"key": "timeout_s", "type": "number", "label": "Timeout por chamada LLM (s)", "default": 900}]},
    {"type": "concept_extraction", "name": "Extração de conceitos", "description": "Extrai conceitos, técnicas e métricas do texto completo do PDF dos artigos incluídos; deduplica contra o catálogo e alimenta a tela Conceitos e termos", "uses_llm": True, "fields": [{"key": "max_papers", "type": "number", "label": "Máx. artigos por execução (0 = sem limite)", "default": 0}, {"key": "batch_size", "type": "number", "label": "Tamanho do lote interno", "default": 5}, {"key": "max_concepts", "type": "number", "label": "Máx. conceitos por artigo (0 = sem limite)", "default": 3}, {"key": "timeout_s", "type": "number", "label": "Timeout por chamada LLM (s)", "default": 900}]},
    {"type": "snowballing", "name": "Snowballing (referências)", "description": "Para cada artigo incluído, busca as referências no OpenAlex e importa como novas publicações; se a fonte não tiver referências, extrai a lista do PDF via LLM e resolve nas fontes habilitadas; requer fonte OpenAlex habilitada", "uses_llm": False, "fields": [{"key": "max_papers", "type": "number", "label": "Máx. artigos semente por execução (0 = sem limite)", "default": 0}, {"key": "batch_size", "type": "number", "label": "Tamanho do lote interno", "default": 5}, {"key": "max_refs", "type": "number", "label": "Máx. referências por artigo (0 = todas)", "default": 0}, {"key": "pdf_fallback", "type": "checkbox", "label": "Fallback: extrair referências do PDF via LLM quando a fonte não tiver (usa LLM)", "default": True}, {"key": "max_pdf_refs", "type": "number", "label": "Máx. referências extraídas do PDF por artigo (0 = sem limite)", "default": 40}]},
    {"type": "query_expansion", "name": "Expansão de consultas", "description": "Deriva consultas de termos frequentes", "uses_llm": True, "fields": [{"key": "max_terms", "type": "number", "label": "Máx. termos por iteração", "default": 15}, {"key": "min_frequency", "type": "number", "label": "Frequência mínima", "default": 3}, {"key": "timeout_s", "type": "number", "label": "Timeout por chamada LLM (s)", "default": 300}]},
]


PIPELINE_TASKS = [(t["type"], t["name"]) for t in TASK_TYPES]

# Tipos de provedor LLM disponíveis na interface (modelos.html).
PROVIDER_TYPES = [
    {"type": "ollama", "label": "Ollama (local ou cloud)",
     "endpoint": "http://localhost:11434", "needs_key": False},
    {"type": "openrouter", "label": "OpenRouter",
     "endpoint": "https://openrouter.ai/api/v1", "needs_key": True},
    {"type": "google", "label": "Google Gemini",
     "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai",
     "needs_key": True},
    {"type": "openai", "label": "OpenAI",
     "endpoint": "https://api.openai.com/v1", "needs_key": True},
]


def provider_type_info(provider_type: str) -> dict | None:
    return next((t for t in PROVIDER_TYPES if t["type"] == provider_type), None)


def get_provider(slug: str, provider_id: int) -> LLMProvider | None:
    ensure_agent_defaults(slug)
    with get_project_session(slug) as s:
        p = s.get(LLMProvider, provider_id)
        if p is None:
            return None
        s.expunge(p)
        return p


def task_type_info(task_type: str) -> dict | None:
    return next((t for t in TASK_TYPES if t["type"] == task_type), None)

REVIEW_POLICIES = ["automatica", "aprovacao_manual", "amostragem"]



def _ensure_column(s, table: str, column: str, ddl: str) -> None:
    cols = {r[1] for r in s.execute(text(f"PRAGMA table_info({table})"))}
    if column not in cols:
        s.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def _rebuild_task_configs(s) -> None:
    """Remove o UNIQUE(task_type) legado: agora a unicidade é por (agent_id, task_type)."""
    indexes = s.execute(text("PRAGMA index_list(agent_task_config)")).all()
    has_unique = any(r[2] for r in indexes)  # r[2] = unique flag
    if not has_unique:
        return
    s.execute(text("DROP TABLE IF EXISTS agent_task_config_new"))
    s.execute(text("""
        CREATE TABLE agent_task_config_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type VARCHAR(60) NOT NULL,
            agent_id INTEGER,
            name VARCHAR(200),
            enabled BOOLEAN,
            llm_model VARCHAR(200),
            prompt_template VARCHAR(200),
            prompt_version VARCHAR(40),
            max_tokens INTEGER,
            max_attempts INTEGER,
            review_policy VARCHAR(20),
            conditions TEXT,
            params TEXT,
            position INTEGER
        )"""))
    s.execute(text("""
        INSERT INTO agent_task_config_new
        (id, task_type, agent_id, name, enabled, llm_model, prompt_template,
         prompt_version, max_tokens, max_attempts, review_policy, conditions,
         params, position)
        SELECT id, task_type, agent_id, name, enabled, llm_model, prompt_template,
               prompt_version, max_tokens, max_attempts, review_policy, conditions,
               params, position FROM agent_task_config"""))
    s.execute(text("DROP TABLE agent_task_config"))
    s.execute(text("ALTER TABLE agent_task_config_new RENAME TO agent_task_config"))


def default_agent_id(slug: str) -> int | None:
    ensure_agents(slug)
    with get_project_session(slug) as s:
        a = s.scalar(select(Agent).where(Agent.is_default == True))  # noqa: E712
        return a.id if a else None


def ensure_agents(slug: str) -> None:
    """Garante o agente padrão e migra agent_id nas tarefas/runs existentes."""
    with get_project_session(slug) as s:
        _ensure_column(s, "agent_task_config", "agent_id", "INTEGER")
        _ensure_column(s, "run", "agent_id", "INTEGER")
        _rebuild_task_configs(s)
        agent = s.scalar(select(Agent).where(Agent.is_default == True))  # noqa: E712
        if agent is None:
            agent = s.scalar(select(Agent).order_by(Agent.id).limit(1))
        if agent is None:
            agent = Agent(name="Agente principal", description="Agente padrão do projeto",
                          enabled=True, is_default=True)
            s.add(agent)
            s.flush()
        else:
            agent.is_default = True
        # tarefas legadas sem agent_id passam a pertencer ao agente padrão
        s.execute(text("UPDATE agent_task_config SET agent_id = :aid WHERE agent_id IS NULL"),
                  {"aid": agent.id})
        s.execute(text("UPDATE run SET agent_id = :aid WHERE agent_id IS NULL"),
                  {"aid": agent.id})
        s.commit()


# ---------- Agentes ----------

def list_agents(slug: str) -> list[Agent]:
    ensure_agents(slug)
    with get_project_session(slug) as s:
        return list(s.scalars(select(Agent).order_by(Agent.is_default.desc(), Agent.id)))


def agent_tasks(slug: str, agent_id: int) -> list[AgentTaskConfig]:
    with get_project_session(slug) as s:
        return list(s.scalars(select(AgentTaskConfig)
                              .where(AgentTaskConfig.agent_id == agent_id)
                              .order_by(AgentTaskConfig.position, AgentTaskConfig.id)))


def create_agent(slug: str, name: str, description: str = "", enabled: bool = True) -> int:
    """Cria agente vazio; as tarefas são adicionadas via Builder (add_agent_task)."""
    ensure_agents(slug)
    with get_project_session(slug) as s:
        agent = Agent(name=name, description=description, enabled=enabled)
        s.add(agent)
        s.commit()
        aid = agent.id
    record_event("agent.created", entity=f"agent:{aid}", operation="create",
                 project_slug=slug, payload={"name": name})
    return aid


def add_agent_task(slug: str, agent_id: int, task_type: str) -> int:
    """Adiciona um tipo de tarefa ao agente (Strategy registrada em TASK_TYPES)."""
    info = task_type_info(task_type)
    if info is None:
        return -1
    ensure_agents(slug)
    with get_project_session(slug) as s:
        existing = s.scalar(select(AgentTaskConfig).where(
            AgentTaskConfig.agent_id == agent_id,
            AgentTaskConfig.task_type == task_type))
        if existing:
            return existing.id
        pos = next((i for i, t in enumerate(TASK_TYPES) if t["type"] == task_type), 0)
        t = AgentTaskConfig(task_type=task_type, agent_id=agent_id,
                            name=info["name"], enabled=True, position=pos,
                            review_policy="automatica", max_attempts=3)
        s.add(t)
        s.commit()
        tid = t.id
    record_event("agent_task.added", entity=f"agent_task:{tid}", operation="create",
                 project_slug=slug, payload={"agent_id": agent_id, "task_type": task_type})
    return tid


def remove_agent_task(slug: str, task_config_id: int) -> None:
    with get_project_session(slug) as s:
        t = s.get(AgentTaskConfig, task_config_id)
        if t is None:
            return
        ttype, aid = t.task_type, t.agent_id
        s.delete(t)
        s.commit()
    record_event("agent_task.removed", entity=f"agent_task:{task_config_id}",
                 operation="delete", project_slug=slug,
                 payload={"agent_id": aid, "task_type": ttype})


def save_agent(slug: str, agent_id: int, name: str, description: str,
               enabled: bool) -> None:
    with get_project_session(slug) as s:
        a = s.get(Agent, agent_id)
        if a is None:
            return
        a.name, a.description, a.enabled = name, description, enabled
        s.commit()
    record_event("agent.updated", entity=f"agent:{agent_id}", operation="update",
                 project_slug=slug, payload={"name": name})


def delete_agent(slug: str, agent_id: int) -> None:
    with get_project_session(slug) as s:
        a = s.get(Agent, agent_id)
        if a is None or a.is_default:
            return
        agents = list(s.scalars(select(Agent)))
        if len(agents) <= 1:
            return
        for t in s.scalars(select(AgentTaskConfig).where(AgentTaskConfig.agent_id == agent_id)):
            s.delete(t)
        s.execute(text("UPDATE run SET agent_id = NULL WHERE agent_id = :aid"), {"aid": agent_id})
        s.delete(a)
        s.commit()
    record_event("agent.deleted", entity=f"agent:{agent_id}", operation="delete",
                 project_slug=slug)


def ensure_agent_defaults(slug: str) -> None:
    ensure_agents(slug)
    """Semeia provider Ollama, modelos do /api/tags e as 10 tarefas do pipeline."""
    with get_project_session(slug) as s:
        provider = s.scalar(select(LLMProvider).where(LLMProvider.provider == "ollama"))
        if provider is None:
            provider = LLMProvider(name="Ollama local", provider="ollama",
                                   endpoint="http://localhost:11434", status="desconhecido")
            s.add(provider)
            s.flush()
        # modelos descobertos no Ollama
        try:
            from lelia.agents.llm import ollama_tags
            tags = ollama_tags(provider.endpoint)
        except Exception:
            tags = []
        existing = {m.identifier for m in s.scalars(select(LLMModel).where(LLMModel.provider_id == provider.id))}
        first = True
        for t in tags:
            name = t.get("name", "")
            if not name or name in existing:
                continue
            s.add(LLMModel(provider_id=provider.id, identifier=name,
                           context_window=(t.get("details") or {}).get("context_length") or 0,
                           is_default=first))
            first = False
        if first and not s.scalar(select(LLMModel).where(LLMModel.is_default == True)):  # noqa: E712
            s.add(LLMModel(provider_id=provider.id, identifier="", is_default=True))
        s.commit()


def list_task_configs(slug: str, agent_id: int | None = None) -> list[AgentTaskConfig]:
    ensure_agent_defaults(slug)
    with get_project_session(slug) as s:
        stmt = select(AgentTaskConfig)
        if agent_id is not None:
            stmt = stmt.where(AgentTaskConfig.agent_id == agent_id)
        return list(s.scalars(stmt.order_by(AgentTaskConfig.agent_id, AgentTaskConfig.position, AgentTaskConfig.id)))


def get_task_config(slug: str, task_type: str, agent_id: int | None = None) -> AgentTaskConfig | None:
    with get_project_session(slug) as s:
        stmt = select(AgentTaskConfig).where(AgentTaskConfig.task_type == task_type)
        if agent_id is not None:
            stmt = stmt.where(AgentTaskConfig.agent_id == agent_id)
        return s.scalar(stmt)


def save_task_config(slug: str, task_type: str, agent_id: int | None = None, **fields) -> None:
    with get_project_session(slug) as s:
        stmt = select(AgentTaskConfig).where(AgentTaskConfig.task_type == task_type)
        if agent_id is not None:
            stmt = stmt.where(AgentTaskConfig.agent_id == agent_id)
        t = s.scalar(stmt)
        if t is None:
            return
        for k in ("enabled", "llm_model", "prompt_template", "prompt_version",
                  "max_tokens", "max_attempts", "review_policy", "conditions", "params", "name"):
            if k in fields and fields[k] is not None:
                setattr(t, k, fields[k])
        s.commit()
    record_event("agent_task.updated", entity=f"task:{task_type}", operation="update",
                 project_slug=slug, payload={k: str(v)[:200] for k, v in fields.items()})


# ---------- Provedores ----------

def list_providers(slug: str) -> list[LLMProvider]:
    ensure_agent_defaults(slug)
    with get_project_session(slug) as s:
        return list(s.scalars(select(LLMProvider).order_by(LLMProvider.id)))


def save_provider(slug: str, provider_id: int | None, name: str, provider: str,
                  endpoint: str, api_key_ref: str = "") -> int:
    with get_project_session(slug) as s:
        if provider_id:
            p = s.get(LLMProvider, provider_id)
        else:
            p = LLMProvider(name=name, provider=provider)
        if p is None:
            return -1
        p.name, p.provider, p.endpoint = name, provider, endpoint
        if api_key_ref:
            p.api_key_ref = api_key_ref
        s.add(p)
        s.commit()
        pid = p.id
    record_event("llm_provider.saved", entity=f"llm_provider:{pid}", operation="upsert",
                 project_slug=slug)
    return pid


def test_provider(slug: str, provider_id: int) -> str:
    from lelia.agents.llm import provider_status

    with get_project_session(slug) as s:
        p = s.get(LLMProvider, provider_id)
        if p is None:
            return "desconhecido"
        status, detail = provider_status(p)
        p.status = status
        p.last_error = "" if status == "disponivel" else detail
        s.commit()
    record_event("llm_provider.tested", entity=f"llm_provider:{provider_id}",
                 operation="test", project_slug=slug,
                 payload={"status": status, "error": detail or None})
    return status


def sync_openai_models(slug: str, provider_id: int) -> str:
    """Sincroniza modelos de provedores compatíveis com OpenAI
    (openrouter, google...): importa identificadores do GET /models e,
    no OpenRouter, preenche os custos a partir do pricing retornado."""
    from lelia.agents.llm import openai_models

    with get_project_session(slug) as s:
        p = s.get(LLMProvider, provider_id)
        if p is None:
            return "provedor não encontrado"
        try:
            remote = openai_models(p)
        except Exception as exc:  # noqa: BLE001
            return f"provedor indisponível: {str(exc)[:120]}"
        by_id = {str(m.get("id", "")): m for m in remote if m.get("id")}
        models = list(s.scalars(select(LLMModel).where(
            LLMModel.provider_id == provider_id)))
        existing = {m.identifier: m for m in models}
        added = removed = 0
        for ident, info in by_id.items():
            pricing = info.get("pricing") or {}
            try:
                cost_in = float(pricing.get("prompt") or 0) * 1_000_000
                cost_out = float(pricing.get("completion") or 0) * 1_000_000
            except (TypeError, ValueError):
                cost_in = cost_out = 0.0
            ctx = int(info.get("context_length") or 0)
            if ident in existing:
                m = existing[ident]
                if m.status == "removido":
                    m.status = "ativo"
                if cost_in or cost_out:
                    m.cost_in_per_mtok, m.cost_out_per_mtok = cost_in, cost_out
                if ctx:
                    m.context_window = ctx
            else:
                m = LLMModel(provider_id=provider_id, identifier=ident,
                             context_window=ctx, status="ativo",
                             cost_in_per_mtok=cost_in,
                             cost_out_per_mtok=cost_out)
                s.add(m)
                added += 1
        for m in models:
            if m.identifier and m.identifier not in by_id and m.status != "removido":
                m.status = "removido"
                if m.is_default:
                    m.is_default = False
                removed += 1
        s.commit()
        parts = []
        if added:
            parts.append(f"{added} novo(s)")
        if removed:
            parts.append(f"{removed} removido(s)")
        return ", ".join(parts) if parts else "nenhuma alteração"


def sync_provider(slug: str, provider_id: int) -> str:
    """Dispatcher: Ollama usa /api/tags; demais tipos usam /models
    (API compatível com OpenAI). Registra last_sync ao final."""
    from datetime import datetime, timezone

    p = get_provider(slug, provider_id)
    if p is None:
        return "provedor não encontrado"
    from lelia.agents.llm import OPENAI_COMPAT_PROVIDERS
    if p.provider in OPENAI_COMPAT_PROVIDERS:
        msg = sync_openai_models(slug, provider_id)
    else:
        msg = sync_ollama_models(slug, provider_id)
    with get_project_session(slug) as s:
        row = s.get(LLMProvider, provider_id)
        if row is not None:
            row.last_sync = datetime.now(timezone.utc)
            s.commit()
    return msg


# ---------- Modelos ----------

def list_models(slug: str, provider_id: int | None = None,
                include_removed: bool = False) -> list[LLMModel]:
    with get_project_session(slug) as s:
        stmt = select(LLMModel)
        if provider_id:
            stmt = stmt.where(LLMModel.provider_id == provider_id)
        if not include_removed:
            stmt = stmt.where(LLMModel.status != "removido")
        return list(s.scalars(stmt.order_by(LLMModel.id)))


def sync_ollama_models(slug: str, provider_id: int) -> str:
    """Sincroniza modelos do provedor Ollama: importa novos, atualiza contexto
    e marca como 'removido' os que não existem mais no /api/tags."""
    from lelia.agents.llm import ollama_tags

    with get_project_session(slug) as s:
        p = s.get(LLMProvider, provider_id)
        if p is None:
            return "provedor não encontrado"
        try:
            tags = ollama_tags(p.endpoint)
        except Exception:
            return "ollama indisponível"
        names = {t.get("name", "") for t in tags if t.get("name")}
        models = list(s.scalars(select(LLMModel).where(LLMModel.provider_id == provider_id)))
        existing = {m.identifier: m for m in models}
        added = removed = 0
        for t in tags:
            name = t.get("name", "")
            if not name:
                continue
            ctx = (t.get("details") or {}).get("context_length") or 0
            if name in existing:
                m = existing[name]
                if ctx:
                    m.context_window = ctx
                if m.status == "removido":
                    m.status = "ativo"
            else:
                m = LLMModel(provider_id=provider_id, identifier=name,
                             context_window=ctx, status="ativo")
                s.add(m)
                added += 1
        for m in models:
            if m.identifier and m.identifier not in names and m.status != "removido":
                m.status = "removido"
                if m.is_default:
                    m.is_default = False
                removed += 1
        s.commit()
        parts = []
        if added:
            parts.append(f"{added} novo(s)")
        if removed:
            parts.append(f"{removed} removido(s) do Ollama")
        return ", ".join(parts) if parts else "nenhuma alteração"


def save_model(slug: str, model_id: int | None, provider_id: int, identifier: str,
               context_window: int = 0, max_output: int = 0,
               default_temperature: float = 0.0,
               cost_in: float = 0.0, cost_out: float = 0.0,
               multimodal: bool = False, is_default: bool = False) -> int:
    with get_project_session(slug) as s:
        if is_default:
            for m in s.scalars(select(LLMModel)):
                m.is_default = False
        if model_id:
            m = s.get(LLMModel, model_id)
        else:
            m = LLMModel(provider_id=provider_id, identifier=identifier)
        if m is None:
            return -1
        m.provider_id, m.identifier = provider_id, identifier
        m.context_window, m.max_output = context_window, max_output
        m.default_temperature, m.cost_in_per_mtok, m.cost_out_per_mtok = (
            default_temperature, cost_in, cost_out)
        m.multimodal, m.is_default = multimodal, is_default
        s.add(m)
        s.commit()
        mid = m.id
    return mid


def set_default_model(slug: str, model_id: int) -> bool:
    """Define o modelo padrão do projeto (usado por tarefas sem modelo
    próprio). Apenas um is_default por vez, entre todos os provedores."""
    with get_project_session(slug) as s:
        m = s.get(LLMModel, model_id)
        if m is None or m.status == "removido":
            return False
        for other in s.scalars(select(LLMModel)):
            other.is_default = other.id == model_id
        s.commit()
    record_event("llm_model.default", entity=f"llm_model:{model_id}",
                 operation="update", project_slug=slug)
    return True


# ---------- Templates de prompt ----------

def list_templates(slug: str) -> list[PromptTemplate]:
    with get_project_session(slug) as s:
        return list(s.scalars(select(PromptTemplate).order_by(PromptTemplate.name, PromptTemplate.version)))


def save_template(slug: str, template_id: int | None, name: str, version: str,
                  task_type: str, content: str) -> int:
    with get_project_session(slug) as s:
        if template_id:
            t = s.get(PromptTemplate, template_id)
        else:
            t = PromptTemplate(name=name, version=version)
        if t is None:
            return -1
        t.name, t.version, t.task_type, t.content = name, version, task_type, content
        s.add(t)
        s.commit()
        tid = t.id
    record_event("prompt_template.saved", entity=f"prompt_template:{tid}", operation="upsert",
                 project_slug=slug)
    return tid


# ---------- Agendamentos ----------

def get_schedule(slug: str) -> Schedule | None:
    with get_project_session(slug) as s:
        return s.scalar(select(Schedule).order_by(Schedule.id.desc()).limit(1))


def save_schedule(slug: str, frequency: str, enabled: bool,
                  start_at=None, end_at=None) -> None:
    with get_project_session(slug) as s:
        sched = s.scalar(select(Schedule).order_by(Schedule.id.desc()).limit(1))
        if sched is None:
            sched = Schedule()
            s.add(sched)
        sched.frequency, sched.enabled = frequency, enabled
        sched.start_at, sched.end_at = start_at, end_at
        s.commit()
    record_event("schedule.saved", entity="schedule", operation="upsert", project_slug=slug,
                 payload={"frequency": frequency, "enabled": enabled})


def serialize(obj) -> dict:
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


def task_to_dict(t: AgentTaskConfig) -> dict:
    d = serialize(t)
    d["params"] = json.loads(t.params or "{}")
    d["conditions"] = json.loads(t.conditions or "{}")
    return d

def move_agent_task(slug: str, task_id: int, direction: str) -> None:
    """Move a tarefa para cima/baixo dentro do agente e renormaliza posições."""
    with get_project_session(slug) as s:
        t = s.get(AgentTaskConfig, task_id)
        if t is None or t.agent_id is None:
            return
        tasks = list(s.scalars(
            select(AgentTaskConfig)
            .where(AgentTaskConfig.agent_id == t.agent_id)
            .order_by(AgentTaskConfig.position, AgentTaskConfig.id)))
        idx = next((i for i, x in enumerate(tasks) if x.id == task_id), -1)
        if idx < 0:
            return
        j = idx - 1 if direction == "up" else idx + 1
        if j < 0 or j >= len(tasks):
            return
        tasks[idx], tasks[j] = tasks[j], tasks[idx]
        for i, x in enumerate(tasks):
            x.position = i
        s.commit()
    record_event("agent_task.moved", entity=f"agent_task:{task_id}",
                 operation="update", project_slug=slug, payload={"direction": direction})
