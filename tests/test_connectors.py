"""Testes unitários dos conectores e da compilação de expressões DBLP."""

from urllib.parse import parse_qs, urlparse

import pytest

from lelia.sources import dblp, openalex
from lelia.search.adapters.dblp import compile_expression as dblp_compile


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


# ---------- DBLP: compilação de cláusulas (DNF) ----------

@pytest.mark.parametrize("expr,expected", [
    # AND de grupos OR -> uma cláusula por combinação
    ('("big data" OR "data analytics") AND (trajectory OR movelets)',
     ["big data trajectory", "big data movelets",
      "data analytics trajectory", "data analytics movelets"]),
    # termos simples -> cláusula única
    ("trajectory classification", ["trajectory classification"]),
    # OR puro -> uma cláusula por alternativa
    ("trajectory OR movelets", ["trajectory", "movelets"]),
    # prefixos de campo
    ("author:Smith venue:KDD year:2021", ["author:Smith venue:KDD year:2021"]),
    ("title:foo year:2020", ["foo year:2020"]),  # title: inválido -> removido
    ("p1:BigData AND p2:DataLake", ["BigData DataLake"]),
    # exact match preservado
    ("graph$ network", ["graph$ network"]),
    # frase entre aspas -> AND de palavras (DBLP não tem frase)
    ('"data mining" classification', ["data mining classification"]),
    # NOT/pontuação residual não quebra
    ("foo - bar", ["foo bar"]),
])
def test_dblp_clauses(expr, expected):
    assert dblp_compile(expr) == expected


def test_dblp_clauses_dedup():
    clauses = dblp_compile("(a OR a) AND (b OR b)")
    assert clauses == ["a b"]


# ---------- DBLP: paginação via offset f ----------

class _FakeBrowser:
    """Retorna páginas programadas; registra as URLs requisitadas."""

    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    def fetch_json(self, url):
        self.urls.append(url)
        idx = min(len(self.urls) - 1, len(self.pages) - 1)
        page = self.pages[idx]
        return {"result": {"hits": {"@total": page["total"], "hit": page["hits"]}}}

    def close(self):
        pass


def _hit(i):
    return {"info": {"title": f"T{i}", "authors": {"author": [{"text": "A"}]},
                     "year": "2020", "venue": "V", "type": "article",
                     "key": f"k{i}", "doi": ""}}


def _hits(start, n):
    return [_hit(i) for i in range(start, start + n)]


def test_dblp_pagination_uses_f_offset():
    """Com limite > 1000, h=1000 e offset avança via f (não p)."""
    conn = dblp.DBLPConnector(max_results=10000)
    browser = _FakeBrowser([
        {"total": 1600, "hits": _hits(0, 1000)},
        {"total": 1600, "hits": _hits(1000, 600)},
    ])
    conn._browser = browser
    recs = conn.search("trajectory", max_results=1500)
    assert len(recs) == 1500
    assert len(browser.urls) == 2
    q1 = parse_qs(urlparse(browser.urls[0]).query)
    q2 = parse_qs(urlparse(browser.urls[1]).query)
    assert q1["f"] == ["0"] and q1["h"] == ["1000"]
    assert q2["f"] == ["1000"] and q2["h"] == ["1000"]


def test_dblp_paginates_when_api_returns_less_than_h():
    """Regressão: DBLP devolve no máx. ~100 hits/página mesmo com h maior.
    O offset deve avançar pelo número real de itens, não por h."""
    conn = dblp.DBLPConnector(max_results=10000)
    browser = _FakeBrowser([
        {"total": 150, "hits": _hits(0, 100)},    # página 1: 100 itens
        {"total": 150, "hits": _hits(100, 50)},  # página 2: restantes 50
    ])
    conn._browser = browser
    recs = conn.search("trajectory big data", max_results=10000)
    assert len(recs) == 150
    assert len(browser.urls) == 2
    offsets = [parse_qs(urlparse(u).query)["f"][0] for u in browser.urls]
    assert offsets == ["0", "100"]


