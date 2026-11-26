"""Padrão Strategy para conectores de fontes bibliográficas."""

import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class RawRecord:
    """Registro normalizado retornado por qualquer fonte."""

    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    month: int | None = None  # 1-12 quando a fonte informa a data completa
    doi: str = ""
    abstract: str = ""
    url: str = ""
    venue: str = ""
    pub_type: str = ""
    external_id: str = ""
    keywords: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


ProgressCallback = Callable[[int, int, int | None], None]  # (page, fetched, total|None)
# (tentativa, espera_segundos) — emitido antes de cada retry de rede
RetryCallback = Callable[[int, float], None]
# (page, batch) — chamado a cada página concluída com os registros novos
# dela, permitindo persistência incremental sem depender do retorno final
PageCallback = Callable[[int, list["RawRecord"]], None]
# () -> bool — True quando a busca deve parar (cancelamento/pausa do run);
# o conector retorna os parciais em vez de continuar paginando
StopCallback = Callable[[], bool]


class SourceConnector(ABC):
    source_type: str = ""
    name: str = ""
    # True quando a fonte exige API key para funcionar dentro do limite
    # prático de uso (ex.: Semantic Scholar sem key tem cota mínima)
    requires_api_key: bool = False

    @abstractmethod
    def search(self, expression: str, max_results: int = 100,
               on_progress: ProgressCallback | None = None,
               on_retry: RetryCallback | None = None,
               on_page: PageCallback | None = None,
               should_stop: StopCallback | None = None) -> list[RawRecord]:
        """Executa a expressão de busca e retorna registros normalizados.

        Se a paginação falhar depois de páginas já coletadas, o conector
        deve retornar os registros parciais marcando
        ``last_request["partial"] = True`` e ``last_request["warning"]``
        com o motivo — em vez de descartar tudo ou propagar o erro.
        ``should_stop()`` permite interrupção cooperativa (cancelar/pausar
        o agente) entre páginas."""

    @abstractmethod
    def is_available(self) -> bool:
        """Verifica se o conector está operante (rede/config)."""


def connector_classes() -> dict[str, type[SourceConnector]]:
    from jatai.sources import (  # imports tardios evitam ciclos
        acm,
        arxiv,
        crossref,
        core,
        dblp,
        ieeexplore,
        openalex,
        scielo,
        scopus,
        sciencedirect,
        semanticscholar,
    )

    return {
        "dblp": dblp.DBLPConnector,
        "openalex": openalex.OpenAlexConnector,
        "semantic_scholar": semanticscholar.SemanticScholarConnector,
        "scopus": scopus.ScopusConnector,
        "ieee": ieeexplore.IEEEXploreConnector,
        "sciencedirect": sciencedirect.ScienceDirectConnector,
        "scielo": scielo.SciELOConnector,
        "acm": acm.ACMConnector,
        "crossref": crossref.CrossrefConnector,
        "arxiv": arxiv.ArxivConnector,
        "core": core.COREConnector,
    }


def implemented_source_types() -> set[str]:
    """source_types com conector implementado (registry real)."""
    return set(connector_classes())


def get_connector(source_type: str, config: dict | None = None) -> SourceConnector | None:
    """Instancia o conector repassando as chaves de `config` que o
    construtor aceita (year, api_key, mailto, inst_token, sort...).

    Chaves desconhecidas são ignoradas — cada fonte decide o que suporta
    (ex.: DBLP não tem filtro de ano; conectores que aceitam `year`
    recebem o range de cobertura do projeto via config["year"])."""
    cls = connector_classes().get(source_type)
    if cls is None:
        return None
    cfg = config or {}
    try:
        params = inspect.signature(cls.__init__).parameters
    except (TypeError, ValueError):
        params = {}
    kwargs = {k: cfg[k] for k in cfg if k in params}
    # aliases: fontes que nomeiam o parâmetro de ordenação diferente
    if "sort" in cfg and "sort" not in params and "sort_by" in params:
        kwargs["sort_by"] = cfg["sort"]
    if "sort" in cfg and "sort" not in params and "sort_field" in params:
        kwargs["sort_field"] = cfg["sort"]
    return cls(**kwargs)
