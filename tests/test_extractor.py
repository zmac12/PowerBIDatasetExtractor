import io
import json
import pathlib
import re
import tempfile
import unittest
import zipfile

from pbi_extractor import model
from pbi_extractor.api import PowerBIError
from pbi_extractor.report_parser import (analyze_report, dax_references, expand_measure_dependencies,
                                         load_report_parts)
from pbi_extractor.writers import TableWriter


def legacy_layout() -> bytes:
    """Mimics Report/Layout: UTF-16LE JSON whose config/filters are JSON strings."""
    visual_config = {
        "singleVisual": {
            "prototypeQuery": {
                "From": [{"Name": "s", "Entity": "Sales", "Type": 0},
                         {"Name": "d", "Entity": "Date", "Type": 0}],
                "Select": [
                    {"Column": {"Expression": {"SourceRef": {"Source": "d"}}, "Property": "Year"}},
                    {"Measure": {"Expression": {"SourceRef": {"Source": "s"}}, "Property": "Total Sales"}},
                    {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Source": "s"}},
                                                               "Property": "Qty"}}, "Function": 0}},
                ],
            }
        }
    }
    page_filter = [{"expression": {"Column": {"Expression": {"SourceRef": {"Entity": "Region"}},
                                              "Property": "Country"}}}]
    layout = {"sections": [{"displayName": "Page 1", "filters": json.dumps(page_filter),
                            "visualContainers": [{"config": json.dumps(visual_config)}]}]}
    return json.dumps(layout).encode("utf-16-le")


