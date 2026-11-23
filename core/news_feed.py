"""
Agregador de Notícias ao Vivo do Mundo dos Investimentos (FyNex Quant AI)
=========================================================================
Busca feeds RSS públicos de finanças, criptomoedas e câmbio (Forex) em paralelo,
normaliza datas/títulos, aplica classificação de categoria e sentimento heurístico,
e mantém cache curto em memória para resposta instantânea no dashboard.
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("QuantTradingBot")

NEWS_SOURCES: List[Dict[str, str]] = [
    {
        "name": "InfoMoney",
        "category": "macro",
        "url": "https://www.infomoney.com.br/mercados/feed/",
    },
    {
        "name": "InfoMoney Economia",
        "category": "forex",
        "url": "https://www.infomoney.com.br/economia/feed/",
    },
    {
        "name": "Cointelegraph BR",
        "category": "cripto",
        "url": "https://br.cointelegraph.com/rss",
    },
    {
        "name": "Investing.com BR",
        "category": "forex",
        "url": "https://br.investing.com/rss/news.rss",
    },
    {
        "name": "Money Times",
        "category": "macro",
        "url": "https://www.moneytimes.com.br/feed/",
    },
]

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")

_BULLISH_TERMS = (
    "alta", "sobe", "subida", "recorde", "lucro", "valorização", "corte de juros",
    "aprovado", "etf", "otimismo", "rally", "surge", "gains", "bullish", "jumps",
    "rises", "soars", "record high", "supera", "avança", "dispara",
)
_BEARISH_TERMS = (
    "queda", "cai", "desaba", "crise", "inflação", "alerta", "liquidação", "perda",
    "risco", "recessão", "crash", "drops", "falls", "plunge", "bearish", "slumps",
    "selloff", "tensão", "recua", "despenca", "despencou", "despencam", "deficit", "tarifa",
)

_BULLISH_PATTERNS = tuple(re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE) for k in _BULLISH_TERMS)
_BEARISH_PATTERNS = tuple(re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE) for k in _BEARISH_TERMS)
_HAWKISH_OVERRIDE_RE = re.compile(r"\b(?:alta dos juros|alta da infla[cç][aã]o|infla[cç][aã]o sobe)\b", re.IGNORECASE)
_CRYPTO_CAT_RE = re.compile(
    r"\b(?:bitcoin|btc|ethereum|eth|cripto|crypto|binance|solana|blockchain)\b", re.IGNORECASE
)
_FOREX_CAT_RE = re.compile(
    r"(?:eur/usd|gbp/usd|usd/jpy|\b(?:forex|d[oó]lar|euro|c[aâ]mbio|fed|banco central|juros|selic|ouro|xau)\b)",
    re.IGNORECASE,
)

_NEWS_CACHE_LOCK = threading.Lock()
_NEWS_CACHE: Tuple[float, str, List[Dict[str, Any]]] = (0.0, "", [])


def clear_news_cache() -> None:
    global _NEWS_CACHE
    with _NEWS_CACHE_LOCK:
        _NEWS_CACHE = (0.0, "", [])


def _clean_text(raw: Optional[str], max_len: int = 240) -> str:
    if not raw:
        return ""
    # Desescapa entidades HTML antes de remover tags (evita vazamento de HTML duplamente escapado)
    unescaped = html.unescape(html.unescape(raw))
    text = _TAG_RE.sub(" ", unescaped)
    text = _SPACE_RE.sub(" ", text).strip()
    if len(text) > max_len:
        return text[: max_len - 1].rstrip() + "…"
    return text


def _classify_sentiment(title: str, summary: str) -> str:
    blob = f"{title} {summary}".lower()
    # Neutraliza frases hawkish ("alta dos juros") antes de contar "alta" como positivo
    hawkish_hits = len(_HAWKISH_OVERRIDE_RE.findall(blob))
    cleaned_blob = _HAWKISH_OVERRIDE_RE.sub(" ", blob)
    bull = sum(1 for rx in _BULLISH_PATTERNS if rx.search(cleaned_blob))
    bear = sum(1 for rx in _BEARISH_PATTERNS if rx.search(cleaned_blob)) + hawkish_hits
    if bull > bear:
        return "positivo"
    if bear > bull:
        return "negativo"
    return "neutro"


def _refine_category(default_cat: str, title: str, summary: str) -> str:
    blob = f"{title} {summary}".lower()
    if _CRYPTO_CAT_RE.search(blob):
        return "cripto"
    if _FOREX_CAT_RE.search(blob):
        return "forex"
    return default_cat


def _parse_pub_date(raw: Optional[str]) -> Tuple[int, str]:
    if not raw:
        return 0, "N/D"
    try:
        dt = parsedate_to_datetime(raw.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_utc = dt.astimezone(timezone.utc)
        return int(dt_utc.timestamp()), dt_utc.strftime("%d/%m %H:%M UTC")
    except Exception:
        try:
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt_utc = dt.astimezone(timezone.utc)
            return int(dt_utc.timestamp()), dt_utc.strftime("%d/%m %H:%M UTC")
        except Exception:
            return 0, "N/D"


def _safe_url(url: Optional[str]) -> str:
    val = (url or "").strip()
    if not val or any(ch.isspace() or ord(ch) < 32 for ch in val):
        return ""
    try:
        parsed = urllib.parse.urlparse(val)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return val
    except Exception:
        pass
    return ""


def parse_rss_xml(xml_bytes: bytes, source_name: str, default_category: str, max_items: int = 12) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not xml_bytes or len(xml_bytes) > 2_097_152:
        return items
    # Defesa contra entidades XML/DTD maliciosas
    head_upper = xml_bytes[:4096].upper()
    if b"<!ENTITY" in head_upper or b"<!DOCTYPE" in head_upper:
        return items
    root = ET.fromstring(xml_bytes)

    # Suporta RSS 2.0 (<item>) e Atom (<entry>)
    nodes = root.findall(".//item")
    is_atom = False
    if not nodes:
        nodes = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        is_atom = True

    for node in nodes[:max_items]:
        if not is_atom:
            title = _clean_text(node.findtext("title"), max_len=160)
            summary = _clean_text(node.findtext("description"), max_len=240)
            link = _safe_url(node.findtext("link"))
            pub_raw = node.findtext("pubDate")
        else:
            ns = "{http://www.w3.org/2005/Atom}"
            title = _clean_text(node.findtext(f"{ns}title"), max_len=160)
            summary = _clean_text(node.findtext(f"{ns}summary") or node.findtext(f"{ns}content"), max_len=240)
            link_el = node.find(f"{ns}link")
            link = _safe_url(link_el.attrib.get("href") if link_el is not None else "")
            pub_raw = node.findtext(f"{ns}updated") or node.findtext(f"{ns}published")

        if not title:
            continue

        ts, published_str = _parse_pub_date(pub_raw)
        category = _refine_category(default_category, title, summary)
        sentiment = _classify_sentiment(title, summary)
        items.append({
            "title": title,
            "summary": summary,
            "link": link,
            "source": source_name,
            "category": category,
            "sentiment": sentiment,
            "timestamp": ts,
            "published_at": published_str,
        })
    return items


def _fetch_single_feed(source: Dict[str, str], timeout: float = 4.5) -> List[Dict[str, Any]]:
    req = urllib.request.Request(
        source["url"],
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FyNexQuantAI/2.0",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(2_097_152)
    return parse_rss_xml(data, source["name"], source["category"], max_items=12)


def fetch_live_news(
    category: str = "all",
    query: str = "",
    limit: int = 30,
    ttl_seconds: float = 120.0,
) -> Dict[str, Any]:
    """
    Busca e agrega notícias ao vivo do mercado financeiro (Cripto, Forex e Macro).
    Usa cache curto (2 min) para carregamento instantâneo na interface.
    """
    global _NEWS_CACHE
    now = time.monotonic()
    with _NEWS_CACHE_LOCK:
        cached_ts, cached_updated_str, cached_items = _NEWS_CACHE
        has_valid_cache = bool(cached_items) and (now - cached_ts) < ttl_seconds

    stale = False
    if has_valid_cache:
        all_items = cached_items
        updated_at_str = cached_updated_str or datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    else:
        collected: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=len(NEWS_SOURCES)) as pool:
            futures = {pool.submit(_fetch_single_feed, src): src["name"] for src in NEWS_SOURCES}
            for fut in as_completed(futures):
                try:
                    collected.extend(fut.result())
                except Exception as e:
                    logger.debug(f"[NEWS] Falha no feed '{futures[fut]}': {e}")
                    continue

        # Remove duplicatas pelo título normalizado e ordena das mais recentes para mais antigas
        seen_titles = set()
        deduped: List[Dict[str, Any]] = []
        collected.sort(key=lambda x: x["timestamp"], reverse=True)
        for item in collected:
            key = item["title"].lower()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            deduped.append(item)

        if deduped:
            updated_at_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            with _NEWS_CACHE_LOCK:
                _NEWS_CACHE = (now, updated_at_str, deduped)
            all_items = deduped
        else:
            all_items = cached_items
            updated_at_str = cached_updated_str or datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            stale = True

    filtered = all_items
    cat = (category or "all").lower().strip()
    if cat in ("cripto", "forex", "macro"):
        filtered = [i for i in filtered if i["category"] == cat]

    q = (query or "").strip().lower()
    if q:
        filtered = [
            i for i in filtered
            if q in i["title"].lower() or q in i["summary"].lower() or q in i["source"].lower()
        ]

    safe_limit = max(1, min(int(limit), 100))
    return {
        "total": len(filtered),
        "updated_at": updated_at_str,
        "stale": stale,
        "items": filtered[:safe_limit],
    }
