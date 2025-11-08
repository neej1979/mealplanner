from typing import Dict, List, Tuple

REQUIRED_RECIPE_KEYS = {
    "id", "name", "tags", "method", "minutes", "ingredients", "macros", "cost_usd"
}
REQUIRED_MACRO_KEYS = {"protein_g", "fiber_g", "kcals"}

BANNED_TOKENS = {"shellfish", "raw onion", "kale"}
MAX_MEAL_COST = 18.0  # hard cap per dinner for two

def validate_recipe_schema(recipe: Dict) -> Tuple[bool, List[str]]:
    errors: List[str] = []
    missing = REQUIRED_RECIPE_KEYS - set(recipe.keys())
    if missing:
        errors.append(f"missing keys: {sorted(missing)}")

    macros = recipe.get("macros", {})
    missing_macros = REQUIRED_MACRO_KEYS - set(macros.keys()) if isinstance(macros, dict) else REQUIRED_MACRO_KEYS
    if missing_macros:
        errors.append(f"macros missing keys: {sorted(missing_macros)}")

    if not isinstance(recipe.get("ingredients"), list):
        errors.append("ingredients must be a list")

    if not isinstance(recipe.get("tags"), list):
        errors.append("tags must be a list")

    return (len(errors) == 0, errors)

def hard_guardrails(recipe: Dict, avoid_whole_tomatoes: bool = True) -> bool:
    name = recipe.get("name","").lower()
    tags = " ".join(recipe.get("tags", [])).lower()
    if any(tok in name or tok in tags for tok in BANNED_TOKENS):
        return False
    if "very_spicy" in tags or "extra spicy" in name:
        return False
    if avoid_whole_tomatoes and "whole_tomatoes" in tags:
        return False
    mac = recipe.get("macros", {})
    if mac.get("protein_g", 0) < 40 or mac.get("fiber_g", 0) < 6:
        return False
    if float(recipe.get("cost_usd", 0)) > MAX_MEAL_COST:
        return False
    return True

def normalize_llm_candidate(c: Dict) -> Dict:
    """
    Ensure keys and types match our internal recipe schema.
    Tag it as LLM-sourced and drop weird fields.
    """
    def _float(x, default=0.0):
        try:
            return float(x)
        except Exception:
            return default

    macros = c.get("macros", {})
    out = {
        "id": str(c.get("id")),
        "name": str(c.get("name")),
        "tags": list(c.get("tags", [])),
        "method": str(c.get("method", "stovetop")),
        "minutes": int(c.get("minutes", 0)),
        "ingredients": list(c.get("ingredients", [])),
        "macros": {
            "protein_g": _float(macros.get("protein_g", 0)),
            "fiber_g": _float(macros.get("fiber_g", 0)),
            "kcals": _float(macros.get("kcals", 0)),
        },
        "cost_usd": _float(c.get("cost_usd", 0)),
        "source": "llm",
    }
    return out
