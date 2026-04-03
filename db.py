import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "saved.db")

def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # latest schema
    cur.execute("""
        CREATE TABLE IF NOT EXISTS saved_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            link TEXT NOT NULL UNIQUE,
            label TEXT,
            score REAL,
            saved_at TEXT
        )
    """)

    # ✅ migrate old DB if it exists (fix: no column named score)
    cur.execute("PRAGMA table_info(saved_articles)")
    cols = {row["name"] for row in cur.fetchall()}

    if "score" not in cols:
        cur.execute("ALTER TABLE saved_articles ADD COLUMN score REAL")
    if "saved_at" not in cols:
        cur.execute("ALTER TABLE saved_articles ADD COLUMN saved_at TEXT")
    if "label" not in cols:
        cur.execute("ALTER TABLE saved_articles ADD COLUMN label TEXT")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT
        )
    """)

    cur.execute("PRAGMA table_info(users)")
    user_cols = {row["name"] for row in cur.fetchall()}
    if "is_admin" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
    if "last_login_at" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN last_login_at TEXT")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            event_type TEXT NOT NULL,
            details TEXT,
            created_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS password_reset_otp (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            otp_code TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)

    conn.commit()
    conn.close()

def create_user(name, email, password_hash, created_at, is_admin=0):
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (name, email, password_hash, created_at, is_admin)
        VALUES (?, ?, ?, ?, ?)
    """, (name, email.lower().strip(), password_hash, created_at, int(bool(is_admin))))
    conn.commit()
    user_id = cur.lastrowid
    conn.close()
    return user_id

def get_user_by_email(email):
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE email=? LIMIT 1", (email.lower().strip(),))
    row = cur.fetchone()
    conn.close()
    return row

def get_user_by_id(user_id):
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id=? LIMIT 1", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row

def update_user_password(email, password_hash):
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE users
        SET password_hash=?
        WHERE email=?
    """, (password_hash, email.lower().strip()))
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return changed > 0

def update_last_login(user_id, login_time):
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE users
        SET last_login_at=?
        WHERE id=?
    """, (login_time, user_id))
    conn.commit()
    conn.close()

def log_activity(user_id, event_type, details, created_at):
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO user_activity (user_id, event_type, details, created_at)
        VALUES (?, ?, ?, ?)
    """, (user_id, event_type, details, created_at))
    conn.commit()
    conn.close()

def get_recent_activity(limit=100):
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT a.*, u.name AS user_name, u.email AS user_email
        FROM user_activity a
        LEFT JOIN users u ON u.id = a.user_id
        ORDER BY a.id DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_all_users():
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return rows

def store_password_reset_otp(email, otp_code, expires_at, created_at):
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE password_reset_otp SET used=1 WHERE email=? AND used=0", (email.lower().strip(),))
    cur.execute("""
        INSERT INTO password_reset_otp (email, otp_code, expires_at, used, created_at)
        VALUES (?, ?, ?, 0, ?)
    """, (email.lower().strip(), otp_code, expires_at, created_at))
    conn.commit()
    conn.close()

def get_valid_password_reset_otp(email, otp_code):
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT *
        FROM password_reset_otp
        WHERE email=? AND otp_code=? AND used=0
        ORDER BY id DESC
        LIMIT 1
    """, (email.lower().strip(), otp_code.strip()))
    row = cur.fetchone()
    conn.close()
    return row

def mark_password_reset_otp_used(otp_id):
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE password_reset_otp SET used=1 WHERE id=?", (otp_id,))
    conn.commit()
    conn.close()

def get_recent_password_reset_requests(limit=50):
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT *
        FROM password_reset_otp
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows

def save_article(title, link, label, score, saved_at):
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO saved_articles (title, link, label, score, saved_at)
        VALUES (?, ?, ?, ?, ?)
    """, (title, link, label, float(score) if score is not None else None, saved_at))
    conn.commit()
    conn.close()

def get_saved():
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM saved_articles ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return rows

def delete_saved(article_id):
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM saved_articles WHERE id=?", (article_id,))
    conn.commit()
    conn.close()

def delete_saved_by_link(link):
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM saved_articles WHERE link=?", (link,))
    conn.commit()
    conn.close()

def is_saved(link) -> bool:
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM saved_articles WHERE link=? LIMIT 1", (link,))
    row = cur.fetchone()
    conn.close()
    return row is not None

def get_saved_links_set():
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT link FROM saved_articles")
    links = {r["link"] for r in cur.fetchall()}
    conn.close()
    return links
