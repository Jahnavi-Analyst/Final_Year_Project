from flask import Flask, render_template, request, redirect, jsonify, session
import os, re, smtplib, secrets
import joblib
import feedparser
import requests
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urlparse, parse_qs, urlencode, urlunparse
from textblob import TextBlob
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from email.message import EmailMessage
vader = SentimentIntensityAnalyzer()
from datetime import datetime, timedelta, timezone
from collections import Counter
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

API_KEY = os.environ.get("NEWSAPI_KEY", "8377c27efe1b4f00adc2df8fdef408a5").strip()
APP_TIMEZONE = ZoneInfo(os.environ.get("APP_TIMEZONE", "Asia/Kolkata"))
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "").strip().lower()
SMTP_SERVER = os.environ.get("SMTP_SERVER", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").strip()
SMTP_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL", SMTP_USERNAME).strip()

def fetch_news(query="technology"):
    try:
        url = "https://newsapi.org/v2/everything"

        params = {
            "q": query,
            "apiKey": API_KEY,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 10
        }

        response = requests.get(url, params=params)
        data = response.json()

        return data.get("articles", [])

    except Exception as e:
        print("ERROR fetching news:", e)
        return []

def newsapi_fetch(query=None, category=None, selected_date=None, max_results=30, source_domain_filter="", country_text=""):
    try:
        url = "https://newsapi.org/v2/everything"

        # ✅ fallback query
        if not query:
            query = CATEGORY_QUERY.get(category, "news")

        country_text = safe_text(country_text).strip()
        if country_text:
            query = f"{query} {country_text}".strip()

        params = {
            "q": query,
            "apiKey": API_KEY,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": max_results
        }

        # ✅ DATE FILTER (VERY IMPORTANT)
        if selected_date:
            from_dt, to_dt = local_day_bounds_for_api(selected_date)
            params["from"] = from_dt
            params["to"] = to_dt

        # ✅ Source filter
        if source_domain_filter:
            params["domains"] = source_domain_filter

        response = requests.get(url, params=params, timeout=15)
        data = response.json()

        articles = []
        saved_links = get_saved_links_set()

        for a in data.get("articles", []):
            articles.append(
                process_article_common(
                    title=a.get("title", ""),
                    description=a.get("description", ""),
                    content=a.get("content", ""),
                    link=a.get("url", ""),
                    source_domain=get_domain(a.get("url", "")),
                    saved_links=saved_links,
                    category=category or "general",
                    published_raw=a.get("publishedAt"),
                    image_url=a.get("urlToImage", "")
                )
            )

        if selected_date:
            articles = [
                a for a in articles
                if article_matches_date(parse_any_datetime(a.get("published_iso")), selected_date)
            ]

        return articles

    except Exception as e:
        print("NEWSAPI ERROR:", e)
        return []

from db import (
    init_db, save_article, get_saved, delete_saved, delete_saved_by_link,
    is_saved, get_saved_links_set, create_user, get_user_by_email,
    get_user_by_id, update_user_password, update_last_login, log_activity,
    get_recent_activity, get_all_users, store_password_reset_otp,
    get_valid_password_reset_otp, mark_password_reset_otp_used,
    get_recent_password_reset_requests
)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "informaxai_secret_2026")
init_db()

AUTH_ALLOWLIST = {
    "login",
    "signup",
    "forgot_password",
    "verify_reset_otp",
    "social_login",
    "logout",
    "static",
}

def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return get_user_by_id(user_id)

def is_admin_user(user):
    if not user:
        return False
    if ADMIN_EMAIL and safe_text(user["email"]).strip().lower() == ADMIN_EMAIL:
        return True
    return bool(user["is_admin"]) or int(user["id"]) == 1

def admin_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect("/login")
        if not is_admin_user(user):
            return redirect("/")
        return view_func(*args, **kwargs)
    return wrapper

def log_user_event(event_type, details=""):
    user = current_user()
    if not user:
        return
    try:
        log_activity(user["id"], event_type, details, now_local().strftime("%d-%m-%Y %I:%M %p"))
    except Exception:
        pass

def send_reset_otp_email(email, otp_code):
    if not (SMTP_SERVER and SMTP_USERNAME and SMTP_PASSWORD and SMTP_FROM_EMAIL):
        raise RuntimeError("Email delivery is not configured. Set SMTP settings in your .env file.")

    msg = EmailMessage()
    msg["Subject"] = "InformaX AI Password Reset OTP"
    msg["From"] = SMTP_FROM_EMAIL
    msg["To"] = email
    msg.set_content(
        f"Hello,\n\nYour InformaX AI password reset OTP is: {otp_code}\n\n"
        "This OTP will expire in 10 minutes.\n"
        "If you did not request this, please ignore this email."
    )

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=20) as server:
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)

@app.context_processor
def inject_user_context():
    user = current_user()
    return {
        "current_user": user,
        "current_user_name": user["name"] if user else "Guest",
        "is_logged_in": bool(user),
        "is_admin": is_admin_user(user),
    }

@app.before_request
def require_login_for_app():
    endpoint = request.endpoint or ""
    if endpoint in AUTH_ALLOWLIST or endpoint.startswith("static"):
        return None
    if current_user():
        return None
    return redirect("/login")

# ---------- Simple in-memory cache ----------
CACHE = {}
CACHE_TTL_SECONDS = 20

def get_cache(key):
    item = CACHE.get(key)
    if not item:
        return None
    saved_time, value = item
    if (datetime.now() - saved_time).total_seconds() > CACHE_TTL_SECONDS:
        CACHE.pop(key, None)
        return None
    return value

def set_cache(key, value):
    CACHE[key] = (datetime.now(), value)

@app.after_request
def add_no_cache_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

def now_local():
    return datetime.now(APP_TIMEZONE)

def today_local_date():
    return now_local().date()

def to_local_datetime(dt):
    if not dt:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(APP_TIMEZONE)
    return dt.replace(tzinfo=timezone.utc).astimezone(APP_TIMEZONE)

def parse_selected_date(selected_date):
    text = safe_text(selected_date).strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except Exception:
        return None

def local_day_bounds(selected_date=None):
    target_date = parse_selected_date(selected_date) or today_local_date()
    start_local = datetime.combine(target_date, datetime.min.time(), tzinfo=APP_TIMEZONE)
    end_local = start_local + timedelta(days=1)
    return target_date, start_local, end_local

def local_day_bounds_for_api(selected_date=None):
    _, start_local, end_local = local_day_bounds(selected_date)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc) - timedelta(seconds=1)
    return (
        start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    )

def article_matches_date(published_dt, selected_date=None):
    target_date = parse_selected_date(selected_date) or today_local_date()
    local_dt = to_local_datetime(published_dt)
    if not local_dt:
        return False
    return local_dt.date() == target_date

def build_base_context(active="home", selected_date=None):
    selected_country = session.get("selected_country", "WORLD")
    selected_source = session.get("selected_source", "")
    normalized_date = parse_selected_date(selected_date)
    date_value = normalized_date.strftime("%Y-%m-%d") if normalized_date else ""
    return {
        "active": active,
        "country_options": COUNTRY_OPTIONS,
        "source_options": SOURCE_OPTIONS,
        "selected_country": selected_country,
        "selected_source": selected_source,
        "selected_date": date_value,
        "link_date": date_value,
    }

def render_auth_page(template_name, **extra):
    context = {
        "page_error": "",
        "page_success": "",
        "prefill_name": "",
        "prefill_email": "",
    }
    context.update(extra)
    return render_template(template_name, **context)

def complete_login(user):
    session["user_id"] = user["id"]
    session["remember_me"] = True
    update_last_login(user["id"], now_local().strftime("%d-%m-%Y %I:%M %p"))
    log_activity(user["id"], "login", "User logged in", now_local().strftime("%d-%m-%Y %I:%M %p"))
    if is_admin_user(user):
        return redirect("/admin")
    return redirect("/")

# ---------- Load ML model ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_FOLDER = os.path.join(BASE_DIR, "model")
model = joblib.load(os.path.join(MODEL_FOLDER, "fake_news_model.pkl"))
vectorizer = joblib.load(os.path.join(MODEL_FOLDER, "vectorizer.pkl"))
# 🔥 ADD BELOW EXISTING ML MODEL

# ---------- NewsAPI KEY ----------
NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY", "").strip()
if not NEWSAPI_KEY:
    NEWSAPI_KEY = "8377c27efe1b4f00adc2df8fdef408a5"

STOPWORDS = {
    "the","a","an","and","or","of","to","in","on","for","with","as","at","by","from",
    "today","live","updates","update","says","said","after","before","over","into",
    "india","news","report","reports","will","may","can","how","why","what","when",
    "it","its","they","their","his","her","you","your","is","are","was","were"
}

PUBLISHER_STOPWORDS = {
    "hindu","hindustan","times","toi","ndtv","reuters","bbc","guardian","express",
    "india","today","mint","economic","tribune","telegraph","print","news","live",
    "updates","update","report","reports"
}

SUMMARY_STOPWORDS = {
    "click", "read more", "watch live", "updated", "breaking", "latest", "photos",
    "video", "subscribe", "newsletter"
}

SENSATIONAL_WORDS = {
    "shocking", "explosive", "massive", "huge", "unbelievable", "stunning", "panic",
    "chaos", "bombshell", "exposed", "viral", "dramatic", "outrage", "crisis"
}

BREAKING_KEYWORDS = {
    "breaking", "earthquake", "tsunami", "cyclone", "flood", "war", "attack",
    "explosion", "crash", "wildfire", "emergency", "evacuation", "storm"
}

COUNTRY_OPTIONS = [
    ("World", "WORLD"),
    ("India", "IN"),
    ("United States", "US"),
    ("United Kingdom", "GB"),
    ("Canada", "CA"),
    ("Australia", "AU"),
    ("UAE", "AE"),
    ("Singapore", "SG"),
    ("Japan", "JP"),
    ("Germany", "DE"),
    ("France", "FR"),
]

SOURCE_OPTIONS = [
    ("All Trusted (Default)", ""),
    ("The Hindu", "thehindu.com"),
    ("Hindustan Times", "hindustantimes.com"),
    ("Times of India", "timesofindia.indiatimes.com"),
    ("Economic Times", "economictimes.indiatimes.com"),
    ("Indian Express", "indianexpress.com"),
    ("India Today", "indiatoday.in"),
    ("NDTV", "ndtv.com"),
    ("Livemint", "livemint.com"),
    ("Business Standard", "business-standard.com"),
    ("Moneycontrol", "moneycontrol.com"),
    ("Deccan Herald", "deccanherald.com"),
    ("Reuters", "reuters.com"),
    ("BBC", "bbc.com"),
    ("Associated Press", "apnews.com"),
    ("Bloomberg", "bloomberg.com"),
    ("The Guardian", "theguardian.com"),
    ("TechCrunch", "techcrunch.com"),
    ("The Verge", "theverge.com"),
    ("Ars Technica", "arstechnica.com"),
    ("ESPN", "espn.com"),
    ("Cricbuzz", "cricbuzz.com"),
    ("ESPNcricinfo", "espncricinfo.com"),
    ("Sky Sports", "skysports.com"),
    ("ICC Cricket", "icc-cricket.com"),
    ("BCCI", "bcci.tv"),
    ("IPL", "iplt20.com"),
    ("FIFA", "fifa.com"),
    ("UEFA", "uefa.com"),
    ("Olympics", "olympics.com"),
    ("NBA", "nba.com"),
    ("NFL", "nfl.com"),
    ("MLB", "mlb.com"),
    ("NHL", "nhl.com"),
    ("Formula 1", "formula1.com"),
    ("MotoGP", "motogp.com"),
    ("ATP Tour", "atptour.com"),
    ("WTA Tennis", "wtatennis.com"),
    ("Premier League", "premierleague.com"),
]

