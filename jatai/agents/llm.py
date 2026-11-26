"""Camada de chamada a LLMs.

Ollama é o provedor prioritário (endpoint HTTP /api/chat). A estrutura
permite adicionar LiteLLM para outros provedores sem alterar as tasks.
Toda chamada é registrada em LLMCall (prompt, modelo, tokens, custo).
"""

import json
import os
from pathlib import Path

import httpx

from jatai.audit.log import record_event
from jatai.storage.db import get_project_session
from jatai.storage.models_project import LLMCall, LLMModel, LLMProvider


class LLMError(Exception):
    pass


# Limite de caracteres de prompt/resposta nos eventos llm.in/llm.out
# (o histórico completo permanece em LLMCall.prompt/response_raw).
_EVENT_CLIP = 20000


def _clip(text: str, limit: int = _EVENT_CLIP) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [{len(text) - limit} caracteres omitidos]"


def _publish_llm(slug, event_type, *, task, model_ref, run_id, iteration,
                 paper_id, **kw):
    """Publica llm.in/llm.out no bus; com run_id vira RunEvent persistido."""
    try:
        from jatai.events import bus
        bus.publish(slug, event_type, run_id=run_id, iteration=iteration or 0,
                    task=task, model=model_ref, paper_id=paper_id, **kw)
    except Exception:  # noqa: BLE001 — telemetria nunca trava a chamada
        pass


# Provedores atendidos pelo adaptador compatível com a API OpenAI
# (/chat/completions + Bearer token). Ollama usa /api/chat nativo.
OPENAI_COMPAT_PROVIDERS = {"openai", "openrouter", "google", "openai_compat"}

DEFAULT_ENDPOINTS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai",
    "openai": "https://api.openai.com/v1",
    "openai_compat": "",
}


def api_key_for(provider: LLMProvider) -> str:
    """Resolve api_key_ref: 'env:VAR' -> variável de ambiente,
    'file:/caminho' -> conteúdo do arquivo, demais valores -> a própria
    chave (permite colar a chave direto na interface)."""
    ref = (provider.api_key_ref or "").strip()
    if not ref:
        return ""
    if ref.startswith("env:"):
        return os.environ.get(ref[4:].strip(), "")
    if ref.startswith("file:"):
        try:
            return Path(ref[5:].strip()).expanduser().read_text(
                encoding="utf-8").strip()
        except OSError:
            return ""
    return ref


def _is_openai_compat(provider: LLMProvider) -> bool:
    return provider.provider in OPENAI_COMPAT_PROVIDERS


def _resolve_model(session, model_ref: str) -> tuple[LLMProvider, LLMModel] | None:
    """model_ref pode ser 'provider_id:model_id' ou apenas o identifier do modelo."""
    if ":" in model_ref:
        pid, mid = model_ref.split(":", 1)
        provider = session.get(LLMProvider, int(pid))
        model = session.get(LLMModel, int(mid))
    else:
        model = session.scalar(
            __import__("sqlalchemy").select(LLMModel).where(LLMModel.identifier == model_ref))
        provider = session.get(LLMProvider, model.provider_id) if model else None
    if provider is None or model is None:
        return None
    if getattr(model, "status", "ativo") == "removido":
        return None
    return provider, model


DEFAULT_TIMEOUT = 300.0


def _raise_for_status(resp: httpx.Response) -> None:
    """raise_for_status incluindo o corpo da resposta — provedores como o
    Gemini explicam o motivo do 4xx/5xx no JSON de erro."""
    if resp.status_code < 400:
        return
    body = (resp.text or "").strip().replace("\n", " ")[:400]
    raise httpx.HTTPStatusError(
        f"HTTP {resp.status_code}: {body}", request=resp.request,
        response=resp)


GOOGLE_BASE = "https://generativelanguage.googleapis.com/v1beta"


