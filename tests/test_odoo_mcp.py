import json

import httpx
import pytest

from odoo_mcp import server
from odoo_mcp.client import OdooClient, OdooConfig, OdooError, _base_url


def make_client(handler, **cfg):
    config = OdooConfig(url="https://demo.odoo.com", database="demo", api_key="secret", **cfg)
    return OdooClient(config, transport=httpx.MockTransport(handler))


@pytest.fixture
def calls():
    return []


@pytest.fixture
def use_client(calls):
    """Instala un cliente falso en el servidor que responde según el método llamado."""

    def install(responses=None, **cfg):
        def handler(request: httpx.Request):
            model, method = request.url.path.split("/")[-2:]
            calls.append({"model": model, "method": method, "body": json.loads(request.content), "headers": request.headers})
            return httpx.Response(200, json=(responses or {}).get(method, True))

        server._client = make_client(handler, **cfg)

    yield install
    server._client = None


def test_base_url_strips_web_paths():
    assert _base_url("https://weddingready.odoo.com/odoo") == "https://weddingready.odoo.com"
    assert _base_url("https://weddingready.odoo.com/web/") == "https://weddingready.odoo.com"
    assert _base_url("https://weddingready.odoo.com") == "https://weddingready.odoo.com"


async def test_client_sends_auth_and_named_args(calls):
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=[{"id": 1, "name": "Ana"}])

    c = make_client(handler)
    result = await c.search_read("res.partner", [["name", "ilike", "ana"]], ["name"], limit=5)
    req = calls[0]
    assert result == [{"id": 1, "name": "Ana"}]
    assert req.url.path == "/json/2/res.partner/search_read"
    assert req.headers["authorization"] == "bearer secret"
    assert req.headers["x-odoo-database"] == "demo"
    assert json.loads(req.content) == {"domain": [["name", "ilike", "ana"]], "fields": ["name"], "limit": 5, "offset": 0}


async def test_client_raises_readable_error():
    body = {"name": "odoo.exceptions.AccessError", "message": "No tienes acceso", "debug": "Traceback..."}
    c = make_client(lambda r: httpx.Response(403, json=body))
    with pytest.raises(OdooError, match="Odoo 403: No tienes acceso"):
        await c.search_count("res.partner")


async def test_search_read_tool_reports_total_when_page_full(use_client, calls):
    use_client({"search_read": [{"id": 1}, {"id": 2}], "search_count": 10})
    result = await server.odoo_search_read("res.partner", [], ["name"], limit=2)
    assert result == {"records": [{"id": 1}, {"id": 2}], "count": 2, "total": 10}


async def test_create_sends_vals_list(use_client, calls):
    use_client({"create": [42]})
    assert await server.odoo_create("res.partner", {"name": "Ana"}) == {"id": 42, "model": "res.partner"}
    assert calls[0]["body"] == {"vals_list": [{"name": "Ana"}]}


async def test_readonly_blocks_writes(use_client, calls):
    use_client(readonly=True)
    with pytest.raises(server.ToolError, match="solo lectura"):
        await server.odoo_update("res.partner", [1], {"name": "x"})
    assert calls == []


async def test_delete_requires_flag(use_client, calls):
    use_client()
    with pytest.raises(server.ToolError, match="ODOO_ALLOW_DELETE"):
        await server.odoo_delete("res.partner", [1])
    assert calls == []


async def test_call_method_rejects_private(use_client):
    use_client()
    with pytest.raises(server.ToolError, match="privados"):
        await server.odoo_call_method("sale.order", "_compute_amounts", [1])


async def test_call_method_passes_ids(use_client, calls):
    use_client()
    await server.odoo_call_method("sale.order", "action_confirm", [7])
    assert calls[0]["method"] == "action_confirm"
    assert calls[0]["body"] == {"ids": [7]}


async def test_tools_are_registered():
    names = {t.name for t in await server.build_server().list_tools()}
    assert names == {
        "odoo_whoami", "odoo_list_models", "odoo_get_fields", "odoo_search_read",
        "odoo_count", "odoo_create", "odoo_update", "odoo_delete", "odoo_call_method",
    }
