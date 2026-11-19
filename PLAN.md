# wallmpop — plan

Cazador de chollos en Wallapop. Uso personal (no nubuza). Corre en Docker en esta máquina.
Un bot de Telegram dedicado gestiona las alertas y recibe los anuncios que encajan.

## Flujo

```
/alta "NVMe 512G <50€ gen3+ buena salud, Madrid"
   └─ Sonnet (1 sola vez) → config de alerta (YAML/JSON) → la enseñas → ✅ confirmar
                                     │
scheduler (cada N min por alerta) ───┘
   fetch Wallapop (API interna search) → dedup SQLite → prefiltro (reglas, 0 tokens)
   → Haiku en lote (solo texto) → match/maybe/reject → Telegram
   └─ botón "🔍 a fondo" (opcional) → Sonnet, solo texto, para ese anuncio
```

## Config de alerta (la deriva Sonnet al darla de alta)

- `name`, `query` (keywords para la búsqueda), `category_id` si aplica
- `price_min/max`, `lat/lon/radius_km` (default Madrid), `shipping_ok`
- `include_regex` / `exclude_regex` (p.ej. `512\s?gb?`, `roto|busco|cambio|averiado`)
- `hard_rules`: lo que el código puede comprobar sin LLM
- `rubric`: criterios cortos para Haiku (≤ 10 líneas): qué es match, maybe y reject,
  conocimiento útil (lista de modelos Gen3+, qué indica "buena salud")
- `interval_min`, `max_llm_per_day`

Se puede retocar con `/editar <alerta> <texto libre>` (Sonnet reescribe la config, enseña el diff).

## Economía de tokens (prioridad)

1. **Sonnet solo en alta/edición** de alertas y en el botón "a fondo". Nunca en el bucle.
2. **Prefiltro determinista** primero: precio, distancia, regex, vendedores/ids vistos. Objetivo: <10% llega al LLM.
3. **Dedup por id**: cada anuncio se juzga una vez. Si baja de precio y su veredicto era match/maybe → re-aviso sin LLM.
4. **Haiku en lote**: 10–20 anuncios por llamada, título + descripción truncada (~400 car.), salida JSON compacta `[{id,v,s,why}]`.
5. **Sin fotos**: ni en el juicio ni en "a fondo" (decidido; solo si fuera imprescindible).
6. **CLI ligero**: `claude -p --model haiku --system-prompt <rubric> --output-format json`
   sin herramientas ni MCP, para no pagar el system prompt por defecto de Claude Code.
7. **Topes**: `max_llm_per_day` por alerta y global; `/estado` enseña llamadas/tokens del día.

## Bot de Telegram (dedicado, solo tu chat_id)

- **Control de acceso**: `ALLOWED_USER_IDS`; cualquier otro usuario se ignora (y se registra en el log).
  Sin ids configurados, el bot solo responde diciendo tu id.
- **Aviso de desarrollador**: si la API de Wallapop falla 3 veces seguidas, cambia de formato
  (aviso inmediato) o devuelve 0 anuncios en todas las búsquedas mucho rato, llega
  "⚠️ Aviso de desarrollador" al chat, una sola vez, y "✅" cuando se recupera. `/estado` lo muestra.
- **Frecuencia en el alta**: el borrador trae botones 5/15/30/60 min; `/frecuencia <n> <min>` la cambia luego.
- Reposts (mismo vendedor + mismo título, id nuevo) heredan el veredicto sin LLM.

- `/alta <texto>` · `/lista` · `/editar <n> <texto>` · `/pausar <n>` · `/reanudar <n>` · `/baja <n>` · `/estado`
- Aviso: foto, título, precio, distancia, veredicto + motivo, enlace.
  Botones: 👍 / 👎 / 🔍 a fondo / 🔕 silenciar vendedor.
- 👍/👎 se guardan; `/afinar <n>` (manual) pasa ejemplos a Sonnet para ajustar regex y rubric.

## Técnico

- Python 3.12 (venv/pip). `python-telegram-bot` (polling) + scheduler asyncio en el mismo proceso.
- SQLite en volumen: `alerts`, `listings` (id, alert, precio, historial, veredicto), `feedback`, `usage`.
- Fuente Wallapop: `api.wallapop.com/api/v3/search` + ficha del item. Headers de navegador,
  jitter, backoff. Interfaz `Source` para añadir Milanuncios/Vinted/eBay en fase 2.
- Claude CLI dentro del contenedor con la suscripción: `CLAUDE_CODE_OAUTH_TOKEN`
  (generado con `claude setup-token`) en `.env`.
- `docker compose up -d`, `restart: unless-stopped`.

## Layout

```
wallmpop/
  wallmpop/ main.py bot.py scheduler.py db.py models.py
            sources/wallapop.py  prefilter.py  llm.py
            prompts/derive_alert.md  prompts/judge.md  prompts/deep.md
  tests/  Dockerfile  docker-compose.yml  .env.example  pyproject.toml
```

## Fases

1. Fuente Wallapop + prefiltro + CLI de prueba (sin LLM) para validar que la API responde.
2. Haiku en lote + avisos Telegram + `/alta` con Sonnet.
3. Botones, feedback, `/afinar`, bajadas de precio, "a fondo" con fotos.
4. Otras fuentes.

## Decidido

- Frecuencia por defecto 15 min (mínimo 5), elegible en el alta.
- "match" con sonido; "maybe" al momento pero silencioso.
- Suscripción (CLI) en vez de API key.
- Sin uv en el contenedor de desarrollo: venv + pip (`run.sh`); Docker para el host.