SOURCE_SHOWCASE = {
    "Technology": [
        {"name": "techcrunch", "logo": "/static/images/TC.jpg", "badge": "TC"},
        {"name": "the verge", "logo": "/static/images/the-verge.jpg", "badge": "TV"},
        {"name": "wired", "logo": "", "badge": "WI"},
        {"name": "ars technica", "logo": "", "badge": "AT"},
        {"name": "engadget", "logo": "", "badge": "EN"},
        {"name": "zdnet", "logo": "", "badge": "ZD"},
    ],
    "Business": [
        {"name": "reuters", "logo": "/static/images/reuters.jpg", "badge": "RE"},
        {"name": "bloomberg", "logo": "/static/images/bloomberg.jpg", "badge": "BL"},
        {"name": "cnbc", "logo": "", "badge": "CN"},
        {"name": "financial times", "logo": "", "badge": "FT"},
        {"name": "forbes", "logo": "", "badge": "FO"},
        {"name": "wsj", "logo": "", "badge": "WS"},
    ],
    "World": [
        {"name": "bbc news", "logo": "/static/images/BBC.jpg", "badge": "BBC"},
        {"name": "ndtv news", "logo": "/static/images/ndtv.jpg", "badge": "ND"},
        {"name": "al jazeera", "logo": "", "badge": "AJ"},
        {"name": "guardian", "logo": "", "badge": "GU"},
        {"name": "cnn", "logo": "", "badge": "CNN"},
        {"name": "ap news", "logo": "", "badge": "AP"},
    ],
    "India": [
        {"name": "the hindu", "logo": "", "badge": "TH"},
        {"name": "hindustan times", "logo": "", "badge": "HT"},
        {"name": "indian express", "logo": "", "badge": "IE"},
        {"name": "times of india", "logo": "", "badge": "TOI"},
        {"name": "india today", "logo": "", "badge": "IT"},
        {"name": "moneycontrol", "logo": "", "badge": "MC"},
    ],
    "Sports": [
        {"name": "espn", "logo": "", "badge": "ES"},
        {"name": "sky sports", "logo": "", "badge": "SS"},
        {"name": "cricbuzz", "logo": "", "badge": "CB"},
        {"name": "sports illustrated", "logo": "", "badge": "SI"},
    ],
    "Entertainment": [
        {"name": "variety", "logo": "", "badge": "VA"},
        {"name": "hollywood reporter", "logo": "", "badge": "HR"},
        {"name": "billboard", "logo": "", "badge": "BB"},
        {"name": "rolling stone", "logo": "", "badge": "RS"},
    ],
}

SOURCE_QUERY_MAP = {
    "techcrunch.com": "techcrunch",
    "theverge.com": "the verge",
    "wired.com": "wired",
    "arstechnica.com": "ars technica",
    "engadget.com": "engadget",
    "zdnet.com": "zdnet",
    "reuters.com": "reuters",
    "bloomberg.com": "bloomberg",
    "cnbc.com": "cnbc",
    "ft.com": "financial times",
    "forbes.com": "forbes",
    "wsj.com": "wall street journal",
    "bbc.com": "bbc news",
    "ndtv.com": "ndtv",
    "aljazeera.com": "al jazeera",
    "theguardian.com": "guardian",
    "cnn.com": "cnn",
    "apnews.com": "ap news",
    "thehindu.com": "the hindu",
    "hindustantimes.com": "hindustan times",
    "indianexpress.com": "indian express",
    "timesofindia.indiatimes.com": "times of india",
    "indiatoday.in": "india today",
    "moneycontrol.com": "moneycontrol",
    "espn.com": "espn",
    "skysports.com": "sky sports",
    "cricbuzz.com": "cricbuzz",
    "si.com": "sports illustrated",
    "variety.com": "variety",
    "hollywoodreporter.com": "hollywood reporter",
    "billboard.com": "billboard",
    "rollingstone.com": "rolling stone",
}

SOURCE_ROUTE_DOMAIN_MAP = {
    "bbc news": "bbc.com",
    "bbc": "bbc.com",
    "ndtv news": "ndtv.com",
    "ndtv": "ndtv.com",
    "reuters": "reuters.com",
    "reuters business": "reuters.com",
    "bloomberg": "bloomberg.com",
    "techcrunch": "techcrunch.com",
    "the verge": "theverge.com",
    "wired": "wired.com",
    "ars technica": "arstechnica.com",
    "engadget": "engadget.com",
    "zdnet": "zdnet.com",
    "cnbc": "cnbc.com",
    "financial times": "ft.com",
    "forbes": "forbes.com",
    "wsj": "wsj.com",
    "wall street journal": "wsj.com",
    "al jazeera": "aljazeera.com",
    "guardian": "theguardian.com",
    "cnn": "cnn.com",
    "ap news": "apnews.com",
    "ap": "apnews.com",
    "the hindu": "thehindu.com",
    "hindustan times": "hindustantimes.com",
    "indian express": "indianexpress.com",
    "times of india": "timesofindia.indiatimes.com",
    "india today": "indiatoday.in",
    "moneycontrol": "moneycontrol.com",
    "espn": "espn.com",
    "sky sports": "skysports.com",
    "cricbuzz": "cricbuzz.com",
    "sports illustrated": "si.com",
    "variety": "variety.com",
    "hollywood reporter": "hollywoodreporter.com",
    "billboard": "billboard.com",
    "rolling stone": "rollingstone.com",
}

SOURCE_FEED_MAP = {
    "techcrunch.com": [
        "https://techcrunch.com/feed/"
    ],
    "theverge.com": [
        "https://www.theverge.com/rss/index.xml"
    ],
    "bbc.com": [
        "https://feeds.bbci.co.uk/news/rss.xml",
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://feeds.bbci.co.uk/news/technology/rss.xml"
    ],
    "ndtv.com": [
        "https://feeds.feedburner.com/ndtvnews-top-stories",
        "https://feeds.feedburner.com/ndtvnews-india-news",
        "https://feeds.feedburner.com/ndtvnews-world-news"
    ],
    "reuters.com": [
        "https://feeds.reuters.com/reuters/topNews",
        "https://feeds.reuters.com/reuters/worldNews",
        "https://feeds.reuters.com/reuters/technologyNews"
    ],
    "wired.com": [
        "https://www.wired.com/feed/rss"
    ],
    "engadget.com": [
        "https://www.engadget.com/rss.xml"
    ],
    "cnn.com": [
        "http://rss.cnn.com/rss/edition.rss"
    ],
    "theguardian.com": [
        "https://www.theguardian.com/world/rss",
        "https://www.theguardian.com/technology/rss"
    ],
    "aljazeera.com": [
        "https://www.aljazeera.com/xml/rss/all.xml"
    ],
    "apnews.com": [
        "https://apnews.com/hub/ap-top-news?output=amp"
    ],
    "cnbc.com": [
        "https://www.cnbc.com/id/100003114/device/rss/rss.html"
    ],
    "espn.com": [
        "https://www.espn.com/espn/rss/news"
    ],
    "variety.com": [
        "https://variety.com/feed/"
    ]
}

TRUSTED_SHOWCASE_QUERY_MAP = {
    "Technology": {"category": "technology"},
    "Business": {"category": "business"},
    "World": {"query": "world news"},
    "India": {"query": "india news"},
    "Sports": {"category": "sports"},
    "Entertainment": {"category": "entertainment"},
}

COUNTRY_NAME_TO_CODE = {
    "world": "WORLD",
    "india": "IN",
    "united states": "US",
    "usa": "US",
    "us": "US",
    "america": "US",
    "united kingdom": "GB",
    "uk": "GB",
    "britain": "GB",
    "england": "GB",
    "canada": "CA",
    "australia": "AU",
    "uae": "AE",
    "united arab emirates": "AE",
    "singapore": "SG",
    "japan": "JP",
    "germany": "DE",
    "france": "FR",
    "italy": "IT",
    "spain": "ES",
    "russia": "RU",
    "qatar": "QA",
    "saudi arabia": "SA",
    "china": "CN",
    "brazil": "BR",
    "south africa": "ZA",
    "pakistan": "PK",
    "bangladesh": "BD",
    "sri lanka": "LK",
    "nepal": "NP",
}

def typed_country_to_code(name: str) -> str:
    n = (name or "").strip().lower()
    if not n:
        return ""
    if n in COUNTRY_NAME_TO_CODE:
        return COUNTRY_NAME_TO_CODE[n]
    for k, v in COUNTRY_NAME_TO_CODE.items():
        if k in n:
            return v
    return ""

def safe_text(x):
    return x if isinstance(x, str) else ""

def clean_html(text):
    text = safe_text(text)
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    cleaned = soup.get_text(" ", strip=True)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned

def get_domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""

TRUSTED_DOMAINS = {
    "reuters.com", "apnews.com", "bbc.co.uk", "bbc.com",
    "theguardian.com", "nytimes.com", "washingtonpost.com",
    "wsj.com", "bloomberg.com", "ft.com", "economist.com",
    "time.com", "cnbc.com", "cnn.com", "aljazeera.com", "dw.com",
    "npr.org", "abcnews.go.com", "cbsnews.com", "nbcnews.com",
    "usatoday.com", "businessinsider.com", "sky.com",
    "thehindu.com", "hindustantimes.com",
    "timesofindia.indiatimes.com", "economictimes.indiatimes.com",
    "ndtv.com", "indiatoday.in", "indianexpress.com",
    "livemint.com", "moneycontrol.com", "business-standard.com",
    "financialexpress.com", "deccanherald.com", "theprint.in",
    "news18.com", "firstpost.com", "scroll.in", "thequint.com",
    "outlookindia.com", "telegraphindia.com", "freepressjournal.in",
    "tribuneindia.com", "asianetnews.com", "mathrubhumi.com",
    "manoramaonline.com", "sakshi.com", "eenadu.net",
    "timesnownews.com", "wionews.com",
    "technologyreview.com", "wired.com", "theverge.com", "arstechnica.com",
    "engadget.com", "zdnet.com", "techcrunch.com", "venturebeat.com",
    "cnet.com", "gsmarena.com",
    "espn.com", "espncricinfo.com", "cricbuzz.com",
    "skysports.com", "fifa.com", "uefa.com", "icc-cricket.com", "bcci.tv",
    "iplt20.com", "olympics.com", "nba.com", "nfl.com",
    "mlb.com", "nhl.com", "formula1.com", "motogp.com",
    "atptour.com", "wtatennis.com", "premierleague.com"
}

def is_trusted_domain(domain: str) -> bool:
    if not domain:
        return False
    if domain in TRUSTED_DOMAINS:
        return True
    return any(domain.endswith("." + td) for td in TRUSTED_DOMAINS)

