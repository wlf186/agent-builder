---
name: AB-xlsx
description: "Create, read, edit, validate, and analyze .xlsx/.xlsm/.csv/.tsv files with project-managed Python dependencies."
license: Proprietary. LICENSE.txt has complete terms
---

# AB-XLSX

This Skill uses `openpyxl` and `pandas` in the Agent's project-local uv
environment. It never searches for or writes configuration for a host office
installation.

## Workflow

1. Read tabular data with `pandas` or preserve an existing workbook with
   `openpyxl.load_workbook()`.
2. Apply formulas, formats, number formats, comments, widths, freeze panes, and
   validation with `openpyxl`.
3. Save the result below the execution working directory.
4. Reopen with `data_only=False` and inspect formula cells and references.
5. Validate OOXML when appropriate with `python scripts/office/validate.py`.

Example:

```python
from openpyxl import Workbook

workbook = Workbook()
sheet = workbook.active
sheet.append(["Item", "Amount"])
sheet.append(["Example", 10])
sheet["B3"] = "=SUM(B2:B2)"
workbook.save("output.xlsx")
```

## Formula limitation

`openpyxl` stores formulas but does not calculate them. This local Skill does
not depend on a system office engine, so cached formula results are not
recalculated server-side. State this limitation when a consumer requires
precomputed cached values; the workbook will calculate normally in a compatible
spreadsheet application.
