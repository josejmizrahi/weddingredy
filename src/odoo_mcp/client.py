"""Cliente mínimo para la API JSON-2 de Odoo (Odoo 19+ / saas~19).

Cada llamada es un POST a ``/json/2/<modelo>/<método>`` con los argumentos
nombrados en el cuerpo JSON y la API key en el header ``Authorization``.
Documentación: https://www.odoo.com/documentation/19.0/developer/reference/external_api.html
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx


class OdooError(Exception):
    """Error devuelto por Odoo (credenciales, permisos, validación, etc.)."""


@dataclass(frozen=True)
class OdooConfig:
    url: str
    database: str
    api_key: str
    readonly: bool = False
    allow_delete: bool = False
    timeout: float = 30.0

    @classmethod
    def from_env(cls) -> OdooConfig:
        missing = [k for k in ("ODOO_URL", "ODOO_DB", "ODOO_API_KEY") if not os.environ.get(k)]
        if missing:
            raise OdooError(f"Faltan variables de entorno: {', '.join(missing)}")
        return cls(
            url=_base_url(os.environ["ODOO_URL"]),
            database=os.environ["ODOO_DB"],
            api_key=os.environ["ODOO_API_KEY"],
            readonly=_env_flag("ODOO_READONLY"),
            allow_delete=_env_flag("ODOO_ALLOW_DELETE"),
            timeout=float(os.environ.get("ODOO_TIMEOUT", "30")),
        )


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "si", "sí"}


def _base_url(url: str) -> str:
    """Acepta tanto ``https://x.odoo.com`` como ``https://x.odoo.com/odoo``."""
    url = url.strip().rstrip("/")
    for suffix in ("/odoo", "/web"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url


class OdooClient:
    def __init__(self, config: OdooConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._http = httpx.AsyncClient(
            base_url=config.url,
            timeout=config.timeout,
            transport=transport,
            headers={
                "Authorization": f"bearer {config.api_key}",
                "X-Odoo-Database": config.database,
                "User-Agent": "odoo-mcp",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def call(self, model: str, method: str, ids: list[int] | None = None, **params: Any) -> Any:
        body = dict(params)
        if ids is not None:
            body["ids"] = ids
        try:
            resp = await self._http.post(f"/json/2/{model}/{method}", json=body)
        except httpx.HTTPError as exc:
            raise OdooError(f"No se pudo conectar con Odoo: {exc}") from exc
        if resp.is_success:
            return resp.json()
        raise OdooError(_error_message(resp))

    async def search_read(
        self,
        model: str,
        domain: list | None = None,
        fields: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        order: str | None = None,
    ) -> list[dict]:
        params: dict[str, Any] = {"domain": domain or [], "offset": offset}
        if fields:
            params["fields"] = fields
        if limit:
            params["limit"] = limit
        if order:
            params["order"] = order
        return await self.call(model, "search_read", **params)

    async def search_count(self, model: str, domain: list | None = None) -> int:
        return await self.call(model, "search_count", domain=domain or [])

    async def fields_get(self, model: str, attributes: list[str] | None = None) -> dict:
        return await self.call(model, "fields_get", attributes=attributes or ["string", "type", "required", "readonly", "relation", "selection", "help"])

    async def create(self, model: str, values: dict) -> int:
        ids = await self.call(model, "create", vals_list=[values])
        return ids[0] if isinstance(ids, list) else ids

    async def write(self, model: str, ids: list[int], values: dict) -> bool:
        return await self.call(model, "write", ids=ids, vals=values)

    async def unlink(self, model: str, ids: list[int]) -> bool:
        return await self.call(model, "unlink", ids=ids)


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}: {resp.text[:500]}"
    # Odoo devuelve {"name": "...", "message": "...", "debug": "<traceback>"}; el traceback no aporta al modelo.
    message = data.get("message") or data.get("name") or str(data)[:500]
    if resp.status_code == 401:
        message += " (revisa ODOO_API_KEY y ODOO_DB)"
    return f"Odoo {resp.status_code}: {message}"
