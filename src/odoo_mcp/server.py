"""Servidor MCP que expone Odoo a Claude."""

from __future__ import annotations

import argparse
import json
import os
from typing import Annotated, Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from .auth import DEFAULT_REDIRECT_HOSTS, OdooAccessToken, OdooOAuthProvider
from .client import OdooClient, OdooConfig, OdooError

MAX_LIMIT = 500

INSTRUCTIONS = """\
Acceso a la instancia de Odoo de la empresa mediante su API.
- Si no conoces un modelo, usa `odoo_list_models`; antes de filtrar o escribir, usa `odoo_get_fields` para ver los campos reales.
- Los dominios usan la sintaxis de Odoo: [["campo", "operador", valor], ...], p. ej. [["name", "ilike", "garcia"]].
- Modelos comunes: res.partner (contactos), sale.order (cotizaciones/pedidos), account.move (facturas),
  product.product / product.template (productos), crm.lead (oportunidades), calendar.event, project.task.
- Pide siempre `fields` concretos en las búsquedas para no traer registros enormes.
- Confirma con el usuario antes de crear, modificar o borrar registros.
"""

# Cliente base. En modo local (stdio) usa ODOO_API_KEY; en modo remoto cada llamada usa la
# API key de la persona conectada, que viaja cifrada dentro de su token OAuth.
_client: OdooClient | None = None

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)

Model = Annotated[str, Field(description="Nombre técnico del modelo, p. ej. 'res.partner'")]
Domain = Annotated[list, Field(description="Dominio de Odoo, p. ej. [['is_company', '=', True]]. Vacío = todos.")]
Ids = Annotated[list[int], Field(description="IDs de los registros", min_length=1)]


def client() -> OdooClient:
    if _client is None:
        raise ToolError("El cliente de Odoo no está inicializado")
    token = get_access_token()
    if isinstance(token, OdooAccessToken):
        return _client.with_api_key(token.api_key)
    if not _client.api_key:
        raise ToolError("No hay API key de Odoo para esta sesión")
    return _client


def ensure_writable() -> None:
    if client().config.readonly:
        raise ToolError("El servidor está en modo solo lectura (ODOO_READONLY=true)")


async def run(coro: Any) -> Any:
    """Convierte errores de Odoo en ToolError para que Claude vea el mensaje."""
    try:
        return await coro
    except OdooError as exc:
        raise ToolError(str(exc)) from exc


async def odoo_whoami() -> dict:
    """Muestra con qué usuario, base de datos y URL está conectado el servidor, y si puede escribir."""
    c = client()
    ctx = await run(c.call("res.users", "context_get"))
    users = await run(c.search_read("res.users", [["id", "=", ctx.get("uid")]], ["name", "login", "company_id"]))
    return {
        "url": c.config.url,
        "database": c.config.database,
        "user": users[0] if users else None,
        "lang": ctx.get("lang"),
        "tz": ctx.get("tz"),
        "readonly": c.config.readonly,
        "allow_delete": c.config.allow_delete,
    }


async def odoo_list_models(
    search: Annotated[str, Field(description="Texto a buscar en el nombre técnico o la descripción")] = "",
    limit: int = 50,
) -> list[dict]:
    """Lista los modelos (tablas) disponibles en Odoo, filtrando por texto."""
    domain: list = [["transient", "=", False]]
    if search:
        domain += ["|", ["model", "ilike", search], ["name", "ilike", search]]
    return await run(client().search_read("ir.model", domain, ["model", "name"], limit=min(limit, MAX_LIMIT), order="model"))


async def odoo_get_fields(
    model: Model,
    search: Annotated[str, Field(description="Filtrar campos cuyo nombre o etiqueta contenga este texto")] = "",
) -> dict:
    """Devuelve los campos de un modelo (tipo, etiqueta, relación, obligatorios, opciones de selección)."""
    fields = await run(client().fields_get(model))
    if search:
        s = search.lower()
        fields = {k: v for k, v in fields.items() if s in k.lower() or s in str(v.get("string", "")).lower()}
    return fields


async def odoo_search_read(
    model: Model,
    domain: Domain = [],
    fields: Annotated[list[str], Field(description="Campos a devolver. Vacío = todos (evítalo, es pesado).")] = [],
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = 50,
    offset: Annotated[int, Field(ge=0)] = 0,
    order: Annotated[str, Field(description="Orden, p. ej. 'create_date desc'")] = "",
) -> dict:
    """Busca registros de cualquier modelo y devuelve sus campos."""
    c = client()
    records = await run(c.search_read(model, domain, fields, limit=limit, offset=offset, order=order or None))
    result: dict[str, Any] = {"records": records, "count": len(records)}
    if len(records) == limit:
        result["total"] = await run(c.search_count(model, domain))
    return result


