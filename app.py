import os
import sys
import time
import logging
import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any, Tuple
from contextlib import contextmanager
from urllib.parse import urlparse, parse_qs, unquote

import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

import feedparser
import requests
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from bs4 import BeautifulSoup

# ------------------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------------------
LOG = logging.getLogger("quantbrief")
LOG.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
if not LOG.handlers:
    LOG.addHandler(handler)

# ------------------------------------------------------------------------------
# Config / Environment
# ------------------------------------------------------------------------------
APP = FastAPI(title="Quantbrief Stock News Aggregator")
app = APP  # for uvicorn

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    LOG.warning("DATABASE_URL not set - database features disabled")

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "changeme-admin-token")

# Email configuration
def _first(*vals) -> Optional[str]:
    for v in vals:
        if v:
            return v
    return None

SMTP_HOST = _first(os.getenv("MAILGUN_SMTP_SERVER"), os.getenv("SMTP_HOST"))
SMTP_PORT = int(_first(os.getenv("MAILGUN_SMTP_PORT"), os.getenv("SMTP_PORT"), "587"))
SMTP_USERNAME = _first(os.getenv("MAILGUN_SMTP_LOGIN"), os.getenv("SMTP_USERNAME"))
SMTP_PASSWORD = _first(os.getenv("MAILGUN_SMTP_PASSWORD"), os.getenv("SMTP_PASSWORD"))
SMTP_STARTTLS = os.getenv("SMTP_STARTTLS", "1") not in ("0", "false", "False", "")

EMAIL_FROM = _first(os.getenv("MAILGUN_FROM"), os.getenv("EMAIL_FROM"), SMTP_USERNAME)
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
DIGEST_TO = _first(os.getenv("DIGEST_TO"), ADMIN_EMAIL)

DEFAULT_RETAIN_DAYS = int(os.getenv("DEFAULT_RETAIN_DAYS", "90"))

# Quality filtering keywords
SPAM_DOMAINS = {
    "marketbeat.com", "www.marketbeat.com",
    "newser.com", "www.newser.com",
    "khodrobank.com", "www.khodrobank.com",
    "خودرو بانک"  # Arabic version of khodrobank
}

QUALITY_DOMAINS = {
    "reuters.com", "bloomberg.com", "wsj.com", "ft.com",
    "barrons.com", "cnbc.com", "marketwatch.com",
    "yahoo.com/finance", "finance.yahoo.com",
    "businesswire.com", "prnewswire.com", "globenewswire.com"
}

# ------------------------------------------------------------------------------
# Stock configurations
# ------------------------------------------------------------------------------
STOCK_FEEDS = {
    "TLN": [
        {
            "url": "https://news.google.com/rss/search?q=Talen+Energy+OR+TLN+when:7d&hl=en-US&gl=US&ceid=US:en",
            "name": "Google News: Talen Energy (7 days)"
        },
        {
            "url": "https://finance.yahoo.com/rss/headline?s=TLN",
            "name": "Yahoo Finance: TLN Headlines"
        },
        {
            "url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=TLN&region=US&lang=en-US",
            "name": "Yahoo Finance: TLN News"
        }
    ]
}

# ------------------------------------------------------------------------------
# Database helpers
# ------------------------------------------------------------------------------
@contextmanager
def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not configured")
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def ensure_schema():
    """Initialize database schema - works with clean schema"""
    # This function now does minimal work since we have a clean schema
    with db() as conn:
        with conn.cursor() as cur:
            # Just ensure the essential indexes exist
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_found_url_hash ON found_url(url_hash);
                CREATE INDEX IF NOT EXISTS idx_found_url_ticker_quality ON found_url(ticker, quality_score DESC);
                CREATE INDEX IF NOT EXISTS idx_found_url_published ON found_url(published_at DESC);
                CREATE INDEX IF NOT EXISTS idx_found_url_digest ON found_url(sent_in_digest, found_at DESC);
                CREATE INDEX IF NOT EXISTS idx_found_url_feed_foundat ON found_url(feed_id, found_at DESC);
            """)

def upsert_feed(url: str, name: str, ticker: str, retain_days: int = 90) -> int:
    """Insert or update a feed source"""
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO source_feed (url, name, ticker, retain_days, active)
            VALUES (%s, %s, %s, %s, TRUE)
            ON CONFLICT (url) DO UPDATE
            SET name = EXCLUDED.name,
                ticker = EXCLUDED.ticker,
                retain_days = EXCLUDED.retain_days,
                active = TRUE
            RETURNING id;
        """, (url, name, ticker, retain_days))
        return cur.fetchone()["id"]

