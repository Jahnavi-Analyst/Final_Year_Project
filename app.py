from flask import Flask, render_template, request, redirect, jsonify, session, g
import os, re, smtplib, secrets
import math
import joblib
import feedparser
import requests
from requests.adapters import HTTPAdapter
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
from bs4 import BeautifulSoup
from urllib.parse import quote, quote_plus, urlparse, parse_qs, urlencode, urlunparse
from textblob import TextBlob
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from email.message import EmailMessage
vader = SentimentIntensityAnalyzer()
from datetime import datetime, timedelta, timezone
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, local
from time import perf_counter
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
OTP_EXPIRY_SECONDS = 120
NEWSAPI_BACKOFF_UNTIL = None

def newsapi_fetch(query=None, category=None, selected_date=None, max_results=30, source_domain_filter="", country_text=""):
    global NEWSAPI_BACKOFF_UNTIL
    try:
        if NEWSAPI_BACKOFF_UNTIL and datetime.now(APP_TIMEZONE) < NEWSAPI_BACKOFF_UNTIL:
            return []

        url = "https://newsapi.org/v2/everything"

        # ✅ fallback query
        if not query:
            query = CATEGORY_QUERY.get(category, "news")

        country_text = safe_text(country_text).strip()
        if country_text:
            query = f"{query} {country_text}".strip()

        page_size = max(1, min(int(max_results or NEWSAPI_PAGE_SIZE), NEWSAPI_PAGE_SIZE))
        params = {
            "q": query,
            "apiKey": API_KEY,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": page_size
        }

        # ✅ DATE FILTER (VERY IMPORTANT)
        if selected_date:
            from_dt, to_dt = local_day_bounds_for_api(selected_date)
            params["from"] = from_dt
            params["to"] = to_dt

        # ✅ Source filter
        if source_domain_filter:
            params["domains"] = source_domain_filter

        articles = []
        saved_links = current_saved_links()
        max_pages = max(1, min(NEWSAPI_MAX_PAGES, math.ceil(max(1, int(max_results or 1)) / NEWSAPI_PAGE_SIZE)))

        for page_number in range(1, max_pages + 1):
            page_params = dict(params)
            page_params["page"] = page_number

            response = http_get(url, params=page_params, timeout=4)
            data = response.json()
            if data.get("status") not in (None, "ok"):
                NEWSAPI_BACKOFF_UNTIL = datetime.now(APP_TIMEZONE) + timedelta(seconds=90)
                return []

            NEWSAPI_BACKOFF_UNTIL = None
            page_articles = data.get("articles", []) or []
            if not page_articles:
                break

            for a in page_articles:
                articles.append(
                    process_article_common(
                        title=a.get("title", ""),
                        description=a.get("description", ""),
                        content=a.get("content", ""),
                        link=resolve_article_url(a.get("url", "")),
                        source_domain=get_domain(resolve_article_url(a.get("url", ""))),
                        saved_links=saved_links,
                        category=category or "general",
                        published_raw=a.get("publishedAt"),
                        image_url=a.get("urlToImage", ""),
                        allow_live_summary_fetch=False,
                    )
                )

            if len(articles) >= max_results or len(page_articles) < page_size:
                break

        if selected_date:
            articles = [
                a for a in articles
                if article_matches_date(parse_any_datetime(a.get("published_iso")), selected_date)
            ]

        return articles[:max_results]

    except Exception as e:
        NEWSAPI_BACKOFF_UNTIL = datetime.now(APP_TIMEZONE) + timedelta(seconds=90)
        print("NEWSAPI ERROR:", e)
        return []

from db import (
    init_db, save_article, get_saved, delete_saved, delete_saved_by_link,
    is_saved, get_saved_links_set, create_user, get_user_by_email,
    get_user_by_id, update_user_password, update_last_login, log_activity,
    get_recent_activity, get_recent_activity_by_user, get_all_users, store_password_reset_otp,
    get_valid_password_reset_otp, get_latest_password_reset_otp, mark_password_reset_otp_used,
    get_recent_password_reset_requests, get_saved_counts_by_user,
    clear_admin_data, get_conn, deactivate_user
)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "informaxai_secret_2026")
app.permanent_session_lifetime = timedelta(days=30)
init_db()

AUTH_ALLOWLIST = {
    "login",
    "signup",
    "forgot_password",
    "verify_reset_otp",
    "reset_password_after_otp",
    "resend_reset_otp",
    "social_login",
    "logout",
    "static",
}

def current_user():
    if hasattr(g, "_current_user_loaded"):
        return getattr(g, "_current_user", None)

    user_id = session.get("user_id")
    if not user_id:
        g._current_user = None
        g._current_user_loaded = True
        return None

    g._current_user = get_user_by_id(user_id)
    g._current_user_loaded = True
    return g._current_user

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

def current_user_id():
    user = current_user()
    return int(user["id"]) if user else 0

def current_saved_links():
    uid = current_user_id()
    if not uid:
        return set()
    if hasattr(g, "_current_saved_links"):
        return g._current_saved_links
    g._current_saved_links = get_saved_links_set(uid)
    return g._current_saved_links

PASSWORD_RULE_TEXT = "Password must be at least 8 characters and include one uppercase letter, one lowercase letter, one number, and one special symbol."

def is_strong_password(password: str) -> bool:
    text = safe_text(password)
    if len(text) < 8:
        return False
    if not re.search(r"[A-Z]", text):
        return False
    if not re.search(r"[a-z]", text):
        return False
    if not re.search(r"\d", text):
        return False
    if not re.search(r"[^A-Za-z0-9]", text):
        return False
    return True

def parse_activity_time(text):
    value = safe_text(text).strip()
    if not value:
        return None
    for fmt in ("%d-%m-%Y %I:%M %p", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except Exception:
            continue
    return None

def send_reset_otp_email(email, otp_code):
    if not (SMTP_SERVER and SMTP_USERNAME and SMTP_PASSWORD and SMTP_FROM_EMAIL):
        raise RuntimeError("Email delivery is not configured. Set SMTP settings in your .env file.")

    msg = EmailMessage()
    msg["Subject"] = "InformaX AI Password Reset OTP"
    msg["From"] = SMTP_FROM_EMAIL
    msg["To"] = email
    msg.set_content(
        f"Hello,\n\nYour InformaX AI password reset OTP is: {otp_code}\n\n"
        f"This OTP will expire in {OTP_EXPIRY_SECONDS // 60} minutes.\n"
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
        "local_today_iso": now_local().strftime("%Y-%m-%d"),
        "admin_in_app_mode": bool(user and is_admin_user(user) and session.get("admin_app_mode")),
        "theme_preference": session.get("theme_preference", "system"),
    }

@app.before_request
def require_login_for_app():
    endpoint = request.endpoint or ""
    if endpoint in AUTH_ALLOWLIST or endpoint.startswith("static"):
        return None
    if current_user():
        return None
    return redirect("/login")

@app.before_request
def keep_admin_in_dashboard():
    endpoint = request.endpoint or ""
    user = current_user()
    if not user or not is_admin_user(user):
        return None
    if endpoint in AUTH_ALLOWLIST or endpoint.startswith("static"):
        return None
    if endpoint in {"admin_dashboard", "admin_open_app", "admin_back_to_dashboard"}:
        return None
    if session.get("admin_app_mode"):
        return None
    return redirect("/admin")

# ---------- Simple in-memory cache ----------
CACHE = {}
CACHE_TTL_SECONDS = 600
LIVE_CACHE_TTL_SECONDS = 20
SOURCE_FEED_CACHE_TTL_SECONDS = 30
TRUSTED_SHOWCASE_CACHE_TTL_SECONDS = 30
ARTICLE_TEXT_CACHE_TTL_SECONDS = 1800
TREND_CACHE_TTL_SECONDS = 300
NETWORK_LATENCY_SAMPLES = deque(maxlen=8)
NETWORK_PROFILE_LOCK = Lock()
FAST_NETWORK_LATENCY_SECONDS = 1.0
SLOW_NETWORK_LATENCY_SECONDS = 2.5
HTTP_POOL_CONNECTIONS = 16
HTTP_POOL_MAXSIZE = 16
DASHBOARD_SUMMARY_FETCH_LIMIT = 6
SOURCE_SUMMARY_FETCH_LIMIT = 8
DAILY_NEWS_MAX_RESULTS = 180
DAILY_RSS_MAX_RESULTS = 120
SOURCE_PAGE_MAX_RESULTS = 120
NEWSAPI_PAGE_SIZE = 100
NEWSAPI_MAX_PAGES = 3
CACHE_VERSION = "v3"
HOME_SOURCE_SCAN_MAX_WORKERS = 6
HOME_SOURCE_SCAN_PER_SOURCE = 16
THREAD_LOCAL = local()

def cache_ttl_for_key(key):
    cache_key = safe_text(key)
    if cache_key.startswith("dashboard_v3::"):
        return LIVE_CACHE_TTL_SECONDS
    if cache_key.startswith("rss::"):
        return LIVE_CACHE_TTL_SECONDS
    if cache_key.startswith("source_v3::"):
        return LIVE_CACHE_TTL_SECONDS
    if cache_key.startswith("sourcefeeds::"):
        return SOURCE_FEED_CACHE_TTL_SECONDS
    if cache_key.startswith("trusted_showcase::"):
        return TRUSTED_SHOWCASE_CACHE_TTL_SECONDS
    if cache_key.startswith("articletext::"):
        return ARTICLE_TEXT_CACHE_TTL_SECONDS
    if cache_key.startswith("trend::"):
        return TREND_CACHE_TTL_SECONDS
    return CACHE_TTL_SECONDS

def get_cache(key):
    item = CACHE.get(key)
    if not item:
        return None
    saved_time, value = item
    if (datetime.now() - saved_time).total_seconds() > cache_ttl_for_key(key):
        CACHE.pop(key, None)
        return None
    return value

def set_cache(key, value):
    CACHE[key] = (datetime.now(), value)

def record_network_latency(elapsed_seconds):
    try:
        elapsed = float(elapsed_seconds)
    except Exception:
        return
    if elapsed <= 0:
        return
    with NETWORK_PROFILE_LOCK:
        NETWORK_LATENCY_SAMPLES.append(elapsed)

def current_network_profile():
    with NETWORK_PROFILE_LOCK:
        if not NETWORK_LATENCY_SAMPLES:
            return "normal"
        avg_latency = sum(NETWORK_LATENCY_SAMPLES) / len(NETWORK_LATENCY_SAMPLES)

    if avg_latency <= FAST_NETWORK_LATENCY_SECONDS:
        return "fast"
    if avg_latency >= SLOW_NETWORK_LATENCY_SECONDS:
        return "slow"
    return "normal"

def adaptive_live_fetch_budget(requested_budget):
    requested = max(0, int(requested_budget))
    if requested <= 0:
        return 0

    profile = current_network_profile()
    if profile == "fast":
        return requested
    if profile == "slow":
        return min(requested, 2)
    return min(requested, 4)

def adaptive_fetch_workers(fetch_count):
    fetch_count = max(0, int(fetch_count))
    if fetch_count <= 0:
        return 0

    profile = current_network_profile()
    if profile == "fast":
        max_workers = 4
    elif profile == "slow":
        max_workers = 1
    else:
        max_workers = 2
    return max(1, min(max_workers, fetch_count))

def should_allow_live_summary_fetch():
    return current_network_profile() != "slow"

def get_http_session():
    session_obj = getattr(THREAD_LOCAL, "http_session", None)
    if session_obj is None:
        session_obj = requests.Session()
        adapter = HTTPAdapter(pool_connections=HTTP_POOL_CONNECTIONS, pool_maxsize=HTTP_POOL_MAXSIZE, max_retries=0)
        session_obj.mount("http://", adapter)
        session_obj.mount("https://", adapter)
        THREAD_LOCAL.http_session = session_obj
    return session_obj

def http_get(url, **kwargs):
    started_at = perf_counter()
    try:
        return get_http_session().get(url, **kwargs)
    finally:
        record_network_latency(perf_counter() - started_at)

@app.after_request
def add_no_cache_headers(response):
    if safe_text(response.mimetype).startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

def now_local():
    return datetime.now(APP_TIMEZONE)

def otp_remaining_seconds(expires_at_text):
    try:
        expires_at = datetime.strptime(safe_text(expires_at_text), "%Y-%m-%d %H:%M:%S").replace(tzinfo=APP_TIMEZONE)
        return max(0, int((expires_at - now_local()).total_seconds()))
    except Exception:
        return 0

def latest_otp_remaining_seconds(email):
    if not email:
        return 0
    otp_row = get_latest_password_reset_otp(email)
    if not otp_row:
        return 0
    return otp_remaining_seconds(otp_row["expires_at"])

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

def normalized_selected_date_text(selected_date=None):
    target_date = parse_selected_date(selected_date) or today_local_date()
    return target_date.strftime("%Y-%m-%d")

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

def build_article_placeholder_image(source_domain="", category="general", title=""):
    domain = safe_text(source_domain).replace("www.", "").strip().lower()
    if not domain:
        return ""
    return f"https://www.google.com/s2/favicons?sz=256&domain_url=https://{quote(domain)}"

MIN_FULL_ARTICLE_WORDS = 35
MAX_ARTICLE_BODY_CHARS = 2200

def normalize_article_body_text(*texts):
    pieces = []
    seen = set()

    for raw in texts:
        text = clean_html(raw)
        text = re.sub(r"\[[^\]]*\]", "", text)
        text = re.sub(r"\[\+\d+\s+chars\]", "", text, flags=re.IGNORECASE)
        text = re.sub(r"(https?://\S+)", "", text)
        text = re.sub(r"\s+", " ", text).strip(" -\n\t")
        if not text:
            continue

        normalized = normalize_source_key(text)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        pieces.append(text)

    merged = " ".join(pieces).strip()
    merged = re.sub(r"\s+", " ", merged).strip()
    if len(merged) > MAX_ARTICLE_BODY_CHARS:
        merged = merged[:MAX_ARTICLE_BODY_CHARS].rsplit(" ", 1)[0].strip()
    return merged

def has_meaningful_article_body(text, min_words=MIN_FULL_ARTICLE_WORDS):
    body = normalize_article_body_text(text)
    if len(body.split()) < min_words:
        return False

    sentence_count = sum(
        1 for sentence in re.split(r"(?<=[.!?])\s+", body)
        if len(safe_text(sentence).split()) >= 8
    )
    return sentence_count >= 2

def summarize_complete_article_text(title, description="", content="", fetched_content=""):
    full_article_body = normalize_article_body_text(content, fetched_content)
    if has_meaningful_article_body(full_article_body):
        summary = make_ai_summary(title, "", full_article_body)
        if not summary or summary == "Summary not available." or summary_needs_expansion(title, summary):
            summary = build_summary_fallback(title, "", full_article_body)
        return summary, full_article_body, True

    fallback_body = normalize_article_body_text(description, content, fetched_content)
    if fallback_body:
        summary = make_ai_summary(title, description, fallback_body)
        if not summary or summary == "Summary not available." or summary_needs_expansion(title, summary):
            summary = build_summary_fallback(title, description, fallback_body)
        return summary, fallback_body, False

    summary = make_ai_summary(title, description, content)
    if not summary or summary == "Summary not available.":
        summary = build_summary_fallback(title, description, content)
    return summary, normalize_article_body_text(content, description), False

def fetch_article_text_excerpt(link: str) -> str:
    raw_link = safe_text(link).strip()
    url = resolve_article_url(raw_link)
    if not url or looks_like_non_article_url(url):
        return ""

    cache_key = f"articletext::{CACHE_VERSION}::{raw_link or url}"
    cached = get_cache(cache_key)
    if cached is not None:
        return cached

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        }
        response = http_get(
            url,
            timeout=6,
            headers=headers,
            allow_redirects=True,
        )
        response.raise_for_status()
        active_url = safe_text(getattr(response, "url", "")).strip() or url
        active_domain = get_domain(active_url)

        if is_google_news_domain(active_domain):
            discovered_url = discover_external_article_url(raw_link or url, response)
            if discovered_url and discovered_url != active_url:
                response = http_get(
                    discovered_url,
                    timeout=6,
                    headers=headers,
                    allow_redirects=True,
                )
                response.raise_for_status()
                active_url = safe_text(getattr(response, "url", "")).strip() or discovered_url
                active_domain = get_domain(active_url)

        if is_google_news_domain(active_domain):
            return ""

        soup = BeautifulSoup(response.text, "html.parser")

        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()

        candidates = []
        for meta_name in ("og:description", "twitter:description", "description"):
            meta = soup.find("meta", attrs={"property": meta_name}) or soup.find("meta", attrs={"name": meta_name})
            if meta and meta.get("content"):
                candidates.append(clean_html(meta.get("content")))

        for selector in (
            'div[itemprop="articleBody"]',
            '[data-testid*="article"]',
            '.article-body',
            '.story-body',
            '.entry-content',
            '.post-content',
            '.news-content',
        ):
            for node in soup.select(selector)[:2]:
                text = clean_html(node.get_text(" ", strip=True))
                if len(text.split()) >= 20:
                    candidates.insert(0, text)

        body_root = soup.find("article") or soup.find("main") or soup.body
        if body_root:
            paragraphs = []
            for p in body_root.find_all("p"):
                text = clean_html(p.get_text(" ", strip=True))
                if len(text.split()) >= 8 and not looks_like_non_article_title(text):
                    paragraphs.append(text)
                if len(paragraphs) >= 12:
                    break
            if paragraphs:
                candidates.insert(0, " ".join(paragraphs))

        merged = normalize_article_body_text(*candidates)
        if merged:
            set_cache(cache_key, merged)
        return merged
    except Exception:
        return ""

