# la-impresora — fork de MoneyPrinterTurbo en español

Este repositorio es un fork de [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo)
en `rubengmlp/MoneyPrinterTurbo`, adaptado para **generar vídeos cortos en español
por defecto** y para funcionar con un stack gratuito basado en suscripciones:

| Capa | Servicio | Coste |
| --- | --- | --- |
| Guion | **Antigravity CLI (`agy`)** con la suscripción Google AI (equivalente al truco de `claude_code` con Claude) | Incluido en la suscripción |
| Voz | **Edge TTS** (Azure TTS V1), `es-ES-AlvaroNeural-Male` | Gratis, sin API key |
| Subtítulos | Modo `edge` (timestamps del TTS) | Gratis, sin GPU |
| Material | **Pexels** (stock HD) | Gratis, solo API key |
| Música | `resource/songs` | Gratis |
| Material IA (opcional, después) | **Metaso MiniMax H3** (`video_source = "metaso_minimax"`) | De pago (~¥0.09/s en 768P, ~¥0.15/s en 2K) |

> Nota: desde el 18/06/2026 **Gemini CLI ya no sirve suscripciones Google AI Pro/Ultra**;
> el reemplazo oficial es **Antigravity CLI**. La suscripción tampoco cubre llamadas
> directas a la API de Gemini: eso requiere una API key con facturación aparte.

## Cambios del fork

- **Español por defecto**
  - `config.example.toml` fija `language = "es"`, `video_language = "es-ES"`,
    `voice_name = "es-ES-AlvaroNeural-Male"`, `font_name = "BeVietnamPro-Bold.ttf"`
    y un `video_script_prompt` en español.
  - La WebUI cae en español si no hay preferencia guardada ni locale de navegador.
  - `VideoParams` / `VideoScriptParams` usan `video_language = "es-ES"` por defecto.
  - `cli.py` usa la voz española por defecto.
- **Nuevo provider `antigravity`** (`adapter = "antigravity_cli"`)
  - Invoca `agy --print <prompt> --output-format json` y consume `response`.
  - Reutiliza la sesión OAuth cacheada por `agy`; no necesita API key.
  - Elimina del entorno `GEMINI_API_KEY`, `GOOGLE_API_KEY`,
    `GOOGLE_GENAI_API_KEY` y `GOOGLE_APPLICATION_CREDENTIALS` para que no se
    facture por API sin querer (conserva `GOOGLE_CLOUD_PROJECT`).
  - Es el provider por defecto (`DEFAULT_LLM_PROVIDER_ID = "antigravity"`).
- La fuente por defecto de subtítulos pasa a `BeVietnamPro-Bold.ttf` (cobertura
  completa de acentos en español).

## Puesta en marcha