PUBLISHER_TO_DOMAIN = {
    "the hindu": "thehindu.com",
    "hindustan times": "hindustantimes.com",
    "times of india": "timesofindia.indiatimes.com",
    "the times of india": "timesofindia.indiatimes.com",
    "the economic times": "economictimes.indiatimes.com",
    "economic times": "economictimes.indiatimes.com",
    "ndtv": "ndtv.com",
    "india today": "indiatoday.in",
    "the indian express": "indianexpress.com",
    "indian express": "indianexpress.com",
    "livemint": "livemint.com",
    "mint": "livemint.com",
    "business standard": "business-standard.com",
    "financial express": "financialexpress.com",
    "moneycontrol": "moneycontrol.com",
    "deccan herald": "deccanherald.com",
    "reuters": "reuters.com",
    "bbc": "bbc.com",
    "associated press": "apnews.com",
    "ap": "apnews.com",
    "the guardian": "theguardian.com",
    "bloomberg": "bloomberg.com",
    "techcrunch": "techcrunch.com",
    "the verge": "theverge.com",
    "ars technica": "arstechnica.com",
    "espn": "espn.com",
    "cricbuzz": "cricbuzz.com",
    "espncricinfo": "espncricinfo.com",
    "sky sports": "skysports.com",
    "icc": "icc-cricket.com",
    "icc cricket": "icc-cricket.com",
    "bcci": "bcci.tv",
    "ipl": "iplt20.com",
    "fifa": "fifa.com",
    "uefa": "uefa.com",
    "olympics": "olympics.com",
    "nba": "nba.com",
    "nfl": "nfl.com",
    "mlb": "mlb.com",
    "nhl": "nhl.com",
    "formula 1": "formula1.com",
    "motogp": "motogp.com",
    "atp": "atptour.com",
    "atp tour": "atptour.com",
    "wta": "wtatennis.com",
    "premier league": "premierleague.com",
}

def publisher_from_title(title: str) -> str:
    t = (title or "").strip()
    if " - " not in t:
        return ""
    return t.split(" - ")[-1].strip()

def extract_original_from_google_link(link: str) -> str:
    if not link:
        return ""
    try:
        u = urlparse(link)
        qs = parse_qs(u.query)
        for key in ("url", "q"):
            if key in qs and qs[key]:
                candidate = qs[key][0]
                if candidate.startswith("http"):
                    return candidate
    except Exception:
        return ""
    return ""

def publisher_domain(title: str, link: str) -> str:
    pub = publisher_from_title(title).lower()
    if pub:
        if pub in PUBLISHER_TO_DOMAIN:
            return PUBLISHER_TO_DOMAIN[pub]
        for k, v in PUBLISHER_TO_DOMAIN.items():
            if k in pub:
                return v

    dom = get_domain(link)
    if dom in ("news.google.com", "news.google.co.in"):
        real_url = extract_original_from_google_link(link)
        real_dom = get_domain(real_url)
        if real_dom:
            return real_dom
    return dom

def fake_signal_count(text: str) -> int:
    txt = safe_text(text).lower()
    if not txt:
        return 0

    risky_patterns = [
        r"\bshocking\b", r"\bviral\b", r"\bunbelievable\b", r"\bexposed\b",
        r"\bbombshell\b", r"\bpanic\b", r"\bchaos\b", r"\bmiracle\b",
        r"\byou won't believe\b", r"\bmust read\b", r"\bbreaking!!!\b"
    ]
    hits = 0
    for pattern in risky_patterns:
        if re.search(pattern, txt):
            hits += 1
    if txt.count("!") >= 3:
        hits += 1
    if re.search(r"\b(all caps|guaranteed|100% true)\b", txt):
        hits += 1
    return hits

def credibility_positive_signals(text: str) -> int:
    txt = safe_text(text).lower()
    if not txt:
        return 0

    positive_patterns = [
        r"\baccording to\b", r"\breport(ed|s)?\b", r"\bofficial(s)?\b",
        r"\bstatement\b", r"\bdata\b", r"\bstudy\b", r"\bresearch\b",
        r"\binterview\b", r"\banalysis\b"
    ]
    hits = 0
    for pattern in positive_patterns:
        if re.search(pattern, txt):
            hits += 1
    return hits

def credibility_adjustment(text: str, source_domain: str = "") -> float:
    txt = safe_text(text)
    adjustment = 0.0

    if is_trusted_domain(source_domain):
        adjustment += 0.06

    signals = fake_signal_count(txt)
    adjustment -= min(0.18, signals * 0.06)

    positive_signals = credibility_positive_signals(txt)
    adjustment += min(0.08, positive_signals * 0.02)

    polarity = abs(TextBlob(txt).sentiment.polarity) if txt else 0.0
    if polarity > 0.65:
        adjustment -= 0.04

    word_count = len(re.findall(r"[A-Za-z]{3,}", txt))
    if word_count < 12:
        adjustment -= 0.06
    elif word_count > 40:
        adjustment += 0.02

    return adjustment

def explain_credibility(text: str, source_domain: str, label: str, prob_real: float):
    reasons = []
    trusted = is_trusted_domain(source_domain)
    fake_signals = fake_signal_count(text)
    positive_signals = credibility_positive_signals(text)
    polarity = abs(TextBlob(safe_text(text)).sentiment.polarity) if text else 0.0

    if trusted:
        reasons.append("Source is from a trusted publisher")
    else:
        reasons.append("Source is less established or could not be verified")

    if fake_signals >= 2:
        reasons.append("Emotional or sensational language detected")
    elif positive_signals >= 2:
        reasons.append("Contains report-style or factual language")

    if prob_real <= 0.35:
        reasons.append("Low factual consistency score from the model")
    elif prob_real >= 0.65:
        reasons.append("Strong factual consistency score from the model")
    else:
        reasons.append("Mixed evidence, so the article needs manual checking")

    if polarity >= 0.65 and "Emotional or sensational language detected" not in reasons:
        reasons.append("Highly emotional tone may affect credibility")

    # Keep the explanation concise for the dashboard.
    return reasons[:3]

def detect_fake(news_text: str, source_domain: str = ""):
    text = safe_text(news_text).strip()
    if not text:
        return ("Check", 0.50)

    try:
        X = vectorizer.transform([text])
        prob_real = float(model.predict_proba(X)[0][1])
    except Exception:
        prob_real = 0.50

    prob_real = max(0.01, min(0.99, prob_real + credibility_adjustment(text, source_domain)))

    trusted = is_trusted_domain(source_domain)
    fake_signals = fake_signal_count(text)

    if trusted:
        if prob_real <= 0.22 and fake_signals >= 4:
            label = "Fake"
        elif prob_real <= 0.45 and fake_signals >= 2:
            label = "Check"
        else:
            label = "Real"
    else:
        if prob_real >= 0.62 and fake_signals <= 1:
            label = "Real"
        elif prob_real <= 0.30 and fake_signals >= 2:
            label = "Fake"
        elif prob_real <= 0.48 or fake_signals >= 2:
            label = "Check"
        else:
            label = "Real"
    return (label, prob_real)

def source_display_name(source_domain: str) -> str:
    domain = safe_text(source_domain).lower().strip()
    if not domain:
        return "Unknown"

    label_map = {domain_value: label for label, domain_value in SOURCE_OPTIONS if domain_value}
    if domain in label_map:
        return label_map[domain]

    core = domain.split(".")[0].replace("-", " ")
    return core.title()

def sentiment_label(text):
    text = safe_text(text)

    try:
        score = vader.polarity_scores(text)["compound"]

        if score > 0.05:
            return "Positive"
        elif score < -0.05:
            return "Negative"
        else:
            return "Neutral"
    except:
        return "Neutral"

def make_counts(items, key):
    pos = neg = neu = 0
    for it in items:
        v = it.get(key, "Neutral")
        if v == "Positive":
            pos += 1
        elif v == "Negative":
            neg += 1
        else:
            neu += 1
    return {"pos": pos, "neg": neg, "neu": neu}

def extract_trending_topics(articles, top_n=5):
    cleaned_titles = []
    for a in articles[:25]:
        t = safe_text(a.get("title", ""))
        if " - " in t:
            t = t.split(" - ")[0]
        cleaned_titles.append(t)

    text = " ".join(cleaned_titles)
    words = re.findall(r"[A-Za-z]{3,}", text.lower())

    freq = {}
    for w in words:
        if w in STOPWORDS or w in PUBLISHER_STOPWORDS or w.isdigit():
            continue
        freq[w] = freq.get(w, 0) + 1

    top = sorted(freq.items(), key=lambda x: x[1], reverse=True)[:top_n]
    if not top:
        return ["Markets", "Politics", "Technology", "Sports", "Health"]
    return [w.title() for w, _ in top]

def remove_duplicates(articles):
    seen = set()
    unique = []

    for a in articles:
        key = a["title"][:60].lower()
        if key not in seen:
            seen.add(key)
            unique.append(a)

    return unique

def parse_any_datetime(value):
    if not value:
        return None

    if isinstance(value, datetime):
        return value

    text = safe_text(value).strip()
    if not text:
        return None

    patterns = [
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S %z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
    ]

    for fmt in patterns:
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except Exception:
            pass

    try:
        iso_text = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso_text)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None

def format_time_ago(dt):
    if not dt:
        return "Recently"
    local_dt = to_local_datetime(dt)
    if not local_dt:
        return "Recently"
    diff = now_local() - local_dt
    secs = max(0, int(diff.total_seconds()))

    if secs < 60:
        return "Just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins} min ago" if mins == 1 else f"{mins} mins ago"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs} hr ago" if hrs == 1 else f"{hrs} hrs ago"
    days = hrs // 24
    if days < 30:
        return f"{days} day ago" if days == 1 else f"{days} days ago"
    months = days // 30
    if months < 12:
        return f"{months} month ago" if months == 1 else f"{months} months ago"
    years = months // 12
    return f"{years} year ago" if years == 1 else f"{years} years ago"

def format_published_display(dt):
    if not dt:
        return "Published: Unknown"
    local_dt = to_local_datetime(dt)
    if not local_dt:
        return "Published: Unknown"
    return "Published: " + local_dt.strftime("%a, %d %b %Y %I:%M %p")

def filter_articles_by_exact_date(articles, selected_date):
    if not selected_date:
        return articles

    return [
        a for a in articles
        if article_matches_date(parse_any_datetime(a.get("published_iso")), selected_date)
    ]

def filter_today_news(articles):
    today = datetime.utcnow().date()
    filtered = []

    for a in articles:
        dt = parse_any_datetime(a.get("published_display"))
        if dt and dt.date() == today:
            filtered.append(a)

    return filtered if filtered else articles

def make_ai_summary(title, description):
    text = f"{safe_text(title)}. {safe_text(description)}".strip()
    text = clean_html(text)
    if not text:
        return "Summary not available."

    sentences = re.split(r'(?<=[.!?])\s+', text)
    cleaned = []
    seen = set()

    for s in sentences:
        s = s.strip(" -•\n\t")
        if not s:
            continue
        if len(s) < 25:
            continue
        if any(sw in s.lower() for sw in SUMMARY_STOPWORDS):
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(s)
        if len(cleaned) == 3:
            break

    if not cleaned:
        chunk = text[:220].strip()
        return chunk + ("..." if len(text) > 220 else "")

    return " ".join(cleaned[:3])

def detect_bias(text):
    txt = safe_text(text).lower()
    if not txt:
        return "Neutral"

    sensational_hits = sum(1 for w in SENSATIONAL_WORDS if w in txt)
    polarity = TextBlob(txt).sentiment.polarity

    if sensational_hits >= 2:
        return "Sensational"
    if polarity > 0.15:
        return "Positive"
    if polarity < -0.15:
        return "Negative"
    return "Neutral"

def normalize_topic_words(text):
    words = re.findall(r"[A-Za-z]{3,}", safe_text(text).lower())
    out = []
    for w in words:
        if w in STOPWORDS or w in PUBLISHER_STOPWORDS:
            continue
        out.append(w)
    return out