def fetch_feed_with_timeout(url: str, timeout=3.0):
    try:
        response = http_get(
            safe_text(url).strip(),
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            }
        )
        response.raise_for_status()
        return feedparser.parse(response.content)
    except Exception:
        return feedparser.parse(b"")

def build_date_search_suffix(selected_date=None):
    target_date = parse_selected_date(selected_date)
    if not target_date:
        return ""
    next_date = target_date + timedelta(days=1)
    return f" after:{target_date.strftime('%Y-%m-%d')} before:{next_date.strftime('%Y-%m-%d')}"

def extract_feed_image(entry, *html_sources):
    for attr in ("media_content", "media_thumbnail"):
        raw = getattr(entry, attr, None)
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    url = safe_text(item.get("url", "")).strip()
                    if url:
                        return url

    for link_obj in getattr(entry, "links", []) or []:
        if safe_text(getattr(link_obj, "type", "")).startswith("image/"):
            url = safe_text(getattr(link_obj, "href", "")).strip()
            if url:
                return url

    for html in html_sources:
        soup = BeautifulSoup(safe_text(html), "html.parser")
        img = soup.find("img")
        if img and img.get("src"):
            return safe_text(img.get("src")).strip()

    return ""

def build_base_context(active="home", selected_date=None):
    selected_country = session.get("selected_country", "WORLD")
    selected_source = session.get("selected_source", "")
    date_value = normalized_selected_date_text(selected_date)
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
        "remember_checked": True,
    }
    context.update(extra)
    return render_template(template_name, **context)

def complete_login(user, remember=False):
    session["welcome_mode"] = "back" if safe_text(user["last_login_at"]).strip() else "welcome"
    session.permanent = bool(remember)
    session["user_id"] = user["id"]
    session["remember_me"] = bool(remember)
    session.pop("password_reset_verified_email", None)
    update_last_login(user["id"], now_local().strftime("%d-%m-%Y %I:%M %p"))
    log_activity(user["id"], "login", "User logged in", now_local().strftime("%d-%m-%Y %I:%M %p"))
    if is_admin_user(user):
        session["admin_app_mode"] = False
        response = redirect("/admin")
    else:
        session.pop("admin_app_mode", None)
        response = redirect("/")

    if remember:
        response.set_cookie("remembered_email", safe_text(user["email"]).strip().lower(), max_age=60 * 60 * 24 * 30, samesite="Lax")
    else:
        response.delete_cookie("remembered_email")
    return response

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

COUNTRY_CODE_TO_NAME = {code: label for label, code in COUNTRY_OPTIONS}

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
        {"name": "Techcrunch", "logo": "/static/images/TC.jpg", "badge": "TC"},
        {"name": "The Verge", "logo": "/static/images/the-verge.jpg", "badge": "TV"},
        {"name": "Wired", "logo": "/static/images/wired.jpg", "badge": "WI"},
        {"name": "Ars Technica", "logo": "/static/images/arstechnica.jpg", "badge": "AT"},
        {"name": "Engadget", "logo": "/static/images/Engadget_Logo.png", "badge": "EN"},
        {"name": "ZDNET", "logo": "/static/images/zdnet.jpg", "badge": "ZD"},
        {"name": "Android Police", "logo": "/static/images/android police.jpg", "badge": "AP"},
        {"name": "Mashable India", "logo": "/static/images/mashable", "badge": "MI"},

    ],
    "Business": [
        {"name": "Reuters", "logo": "/static/images/reuters.jpg", "badge": "RE"},
        {"name": "Bloomberg", "logo": "/static/images/bloomberg.jpg", "badge": "BL"},
        {"name": "CNBC", "logo": "/static/images/Cnbc.jpg", "badge": "CN"},
        {"name": "Financial Times", "logo": "/static/images/financial-times.jpg", "badge": "FT"},
        {"name": "Forbes", "logo": "/static/images/forbes.jpg", "badge": "FO"},
        {"name": "WSJ", "logo": "/static/images/wsj-logo.jpg", "badge": "WS"},
        {"name": "Economic Times", "logo": "/static/images/economic times.jpg", "badge": "ET"},
        {"name": "NDTV Profit", "logo": "/static/images/ndtv-profit.jpg", "badge": "NP"},
        {"name": "Business Standards", "logo": "/static/images/business standards.jpg", "badge": "BS"},
        {"name": "Investment Guru", "logo": "/static/images/investment guru.jpg", "badge": "IG"},
    ],
    "World": [
        {"name": "BBC News", "logo": "/static/images/BBC.jpg", "badge": "BBC"},
        {"name": "NDTV News", "logo": "/static/images/ndtv.jpg", "badge": "ND"},
        {"name": "AL jazeera", "logo": "/static/images/aljazeera.jpg", "badge": "AJ"},
        {"name": "The Guardian", "logo": "/static/images/The-Guardian.jpg", "badge": "GU"},
        {"name": "CNN", "logo": "/static/images/cnn.jpg", "badge": "CNN"},
        {"name": "AP News", "logo": "/static/images/AP.jpg", "badge": "AP"},
    ],
    "India": [
        {"name": "The Hindu", "logo": "/static/images/the hindu.jpg", "badge": "TH"},
        {"name": "Hindustan Times", "logo": "/static/images/hindustan times.jpg", "badge": "HT"},
        {"name": "Indian Express", "logo": "/static/images/the indian express.jpg", "badge": "IE"},
        {"name": "Times of India", "logo": "/static/images/the times of india.jpg", "badge": "TOI"},
        {"name": "India Today", "logo": "/static/images/india today.jpg", "badge": "IT"},
        {"name": "Money Control", "logo": "/static/images/money control.jpg", "badge": "MC"},
        {"name": "Deccan Herald", "logo": "/static/images/deccan herland.jpg", "badge": "DH"},
        {"name": "News18", "logo": "/static/images/news18.jpg", "badge": "N18"},
        {"name": "WION", "logo": "/static/images/wion.jpg", "badge": "WION"},
    ],
    "Sports": [
        {"name": "NDTV sports", "logo": "/static/images/ndtvsports.jpg", "badge": "NS"},
        {"name": "Sport Star", "logo": "/static/images/sportstar.jpg", "badge": "SS"},
        {"name": "IPL T20", "logo": "/static/images/ipl.jpg", "badge": "IPL"},
        {"name": "Chess", "logo": "/static/images/chess.jpg", "badge": "Chess"},
        {"name": "ICC", "logo": "/static/images/icc.jpg", "badge": "ICC"},
        {"name": "Cricketworld", "logo": "/static/images/cricketworld.jpg", "badge": "CW"},
        {"name": "ESPN", "logo": "/static/images/espn.jpg", "badge": "ES"},
        {"name": "Sky Sports", "logo": "/static/images/skysports.jpg", "badge": "SS"},
        {"name": "Cricbuzz", "logo": "/static/images/cricbuzz.jpg", "badge": "CB"},
        {"name": "Sports illustrated", "logo": "/static/images/sportsillustrated.jpg", "badge": "SI"},
        {"name": "Barca Universal", "logo": "/static/images/barcauniversal.jpg", "badge": "BU"},
    ],
    "Entertainment": [
        {"name": "Variety", "logo": "/static/images/variety.jpg", "badge": "VA"},
        {"name": "Hollywood reporter", "logo": "/static/images/hollywood.jpg", "badge": "HR"},
        {"name": "Billboard", "logo": "/static/images/billboard.jpg", "badge": "BB"},
        {"name": "Rolling Stone", "logo": "/static/images/Rolling_Stone.jpg", "badge": "RS"},
        {"name": "123Telugu", "logo": "/static/images/123telugu.jpg", "badge": "123"},
        {"name": "Gulte", "logo": "/static/images/gulte.jpg", "badge": "GU"},
        {"name": "Bollywood Hungama", "logo": "/static/images/bollywoodhungama.jpg", "badge": "BH"},
        {"name": "Sacnilk", "logo": "/static/images/sacnilk.jpg", "badge": "SA"},
    ],
    "Science": [
        {"name": "Space", "logo": "/static/images/space.jpg", "badge": "SP"},
        {"name": "NASA", "logo": "/static/images/nasa.jpg", "badge": "NASA"},
        {"name": "Science Daily", "logo": "/static/images/sciencedaily.jpg", "badge": "SD"},
        {"name": "Science News", "logo": "/static/images/sciencenews.jpg", "badge": "SN"},
    ],
}

SOURCE_QUERY_MAP = {
    "techcrunch.com": "Techcrunch",
    "theverge.com": "The Verge",
    "wired.com": "Wired",
    "arstechnica.com": "Ars Technica",
    "engadget.com": "Engadget",
    "zdnet.com": "ZDNET",
    "androidpolice.com": "Android Police",
    "in.mashable.com": "Mashable india",
    "reuters.com": "Reuters",
    "bloomberg.com": "Bloomberg",
    "cnbc.com": "CNBC",
    "ft.com": "Financial Times",
    "forbes.com": "Forbes",
    "wsj.com": "Wall Street Journal",
    "m.economictimes.com": "Economic Times",
    "ndtvprofit.com": "NDTV Profit",
    "business-standard.com": "Business Standards",
    "investmentguruindia.com": "Investment Guru",
    "bbc.com": "BBC News",
    "ndtv.com": "NDTV",
    "aljazeera.com": "AL jazeera",
    "theguardian.com": "The Guardian",
    "cnn.com": "CNN",
    "apnews.com": "AP News",
    "thehindu.com": "The Hindu",
    "hindustantimes.com": "Hindustan Times",
    "indianexpress.com": "Indian Express",
    "timesofindia.indiatimes.com": "Times of India",
    "indiatoday.in": "India Today",
    "moneycontrol.com": "Money Control",
    "deccanherald.com": "Deccan Herland",
    "news18.com": "News18",
    "wionews.com": "WION",
    "espn.com": "ESPN",
    "skysports.com": "Sky Sports",
    "cricbuzz.com": "Cricbuzz",
    "si.com": "Sports Illustrated",
    "sports.ndtv.com": "NDTV Sports",
    "sportstat.thehindu.com": "Sports Star",
    "iplt20.com": "IPL T20",
    "chess.com": "Chess",
    "icc-cricket.com": "ICC",
    "cricketworld.com": "Cricket World",
    "barcauniversal.com": "Barca Universal",
    "variety.com": "Variety",
    "hollywoodreporter.com": "Hollywood Reporter",
    "billboard.com": "Billboard",
    "rollingstone.com": "Rolling Stone",
    "123Telugu.com": "123Telugu",
    "gulte.com": "Gulte", 
    "bollywoodhungama.com": "Bollywood Hungama",
    "sacnilk.com": "Sacnilk",
    "space.com": "Space",
    "nasa.gov": "NASA",
    "sciencenews.org": "Science News",
    "sciencedaily.com": "Science Daily",
    
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
    "android police": "androidpolice.com",
    "mashable india": "in.mashable.com",
    "economic times": "m.economictimes.com",
    "ndtv profit": "ndtvprofit.com",
    "business standards": "business-standard.com",
    "business standard": "business-standard.com",
    "investment guru": "investmentguruindia.com",
    "the guardian": "theguardian.com",
    "money control": "moneycontrol.com",
    "deccan herald": "deccanherald.com",
    "news18": "news18.com",
    "wion": "wionews.com",
    "ndtv sports": "sports.ndtv.com",
    "sport star": "sportstar.thehindu.com",
    "sports star": "sportstar.thehindu.com",
    "ipl t20": "iplt20.com",
    "chess": "chess.com",
    "icc": "icc-cricket.com",
    "cricketworld": "cricketworld.com",
    "cricket world": "cricketworld.com",
    "barca universal": "barcauniversal.com",
    "variety": "variety.com",
    "hollywood reporter": "hollywoodreporter.com",
    "billboard": "billboard.com",
    "rolling stone": "rollingstone.com",
    "123telugu": "123telugu.com",
    "gulte": "gulte.com",
    "bollywood hungama": "bollywoodhungama.com",
    "sacnilk": "sacnilk.com",
    "space": "space.com",
    "nasa": "nasa.gov",
    "science daily": "sciencedaily.com",
    "science news": "sciencenews.org",
}

