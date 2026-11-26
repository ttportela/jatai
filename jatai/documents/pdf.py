"""Extração de texto de PDFs com PyMuPDF."""

from pathlib import Path

import pymupdf


def extract_pages(path: str | Path, max_pages: int = 200) -> list[tuple[int, str]]:
    """Retorna [(numero_pagina, texto)] para cada página."""
    doc = pymupdf.open(str(path))
    pages = []
    try:
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            pages.append((i + 1, page.get_text("text") or ""))
    finally:
        doc.close()
    return pages


def extract_text(path: str | Path, max_pages: int = 200) -> str:
    return "\n\n".join(t for _, t in extract_pages(path, max_pages))


def page_count(path: str | Path) -> int:
    doc = pymupdf.open(str(path))
    try:
        return doc.page_count
    finally:
        doc.close()


def extract_to_file(pdf_path: str, out_dir: str | Path) -> str:
    """Extrai texto e grava .txt ao lado do workspace; retorna o caminho."""
    out = Path(out_dir) / (Path(pdf_path).stem + ".txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    pages = extract_pages(pdf_path)
    out.write_text("\n\n".join(f"--- página {n} ---\n{t}" for n, t in pages),
                   encoding="utf-8")
    return str(out)


def chunk_text(text: str, size: int = 6000, overlap: int = 400) -> list[str]:
    """Divide texto em chunks com sobreposição para janelas de contexto."""
    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start:start + size])
        if start + size >= len(text):
            break
        start += size - overlap
    return chunks


def excerpts_for_evidence(text: str, terms: list[str], window: int = 400) -> list[str]:
    """Localiza trechos do texto que mencionam os termos (para Evidence)."""
    found = []
    low = text.lower()
    for term in terms:
        idx = low.find(term.lower())
        if idx >= 0:
            found.append(text[max(0, idx - window // 2): idx + window // 2].strip())
    return found[:10]