def test_dblp_single_page_when_under_limit():
    conn = dblp.DBLPConnector()
    browser = _FakeBrowser([
        {"total": 3, "hits": _hits(0, 3)},
    ])
    conn._browser = browser
    recs = conn.search("x", max_results=50)
    assert len(recs) == 3 and len(browser.urls) == 1


def test_dblp_dedup_across_clauses():
    """Mesmo registro retornado por cláusulas diferentes conta uma vez."""
    conn = dblp.DBLPConnector()
    browser = _FakeBrowser([
        {"total": 2, "hits": [_hit(1), _hit(2)]},   # cláusula "a c"
        {"total": 2, "hits": [_hit(2), _hit(3)]},   # cláusula "b c" (hit2 repetido)
    ])
    conn._browser = browser
    recs = conn.search("(a OR b) AND c", max_results=50)
    assert len(recs) == 3
    assert [r.external_id for r in recs] == ["k1", "k2", "k3"]
    assert len(browser.urls) == 2
    # cada URL usa uma cláusula AND diferente
    qs = [parse_qs(urlparse(u).query)["q"][0] for u in browser.urls]
    assert qs == ["a c", "b c"]


def test_dblp_progress_and_last_request():
    conn = dblp.DBLPConnector()
    browser = _FakeBrowser([{"total": 2, "hits": _hits(0, 2)}])
    conn._browser = browser
    progress = []
    recs = conn.search("x", max_results=10,
                       on_progress=lambda p, f, t: progress.append((p, f, t)))
    assert len(recs) == 2
    assert progress == [(1, 2, 2)]
    assert conn.last_request["pages"] == 1
    assert "fake" not in conn.last_request["url"]


def test_dblp_empty_result():
    conn = dblp.DBLPConnector()
    conn._browser = _FakeBrowser([{"total": 0, "hits": []}])
    assert conn.search("zzz", max_results=10) == []


# ---------- OpenAlex: paginação por cursor ----------

def test_openalex_cursor_pagination():
    calls = []

    def fake_req(url, params=None, **kw):
        calls.append(dict(params))
        cursor = (params or {}).get("cursor")
        if cursor == "*":
            return {"meta": {"count": 3, "next_cursor": "c2"},
                    "results": [{"display_name": "W1", "id": "a1"},
                                {"display_name": "W2", "id": "a2"}]}
        return {"meta": {"count": 3, "next_cursor": None},
                "results": [{"display_name": "W3", "id": "a3"}]}

    # cursor paging exige api_key
    conn = openalex.OpenAlexConnector(mailto="t@x.com", api_key="k")
    conn._request_json = fake_req
    progress = []
    recs = conn.search("machine learning", max_results=10,
                       on_progress=lambda p, f, t: progress.append((p, f, t)))
    assert [r.title for r in recs] == ["W1", "W2", "W3"]
    assert calls[0]["cursor"] == "*" and calls[1]["cursor"] == "c2"
    assert len(progress) == 2


def test_openalex_stops_without_next_cursor():
    conn = openalex.OpenAlexConnector(api_key="k")
    conn._request_json = lambda *a, **k: {
        "meta": {"count": 1, "next_cursor": None},
        "results": [{"display_name": "W"}]}
    assert len(conn.search("x", max_results=50)) == 1


def test_openalex_boolean_expression_passthrough():
    seen = []

    def fake_req(url, params=None, **kw):
        seen.append(params.get("search"))
        return {"meta": {}, "results": []}

    conn = openalex.OpenAlexConnector()
    conn._request_json = fake_req
    conn.search('((trajectory OR movelets) AND (classification OR clustering))', max_results=5)
    assert seen[0] == '((trajectory OR movelets) AND (classification OR clustering))'


# ---------- Semantic Scholar ----------

from lelia.sources import semanticscholar
from lelia.sources.base import get_connector, implemented_source_types


