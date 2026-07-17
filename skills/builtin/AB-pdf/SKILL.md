---
name: AB-pdf
description: "Read, inspect, fill, render, or create PDF documents with project-managed Python libraries."
license: Proprietary. LICENSE.txt has complete terms
---

# AB-PDF

The PDF Skill is self-contained in the Agent's uv environment. It uses `pypdf`,
`pdfplumber`, `pypdfium2`, Pillow, and ReportLab; it has no Poppler or other
system executable dependency.

## Commands

| Task | Command |
| --- | --- |
| Extract text | `python scripts/main.py ./input/file.pdf --action extract_text` |
| Inspect form fields | `python scripts/main.py ./input/file.pdf --action extract_forms` |
| Fill form fields | `python scripts/main.py ./input/file.pdf --action fill_form --data '{"field":"value"}' --output ./filled.pdf` |
| Render pages to PNG | `python scripts/main.py ./input/file.pdf --action convert_images --output ./pages` |
| Check fillability | `python scripts/check_fillable_fields.py ./input/file.pdf` |

Keep input and output paths inside the execution working directory. Apply
bounded page/character limits for large documents and return generated file
paths explicitly.

For new PDFs, use ReportLab. For structural edits and forms, use `pypdf`. For
layout-aware extraction, use `pdfplumber`. For rendering, use `pypdfium2`.
