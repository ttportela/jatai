"""Conector fake programável para testes do pipeline de execução."""

from jatai.sources.base import RawRecord, SourceConnector


class FakeConnector(SourceConnector):
    source_type = "fake"
    name = "Fake"

    # programado por teste:
    #   results: dict[expression, list[RawRecord]] ou lista única para qualquer expr
    #   error: exceção a levantar (ou None)
    results = {}
    error: Exception | None = None
    calls: list[str] = []
    last_request: dict = {}

    def __init__(self, **_kw):
        pass

    def is_available(self) -> bool:
        return True

    def _records_for(self, expression: str) -> list[RawRecord]:
        if isinstance(self.results, list):
            return list(self.results)
        return list(self.results.get(expression, []))

    def search(self, expression: str, max_results: int = 100,
               on_progress=None, on_retry=None, on_page=None,
               should_stop=None):
        type(self).calls.append(expression)
        self.last_request = {"method": "GET", "url": "fake://search",
                             "params": {"q": expression}}
        recs = self._records_for(expression)
        if type(self).error is not None:
            raise type(self).error
        if on_progress:
            on_progress(1, min(len(recs), max_results), len(recs) or None)
        self.last_request["pages"] = 1
        out = recs[:max_results]
        if on_page and out:
            on_page(1, out)
        return out

    @classmethod
    def reset(cls, results=None, error=None):
        cls.results = results if results is not None else {}
        cls.error = error
        cls.calls = []
        cls.last_request = {}


def rec(title: str, year: int | None = 2020, doi: str = "",
        authors=None, abstract: str = "") -> RawRecord:
    return RawRecord(title=title, authors=authors or ["Autor Teste"],
                     year=year, doi=doi, abstract=abstract,
                     url=f"https://example.com/{title[:10]}", venue="Venue",
                     pub_type="article", external_id=title[:12],
                     keywords=["kw1"])
