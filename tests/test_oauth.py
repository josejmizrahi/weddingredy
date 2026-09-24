"""Simula lo que hace claude.ai al conectar el conector: registro, login, token y llamada."""

import base64
import hashlib
import json
import re
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from starlette.testclient import TestClient

from odoo_mcp import server
from odoo_mcp.auth import OdooOAuthProvider, Sealer
from odoo_mcp.client import OdooClient, OdooConfig

GOOD_KEY = "clave-buena"
PUBLIC = "https://odoo-mcp.example.com"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def fake_odoo(request: httpx.Request) -> httpx.Response:
    if request.headers["authorization"] != f"bearer {GOOD_KEY}":
        return httpx.Response(401, json={"message": "Invalid apikey"})
    method = request.url.path.rsplit("/", 1)[-1]
    if method == "context_get":
        return httpx.Response(200, json={"uid": 7, "lang": "es_MX", "tz": "America/Mexico_City"})
    if method == "search_count":
        return httpx.Response(200, json=123)
    return httpx.Response(200, json=[])


@pytest.fixture
def app():
    config = OdooConfig(url="https://demo.odoo.com", database="demo", api_key="")
    server._client = OdooClient(config, transport=httpx.MockTransport(fake_odoo))
    provider = OdooOAuthProvider(server._client, secret="x" * 40, public_url=PUBLIC)
    starlette_app = server.build_server(provider).streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")
    with TestClient(starlette_app, base_url=PUBLIC) as client:
        yield client
    server._client = None


def register(client, redirect=REDIRECT):
    return client.post("/register", json={"redirect_uris": [redirect], "client_name": "Claude", "token_endpoint_auth_method": "client_secret_post"})


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def login(client, reg, api_key):
    verifier, challenge = pkce()
    resp = client.get("/authorize", params={
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "abc",
    }, follow_redirects=False)
    assert resp.status_code == 302, resp.text
    login_url = resp.headers["location"]
    assert login_url.startswith(f"{PUBLIC}/login?p=")
    page = client.get(login_url)
    assert "Conectar Odoo" in page.text
    pending = re.search(r'name="p" value="([^"]+)"', page.text).group(1)
    return verifier, client.post("/login", data={"p": pending, "api_key": api_key}, follow_redirects=False)


def test_rejects_unknown_redirect_hosts(app):
    assert register(app, "https://evil.example/cb").status_code == 400


def test_bad_api_key_shows_error(app):
    reg = register(app).json()
    _, resp = login(app, reg, "clave-mala")
    assert resp.status_code == 400
    assert "rechazó" in resp.text


def test_mcp_requires_token(app):
    resp = app.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 401
    assert "resource_metadata" in resp.headers["www-authenticate"]


def test_full_flow_then_tool_call_uses_users_key(app):
    reg = register(app).json()
    verifier, resp = login(app, reg, GOOD_KEY)
    assert resp.status_code == 302
    callback = urlparse(resp.headers["location"])
    assert f"{callback.scheme}://{callback.netloc}{callback.path}" == REDIRECT
    query = parse_qs(callback.query)
    assert query["state"] == ["abc"]

    token_req = {
        "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": REDIRECT,
        "client_id": reg["client_id"], "client_secret": reg["client_secret"], "code_verifier": verifier,
    }
    tokens = app.post("/token", data=token_req).json()
    assert "access_token" in tokens and "refresh_token" in tokens
    # El código no se puede reutilizar.
    assert app.post("/token", data=token_req).status_code == 400

    headers = {
        "Authorization": f"Bearer {tokens['access_token']}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
    assert app.post("/mcp", json=init, headers=headers).status_code == 200
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "odoo_count", "arguments": {"model": "res.partner"}}}
    result = app.post("/mcp", json=call, headers=headers).json()["result"]
    assert result.get("isError") is not True, result
    assert "123" in json.dumps(result["content"])

    refreshed = app.post("/token", data={
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
        "client_id": reg["client_id"], "client_secret": reg["client_secret"],
    })
    assert refreshed.status_code == 200 and "access_token" in refreshed.json()


def test_sealer_rejects_wrong_kind_and_tampering():
    s = Sealer("y" * 40)
    token = s.seal("access", {"a": 1}, ttl=60)
    assert s.open("access", token) == {"a": 1}
    assert s.open("refresh", token) is None
    assert s.open("access", token[:-4] + "AAAA") is None
    assert Sealer("z" * 40).open("access", token) is None
