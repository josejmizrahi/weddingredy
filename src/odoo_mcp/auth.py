"""OAuth para usar el servidor como conector remoto en claude.ai.

El servidor actúa como su propio servidor de autorización: cuando alguien conecta el
conector, claude.ai lo manda a ``/login``, donde pega su API key de Odoo. Validamos la clave
contra Odoo y la guardamos *cifrada dentro del token* que recibe Claude. Así:

- cada persona usa Odoo con sus propios permisos;
- no hace falta base de datos: todo (clientes registrados, códigos y tokens) va sellado con
  ``MCP_SECRET_KEY`` (Fernet = AES + HMAC), y sobrevive a reinicios del servidor;
- si se revoca la API key en Odoo, el acceso se corta en la siguiente llamada.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import secrets
import time
import zlib
from typing import Any
from urllib.parse import urlencode, urlparse

from cryptography.fernet import Fernet, InvalidToken
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .client import OdooClient, OdooError

CODE_TTL = 5 * 60
LOGIN_TTL = 15 * 60
ACCESS_TTL = 60 * 60
REFRESH_TTL = 90 * 24 * 60 * 60
DEFAULT_REDIRECT_HOSTS = ("claude.ai", "claude.com", "localhost", "127.0.0.1")


class OdooAccessToken(AccessToken):
    api_key: str
    uid: int


class OdooAuthorizationCode(AuthorizationCode):
    api_key: str
    uid: int


class OdooRefreshToken(RefreshToken):
    api_key: str
    uid: int


class Sealer:
    """Cifra y firma pequeños diccionarios con caducidad."""

    def __init__(self, secret: str):
        if len(secret) < 32:
            raise ValueError("MCP_SECRET_KEY debe tener al menos 32 caracteres")
        self._fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))
        self._hmac_key = hashlib.sha256(b"client-secret:" + secret.encode()).digest()

    def seal(self, kind: str, data: dict, ttl: int) -> str:
        payload = json.dumps({"k": kind, "e": int(time.time()) + ttl, "d": data}, separators=(",", ":"))
        return self._fernet.encrypt(zlib.compress(payload.encode())).decode()

    def open(self, kind: str, token: str) -> dict | None:
        try:
            payload = json.loads(zlib.decompress(self._fernet.decrypt(token.encode())))
        except (InvalidToken, ValueError, zlib.error):
            return None
        if payload.get("k") != kind or payload.get("e", 0) < time.time():
            return None
        return payload["d"]

    def expiry(self, token: str) -> int:
        return json.loads(zlib.decompress(self._fernet.decrypt(token.encode())))["e"]

    def client_secret(self, client_id: str) -> str:
        return hmac.new(self._hmac_key, client_id.encode(), hashlib.sha256).hexdigest()


class OdooOAuthProvider:
    def __init__(
        self,
        odoo: OdooClient,
        secret: str,
        public_url: str,
        allowed_redirect_hosts: tuple[str, ...] = DEFAULT_REDIRECT_HOSTS,
    ):
        self.odoo = odoo
        self.sealer = Sealer(secret)
        self.public_url = public_url.rstrip("/")
        self.allowed_redirect_hosts = allowed_redirect_hosts
        # Protección contra reutilizar un código de autorización (solo en memoria; caducan en 5 min).
        self._used_codes: dict[str, float] = {}

    # --- Registro dinámico de clientes (claude.ai se registra solo) ---

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            host = urlparse(str(uri)).hostname or ""
            if host not in self.allowed_redirect_hosts:
                raise RegistrationError("invalid_redirect_uri", f"Redirección no permitida: {host}")
        data = client_info.model_dump(
            mode="json",
            include={"redirect_uris", "token_endpoint_auth_method", "grant_types", "response_types", "client_name", "scope"},
            exclude_none=True,
        )
        # El client_id lleva sus propios datos sellados, así no hay que guardarlo en ningún lado.
        client_info.client_id = "c." + self.sealer.seal("client", data, ttl=10 * 365 * 24 * 3600)
        if client_info.token_endpoint_auth_method != "none":
            client_info.client_secret = self.sealer.client_secret(client_info.client_id)
            client_info.client_secret_expires_at = 0

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self.sealer.open("client", client_id.removeprefix("c."))
        if data is None:
            return None
        info = OAuthClientInformationFull(client_id=client_id, **data)
        if info.token_endpoint_auth_method != "none":
            info.client_secret = self.sealer.client_secret(client_id)
            info.client_secret_expires_at = 0
        return info

    # --- Autorización: pantalla de login con la API key de Odoo ---

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        pending = self.sealer.seal(
            "login",
            {
                "client_id": client.client_id,
                "client_name": client.client_name,
                "redirect_uri": str(params.redirect_uri),
                "explicit": params.redirect_uri_provided_explicitly,
                "challenge": params.code_challenge,
                "state": params.state,
                "scopes": params.scopes or [],
                "resource": params.resource,
            },
            ttl=LOGIN_TTL,
        )
        return f"{self.public_url}/login?{urlencode({'p': pending})}"

    async def login_page(self, request: Request) -> Response:
        if request.method == "GET":
            pending = request.query_params.get("p", "")
            data = self.sealer.open("login", pending)
            if data is None:
                return self._page("El enlace expiró. Vuelve a conectar el conector desde Claude.", status=400)
            return self._login_form(pending, data)

        form = await request.form()
        pending = str(form.get("p", ""))
        api_key = str(form.get("api_key", "")).strip()
        data = self.sealer.open("login", pending)
        if data is None:
            return self._page("El enlace expiró. Vuelve a conectar el conector desde Claude.", status=400)
        if not api_key:
            return self._login_form(pending, data, error="Pega tu API key de Odoo.")

        try:
            ctx = await self.odoo.with_api_key(api_key).call("res.users", "context_get")
        except OdooError:
            return self._login_form(pending, data, error="Odoo rechazó esa API key. Revisa que esté bien copiada y vigente.")

        code = self.sealer.seal("code", {**data, "api_key": api_key, "uid": ctx["uid"], "nonce": secrets.token_hex(8)}, ttl=CODE_TTL)
        return RedirectResponse(construct_redirect_uri(data["redirect_uri"], code=code, state=data["state"]), status_code=302)

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> OdooAuthorizationCode | None:
        data = self.sealer.open("code", authorization_code)
        if data is None or data["client_id"] != client.client_id or authorization_code in self._used_codes:
            return None
        return OdooAuthorizationCode(
            code=authorization_code,
            scopes=data["scopes"],
            expires_at=self.sealer.expiry(authorization_code),
            client_id=data["client_id"],
            code_challenge=data["challenge"],
            redirect_uri=data["redirect_uri"],
            redirect_uri_provided_explicitly=data["explicit"],
            resource=data["resource"],
            subject=str(data["uid"]),
            api_key=data["api_key"],
            uid=data["uid"],
        )

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: OdooAuthorizationCode) -> OAuthToken:
        now = time.time()
        self._used_codes = {c: exp for c, exp in self._used_codes.items() if exp > now}
        self._used_codes[authorization_code.code] = authorization_code.expires_at
        return self._issue(client.client_id, authorization_code.api_key, authorization_code.uid, authorization_code.scopes, authorization_code.resource)

    # --- Tokens ---

    def _issue(self, client_id: str, api_key: str, uid: int, scopes: list[str], resource: str | None) -> OAuthToken:
        data = {"client_id": client_id, "api_key": api_key, "uid": uid, "scopes": scopes, "resource": resource}
        return OAuthToken(
            access_token=self.sealer.seal("access", data, ACCESS_TTL),
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            refresh_token=self.sealer.seal("refresh", data, REFRESH_TTL),
            scope=" ".join(scopes) or None,
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> OdooRefreshToken | None:
        data = self.sealer.open("refresh", refresh_token)
        if data is None or data["client_id"] != client.client_id:
            return None
        return OdooRefreshToken(
            token=refresh_token,
            client_id=data["client_id"],
            scopes=data["scopes"],
            expires_at=self.sealer.expiry(refresh_token),
            resource=data["resource"],
            subject=str(data["uid"]),
            api_key=data["api_key"],
            uid=data["uid"],
        )

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: OdooRefreshToken, scopes: list[str]) -> OAuthToken:
        if scopes and not set(scopes) <= set(refresh_token.scopes):
            raise TokenError("invalid_scope", "No se pueden ampliar los permisos al refrescar")
        return self._issue(client.client_id, refresh_token.api_key, refresh_token.uid, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> OdooAccessToken | None:
        data = self.sealer.open("access", token)
        if data is None:
            return None
        return OdooAccessToken(
            token=token,
            client_id=data["client_id"],
            scopes=data["scopes"],
            expires_at=self.sealer.expiry(token),
            resource=data["resource"],
            subject=str(data["uid"]),
            api_key=data["api_key"],
            uid=data["uid"],
        )

    async def revoke_token(self, token: Any) -> None:
        # Los tokens son autocontenidos; para cortar el acceso se revoca la API key en Odoo.
        return None

    # --- HTML ---

    def _login_form(self, pending: str, data: dict, error: str = "") -> HTMLResponse:
        app = html.escape(data.get("client_name") or "Claude")
        dest = html.escape(urlparse(data["redirect_uri"]).hostname or "")
        odoo = html.escape(self.odoo.config.url)
        err = f'<p class="error">{html.escape(error)}</p>' if error else ""
        body = f"""