def list_active_feeds() -> List[Dict]:
    """Get all active feeds"""
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT id, url, name, ticker, retain_days
            FROM source_feed
            WHERE active = TRUE
            ORDER BY ticker, id
        """)
        return list(cur.fetchall())

# ------------------------------------------------------------------------------
# URL Resolution and Quality Scoring
# ------------------------------------------------------------------------------
def resolve_google_news_url(url: str) -> Tuple[str, str]:
    """Resolve Google News redirect URL to actual article URL"""
    try:
        # Extract actual URL from Google News redirect
        if "news.google.com" in url and "/articles/" in url:
            response = requests.get(url, timeout=10, allow_redirects=True)
            return response.url, urlparse(response.url).netloc
        
        # For direct Google redirect URLs
        if "google.com/url" in url:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            if 'url' in params:
                actual_url = params['url'][0]
                return actual_url, urlparse(actual_url).netloc
            elif 'q' in params:
                actual_url = params['q'][0]
                return actual_url, urlparse(actual_url).netloc
        
        # Already a direct URL
        return url, urlparse(url).netloc
    except Exception as e:
        LOG.warning(f"Failed to resolve URL {url}: {e}")
        return url, urlparse(url).netloc

def calculate_quality_score(
    title: str, 
    domain: str, 
    ticker: str,
    description: str = ""
) -> float:
    """Calculate article quality score (0-100)"""
    score = 50.0  # Base score
    
    # Check for spam in title, domain, or description
    spam_indicators = [
        "marketbeat", "newser", "khodrobank", "خودرو بانک"
    ]
    
    content_to_check = f"{title} {domain} {description}".lower()
    if any(spam in content_to_check for spam in spam_indicators):
        return 0.0  # Auto-reject spam content
    
    # Domain quality
    if domain in SPAM_DOMAINS:
        return 0.0  # Auto-reject spam domains
    
    if domain in QUALITY_DOMAINS:
        score += 30
    elif any(q in domain for q in ["reuters", "bloomberg", "wsj", "ft", "cnbc"]):
        score += 25
    
    # Title relevance
    if ticker in (title or "").upper():
        score += 10
    if "Talen" in (title or ""):
        score += 10
    
    # Negative signals
    spam_keywords = ["sponsored", "advertisement", "promoted", "partner content"]
    if any(kw in (title or "").lower() for kw in spam_keywords):
        score -= 30
    
    # Length and substance
    if len(title or "") > 20:
        score += 5
    if len(description or "") > 50:
        score += 5
    
    return max(0, min(100, score))

def get_url_hash(url: str) -> str:
    """Generate hash for URL deduplication"""
    # Normalize URL for better dedup
    url_lower = url.lower()
    # Remove common tracking parameters
    url_clean = re.sub(r'[?&](utm_|ref=|source=).*', '', url_lower)
    return hashlib.md5(url_clean.encode()).hexdigest()

# ------------------------------------------------------------------------------
# Feed Processing
# ------------------------------------------------------------------------------
def parse_datetime(candidate) -> Optional[datetime]:
    """Parse various datetime formats"""
    if not candidate:
        return None
    if isinstance(candidate, datetime):
        return candidate if candidate.tzinfo else candidate.replace(tzinfo=timezone.utc)
    
    # Handle struct_time from feedparser
    if hasattr(candidate, "tm_year"):
        try:
            return datetime.fromtimestamp(time.mktime(candidate), tz=timezone.utc)
        except:
            pass
    
    # Try ISO format
    try:
        return datetime.fromisoformat(str(candidate))
    except:
        return None

def ingest_feed(feed: Dict) -> Dict[str, int]:
    """Process a single feed and store articles"""
    stats = {"processed": 0, "inserted": 0, "duplicates": 0, "low_quality": 0}
    
    try:
        parsed = feedparser.parse(feed["url"])
        LOG.info(f"Processing {feed['name']}: {len(parsed.entries)} entries")
        
        for entry in parsed.entries:
            stats["processed"] += 1
            
            # Get URL and resolve if needed
            original_url = getattr(entry, "link", None)
            if not original_url:
                continue
            
            resolved_url, domain = resolve_google_news_url(original_url)
            url_hash = get_url_hash(resolved_url)
            
            # Extract metadata with proper defaults
            title = getattr(entry, "title", "") or "No Title"
            description = getattr(entry, "summary", "")[:500] if hasattr(entry, "summary") else ""
            
            # Calculate quality score
            quality_score = calculate_quality_score(
                title, domain, feed["ticker"], description
            )
            
            if quality_score < 20:
                stats["low_quality"] += 1
                LOG.debug(f"Skipping low quality: {title} (score: {quality_score})")
                continue
            
            # Get publication date
            published_at = None
            if hasattr(entry, "published_parsed"):
                published_at = parse_datetime(entry.published_parsed)
            elif hasattr(entry, "updated_parsed"):
                published_at = parse_datetime(entry.updated_parsed)
            
            # Insert to database - each insert gets its own transaction
            try:
                with db() as conn, conn.cursor() as cur:
                    # Check if already exists first
                    cur.execute("SELECT id FROM found_url WHERE url_hash = %s", (url_hash,))
                    if cur.fetchone():
                        stats["duplicates"] += 1
                        continue
                    
                    # Insert new record
                    cur.execute("""
                        INSERT INTO found_url (
                            url, resolved_url, url_hash, title, description,
                            feed_id, ticker, domain, quality_score, published_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                    """, (
                        original_url, resolved_url, url_hash, title, description,
                        feed["id"], feed["ticker"], domain, quality_score, published_at
                    ))
                    
                    if cur.fetchone():
                        stats["inserted"] += 1
                        LOG.debug(f"Inserted: {title[:50]}...")
                        
            except Exception as e:
                LOG.error(f"Database insert error for '{title[:50]}': {e}")
                # Continue processing other articles instead of failing completely
                continue
                
    except Exception as e:
        LOG.error(f"Feed processing error for {feed['name']}: {e}")
    
    return stats

# ------------------------------------------------------------------------------
# Email Digest
# ------------------------------------------------------------------------------
def build_digest_html(articles_by_ticker: Dict[str, List[Dict]], period_days: int) -> str:
    """Build HTML email digest - compact format"""
    html = [
        "<html><body style='font-family: Arial, sans-serif; font-size: 12px;'>",
        f"<h2>Quantbrief Stock Digest - Last {period_days} Days</h2>",
        f"<p style='color: #666; margin-bottom: 20px;'>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} UTC</p>"
    ]
    
    for ticker, articles in articles_by_ticker.items():
        html.append(f"<h3 style='color: #1a73e8; margin: 15px 0 5px 0;'>{ticker}</h3>")
        
        if not articles:
            html.append("<p style='color: #999;'>No quality articles found for this period.</p>")
            continue
        
        for article in articles[:100]:  # Limit to top 100 per stock
            pub_date = article["published_at"].strftime("%m/%d %H:%M") if article["published_at"] else "N/A"
            
            # Clean up title - remove source suffixes and normalize
            title = article["title"] or "No Title"
            
            # Remove common suffixes that indicate source
            suffixes_to_remove = [
                " - MarketBeat", " - Newser", " - TipRanks", " - MSN", 
                " - The Daily Item", " - MarketScreener", " - Seeking Alpha",
                " - simplywall.st", " - Investopedia", " - خودرو بانک"
            ]
            
            for suffix in suffixes_to_remove:
                if title.endswith(suffix):
                    title = title[:-len(suffix)].strip()
            
            # Remove $TLN ticker references that clutter titles
            title = re.sub(r'\s*\$TLN\s*-?\s*', ' ', title)
            title = re.sub(r'\s+', ' ', title).strip()
            
            # Get first sentence from description and clean it up
            first_sentence = ""
            if article.get("description"):
                desc = article["description"].strip()
                
                # For Google News articles, clean up the messy descriptions
                if "news.google.com" in (article.get("url", "") or ""):
                    # Remove everything after the first domain name (like " TipRanks", " MSN", etc.)
                    desc = re.sub(r'\s+[A-Z][a-zA-Z]*\.?
            
            # Clean up domain name for consistency
            domain = article["domain"] or "unknown"
            domain = domain.replace("www.", "")
            
            html.append(f"""<div style='margin: 2px 0;'><a href='{article["resolved_url"] or article["url"]}' style='color: #1a73e8; text-decoration: none; font-weight: bold;'>{title}</a> | {domain} | {pub_date} | Score: {article["quality_score"]:.0f} | {first_sentence}</div>""")
    
    html.append("""
        <hr style='margin-top: 20px; border: 1px solid #e0e0e0;'>
        <p style='color: #999; font-size: 10px;'>
            This digest includes articles with quality scores above 20. Higher scores indicate more reputable sources and relevant content.
        </p>
        </body></html>
    """)
    
    return "".join(html)

def fetch_digest_articles(hours: int = 24) -> Dict[str, List[Dict]]:
    """Fetch articles for digest grouped by ticker"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT 
                f.url, f.resolved_url, f.title, f.description,
                f.ticker, f.domain, f.quality_score, f.published_at,
                f.found_at
            FROM found_url f
            WHERE f.found_at >= %s
                AND f.quality_score >= 20
                AND NOT f.sent_in_digest
            ORDER BY f.ticker, f.quality_score DESC, f.published_at DESC
        """, (cutoff,))
        
        articles_by_ticker = {}
        for row in cur.fetchall():
            ticker = row["ticker"] or "UNKNOWN"
            if ticker not in articles_by_ticker:
                articles_by_ticker[ticker] = []
            articles_by_ticker[ticker].append(dict(row))
        
        # Mark articles as sent
        cur.execute("""
            UPDATE found_url
            SET sent_in_digest = TRUE
            WHERE found_at >= %s AND quality_score >= 20
        """, (cutoff,))
        
    return articles_by_ticker

def send_email(subject: str, html_body: str, to: str = None):
    """Send email via SMTP"""
    if not all([SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, EMAIL_FROM]):
        LOG.error("SMTP not fully configured")
        return False
    
    try:
        recipient = to or DIGEST_TO
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = EMAIL_FROM
        msg["To"] = recipient
        
        # Add plain text version
        text_body = "Please view this email in HTML format."
        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))
        
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            if SMTP_STARTTLS:
                server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [recipient], msg.as_string())
        
        LOG.info(f"Email sent to {recipient}")
        return True
        
    except Exception as e:
        LOG.error(f"Email send failed: {e}")
        return False

# ------------------------------------------------------------------------------
# Auth Middleware
# ------------------------------------------------------------------------------
def require_admin(request: Request):
    """Verify admin token"""
    token = request.headers.get("x-admin-token") or \
            request.headers.get("authorization", "").replace("Bearer ", "")
    
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

# ------------------------------------------------------------------------------
# API Routes
# ------------------------------------------------------------------------------
@APP.get("/")
def root():
    return {"status": "ok", "service": "Quantbrief Stock News Aggregator"}

@APP.post("/admin/init")
def admin_init(request: Request):
    """Initialize database and seed feeds"""
    require_admin(request)
    ensure_schema()
    
    results = []
    for ticker, feeds in STOCK_FEEDS.items():
        for feed_config in feeds:
            feed_id = upsert_feed(
                url=feed_config["url"],
                name=feed_config["name"],
                ticker=ticker,
                retain_days=DEFAULT_RETAIN_DAYS
            )
            results.append({"ticker": ticker, "feed": feed_config["name"], "id": feed_id})
    
    return {"status": "initialized", "feeds": results}

@APP.post("/cron/ingest")
def cron_ingest(
    request: Request,
    minutes: int = Query(default=1440, description="Time window in minutes")
):
    """Ingest articles from all feeds"""
    require_admin(request)
    ensure_schema()
    
    feeds = list_active_feeds()
    total_stats = {"feeds_processed": 0, "total_inserted": 0, "total_duplicates": 0}
    
    for feed in feeds:
        stats = ingest_feed(feed)
        total_stats["feeds_processed"] += 1
        total_stats["total_inserted"] += stats["inserted"]
        total_stats["total_duplicates"] += stats["duplicates"]
        
        LOG.info(f"Feed {feed['name']}: {stats}")
    
    # Clean old articles
    cutoff = datetime.now(timezone.utc) - timedelta(days=DEFAULT_RETAIN_DAYS)
    with db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM found_url WHERE found_at < %s", (cutoff,))
        deleted = cur.rowcount
    
    total_stats["old_articles_deleted"] = deleted
    return total_stats

@APP.post("/cron/digest")
def cron_digest(
    request: Request,
    minutes: int = Query(default=1440, description="Time window in minutes")
):
    """Generate and send email digest"""
    require_admin(request)
    ensure_schema()
    
    hours = minutes / 60
    days = int(hours / 24) if hours >= 24 else 0
    period_label = f"{days} days" if days > 0 else f"{hours:.0f} hours"
    
    articles = fetch_digest_articles(hours=hours)
    total_articles = sum(len(arts) for arts in articles.values())
    
    if total_articles == 0:
        return {
            "status": "no_articles",
            "message": f"No new quality articles found in the last {period_label}"
        }
    
    html = build_digest_html(articles, days if days > 0 else 1)
    
    subject = f"Stock Digest: {', '.join(articles.keys())} - {total_articles} articles"
    success = send_email(subject, html)
    
    # Log digest history
    if success:
        with db() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO digest_history (recipient, article_count, tickers)
                VALUES (%s, %s, %s)
            """, (DIGEST_TO, total_articles, list(articles.keys())))
    
    return {
        "status": "sent" if success else "failed",
        "articles": total_articles,
        "tickers": list(articles.keys()),
        "recipient": DIGEST_TO
    }