SOURCE_FEED_MAP = {
    "techcrunch.com": [
        "https://techcrunch.com/feed/"
    ],
    "theverge.com": [
        "https://www.theverge.com/rss/index.xml"
    ],
    "arstechnica.com": [
        "https://feeds.arstechnica.com/arstechnica/index"
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
    "zdnet.com": [
        "https://www.zdnet.com/news/rss.xml"
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
    ],
    "androidpolice.com": [
        "https://www.androidpolice.com/feed/"
    ],
    "in.mashable.com": [
        "https://in.mashable.com/feeds/rss/all"
    ],
    "m.economictimes.com": [
        "https://m.economictimes.com/rssfeedsdefault.cms"
    ],
    "business-standard.com": [
        "https://www.business-standard.com/rss/home_page_top_stories.rss"
    ],
    "news18.com": [
        "https://www.news18.com/rss/india.xml"
    ],
    "wionews.com": [
        "https://www.wionews.com/rss/world.xml"
    ],
    "sports.ndtv.com": [
        "https://feeds.feedburner.com/ndtvsports-latest"
    ],
    "cricketworld.com": [
        "https://www.cricketworld.com/rss.xml"
    ],
    "space.com": [
        "https://www.space.com/feeds/all"
    ],
    "nasa.gov": [
        "https://www.nasa.gov/news-release/feed/"
    ],
    "sciencedaily.com": [
        "https://www.sciencedaily.com/rss/all.xml"
    ],
    "sciencenews.org": [
        "https://www.sciencenews.org/feed"
    ]
}

SOURCE_DOMAIN_ALIASES = {
    "in.mashable.com": ["mashable.com"],
    "zdnet.com": ["www.zdnet.com"],
    "bbc.com": ["bbc.co.uk", "www.bbc.com"],
    "ndtv.com": ["www.ndtv.com"],
    "ndtvprofit.com": ["www.ndtvprofit.com"],
    "apnews.com": ["apnews.com", "www.apnews.com"],
    "theguardian.com": ["www.theguardian.com"],
    "techcrunch.com": ["www.techcrunch.com"],
    "theverge.com": ["www.theverge.com"],
}

SOURCE_FETCH_VARIANTS = {
    "technology": ["technology", "ai", "gadgets", "software"],
    "business": ["business", "markets", "economy", "finance"],
    "world": ["world news", "international news"],
    "india": ["india news", "india politics", "india business"],
    "sports": ["sports", "cricket", "football", "tennis"],
    "entertainment": ["entertainment", "movies", "music", "celebrity"],
    "climate": [
        "climate change",
        "environment",
        "global warming",
        "renewable energy",
        "extreme weather",
        "sustainability"
    ],
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

def country_code_to_name(code: str) -> str:
    cc = safe_text(code).strip().upper()
    if not cc or cc == "WORLD":
        return ""
    return COUNTRY_CODE_TO_NAME.get(cc, "")

def effective_country_query_text(country_code: str = "WORLD", typed_country: str = "") -> str:
    typed = safe_text(typed_country).strip()
    if typed:
        return typed
    return country_code_to_name(country_code)

def safe_text(x):
    return x if isinstance(x, str) else ""

def clean_html(text):
    text = safe_text(text)
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    cleaned = soup.get_text(" ", strip=True)
    boilerplate_patterns = [
        r"Comprehensive,\s*up-to-date news coverage,\s*aggregated from sources all over the world by Google News\.?",
        r"aggregated from sources all over the world by Google News\.?",
        r"from sources all over the world by Google News\.?",
        r"\bby Google News\.?",
        r"\bFull coverage\b.*$",
        r"\bSee full coverage\b.*$",
    ]
    for pattern in boilerplate_patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned

def looks_like_non_article_url(url: str) -> bool:
    link = safe_text(url).strip().lower()
    if not link:
        return True
    bad_parts = [
        ".xml", "sitemap", "news-sitemap", "video-sitemap", "tag/", "/tags/",
        "/category/", "/categories/", "/topic/", "/topics/", "/authors/", "/author/",
        "/page/", "/pages/"
    ]
    return any(part in link for part in bad_parts)

def looks_like_non_article_title(title: str) -> bool:
    text = safe_text(title).strip().lower()
    if not text:
        return True
    if text.startswith("http://") or text.startswith("https://"):
        return True
    bad_parts = [
        "sitemap", ".xml", "rss feed", "feed", "category", "tag archive",
        "author archive", "site map"
    ]
    return any(part in text for part in bad_parts)

def looks_like_placeholder_source_title(title: str) -> bool:
    raw_title = clean_html(title)
    if not raw_title:
        return True

    parts = [safe_text(part).strip(" -") for part in raw_title.split(" - ") if safe_text(part).strip(" -")]
    if len(parts) < 3:
        return False

    generic_leads = {
        "report", "reports", "live", "updates", "update", "analysis",
        "opinion", "review", "reviews", "podcast", "video", "watch"
    }
    lead_norm = normalize_source_key(parts[0])
    tail_norms = [normalize_source_key(part) for part in parts[1:] if normalize_source_key(part)]

    if not tail_norms:
        return False

    return lead_norm in generic_leads and len(set(tail_norms)) == 1

def is_probable_real_article(title: str, link: str, description: str = "") -> bool:
    if looks_like_non_article_url(link):
        return False
    if looks_like_non_article_title(title):
        return False
    if looks_like_placeholder_source_title(title):
        return False
    cleaned_desc = clean_html(description)
    if cleaned_desc:
        lower_desc = cleaned_desc.lower()
        if lower_desc.startswith("http://") or lower_desc.startswith("https://"):
            return False
        if any(bad in lower_desc for bad in ("sitemap", ".xml", "rss feed", "feed archive")):
            return False
    text = f"{safe_text(title)} {cleaned_desc}".strip()
    if len(re.findall(r"[A-Za-z]{3,}", text)) < 6:
        return False
    return True

def dedupe_sentences(sentences):
    kept = []
    seen_norms = []
    for sentence in sentences:
        norm = " ".join(normalize_topic_words(sentence))
        if not norm:
            continue
        if any(norm == prev or norm in prev or prev in norm for prev in seen_norms):
            continue
        kept.append(sentence)
        seen_norms.append(norm)
    return kept

def sentence_title_overlap(base_title, sentence):
    title_words = set(normalize_topic_words(base_title))
    sentence_words = set(normalize_topic_words(sentence))
    if not title_words or not sentence_words:
        return 0.0
    return len(title_words.intersection(sentence_words)) / max(1, len(title_words))

def headline_context_phrase(title):
    cleaned_title = re.sub(r"\s*-\s*[A-Z][A-Za-z0-9 .,&'-]{1,40}$", "", clean_html(title)).strip(" -")
    cleaned_title = re.sub(r"\s+", " ", cleaned_title).strip(" .,!?:;")
    if not cleaned_title:
        return ""
    words = cleaned_title.split()
    if len(words) > 18:
        cleaned_title = " ".join(words[:18]).rstrip(" ,;:-")
    if cleaned_title.isupper():
        return cleaned_title
    return cleaned_title[:1].lower() + cleaned_title[1:]

def build_summary_context_sentence(title):
    topic = headline_context_phrase(title)
    if not topic:
        return "This summary explains what happened, why it matters, and what may happen next."
    return (
        f"This news is about {topic}. It gives more detail about the impact, response, or next step."
    )

def build_summary_followup_sentence(title):
    topic = headline_context_phrase(title)
    if not topic:
        return "It also helps readers quickly understand the main point of the update."
    return (
        f"It also explains why {topic} matters and what people should watch next."
    )

SUMMARY_TARGET_SENTENCES = 4
SUMMARY_MIN_WORDS = 24
SUMMARY_MIN_SENTENCE_COUNT = 3
SUMMARY_MAX_CHARS = 680

def simplify_summary_sentence(sentence):
    text = re.sub(r"\s+", " ", safe_text(sentence)).strip(" -\n\t")
    if not text:
        return ""
    text = re.sub(r"\s*[;:]\s*", ". ", text)
    text = re.sub(r"\((.*?)\)", r"\1", text)
    text = re.sub(r"\bhowever,\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bmeanwhile,\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bin addition,\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" ,;:-")
    if text and text[-1] not in ".!?":
        text += "."
    return text

def summary_sentence_count(text):
    return len([
        s for s in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", safe_text(text)).strip())
        if s.strip()
    ])

def trim_summary_text(text):
    summary_text = re.sub(r"\s+", " ", safe_text(text)).strip()
    summary_text = re.sub(r"([.!?])\1+", r"\1", summary_text)
    if len(summary_text) > SUMMARY_MAX_CHARS:
        summary_text = summary_text[:SUMMARY_MAX_CHARS - 3].rsplit(" ", 1)[0].rstrip(" ,;:-") + "..."
    if summary_text and summary_text[-1] not in ".!?":
        summary_text += "."
    return summary_text

def extract_summary_sentences(*texts):
    sentences = []
    for text in texts:
        cleaned = re.sub(r"\s+", " ", clean_html(text)).strip()
        if not cleaned:
            continue
        parts = [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned) if len(s.strip().split()) >= 5]
        sentences.extend(parts)
    return dedupe_sentences(sentences)

def finalize_summary_output(title, summary, description="", content=""):
    title_text = clean_html(title)
    normalized_title = " ".join(normalize_topic_words(title_text))

    def allow_sentence(sentence):
        normalized_sentence = " ".join(normalize_topic_words(sentence))
        if normalized_title and normalized_sentence and (
            normalized_sentence == normalized_title
            or normalized_sentence.startswith(normalized_title)
        ):
            return False
        if sentence_title_overlap(title_text, sentence) >= 0.9:
            return False
        return True

    selected = []
    for sentence in extract_summary_sentences(summary, description, content):
        if not allow_sentence(sentence):
            continue
        simple_sentence = simplify_summary_sentence(sentence)
        if not simple_sentence:
            continue
        selected.append(simple_sentence)
        if len(selected) >= SUMMARY_TARGET_SENTENCES:
            break

    selected = dedupe_sentences(selected)

    if len(selected) < SUMMARY_MIN_SENTENCE_COUNT:
        for source_text in (content, description, summary):
            for sentence in extract_summary_sentences(source_text):
                if not allow_sentence(sentence):
                    continue
                simple_sentence = simplify_summary_sentence(sentence)
                if not simple_sentence:
                    continue
                selected.append(simple_sentence)
                if len(selected) >= SUMMARY_TARGET_SENTENCES:
                    break
            selected = dedupe_sentences(selected)
            if len(selected) >= SUMMARY_MIN_SENTENCE_COUNT:
                break

    if not selected:
        raw_fallback = clean_html(content) or clean_html(description)
        raw_fallback = re.sub(r"\s+", " ", raw_fallback).strip()
        raw_sentences = extract_summary_sentences(raw_fallback)
        if raw_sentences:
            selected.extend([simplify_summary_sentence(s) for s in raw_sentences[:SUMMARY_TARGET_SENTENCES] if simplify_summary_sentence(s)])
        elif raw_fallback:
            selected.append(raw_fallback)

    if len(selected) < 2:
        selected.append(build_summary_context_sentence(title_text))
    if len(selected) < SUMMARY_MIN_SENTENCE_COUNT:
        selected.append(build_summary_followup_sentence(title_text))

    final_summary = " ".join(dedupe_sentences(selected[:SUMMARY_TARGET_SENTENCES])).strip()
    final_summary = trim_summary_text(final_summary)

    if len(final_summary.split()) < SUMMARY_MIN_WORDS:
        expansion = []
        for sentence in extract_summary_sentences(content, description):
            formatted = sentence.rstrip(" .!?") + "."
            simple_sentence = simplify_summary_sentence(formatted)
            if not simple_sentence:
                continue
            if simple_sentence in selected:
                continue
            if not allow_sentence(sentence):
                continue
            expansion.append(simple_sentence)
            if len(expansion) >= 2:
                break
        if expansion:
            final_summary = trim_summary_text(" ".join(dedupe_sentences(selected + expansion)))
        elif summary_sentence_count(final_summary) < SUMMARY_MIN_SENTENCE_COUNT:
            final_summary = trim_summary_text(
                " ".join(dedupe_sentences(selected + [build_summary_context_sentence(title_text), build_summary_followup_sentence(title_text)]))
            )

    return final_summary

def summary_needs_expansion(title, summary):
    summary_text = re.sub(r"\s+", " ", safe_text(summary)).strip()
    if not summary_text:
        return True
    if summary_text == "Summary not available.":
        return True
    if len(summary_text.split()) < SUMMARY_MIN_WORDS:
        return True
    if summary_sentence_count(summary_text) < SUMMARY_MIN_SENTENCE_COUNT:
        return True
    if sentence_title_overlap(title, summary_text) >= 0.72:
        return True
    if summary_text.lower().startswith((
        "this report covers",
        "this article covers",
        "latest update",
        "key update:"
    )):
        return True
    return False

def build_summary_fallback(title, description="", content=""):
    raw_title = clean_html(title)
    title_text = re.sub(r"\s*-\s*[A-Z][A-Za-z0-9 .,&'-]{1,40}$", "", raw_title).strip(" -")
    description_text = clean_html(description)
    content_text = clean_html(content)
    title_norm = " ".join(normalize_topic_words(title_text))

    candidates = []
    for raw in [description_text, content_text]:
        candidates.extend([s.strip() for s in re.split(r"(?<=[.!?])\s+", raw) if len(safe_text(s).split()) >= 6])

    picked = []
    for sentence in dedupe_sentences(candidates):
        norm = " ".join(normalize_topic_words(sentence))
        if not norm:
            continue
        if title_norm and (norm == title_norm or norm.startswith(title_norm)):
            continue
        if sentence_title_overlap(title_text, sentence) >= 0.82:
            continue
        picked.append(sentence)
        if len(picked) >= SUMMARY_TARGET_SENTENCES:
            break

    if picked:
        summary = " ".join(picked[:SUMMARY_TARGET_SENTENCES]).strip()
        return finalize_summary_output(title_text, summary, description_text, content_text)

    excerpt_bits = []
    for raw in (content_text, description_text):
        cleaned = re.sub(r"\s+", " ", safe_text(raw)).strip(" -\n\t")
        if not cleaned:
            continue
        excerpt_bits.append(cleaned.rstrip(".!?"))
        if len(excerpt_bits) >= SUMMARY_TARGET_SENTENCES:
            break
    if excerpt_bits:
        return finalize_summary_output(title_text, " ".join(excerpt_bits), description_text, content_text)

    return finalize_summary_output(title_text, description_text or content_text, description_text, content_text)

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
    "audacy.com", "cbs8.com",
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
    "audacy": "audacy.com",
    "cbs8": "cbs8.com",
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

def is_google_news_domain(domain: str) -> bool:
    dom = safe_text(domain).strip().lower()
    return dom in {"news.google.com", "news.google.co.in", "google.com", "www.google.com"}

def discover_external_article_url(raw_link: str, response) -> str:
    try:
        html = safe_text(getattr(response, "text", ""))
        if not html:
            return ""
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return ""

    candidates = []

    final_url = safe_text(getattr(response, "url", "")).strip()
    if final_url:
        candidates.append(final_url)

    canonical = soup.find("link", attrs={"rel": "canonical"})
    if canonical and canonical.get("href"):
        candidates.append(safe_text(canonical.get("href")))

    for meta_name in ("og:url", "twitter:url"):
        meta = soup.find("meta", attrs={"property": meta_name}) or soup.find("meta", attrs={"name": meta_name})
        if meta and meta.get("content"):
            candidates.append(safe_text(meta.get("content")))

    refresh = soup.find("meta", attrs={"http-equiv": re.compile(r"refresh", re.IGNORECASE)})
    if refresh and refresh.get("content"):
        match = re.search(r"url=([^;]+)$", safe_text(refresh.get("content")), flags=re.IGNORECASE)
        if match:
            candidates.append(match.group(1).strip())

    for tag in soup.find_all("a", href=True)[:80]:
        candidates.append(safe_text(tag.get("href")))

    candidates.extend(re.findall(r"https?://[^\s\"'<>]+", html))

    raw_domain = get_domain(raw_link)
    for candidate in candidates:
        candidate = safe_text(candidate).strip()
        if not candidate.startswith("http"):
            continue
        candidate_domain = get_domain(candidate)
        if not candidate_domain or is_google_news_domain(candidate_domain):
            continue
        if raw_domain and candidate_domain == raw_domain and is_google_news_domain(raw_domain):
            continue
        if looks_like_non_article_url(candidate):
            continue
        return candidate

    return ""

def resolve_article_url(link: str) -> str:
    raw_link = safe_text(link).strip()
    if not raw_link:
        return ""
    domain = get_domain(raw_link)
    if domain in ("news.google.com", "news.google.co.in"):
        original = extract_original_from_google_link(raw_link)
        if original:
            return original
    return raw_link

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

    if label == "Real":
        if trusted:
            reasons.append("This news comes from a well-known source.")
        else:
            reasons.append("This source is not in our main trusted list, but the article still looks reliable.")

        if positive_signals >= 2:
            reasons.append("The writing includes facts, references, or normal report-style wording.")
        elif fake_signals == 0:
            reasons.append("We did not find strong fake-news warning signs in the wording.")

        if prob_real >= 0.70:
            reasons.append("Our system found strong signs that this article is reliable.")
        else:
            reasons.append("Overall, this article looks more reliable than suspicious.")

    elif label == "Fake":
        if not trusted:
            reasons.append("This source is not in our trusted list.")
        if fake_signals >= 2:
            reasons.append("The wording sounds emotional, exaggerated, or sensational.")
        elif polarity >= 0.65:
            reasons.append("The article uses very emotional language, which can reduce trust.")
        else:
            reasons.append("We did not find enough reliable reporting signs in the article.")
        reasons.append("Our system found several signs that this article may not be reliable.")

    else:
        if trusted:
            reasons.append("The source is known, but this article still needs a manual check.")
        else:
            reasons.append("This source is not in our trusted list, so it needs extra checking.")

        if fake_signals >= 2:
            reasons.append("Some wording sounds emotional or exaggerated.")
        elif positive_signals >= 2:
            reasons.append("The article has some normal reporting signs, but not enough for a clear result.")
        else:
            reasons.append("We did not find enough clear signs to mark this as fully reliable.")

        reasons.append("The signs are mixed, so this article should be checked carefully.")

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
    positive_signals = credibility_positive_signals(text)
    polarity = abs(TextBlob(text).sentiment.polarity) if text else 0.0

    if trusted:
        if prob_real <= 0.18 and fake_signals >= 3:
            label = "Fake"
        elif prob_real <= 0.28 and fake_signals >= 2:
            label = "Check"
        elif prob_real <= 0.22 and fake_signals >= 1 and positive_signals == 0:
            label = "Check"
        else:
            label = "Real"
    else:
        if prob_real <= 0.18 and fake_signals >= 2:
            label = "Fake"
        elif fake_signals >= 3:
            label = "Check"
        elif prob_real <= 0.24 and (fake_signals >= 1 or positive_signals == 0):
            label = "Check"
        elif prob_real <= 0.32 and fake_signals >= 1 and polarity >= 0.60:
            label = "Check"
        elif prob_real <= 0.18 and not source_domain:
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

def normalize_source_key(text: str) -> str:
    value = safe_text(text).replace("%20", " ").strip().lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()

def resolve_source_domain(source_name: str) -> str:
    key = normalize_source_key(source_name)
    if not key:
        return ""
    if key in SOURCE_ROUTE_DOMAIN_MAP:
        return SOURCE_ROUTE_DOMAIN_MAP[key]
    for alias, domain in SOURCE_ROUTE_DOMAIN_MAP.items():
        if normalize_source_key(alias) == key:
            return domain
    compact_key = key.replace(" ", "")
    for alias, domain in SOURCE_ROUTE_DOMAIN_MAP.items():
        alias_norm = normalize_source_key(alias)
        if alias_norm.replace(" ", "") == compact_key:
            return domain
    return safe_text(source_name).strip().lower()

def summarize_source_names(names, limit=8):
    unique_names = []
    seen = set()
    for raw_name in names:
        cleaned = safe_text(raw_name).strip()
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        unique_names.append(cleaned)
    if not unique_names:
        return ""
    preview = ", ".join(unique_names[:limit])
    remaining = len(unique_names) - limit
    if remaining > 0:
        preview = f"{preview}, +{remaining} more"
    return preview

def group_source_names_by_category(source_rows):
    grouped = []
    category_map = {}
    for row in source_rows:
        category = safe_text(row.get("category", "")).strip() or "Other"
        name = safe_text(row.get("name", "")).strip()
        if not name:
            continue
        category_map.setdefault(category, [])
        if name not in category_map[category]:
            category_map[category].append(name)
    for category, names in category_map.items():
        grouped.append({
            "category": category,
            "names": names,
            "text": ", ".join(names),
        })
    return grouped

def source_domain_candidates(domain: str):
    primary = safe_text(domain).strip().lower()
    if not primary:
        return []
    candidates = [primary]
    for alias in SOURCE_DOMAIN_ALIASES.get(primary, []):
        alias_text = safe_text(alias).strip().lower()
        if alias_text and alias_text not in candidates:
            candidates.append(alias_text)
    return candidates

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

    def normalize_title(value):
        text = safe_text(value).lower().strip()
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"[^a-z0-9 ]+", "", text)
        return text

    def normalize_link(value):
        text = safe_text(value).strip().lower()
        if not text:
            return ""
        try:
            parsed = urlparse(text)
            path = re.sub(r"/+", "/", safe_text(parsed.path or "/").rstrip("/"))
            return f"{parsed.netloc}{path}"
        except Exception:
            return text

    for article in articles:
        title_key = normalize_title(article.get("title"))
        link_key = normalize_link(article.get("link"))
        source_key = normalize_title(article.get("source") or get_domain(article.get("link")))

        dedupe_keys = []
        if link_key:
            dedupe_keys.append(link_key)
            if title_key:
                dedupe_keys.append(f"{title_key}|{link_key}")
        elif title_key and source_key:
            dedupe_keys.append(f"{title_key}|{source_key}")
        elif title_key:
            dedupe_keys.append(title_key)

        if any(key in seen for key in dedupe_keys):
            continue

        for key in dedupe_keys:
            seen.add(key)
        unique.append(article)

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

    def strip_title_echo(base_title, text):
        title_raw = safe_text(base_title).strip()
        title_clean = re.sub(r"\s*-\s*[A-Z][A-Za-z0-9 .,&'-]{1,40}$", "", title_raw).strip(" -")
        body = safe_text(text).strip()
        if not title_clean or not body:
            return body

        body_norm = normalize_source_key(body)
        title_variants = []
        for candidate in [title_raw, title_clean]:
            candidate = re.sub(r"\s+", " ", safe_text(candidate)).strip(" -")
            if not candidate:
                continue
            title_variants.append(candidate)
        seen_variants = set()
        title_variants = [v for v in title_variants if not (normalize_source_key(v) in seen_variants or seen_variants.add(normalize_source_key(v)))]

        if not body_norm:
            return body

        for variant in title_variants:
            variant_norm = normalize_source_key(variant)
            if not variant_norm:
                continue

            if body_norm == variant_norm:
                return ""

            if body_norm.startswith(variant_norm):
                pattern = re.escape(variant)
                updated = re.sub(rf"^{pattern}\s*[-:|,.]*\s*", "", body, flags=re.IGNORECASE)
                if updated != body:
                    body = updated
                    break

                title_words = normalize_topic_words(variant)
                body_words = re.split(r"\s+", body)
                if title_words and len(body_words) >= len(title_words):
                    body = " ".join(body_words[len(title_words):]).lstrip(" :-|,.")
                    break

        return re.sub(r"\s+", " ", body).strip()

    description_text = strip_title_echo(title_text, description_text)
    content_text = strip_title_echo(title_text, content_text)

    parts = []
    seen_parts = set()
    for part in [description_text, content_text]:
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
    combined = re.sub(r"\[\+\d+\s+chars\]", "", combined, flags=re.IGNORECASE)
    combined = re.sub(r"\b[A-Za-z0-9_-]+\.{3,}\b", "", combined)
    combined = re.sub(r"(https?://\S+)", "", combined).strip()
    combined = re.sub(r"\s*-\s*[A-Z][A-Za-z0-9 .,&'-]{1,40}$", "", combined).strip()

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
        ranked = []

    top_sentences = sorted(sorted(ranked, key=lambda x: x[0], reverse=True)[:6], key=lambda x: x[1])
    sentence_texts = [sentence for _, _, sentence in top_sentences]
    sentence_texts = dedupe_sentences(sentence_texts)

    cleaned_sentences = []
    normalized_title = " ".join(normalize_topic_words(title_text))
    for sentence in sentence_texts:
        normalized_sentence = " ".join(normalize_topic_words(sentence))
        if normalized_title and normalized_sentence and (
            normalized_sentence == normalized_title
            or normalized_sentence.startswith(normalized_title)
        ):
            continue
        simple_sentence = simplify_summary_sentence(sentence.strip())
        if simple_sentence:
            cleaned_sentences.append(simple_sentence)

    if not cleaned_sentences:
        fallback_candidates = [description_text, content_text, combined]
        for candidate in fallback_candidates:
            candidate_sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", candidate) if len(s.strip()) > 20]
            candidate_sentences = dedupe_sentences(candidate_sentences)
            for sentence in candidate_sentences:
                normalized_sentence = " ".join(normalize_topic_words(sentence))
                if normalized_title and normalized_sentence and normalized_sentence.startswith(normalized_title):
                    continue
                if sentence_title_overlap(title_text, sentence) >= 0.82:
                    continue
                simple_sentence = simplify_summary_sentence(sentence)
                if not simple_sentence:
                    continue
                cleaned_sentences.append(simple_sentence)
                if len(cleaned_sentences) >= SUMMARY_TARGET_SENTENCES:
                    break
            if cleaned_sentences:
                break

    if not cleaned_sentences:
        fragments = []
        for candidate in [description_text, content_text]:
            candidate = re.sub(r"\s+", " ", safe_text(candidate)).strip(" -\n\t")
            if candidate and len(candidate) > 20:
                simple_sentence = simplify_summary_sentence(candidate.rstrip(".!?"))
                if simple_sentence:
                    fragments.append(simple_sentence)
        if title_text and not fragments:
            head = re.sub(r"\s*-\s*[A-Z][A-Za-z0-9 .,&'-]{1,40}$", "", title_text).strip(" -")
            if head and len(head.split()) >= 5:
                simple_sentence = simplify_summary_sentence(head.rstrip(".!?"))
                if simple_sentence:
                    fragments.insert(0, simple_sentence)
        if fragments:
            cleaned_sentences = fragments[:SUMMARY_TARGET_SENTENCES]

    summary = " ".join(cleaned_sentences[:SUMMARY_TARGET_SENTENCES])
    summary = re.sub(r"\s+", " ", summary).strip()
    summary = re.sub(r"([.!?])\1+", r"\1", summary)
    summary = re.sub(r"\b([A-Za-z0-9][A-Za-z0-9 '&-]{3,})\s+\1\b", r"\1", summary, flags=re.IGNORECASE)
    summary = re.sub(r"\b([A-Za-z0-9][A-Za-z0-9 '&-]{3,})\s+\1\b", r"\1", summary, flags=re.IGNORECASE)

    summary = trim_summary_text(summary)

    if len(summary.split()) < SUMMARY_MIN_WORDS:
        fallback_bits = []
        if description_text and len(description_text.split()) >= 6:
            simple_sentence = simplify_summary_sentence(description_text.rstrip(".!?"))
            if simple_sentence:
                fallback_bits.append(simple_sentence)
        if content_text and len(content_text.split()) >= 8:
            simple_sentence = simplify_summary_sentence(content_text.rstrip(".!?"))
            if simple_sentence:
                fallback_bits.append(simple_sentence)
        if content_text and len(content_text.split()) >= 14:
            simple_sentence = simplify_summary_sentence(content_text.rstrip(".!?"))
            if simple_sentence:
                fallback_bits.append(simple_sentence)
        if len(fallback_bits) < 3:
            fallback_bits.append(build_summary_context_sentence(title_text))
        if len(fallback_bits) < SUMMARY_MIN_SENTENCE_COUNT:
            fallback_bits.append(build_summary_followup_sentence(title_text))
        summary = trim_summary_text(" ".join(dedupe_sentences(fallback_bits[:SUMMARY_TARGET_SENTENCES])).strip())
    normalized_summary = normalize_source_key(summary)
    normalized_title = normalize_source_key(title_text)
    if normalized_title and normalized_summary and (
        normalized_summary == normalized_title
        or normalized_summary.startswith(normalized_title)
    ):
        return build_summary_fallback(title_text, description_text, content_text)

    if len(summary.split()) < 12:
        return build_summary_fallback(title_text, description_text, content_text)

    return finalize_summary_output(title_text, summary, description_text, content_text)

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
    uid = current_user_id()
    if not uid:
        return []
    rows = get_saved(uid)
    freq = Counter()
    for row in rows[:20]:
        title = safe_text(row["title"])
        if " - " in title:
            title = title.split(" - ")[0]
        for w in normalize_topic_words(title):
            freq[w] += 1
    return [w for w, _ in freq.most_common(10)]

def get_user_activity_rows(limit=400):
    uid = current_user_id()
    if not uid:
        return []
    cache_key = f"_activity_rows_{uid}_{int(limit)}"
    if hasattr(g, cache_key):
        return getattr(g, cache_key)
    rows = list(get_recent_activity_by_user(uid, limit))
    setattr(g, cache_key, rows)
    return rows

def extract_detail_value(text, key):
    pattern = rf"{re.escape(key)}=([^|]+)"
    match = re.search(pattern, safe_text(text))
    return match.group(1).strip() if match else ""

def build_ai_recommendations(articles, category=""):
    rows = get_user_activity_rows(500)
    search_freq = Counter()
    click_freq = Counter()

    for row in rows:
        event_type = safe_text(row["event_type"])
        details = safe_text(row["details"])
        if event_type == "search":
            search_term = details.replace("Searched topic:", "").strip()
            for w in normalize_topic_words(search_term):
                search_freq[w] += 1
        elif event_type == "article_click":
            title = extract_detail_value(details, "title")
            cat = extract_detail_value(details, "category")
            src = extract_detail_value(details, "source")
            for w in normalize_topic_words(f"{title} {cat} {src}"):
                click_freq[w] += 1

    saved_freq = Counter(extract_keywords_from_saved())
    merged = Counter()
    merged.update(search_freq)
    merged.update(saved_freq)
    merged.update(click_freq)
    if category:
        for w in normalize_topic_words(category):
            merged[w] += 1

    recommendations = [w.title() for w, _ in merged.most_common(6) if w]
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
    log_user_event("article_click", f"title={title} | category={category} | source={source}")

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
    if len(rows) < 1:
        return None
    return {"topic": chosen_topic.title(), "rows": rows}

def get_activity_summary(default_category="Technology"):
    rows = get_user_activity_rows(500)
    today = today_local_date()

    category_counter = Counter()
    search_counter = Counter()
    total_seconds = 0

    for row in rows:
        event_type = safe_text(row["event_type"])
        details = safe_text(row["details"])
        if event_type == "category_view":
            category_name = details.replace("Opened category:", "").strip().title()
            if category_name:
                category_counter[category_name] += 1
        elif event_type == "article_click":
            category_name = extract_detail_value(details, "category").title()
            if category_name:
                category_counter[category_name] += 1
        elif event_type == "search":
            search_term = details.replace("Searched topic:", "").strip()
            if search_term:
                search_counter[search_term] += 1
        elif event_type == "reading_time":
            try:
                row_time = parse_activity_time(row.get("created_at", ""))
                if row_time and row_time.date() == today:
                    total_seconds += int(extract_detail_value(details, "seconds") or "0")
            except Exception:
                pass

    most_clicked = category_counter.most_common(1)[0][0] if category_counter else ""
    top = [f"#{w.replace(' ', '')}" for w, _ in search_counter.most_common(3)]

    minutes = total_seconds // 60
    hours = minutes // 60
    mins = minutes % 60
    if total_seconds and hours:
        reading_time = f"{hours}h {mins}m"
    elif total_seconds and minutes:
        reading_time = f"{minutes}m"
    elif total_seconds:
        reading_time = "<1m"
    else:
        reading_time = ""

    return {
        "reading_time": reading_time,
        "reading_seconds": total_seconds,
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

def process_article_common(title, description, link, source_domain, saved_links, category="general", published_raw=None, image_url="", content="", allow_live_summary_fetch=False):
    article_body = normalize_article_body_text(content)
    fetched_article_body = ""
    if (
        allow_live_summary_fetch
        and should_allow_live_summary_fetch()
        and not has_meaningful_article_body(article_body)
        and safe_text(link).strip()
    ):
        fetched_article_body = fetch_article_text_excerpt(link)

    summary, article_body, summary_uses_full_article = summarize_complete_article_text(
        title,
        description,
        article_body,
        fetched_article_body,
    )
    analysis_text = normalize_article_body_text(article_body, description)
    summary_input_text = f"{title} {description} {analysis_text}".strip()
    label, score = detect_fake(summary_input_text, source_domain=source_domain)
    published_dt = parse_any_datetime(published_raw)
    time_ago = format_time_ago(published_dt)
    bias = detect_bias(f"{title} {description}".strip())
    published_display = format_published_display(published_dt)
    keywords = extract_keywords(title, description)
    credibility = round(score * 100, 2)
    local_published = to_local_datetime(published_dt)
    explanation_reasons = explain_credibility(f"{title} {description} {analysis_text}".strip(), source_domain, label, score)
    raw_image = safe_text(image_url).strip()
    is_fallback_image = False
    if raw_image.startswith("http"):
        final_image = raw_image
    else:
        final_image = build_article_placeholder_image(source_domain=source_domain, category=category, title=title)
        is_fallback_image = bool(final_image)

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
        "content": article_body,
        "summary_uses_full_article": summary_uses_full_article,
        "bias": bias,
        "source": source_domain,
        "source_name": source_display_name(source_domain),
        "keywords": keywords,
        "credibility": credibility,
        "image": final_image,
        "image_is_fallback": is_fallback_image,
        "explanation_reasons": explanation_reasons,
    }

def enrich_article_summaries(articles, live_fetch_budget=3):
    if not articles:
        return articles

    remaining_budget = adaptive_live_fetch_budget(live_fetch_budget)
    enriched = [dict(article) for article in articles]

    for item in enriched:
        item["content"] = normalize_article_body_text(item.get("content", ""))

    fetch_targets = []
    for idx, item in enumerate(enriched):
        if remaining_budget <= 0:
            break
        if item.get("summary_uses_full_article") and not summary_needs_expansion(item.get("title", ""), item.get("ai_summary", "")):
            continue

        link = safe_text(item.get("link", "")).strip()
        if not link:
            continue

        fetch_targets.append((idx, link))
        remaining_budget -= 1

    if fetch_targets:
        max_workers = adaptive_fetch_workers(len(fetch_targets))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(fetch_article_text_excerpt, link): idx
                for idx, link in fetch_targets
            }
            for future in as_completed(future_map):
                idx = future_map[future]
                fetched_content = ""
                try:
                    fetched_content = future.result() or ""
                except Exception:
                    fetched_content = ""

                item = enriched[idx]
                improved_summary, article_body, summary_uses_full_article = summarize_complete_article_text(
                    item.get("title", ""),
                    item.get("description", ""),
                    item.get("content", ""),
                    fetched_content,
                )
                item["content"] = article_body
                item["summary_uses_full_article"] = summary_uses_full_article
                item["ai_summary"] = improved_summary

    for item in enriched:
        if item.get("summary_uses_full_article"):
            if summary_needs_expansion(item.get("title", ""), item.get("ai_summary", "")):
                item["ai_summary"] = build_summary_fallback(
                    item.get("title", ""),
                    "",
                    item.get("content", "")
                )
        else:
            if summary_needs_expansion(item.get("title", ""), item.get("ai_summary", "")):
                item["ai_summary"] = build_summary_fallback(
                    item.get("title", ""),
                    item.get("description", ""),
                    item.get("content", "")
                )

    return enriched

