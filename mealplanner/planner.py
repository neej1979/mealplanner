from typing import List, Dict, Optional, Set
import random

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

BANNED_TOKENS = {"shellfish", "raw onion", "kale"}
MAX_MEAL_COST = 18.0  # hard cap per dinner for two

PROTEIN_KEYWORDS = {
    "chicken": "chicken",
    "beef": "beef",
    "salmon": "fish",
    "fish": "fish",
    "egg": "eggs",
    "eggs": "eggs",
    "turkey": "turkey",
    "pork": "pork",
    "tofu": "veg",
    "bean": "veg",
    "lentil": "veg",
}

def _protein_group(recipe_name: str, ingredients: List[Dict]) -> str:
    name = recipe_name.lower()
    for kw, grp in PROTEIN_KEYWORDS.items():
        if kw in name:
            return grp
    for ing in ingredients:
        item = str(ing.get("item", "")).lower()
        for kw, grp in PROTEIN_KEYWORDS.items():
            if kw in item:
                return grp
    return "other"

def _ok(recipe: Dict, prefs: Dict) -> bool:
    name = recipe["name"].lower()
    for token in BANNED_TOKENS:
        if token in name:
            return False
        if any(token in t for t in recipe.get("tags", [])):
            return False
    if prefs.get("avoid_whole_tomatoes", True) and "whole_tomatoes" in recipe.get("tags", []):
        return False
    if "very_spicy" in recipe.get("tags", []):
        return False
    mac = recipe["macros"]
    if mac["protein_g"] < 40 or mac["fiber_g"] < 6:
        return False
    if recipe["cost_usd"] > MAX_MEAL_COST:
        return False
    return True

def _score(recipe: Dict,
           ratings_avg: Dict[str, float],
           ratings_count: Dict[str, int],
           used_methods: Set[str],
           used_groups: Set[str],
           rng: random.Random,
           rating_weight: float) -> float:
    mac = recipe["macros"]
    protein = float(mac.get("protein_g", 0))
    fiber = float(mac.get("fiber_g", 0))
    cost = float(recipe.get("cost_usd", 0))

    base = 0.30 * protein + 0.15 * fiber - 0.25 * cost

    # Ratings bias: map avg 1..5 -> roughly -1.5 .. +1.5 then scale by rating_weight
    rid = recipe["id"]
    avg = ratings_avg.get(rid)
    n = ratings_count.get(rid, 0)
    # Cold-start dampener: if n==0, no bias. If n==1, halve the effect.
    strength = 1.0 if n >= 2 else 0.5 if n == 1 else 0.0
    bias = ((avg - 3.0) * 0.75 * strength if avg is not None else 0.0) * rating_weight

    # Diversity bonuses
    method_bonus = 0.15 if recipe.get("method") not in used_methods else 0.0
    group = _protein_group(recipe["name"], recipe.get("ingredients", []))
    group_bonus = 0.15 if group not in used_groups else 0.0

    # Small novelty nudge for LLM picks
    novelty = 0.10 if str(recipe.get("source", "curated")) == "llm" else 0.0

    # Seeded jitter to break ties deterministically per run
    jitter = rng.uniform(-0.02, 0.02)

    return base + bias + method_bonus + group_bonus + novelty + jitter

def _greedy_pick(pool: List[Dict], k: int,
                 ratings_avg: Dict[str, float], ratings_count: Dict[str, int],
                 used_methods: Set[str], used_groups: Set[str],
                 already_ids: Set[str], rng: random.Random, rating_weight: float) -> List[Dict]:
    chosen: List[Dict] = []
    remaining = [r for r in pool if r["id"] not in already_ids]
    while remaining and len(chosen) < k:
        scored = [(r, _score(r, ratings_avg, ratings_count, used_methods, used_groups, rng, rating_weight)) for r in remaining]
        scored.sort(key=lambda t: t[1], reverse=True)
        best = scored[0][0]
        chosen.append(best)
        already_ids.add(best["id"])
        used_methods.add(best.get("method"))
        used_groups.add(_protein_group(best["name"], best.get("ingredients", [])))
        remaining = [r for r in remaining if r["id"] not in already_ids]
    return chosen

