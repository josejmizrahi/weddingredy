# Odoo MCP — conectar Claude con Odoo

Servidor [MCP](https://modelcontextprotocol.io) que le da a Claude acceso a tu Odoo
(`https://weddingready.odoo.com`) usando la **API JSON-2** de Odoo 19, la que reemplaza a
XML-RPC/JSON-RPC (que Odoo va a retirar).

```
Claude  ⇄  odoo-mcp (este proyecto, corre en tu compu)  ⇄  https://weddingready.odoo.com/json/2/...
```

Con esto le puedes pedir a Claude cosas como:

- "¿Cuántas cotizaciones confirmadas tengo este mes?"
- "Busca el contacto de la novia García y muéstrame su teléfono y email"
- "Crea un contacto nuevo: Ana López, ana@correo.com"
- "Confirma la cotización S00042"

## Herramientas

| Herramienta | Qué hace |
|---|---|
| `odoo_whoami` | Usuario, base de datos y permisos con los que está conectado |
| `odoo_list_models` | Lista los modelos (tablas) de Odoo |
| `odoo_get_fields` | Campos de un modelo |
| `odoo_search_read` | Busca y lee registros de cualquier modelo |
| `odoo_count` | Cuenta registros |
| `odoo_create` | Crea un registro |
| `odoo_update` | Modifica registros |
| `odoo_delete` | Borra registros (desactivado por defecto) |
| `odoo_call_method` | Ejecuta acciones de negocio (`action_confirm`, `action_post`, …) |

## 1. Crear la API key en Odoo

1. Entra a Odoo → clic en tu avatar (arriba a la derecha) → **Mi perfil / Preferencias**.
2. Pestaña **Seguridad de la cuenta** → **Nueva clave API**.
3. Ponle un nombre (p. ej. `claude`) y copia la clave. **No la subas a git.**

> Claude va a actuar con los permisos de ese usuario. Si quieres limitarlo, crea un usuario
> aparte en Odoo con permisos reducidos y genera la clave desde ese usuario.
> Nota: en Odoo Online, el acceso a la API externa requiere el plan **Custom**.

## 2. Instalar

Requiere Python 3.10+ y [uv](https://docs.astral.sh/uv/) (o pip).

```bash
git clone https://github.com/josejmizrahi/weddingredy.git
cd weddingredy
uv sync          # o: python -m venv .venv && .venv/bin/pip install -e .
```

## 3. Conectarlo a Claude

Variables de entorno:

| Variable | Valor |
|---|---|
| `ODOO_URL` | `https://weddingready.odoo.com` |
| `ODOO_DB` | `weddingready` |
| `ODOO_API_KEY` | la clave del paso 1 |
| `ODOO_READONLY` | `true` para que Claude solo pueda leer (opcional) |
| `ODOO_ALLOW_DELETE` | `true` para permitir borrar registros (opcional, no recomendado) |

### Claude Desktop

Edita `claude_desktop_config.json` (Configuración → Desarrollador → Editar configuración):

```json
{
  "mcpServers": {
    "odoo": {
      "command": "uv",
      "args": ["--directory", "/RUTA/A/weddingredy", "run", "odoo-mcp"],
      "env": {
        "ODOO_URL": "https://weddingready.odoo.com",
        "ODOO_DB": "weddingready",
        "ODOO_API_KEY": "TU_API_KEY"
      }
    }
  }
}
```

Reinicia Claude Desktop y pregúntale: *"¿con qué usuario estás conectado a Odoo?"*.

### Claude Code

```bash
claude mcp add odoo \
  -e ODOO_URL=https://weddingready.odoo.com \
  -e ODOO_DB=weddingready \
  -e ODOO_API_KEY=TU_API_KEY \
  -- uv --directory /RUTA/A/weddingredy run odoo-mcp
```

### claude.ai (web / móvil)

claude.ai solo acepta conectores remotos (por HTTPS con autenticación). Este servidor corre
localmente por stdio; para usarlo desde la web habría que desplegarlo en un servidor y
agregarle OAuth. Es el siguiente paso posible.

## Probar sin Claude

```bash
ODOO_URL=https://weddingready.odoo.com ODOO_DB=weddingready ODOO_API_KEY=... \
  npx @modelcontextprotocol/inspector uv run odoo-mcp
```

## Desarrollo

```bash
uv sync --extra dev
uv run pytest
```
