from __future__ import annotations

from datetime import datetime, date, timedelta
import sqlite3
from pathlib import Path
from typing import TypedDict

from flask import Flask, redirect, render_template, request, url_for

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "tasks.db"

app = Flask(__name__)
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


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                time_block TEXT NOT NULL DEFAULT '',
                priority INTEGER NOT NULL DEFAULT 2,
                scheduled_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS checklist_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                scheduled_date TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS routines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                time_of_day TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS routine_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                routine_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (routine_id) REFERENCES routines (id)
            );

            CREATE TABLE IF NOT EXISTS routine_item_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                routine_item_id INTEGER NOT NULL,
                log_date TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0,
                UNIQUE (routine_item_id, log_date),
                FOREIGN KEY (routine_item_id) REFERENCES routine_items (id)
            );

            CREATE TABLE IF NOT EXISTS habits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                target_count INTEGER NOT NULL DEFAULT 1,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS habit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                habit_id INTEGER NOT NULL,
                log_date TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                UNIQUE (habit_id, log_date),
                FOREIGN KEY (habit_id) REFERENCES habits (id)
            );

            CREATE TABLE IF NOT EXISTS spending_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                note TEXT NOT NULL,
                spend_date TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS spending_budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL UNIQUE,
                daily_limit REAL NOT NULL DEFAULT 0,
                weekly_limit REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                daily_spend_limit REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS daily_reflections (
                log_date TEXT PRIMARY KEY,
                mood TEXT NOT NULL DEFAULT '',
                wins TEXT NOT NULL DEFAULT '',
                blockers TEXT NOT NULL DEFAULT '',
                gratitude TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            """
        )


def ensure_db() -> None:
    global _db_ready
    if not _db_ready:
        init_db()
        _db_ready = True


def today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


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


def get_settings() -> sqlite3.Row:
    with get_conn() as conn:
        settings = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        if settings is None:
            conn.execute("INSERT INTO settings (id, daily_spend_limit) VALUES (1, 0)")
            settings = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    return settings


def compute_habit_streaks(
    habits: list[sqlite3.Row], habit_log_rows: list[sqlite3.Row]
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
        for offset in range(0, 366):
            check_date = today - timedelta(days=offset)
            count = log_map.get(habit_id, {}).get(check_date, 0)
            if count >= target and target > 0:
                streak += 1
            else:
                break
        streaks[habit_id] = streak
    return streaks


def update_missed_plans() -> None:
    today_iso = today_str()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE plans
            SET status = 'missed'
            WHERE status = 'pending' AND scheduled_date < ?
            """,
            (today_iso,),
        )


def get_overview_stats() -> dict[str, int]:
    stats = {
        "plans": 0,
        "checklist": 0,
        "habits": 0,
        "routines": 0,
        "spending_entries": 0,
    }
    with get_conn() as conn:
        stats["plans"] = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
        stats["checklist"] = conn.execute(
            "SELECT COUNT(*) FROM checklist_items"
        ).fetchone()[0]
        stats["habits"] = conn.execute("SELECT COUNT(*) FROM habits").fetchone()[0]
        stats["routines"] = conn.execute("SELECT COUNT(*) FROM routines").fetchone()[0]
        stats["spending_entries"] = conn.execute(
            "SELECT COUNT(*) FROM spending_entries"
        ).fetchone()[0]
    return stats