@APP.post("/admin/force-digest")
def force_digest(request: Request):
    """Force digest with existing articles (for testing)"""
    require_admin(request)
    
    with db() as conn, conn.cursor() as cur:
        # Get ALL recent articles regardless of sent_in_digest status (no limit)
        cur.execute("""
            SELECT 
                f.url, f.resolved_url, f.title, f.description,
                f.ticker, f.domain, f.quality_score, f.published_at,
                f.found_at
            FROM found_url f
            WHERE f.found_at >= NOW() - INTERVAL '7 days'
                AND f.quality_score >= 20
            ORDER BY f.ticker, f.quality_score DESC, f.published_at DESC
        """)
        
        articles_by_ticker = {}
        for row in cur.fetchall():
            ticker = row["ticker"] or "UNKNOWN"
            if ticker not in articles_by_ticker:
                articles_by_ticker[ticker] = []
            articles_by_ticker[ticker].append(dict(row))
    
    total_articles = sum(len(arts) for arts in articles_by_ticker.values())
    
    if total_articles == 0:
        return {"status": "no_articles", "message": "No articles found in database"}
    
    html = build_digest_html(articles_by_ticker, 7)
    subject = f"FULL Stock Digest: {', '.join(articles_by_ticker.keys())} - {total_articles} articles"
    success = send_email(subject, html)
    
    return {
        "status": "sent" if success else "failed",
        "articles": total_articles,
        "tickers": list(articles_by_ticker.keys()),
        "recipient": DIGEST_TO
    }

