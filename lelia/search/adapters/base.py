"""Interface Strategy para adaptadores de fonte bibliográfica."""

from abc import ABC, abstractmethod

from lelia.search.domain import QueryDefinition


class QueryAdapter(ABC):
    source_type: str = ""

    @abstractmethod
    def compile(self, definition: QueryDefinition, limit: int = 25) -> list[str]:
        """Traduz a QueryDefinition para expressões próprias da fonte."""


def get_adapter(source_type: str) -> QueryAdapter | None:
    from lelia.search.adapters import (
        dblp,
        openalex,
        semanticscholar,
        scopus,
        ieeexplore,
        sciencedirect,
        scielo,
        acm,
        crossref,
        arxiv,
        core,
    )

    registry = {
        "dblp": dblp.DBLPQueryAdapter,
        "openalex": openalex.OpenAlexQueryAdapter,
        "semantic_scholar": semanticscholar.SemanticScholarQueryAdapter,
        "scopus": scopus.ScopusQueryAdapter,
        "ieee": ieeexplore.IEEEXploreQueryAdapter,
        "sciencedirect": sciencedirect.ScienceDirectQueryAdapter,
        "scielo": scielo.SciELOQueryAdapter,
        "acm": acm.ACMQueryAdapter,
        "crossref": crossref.CrossrefQueryAdapter,
        "arxiv": arxiv.ArxivQueryAdapter,
        "core": core.COREQueryAdapter,
    }
    cls = registry.get(source_type)
    return cls() if cls else None
