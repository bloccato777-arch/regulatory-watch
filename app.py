from flask import Flask, jsonify, request
from datetime import datetime, timedelta
import os
import json
import feedparser
import requests
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
import logging
import atexit

app = Flask(__name__)

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Data file path
DATA_FILE = "data/news.json"
os.makedirs("data", exist_ok=True)

# Initialize data file if it doesn't exist
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, 'w') as f:
        json.dump([], f)

# API Keys from environment
GOOGLE_NEWS_API_KEY = os.getenv("GOOGLE_NEWS_API_KEY", "")

# Define all sources
SOURCES = {
    # Banking supervisory authorities
    "Banca d'Italia": {
        "url": "https://www.bancaditalia.it/media/notizie/",
        "type": "scrape",
        "category": "bdi"
    },
    # ECB/SSM sources
    "ECB - Press Releases": {
        "url": "https://www.ecb.europa.eu/press/pr/date/html/index.en.html",
        "type": "scrape",
        "category": "ecb"
    },
    "ECB - Monetary Policy": {
        "url": "https://www.ecb.europa.eu/mopo/html/index.en.html",
        "type": "scrape",
        "category": "ecb"
    },
    # EBA sources
    "EBA - News": {
        "url": "https://www.eba.europa.eu/news",
        "type": "scrape",
        "category": "eba"
    },
    "EBA - RSS": {
        "url": "https://www.eba.europa.eu/sites/default/files/feed/eba-news.xml",
        "type": "rss",
        "category": "eba"
    },
    # ESMA sources
    "ESMA - News": {
        "url": "https://www.esma.europa.eu/news-press/news",
        "type": "scrape",
        "category": "esma"
    },
    # DORA and NIS2
    "DORA - News": {
        "url": "https://finance.ec.europa.eu/capital-markets-union-and-financial-markets/digital-finance/digital-operational-resilience-act-dora_en",
        "type": "scrape",
        "category": "dora"
    },
    "NIS2 - News": {
        "url": "https://digital-strategy.ec.europa.eu/en/policies/nis2-directive",
        "type": "scrape",
        "category": "nis2"
    },
    # AI Act
    "AI Act - News": {
        "url": "https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai",
        "type": "scrape",
        "category": "aiact"
    },
    # RSS Feeds - Financial News
    "Reuters - Banking": {
        "url": "https://feeds.bloomberg.com/markets/news.rss",
        "type": "rss",
        "category": "bdi"
    },
    "Financial Times": {
        "url": "https://feeds.ft.com/markets",
        "type": "rss",
        "category": "ecb"
    },
    # Google News - Banking
    "google_news_banking": {
        "type": "google_news",
        "keywords": ["banking regulation ECB supervision"],
        "category": "bdi"
    },
    # Google News - Regulatory
    "google_news_regulation": {
        "type": "google_news",
        "keywords": ["EU regulation DORA NIS2"],
        "category": "dora"
    }
}

def fetch_rss(source_name, source_config):
    """Fetch news from RSS feeds"""
    try:
        feed = feedparser.parse(source_config["url"])
        articles = []

        for entry in feed.entries[:10]:  # Limit to 10 per source
            article = {
                "title": entry.get("title", ""),
                "summary": entry.get("summary", "")[:200],
                "source": source_name,
                "category": source_config.get("category", ""),
                "published": datetime.now().isoformat(),
                "url": entry.get("link", "")
            }
            articles.append(article)

        logger.info(f"RSS [{source_name}] → {len(articles)} items")
        return articles
    except Exception as e:
        logger.error(f"RSS [{source_name}] Error: {str(e)}")
        return []

