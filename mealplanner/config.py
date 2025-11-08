from __future__ import annotations
import json, os
from pathlib import Path
from typing import Dict, Optional

def _config_paths():
    # XDG default, then legacy fallback
    xdg = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return [
        Path(xdg) / "mealplanner" / "config.json",
        Path.home() / ".mealplanner" / "config.json",
    ]

def load_config() -> Dict:
    for p in _config_paths():
        try:
            if p.is_file():
                return json.loads(p.read_text())
        except Exception:
            # ignore bad files; fall through to empty
            pass
    return {}

def get_openai_key() -> Optional[str]:
    # Precedence: env > config file
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    cfg = load_config()
    return cfg.get("openai_api_key")

def get_llm_model() -> Optional[str]:
    # env wins; else config
    return os.environ.get("MEALPLANNER_LLM_MODEL") or load_config().get("model")

def get_provider() -> str:
    return os.environ.get("MEALPLANNER_LLM_PROVIDER", "openai")
