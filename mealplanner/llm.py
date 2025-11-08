from __future__ import annotations
import json
from typing import Dict, List
from .config import get_openai_key, get_llm_model, get_provider

LLM_MODEL = get_llm_model() or "gpt-4o-mini"
LLM_PROVIDER = get_provider()

PROMPT_SYSTEM = """You are MealAgent Planner. Output JSON ONLY. No prose.

Goal: propose dinner recipes for 2 adults that are high-protein, high-fiber, low effort, total week budget ≈ $100.

HARD DIET RULES:
- Exclude: shellfish, raw onion, kale, and very spicy dishes.
- Tomatoes allowed only if blended/passata; no chunks.
- Use common US grocery items.
- Suitable for weeknights: prefer 20–35 minutes total time.

HARD NUTRITION/COST BOUNDS (per recipe):
- macros.protein_g >= 40
- macros.fiber_g >= 6
- macros.kcals between 450 and 850
- cost_usd <= 18.0

METHODS: one of [stovetop, oven, grill, air_fryer, sheetpan, onepot, smoker]
Keep methods varied across the set.

UNIQUENESS:
- Provide exactly 12 unique candidates.
- Each candidate must have a unique 'id' slug in kebab-case (letters, digits, hyphens), max 40 chars.

OUTPUT JSON EXACTLY IN THIS SHAPE:
{
  "candidates": [
    {
      "id": "kebab-case-unique-id",
      "name": "Readable Dish Name",
      "minutes": 20,
      "method": "stovetop",
      "tags": ["mild","blend_tomatoes_ok"],
      "macros": {"protein_g": 45, "fiber_g": 8, "kcals": 650},
      "cost_usd": 11.50,
      "ingredients": [
        {"item": "boneless skinless chicken thighs", "qty": "600 g"},
        {"item": "frozen peas", "qty": "1 cup"}
      ]
    }
  ]
}
Return only the JSON object, no commentary.
"""

PROMPT_USER_TEMPLATE = """Preferences:
people: {people}
budget_week_usd: {budget}
dislikes: {dislikes}
avoid_whole_tomatoes: {avoid_whole_tomatoes}
appliances: {appliances}
effort: {effort}

Avoid these recent recipe IDs: {avoid_ids}
Avoid these known dishes by name (suggest something else): {avoid_names}

Target a mix across cuisines and methods (e.g., Italian, Indian, Tex-Mex, Japanese-inspired, Mediterranean).
Return 12 diverse dinner candidates that honor all hard rules and bounds.
"""

def _openai_chat_json(messages: List[Dict]) -> str:
    api_key = get_openai_key()
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY. Set it in your shell or ~/.config/mealplanner/config.json")
    try:
        from openai import OpenAI
    except Exception as e:
        raise RuntimeError("openai package not installed. pip install openai") from e
    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        temperature=0.6,
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content

def _openai_chat_text(messages: List[Dict]) -> str:
    api_key = get_openai_key()
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY. Set it in your shell or ~/.config/mealplanner/config.json")
    try:
        from openai import OpenAI
    except Exception as e:
        raise RuntimeError("openai package not installed. pip install openai") from e
    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        temperature=0.5,
    )
    return resp.choices[0].message.content

def propose_candidates(context: Dict) -> List[Dict]:
    if get_provider() != "openai":
        raise RuntimeError(f"Unsupported LLM provider: {get_provider()}")

    user_prompt = PROMPT_USER_TEMPLATE.format(
        people=context.get("people", 2),
        budget=context.get("budget_week_usd", 100),
        dislikes=", ".join(context.get("dislikes", [])),
        avoid_whole_tomatoes=context.get("avoid_whole_tomatoes", True),
        appliances=", ".join(context.get("appliances", [])),
        effort=context.get("effort", "low"),
        avoid_ids=", ".join(sorted(context.get("avoid_ids", []))) or "[]",
        avoid_names=", ".join(sorted(context.get("avoid_names", []))) or "[]",
    )

    raw = _openai_chat_json([
        {"role": "system", "content": PROMPT_SYSTEM},
        {"role": "user", "content": user_prompt},
    ])
    data = json.loads(raw)
    cands = data.get("candidates", [])
    if not isinstance(cands, list) or not cands:
        raise RuntimeError("LLM returned no candidates")
    return cands

# ---------- Instructions generation ----------

INSTR_SYSTEM = """You write concise, fail-safe cooking instructions as Markdown.
Constraints:
- 2 servings, weeknight-friendly.
- Respect: no shellfish, no raw onion, no kale, avoid very spicy, tomatoes only blended.
- Include: title, time summary, equipment, ingredients (with given quantities), numbered steps with timers and doneness cues, serving suggestions, optional swaps.
- Use Fahrenheit where relevant, include safe temps (e.g., chicken 165°F, pork 145°F).
- Keep it ~12–18 lines total. No chit-chat, just the recipe.
"""

INSTR_USER_TEMPLATE = """Dish: {name}
Method: {method}
Total minutes: {minutes}
Ingredients:
{ingredients}

Notes:
- Prioritize high protein and decent fiber.
- Keep heat level mild.
"""

def generate_instructions_md(recipe: Dict, prefs: Dict) -> str:
    # Build an ingredients bullet list with quantities
    ing_lines = []
    for ing in recipe.get("ingredients", []):
        item = str(ing.get("item", "")).strip()
        qty = str(ing.get("qty", "")).strip()
        if not item:
            continue
        if qty:
            ing_lines.append(f"- {qty} {item}")
        else:
            ing_lines.append(f"- {item}")
    ingredients_block = "\n".join(ing_lines) if ing_lines else "- See ingredients list above"

    user = INSTR_USER_TEMPLATE.format(
        name=recipe.get("name", "Recipe"),
        method=recipe.get("method", "stovetop"),
        minutes=recipe.get("minutes", 30),
        ingredients=ingredients_block,
    )

    md = _openai_chat_text([
        {"role": "system", "content": INSTR_SYSTEM},
        {"role": "user", "content": user},
    ])
    return md.strip()
