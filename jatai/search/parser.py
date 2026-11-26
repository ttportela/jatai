"""Parser de expressões de busca em texto para a AST de domínio.

Suporta: AND, OR, NOT (maiúsculas ou minúsculas), ``|`` como OR,
parênteses, frases entre aspas e termos simples. AND implícito é
reconhecido entre primárias adjacentes (``a b`` = ``a AND b``).

Termos com ``$`` no final viram match="exact"; frases viram
match="phrase"; demais viram match="prefix".
"""

import re

from jatai.search.domain import Group, Term

_TOKEN_RE = re.compile(
    r"""
    \s*(?:
        (?P<lparen>\() | (?P<rparen>\)) |
        (?P<quote>"[^"]*") |
        (?P<or>\|\||\||\bOR\b) |
        (?P<and>\bAND\b) |
        (?P<not>\bNOT\b) |
        (?P<term>[^\s()|]+)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self) -> tuple[str, str] | None:
        t = self.peek()
        if t:
            self.pos += 1
        return t

    def parse(self):
        node = self._or()
        if self.peek() is not None:
            raise ValueError(f"token inesperado: {self.peek()}")
        return node

    def _or(self):
        children = [self._and()]
        while self.peek() and self.peek()[0] == "or":
            self.next()
            children.append(self._and())
        return children[0] if len(children) == 1 else Group("OR", children)

    def _and(self):
        children = [self._not()]
        while True:
            t = self.peek()
            if t is None or t[0] == "rparen" or t[0] == "or":
                break
            if t[0] == "and":
                self.next()
            children.append(self._not())
        return children[0] if len(children) == 1 else Group("AND", children)

    def _not(self):
        if self.peek() and self.peek()[0] == "not":
            self.next()
            node = self._not()
            node.negated = not getattr(node, "negated", False)
            return node
        return self._primary()

    def _primary(self):
        t = self.next()
        if t is None:
            raise ValueError("expressão incompleta")
        kind, val = t
        if kind == "lparen":
            node = self._or()
            if not self.peek() or self.peek()[0] != "rparen":
                raise ValueError("parêntese não fechado")
            self.next()
            return node
        if kind == "rparen":
            raise ValueError("parêntese sem abertura")
        if kind == "quote":
            return Term(value=val[1:-1].strip(), match="phrase")
        if kind == "term":
            if val.endswith("$") and len(val) > 1:
                return Term(value=val[:-1], match="exact")
            return Term(value=val, match="prefix")
        raise ValueError(f"token inesperado: {val}")


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens = []
    pos = 0
    for m in _TOKEN_RE.finditer(text):
        # garante que não há caracteres não-tokenizados entre matches
        if text[pos:m.start()].strip():
            raise ValueError("caractere inválido na expressão")
        pos = m.end()
        kind = m.lastgroup
        tokens.append((kind, m.group(kind)))
    if text[pos:].strip():
        raise ValueError("caractere inválido na expressão")
    return tokens


def parse_expression(text: str):
    """Retorna Term|Group da AST, ou None se a expressão for inválida/vazia."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        tokens = _tokenize(text)
        if not tokens:
            return None
        return _Parser(tokens).parse()
    except (ValueError, IndexError):
        return None
