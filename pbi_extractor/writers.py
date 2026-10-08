"""Output writers: stream pages of rows to CSV, JSON Lines, or Parquet."""
from __future__ import annotations

import csv
import json
import pathlib
import re

FORMATS = ("csv", "jsonl", "parquet")


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned or "table"


class TableWriter:
    def __init__(self, path: pathlib.Path, fmt: str, columns: list[str]):
        self.path = path
        self.fmt = fmt
        self.columns = list(columns)
        self.rows = 0
        self._fh = None
        self._csv = None
        self._parquet_pages = []

    def __enter__(self):
        if self.fmt in ("csv", "jsonl"):
            self._fh = open(self.path, "w", newline="", encoding="utf-8-sig" if self.fmt == "csv" else "utf-8")
        return self

    def write(self, rows: list[dict]) -> None:
        if not rows:
            return
        for key in rows[0]:
            if key not in self.columns:
                self.columns.append(key)
        if self.fmt == "csv":
            if self._csv is None:
                self._csv = csv.DictWriter(self._fh, fieldnames=self.columns, extrasaction="ignore")
                self._csv.writeheader()
            self._csv.writerows(rows)
        elif self.fmt == "jsonl":
            for r in rows:
                self._fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        else:
            import pyarrow as pa
            self._parquet_pages.append(pa.Table.from_pylist(rows))
        self.rows += len(rows)

    def __exit__(self, *exc):
        if self.fmt == "csv" and self._csv is None and self._fh:
            csv.writer(self._fh).writerow(self.columns)  # header-only for empty tables
        if self._fh:
            self._fh.close()
        if self.fmt == "parquet" and exc[0] is None:
            import pyarrow as pa
            import pyarrow.parquet as pq
            if self._parquet_pages:
                table = pa.concat_tables(self._parquet_pages, promote_options="permissive")
            else:
                table = pa.table({c: pa.array([], pa.string()) for c in self.columns})
            pq.write_table(table, self.path)
        return False
