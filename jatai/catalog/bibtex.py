"""Parser BibTeX simples: extrai campos de entradas @type{...} para formulários."""

import re

_ENTRY_START = re.compile(r"@([A-Za-z]+)\s*\{")
_FIELD = re.compile(r"([A-Za-z][A-Za-z0-9_-]*)\s*=\s*", re.S)

_PUB_TYPES = {
    "article": "artigo",
    "inproceedings": "conferência",
    "conference": "conferência",
    "proceedings": "anais",
    "book": "livro",
    "incollection": "capítulo",
    "inbook": "capítulo",
    "phdthesis": "tese",
    "mastersthesis": "dissertação",
    "techreport": "relatório técnico",
    "misc": "outro",
    "unpublished": "não publicado",
    "online": "online",
}


def _match_brace(text: str, start: int) -> int:
    """Retorna o índice do '}' que fecha a '{' em `start` (ou -1)."""
    depth = 0
    i = start
    while i < len(text):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _read_value(text: str, pos: int) -> tuple[str, int]:
    """Lê um valor BibTeX ({} balanceado, "..." ou número, com concatenação #)."""
    parts: list[str] = []
    i = pos
    while i < len(text):
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        if i >= len(text):
            break
        c = text[i]
        if c == "#":
            parts.append(" ")
            i += 1
            continue
        if c == "{":
            end = _match_brace(text, i)
            if end < 0:
                break
            parts.append(text[i + 1:end])
            i = end + 1
            continue
        if c == '"':
            j = text.find('"', i + 1)
            if j < 0:
                break
            parts.append(text[i + 1:j])
            i = j + 1
            continue
        m = re.match(r"[^,}\s#]+", text[i:])
        if not m:
            break
        parts.append(m.group(0))
        i += m.end()
        # após um valor simples, só continua se vier '#'
        k = i
        while k < len(text) and text[k] in " \t\r\n":
            k += 1
        if k >= len(text) or text[k] != "#":
            break
        i = k
    return "".join(parts).strip(), i


def _clean(value: str) -> str:
    """Remove marcação LaTeX simples e normaliza espaços."""
    v = value.strip()
    v = re.sub(r"\\[{}$%&#_]", lambda m: m.group(0)[1], v)
    v = re.sub(r"\\(em|en|textendash|textemdash|textquotesingle|textquotedblleft|textquotedblright|LaTeX|TeX)", "-", v)
    v = re.sub(r"\\(quad|qquad|,|;|:|!|\?)", " ", v)
    v = re.sub(r"~", " ", v)
    v = re.sub(r"\s+", " ", v)
    return v.strip()


def _strip_braces(value: str) -> str:
    """Remove chaves de agrupamento LaTeX preservando o conteúdo."""
    prev = None
    v = value
    while prev != v:
        prev = v
        v = re.sub(r"\{([^{}]*)\}", r"\1", v)
    return v


def _split_authors(value: str) -> list[str]:
    parts = re.split(r"\s+and\s+", value, flags=re.I)
    return [p.strip() for p in parts if p.strip()]


def _keywords(value: str) -> str:
    return ", ".join(k.strip() for k in re.split(r"[,;]", value) if k.strip())


def parse_bibtex(text: str) -> list[dict]:
    """Extrai entradas BibTeX. Retorna lista de dicts com campos do Paper."""
    entries: list[dict] = []
    for m in _ENTRY_START.finditer(text or ""):
        etype = m.group(1).lower()
        pos = m.end()  # logo após o '{' de abertura
        end = _match_brace(text, pos - 1)
        if end < 0:
            continue
        body = text[pos:end]
        # chave de citação: até a primeira vírgula no nível externo
        comma = body.find(",")
        if comma < 0:
            continue
        fields_text = body[comma + 1:]

        fields: dict[str, str] = {}
        for fm in _FIELD.finditer(fields_text):
            vstart = fm.end()
            value, vend = _read_value(fields_text, vstart)
            fields[fm.group(1).lower()] = _strip_braces(_clean(value))

        title = fields.get("title", "")
        if not title:
            continue
        authors = _split_authors(fields.get("author", ""))
        year_raw = fields.get("year", "")
        try:
            year = int(re.sub(r"\D", "", year_raw)[:4]) or None
        except ValueError:
            year = None
        venue = fields.get("journal") or fields.get("booktitle") or fields.get("school") or ""
        entries.append({
            "entry_type": etype,
            "pub_type": _PUB_TYPES.get(etype, etype),
            "title": title,
            "authors": authors,
            "year": year,
            "doi": fields.get("doi", ""),
            "abstract": fields.get("abstract", ""),
            "url": fields.get("url", "") or fields.get("ee", ""),
            "venue": venue,
            "keywords": _keywords(fields.get("keywords", "")),
        })
    return entries
