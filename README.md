# Odoo MCP — conectar Claude con Odoo

Servidor [MCP](https://modelcontextprotocol.io) que le da a Claude acceso a tu Odoo
(`https://weddingready.odoo.com`) usando la **API JSON-2** de Odoo 19, la que reemplaza a
XML-RPC/JSON-RPC (que Odoo va a retirar).

```
Claude  ⇄  odoo-mcp (este proyecto)  ⇄  https://weddingready.odoo.com/json/2/...
```

Funciona de dos formas:

- **Local** (Claude Desktop / Claude Code): corre en tu computadora con tu API key en la config.
- **Remoto** (claude.ai web y móvil): publicado en internet, cada quien se conecta con su API key.

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

### claude.ai (web, móvil y Desktop) — conector remoto

Para claude.ai el servidor tiene que estar publicado en internet (HTTPS). En ese modo **no
guarda ninguna API key**: al conectar, Claude te abre una página donde pegas tu API key de Odoo;
se valida contra Odoo y viaja cifrada dentro del token de Claude. Cada persona entra con sus
propios permisos de Odoo.

**1. Publicarlo en Render** (el repo ya trae `render.yaml` y `Dockerfile`):

1. Crea una cuenta en [render.com](https://render.com) y conecta tu GitHub.
2. **New → Blueprint** → elige el repo `weddingredy` (la rama donde esté este código).
3. Render crea el servicio `odoo-mcp` con `ODOO_URL`, `ODOO_DB` y un `MCP_SECRET_KEY` aleatorio.
   La URL pública la toma sola (`https://odoo-mcp-xxxx.onrender.com`).
4. Cuando termine, abre `https://TU-SERVICIO.onrender.com/health`: debe decir `ok`.

> El plan *free* de Render apaga el servicio tras 15 min sin uso y tarda ~1 min en despertar;
> claude.ai puede fallar mientras tanto. Para uso diario conviene el plan *Starter*.
> Cualquier otro host sirve (Railway, Fly.io, Cloud Run…) usando el `Dockerfile` y definiendo
> `PUBLIC_URL` a mano.

**2. Agregarlo en claude.ai:**

1. claude.ai → **Configuración → Conectores → Agregar conector personalizado**.
2. Nombre: `Odoo`. URL: `https://TU-SERVICIO.onrender.com/mcp`. Deja vacío lo de OAuth avanzado.
3. **Conectar** → se abre la página "Conectar Odoo" → pega tu API key → **Conectar**.
4. En un chat, activa el conector Odoo y pregunta: *"¿con qué usuario estás conectado a Odoo?"*.

Variables del modo remoto:

| Variable | Valor |
|---|---|
| `MCP_TRANSPORT` | `http` (ya viene en el Dockerfile) |
| `PUBLIC_URL` | URL pública del servicio (en Render no hace falta) |
| `MCP_SECRET_KEY` | cadena aleatoria de 32+ caracteres; si la cambias, hay que reconectar |
| `MCP_ALLOWED_REDIRECT_HOSTS` | opcional, por defecto `claude.ai,claude.com,localhost,127.0.0.1` |
| `ODOO_READONLY` / `ODOO_ALLOW_DELETE` | igual que en local |

Para cortarle el acceso a Claude: borra la API key en Odoo (Seguridad de la cuenta).

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