def _google_base(provider: LLMProvider) -> str:
    """Base da API Gemini. Se o endpoint salvo contém o host do Google
    (mesmo com path errado colado, ex.: URL de generateContent), ignora
    o path e usa a base oficial."""
    ep = (provider.endpoint or "").strip()
    if not ep or "generativelanguage.googleapis.com" in ep:
        return GOOGLE_BASE
    return ep.rstrip("/")


def _openai_endpoint(provider: LLMProvider) -> str:
    if provider.provider == "google":
        return _google_base(provider) + "/openai"
    endpoint = (provider.endpoint or DEFAULT_ENDPOINTS.get(provider.provider, "")).rstrip("/")
    if not endpoint:
        raise LLMError(f"Endpoint não configurado para o provedor '{provider.provider}'")
    return endpoint


def _openai_headers(provider: LLMProvider) -> dict:
    key = api_key_for(provider)
    if not key:
        raise LLMError(f"API key não configurada para o provedor '{provider.provider}'")
    headers = {"Authorization": f"Bearer {key}",
               "Content-Type": "application/json"}
    if provider.provider == "google":
        headers["X-goog-api-key"] = key
    if provider.provider == "openrouter":
        headers["HTTP-Referer"] = "https://localhost"
        headers["X-Title"] = "Jataí"
    return headers