def google_rss(query=None, category=None, max_results=30, country_code="WORLD", source_domain_filter="", country_text="", selected_date=None):
    cc = (country_code or "WORLD").upper()
    country_text = effective_country_query_text(cc, country_text)

    cache_key = f"rss::{CACHE_VERSION}::{query}::{category}::{max_results}::{cc}::{source_domain_filter}::{country_text}::{selected_date or ''}"
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

    if selected_date:
        date_suffix = build_date_search_suffix(selected_date)
        if date_suffix:
            if q_text:
                q_text = f"{q_text}{date_suffix}"
            else:
                q_text = f"top news{date_suffix}"

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

    feed = fetch_feed_with_timeout(url)
    saved_links = current_saved_links()
    articles = []

    for entry in feed.entries[:max_results]:
        title = safe_text(getattr(entry, "title", ""))
        raw_summary_html = getattr(entry, "summary", "")
        summary = clean_html(raw_summary_html)
        link = resolve_article_url(getattr(entry, "link", ""))
        src_domain = publisher_domain(title, link)
        image_url = extract_feed_image(entry, raw_summary_html)

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
                image_url=image_url,
                allow_live_summary_fetch=False,
            )
        )

    set_cache(cache_key, articles)
    return articles

def filter_articles_by_exact_date(articles, selected_date):
    if not selected_date:
        return articles
    return [
        a for a in articles
        if article_matches_date(parse_any_datetime(a.get("published_iso")), selected_date)
    ]