@APP.post("/admin/test-email")
def test_email(request: Request):
    """Send test email"""
    require_admin(request)
    
    test_html = """
    <html><body>
        <h2>Quantbrief Test Email</h2>
        <p>Your email configuration is working correctly!</p>
        <p>Time: {}</p>
    </body></html>
    """.format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    
    success = send_email("Quantbrief Test Email", test_html)
    return {"status": "sent" if success else "failed", "recipient": DIGEST_TO}

@APP.get("/admin/stats")
def get_stats(request: Request):
    """Get system statistics"""
    require_admin(request)
    ensure_schema()
    
    with db() as conn, conn.cursor() as cur:
        # Article stats
        cur.execute("""
            SELECT 
                COUNT(*) as total_articles,
                COUNT(DISTINCT ticker) as tickers,
                COUNT(DISTINCT domain) as domains,
                AVG(quality_score) as avg_quality,
                MAX(published_at) as latest_article
            FROM found_url
            WHERE found_at > NOW() - INTERVAL '7 days'
        """)
        stats = dict(cur.fetchone())
        
        # Top domains
        cur.execute("""
            SELECT domain, COUNT(*) as count, AVG(quality_score) as avg_score
            FROM found_url
            WHERE found_at > NOW() - INTERVAL '7 days'
            GROUP BY domain
            ORDER BY count DESC
            LIMIT 10
        """)
        stats["top_domains"] = list(cur.fetchall())
        
        # Articles by ticker
        cur.execute("""
            SELECT ticker, COUNT(*) as count, AVG(quality_score) as avg_score
            FROM found_url
            WHERE found_at > NOW() - INTERVAL '7 days'
            GROUP BY ticker
            ORDER BY ticker
        """)
        stats["by_ticker"] = list(cur.fetchall())
    
    return stats

