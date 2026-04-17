from flask import Flask, render_template, request, redirect, jsonify, session, g, has_request_context
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

            response = http_get(url, params=page_params, timeout=adaptive_http_timeout(3.0, 4.0, 6.0))
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
from app_constants import (
    BREAKING_KEYWORDS, CATEGORY_QUERY, COUNTRY_CODE_TO_NAME, COUNTRY_NAME_TO_CODE,
    COUNTRY_OPTIONS, PUBLISHER_STOPWORDS, SENSATIONAL_WORDS, SOURCE_DOMAIN_ALIASES,
    SOURCE_FEED_MAP, SOURCE_FETCH_VARIANTS, SOURCE_OPTIONS, SOURCE_QUERY_MAP,
    SOURCE_ROUTE_DOMAIN_MAP, SOURCE_SHOWCASE, STOPWORDS, SUMMARY_STOPWORDS,
    TRUSTED_ONLY_CATEGORIES, TRUSTED_SHOWCASE_QUERY_MAP,
)
from app_auth import register_auth_routes
from app_admin import register_admin_routes

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

TIMESTAMP_FORMAT = "%d-%m-%Y %I:%M %p"
SUPPORT_EMAIL = "informaxai.support@gmail.com"
CATEGORY_ALLOWLIST = {
    "technology", "business", "health", "sports",
    "politics", "entertainment", "disaster", "climate",
}
SOCIAL_PROVIDER_META = {
    "google": ("Google", "google"),
    "microsoft": ("Microsoft", "microsoft"),
}
HELP_ITEMS = [
    ("Why is an article marked Real, Fake, or Check?", "The app combines source reputation, article text analysis, and model confidence to assign a credibility label."),
    ("How does date filtering work?", "Today is shown by default. If you pick a previous date, the app tries to fetch only that day's articles."),
    ("Why do some trusted sources show fewer articles?", "That depends on what NewsAPI and RSS feeds publish or index for the selected day."),
    ("How do saved articles work?", "Saved articles are linked to the logged-in user only, so one user cannot see another user's saved list."),
]

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
        log_activity(user["id"], event_type, details, now_text())
    except Exception:
        pass

def current_user_id():
    user = current_user()
    return int(user["id"]) if user else 0

def current_saved_links():
    if not has_request_context():
        return set()
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
    for fmt in (TIMESTAMP_FORMAT, "%Y-%m-%d %H:%M:%S"):
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
LIVE_CACHE_TTL_SECONDS = 60
SOURCE_FEED_CACHE_TTL_SECONDS = 180
TRUSTED_SHOWCASE_CACHE_TTL_SECONDS = 120
ARTICLE_TEXT_CACHE_TTL_SECONDS = 1800
TREND_CACHE_TTL_SECONDS = 300
NETWORK_LATENCY_SAMPLES = deque(maxlen=8)
NETWORK_PROFILE_LOCK = Lock()
FAST_NETWORK_LATENCY_SECONDS = 1.0
SLOW_NETWORK_LATENCY_SECONDS = 2.5
HTTP_POOL_CONNECTIONS = 16
HTTP_POOL_MAXSIZE = 16
DASHBOARD_SUMMARY_FETCH_LIMIT = 2
SOURCE_SUMMARY_FETCH_LIMIT = 4
DAILY_NEWS_MAX_RESULTS = 60
DAILY_RSS_MAX_RESULTS = 45
SOURCE_PAGE_MAX_RESULTS = 60
NEWSAPI_PAGE_SIZE = 100
NEWSAPI_MAX_PAGES = 2
CACHE_VERSION = "v5"
HOME_SOURCE_SCAN_MAX_WORKERS = 4
HOME_SOURCE_SCAN_PER_SOURCE = 8
INITIAL_FETCH_MAX_WORKERS = 3
THREAD_LOCAL = local()

def cache_ttl_for_key(key):
    cache_key = safe_text(key)
    if cache_key.startswith("dashboard_v4::"):
        return LIVE_CACHE_TTL_SECONDS
    if cache_key.startswith("rss::"):
        return LIVE_CACHE_TTL_SECONDS
    if cache_key.startswith("source_v4::"):
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