<h1>Conectar Odoo</h1>
<p><b>{app}</b> ({dest}) quiere acceder a <b>{odoo}</b> con tu usuario.</p>
{err}
<form method="post" action="/login">
  <input type="hidden" name="p" value="{html.escape(pending)}">
  <label for="k">API key de Odoo</label>
  <input id="k" name="api_key" type="password" autocomplete="off" required autofocus>
  <button type="submit">Conectar</button>
</form>
<details>
  <summary>¿Dónde consigo mi API key?</summary>
  <p>En Odoo: tu avatar → <i>Mi perfil</i> → pestaña <i>Seguridad de la cuenta</i> → <i>Nueva clave API</i>.
  Claude podrá hacer en Odoo lo mismo que tu usuario.</p>
</details>"""
        return self._page(body, raw=True, status=400 if error else 200)

    @staticmethod
    def _page(content: str, raw: bool = False, status: int = 200) -> HTMLResponse:
        if not raw:
            content = f"<h1>Conectar Odoo</h1><p>{html.escape(content)}</p>"
        return HTMLResponse(
            f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Conectar Odoo</title>
<style>
:root{{color-scheme:light dark;--bg:#f6f5f2;--card:#fff;--fg:#1f1e1c;--muted:#6b6862;--accent:#714b67;--err:#b3261e}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1c1b1a;--card:#262523;--fg:#eceae6;--muted:#a5a19a;--accent:#c79bbd;--err:#f2b8b5}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;padding:16px;box-sizing:border-box}}
main{{background:var(--card);max-width:420px;width:100%;padding:28px;border-radius:14px;box-shadow:0 2px 12px #0002}}
h1{{font-size:1.3rem;margin:0 0 12px}} label{{display:block;font-weight:600;margin:16px 0 6px}}
input{{width:100%;box-sizing:border-box;padding:10px;border:1px solid var(--muted);border-radius:8px;background:transparent;color:inherit;font:inherit}}
button{{margin-top:16px;width:100%;padding:11px;border:0;border-radius:8px;background:var(--accent);color:#fff;font:inherit;font-weight:600;cursor:pointer}}
details{{margin-top:18px;color:var(--muted);font-size:.9rem}} .error{{color:var(--err)}}
</style></head><body><main>{content}</main></body></html>""",
            status_code=status,
            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"},
        )
