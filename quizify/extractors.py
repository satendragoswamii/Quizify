"""Turn any supported source into a flat list of :class:`Block`s.

Every extractor produces the same shape, so the parsing engine never has to care
whether the quiz arrived as a Word file, a PDF, a spreadsheet, or pasted text.
Optional dependencies degrade gracefully: a missing library disables that one
format instead of breaking the import.
"""

import csv
import io
import json
import os
import re
import zipfile
from typing import Any, Dict, List, Optional, Tuple

from .models import Block
from .patterns import normalize_text

# --- optional dependencies -------------------------------------------------------------

try:
    from docx import Document
    from docx.oxml.ns import qn
    DOCX_OK = True
except ImportError:  # pragma: no cover
    DOCX_OK = False

try:
    import pdfplumber
    PDF_OK = True
except ImportError:  # pragma: no cover
    PDF_OK = False

try:
    from bs4 import BeautifulSoup
    HTML_OK = True
except ImportError:  # pragma: no cover
    HTML_OK = False

try:
    from striprtf.striprtf import rtf_to_text
    RTF_OK = True
except ImportError:  # pragma: no cover
    RTF_OK = False

try:
    import openpyxl
    XLSX_OK = True
except ImportError:  # pragma: no cover
    XLSX_OK = False

try:
    import chardet
    CHARDET_OK = True
except ImportError:  # pragma: no cover
    CHARDET_OK = False

try:  # OCR needs the tesseract binary too, so treat it as best-effort.
    import pytesseract
    from PIL import Image
    OCR_OK = True
except ImportError:  # pragma: no cover
    OCR_OK = False