@APP.post("/admin/reset-digest-flags")
def reset_digest_flags(request: Request):
    """Reset sent_in_digest flags for testing"""
    require_admin(request)
    
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE found_url SET sent_in_digest = FALSE")
        count = cur.rowcount
    
    return {"status": "reset", "articles_reset": count}

# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run(APP, host="0.0.0.0", port=port), '', desc)
                    # Remove trailing source names in various formats
                    desc = re.sub(r'\s+(TipRanks|MSN|MarketScreener|The Daily Item|Seeking Alpha)\.?
            
            # Clean up domain name for consistency
            domain = article["domain"] or "unknown"
            domain = domain.replace("www.", "")
            
            html.append(f"""<div style='margin: 2px 0;'><a href='{article["resolved_url"] or article["url"]}' style='color: #1a73e8; text-decoration: none; font-weight: bold;'>{title}</a> | {domain} | {pub_date} | Score: {article["quality_score"]:.0f} | {first_sentence}</div>""")
    
    html.append("""
        <hr style='margin-top: 20px; border: 1px solid #e0e0e0;'>
        <p style='color: #999; font-size: 10px;'>
            This digest includes articles with quality scores above 20. Higher scores indicate more reputable sources and relevant content.
        </p>
        </body></html>
    """)
    
    return "".join(html)

def fetch_digest_articles(hours: int = 24) -> Dict[str, List[Dict]]:
    """Fetch articles for digest grouped by ticker"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT 
                f.url, f.resolved_url, f.title, f.description,
                f.ticker, f.domain, f.quality_score, f.published_at,
                f.found_at
            FROM found_url f
            WHERE f.found_at >= %s
                AND f.quality_score >= 20
                AND NOT f.sent_in_digest
            ORDER BY f.ticker, f.quality_score DESC, f.published_at DESC
        """, (cutoff,))
        
        articles_by_ticker = {}
        for row in cur.fetchall():
            ticker = row["ticker"] or "UNKNOWN"
            if ticker not in articles_by_ticker:
                articles_by_ticker[ticker] = []
            articles_by_ticker[ticker].append(dict(row))
        
        # Mark articles as sent
        cur.execute("""
            UPDATE found_url
            SET sent_in_digest = TRUE
            WHERE found_at >= %s AND quality_score >= 20
        """, (cutoff,))
        
    return articles_by_ticker

def send_email(subject: str, html_body: str, to: str = None):
    """Send email via SMTP"""
    if not all([SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, EMAIL_FROM]):
        LOG.error("SMTP not fully configured")
        return False
    
    try:
        recipient = to or DIGEST_TO
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = EMAIL_FROM
        msg["To"] = recipient
        
        # Add plain text version
        text_body = "Please view this email in HTML format."
        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))
        
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            if SMTP_STARTTLS:
                server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [recipient], msg.as_string())
        
        LOG.info(f"Email sent to {recipient}")
        return True
        
    except Exception as e:
        LOG.error(f"Email send failed: {e}")
        return False

# ------------------------------------------------------------------------------
# Auth Middleware
# ------------------------------------------------------------------------------
def require_admin(request: Request):
    """Verify admin token"""
    token = request.headers.get("x-admin-token") or \
            request.headers.get("authorization", "").replace("Bearer ", "")
    
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

# ------------------------------------------------------------------------------
# API Routes
# ------------------------------------------------------------------------------
@APP.get("/")
def root():
    return {"status": "ok", "service": "Quantbrief Stock News Aggregator"}

@APP.post("/admin/init")
def admin_init(request: Request):
    """Initialize database and seed feeds"""
    require_admin(request)
    ensure_schema()
    
    results = []
    for ticker, feeds in STOCK_FEEDS.items():
        for feed_config in feeds:
            feed_id = upsert_feed(
                url=feed_config["url"],
                name=feed_config["name"],
                ticker=ticker,
                retain_days=DEFAULT_RETAIN_DAYS
            )
            results.append({"ticker": ticker, "feed": feed_config["name"], "id": feed_id})
    
    return {"status": "initialized", "feeds": results}