def adaptive_http_timeout(fast_timeout, normal_timeout=None, slow_timeout=None):
    normal_value = normal_timeout if normal_timeout is not None else fast_timeout
    slow_value = slow_timeout if slow_timeout is not None else max(fast_timeout, normal_value)
    profile = current_network_profile()
    if profile == "fast":
        return fast_timeout
    if profile == "slow":
        return slow_value
    return normal_value

def adaptive_result_count(requested_count, minimum_count=10):
    requested = max(0, int(requested_count or 0))
    if requested <= 0:
        return 0

    profile = current_network_profile()
    if profile == "fast":
        return requested
    if profile == "slow":
        return min(requested, max(minimum_count, requested // 3))
    return min(requested, max(minimum_count, requested // 2))

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
        endpoint = safe_text(request.endpoint)
        if endpoint in AUTH_ALLOWLIST or endpoint.startswith("admin"):
            response.headers["Cache-Control"] = "no-store, no-cache, max-age=0, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        else:
            response.headers["Cache-Control"] = "private, max-age=30, stale-while-revalidate=90"
            response.headers.pop("Pragma", None)
            response.headers.pop("Expires", None)
    return response

def now_local():
    return datetime.now(APP_TIMEZONE)

def now_text():
    return now_local().strftime(TIMESTAMP_FORMAT)

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

def build_content_preview(text, max_words=90):
    body = normalize_article_body_text(text)
    if not body:
        return ""
    words = body.split()
    if len(words) <= max_words:
        return body
    preview = " ".join(words[:max_words]).strip()
    if preview and not preview.endswith((".", "!", "?")):
        preview += "..."
    return preview

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
            timeout=adaptive_http_timeout(4.0, 6.0, 8.0),
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
                    timeout=adaptive_http_timeout(4.0, 6.0, 8.0),
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
            timeout=adaptive_http_timeout(min(timeout, 2.5), timeout, max(timeout, 5.0)),
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

def create_account_record(name, email, password_hash, activity_message):
    should_be_admin = False
    if ADMIN_EMAIL and email == ADMIN_EMAIL:
        should_be_admin = True
    elif not get_all_users():
        should_be_admin = True

    user_id = create_user(
        name=name,
        email=email,
        password_hash=password_hash,
        created_at=now_text(),
        is_admin=1 if should_be_admin else 0
    )
    log_activity(user_id, "signup", activity_message, now_text())
    return user_id

def store_and_send_password_reset_otp(email):
    otp_code = f"{secrets.randbelow(900000) + 100000}"
    store_password_reset_otp(
        email,
        otp_code,
        (now_local() + timedelta(seconds=OTP_EXPIRY_SECONDS)).strftime("%Y-%m-%d %H:%M:%S"),
        now_text()
    )
    send_reset_otp_email(email, otp_code)

def reset_news_filters(clear_search=False):
    session["selected_country"] = "WORLD"
    session["selected_source"] = ""
    session["typed_country"] = ""
    if clear_search:
        session.pop("last_search_topic", None)
    CACHE.clear()

def render_dashboard_page(mode="home", **kwargs):
    return render_template("dashboard.html", **build_dashboard(mode=mode, **kwargs))

def source_page_context(query, selected_date=None, articles=None, error_msg=None, is_loading=False):
    context = build_base_context(active="home", selected_date=selected_date)
    context.update({
        "articles": articles or [],
        "source_name": query.replace("%20", " ").upper(),
        "source_query": query,
        "today_text": (parse_selected_date(selected_date) or today_local_date()).strftime("%A, %d %B %Y"),
        "max_date": now_local().strftime("%Y-%m-%d"),
        "error_msg": error_msg,
        "is_loading": bool(is_loading),
    })
    return context

def set_article_saved_state(uid, title, link, label="Real", score=0.0, saved_at=None):
    if is_saved(link, uid):
        delete_saved_by_link(link, uid)
        log_user_event("save_remove", f"Removed saved article: {title or link}")
        return False

    save_article(uid, title or "No title", link, label or "Real", score, saved_at or datetime.now().strftime(TIMESTAMP_FORMAT))
    log_user_event("save_add", f"Saved article: {title or link}")
    return True

def settings_context(settings_saved=False):
    context = build_base_context(active="settings")
    context.update({
        "settings_saved": settings_saved,
        "support_email": SUPPORT_EMAIL,
        "selected_theme_preference": session.get("theme_preference", "system"),
    })
    return context

def complete_login(user, remember=False):
    session["welcome_mode"] = "back" if safe_text(user["last_login_at"]).strip() else "welcome"
    session.permanent = bool(remember)
    session["user_id"] = user["id"]
    session["remember_me"] = bool(remember)
    session.pop("password_reset_verified_email", None)
    update_last_login(user["id"], now_text())
    log_activity(user["id"], "login", "User logged in", now_text())
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

def is_today_selected_date(selected_date=None):
    target = parse_selected_date(selected_date)
    return not target or target == today_local_date()

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
        "content_preview": build_content_preview(article_body or description),
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

    if articles:
        set_cache(cache_key, articles)
    return articles

def filter_articles_by_exact_date(articles, selected_date):
    if not selected_date:
        return articles
    return [
        a for a in articles
        if article_matches_date(parse_any_datetime(a.get("published_iso")), selected_date)
    ]

def article_matches_category(article, category):
    category_key = safe_text(category).strip().lower()
    if not category_key:
        return True

    category_terms = set(normalize_topic_words(CATEGORY_QUERY.get(category_key, category_key)))
    category_terms.update(normalize_topic_words(category_key.replace("-", " ")))
    if not category_terms:
        return True

    article_text = " ".join([
        safe_text(article.get("title", "")),
        safe_text(article.get("description", "")),
        safe_text(article.get("summary", "")),
        safe_text(article.get("source", "")),
    ])
    article_terms = set(normalize_topic_words(article_text))

    category_aliases = {
        "sports": {"sports", "cricket", "football", "tennis", "olympics", "fifa", "uefa", "nba", "ipl"},
        "entertainment": {"entertainment", "celebrity", "movie", "movies", "film", "music", "streaming", "bollywood", "hollywood", "ott"},
        "climate": {"climate", "environment", "warming", "pollution", "renewable", "sustainability", "weather", "wildlife", "biodiversity"},
        "disaster": {"accident", "disaster", "earthquake", "tsunami", "flood", "cyclone", "fire", "explosion", "landslide", "storm"},
    }
    if category_key in category_aliases:
        return bool(article_terms.intersection(category_aliases[category_key]))

    return bool(article_terms.intersection(category_terms))

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

def latest_fallback_articles(mode="home", query=None, category=None, country_code="WORLD", source_domain_filter="", country_text=""):
    latest_pool = []
    country_focus = effective_country_query_text(country_code, country_text)
    rss_country_text = country_focus if safe_text(country_text).strip() else ""
    latest_news_limit = adaptive_result_count(18, minimum_count=8)
    latest_rss_limit = adaptive_result_count(24, minimum_count=10)

    fallback_queries = []
    if query:
        fallback_queries.extend([query, f"{query} latest news", f"{query} headlines"])
    if category:
        base_query = CATEGORY_QUERY.get(category, category)
        fallback_queries.extend([base_query, f"{base_query} latest news", f"{base_query} headlines"])
    if not fallback_queries:
        fallback_queries.extend(["top news", "breaking news", "latest news", "world headlines"])

    seen_queries = set()
    for fallback_query in fallback_queries:
        fq = safe_text(fallback_query).strip()
        if not fq or fq.lower() in seen_queries:
            continue
        seen_queries.add(fq.lower())
        try:
            latest_pool.extend(
                newsapi_fetch(
                    query=fq,
                    category=category if mode == "category" else None,
                    selected_date=None,
                    max_results=latest_news_limit,
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
                query=query if mode == "search" else None,
                category=category if mode == "category" else None,
                max_results=latest_rss_limit,
                country_code=country_code,
                source_domain_filter=source_domain_filter,
                country_text=rss_country_text,
                selected_date=None
            )
        )
    except Exception:
        pass

    if mode == "home":
        try:
            latest_pool.extend(
                collect_home_source_day_articles(today_local_date().strftime("%Y-%m-%d"), source_domain_filter=source_domain_filter)
            )
        except Exception:
            pass

    return sorted(
        remove_duplicates([
            item for item in latest_pool
            if is_probable_real_article(item.get("title", ""), item.get("link", ""), item.get("description", ""))
        ]),
        key=lambda x: parse_any_datetime(x.get("published_iso")) or datetime.min,
        reverse=True
    )

def fetch_daily_articles(mode="home", query=None, category=None, selected_date=None, country_code="WORLD", source_domain_filter="", country_text=""):
    target_date = parse_selected_date(selected_date) or today_local_date()
    target_date_text = target_date.strftime("%Y-%m-%d")
    typed_country = safe_text(country_text).strip()
    country_focus = effective_country_query_text(country_code, typed_country)
    daily_news_limit = adaptive_result_count(DAILY_NEWS_MAX_RESULTS, minimum_count=18)
    daily_rss_limit = adaptive_result_count(DAILY_RSS_MAX_RESULTS, minimum_count=15)
    # Dropdown country selection should use the selected locale feed directly.
    # Only explicit typed-country searches should be forced into the RSS query text.
    rss_country_text = country_focus if typed_country else ""

    collected = []
    initial_fetches = {
        "newsapi": lambda: newsapi_fetch(
            query=query,
            category=category,
            selected_date=target_date_text,
            max_results=daily_news_limit,
            source_domain_filter=source_domain_filter,
            country_text=country_focus
        ),
        "rss": lambda: google_rss(
            query=query if mode == "search" else None,
            category=category if mode == "category" else None,
            max_results=daily_rss_limit,
            country_code=country_code,
            source_domain_filter=source_domain_filter,
            country_text=rss_country_text,
            selected_date=target_date_text
        ),
    }
    if mode == "home":
        initial_fetches["home_sources"] = lambda: collect_home_source_day_articles(
            target_date_text,
            source_domain_filter=source_domain_filter
        )

    with ThreadPoolExecutor(max_workers=min(INITIAL_FETCH_MAX_WORKERS, len(initial_fetches))) as executor:
        future_map = {executor.submit(fetcher): name for name, fetcher in initial_fetches.items()}
        for future in as_completed(future_map):
            try:
                items = future.result() or []
            except Exception:
                items = []
            collected.extend(filter_articles_by_exact_date(remove_duplicates(items), target_date_text))

    if mode == "home" and not collected:
        for fallback_query in ["top news", "breaking news", "world news", "latest headlines"]:
            fallback_newsapi = newsapi_fetch(
                query=fallback_query,
                selected_date=target_date_text,
                max_results=adaptive_result_count(30, minimum_count=12),
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
                max_results=adaptive_result_count(40, minimum_count=15),
                source_domain_filter=source_domain_filter,
                country_text=""
            )
            collected.extend(filter_articles_by_exact_date(remove_duplicates(climate_newsapi), target_date_text))

            climate_world_rss = google_rss(
                query=climate_query,
                category=None,
                max_results=adaptive_result_count(40, minimum_count=15),
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
                max_results=adaptive_result_count(35, minimum_count=12),
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
                    max_results=adaptive_result_count(20, minimum_count=10),
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

    if mode == "category" and len(remove_duplicates(collected)) < 8:
        category_feed_items = [
            item for item in filter_articles_by_exact_date(
                remove_duplicates(collect_home_source_day_articles(target_date_text, source_domain_filter=source_domain_filter)),
                target_date_text
            )
            if article_matches_category(item, category)
        ]
        collected.extend(category_feed_items)

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

    if not collected and target_date == today_local_date():
        collected = latest_fallback_articles(
            mode=mode,
            query=query,
            category=category,
            country_code=country_code,
            source_domain_filter=source_domain_filter,
            country_text=country_text
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
    network_profile = current_network_profile()
    dashboard_cache_key = f"dashboard_v4::{CACHE_VERSION}::{uid}::{mode}::{query}::{category}::{selected_date}::{session.get('selected_country','WORLD')}::{session.get('selected_source','')}::{session.get('typed_country','')}"
    cached_dashboard = get_cache(dashboard_cache_key)
    if cached_dashboard is not None and cached_dashboard.get("articles"):
        user_saved_rows = get_saved(uid) if uid else []
        refreshed = dict(cached_dashboard)
        refreshed["latest_saved"] = list(user_saved_rows[:3]) if user_saved_rows else []
        refreshed["activity"] = get_activity_summary(default_category=(category.capitalize() if category else "Technology"))
        refreshed["ai_recommendations"] = build_ai_recommendations(
            refreshed.get("articles", [])[:18],
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
        today_articles = filter_today_news(articles)
        articles = today_articles or articles
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
        breaking_alert = build_breaking_alert(articles) if articles else None
    except Exception:
        breaking_alert = None
    smart_alert = build_smart_alert(articles) if articles else None

    # REMOVE BREAKING FROM MAIN LIST
    if breaking_alert and isinstance(breaking_alert, dict) and breaking_alert.get("headline_links"):
        breaking_links = set(breaking_alert.get("headline_links", []))
        remaining = [a for a in articles if a.get("link") not in breaking_links]

        if len(remaining) >= 3:
            filtered_articles = remaining

    live_summary_budget = min(len(filtered_articles), DASHBOARD_SUMMARY_FETCH_LIMIT)
    if network_profile == "slow":
        live_summary_budget = 0
    elif network_profile == "normal":
        live_summary_budget = min(live_summary_budget, 1)
    filtered_articles = enrich_article_summaries(filtered_articles, live_fetch_budget=live_summary_budget)

    # ALWAYS OUTSIDE
    highlights = filtered_articles[:3]
    calc_items = filtered_articles[:12]
    analysis_items = filtered_articles[:18]

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

    trending_topics = extract_trending_topics(analysis_items, top_n=5)

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

    allow_heavy_analytics = network_profile == "fast"
    sentiment_trend = None
    if allow_heavy_analytics and mode == "search" and query and len(query.strip()) >= 3:
        sentiment_trend = build_sentiment_trend(query, dt, source_domain_filter=source_domain_filter)

    popularity = build_topic_popularity(analysis_items, query=query if mode == "search" else "")
    ai_recommendations = build_ai_recommendations(analysis_items, category=category or query or "")
    source_comparison = None
    if allow_heavy_analytics and mode == "search" and query and len(query.strip()) >= 2:
        source_comparison = build_source_comparison(analysis_items, topic=query)

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
        "auto_refresh_seconds": 45 if network_profile == "fast" else 60 if network_profile == "normal" else 120,
    }

    if filtered_articles:
        set_cache(dashboard_cache_key, result)
    return result

def fetch_source_articles(query, selected_date=None):
    source_key = safe_text(query).replace("%20", " ").strip().lower()
    domain = resolve_source_domain(source_key)
    domain_candidates = source_domain_candidates(domain) or [domain]
    source_phrase = safe_text(SOURCE_QUERY_MAP.get(domain, source_display_name(domain) or source_key.replace(".", " "))).strip()
    selected_day = parse_selected_date(selected_date)
    target_date_text = (selected_day or today_local_date()).strftime("%Y-%m-%d")
    source_news_limit = min(adaptive_result_count(18, minimum_count=8), 18)
    source_target_count = min(source_news_limit, 12)
    source_scan_limit = max(source_news_limit, 60 if selected_day else 24)
    cache_key = f"source_v4::{CACHE_VERSION}::{domain}::{selected_date or now_local().strftime('%Y-%m-%d')}"
    cached = get_cache(cache_key)
    if cached is not None:
        return cached

    def filter_source_items(items, required_date):
        matched = []
        for item in remove_duplicates(items or []):
            published_dt = parse_any_datetime(
                item.get("published_iso")
                or item.get("published_raw")
                or item.get("published")
            )
            if required_date and not article_matches_date(published_dt, required_date):
                continue
            if not article_matches_source_domain(item, domain):
                continue
            if not is_probable_real_article(
                item.get("title", ""),
                item.get("link", ""),
                item.get("description", "")
            ):
                continue
            matched.append(item)
        return matched

    joined_domains = ",".join(domain_candidates)
    primary_fetches = [
        lambda: source_feed_articles(
            domain,
            selected_date=target_date_text,
            max_results=source_scan_limit
        ),
        lambda: google_rss(
            query=None,
            category=None,
            max_results=source_scan_limit,
            country_code="WORLD",
            source_domain_filter=domain,
            country_text="",
            selected_date=target_date_text
        ),
    ]

    if source_phrase:
        primary_fetches.append(
            lambda phrase=source_phrase: google_rss(
                query=phrase,
                category=None,
                max_results=min(source_scan_limit, 40),
                country_code="WORLD",
                source_domain_filter=domain,
                country_text="",
                selected_date=target_date_text
            )
        )
        primary_fetches.append(
            lambda phrase=source_phrase: newsapi_fetch(
                query=phrase,
                selected_date=target_date_text,
                max_results=min(source_scan_limit, 30),
                source_domain_filter=joined_domains,
                country_text=""
            )
        )

    articles = []
    max_workers = max(1, min(4, len(primary_fetches)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fetch_fn) for fetch_fn in primary_fetches]
        for future in as_completed(futures):
            try:
                articles.extend(filter_source_items(future.result() or [], target_date_text))
            except Exception:
                continue

    if len(remove_duplicates(articles)) < source_target_count and source_phrase:
        broad_fetches = [
            lambda phrase=source_phrase: google_rss(
                query=phrase,
                category=None,
                max_results=min(source_scan_limit, 40),
                country_code="WORLD",
                source_domain_filter="",
                country_text="",
                selected_date=target_date_text
            ),
            lambda phrase=source_phrase: newsapi_fetch(
                query=phrase,
                selected_date=target_date_text,
                max_results=min(source_scan_limit, 30),
                source_domain_filter="",
                country_text=""
            ),
        ]
        display_phrase = safe_text(source_display_name(domain)).strip()
        if display_phrase and display_phrase.lower() != source_phrase.lower():
            broad_fetches.append(
                lambda phrase=display_phrase: google_rss(
                    query=phrase,
                    category=None,
                    max_results=min(source_scan_limit, 30),
                    country_code="WORLD",
                    source_domain_filter="",
                    country_text="",
                    selected_date=target_date_text
                )
            )

        with ThreadPoolExecutor(max_workers=max(1, min(3, len(broad_fetches)))) as executor:
            futures = [executor.submit(fetch_fn) for fetch_fn in broad_fetches]
            for future in as_completed(futures):
                try:
                    articles.extend(filter_source_items(future.result() or [], target_date_text))
                except Exception:
                    continue

    articles = sorted(
        remove_duplicates(articles),
        key=lambda x: parse_any_datetime(x.get("published_iso")) or datetime.min,
        reverse=True
    )[:source_news_limit]

    if len(articles) < source_target_count:
        fallback_fetches = [
            lambda: source_feed_articles(
                domain,
                selected_date=None,
                max_results=max(24, source_news_limit)
            ),
            lambda: google_rss(
                query=source_phrase or None,
                category=None,
                max_results=max(24, source_news_limit),
                country_code="WORLD",
                source_domain_filter=domain,
                country_text="",
                selected_date=None
            ),
        ]
        if source_phrase:
            fallback_fetches.append(
                lambda phrase=source_phrase: newsapi_fetch(
                    query=phrase,
                    selected_date=None,
                    max_results=max(18, source_news_limit),
                    source_domain_filter=joined_domains,
                    country_text=""
                )
            )

        with ThreadPoolExecutor(max_workers=max(1, min(4, len(fallback_fetches)))) as executor:
            futures = [executor.submit(fetch_fn) for fetch_fn in fallback_fetches]
            for future in as_completed(futures):
                try:
                    articles.extend(filter_source_items(future.result() or [], target_date_text))
                except Exception:
                    continue

        articles = sorted(
            remove_duplicates(articles),
            key=lambda x: parse_any_datetime(x.get("published_iso")) or datetime.min,
            reverse=True
        )[:source_news_limit]

    source_summary_budget = 1 if articles else 0
    articles = enrich_article_summaries(articles, live_fetch_budget=source_summary_budget)

    if articles:
        set_cache(cache_key, articles)
    return articles

@app.route("/api/article-excerpt")
def api_article_excerpt():
    link = request.args.get("link", "").strip()
    if not link:
        return jsonify({"ok": False, "error": "Missing article link."}), 400
    try:
        excerpt = fetch_article_text_excerpt(link)
        preview = build_content_preview(excerpt, max_words=140)
        if not preview:
            return jsonify({
                "ok": False,
                "error": "Readable article text is not available from this publisher."
            }), 404
        return jsonify({"ok": True, "content_preview": preview})
    except Exception:
        return jsonify({"ok": False, "error": "Unable to load article text right now."}), 500

def article_matches_source_domain(article, domain: str) -> bool:
    domains = source_domain_candidates(domain)
    if not domains:
        return False
    source = safe_text(article.get("source", "") or article.get("source_name", "")).strip().lower()
    link = resolve_article_url(safe_text(article.get("link", "")).strip())
    link_domain = get_domain(link)
    display_name = normalize_source_key(source_display_name(domain))
    normalized_source = normalize_source_key(source)
    title_publisher = normalize_source_key(publisher_from_title(safe_text(article.get("title", ""))))
    source_aliases = {
        normalize_source_key(alias)
        for alias, mapped_domain in SOURCE_ROUTE_DOMAIN_MAP.items()
        if mapped_domain == domain
    }
    source_aliases.discard("")

    for item_domain in domains:
        if link_domain == item_domain or link_domain.endswith("." + item_domain):
            return True
        if source == item_domain or source.endswith("." + item_domain):
            return True

    if display_name and normalized_source and (
        normalized_source == display_name or display_name in normalized_source
    ):
        return True

    if title_publisher and (title_publisher == display_name or title_publisher in source_aliases):
        return True

    for alias in source_aliases:
        if alias and normalized_source and alias in normalized_source:
            return True

    return False

def build_trusted_source_sections(selected_date=None, enable_direct_fetch=True):
    target_date = selected_date or now_local().strftime("%Y-%m-%d")
    cache_key = f"trusted_showcase::{CACHE_VERSION}::{target_date}::{'deep' if enable_direct_fetch else 'fast'}"
    cached = get_cache(cache_key)
    if cached is not None:
        return cached
    preview_limit = adaptive_result_count(80, minimum_count=24)

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
                    max_results=preview_limit,
                    country_code="WORLD",
                    source_domain_filter="",
                    country_text="",
                    selected_date=target_date
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

    if any(section.get("headlines") for section in sections):
        set_cache(cache_key, sections)
    return sections

def source_feed_articles(domain, selected_date=None, max_results=40):
    feed_urls = SOURCE_FEED_MAP.get(domain, [])
    if not feed_urls:
        return []

    cache_date_key = selected_date or "latest"
    cache_key = f"sourcefeeds::{CACHE_VERSION}::{domain}::{cache_date_key}::{max_results}"
    cached = get_cache(cache_key)
    if cached is not None:
        return cached

    saved_links = current_saved_links()
    collected = []

    def parse_feed_entries(feed):
        items = []
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
            if selected_date and not article_matches_date(published_dt, selected_date):
                continue

            items.append(
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
        return items

    max_workers = max(1, min(4, len(feed_urls)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fetch_feed_with_timeout, feed_url) for feed_url in feed_urls]
        for future in as_completed(futures):
            try:
                collected.extend(parse_feed_entries(future.result()))
            except Exception:
                continue

    collected = sorted(
        remove_duplicates(collected),
        key=lambda x: parse_any_datetime(x.get("published_iso")) or datetime.min,
        reverse=True
    )
    if collected:
        set_cache(cache_key, collected)
    return collected

register_auth_routes(app, globals())
register_admin_routes(app, globals())

@app.route("/")
def home():
    if request.args.get("reset") == "1":
        reset_news_filters(clear_search=True)
    selected_date = request.args.get("date", "").strip() or None
    log_user_event("page_view", "Visited home dashboard")
    return render_dashboard_page(mode="home", selected_date=selected_date)

@app.route("/category/<cat>")
def category(cat):
    selected_date = request.args.get("date", "").strip() or None

    cat = (cat or "").strip().lower()
    if cat not in CATEGORY_ALLOWLIST:
        cat = "technology"

    track_category_click(cat.capitalize())
    log_user_event("category_view", f"Opened category: {cat}")
    return render_dashboard_page(mode="category", category=cat, selected_date=selected_date)

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
    return render_dashboard_page(mode="search", query=topic, selected_date=selected_date)

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

    saved = set_article_saved_state(uid, title, link, label=label, score=score_val)
    return jsonify({"ok": True, "saved": saved}), 200

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
    reset_news_filters()
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
    context["trusted_sections"] = []
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
    label = data.get("label")

    if not link:
        return jsonify({"status": "error"}), 400

    # ✅ Use your EXISTING DB function
    saved = set_article_saved_state(
        uid,
        title or "No Title",
        link,
        label=label,
        score=0.8,
        saved_at=datetime.now().strftime(TIMESTAMP_FORMAT)
    )
    return jsonify({"status": "saved" if saved else "removed"})

@app.route("/dismiss_breaking", methods=["POST"])
def dismiss_breaking():
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)

