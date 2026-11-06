"""Run real isolated PDF extraction against bounded synthetic documents."""

import io
import subprocess
import sys
from tempfile import TemporaryDirectory
from pathlib import Path
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def _pdf(pages=1, *, encrypted=False, blank=False, short=False, owner_only=False):
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=2000, height=500)
        if not blank:
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
            )
            stream = DecodedStreamObject()
            text = b"Short text." if short else b"Official announcement evidence. " * 5
            stream.set_data(b"BT /F1 12 Tf 10 10 Td (" + text + b") Tj ET")
            page[NameObject("/Contents")] = writer._add_object(stream)
    if owner_only:
        writer.encrypt("", owner_password="fixture-owner", permissions_flag=0)
    elif encrypted:
        writer.encrypt("fixture")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _parse(payload, *, max_pages=200, max_text=1_000_000):
    with TemporaryDirectory(prefix="test-company-text-") as directory:
        return _parse_in_directory(payload, directory, max_pages, max_text)


def _parse_in_directory(payload, directory, max_pages, max_text):
    pdf = Path(directory) / "input.pdf"
    pdf.write_bytes(payload)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "trader.download.infra.company_document_parser",
            "--directory",
            directory,
            "--input",
            str(pdf),
            "--max-bytes",
            "16777216",
            "--max-pages",
            str(max_pages),
            "--max-text-characters",
            str(max_text),
        ],
        input=payload,
        capture_output=True,
        timeout=15,
        check=False,
    )