Requisitos: macOS/Linux (o Windows), Python 3.11 gestionado por
[`uv`](https://docs.astral.sh/uv/) y `ffmpeg` (el proyecto puede autodescargarlo).

```bash
cd la-impresora
uv sync --frozen                    # crea .venv con Python 3.11 y dependencias

# 1) Instalar el CLI de Antigravity (deja el binario en ~/.local/bin/agy)
curl -fsSL https://antigravity.google/cli/install.sh | bash

# 2) Iniciar sesión UNA vez en una terminal normal (abre el navegador)
agy

# 3) Configuración: copiar el ejemplo la primera vez
cp config.example.toml config.toml
```

La clave de Pexels (gratis) se pega en la WebUI (Ajustes básicos) o directamente:

```toml
# config.toml
[app]
pexels_api_keys = ["tu-key-de-pexels"]
```

## Uso

```bash
# CLI, vídeo completo
uv run python cli.py --video-subject "Cómo la IA cambia el día a día"

# CLI, solo guion (rápido, útil para probar la conexión con agy)
uv run python cli.py --video-subject "..." --stop-at script

# WebUI en español
sh webui.sh

# API + documentación
uv run python main.py     # http://127.0.0.1:8080/docs
```

Comprobar el CLI de Google y sus modelos disponibles:

```bash
agy --version
agy models                # p. ej. gemini-3.8-flash-high / gemini-3.8-flash-medium
```

Para fijar un modelo concreto, en `config.toml`:

```toml
[app]
llm_provider = "antigravity"
antigravity_model_name = "gemini-3.8-flash-high"   # vacío = modelo por defecto del CLI
antigravity_timeout = 300                          # segundos
antigravity_cli_path = ""                          # vacío = PATH o ~/.local/bin/agy
```

## Cambiar de proveedor

- **Volver a la API de Gemini**: `llm_provider = "gemini"` y
  `gemini_model_name = "gemini-3.8-flash"` con una API key de
  <https://aistudio.google.com/app/apikey>.
- **Claude con suscripción**: `llm_provider = "claude_code"` (requiere el CLI
  `claude` instalado y logueado).
- **Material generado con IA**: cuando tengas la clave `mk-*` de Metaso, pon
  `video_source = "metaso_minimax"` y `metaso_minimax_api_key = "mk-..."`.
  Cada generación pide confirmación de gasto en la WebUI.

## Pruebas

```bash
uv run pytest test/services/test_llm.py test/services/test_config.py \
  test/services/test_webui_i18n.py test/services/test_mpt_agent_skill.py -q
```

## Publicar los cambios en el fork

```bash
git push -u origin espanol
```

## Descubrimiento de temas virales (`topics.py`)

Herramienta para encontrar **temas ya validados por el mercado** (método
outlier) y generar un manifiesto para producción en lote. No copia contenidos:
extrae el **tema y el ángulo** de vídeos que están funcionando y el LLM escribe
un guion propio.

```bash
# 1) Sin API key: expandir ideas con el autocompletado de Google
uv run python topics.py --suggest "ai tools"

# 2) Con la API key gratuita de YouTube Data API v3
export YOUTUBE_API_KEY="tu-key"
uv run python topics.py --niche ai-tools --regions US,GB --days 14 \
    --limit 20 --voice en-US-AndrewNeural-Male --out tasks.json

# 3) Generar los vídeos del manifiesto
uv run python cli.py --batch-file ./tasks.json --stop-at script

# 4) Google Trends en ascenso (opcional; pytrends se instala al vuelo)
uv run --with pytrends python topics.py --niche ai-tools --trends --out tasks.json
```

Qué hace:

- **Búsqueda por keyword** (`search.list`, 100 unidades de cupo): los vídeos
  más vistos en los últimos N días para cada keyword del nicho. Se descartan
  títulos en escrituras no latinas (spam de granjas) y clips de menos de 15 s.
- **Tendencias generales** (`--trending`, desactivadas por defecto): música y
  gaming dominan el ranking; si se activan, solo se conservan los vídeos cuyo
  título menciona el nicho.
- **Autocompletado de Google** (gratis, sin key): expande ideas semilla.
- **Google Trends** (`--trends`): consultas en ascenso de los últimos 7 días.
- **Scoring**: `log10(vistas/hora) × engagement × frescura × bonus Shorts ×
  multiplicador del nicho`; deduplica temas parecidos y descarta señales de
  contenido no monetizable.
- **Salida**: `tasks.json` listo para `cli.py --batch-file` (límite del CLI:
  100 tareas y 1 MiB).

Nichos predefinidos: `ai-tools` (recomendado), `business-cases`, `tech-news`,
`science-education`. Lista con `--list-niches`; puedes pasar tus propias
keywords con `--keywords "a,b,c"`.

> Nota de monetización: YouTube desmonetiza el contenido genérico/repetitivo de
> plantilla y la IA haciéndose pasar por experto en salud, legal, finanzas o
> política. Usa la herramienta para inspiración de temas, añade siempre ángulo
> propio y evita esos verticales con avatares de IA.
