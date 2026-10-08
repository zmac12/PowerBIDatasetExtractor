"""Thin client for the Power BI REST API (Pro-compatible endpoints) plus the
Fabric item-definition API used as a fallback for reading report layouts."""
from __future__ import annotations

import base64
import re
import sys
import time

import requests

from .auth import FABRIC_SCOPE, PBI_SCOPE, Authenticator

PBI_BASE = "https://api.powerbi.com/v1.0/myorg"
FABRIC_BASE = "https://api.fabric.microsoft.com/v1"

GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

RETRYABLE = {429, 500, 502, 503, 504}


def is_guid(value: str | None) -> bool:
    return bool(value and GUID_RE.match(value))


class PowerBIError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _error_message(resp: requests.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:2000] or resp.reason
    err = body.get("error", body)
    # executeQueries buries the real DAX error under pbi.error.details
    details = (err.get("pbi.error") or {}).get("details") or []
    for d in details:
        value = (d.get("detail") or {}).get("value")
        if value:
            return value
    return err.get("message") or err.get("code") or str(body)[:2000]


class PowerBIClient:
    def __init__(self, auth: Authenticator, verbose: bool = False, max_retries: int = 6):
        self.auth = auth
        self.verbose = verbose
        self.max_retries = max_retries
        self.session = requests.Session()

    def log(self, msg: str) -> None:
        if self.verbose:
            print(f"  [api] {msg}", file=sys.stderr, flush=True)

    def request(self, method: str, url: str, scope: str = PBI_SCOPE,
                timeout: float = 300, **kwargs) -> requests.Response:
        if not url.startswith("http"):
            url = PBI_BASE + url
        for attempt in range(self.max_retries + 1):
            headers = kwargs.pop("headers", {}) or {}
            headers["Authorization"] = f"Bearer {self.auth.token(scope)}"
            self.log(f"{method} {url}")
            resp = self.session.request(method, url, headers=headers, timeout=timeout, **kwargs)
            kwargs["headers"] = headers
            if resp.status_code in RETRYABLE and attempt < self.max_retries:
                wait = float(resp.headers.get("Retry-After") or min(60, 2 ** attempt * 2))
                print(f"  HTTP {resp.status_code}; retrying in {wait:.0f}s...", file=sys.stderr, flush=True)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                raise PowerBIError(f"{method} {url} -> HTTP {resp.status_code}: {_error_message(resp)}",
                                   resp.status_code)
            return resp
        raise PowerBIError(f"{method} {url} failed after {self.max_retries} retries")

    def get_json(self, path: str, **kw) -> dict:
        return self.request("GET", path, **kw).json()

    # ---- Discovery -------------------------------------------------------
    @staticmethod
    def _scope(workspace_id: str | None) -> str:
        return f"/groups/{workspace_id}" if workspace_id else ""

    def workspaces(self) -> list[dict]:
        return self.get_json("/groups?$top=5000").get("value", [])

    def reports(self, workspace_id: str | None = None) -> list[dict]:
        return self.get_json(f"{self._scope(workspace_id)}/reports").get("value", [])

    def datasets(self, workspace_id: str | None = None) -> list[dict]:
        return self.get_json(f"{self._scope(workspace_id)}/datasets").get("value", [])

    def get_report(self, report_id: str, workspace_id: str | None = None) -> dict:
        return self.get_json(f"{self._scope(workspace_id)}/reports/{report_id}")

    def get_dataset(self, dataset_id: str, workspace_id: str | None = None) -> dict:
        return self.get_json(f"{self._scope(workspace_id)}/datasets/{dataset_id}")

    # ---- Report definition ----------------------------------------------
    def export_report(self, report_id: str, workspace_id: str | None, live_connect: bool) -> bytes:
        """Download the report as .pbix. LiveConnect mode skips the model, so it is
        small and fast; we only need the layout from it."""
        path = f"{self._scope(workspace_id)}/reports/{report_id}/Export"
        if live_connect:
            path += "?downloadType=LiveConnect"
        return self.request("GET", path, timeout=900).content

    def fabric_report_definition(self, workspace_id: str, report_id: str) -> dict[str, bytes]:
        """Fabric getDefinition (PBIR/legacy parts). Long-running operation."""
        url = f"{FABRIC_BASE}/workspaces/{workspace_id}/reports/{report_id}/getDefinition"
        resp = self.request("POST", url, scope=FABRIC_SCOPE)
        if resp.status_code == 202:
            location = resp.headers["Location"]
            while True:
                time.sleep(float(resp.headers.get("Retry-After") or 2))
                resp = self.request("GET", location, scope=FABRIC_SCOPE)
                status = resp.json().get("status") if resp.content else None
                if status in ("Succeeded", None) and resp.status_code == 200:
                    break
                if status in ("Failed", "Cancelled"):
                    raise PowerBIError(f"getDefinition {status}: {resp.text[:500]}")
            resp = self.request("GET", location.rstrip("/") + "/result", scope=FABRIC_SCOPE)
        parts = resp.json().get("definition", {}).get("parts", [])
        return {p["path"]: base64.b64decode(p["payload"]) for p in parts}

    # ---- DAX -------------------------------------------------------------
    def execute_query(self, dataset_id: str, dax: str) -> list[dict]:
        body = {"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}}
        data = self.request("POST", f"/datasets/{dataset_id}/executeQueries", json=body).json()
        result = data["results"][0]
        if "error" in result:
            raise PowerBIError(f"DAX error: {result['error']}")
        tables = result.get("tables") or [{}]
        if tables[0].get("error"):
            raise PowerBIError(f"DAX error: {tables[0]['error']}")
        return tables[0].get("rows", [])