def extract_article_topics(articles, top_n=8):
    freq = Counter()
    for a in articles[:30]:
        title = safe_text(a.get("title", ""))
        if " - " in title:
            title = title.split(" - ")[0]
        for w in normalize_topic_words(title):
            freq[w] += 1
    return [w.title() for w, _ in freq.most_common(top_n)]

def extract_keywords(title, description):
    text = f"{title} {description}".lower()
    words = re.findall(r"[a-zA-Z]{4,}", text)

    freq = Counter(words)
    return [w.title() for w, _ in freq.most_common(5)]

def filter_today_news(articles):
    return [
        a for a in articles
        if article_matches_date(parse_any_datetime(a.get("published_iso")))
    ]

def make_ai_summary(title, description, content=""):
    title_text = clean_html(title)
    description_text = clean_html(description)
    content_text = clean_html(content)

    parts = []
    seen_parts = set()
    for part in [title_text, description_text, content_text]:
        norm = re.sub(r"\s+", " ", safe_text(part)).strip().lower()
        if not norm or norm in seen_parts:
            continue
        seen_parts.add(norm)
        parts.append(part)
    if not parts:
        return "Summary not available."

    combined = " ".join(parts)
    combined = re.sub(r"\s+", " ", combined).strip()
    combined = re.sub(r"\[[^\]]*\]", "", combined)
    combined = re.sub(r"\b[A-Za-z0-9_-]+\.{3,}\b", "", combined)

    sentences = re.split(r"(?<=[.!?])\s+", combined)
    title_words = set(normalize_topic_words(title_text))

    freq = Counter()
    for word in normalize_topic_words(combined):
        freq[word] += 1

    ranked = []
    seen = set()
    for idx, sentence in enumerate(sentences):
        sentence = sentence.strip(" -\n\t")
        if len(sentence) < 35:
            continue
        lower_sentence = sentence.lower()
        if lower_sentence in seen:
            continue
        if any(sw in lower_sentence for sw in SUMMARY_STOPWORDS):
            continue
        words = normalize_topic_words(sentence)
        if not words:
            continue
        score = sum(freq.get(w, 0) for w in words)
        score += len(title_words.intersection(words)) * 3
        score += max(0, 2 - idx)
        ranked.append((score, idx, sentence))
        seen.add(lower_sentence)

    if not ranked:
        chunk = combined[:260].strip()
        return chunk + ("..." if len(combined) > 260 else "")

    top_sentences = sorted(sorted(ranked, key=lambda x: x[0], reverse=True)[:2], key=lambda x: x[1])
    summary = " ".join(sentence for _, _, sentence in top_sentences)
    summary = re.sub(r"\s+", " ", summary).strip()

    if len(summary) > 320:
        summary = summary[:317].rstrip() + "..."

    return summary

def build_breaking_alert(articles):
    if not articles:
        return None

    keyword_hits = 0
    topic_counter = Counter()

    for a in articles[:20]:
        title = safe_text(a.get("title", "")).lower()

        for kw in BREAKING_KEYWORDS:
            if kw in title:
                keyword_hits += 1

        for w in normalize_topic_words(title):
            topic_counter[w] += 1

    top_topic, top_count = ("", 0)
    if topic_counter:
        top_topic, top_count = topic_counter.most_common(1)[0]

    if not (keyword_hits >= 2 or top_count >= 4):
        return None

    if keyword_hits >= 3:
        level = "high"
    elif top_count >= 5:
        level = "medium"
    else:
        level = "low"

    matched = []
    matched_links = set()

    for a in articles[:20]:
        title = safe_text(a.get("title", ""))
        low_title = title.lower()
        link = a.get("link", "#")

        is_breaking_match = False

        if top_topic and top_topic in low_title:
            is_breaking_match = True
        elif any(kw in low_title for kw in BREAKING_KEYWORDS):
            is_breaking_match = True

        if is_breaking_match and link not in matched_links:
            matched.append({
                "title": title,
                "link": link,
                "published": a.get("published_display", "Published: Unknown"),
                "time_ago": a.get("time_ago", "Recently")
            })
            matched_links.add(link)

        if len(matched) == 3:
            break

    if not matched:
        return None

    return {
        "show": True,
        "title": f"Breaking Alert: {top_topic.title() if top_topic else 'Major story'} spike",
        "message": f"High activity detected across recent headlines. {top_count} similar mentions found.",
        "level": level,
        "headlines": matched,
        "headline_links": list(matched_links)
    }

def build_smart_alert(articles):
    if not articles:
        return None

    topic_counter = Counter()
    for article in articles[:30]:
        for word in normalize_topic_words(article.get("title", "")):
            topic_counter[word] += 1

    if not topic_counter:
        return None

    topic, count = topic_counter.most_common(1)[0]
    if count < 3:
        return None

    if count >= 7:
        strength = "rising rapidly"
    elif count >= 5:
        strength = "gaining attention"
    else:
        strength = "showing steady momentum"

    return {
        "topic": topic.title(),
        "count": count,
        "message": f"Trending Topic: {topic.title()} {strength}"
    }

def build_topic_popularity(articles, query=""):
    query = safe_text(query).strip()
    topics = extract_article_topics(articles, top_n=8)
    topic_counter = Counter()

    for a in articles[:30]:
        title = safe_text(a.get("title", "")).lower()
        for w in normalize_topic_words(title):
            topic_counter[w] += 1

    if query:
        q_words = normalize_topic_words(query)
        hits = sum(topic_counter.get(w, 0) for w in q_words)
        if hits >= 5:
            status = "🔥 Trending"
        elif hits >= 2:
            status = "➖ Stable"
        else:
            status = "📉 Declining"
        return {"status": status, "score": hits, "topics": topics}

    total_mentions = sum(topic_counter.values())
    if total_mentions >= 45:
        status = "🔥 Trending"
    elif total_mentions >= 20:
        status = "➖ Stable"
    else:
        status = "📉 Declining"
    return {"status": status, "score": total_mentions, "topics": topics}

def extract_keywords_from_saved():
    rows = get_saved()
    freq = Counter()
    for row in rows[:20]:
        title = safe_text(row["title"])
        if " - " in title:
            title = title.split(" - ")[0]
        for w in normalize_topic_words(title):
            freq[w] += 1
    return [w for w, _ in freq.most_common(10)]

def build_ai_recommendations(articles, category=""):
    search_terms = session.get("search_terms", [])
    search_freq = Counter()

    for term in search_terms:
        for w in normalize_topic_words(term):
            search_freq[w] += 1

    saved_freq = Counter(extract_keywords_from_saved())
    click_freq = Counter(session.get("clicked_topics", []))
    merged = Counter()
    merged.update(search_freq)
    merged.update(saved_freq)
    merged.update(click_freq)

    recommendations = [w.title() for w, _ in merged.most_common(6)]
    return recommendations

def track_category_click(cat_name: str):
    clicks = session.get("category_clicks", {})
    clicks[cat_name] = clicks.get(cat_name, 0) + 1
    session["category_clicks"] = clicks

def track_search_term(term: str):
    term = (term or "").strip()
    if not term:
        return
    searches = session.get("search_terms", [])
    searches.append(term)
    session["search_terms"] = searches[-20:]

def track_article_click(title: str = "", category: str = "", source: str = ""):
    click_count = session.get("read_more_clicks", 0)
    session["read_more_clicks"] = click_count + 1

    topic_store = session.get("clicked_topics", [])
    title_words = normalize_topic_words(title)
    topic_store.extend(title_words[:3])
    if category:
        topic_store.extend(normalize_topic_words(category)[:2])
    if source:
        topic_store.extend(normalize_topic_words(source)[:1])
    session["clicked_topics"] = topic_store[-40:]

def build_source_comparison(articles, topic=""):
    if not articles:
        return None

    chosen_topic = safe_text(topic).strip()
    if not chosen_topic:
        extracted = extract_trending_topics(articles, top_n=1)
        chosen_topic = extracted[0] if extracted else ""
    if not chosen_topic:
        return None

    topic_words = set(normalize_topic_words(chosen_topic))
    if not topic_words:
        return None

    grouped = {}
    for article in articles[:40]:
        title = safe_text(article.get("title", ""))
        description = safe_text(article.get("description", ""))
        haystack_words = set(normalize_topic_words(f"{title} {description}"))
        if not haystack_words.intersection(topic_words):
            continue
        source_name = source_display_name(article.get("source", ""))
        grouped.setdefault(source_name, []).append(article.get("headline_sentiment", "Neutral"))

    rows = []
    for source_name, sentiments in grouped.items():
        counts = Counter(sentiments)
        dominant = counts.most_common(1)[0][0] if counts else "Neutral"
        rows.append({"source": source_name, "sentiment": dominant, "sample": len(sentiments)})

    rows = sorted(rows, key=lambda x: (-x["sample"], x["source"]))[:8]
    if len(rows) < 2:
        return None
    return {"topic": chosen_topic.title(), "rows": rows}

def get_activity_summary(default_category="Technology"):
    clicks = session.get("category_clicks", {})
    most_clicked = max(clicks.items(), key=lambda x: x[1])[0] if clicks else ""

    searches = session.get("search_terms", [])
    top = [f"#{w.replace(' ', '')}" for w, _ in Counter(searches).most_common(3)]

    total_reads = session.get("read_more_clicks", 0)
    minutes = total_reads * 4
    hours = minutes // 60
    mins = minutes % 60
    reading_time = f"{hours}h {mins}m" if total_reads and hours else (f"{mins}m" if total_reads else "")

    return {
        "reading_time": reading_time,
        "most_clicked_category": most_clicked,
        "top_searched_topics": top
    }

CATEGORY_QUERY = {
    "technology": "technology",
    "business": "business",
    "health": "health",
    "sports": "sports OR cricket OR football OR tennis OR formula 1 OR olympics OR fifa OR uefa OR nba OR premier league",
    "politics": "politics",
    "entertainment": "entertainment OR celebrity OR movie OR movies OR film OR cinema OR music OR streaming OR netflix OR bollywood OR hollywood OR tollywood OR web series OR OTT",
    "disaster": "accident OR disaster OR earthquake OR tsunami OR flood OR cyclone OR fire OR explosion OR landslide",
    "climate": "climate OR climate change OR environment OR global warming OR pollution OR carbon emissions OR renewable energy OR sustainability OR conservation OR extreme weather OR wildlife OR biodiversity"
}

TRUSTED_ONLY_CATEGORIES = {"disaster"}

def get_selected_country_code() -> str:
    return session.get("selected_country", "WORLD")

def get_selected_source_domain() -> str:
    return session.get("selected_source", "")

def process_article_common(title, description, link, source_domain, saved_links, category="general", published_raw=None, image_url="", content=""):
    label, score = detect_fake(f"{title} {description}".strip(), source_domain=source_domain)
    published_dt = parse_any_datetime(published_raw)
    time_ago = format_time_ago(published_dt)
    summary = make_ai_summary(title, description, content)
    bias = detect_bias(f"{title} {description}".strip())
    published_display = format_published_display(published_dt)
    keywords = extract_keywords(title, description)
    credibility = round(score * 100, 2)
    local_published = to_local_datetime(published_dt)
    explanation_reasons = explain_credibility(f"{title} {description} {content}".strip(), source_domain, label, score)

    return {
        "title": title if title else "No title",
        "description": description,
        "link": link,
        "label": label,
        "score": score,
        "headline_sentiment": sentiment_label(title),
        "public_sentiment": sentiment_label(description) if description else sentiment_label(title),
        "is_saved": (link in saved_links),
        "category": category or "general",
        "published_at": published_dt.strftime("%d %b %Y, %I:%M %p") if published_dt else "Unknown",
        "published_iso": published_dt.isoformat() if published_dt else "",
        "published_local_date": local_published.strftime("%Y-%m-%d") if local_published else "",
        "published_display": published_display,
        "time_ago": time_ago,
        "ai_summary": summary,
        "bias": bias,
        "source": source_domain,
        "source_name": source_display_name(source_domain),
        "keywords": keywords,
        "credibility": credibility,
        "image": image_url,
        "explanation_reasons": explanation_reasons,
    }