def collect_home_source_day_articles(selected_date, source_domain_filter=""):
    target_date_text = (parse_selected_date(selected_date) or today_local_date()).strftime("%Y-%m-%d")
    domains = []
    if source_domain_filter:
        domains.append(source_domain_filter)
    domains.extend([domain for domain in SOURCE_FEED_MAP.keys() if domain not in domains])

    collected = []
    max_workers = min(HOME_SOURCE_SCAN_MAX_WORKERS, max(1, len(domains)))

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                source_feed_articles,
                domain,
                selected_date=target_date_text,
                max_results=HOME_SOURCE_SCAN_PER_SOURCE
            ): domain
            for domain in domains
        }
        for future in as_completed(future_map):
            try:
                items = future.result() or []
            except Exception:
                items = []
            if items:
                collected.extend([
                    item for item in items
                    if is_probable_real_article(item.get("title", ""), item.get("link", ""), item.get("description", ""))
                ])

    return collected

def fetch_daily_articles(mode="home", query=None, category=None, selected_date=None, country_code="WORLD", source_domain_filter="", country_text=""):
    target_date = parse_selected_date(selected_date) or today_local_date()
    target_date_text = target_date.strftime("%Y-%m-%d")
    typed_country = safe_text(country_text).strip()
    country_focus = effective_country_query_text(country_code, typed_country)
    # Dropdown country selection should use the selected locale feed directly.
    # Only explicit typed-country searches should be forced into the RSS query text.
    rss_country_text = country_focus if typed_country else ""

    collected = []

    newsapi_articles = newsapi_fetch(
        query=query,
        category=category,
        selected_date=target_date_text,
        max_results=DAILY_NEWS_MAX_RESULTS,
        source_domain_filter=source_domain_filter,
        country_text=country_focus
    )
    collected.extend(filter_articles_by_exact_date(remove_duplicates(newsapi_articles), target_date_text))

    rss_articles = google_rss(
        query=query if mode == "search" else None,
        category=category if mode == "category" else None,
        max_results=DAILY_RSS_MAX_RESULTS,
        country_code=country_code,
        source_domain_filter=source_domain_filter,
        country_text=rss_country_text,
        selected_date=target_date_text
    )
    collected.extend(filter_articles_by_exact_date(remove_duplicates(rss_articles), target_date_text))

    if mode == "home":
        collected.extend(
            filter_articles_by_exact_date(
                remove_duplicates(collect_home_source_day_articles(target_date_text, source_domain_filter=source_domain_filter)),
                target_date_text
            )
        )

    if mode == "home" and not collected:
        for fallback_query in ["top news", "breaking news", "world news", "latest headlines"]:
            fallback_newsapi = newsapi_fetch(
                query=fallback_query,
                selected_date=target_date_text,
                max_results=30,
                source_domain_filter=source_domain_filter,
                country_text=country_focus
            )
            collected.extend(filter_articles_by_exact_date(remove_duplicates(fallback_newsapi), target_date_text))
            if collected:
                break

    if category == "climate" and not collected:
        for climate_query in SOURCE_FETCH_VARIANTS.get("climate", []):
            climate_newsapi = newsapi_fetch(
                query=climate_query,
                category="climate",
                selected_date=target_date_text,
                max_results=40,
                source_domain_filter=source_domain_filter,
                country_text=""
            )
            collected.extend(filter_articles_by_exact_date(remove_duplicates(climate_newsapi), target_date_text))

            climate_world_rss = google_rss(
                query=climate_query,
                category=None,
                max_results=40,
                country_code="WORLD",
                source_domain_filter=source_domain_filter,
                country_text="",
                selected_date=target_date_text
            )
            collected.extend(filter_articles_by_exact_date(remove_duplicates(climate_world_rss), target_date_text))

    if len(collected) < 8:
        fallback_queries = []
        if query:
            fallback_queries.extend([query, f"{query} latest news", f"{query} headlines"])
        if category:
            base_query = CATEGORY_QUERY.get(category, category)
            fallback_queries.extend([
                base_query,
                f"{base_query} latest news",
                f"{base_query} headlines",
                "breaking news"
            ])
        if not fallback_queries:
            fallback_queries.extend(["top news", "latest news", "breaking news", "world headlines"])

        seen_queries = set()
        for fallback_query in fallback_queries:
            fq = safe_text(fallback_query).strip()
            if not fq or fq.lower() in seen_queries:
                continue
            seen_queries.add(fq.lower())
            fallback_articles = newsapi_fetch(
                query=fq,
                selected_date=target_date_text,
                max_results=35,
                source_domain_filter=source_domain_filter,
                country_text=country_focus
            )
            collected.extend(filter_articles_by_exact_date(remove_duplicates(fallback_articles), target_date_text))
            if len(remove_duplicates(collected)) >= 12:
                break

    if len(collected) < 8:
        rss_fallback_queries = []
        if query:
            rss_fallback_queries.extend([query, f"{query} news", f"{query} headlines"])
        if category:
            base_query = CATEGORY_QUERY.get(category, category)
            rss_fallback_queries.extend([base_query, f"{base_query} news", f"{base_query} headlines"])
        if not rss_fallback_queries:
            rss_fallback_queries.extend(["top news", "world news", "breaking news", "latest headlines"])

        seen_rss_queries = set()
        for fallback_query in rss_fallback_queries:
            fq = safe_text(fallback_query).strip()
            if not fq or fq.lower() in seen_rss_queries:
                continue
            seen_rss_queries.add(fq.lower())
            try:
                rss_items = google_rss(
                    query=fq,
                    category=None,
                    max_results=20,
                    country_code=country_code,
                    source_domain_filter=source_domain_filter,
                    country_text=rss_country_text,
                    selected_date=target_date_text
                )
            except Exception:
                rss_items = []
            collected.extend(filter_articles_by_exact_date(remove_duplicates(rss_items), target_date_text))
            if len(remove_duplicates(collected)) >= 12:
                break

    if len(collected) < 6 and target_date < today_local_date():
        archive_source_order = []
        if source_domain_filter:
            archive_source_order.append(source_domain_filter)
        archive_source_order.extend([
            "reuters.com", "bbc.com", "theguardian.com", "cnn.com", "ndtv.com",
            "news18.com", "wionews.com", "cnbc.com", "forbes.com", "ndtvprofit.com",
            "techcrunch.com", "theverge.com", "wired.com", "arstechnica.com", "engadget.com",
            "zdnet.com", "androidpolice.com", "mashable.com", "in.mashable.com",
            "sciencedaily.com", "espn.com", "variety.com"
        ])
        archive_source_order.extend([
            domain for domain in SOURCE_FEED_MAP.keys()
            if domain not in archive_source_order
        ])
        seen_archive_domains = set()
        for domain in archive_source_order:
            if domain in seen_archive_domains:
                continue
            seen_archive_domains.add(domain)
            try:
                feed_items = source_feed_articles(domain, selected_date=target_date_text, max_results=8)
            except Exception:
                feed_items = []
            feed_items = [
                item for item in feed_items
                if is_probable_real_article(item.get("title", ""), item.get("link", ""), item.get("description", ""))
            ]
            collected.extend(feed_items)
            if len(remove_duplicates(collected)) >= 12:
                break

    collected = sorted(
        remove_duplicates(collected),
        key=lambda x: parse_any_datetime(x.get("published_iso")) or datetime.min,
        reverse=True
    )

    # If the user is on today's home feed and exact-day sources have not published
    # enough items yet, search broader latest feeds but still keep only today's
    # local-date headlines so yesterday's news never appears after midnight.
    if not collected and mode == "home" and target_date == today_local_date():
        latest_pool = []
        for fallback_query in ["top news", "breaking news", "latest news", "world headlines"]:
            try:
                latest_pool.extend(
                    newsapi_fetch(
                        query=fallback_query,
                        selected_date=None,
                        max_results=18,
                        source_domain_filter=source_domain_filter,
                        country_text=country_focus
                    )
                )
            except Exception:
                pass
            if latest_pool:
                break

        try:
            latest_pool.extend(
                google_rss(
                    query=None,
                    category=None,
                    max_results=24,
                    country_code=country_code,
                    source_domain_filter=source_domain_filter,
                    country_text=country_focus
                )
            )
        except Exception:
            pass

        collected = sorted(
            filter_articles_by_exact_date(remove_duplicates([
                item for item in latest_pool
                if is_probable_real_article(item.get("title", ""), item.get("link", ""), item.get("description", ""))
            ]), target_date_text),
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
        r = http_get(url, params=params, timeout=12)
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
    uid = current_user_id()
    dashboard_cache_key = f"dashboard_v4::{CACHE_VERSION}::{uid}::{mode}::{query}::{category}::{selected_date}::{session.get('selected_country','WORLD')}::{session.get('selected_source','')}::{session.get('typed_country','')}"
    cached_dashboard = get_cache(dashboard_cache_key)
    if cached_dashboard is not None:
        user_saved_rows = get_saved(uid) if uid else []
        refreshed = dict(cached_dashboard)
        refreshed["latest_saved"] = list(user_saved_rows[:3]) if user_saved_rows else []
        refreshed["activity"] = get_activity_summary(default_category=(category.capitalize() if category else "Technology"))
        refreshed["ai_recommendations"] = build_ai_recommendations(
            refreshed.get("articles", []),
            category=category or query or ""
        )
        refreshed["quick_stats"] = dict(refreshed.get("quick_stats", {}))
        refreshed["quick_stats"]["saved_count"] = len(user_saved_rows)
        return refreshed

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

    live_summary_budget = min(len(filtered_articles), DASHBOARD_SUMMARY_FETCH_LIMIT)
    filtered_articles = enrich_article_summaries(filtered_articles, live_fetch_budget=live_summary_budget)

    # ALWAYS OUTSIDE
    highlights = filtered_articles[:3]
    calc_items = filtered_articles[:12]

    headline_counts = make_counts(calc_items, "headline_sentiment")
    public_counts = make_counts(calc_items, "public_sentiment")

    user_saved_rows = get_saved(uid) if uid else []
    quick_stats = {
        "articles_read": len(calc_items),
        "fake_count": sum(1 for a in calc_items if a.get("label") == "Fake"),
        "real_count": sum(1 for a in calc_items if a.get("label") == "Real"),
        "avg_positive": int((headline_counts["pos"] / max(1, len(calc_items))) * 100),
        "saved_count": len(user_saved_rows),
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
    saved_rows = user_saved_rows
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
        "welcome_prefix": "Welcome Back" if session.get("welcome_mode") == "back" else "Welcome",
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
    domain = resolve_source_domain(source_key)
    domain_candidates = source_domain_candidates(domain)
    source_phrase = SOURCE_QUERY_MAP.get(domain, source_key.replace(".", " "))
    selected_day = parse_selected_date(selected_date)
    cache_key = f"source_v4::{CACHE_VERSION}::{domain}::{selected_date or now_local().strftime('%Y-%m-%d')}"
    cached = get_cache(cache_key)
    if cached is not None:
        return cached

    from_dt, to_dt = local_day_bounds_for_api(selected_date)
    params = {
        "domains": ",".join(domain_candidates) if domain_candidates else domain,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": NEWSAPI_PAGE_SIZE,
        "from": from_dt,
        "to": to_dt,
        "apiKey": API_KEY,
    }

    saved_links = current_saved_links()
    articles = []
    target_date_text = (selected_day or today_local_date()).strftime("%Y-%m-%d")
    source_page_target_count = min(SOURCE_PAGE_MAX_RESULTS, 18)
    source_has_direct_feed = bool(SOURCE_FEED_MAP.get(domain))
    feed_articles = [
        item for item in source_feed_articles(domain, selected_date=target_date_text, max_results=SOURCE_PAGE_MAX_RESULTS)
        if is_probable_real_article(item.get("title", ""), item.get("link", ""), item.get("description", ""))
    ]
    articles.extend(feed_articles)

    def append_newsapi_results(payload):
        for item in payload.get("articles", []):
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
            if not is_probable_real_article(title, link, description):
                continue

            if not article_matches_source_domain({"source": publisher_domain(title, link), "link": link}, domain):
                title_check = title.lower()
                phrase_check = source_phrase.lower()
                if phrase_check not in title_check and source_key not in title_check:
                    continue

            articles.append(
                process_article_common(
                    title=title,
                    description=description,
                    content=content,
                    link=resolve_article_url(link),
                    source_domain=domain,
                    saved_links=saved_links,
                    category="general",
                    published_raw=published_raw,
                    image_url=image_url,
                    allow_live_summary_fetch=False,
                )
            )

    if len(remove_duplicates(articles)) < source_page_target_count:
        for page_number in range(1, NEWSAPI_MAX_PAGES + 1):
            try:
                page_params = dict(params)
                page_params["page"] = page_number
                response = http_get("https://newsapi.org/v2/everything", params=page_params, timeout=3.5)
                payload = response.json()
                append_newsapi_results(payload)
                if len((payload or {}).get("articles", []) or []) < NEWSAPI_PAGE_SIZE:
                    break
                if len(remove_duplicates(articles)) >= SOURCE_PAGE_MAX_RESULTS:
                    break
            except Exception:
                break

    if len(remove_duplicates(articles)) < source_page_target_count:
        source_aliases = [alias for alias, mapped_domain in SOURCE_ROUTE_DOMAIN_MAP.items() if mapped_domain == domain]
        fallback_queries = [source_phrase, source_key.replace(".", " "), source_display_name(domain)]
        fallback_queries.extend(source_aliases[:3])
        if domain == "in.mashable.com":
            fallback_queries.extend(["Mashable", "Mashable India tech"])
        if domain == "zdnet.com":
            fallback_queries.extend(["ZDNET", "ZD Net"])
        fallback_queries.extend([f"{source_phrase} news", f"{source_phrase} headlines"])
        seen_fallbacks = set()
        for fallback_query in fallback_queries:
            fq = safe_text(fallback_query).strip()
            if not fq or fq.lower() in seen_fallbacks:
                continue
            seen_fallbacks.add(fq.lower())
            try:
                extra_articles = newsapi_fetch(
                    query=fq,
                    selected_date=target_date_text,
                    max_results=18,
                    source_domain_filter="",
                    country_text=""
                )
            except Exception:
                extra_articles = []

            for item in extra_articles:
                if article_matches_source_domain(item, domain) and is_probable_real_article(
                    item.get("title", ""),
                    item.get("link", ""),
                    item.get("description", "")
                ):
                    articles.append(item)

    if (not source_has_direct_feed) or len(remove_duplicates(articles)) < source_page_target_count:
        rss_variants = []
        rss_variants.extend(google_rss(
            query=source_phrase,
            category=None,
            max_results=30,
            country_code="WORLD",
            source_domain_filter=domain,
            country_text="",
            selected_date=target_date_text
        ))
        rss_variants.extend(google_rss(
            query=None,
            category=None,
            max_results=30,
            country_code="WORLD",
            source_domain_filter=domain,
            country_text="",
            selected_date=target_date_text
        ))
        for site_domain in domain_candidates or [domain]:
            rss_variants.extend(google_rss(
                query=f"site:{site_domain}",
                category=None,
                max_results=20,
                country_code="WORLD",
                source_domain_filter="",
                country_text="",
                selected_date=target_date_text
            ))
        if source_phrase and source_phrase != source_key:
            rss_variants.extend(google_rss(
                query=source_key.replace(".", " "),
                category=None,
                max_results=20,
                country_code="WORLD",
                source_domain_filter=domain,
                country_text="",
                selected_date=target_date_text
            ))
            rss_variants.extend(google_rss(
                query=f"{source_phrase} news",
                category=None,
                max_results=20,
                country_code="WORLD",
                source_domain_filter="",
                country_text="",
                selected_date=target_date_text
            ))

        rss_articles = [
            item for item in filter_articles_by_exact_date(remove_duplicates(rss_variants), target_date_text)
            if article_matches_source_domain(item, domain)
            and is_probable_real_article(item.get("title", ""), item.get("link", ""), item.get("description", ""))
        ]

        articles.extend(rss_articles)
    articles = sorted(
        remove_duplicates(articles),
        key=lambda x: parse_any_datetime(x.get("published_iso")) or datetime.min,
        reverse=True
    )

    if len(articles) < 4 and not selected_day:
        latest_fallbacks = []
        for fallback_query in [source_phrase, source_display_name(domain), source_key.replace(".", " "), f"site:{domain}"]:
            fq = safe_text(fallback_query).strip()
            if not fq:
                continue
            try:
                latest_fallbacks.extend(
                    newsapi_fetch(
                        query=fq,
                        selected_date=None,
                        max_results=10,
                        source_domain_filter="",
                        country_text=""
                    )
                )
            except Exception:
                pass
        for site_domain in domain_candidates or [domain]:
            try:
                latest_fallbacks.extend(
                    google_rss(
                        query=f"site:{site_domain}",
                        category=None,
                        max_results=15,
                        country_code="WORLD",
                        source_domain_filter="",
                        country_text=""
                    )
                )
            except Exception:
                pass
        latest_fallbacks.extend(source_feed_articles(domain, selected_date=None, max_results=15))
        articles.extend([
            item for item in filter_articles_by_exact_date(remove_duplicates(latest_fallbacks), target_date_text)
            if article_matches_source_domain(item, domain)
            and is_probable_real_article(item.get("title", ""), item.get("link", ""), item.get("description", ""))
        ])
        articles = sorted(
            remove_duplicates(articles),
            key=lambda x: parse_any_datetime(x.get("published_iso")) or datetime.min,
            reverse=True
        )[:SOURCE_PAGE_MAX_RESULTS]

    articles = enrich_article_summaries(articles, live_fetch_budget=min(len(articles), SOURCE_SUMMARY_FETCH_LIMIT))

    set_cache(cache_key, articles)
    return articles

def article_matches_source_domain(article, domain: str) -> bool:
    domains = source_domain_candidates(domain)
    if not domains:
        return False
    source = safe_text(article.get("source", "") or article.get("source_name", "")).strip().lower()
    link = safe_text(article.get("link", "")).strip().lower()
    title = safe_text(article.get("title", "")).strip().lower()
    description = safe_text(article.get("description", "") or article.get("ai_summary", "")).strip().lower()
    display_name = safe_text(source_display_name(domain)).strip().lower()
    normalized_display = normalize_source_key(display_name)
    normalized_source = normalize_source_key(source)
    normalized_title = normalize_source_key(title)
    normalized_desc = normalize_source_key(description)
    source_aliases = [alias for alias, mapped_domain in SOURCE_ROUTE_DOMAIN_MAP.items() if mapped_domain == domain]
    if normalized_display and normalized_source and (
        normalized_display in normalized_source or normalized_source in normalized_display
    ):
        return True
    if normalized_display and (
        normalized_display in normalized_title
        or normalized_display in normalized_desc
    ):
        return True
    for alias in source_aliases:
        norm_alias = normalize_source_key(alias)
        if norm_alias and (
            norm_alias in normalized_source
            or norm_alias in normalized_title
            or norm_alias in normalized_desc
        ):
            return True
    for item_domain in domains:
        if (
            source == item_domain
            or source.endswith("." + item_domain)
            or item_domain in source
            or item_domain in link
            or (display_name and display_name in title and item_domain.split(".")[0] in title)
        ):
            return True
    return False

def build_trusted_source_sections(selected_date=None, enable_direct_fetch=True):
    target_date = selected_date or now_local().strftime("%Y-%m-%d")
    cache_key = f"trusted_showcase::{CACHE_VERSION}::{target_date}::{'deep' if enable_direct_fetch else 'fast'}"
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
            domain = resolve_source_domain(source_key)

            source_articles = [
                item for item in preview_pool
                if article_matches_source_domain(item, domain)
            ][:4]
            coverage_mode = "selected_date" if source_articles else "none"
            coverage_note = "Matched articles from the selected date."

            if enable_direct_fetch and len(source_articles) < 2:
                try:
                    direct_articles = fetch_source_articles(source_name, selected_date=target_date)
                except Exception:
                    direct_articles = []

                direct_articles = remove_duplicates(direct_articles)
                direct_selected_date_articles = filter_articles_by_exact_date(direct_articles, target_date)

                if direct_selected_date_articles:
                    source_articles = sorted(
                        remove_duplicates(source_articles + direct_selected_date_articles),
                        key=lambda x: parse_any_datetime(x.get("published_iso")) or datetime.min,
                        reverse=True
                    )[:4]
                    coverage_mode = "selected_date"
                    coverage_note = "Matched articles from the selected date using direct source fetch."
                else:
                    coverage_mode = "none"
                    coverage_note = "No headlines matched the selected date for this source."
            elif not source_articles:
                coverage_mode = "none"
                coverage_note = "No selected-date headlines were found in the fast dashboard scan."

            enriched_sources.append({
                **source,
                "route_query": source_name,
                "headline_count": len(source_articles),
                "headlines": source_articles[:2],
                "coverage_mode": coverage_mode,
                "coverage_note": coverage_note,
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

def source_feed_articles(domain, selected_date=None, max_results=40):
    feed_urls = SOURCE_FEED_MAP.get(domain, [])
    if not feed_urls:
        return []

    cache_key = f"sourcefeeds::{CACHE_VERSION}::{domain}::{selected_date or now_local().strftime('%Y-%m-%d')}::{max_results}"
    cached = get_cache(cache_key)
    if cached is not None:
        return cached

    saved_links = current_saved_links()
    collected = []

    for feed_url in feed_urls:
        try:
            feed = fetch_feed_with_timeout(feed_url)
        except Exception:
            continue

        for entry in getattr(feed, "entries", [])[:max_results]:
            title = safe_text(getattr(entry, "title", ""))
            raw_summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
            description = clean_html(raw_summary)
            content = ""
            raw_content = getattr(entry, "content", None)
            if raw_content and isinstance(raw_content, list):
                content = clean_html(safe_text(raw_content[0].get("value", "")))
            link = safe_text(getattr(entry, "link", ""))
            image_url = extract_feed_image(entry, raw_summary, content)
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
                    image_url=image_url,
                    allow_live_summary_fetch=False,
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
                prefill_email=email,
                remember_checked=bool(remember)
            )

        return complete_login(user, bool(remember))

    remembered_email = request.cookies.get("remembered_email", "")
    return render_auth_page("login.html", prefill_email=remembered_email, remember_checked=bool(remembered_email))

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

        if not is_strong_password(password):
            return render_auth_page(
                "signup.html",
                page_error=PASSWORD_RULE_TEXT,
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
        expires_at = (now_local() + timedelta(seconds=OTP_EXPIRY_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
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
        session.pop("password_reset_verified_email", None)
        return render_auth_page(
            "verify_otp.html",
            page_success="OTP sent to your email. Verify it first, then create your new password.",
            prefill_email=email,
            otp_expires_seconds=OTP_EXPIRY_SECONDS,
            otp_verified=False
        )

    return render_auth_page("forgot_password.html")

@app.route("/verify-reset-otp", methods=["GET", "POST"])
def verify_reset_otp():
    if request.method == "POST":
        email = safe_text(request.form.get("email")).strip().lower()
        otp_code = safe_text(request.form.get("otp_code")).strip()

        if not email or not otp_code:
            return render_auth_page(
                "verify_otp.html",
                page_error="Please enter your email and OTP.",
                prefill_email=email,
                otp_expires_seconds=latest_otp_remaining_seconds(email),
                otp_verified=False
            )

        otp_row = get_valid_password_reset_otp(email, otp_code)
        if not otp_row:
            remaining_seconds = latest_otp_remaining_seconds(email)
            return render_auth_page(
                "verify_otp.html",
                page_error="Invalid OTP. Please try again." if remaining_seconds else "OTP has expired. Please request a new one.",
                prefill_email=email,
                otp_expires_seconds=remaining_seconds,
                otp_verified=False
            )

        if otp_remaining_seconds(otp_row["expires_at"]) <= 0:
            return render_auth_page(
                "verify_otp.html",
                page_error="OTP has expired. Please request a new one.",
                prefill_email=email,
                otp_expires_seconds=0,
                otp_verified=False
            )

        session["password_reset_verified_email"] = email
        return render_auth_page(
            "reset_password.html",
            page_success="OTP verified. Create your new password now.",
            prefill_email=email
        )

    return render_auth_page("verify_otp.html")

@app.route("/reset-password", methods=["POST"])
def reset_password_after_otp():
    email = safe_text(request.form.get("email")).strip().lower()
    new_password = safe_text(request.form.get("new_password")).strip()
    confirm_password = safe_text(request.form.get("confirm_password")).strip()

    if not email or session.get("password_reset_verified_email") != email:
        return render_auth_page(
            "verify_otp.html",
            page_error="Please verify your OTP first.",
            prefill_email=email,
            otp_expires_seconds=0,
            otp_verified=False
        )

    if new_password != confirm_password:
        return render_auth_page(
            "reset_password.html",
            page_error="Passwords do not match.",
            prefill_email=email
        )

    if not is_strong_password(new_password):
        return render_auth_page(
            "reset_password.html",
            page_error=PASSWORD_RULE_TEXT,
            prefill_email=email
        )

    updated = update_user_password(
        email,
        generate_password_hash(new_password),
        now_local().strftime("%d-%m-%Y %I:%M %p")
    )
    if not updated:
        return render_auth_page(
            "forgot_password.html",
            page_error="No account found with that email. Please create an account first.",
            prefill_email=email
        )

    user = get_user_by_email(email)
    if user:
        log_activity(user["id"], "password_reset_success", f"Password reset completed for {email}", now_local().strftime("%d-%m-%Y %I:%M %p"))
    session.pop("password_reset_verified_email", None)
    return render_auth_page(
        "login.html",
        page_success="Password updated successfully. Please log in.",
        prefill_email=email
    )

@app.route("/resend-reset-otp", methods=["POST"])
def resend_reset_otp():
    email = safe_text(request.form.get("email")).strip().lower()
    user = get_user_by_email(email) if email else None
    if not user:
        return render_auth_page(
            "forgot_password.html",
            page_error="No account found with that email. Please create an account first.",
            prefill_email=email
        )

    otp_code = f"{secrets.randbelow(900000) + 100000}"
    expires_at = (now_local() + timedelta(seconds=OTP_EXPIRY_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
    store_password_reset_otp(email, otp_code, expires_at, now_local().strftime("%d-%m-%Y %I:%M %p"))

    try:
        send_reset_otp_email(email, otp_code)
    except Exception as e:
        return render_auth_page(
            "verify_otp.html",
            page_error=f"{safe_text(str(e)) or 'Unable to send OTP email right now.'}",
            prefill_email=email,
            otp_expires_seconds=latest_otp_remaining_seconds(email),
            otp_verified=False
        )

    log_activity(user["id"], "password_reset_requested", f"OTP resent to {email}", now_local().strftime("%d-%m-%Y %I:%M %p"))
    session.pop("password_reset_verified_email", None)
    return render_auth_page(
        "verify_otp.html",
        page_success="A new OTP has been sent to your email.",
        prefill_email=email,
        otp_expires_seconds=OTP_EXPIRY_SECONDS,
        otp_verified=False
    )

@app.route("/logout")
def logout():
    user = current_user()
    if user:
        log_activity(user["id"], "logout", "User logged out", now_local().strftime("%d-%m-%Y %I:%M %p"))
    session.clear()
    return redirect("/login")

def build_profile_stats(user):
    rows = get_user_activity_rows(1000)
    saved_rows = get_saved(user["id"]) if user else []
    today = today_local_date()
    daily_seconds_map = {today - timedelta(days=offset): 0 for offset in range(6, -1, -1)}
    daily_reading = []
    search_counter = Counter()
    category_counter = Counter()
    total_searches = 0
    total_seconds = 0

    for row in rows:
        event_type = safe_text(row["event_type"])
        details = safe_text(row["details"])
        row_dt = parse_activity_time(row["created_at"])
        if event_type == "search":
            term = details.replace("Searched topic:", "").strip()
            if term:
                search_counter[term] += 1
                total_searches += 1
        elif event_type == "category_view":
            cat = details.replace("Opened category:", "").strip().title()
            if cat:
                category_counter[cat] += 1
        elif event_type == "article_click":
            cat = extract_detail_value(details, "category").strip().title()
            if cat:
                category_counter[cat] += 1
        elif event_type == "reading_time":
            try:
                seconds = int(extract_detail_value(details, "seconds") or "0")
            except Exception:
                seconds = 0
            total_seconds += seconds
            if row_dt and row_dt.date() in daily_seconds_map:
                daily_seconds_map[row_dt.date()] += seconds

    for day, day_seconds in sorted(daily_seconds_map.items()):
        daily_reading.append({
            "date": day.strftime("%d %b"),
            "time": format_seconds_compact(day_seconds),
            "seconds": day_seconds,
        })

    return {
        "created_at": user["created_at"] or "Not available",
        "last_login_at": user["last_login_at"] or "Not yet",
        "saved_count": len(saved_rows),
        "search_count": total_searches,
        "reading_time": format_seconds_compact(total_seconds) if total_seconds else "<1m",
        "favorite_category": category_counter.most_common(1)[0][0] if category_counter else "Not enough activity yet",
        "top_searched_topic": search_counter.most_common(1)[0][0] if search_counter else "No searches yet",
        "most_searched": [{"topic": topic, "count": count} for topic, count in search_counter.most_common(5)],
        "daily_reading": daily_reading,
        "recent_saved": list(saved_rows[:5]),
    }

@app.route("/profile")
def profile_page():
    user = current_user()
    if is_admin_user(user):
        return redirect("/admin")
    context = build_base_context(active="settings")
    context.update({"profile_stats": build_profile_stats(user)})
    return render_template("profile.html", **context)

@app.route("/update-password", methods=["GET", "POST"])
def update_password_page():
    user = current_user()
    page_error = ""
    page_success = ""
    if request.method == "POST":
        current_password = safe_text(request.form.get("current_password")).strip()
        new_password = safe_text(request.form.get("new_password")).strip()
        confirm_password = safe_text(request.form.get("confirm_password")).strip()

        if not current_password or not new_password or not confirm_password:
            page_error = "Please fill all password fields."
        elif not check_password_hash(user["password_hash"], current_password):
            page_error = "Current password is incorrect."
        elif new_password != confirm_password:
            page_error = "New password and confirm password do not match."
        elif not is_strong_password(new_password):
            page_error = PASSWORD_RULE_TEXT
        else:
            update_user_password(
                user["email"],
                generate_password_hash(new_password),
                now_local().strftime("%d-%m-%Y %I:%M %p")
            )
            log_user_event("password_change", "User updated password from profile menu")
            page_success = "Password updated successfully."

    context = build_base_context(active="settings")
    context.update({
        "page_error": page_error,
        "page_success": page_success,
        "password_rule_text": PASSWORD_RULE_TEXT,
    })
    return render_template("update_password.html", **context)

@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        action = safe_text(request.form.get("action")).strip()
        if action == "delete_account":
            user = current_user()
            if user:
                deactivate_user(user["id"], now_local().strftime("%d-%m-%Y %I:%M %p"))
                log_activity(user["id"], "account_deactivated", "User deleted account", now_local().strftime("%d-%m-%Y %I:%M %p"))
            session.clear()
            return render_auth_page(
                "signup.html",
                page_success="Your account was deleted. Please create a new account if you want to use InformaX AI again."
            )

        if "default_country" in request.form:
            selected_country = safe_text(request.form.get("default_country") or "WORLD").strip().upper()
            if selected_country not in {cc for _, cc in COUNTRY_OPTIONS}:
                selected_country = "WORLD"
            session["selected_country"] = selected_country
        theme_preference = safe_text(request.form.get("theme_preference") or "system").strip().lower()
        if theme_preference not in {"light", "dark", "system"}:
            theme_preference = "system"
        session["theme_preference"] = theme_preference
        if request.form.get("reset_preferences"):
            session["selected_country"] = "WORLD"
            session["selected_source"] = ""
            session["typed_country"] = ""
            session.pop("last_search_topic", None)
        context = build_base_context(active="settings")
        context.update({
            "settings_saved": True,
            "support_email": "informaxai.support@gmail.com",
            "selected_theme_preference": session.get("theme_preference", "system"),
        })
        return render_template("settings.html", **context)

    context = build_base_context(active="settings")
    context.update({
        "settings_saved": False,
        "support_email": "informaxai.support@gmail.com",
        "selected_theme_preference": session.get("theme_preference", "system"),
    })
    return render_template("settings.html", **context)

@app.route("/help-support")
def help_support_page():
    context = build_base_context(active="help")
    context.update({
        "support_email": "informaxai.support@gmail.com",
        "help_items": [
            ("Why is an article marked Real, Fake, or Check?", "The app combines source reputation, article text analysis, and model confidence to assign a credibility label."),
            ("How does date filtering work?", "Today is shown by default. If you pick a previous date, the app tries to fetch only that day's articles."),
            ("Why do some trusted sources show fewer articles?", "That depends on what NewsAPI and RSS feeds publish or index for the selected day."),
            ("How do saved articles work?", "Saved articles are linked to the logged-in user only, so one user cannot see another user's saved list."),
        ]
    })
    return render_template("help_support.html", **context)

def filter_rows_by_date(rows, selected_date=""):
    if not selected_date:
        return list(rows)
    target = parse_selected_date(selected_date)
    if not target:
        return list(rows)
    filtered = []
    for row in rows:
        dt = parse_activity_time(row["created_at"])
        if dt and dt.date() == target:
            filtered.append(row)
    return filtered

def filter_rows_by_date_field(rows, selected_date="", field_name="created_at"):
    if not selected_date:
        return list(rows)
    target = parse_selected_date(selected_date)
    if not target:
        return list(rows)
    filtered = []
    for row in rows:
        dt = parse_activity_time(row[field_name])
        if dt and dt.date() == target:
            filtered.append(row)
    return filtered

def format_seconds_compact(total_seconds):
    seconds = max(0, int(total_seconds or 0))
    if seconds < 60:
        return "<1m"
    minutes = seconds // 60
    hours = minutes // 60
    mins = minutes % 60
    if hours:
        return f"{hours}h {mins}m"
    return f"{minutes}m"

def activity_rows_in_window(rows, start_date, end_date):
    filtered = []
    for row in rows:
        dt = parse_activity_time(row["created_at"])
        if dt and start_date <= dt.date() <= end_date:
            filtered.append(row)
    return filtered

def article_meta_from_rows(rows):
    meta = {}
    for row in rows:
        if safe_text(row["event_type"]) != "article_click":
            continue
        details = safe_text(row["details"])
        title = extract_detail_value(details, "title").strip()
        if not title:
            continue
        meta[title.lower()] = {
            "source": extract_detail_value(details, "source").strip() or "Unknown",
            "category": extract_detail_value(details, "category").strip().title() or "General",
        }
    return meta

@app.route("/admin/clear-activity", methods=["POST"])
@admin_required
def admin_clear_activity():
    from db import get_conn
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM user_activity")
    cur.execute("DELETE FROM password_reset_otp")
    conn.commit()
    conn.close()
    CACHE.clear()
    return redirect("/admin")

@app.route("/admin/clear-users", methods=["POST"])
@admin_required
def admin_clear_users():
    clear_admin_data(ADMIN_EMAIL)
    CACHE.clear()
    return redirect("/admin")

@app.route("/admin")
@admin_required
def admin_dashboard():
    admin_user = current_user()
    all_users = get_all_users()
    admin_ids = {int(u["id"]) for u in all_users if is_admin_user(u)}
    users = [u for u in all_users if int(u["id"]) not in admin_ids]
    active_user_ids = {int(u["id"]) for u in users if int(u["is_active"] if "is_active" in u.keys() else 1)}
    today = today_local_date()
    selected_date = (request.args.get("date") or today.strftime("%Y-%m-%d")).strip()
    view_date = parse_selected_date(selected_date) or today
    selected_date_text = view_date.strftime("%Y-%m-%d")
    is_today_view = view_date == today
    selected_date_title = "Today" if is_today_view else "Selected Date"
    selected_date_textual = view_date.strftime("%A, %d %B %Y")
    selected_date_lower = "today" if is_today_view else "the selected date"

    all_activity_rows = get_recent_activity(5000)
    activity_rows = [
        r for r in all_activity_rows
        if not (r["user_id"] and int(r["user_id"]) in admin_ids)
    ]
    view_activity_rows = filter_rows_by_date(activity_rows, selected_date_text)

    seven_day_start = view_date - timedelta(days=6)
    trend_rows = activity_rows_in_window(activity_rows, seven_day_start, view_date)
    article_meta = article_meta_from_rows(activity_rows)

    otp_rows_all = [
        r for r in get_recent_password_reset_requests(300)
        if safe_text(r["email"]).strip().lower() != safe_text(ADMIN_EMAIL)
    ]
    view_otp_rows = filter_rows_by_date(otp_rows_all, selected_date_text)

    daily_labels = []
    reading_trend_values = []
    active_user_trend_values = []
    for offset in range(7):
        day = seven_day_start + timedelta(days=offset)
        daily_labels.append(day.strftime("%d %b"))
        day_rows = [r for r in trend_rows if (parse_activity_time(r["created_at"]) and parse_activity_time(r["created_at"]).date() == day)]
        day_seconds = 0
        active_users = set()
        for row in day_rows:
            if row["user_id"] and int(row["user_id"]) in active_user_ids:
                active_users.add(int(row["user_id"]))
            if safe_text(row["event_type"]) == "reading_time":
                try:
                    day_seconds += int(extract_detail_value(safe_text(row["details"]), "seconds") or "0")
                except Exception:
                    pass
        reading_trend_values.append(round(day_seconds / 60, 1))
        active_user_trend_values.append(len(active_users))

    search_counter = Counter()
    category_counter = Counter()
    article_view_counter = Counter()
    most_used_source_counter = Counter()
    for row in view_activity_rows:
        event_type = safe_text(row["event_type"])
        details = safe_text(row["details"])
        if event_type == "search":
            search_term = details.replace("Searched topic:", "").strip()
            if search_term:
                search_counter[search_term] += 1
        elif event_type == "category_view":
            category_name = details.replace("Opened category:", "").strip().title()
            if category_name:
                category_counter[category_name] += 1
        elif event_type == "article_click":
            title = extract_detail_value(details, "title").strip()
            source = extract_detail_value(details, "source").strip() or "Unknown"
            category_name = extract_detail_value(details, "category").strip().title() or "General"
            if title:
                article_view_counter[title] += 1
            category_counter[category_name] += 1
            most_used_source_counter[source_display_name(source)] += 1

    top_search_labels = [name for name, _ in search_counter.most_common(10)]
    top_search_values = [count for _, count in search_counter.most_common(10)]
    category_chart_labels = [name for name, _ in category_counter.most_common(8)]
    category_chart_values = [count for _, count in category_counter.most_common(8)]

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT s.title, s.link, s.saved_at, u.id AS user_id, u.email
        FROM user_saved_articles s
        JOIN users u ON u.id = s.user_id
        WHERE COALESCE(u.is_admin, 0) = 0
        ORDER BY s.id DESC
    """)
    all_saved_rows = cur.fetchall()
    conn.close()

    view_saved_rows = filter_rows_by_date_field(all_saved_rows, selected_date_text, field_name="saved_at")
    saved_counts = Counter()
    saved_article_counter = Counter()
    saved_article_latest_link = {}
    for row in view_saved_rows:
        email_key = safe_text(row["email"]).strip().lower()
        title = safe_text(row["title"]).strip()
        link = safe_text(row["link"]).strip()
        if email_key:
            saved_counts[email_key] += 1
        if title:
            saved_article_counter[title] += 1
            if title not in saved_article_latest_link and link:
                saved_article_latest_link[title] = link

    most_viewed_articles = []
    for title, views in article_view_counter.most_common(8):
        meta = article_meta.get(title.lower(), {})
        most_viewed_articles.append({
            "title": title,
            "views": views,
            "source": source_display_name(meta.get("source", "")),
            "category": meta.get("category", "General"),
        })

    most_saved_articles = []
    for title, saves in saved_article_counter.most_common(8):
        link = saved_article_latest_link.get(title, "")
        meta = article_meta.get(safe_text(title).strip().lower(), {})
        most_saved_articles.append({
            "title": title,
            "saves": int(saves or 0),
            "source": source_display_name(meta.get("source", get_domain(link))),
            "category": meta.get("category", "General"),
        })

    event_badge_map = {
        "login": ("Login", "#1fd28a"),
        "logout": ("Logout", "#ff7b72"),
        "search": ("Search", "#4da3ff"),
        "category_view": ("Category", "#9b6bff"),
        "article_click": ("Read", "#00c2a8"),
        "save_add": ("Save", "#ffb020"),
        "save_remove": ("Remove", "#ff8a5b"),
        "password_reset_requested": ("Reset", "#f06292"),
        "password_reset_success": ("Reset", "#f06292"),
        "page_view": ("Visit", "#7aa2ff"),
    }
    timeline_rows = []
    for row in view_activity_rows[:20]:
        label, color = event_badge_map.get(safe_text(row["event_type"]), (safe_text(row["event_type"]).replace("_", " ").title(), "#7aa2ff"))
        timeline_rows.append({
            "time": row["created_at"],
            "user_name": row["user_name"] or "Unknown User",
            "details": row["details"],
            "label": label,
            "color": color,
        })

    source_day_articles = []
    try:
        source_day_articles = fetch_articles(
            mode="home",
            query=None,
            category=None,
            selected_date=selected_date_text,
            country_code="WORLD",
            source_domain_filter="",
            country_text=""
        )
        source_day_articles = filter_articles_by_exact_date(remove_duplicates(source_day_articles), selected_date_text)
    except Exception:
        source_day_articles = []

    try:
        trusted_sections_today = build_trusted_source_sections(
            selected_date=selected_date_text,
            enable_direct_fetch=False
        )
    except Exception:
        trusted_sections_today = []

    failed_sources = []
    latest_only_sources = []
    for section in trusted_sections_today:
        category_name = safe_text(section.get("category", "")).strip()
        for source in section.get("sources", []):
            source_name = safe_text(source.get("name", "")).strip() or "Unknown Source"
            source_status = {
                "name": source_name,
                "category": category_name,
                "coverage_mode": safe_text(source.get("coverage_mode", "")).strip(),
                "coverage_note": safe_text(source.get("coverage_note", "")).strip(),
            }
            if int(source.get("headline_count", 0) or 0) == 0:
                failed_sources.append(source_status)
            elif source_status["coverage_mode"] == "latest":
                latest_only_sources.append(source_status)

    failed_source_fetch_count = len(failed_sources)
    failed_source_names_text = summarize_source_names([item["name"] for item in failed_sources], limit=8)
    latest_only_names_text = summarize_source_names([item["name"] for item in latest_only_sources], limit=6)
    failed_source_groups = group_source_names_by_category(failed_sources)
    active_users_today = len({int(r["user_id"]) for r in view_activity_rows if r["user_id"] and int(r["user_id"]) in active_user_ids})
    total_searches_today = sum(1 for r in view_activity_rows if safe_text(r["event_type"]) == "search")
    total_reading_seconds_today = 0
    for row in view_activity_rows:
        if safe_text(row["event_type"]) == "reading_time":
            try:
                total_reading_seconds_today += int(extract_detail_value(safe_text(row["details"]), "seconds") or "0")
            except Exception:
                pass

    active_alerts = []
    if not API_KEY or API_KEY == "your_real_newsapi_key_here":
        active_alerts.append({
            "level": "warning",
            "title": "NewsAPI needs configuration",
            "detail": "The app can still use RSS and source fallbacks, but NewsAPI should be configured for stronger article coverage.",
            "time": now_local().strftime("%I:%M %p")
        })
    if failed_source_fetch_count:
        failed_sources_detail = f"These trusted sources did not return a matched headline for {selected_date_lower}."
        if latest_only_sources:
            failed_sources_detail += f" Latest fallback headlines were still found for {len(latest_only_sources)} sources."
        active_alerts.append({
            "level": "warning",
            "title": f"{failed_source_fetch_count} trusted sources had no headlines for {selected_date_lower}",
            "detail": failed_sources_detail,
            "source_groups": failed_source_groups,
            "time": now_local().strftime("%I:%M %p")
        })
    if not source_day_articles:
        active_alerts.append({
            "level": "danger",
            "title": f"No articles fetched for {selected_date_lower}",
            "detail": "The home feed for the selected date is empty, so users may see missing content until refresh succeeds.",
            "time": now_local().strftime("%I:%M %p")
        })
    if len(view_otp_rows) >= 4:
        active_alerts.append({
            "level": "warning",
            "title": f"Password reset requests are unusually high for {selected_date_lower}",
            "detail": f"{len(view_otp_rows)} reset requests were recorded for {selected_date_lower}, which is above the alert threshold of 4.",
            "time": now_local().strftime("%I:%M %p")
        })
    fake_count = sum(1 for a in source_day_articles if safe_text(a.get("label")) == "Fake")
    check_count = sum(1 for a in source_day_articles if safe_text(a.get("label")) == "Check")
    suspicious_count = fake_count + check_count
    suspicious_threshold = max(8, int(math.ceil(len(source_day_articles) * 0.40))) if source_day_articles else 8
    if suspicious_count >= suspicious_threshold or fake_count >= 3:
        if fake_count == 0 and check_count:
            suspicious_title = f"Many articles were marked Check for {selected_date_lower}"
            suspicious_detail = (
                f"This does not mean fake news. It means {check_count} of {len(source_day_articles)} home-feed articles "
                f"were marked Check and should be manually verified before trusting them fully. "
                f"Alert threshold: {suspicious_threshold} Check/Fake labels or at least 3 Fake labels."
            )
        else:
            suspicious_title = f"Some articles were marked Fake or Check for {selected_date_lower}"
            suspicious_detail = (
                f"{suspicious_count} of {len(source_day_articles)} home-feed articles were labeled "
                f"Fake/Check ({fake_count} Fake, {check_count} Check). "
                f"Check means manual verification is needed. Alert threshold: {suspicious_threshold} Check/Fake labels "
                f"or at least 3 Fake labels."
            )
        active_alerts.append({
            "level": "warning",
            "title": suspicious_title,
            "detail": suspicious_detail,
            "time": now_local().strftime("%I:%M %p")
        })

    try:
        db_status = "Healthy"
        _ = get_all_users()
    except Exception:
        db_status = "Issue"

    system_health_items = [
        {"label": "NewsAPI Status", "status": "Operational" if API_KEY and API_KEY != "your_real_newsapi_key_here" else "Needs Setup", "meta": "Primary live article provider"},
        {"label": "RSS Feed Status", "status": "Operational" if SOURCE_FEED_MAP else "Check", "meta": f"{len(SOURCE_FEED_MAP)} source feeds configured"},
        {"label": "Last Successful Refresh", "status": now_local().strftime("%I:%M %p"), "meta": "Latest admin refresh time"},
        {"label": f"Articles Fetched For {selected_date_title}", "status": str(len(source_day_articles)), "meta": f"Home feed article count for {selected_date_textual}"},
        {
            "label": "Failed Source Fetch Count",
            "status": str(failed_source_fetch_count),
            "meta": (
                f"No headlines for {selected_date_lower}: {failed_source_names_text}"
                if failed_source_fetch_count and failed_source_names_text
                else (
                    f"All trusted sources returned news. Latest-only fallback used for: {latest_only_names_text}"
                    if latest_only_names_text
                    else "All trusted sources returned at least one article."
                )
            )
        },
        {"label": "Database Status", "status": db_status, "meta": "User and activity storage"},
    ]

    summary_cards = [
        {"title": "Total Users", "value": len(users), "accent": "#4da3ff", "subtext": "Registered non-admin accounts"},
        {"title": f"Active Users {selected_date_title}", "value": active_users_today, "accent": "#1fd28a", "subtext": f"Unique users active on {selected_date_lower}"},
        {"title": f"Total Searches {selected_date_title}", "value": total_searches_today, "accent": "#00c2a8", "subtext": f"Search actions recorded on {selected_date_lower}"},
        {"title": f"Total Reading Time {selected_date_title}", "value": format_seconds_compact(total_reading_seconds_today), "accent": "#ffb020", "subtext": f"User time spent in app on {selected_date_lower}"},
        {"title": f"Saved Articles {selected_date_title}", "value": sum(saved_counts.values()), "accent": "#ff6b9c", "subtext": f"Articles saved by users on {selected_date_lower}"},
        {"title": f"Articles Fetched {selected_date_title}", "value": len(source_day_articles), "accent": "#36cfc9", "subtext": f"Articles available for {selected_date_lower}"},
        {"title": "Active Alerts", "value": len(active_alerts), "accent": "#ff6b6b", "subtext": "Alerts needing attention"},
        {"title": "System Health", "value": "Healthy" if not active_alerts else "Monitor", "accent": "#2ecc71", "subtext": "Overall platform status"},
    ]

    user_rows = []
    dataset_rows = []
    for user in users:
        email_key = safe_text(user["email"]).strip().lower()
        display_email = safe_text(user["original_email"] if "original_email" in user.keys() and user["original_email"] else user["email"]).strip().lower()
        active_status = int(user["is_active"] if "is_active" in user.keys() else 1)
        per_user_activity = [r for r in view_activity_rows if safe_text(r["user_email"]).strip().lower() == email_key]
        per_user_searches = sum(1 for r in per_user_activity if safe_text(r["event_type"]) == "search")
        per_user_logins = sum(1 for r in per_user_activity if safe_text(r["event_type"]) == "login")
        per_user_logouts = sum(1 for r in per_user_activity if safe_text(r["event_type"]) == "logout")
        per_user_reading_seconds = 0
        per_user_category_counter = Counter()
        for row in per_user_activity:
            event_type = safe_text(row["event_type"])
            details = safe_text(row["details"])
            if event_type == "reading_time":
                try:
                    per_user_reading_seconds += int(extract_detail_value(details, "seconds") or "0")
                except Exception:
                    pass
            if event_type == "category_view":
                cat = details.replace("Opened category:", "").strip().title()
            elif event_type == "article_click":
                cat = extract_detail_value(details, "category").strip().title()
            else:
                cat = ""
            if cat:
                per_user_category_counter[cat] += 1
        user_rows.append({
            "name": user["name"],
            "email": display_email,
            "last_login_at": user["last_login_at"] or "Not yet",
            "search_count": per_user_searches,
            "saved_count": int(saved_counts.get(email_key, 0)),
            "reading_time": format_seconds_compact(per_user_reading_seconds) if per_user_reading_seconds else "-",
            "most_used_category": per_user_category_counter.most_common(1)[0][0] if per_user_category_counter else "-",
            "engagement_score": (per_user_searches * 2) + int(saved_counts.get(email_key, 0)) + (per_user_reading_seconds // 60),
        })
        dataset_rows.append({
            "name": user["name"],
            "email": display_email,
            "role": "User" if active_status else "Inactive",
            "last_login_at": user["last_login_at"] or "Not yet",
            "login_count": per_user_logins,
            "logout_count": per_user_logouts,
            "search_count": per_user_searches,
            "saved_count": int(saved_counts.get(email_key, 0)),
            "password_status": "Protected",
            "password_updated_at": user["password_updated_at"] or user["created_at"] or "Not available",
            "created_at": user["created_at"] or "Not available",
            "status": "Active" if active_status else "Inactive",
        })
    user_rows = sorted(user_rows, key=lambda x: x["engagement_score"], reverse=True)
    dataset_rows = sorted(dataset_rows, key=lambda x: (x["status"] == "Active", x["login_count"]), reverse=True)

    return render_template(
        "admin.html",
        admin_user=admin_user,
        selected_date=selected_date_text,
        max_date=today.strftime("%Y-%m-%d"),
        summary_cards=summary_cards,
        reading_trend_labels=daily_labels,
        reading_trend_values=reading_trend_values,
        active_user_trend_labels=daily_labels,
        active_user_trend_values=active_user_trend_values,
        top_search_labels=top_search_labels,
        top_search_values=top_search_values,
        category_chart_labels=category_chart_labels,
        category_chart_values=category_chart_values,
        most_viewed_articles=most_viewed_articles,
        most_saved_articles=most_saved_articles,
        timeline_rows=timeline_rows,
        system_health_items=system_health_items,
        active_alerts=active_alerts,
        engaged_users=user_rows,
        dataset_rows=dataset_rows,
        view_date_text=selected_date_textual,
        selected_date_title=selected_date_title,
        selected_date_lower=selected_date_lower,
        most_used_source=most_used_source_counter.most_common(1)[0][0] if most_used_source_counter else "-",
    )

@app.route("/admin/open-app")
@admin_required
def admin_open_app():
    session["admin_app_mode"] = True
    return redirect("/")

@app.route("/admin/back")
@admin_required
def admin_back_to_dashboard():
    session["admin_app_mode"] = False
    return redirect("/admin")

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
    uid = current_user_id()
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

    if is_saved(link, uid):
        delete_saved_by_link(link, uid)
        log_user_event("save_remove", f"Removed saved article: {title or link}")
        return jsonify({"ok": True, "saved": False}), 200

    save_article(uid, title or "No title", link, label or "Real", score_val, saved_at)
    log_user_event("save_add", f"Saved article: {title or link}")
    return jsonify({"ok": True, "saved": True}), 200

@app.route("/saved")
def saved():
    rows = get_saved(current_user_id())
    log_user_event("saved_view", "Opened saved articles page")
    context = build_base_context(active="saved")
    context["saved_rows"] = rows
    return render_template("saved.html", **context)

@app.route("/remove_saved/<int:article_id>", methods=["POST"])
def remove_saved(article_id):
    delete_saved(article_id, current_user_id())
    return redirect("/saved")

@app.route("/latest_saved_json")
def latest_saved_json():
    rows = get_saved(current_user_id())
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
    return jsonify({"ok": True})

@app.route("/track_presence", methods=["POST"])
def track_presence():
    seconds = safe_text(request.form.get("seconds")).strip()
    try:
        sec_val = max(0, min(int(seconds), 3600))
    except Exception:
        sec_val = 0
    if sec_val:
        log_user_event("reading_time", f"seconds={sec_val}")
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
    selected_date = request.args.get("date", "").strip() or None
    context = build_base_context(active="home", selected_date=selected_date)
    context["source_showcase"] = SOURCE_SHOWCASE
    context["today_text"] = (parse_selected_date(selected_date) or today_local_date()).strftime("%A, %d %B %Y")
    return render_template("trusted_sources.html", **context)

@app.route("/source")
def source_filter():
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

@app.route("/api/source-news")
def api_source_news():
    query = request.args.get("query")
    selected_date = request.args.get("date", "").strip() or None
    try:
        articles = fetch_source_articles(query, selected_date=selected_date)
        return jsonify({"articles": articles})
    except Exception as e:
        print("SOURCE API ERROR:", e)
        return jsonify({"articles": []}), 500

@app.route("/source/<query>")
def source_news(query):
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

@app.route("/save_article", methods=["POST"])
def save_article_api():
    uid = current_user_id()

    data = request.get_json()

    title = data.get("title")
    link = data.get("link")
    summary = data.get("summary")
    label = data.get("label")

    if not link:
        return jsonify({"status": "error"}), 400

    # ✅ Use your EXISTING DB function
    if is_saved(link, uid):
        delete_saved_by_link(link, uid)
        log_user_event("save_remove", f"Removed saved article: {title or link}")
        return jsonify({"status": "removed"})

    save_article(uid, title or "No Title", link, label or "Real", 0.8, datetime.now().strftime("%d-%m-%Y %I:%M %p"))
    log_user_event("save_add", f"Saved article: {title or link}")

    return jsonify({"status": "saved"})

@app.route("/dismiss_breaking", methods=["POST"])
def dismiss_breaking():
    session["dismiss_breaking"] = True
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)

