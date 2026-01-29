from __future__ import annotations

from datetime import datetime, date, timedelta
from functools import wraps
import os
import secrets
import sqlite3
from pathlib import Path
from typing import TypedDict

from flask import Flask, g, redirect, render_template, request, send_from_directory, session, url_for
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "tasks.db"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DB_BACKEND = "postgres" if DATABASE_URL else "sqlite"
RESET_CYCLE_DAYS_DEFAULT = 90

app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET_KEY", "dev-secret-change-me")
_db_ready = False


class RoutineItem(TypedDict):
    id: int
    label: str
    done: bool


class Routine(TypedDict):
    id: int
    name: str
    time_of_day: str
    items: list[RoutineItem]


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


@app.before_request
def load_user() -> None:
    user_id = session.get("user_id")
    if user_id is None:
        g.user = None
        return

    with get_conn() as conn:
        g.user = conn.execute(
            "SELECT id, username FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()


@app.context_processor
def inject_user():
    return {"current_user": g.user}


class DBConn:
    def __init__(self, conn, backend: str):
        self.conn = conn
        self.backend = backend

    def execute(self, query: str, params: tuple | list = ()):
        if self.backend == "postgres":
            sql = query.replace("?", "%s")
            cur = self.conn.cursor(cursor_factory=RealDictCursor)
            cur.execute(sql, params)
            return cur
        return self.conn.execute(query, params)

    def executescript(self, script: str) -> None:
        if self.backend == "postgres":
            statements = [s.strip() for s in script.split(";") if s.strip()]
            for statement in statements:
                self.execute(statement)
        else:
            self.conn.executescript(script)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            try:
                self.conn.commit()
            except Exception:
                pass
        self.conn.close()


def get_conn() -> DBConn:
    if DB_BACKEND == "postgres":
        conn = psycopg2.connect(DATABASE_URL)
        return DBConn(conn, "postgres")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return DBConn(conn, "sqlite")


@app.route("/styles.css")
def styles_css():
    public_dir = BASE_DIR / "public"
    return send_from_directory(public_dir, "styles.css")


def send_reset_email(to_email: str, token: str, base_url: str) -> bool:
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    sender = os.environ.get("RESET_EMAIL_FROM", "").strip()
    if not api_key or not sender:
        return False

    reset_link = f"{base_url.rstrip('/')}/password-reset/{token}"
    payload = {
        "from": sender,
        "to": [to_email],
        "subject": "Reset your Daily Planner Studio password",
        "html": (
            "<p>Click the link below to reset your password. "
            "This link expires in 2 hours.</p>"
            f"<p><a href=\"{reset_link}\">{reset_link}</a></p>"
        ),
    }

    response = requests.post(
        "https://api.resend.com/emails",
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10,
    )
    return response.status_code in (200, 201)


def init_db() -> None:
    with get_conn() as conn:
        if DB_BACKEND == "postgres":
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    email TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS invites (
                    id SERIAL PRIMARY KEY,
                    inviter_user_id INTEGER NOT NULL,
                    token TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_by_user_id INTEGER
                );

                CREATE TABLE IF NOT EXISTS password_resets (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    token TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS plans (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    time_block TEXT NOT NULL DEFAULT '',
                    priority INTEGER NOT NULL DEFAULT 2,
                    scheduled_date TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS checklist_items (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    scheduled_date TEXT NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS routines (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    time_of_day TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS routine_items (
                    id SERIAL PRIMARY KEY,
                    routine_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS routine_item_logs (
                    id SERIAL PRIMARY KEY,
                    routine_item_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    log_date TEXT NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (routine_item_id, log_date)
                );

                CREATE TABLE IF NOT EXISTS habits (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    target_count INTEGER NOT NULL DEFAULT 1,
                    active INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS habit_logs (
                    id SERIAL PRIMARY KEY,
                    habit_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    log_date TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (habit_id, log_date)
                );

                CREATE TABLE IF NOT EXISTS spending_entries (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    category TEXT NOT NULL,
                    note TEXT NOT NULL,
                    spend_date TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS spending_budgets (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    daily_limit REAL NOT NULL DEFAULT 0,
                    weekly_limit REAL NOT NULL DEFAULT 0,
                    UNIQUE (user_id, category)
                );

                CREATE TABLE IF NOT EXISTS settings (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL UNIQUE,
                    daily_spend_limit REAL NOT NULL DEFAULT 0,
                    reset_cycle_days INTEGER NOT NULL DEFAULT 90
                );

                CREATE TABLE IF NOT EXISTS daily_reflections (
                    user_id INTEGER NOT NULL,
                    log_date TEXT NOT NULL,
                    mood TEXT NOT NULL DEFAULT '',
                    wins TEXT NOT NULL DEFAULT '',
                    blockers TEXT NOT NULL DEFAULT '',
                    gratitude TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, log_date)
                );
                """
            )
        else:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    email TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS invites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    inviter_user_id INTEGER NOT NULL,
                    token TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_by_user_id INTEGER,
                    FOREIGN KEY (inviter_user_id) REFERENCES users (id)
                );

                CREATE TABLE IF NOT EXISTS password_resets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    token TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                );

                CREATE TABLE IF NOT EXISTS plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    time_block TEXT NOT NULL DEFAULT '',
                    priority INTEGER NOT NULL DEFAULT 2,
                    scheduled_date TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS checklist_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    scheduled_date TEXT NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS routines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    time_of_day TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS routine_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    routine_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (routine_id) REFERENCES routines (id)
                );

                CREATE TABLE IF NOT EXISTS routine_item_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    routine_item_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    log_date TEXT NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (routine_item_id, log_date),
                    FOREIGN KEY (routine_item_id) REFERENCES routine_items (id)
                );

                CREATE TABLE IF NOT EXISTS habits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    target_count INTEGER NOT NULL DEFAULT 1,
                    active INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS habit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    habit_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    log_date TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (habit_id, log_date),
                    FOREIGN KEY (habit_id) REFERENCES habits (id)
                );

                CREATE TABLE IF NOT EXISTS spending_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    category TEXT NOT NULL,
                    note TEXT NOT NULL,
                    spend_date TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS spending_budgets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    daily_limit REAL NOT NULL DEFAULT 0,
                    weekly_limit REAL NOT NULL DEFAULT 0,
                    UNIQUE (user_id, category)
                );

                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL UNIQUE,
                    daily_spend_limit REAL NOT NULL DEFAULT 0,
                    reset_cycle_days INTEGER NOT NULL DEFAULT 90
                );

                CREATE TABLE IF NOT EXISTS daily_reflections (
                    user_id INTEGER NOT NULL,
                    log_date TEXT NOT NULL,
                    mood TEXT NOT NULL DEFAULT '',
                    wins TEXT NOT NULL DEFAULT '',
                    blockers TEXT NOT NULL DEFAULT '',
                    gratitude TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, log_date)
                );
                """
            )


def ensure_db() -> None:
    global _db_ready
    if not _db_ready:
        init_db()
        migrate_db()
        _db_ready = True


def table_exists(conn: DBConn, table: str) -> bool:
    if DB_BACKEND == "postgres":
        row = conn.execute(
            "SELECT to_regclass(?) as name",
            (table,),
        ).fetchone()
        return row is not None and row["name"] is not None
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def column_exists(conn: DBConn, table: str, column: str) -> bool:
    if DB_BACKEND == "postgres":
        rows = conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = ? AND column_name = ?
            """,
            (table, column),
        ).fetchall()
        return len(rows) > 0
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def migrate_db() -> None:
    with get_conn() as conn:
        if DB_BACKEND == "postgres":
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    email TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS invites (
                    id SERIAL PRIMARY KEY,
                    inviter_user_id INTEGER NOT NULL,
                    token TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_by_user_id INTEGER
                );

                CREATE TABLE IF NOT EXISTS password_resets (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    token TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS settings (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL UNIQUE,
                    daily_spend_limit REAL NOT NULL DEFAULT 0,
                    reset_cycle_days INTEGER NOT NULL DEFAULT 90
                );
                """
            )
            if table_exists(conn, "settings") and not column_exists(
                conn, "settings", "reset_cycle_days"
            ):
                conn.execute(
                    "ALTER TABLE settings ADD COLUMN reset_cycle_days INTEGER NOT NULL DEFAULT 90"
                )
            return

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        if not column_exists(conn, "users", "email"):
            conn.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
            conn.execute(
                """
                UPDATE users
                SET email = username
                WHERE email = '' AND username LIKE '%@%'
                """
            )

        if table_exists(conn, "settings") and not column_exists(conn, "settings", "user_id"):
            conn.execute("ALTER TABLE settings RENAME TO settings_old")
            conn.execute(
                """
                CREATE TABLE settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL UNIQUE,
                    daily_spend_limit REAL NOT NULL DEFAULT 0,
                    reset_cycle_days INTEGER NOT NULL DEFAULT 90
                )
                """
            )
            conn.execute(
                """
                INSERT INTO settings (user_id, daily_spend_limit, reset_cycle_days)
                SELECT 1, daily_spend_limit, 90 FROM settings_old
                """
            )
            conn.execute("DROP TABLE settings_old")

        if table_exists(conn, "settings") and not column_exists(
            conn, "settings", "reset_cycle_days"
        ):
            conn.execute(
                "ALTER TABLE settings ADD COLUMN reset_cycle_days INTEGER NOT NULL DEFAULT 90"
            )

        if table_exists(conn, "daily_reflections") and not column_exists(
            conn, "daily_reflections", "user_id"
        ):
            conn.execute("ALTER TABLE daily_reflections RENAME TO daily_reflections_old")
            conn.execute(
                """
                CREATE TABLE daily_reflections (
                    user_id INTEGER NOT NULL,
                    log_date TEXT NOT NULL,
                    mood TEXT NOT NULL DEFAULT '',
                    wins TEXT NOT NULL DEFAULT '',
                    blockers TEXT NOT NULL DEFAULT '',
                    gratitude TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, log_date)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO daily_reflections (
                    user_id, log_date, mood, wins, blockers, gratitude, created_at
                )
                SELECT 1, log_date, mood, wins, blockers, gratitude, created_at
                FROM daily_reflections_old
                """
            )
            conn.execute("DROP TABLE daily_reflections_old")

        if table_exists(conn, "spending_budgets") and not column_exists(
            conn, "spending_budgets", "user_id"
        ):
            conn.execute("ALTER TABLE spending_budgets RENAME TO spending_budgets_old")
            conn.execute(
                """
                CREATE TABLE spending_budgets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    daily_limit REAL NOT NULL DEFAULT 0,
                    weekly_limit REAL NOT NULL DEFAULT 0,
                    UNIQUE (user_id, category)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO spending_budgets (user_id, category, daily_limit, weekly_limit)
                SELECT 1, category, daily_limit, weekly_limit FROM spending_budgets_old
                """
            )
            conn.execute("DROP TABLE spending_budgets_old")

        if not table_exists(conn, "invites"):
            conn.execute(
                """
                CREATE TABLE invites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    inviter_user_id INTEGER NOT NULL,
                    token TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_by_user_id INTEGER,
                    FOREIGN KEY (inviter_user_id) REFERENCES users (id)
                )
                """
            )

        if not table_exists(conn, "password_resets"):
            conn.execute(
                """
                CREATE TABLE password_resets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    token TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
                """
            )

        tables_to_update = [
            "plans",
            "checklist_items",
            "routines",
            "routine_items",
            "routine_item_logs",
            "habits",
            "habit_logs",
            "spending_entries",
        ]
        for table in tables_to_update:
            if table_exists(conn, table) and not column_exists(conn, table, "user_id"):
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1"
                )
                conn.execute(f"UPDATE {table} SET user_id = 1 WHERE user_id IS NULL")


def today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def normalize_cycle_days(value: int | str | None) -> int:
    try:
        days = int(value) if value is not None else RESET_CYCLE_DAYS_DEFAULT
    except (TypeError, ValueError):
        days = RESET_CYCLE_DAYS_DEFAULT
    return max(7, min(days, 365))


def cycle_start_str(cycle_days: int) -> str:
    return (date.today() - timedelta(days=cycle_days - 1)).strftime("%Y-%m-%d")


def parse_int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_float(value: str, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_settings(user_id: int) -> sqlite3.Row:
    with get_conn() as conn:
        settings = conn.execute(
            "SELECT * FROM settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if settings is None:
            conn.execute(
                """
                INSERT INTO settings (user_id, daily_spend_limit, reset_cycle_days)
                VALUES (?, 0, ?)
                """,
                (user_id, RESET_CYCLE_DAYS_DEFAULT),
            )
            settings = conn.execute(
                "SELECT * FROM settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
    return settings


def compute_habit_streaks(
    habits: list[sqlite3.Row], habit_log_rows: list[sqlite3.Row], max_days: int
) -> dict[int, int]:
    today = date.today()
    log_map: dict[int, dict[date, int]] = {}
    for row in habit_log_rows:
        habit_id = row["habit_id"]
        log_date = date.fromisoformat(row["log_date"])
        log_map.setdefault(habit_id, {})[log_date] = row["count"]

    streaks: dict[int, int] = {}
    for habit in habits:
        habit_id = habit["id"]
        target = habit["target_count"]
        streak = 0
        for offset in range(0, max_days):
            check_date = today - timedelta(days=offset)
            count = log_map.get(habit_id, {}).get(check_date, 0)
            if count >= target and target > 0:
                streak += 1
            else:
                break
        streaks[habit_id] = streak
    return streaks


def update_missed_plans(user_id: int) -> None:
    today_iso = today_str()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE plans
            SET status = 'missed'
            WHERE status = 'pending' AND scheduled_date < ? AND user_id = ?
            """,
            (today_iso, user_id),
        )


def get_overview_stats(user_id: int) -> dict[str, int]:
    stats = {
        "plans": 0,
        "checklist": 0,
        "habits": 0,
        "routines": 0,
        "spending_entries": 0,
    }
    with get_conn() as conn:
        stats["plans"] = conn.execute(
            "SELECT COUNT(*) as count FROM plans WHERE user_id = ?",
            (user_id,),
        ).fetchone()["count"]
        stats["checklist"] = conn.execute(
            "SELECT COUNT(*) as count FROM checklist_items WHERE user_id = ?",
            (user_id,),
        ).fetchone()["count"]
        stats["habits"] = conn.execute(
            "SELECT COUNT(*) as count FROM habits WHERE user_id = ?",
            (user_id,),
        ).fetchone()["count"]
        stats["routines"] = conn.execute(
            "SELECT COUNT(*) as count FROM routines WHERE user_id = ?",
            (user_id,),
        ).fetchone()["count"]
        stats["spending_entries"] = conn.execute(
            "SELECT COUNT(*) as count FROM spending_entries WHERE user_id = ?",
            (user_id,),
        ).fetchone()["count"]
    return stats


@app.route("/", methods=["GET"])
@login_required
def index():
    ensure_db()
    user_id = g.user["id"]
    update_missed_plans(user_id)
    today_iso = today_str()
    settings = get_settings(user_id)
    reset_cycle_days = normalize_cycle_days(settings["reset_cycle_days"])
    cycle_start = cycle_start_str(reset_cycle_days)
    week_start = (date.today() - timedelta(days=6)).strftime("%Y-%m-%d")

    with get_conn() as conn:
        plans = conn.execute(
            """
            SELECT * FROM plans
            WHERE scheduled_date BETWEEN ? AND ? AND user_id = ?
            ORDER BY scheduled_date DESC, priority DESC, id ASC
            """,
            (cycle_start, today_iso, user_id),
        ).fetchall()
        checklist = conn.execute(
            """
            SELECT * FROM checklist_items
            WHERE scheduled_date BETWEEN ? AND ? AND user_id = ?
            ORDER BY scheduled_date DESC, id ASC
            """,
            (cycle_start, today_iso, user_id),
        ).fetchall()
        habits = conn.execute(
            "SELECT * FROM habits WHERE active = 1 AND user_id = ? ORDER BY id ASC",
            (user_id,),
        ).fetchall()
        habit_logs = conn.execute(
            """
            SELECT habit_id, count FROM habit_logs
            WHERE log_date = ? AND user_id = ?
            """,
            (today_iso, user_id),
        ).fetchall()
        habit_logs_all = conn.execute(
            """
            SELECT habit_id, log_date, count
            FROM habit_logs
            WHERE log_date BETWEEN ? AND ? AND user_id = ?
            """,
            (cycle_start, today_iso, user_id),
        ).fetchall()
        routines = conn.execute(
            """
            SELECT * FROM routines
            WHERE active = 1 AND user_id = ?
            ORDER BY time_of_day, name
            """,
            (user_id,),
        ).fetchall()
        routine_items = conn.execute(
            """
            SELECT * FROM routine_items
            WHERE routine_id IN (SELECT id FROM routines WHERE active = 1 AND user_id = ?)
                AND user_id = ?
            ORDER BY sort_order, id
            """,
            (user_id, user_id),
        ).fetchall()
        routine_logs = conn.execute(
            """
            SELECT routine_item_id, done FROM routine_item_logs
            WHERE log_date = ? AND user_id = ?
            """,
            (today_iso, user_id),
        ).fetchall()
        spending_entries = conn.execute(
            """
            SELECT * FROM spending_entries
            WHERE spend_date BETWEEN ? AND ? AND user_id = ?
            ORDER BY spend_date DESC, created_at DESC
            """,
            (cycle_start, today_iso, user_id),
        ).fetchall()
        spending_budgets = conn.execute(
            "SELECT * FROM spending_budgets WHERE user_id = ? ORDER BY category",
            (user_id,),
        ).fetchall()
        spending_by_category = conn.execute(
            """
            SELECT category, SUM(amount) as total
            FROM spending_entries
            WHERE spend_date = ? AND user_id = ?
            GROUP BY category
            """,
            (today_iso, user_id),
        ).fetchall()
        spending_by_category_week = conn.execute(
            """
            SELECT category, SUM(amount) as total
            FROM spending_entries
            WHERE spend_date BETWEEN ? AND ? AND user_id = ?
            GROUP BY category
            """,
            (week_start, today_iso, user_id),
        ).fetchall()
        reflection = conn.execute(
            "SELECT * FROM daily_reflections WHERE log_date = ? AND user_id = ?",
            (today_iso, user_id),
        ).fetchone()
        reflections = conn.execute(
            """
            SELECT * FROM daily_reflections
            WHERE log_date BETWEEN ? AND ? AND user_id = ?
            ORDER BY log_date DESC
            """,
            (cycle_start, today_iso, user_id),
        ).fetchall()

    habit_log_map = {row["habit_id"]: row["count"] for row in habit_logs}
    habit_streaks = compute_habit_streaks(habits, habit_logs_all, reset_cycle_days)
    routine_log_map = {row["routine_item_id"]: row["done"] for row in routine_logs}
    spend_category_map = {row["category"]: row["total"] for row in spending_by_category}
    spend_week_category_map = {
        row["category"]: row["total"] for row in spending_by_category_week
    }
    routine_map: dict[int, Routine] = {}
    for routine in routines:
        routine_map[routine["id"]] = {
            "id": routine["id"],
            "name": routine["name"],
            "time_of_day": routine["time_of_day"],
            "items": [],
        }

    for item in routine_items:
        routine = routine_map.get(item["routine_id"])
        if routine is None:
            continue
        routine["items"].append(
            {
                "id": item["id"],
                "label": item["label"],
                "done": bool(routine_log_map.get(item["id"], 0)),
            }
        )

    spend_total = sum(entry["amount"] for entry in spending_entries)
    spend_total_today = sum(
        entry["amount"] for entry in spending_entries if entry["spend_date"] == today_iso
    )
    daily_spend_limit = settings["daily_spend_limit"]
    left_to_spend = (
        daily_spend_limit - spend_total_today if daily_spend_limit > 0 else None
    )
    plans_done = sum(1 for plan in plans if plan["status"] == "done")
    plans_pending = sum(1 for plan in plans if plan["status"] == "pending")
    plans_missed = sum(1 for plan in plans if plan["status"] == "missed")
    checklist_done = sum(1 for item in checklist if item["done"])
    checklist_total = len(checklist)
    habits_hit = 0
    for habit in habits:
        if habit_log_map.get(habit["id"], 0) >= habit["target_count"]:
            habits_hit += 1
    habit_total = len(habits)
    routine_items_total = sum(len(routine["items"]) for routine in routine_map.values())
    routine_items_done = sum(
        1 for routine in routine_map.values() for item in routine["items"] if item["done"]
    )

    return render_template(
        "index.html",
        now=datetime.now(),
        today_iso=today_iso,
        week_start=week_start,
        plans=plans,
        checklist=checklist,
        habits=habits,
        habit_log_map=habit_log_map,
        habit_streaks=habit_streaks,
        routines=list(routine_map.values()),
        spending_entries=spending_entries,
        spend_total=spend_total,
        spend_total_today=spend_total_today,
        spending_budgets=spending_budgets,
        spend_category_map=spend_category_map,
        spend_week_category_map=spend_week_category_map,
        daily_spend_limit=daily_spend_limit,
        left_to_spend=left_to_spend,
        reflection=reflection,
        reflections=reflections,
        reset_cycle_days=reset_cycle_days,
        plans_done=plans_done,
        plans_pending=plans_pending,
        plans_missed=plans_missed,
        checklist_done=checklist_done,
        checklist_total=checklist_total,
        habits_hit=habits_hit,
        habit_total=habit_total,
        routine_items_done=routine_items_done,
        routine_items_total=routine_items_total,
    )


@app.route("/about", methods=["GET"])
@login_required
def about():
    ensure_db()
    user_id = g.user["id"]
    update_missed_plans(user_id)
    stats = get_overview_stats(user_id)
    return render_template("about.html", stats=stats, now=datetime.now())


@app.route("/review", methods=["GET"])
@login_required
def weekly_review():
    ensure_db()
    user_id = g.user["id"]
    update_missed_plans(user_id)
    today = date.today()
    start = today - timedelta(days=6)
    start_iso = start.strftime("%Y-%m-%d")
    end_iso = today.strftime("%Y-%m-%d")

    with get_conn() as conn:
        plan_stats = conn.execute(
            """
            SELECT status, COUNT(*) as count
            FROM plans
            WHERE scheduled_date BETWEEN ? AND ? AND user_id = ?
            GROUP BY status
            """,
            (start_iso, end_iso, user_id),
        ).fetchall()
        spending_by_day = conn.execute(
            """
            SELECT spend_date, SUM(amount) as total
            FROM spending_entries
            WHERE spend_date BETWEEN ? AND ? AND user_id = ?
            GROUP BY spend_date
            ORDER BY spend_date DESC
            """,
            (start_iso, end_iso, user_id),
        ).fetchall()
        spending_by_category = conn.execute(
            """
            SELECT category, SUM(amount) as total
            FROM spending_entries
            WHERE spend_date BETWEEN ? AND ? AND user_id = ?
            GROUP BY category
            ORDER BY total DESC
            """,
            (start_iso, end_iso, user_id),
        ).fetchall()
        habit_counts = conn.execute(
            """
            SELECT h.name, SUM(hl.count) as total
            FROM habits h
            LEFT JOIN habit_logs hl ON h.id = hl.habit_id
                AND hl.log_date BETWEEN ? AND ? AND hl.user_id = ?
            WHERE h.active = 1 AND h.user_id = ?
            GROUP BY h.id
            ORDER BY total DESC
            """,
            (start_iso, end_iso, user_id, user_id),
        ).fetchall()
        routine_completions = conn.execute(
            """
            SELECT r.name, SUM(ril.done) as total
            FROM routines r
            LEFT JOIN routine_items ri ON r.id = ri.routine_id
            LEFT JOIN routine_item_logs ril ON ri.id = ril.routine_item_id
                AND ril.log_date BETWEEN ? AND ? AND ril.user_id = ?
            WHERE r.active = 1 AND r.user_id = ?
            GROUP BY r.id
            ORDER BY total DESC
            """,
            (start_iso, end_iso, user_id, user_id),
        ).fetchall()

    plan_stat_map = {row["status"]: row["count"] for row in plan_stats}
    return render_template(
        "review.html",
        now=datetime.now(),
        start_date=start_iso,
        end_date=end_iso,
        plan_stat_map=plan_stat_map,
        spending_by_day=spending_by_day,
        spending_by_category=spending_by_category,
        habit_counts=habit_counts,
        routine_completions=routine_completions,
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    ensure_db()
    if g.user is not None:
        return redirect(url_for("index"))

    error = ""
    invite_token = session.get("invite_token")
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        if not username or not email or not password:
            error = "Username, email, and password are required."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            if invite_token:
                with get_conn() as conn:
                    invite = conn.execute(
                        """
                        SELECT * FROM invites
                        WHERE token = ? AND used_by_user_id IS NULL
                          AND expires_at >= ?
                        """,
                        (invite_token, datetime.now().isoformat(timespec="seconds")),
                    ).fetchone()
                if invite is None:
                    error = "That invite link is invalid or expired."
                    return render_template("register.html", error=error, now=datetime.now())

            now = datetime.now().isoformat(timespec="seconds")
            with get_conn() as conn:
                user_count = conn.execute(
                    "SELECT COUNT(*) as count FROM users"
                ).fetchone()["count"]
                email_exists = conn.execute(
                    "SELECT 1 FROM users WHERE email = ?",
                    (email,),
                ).fetchone()
                if email_exists:
                    error = "That email is already registered."
                    return render_template(
                        "register.html",
                        error=error,
                        now=datetime.now(),
                        invite=invite_token,
                    )
                try:
                    if user_count == 0:
                        conn.execute(
                            """
                            INSERT INTO users (id, username, email, password_hash, created_at)
                            VALUES (1, ?, ?, ?, ?)
                            """,
                            (username, email, generate_password_hash(password), now),
                        )
                        user_id = 1
                        conn.execute(
                            "UPDATE plans SET user_id = 1 WHERE user_id IS NULL"
                        )
                        conn.execute(
                            "UPDATE checklist_items SET user_id = 1 WHERE user_id IS NULL"
                        )
                        conn.execute(
                            "UPDATE routines SET user_id = 1 WHERE user_id IS NULL"
                        )
                        conn.execute(
                            "UPDATE routine_items SET user_id = 1 WHERE user_id IS NULL"
                        )
                        conn.execute(
                            "UPDATE routine_item_logs SET user_id = 1 WHERE user_id IS NULL"
                        )
                        conn.execute(
                            "UPDATE habits SET user_id = 1 WHERE user_id IS NULL"
                        )
                        conn.execute(
                            "UPDATE habit_logs SET user_id = 1 WHERE user_id IS NULL"
                        )
                        conn.execute(
                            "UPDATE spending_entries SET user_id = 1 WHERE user_id IS NULL"
                        )
                        conn.execute(
                            "UPDATE spending_budgets SET user_id = 1 WHERE user_id IS NULL"
                        )
                        conn.execute(
                            "UPDATE settings SET user_id = 1 WHERE user_id IS NULL"
                        )
                        conn.execute(
                            "UPDATE daily_reflections SET user_id = 1 WHERE user_id IS NULL"
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO users (username, email, password_hash, created_at)
                            VALUES (?, ?, ?, ?)
                            """,
                            (username, email, generate_password_hash(password), now),
                        )
                        user_id = conn.execute(
                            "SELECT id as id FROM users WHERE username = ?",
                            (username,),
                        ).fetchone()["id"]

                    if invite_token:
                        conn.execute(
                            """
                            UPDATE invites
                            SET used_by_user_id = ?
                            WHERE token = ?
                            """,
                            (user_id, invite_token),
                        )
                        session.pop("invite_token", None)
                except sqlite3.IntegrityError:
                    error = "That username is taken."
                else:
                    session["user_id"] = user_id
                    return redirect(url_for("index"))

    return render_template(
        "register.html",
        error=error,
        now=datetime.now(),
        invite=invite_token,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    ensure_db()
    if g.user is not None:
        return redirect(url_for("index"))

    error = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")

        with get_conn() as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE username = ? OR email = ?",
                (username, username),
            ).fetchone()

        if user is None or not check_password_hash(user["password_hash"], password):
            error = "Invalid username or password."
        else:
            session["user_id"] = user["id"]
            return redirect(url_for("index"))

    return render_template("login.html", error=error, now=datetime.now())


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    ensure_db()
    user_id = g.user["id"]
    message = ""
    error = ""

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        with get_conn() as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()

            if email and email != user["email"]:
                email_exists = conn.execute(
                    "SELECT 1 FROM users WHERE email = ? AND id != ?",
                    (email, user_id),
                ).fetchone()
                if email_exists:
                    error = "That email is already registered."
                else:
                    conn.execute(
                        "UPDATE users SET email = ? WHERE id = ?",
                        (email, user_id),
                    )
                    message = "Email updated."

            if new_password:
                if not current_password or not check_password_hash(
                    user["password_hash"], current_password
                ):
                    error = "Current password is incorrect."
                elif new_password != confirm_password:
                    error = "New passwords do not match."
                else:
                    conn.execute(
                        "UPDATE users SET password_hash = ? WHERE id = ?",
                        (generate_password_hash(new_password), user_id),
                    )
                    message = "Password updated."

    with get_conn() as conn:
        user = conn.execute(
            "SELECT username, email FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()

    return render_template(
        "account.html",
        now=datetime.now(),
        user=user,
        message=message,
        error=error,
    )


@app.route("/password-reset", methods=["GET", "POST"])
def password_reset_request():
    ensure_db()
    message = ""
    error = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not email:
            error = "Enter your email address."
        else:
            with get_conn() as conn:
                user = conn.execute(
                    "SELECT id, email FROM users WHERE email = ?",
                    (email,),
                ).fetchone()
            if user is None:
                message = "If that email exists, a reset link has been sent."
            else:
                if not user["email"]:
                    error = "No email is saved on that account."
                else:
                    token = secrets.token_urlsafe(24)
                    now = datetime.now()
                    expires_at = (now + timedelta(hours=2)).isoformat(timespec="seconds")
                    with get_conn() as conn:
                        conn.execute(
                            """
                            INSERT INTO password_resets (user_id, token, created_at, expires_at)
                            VALUES (?, ?, ?, ?)
                            """,
                            (user["id"], token, now.isoformat(timespec="seconds"), expires_at),
                        )
                    base_url = os.environ.get("APP_BASE_URL", "").strip()
                    if not base_url:
                        base_url = request.host_url.rstrip("/")
                    sent = send_reset_email(user["email"], token, base_url)
                    if sent:
                        message = "Reset link sent. Check your email."
                    else:
                        error = "Email service is not configured."

    return render_template(
        "password_reset_request.html",
        now=datetime.now(),
        message=message,
        error=error,
    )


@app.route("/password-reset/<token>", methods=["GET", "POST"])
def password_reset(token: str):
    ensure_db()
    error = ""
    with get_conn() as conn:
        reset = conn.execute(
            """
            SELECT * FROM password_resets
            WHERE token = ? AND used = 0 AND expires_at >= ?
            """,
            (token, datetime.now().isoformat(timespec="seconds")),
        ).fetchone()
    if reset is None:
        return render_template("password_reset_invalid.html", now=datetime.now())

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if not password:
            error = "Password is required."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            with get_conn() as conn:
                conn.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (generate_password_hash(password), reset["user_id"]),
                )
                conn.execute(
                    "UPDATE password_resets SET used = 1 WHERE id = ?",
                    (reset["id"],),
                )
            return redirect(url_for("login"))

    return render_template(
        "password_reset_form.html",
        now=datetime.now(),
        token=token,
        error=error,
    )
@app.route("/invite/<token>", methods=["GET"])
def accept_invite(token: str):
    ensure_db()
    with get_conn() as conn:
        invite = conn.execute(
            """
            SELECT * FROM invites
            WHERE token = ? AND used_by_user_id IS NULL
              AND expires_at >= ?
            """,
            (token, datetime.now().isoformat(timespec="seconds")),
        ).fetchone()
    if invite is None:
        return render_template("invite_invalid.html", now=datetime.now())

    session["invite_token"] = token
    return redirect(url_for("register"))


@app.route("/invites", methods=["GET"])
@login_required
def invites():
    ensure_db()
    user_id = g.user["id"]
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        active_invites = conn.execute(
            """
            SELECT * FROM invites
            WHERE inviter_user_id = ?
              AND expires_at >= ?
              AND used_by_user_id IS NULL
            ORDER BY created_at DESC
            """,
            (user_id, now),
        ).fetchall()
        used_invites = conn.execute(
            """
            SELECT i.*, u.username as used_by
            FROM invites i
            LEFT JOIN users u ON u.id = i.used_by_user_id
            WHERE i.inviter_user_id = ?
              AND i.used_by_user_id IS NOT NULL
            ORDER BY i.created_at DESC
            """,
            (user_id,),
        ).fetchall()
    return render_template(
        "invites.html",
        now=datetime.now(),
        active_invites=active_invites,
        used_invites=used_invites,
    )


@app.route("/invites/create", methods=["POST"])
@login_required
def create_invite():
    ensure_db()
    user_id = g.user["id"]
    token = secrets.token_urlsafe(16)
    now = datetime.now()
    expires_at = (now + timedelta(days=7)).isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO invites (inviter_user_id, token, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, token, now.isoformat(timespec="seconds"), expires_at),
        )
    return redirect(url_for("invites"))


@app.route("/plan/add", methods=["POST"])
@login_required
def add_plan():
    ensure_db()
    user_id = g.user["id"]
    title = request.form.get("title", "").strip()
    time_block = request.form.get("time_block", "").strip()
    priority = parse_int(request.form.get("priority", "2"), 2)
    scheduled_date = request.form.get("scheduled_date", "").strip() or today_str()

    if not title:
        return redirect(url_for("index"))

    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO plans (user_id, title, time_block, priority, scheduled_date, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """,
            (user_id, title, time_block, priority, scheduled_date, now),
        )
    return redirect(url_for("index"))


@app.route("/plan/complete/<int:plan_id>", methods=["POST"])
@login_required
def complete_plan(plan_id: int):
    ensure_db()
    user_id = g.user["id"]
    with get_conn() as conn:
        conn.execute(
            "UPDATE plans SET status = 'done' WHERE id = ? AND user_id = ?",
            (plan_id, user_id),
        )
    return redirect(url_for("index"))


@app.route("/plan/reopen/<int:plan_id>", methods=["POST"])
@login_required
def reopen_plan(plan_id: int):
    ensure_db()
    user_id = g.user["id"]
    with get_conn() as conn:
        conn.execute(
            "UPDATE plans SET status = 'pending' WHERE id = ? AND user_id = ?",
            (plan_id, user_id),
        )
    return redirect(url_for("index"))


@app.route("/plan/delete/<int:plan_id>", methods=["POST"])
@login_required
def delete_plan(plan_id: int):
    ensure_db()
    user_id = g.user["id"]
    with get_conn() as conn:
        conn.execute("DELETE FROM plans WHERE id = ? AND user_id = ?", (plan_id, user_id))
    return redirect(url_for("index"))


@app.route("/checklist/add", methods=["POST"])
@login_required
def add_checklist_item():
    ensure_db()
    user_id = g.user["id"]
    label = request.form.get("label", "").strip()
    scheduled_date = request.form.get("scheduled_date", "").strip() or today_str()

    if not label:
        return redirect(url_for("index"))

    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO checklist_items (user_id, label, scheduled_date, done, created_at)
            VALUES (?, ?, ?, 0, ?)
            """,
            (user_id, label, scheduled_date, now),
        )
    return redirect(url_for("index"))


@app.route("/checklist/toggle/<int:item_id>", methods=["POST"])
@login_required
def toggle_checklist_item(item_id: int):
    ensure_db()
    user_id = g.user["id"]
    with get_conn() as conn:
        current = conn.execute(
            "SELECT done FROM checklist_items WHERE id = ? AND user_id = ?",
            (item_id, user_id),
        ).fetchone()
        if current is None:
            return redirect(url_for("index"))
        new_value = 0 if current["done"] else 1
        conn.execute(
            "UPDATE checklist_items SET done = ? WHERE id = ? AND user_id = ?",
            (new_value, item_id, user_id),
        )
    return redirect(url_for("index"))


@app.route("/checklist/delete/<int:item_id>", methods=["POST"])
@login_required
def delete_checklist_item(item_id: int):
    ensure_db()
    user_id = g.user["id"]
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM checklist_items WHERE id = ? AND user_id = ?",
            (item_id, user_id),
        )
    return redirect(url_for("index"))


@app.route("/routines/add", methods=["POST"])
@login_required
def add_routine():
    ensure_db()
    user_id = g.user["id"]
    name = request.form.get("name", "").strip()
    time_of_day = request.form.get("time_of_day", "any").strip()

    if not name:
        return redirect(url_for("index"))

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO routines (user_id, name, time_of_day, active)
            VALUES (?, ?, ?, 1)
            """,
            (user_id, name, time_of_day),
        )
    return redirect(url_for("index"))


@app.route("/routine-items/add", methods=["POST"])
@login_required
def add_routine_item():
    ensure_db()
    user_id = g.user["id"]
    routine_id = request.form.get("routine_id", "").strip()
    label = request.form.get("label", "").strip()
    sort_order = parse_int(request.form.get("sort_order", "0"), 0)

    if not routine_id or not label:
        return redirect(url_for("index"))

    with get_conn() as conn:
        routine = conn.execute(
            "SELECT id FROM routines WHERE id = ? AND user_id = ?",
            (routine_id, user_id),
        ).fetchone()
        if routine is None:
            return redirect(url_for("index"))
        conn.execute(
            """
            INSERT INTO routine_items (routine_id, user_id, label, sort_order)
            VALUES (?, ?, ?, ?)
            """,
            (routine_id, user_id, label, sort_order),
        )
    return redirect(url_for("index"))


@app.route("/routine-items/toggle/<int:item_id>", methods=["POST"])
@login_required
def toggle_routine_item(item_id: int):
    ensure_db()
    user_id = g.user["id"]
    log_date = today_str()
    with get_conn() as conn:
        item = conn.execute(
            "SELECT id FROM routine_items WHERE id = ? AND user_id = ?",
            (item_id, user_id),
        ).fetchone()
        if item is None:
            return redirect(url_for("index"))
        current = conn.execute(
            """
            SELECT done FROM routine_item_logs
            WHERE routine_item_id = ? AND log_date = ? AND user_id = ?
            """,
            (item_id, log_date, user_id),
        ).fetchone()
        if current is None:
            conn.execute(
                """
                INSERT INTO routine_item_logs (routine_item_id, user_id, log_date, done)
                VALUES (?, ?, ?, 1)
                """,
                (item_id, user_id, log_date),
            )
        else:
            new_value = 0 if current["done"] else 1
            conn.execute(
                """
                UPDATE routine_item_logs
                SET done = ?
                WHERE routine_item_id = ? AND log_date = ? AND user_id = ?
                """,
                (new_value, item_id, log_date, user_id),
            )
    return redirect(url_for("index"))


@app.route("/habits/add", methods=["POST"])
@login_required
def add_habit():
    ensure_db()
    user_id = g.user["id"]
    name = request.form.get("name", "").strip()
    target_count = parse_int(request.form.get("target_count", "1"), 1)

    if not name:
        return redirect(url_for("index"))

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO habits (user_id, name, target_count, active)
            VALUES (?, ?, ?, 1)
            """,
            (user_id, name, target_count),
        )
    return redirect(url_for("index"))


@app.route("/habits/log/<int:habit_id>", methods=["POST"])
@login_required
def log_habit(habit_id: int):
    ensure_db()
    user_id = g.user["id"]
    log_date = today_str()
    with get_conn() as conn:
        habit = conn.execute(
            "SELECT id FROM habits WHERE id = ? AND user_id = ?",
            (habit_id, user_id),
        ).fetchone()
        if habit is None:
            return redirect(url_for("index"))
        current = conn.execute(
            """
            SELECT count FROM habit_logs
            WHERE habit_id = ? AND log_date = ? AND user_id = ?
            """,
            (habit_id, log_date, user_id),
        ).fetchone()
        if current is None:
            conn.execute(
                """
                INSERT INTO habit_logs (habit_id, user_id, log_date, count)
                VALUES (?, ?, ?, 1)
                """,
                (habit_id, user_id, log_date),
            )
        else:
            conn.execute(
                """
                UPDATE habit_logs
                SET count = count + 1
                WHERE habit_id = ? AND log_date = ? AND user_id = ?
                """,
                (habit_id, log_date, user_id),
            )
    return redirect(url_for("index"))


@app.route("/habits/reset/<int:habit_id>", methods=["POST"])
@login_required
def reset_habit(habit_id: int):
    ensure_db()
    user_id = g.user["id"]
    log_date = today_str()
    with get_conn() as conn:
        habit = conn.execute(
            "SELECT id FROM habits WHERE id = ? AND user_id = ?",
            (habit_id, user_id),
        ).fetchone()
        if habit is None:
            return redirect(url_for("index"))
        conn.execute(
            """
            INSERT INTO habit_logs (habit_id, user_id, log_date, count)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(habit_id, log_date) DO UPDATE SET count = 0
            """,
            (habit_id, user_id, log_date),
        )
    return redirect(url_for("index"))


@app.route("/spending/add", methods=["POST"])
@login_required
def add_spending():
    ensure_db()
    user_id = g.user["id"]
    amount_raw = request.form.get("amount", "").strip()
    category = request.form.get("category", "").strip() or "Other"
    note = request.form.get("note", "").strip() or "Daily spend"
    spend_date = request.form.get("spend_date", "").strip() or today_str()

    try:
        amount = float(amount_raw)
    except ValueError:
        return redirect(url_for("index"))

    if amount <= 0:
        return redirect(url_for("index"))

    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO spending_entries (user_id, amount, category, note, spend_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, amount, category, note, spend_date, now),
        )
    return redirect(url_for("index"))


@app.route("/settings/spend-limit", methods=["POST"])
@login_required
def update_spend_limit():
    ensure_db()
    user_id = g.user["id"]
    daily_limit = parse_float(request.form.get("daily_spend_limit", "0"), 0.0)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO settings (user_id, daily_spend_limit)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET daily_spend_limit = excluded.daily_spend_limit
            """,
            (user_id, daily_limit),
        )
    return redirect(url_for("index"))


@app.route("/settings/reset-cycle", methods=["POST"])
@login_required
def update_reset_cycle():
    ensure_db()
    user_id = g.user["id"]
    cycle_days = normalize_cycle_days(request.form.get("reset_cycle_days", ""))
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO settings (user_id, daily_spend_limit, reset_cycle_days)
            VALUES (?, 0, ?)
            ON CONFLICT(user_id) DO UPDATE SET reset_cycle_days = excluded.reset_cycle_days
            """,
            (user_id, cycle_days),
        )
    return redirect(url_for("index"))


@app.route("/reflection/save", methods=["POST"])
@login_required
def save_reflection():
    ensure_db()
    user_id = g.user["id"]
    log_date = request.form.get("log_date", "").strip() or today_str()
    mood = request.form.get("mood", "").strip()
    wins = request.form.get("wins", "").strip()
    blockers = request.form.get("blockers", "").strip()
    gratitude = request.form.get("gratitude", "").strip()
    created_at = datetime.now().isoformat(timespec="seconds")

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO daily_reflections (user_id, log_date, mood, wins, blockers, gratitude, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, log_date) DO UPDATE SET
                mood = excluded.mood,
                wins = excluded.wins,
                blockers = excluded.blockers,
                gratitude = excluded.gratitude
            """,
            (user_id, log_date, mood, wins, blockers, gratitude, created_at),
        )
    return redirect(url_for("index"))


@app.route("/budgets/add", methods=["POST"])
@login_required
def add_budget():
    ensure_db()
    user_id = g.user["id"]
    category = request.form.get("category", "").strip()
    daily_limit = parse_float(request.form.get("daily_limit", "0"), 0.0)
    weekly_limit = parse_float(request.form.get("weekly_limit", "0"), 0.0)

    if not category:
        return redirect(url_for("index"))

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO spending_budgets (user_id, category, daily_limit, weekly_limit)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, category) DO UPDATE SET
                daily_limit = excluded.daily_limit,
                weekly_limit = excluded.weekly_limit
            """,
            (user_id, category, daily_limit, weekly_limit),
        )
    return redirect(url_for("index"))


@app.route("/budgets/delete/<int:budget_id>", methods=["POST"])
@login_required
def delete_budget(budget_id: int):
    ensure_db()
    user_id = g.user["id"]
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM spending_budgets WHERE id = ? AND user_id = ?",
            (budget_id, user_id),
        )
    return redirect(url_for("index"))


@app.route("/spending/delete/<int:entry_id>", methods=["POST"])
@login_required
def delete_spending(entry_id: int):
    ensure_db()
    user_id = g.user["id"]
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM spending_entries WHERE id = ? AND user_id = ?",
            (entry_id, user_id),
        )
    return redirect(url_for("index"))


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
