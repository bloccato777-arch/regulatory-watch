"""
Regulatory Watch — Backend
Aggrega automaticamente le pubblicazioni dalle autorità di vigilanza bancaria
e normative EU (DORA, NIS2, AI Act) tramite RSS feed e web scraping.

Range operativo: 1 Marzo 2026 → 1 Gennaio 2029
Aggiornamento: ogni 30 minuti (configurabile via FETCH_INTERVAL)
"""

import os
import json
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, send_from_directory, request
from apscheduler.schedulers.background import BackgroundScheduler
# API Keys
GOOGLE_NEWS_API_KEY = os.getenv("GOOGLE_NEWS_API_KEY")
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN")
# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "news.json"
DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

FETCH_INTERVAL_MINUTES = int(os.getenv("FETCH_INTERVAL", "30"))

# ===== RANGE DI TEMPO =====
DATE_START = datetime(2026, 3, 1)   # Inizio: 1 Marzo 2026
DATE_END   = datetime(2029, 1, 1)   # Fine:   1 Gennaio 2029
# ===========================

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("regulatory-watch")

# ---------------------------------------------------------------------------
# Source definitions — RSS feeds + fallback scrapers
# ---------------------------------------------------------------------------
SOURCES = [
    # ---- Banca d'Italia ----
    {
        "id": "bdi_rss",
        "label": "Banca d'Italia",
        "type": "rss",
        "url": "https://www.bancaditalia.it/media/rss/comunicati-stampa.xml",
        "tags": ["bdi"],
    },
    {
        "id": "bdi_rss2",
        "label": "Banca d'Italia",
        "type": "rss",
        "url": "https://www.bancaditalia.it/media/rss/notizie.xml",
        "tags": ["bdi"],
    },
    {
        "id": "bdi_comunicati",
        "label": "Banca d'Italia",
        "type": "scrape",
        "url": "https://www.bancaditalia.it/media/comunicati/index.html",
        "tags": ["bdi"],
    },
    {
        "id": "bdi_notizie",
        "label": "Banca d'Italia",
        "type": "scrape",
        "url": "https://www.bancaditalia.it/media/notizie/index.html",
        "tags": ["bdi"],
    },
    {
        "id": "bdi_pubblicazioni",
        "label": "Banca d'Italia",
        "type": "scrape",
        "url": "https://www.bancaditalia.it/pubblicazioni/index.html",
        "tags": ["bdi"],
    },
    # ---- ECB / SSM ----
    {
        "id": "ecb_ssm_pr",
        "label": "ECB / SSM",
        "type": "rss",
        "url": "https://www.bankingsupervision.europa.eu/rss/press.xml",
        "tags": ["ecb"],
    },
    {
        "id": "ecb_ssm_speeches",
        "label": "ECB / SSM",
        "type": "rss",
        "url": "https://www.bankingsupervision.europa.eu/rss/speeches.xml",
        "tags": ["ecb"],
    },
    {
        "id": "ecb_ssm_pubs",
        "label": "ECB / SSM",
        "type": "rss",
        "url": "https://www.bankingsupervision.europa.eu/rss/publications.xml",
        "tags": ["ecb"],
    },
    {
        "id": "ecb_ssm_letters",
        "label": "ECB / SSM",
        "type": "rss",
        "url": "https://www.bankingsupervision.europa.eu/rss/letters.xml",
        "tags": ["ecb"],
    },
    # ---- EBA ----
    {
        "id": "eba_press",
        "label": "EBA",
        "type": "rss",
        "url": "https://www.eba.europa.eu/rss-feeds/press-releases.xml",
        "tags": ["eba"],
    },
    {
        "id": "eba_news",
        "label": "EBA",
        "type": "rss",
        "url": "https://www.eba.europa.eu/rss-feeds/news.xml",
        "tags": ["eba"],
    },
    {
        "id": "eba_consultations",
        "label": "EBA",
        "type": "rss",
        "url": "https://www.eba.europa.eu/rss-feeds/consultations.xml",
        "tags": ["eba"],
    },
    {
        "id": "eba_scrape",
        "label": "EBA",
        "type": "scrape",
        "url": "https://www.eba.europa.eu/publications-and-media/press-releases",
        "tags": ["eba"],
    },
    # ---- ESMA ----
    {
        "id": "esma_news",
        "label": "ESMA",
        "type": "scrape",
        "url": "https://www.esma.europa.eu/press-news/esma-news",
        "tags": ["esma"],
    },
    {
        "id": "esma_press",
        "label": "ESMA",
        "type": "scrape",
        "url": "https://www.esma.europa.eu/press-news/press-releases",
        "tags": ["esma"],
    },
    # ---- DORA ----
    {
        "id": "dora_site",
        "label": "DORA",
        "type": "scrape",
        "url": "https://www.digital-operational-resilience-act.com/",
        "tags": ["dora"],
    },
    # ---- NIS2 ----
    {
        "id": "nis2_site",
        "label": "NIS2",
        "type": "scrape",
        "url": "https://www.nis-2-directive.com/",
        "tags": ["nis2"],
    },
    # ---- AI Act ----
    {
        "id": "aiact_ec",
        "label": "AI Act",
        "type": "scrape",
        "url": "https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai",
        "tags": ["aiact"],
    },
    # ---- EUR-Lex (legislative updates DORA/NIS2/AI Act) ----
    {
        "id": "eurlex_recent",
        "label": "EUR-Lex",
        "type": "scrape",
        "url": "https://eur-lex.europa.eu/search.html?type=act&qid=regulatory&SUBDOM_INIT=LEGISLATION&DTS_SUBDOM=LEGISLATION",
        "tags": ["dora", "nis2", "aiact"],
    },
    # ---- Consiglio UE ----
    {
        "id": "council_eu",
        "label": "Consiglio UE",
        "type": "scrape",
        "url": "https://www.consilium.europa.eu/en/press/press-releases/?filters=2036&filters=2175",
        "tags": ["aiact", "nis2", "dora"],
    },
    # ---- Reuters / MarketWatch / Financial News ----
    {
        "id": "reuters_finance",
        "label": "Reuters",
        "type": "rss",
        "url": "https://feeds.reuters.com/finance/financialservices",
        "tags": ["bdi", "ecb"],
    },
    {
        "id": "marketwatch",
        "label": "MarketWatch",
        "type": "rss",
        "url": "https://feeds.marketwatch.com/marketwatch/topstories",
        "tags": ["ecb"],
    },
    {
        "id": "ft_finance",
        "label": "Financial Times",
        "type": "rss",
        "url": "https://feeds.ft.com/ftcms/rss/home",
        "tags": ["ecb", "eba"],
    },
    {
        "id": "politico_eu",
        "label": "Politico EU",
        "type": "rss",
        "url": "https://www.politico.eu/feed/",
        "tags": ["aiact", "dora", "nis2"],
    },
    {
        "id": "compliance_ai",
        "label": "Compliance News",
        "type": "rss",
        "url": "https://www.compliancesearch.com/rss/",
        "tags": ["dora", "nis2", "aiact"],
    },
    {
        "id": "fintech_mag",
        "label": "Fintech Magazine",
        "type": "rss",
        "url": "https://www.fintechmagazine.com/feed/rss.xml",
        "tags": ["aiact", "dora"],
    },
    # ---- Social Media - Google News ----
    {
        "id": "google_news_banking",
        "label": "Google News - Finanza",
        "type": "google_news",
        "keywords": "banca ECB supervisione regulazione",
        "tags": ["ecb", "bdi"],
    },
    {
        "id": "google_news_regulation",
        "label": "Google News - Regulazione",
        "type": "google_news",
        "keywords": "DORA NIS2 AI Act cybersecurity",
        "tags": ["dora", "nis2", "aiact"],
    },
    # ---- Social Media - Twitter/X ----
    {
        "id": "twitter_ecb",
        "label": "Twitter - ECB",
        "type": "twitter",
        "accounts": ["ecb", "bankingsupervision"],
        "tags": ["ecb"],
    },
    {
        "id": "twitter_eba",
        "label": "Twitter - EBA",
        "type": "twitter",
        "accounts": ["eba_bcreg"],
        "tags": ["eba"],
    },
    {
        "id": "twitter_esma",
        "label": "Twitter - ESMA",
        "type": "twitter",
        "accounts": ["ESMA_EBA_EIOPA"],
        "tags": ["esma"],
    },
]