@app.route("/", methods=["GET"])
def index():
    ensure_db()
    update_missed_plans()
    today_iso = today_str()
    week_start = (date.today() - timedelta(days=6)).strftime("%Y-%m-%d")
    year_start = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")

    with get_conn() as conn:
        plans = conn.execute(
            """
            SELECT * FROM plans
            WHERE scheduled_date = ?
            ORDER BY priority DESC, id ASC
            """,
            (today_iso,),
        ).fetchall()
        checklist = conn.execute(
            """
            SELECT * FROM checklist_items
            WHERE scheduled_date = ?
            ORDER BY id ASC
            """,
            (today_iso,),
        ).fetchall()
        habits = conn.execute(
            "SELECT * FROM habits WHERE active = 1 ORDER BY id ASC"
        ).fetchall()
        habit_logs = conn.execute(
            "SELECT habit_id, count FROM habit_logs WHERE log_date = ?",
            (today_iso,),
        ).fetchall()
        habit_logs_all = conn.execute(
            """
            SELECT habit_id, log_date, count
            FROM habit_logs
            WHERE log_date BETWEEN ? AND ?
            """,
            (year_start, today_iso),
        ).fetchall()
        routines = conn.execute(
            "SELECT * FROM routines WHERE active = 1 ORDER BY time_of_day, name"
        ).fetchall()
        routine_items = conn.execute(
            """
            SELECT * FROM routine_items
            WHERE routine_id IN (SELECT id FROM routines WHERE active = 1)
            ORDER BY sort_order, id
            """
        ).fetchall()
        routine_logs = conn.execute(
            "SELECT routine_item_id, done FROM routine_item_logs WHERE log_date = ?",
            (today_iso,),
        ).fetchall()
        spending_entries = conn.execute(
            """
            SELECT * FROM spending_entries
            WHERE spend_date = ?
            ORDER BY created_at DESC
            """,
            (today_iso,),
        ).fetchall()
        spending_budgets = conn.execute(
            "SELECT * FROM spending_budgets ORDER BY category"
        ).fetchall()
        spending_by_category = conn.execute(
            """
            SELECT category, SUM(amount) as total
            FROM spending_entries
            WHERE spend_date = ?
            GROUP BY category
            """,
            (today_iso,),
        ).fetchall()
        spending_by_category_week = conn.execute(
            """
            SELECT category, SUM(amount) as total
            FROM spending_entries
            WHERE spend_date BETWEEN ? AND ?
            GROUP BY category
            """,
            (week_start, today_iso),
        ).fetchall()
        reflection = conn.execute(
            "SELECT * FROM daily_reflections WHERE log_date = ?",
            (today_iso,),
        ).fetchone()

    habit_log_map = {row["habit_id"]: row["count"] for row in habit_logs}
    habit_streaks = compute_habit_streaks(habits, habit_logs_all)
    routine_log_map = {row["routine_item_id"]: row["done"] for row in routine_logs}
    spend_category_map = {row["category"]: row["total"] for row in spending_by_category}
    spend_week_category_map = {
        row["category"]: row["total"] for row in spending_by_category_week
    }
    settings = get_settings()

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
    daily_spend_limit = settings["daily_spend_limit"]
    left_to_spend = (
        daily_spend_limit - spend_total if daily_spend_limit > 0 else None
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
        spending_budgets=spending_budgets,
        spend_category_map=spend_category_map,
        spend_week_category_map=spend_week_category_map,
        daily_spend_limit=daily_spend_limit,
        left_to_spend=left_to_spend,
        reflection=reflection,
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
def about():
    ensure_db()
    update_missed_plans()
    stats = get_overview_stats()
    return render_template("about.html", stats=stats, now=datetime.now())


@app.route("/review", methods=["GET"])
def weekly_review():
    ensure_db()
    update_missed_plans()
    today = date.today()
    start = today - timedelta(days=6)
    start_iso = start.strftime("%Y-%m-%d")
    end_iso = today.strftime("%Y-%m-%d")

    with get_conn() as conn:
        plan_stats = conn.execute(
            """
            SELECT status, COUNT(*) as count
            FROM plans
            WHERE scheduled_date BETWEEN ? AND ?
            GROUP BY status
            """,
            (start_iso, end_iso),
        ).fetchall()
        spending_by_day = conn.execute(
            """
            SELECT spend_date, SUM(amount) as total
            FROM spending_entries
            WHERE spend_date BETWEEN ? AND ?
            GROUP BY spend_date
            ORDER BY spend_date DESC
            """,
            (start_iso, end_iso),
        ).fetchall()
        spending_by_category = conn.execute(
            """
            SELECT category, SUM(amount) as total
            FROM spending_entries
            WHERE spend_date BETWEEN ? AND ?
            GROUP BY category
            ORDER BY total DESC
            """,
            (start_iso, end_iso),
        ).fetchall()
        habit_counts = conn.execute(
            """
            SELECT h.name, SUM(hl.count) as total
            FROM habits h
            LEFT JOIN habit_logs hl ON h.id = hl.habit_id
                AND hl.log_date BETWEEN ? AND ?
            WHERE h.active = 1
            GROUP BY h.id
            ORDER BY total DESC
            """,
            (start_iso, end_iso),
        ).fetchall()
        routine_completions = conn.execute(
            """
            SELECT r.name, SUM(ril.done) as total
            FROM routines r
            LEFT JOIN routine_items ri ON r.id = ri.routine_id
            LEFT JOIN routine_item_logs ril ON ri.id = ril.routine_item_id
                AND ril.log_date BETWEEN ? AND ?
            WHERE r.active = 1
            GROUP BY r.id
            ORDER BY total DESC
            """,
            (start_iso, end_iso),
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


@app.route("/plan/add", methods=["POST"])
def add_plan():
    ensure_db()
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
            INSERT INTO plans (title, time_block, priority, scheduled_date, created_at, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
            """,
            (title, time_block, priority, scheduled_date, now),
        )
    return redirect(url_for("index"))


@app.route("/plan/complete/<int:plan_id>", methods=["POST"])
def complete_plan(plan_id: int):
    ensure_db()
    with get_conn() as conn:
        conn.execute(
            "UPDATE plans SET status = 'done' WHERE id = ?",
            (plan_id,),
        )
    return redirect(url_for("index"))


@app.route("/plan/reopen/<int:plan_id>", methods=["POST"])
def reopen_plan(plan_id: int):
    ensure_db()
    with get_conn() as conn:
        conn.execute(
            "UPDATE plans SET status = 'pending' WHERE id = ?",
            (plan_id,),
        )
    return redirect(url_for("index"))


@app.route("/plan/delete/<int:plan_id>", methods=["POST"])
def delete_plan(plan_id: int):
    ensure_db()
    with get_conn() as conn:
        conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
    return redirect(url_for("index"))


@app.route("/checklist/add", methods=["POST"])
def add_checklist_item():
    ensure_db()
    label = request.form.get("label", "").strip()
    scheduled_date = request.form.get("scheduled_date", "").strip() or today_str()

    if not label:
        return redirect(url_for("index"))

    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO checklist_items (label, scheduled_date, done, created_at)
            VALUES (?, ?, 0, ?)
            """,
            (label, scheduled_date, now),
        )
    return redirect(url_for("index"))


@app.route("/checklist/toggle/<int:item_id>", methods=["POST"])
def toggle_checklist_item(item_id: int):
    ensure_db()
    with get_conn() as conn:
        current = conn.execute(
            "SELECT done FROM checklist_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        if current is None:
            return redirect(url_for("index"))
        new_value = 0 if current["done"] else 1
        conn.execute(
            "UPDATE checklist_items SET done = ? WHERE id = ?",
            (new_value, item_id),
        )
    return redirect(url_for("index"))


@app.route("/checklist/delete/<int:item_id>", methods=["POST"])
def delete_checklist_item(item_id: int):
    ensure_db()
    with get_conn() as conn:
        conn.execute("DELETE FROM checklist_items WHERE id = ?", (item_id,))
    return redirect(url_for("index"))


@app.route("/routines/add", methods=["POST"])
def add_routine():
    ensure_db()
    name = request.form.get("name", "").strip()
    time_of_day = request.form.get("time_of_day", "any").strip()

    if not name:
        return redirect(url_for("index"))

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO routines (name, time_of_day, active) VALUES (?, ?, 1)",
            (name, time_of_day),
        )
    return redirect(url_for("index"))


@app.route("/routine-items/add", methods=["POST"])
def add_routine_item():
    ensure_db()
    routine_id = request.form.get("routine_id", "").strip()
    label = request.form.get("label", "").strip()
    sort_order = parse_int(request.form.get("sort_order", "0"), 0)

    if not routine_id or not label:
        return redirect(url_for("index"))

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO routine_items (routine_id, label, sort_order)
            VALUES (?, ?, ?)
            """,
            (routine_id, label, sort_order),
        )
    return redirect(url_for("index"))


@app.route("/routine-items/toggle/<int:item_id>", methods=["POST"])
def toggle_routine_item(item_id: int):
    ensure_db()
    log_date = today_str()
    with get_conn() as conn:
        current = conn.execute(
            """
            SELECT done FROM routine_item_logs
            WHERE routine_item_id = ? AND log_date = ?
            """,
            (item_id, log_date),
        ).fetchone()
        if current is None:
            conn.execute(
                """
                INSERT INTO routine_item_logs (routine_item_id, log_date, done)
                VALUES (?, ?, 1)
                """,
                (item_id, log_date),
            )
        else:
            new_value = 0 if current["done"] else 1
            conn.execute(
                """
                UPDATE routine_item_logs
                SET done = ?
                WHERE routine_item_id = ? AND log_date = ?
                """,
                (new_value, item_id, log_date),
            )
    return redirect(url_for("index"))


@app.route("/habits/add", methods=["POST"])
def add_habit():
    ensure_db()
    name = request.form.get("name", "").strip()
    target_count = parse_int(request.form.get("target_count", "1"), 1)

    if not name:
        return redirect(url_for("index"))

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO habits (name, target_count, active) VALUES (?, ?, 1)",
            (name, target_count),
        )
    return redirect(url_for("index"))


@app.route("/habits/log/<int:habit_id>", methods=["POST"])
def log_habit(habit_id: int):
    ensure_db()
    log_date = today_str()
    with get_conn() as conn:
        current = conn.execute(
            """
            SELECT count FROM habit_logs
            WHERE habit_id = ? AND log_date = ?
            """,
            (habit_id, log_date),
        ).fetchone()
        if current is None:
            conn.execute(
                """
                INSERT INTO habit_logs (habit_id, log_date, count)
                VALUES (?, ?, 1)
                """,
                (habit_id, log_date),
            )
        else:
            conn.execute(
                """
                UPDATE habit_logs
                SET count = count + 1
                WHERE habit_id = ? AND log_date = ?
                """,
                (habit_id, log_date),
            )
    return redirect(url_for("index"))


@app.route("/habits/reset/<int:habit_id>", methods=["POST"])
def reset_habit(habit_id: int):
    ensure_db()
    log_date = today_str()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO habit_logs (habit_id, log_date, count)
            VALUES (?, ?, 0)
            ON CONFLICT(habit_id, log_date) DO UPDATE SET count = 0
            """,
            (habit_id, log_date),
        )
    return redirect(url_for("index"))


@app.route("/spending/add", methods=["POST"])
def add_spending():
    ensure_db()
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
            INSERT INTO spending_entries (amount, category, note, spend_date, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (amount, category, note, spend_date, now),
        )
    return redirect(url_for("index"))


@app.route("/settings/spend-limit", methods=["POST"])
def update_spend_limit():
    ensure_db()
    daily_limit = parse_float(request.form.get("daily_spend_limit", "0"), 0.0)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO settings (id, daily_spend_limit)
            VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET daily_spend_limit = excluded.daily_spend_limit
            """,
            (daily_limit,),
        )
    return redirect(url_for("index"))


@app.route("/reflection/save", methods=["POST"])
def save_reflection():
    ensure_db()
    log_date = request.form.get("log_date", "").strip() or today_str()
    mood = request.form.get("mood", "").strip()
    wins = request.form.get("wins", "").strip()
    blockers = request.form.get("blockers", "").strip()
    gratitude = request.form.get("gratitude", "").strip()
    created_at = datetime.now().isoformat(timespec="seconds")

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO daily_reflections (log_date, mood, wins, blockers, gratitude, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(log_date) DO UPDATE SET
                mood = excluded.mood,
                wins = excluded.wins,
                blockers = excluded.blockers,
                gratitude = excluded.gratitude
            """,
            (log_date, mood, wins, blockers, gratitude, created_at),
        )
    return redirect(url_for("index"))


