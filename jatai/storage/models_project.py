"""Modelos do banco por-projeto (dados de pesquisa)."""

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jatai.storage.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProjectConfig(Base):
    """Configurações-chave do projeto de pesquisa (aba Identificação etc.)."""

    __tablename__ = "project_config"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class ResearchArea(Base):
    __tablename__ = "research_area"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ativa")  # ativa | inativa
    position: Mapped[int] = mapped_column(Integer, default=0)
    keywords: Mapped[list["Keyword"]] = relationship(back_populates="area", cascade="all, delete-orphan")


class Keyword(Base):
    __tablename__ = "keyword"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    area_id: Mapped[int] = mapped_column(ForeignKey("research_area.id", ondelete="CASCADE"))
    term: Mapped[str] = mapped_column(String(300), nullable=False)
    area: Mapped[ResearchArea] = relationship(back_populates="keywords")


class ResearchQuestion(Base):
    __tablename__ = "research_question"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False)  # RQ1, RQ2...
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer_type: Mapped[str] = mapped_column(String(40), default="texto")
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(Integer, default=0)


class Criterion(Base):
    __tablename__ = "criterion"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False)  # IC01, EC01...
    type: Mapped[str] = mapped_column(String(10), nullable=False)  # inclusao | exclusao
    description: Mapped[str] = mapped_column(Text, nullable=False)
    applies_to: Mapped[str] = mapped_column(String(30), default="titulo_resumo")
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(Integer, default=0)


class CatalogField(Base):
    """Caracterização científica: atributos extraídos de cada publicação."""

    __tablename__ = "catalog_field"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    identifier: Mapped[str] = mapped_column(String(120), nullable=False)
    label: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    data_type: Mapped[str] = mapped_column(String(30), default="texto")
    origin: Mapped[str] = mapped_column(String(30), default="agente")
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    llm_instruction: Mapped[str] = mapped_column(Text, default="")
    rq_code: Mapped[str] = mapped_column(String(20), default="")
    position: Mapped[int] = mapped_column(Integer, default=0)


class ProjectSource(Base):
    __tablename__ = "project_source"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)  # dblp, openalex...
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Query(Base):
    __tablename__ = "query"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    expression: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(40), default="")
    iteration: Mapped[int] = mapped_column(Integer, default=0)
    origin: Mapped[str] = mapped_column(String(20), default="manual")  # manual | agent | expanded
    related_terms: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="pendente")  # pendente|executada|erro|aprovada
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)  # disponível p/ agentes
    sort_order: Mapped[int] = mapped_column(Integer, default=0)  # ordem de execução definida pelo usuário
    results_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QueryExecution(Base):
    """Trilha de auditoria de cada execução de consulta numa fonte."""

    __tablename__ = "query_execution"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    query_id: Mapped[int] = mapped_column(Integer, index=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    iteration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(40), default="")
    expression: Mapped[str] = mapped_column(Text, default="")
    request: Mapped[str] = mapped_column(Text, default="")  # JSON: url/params
    status: Mapped[str] = mapped_column(String(20), default="")  # ok|vazio|erro
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    results_count: Mapped[int] = mapped_column(Integer, default=0)
    new_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    filtered_count: Mapped[int] = mapped_column(Integer, default=0)
    pages: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Paper(Base):
    __tablename__ = "paper"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    authors: Mapped[str] = mapped_column(Text, default="")  # JSON list
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    doi: Mapped[str] = mapped_column(String(300), default="", index=True)
    abstract: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(40), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    venue: Mapped[str] = mapped_column(String(300), default="")
    pub_type: Mapped[str] = mapped_column(String(60), default="")
    keywords: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(20), default="novo", index=True)
    pdf_path: Mapped[str] = mapped_column(String(500), default="")
    pdf_hash: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    sightings: Mapped[list["PaperSighting"]] = relationship(back_populates="paper", cascade="all, delete-orphan")