# Keywords for auto-tagging
KEYWORD_TAGS = {
    "dora": [
        "dora", "digital operational resilience", "resilienza operativa digitale",
        "ict risk", "rischio ict", "third-party provider", "register of information",
        "ict third", "regolamento 2022/2554",
    ],
    "nis2": [
        "nis2", "nis 2", "network and information security", "cybersecurity directive",
        "direttiva cybersicurezza", "sicurezza delle reti", "direttiva 2022/2555",
        "cybersecurity", "cyber resilience",
    ],
    "aiact": [
        "ai act", "artificial intelligence act", "intelligenza artificiale",
        "regolamento ia", "ai regulation", "high-risk ai", "ai system",
        "regolamento 2024/1689", "sistemi di ia",
    ],
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
HEADERS = {
    "User-Agent": "RegulatoryWatch/2.0 (+https://github.com/regulatory-watch)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}


def make_id(title: str, url: str) -> str:
    raw = f"{title.strip().lower()}|{url.strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def parse_date(date_str: str | None) -> str | None:
    """Try to parse a date string into YYYY-MM-DD."""
    if not date_str:
        return None

    # Normalise Italian month names
    it_months = {
        "gennaio": "January", "febbraio": "February", "marzo": "March",
        "aprile": "April", "maggio": "May", "giugno": "June",
        "luglio": "July", "agosto": "August", "settembre": "September",
        "ottobre": "October", "novembre": "November", "dicembre": "December",
        "gen": "Jan", "feb": "Feb", "mar": "Mar", "apr": "Apr",
        "mag": "May", "giu": "Jun", "lug": "Jul", "ago": "Aug",
        "set": "Sep", "ott": "Oct", "nov": "Nov", "dic": "Dec",
    }
    normalised = date_str.strip()
    for it, en in it_months.items():
        normalised = re.sub(rf'\b{it}\b', en, normalised, flags=re.IGNORECASE)

    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d %B %Y",
        "%d %b %Y",
        "%B %d, %Y",
        "%b %d, %Y",
    ):
        try:
            return datetime.strptime(normalised, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    # feedparser internal date parser
    try:
        parsed = feedparser._parse_date(date_str)
        if parsed:
            return datetime(*parsed[:3]).strftime("%Y-%m-%d")
    except Exception:
        pass

    return None


def detect_extra_tags(title: str, summary: str = "") -> list[str]:
    """Auto-detect DORA/NIS2/AI Act tags from content."""
    text = f"{title} {summary}".lower()
    extra = []
    for tag, keywords in KEYWORD_TAGS.items():
        if any(kw in text for kw in keywords):
            extra.append(tag)
    return extra


def is_in_range(date_str: str | None) -> bool:
    """Check if date is within our operational range: 1 Mar 2026 → 1 Jan 2029."""
    if not date_str:
        return False
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return DATE_START <= d <= DATE_END
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------
def fetch_rss(source: dict) -> list[dict]:
    """Parse an RSS/Atom feed and return normalised items."""
    items = []
    try:
        feed = feedparser.parse(source["url"], agent=HEADERS["User-Agent"])
        for entry in feed.entries:
            date = parse_date(
                getattr(entry, "published", None)
                or getattr(entry, "updated", None)
            )
            if not is_in_range(date):
                continue

            title = entry.get("title", "").strip()
            if not title:
                continue
            link = entry.get("link", "").strip()
            summary_raw = entry.get("summary", "") or entry.get("description", "")
            summary = BeautifulSoup(summary_raw, "html.parser").get_text(strip=True)
            tags = list(source["tags"]) + detect_extra_tags(title, summary)

            items.append({
                "id": make_id(title, link),
                "date": date,
                "title": title,
                "summary": summary[:400] if summary else "",
                "source": source["tags"][0],
                "sourceLabel": source["label"],
                "tags": list(set(tags)),
                "url": link,
            })
        log.info(f"RSS  [{source['id']}] → {len(items)} items in range")
    except Exception as e:
        log.warning(f"RSS  [{source['id']}] failed: {e}")
    return items


def fetch_scrape(source: dict) -> list[dict]:
    """Scrape a web page for news items."""
    items = []
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=25)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        seen_ids = set()

        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            if len(text) < 15 or len(text) > 350:
                continue

            parent = a.find_parent(["li", "div", "article", "tr", "section", "td"])
            parent_text = parent.get_text(" ", strip=True) if parent else ""

            # Try to find date
            date = None
            date_patterns = [
                r"(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{4})",
                r"(\d{4}[/\-\.]\d{1,2}[/\-\.]\d{1,2})",
                (r"(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|"
                 r"September|October|November|December|"
                 r"Gennaio|Febbraio|Marzo|Aprile|Maggio|Giugno|"
                 r"Luglio|Agosto|Settembre|Ottobre|Novembre|Dicembre)\s+\d{4})"),
            ]
            for pat in date_patterns:
                m = re.search(pat, parent_text, re.IGNORECASE)
                if m:
                    date = parse_date(m.group(1))
                    if date:
                        break

            # Fallback: date in URL
            if not date:
                url_match = re.search(
                    r"(202[6-9])[/\-](\d{1,2})[/\-]?(\d{0,2})", a["href"]
                )
                if url_match:
                    y = url_match.group(1)
                    mo = url_match.group(2)
                    dy = url_match.group(3) or "15"
                    try:
                        date = f"{y}-{int(mo):02d}-{int(dy):02d}"
                    except ValueError:
                        pass

            if not is_in_range(date):
                continue

            href = a["href"]
            if not href.startswith("http"):
                href = urljoin(source["url"], href)

            # Extract summary from nearby elements
            summary_el = None
            if parent:
                summary_el = parent.find(
                    ["p", "span", "div"],
                    class_=lambda c: c and any(
                        x in (c if isinstance(c, str) else " ".join(c))
                        for x in ["desc", "summary", "abstract", "teaser",
                                   "excerpt", "body", "text"]
                    ),
                )
            summary = summary_el.get_text(strip=True)[:400] if summary_el else ""

            tags = list(source["tags"]) + detect_extra_tags(text, summary)
            item_id = make_id(text, href)

            if item_id not in seen_ids:
                seen_ids.add(item_id)
                items.append({
                    "id": item_id,
                    "date": date,
                    "title": text,
                    "summary": summary,
                    "source": source["tags"][0],
                    "sourceLabel": source["label"],
                    "tags": list(set(tags)),
                    "url": href,
                })

        log.info(f"SCRAPE [{source['id']}] → {len(items)} items in range")
    except Exception as e:
        log.warning(f"SCRAPE [{source['id']}] failed: {e}")
    return items

def fetch_google_news(source: dict) -> list[dict]:
    """Fetch news from Google News API."""
    items = []
    if not GOOGLE_NEWS_API_KEY:
        log.warning("GOOGLE_NEWS_API_KEY not set")
        return items
    
    try:
        url = "https://newsapi.org/v2/everything"
        params = {
            "q": source["keywords"],
            "sortBy": "publishedAt",
            "language": "en",
            "apiKey": GOOGLE_NEWS_API_KEY,
        }
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        
        for article in data.get("articles", []):
            date = parse_date(article.get("publishedAt"))
            if not is_in_range(date):
                continue
            
            title = article.get("title", "").strip()
            url_article = article.get("url", "").strip()
            summary = article.get("description", "")[:300]
            tags = list(source["tags"]) + detect_extra_tags(title, summary)
            
            items.append({
                "id": make_id(title, url_article),
                "date": date,
                "title": title,
                "summary": summary,
                "source": "google_news",
                "sourceLabel": source["label"],
                "tags": list(set(tags)),
                "url": url_article,
            })
        
        log.info(f"GOOGLE_NEWS [{source['id']}] → {len(items)} items")
    except Exception as e:
        log.warning(f"GOOGLE_NEWS [{source['id']}] failed: {e}")
    
    return items


def fetch_twitter(source: dict) -> list[dict]:
    """Fetch tweets from Twitter/X API v2."""
    items = []
    if not TWITTER_BEARER_TOKEN:
        log.warning("TWITTER_BEARER_TOKEN not set")
        return items
    
    try:
        headers = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}
        
        for account in source.get("accounts", []):
            url = "https://api.twitter.com/2/tweets/search/recent"
            params = {
                "query": f"from:{account} -is:retweet",
                "max_results": 50,
                "tweet.fields": "created_at,author_id",
                "expansions": "author_id",
                "user.fields": "username",
            }
            
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            
            for tweet in data.get("data", []):
                date = parse_date(tweet.get("created_at"))
                if not is_in_range(date):
                    continue
                
                title = tweet.get("text", "").strip()[:150]
                if len(title) < 20:
                    continue
                
                url_tweet = f"https://twitter.com/{account}/status/{tweet['id']}"
                tags = list(source["tags"]) + detect_extra_tags(title)
                
                items.append({
                    "id": make_id(title, url_tweet),
                    "date": date,
                    "title": f"[{account.upper()}] {title}",
                    "summary": "",
                    "source": source["tags"][0],
                    "sourceLabel": source["label"],
                    "tags": list(set(tags)),
                    "url": url_tweet,
                })
        
        log.info(f"TWITTER [{source['id']}] → {len(items)} items")
    except Exception as e:
        log.warning(f"TWITTER [{source['id']}] failed: {e}")
    
    return items


