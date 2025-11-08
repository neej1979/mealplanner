# gui_app.py
import io
import os
import zipfile
from datetime import date
from pathlib import Path
from typing import Dict, List

import streamlit as st

# Import your existing modules
from mealplanner.paths import default_outdir, data_dir
from mealplanner import db as dbmod
from mealplanner.validators import (
    validate_recipe_schema,
    normalize_llm_candidate,
    hard_guardrails,
)
from mealplanner.groceries import aggregate_ingredients
from mealplanner.planner import plan_week, DAYS
from mealplanner import llm as llmmod

# ------------------------ utilities ------------------------

def _load_json(path: Path):
    import json
    return json.loads(path.read_text())

def _write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)

def _zip_dir(dir_path: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in dir_path.rglob("*"):
            if p.is_file():
                zf.write(p, arcname=p.relative_to(dir_path))
    buf.seek(0)
    return buf.read()

def _slugify_for_filename(s: str) -> str:
    import re, secrets
    s = s.lower().strip().replace("&", "and").replace("/", "-")
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^a-z0-9-]+", "", s)
    return s[:40] or f"r-{secrets.token_hex(3)}"

def _ensure_llm_key():
    # Respect env or ~/.config/mealplanner/config.json
    from mealplanner.config import get_openai_key
    key = get_openai_key()
    if not key:
        st.error("Missing OpenAI API key. Set OPENAI_API_KEY env or ~/.config/mealplanner/config.json")
        st.stop()

# ------------------------ planning core (lifted from CLI) ------------------------