def _openai_chat(provider: LLMProvider, model: LLMModel, messages: list[dict],
                 *, temperature: float, max_tokens: int, timeout: float,
                 response_format: dict | None = None) -> dict:
    """POST {endpoint}/chat/completions (API compatível com OpenAI)."""
    payload = {
        "model": model.identifier,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    resp = httpx.post(f"{_openai_endpoint(provider)}/chat/completions",
                      json=payload, headers=_openai_headers(provider),
                      timeout=timeout)
    _raise_for_status(resp)
    return resp.json()


def chat(slug: str, *, model_ref: str, messages: list[dict], task: str = "",
         temperature: float | None = None, max_tokens: int | None = None,
         timeout: float | None = None, run_id: int | None = None,
         iteration: int | None = None, paper_id: int | None = None) -> str:
    """Envia mensagens ao modelo e retorna o texto da resposta."""
    with get_project_session(slug) as s:
        resolved = _resolve_model(s, model_ref)
        if resolved is None:
            raise LLMError(f"Modelo não encontrado: {model_ref}")
        provider, model = resolved
        endpoint = (provider.endpoint or "http://localhost:11434").rstrip("/")
        temp = model.default_temperature if temperature is None else temperature
        num_predict = max_tokens or model.max_output or 2048
        provider_id, model_id = provider.id, model.id
        model_name = model.identifier

    _publish_llm(slug, "llm.in", task=task, model_ref=model_ref,
                 model=model_name, prompt=_clip(json.dumps(messages, ensure_ascii=False)),
                 run_id=run_id, iteration=iteration, paper_id=paper_id)

    if _is_openai_compat(provider):
        params = json.dumps({"temperature": temp, "max_tokens": num_predict,
                             "api": "openai_compat"}, ensure_ascii=False)
        try:
            data = _openai_chat(provider, model, messages,
                                temperature=temp, max_tokens=num_predict,
                                timeout=timeout or DEFAULT_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            _log_call(slug, task, model_ref, params, temperature, max_tokens,
                      "", "", 0, 0, 0.0, provider_id, model_id,
                      error=str(exc), run_id=run_id, iteration=iteration,
                      paper_id=paper_id)
            raise LLMError(f"Falha na chamada ao provedor '{provider.provider}': {exc}") from exc
        choice = (data.get("choices") or [{}])[0] or {}
        msg = choice.get("message") or {}
        content = _content_text(msg.get("content"))
        if not content:
            # modelos de raciocínio podem devolver o texto em 'reasoning'
            content = _content_text(msg.get("reasoning"))
        usage = data.get("usage") or {}
        tin, tout = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        cost = (tin * model.cost_in_per_mtok + tout * model.cost_out_per_mtok) / 1_000_000
        _log_call(slug, task, model_ref, params, temperature, max_tokens,
                  json.dumps(messages, ensure_ascii=False), content,
                  tin, tout, cost, provider_id, model_id,
                  run_id=run_id, iteration=iteration, paper_id=paper_id)
        return content

    if provider.provider != "ollama":
        raise LLMError(f"Provedor '{provider.provider}' não suportado")

    payload = {
        "model": model.identifier,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temp, "num_predict": num_predict},
    }
    try:
        resp = httpx.post(f"{endpoint}/api/chat", json=payload,
                          timeout=timeout or DEFAULT_TIMEOUT)
        _raise_for_status(resp)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        _log_call(slug, task, model_ref, "", temperature, max_tokens, "", "", 0, 0, 0.0,
                  provider_id, model_id, error=str(exc),
                  run_id=run_id, iteration=iteration, paper_id=paper_id)
        raise LLMError(f"Falha na chamada ao Ollama: {exc}") from exc

    content = _content_text((data.get("message") or {}).get("content"))
    usage = data.get("prompt_eval_count", 0), data.get("eval_count", 0)
    cost = (usage[0] * model.cost_in_per_mtok + usage[1] * model.cost_out_per_mtok) / 1_000_000
    _log_call(slug, task, model_ref, json.dumps(payload, ensure_ascii=False),
              temperature, max_tokens, json.dumps(messages, ensure_ascii=False),
              content, usage[0], usage[1], cost, provider_id, model_id,
              run_id=run_id, iteration=iteration, paper_id=paper_id)
    return content


def model_label(slug: str, model_ref: str) -> str:
    """Identificador legível do modelo (ex.: 'google/gemini-3.5-flash-lite').
    Fallback: o próprio ref quando não resolve."""
    if not model_ref:
        return ""
    try:
        with get_project_session(slug) as s:
            resolved = _resolve_model(s, model_ref)
            return resolved[1].identifier if resolved else model_ref
    except Exception:  # noqa: BLE001
        return model_ref


def _content_text(content) -> str:
    """Normaliza message.content para str. Provedores OpenAI-compat podem
    devolver null (modelos de raciocínio), string ou lista de partes."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                parts.append(str(p.get("text") or p.get("content") or ""))
        return "".join(parts)
    return str(content)


def _log_call(slug, task, model_ref, params, temperature, max_tokens,
              prompt, response, tin, tout, cost, provider_id, model_id, error="",
              prompt_template="", prompt_version="",
              run_id=None, iteration=None, paper_id=None):
    model_name = ""
    with get_project_session(slug) as s:
        if model_id:
            m = s.get(LLMModel, model_id)
            model_name = m.identifier if m else ""
        s.add(LLMCall(
            task=task,
            model=model_ref,
            prompt_template=prompt_template,
            prompt_version=prompt_version,
            params=json.dumps({"temperature": temperature, "max_tokens": max_tokens,
                               "params": params, "error": error}, ensure_ascii=False)[:40000],
            prompt=(prompt or "")[:60000],
            response_raw=(response or "")[:30000],
            tokens_in=tin,
            tokens_out=tout,
            cost_usd=cost,
        ))
        s.commit()
    record_event("llm.call", entity=f"task:{task}", operation="call", project_slug=slug,
                 payload={"model": model_ref, "model_name": model_name or None,
                          "tokens_in": tin, "tokens_out": tout,
                          "cost_usd": round(cost, 6), "error": error or None})
    _publish_llm(slug, "llm.out", task=task, model_ref=model_ref,
                 response=_clip(response or ""), error=error or None,
                 tokens_in=tin, tokens_out=tout, cost_usd=round(cost, 6),
                 run_id=run_id, iteration=iteration, paper_id=paper_id)


def chat_structured(slug: str, *, model_ref: str, messages: list[dict],
                    response_model, task: str = "",
                    temperature: float | None = None, max_tokens: int | None = None,
                    prompt_template: str = "", prompt_version: str = "",
                    timeout: float | None = None, run_id: int | None = None,
                    iteration: int | None = None, paper_id: int | None = None):
    """Chamada com saída estruturada: envia o JSON Schema do modelo Pydantic
    em `format` (structured outputs nativos do Ollama) e valida a resposta."""
    with get_project_session(slug) as s:
        resolved = _resolve_model(s, model_ref)
        if resolved is None:
            raise LLMError(f"Modelo não encontrado: {model_ref}")
        provider, model = resolved
        endpoint = (provider.endpoint or "http://localhost:11434").rstrip("/")
        temp = model.default_temperature if temperature is None else temperature
        num_predict = max_tokens or model.max_output or 2048
        provider_id, model_id = provider.id, model.id
        model_name = model.identifier

    _publish_llm(slug, "llm.in", task=task, model_ref=model_ref,
                 model=model_name,
                 prompt=_clip(json.dumps(messages, ensure_ascii=False)),
                 schema=getattr(response_model, "__name__", ""),
                 run_id=run_id, iteration=iteration, paper_id=paper_id)

    if _is_openai_compat(provider):
        schema = response_model.model_json_schema()
        rf = {"type": "json_schema",
              "json_schema": {"name": response_model.__name__,
                              "schema": schema, "strict": True}}
        msgs = list(messages)
        last_error = ""
        for attempt in range(2):
            # retry dobra o teto de tokens — JSON truncado é a causa mais
            # comum de 'resposta fora do schema: Expecting ...'
            predict = min(num_predict * (2 ** attempt), 16384)
            params = json.dumps({"temperature": temp, "max_tokens": predict,
                                 "api": "openai_compat",
                                 "response_format": "json_schema",
                                 "attempt": attempt + 1},
                                ensure_ascii=False)
            try:
                try:
                    data = _openai_chat(provider, model, msgs,
                                        temperature=temp, max_tokens=predict,
                                        timeout=timeout or DEFAULT_TIMEOUT,
                                        response_format=rf)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 400:
                        raise
                    # provedor não aceita json_schema -> tenta só json_object
                    data = _openai_chat(
                        provider, model, msgs, temperature=temp,
                        max_tokens=predict,
                        timeout=timeout or DEFAULT_TIMEOUT,
                        response_format={"type": "json_object"})
            except Exception as exc:  # noqa: BLE001
                _log_call(slug, task, model_ref, params, temperature, predict,
                          "", "", 0, 0, 0.0, provider_id, model_id, error=str(exc),
                          prompt_template=prompt_template,
                          prompt_version=prompt_version,
                          run_id=run_id, iteration=iteration, paper_id=paper_id)
                raise LLMError(
                    f"Falha na chamada ao provedor '{provider.provider}': {exc}") from exc
            choice = (data.get("choices") or [{}])[0] or {}
            msg = choice.get("message") or {}
            raw = _content_text(msg.get("content"))
            if not raw:
                raw = _content_text(msg.get("reasoning"))
            finish = choice.get("finish_reason")
            usage = data.get("usage") or {}
            tin = usage.get("prompt_tokens", 0)
            tout = usage.get("completion_tokens", 0)
            cost = (tin * model.cost_in_per_mtok + tout * model.cost_out_per_mtok) / 1_000_000
            error = ""
            parsed = None
            if finish == "length":
                error = "resposta truncada pelo limite de tokens"
            else:
                try:
                    parsed = response_model.model_validate(_extract_json(raw))
                except Exception as exc:  # noqa: BLE001
                    error = f"resposta fora do schema: {exc}"
            _log_call(slug, task, model_ref, params, temperature, predict,
                      json.dumps(msgs, ensure_ascii=False), raw,
                      tin, tout, cost, provider_id, model_id, error=error,
                      prompt_template=prompt_template, prompt_version=prompt_version,
                      run_id=run_id, iteration=iteration, paper_id=paper_id)
            if parsed is not None:
                return parsed
            last_error = error
            if attempt == 0:
                msgs = msgs + [
                    {"role": "assistant", "content": raw[:4000]},
                    {"role": "user", "content":
                     "Sua resposta anterior estava truncada ou fora do schema "
                     "JSON esperado. Retorne APENAS o JSON válido e completo, "
                     "de forma concisa, sem texto adicional."},
                ]
        raise LLMError(f"Resposta do modelo inválida para o schema: {last_error}")

    if provider.provider != "ollama":
        raise LLMError(f"Provedor '{provider.provider}' não suportado")

    msgs = list(messages)
    last_error = ""
    for attempt in range(2):
        predict = min(num_predict * (2 ** attempt), 16384)
        payload = {
            "model": model.identifier,
            "messages": msgs,
            "stream": False,
            "format": response_model.model_json_schema(),
            "options": {"temperature": temp, "num_predict": predict},
        }
        try:
            resp = httpx.post(f"{endpoint}/api/chat", json=payload,
                              timeout=timeout or DEFAULT_TIMEOUT)
            _raise_for_status(resp)
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            _log_call(slug, task, model_ref, "", temperature, predict, "", "",
                      0, 0, 0.0, provider_id, model_id, error=str(exc),
                      prompt_template=prompt_template,
                      prompt_version=prompt_version,
                      run_id=run_id, iteration=iteration, paper_id=paper_id)
            raise LLMError(f"Falha na chamada ao Ollama: {exc}") from exc

        raw = _content_text((data.get("message") or {}).get("content"))
        tin, tout = data.get("prompt_eval_count", 0), data.get("eval_count", 0)
        cost = (tin * model.cost_in_per_mtok + tout * model.cost_out_per_mtok) / 1_000_000
        error = ""
        parsed = None
        if data.get("done_reason") == "length":
            error = "resposta truncada pelo limite de tokens"
        else:
            try:
                parsed = response_model.model_validate(_extract_json(raw))
            except Exception as exc:  # noqa: BLE001
                error = f"resposta fora do schema: {exc}"
        _log_call(slug, task, model_ref, json.dumps(payload, ensure_ascii=False),
                  temperature, predict, json.dumps(msgs, ensure_ascii=False),
                  raw, tin, tout, cost, provider_id, model_id, error=error,
                  prompt_template=prompt_template, prompt_version=prompt_version,
                  run_id=run_id, iteration=iteration, paper_id=paper_id)
        if parsed is not None:
            return parsed
        last_error = error
        if attempt == 0:
            msgs = msgs + [
                {"role": "assistant", "content": raw[:4000]},
                {"role": "user", "content":
                 "Sua resposta anterior estava truncada ou fora do schema "
                 "JSON esperado. Retorne APENAS o JSON válido e completo, "
                 "de forma concisa, sem texto adicional."},
            ]
    raise LLMError(f"Resposta do modelo inválida para o schema: {last_error}")


def _extract_json(text: str):
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    start = min([i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise LLMError("Resposta do modelo não contém JSON")
    end = max(cleaned.rfind("}"), cleaned.rfind("]"))
    return json.loads(cleaned[start:end + 1])


def chat_json(slug: str, *, model_ref: str, messages: list[dict], task: str = "",
              temperature: float | None = 0.2, max_tokens: int | None = None,
              retries: int = 1, timeout: float | None = None,
              run_id: int | None = None, iteration: int | None = None,
              paper_id: int | None = None) -> dict | list:
    """Chama o modelo e extrai JSON; em caso de JSON inválido, pede correção."""
    msgs = list(messages)
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        text = chat(slug, model_ref=model_ref, messages=msgs, task=task,
                    temperature=temperature, max_tokens=max_tokens,
                    timeout=timeout, run_id=run_id, iteration=iteration,
                    paper_id=paper_id)
        try:
            return _extract_json(text)
        except (json.JSONDecodeError, LLMError) as exc:
            last_exc = exc
            msgs = msgs + [
                {"role": "assistant", "content": text[:4000]},
                {"role": "user", "content":
                 "Sua resposta anterior não era um JSON válido. "
                 "Retorne APENAS o objeto/array JSON correto, sem texto adicional."},
            ]
    raise LLMError(f"Modelo não retornou JSON válido após {retries + 1} tentativas: {last_exc}")


def ollama_tags(endpoint: str) -> list[dict]:
    """Lista modelos disponíveis em um servidor Ollama."""
    resp = httpx.get(f"{endpoint.rstrip('/')}/api/tags", timeout=10)
    resp.raise_for_status()
    return resp.json().get("models", [])


def openai_models(provider: LLMProvider) -> list[dict]:
    """Lista modelos do provedor.

    OpenRouter/OpenAI: GET {endpoint}/models -> lista 'data' com 'id'
    (e 'pricing' no OpenRouter). Google usa a API nativa
    GET {base}/models (X-goog-api-key) e os nomes vêm como
    'models/gemini-...' — normalizado para {'id': 'gemini-...'} com
    contexto estimado quando disponível."""
    if provider.provider == "google":
        key = api_key_for(provider)
        if not key:
            raise LLMError("API key não configurada para o provedor 'google'")
        resp = httpx.get(f"{_google_base(provider)}/models",
                         params={"pageSize": 1000},
                         headers={"X-goog-api-key": key}, timeout=15)
        resp.raise_for_status()
        out = []
        for m in resp.json().get("models", []):
            name = str(m.get("name", "")).removeprefix("models/")
            if not name or "generateContent" not in (
                    m.get("supportedGenerationMethods") or ["generateContent"]):
                continue
            out.append({"id": name})
        return out
    resp = httpx.get(f"{_openai_endpoint(provider)}/models",
                     headers=_openai_headers(provider), timeout=15)
    resp.raise_for_status()
    return resp.json().get("data", [])


def provider_status(provider: LLMProvider) -> tuple[str, str]:
    """Testa o provedor. Retorna (status, detalhe_do_erro).

    status: 'disponivel' | 'indisponivel'; detalhe vazio quando OK,
    senão mensagem com status HTTP e trecho do corpo (ex.: 401 da API)."""
    try:
        if _is_openai_compat(provider):
            openai_models(provider)
            return "disponivel", ""
        endpoint = (provider.endpoint or "http://localhost:11434").rstrip("/")
        resp = httpx.get(f"{endpoint}/api/tags", timeout=5)
        if resp.status_code == 200:
            return "disponivel", ""
        return "indisponivel", f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)
        resp = getattr(exc, "response", None)
        if resp is not None:
            detail += f" — HTTP {resp.status_code}: {(resp.text or '')[:200]}"
        return "indisponivel", detail[:500]


def list_llm_calls(slug: str, limit: int = 100):
    from sqlalchemy import select

    with get_project_session(slug) as s:
        return list(s.scalars(select(LLMCall).order_by(LLMCall.id.desc()).limit(limit)))


def llm_usage(slug: str) -> dict:
    from sqlalchemy import func, select

    with get_project_session(slug) as s:
        row = s.execute(select(
            func.count(LLMCall.id),
            func.coalesce(func.sum(LLMCall.tokens_in), 0),
            func.coalesce(func.sum(LLMCall.tokens_out), 0),
            func.coalesce(func.sum(LLMCall.cost_usd), 0.0),
        )).one()
        return {"calls": row[0], "tokens_in": row[1], "tokens_out": row[2], "cost_usd": row[3]}


def default_model_ref(session) -> str:
    """Modelo padrão do projeto: o único LLMModel.is_default ativo.
    Fallbacks determinísticos: primeiro modelo ativo, depois qualquer um."""
    sel = __import__("sqlalchemy").select
    model = session.scalar(sel(LLMModel).where(
        LLMModel.is_default == True,  # noqa: E712
        LLMModel.status != "removido").order_by(LLMModel.id))
    if model is None:
        model = session.scalar(sel(LLMModel)
                               .where(LLMModel.status != "removido")
                               .order_by(LLMModel.id).limit(1))
    if model is None:
        model = session.scalar(sel(LLMModel).order_by(LLMModel.id).limit(1))
    return f"{model.provider_id}:{model.id}" if model else ""


def task_model_ref(session, task) -> str:
    if task.llm_model:
        return task.llm_model
    return default_model_ref(session)