def pbix(parts: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in parts.items():
            zf.writestr(name, data)
    return buf.getvalue()


class ReportParserTests(unittest.TestCase):
    def test_legacy_layout(self):
        conn = json.dumps({"RemoteArtifacts": [{"DatasetId": "11111111-2222-3333-4444-555555555555"}]})
        usage = analyze_report(load_report_parts(pbix({"Report/Layout": legacy_layout(),
                                                       "Connections": conn.encode()})))
        self.assertEqual(usage.tables, {"Sales", "Date", "Region"})
        self.assertIn(("Date", "Year"), usage.columns)
        self.assertIn(("Sales", "Qty"), usage.columns)
        self.assertIn(("Region", "Country"), usage.columns)
        self.assertEqual(usage.measures, {("Sales", "Total Sales")})
        self.assertEqual(usage.dataset_id, "11111111-2222-3333-4444-555555555555")

    def test_pbir_folder(self):
        visual = {"visual": {"query": {"queryState": {"Values": {"projections": [
            {"field": {"Column": {"Expression": {"SourceRef": {"Entity": "Product"}}, "Property": "Color"}}},
            {"field": {"HierarchyLevel": {"Expression": {"Hierarchy": {"Expression": {"PropertyVariationSource": {
                "Expression": {"SourceRef": {"Entity": "Orders"}}, "Name": "Variation", "Property": "OrderDate"}},
                "Hierarchy": "Date Hierarchy"}}, "Level": "Year"}}}]}}}}}
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "Demo.Report"
            vdir = root / "definition" / "pages" / "p1" / "visuals" / "v1"
            vdir.mkdir(parents=True)
            (vdir / "visual.json").write_text(json.dumps(visual))
            (root / "definition.pbir").write_text(json.dumps({"datasetReference": {"byConnection": {
                "connectionString": "Data Source=powerbi://x;semanticmodelid=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}}}))
            theme = root / "StaticResources" / "theme.json"
            theme.parent.mkdir()
            theme.write_text(json.dumps({"Entity": "NotATable"}))
            usage = analyze_report(load_report_parts(root))
        self.assertEqual(usage.tables, {"Product", "Orders"})
        self.assertIn(("Orders", "OrderDate"), usage.columns)
        self.assertEqual(usage.dataset_id, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    def test_measure_dependencies(self):
        known = {"Sales", "Fx Rates", "Date", "Budget"}
        measures = [
            {"name": "Total Sales", "table": "Sales", "expression": "SUMX(Sales, Sales[Qty] * RELATED('Fx Rates'[Rate]))"},
            {"name": "Sales vs Budget", "table": "Sales", "expression": "[Total Sales] - [Budget Amt] // 'Date'[x]"},
            {"name": "Budget Amt", "table": "Budget", "expression": "SUM(Budget[Amount])"},
            {"name": "Unused", "table": "Date", "expression": "COUNTROWS('Date')"},
        ]
        self.assertEqual(expand_measure_dependencies({"Sales vs Budget"}, measures, known),
                         {"Sales", "Fx Rates", "Budget"})
        tables, _ = dax_references('CALCULATE([X], "Date"[y])', known)
        self.assertEqual(tables, set())


class FakeClient:
    """Serves a fake model through the same DAX shapes the extractor issues."""

    def __init__(self, data: dict[str, list[dict]], value_limit=None, no_topnskip=False):
        self.data = data
        self.value_limit = value_limit
        self.no_topnskip = no_topnskip
        self.queries = []

    def execute_query(self, dataset_id, dax):
        self.queries.append(dax)
        if dax == "EVALUATE COLUMNSTATISTICS()":
            return [{"[Table Name]": t, "[Column Name]": c}
                    for t, rows in self.data.items() for c in [*rows[0], "RowNumber-2662979B"]]
        m = re.match(r"EVALUATE ROW\(\"n\", COUNTROWS\('(.*)'\)\)", dax)
        if m:
            return [{"[n]": len(self.data[m.group(1)])}]
        m = re.match(r"EVALUATE TOPNSKIP\((\d+), (\d+), '(.*)'\)$", dax)
        if m:
            if self.no_topnskip:
                raise PowerBIError("Failed to resolve name 'TOPNSKIP'.")
            n, skip, t = int(m.group(1)), int(m.group(2)), m.group(3)
            return self._rows(t, self.data[t][skip:skip + n])
        m = re.match(r"EVALUATE WINDOW\((\d+), ABS, (\d+), ABS, '(.*?)', ORDERBY", dax)
        if m:
            a, b, t = int(m.group(1)), int(m.group(2)), m.group(3)
            return self._rows(t, self.data[t][a - 1:b])
        m = re.match(r"EVALUATE '(.*)'$", dax)
        if m:
            return self._rows(m.group(1), self.data[m.group(1)])
        raise PowerBIError(f"unexpected {dax}")

    def _rows(self, table, rows):
        if self.value_limit and rows and len(rows) * len(rows[0]) > self.value_limit:
            raise PowerBIError("The query exceeded the maximum allowed response size.")
        return [{f"{table}[{k}]": v for k, v in r.items()} for r in rows]


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.orig_rows = model.MAX_ROWS_PER_QUERY
        model.MAX_ROWS_PER_QUERY = 7

    def tearDown(self):
        model.MAX_ROWS_PER_QUERY = self.orig_rows

    def _export(self, client, table):
        cols = model.list_tables(client, "ds")[table]
        total = model.count_rows(client, "ds", table)
        out = []
        for page in model.iter_table_pages(client, "ds", table, cols, total):
            out.extend(page)
        return cols, out

    def test_list_tables_drops_rownumber(self):
        client = FakeClient({"Sales": [{"Id": 1, "Amt": 2.5}]})
        self.assertEqual(model.list_tables(client, "ds"), {"Sales": ["Id", "Amt"]})

    def test_paging_returns_all_rows_including_duplicates(self):
        rows = [{"Id": i % 5, "Name": f"n{i % 5}"} for i in range(23)]
        client = FakeClient({"Sales": rows})
        _, out = self._export(client, "Sales")
        self.assertEqual(out, rows)
        self.assertEqual(sum("TOPNSKIP" in q for q in client.queries), 4)

    def test_page_shrinks_when_response_too_large(self):
        rows = [{"Id": i, "V": i} for i in range(20)]
        client = FakeClient({"T": rows}, value_limit=8)
        _, out = self._export(client, "T")
        self.assertEqual(out, rows)

    def test_window_fallback(self):
        rows = [{"Id": i} for i in range(15)]
        client = FakeClient({"T": rows}, no_topnskip=True)
        _, out = self._export(client, "T")
        self.assertEqual(out, rows)

    def test_quoting(self):
        self.assertEqual(model.quote_table("Bob's Sales"), "'Bob''s Sales'")
        self.assertEqual(model.column_name("Sales[Amt [USD]]]"), "Amt [USD]")


class CliTests(unittest.TestCase):
    def test_extract_from_report_file_exports_only_used_tables(self):
        from pbi_extractor import cli
        data = {"Sales": [{"Qty": 1, "Total": 2}], "Date": [{"Year": 2024}], "Region": [{"Country": "US"}],
                "Unused": [{"x": 1}], "LocalDateTable_abc": [{"Date": "2024-01-01"}]}
        client = FakeClient(data)
        with tempfile.TemporaryDirectory() as tmp:
            pbix_path = pathlib.Path(tmp) / "r.pbix"
            pbix_path.write_bytes(pbix({"Report/Layout": legacy_layout()}))
            args = cli.build_parser().parse_args(
                ["extract", "--report-file", str(pbix_path), "--dataset", "11111111-2222-3333-4444-555555555555",
                 "--out", tmp])
            cli.cmd_extract(client, args)
            out = pathlib.Path(tmp) / "11111111-2222-3333-4444-555555555555"
            self.assertEqual(sorted(p.name for p in out.glob("*.csv")), ["Date.csv", "Region.csv", "Sales.csv"])
            manifest = json.loads((out / "_manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(all(t["complete"] for t in manifest["tables"]))


class WriterTests(unittest.TestCase):
    def test_formats(self):
        rows = [{"a": 1, "b": None}, {"a": 2, "b": "x"}]
        with tempfile.TemporaryDirectory() as tmp:
            for fmt in ("csv", "jsonl", "parquet"):
                path = pathlib.Path(tmp) / f"t.{fmt}"
                with TableWriter(path, fmt, ["a", "b"]) as w:
                    w.write(rows[:1])
                    w.write(rows[1:])
                self.assertEqual(w.rows, 2)
                self.assertGreater(path.stat().st_size, 0)
            self.assertEqual((pathlib.Path(tmp) / "t.csv").read_text(encoding="utf-8-sig").splitlines(),
                             ["a,b", "1,", "2,x"])


if __name__ == "__main__":
    unittest.main()
