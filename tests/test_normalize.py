from app.pipeline.normalize import normalize_document
from app.pipeline.text_extract import ExtractedLine


def _line(text, x0, y0, x1, y1, page=0):
    return ExtractedLine(page=page, text=text, bbox=(x0, y0, x1, y1))


def test_label_and_value_with_offset_y_stay_on_one_row():
    # Menzies header: label and value are separate PDF lines with slightly different y positions.
    lines = [
        _line("Invoice No :", 466, 216.0, 540, 228.0),
        _line("Invoice Date : 26/04/2026", 466, 240.0, 650, 252.0),
        _line("002/12038", 578, 217.5, 630, 229.5),
        _line("Page 1 of 1", 836, 217.5, 895, 229.5),
        _line("Account No :", 466, 264.0, 545, 276.0),
        _line("10199320", 578, 264.5, 625, 276.5),
    ]
    rows = [" ".join(row.split()) for row in normalize_document(lines, {0: "digital"}).source_text.split("\n")]

    assert rows == ["Invoice No : 002/12038 Page 1 of 1", "Invoice Date : 26/04/2026", "Account No : 10199320"]


def test_table_row_keeps_columns_in_x_order():
    lines = [
        _line("£440.00", 845, 450, 895, 462),
        _line("Storage", 47, 450, 95, 462),
        _line("220", 655, 450, 675, 462),
        _line("Pallet", 497, 450, 530, 462),
        _line("£2.00", 755, 450, 785, 462),
    ]
    text = normalize_document(lines, {0: "digital"}).source_text

    assert text == "Storage   Pallet   220   £2.00   £440.00"


def test_pages_are_not_merged():
    lines = [_line("page two", 10, 100, 60, 110, page=1), _line("page one", 10, 100, 60, 110, page=0)]
    assert normalize_document(lines, {0: "digital", 1: "digital"}).source_text == "page one\npage two"