def plan_week(
    recipes: List[Dict],
    prefs: Dict,
    budget: float,
    exclude_recipe_ids: Optional[set] = None,
    ratings_map: Optional[Dict[str, float]] = None,   # kept for backward compat (unused)
    ratings_avg: Optional[Dict[str, float]] = None,
    ratings_count: Optional[Dict[str, int]] = None,
    fallback_on_shortage: bool = True,
    min_llm: int = 2,
    seed: Optional[int] = None,
    rating_weight: float = 1.2
) -> Dict:
    """
    Greedy selection with diversity and ratings bias.
    Enforce a minimum number of LLM-sourced recipes (min_llm) if available.
    """
    exclude_recipe_ids = exclude_recipe_ids or set()
    ratings_avg = ratings_avg or {}
    ratings_count = ratings_count or {}
    rng = random.Random(seed)

    def filtered(exclude: bool) -> List[Dict]:
        return [
            r for r in recipes
            if _ok(r, prefs) and (r["id"] not in exclude_recipe_ids if exclude else True)
        ]

    candidates = filtered(exclude=True)
    fallback_used = False
    if len(candidates) < 7 and fallback_on_shortage:
        candidates = filtered(exclude=False)
        fallback_used = True

    llm_pool = [r for r in candidates if str(r.get("source", "curated")) == "llm"]
    curated_pool = [r for r in candidates if str(r.get("source", "curated")) != "llm"]

    used_methods: Set[str] = set()
    used_groups: Set[str] = set()
    chosen: List[Dict] = []
    chosen_ids: Set[str] = set()

    if llm_pool:
        need = min(min_llm, 7, len(llm_pool))
        chosen += _greedy_pick(llm_pool, need, ratings_avg, ratings_count, used_methods, used_groups, chosen_ids, rng, rating_weight)

    remaining_slots = max(0, 7 - len(chosen))
    if remaining_slots > 0:
        combined = llm_pool + curated_pool
        chosen += _greedy_pick(combined, remaining_slots, ratings_avg, ratings_count, used_methods, used_groups, chosen_ids, rng, rating_weight)

    if len(chosen) < 7:
        pad_pool = [r for r in candidates if r["id"] not in chosen_ids]
        pad_pool.sort(key=lambda r: r["cost_usd"])
        for r in pad_pool:
            if len(chosen) < 7:
                chosen.append(r)
                chosen_ids.add(r["id"])

    total_cost = sum(r["cost_usd"] for r in chosen)
    protein_total = sum(r["macros"]["protein_g"] for r in chosen)
    fiber_total = sum(r["macros"]["fiber_g"] for r in chosen)

    if total_cost > budget:
        pool = [r for r in candidates if r["id"] not in chosen_ids]
        pool.sort(key=lambda r: r["cost_usd"])
        for _ in range(12):
            priciest = max(chosen, key=lambda r: r["cost_usd"], default=None)
            if not priciest or not pool:
                break
            cheaper = pool.pop(0)
            new_list = chosen.copy()
            idx = new_list.index(priciest)
            new_list[idx] = cheaper
            new_cost = sum(r["cost_usd"] for r in new_list)
            if new_cost <= budget:
                chosen = new_list
                total_cost = new_cost
                protein_total = sum(r["macros"]["protein_g"] for r in chosen)
                fiber_total = sum(r["macros"]["fiber_g"] for r in chosen)
                break

    items = [{"day": d, "recipe_id": r["id"]} for d, r in zip(DAYS, chosen)]
    return {
        "items": items,
        "total_cost_usd": round(total_cost, 2),
        "protein_g_total": round(protein_total, 1),
        "fiber_g_total": round(fiber_total, 1),
        "meta": {"fallback_used": fallback_used}
    }