async def odoo_count(model: Model, domain: Domain = []) -> int:
    """Cuenta cuántos registros de un modelo cumplen el dominio."""
    return await run(client().search_count(model, domain))


async def odoo_create(
    model: Model,
    values: Annotated[dict, Field(description="Valores del nuevo registro, p. ej. {'name': 'Ana', 'email': 'ana@x.com'}")],
) -> dict:
    """Crea un registro y devuelve su ID. Confirma con el usuario antes de usarlo."""
    ensure_writable()
    new_id = await run(client().create(model, values))
    return {"id": new_id, "model": model}


async def odoo_update(
    model: Model,
    ids: Ids,
    values: Annotated[dict, Field(description="Campos a modificar y sus nuevos valores")],
) -> dict:
    """Modifica registros existentes. Confirma con el usuario antes de usarlo."""
    ensure_writable()
    await run(client().write(model, ids, values))
    return {"updated": ids, "model": model}


async def odoo_delete(model: Model, ids: Ids) -> dict:
    """Borra registros definitivamente. Solo disponible con ODOO_ALLOW_DELETE=true."""
    ensure_writable()
    if not client().config.allow_delete:
        raise ToolError("Borrar está desactivado. Actívalo con ODOO_ALLOW_DELETE=true, o archiva con odoo_update {'active': False}.")
    await run(client().unlink(model, ids))
    return {"deleted": ids, "model": model}


async def odoo_call_method(
    model: Model,
    method: Annotated[str, Field(description="Método público del modelo, p. ej. 'action_confirm' en sale.order")],
    ids: Annotated[list[int], Field(description="Registros sobre los que se ejecuta; vacío si es un método de modelo")] = [],
    params: Annotated[dict, Field(description="Argumentos nombrados del método")] = {},
) -> Any:
    """Ejecuta un método de negocio (confirmar pedido, validar factura, enviar email...). Confirma con el usuario antes."""
    ensure_writable()
    if method.startswith("_"):
        raise ToolError("Los métodos privados (que empiezan con '_') no se pueden llamar por la API")
    result = await run(client().call(model, method, ids=ids or None, **params))
    # Algunos métodos devuelven acciones de ventana con objetos no triviales; aseguramos que sea serializable.
    return json.loads(json.dumps(result, default=str))


TOOLS = [
    (odoo_whoami, READ),
    (odoo_list_models, READ),
    (odoo_get_fields, READ),
    (odoo_search_read, READ),
    (odoo_count, READ),
    (odoo_create, WRITE),
    (odoo_update, WRITE),
    (odoo_delete, DESTRUCTIVE),
    (odoo_call_method, DESTRUCTIVE),
]


def build_server(provider: OdooOAuthProvider | None = None) -> MCPServer:
    auth = None
    if provider:
        auth = AuthSettings(
            issuer_url=provider.public_url,
            resource_server_url=f"{provider.public_url}/mcp",
            validate_token_resource=False,
            client_registration_options=ClientRegistrationOptions(enabled=True),
        )
    server = MCPServer("odoo", instructions=INSTRUCTIONS, auth_server_provider=provider, auth=auth)
    for fn, annotations in TOOLS:
        server.add_tool(fn, annotations=annotations)

    @server.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    if provider:
        server.custom_route("/login", methods=["GET", "POST"])(provider.login_page)
    return server


def main() -> None:
    global _client
    parser = argparse.ArgumentParser(description="Servidor MCP para Odoo")
    parser.add_argument("--http", action="store_true", default=os.environ.get("MCP_TRANSPORT") == "http",
                        help="Servir por HTTPS con OAuth (para claude.ai) en vez de stdio")
    args = parser.parse_args()

    if not args.http:
        _client = OdooClient(OdooConfig.from_env())
        build_server().run()
        return

    # Render define RENDER_EXTERNAL_URL solo; en otros hosts hay que poner PUBLIC_URL.
    public_url = os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    missing = [k for k, v in (("PUBLIC_URL", public_url), ("MCP_SECRET_KEY", os.environ.get("MCP_SECRET_KEY"))) if not v]
    if missing:
        raise SystemExit(f"Faltan variables de entorno para el modo HTTP: {', '.join(missing)}")
    _client = OdooClient(OdooConfig.from_env(require_api_key=False))
    hosts = os.environ.get("MCP_ALLOWED_REDIRECT_HOSTS")
    provider = OdooOAuthProvider(
        _client,
        secret=os.environ["MCP_SECRET_KEY"],
        public_url=public_url,
        allowed_redirect_hosts=tuple(h.strip() for h in hosts.split(",")) if hosts else DEFAULT_REDIRECT_HOSTS,
    )
    build_server(provider).run(
        "streamable-http",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
