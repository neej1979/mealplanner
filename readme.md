🥘 MealPlanner

Local AI meal planner with weekly recipes, grocery lists, and ratings.

Generate balanced, high-protein, high-fiber meal plans under budget, complete with ready-to-cook recipes, shopping lists, and automatic history tracking. Works entirely on your machine with optional OpenAI API integration for fresh weekly ideas.

✨ Features

One-click planning: Generates a full week of dinners within your budget.

LLM recipe generation: Uses GPT-4o-mini to create new meals when your curated list runs out.

Full outputs:

mealplan.txt – readable weekly overview

shopping_list.csv – consolidated grocery list

recipes/COOKBOOK.md – printable recipe book

Ratings memory: Rate what you cooked; low-rated dishes automatically fade from future plans.

SQLite persistence: All plans, ratings, and recipes are stored locally (mealplanner.db).

Streamlit GUI: No more flags or terminal work — generate, review, and rate from your browser.

🧠 Quick Start
1. Clone & install
git clone https://github.com/<your-username>/mealplanner.git
cd mealplanner
pip install -e .
pip install streamlit openai

2. Configure API key

Either export it:

export OPENAI_API_KEY="sk-your-key"


Or create ~/.config/mealplanner/config.json:

{
  "openai_api_key": "sk-your-key",
  "model": "gpt-4o-mini"
}

3. Run the GUI
streamlit run gui_app.py


The app opens at http://localhost:8501
.
Click Generate Plan to build the week, or switch to the Rate Last Week tab to score your past dinners.

📂 Output structure
out/
 ├─ mealplan.txt
 ├─ shopping_list.csv
 └─ recipes/
     ├─ COOKBOOK.md
     ├─ chicken-tikka-bowls.md
     ├─ beef-stir-fry.md
     └─ ...

🗂 Database

The planner creates a simple SQLite database (mealplanner.db) containing:

Table	Purpose
recipes	All curated + AI recipes, with instructions
plans	Each weekly plan summary
plan_items	Day-by-day recipe assignments
ratings	User feedback (1–5 stars)
⚙️ Roadmap

Next:

✅ Ratings bias

✅ Recipe Markdown booklet

🔜 Pantry tracker (subtracts items you already own)

🔜 Grocery-store adapters (Wegmans, Whole Foods, Publix, Aldi)

🔜 Per-user dislikes and preferences (e.g. Rachel ≠ ground turkey)

🔜 Auto-balance to budget

Longer-term:

Nutrition sanity checks

PDF export

“Explore Mode” for novelty recipes

Packaged desktop app

🧾 License

MIT — free to fork, modify, and ruin your macros however you please.