@app.route("/budgets/add", methods=["POST"])
def add_budget():
    ensure_db()
    category = request.form.get("category", "").strip()
    daily_limit = parse_float(request.form.get("daily_limit", "0"), 0.0)
    weekly_limit = parse_float(request.form.get("weekly_limit", "0"), 0.0)

    if not category:
        return redirect(url_for("index"))

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO spending_budgets (category, daily_limit, weekly_limit)
            VALUES (?, ?, ?)
            ON CONFLICT(category) DO UPDATE SET
                daily_limit = excluded.daily_limit,
                weekly_limit = excluded.weekly_limit
            """,
            (category, daily_limit, weekly_limit),
        )
    return redirect(url_for("index"))


@app.route("/budgets/delete/<int:budget_id>", methods=["POST"])
def delete_budget(budget_id: int):
    ensure_db()
    with get_conn() as conn:
        conn.execute("DELETE FROM spending_budgets WHERE id = ?", (budget_id,))
    return redirect(url_for("index"))


@app.route("/spending/delete/<int:entry_id>", methods=["POST"])
def delete_spending(entry_id: int):
    ensure_db()
    with get_conn() as conn:
        conn.execute("DELETE FROM spending_entries WHERE id = ?", (entry_id,))
    return redirect(url_for("index"))


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
