"""Normalização e similaridade para deduplicação de publicações."""

import re
import unicodedata

from rapidfuzz import fuzz

_TITLE_THRESHOLD = 92  # similaridade mínima para considerar mesmo trabalho


def normalize_title(title: str) -> str:
    t = unicodedata.normalize("NFKD", title)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def normalize_doi(doi: str) -> str:
    d = doi.strip().lower()
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d)
    d = re.sub(r"^doi:\s*", "", d)
    return d.strip()


def titles_similar(a: str, b: str, threshold: int = _TITLE_THRESHOLD) -> bool:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return False
    return fuzz.ratio(na, nb) >= threshold or fuzz.token_sort_ratio(na, nb) >= threshold


def dedup_key(doi: str, title: str, year: int | None) -> tuple[str, str]:
    """Retorna (tipo, valor) da chave de deduplicação."""
    doi_n = normalize_doi(doi)
    if doi_n:
        return ("doi", doi_n)
    return ("title", f"{normalize_title(title)}|{year or ''}")
