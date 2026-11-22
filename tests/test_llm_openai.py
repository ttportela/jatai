"""Provedores compatíveis com OpenAI (openrouter, google, openai)."""

import json

import httpx
import pytest

from lelia.agents import llm
from lelia.storage.db import get_project_session
from lelia.storage.models_project import LLMCall, LLMModel, LLMProvider


def _provider(slug, ptype="openrouter", key="sk-test"):
    with get_project_session(slug) as s:
        p = LLMProvider(name=ptype, provider=ptype,
                        endpoint="https://openrouter.ai/api/v1",
                        api_key_ref=key)
        s.add(p)
        s.flush()
        m = LLMModel(provider_id=p.id, identifier="test/model-1",
                     is_default=True)
        s.add(m)
        s.commit()
        return p.id, m.id


def _resp(payload, status=200):
    r = httpx.Response(status, json=payload,
                       request=httpx.Request("POST", "http://x"))
    return r


def test_api_key_for_literal_env_file(slug, tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "env-secret")
    f = tmp_path / "k.txt"
    f.write_text("file-secret\n")
    assert llm.api_key_for(LLMProvider(api_key_ref="plain-key")) == "plain-key"
    assert llm.api_key_for(LLMProvider(api_key_ref="env:TEST_LLM_KEY")) == "env-secret"
    assert llm.api_key_for(LLMProvider(api_key_ref=f"file:{f}")) == "file-secret"
    assert llm.api_key_for(LLMProvider(api_key_ref="env:MISSING_VAR")) == ""


