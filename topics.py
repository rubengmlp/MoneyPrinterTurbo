#!/usr/bin/env python3
"""
Descubrimiento de temas virales para la-impresora (fork de MoneyPrinterTurbo).

Pipeline: tendencias de YouTube + autocompletado de Google (+ Google Trends y
TikTok Creative Center opcionales) -> puntuación -> ``tasks.json`` listo para
`uv run python cli.py --batch-file ./tasks.json`.

Uso típico:

    export YOUTUBE_API_KEY="tu-key-gratuita"
    uv run python topics.py --niche ai-tools --regions US,GB --days 14 \
        --limit 20 --out tasks.json

    # Expansión de ideas sin API key (autocompletado de Google):
    uv run python topics.py --suggest "ai tools"

    # Añadir consultas en ascenso de Google Trends (necesita red + pytrends):
    uv run --with pytrends python topics.py --niche ai-tools --trends

API key gratuita de YouTube Data API v3:
https://console.cloud.google.com/apis/library/youtube.googleapis.com
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
GOOGLE_SUGGEST_URL = "https://suggestqueries.google.com/complete/search"
USER_AGENT = "la-impresora-topics/1.0 (+https://github.com/rubengmlp/MoneyPrinterTurbo)"

# Límites de YouTube Data API: search.list cuesta 100 unidades y videos.list 1.
# El cupo diario por defecto son 10.000 unidades, así que el número de keywords
# buscadas con search.list está limitado para no agotarlo en una sola ejecución.
SEARCH_COST_UNITS = 100
MAX_SEARCH_KEYWORDS = 8

# Presets de nicho. ``cpm_multiplier`` refleja el valor comercial del nicho
# (afiliación + CPM publicitario) según los datos recopilados en 2026.
NICHE_PRESETS: dict[str, dict[str, Any]] = {
    "ai-tools": {
        "label": "AI Tools & Productivity (recomendado, inglés/US)",
        "keywords": [
            "ai tools",
            "ai automation",
            "ai productivity",
            "chatgpt tips",
            "best ai apps",
            "ai workflow",
            "ai for work",
            "run local ai",
        ],
        "match_tokens": [
            "ai", "chatgpt", "gpt", "openai", "claude", "gemini", "llm",
            "automation", "copilot", "agent", "prompt", "machine learning",
        ],
        "cpm_multiplier": 1.6,
    },
    "business-cases": {
        "label": "Business breakdowns / how companies make money",
        "keywords": [
            "how company makes money",
            "business model explained",
            "startup case study",
            "brand success story",
            "revenue breakdown",
        ],
        "match_tokens": [
            "business", "company", "startup", "revenue", "profit", "brand",
            "billion", "million", "money", "marketing",
        ],
        "cpm_multiplier": 1.8,
    },
    "tech-news": {
        "label": "Tech & AI news",
        "keywords": [
            "ai news",
            "tech news",
            "new gadget",
            "software update explained",
            "ai model release",
        ],
        "match_tokens": [
            "ai", "tech", "gadget", "apple", "google", "microsoft", "iphone",
            "android", "chip", "nvidia", "software", "app", "openai",
        ],
        "cpm_multiplier": 1.4,
    },
    "science-education": {
        "label": "Science & education explainers",
        "keywords": [
            "how things work",
            "science explained",
            "psychology facts",
            "history explained",
            "space facts",
        ],
        "match_tokens": [
            "science", "space", "physics", "brain", "psychology", "history",
            "universe", "study", "how", "why", "explained",
        ],
        "cpm_multiplier": 1.2,
    },
}

# Rangos Unicode de escrituras que delatan contenido no dirigido al público
# anglosajón (spam de granjas de vídeos, principalmente). Los títulos con
# acentos latinos (español, francés, alemán) usan códigos < 0x0400 y sí pasan.
DISALLOWED_SCRIPT_RANGES = (
    (0x0400, 0x04FF),  # cirílico
    (0x0590, 0x05FF),  # hebreo
    (0x0600, 0x06FF),  # árabe
    (0x0900, 0x097F),  # devanagari
    (0x0E00, 0x0E7F),  # tailandés
    (0x3040, 0x30FF),  # hiragana/katakana
    (0x3400, 0x9FFF),  # CJK
    (0xAC00, 0xD7AF),  # hangul
)

# Palabras que delatan contenido no monetizable o ya saturado. El filtro es
# deliberadamente conservador: solo descarta señales claras.
BANNED_TITLE_PATTERNS = (
    r"\bgiveaway\b",
    r"\bcompilation\b",
    r"\breaction\b",
    r"\bmukbang\b",
)


@dataclass
class Candidate:
    """Un vídeo de YouTube (o consulta en tendencia) con su puntuación."""

    subject: str
    source: str  # "trending" | "search" | "trends"
    url: str = ""
    channel: str = ""
    views: int = 0
    likes: int = 0
    comments: int = 0
    duration_seconds: int = 0
    age_hours: float = 0.0
    published_at: str = ""
    score: float = 0.0
    keyword: str = ""
    language: str = ""

    @property
    def is_short(self) -> bool:
        return 0 < self.duration_seconds <= 62

    def as_row(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 2),
            "views": self.views,
            "vph": round(self.views / max(self.age_hours, 1.0), 1),
            "hours": round(self.age_hours, 1),
            "duration": self.duration_seconds,
            "short": self.is_short,
            "subject": self.subject,
            "channel": self.channel,
            "url": self.url,
        }


# ---------------------------------------------------------------------------
# Utilidades HTTP y de parseo
# ---------------------------------------------------------------------------


def http_json(url: str, timeout: int = 30) -> Any:
    """GET JSON con User-Agent propio; lanza RuntimeError con detalle legible."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            payload = json.loads(exc.read().decode("utf-8", errors="replace"))
            detail = payload.get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001 - el detalle es solo informativo
            detail = ""
        raise RuntimeError(f"HTTP {exc.code} en {url}: {detail or exc.reason}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"no se pudo conectar a {url}: {exc.reason}") from None


def parse_iso8601_duration(value: str) -> int:
    """Convierte una duración ISO-8601 de YouTube (PT1H2M3S) a segundos."""
    match = re.fullmatch(
        r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or ""
    )
    if not match:
        return 0
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_published_at(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return None


def clean_title(title: str) -> str:
    """Normaliza el título de un vídeo para usarlo como tema de guion.

    Solo se elimina el ruido de formato (hashtags, emojis, corchetes, sufijos de
    canal). El contenido y el enfoque no se copian: el LLM escribirá un guion
    propio a partir del tema.
    """
    text = str(title or "")
    # Sufijos habituales del tipo "TITLE | Channel Name" o "TITLE - Channel".
    text = re.split(r"\s[|\-–—]\s", text, maxsplit=1)[0]
    text = re.sub(r"#\w+", " ", text)
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", text)
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -–—:•|")
    if len(text) > 90:
        text = text[:87].rstrip() + "..."
    return text


def is_monetizable_subject(subject: str) -> bool:
    """Descarta temas con señales claras de contenido no monetizable o spam."""
    lowered = subject.lower()
    return not any(re.search(pattern, lowered) for pattern in BANNED_TITLE_PATTERNS)


def has_disallowed_script(text: str) -> bool:
    """True si el título usa una escritura ajena al mercado objetivo."""
    return any(
        any(start <= ord(char) <= end for start, end in DISALLOWED_SCRIPT_RANGES)
        for char in text or ""
    )


def title_matches_niche(title: str, tokens: Iterable[str]) -> bool:
    """True si el título menciona algún token del nicho (límite de palabra).

    Se usa para filtrar las tendencias generales de YouTube, que están dominadas
    por música y gaming y de otro modo aplastarían a los resultados del nicho.
    """
    lowered = str(title or "").lower()
    for token in tokens:
        token = token.strip().lower()
        if not token:
            continue
        pattern = r"\b" + re.escape(token) + r"\b"
        if re.search(pattern, lowered):
            return True
    return False


def resolve_match_tokens(preset: dict[str, Any], keywords: Iterable[str]) -> list[str]:
    """Tokens de relevancia del preset; si el usuario pasa keywords propias, se
    derivan de ellas para que el filtro siga funcionando."""
    tokens = preset.get("match_tokens")
    if tokens:
        return list(tokens)
    derived = []
    for keyword in keywords:
        for word in re.split(r"[^a-zA-Z0-9]+", str(keyword).lower()):
            if len(word) >= 2:
                derived.append(word)
    return list(dict.fromkeys(derived))


def is_usable_candidate(candidate: "Candidate") -> bool:
    """Filtros de calidad mínimos antes de puntuar un candidato."""
    if not candidate.subject or not is_monetizable_subject(candidate.subject):
        return False
    if has_disallowed_script(candidate.subject):
        return False
    # defaultLanguage/defaultAudioLanguage solo existe en parte de los vídeos;
    # cuando está y no es inglés, se descarta (spam multilingüe).
    if candidate.language and not candidate.language.lower().startswith("en"):
        return False
    # Los clips de menos de 15 s no dan material útil para un vídeo de 30-60 s.
    if 0 < candidate.duration_seconds < 15:
        return False
    return True


ENV_FILE_NAMES = (".env.topics", ".env")


def load_env_file(directory: str | None = None) -> list[str]:
    """Carga variables desde .env.topics / .env si no están ya en el entorno.

    Evita tener que exportar la API key en cada terminal sin escribirla en el
    repositorio: los archivos .env* están en .gitignore. Nunca imprime valores;
    devuelve la lista de archivos usados para poder informar al usuario.
    """
    if os.environ.get("YOUTUBE_API_KEY"):
        return []
    base = directory or os.path.dirname(os.path.abspath(__file__))
    for name in ENV_FILE_NAMES:
        path = os.path.join(base, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip("'\"")
                    if key and value:
                        os.environ.setdefault(key, value)
        except OSError:
            continue
        if os.environ.get("YOUTUBE_API_KEY"):
            return [name]
    return []


# ---------------------------------------------------------------------------
# Fuentes: YouTube, autocompletado de Google, Google Trends
# ---------------------------------------------------------------------------


def youtube_get(api_key: str, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    query = urllib.parse.urlencode({**params, "key": api_key})
    return http_json(f"{YOUTUBE_API_BASE}/{endpoint}?{query}")


def fetch_trending(api_key: str, region: str, max_results: int = 50) -> list[dict]:
    """Vídeos en tendencia de una región (videos.list chart=mostPopular, 1 unidad)."""
    payload = youtube_get(
        api_key,
        "videos",
        {
            "part": "snippet,statistics,contentDetails",
            "chart": "mostPopular",
            "regionCode": region,
            "maxResults": min(max(max_results, 1), 50),
        },
    )
    return payload.get("items", [])


def search_recent(
    api_key: str, keyword: str, region: str, days: int, max_results: int = 25
) -> list[dict]:
    """Vídeos más vistos para una keyword en los últimos N días (100 unidades)."""
    published_after = (
        datetime.now(timezone.utc) - timedelta(days=max(days, 1))
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = youtube_get(
        api_key,
        "search",
        {
            "part": "id",
            "q": keyword,
            "type": "video",
            "order": "viewCount",
            "videoDuration": "short",
            "relevanceLanguage": "en",
            "regionCode": region,
            "publishedAfter": published_after,
            "maxResults": min(max(max_results, 1), 50),
        },
    )
    video_ids = [
        item.get("id", {}).get("videoId", "")
        for item in payload.get("items", [])
        if item.get("id", {}).get("videoId")
    ]
    if not video_ids:
        return []
    details = youtube_get(
        api_key,
        "videos",
        {
            "part": "snippet,statistics,contentDetails",
            "id": ",".join(video_ids[:50]),
            "maxResults": 50,
        },
    )
    return details.get("items", [])


def fetch_suggestions(seed: str, language: str = "en") -> list[str]:
    """Expansiones de ideas vía autocompletado de Google (gratis, sin API key)."""
    query = urllib.parse.urlencode(
        {"client": "firefox", "hl": language, "q": seed}
    )
    try:
        payload = http_json(f"{GOOGLE_SUGGEST_URL}?{query}")
    except RuntimeError:
        return []
    if isinstance(payload, list) and len(payload) > 1 and isinstance(payload[1], list):
        return [str(item).strip() for item in payload[1] if str(item).strip()]
    return []


def fetch_trends_rising(seeds: Iterable[str], geo: str = "US") -> list[str]:
    """Consultas en ascenso de Google Trends (requiere pytrends).

    pytrends no es dependencia del proyecto; ejecuta el script con:
        uv run --with pytrends python topics.py ... --trends
    """
    try:
        from pytrends.request import TrendReq  # type: ignore[import-not-found]
    except ImportError:
        print(
            "aviso: Google Trends requiere pytrends; ejecuta "
            "`uv run --with pytrends python topics.py ... --trends`",
            file=sys.stderr,
        )
        return []

    rising: list[str] = []
    try:
        client = TrendReq(hl="en-US", tz=0)
        for seed in list(seeds)[:5]:
            try:
                client.build_payload([seed], timeframe="now 7-d", geo=geo)
                related = client.related_queries().get(seed, {}).get("rising")
                if related is not None and not related.empty:
                    rising.extend(
                        str(value).strip()
                        for value in related["query"].head(5).tolist()
                        if str(value).strip()
                    )
            except Exception as exc:  # noqa: BLE001 - fuente opcional y frágil
                print(f"aviso: Google Trends falló para {seed!r}: {exc}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"aviso: Google Trends no disponible: {exc}", file=sys.stderr)
    # Deduplicar conservando el orden.
    return list(dict.fromkeys(rising))


# ---------------------------------------------------------------------------
# Puntuación
# ---------------------------------------------------------------------------


def score_candidate(
    candidate: Candidate, cpm_multiplier: float, days_window: int
) -> float:
    """Puntúa un candidato por velocidad, engagement y frescura.

    - ``velocity``: vistas por hora (se normaliza con log10 para que un viral
      desmesurado no aplaste al resto).
    - ``engagement``: likes + comentarios ponderados sobre vistas; actúa como
      desempate acotado (factor 1.0-1.5) para que un vídeo con pocas vistas y
      mucho engagement no supere a uno realmente masivo.
    - ``freshness``: decae linealmente con la ventana temporal configurada.
    - ``shorts_bonus``: prioriza el formato corto, que es el que produce este
      fork.
    """
    age_hours = max(candidate.age_hours, 1.0)
    velocity = candidate.views / age_hours
    views = max(candidate.views, 1)
    engagement = (candidate.likes + 2 * candidate.comments) / views
    engagement_factor = 1.0 + min(engagement * 5.0, 0.5)
    freshness = max(0.2, 1.0 - age_hours / (max(days_window, 1) * 24))
    shorts_bonus = 1.15 if candidate.is_short else 1.0
    base = math.log10(velocity + 1.0)
    return base * engagement_factor * freshness * shorts_bonus * cpm_multiplier


def candidate_from_video(item: dict, source: str, keyword: str = "") -> Candidate:
    snippet = item.get("snippet", {}) or {}
    statistics = item.get("statistics", {}) or {}
    content = item.get("contentDetails", {}) or {}
    published = parse_published_at(snippet.get("publishedAt", ""))
    age_hours = (
        (datetime.now(timezone.utc) - published).total_seconds() / 3600
        if published
        else 24 * 30
    )
    video_id = item.get("id", "")
    if isinstance(video_id, dict):
        video_id = video_id.get("videoId", "")
    return Candidate(
        subject=clean_title(snippet.get("title", "")),
        source=source,
        url=f"https://www.youtube.com/watch?v={video_id}" if video_id else "",
        channel=snippet.get("channelTitle", ""),
        views=int(statistics.get("viewCount") or 0),
        likes=int(statistics.get("likeCount") or 0),
        comments=int(statistics.get("commentCount") or 0),
        duration_seconds=parse_iso8601_duration(content.get("duration", "")),
        age_hours=age_hours,
        published_at=snippet.get("publishedAt", ""),
        keyword=keyword,
        language=str(
            snippet.get("defaultAudioLanguage")
            or snippet.get("defaultLanguage")
            or ""
        ),
    )


def rank_candidates(
    candidates: Iterable[Candidate],
    cpm_multiplier: float,
    days_window: int,
    min_views: int = 0,
    min_vph: float = 0.0,
) -> list[Candidate]:
    """Puntúa, aplica umbrales de señal, deduplica y ordena de mayor a menor."""
    for candidate in candidates:
        # Normalizar aquí garantiza que la deduplicación funcione también cuando
        # los candidatos no vienen de la API (tests, datos importados, etc.).
        candidate.subject = clean_title(candidate.subject)
        candidate.score = score_candidate(candidate, cpm_multiplier, days_window)

    best_by_subject: dict[str, Candidate] = {}
    for candidate in candidates:
        if not is_usable_candidate(candidate):
            continue
        # Google Trends no trae métricas de vídeo: sus umbrales no aplican.
        if candidate.source != "trends":
            if candidate.views < max(min_views, 0):
                continue
            vph = candidate.views / max(candidate.age_hours, 1.0)
            if vph < max(min_vph, 0.0):
                continue
        key = re.sub(r"[^a-z0-9]+", " ", candidate.subject.lower()).strip()
        key = " ".join(key.split()[:6])  # dedupe por primeras palabras significativas
        current = best_by_subject.get(key)
        if current is None or candidate.score > current.score:
            best_by_subject[key] = candidate

    return sorted(best_by_subject.values(), key=lambda item: item.score, reverse=True)


# ---------------------------------------------------------------------------
# Salida
# ---------------------------------------------------------------------------


def build_manifest_tasks(
    candidates: Iterable[Candidate],
    language: str,
    aspect: str,
    paragraph_number: int,
    max_tasks: int = 100,
    voice_name: str = "",
) -> list[dict[str, Any]]:
    """Genera objetos válidos para ``cli.py --batch-file``.

    El límite de 100 tareas y 1 MiB por manifiesto lo impone el propio CLI.
    ``voice_name`` es opcional: si se indica, cada tarea lo fija (útil cuando el
    idioma del guion no coincide con la voz guardada en config.toml).
    """
    tasks = []
    for candidate in list(candidates)[:max_tasks]:
        task = {
            "video_subject": candidate.subject,
            "video_language": language,
            "video_aspect": aspect,
            "paragraph_number": paragraph_number,
        }
        if voice_name:
            task["voice_name"] = voice_name
        tasks.append(task)
    return tasks


def print_ranking(candidates: list[Candidate], top: int) -> None:
    if not candidates:
        return
    header = (
        f"{'#':>3}  {'score':>7}  {'vistas':>10}  {'vph':>8}  {'h':>6}  "
        f"{'s':>4}  {'origen':<8}  tema"
    )
    print(header)
    print("-" * len(header))
    for index, candidate in enumerate(candidates[:top], start=1):
        vph = candidate.views / max(candidate.age_hours, 1.0)
        print(
            f"{index:>3}  {candidate.score:>7.2f}  {candidate.views:>10,}  "
            f"{vph:>8.0f}  {candidate.age_hours:>6.0f}  "
            f"{'si' if candidate.is_short else 'no':>4}  "
            f"{candidate.source:<8}  {candidate.subject}"
        )


def write_manifest(path: str, tasks: list[dict[str, Any]]) -> None:
    payload = json.dumps(tasks, ensure_ascii=False, indent=2)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(payload + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Encuentra temas virales (YouTube + autocompletado + Google Trends) "
            "y genera tasks.json para cli.py --batch-file."
        )
    )
    parser.add_argument("--niche", default="ai-tools", choices=sorted(NICHE_PRESETS))
    parser.add_argument("--list-niches", action="store_true", help="lista los presets")
    parser.add_argument(
        "--suggest",
        metavar="SEED",
        help="solo expansiones de ideas por autocompletado (no requiere API key)",
    )
    parser.add_argument(
        "--keywords",
        help="keywords separadas por comas; sustituyen a las del preset",
    )
    parser.add_argument(
        "--regions",
        default="US",
        help="regiones YouTube separadas por comas (US,GB,CA,AU,ES,MX)",
    )
    parser.add_argument("--days", type=int, default=14, help="ventana temporal")
    parser.add_argument("--limit", type=int, default=20, help="temas en la salida")
    parser.add_argument(
        "--max-search-keywords",
        type=int,
        default=MAX_SEARCH_KEYWORDS,
        help=f"keywords buscadas con search.list (100 unidades cada una; máx. {MAX_SEARCH_KEYWORDS})",
    )
    parser.add_argument(
        "--min-views",
        type=int,
        default=10_000,
        help="vistas mínimas del candidato (0 desactiva; por defecto 10000)",
    )
    parser.add_argument(
        "--min-vph",
        type=float,
        default=200.0,
        help="velocidad mínima en vistas/hora (0 desactiva; por defecto 200)",
    )
    parser.add_argument(
        "--trending",
        action="store_true",
        help="incluir tendencias generales de YouTube filtradas por tokens del nicho "
        "(desactivadas por defecto: suelen ser música y gaming)",
    )
    parser.add_argument("--trends", action="store_true", help="añadir Google Trends")
    parser.add_argument("--language", default="en-US", help="idioma del guion")
    parser.add_argument(
        "--voice",
        default="",
        help="voz TTS fijada en cada tarea (p. ej. en-US-AndrewNeural-Male); "
        "vacío = usa la de config.toml",
    )
    parser.add_argument("--aspect", default="9:16", help="formato de salida")
    parser.add_argument("--paragraphs", type=int, default=1, help="párrafos por guion")
    parser.add_argument("--out", default="tasks.json", help="manifiesto de salida")
    parser.add_argument(
        "--no-manifest", action="store_true", help="solo mostrar la tabla"
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("YOUTUBE_API_KEY", ""),
        help="YouTube Data API v3 key (o variable YOUTUBE_API_KEY)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    loaded_env_files = load_env_file()
    args = parse_args(argv)
    if loaded_env_files:
        print(
            f"YOUTUBE_API_KEY cargada desde {', '.join(loaded_env_files)}",
            file=sys.stderr,
        )

    if args.list_niches:
        for niche_id, preset in NICHE_PRESETS.items():
            print(f"{niche_id}: {preset['label']}")
            print(f"  keywords: {', '.join(preset['keywords'])}")
        return 0

    if args.suggest:
        suggestions = fetch_suggestions(args.suggest, language=args.language[:2])
        if not suggestions:
            print("sin sugerencias (¿sin conexión?)", file=sys.stderr)
            return 1
        for item in suggestions:
            print(item)
        return 0

    preset = NICHE_PRESETS[args.niche]
    keywords = (
        [item.strip() for item in args.keywords.split(",") if item.strip()]
        if args.keywords
        else list(preset["keywords"])
    )
    keywords = keywords[: max(0, min(args.max_search_keywords, MAX_SEARCH_KEYWORDS))]
    regions = [item.strip().upper() for item in args.regions.split(",") if item.strip()]
    cpm_multiplier = float(preset["cpm_multiplier"])
    match_tokens = resolve_match_tokens(preset, keywords)

    candidates: list[Candidate] = []

    if args.trends:
        for subject in fetch_trends_rising(preset["keywords"], geo=regions[0]):
            candidates.append(
                Candidate(
                    subject=clean_title(subject),
                    source="trends",
                    score=0.0,
                    age_hours=max(args.days, 1) * 12,
                    keyword=subject,
                )
            )

    if not args.api_key:
        print(
            "aviso: falta YOUTUBE_API_KEY; solo se usarán Google Trends / "
            "autocompletado. Consigue una key gratuita en "
            "https://console.cloud.google.com/apis/library/youtube.googleapis.com",
            file=sys.stderr,
        )
    else:
        # Las tendencias generales están dominadas por música y gaming: solo se
        # incorporan con --trending y si el título menciona el nicho. Los
        # resultados de search.list ya son específicos del nicho por keyword.
        if args.trending:
            for region in regions:
                try:
                    kept = 0
                    for item in fetch_trending(args.api_key, region):
                        title = clean_title(
                            (item.get("snippet", {}) or {}).get("title", "")
                        )
                        if title_matches_niche(title, match_tokens):
                            candidates.append(candidate_from_video(item, "trending"))
                            kept += 1
                    print(
                        f"tendencias {region}: {kept} vídeos relevantes al nicho",
                        file=sys.stderr,
                    )
                except RuntimeError as exc:
                    print(
                        f"aviso: tendencias de {region} fallaron: {exc}",
                        file=sys.stderr,
                    )
        for keyword in keywords:
            for region in regions[:1]:  # search.list es caro: solo región principal
                try:
                    for item in search_recent(
                        args.api_key, keyword, region, args.days
                    ):
                        # El filtro de nicho se aplica al título YA limpiado: si
                        # se aplica al crudo, "monday CRM | AI-powered work..."
                        # pasa por el "AI" del sufijo que clean_title descarta.
                        title = clean_title(
                            (item.get("snippet", {}) or {}).get("title", "")
                        )
                        if not title_matches_niche(title, match_tokens):
                            continue
                        candidates.append(
                            candidate_from_video(item, "search", keyword=keyword)
                        )
                except RuntimeError as exc:
                    print(
                        f"aviso: búsqueda {keyword!r} falló: {exc}", file=sys.stderr
                    )

    if not candidates:
        print("sin candidatos: revisa la API key y la conexión", file=sys.stderr)
        return 1

    ranked = rank_candidates(
        candidates,
        cpm_multiplier,
        args.days,
        min_views=args.min_views,
        min_vph=args.min_vph,
    )
    print_ranking(ranked, args.limit)

    if not args.no_manifest:
        tasks = build_manifest_tasks(
            ranked,
            language=args.language,
            aspect=args.aspect,
            paragraph_number=max(1, min(args.paragraphs, 10)),
            max_tasks=min(args.limit, 100),
            voice_name=args.voice,
        )
        write_manifest(args.out, tasks)
        print(
            f"\n{len(tasks)} temas escritos en {args.out}\n"
            f"siguiente paso: uv run python cli.py --batch-file ./{args.out} --stop-at script"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