def _s2_paper(**kw):
    base = {
        "paperId": "abc123",
        "title": "Paper S2",
        "abstract": "Abs",
        "year": 2022,
        "publicationDate": "2022-06-15",
        "venue": "NeurIPS",
        "journal": {"name": "J. ML"},
        "publicationTypes": ["JournalArticle"],
        "externalIds": {"DOI": "10.1/s2", "ArXiv": "2201.00001"},
        "authors": [{"name": "Ana"}, {"name": "Bruno"}],
        "openAccessPdf": {"url": "https://oa.example/p.pdf"},
        "url": "https://www.semanticscholar.org/paper/abc123",
    }
    base.update(kw)
    return base


def test_s2_paper_to_raw_mapping():
    r = semanticscholar._paper_to_raw(_s2_paper())
    assert r.title == "Paper S2"
    assert r.authors == ["Ana", "Bruno"]
    assert r.year == 2022 and r.month == 6
    assert r.doi == "10.1/s2"
    assert r.venue == "J. ML"          # journal.name tem precedência
    assert r.pub_type == "JournalArticle"
    assert r.external_id == "abc123"
    assert r.url == "https://oa.example/p.pdf"  # openAccessPdf preferido


def test_s2_paper_to_raw_fallbacks():
    r = semanticscholar._paper_to_raw(_s2_paper(
        journal=None, venue="Conf X", openAccessPdf=None,
        publicationDate="2020", externalIds={}))
    assert r.venue == "Conf X"
    assert r.url == "https://www.semanticscholar.org/paper/abc123"
    assert r.month is None and r.doi == ""


def _s2_conn(**kw):
    kw.setdefault("api_key", "k")
    kw.setdefault("min_interval", 0)
    return semanticscholar.SemanticScholarConnector(**kw)


def test_s2_search_token_pagination():
    calls = []

    def fake_get(url, params=None, **kw):
        calls.append(dict(params))
        tok = params.get("token")
        if tok is None:
            return _FakeResp({"total": 5, "token": "t2",
                              "data": [_s2_paper(paperId="p1"),
                                       _s2_paper(paperId="p2")]})
        if tok == "t2":
            return _FakeResp({"total": 5, "token": "t3",
                              "data": [_s2_paper(paperId="p3"),
                                       _s2_paper(paperId="p4")]})
        return _FakeResp({"total": 5,
                          "data": [_s2_paper(paperId="p5")]})

    conn = _s2_conn()
    conn._client.get = fake_get
    progress = []
    recs = conn.search("mobility", max_results=10,
                       on_progress=lambda p, f, t: progress.append((p, f, t)))
    assert [r.external_id for r in recs] == ["p1", "p2", "p3", "p4", "p5"]
    assert [c.get("token") for c in calls] == [None, "t2", "t3"]
    assert progress[-1][2] == 5


def test_s2_api_key_header():
    conn = _s2_conn(api_key="sk-test")
    assert conn._client.headers.get("x-api-key") == "sk-test"
    assert conn._headers()["x-api-key"] == "sk-test"


def test_s2_registry_and_factory():
    assert "semantic_scholar" in implemented_source_types()
    conn = get_connector("semantic_scholar", {"api_key": "k1"})
    assert isinstance(conn, semanticscholar.SemanticScholarConnector)
    assert conn.api_key == "k1"
    # S2 é a única fonte que exige API key na prática
    assert conn.requires_api_key is True
    assert get_connector("dblp").requires_api_key is False
    assert get_connector("openalex").requires_api_key is False


def test_s2_search_compila_expressao_booleana_bulk():
    """(a OR b) AND c vira '(a | b) + c' na sintaxe bulk; duplicados
    entre páginas são deduplicados."""
    queries = []

    def fake_get(url, params=None, **kw):
        queries.append(params["query"])
        if params.get("token") is None:
            return _FakeResp({"total": 3, "token": "t2", "data": [
                _s2_paper(paperId="p1"), _s2_paper(paperId="p2")]})
        return _FakeResp({"total": 3, "data": [
            _s2_paper(paperId="p2"), _s2_paper(paperId="p3")]})

    conn = _s2_conn()
    conn._client.get = fake_get
    recs = conn.search("(a OR b) AND c", max_results=10)
    assert queries == ["((a | b) + c)", "((a | b) + c)"]
    assert [r.external_id for r in recs] == ["p1", "p2", "p3"]