def test_chat_openai_compat(slug, monkeypatch):
    pid, mid = _provider(slug)
    calls = []

    def fake_post(url, **kw):
        calls.append((url, kw))
        return _resp({
            "choices": [{"message": {"content": "resposta ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    out = llm.chat(slug, model_ref=f"{pid}:{mid}",
                   messages=[{"role": "user", "content": "oi"}], task="t")
    assert out == "resposta ok"
    url, kw = calls[0]
    assert url == "https://openrouter.ai/api/v1/chat/completions"
    assert kw["headers"]["Authorization"] == "Bearer sk-test"
    assert kw["json"]["model"] == "test/model-1"
    with get_project_session(slug) as s:
        c = s.scalar(__import__("sqlalchemy").select(LLMCall))
        assert c.tokens_in == 10 and c.tokens_out == 5


def test_chat_structured_openai_fallback_sem_schema(slug, monkeypatch):
    """Provedor que rejeita json_schema (400) -> retry com json_object."""
    from pydantic import BaseModel

    class Out(BaseModel):
        value: str

    pid, mid = _provider(slug, ptype="google",
                         key="AIza-test")
    # endpoint default do google
    with get_project_session(slug) as s:
        s.get(LLMProvider, pid).endpoint = \
            "https://generativelanguage.googleapis.com/v1beta/openai"
        s.commit()
    attempts = []

    def fake_post(url, **kw):
        attempts.append(kw["json"].get("response_format"))
        if len(attempts) == 1:
            raise httpx.HTTPStatusError(
                "bad request", request=kw and httpx.Request("POST", url),
                response=_resp({"error": "response_format not supported"}, 400))
        return _resp({
            "choices": [{"message": {"content": '{"value": "ok"}'}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        })

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    res = llm.chat_structured(slug, model_ref=f"{pid}:{mid}",
                              messages=[{"role": "user", "content": "oi"}],
                              response_model=Out, task="t")
    assert res.value == "ok"
    assert attempts[0]["type"] == "json_schema"
    assert attempts[1]["type"] == "json_object"


def test_sync_openai_models_importa_e_precifica(slug, monkeypatch):
    from lelia.agents import config_service

    pid, _ = _provider(slug)

    def fake_get(url, **kw):
        assert url.endswith("/models")
        assert kw["headers"]["Authorization"] == "Bearer sk-test"
        return _resp({"data": [
            {"id": "test/model-1",
             "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
            {"id": "openrouter/new-model",
             "pricing": {"prompt": "0.000003", "completion": "0.000004"}},
        ]})

    monkeypatch.setattr(llm.httpx, "get", fake_get)
    msg = config_service.sync_provider(slug, pid)
    assert "1 novo(s)" in msg
    models = {m.identifier: m for m in config_service.list_models(slug, pid)}
    assert "openrouter/new-model" in models
    m = models["openrouter/new-model"]
    assert m.cost_in_per_mtok == pytest.approx(3.0)
    assert m.cost_out_per_mtok == pytest.approx(4.0)


def test_provider_status_openai_compat(slug, monkeypatch):
    pid, _ = _provider(slug)
    from lelia.agents import config_service
    p = config_service.get_provider(slug, pid)

    monkeypatch.setattr(llm.httpx, "get",
                        lambda *a, **k: _resp({"data": []}))
    assert llm.provider_status(p) == ("disponivel", "")

    def boom(*a, **k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(llm.httpx, "get", boom)
    status, detail = llm.provider_status(p)
    assert status == "indisponivel" and "down" in detail


def test_chat_structured_content_null_nao_quebra(slug, monkeypatch):
    """Modelos de raciocínio (ex.: OpenRouter nemotron) podem devolver
    message.content=null com o texto em 'reasoning'. Não pode gerar
    'NoneType object is not subscriptable' nem perder a auditoria."""
    from pydantic import BaseModel

    class Out(BaseModel):
        value: str

    pid, mid = _provider(slug)

    def fake_post(url, **kw):
        return _resp({
            "choices": [{"message": {"content": None,
                                     "reasoning": '{"value": "via-reasoning"}'},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    res = llm.chat_structured(slug, model_ref=f"{pid}:{mid}",
                              messages=[{"role": "user", "content": "oi"}],
                              response_model=Out, task="t")
    assert res.value == "via-reasoning"
    with get_project_session(slug) as s:
        c = s.scalar(__import__("sqlalchemy").select(LLMCall))
        assert c is not None and c.response_raw


def test_chat_structured_resposta_vazia_gera_erro_auditado(slug, monkeypatch):
    """content=null sem reasoning -> erro de schema tratado, com LLMCall
    registrado (não exceção crua do tipo NoneType)."""
    from pydantic import BaseModel

    class Out(BaseModel):
        value: str

    pid, mid = _provider(slug)

    def fake_post(url, **kw):
        return _resp({
            "choices": [{"message": {"content": None},
                         "finish_reason": "stop"}],
            "usage": {},
        })

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    with pytest.raises(llm.LLMError, match="schema"):
        llm.chat_structured(slug, model_ref=f"{pid}:{mid}",
                            messages=[{"role": "user", "content": "oi"}],
                            response_model=Out, task="t")
    with get_project_session(slug) as s:
        calls = list(s.scalars(__import__("sqlalchemy").select(LLMCall)))
        assert len(calls) == 2  # duas tentativas auditadas
        assert all("error" in (c.params or "") for c in calls)


def test_chat_content_null_usa_reasoning(slug, monkeypatch):
    pid, mid = _provider(slug)

    def fake_post(url, **kw):
        return _resp({
            "choices": [{"message": {"content": None,
                                     "reasoning": "raciocinio… resposta"}}],
            "usage": {},
        })

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    out = llm.chat(slug, model_ref=f"{pid}:{mid}",
                   messages=[{"role": "user", "content": "oi"}], task="t")
    assert out == "raciocinio… resposta"


def test_google_endpoint_errado_e_normalizado(slug, monkeypatch):
    """Endpoint salvo com URL completa do generateContent é corrigido
    para a base oficial; /models usa a API nativa do Gemini."""
    pid, mid = _provider(slug, ptype="google", key="g-key")
    with get_project_session(slug) as s:
        s.get(LLMProvider, pid).endpoint = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-flash-latest:generateContent")
        s.commit()

    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        return _resp({"models": [
            {"name": "models/gemini-2.5-flash",
             "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/text-embedding-004",
             "supportedGenerationMethods": ["embedContent"]},
        ]})

    monkeypatch.setattr(llm.httpx, "get", fake_get)
    p = config_service_get(slug, pid)
    seen.clear()
    models = llm.openai_models(p)
    assert seen == ["https://generativelanguage.googleapis.com/v1beta/models"]
    assert [m["id"] for m in models] == ["gemini-2.5-flash"]
    # chat usa o endpoint openai-compat correto
    def fake_post(url, **kw):
        seen.append(url)
        return _resp({
            "choices": [{"message": {"content": "ok"}}],
            "usage": {}})
    monkeypatch.setattr(llm.httpx, "post", fake_post)
    llm.chat(slug, model_ref=f"{pid}:{mid}",
             messages=[{"role": "user", "content": "oi"}])
    assert seen[-1] == ("https://generativelanguage.googleapis.com"
                        "/v1beta/openai/chat/completions")


def config_service_get(slug, pid):
    from lelia.agents import config_service
    return config_service.get_provider(slug, pid)


def test_test_provider_grava_last_error(slug, monkeypatch):
    from lelia.agents import config_service

    pid, _ = _provider(slug, ptype="google",
                       key="bad-key")
    with get_project_session(slug) as s:
        s.get(LLMProvider, pid).endpoint = \
            "https://generativelanguage.googleapis.com/v1beta/openai"
        s.commit()

    def fake_get(url, **kw):
        return _resp({"error": {"message": "API key not valid"}}, 401)

    monkeypatch.setattr(llm.httpx, "get", fake_get)
    assert config_service.test_provider(slug, pid) == "indisponivel"
    p = config_service.get_provider(slug, pid)
    assert p.status == "indisponivel"
    assert "401" in p.last_error and "API key not valid" in p.last_error
