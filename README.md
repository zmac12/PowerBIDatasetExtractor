# Power BI Dataset Extractor

Exports every table a Power BI report uses to CSV, JSONL, or Parquet, using only Pro-licensed APIs.
You don't need Premium, PPU, Fabric capacity, or the XMLA endpoint.

## How it works

1. **Finds the report** by name or id, in any workspace you can see.
2. **Reads the report definition** to find the tables, columns, and measures that visuals, filters, and bookmarks reference.
   It tries three routes in order: a thin `.pbix` export, a full `.pbix` export, and the Fabric `getDefinition` API.
   It reads the legacy `Layout` format and the newer PBIR format.
3. **Follows measure dependencies.** It reads measure DAX through `INFO.VIEW.MEASURES()` and adds every table the used measures touch, including measures referenced by other measures.
4. **Exports the data** through the `executeQueries` REST endpoint, which works on Pro.
   It pages with `TOPNSKIP` to stay under the 100k-row / 1M-value per-query limits.
   If a response is too large, it halves the page size and retries. On 429 throttling it backs off and retries.
   If `TOPNSKIP` isn't supported, it falls back to `WINDOW`.
5. **Writes a manifest** (`_manifest.json`) with the report usage, model schema, and row counts.
   Each table is checked against `COUNTROWS`. It also writes the measure definitions (`_measures.json`).

## Setup

```powershell
pip install -e .            # or: pip install msal requests
pip install pyarrow         # only for --format parquet
pbi-extract login           # opens a browser once; token is cached in ~/.pbi_extractor
```

## Usage

```powershell
# What's out there
pbi-extract list
pbi-extract list -w "Finance"

# Everything the report uses -> ./extracts/<report name>/*.csv
pbi-extract extract -r "Sales Overview"
pbi-extract extract -r "Sales Overview" -w "Finance" -f parquet -o D:\exports

# Preview which tables would be pulled (writes the manifest only)
pbi-extract extract -r "Sales Overview" --dry-run

# Whole model, or specific tables
pbi-extract extract -d "Sales Model" --all-tables
pbi-extract extract -d "Sales Model" --tables "Sales,Customer"

# Local file for the layout (.pbix / .pbit / .pbip / .Report folder).
# Thin reports carry their dataset id. Otherwise pass -d.
pbi-extract extract --report-file .\Sales.pbix -d "Sales Model"

# Sample 1000 rows per table
pbi-extract extract -r "Sales Overview" --max-rows 1000
```

Global options go **before** the subcommand:

```powershell
pbi-extract --device-code login                                     # headless sign-in
pbi-extract --tenant contoso.com --client-id <app> --client-secret <s> extract -r ...   # service principal
pbi-extract --token <bearer> extract -r ...                         # bring your own token (or $env:PBI_ACCESS_TOKEN)
pbi-extract -v extract -r ...                                       # log every API call
```

## Requirements on the Power BI side

- You need **Build** permission on the semantic model, or a Contributor+ role in its workspace.
- The tenant setting **"Dataset Execute Queries REST API"** must be on. It's on by default.
- Report download is optional. If it's blocked, the tool tries Fabric `getDefinition`.
  If that's blocked too, use `--report-file` or `--all-tables`.
- Sign-in uses the Azure CLI's public client id, which is pre-consented in most tenants.
  If your tenant blocks it, register a public-client app with the Power BI Service delegated permissions
  `Dataset.Read.All` and `Report.Read.All`, then pass `--client-id`.

## Tests

```powershell
python -m unittest discover -s tests -t .
```
