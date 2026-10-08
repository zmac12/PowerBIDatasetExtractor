"""Authentication against Entra ID (Azure AD) for the Power BI and Fabric REST APIs.

Supports, in order of precedence:
  1. A raw bearer token (--token or PBI_ACCESS_TOKEN env var)
  2. Service principal (client id + secret + tenant)
  3. Interactive user login (browser, or device code with --device-code),
     with a persistent token cache so you only sign in once.
"""
from __future__ import annotations

import os
import pathlib
import sys

import msal

PBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"

# Azure CLI's public client id. It is first-party and pre-consented in most
# tenants, so no app registration is needed. Override with --client-id.
DEFAULT_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"

CACHE_PATH = pathlib.Path.home() / ".pbi_extractor" / "token_cache.json"


class AuthError(RuntimeError):
    pass


class Authenticator:
    def __init__(self, tenant: str | None = None, client_id: str | None = None,
                 client_secret: str | None = None, device_code: bool = False,
                 static_token: str | None = None):
        self.static_token = static_token or os.environ.get("PBI_ACCESS_TOKEN")
        self.tenant = tenant or os.environ.get("PBI_TENANT_ID")
        self.client_id = client_id or os.environ.get("PBI_CLIENT_ID") or DEFAULT_CLIENT_ID
        self.client_secret = client_secret or os.environ.get("PBI_CLIENT_SECRET")
        self.device_code = device_code
        self._app = None
        self._cache = None

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant or 'organizations'}"

    def _load_cache(self) -> msal.SerializableTokenCache:
        cache = msal.SerializableTokenCache()
        if CACHE_PATH.exists():
            cache.deserialize(CACHE_PATH.read_text(encoding="utf-8"))
        return cache

    def _save_cache(self) -> None:
        if self._cache is not None and self._cache.has_state_changed:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(self._cache.serialize(), encoding="utf-8")

    def _get_app(self):
        if self._app is not None:
            return self._app
        if self.client_secret:
            if not self.tenant:
                raise AuthError("Service principal auth requires --tenant (or PBI_TENANT_ID).")
            self._app = msal.ConfidentialClientApplication(
                self.client_id, authority=self.authority, client_credential=self.client_secret)
        else:
            self._cache = self._load_cache()
            self._app = msal.PublicClientApplication(
                self.client_id, authority=self.authority, token_cache=self._cache)
        return self._app

    def token(self, scope: str = PBI_SCOPE) -> str:
        if self.static_token:
            if scope != PBI_SCOPE:
                raise AuthError("A static token only covers the Power BI API.")
            return self.static_token

        app = self._get_app()
        if self.client_secret:
            result = app.acquire_token_for_client(scopes=[scope])
            return self._unwrap(result)

        result = None
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent([scope], account=accounts[0])
        if not result:
            if self.device_code:
                flow = app.initiate_device_flow(scopes=[scope])
                if "user_code" not in flow:
                    raise AuthError(f"Device flow failed: {flow.get('error_description', flow)}")
                print(flow["message"], file=sys.stderr, flush=True)
                result = app.acquire_token_by_device_flow(flow)
            else:
                print("Opening browser for Power BI sign-in...", file=sys.stderr, flush=True)
                result = app.acquire_token_interactive(scopes=[scope], prompt="select_account")
        token = self._unwrap(result)
        self._save_cache()
        return token

    @staticmethod
    def _unwrap(result: dict | None) -> str:
        if not result or "access_token" not in result:
            detail = (result or {}).get("error_description") or (result or {}).get("error") or result
            raise AuthError(f"Sign-in failed: {detail}")
        return result["access_token"]

    @staticmethod
    def logout() -> bool:
        if CACHE_PATH.exists():
            CACHE_PATH.unlink()
            return True
        return False
