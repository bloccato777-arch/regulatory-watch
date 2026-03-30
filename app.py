"""
Regulatory Watch — Backend
Aggrega automaticamente le pubblicazioni dalle autorità di vigilanza bancaria
e normative EU (DORA, NIS2, AI Act) tramite RSS feed e web scraping.
"""

import os
import json
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, send_from_directory, request
from apscheduler.schedulers.background import BackgroundScheduler

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "news.json"
DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

FETCH_INTERVAL_MINUTES = int(os.getenv("FETCH_INTERVAL", "30"))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("regulatory-watch")

# ---------------------------------------------------------------------------
# Source definitions — RSS feeds + fallback scrapers
# ---------------------------------------------------------------------------
SOURCES = [
    # ---- Banca d'Italia ----
    {
        "id": "bdi",
        "label": "Banca d'Italia",
        "type": "rss",
        "url": "https://alert.bancaditalia.it/webApp/rss?LANGUAGE=it",
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
    # ---- EBA ----
    {
        "id": "eba_news",
        "label": "EBA",
        "type": "rss",
        "url": "https://www.eba.europa.eu/news-press/news/rss.xml",
        "tags": ["eba"],
    },
    {
        "id": "eba_press",
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
    # ---- DORA (via ESAs + dedicated) ----
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
    # ---- EUR-Lex (for DORA / NIS2 / AI Act legislative updates) ----
    {
        "id": "eurlex_dora",
        "label": "DORA",
        "type": "rss",
        "url": "https://eur-lex.europa.eu/EN/display-feed.html?rssId=TFkgMTA5&language=en",
        "tags": ["dora"],
    },
]

# Keywords for auto-tagging additional regulatory categories
KEYWORD_TAGS = {
    "dora": ["dora", "digital operational resilience", "resilienza operativa digitale",
             "ict risk", "rischio ict", "third-party provider", "register of information"],
    "nis2": ["nis2", "nis 2", "network and information security", "cybersecurity directive",
             "direttiva cybersicurezza", "sicurezza delle reti"],
    "aiact": ["ai act", "artificial intelligence act", "intelligenza artificiale",
              "regolamento ia", "ai regulation", "high-risk ai", "ai system"],
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
HEADERS = {
    "User-Agent": "RegulatoryWatch/1.0 (+https://github.com/regulatory-watch)"
}

def make_id(title: str, url: str) -> str:
    raw = f"{title.strip().lower()}|{url.strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

def parse_date(date_str: str | None) -> str | None:
    """Try to parse a date string into YYYY-MM-DD."""
    if not date_str:
        return None
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d %B %Y",
        "%d %b %Y",
    ):
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Try feedparser's date parser
    parsed = feedparser._parse_date(date_str) if hasattr(feedparser, '_parse_date') else None
    if parsed:
        return datetime(*parsed[:3]).strftime("%Y-%m-%d")
    return None

def detect_extra_tags(title: str, summary: str = "") -> list[str]:
    """Auto-detect DORA/NIS2/AI Act tags from content."""
    text = f"{title} {summary}".lower()
    extra = []
    for tag, keywords in KEYWORD_TAGS.items():
        if any(kw in text for kw in keywords):
            extra.append(tag)
    return extra

def is_in_range(date_str: str) -> bool:
    """Check if date is within our range (1 March 2026 → today)."""
    if not date_str:
        return False
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        start = datetime(2026, 3, 1)
        end = datetime.now() + timedelta(days=1)
        return start <= d <= end
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
            date = parse_date(getattr(entry, "published", None) or
                             getattr(entry, "updated", None))
            if not is_in_range(date):
                continue
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(strip=True)
            tags = list(source["tags"]) + detect_extra_tags(title, summary)
            items.append({
                "id": make_id(title, link),
                "date": date,
                "title": title,
                "summary": summary[:300] if summary else "",
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
        resp = requests.get(source["url"], headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # Generic approach: find links with dates
        # We look for common patterns in institutional sites
        candidates = []

        # Pattern 1: date in text near links
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            if len(text) < 15 or len(text) > 300:
                continue

            # Look for a date in nearby elements
            parent = a.find_parent(["li", "div", "article", "tr", "section"])
            parent_text = parent.get_text(" ", strip=True) if parent else ""

            # Try to find dates in various formats
            date = None
            date_patterns = [
                r"(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{4})",
                r"(\d{4}[/\-\.]\d{1,2}[/\-\.]\d{1,2})",
                r"(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December|"
                r"Gen(?:naio)?|Feb(?:braio)?|Mar(?:zo)?|Apr(?:ile)?|Mag(?:gio)?|Giu(?:gno)?|"
                r"Lug(?:lio)?|Ago(?:sto)?|Set(?:tembre)?|Ott(?:obre)?|Nov(?:embre)?|Dic(?:embre)?)\s+\d{4})",
            ]
            for pat in date_patterns:
                m = re.search(pat, parent_text, re.IGNORECASE)
                if m:
                    date = parse_date(m.group(1))
                    if date:
                        break

            if not date:
                # Check for date in URL
                url_date_match = re.search(r"(2026)[/\-](\d{1,2})[/\-]?(\d{0,2})", a["href"])
                if url_date_match:
                    y, m_str = url_date_match.group(1), url_date_match.group(2)
                    d_str = url_date_match.group(3) or "15"
                    try:
                        date = f"{y}-{int(m_str):02d}-{int(d_str):02d}"
                    except ValueError:
                        pass

            if not is_in_range(date):
                continue

            href = a["href"]
            if not href.startswith("http"):
                from urllib.parse import urljoin
                href = urljoin(source["url"], href)

            summary_el = parent.find(["p", "span", "div"], class_=lambda c: c and
                                     any(x in (c if isinstance(c, str) else " ".join(c))
                                         for x in ["desc", "summary", "abstract", "teaser", "excerpt"]))
            summary = summary_el.get_text(strip=True)[:300] if summary_el else ""

            tags = list(source["tags"]) + detect_extra_tags(text, summary)

            candidates.append({
                "id": make_id(text, href),
                "date": date,
                "title": text,
                "summary": summary,
                "source": source["tags"][0],
                "sourceLabel": source["label"],
                "tags": list(set(tags)),
                "url": href,
            })

        # Deduplicate
        seen = set()
        for c in candidates:
            if c["id"] not in seen:
                seen.add(c["id"])
                items.append(c)

        log.info(f"SCRAPE [{source['id']}] → {len(items)} items in range")
    except Exception as e:
        log.warning(f"SCRAPE [{source['id']}] failed: {e}")
    return items

# ---------------------------------------------------------------------------
# Main fetch orchestrator
# ---------------------------------------------------------------------------
def fetch_all() -> list[dict]:
    """Fetch from all sources, merge and deduplicate."""
    all_items = []
    for src in SOURCES:
        if src["type"] == "rss":
            all_items.extend(fetch_rss(src))
        elif src["type"] == "scrape":
            all_items.extend(fetch_scrape(src))

    # Deduplicate by id
    seen = {}
    for item in all_items:
        existing = seen.get(item["id"])
        if existing:
            # Merge tags
            existing["tags"] = list(set(existing["tags"] + item["tags"]))
        else:
            seen[item["id"]] = item

    result = sorted(seen.values(), key=lambda x: x["date"] or "0000-00-00", reverse=True)
    log.info(f"Total unique items: {len(result)}")
    return result

def refresh_data():
    """Fetch all data and save to disk."""
    log.info("Starting data refresh...")
    try:
        items = fetch_all()

        # Load existing data to preserve manually-added fallback items
        existing = load_data()
        existing_ids = {item["id"] for item in existing if item.get("_manual")}
        manual_items = [item for item in existing if item.get("_manual")]

        # Merge: fetched items + manual items (manual ones fill gaps)
        fetched_ids = {item["id"] for item in items}
        for mi in manual_items:
            if mi["id"] not in fetched_ids:
                items.append(mi)

        items.sort(key=lambda x: x["date"] or "0000-00-00", reverse=True)

        # Save
        payload = {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "count": len(items),
            "items": items,
        }
        DATA_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info(f"Data saved: {len(items)} items")
    except Exception as e:
        log.error(f"Refresh failed: {e}", exc_info=True)

def load_data() -> list[dict]:
    """Load items from the data file."""
    if DATA_FILE.exists():
        try:
            payload = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            return payload.get("items", [])
        except Exception:
            return []
    return []

def load_payload() -> dict:
    """Load the full payload from the data file."""
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_updated": None, "count": 0, "items": []}

# ---------------------------------------------------------------------------
# Seed data (the manually curated items from our initial research)
# ---------------------------------------------------------------------------
def seed_initial_data():
    """Populate data file with known items if empty, as a reliable baseline."""
    if DATA_FILE.exists():
        payload = load_payload()
        if payload.get("items"):
            return  # Already have data

    log.info("Seeding initial data...")
    seed = [
        {"id":"seed01","date":"2026-03-30","title":"ECB semplifica la supervisione sui modelli interni delle banche","summary":"La BCE snellisce il processo di valutazione delle modifiche ai modelli interni per il rischio di credito.","source":"ecb","sourceLabel":"ECB / SSM","tags":["ecb"],"url":"https://www.bankingsupervision.europa.eu/press/pr/date/2026/html/index.en.html","_manual":True},
        {"id":"seed02","date":"2026-03-27","title":"Banca d'Italia: Banche e istituzioni finanziarie — articolazione territoriale 2025","summary":"Statistica annuale sull'articolazione territoriale di banche e istituzioni finanziarie in Italia.","source":"bdi","sourceLabel":"Banca d'Italia","tags":["bdi"],"url":"https://www.bancaditalia.it/media/notizia/banche-e-istituzioni-finanziarie-2025/","_manual":True},
        {"id":"seed03","date":"2026-03-27","title":"Banca d'Italia dispone affiancamento commissariale per BFF Bank","summary":"Nomina di due commissari in temporaneo affiancamento al consiglio di amministrazione di BFF Bank.","source":"bdi","sourceLabel":"Banca d'Italia","tags":["bdi"],"url":"https://www.bancaditalia.it/media/comunicati/index.html","_manual":True},
        {"id":"seed04","date":"2026-03-27","title":"ESAs: rinvio del reporting settimanale sulle posizioni in derivati su materie prime","summary":"Le Autorità di Vigilanza Europee annunciano il posticipo dell'avvio del reporting settimanale.","source":"esma","sourceLabel":"ESMA / ESAs","tags":["esma","eba"],"url":"https://www.esma.europa.eu/press-news/esma-news","_manual":True},
        {"id":"seed05","date":"2026-03-24","title":"ECB: la supervisione bancaria in un mercato del credito frammentato","summary":"Intervista della BCE sulle interconnessioni nel mercato del credito e l'importanza della supervisione.","source":"ecb","sourceLabel":"ECB / SSM","tags":["ecb"],"url":"https://www.bankingsupervision.europa.eu/press/interviews/date/2026/html/index.en.html","_manual":True},
        {"id":"seed06","date":"2026-03-22","title":"DORA: scadenza per la presentazione del Register of Information (RoI)","summary":"Scadenza per la consegna del Registro delle Informazioni ai sensi di DORA.","source":"dora","sourceLabel":"DORA","tags":["dora","eba"],"url":"https://www.digital-operational-resilience-act.com/","_manual":True},
        {"id":"seed07","date":"2026-03-18","title":"ECB pubblica le statistiche di vigilanza bancaria — Q4 2025","summary":"CET1 ratio al 16,18%. NPL ratio scende al 2,18%, il livello più basso dalla prima pubblicazione.","source":"ecb","sourceLabel":"ECB / SSM","tags":["ecb"],"url":"https://www.bankingsupervision.europa.eu/press/pr/date/2026/html/index.en.html","_manual":True},
        {"id":"seed08","date":"2026-03-18","title":"ECB pubblica il Rapporto Annuale sulle attività di vigilanza 2025","summary":"Il rapporto annuale descrive le attività di vigilanza bancaria svolte nel 2025 nell'ambito dell'SSM.","source":"ecb","sourceLabel":"ECB / SSM","tags":["ecb"],"url":"https://www.bankingsupervision.europa.eu/press/other-publications/annual-report/html/all-releases.en.html","_manual":True},
        {"id":"seed09","date":"2026-03-17","title":"EBA pubblica il secondo Impact Assessment Report sul MREL","summary":"Valutazione d'impatto sul requisito minimo di fondi propri e passività ammissibili (MREL).","source":"eba","sourceLabel":"EBA","tags":["eba"],"url":"https://www.eba.europa.eu/publications-and-media/publications","_manual":True},
        {"id":"seed10","date":"2026-03-17","title":"ESAs pubblicano l'aggiornamento Spring 2026 sui rischi nel sistema finanziario UE","summary":"Aggiornamento congiunto su rischi e vulnerabilità: tensioni geopolitiche e finanza privata.","source":"eba","sourceLabel":"EBA / ESAs","tags":["eba","esma"],"url":"https://www.eba.europa.eu/publications-and-media/press-releases","_manual":True},
        {"id":"seed11","date":"2026-03-13","title":"Banca d'Italia pubblica le Disposizioni consolidate antiriciclaggio","summary":"Versione consolidata delle Disposizioni in materia antiriciclaggio.","source":"bdi","sourceLabel":"Banca d'Italia","tags":["bdi"],"url":"https://www.bancaditalia.it/compiti/supervisione-normativa-antiriciclaggio/","_manual":True},
        {"id":"seed12","date":"2026-03-13","title":"Consiglio UE: posizione comune per semplificare le regole sull'Intelligenza Artificiale","summary":"Posizione del Consiglio nell'ambito del pacchetto Omnibus VII per semplificare il framework AI Act.","source":"aiact","sourceLabel":"AI Act","tags":["aiact"],"url":"https://www.consilium.europa.eu/en/press/press-releases/2026/03/13/","_manual":True},
        {"id":"seed13","date":"2026-03-11","title":"ESMA: i mercati UE restano ad alto rischio — aggiornamento TRV Spring 2026","summary":"Tre fattori chiave di rischio: geopolitica, valutazioni elevate e minacce cyber/ibride.","source":"esma","sourceLabel":"ESMA","tags":["esma"],"url":"https://www.esma.europa.eu/press-news/esma-news","_manual":True},
        {"id":"seed14","date":"2026-03-10","title":"L'economia italiana in breve, n. 3 — Marzo 2026","summary":"Statistiche sugli andamenti economici del sistema economico e finanziario italiano.","source":"bdi","sourceLabel":"Banca d'Italia","tags":["bdi"],"url":"https://www.bancaditalia.it/media/notizia/l-economia-italiana-in-breve-n-3-marzo-2026/","_manual":True},
        {"id":"seed15","date":"2026-03-06","title":"NIS2: scadenza registrazione BSI per le aziende tedesche","summary":"Scadenza per la registrazione presso il BSI per le 29.500 aziende soggette NIS2 in Germania.","source":"nis2","sourceLabel":"NIS2","tags":["nis2"],"url":"https://www.nis-2-directive.com/","_manual":True},
        {"id":"seed16","date":"2026-03-03","title":"AI Act: seconda bozza del Codice di Pratica sulla marcatura dei contenuti generati da AI","summary":"Codice di Pratica volontario sulla marcatura ed etichettatura dei contenuti generati dall'IA.","source":"aiact","sourceLabel":"AI Act","tags":["aiact"],"url":"https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai","_manual":True},
        {"id":"seed17","date":"2026-03-01","title":"EBA avvia la validazione centralizzata dell'ISDA SIMM nell'UE","summary":"Avvio della funzione di validazione centralizzata del modello ISDA SIMM.","source":"eba","sourceLabel":"EBA","tags":["eba"],"url":"https://www.eba.europa.eu/publications-and-media/press-releases/eba-kicks-eu-central-validation-isda-simm-1-march-2026","_manual":True},
        {"id":"seed18","date":"2026-03-01","title":"DORA: passaggio alla fase di enforcement attivo","summary":"Dopo il 2025 come anno di transizione, DORA entra nella fase di enforcement attivo nel 2026.","source":"dora","sourceLabel":"DORA","tags":["dora"],"url":"https://www.digital-operational-resilience-act.com/","_manual":True},
        {"id":"seed19","date":"2026-03-01","title":"ESAs pubblicano la lista dei Critical ICT Third-Party Providers (CTPPs) ai sensi di DORA","summary":"Prima lista ufficiale dei fornitori ICT critici designati.","source":"dora","sourceLabel":"DORA / ESAs","tags":["dora","eba","esma"],"url":"https://www.digital-operational-resilience-act.com/","_manual":True},
        {"id":"seed20","date":"2026-03-01","title":"EBA consulta sugli standard tecnici per requisiti prudenziali dei CSD","summary":"Consultazione sulle modifiche agli standard tecnici per i depositari centrali di titoli.","source":"eba","sourceLabel":"EBA","tags":["eba"],"url":"https://www.eba.europa.eu/publications-and-media/press-releases","_manual":True},
    ]
    payload = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "count": len(seed),
        "items": seed,
    }
    DATA_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"Seeded {len(seed)} items")

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder="static")

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/news")
def api_news():
    """Return all news items, optionally filtered by source/tag."""
    payload = load_payload()
    tag = request.args.get("tag")
    if tag and tag != "all":
        payload["items"] = [
            item for item in payload["items"]
            if tag in item.get("tags", [])
        ]
        payload["count"] = len(payload["items"])
    return jsonify(payload)

@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Trigger a manual refresh of the data."""
    refresh_data()
    payload = load_payload()
    return jsonify({"status": "ok", "count": payload["count"],
                    "last_updated": payload["last_updated"]})

@app.route("/api/status")
def api_status():
    """Health check / status endpoint."""
    payload = load_payload()
    return jsonify({
        "status": "ok",
        "last_updated": payload.get("last_updated"),
        "total_items": payload.get("count", 0),
        "sources": len(SOURCES),
        "refresh_interval_minutes": FETCH_INTERVAL_MINUTES,
    })

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
scheduler = BackgroundScheduler()

def start_scheduler():
    scheduler.add_job(refresh_data, "interval", minutes=FETCH_INTERVAL_MINUTES,
                      id="refresh_data", replace_existing=True,
                      next_run_time=datetime.now() + timedelta(seconds=10))
    scheduler.start()
    log.info(f"Scheduler started — refresh every {FETCH_INTERVAL_MINUTES} min")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    seed_initial_data()
    start_scheduler()
    port = int(os.getenv("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=False)