TEXT_EXTS = {".txt", ".text", ".md", ".markdown", ".rst", ".log"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp"}

SUPPORTED_EXTS = (
    {".docx", ".doc", ".pdf", ".csv", ".tsv", ".xlsx", ".xlsm", ".xls",
     ".json", ".html", ".htm", ".xml", ".rtf"}
    | TEXT_EXTS
    | IMAGE_EXTS
)


class ExtractionError(Exception):
    """Raised when a source cannot be read at all."""


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------

def _decode(data: bytes) -> str:
    """Best-effort bytes -> str, trying declared/detected encodings before falling back."""
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    if CHARDET_OK:
        guess = chardet.detect(data[:200_000]) or {}
        enc = guess.get("encoding")
        if enc:
            try:
                return data.decode(enc)
            except (UnicodeDecodeError, LookupError):
                pass
    return data.decode("latin-1", errors="replace")


def _blocks_from_lines(lines: List[str], origin: str) -> List[Block]:
    """Common tail step: normalise lines, drop blanks, remember where blanks were."""
    blocks: List[Block] = []
    pending_blank = False
    for raw in lines:
        text = normalize_text(raw)
        if not text:
            pending_blank = True
            continue
        blocks.append(Block(text=text, index=len(blocks), origin=origin, blank_before=pending_blank))
        pending_blank = False
    return blocks


def _is_bold(runs) -> bool:
    """True when every run carrying visible text is bold (partial bolding is not a cue)."""
    visible = [r for r in runs if r.text and r.text.strip()]
    return bool(visible) and all(bool(r.bold) for r in visible)


def _is_highlighted(runs) -> bool:
    for run in runs:
        if not (run.text and run.text.strip()):
            continue
        try:
            if run.font.highlight_color is not None:
                return True
        except (AttributeError, ValueError):
            pass
    return False


# --------------------------------------------------------------------------------------
# DOCX
# --------------------------------------------------------------------------------------

def _docx_numbering(para) -> Optional[int]:
    """Word auto-numbering level, when the visible number lives in the XML not the text."""
    try:
        pPr = para._p.find(qn("w:pPr"))
        if pPr is None:
            return None
        numPr = pPr.find(qn("w:numPr"))
        if numPr is None:
            return None
        ilvl = numPr.find(qn("w:ilvl"))
        return int(ilvl.get(qn("w:val"))) if ilvl is not None else 0
    except (AttributeError, TypeError, ValueError):
        return None


def _docx_paragraph_block(para, index: int, pending_blank: bool, origin: str) -> Optional[Block]:
    text = normalize_text(para.text)
    if not text:
        return None
    return Block(
        text=text,
        index=index,
        bold=_is_bold(para.runs),
        italic=all(bool(r.italic) for r in para.runs if r.text.strip()) if para.runs else False,
        underline=all(bool(r.underline) for r in para.runs if r.text.strip()) if para.runs else False,
        highlight=_is_highlighted(para.runs),
        list_level=_docx_numbering(para),
        origin=origin,
        blank_before=pending_blank,
    )


def extract_docx(data: bytes) -> List[Block]:
    if not DOCX_OK:
        raise ExtractionError("Reading .docx needs the 'python-docx' package.")
    try:
        doc = Document(io.BytesIO(data))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise ExtractionError(f"This .docx file could not be opened ({exc}).") from exc

    blocks: List[Block] = []
    pending_blank = False

    for para in doc.paragraphs:
        if not para.text.strip():
            pending_blank = True
            continue
        block = _docx_paragraph_block(para, len(blocks), pending_blank, "docx")
        if block:
            blocks.append(block)
            pending_blank = False

    # Tables often hold the whole quiz, one question per row or per cell.
    for table in doc.tables:
        for row in table.rows:
            seen_cells = set()
            for cell in row.cells:
                if id(cell._tc) in seen_cells:  # merged cells repeat across the row
                    continue
                seen_cells.add(id(cell._tc))
                for para in cell.paragraphs:
                    if not para.text.strip():
                        continue
                    block = _docx_paragraph_block(para, len(blocks), False, "table")
                    if block:
                        blocks.append(block)
    return blocks


def extract_doc(data: bytes) -> List[Block]:
    """Legacy binary .doc — we can only salvage readable text runs."""
    text = _decode(data)
    lines = [ln for ln in re.split(r"[\r\n]+", text) if len(re.findall(r"[A-Za-z]", ln)) > 3]
    if not lines:
        raise ExtractionError(
            "Legacy .doc files are not supported. Please re-save the file as .docx or .pdf."
        )
    return _blocks_from_lines(lines, "doc")


# --------------------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------------------

def extract_pdf(data: bytes) -> List[Block]:
    if not PDF_OK:
        raise ExtractionError("Reading .pdf needs the 'pdfplumber' package.")
    blocks: List[Block] = []
    empty_pages = 0
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page_no, page in enumerate(pdf.pages, 1):
                text = page.extract_text(x_tolerance=1.5, y_tolerance=3) or ""
                if not text.strip():
                    empty_pages += 1
                    continue
                pending_blank = True
                for raw in text.split("\n"):
                    line = normalize_text(raw)
                    if not line:
                        pending_blank = True
                        continue
                    blocks.append(Block(
                        text=line, index=len(blocks), origin="pdf",
                        blank_before=pending_blank, page=page_no,
                    ))
                    pending_blank = False

                # Ruled tables are invisible to extract_text, so pull them separately.
                for table in page.extract_tables() or []:
                    for row in table:
                        for cell in row:
                            line = normalize_text(cell or "")
                            if line and not any(b.text == line for b in blocks[-40:]):
                                blocks.append(Block(
                                    text=line, index=len(blocks), origin="pdf",
                                    page=page_no,
                                ))
    except Exception as exc:  # pdfminer raises a wide range of parse errors
        raise ExtractionError(f"This PDF could not be read ({exc}).") from exc

    if not blocks:
        hint = " It looks like a scanned PDF — try an OCR'd copy or paste the text instead."
        raise ExtractionError("No text could be extracted from this PDF." + (hint if empty_pages else ""))
    return blocks


# --------------------------------------------------------------------------------------
# Plain text / Markdown / RTF / HTML
# --------------------------------------------------------------------------------------

def extract_text(data: bytes) -> List[Block]:
    return blocks_from_text(_decode(data), origin="text")


def blocks_from_text(text: str, origin: str = "text") -> List[Block]:
    """Public entry point for pasted text."""
    return _blocks_from_lines(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), origin)


def extract_rtf(data: bytes) -> List[Block]:
    raw = _decode(data)
    if RTF_OK:
        try:
            return blocks_from_text(rtf_to_text(raw, errors="ignore"), origin="rtf")
        except Exception:  # striprtf is strict about malformed control words
            pass
    stripped = re.sub(r"\\[a-z]+-?\d*\s?|[{}]", " ", raw)
    return blocks_from_text(stripped, origin="rtf")


def extract_html(data: bytes) -> List[Block]:
    raw = _decode(data)
    if not HTML_OK:
        text = re.sub(r"<br\s*/?>|</(?:p|div|li|tr|h\d)>", "\n", raw, flags=re.I)
        return blocks_from_text(re.sub(r"<[^>]+>", " ", text), origin="html")

    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    blocks: List[Block] = []
    emphasis = {"b", "strong", "mark"}
    for node in soup.find_all(["p", "li", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "div", "pre"]):
        # Skip containers whose text is fully covered by a nested block we'll visit anyway.
        if node.find(["p", "li", "td", "th", "div"]):
            continue
        text = normalize_text(node.get_text(" ", strip=True))
        if not text:
            continue
        bold = node.name in emphasis or bool(
            node.find(list(emphasis)) and normalize_text(node.find(list(emphasis)).get_text(" ", strip=True)) == text
        )
        blocks.append(Block(text=text, index=len(blocks), bold=bold, origin="html"))
    if not blocks:
        return blocks_from_text(soup.get_text("\n"), origin="html")
    return blocks


