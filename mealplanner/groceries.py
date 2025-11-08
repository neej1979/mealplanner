from collections import defaultdict

def aggregate_ingredients(recipes):
    """
    Combine ingredient lists across recipes.  If qty is numeric, sum it.
    Otherwise, just concatenate the text (e.g. "1 can + 1 cup").
    """
    need = defaultdict(str)
    for r in recipes:
        for ing in r.get("ingredients", []):
            item = str(ing.get("item", "")).strip()
            qty = str(ing.get("qty", "")).strip()
            if not item:
                continue
            prev = need.get(item)
            # Try to add numeric values if possible
            try:
                val = float(qty)
                if prev:
                    try:
                        val += float(prev)
                        need[item] = str(val)
                    except ValueError:
                        need[item] = f"{prev} + {val}"
                else:
                    need[item] = str(val)
            except ValueError:
                # non-numeric quantity, just append nicely
                if prev:
                    need[item] = f"{prev} + {qty}"
                else:
                    need[item] = qty
    return need
