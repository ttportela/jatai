"""Representação lógica (AST) de uma query bibliográfica, independente de fonte.

Term   -> valor + tipo de correspondência (prefix | exact | phrase)
And/Or  -> nós com filhos (operador n-ário)
QueryDefinition -> nome + árvore raiz
"""

from dataclasses import dataclass, field

MATCH_TYPES = {"prefix", "exact", "phrase"}


@dataclass
class Term:
    value: str
    match: str = "prefix"  # prefix | exact | phrase
    negated: bool = False  # NOT aplicado ao termo

    def validate(self) -> str | None:
        v = self.value.strip()
        if not v:
            return "termo vazio"
        if self.match not in MATCH_TYPES:
            return f"match_type inválido: {self.match}"
        return None


@dataclass
class Group:
    """Nó AND/OR com filhos (Term ou Group)."""
    op: str  # "AND" | "OR"
    children: list = field(default_factory=list)
    negated: bool = False  # NOT aplicado ao grupo inteiro

    def validate(self) -> list[str]:
        errs = []
        if self.op not in ("AND", "OR"):
            errs.append(f"operador inválido: {self.op}")
        if not self.children:
            errs.append("grupo vazio")
        for c in self.children:
            if isinstance(c, Group):
                errs.extend(c.validate())
            elif isinstance(c, Term):
                e = c.validate()
                if e:
                    errs.append(e)
            else:
                errs.append(f"nó inválido: {type(c).__name__}")
        return errs


@dataclass
class QueryDefinition:
    name: str
    description: str = ""
    root: Group | Term | None = None

    def validate(self) -> list[str]:
        if self.root is None:
            return ["definição sem expressão raiz"]
        if isinstance(self.root, Group):
            return self.root.validate()
        if isinstance(self.root, Term):
            e = self.root.validate()
            return [e] if e else []
        return ["raiz deve ser Term ou Group"]

    def to_expression(self) -> str:
        """Serializa a árvore em notação lógica legível."""
        return _node_str(self.root)


def _node_str(node) -> str:
    prefix = "NOT " if getattr(node, "negated", False) else ""
    if isinstance(node, Term):
        v = node.value.strip()
        if node.match == "phrase" and " " in v:
            return prefix + f'"{v}"'
        return prefix + v
    if isinstance(node, Group):
        parts = [_node_str(c) for c in node.children]
        joiner = f" {node.op} "
        inner = joiner.join(parts)
        s = f"({inner})" if len(node.children) > 1 else inner
        return prefix + s
    return ""