# ---------------------------------------------------------------------------
# Main fetch orchestrator
# ---------------------------------------------------------------------------
def fetch_all() -> list[dict]:
    """Fetch from all sources, merge and deduplicate."""
    all_items = []
    for src in SOURCES:
        try:
            if src["type"] == "rss":
                all_items.extend(fetch_rss(src))
            elif src["type"] == "scrape":
                all_items.extend(fetch_scrape(src))
            elif src["type"] == "google_news":
                all_items.extend(fetch_google_news(src))
            elif src["type"] == "twitter":
                all_items.extend(fetch_twitter(src))
        except Exception as e:
            log.error(f"Source [{src['id']}] error: {e}")

    # Deduplicate by id, merge tags
    seen = {}
    for item in all_items:
        existing = seen.get(item["id"])
        if existing:
            existing["tags"] = list(set(existing["tags"] + item["tags"]))
        else:
            seen[item["id"]] = item

    result = sorted(seen.values(), key=lambda x: x["date"] or "0000-00-00", reverse=True)
    log.info(f"Total unique items after fetch: {len(result)}")
    return result


def refresh_data():
    """Fetch all data and save to disk. Merges with existing seed/manual items."""
    log.info("=" * 60)
    log.info("DATA REFRESH STARTED")
    log.info("=" * 60)
    try:
        new_items = fetch_all()

        # Load existing to keep seed items as fallback
        existing = load_data()
        existing_manual = {
            item["id"]: item for item in existing if item.get("_manual")
        }

        # Build final set: fetched items take priority, manual fill gaps
        final = {}
        for item in new_items:
            final[item["id"]] = item
        for mid, mitem in existing_manual.items():
            if mid not in final:
                final[mid] = mitem

        items = sorted(final.values(),
                       key=lambda x: x["date"] or "0000-00-00", reverse=True)

        payload = {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "count": len(items),
            "date_range": {
                "start": DATE_START.strftime("%Y-%m-%d"),
                "end": DATE_END.strftime("%Y-%m-%d"),
            },
            "items": items,
        }
        DATA_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info(f"Data saved: {len(items)} items | range {DATE_START.date()} → {DATE_END.date()}")
    except Exception as e:
        log.error(f"Refresh failed: {e}", exc_info=True)