def generate_plan(
    budget: float,
    db_path: Path | None,
    outdir: Path,
    week_start_str: str | None,
    min_llm: int,
    seed: int | None,
    rating_weight: float,
    no_repeat_weeks: int,
    block_low_rated_weeks: int,
    allow_llm: bool = True,
) -> Dict:
    """
    Runs the same logic as `mealplanner plan` but returns a dict with paths and summary.
    Writes mealplan.txt, shopping_list.csv, recipes/*.md, COOKBOOK.md into outdir.
    """
    _ensure_llm_key()

    recipes_path = data_dir() / "recipes.json"
    prefs_path = data_dir() / "user_prefs.json"

    curated = _load_json(recipes_path)
    # schema check
    bad = []
    for r in curated:
        ok, errs = validate_recipe_schema(r)
        if not ok:
            bad.append((r.get("id", "<no id>"), "; ".join(errs)))
    if bad:
        raise RuntimeError("Invalid curated recipe schema; fix data/recipes.json")

    prefs = _load_json(prefs_path)

    # DB wiring
    exclude_ids = set()
    ratings_avg, ratings_count = {}, {}
    conn = None
    if db_path:
        conn = dbmod.ensure_db(str(db_path))
        dbmod.upsert_recipes(conn, [dict(r, **{"source": r.get("source","curated")}) for r in curated])

        if no_repeat_weeks > 0:
            exclude_ids |= set(dbmod.recent_recipe_ids(conn, days=7 * no_repeat_weeks))
        if block_low_rated_weeks > 0:
            exclude_ids |= set(dbmod.recent_low_rated(conn, weeks=block_low_rated_weeks, threshold=2))

        ratings_avg = dbmod.average_ratings(conn)
        ratings_count = dbmod.rating_counts(conn)

    curated_names = sorted({r["name"] for r in curated})

    # LLM proposals
    all_recipes = list(curated)
    llm_raw = llm_norm = llm_kept = 0
    reject_schema = reject_guard = reject_recentban = 0

    if allow_llm:
        context = {
            "people": prefs.get("people", 2),
            "budget_week_usd": budget,
            "dislikes": prefs.get("dislikes", []),
            "avoid_whole_tomatoes": prefs.get("avoid_whole_tomatoes", True),
            "appliances": prefs.get("appliances", []),
            "effort": prefs.get("effort", "low"),
            "avoid_ids": list(exclude_ids | {r["id"] for r in curated}),
            "avoid_names": curated_names,
        }
        import secrets, re
        _slug_re = re.compile(r"[^a-z0-9-]+")
        def slugify(s: str) -> str:
            s = s.lower().strip().replace("&", "and").replace("/", "-")
            s = re.sub(r"\s+", "-", s)
            s = _slug_re.sub("", s)
            return s[:40] or f"r-{secrets.token_hex(3)}"

        raw_cands = llmmod.propose_candidates(context)
        llm_raw = len(raw_cands)

        normalized = []
        for c in raw_cands:
            cid = c.get("id") or c.get("name") or f"r-{secrets.token_hex(3)}"
            c["id"] = slugify(str(cid))
            n = normalize_llm_candidate(c)
            ok_schema, _ = validate_recipe_schema(n)
            if ok_schema:
                normalized.append(n)
            else:
                reject_schema += 1
        llm_norm = len(normalized)

        seen_ids = {r["id"] for r in all_recipes}
        accepted = []
        for n in normalized:
            if not hard_guardrails(n, prefs.get("avoid_whole_tomatoes", True)):
                reject_guard += 1
                continue
            if n["id"] in exclude_ids:
                reject_recentban += 1
                continue
            if n["id"] in seen_ids:
                n["id"] = f"{n['id']}-{secrets.token_hex(2)}"
            seen_ids.add(n["id"])
            n["source"] = "llm"
            accepted.append(n)

        llm_kept = len(accepted)
        if accepted:
            all_recipes.extend(accepted)
            if conn:
                dbmod.upsert_recipes(conn, accepted)

    # Plan selection
    plan = plan_week(
        recipes=all_recipes,
        prefs=prefs,
        budget=budget,
        exclude_recipe_ids=exclude_ids,
        ratings_avg=ratings_avg,
        ratings_count=ratings_count,
        fallback_on_shortage=True,
        min_llm=max(0, int(min_llm)),
        seed=seed,
        rating_weight=float(rating_weight),
    )

    # Write outputs
    outdir.mkdir(parents=True, exist_ok=True)
    rec_map = {r["id"]: r for r in all_recipes}

    # mealplan.txt
    plan_txt = outdir / "mealplan.txt"
    with plan_txt.open("w") as f:
        f.write(f"Estimated weekly cost: ${plan['total_cost_usd']:.2f}\n")
        f.write(f"Weekly protein: {plan['protein_g_total']:.0f} g | Weekly fiber: {plan['fiber_g_total']:.0f} g\n")
        f.write(f"(Info: LLM raw={llm_raw}, normalized={llm_norm}, accepted={llm_kept}; "
                f"rejected schema={reject_schema}, guardrails={reject_guard}, recent-ban={reject_recentban})\n\n")
        for item in plan["items"]:
            r = rec_map[item["recipe_id"]]
            src = r.get("source", "curated")
            label = "LLM" if str(src) == "llm" else "curated"
            f.write(
                f"{item['day']}: {r['name']} [{label}] ({r['minutes']} min, {r['method']})  "
                f"[Protein {r['macros']['protein_g']} g | Fiber {r['macros']['fiber_g']} g | ${r['cost_usd']:.2f}]\n"
            )

    # shopping_list.csv
    chosen = [rec_map[i["recipe_id"]] for i in plan["items"]]
    need = aggregate_ingredients(chosen)
    shop_csv = outdir / "shopping_list.csv"
    with shop_csv.open("w") as f:
        f.write("item,qty\n")
        for k, v in sorted(need.items()):
            f.write(f"{k},{v}\n")

    # recipes markdowns
    recipes_dir = outdir / "recipes"
    recipes_dir.mkdir(parents=True, exist_ok=True)
    cookbook_parts = []
    newly_instructed = []

    for r in chosen:
        md = r.get("instructions_md")
        if not md:
            try:
                md = llmmod.generate_instructions_md(r, _load_json(prefs_path))
                r["instructions_md"] = md
                newly_instructed.append(r["id"])
            except Exception:
                md = f"# {r['name']}\n\n_Time_: ~{r.get('minutes', 30)} min  \n_Method_: {r.get('method','stovetop')}\n\n## Ingredients\n" + \
                     "\n".join([f"- {i.get('qty','')} {i.get('item','')}".strip() for i in r.get('ingredients', [])]) + \
                     "\n\n## Steps\n1. Prep ingredients.\n2. Cook using listed method.\n3. Season to taste.\n"
                r["instructions_md"] = md

        (_ := (recipes_dir / f"{_slugify_for_filename(r['id'])}.md")).write_text(md)
        cookbook_parts.append(md.strip() + "\n")

    if newly_instructed and conn:
        dbmod.upsert_recipes(conn, chosen)

    (recipes_dir / "COOKBOOK.md").write_text("\n\n---\n\n".join(cookbook_parts))

    # persist plan into DB
    if conn:
        from datetime import datetime
        if week_start_str:
            ws = datetime.strptime(week_start_str, "%Y-%m-%d").date()
        else:
            ws = date.today()
        day_index_map = {d: i for i, d in enumerate(DAYS)}
        items_for_db = [{"day_index": day_index_map[i["day"]], "recipe_id": i["recipe_id"]} for i in plan["items"]]
        totals = {
            "total_cost_usd": plan["total_cost_usd"],
            "protein_g_total": plan["protein_g_total"],
            "fiber_g_total": plan["fiber_g_total"],
        }
        dbmod.save_plan(conn, ws, prefs.get("people", 2), float(budget), items_for_db, totals)

    return {
        "plan": plan,
        "rec_map": rec_map,
        "paths": {
            "plan_txt": plan_txt,
            "shopping_csv": shop_csv,
            "recipes_dir": recipes_dir,
        }
    }

