"""Persistencia de la biblioteca local en JSON. Sin dependencias de GTK."""
import os
import json

LIBRARY_DIR = os.path.expanduser("~/.local/share/tunepod")
LIBRARY_FILE = os.path.join(LIBRARY_DIR, "library.json")


def load():
    """Devuelve la lista de pistas guardadas (o [] si no hay o está corrupto)."""
    if not os.path.exists(LIBRARY_FILE):
        return []
    try:
        with open(LIBRARY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        tracks = data.get("tracks", [])
        if not isinstance(tracks, list):
            return []
        return tracks
    except Exception as e:
        print(f"[library] Error cargando {LIBRARY_FILE}: {e}")
        return []


def save(tracks):
    """Guarda la lista de pistas. Silencioso si falla (loguea)."""
    os.makedirs(LIBRARY_DIR, exist_ok=True)
    try:
        tmp = LIBRARY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 2, "tracks": tracks}, f, ensure_ascii=False)
        os.replace(tmp, LIBRARY_FILE)
    except Exception as e:
        print(f"[library] Error guardando {LIBRARY_FILE}: {e}")