def test_s2_search_texto_simples_direto():
    queries = []

    def fake_get(url, params=None, **kw):
        queries.append(params["query"])
        return _FakeResp({"total": 0, "data": []})

    conn = _s2_conn()
    conn._client.get = fake_get
    conn.search("foo (bar", max_results=5)  # parêntese aberto -> inválida
    assert queries == ["foo (bar"]
    queries.clear()
    conn.search("trajectory classification", max_results=5)
    assert queries == ["(trajectory + classification)"]  # AND -> '+'


def test_s2_compiled_query_tolera_sintaxe_bulk():
    """'+' colado como operador AND (sintaxe bulk) é normalizado."""
    conn = semanticscholar.SemanticScholarConnector(min_interval=0)
    bulk_style = ('((mobility|trajectory|movelets)+("big data"|'
                  '"data mining"|analytics|classification))')
    lelia_style = ('((mobility OR trajectory OR movelets) AND '
                   '("big data" OR "data mining" OR analytics OR '
                   'classification))')
    assert conn._compiled_query(bulk_style) == \
        conn._compiled_query(lelia_style)


def test_s2_429_backoff_exponencial(monkeypatch):
    """429 -> nova tentativa com espera exponencial 2^tentativa; sucesso
    na 3ª. on_retry é chamado antes de cada espera."""
    sleeps = []
    retries = []
    monkeypatch.setattr(semanticscholar.time, "sleep",
                        lambda s: sleeps.append(s))
    monkeypatch.setattr(semanticscholar.random, "uniform", lambda a, b: 0)
    calls = []

    def fake_get(url, params=None, **kw):
        calls.append(1)
        if len(calls) < 3:
            r = _FakeResp({})
            r.status_code = 429
            return r
        return _FakeResp({"total": 0, "data": []})

    conn = _s2_conn()
    conn._client.get = fake_get
    conn.search("x", max_results=5,
                on_retry=lambda a, w: retries.append((a, w)))
    assert len(calls) == 3
    assert sleeps == [2, 4]  # 8s não ocorre: sucesso na 3ª tentativa
    assert retries == [(1, 2), (2, 4)]


def test_s2_429_esgota_tentativas(monkeypatch):
    sleeps = []
    monkeypatch.setattr(semanticscholar.time, "sleep",
                        lambda s: sleeps.append(s))
    monkeypatch.setattr(semanticscholar.random, "uniform", lambda a, b: 0)

    def fake_get(url, params=None, **kw):
        r = _FakeResp({})
        r.status_code = 429
        return r

    conn = _s2_conn()
    conn._client.get = fake_get
    with pytest.raises(RuntimeError, match="429"):
        conn.search("x", max_results=5)
    assert sleeps == [2, 4, 8, 16, 32]  # 5 tentativas


def test_s2_ignora_env_api_key(monkeypatch):
    """A chave vem só da config da fonte — env não é lida."""
    monkeypatch.setenv("S2_API_KEY", "env-key")
    conn = semanticscholar.SemanticScholarConnector()
    assert conn.api_key == ""
    conn2 = semanticscholar.SemanticScholarConnector(api_key="param-key")
    assert conn2.api_key == "param-key"


def test_s2_year_filter_enviado():
    seen = []

    def fake_get(url, params=None, **kw):
        seen.append(dict(params))
        return _FakeResp({"total": 0, "data": []})

    conn = _s2_conn(year="2020-2024")
    conn._client.get = fake_get
    conn.search("mobility", max_results=5)
    assert seen[0].get("year") == "2020-2024"
    conn2 = _s2_conn()
    conn2._client.get = fake_get
    conn2.search("mobility", max_results=5)
    assert "year" not in seen[1]