class PaperSighting(Base):
    """Proveniência: em qual consulta/fonte o artigo apareceu."""

    __tablename__ = "paper_sighting"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("paper.id", ondelete="CASCADE"), index=True)
    query_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(40), default="")
    external_id: Mapped[str] = mapped_column(String(300), default="")
    raw: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    paper: Mapped[Paper] = relationship(back_populates="sightings")


class PaperFile(Base):
    __tablename__ = "paper_file"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("paper.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), default="pdf")
    origin: Mapped[str] = mapped_column(String(20), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Decision(Base):
    __tablename__ = "decision"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("paper.id", ondelete="CASCADE"), index=True)
    criterion_code: Mapped[str] = mapped_column(String(20), default="")
    verdict: Mapped[str] = mapped_column(String(20), nullable=False)  # incluir | excluir | talvez
    justification: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(20), default="agent")  # agent | human
    iteration: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PaperAnalysis(Base):
    __tablename__ = "paper_analysis"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("paper.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40), default="")
    result: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("paper.id", ondelete="CASCADE"), index=True)
    decision_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    origin: Mapped[str] = mapped_column(String(20), default="agente")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Tag(Base):
    __tablename__ = "tag"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)


class PaperTag(Base):
    __tablename__ = "paper_tag"

    paper_id: Mapped[int] = mapped_column(ForeignKey("paper.id", ondelete="CASCADE"), primary_key=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True)


class Concept(Base):
    """Termos/conceitos extraídos durante a análise (Tela 11)."""

    __tablename__ = "concept"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    term: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(40), default="")
    frequency: Mapped[int] = mapped_column(Integer, default=1)
    iteration: Mapped[int] = mapped_column(Integer, default=0)
    area_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="proposto")  # proposto|aceito|rejeitado
    related_terms: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConceptPaper(Base):
    """Vínculo conceito↔artigo: de quais publicações cada conceito veio
    (extração do agente ou inclusão manual na ficha da publicação)."""

    __tablename__ = "concept_paper"

    concept_id: Mapped[int] = mapped_column(ForeignKey("concept.id", ondelete="CASCADE"), primary_key=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("paper.id", ondelete="CASCADE"), primary_key=True)
    origin: Mapped[str] = mapped_column(String(20), default="agente")  # agente | manual
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DedupGroup(Base):
    """Grupo de artigos possivelmente duplicados, registrado pela task
    dedup para revisão humana (aba Deduplicação da Triagem)."""

    __tablename__ = "dedup_group"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_ids: Mapped[str] = mapped_column(Text, default="[]")  # JSON list[int]
    reason: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[float] = mapped_column(default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="pendente")  # pendente|mesclado|ignorado
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HumanIntervention(Base):
    __tablename__ = "human_intervention"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity: Mapped[str] = mapped_column(String(120), nullable=False)
    field: Mapped[str] = mapped_column(String(80), default="")
    old_value: Mapped[str] = mapped_column(Text, default="")
    new_value: Mapped[str] = mapped_column(Text, default="")
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LLMCall(Base):
    __tablename__ = "llm_call"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task: Mapped[str] = mapped_column(String(80), default="")
    model: Mapped[str] = mapped_column(String(200), default="")
    prompt_template: Mapped[str] = mapped_column(String(200), default="")
    prompt_version: Mapped[str] = mapped_column(String(40), default="")
    params: Mapped[str] = mapped_column(Text, default="{}")
    prompt: Mapped[str] = mapped_column(Text, default="")
    response_raw: Mapped[str] = mapped_column(Text, default="")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Agent(Base):
    """Agente configurável: conjunto de tarefas do pipeline que ele executa."""

    __tablename__ = "agent"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Run(Base):
    __tablename__ = "run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(20), default="pendente")
    agent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    config_snapshot: Mapped[str] = mapped_column(Text, default="{}")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Iteration(Base):
    __tablename__ = "iteration"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("run.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer, default=1)
    queries_count: Mapped[int] = mapped_column(Integer, default=0)
    new_papers: Mapped[int] = mapped_column(Integer, default=0)
    new_terms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="pendente")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunEvent(Base):
    """Evento persistido de uma execução — histórico de ações do agente."""
    __tablename__ = "run_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("run.id", ondelete="CASCADE"), index=True)
    iteration: Mapped[int] = mapped_column(Integer, default=0)
    type: Mapped[str] = mapped_column(String(40), default="")
    payload: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentTaskConfig(Base):
    __tablename__ = "agent_task_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_type: Mapped[str] = mapped_column(String(60), nullable=False)
    agent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    llm_model: Mapped[str] = mapped_column(String(200), default="")
    prompt_template: Mapped[str] = mapped_column(String(200), default="")
    prompt_version: Mapped[str] = mapped_column(String(40), default="")
    max_tokens: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    review_policy: Mapped[str] = mapped_column(String(20), default="automatica")
    conditions: Mapped[str] = mapped_column(Text, default="{}")
    params: Mapped[str] = mapped_column(Text, default="{}")
    position: Mapped[int] = mapped_column(Integer, default=0)