def fetch_scrape(source_name, source_config):
    """Fetch news by scraping web pages"""
    try:
        response = requests.get(source_config["url"], timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')

        # Generic article extraction
        articles = []
        article_elements = soup.find_all('article')[:5]

        if not article_elements:
            article_elements = soup.find_all(['div', 'li'], class_=lambda x: x and ('article' in x.lower() or 'news' in x.lower()))[:5]

        for elem in article_elements:
            title_elem = elem.find(['h2', 'h3', 'a'])
            title = title_elem.get_text(strip=True) if title_elem else ""

            summary_elem = elem.find(['p', 'summary'])
            summary = summary_elem.get_text(strip=True)[:200] if summary_elem else ""

            if title:
                article = {
                    "title": title,
                    "summary": summary,
                    "source": source_name,
                    "category": source_config.get("category", ""),
                    "published": datetime.now().isoformat(),
                    "url": source_config["url"]
                }
                articles.append(article)

        logger.info(f"SCRAPE [{source_name}] → {len(articles)} items")
        return articles
    except Exception as e:
        logger.error(f"SCRAPE [{source_name}] Error: {str(e)}")
        return []

def fetch_google_news(source_name, source_config):
    """Fetch news from Google News API (via NewsAPI.org)"""
    if not GOOGLE_NEWS_API_KEY:
        logger.warning("GOOGLE_NEWS_API_KEY not set")
        return []

    try:
        articles = []
        keywords = source_config.get("keywords", [])

        for keyword in keywords:
            # Use simpler, more targeted queries for better results
            api_url = "https://newsapi.org/v2/everything"
            params = {
                "q": keyword,
                "apiKey": GOOGLE_NEWS_API_KEY,
                "pageSize": 10,
                "sortBy": "publishedAt",
                "language": "en"
            }

            response = requests.get(api_url, params=params, timeout=10)

            if response.status_code == 200:
                data = response.json()
                for article in data.get("articles", []):
                    article_obj = {
                        "title": article.get("title", ""),
                        "summary": article.get("description", "")[:200],
                        "source": source_name,
                        "category": source_config.get("category", ""),
                        "published": article.get("publishedAt", datetime.now().isoformat()),
                        "url": article.get("url", "")
                    }
                    articles.append(article_obj)
            elif response.status_code == 401:
                logger.error(f"GOOGLE_NEWS [{source_name}] API Key invalid (401)")
            elif response.status_code == 429:
                logger.warning(f"GOOGLE_NEWS [{source_name}] Rate limited (429)")
            else:
                logger.error(f"GOOGLE_NEWS [{source_name}] HTTP {response.status_code}")

        logger.info(f"GOOGLE_NEWS [{source_name}] → {len(articles)} items")
        return articles
    except Exception as e:
        logger.error(f"GOOGLE_NEWS [{source_name}] Error: {str(e)}")
        return []

def fetch_all():
    """Fetch news from all sources"""
    all_articles = []

    for source_name, source_config in SOURCES.items():
        if source_config["type"] == "rss":
            articles = fetch_rss(source_name, source_config)
        elif source_config["type"] == "scrape":
            articles = fetch_scrape(source_name, source_config)
        elif source_config["type"] == "google_news":
            articles = fetch_google_news(source_name, source_config)
        else:
            articles = []

        all_articles.extend(articles)

    return all_articles

def refresh_data():
    """Refresh all news data and save to file"""
    logger.info("Starting news refresh...")
    articles = fetch_all()

    # Remove duplicates based on title
    seen_titles = set()
    unique_articles = []
    for article in articles:
        if article["title"] not in seen_titles:
            seen_titles.add(article["title"])
            unique_articles.append(article)

    # Save to file
    try:
        with open(DATA_FILE, 'w') as f:
            json.dump(unique_articles, f, indent=2)
        logger.info(f"Saved {len(unique_articles)} articles to {DATA_FILE}")
    except Exception as e:
        logger.error(f"Error saving data: {str(e)}")

# Initialize scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(refresh_data, 'interval', minutes=30)
scheduler.start()

# Shut down the scheduler when exiting the app
atexit.register(lambda: scheduler.shutdown())

# API Endpoints
@app.route('/api/news', methods=['GET'])
def get_news():
    """Get all news articles with optional filtering"""
    try:
        with open(DATA_FILE, 'r') as f:
            articles = json.load(f)
    except:
        articles = []

    # Apply filters
    category = request.args.get('category', 'all')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    search = request.args.get('search', '').lower()

    filtered = articles

    if category != 'all':
        filtered = [a for a in filtered if a.get('category') == category]

    if start_date:
        start_dt = datetime.fromisoformat(start_date)
        filtered = [a for a in filtered if datetime.fromisoformat(a['published']) >= start_dt]

    if end_date:
        end_dt = datetime.fromisoformat(end_date)
        filtered = [a for a in filtered if datetime.fromisoformat(a['published']) <= end_dt]

    if search:
        filtered = [a for a in filtered if search in a['title'].lower() or search in a['summary'].lower()]

    return jsonify(filtered)

@app.route('/api/refresh', methods=['POST'])
def trigger_refresh():
    """Manually trigger a refresh"""
    refresh_data()
    return jsonify({"status": "refresh_started"})

@app.route('/api/status', methods=['GET'])
def get_status():
    """Get app status"""
    try:
        with open(DATA_FILE, 'r') as f:
            articles = json.load(f)

        categories = {}
        for article in articles:
            cat = article.get('category', 'unknown')
            categories[cat] = categories.get(cat, 0) + 1

        return jsonify({
            "status": "running",
            "total_articles": len(articles),
            "by_category": categories
        })
    except:
        return jsonify({"status": "error"}), 500

# Serve frontend
@app.route('/')
def index():
    """Serve the HTML frontend"""
    return app.send_static_file('index.html')

@app.route('/<path:path>')
def static_files(path):
    """Serve static files"""
    return app.send_static_file(path)

# Startup - refresh data on app start
if __name__ != '__main__':
    # This runs when deployed (not in debug mode)
    with app.app_context():
        refresh_data()
else:
    # This runs in debug mode - also refresh on startup
    with app.app_context():
        refresh_data()

if __name__ == '__main__':
    app.run(debug=True)
