"""Preferencias del reproductor: volumen, shuffle, repeat."""
import os
import json

PREFS_DIR = os.path.expanduser("~/.local/share/tunepod")
PREFS_FILE = os.path.join(PREFS_DIR, "preferences.json")

DEFAULTS = {
    "volume": 1.0,
    "shuffle": False,
    "repeat": "off",  # "off" | "all" | "one"
    "auto_sync": False,
}


def load():
    prefs = dict(DEFAULTS)
    if os.path.exists(PREFS_FILE):
        try:
            with open(PREFS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k in DEFAULTS:
                if k in data:
                    prefs[k] = data[k]
        except Exception as e:
            print(f"[prefs] Error cargando: {e}")
    return prefs


def save(prefs):
    os.makedirs(PREFS_DIR, exist_ok=True)
    try:
        tmp = PREFS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(prefs, f, ensure_ascii=False, indent=1)
        os.replace(tmp, PREFS_FILE)
    except Exception as e:
        print(f"[prefs] Error guardando: {e}")