def load_data() -> list[dict]:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8")).get("items", [])
        except Exception:
            return []
    return []


def load_payload() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_updated": None, "count": 0, "items": [],
            "date_range": {"start": "2026-03-01", "end": "2029-01-01"}}


# ---------------------------------------------------------------------------
# Seed data (baseline — verrà integrata dai feed reali)
# ---------------------------------------------------------------------------
def seed_initial_data():
    """Populate data file with known items if empty."""
    if DATA_FILE.exists():
        payload = load_payload()
        if payload.get("items"):
            return

    log.info("Seeding initial baseline data...")
    seed = [
        {"id":"seed01","date":"2026-03-30","title":"ECB semplifica la supervisione sui modelli interni delle banche","summary":"La BCE snellisce il processo di valutazione delle modifiche ai modelli interni per il rischio di credito.","source":"ecb","sourceLabel":"ECB / SSM","tags":["ecb"],"url":"https://www.bankingsupervision.europa.eu/press/pr/date/2026/html/index.en.html","_manual":True},
        {"id":"seed02","date":"2026-03-27","title":"Banca d'Italia: Banche e istituzioni finanziarie — articolazione territoriale 2025","summary":"Statistica annuale sull'articolazione territoriale di banche e istituzioni finanziarie in Italia.","source":"bdi","sourceLabel":"Banca d'Italia","tags":["bdi"],"url":"https://www.bancaditalia.it/media/notizia/banche-e-istituzioni-finanziarie-2025/","_manual":True},
        {"id":"seed03","date":"2026-03-27","title":"Banca d'Italia dispone affiancamento commissariale per BFF Bank","summary":"Nomina di due commissari in temporaneo affiancamento al consiglio di amministrazione di BFF Bank.","source":"bdi","sourceLabel":"Banca d'Italia","tags":["bdi"],"url":"https://www.bancaditalia.it/media/comunicati/index.html","_manual":True},
        {"id":"seed04","date":"2026-03-27","title":"ESAs: rinvio del reporting settimanale sulle posizioni in derivati su materie prime","summary":"Le Autorità di Vigilanza Europee annunciano il posticipo dell'avvio del reporting settimanale.","source":"esma","sourceLabel":"ESMA / ESAs","tags":["esma","eba"],"url":"https://www.esma.europa.eu/press-news/esma-news","_manual":True},
        {"id":"seed05","date":"2026-03-24","title":"ECB: la supervisione bancaria in un mercato del credito frammentato","summary":"Intervista della BCE sulle interconnessioni nel mercato del credito e l'importanza della supervisione.","source":"ecb","sourceLabel":"ECB / SSM","tags":["ecb"],"url":"https://www.bankingsupervision.europa.eu/press/interviews/date/2026/html/index.en.html","_manual":True},
        {"id":"seed06","date":"2026-03-22","title":"DORA: scadenza per la presentazione del Register of Information (RoI)","summary":"Scadenza per la consegna del Registro delle Informazioni ai sensi di DORA.","source":"dora","sourceLabel":"DORA","tags":["dora","eba"],"url":"https://www.digital-operational-resilience-act.com/","_manual":True},
        {"id":"seed07","date":"2026-03-18","title":"ECB pubblica le statistiche di vigilanza bancaria — Q4 2025","summary":"CET1 ratio al 16,18%. NPL ratio scende al 2,18%, livello più basso dalla prima pubblicazione.","source":"ecb","sourceLabel":"ECB / SSM","tags":["ecb"],"url":"https://www.bankingsupervision.europa.eu/press/pr/date/2026/html/index.en.html","_manual":True},
        {"id":"seed08","date":"2026-03-18","title":"ECB pubblica il Rapporto Annuale sulle attività di vigilanza 2025","summary":"Il rapporto annuale descrive le attività di vigilanza bancaria svolte nel 2025 nell'ambito dell'SSM.","source":"ecb","sourceLabel":"ECB / SSM","tags":["ecb"],"url":"https://www.bankingsupervision.europa.eu/press/other-publications/annual-report/html/all-releases.en.html","_manual":True},
        {"id":"seed09","date":"2026-03-17","title":"EBA pubblica il secondo Impact Assessment Report sul MREL","summary":"Valutazione d'impatto sul requisito minimo di fondi propri e passività ammissibili (MREL).","source":"eba","sourceLabel":"EBA","tags":["eba"],"url":"https://www.eba.europa.eu/publications-and-media/publications","_manual":True},
        {"id":"seed10","date":"2026-03-17","title":"ESAs pubblicano l'aggiornamento Spring 2026 sui rischi nel sistema finanziario UE","summary":"Aggiornamento congiunto su rischi e vulnerabilità: tensioni geopolitiche e finanza privata.","source":"eba","sourceLabel":"EBA / ESAs","tags":["eba","esma"],"url":"https://www.eba.europa.eu/publications-and-media/press-releases","_manual":True},
        {"id":"seed11","date":"2026-03-13","title":"Banca d'Italia pubblica le Disposizioni consolidate antiriciclaggio","summary":"Versione consolidata delle Disposizioni in materia antiriciclaggio.","source":"bdi","sourceLabel":"Banca d'Italia","tags":["bdi"],"url":"https://www.bancaditalia.it/compiti/supervisione-normativa-antiriciclaggio/","_manual":True},
        {"id":"seed12","date":"2026-03-13","title":"Consiglio UE: posizione comune per semplificare le regole sull'Intelligenza Artificiale","summary":"Posizione del Consiglio nell'ambito del pacchetto Omnibus VII per semplificare il framework AI Act.","source":"aiact","sourceLabel":"AI Act","tags":["aiact"],"url":"https://www.consilium.europa.eu/en/press/press-releases/2026/03/13/","_manual":True},
        {"id":"seed13","date":"2026-03-11","title":"ESMA: i mercati UE restano ad alto rischio — aggiornamento TRV Spring 2026","summary":"Tre fattori chiave: geopolitica, valutazioni elevate e minacce cyber/ibride.","source":"esma","sourceLabel":"ESMA","tags":["esma"],"url":"https://www.esma.europa.eu/press-news/esma-news","_manual":True},
        {"id":"seed14","date":"2026-03-10","title":"L'economia italiana in breve, n. 3 — Marzo 2026","summary":"Statistiche sugli andamenti economici del sistema finanziario italiano.","source":"bdi","sourceLabel":"Banca d'Italia","tags":["bdi"],"url":"https://www.bancaditalia.it/media/notizia/l-economia-italiana-in-breve-n-3-marzo-2026/","_manual":True},
        {"id":"seed15","date":"2026-03-06","title":"NIS2: scadenza registrazione BSI per le aziende tedesche","summary":"Scadenza registrazione BSI per le 29.500 aziende soggette NIS2 in Germania.","source":"nis2","sourceLabel":"NIS2","tags":["nis2"],"url":"https://www.nis-2-directive.com/","_manual":True},
        {"id":"seed16","date":"2026-03-03","title":"AI Act: seconda bozza del Codice di Pratica sulla marcatura dei contenuti generati da AI","summary":"Codice di Pratica volontario sulla marcatura ed etichettatura dei contenuti generati dall'IA.","source":"aiact","sourceLabel":"AI Act","tags":["aiact"],"url":"https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai","_manual":True},
        {"id":"seed17","date":"2026-03-01","title":"EBA avvia la validazione centralizzata dell'ISDA SIMM nell'UE","summary":"Avvio della funzione di validazione centralizzata del modello ISDA SIMM.","source":"eba","sourceLabel":"EBA","tags":["eba"],"url":"https://www.eba.europa.eu/publications-and-media/press-releases/eba-kicks-eu-central-validation-isda-simm-1-march-2026","_manual":True},
        {"id":"seed18","date":"2026-03-01","title":"DORA: passaggio alla fase di enforcement attivo","summary":"Dopo il 2025 come transizione, DORA entra nella fase di enforcement attivo nel 2026.","source":"dora","sourceLabel":"DORA","tags":["dora"],"url":"https://www.digital-operational-resilience-act.com/","_manual":True},
        {"id":"seed19","date":"2026-03-01","title":"ESAs pubblicano la lista dei Critical ICT Third-Party Providers (CTPPs) ai sensi di DORA","summary":"Prima lista ufficiale dei fornitori ICT critici designati.","source":"dora","sourceLabel":"DORA / ESAs","tags":["dora","eba","esma"],"url":"https://www.digital-operational-resilience-act.com/","_manual":True},
        {"id":"seed20","date":"2026-03-01","title":"EBA consulta sugli standard tecnici per requisiti prudenziali dei CSD","summary":"Consultazione modifiche standard tecnici per depositari centrali di titoli.","source":"eba","sourceLabel":"EBA","tags":["eba"],"url":"https://www.eba.europa.eu/publications-and-media/press-releases","_manual":True},
    ]
    payload = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "count": len(seed),
        "date_range": {"start": "2026-03-01", "end": "2029-01-01"},
        "items": seed,
    }
    DATA_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"Seeded {len(seed)} baseline items")


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder="static")


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/news")
def api_news():
    """Return all news items, optionally filtered."""
    payload = load_payload()
    tag = request.args.get("tag")
    if tag and tag != "all":
        payload["items"] = [
            item for item in payload["items"] if tag in item.get("tags", [])
        ]
        payload["count"] = len(payload["items"])
    return jsonify(payload)


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Trigger a manual refresh."""
    refresh_data()
    payload = load_payload()
    return jsonify({
        "status": "ok",
        "count": payload["count"],
        "last_updated": payload["last_updated"],
    })


@app.route("/api/status")
def api_status():
    payload = load_payload()
    return jsonify({
        "status": "ok",
        "last_updated": payload.get("last_updated"),
        "total_items": payload.get("count", 0),
        "sources": len(SOURCES),
        "refresh_interval_minutes": FETCH_INTERVAL_MINUTES,
        "date_range": payload.get("date_range", {}),
    })


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
scheduler = BackgroundScheduler(daemon=True)


def start_scheduler():
    if not scheduler.running:
        scheduler.add_job(
            refresh_data,
            "interval",
            minutes=FETCH_INTERVAL_MINUTES,
            id="refresh_data",
            replace_existing=True,
            next_run_time=datetime.now() + timedelta(seconds=15),
        )
        scheduler.start()
        log.info(f"Scheduler started — refresh every {FETCH_INTERVAL_MINUTES} min")
        log.info(f"Date range: {DATE_START.date()} → {DATE_END.date()}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Refresh subito al startup
    refresh_data()
    # Poi avvia lo scheduler
    start_scheduler()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)