# --------------------------------------------------------------------------------------
# Tabular sources
# --------------------------------------------------------------------------------------

_COL_ALIASES = {
    "question": {"question", "questions", "question text", "q", "questiontext", "stem", "problem", "prompt"},
    "answer": {"answer", "correct", "correct answer", "correct option", "key", "ans", "solution"},
    "explanation": {"explanation", "rationale", "reason", "justification", "note"},
    "type": {"type", "question type", "qtype", "format"},
    "marks": {"marks", "mark", "points", "score", "weight"},
}
_OPTION_COL_RE = re.compile(r"^(?:option|opt|choice|ans(?:wer)?)\s*[_\- ]?\s*([A-Za-z]|\d{1,2})$", re.I)


def _classify_columns(headers: List[str]) -> Dict[str, Any]:
    """Map a spreadsheet header row onto our known column roles."""
    mapping: Dict[str, Any] = {"options": []}
    for idx, raw in enumerate(headers):
        name = (raw or "").strip().lower()
        if not name:
            continue
        opt = _OPTION_COL_RE.match(name)
        if opt:
            mapping["options"].append(idx)
            continue
        for role, aliases in _COL_ALIASES.items():
            if name in aliases and role not in mapping:
                mapping[role] = idx
                break
    return mapping


def _rows_to_blocks(rows: List[List[str]], origin: str) -> List[Block]:
    """Structured sheet -> blocks. Falls back to flattening when headers are unfamiliar."""
    rows = [[normalize_text(str(c)) if c is not None else "" for c in row] for row in rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return []

    mapping = _classify_columns(rows[0])
    structured = "question" in mapping and mapping["options"]

    blocks: List[Block] = []
    if structured:
        q_idx = mapping["question"]
        for row in rows[1:]:
            def cell(i: Optional[int]) -> str:
                return row[i] if i is not None and i < len(row) else ""

            question = cell(q_idx)
            if not question:
                continue
            blocks.append(Block(text=f"Q{len(blocks) + 1}. {question}", index=len(blocks),
                                origin=origin, blank_before=True))
            for n, opt_idx in enumerate(mapping["options"], 1):
                value = cell(opt_idx)
                if value:
                    blocks.append(Block(text=f"{chr(64 + n)}. {value}", index=len(blocks), origin=origin))
            for role, prefix in (("answer", "Answer"), ("explanation", "Explanation"), ("marks", "Marks")):
                value = cell(mapping.get(role))
                if value:
                    blocks.append(Block(text=f"{prefix}: {value}", index=len(blocks), origin=origin))
        if blocks:
            return blocks

    # Unknown layout: emit each non-empty cell as its own line and let the engine sort it out.
    lines: List[str] = []
    for row in rows:
        cells = [c for c in row if c]
        if len(cells) == 1:
            lines.append(cells[0])
        else:
            lines.extend(cells)
        lines.append("")
    return _blocks_from_lines(lines, origin)


def extract_csv(data: bytes, delimiter: Optional[str] = None) -> List[Block]:
    text = _decode(data)
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:8000], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    return _rows_to_blocks(rows, "csv")


def extract_xlsx(data: bytes) -> List[Block]:
    if not XLSX_OK:
        raise ExtractionError("Reading .xlsx needs the 'openpyxl' package.")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ExtractionError(f"This spreadsheet could not be opened ({exc}).") from exc

    blocks: List[Block] = []
    try:
        for sheet in wb.worksheets:
            rows = [[c for c in row] for row in sheet.iter_rows(values_only=True)]
            sheet_blocks = _rows_to_blocks(rows, "xlsx")
            for block in sheet_blocks:
                block.index = len(blocks)
                blocks.append(block)
    finally:
        wb.close()
    return blocks


