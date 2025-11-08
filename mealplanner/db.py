from __future__ import annotations

import sqlite3
from sqlite3 import Connection
from typing import Iterable, Dict, List, Optional
from uuid import uuid4
from datetime import date

# ---------- Public API ----------

def connect(db_path: str | None) -> Connection:
    path = db_path or ":memory:"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _pragmas(conn)
    return conn

def ensure_db(db_path: str | None) -> Connection:
    conn = connect(db_path)
    init_db(conn)
    return conn

def init_db(conn: Connection) -> None:
    cur = conn.cursor()
    cur.executescript(SCHEMA_SQL)
    # migration: add instructions_md if missing (older installs)
    _ensure_column(conn, "recipes", "instructions_md", "TEXT")
    conn.commit()

def upsert_recipes(conn: Connection, recipes: Iterable[Dict]) -> None:
    cur = conn.cursor()
    for r in recipes:
        cur.execute(
            """
            INSERT INTO recipes (id, name, source, minutes, method,
                                 protein_g, fiber_g, kcals, cost_usd,
                                 tags, ingredients_json, instructions_md)
            VALUES (:id, :name, :source, :minutes, :method,
                    :protein_g, :fiber_g, :kcals, :cost_usd,
                    :tags_csv, :ingredients_json, :instructions_md)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                source=excluded.source,
                minutes=excluded.minutes,
                method=excluded.method,
                protein_g=excluded.protein_g,
                fiber_g=excluded.fiber_g,
                kcals=excluded.kcals,
                cost_usd=excluded.cost_usd,
                tags=excluded.tags,
                ingredients_json=excluded.ingredients_json,
                instructions_md=COALESCE(excluded.instructions_md, instructions_md)
            """,
            _recipe_params(r),
        )
    conn.commit()

def save_plan(conn: Connection, week_start: date, people: int, budget_usd: float,
              items: List[Dict], totals: Dict[str, float]) -> str:
    plan_id = uuid4().hex
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO plans (plan_id, week_start, people, budget_usd,
                           total_cost_usd, total_protein_g, total_fiber_g)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            plan_id,
            week_start.isoformat(),
            people,
            float(budget_usd),
            float(totals.get("total_cost_usd", 0.0)),
            float(totals.get("protein_g_total", 0.0)),
            float(totals.get("fiber_g_total", 0.0)),
        ),
    )
    cur.executemany(
        "INSERT INTO plan_items (plan_id, day_index, recipe_id) VALUES (?, ?, ?)",
        [(plan_id, it["day_index"], it["recipe_id"]) for it in items],
    )
    conn.commit()
    return plan_id

def add_rating(conn: Connection, recipe_id: str, cooked_on: date,
               rating: int, comments: Optional[str] = None) -> None:
    if not (1 <= rating <= 5):
        raise ValueError("rating must be between 1 and 5")
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO ratings (recipe_id, cooked_on, rating, comments)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(recipe_id, cooked_on) DO UPDATE SET
            rating=excluded.rating,
            comments=COALESCE(excluded.comments, comments)
        """,
        (recipe_id, cooked_on.isoformat(), rating, comments),
    )
    conn.commit()

def recent_recipe_ids(conn: Connection, days: int = 28) -> List[str]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT pi.recipe_id
        FROM plan_items pi
        JOIN plans p ON p.plan_id = pi.plan_id
        WHERE date(p.week_start) >= date('now', ?)
        """,
        (f"-{days} days",),
    )
    return [row[0] for row in cur.fetchall()]

def average_ratings(conn: Connection) -> Dict[str, float]:
    cur = conn.cursor()
    cur.execute("SELECT recipe_id, AVG(rating) AS avg_rating FROM ratings GROUP BY recipe_id")
    return {row["recipe_id"]: float(row["avg_rating"]) for row in cur.fetchall()}

def rating_counts(conn: Connection) -> Dict[str, int]:
    cur = conn.cursor()
    cur.execute("SELECT recipe_id, COUNT(*) AS n FROM ratings GROUP BY recipe_id")
    return {row["recipe_id"]: int(row["n"]) for row in cur.fetchall()}

def recent_low_rated(conn: Connection, weeks: int = 4, threshold: int = 2) -> List[str]:
    if weeks <= 0:
        return []
    days = 7 * weeks
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT recipe_id
        FROM ratings
        WHERE rating <= ?
          AND date(cooked_on) >= date('now', ?)
        """,
        (threshold, f"-{days} days"),
    )
    return [row[0] for row in cur.fetchall()]

# ---------- Internal helpers ----------

def _pragmas(conn: Connection) -> None:
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys = ON;")
    cur.execute("PRAGMA journal_mode = WAL;")
    cur.execute("PRAGMA synchronous = NORMAL;")

def _has_column(conn: Connection, table: str, column: str) -> bool:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table});")
    return any(row[1] == column for row in cur.fetchall())

def _ensure_column(conn: Connection, table: str, column: str, type_sql: str) -> None:
    if not _has_column(conn, table, column):
        cur = conn.cursor()
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_sql};")
        conn.commit()

def _recipe_params(r: Dict) -> Dict:
    import json
    tags = r.get("tags", [])
    mac = r.get("macros", {})
    return {
        "id": r["id"],
        "name": r["name"],
        "source": r.get("source", "curated"),
        "minutes": int(r.get("minutes", 0)),
        "method": r.get("method", "stovetop"),
        "protein_g": float(mac.get("protein_g", 0.0)),
        "fiber_g": float(mac.get("fiber_g", 0.0)),
        "kcals": float(mac.get("kcals", 0.0)),
        "cost_usd": float(r.get("cost_usd", 0.0)),
        "tags_csv": ",".join(tags),
        "ingredients_json": json.dumps(r.get("ingredients", []), separators=(",", ":")),
        "instructions_md": r.get("instructions_md"),
    }

# ---------- Schema ----------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS recipes (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('curated','llm')),
  minutes INTEGER,
  method TEXT,
  protein_g REAL,
  fiber_g REAL,
  kcals REAL,
  cost_usd REAL,
  tags TEXT,
  ingredients_json TEXT,
  instructions_md TEXT
);

CREATE TABLE IF NOT EXISTS plans (
  plan_id TEXT PRIMARY KEY,
  week_start DATE NOT NULL,
  people INTEGER NOT NULL,
  budget_usd REAL NOT NULL,
  total_cost_usd REAL,
  total_protein_g REAL,
  total_fiber_g REAL
);

CREATE TABLE IF NOT EXISTS plan_items (
  plan_id TEXT NOT NULL,
  day_index INTEGER NOT NULL CHECK (day_index BETWEEN 0 AND 6),
  recipe_id TEXT NOT NULL,
  PRIMARY KEY (plan_id, day_index),
  FOREIGN KEY (plan_id) REFERENCES plans(plan_id) ON DELETE CASCADE,
  FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS ratings (
  recipe_id TEXT NOT NULL,
  cooked_on DATE NOT NULL,
  rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
  comments TEXT,
  PRIMARY KEY (recipe_id, cooked_on),
  FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_plans_week_start ON plans(week_start);
CREATE INDEX IF NOT EXISTS idx_plan_items_recipe ON plan_items(recipe_id);
CREATE INDEX IF NOT EXISTS idx_ratings_recipe ON ratings(recipe_id);
"""
