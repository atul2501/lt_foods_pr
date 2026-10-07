"""PO / BL (Bill of Lading) numbers mentioned in an email's subject or body - recorded in the
result's `email` block as Subject_PO / Subject_BL / Body_PO / Body_BL."""
import re
from html.parser import HTMLParser

# Value after a label: letters/digits with - / . inside, at least 4 chars, at least one digit
# (so "PO Box", "BL attached" etc. don't match).
_VALUE = r"(?=[A-Z0-9\-/.]*\d)([A-Z0-9][A-Z0-9\-/.]{2,}[A-Z0-9])"
# "No", "No.", "Number", "#", ":" ... between the label and the value.
_SEP = r"[\s:#.\-]*(?:(?:no|nr|num|number|ref)\b\.?[\s:#.\-]*)?"

_PO_RE = re.compile(
    r"\b(?:p\.?\s?o\b\.?|purch[a-z]*\s+order)" + _SEP + _VALUE,
    re.IGNORECASE,
)
_BL_RE = re.compile(
    r"(?:\bb\s?/\s?l\b|\bbl\b|\bbol\b|\bbill\s+of\s+lading\b)" + _SEP + _VALUE,
    re.IGNORECASE,
)
# SAP purchase orders start with 66 - counted as a PO even with no label.
PO_66_RE = re.compile(r"(?<![A-Za-z0-9])66\d{6,}(?![A-Za-z0-9])")


# Column headers of a table in the body - "PO No.", "Purchase Order", "Booking/BL number", "B/L No".
_PO_HEADER_RE = re.compile(r"\bp\.?\s?o\b|purch\w*\s+order", re.IGNORECASE)
_BL_HEADER_RE = re.compile(r"\bb\s?/\s?l\b|\bbl\b|\bbol\b|bill\s+of\s+lading", re.IGNORECASE)
_HEADER_MAX_CHARS = 40
_CELL_VALUE_RE = re.compile(_VALUE, re.IGNORECASE)


class _TableParser(HTMLParser):
    """Collects every <table> of an HTML body as rows of cell texts."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._stack: list[list[list[str]]] = []   # open tables (they can be nested)
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._stack[-1].append([])
        elif tag in ("td", "th") and self._stack:
            if not self._stack[-1]:
                self._stack[-1].append([])
            self._cell = []
        elif self._cell is not None:
            self._cell.append(" ")  # "Booking/BL<br>number" -> "Booking/BL number"

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._stack:
            self._stack[-1][-1].append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "table" and self._stack:
            self.tables.append(self._stack.pop())

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _column_values(html: str | None, header_re: re.Pattern) -> list[str]:
    """Values under a matching column header, in every table of html: header row
    "PO No. | Inv no. | ..." then "9400000909 | 9103002612 | ..." gives ["9400000909"]."""
    if not html or "<table" not in html.lower():
        return []
    parser = _TableParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed HTML: no table values, not a failed email
        return []
    values = []
    for rows in parser.tables:
        for header_index, row in enumerate(rows):
            columns = [i for i, cell in enumerate(row) if len(cell) <= _HEADER_MAX_CHARS and header_re.search(cell)]
            if columns:
                break
        else:
            continue
        for row in rows[header_index + 1:]:
            for i in columns:
                if i < len(row) and _CELL_VALUE_RE.fullmatch(row[i]):
                    values.append(row[i])
    return values


def _join(values: list[str]) -> str | None:
    seen: list[str] = []
    for value in values:
        value = value.strip(" .-/")
        if value and value not in seen:
            seen.append(value)
    return ", ".join(seen) or None


def find_po(text: str | None, html: str | None = None) -> str | None:
    """Every PO number in text (and in a PO column of html's tables), in order, comma-joined -
    None when there is none."""
    found = sorted(
        [(m.start(1), m.group(1)) for m in _PO_RE.finditer(text or "")]
        + [(m.start(), m.group()) for m in PO_66_RE.finditer(text or "")]
    )
    return _join([value for _, value in found] + _column_values(html, _PO_HEADER_RE))


def find_bl(text: str | None, html: str | None = None) -> str | None:
    """Every BL (Bill of Lading) number in text (and in a BL column of html's tables), in order,
    comma-joined - None when there is none."""
    return _join([m.group(1) for m in _BL_RE.finditer(text or "")] + _column_values(html, _BL_HEADER_RE))