# ---------- Preservação de resultados parciais ----------

def test_openalex_partial_on_late_failure():
    """Falha na página 2 retorna o que já foi coletado (partial=True)."""
    calls = []

    def fake_req(url, params=None, **kw):
        calls.append(dict(params))
        cursor = (params or {}).get("cursor")
        if cursor == "*":
            return {"meta": {"count": 5, "next_cursor": "c2"},
                    "results": [{"display_name": "W1", "id": "a1"},
                                {"display_name": "W2", "id": "a2"}]}
        raise ConnectionError("timeout na página 2")

    conn = openalex.OpenAlexConnector(api_key="k")
    conn._request_json = fake_req
    pages = []
    recs = conn.search("x", max_results=10,
                       on_page=lambda p, b: pages.append((p, len(b))))
    assert [r.title for r in recs] == ["W1", "W2"]
    assert conn.last_request["partial"] is True
    assert "página 2" in conn.last_request["warning"]
    assert pages == [(1, 2)]


def test_openalex_first_page_failure_still_raises():
    conn = openalex.OpenAlexConnector(api_key="k")
    conn._request_json = lambda *a, **k: (_ for _ in ()).throw(
        ConnectionError("down"))
    try:
        conn.search("x", max_results=10)
        assert False, "devia propagar o erro"
    except ConnectionError:
        pass
    assert "partial" not in conn.last_request


def test_openalex_json_invalido_retenta():
    """Corpo truncado (JSON inválido) é falha transitória: retenta e
    emite on_retry como no 429."""
    import json as _json

    class _BadResp:
        status_code = 200
        headers = {}
        text = '{"meta": {"count": 1, "next_cursor": null}, "resul'

        def json(self):
            raise ValueError("Expecting value: line 1 column 1054626")

        def raise_for_status(self):
            pass

    calls = []
    retries = []

    class _Client:
        def get(self, url, params=None, **kw):
            calls.append(1)
            if len(calls) == 1:
                return _BadResp()
            return _FakeResp({"meta": {"count": 1, "next_cursor": None},
                              "results": [{"display_name": "W", "id": "a"}]})

    conn = openalex.OpenAlexConnector(api_key="k", min_interval=0)
    conn._client = _Client()
    recs = conn.search("x", max_results=5,
                       on_retry=lambda a, w: retries.append(a))
    assert [r.title for r in recs] == ["W"]
    assert retries == [1]


# ---------- Filtro de ano via config ----------

def test_get_connector_repassa_year_para_fontes_compativeis():
    for st in ("crossref", "arxiv", "core", "scielo", "scopus",
               "ieee", "sciencedirect", "acm", "semantic_scholar",
               "openalex"):
        conn = get_connector(st, {"year": "2020-2024"})
        assert conn is not None and conn.year == "2020-2024", st
    # dblp não tem filtro de ano — config ignorada sem erro
    conn = get_connector("dblp", {"year": "2020-2024"})
    assert conn is not None


def test_openalex_year_vira_filter_date():
    conn = openalex.OpenAlexConnector(api_key="k", year="2020-2024",
                                      min_interval=0)
    conn._request_json = lambda url, params=None, **kw: {
        "meta": {}, "results": []}
    conn.search("x", max_results=5)
    # params foram para last_request? search usa params locais; verificar
    # via filtro unitário
    from lelia.sources.openalex import _year_date_filter
    assert _year_date_filter("2020-2024") == \
        "from_publication_date:2020-01-01,to_publication_date:2024-12-31"
    assert _year_date_filter("2019-") == "from_publication_date:2019-01-01"
    assert _year_date_filter("-2021") == "to_publication_date:2021-12-31"
    assert _year_date_filter("2022") == \
        "from_publication_date:2022-01-01,to_publication_date:2022-12-31"
    assert _year_date_filter("") == ""