@APP.post("/cron/ingest")
def cron_ingest(
    request: Request,
    minutes: int = Query(default=1440, description="Time window in minutes")
):
    """Ingest articles from all feeds"""
    require_admin(request)
    ensure_schema()
    
    feeds = list_active_feeds()
    total_stats = {"feeds_processed": 0, "total_inserted": 0, "total_duplicates": 0}
    
    for feed in feeds:
        stats = ingest_feed(feed)
        total_stats["feeds_processed"] += 1
        total_stats["total_inserted"] += stats["inserted"]
        total_stats["total_duplicates"] += stats["duplicates"]
        
        LOG.info(f"Feed {feed['name']}: {stats}")
    
    # Clean old articles
    cutoff = datetime.now(timezone.utc) - timedelta(days=DEFAULT_RETAIN_DAYS)
    with db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM found_url WHERE found_at < %s", (cutoff,))
        deleted = cur.rowcount
    
    total_stats["old_articles_deleted"] = deleted
    return total_stats

@APP.post("/cron/digest")
def cron_digest(
    request: Request,
    minutes: int = Query(default=1440, description="Time window in minutes")
):
    """Generate and send email digest"""
    require_admin(request)
    ensure_schema()
    
    hours = minutes / 60
    days = int(hours / 24) if hours >= 24 else 0
    period_label = f"{days} days" if days > 0 else f"{hours:.0f} hours"
    
    articles = fetch_digest_articles(hours=hours)
    total_articles = sum(len(arts) for arts in articles.values())
    
    if total_articles == 0:
        return {
            "status": "no_articles",
            "message": f"No new quality articles found in the last {period_label}"
        }
    
    html = build_digest_html(articles, days if days > 0 else 1)
    
    subject = f"Stock Digest: {', '.join(articles.keys())} - {total_articles} articles"
    success = send_email(subject, html)
    
    # Log digest history
    if success:
        with db() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO digest_history (recipient, article_count, tickers)
                VALUES (%s, %s, %s)
            """, (DIGEST_TO, total_articles, list(articles.keys())))
    
    return {
        "status": "sent" if success else "failed",
        "articles": total_articles,
        "tickers": list(articles.keys()),
        "recipient": DIGEST_TO
    }

@APP.post("/admin/force-digest")
def force_digest(request: Request):
    """Force digest with existing articles (for testing)"""
    require_admin(request)
    
    with db() as conn, conn.cursor() as cur:
        # Get ALL recent articles regardless of sent_in_digest status (no limit)
        cur.execute("""
            SELECT 
                f.url, f.resolved_url, f.title, f.description,
                f.ticker, f.domain, f.quality_score, f.published_at,
                f.found_at
            FROM found_url f
            WHERE f.found_at >= NOW() - INTERVAL '7 days'
                AND f.quality_score >= 20
            ORDER BY f.ticker, f.quality_score DESC, f.published_at DESC
        """)
        
        articles_by_ticker = {}
        for row in cur.fetchall():
            ticker = row["ticker"] or "UNKNOWN"
            if ticker not in articles_by_ticker:
                articles_by_ticker[ticker] = []
            articles_by_ticker[ticker].append(dict(row))
    
    total_articles = sum(len(arts) for arts in articles_by_ticker.values())
    
    if total_articles == 0:
        return {"status": "no_articles", "message": "No articles found in database"}
    
    html = build_digest_html(articles_by_ticker, 7)
    subject = f"FULL Stock Digest: {', '.join(articles_by_ticker.keys())} - {total_articles} articles"
    success = send_email(subject, html)
    
    return {
        "status": "sent" if success else "failed",
        "articles": total_articles,
        "tickers": list(articles_by_ticker.keys()),
        "recipient": DIGEST_TO
    }

@APP.post("/admin/test-email")
def test_email(request: Request):
    """Send test email"""
    require_admin(request)
    
    test_html = """
    <html><body>
        <h2>Quantbrief Test Email</h2>
        <p>Your email configuration is working correctly!</p>
        <p>Time: {}</p>
    </body></html>
    """.format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    
    success = send_email("Quantbrief Test Email", test_html)
    return {"status": "sent" if success else "failed", "recipient": DIGEST_TO}

@APP.get("/admin/stats")
def get_stats(request: Request):
    """Get system statistics"""
    require_admin(request)
    ensure_schema()
    
    with db() as conn, conn.cursor() as cur:
        # Article stats
        cur.execute("""
            SELECT 
                COUNT(*) as total_articles,
                COUNT(DISTINCT ticker) as tickers,
                COUNT(DISTINCT domain) as domains,
                AVG(quality_score) as avg_quality,
                MAX(published_at) as latest_article
            FROM found_url
            WHERE found_at > NOW() - INTERVAL '7 days'
        """)
        stats = dict(cur.fetchone())
        
        # Top domains
        cur.execute("""
            SELECT domain, COUNT(*) as count, AVG(quality_score) as avg_score
            FROM found_url
            WHERE found_at > NOW() - INTERVAL '7 days'
            GROUP BY domain
            ORDER BY count DESC
            LIMIT 10
        """)
        stats["top_domains"] = list(cur.fetchall())
        
        # Articles by ticker
        cur.execute("""
            SELECT ticker, COUNT(*) as count, AVG(quality_score) as avg_score
            FROM found_url
            WHERE found_at > NOW() - INTERVAL '7 days'
            GROUP BY ticker
            ORDER BY ticker
        """)
        stats["by_ticker"] = list(cur.fetchall())
    
    return stats

@APP.post("/admin/reset-digest-flags")
def reset_digest_flags(request: Request):
    """Reset sent_in_digest flags for testing"""
    require_admin(request)
    
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE found_url SET sent_in_digest = FALSE")
        count = cur.rowcount
    
    return {"status": "reset", "articles_reset": count}

# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run(APP, host="0.0.0.0", port=port), '', desc)
                    # If description is just a source name or very short, skip it
                    if len(desc) < 20 or desc.lower() in ['tipranks', 'msn', 'marketscreener', 'seeking alpha']:
                        first_sentence = ""
                    else:
                        first_sentence = desc + "." if not desc.endswith('.') else desc
                else:
                    # For other sources (like Yahoo), use normal processing
                    # Remove common prefixes from news aggregators
                    desc = re.sub(r'^[^:]+:\s*--\s*', '', desc)  # Remove "Source: -- " prefixes
                    desc = re.sub(r'^[^\.]+\.com\s*--\s*', '', desc)  # Remove "domain.com -- " prefixes
                    
                    # Find first sentence (look for . followed by space or end of string)
                    sentences = re.split(r'\.(?:\s|$)', desc)
                    if sentences and sentences[0] and len(sentences[0]) > 10:
                        first_sentence = sentences[0].strip()
                        if not first_sentence.endswith('.'):
                            first_sentence += '.'
            
            # Clean up domain name for consistency
            domain = article["domain"] or "unknown"
            domain = domain.replace("www.", "")
            
            html.append(f"""<div style='margin: 2px 0;'><a href='{article["resolved_url"] or article["url"]}' style='color: #1a73e8; text-decoration: none; font-weight: bold;'>{title}</a> | {domain} | {pub_date} | Score: {article["quality_score"]:.0f} | {first_sentence}</div>""")
    
    html.append("""
        <hr style='margin-top: 20px; border: 1px solid #e0e0e0;'>
        <p style='color: #999; font-size: 10px;'>
            This digest includes articles with quality scores above 20. Higher scores indicate more reputable sources and relevant content.
        </p>
        </body></html>
    """)
    
    return "".join(html)

def fetch_digest_articles(hours: int = 24) -> Dict[str, List[Dict]]:
    """Fetch articles for digest grouped by ticker"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT 
                f.url, f.resolved_url, f.title, f.description,
                f.ticker, f.domain, f.quality_score, f.published_at,
                f.found_at
            FROM found_url f
            WHERE f.found_at >= %s
                AND f.quality_score >= 20
                AND NOT f.sent_in_digest
            ORDER BY f.ticker, f.quality_score DESC, f.published_at DESC
        """, (cutoff,))
        
        articles_by_ticker = {}
        for row in cur.fetchall():
            ticker = row["ticker"] or "UNKNOWN"
            if ticker not in articles_by_ticker:
                articles_by_ticker[ticker] = []
            articles_by_ticker[ticker].append(dict(row))
        
        # Mark articles as sent
        cur.execute("""
            UPDATE found_url
            SET sent_in_digest = TRUE
            WHERE found_at >= %s AND quality_score >= 20
        """, (cutoff,))
        
    return articles_by_ticker

def send_email(subject: str, html_body: str, to: str = None):
    """Send email via SMTP"""
    if not all([SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, EMAIL_FROM]):
        LOG.error("SMTP not fully configured")
        return False
    
    try:
        recipient = to or DIGEST_TO
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = EMAIL_FROM
        msg["To"] = recipient
        
        # Add plain text version
        text_body = "Please view this email in HTML format."
        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))
        
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            if SMTP_STARTTLS:
                server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [recipient], msg.as_string())
        
        LOG.info(f"Email sent to {recipient}")
        return True
        
    except Exception as e:
        LOG.error(f"Email send failed: {e}")
        return False

# ------------------------------------------------------------------------------
# Auth Middleware
# ------------------------------------------------------------------------------
def require_admin(request: Request):
    """Verify admin token"""
    token = request.headers.get("x-admin-token") or \
            request.headers.get("authorization", "").replace("Bearer ", "")
    
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

# ------------------------------------------------------------------------------
# API Routes
# ------------------------------------------------------------------------------
@APP.get("/")
def root():
    return {"status": "ok", "service": "Quantbrief Stock News Aggregator"}

@APP.post("/admin/init")
def admin_init(request: Request):
    """Initialize database and seed feeds"""
    require_admin(request)
    ensure_schema()
    
    results = []
    for ticker, feeds in STOCK_FEEDS.items():
        for feed_config in feeds:
            feed_id = upsert_feed(
                url=feed_config["url"],
                name=feed_config["name"],
                ticker=ticker,
                retain_days=DEFAULT_RETAIN_DAYS
            )
            results.append({"ticker": ticker, "feed": feed_config["name"], "id": feed_id})
    
    return {"status": "initialized", "feeds": results}

@APP.post("/cron/ingest")
def cron_ingest(
    request: Request,
    minutes: int = Query(default=1440, description="Time window in minutes")
):
    """Ingest articles from all feeds"""
    require_admin(request)
    ensure_schema()
    
    feeds = list_active_feeds()
    total_stats = {"feeds_processed": 0, "total_inserted": 0, "total_duplicates": 0}
    
    for feed in feeds:
        stats = ingest_feed(feed)
        total_stats["feeds_processed"] += 1
        total_stats["total_inserted"] += stats["inserted"]
        total_stats["total_duplicates"] += stats["duplicates"]
        
        LOG.info(f"Feed {feed['name']}: {stats}")
    
    # Clean old articles
    cutoff = datetime.now(timezone.utc) - timedelta(days=DEFAULT_RETAIN_DAYS)
    with db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM found_url WHERE found_at < %s", (cutoff,))
        deleted = cur.rowcount
    
    total_stats["old_articles_deleted"] = deleted
    return total_stats

@APP.post("/cron/digest")
def cron_digest(
    request: Request,
    minutes: int = Query(default=1440, description="Time window in minutes")
):
    """Generate and send email digest"""
    require_admin(request)
    ensure_schema()
    
    hours = minutes / 60
    days = int(hours / 24) if hours >= 24 else 0
    period_label = f"{days} days" if days > 0 else f"{hours:.0f} hours"
    
    articles = fetch_digest_articles(hours=hours)
    total_articles = sum(len(arts) for arts in articles.values())
    
    if total_articles == 0:
        return {
            "status": "no_articles",
            "message": f"No new quality articles found in the last {period_label}"
        }
    
    html = build_digest_html(articles, days if days > 0 else 1)
    
    subject = f"Stock Digest: {', '.join(articles.keys())} - {total_articles} articles"
    success = send_email(subject, html)
    
    # Log digest history
    if success:
        with db() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO digest_history (recipient, article_count, tickers)
                VALUES (%s, %s, %s)
            """, (DIGEST_TO, total_articles, list(articles.keys())))
    
    return {
        "status": "sent" if success else "failed",
        "articles": total_articles,
        "tickers": list(articles.keys()),
        "recipient": DIGEST_TO
    }

