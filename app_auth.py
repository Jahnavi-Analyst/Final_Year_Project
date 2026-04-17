import secrets
from collections import Counter
from datetime import timedelta

from flask import redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash


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


def build_profile_stats(user, deps):
    get_user_activity_rows = deps["get_user_activity_rows"]
    get_saved = deps["get_saved"]
    today_local_date = deps["today_local_date"]
    safe_text = deps["safe_text"]
    parse_activity_time = deps["parse_activity_time"]
    extract_detail_value = deps["extract_detail_value"]

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


def register_auth_routes(app, deps):
    current_user = deps["current_user"]
    safe_text = deps["safe_text"]
    render_auth_page = deps["render_auth_page"]
    get_user_by_email = deps["get_user_by_email"]
    complete_login = deps["complete_login"]
    SOCIAL_PROVIDER_META = deps["SOCIAL_PROVIDER_META"]
    create_account_record = deps["create_account_record"]
    get_user_by_id = deps["get_user_by_id"]
    log_activity = deps["log_activity"]
    now_text = deps["now_text"]
    is_strong_password = deps["is_strong_password"]
    PASSWORD_RULE_TEXT = deps["PASSWORD_RULE_TEXT"]
    store_and_send_password_reset_otp = deps["store_and_send_password_reset_otp"]
    OTP_EXPIRY_SECONDS = deps["OTP_EXPIRY_SECONDS"]
    latest_otp_remaining_seconds = deps["latest_otp_remaining_seconds"]
    get_valid_password_reset_otp = deps["get_valid_password_reset_otp"]
    otp_remaining_seconds = deps["otp_remaining_seconds"]
    update_user_password = deps["update_user_password"]
    is_admin_user = deps["is_admin_user"]
    build_base_context = deps["build_base_context"]
    log_user_event = deps["log_user_event"]
    deactivate_user = deps["deactivate_user"]
    COUNTRY_OPTIONS = deps["COUNTRY_OPTIONS"]
    reset_news_filters = deps["reset_news_filters"]
    settings_context = deps["settings_context"]
    SUPPORT_EMAIL = deps["SUPPORT_EMAIL"]
    HELP_ITEMS = deps["HELP_ITEMS"]

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
        if provider not in SOCIAL_PROVIDER_META:
            return redirect("/login")

        if current_user():
            return redirect("/")

        provider_title, provider_icon = SOCIAL_PROVIDER_META[provider]

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
                user_id = create_account_record(
                    name=name,
                    email=email,
                    password_hash=generate_password_hash(secrets.token_urlsafe(24)),
                    activity_message=f"{provider_title} quick sign-in created account for {email}"
                )
                user = get_user_by_id(user_id)
            else:
                log_activity(user["id"], "social_login", f"Signed in with {provider_title}", now_text())

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

            create_account_record(name, email, generate_password_hash(password), f"Account created for {email}")
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

            try:
                store_and_send_password_reset_otp(email)
            except Exception as e:
                return render_auth_page(
                    "forgot_password.html",
                    page_error=f"{safe_text(str(e)) or 'Unable to send OTP email right now.'}",
                    prefill_email=email
                )

            log_activity(user["id"], "password_reset_requested", f"OTP sent to {email}", now_text())
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
            now_text()
        )
        if not updated:
            return render_auth_page(
                "forgot_password.html",
                page_error="No account found with that email. Please create an account first.",
                prefill_email=email
            )

        user = get_user_by_email(email)
        if user:
            log_activity(user["id"], "password_reset_success", f"Password reset completed for {email}", now_text())
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

        try:
            store_and_send_password_reset_otp(email)
        except Exception as e:
            return render_auth_page(
                "verify_otp.html",
                page_error=f"{safe_text(str(e)) or 'Unable to send OTP email right now.'}",
                prefill_email=email,
                otp_expires_seconds=latest_otp_remaining_seconds(email),
                otp_verified=False
            )

        log_activity(user["id"], "password_reset_requested", f"OTP resent to {email}", now_text())
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
            log_activity(user["id"], "logout", "User logged out", now_text())
        session.clear()
        return redirect("/login")

    @app.route("/profile")
    def profile_page():
        user = current_user()
        if is_admin_user(user):
            return redirect("/admin")
        context = build_base_context(active="settings")
        context.update({"profile_stats": build_profile_stats(user, deps)})
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
                    now_text()
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
                    deactivate_user(user["id"], now_text())
                    log_activity(user["id"], "account_deactivated", "User deleted account", now_text())
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
                reset_news_filters(clear_search=True)
            return render_template("settings.html", **settings_context(settings_saved=True))

        return render_template("settings.html", **settings_context())

    @app.route("/help-support")
    def help_support_page():
        context = build_base_context(active="help")
        context.update({
            "support_email": SUPPORT_EMAIL,
            "help_items": HELP_ITEMS,
        })
        return render_template("help_support.html", **context)