def fetch_last_week_for_rating(db_path: Path) -> List[Dict]:
    """
    Return the most recent plan's items joined with recipe names.
    """
    conn = dbmod.ensure_db(str(db_path))
    cur = conn.cursor()
    cur.execute("""
      SELECT p.plan_id, p.week_start
      FROM plans p
      ORDER BY date(p.week_start) DESC
      LIMIT 1
    """)
    row = cur.fetchone()
    if not row:
        return []

    plan_id = row["plan_id"]
    cur.execute("""
      SELECT pi.day_index, pi.recipe_id, r.name
      FROM plan_items pi
      JOIN recipes r ON r.id = pi.recipe_id
      WHERE pi.plan_id = ?
      ORDER BY pi.day_index ASC
    """, (plan_id,))
    items = [{"day_index": r["day_index"], "recipe_id": r["recipe_id"], "name": r["name"]} for r in cur.fetchall()]
    return items

# ------------------------ UI ------------------------

st.set_page_config(page_title="MealPlanner", page_icon="🍽️", layout="wide")

st.title("MealPlanner GUI")

with st.sidebar:
    st.header("Settings")
    db_path = st.text_input("SQLite DB path", value=str(Path.cwd() / "mealplanner.db"))
    out_dir = st.text_input("Output folder", value=str(default_outdir()))
    budget = st.number_input("Weekly Budget ($)", min_value=20.0, max_value=500.0, value=100.0, step=1.0)
    min_llm = st.number_input("Min LLM recipes", min_value=0, max_value=7, value=2, step=1)
    seed = st.number_input("Seed (optional)", min_value=0, max_value=10_000, value=23, step=1)
    rating_weight = st.slider("Ratings weight", min_value=0.0, max_value=2.5, value=1.2, step=0.1)
    no_repeat_weeks = st.slider("Avoid repeats (weeks)", min_value=0, max_value=12, value=4, step=1)
    block_low_rated_weeks = st.slider("Block ≤2★ (weeks)", min_value=0, max_value=12, value=4, step=1)
    week_start = st.text_input("Week start (YYYY-MM-DD, optional)", value="")
    allow_llm = st.checkbox("Use LLM for new recipes", value=True)

tab_plan, tab_rate = st.tabs(["📅 Plan & Downloads", "⭐ Rate Last Week"])

with tab_plan:
    st.subheader("Generate this week's plan")
    if st.button("Generate Plan", type="primary", use_container_width=True):
        try:
            result = generate_plan(
                budget=float(budget),
                db_path=Path(db_path) if db_path else None,
                outdir=Path(out_dir),
                week_start_str=week_start or None,
                min_llm=int(min_llm),
                seed=int(seed) if seed is not None else None,
                rating_weight=float(rating_weight),
                no_repeat_weeks=int(no_repeat_weeks),
                block_low_rated_weeks=int(block_low_rated_weeks),
                allow_llm=bool(allow_llm),
            )
            plan = result["plan"]
            paths = result["paths"]

            st.success("Plan generated.")
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Estimated cost", f"${plan['total_cost_usd']:.2f}")
            with col2:
                st.metric("Weekly protein", f"{plan['protein_g_total']:.0f} g")
            with col3:
                st.metric("Weekly fiber", f"{plan['fiber_g_total']:.0f} g")

            # Preview plan text
            st.code(paths["plan_txt"].read_text(), language="text")

            # Downloads
            st.download_button(
                "Download mealplan.txt",
                data=paths["plan_txt"].read_bytes(),
                file_name="mealplan.txt",
                mime="text/plain",
                use_container_width=True,
            )
            st.download_button(
                "Download shopping_list.csv",
                data=paths["shopping_csv"].read_bytes(),
                file_name="shopping_list.csv",
                mime="text/csv",
                use_container_width=True,
            )
            # Zip recipes
            zbytes = _zip_dir(paths["recipes_dir"])
            st.download_button(
                "Download recipes (ZIP)",
                data=zbytes,
                file_name="recipes.zip",
                mime="application/zip",
                use_container_width=True,
            )
            st.info(f"Recipes saved to: {paths['recipes_dir']}")
        except Exception as e:
            st.error(f"Planning failed: {e}")

with tab_rate:
    st.subheader("Rate last week's dinners")
    if not Path(db_path).exists():
        st.info("No database yet. Generate a plan first to create one.")
    else:
        items = fetch_last_week_for_rating(Path(db_path))
        if not items:
            st.info("No prior plans found.")
        else:
            ratings_state = {}
            day_names = DAYS
            for it in items:
                rid = it["recipe_id"]
                lbl = f"{day_names[it['day_index']]} — {it['name']}"
                ratings_state[rid] = st.radio(lbl, [1, 2, 3, 4, 5], index=3, horizontal=True, key=f"rate_{rid}")

            if st.button("Save Ratings", type="primary"):
                try:
                    conn = dbmod.ensure_db(str(db_path))
                    today = date.today()
                    n = 0
                    for rid, stars in ratings_state.items():
                        dbmod.add_rating(conn, recipe_id=rid, cooked_on=today, rating=int(stars), comments=None)
                        n += 1
                    st.success(f"Saved {n} ratings to {db_path}.")
                except Exception as e:
                    st.error(f"Failed to save ratings: {e}")