@APP.post("/admin/force-digest")
def force_digest(request: Request):
    """Force digest with existing articles (for testing)"""
    require_admin(request)
    
    with db() as conn, conn.cursor() as cur:
        # Get ALL recent articles regardless of sent_in_digest status (no limit)
        cur.execute("""
            SELECT 
                f.url, f.resolved_url, f.title, f.description,
                f.ticker, f.domain, f.quality_score, f.published_at,
                f.found_at
            FROM found_url f
            WHERE f.found_at >= NOW() - INTERVAL '7 days'
                AND f.quality_score >= 20
            ORDER BY f.ticker, f.quality_score DESC, f.published_at DESC
        """)
        
        articles_by_ticker = {}
        for row in cur.fetchall():
            ticker = row["ticker"] or "UNKNOWN"
            if ticker not in articles_by_ticker:
                articles_by_ticker[ticker] = []
            articles_by_ticker[ticker].append(dict(row))
    
    total_articles = sum(len(arts) for arts in articles_by_ticker.values())
    
    if total_articles == 0:
        return {"status": "no_articles", "message": "No articles found in database"}
    
    html = build_digest_html(articles_by_ticker, 7)
    subject = f"FULL Stock Digest: {', '.join(articles_by_ticker.keys())} - {total_articles} articles"
    success = send_email(subject, html)
    
    return {
        "status": "sent" if success else "failed",
        "articles": total_articles,
        "tickers": list(articles_by_ticker.keys()),
        "recipient": DIGEST_TO
    }

