from pathlib import Path

from pypdf import PdfReader


MAX_DOCUMENT_CHARACTERS = 60000


def extract_pdf_text(file_path: str) -> dict:
    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {file_path}")

    if path.suffix.lower() != ".pdf":
        raise ValueError("Only PDF files are supported")

    reader = PdfReader(path)

    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as error:
            raise ValueError(
                "Password-protected PDFs are not supported"
            ) from error

    extracted_pages = []
    total_characters = 0
    truncated = False

    for page_number, page in enumerate(
        reader.pages,
        start=1,
    ):
        page_text = page.extract_text() or ""
        page_text = page_text.strip()

        if not page_text:
            continue

        remaining = (
            MAX_DOCUMENT_CHARACTERS - total_characters
        )

        if remaining <= 0:
            truncated = True
            break

        if len(page_text) > remaining:
            page_text = page_text[:remaining]
            truncated = True

        extracted_pages.append(
            f"[Page {page_number}]\n{page_text}"
        )

        total_characters += len(page_text)

    full_text = "\n\n".join(extracted_pages)

    if not full_text:
        raise ValueError(
            "No readable text was found. The PDF may be scanned."
        )

    return {
        "filename": path.name,
        "page_count": len(reader.pages),
        "text": full_text,
        "character_count": len(full_text),
        "truncated": truncated,
    }