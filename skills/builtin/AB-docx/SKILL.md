---
name: AB-docx
description: "Create, inspect, edit, validate, unpack, or repack Microsoft Word .docx files using project-managed Python dependencies."
license: Proprietary. LICENSE.txt has complete terms
---

# AB-DOCX

This Skill handles DOCX files without system-wide office software or global npm
packages. Its supported path is pure Python and OOXML processing inside the
Agent's project-local uv environment.

## Supported operations

| Task | Command |
| --- | --- |
| Extract text and table contents | `python scripts/main.py ./input/file.docx --action extract_text` |
| Inspect document metadata | `python scripts/main.py ./input/file.docx --action get_info` |
| Create a document | `python scripts/main.py ./output.docx --action create_document --data '{"title":"Title","content":["Body"]}'` |
| Validate OOXML | `python scripts/office/validate.py ./input/file.docx` |
| Unpack safely for XML editing | `python scripts/office/unpack.py ./input/file.docx ./unpacked` |
| Repack an edited tree | `python scripts/office/pack.py ./unpacked ./output.docx` |

Use paths below the execution working directory. Uploaded files are available
under `./input/`; write deliverables into the working directory and return their
relative paths.

## Editing guidance

- Prefer `python-docx` for paragraphs, runs, tables, sections, and document
  properties.
- Use the unpack/edit/repack workflow only for OOXML features not exposed by
  `python-docx`.
- Parse XML with the included `defusedxml`/`lxml` dependencies.
- Preserve unknown relationships and content types when editing an existing
  archive.
- Run the validator before returning a generated file.

## Deliberately unsupported

Legacy `.doc` conversion, page rendering, PDF conversion, and office-engine
formula/layout recalculation are not part of the local deployment. They require
an external office renderer and therefore fail closed instead of searching for
host-installed executables.