@APP.post("/admin/test-email")
def test_email(request: Request):
    """Send test email"""
    require_admin(request)
    
    test_html = """
    <html><body>
        <h2>Quantbrief Test Email</h2>
        <p>Your email configuration is working correctly!</p>
        <p>Time: {}</p>
    </body></html>
    """.format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    
    success = send_email("Quantbrief Test Email", test_html)
    return {"status": "sent" if success else "failed", "recipient": DIGEST_TO}

@APP.get("/admin/stats")
def get_stats(request: Request):
    """Get system statistics"""
    require_admin(request)
    ensure_schema()
    
    with db() as conn, conn.cursor() as cur:
        # Article stats
        cur.execute("""
            SELECT 
                COUNT(*) as total_articles,
                COUNT(DISTINCT ticker) as tickers,
                COUNT(DISTINCT domain) as domains,
                AVG(quality_score) as avg_quality,
                MAX(published_at) as latest_article
            FROM found_url
            WHERE found_at > NOW() - INTERVAL '7 days'
        """)
        stats = dict(cur.fetchone())
        
        # Top domains
        cur.execute("""
            SELECT domain, COUNT(*) as count, AVG(quality_score) as avg_score
            FROM found_url
            WHERE found_at > NOW() - INTERVAL '7 days'
            GROUP BY domain
            ORDER BY count DESC
            LIMIT 10
        """)
        stats["top_domains"] = list(cur.fetchall())
        
        # Articles by ticker
        cur.execute("""
            SELECT ticker, COUNT(*) as count, AVG(quality_score) as avg_score
            FROM found_url
            WHERE found_at > NOW() - INTERVAL '7 days'
            GROUP BY ticker
            ORDER BY ticker
        """)
        stats["by_ticker"] = list(cur.fetchall())
    
    return stats

@APP.post("/admin/reset-digest-flags")
def reset_digest_flags(request: Request):
    """Reset sent_in_digest flags for testing"""
    require_admin(request)
    
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE found_url SET sent_in_digest = FALSE")
        count = cur.rowcount
    
    return {"status": "reset", "articles_reset": count}

# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run(APP, host="0.0.0.0", port=port)
