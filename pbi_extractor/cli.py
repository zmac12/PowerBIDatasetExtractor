"""pbi-extract: pull every table a Power BI report uses, no Premium/XMLA required."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import time

from . import model
from .api import PowerBIClient, PowerBIError, is_guid
from .auth import AuthError, Authenticator
from .report_parser import ReportUsage, analyze_report, expand_measure_dependencies, load_report_parts
from .writers import FORMATS, TableWriter, safe_filename


def info(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---- Resolution ---------------------------------------------------------------
def resolve_workspace(client: PowerBIClient, ws: str | None) -> str | None:
    if not ws or ws.lower() in ("me", "my workspace", "my"):
        return None
    if is_guid(ws):
        return ws
    for w in client.workspaces():
        if w["name"].lower() == ws.lower():
            return w["id"]
    raise SystemExit(f"Workspace not found: {ws}")


def _match(items: list[dict], key: str) -> dict | None:
    for it in items:
        if it["id"].lower() == key.lower() or it["name"].lower() == key.lower():
            return it
    return None


def find_item(client: PowerBIClient, kind: str, key: str, workspace: str | None) -> tuple[dict, str | None]:
    """Find a report/dataset by name or id. Without --workspace, searches
    My workspace and then every workspace you can see."""
    lister = client.reports if kind == "report" else client.datasets
    if workspace:
        wid = resolve_workspace(client, workspace)
        hit = _match(lister(wid), key)
        if hit:
            return hit, wid
        raise SystemExit(f"{kind.title()} '{key}' not found in workspace '{workspace}'.")
    hit = _match(lister(None), key)
    if hit:
        return hit, None
    for w in client.workspaces():
        try:
            hit = _match(lister(w["id"]), key)
        except PowerBIError:
            continue
        if hit:
            info(f"Found {kind} in workspace '{w['name']}'")
            return hit, w["id"]
    raise SystemExit(f"{kind.title()} '{key}' not found in any workspace you can access.")


def fetch_report_parts(client: PowerBIClient, report: dict, wid: str | None) -> dict[str, bytes] | None:
    attempts = [("thin .pbix export", lambda: load_report_parts(client.export_report(report["id"], wid, True))),
                ("full .pbix export", lambda: load_report_parts(client.export_report(report["id"], wid, False)))]
    if wid:
        attempts.append(("Fabric getDefinition", lambda: client.fabric_report_definition(wid, report["id"])))
    for label, fn in attempts:
        try:
            info(f"Reading report definition via {label}...")
            return fn()
        except (PowerBIError, AuthError, OSError, ValueError) as e:
            info(f"  {label} failed: {str(e)[:300]}")
    return None


# ---- Commands -------------------------------------------------------------------
def cmd_login(client: PowerBIClient, args) -> None:
    client.auth.token()
    me = client.workspaces()
    info(f"Signed in. You can see {len(me)} workspace(s) plus My workspace.")


def cmd_logout(_client, _args) -> None:
    info("Token cache cleared." if Authenticator.logout() else "No cached sign-in.")


def cmd_list(client: PowerBIClient, args) -> None:
    targets = [(None, "My workspace")]
    if args.workspace:
        wid = resolve_workspace(client, args.workspace)
        targets = [(wid, args.workspace)]
    else:
        targets += [(w["id"], w["name"]) for w in client.workspaces()]
    for wid, name in targets:
        try:
            reports, datasets = client.reports(wid), client.datasets(wid)
        except PowerBIError as e:
            print(f"\n== {name}: {e}")
            continue
        ds_names = {d["id"]: d["name"] for d in datasets}
        print(f"\n== {name}" + (f"  ({wid})" if wid else ""))
        for r in reports:
            print(f"  report   {r['name']}  [{r['id']}]  -> dataset {ds_names.get(r.get('datasetId'), r.get('datasetId'))}")
        for d in datasets:
            print(f"  dataset  {d['name']}  [{d['id']}]")


def cmd_extract(client: PowerBIClient, args) -> None:
    started = time.time()
    report, wid, usage, parts = None, None, None, None

    if args.report_file:
        parts = load_report_parts(args.report_file)
    elif args.report:
        report, wid = find_item(client, "report", args.report, args.workspace)
        info(f"Report: {report['name']} ({report['id']})")
        parts = fetch_report_parts(client, report, wid)

    if parts is not None:
        usage = analyze_report(parts)
        info(f"Report references {len(usage.tables)} table(s), {len(usage.columns)} column(s), "
             f"{len(usage.measures)} measure(s) across {usage.parts_scanned} definition part(s).")

    # Which semantic model to query
    dataset_id, dataset_name = None, None
    if args.dataset:
        ds, _ = (({"id": args.dataset, "name": args.dataset}, None) if is_guid(args.dataset)
                 else find_item(client, "dataset", args.dataset, args.workspace))
        dataset_id, dataset_name = ds["id"], ds["name"]
    elif report:
        dataset_id = report.get("datasetId")
    elif usage and usage.dataset_id:
        dataset_id = usage.dataset_id
    if not dataset_id:
        raise SystemExit("Could not determine the semantic model. Pass --dataset <name or id>.")
    info(f"Semantic model: {dataset_name or dataset_id}")

    model_tables = model.list_tables(client, dataset_id)
    info(f"Model has {len(model_tables)} table(s).")
    measures = model.list_measures(client, dataset_id)
    measure_tables: set[str] = set()
    if usage and measures:
        measure_tables = expand_measure_dependencies({m for _, m in usage.measures}, measures, set(model_tables))

    # Choose tables
    if args.tables:
        wanted = [t.strip() for t in args.tables.split(",") if t.strip()]
        selection = {t for t in model_tables if t.lower() in {w.lower() for w in wanted}}
        reason = "--tables"
    elif args.all_tables or usage is None:
        if usage is None and not args.all_tables:
            info("No report definition available; exporting every table in the model.")
        selection = set(model_tables)
        reason = "all tables"
    else:
        lower = {t.lower(): t for t in model_tables}
        selection = {lower[t.lower()] for t in usage.tables | measure_tables if t.lower() in lower}
        missing = sorted(t for t in usage.tables if t.lower() not in lower)
        if missing:
            info(f"Note: report references objects not in the model (report-level measures/visual calcs?): {missing}")
        reason = "used by report"
    if not args.include_auto_date:
        selection = {t for t in selection if not model.is_auto_date_table(t)}
    selection = sorted(selection)

    info(f"\nTables to extract ({reason}): {len(selection)}")
    for t in selection:
        tag = " (via measure DAX)" if usage and t in measure_tables and t not in usage.tables else ""
        info(f"  - {t}{tag}")

    out_name = safe_filename((report or {}).get("name") or dataset_name or dataset_id)
    out_dir = pathlib.Path(args.out) / out_name
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "extracted_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "report": {k: (report or {}).get(k) for k in ("id", "name", "webUrl", "datasetId")} if report else None,
        "report_file": str(args.report_file) if args.report_file else None,
        "workspace_id": wid,
        "dataset_id": dataset_id,
        "selection_reason": reason,
        "report_usage": usage.to_dict() if usage else None,
        "tables_via_measures": sorted(measure_tables),
        "model_tables": {t: cols for t, cols in sorted(model_tables.items())},
        "tables": [],
    }
    if measures:
        (out_dir / "_measures.json").write_text(json.dumps(measures, indent=2, ensure_ascii=False), encoding="utf-8")

    if args.dry_run:
        _write_manifest(out_dir, manifest)
        info(f"\nDry run: wrote metadata to {out_dir}")
        return

    failures = 0
    for i, table in enumerate(selection, 1):
        cols = model_tables[table]
        path = out_dir / f"{safe_filename(table)}.{args.format}"
        entry = {"table": table, "file": path.name, "columns": cols}
        try:
            total = model.count_rows(client, dataset_id, table)
            info(f"[{i}/{len(selection)}] {table}: {total:,} rows x {len(cols)} cols")
            with TableWriter(path, args.format, cols) as w:
                for page in model.iter_table_pages(client, dataset_id, table, cols, total, args.max_rows):
                    w.write(page)
                    if total > len(page):
                        info(f"    {w.rows:,}/{total:,}")
            expected = total if args.max_rows is None else min(total, args.max_rows)
            entry.update(rows=w.rows, expected_rows=expected, complete=(w.rows == expected))
            if w.rows != expected:
                info(f"    WARNING: wrote {w.rows:,} rows, expected {expected:,}")
        except PowerBIError as e:
            failures += 1
            entry["error"] = str(e)
            info(f"    FAILED: {e}")
        manifest["tables"].append(entry)

    _write_manifest(out_dir, manifest)
    info(f"\nDone in {time.time() - started:.0f}s. {len(selection) - failures}/{len(selection)} tables -> {out_dir}")
    if failures:
        sys.exit(2)


def _write_manifest(out_dir: pathlib.Path, manifest: dict) -> None:
    (out_dir / "_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str),
                                            encoding="utf-8")


# ---- Entry point -------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pbi-extract", description=__doc__)
    auth = p.add_argument_group("authentication")
    auth.add_argument("--tenant", help="Tenant id or domain (default: your home tenant)")
    auth.add_argument("--client-id", help="App registration client id (default: Azure CLI public client)")
    auth.add_argument("--client-secret", help="Use service principal auth with this secret")
    auth.add_argument("--device-code", action="store_true", help="Sign in with a device code instead of a browser")
    auth.add_argument("--token", help="Use this bearer token as-is (or set PBI_ACCESS_TOKEN)")
    p.add_argument("-v", "--verbose", action="store_true")

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="Sign in and cache the token")
    sub.add_parser("logout", help="Clear the cached sign-in")

    ls = sub.add_parser("list", help="List workspaces, reports and datasets")
    ls.add_argument("-w", "--workspace")

    ex = sub.add_parser("extract", help="Export every table a report uses")
    src = ex.add_mutually_exclusive_group()
    src.add_argument("-r", "--report", help="Report name or id in the Power BI service")
    src.add_argument("--report-file", help="Local .pbix / .pbit / .pbip / .Report folder to read the layout from")
    ex.add_argument("-d", "--dataset", help="Semantic model name or id (default: the report's own)")
    ex.add_argument("-w", "--workspace", help="Workspace name or id (default: search all)")
    ex.add_argument("-o", "--out", default="extracts", help="Output folder (default: ./extracts)")
    ex.add_argument("-f", "--format", choices=FORMATS, default="csv")
    ex.add_argument("--tables", help="Comma-separated table list (overrides report detection)")
    ex.add_argument("--all-tables", action="store_true", help="Export every table in the model")
    ex.add_argument("--include-auto-date", action="store_true", help="Include LocalDateTable_* auto date tables")
    ex.add_argument("--max-rows", type=int, help="Cap rows per table (for sampling)")
    ex.add_argument("--dry-run", action="store_true", help="Only work out which tables would be exported")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "extract" and not (args.report or args.report_file or args.dataset):
        raise SystemExit("extract needs --report, --report-file, or --dataset")
    auth = Authenticator(tenant=args.tenant, client_id=args.client_id, client_secret=args.client_secret,
                         device_code=args.device_code, static_token=args.token)
    client = PowerBIClient(auth, verbose=args.verbose)
    handler = {"login": cmd_login, "logout": cmd_logout, "list": cmd_list, "extract": cmd_extract}[args.command]
    try:
        handler(client, args)
    except (AuthError, PowerBIError) as e:
        raise SystemExit(f"Error: {e}")
    except KeyboardInterrupt:
        raise SystemExit("Interrupted.")