def extract_json(data: bytes) -> List[Block]:
    """Accepts already-structured quiz JSON in several common shapes."""
    try:
        payload = json.loads(_decode(data))
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"This file is not valid JSON ({exc}).") from exc

    items: List[Any] = []
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        for key in ("questions", "items", "data", "quiz", "results", "records"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
        else:
            items = [payload]

    lines: List[str] = []
    for n, item in enumerate(items, 1):
        if not isinstance(item, dict):
            lines.extend([str(item), ""])
            continue
        lowered = {str(k).lower(): v for k, v in item.items()}
        question = next((lowered[k] for k in ("question", "text", "stem", "prompt", "title", "q")
                         if lowered.get(k)), None)
        if not question:
            continue
        lines.append(f"Q{n}. {question}")

        options = next((lowered[k] for k in ("options", "choices", "answers", "alternatives")
                        if isinstance(lowered.get(k), (list, dict))), None)
        labels: List[str] = []
        if isinstance(options, dict):
            for i, (key, value) in enumerate(sorted(options.items()), 1):
                label = key.strip().upper()[:1] if str(key).strip()[:1].isalpha() else chr(64 + i)
                labels.append(label)
                lines.append(f"{label}. {value}")
        elif isinstance(options, list):
            for i, value in enumerate(options, 1):
                label = chr(64 + i)
                if isinstance(value, dict):
                    body = next((value[k] for k in ("text", "label", "value", "option") if k in value), str(value))
                    if value.get("correct") or value.get("is_correct"):
                        body = f"{body} *"
                    value = body
                labels.append(label)
                lines.append(f"{label}. {value}")

        answer = next((lowered[k] for k in ("answer", "correct", "correct_answer", "correctoption", "key")
                       if lowered.get(k) is not None), None)
        if answer is not None:
            if isinstance(answer, int) and labels and 1 <= answer <= len(labels):
                answer = labels[answer - 1]
            elif isinstance(answer, list):
                answer = ", ".join(str(a) for a in answer)
            lines.append(f"Answer: {answer}")

        explanation = next((lowered[k] for k in ("explanation", "rationale", "reason") if lowered.get(k)), None)
        if explanation:
            lines.append(f"Explanation: {explanation}")
        lines.append("")

    if not lines:
        raise ExtractionError("No questions were found in this JSON file.")
    return _blocks_from_lines(lines, "json")


# --------------------------------------------------------------------------------------
# Images (OCR)
# --------------------------------------------------------------------------------------

def extract_image(data: bytes) -> List[Block]:
    if not OCR_OK:
        raise ExtractionError(
            "Reading images requires OCR. Install 'pytesseract' plus the Tesseract binary, "
            "or paste the quiz text directly."
        )
    try:
        text = pytesseract.image_to_string(Image.open(io.BytesIO(data)))
    except Exception as exc:
        raise ExtractionError(f"OCR failed on this image ({exc}).") from exc
    if not text.strip():
        raise ExtractionError("No readable text was found in this image.")
    return blocks_from_text(text, origin="ocr")


# --------------------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------------------

_EXTRACTORS = {
    ".docx": extract_docx,
    ".doc": extract_doc,
    ".pdf": extract_pdf,
    ".rtf": extract_rtf,
    ".html": extract_html,
    ".htm": extract_html,
    ".xml": extract_html,
    ".csv": extract_csv,
    ".tsv": lambda d: extract_csv(d, delimiter="\t"),
    ".xlsx": extract_xlsx,
    ".xlsm": extract_xlsx,
    ".xls": extract_xlsx,
    ".json": extract_json,
}


def _sniff_format(data: bytes, ext: str) -> str:
    """Trust magic bytes over the extension — misnamed uploads are common."""
    head = data[:8]
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"PK\x03\x04"):
        # An OOXML container: decide between .docx and .xlsx by what's inside.
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
            if any(n.startswith("word/") for n in names):
                return ".docx"
            if any(n.startswith("xl/") for n in names):
                return ".xlsx"
        except zipfile.BadZipFile:
            pass
    if head.startswith(b"{\\rtf"):
        return ".rtf"
    if head.startswith(b"\xd0\xcf\x11\xe0"):  # legacy OLE2 (.doc / .xls)
        return ".xls" if ext in (".xls", ".xlsx") else ".doc"
    if head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".jpg" if head[:3] == b"\xff\xd8\xff" else ".png"
    return ext


def extract_blocks(data: bytes, filename: str) -> Tuple[List[Block], str]:
    """Extract blocks from an uploaded file. Returns ``(blocks, resolved_format)``."""
    ext = os.path.splitext(filename or "")[1].lower()
    resolved = _sniff_format(data, ext)

    if resolved in IMAGE_EXTS:
        return extract_image(data), resolved

    extractor = _EXTRACTORS.get(resolved)
    if extractor is None:
        if resolved in TEXT_EXTS or not resolved:
            return extract_text(data), resolved or ".txt"
        raise ExtractionError(
            f"'{resolved}' files are not supported. Supported types: "
            + ", ".join(sorted(SUPPORTED_EXTS))
        )
    return extractor(data), resolved


def blocks_to_text(blocks: List[Block]) -> str:
    """Re-render blocks as plain text — used when handing content to the AI assist."""
    out: List[str] = []
    for block in blocks:
        if block.blank_before and out:
            out.append("")
        out.append(block.text)
    return "\n".join(out)
