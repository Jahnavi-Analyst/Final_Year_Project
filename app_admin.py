import math
from collections import Counter
from datetime import timedelta

from flask import redirect, render_template, request, session


def filter_rows_by_date_field(rows, parse_selected_date, parse_activity_time, selected_date="", field_name="created_at"):
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


def filter_rows_by_date(rows, parse_selected_date, parse_activity_time, selected_date=""):
    return filter_rows_by_date_field(rows, parse_selected_date, parse_activity_time, selected_date)


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


def activity_rows_in_window(rows, parse_activity_time, start_date, end_date):
    filtered = []
    for row in rows:
        dt = parse_activity_time(row["created_at"])
        if dt and start_date <= dt.date() <= end_date:
            filtered.append(row)
    return filtered


def article_meta_from_rows(rows, safe_text, extract_detail_value):
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


def register_admin_routes(app, deps):
    admin_required = deps["admin_required"]
    get_conn = deps["get_conn"]
    CACHE = deps["CACHE"]
    clear_admin_data = deps["clear_admin_data"]
    ADMIN_EMAIL = deps["ADMIN_EMAIL"]
    current_user = deps["current_user"]
    get_all_users = deps["get_all_users"]
    is_admin_user = deps["is_admin_user"]
    today_local_date = deps["today_local_date"]
    parse_selected_date = deps["parse_selected_date"]
    get_recent_activity = deps["get_recent_activity"]
    parse_activity_time = deps["parse_activity_time"]
    get_recent_password_reset_requests = deps["get_recent_password_reset_requests"]
    safe_text = deps["safe_text"]
    extract_detail_value = deps["extract_detail_value"]
    source_display_name = deps["source_display_name"]
    fetch_articles = deps["fetch_articles"]
    filter_articles_by_exact_date = deps["filter_articles_by_exact_date"]
    remove_duplicates = deps["remove_duplicates"]
    build_trusted_source_sections = deps["build_trusted_source_sections"]
    summarize_source_names = deps["summarize_source_names"]
    group_source_names_by_category = deps["group_source_names_by_category"]
    API_KEY = deps["API_KEY"]
    now_local = deps["now_local"]
    SOURCE_FEED_MAP = deps["SOURCE_FEED_MAP"]
    get_domain = deps["get_domain"]

    @app.route("/admin/clear-activity", methods=["POST"])
    @admin_required
    def admin_clear_activity():
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
        view_activity_rows = filter_rows_by_date(activity_rows, parse_selected_date, parse_activity_time, selected_date_text)

        seven_day_start = view_date - timedelta(days=6)
        trend_rows = activity_rows_in_window(activity_rows, parse_activity_time, seven_day_start, view_date)
        article_meta = article_meta_from_rows(activity_rows, safe_text, extract_detail_value)

        otp_rows_all = [
            r for r in get_recent_password_reset_requests(300)
            if safe_text(r["email"]).strip().lower() != safe_text(ADMIN_EMAIL)
        ]
        view_otp_rows = filter_rows_by_date(otp_rows_all, parse_selected_date, parse_activity_time, selected_date_text)

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

        view_saved_rows = filter_rows_by_date_field(all_saved_rows, parse_selected_date, parse_activity_time, selected_date_text, field_name="saved_at")
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
