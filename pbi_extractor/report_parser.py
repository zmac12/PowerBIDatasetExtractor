"""Figure out which tables/columns/measures a report uses by walking its
definition. Handles all three on-disk formats:

  * legacy .pbix/.pbit  -> Report/Layout (UTF-16 JSON with JSON-in-strings)
  * PBIR inside .pbix   -> Report/definition/**.json
  * .pbip project folder -> <name>.Report/definition/**.json (or report.json)
"""
from __future__ import annotations

import io
import json
import pathlib
import re
import zipfile
from dataclasses import dataclass, field

GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

# Parts of a report package that never reference model objects.
_SKIP_PARTS = ("staticresources", ".semanticmodel", "/.pbi/", "datamodel", "[content_types]",
               "metadata", "settings", "securitybindings", "version", "diagramlayout")


@dataclass
class ReportUsage:
    tables: set[str] = field(default_factory=set)
    columns: set[tuple[str, str]] = field(default_factory=set)
    measures: set[tuple[str, str]] = field(default_factory=set)
    dataset_id: str | None = None
    parts_scanned: int = 0

    def to_dict(self) -> dict:
        return {
            "tables": sorted(self.tables),
            "columns": [f"{t}[{c}]" for t, c in sorted(self.columns)],
            "measures": [f"{t}[{m}]" for t, m in sorted(self.measures)],
            "dataset_id": self.dataset_id,
            "parts_scanned": self.parts_scanned,
        }


def decode_bytes(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    if len(raw) > 1 and raw[1:2] == b"\x00":
        return raw.decode("utf-16-le")
    return raw.decode("utf-8-sig")


def load_report_parts(source) -> dict[str, bytes]:
    """Accepts .pbix/.pbit bytes, a path to one, or a .pbip/.Report folder."""
    if isinstance(source, (bytes, bytearray)):
        with zipfile.ZipFile(io.BytesIO(source)) as zf:
            return {n: zf.read(n) for n in zf.namelist()}
    path = pathlib.Path(source)
    if path.is_dir():
        return {p.relative_to(path).as_posix(): p.read_bytes()
                for p in path.rglob("*") if p.is_file() and p.suffix.lower() in (".json", ".pbir", "")}
    if path.suffix.lower() == ".pbip":
        return load_report_parts(path.parent)
    with zipfile.ZipFile(path) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def _is_report_part(name: str) -> bool:
    low = "/" + name.lower()
    if any(s in low for s in _SKIP_PARTS):
        return False
    return low.endswith("/layout") or low.endswith(".json")


class _Walker:
    def __init__(self, usage: ReportUsage):
        self.u = usage

    def walk(self, node, aliases: dict[str, str]) -> None:
        if isinstance(node, str):
            s = node.strip()
            if s[:1] in "{[" and ("Entity" in s or "SourceRef" in s):
                try:
                    self.walk(json.loads(s), aliases)
                except ValueError:
                    pass
            return
        if isinstance(node, list):
            for item in node:
                self.walk(item, aliases)
            return
        if not isinstance(node, dict):
            return

        # A query's From clause defines aliases ("s" -> "Sales") for its subtree.
        frm = node.get("From")
        if isinstance(frm, list):
            aliases = dict(aliases)
            for f in frm:
                if isinstance(f, dict) and isinstance(f.get("Entity"), str):
                    aliases[f.get("Name", "")] = f["Entity"]

        entity = node.get("Entity")
        if isinstance(entity, str) and entity:
            self.u.tables.add(entity)

        for key, bucket in (("Column", self.u.columns), ("Measure", self.u.measures),
                            ("PropertyVariationSource", self.u.columns)):
            ref = node.get(key)
            if isinstance(ref, dict) and isinstance(ref.get("Property"), str):
                table = self._resolve(ref.get("Expression"), aliases)
                if table:
                    self.u.tables.add(table)
                    bucket.add((table, ref["Property"]))

        for value in node.values():
            self.walk(value, aliases)

    @staticmethod
    def _resolve(expr, aliases: dict[str, str]) -> str | None:
        if not isinstance(expr, dict):
            return None
        src = expr.get("SourceRef")
        if isinstance(src, dict):
            if isinstance(src.get("Entity"), str):
                return src["Entity"]
            return aliases.get(src.get("Source", ""))
        return None


def find_dataset_id(parts: dict[str, bytes]) -> str | None:
    """Thin reports record the semantic model they are bound to."""
    for name, raw in parts.items():
        low = name.lower()
        if low == "connections" or low.endswith("definition.pbir"):
            text = decode_bytes(raw)
            for pattern in (r'"DatasetId"\s*:\s*"(' + GUID + ')"',
                            r"semanticmodelid=(" + GUID + ")",
                            r"Initial Catalog=(" + GUID + ")"):
                m = re.search(pattern, text, re.IGNORECASE)
                if m:
                    return m.group(1)
    return None


def analyze_report(parts: dict[str, bytes]) -> ReportUsage:
    usage = ReportUsage(dataset_id=find_dataset_id(parts))
    walker = _Walker(usage)
    for name, raw in parts.items():
        if not _is_report_part(name):
            continue
        try:
            doc = json.loads(decode_bytes(raw))
        except (ValueError, UnicodeDecodeError):
            continue
        usage.parts_scanned += 1
        walker.walk(doc, {})
    return usage


# ---- Measure dependencies ---------------------------------------------------
_TABLE_REF = re.compile(r"'((?:[^']|'')+)'\s*\[|(?<![\w\]'])([A-Za-z_][\w]*)\s*\[")
_BARE_REF = re.compile(r"(?<![\w'\]])\[((?:[^\]]|\]\])+)\]")
_STRINGS = re.compile(r'"(?:[^"]|"")*"')
_COMMENTS = re.compile(r"//[^\n]*|--[^\n]*|/\*.*?\*/", re.DOTALL)


def dax_references(expression: str, known_tables: set[str]) -> tuple[set[str], set[str]]:
    """Return (tables referenced, bracketed names referenced) in a DAX expression."""
    text = _STRINGS.sub('""', _COMMENTS.sub(" ", expression or ""))
    lower = {t.lower(): t for t in known_tables}
    tables = set()
    for quoted, bare in _TABLE_REF.findall(text):
        name = quoted.replace("''", "'") if quoted else bare
        if name.lower() in lower:
            tables.add(lower[name.lower()])
    # Unbracketed table names used as table expressions, e.g. COUNTROWS(Sales)
    for t in known_tables:
        if re.search(r"(?<![\w'\[])" + re.escape(t) + r"(?![\w\]'])", text) and " " not in t:
            tables.add(t)
    names = {m.replace("]]", "]") for m in _BARE_REF.findall(text)}
    return tables, names


def expand_measure_dependencies(used_measures: set[str], measures: list[dict],
                                known_tables: set[str]) -> set[str]:
    """Walk measure -> measure references transitively and collect every table
    their DAX touches. `measures` items: {"name", "table", "expression"}."""
    by_name = {m["name"].lower(): m for m in measures}
    seen: set[str] = set()
    tables: set[str] = set()
    stack = [n.lower() for n in used_measures]
    while stack:
        key = stack.pop()
        if key in seen or key not in by_name:
            continue
        seen.add(key)
        m = by_name[key]
        if m.get("table"):
            tables.add(m["table"])
        t, names = dax_references(m.get("expression", ""), known_tables)
        tables |= t
        stack.extend(n.lower() for n in names if n.lower() in by_name)
    return tables