class LLMProvider(Base):
    __tablename__ = "llm_provider"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), default="ollama")
    endpoint: Mapped[str] = mapped_column(String(500), default="")
    api_key_ref: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="desconhecido")
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_sync: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class LLMModel(Base):
    __tablename__ = "llm_model"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("llm_provider.id", ondelete="CASCADE"))
    identifier: Mapped[str] = mapped_column(String(200), nullable=False)
    context_window: Mapped[int] = mapped_column(Integer, default=0)
    max_output: Mapped[int] = mapped_column(Integer, default=0)
    default_temperature: Mapped[float] = mapped_column(default=0.0)
    cost_in_per_mtok: Mapped[float] = mapped_column(default=0.0)
    cost_out_per_mtok: Mapped[float] = mapped_column(default=0.0)
    multimodal: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="ativo")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)


class PromptTemplate(Base):
    __tablename__ = "prompt_template"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[str] = mapped_column(String(40), default="1")
    task_type: Mapped[str] = mapped_column(String(80), default="")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Schedule(Base):
    __tablename__ = "schedule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    frequency: Mapped[str] = mapped_column(String(40), default="manual")  # manual|diaria|semanal
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QualityCriterion(Base):
    """Critério de avaliação da qualidade dos estudos (AQ1, AQ2...)."""

    __tablename__ = "quality_criterion"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False)  # AQ01...
    description: Mapped[str] = mapped_column(Text, nullable=False)
    weight: Mapped[float] = mapped_column(default=1.0)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QualityScore(Base):
    """Nota 0.0–1.0 de um paper para um critério de qualidade."""

    __tablename__ = "quality_score"
    __table_args__ = (UniqueConstraint("paper_id", "criterion_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    criterion_id: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[float] = mapped_column(default=0.0)
    comment: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Notification(Base):
    """Notificação in-app por projeto (sino na topbar)."""

    __tablename__ = "notification"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(40), default="info")
    title: Mapped[str] = mapped_column(String(300), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str] = mapped_column(String(500), default="")
    read: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Proposal(Base):
    """Proposta de novo estudo/lacuna/trendência identificada pelo agente
    a partir do catálogo — revisada e priorizada pelo pesquisador."""

    __tablename__ = "proposal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(20), default="oportunidade")
    # lacuna | tendencia | limitacao | oportunidade
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    rationale: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[list] = mapped_column(JSON, default=list)  # [{paper_id,title,detail}]
    questions: Mapped[list] = mapped_column(JSON, default=list)  # perguntas sugeridas
    priority: Mapped[str] = mapped_column(String(10), default="media")  # baixa|media|alta
    status: Mapped[str] = mapped_column(String(20), default="proposto")
    # proposto | em_analise | priorizado | convertido | descartado
    origin: Mapped[str] = mapped_column(String(20), default="agente")  # agente|manual
    notes: Mapped[str] = mapped_column(Text, default="")  # refinamentos do pesquisador
    converted_to: Mapped[str] = mapped_column(String(200), default="")
    # "projeto:<slug>" | "questao:<id>"
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    iteration: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow)