def google_rss(query=None, category=None, max_results=30, country_code="WORLD", source_domain_filter="", country_text=""):
    cc = (country_code or "WORLD").upper()
    country_text = (country_text or "").strip()

    cache_key = f"rss::{query}::{category}::{max_results}::{cc}::{source_domain_filter}::{country_text}"
    cached = get_cache(cache_key)
    if cached is not None:
        return cached

    if query:
        q_text = query
    elif category:
        q_text = CATEGORY_QUERY.get(category, category)
    else:
        q_text = None

    if country_text and q_text:
        q_text = f"{q_text} {country_text}"
    elif country_text and not q_text:
        q_text = country_text

    if source_domain_filter:
        if q_text:
            q_text = f"{q_text} site:{source_domain_filter}"
        else:
            q_text = f"site:{source_domain_filter}"

    if cc == "WORLD":
        if q_text:
            q = quote_plus(q_text)
            url = f"https://news.google.com/rss/search?q={q}&hl=en&gl=US&ceid=US:en"
        else:
            url = "https://news.google.com/rss?hl=en&gl=US&ceid=US:en"
    else:
        hl = f"en-{cc}"
        gl = cc
        ceid = f"{cc}:en"
        if q_text:
            q = quote_plus(q_text)
            url = f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"
        else:
            url = f"https://news.google.com/rss?hl={hl}&gl={gl}&ceid={ceid}"

    feed = feedparser.parse(url)
    saved_links = get_saved_links_set()
    articles = []

    for entry in feed.entries[:max_results]:
        title = safe_text(getattr(entry, "title", ""))
        raw_summary_html = getattr(entry, "summary", "")
        summary = clean_html(raw_summary_html)
        link = safe_text(getattr(entry, "link", ""))
        src_domain = publisher_domain(title, link)

        if category in TRUSTED_ONLY_CATEGORIES and not is_trusted_domain(src_domain):
            continue

        if source_domain_filter and not (src_domain == source_domain_filter or src_domain.endswith("." + source_domain_filter)):
            continue

        published_raw = getattr(entry, "published", None) or getattr(entry, "updated", None)

        articles.append(
            process_article_common(
                title=title,
                description=summary,
                link=link,
                source_domain=src_domain,
                saved_links=saved_links,
                category=category or "general",
                published_raw=published_raw,
            )
        )

    set_cache(cache_key, articles)
    return articles

def filter_articles_by_exact_date(articles, selected_date):
    if not selected_date:
        return articles

    try:
        target_date = datetime.strptime(selected_date, "%Y-%m-%d").date()
    except Exception:
        return articles

    filtered = []
    for a in articles:
        published_text = a.get("published_at", "")
        dt = None

        # try from published_at string
        try:
            dt = datetime.strptime(published_text, "%d %b %Y, %I:%M %p")
        except Exception:
            dt = None

        # if not possible, try parsing from published_display
        if dt is None:
            pd = a.get("published_display", "").replace("Published: ", "").strip()
            try:
                dt = datetime.strptime(pd, "%a, %d %b %Y %I:%M %p")
            except Exception:
                dt = None

        if dt and dt.date() == target_date:
            filtered.append(a)

    return filtered

def fetch_daily_articles(mode="home", query=None, category=None, selected_date=None, country_code="WORLD", source_domain_filter="", country_text=""):
    target_date = parse_selected_date(selected_date) or today_local_date()
    target_date_text = target_date.strftime("%Y-%m-%d")

    collected = []

    newsapi_articles = newsapi_fetch(
        query=query,
        category=category,
        selected_date=target_date_text,
        max_results=50,
        source_domain_filter=source_domain_filter,
        country_text=country_text
    )
    collected.extend(filter_articles_by_exact_date(remove_duplicates(newsapi_articles), target_date_text))

    rss_articles = google_rss(
        query=query if mode == "search" else None,
        category=category if mode == "category" else None,
        max_results=50,
        country_code=country_code,
        source_domain_filter=source_domain_filter,
        country_text=country_text
    )
    collected.extend(filter_articles_by_exact_date(remove_duplicates(rss_articles), target_date_text))

    if category == "climate" and not collected:
        world_query = CATEGORY_QUERY.get("climate", "climate")
        climate_newsapi = newsapi_fetch(
            query=world_query,
            category="climate",
            selected_date=target_date_text,
            max_results=50,
            source_domain_filter=source_domain_filter,
            country_text=country_text
        )
        collected.extend(filter_articles_by_exact_date(remove_duplicates(climate_newsapi), target_date_text))

        climate_world_rss = google_rss(
            query=world_query,
            category=None,
            max_results=60,
            country_code="WORLD",
            source_domain_filter=source_domain_filter,
            country_text=""
        )
        collected.extend(filter_articles_by_exact_date(remove_duplicates(climate_world_rss), target_date_text))

    collected = sorted(
        remove_duplicates(collected),
        key=lambda x: parse_any_datetime(x.get("published_iso")) or datetime.min,
        reverse=True
    )
    return collected

def fetch_articles(mode="home", query=None, category=None, selected_date=None, country_code="WORLD", source_domain_filter="", country_text=""):
    if selected_date:
        return fetch_daily_articles(
            mode=mode,
            query=query,
            category=category,
            selected_date=selected_date,
            country_code=country_code,
            source_domain_filter=source_domain_filter,
            country_text=country_text
        )

    return fetch_daily_articles(
        mode=mode,
        query=query,
        category=category,
        selected_date=today_local_date().strftime("%Y-%m-%d"),
        country_code=country_code,
        source_domain_filter=source_domain_filter,
        country_text=country_text
    )

def _dominant_label(pos, neg, neu) -> str:
    if pos >= neg and pos >= neu:
        return "Mostly Positive"
    if neg >= pos and neg >= neu:
        return "Mostly Negative"
    return "Mostly Neutral"

def newsapi_sentiment_counts(topic: str, start_date: datetime, end_date: datetime, source_domain_filter: str = ""):
    if not NEWSAPI_KEY or not topic:
        return {"pos": 0, "neg": 0, "neu": 0, "sample": 0}

    cache_key = f"trend::{topic}::{start_date.strftime('%Y-%m-%d')}::{end_date.strftime('%Y-%m-%d')}::{source_domain_filter}"
    cached = get_cache(cache_key)
    if cached is not None:
        return cached

    url = "https://newsapi.org/v2/everything"
    params = {
        "q": topic,
        "from": start_date.strftime("%Y-%m-%d"),
        "to": end_date.strftime("%Y-%m-%d"),
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": 60,
        "apiKey": NEWSAPI_KEY
    }
    if source_domain_filter:
        params["domains"] = source_domain_filter

    try:
        r = requests.get(url, params=params, timeout=12)
        data = r.json()
        if data.get("status") != "ok":
            return {"pos": 0, "neg": 0, "neu": 0, "sample": 0}
    except Exception:
        return {"pos": 0, "neg": 0, "neu": 0, "sample": 0}

    pos = neg = neu = 0
    items = data.get("articles", [])[:60]
    for a in items:
        title = safe_text(a.get("title", ""))
        desc = safe_text(a.get("description", ""))
        txt = (title + " " + desc).strip()
        s = sentiment_label(txt)
        if s == "Positive":
            pos += 1
        elif s == "Negative":
            neg += 1
        else:
            neu += 1

    result = {"pos": pos, "neg": neg, "neu": neu, "sample": len(items)}
    set_cache(cache_key, result)
    return result

def build_sentiment_trend(topic: str, anchor_date: datetime, source_domain_filter: str = ""):
    topic = (topic or "").strip()
    if not topic:
        return None

    this_end = anchor_date
    this_start = this_end - timedelta(days=7)
    last_end = this_start
    last_start = last_end - timedelta(days=7)

    last_c = newsapi_sentiment_counts(topic, last_start, last_end, source_domain_filter)
    this_c = newsapi_sentiment_counts(topic, this_start, this_end, source_domain_filter)

    if last_c["sample"] == 0 and this_c["sample"] == 0:
        last_c = {"pos": 4, "neg": 2, "neu": 3, "sample": 9}
        this_c = {"pos": 5, "neg": 2, "neu": 2, "sample": 9}
    elif last_c["sample"] == 0:
        last_c = {
            "pos": max(1, this_c["pos"] - 1),
            "neg": max(1, this_c["neg"]),
            "neu": max(1, this_c["neu"]),
            "sample": max(3, this_c["sample"])
        }
    elif this_c["sample"] == 0:
        this_c = {
            "pos": max(1, last_c["pos"]),
            "neg": max(1, last_c["neg"]),
            "neu": max(1, last_c["neu"]),
            "sample": max(3, last_c["sample"])
        }

    def forecast_val(this_v, last_v):
        return int(max(1, round(this_v + (this_v - last_v) * 0.5)))

    fc_pos = forecast_val(this_c["pos"], last_c["pos"])
    fc_neg = forecast_val(this_c["neg"], last_c["neg"])
    fc_neu = forecast_val(this_c["neu"], last_c["neu"])

    last_dom = _dominant_label(last_c["pos"], last_c["neg"], last_c["neu"])
    this_dom = _dominant_label(this_c["pos"], this_c["neg"], this_c["neu"])
    fc_dom = _dominant_label(fc_pos, fc_neg, fc_neu)

    message = f"Mood changed from {last_dom} (last week) to {this_dom} (this week)." if this_dom != last_dom else f"Mood is {this_dom} in both last week and this week."

    return {
        "labels": ["Last Week", "This Week", "Next Week (Forecast)"],
        "pos": [last_c["pos"], this_c["pos"], fc_pos],
        "neg": [last_c["neg"], this_c["neg"], fc_neg],
        "neu": [last_c["neu"], this_c["neu"], fc_neu],
        "dominant": [last_dom, this_dom, fc_dom],
        "message": message,
        "note": "Forecast is an estimate based on recent change.",
        "sample": {"last": last_c["sample"], "this": this_c["sample"]}
    }

