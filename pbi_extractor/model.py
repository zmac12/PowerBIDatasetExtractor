"""Read semantic-model metadata and table data through DAX over executeQueries.

executeQueries limits (Pro): 100,000 rows or 1,000,000 values per query,
~15 MB per response, 120 queries/minute/user. We page with TOPNSKIP and shrink
the page automatically when a response is too large.
"""
from __future__ import annotations

import re
import sys
from typing import Iterator

from .api import PowerBIClient, PowerBIError

MAX_ROWS_PER_QUERY = 100_000
MAX_VALUES_PER_QUERY = 1_000_000
AUTO_DATE_PREFIXES = ("LocalDateTable_", "DateTableTemplate_")

_KEY_RE = re.compile(r"\[((?:[^\]]|\]\])*)\]$")


def quote_table(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def quote_column(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def column_name(key: str) -> str:
    """'Sales[Amount]' / '[Amount]' -> 'Amount'."""
    m = _KEY_RE.search(key)
    return m.group(1).replace("]]", "]") if m else key


def normalize_row(row: dict) -> dict:
    return {column_name(k): v for k, v in row.items()}


def is_auto_date_table(name: str) -> bool:
    return name.startswith(AUTO_DATE_PREFIXES)


def list_tables(client: PowerBIClient, dataset_id: str) -> dict[str, list[str]]:
    """{table: [columns]} for every table in the model. COLUMNSTATISTICS() works
    for any user with Build/read permission (unlike INFO.* / DMVs)."""
    rows = client.execute_query(dataset_id, "EVALUATE COLUMNSTATISTICS()")
    tables: dict[str, list[str]] = {}
    for r in map(normalize_row, rows):
        col = r.get("Column Name") or ""
        tables.setdefault(r["Table Name"], [])
        if not col.startswith("RowNumber-"):
            tables[r["Table Name"]].append(col)
    return tables


def list_measures(client: PowerBIClient, dataset_id: str) -> list[dict]:
    """Best effort: measure names + DAX. Tries the INFO.VIEW functions (read-level),
    then INFO.MEASURES (needs write). Returns [] if neither is allowed."""
    for dax in ("EVALUATE SELECTCOLUMNS(INFO.VIEW.MEASURES(), \"name\", [Name], "
                "\"table\", [Table], \"expression\", [Expression])",
                "EVALUATE SELECTCOLUMNS(INFO.MEASURES(), \"name\", [Name], "
                "\"table\", [TableID], \"expression\", [Expression])"):
        try:
            rows = [normalize_row(r) for r in client.execute_query(dataset_id, dax)]
            return [{"name": r.get("name"), "table": r.get("table") if isinstance(r.get("table"), str) else None,
                     "expression": r.get("expression") or ""} for r in rows]
        except PowerBIError:
            continue
    return []


def count_rows(client: PowerBIClient, dataset_id: str, table: str) -> int:
    rows = client.execute_query(dataset_id, f'EVALUATE ROW("n", COUNTROWS({quote_table(table)}))')
    value = next(iter(rows[0].values())) if rows else 0
    return int(value or 0)


def _too_big(err: PowerBIError) -> bool:
    msg = str(err).lower()
    return any(s in msg for s in ("exceed", "too large", "maximum allowed", "response size"))


def iter_table_pages(client: PowerBIClient, dataset_id: str, table: str, columns: list[str],
                     total_rows: int, max_rows: int | None = None) -> Iterator[list[dict]]:
    """Yield the table in pages. Uses TOPNSKIP (the same paging Power BI Desktop's
    data view uses); falls back to WINDOW ordered by every column if needed."""
    target = total_rows if max_rows is None else min(total_rows, max_rows)
    page = max(1, min(MAX_ROWS_PER_QUERY, MAX_VALUES_PER_QUERY // max(1, len(columns))))
    t = quote_table(table)

    if target <= page:
        dax = f"EVALUATE {t}" if max_rows is None else f"EVALUATE TOPNSKIP({target}, 0, {t})"
        try:
            yield [normalize_row(r) for r in client.execute_query(dataset_id, dax)]
            return
        except PowerBIError as e:
            if not _too_big(e):
                raise

    order_by = ", ".join(f"{t}{quote_column(c)}" for c in columns)
    mode, skip = "topnskip", 0
    while skip < target:
        size = min(page, target - skip)
        if mode == "topnskip":
            dax = f"EVALUATE TOPNSKIP({size}, {skip}, {t})"
        else:
            dax = f"EVALUATE WINDOW({skip + 1}, ABS, {skip + size}, ABS, {t}, ORDERBY({order_by}))"
        try:
            rows = client.execute_query(dataset_id, dax)
        except PowerBIError as e:
            if _too_big(e) and page > 1:
                page = max(1, page // 2)
                print(f"    response too large; page size -> {page}", file=sys.stderr, flush=True)
                continue
            if mode == "topnskip" and "topnskip" in str(e).lower():
                mode = "window"
                continue
            raise
        if not rows:
            break
        yield [normalize_row(r) for r in rows]
        skip += len(rows)

