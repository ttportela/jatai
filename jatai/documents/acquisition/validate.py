"""Validação de PDFs adquiridos: bytes, integridade, páginas e
correspondência com os metadados do artigo."""

import re
from pathlib import Path

import pymupdf
from rapidfuzz import fuzz

MAX_BYTES = 50 * 1024 * 1024
MIN_BYTES = 1000
MIN_PAGES = 1
# abaixo disso, quando há texto extraível, o PDF provavelmente é
# de outro trabalho — rejeita como "conteúdo divergente"
TITLE_MATCH_MIN = 40.0

_WORD_RE = re.compile(r"[^a-z0-9 ]+")


def _norm(text: str) -> str:
    return _WORD_RE.sub(" ", (text or "").lower()).strip()


def is_pdf_bytes(data: bytes, ctype: str = "") -> bool:
    """Verifica assinatura %PDF e rejeita HTML disfarçado."""
    if len(data) < MIN_BYTES or len(data) > MAX_BYTES:
        return False
    if b"%PDF" in data[:1024]:
        return True
    return "pdf" in ctype.lower() and not data.lstrip()[:1] == b"<"


def looks_like_pdf(url: str) -> bool:
    path = (url or "").split("?")[0].split("#")[0].lower()
    return ".pdf" in path or "/pdf" in path


def _title_similarity(path: Path, expected_title: str) -> float | None:
    """Comparação fuzzy do título esperado com o texto das 2 primeiras
    páginas. Retorna None quando não há texto extraível (PDF escaneado)."""
    if not expected_title or len(_norm(expected_title)) < 8:
        return None
    try:
        doc = pymupdf.open(str(path))
    except Exception:  # noqa: BLE001
        return None
    try:
        text = ""
        for i in range(min(2, doc.page_count)):
            text += doc[i].get_text("text") or ""
            if len(text) > 4000:
                break
    finally:
        doc.close()
    if len(text.strip()) < 300:
        return None
    return fuzz.token_set_ratio(_norm(expected_title), _norm(text))


def validate_pdf_file(path: str | Path,
                      expected_title: str | None = None) -> dict:
    """Valida um arquivo PDF já gravado.

    Retorna {"ok": bool, "pages": int, "title_match": float|None,
             "reason": str}.
    """
    p = Path(path)
    if not p.exists():
        return {"ok": False, "pages": 0, "title_match": None,
                "reason": "arquivo inexistente"}
    size = p.stat().st_size
    if size < MIN_BYTES:
        return {"ok": False, "pages": 0, "title_match": None,
                "reason": f"arquivo pequeno demais ({size} bytes)"}
    if size > MAX_BYTES:
        return {"ok": False, "pages": 0, "title_match": None,
                "reason": "arquivo excede 50MB"}
    try:
        with open(p, "rb") as f:
            head = f.read(1024)
        if b"%PDF" not in head:
            return {"ok": False, "pages": 0, "title_match": None,
                    "reason": "sem assinatura %PDF"}
        doc = pymupdf.open(str(p))
        pages = doc.page_count
        doc.close()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "pages": 0, "title_match": None,
                "reason": f"PDF inválido: {exc}"}
    if pages < MIN_PAGES:
        return {"ok": False, "pages": pages, "title_match": None,
                "reason": "PDF sem páginas"}
    match = _title_similarity(p, expected_title or "")
    if match is not None and match < TITLE_MATCH_MIN:
        return {"ok": False, "pages": pages, "title_match": match,
                "reason": f"conteúdo diverge do artigo "
                          f"(título {match:.0f}%)"}
    return {"ok": True, "pages": pages, "title_match": match,
            "reason": "ok"}


__all__ = ["is_pdf_bytes", "looks_like_pdf", "validate_pdf_file",
           "MAX_BYTES", "MIN_BYTES", "TITLE_MATCH_MIN"]