def build_dashboard(mode="home", query=None, category=None, selected_date=None):

    dashboard_cache_key = f"dashboard::{mode}::{query}::{category}::{selected_date}::{session.get('selected_country','WORLD')}::{session.get('selected_source','')}::{session.get('typed_country','')}"
    cached_dashboard = get_cache(dashboard_cache_key)
    if cached_dashboard is not None:
        return cached_dashboard

    # TITLE
    if mode == "search":
        page_title = f"Search Results: {query}" if query else "Search Results"
    elif mode == "category":
        page_title = f"{(category or '').capitalize()} News"
    else:
        page_title = "Today’s Top News"

    # FETCH
    country_code = get_selected_country_code()
    source_domain_filter = get_selected_source_domain()
    country_text = session.get("typed_country", "").strip()

    articles = fetch_articles(
        mode=mode,
        query=query,
        category=category,
        selected_date=selected_date,
        country_code=country_code,
        source_domain_filter=source_domain_filter,
        country_text=country_text
    )

    # CLEAN
    articles = remove_duplicates(articles)

    if not selected_date:
        articles = filter_today_news(articles)
    else:
        articles = filter_articles_by_exact_date(articles, selected_date)

    articles = sorted(
        articles,
        key=lambda x: parse_any_datetime(x.get("published_iso")) or datetime.min,
        reverse=True
    )

    filtered_articles = articles

    # BREAKING ALERT
    try:
        breaking_alert = build_breaking_alert(articles)
    except Exception:
        breaking_alert = None
    smart_alert = build_smart_alert(articles)

    # TEMP HIDE (ONLY ONE TIME)
    dismissed = session.pop("dismiss_breaking", False)
    if dismissed:
        breaking_alert = None

    # REMOVE BREAKING FROM MAIN LIST
    if breaking_alert and isinstance(breaking_alert, dict) and breaking_alert.get("headline_links"):
        breaking_links = set(breaking_alert.get("headline_links", []))
        remaining = [a for a in articles if a.get("link") not in breaking_links]

        if len(remaining) >= 3:
            filtered_articles = remaining

    # ALWAYS OUTSIDE
    highlights = filtered_articles[:3]
    calc_items = filtered_articles[:12]

    headline_counts = make_counts(calc_items, "headline_sentiment")
    public_counts = make_counts(calc_items, "public_sentiment")

    quick_stats = {
        "articles_read": len(calc_items),
        "fake_count": sum(1 for a in calc_items if a.get("label") == "Fake"),
        "real_count": sum(1 for a in calc_items if a.get("label") == "Real"),
        "avg_positive": int((headline_counts["pos"] / max(1, len(calc_items))) * 100),
        "saved_count": len(get_saved()),
    }

    trending_topics = extract_trending_topics(filtered_articles, top_n=5)

    # DATE
    if selected_date:
        try:
            dt = datetime.strptime(selected_date, "%Y-%m-%d")
        except Exception:
            dt = now_local().replace(tzinfo=None)
    else:
        dt = now_local().replace(tzinfo=None)

    today_text = dt.strftime("%A, %d %B %Y")
    max_date = now_local().strftime("%Y-%m-%d")

    # EXTRA
    saved_rows = get_saved()
    latest_saved = list(saved_rows[:3]) if saved_rows else []

    activity = get_activity_summary(default_category=(category.capitalize() if category else "Technology"))

    sentiment_trend = None
    if mode == "search" and query and len(query.strip()) >= 3:
        sentiment_trend = build_sentiment_trend(query, dt, source_domain_filter=source_domain_filter)

    popularity = build_topic_popularity(filtered_articles, query=query if mode == "search" else "")
    ai_recommendations = build_ai_recommendations(filtered_articles, category=category or query or "")
    source_comparison = None
    if mode == "search" and query and len(query.strip()) >= 2:
        source_comparison = build_source_comparison(filtered_articles, topic=query)

    bias_counts = Counter(a.get("bias", "Neutral") for a in calc_items)

    result = {
        "page_title": page_title,
        "articles": filtered_articles,
        "highlights": highlights,
        "headline_counts": headline_counts,
        "public_counts": public_counts,
        "quick_stats": quick_stats,
        "trending_topics": trending_topics,
        "error_msg": None if filtered_articles else "No news found.",
        "active": category if mode == "category" else mode,
        "today_text": today_text,
        "selected_date": (selected_date or now_local().strftime("%Y-%m-%d")),
        "link_date": (selected_date or now_local().strftime("%Y-%m-%d")),
        "max_date": max_date,
        "latest_saved": latest_saved,
        "activity": activity,
        "country_options": COUNTRY_OPTIONS,
        "source_options": SOURCE_OPTIONS,
        "selected_country": country_code,
        "selected_source": source_domain_filter,
        "sentiment_trend": sentiment_trend,
        "topic_query": query if mode == "search" else "",
        "breaking_alert": breaking_alert,
        "smart_alert": smart_alert,
        "topic_popularity": popularity,
        "ai_recommendations": ai_recommendations,
        "source_comparison": source_comparison,
        "bias_counts": {
            "positive": bias_counts.get("Positive", 0),
            "negative": bias_counts.get("Negative", 0),
            "neutral": bias_counts.get("Neutral", 0),
            "sensational": bias_counts.get("Sensational", 0),
        },
        "auto_refresh_seconds": 60,
    }

    set_cache(dashboard_cache_key, result)
    return result

def fetch_source_articles(query, selected_date=None):
    source_key = safe_text(query).replace("%20", " ").strip().lower()
    domain = SOURCE_ROUTE_DOMAIN_MAP.get(source_key, source_key)
    source_phrase = SOURCE_QUERY_MAP.get(domain, source_key.replace(".", " "))
    cache_key = f"source::{domain}::{selected_date or now_local().strftime('%Y-%m-%d')}"
    cached = get_cache(cache_key)
    if cached is not None:
        return cached

    from_dt, to_dt = local_day_bounds_for_api(selected_date)
    params = {
        "domains": domain,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": 100,
        "from": from_dt,
        "to": to_dt,
        "apiKey": API_KEY,
    }

    response = requests.get("https://newsapi.org/v2/everything", params=params, timeout=15)
    data = response.json()

    saved_links = get_saved_links_set()
    articles = []
    target_date_text = (parse_selected_date(selected_date) or today_local_date()).strftime("%Y-%m-%d")
    for item in data.get("articles", []):
        published_raw = item.get("publishedAt")
        published_dt = parse_any_datetime(published_raw)
        if not article_matches_date(published_dt, selected_date):
            continue

        title = safe_text(item.get("title", ""))
        description = safe_text(item.get("description", ""))
        content = safe_text(item.get("content", ""))
        link = safe_text(item.get("url", ""))
        image_url = safe_text(item.get("urlToImage", ""))

        if not title or not link:
            continue

        articles.append(
            process_article_common(
                title=title,
                description=description,
                content=content,
                link=link,
                source_domain=domain,
                saved_links=saved_links,
                category="general",
                published_raw=published_raw,
                image_url=image_url,
            )
        )

    rss_variants = []
    rss_variants.extend(google_rss(
        query=source_phrase,
        category=None,
        max_results=80,
        country_code="WORLD",
        source_domain_filter=domain,
        country_text=""
    ))
    rss_variants.extend(google_rss(
        query=None,
        category=None,
        max_results=80,
        country_code="WORLD",
        source_domain_filter=domain,
        country_text=""
    ))
    if source_phrase and source_phrase != source_key:
        rss_variants.extend(google_rss(
            query=source_key.replace(".", " "),
            category=None,
            max_results=60,
            country_code="WORLD",
            source_domain_filter=domain,
            country_text=""
        ))

    rss_articles = filter_articles_by_exact_date(remove_duplicates(rss_variants), target_date_text)
    feed_articles = source_feed_articles(domain, selected_date=target_date_text, max_results=100)

    articles.extend(rss_articles)
    articles.extend(feed_articles)
    articles = sorted(
        remove_duplicates(articles),
        key=lambda x: parse_any_datetime(x.get("published_iso")) or datetime.min,
        reverse=True
    )

    set_cache(cache_key, articles)
    return articles

def article_matches_source_domain(article, domain: str) -> bool:
    domain = safe_text(domain).strip().lower()
    if not domain:
        return False
    source = safe_text(article.get("source", "")).strip().lower()
    link = safe_text(article.get("link", "")).strip().lower()
    return (
        source == domain
        or source.endswith("." + domain)
        or domain in source
        or domain in link
    )

def build_trusted_source_sections(selected_date=None):
    target_date = selected_date or now_local().strftime("%Y-%m-%d")
    cache_key = f"trusted_showcase::{target_date}"
    cached = get_cache(cache_key)
    if cached is not None:
        return cached

    try:
        home_articles = build_dashboard(mode="home", selected_date=selected_date).get("articles", [])
    except Exception:
        home_articles = []

    sections = []

    for category_name, sources in SOURCE_SHOWCASE.items():
        query_cfg = TRUSTED_SHOWCASE_QUERY_MAP.get(category_name, {})
        preview_pool = list(home_articles)

        try:
            preview_pool.extend(
                google_rss(
                    query=query_cfg.get("query"),
                    category=query_cfg.get("category"),
                    max_results=80,
                    country_code="WORLD",
                    source_domain_filter="",
                    country_text=""
                )
            )
        except Exception:
            pass

        preview_pool = remove_duplicates(preview_pool)
        if selected_date:
            preview_pool = filter_articles_by_exact_date(preview_pool, target_date)
        else:
            preview_pool = filter_today_news(preview_pool)

        preview_pool = sorted(
            preview_pool,
            key=lambda x: parse_any_datetime(x.get("published_iso")) or datetime.min,
            reverse=True
        )

        category_headlines = []
        enriched_sources = []

        for source in sources:
            source_name = safe_text(source.get("name", "")).strip()
            source_key = source_name.lower()
            domain = SOURCE_ROUTE_DOMAIN_MAP.get(source_key, source_key)

            source_articles = [
                item for item in preview_pool
                if article_matches_source_domain(item, domain)
            ][:4]

            enriched_sources.append({
                **source,
                "route_query": source_name,
                "headline_count": len(source_articles),
                "headlines": source_articles[:2],
            })

            for item in source_articles[:2]:
                category_headlines.append({
                    "title": item.get("title", "No title"),
                    "source_name": item.get("source_name") or source_display_name(domain),
                    "published_display": item.get("published_display", "Published: Unknown"),
                    "time_ago": item.get("time_ago", ""),
                    "route_query": source_name,
                })

        seen_items = set()
        deduped_headlines = []
        for item in category_headlines:
            key = (
                safe_text(item.get("title", "")).strip().lower(),
                safe_text(item.get("source_name", "")).strip().lower(),
            )
            if key in seen_items:
                continue
            seen_items.add(key)
            deduped_headlines.append(item)

        sections.append({
            "category": category_name,
            "sources": enriched_sources,
            "headlines": deduped_headlines[:10],
        })

    set_cache(cache_key, sections)
    return sections

def source_feed_articles(domain, selected_date=None, max_results=80):
    feed_urls = SOURCE_FEED_MAP.get(domain, [])
    if not feed_urls:
        return []

    cache_key = f"sourcefeeds::{domain}::{selected_date or now_local().strftime('%Y-%m-%d')}::{max_results}"
    cached = get_cache(cache_key)
    if cached is not None:
        return cached

    saved_links = get_saved_links_set()
    collected = []

    for feed_url in feed_urls:
        try:
            feed = feedparser.parse(feed_url)
        except Exception:
            continue

        for entry in getattr(feed, "entries", [])[:max_results]:
            title = safe_text(getattr(entry, "title", ""))
            description = clean_html(getattr(entry, "summary", "") or getattr(entry, "description", ""))
            content = ""
            raw_content = getattr(entry, "content", None)
            if raw_content and isinstance(raw_content, list):
                content = clean_html(safe_text(raw_content[0].get("value", "")))
            link = safe_text(getattr(entry, "link", ""))
            published_raw = getattr(entry, "published", None) or getattr(entry, "updated", None)
            published_dt = parse_any_datetime(published_raw)

            if not title or not link:
                continue
            if not article_matches_date(published_dt, selected_date):
                continue

            collected.append(
                process_article_common(
                    title=title,
                    description=description,
                    content=content,
                    link=link,
                    source_domain=domain,
                    saved_links=saved_links,
                    category="general",
                    published_raw=published_raw,
                )
            )

    collected = sorted(
        remove_duplicates(collected),
        key=lambda x: parse_any_datetime(x.get("published_iso")) or datetime.min,
        reverse=True
    )
    set_cache(cache_key, collected)
    return collected

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect("/")

    if request.method == "POST":
        email = safe_text(request.form.get("email")).strip().lower()
        password = safe_text(request.form.get("password")).strip()
        remember = request.form.get("remember_me")

        user = get_user_by_email(email) if email else None
        if not user or not check_password_hash(user["password_hash"], password):
            return render_auth_page(
                "login.html",
                page_error="Invalid email or password.",
                prefill_email=email
            )

        session["remember_me"] = bool(remember)
        return complete_login(user)

    return render_auth_page("login.html")

