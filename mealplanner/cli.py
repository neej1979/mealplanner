import argparse
import json
import sys
import pathlib
import re
import secrets
from datetime import date, datetime
from typing import Any, Dict, List, Set

from .planner import plan_week, DAYS
from .groceries import aggregate_ingredients
from .paths import data_dir, default_outdir
from .validators import (
    validate_recipe_schema,
    normalize_llm_candidate,
    hard_guardrails,
)
from . import db as dbmod
from . import llm as llmmod


_slug_re = re.compile(r"[^a-z0-9-]+")

def slugify(s: str) -> str:
    s = s.lower().strip().replace("&", "and").replace("/", "-")
    s = re.sub(r"\s+", "-", s)
    s = _slug_re.sub("", s)
    return s[:40] or f"r-{secrets.token_hex(3)}"

def _read_json(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        sys.exit(f"Error: required data file not found: {path}")
    except json.JSONDecodeError as e:
        sys.exit(f"Error: invalid JSON in {path} ({e})")

def _validate_recipes(recipes: List[Dict]) -> None:
    bad = []
    for r in recipes:
        ok, errs = validate_recipe_schema(r)
        if not ok:
            bad.append((r.get("id", "<no id>"), "; ".join(errs)))
    if bad:
        lines = "\n".join([f" - {rid}: {err}" for rid, err in bad])
        sys.exit(f"Error: invalid recipe schema:\n{lines}")

def _parse_week_start(s: str | None) -> date:
    if not s:
        return date.today()
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        sys.exit("Error: --week-start must be YYYY-MM-DD (e.g., 2025-11-17)")

def _parse_date(s: str | None) -> date:
    if not s:
        return date.today()
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        sys.exit("Error: --date must be YYYY-MM-DD")

def main():
    ap = argparse.ArgumentParser(
        prog="mealplanner",
        description="Budget-aware weekly meal planner with LLM variety and ratings bias",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    # plan
    ap_plan = sub.add_parser("plan", help="Generate a weekly plan and shopping list")
    ap_plan.add_argument("--budget", type=float, default=100.0, help="Weekly budget in USD (default: 100)")
    ap_plan.add_argument("--out", type=pathlib.Path, default=default_outdir(), help="Output directory (default: ./out)")
    ap_plan.add_argument("--db", type=pathlib.Path, default=None, help="SQLite db path (if set, plan is saved)")
    ap_plan.add_argument("--week-start", type=str, default=None, help="Week start date YYYY-MM-DD (default: today)")
    ap_plan.add_argument("--no-repeat-weeks", type=int, default=4, help="Avoid recipes used within N weeks (default: 4; requires --db)")
    ap_plan.add_argument("--no-llm", action="store_true", help="Disable LLM recipe proposals for this run")
    ap_plan.add_argument("--min-llm", type=int, default=2, help="Minimum LLM recipes to include when available (default: 2)")
    ap_plan.add_argument("--seed", type=int, default=None, help="Random seed for tie-breaks (optional)")
    ap_plan.add_argument("--rating-weight", type=float, default=1.2, help="Strength of ratings bias (default: 1.2)")
    ap_plan.add_argument("--block-low-rated-weeks", type=int, default=4, help="Block recipes rated ≤2★ in last N weeks (default: 4; 0 disables)")

    # rate
    ap_rate = sub.add_parser("rate", help="Rate a cooked recipe 1..5")
    ap_rate.add_argument("--db", type=pathlib.Path, required=True, help="SQLite db path")
    ap_rate.add_argument("--recipe-id", required=True, help="Recipe ID to rate")
    ap_rate.add_argument("--rating", type=int, required=True, help="1..5 stars")
    ap_rate.add_argument("--date", type=str, default=None, help="Cooked on (YYYY-MM-DD). Default: today")
    ap_rate.add_argument("--comments", type=str, default=None, help="Optional comments")

    # history
    ap_hist = sub.add_parser("history", help="Show recent plans")
    ap_hist.add_argument("--db", type=pathlib.Path, required=True, help="SQLite db path")
    ap_hist.add_argument("--weeks", type=int, default=6, help="How many recent plans to show (default: 6)")

    args = ap.parse_args()

    if args.cmd == "plan":
        if args.budget <= 0:
            sys.exit("Error: --budget must be > 0")

        outdir: pathlib.Path = args.out
        outdir.mkdir(parents=True, exist_ok=True)

        recipes_path = data_dir() / "recipes.json"
        prefs_path = data_dir() / "user_prefs.json"

        curated = _read_json(recipes_path)
        _validate_recipes(curated)
        prefs = _read_json(prefs_path)

        # Exclusions + ratings if DB available
        exclude_ids: Set[str] = set()
        ratings_avg: Dict[str, float] = {}
        ratings_count: Dict[str, int] = {}
        conn = None
        if args.db:
            conn = dbmod.ensure_db(str(args.db))
            dbmod.upsert_recipes(conn, [dict(r, **{"source": r.get("source","curated")}) for r in curated])

            if args.no_repeat_weeks > 0:
                exclude_ids |= set(dbmod.recent_recipe_ids(conn, days=7 * args.no_repeat_weeks))
            if args.block_low_rated_weeks > 0:
                exclude_ids |= set(dbmod.recent_low_rated(conn, weeks=args.block_low_rated_weeks, threshold=2))

            ratings_avg = dbmod.average_ratings(conn)
            ratings_count = dbmod.rating_counts(conn)

        curated_names = sorted({r["name"] for r in curated})

        # LLM proposals
        llm_raw = llm_norm = llm_kept = 0
        reject_schema = reject_guard = reject_recentban = 0
        all_recipes = list(curated)
        if not args.no_llm:
            context = {
                "people": prefs.get("people", 2),
                "budget_week_usd": args.budget,
                "dislikes": prefs.get("dislikes", []),
                "avoid_whole_tomatoes": prefs.get("avoid_whole_tomatoes", True),
                "appliances": prefs.get("appliances", []),
                "effort": prefs.get("effort", "low"),
                "avoid_ids": list(exclude_ids | {r["id"] for r in curated}),
                "avoid_names": curated_names,
            }
            try:
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
                accepted: List[Dict] = []
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

            except Exception as e:
                print(f"⚠ LLM disabled this run: {e}")

        # Plan with ratings bias + LLM quota
        plan = plan_week(
            recipes=all_recipes,
            prefs=prefs,
            budget=args.budget,
            exclude_recipe_ids=exclude_ids,
            ratings_avg=ratings_avg,
            ratings_count=ratings_count,
            fallback_on_shortage=True,
            min_llm=max(0, int(args.min_llm)),
            seed=args.seed,
            rating_weight=float(args.rating_weight),
        )

        rec_map = {r["id"]: r for r in all_recipes}
        plan_txt = outdir / "mealplan.txt"
        with plan_txt.open("w") as f:
            f.write(f"Estimated weekly cost: ${plan['total_cost_usd']:.2f}\n")
            f.write(f"Weekly protein: {plan['protein_g_total']:.0f} g | Weekly fiber: {plan['fiber_g_total']:.0f} g\n")
            if not args.no_llm:
                f.write(f"(Info: LLM candidates raw={llm_raw}, normalized={llm_norm}, accepted={llm_kept}; "
                        f"rejected schema={reject_schema}, guardrails={reject_guard}, recent-ban={reject_recentban})\n")
            if args.block_low_rated_weeks > 0:
                f.write(f"(Low-rated blocker: hiding ≤2★ from last {args.block_low_rated_weeks} weeks.)\n")
            f.write("\n")
            for item in plan["items"]:
                r = rec_map[item["recipe_id"]]
                src = r.get("source", "curated")
                label = "LLM" if str(src) == "llm" else "curated"
                f.write(
                    f"{item['day']}: {r['name']} [{label}] ({r['minutes']} min, {r['method']})  "
                    f"[Protein {r['macros']['protein_g']} g | Fiber {r['macros']['fiber_g']} g | ${r['cost_usd']:.2f}]\n"
                )

        # Groceries CSV
        chosen = [rec_map[i["recipe_id"]] for i in plan["items"]]
        need = aggregate_ingredients(chosen)
        with (outdir / "shopping_list.csv").open("w") as f:
            f.write("item,qty\n")
            for k, v in sorted(need.items()):
                f.write(f"{k},{v}\n")

        # --------- NEW: write recipe Markdown files (generate if missing) ----------
        recipes_dir = outdir / "recipes"
        recipes_dir.mkdir(parents=True, exist_ok=True)
        cookbook_parts = []
        newly_instructed = []

        for r in chosen:
            md = r.get("instructions_md")
            if not md:
                try:
                    md = llmmod.generate_instructions_md(r, prefs)
                    r["instructions_md"] = md
                    newly_instructed.append(r["id"])
                except Exception as e:
                    # Fallback minimal scaffold so you’re not blocked
                    md = f"# {r['name']}\n\n_Time_: ~{r.get('minutes', 30)} min  \n_Method_: {r.get('method','stovetop')}\n\n## Ingredients\n" + \
                         "\n".join([f"- {i.get('qty','')} {i.get('item','')}".strip() for i in r.get('ingredients', [])]) + \
                         "\n\n## Steps\n1. Prep ingredients.\n2. Cook using listed method.\n3. Season to taste.\n"

                    r["instructions_md"] = md

            # write individual file
            path = recipes_dir / f"{r['id']}.md"
            path.write_text(md)
            # collect for cookbook
            cookbook_parts.append(md.strip() + "\n")

        # persist any new instructions into DB so we don’t regen next time
        if newly_instructed and conn:
            dbmod.upsert_recipes(conn, chosen)

        # write a combined booklet
        (recipes_dir / "COOKBOOK.md").write_text("\n\n---\n\n".join(cookbook_parts))

        print(f"✓ Plan written to {plan_txt}")
        print(f"✓ Shopping list written to {outdir/'shopping_list.csv'}")
        print(f"✓ Recipes written to {recipes_dir} (including COOKBOOK.md)")

        # Save plan
        if conn:
            week_start = _parse_week_start(args.week_start)
            day_index_map = {d: i for i, d in enumerate(DAYS)}
            items_for_db = [{"day_index": day_index_map[i["day"]], "recipe_id": i["recipe_id"]} for i in plan["items"]]
            totals = {
                "total_cost_usd": plan["total_cost_usd"],
                "protein_g_total": plan["protein_g_total"],
                "fiber_g_total": plan["fiber_g_total"],
            }
            plan_id = dbmod.save_plan(
                conn=conn,
                week_start=week_start,
                people=prefs.get("people", 2),
                budget_usd=float(args.budget),
                items=items_for_db,
                totals=totals,
            )
            print(f"✓ Saved plan to DB: {args.db} (plan_id={plan_id})")

    elif args.cmd == "rate":
        cooked_on = _parse_date(args.date)
        conn = dbmod.ensure_db(str(args.db))
        try:
            dbmod.add_rating(conn, recipe_id=args.recipe_id, cooked_on=cooked_on, rating=int(args.rating), comments=args.comments)
        except ValueError as e:
            sys.exit(f"Error: {e}")
        print(f"✓ Rated {args.recipe_id} = {args.rating} on {cooked_on.isoformat()}")

    elif args.cmd == "history":
        conn = dbmod.ensure_db(str(args.db))
        plans = dbmod.recent_plans(conn, weeks=int(args.weeks))
        if not plans:
            print("No plans found.")
            return
        for p in plans:
            print(
                f"Week {p['week_start']} | cost ${p['total_cost_usd']:.2f} | "
                f"protein {p['total_protein_g']:.0f}g | fiber {p['total_fiber_g']:.0f}g"
            )
            for it in p["items"]:
                day = DAYS[it["day_index"]]
                name = it["name"] or it["recipe_id"]
                print(f"  {day}: {name}")
            print("")