@app.route("/social-login/<provider>", methods=["GET", "POST"])
def social_login(provider):
    provider = safe_text(provider).strip().lower()
    if provider not in {"google", "microsoft"}:
        return redirect("/login")

    if current_user():
        return redirect("/")

    provider_title = "Google" if provider == "google" else "Microsoft"
    provider_icon = "google" if provider == "google" else "microsoft"

    if request.method == "POST":
        name = safe_text(request.form.get("name")).strip()
        email = safe_text(request.form.get("email")).strip().lower()

        if not email:
            return render_auth_page(
                "social_login.html",
                page_error="Please enter your email to continue.",
                prefill_name=name,
                prefill_email=email,
                provider=provider,
                provider_title=provider_title,
                provider_icon=provider_icon
            )

        user = get_user_by_email(email)
        if not user:
            if not name:
                return render_auth_page(
                    "social_login.html",
                    page_error="Please enter your name to create a new account.",
                    prefill_name=name,
                    prefill_email=email,
                    provider=provider,
                    provider_title=provider_title,
                    provider_icon=provider_icon
                )
            should_be_admin = False
            if ADMIN_EMAIL and email == ADMIN_EMAIL:
                should_be_admin = True
            elif not get_all_users():
                should_be_admin = True
            user_id = create_user(
                name=name,
                email=email,
                password_hash=generate_password_hash(secrets.token_urlsafe(24)),
                created_at=datetime.now().strftime("%d-%m-%Y %I:%M %p"),
                is_admin=1 if should_be_admin else 0
            )
            log_activity(user_id, "signup", f"{provider_title} quick sign-in created account for {email}", now_local().strftime("%d-%m-%Y %I:%M %p"))
            user = get_user_by_id(user_id)
        else:
            log_activity(user["id"], "social_login", f"Signed in with {provider_title}", now_local().strftime("%d-%m-%Y %I:%M %p"))

        return complete_login(user)

    return render_auth_page(
        "social_login.html",
        provider=provider,
        provider_title=provider_title,
        provider_icon=provider_icon
    )

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user():
        return redirect("/")

    if request.method == "POST":
        name = safe_text(request.form.get("name")).strip()
        email = safe_text(request.form.get("email")).strip().lower()
        password = safe_text(request.form.get("password")).strip()
        confirm_password = safe_text(request.form.get("confirm_password")).strip()

        if not name or not email or not password or not confirm_password:
            return render_auth_page(
                "signup.html",
                page_error="Please fill all fields.",
                prefill_name=name,
                prefill_email=email
            )

        if password != confirm_password:
            return render_auth_page(
                "signup.html",
                page_error="Passwords do not match.",
                prefill_name=name,
                prefill_email=email
            )

        if len(password) < 6:
            return render_auth_page(
                "signup.html",
                page_error="Password must be at least 6 characters.",
                prefill_name=name,
                prefill_email=email
            )

        if get_user_by_email(email):
            return render_auth_page(
                "signup.html",
                page_error="An account with this email already exists.",
                prefill_name=name,
                prefill_email=email
            )

        should_be_admin = False
        if ADMIN_EMAIL and email == ADMIN_EMAIL:
            should_be_admin = True
        elif not get_all_users():
            should_be_admin = True

        user_id = create_user(
            name=name,
            email=email,
            password_hash=generate_password_hash(password),
            created_at=datetime.now().strftime("%d-%m-%Y %I:%M %p"),
            is_admin=1 if should_be_admin else 0
        )
        log_activity(user_id, "signup", f"Account created for {email}", now_local().strftime("%d-%m-%Y %I:%M %p"))
        return render_auth_page(
            "login.html",
            page_success="Account created successfully. Please log in.",
            prefill_email=email
        )

    return render_auth_page("signup.html")

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = safe_text(request.form.get("email")).strip().lower()
        if not email:
            return render_auth_page(
                "forgot_password.html",
                page_error="Please enter your registered email.",
                prefill_email=email
            )

        user = get_user_by_email(email)
        if not user:
            return render_auth_page(
                "forgot_password.html",
                page_error="No account found with that email. Please create an account first.",
                prefill_email=email
            )

        otp_code = f"{secrets.randbelow(900000) + 100000}"
        expires_at = (now_local() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        store_password_reset_otp(email, otp_code, expires_at, now_local().strftime("%d-%m-%Y %I:%M %p"))

        try:
            send_reset_otp_email(email, otp_code)
        except Exception as e:
            return render_auth_page(
                "forgot_password.html",
                page_error=f"{safe_text(str(e)) or 'Unable to send OTP email right now.'}",
                prefill_email=email
            )

        log_activity(user["id"], "password_reset_requested", f"OTP sent to {email}", now_local().strftime("%d-%m-%Y %I:%M %p"))
        return render_auth_page(
            "verify_otp.html",
            page_success="OTP sent to your email. Enter it below to reset your password.",
            prefill_email=email
        )

    return render_auth_page("forgot_password.html")

@app.route("/verify-reset-otp", methods=["GET", "POST"])
def verify_reset_otp():
    if request.method == "POST":
        email = safe_text(request.form.get("email")).strip().lower()
        otp_code = safe_text(request.form.get("otp_code")).strip()
        new_password = safe_text(request.form.get("new_password")).strip()
        confirm_password = safe_text(request.form.get("confirm_password")).strip()

        if not email or not otp_code or not new_password or not confirm_password:
            return render_auth_page(
                "verify_otp.html",
                page_error="Please fill all fields.",
                prefill_email=email
            )

        if new_password != confirm_password:
            return render_auth_page(
                "verify_otp.html",
                page_error="Passwords do not match.",
                prefill_email=email
            )

        if len(new_password) < 6:
            return render_auth_page(
                "verify_otp.html",
                page_error="Password must be at least 6 characters.",
                prefill_email=email
            )

        otp_row = get_valid_password_reset_otp(email, otp_code)
        if not otp_row:
            return render_auth_page(
                "verify_otp.html",
                page_error="Invalid OTP. Please try again.",
                prefill_email=email
            )

        expires_at = datetime.strptime(otp_row["expires_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=APP_TIMEZONE)
        if now_local() > expires_at:
            return render_auth_page(
                "verify_otp.html",
                page_error="OTP has expired. Please request a new one.",
                prefill_email=email
            )

        updated = update_user_password(email, generate_password_hash(new_password))
        if not updated:
            return render_auth_page(
                "verify_otp.html",
                page_error="No account found with that email. Please create an account first.",
                prefill_email=email
            )

        mark_password_reset_otp_used(otp_row["id"])
        user = get_user_by_email(email)
        if user:
            log_activity(user["id"], "password_reset_success", f"Password reset completed for {email}", now_local().strftime("%d-%m-%Y %I:%M %p"))

        return render_auth_page(
            "login.html",
            page_success="Password updated successfully. Please log in.",
            prefill_email=email
        )

    return render_auth_page("verify_otp.html")

@app.route("/logout")
def logout():
    user = current_user()
    if user:
        log_activity(user["id"], "logout", "User logged out", now_local().strftime("%d-%m-%Y %I:%M %p"))
    session.clear()
    return redirect("/login")

@app.route("/admin")
@admin_required
def admin_dashboard():
    all_users = get_all_users()
    users = [u for u in all_users if not is_admin_user(u)]

    all_activity_rows = get_recent_activity(120)
    activity_rows = [
        r for r in all_activity_rows
        if not (r["user_email"] and safe_text(r["user_email"]).strip().lower() == ADMIN_EMAIL)
    ]

    otp_rows = get_recent_password_reset_requests(60)

    stats = {
        "total_users": len(users),
        "admins": sum(1 for u in all_users if is_admin_user(u)),
        "recent_logins": sum(1 for r in activity_rows if safe_text(r["event_type"]) == "login"),
        "password_resets": sum(1 for r in activity_rows if "password_reset" in safe_text(r["event_type"])),
    }

    return render_template(
        "admin.html",
        users=users,
        activity_rows=activity_rows,
        otp_rows=otp_rows,
        admin_stats=stats
    )

@app.route("/")
def home():
    if request.args.get("reset") == "1":
        session.pop("last_search_topic", None)
        session["selected_country"] = "WORLD"
        session["selected_source"] = ""
        session["typed_country"] = ""
        CACHE.clear()
    selected_date = request.args.get("date", "").strip() or None
    log_user_event("page_view", "Visited home dashboard")
    data = build_dashboard(mode="home", selected_date=selected_date)
    return render_template("dashboard.html", **data)

@app.route("/category/<cat>")
def category(cat):
    selected_date = request.args.get("date", "").strip() or None

    allowed = {
        "technology", "business", "health", "sports",
        "politics", "entertainment", "disaster", "climate"
    }

    cat = (cat or "").strip().lower()
    if cat not in allowed:
        cat = "technology"

    track_category_click(cat.capitalize())
    log_user_event("category_view", f"Opened category: {cat}")
    data = build_dashboard(mode="category", category=cat, selected_date=selected_date)
    return render_template("dashboard.html", **data)

@app.route("/search", methods=["GET", "POST"])
def search():
    if request.method == "POST":
        topic = (request.form.get("topic") or "").strip()
        selected_date = (request.form.get("date") or "").strip() or None
    else:
        topic = (request.args.get("topic") or session.get("last_search_topic") or "").strip()
        selected_date = (request.args.get("date") or "").strip() or None

    if not topic:
        return redirect("/")

    session["last_search_topic"] = topic
    track_search_term(topic)
    log_user_event("search", f"Searched topic: {topic}")

    data = build_dashboard(mode="search", query=topic, selected_date=selected_date)
    return render_template("dashboard.html", **data)

@app.route("/set_date", methods=["POST"])
def set_date():
    d = (request.form.get("date") or "").strip()
    next_url = (request.form.get("next") or "/").strip()

    try:
        if d:
            datetime.strptime(d, "%Y-%m-%d")
    except Exception:
        d = ""

    parsed = urlparse(next_url)
    qs = parse_qs(parsed.query)

    if "date" in qs:
        qs.pop("date", None)

    if d:
        qs["date"] = [d]

    new_query = urlencode(qs, doseq=True)
    final_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))

    if not final_url:
        final_url = "/"

    return redirect(final_url)

@app.route("/set_filters", methods=["GET", "POST"])
def set_filters():
    data = request.form if request.method == "POST" else request.args

    country = (data.get("country") or "WORLD").strip().upper()
    source = (data.get("source") or "").strip().lower()
    next_url = (data.get("next") or "/").strip()

    typed = (data.get("country_text") or "").strip()

    if typed:
        cc = typed_country_to_code(typed)
        session["selected_country"] = cc if cc else "WORLD"
        session["typed_country"] = typed
    else:
        valid_cc = {cc for _, cc in COUNTRY_OPTIONS}
        if country not in valid_cc:
            country = "WORLD"
        session["selected_country"] = country
        session["typed_country"] = ""

    valid_sources = {d for _, d in SOURCE_OPTIONS}
    if source not in valid_sources:
        source = ""
    session["selected_source"] = source

    return redirect(next_url or "/")

@app.route("/toggle_save", methods=["POST"])
def toggle_save():
    title = (request.form.get("title") or "").strip()
    link = (request.form.get("link") or "").strip()
    label = (request.form.get("label") or "").strip()
    score = (request.form.get("score") or "0").strip()

    if not link:
        return jsonify({"ok": False, "error": "Missing link"}), 400

    try:
        score_val = float(score)
    except Exception:
        score_val = 0.0

    saved_at = datetime.now().strftime("%d-%m-%Y %I:%M %p")

    if is_saved(link):
        delete_saved_by_link(link)
        log_user_event("save_remove", f"Removed saved article: {title or link}")
        return jsonify({"ok": True, "saved": False}), 200

    save_article(title or "No title", link, label or "Real", score_val, saved_at)
    log_user_event("save_add", f"Saved article: {title or link}")
    return jsonify({"ok": True, "saved": True}), 200

@app.route("/saved")
def saved():
    rows = get_saved()
    log_user_event("saved_view", "Opened saved articles page")
    return render_template("saved.html", saved_rows=rows)

@app.route("/remove_saved/<int:article_id>", methods=["POST"])
def remove_saved(article_id):
    delete_saved(article_id)
    return redirect("/saved")

@app.route("/latest_saved_json")
def latest_saved_json():
    rows = get_saved()
    latest = rows[:3]
    return jsonify({
        "ok": True,
        "items": [{"title": r["title"], "link": r["link"]} for r in latest]
    })

@app.route("/reset_filters", methods=["POST"])
def reset_filters():
    session["selected_country"] = "WORLD"
    session["selected_source"] = ""
    session["typed_country"] = ""
    return jsonify({"ok": True})

@app.route("/refresh_news_json")
def refresh_news_json():
    selected_date = request.args.get("date", "").strip() or None
    mode = request.args.get("mode", "home").strip()
    category_name = request.args.get("category", "").strip() or None
    topic = request.args.get("topic", "").strip() or None

    data = build_dashboard(
        mode=mode if mode in {"home", "category", "search"} else "home",
        query=topic,
        category=category_name,
        selected_date=selected_date
    )

    return jsonify({
        "ok": True,
        "articles": data["articles"],
        "highlights": data["highlights"],
        "breaking_alert": data.get("breaking_alert"),
        "smart_alert": data.get("smart_alert"),
        "topic_popularity": data.get("topic_popularity"),
        "ai_recommendations": data.get("ai_recommendations"),
        "source_comparison": data.get("source_comparison"),
        "today_text": data.get("today_text"),
    })

@app.route("/track_article_click", methods=["POST"])
def track_article_click_api():
    title = (request.form.get("title") or "").strip()
    category = (request.form.get("category") or "").strip()
    source = (request.form.get("source") or "").strip()
    track_article_click(title=title, category=category, source=source)
    log_user_event("article_click", f"Read more clicked: {title} | {category} | {source}")
    return jsonify({"ok": True})

@app.route("/recommendations_json")
def recommendations_json():
    selected_date = request.args.get("date", "").strip() or None
    data = build_dashboard(mode="home", selected_date=selected_date)
    return jsonify({
        "ok": True,
        "recommendations": data.get("ai_recommendations", [])
    })

@app.route("/trusted-sources")
def trusted_sources():
    context = build_base_context(active="home")
    context["source_showcase"] = SOURCE_SHOWCASE
    log_user_event("trusted_sources_view", "Opened trusted sources page")
    return render_template("trusted_sources.html", **context)

@app.route("/source")
def source_filter():
    domain = request.args.get("domain")

    # 🔥 DO NOT TOUCH EXISTING DASHBOARD LOGIC
    data = build_dashboard(mode="home")

    # 🔥 FILTER ARTICLES PROPERLY
    filtered_articles = []
    for a in data.get("articles", []):
        link = (a.get("link") or "").lower()
        if domain and domain in link:
            filtered_articles.append(a)

    # ✅ Replace only articles
    data["articles"] = filtered_articles

    return render_template("dashboard.html", **data)

from flask import jsonify, request

from flask import jsonify, request

@app.route("/api/source-news")
def api_source_news():

    query = request.args.get("query")

    print("QUERY RECEIVED:", query)   # 🔥 DEBUG

    today = datetime.now().strftime("%Y-%m-%d")

    url = f"https://newsapi.org/v2/everything?q={query}&from={today}&to={today}&sortBy=publishedAt&pageSize=10&apiKey={API_KEY}"

    response = requests.get(url)
    data = response.json()

    print("TOTAL ARTICLES:", len(data.get("articles", [])))  # 🔥 DEBUG

    articles = []

    for a in data.get("articles", []):
        if not a.get("title"):
            continue

        articles.append({
            "title": a["title"],
            "link": a["url"],
            "ai_summary": a.get("description", "")
        })

    return jsonify({"articles": articles})

@app.route("/source/<query>")
def source_news(query):

    try:
        import re
        from datetime import datetime

        query = query.replace("%20", " ").lower().strip()

        # 🔥 DOMAIN MAPPING (important)
        domain_map = {
            "bbc news": "bbc.co.uk",
            "bbc": "bbc.co.uk",
            "ndtv news": "ndtv.com",
            "ndtv": "ndtv.com",
            "reuters": "reuters.com",
            "reuters business": "reuters.com",
            "bloomberg": "bloomberg.com",
            "techcrunch": "techcrunch.com",
            "the verge": "theverge.com"
        }

        domain = domain_map.get(query, query)

        print("DOMAIN:", domain)

        # 🔥 API CALL (more data for better results)
        url = f"https://newsapi.org/v2/everything?domains={domain}&sortBy=publishedAt&pageSize=50&apiKey={API_KEY}"

        response = requests.get(url)
        data = response.json()

        print("TOTAL ARTICLES:", len(data.get("articles", [])))

        articles = []
        seen_titles = set()

        today = datetime.now().date()

        for a in data.get("articles", []):

            title = (a.get("title") or "").strip()
            link = a.get("url") or "#"
            published = a.get("publishedAt") or ""
            raw_summary = a.get("description") or ""

            # ❌ skip empty titles
            if not title or title == "[Removed]":
                continue

            # ❌ REMOVE DUPLICATES
            key = title.lower()
            if key in seen_titles:
                continue
            seen_titles.add(key)

            # 🔥 FILTER ONLY TODAY + YESTERDAY (timezone safe)
            if published:
                try:
                    article_date = datetime.strptime(published[:10], "%Y-%m-%d").date()
                    if (today - article_date).days > 1:
                        continue
                except:
                    pass

                published = published[:10]

            # 🔥 CLEAN SUMMARY (VERY IMPORTANT FIX)
            clean = re.sub(r'<.*?>', '', raw_summary)
            clean = clean.replace("\n", " ").strip()

            # remove duplicate lines
            sentences = list(dict.fromkeys(clean.split(". ")))

            # simple human summary (2 lines)
            summary = ". ".join(sentences[:2])

            if len(summary) > 180:
                summary = summary[:180] + "..."

            if not summary:
                summary = "This news explains an important recent update in simple terms."

            articles.append({
                "title": title,
                "link": link,
                "published": published,
                "ai_summary": summary
            })

        # 🔥 FALLBACK (if nothing today)
        if not articles:
            print("⚠️ No today's news → showing latest")

            for a in data.get("articles", [])[:5]:
                articles.append({
                    "title": a.get("title", "No title"),
                    "link": a.get("url", "#"),
                    "published": (a.get("publishedAt") or "")[:10],
                    "ai_summary": a.get("description") or "Latest update from this source."
                })

        return render_template(
            "source_page.html",
            articles=articles,
            source_name=query.upper()
        )

    except Exception as e:
        print("🔥 ERROR:", e)
        return "Internal Server Error - Check Terminal"

@app.route("/save_article", methods=["POST"])
def save_article_api():

    data = request.get_json()

    title = data.get("title")
    link = data.get("link")
    summary = data.get("summary")
    label = data.get("label")

    if not link:
        return jsonify({"status": "error"}), 400

    # ✅ Use your EXISTING DB function
    if is_saved(link):
        delete_saved_by_link(link)
        return jsonify({"status": "removed"})

    save_article(title or "No Title", link, label or "Real", 0.8, datetime.now().strftime("%d-%m-%Y %I:%M %p"))

    return jsonify({"status": "saved"})

@app.route("/dismiss_breaking", methods=["POST"])
def dismiss_breaking():
    session["dismiss_breaking"] = True
    return jsonify({"ok": True})

def trusted_sources_view():
    selected_date = request.args.get("date", "").strip() or None
    context = build_base_context(active="home", selected_date=selected_date)
    context["source_showcase"] = SOURCE_SHOWCASE
    context["trusted_sections"] = build_trusted_source_sections(selected_date=selected_date)
    context["today_text"] = (parse_selected_date(selected_date) or today_local_date()).strftime("%A, %d %B %Y")
    return render_template("trusted_sources.html", **context)

def source_filter_view():
    domain = request.args.get("domain")
    selected_date = request.args.get("date", "").strip() or None
    data = build_dashboard(mode="home", selected_date=selected_date)
    if domain:
        domain = domain.lower()
        filtered_articles = [
            a for a in data.get("articles", [])
            if domain in safe_text(a.get("source", "")).lower()
        ]
        data["articles"] = filtered_articles
        data["highlights"] = filtered_articles[:3]
        data["error_msg"] = None if filtered_articles else "No news found for this source on the selected date."
    return render_template("dashboard.html", **data)

def api_source_news_view():
    query = request.args.get("query")
    selected_date = request.args.get("date", "").strip() or None
    try:
        articles = fetch_source_articles(query, selected_date=selected_date)
        return jsonify({"articles": articles})
    except Exception as e:
        print("SOURCE API ERROR:", e)
        return jsonify({"articles": []}), 500

def source_news_view(query):
    selected_date = request.args.get("date", "").strip() or None
    try:
        articles = fetch_source_articles(query, selected_date=selected_date)
        context = build_base_context(active="home", selected_date=selected_date)
        context.update({
            "articles": articles,
            "source_name": query.replace("%20", " ").upper(),
            "source_query": query,
            "today_text": (parse_selected_date(selected_date) or today_local_date()).strftime("%A, %d %B %Y"),
            "max_date": now_local().strftime("%Y-%m-%d"),
            "error_msg": None if articles else "No news found for this source on the selected date.",
        })
        return render_template("source_page.html", **context)
    except Exception as e:
        print("SOURCE PAGE ERROR:", e)
        context = build_base_context(active="home", selected_date=selected_date)
        context.update({
            "articles": [],
            "source_name": query.replace("%20", " ").upper(),
            "source_query": query,
            "today_text": (parse_selected_date(selected_date) or today_local_date()).strftime("%A, %d %B %Y"),
            "max_date": now_local().strftime("%Y-%m-%d"),
            "error_msg": "Unable to load source news right now.",
        })
        return render_template("source_page.html", **context)

app.view_functions["trusted_sources"] = trusted_sources_view
app.view_functions["source_filter"] = source_filter_view
app.view_functions["api_source_news"] = api_source_news_view
app.view_functions["source_news"] = source_news_view

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)


