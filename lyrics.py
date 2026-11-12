"""Módulo de obtención de letras. Sin dependencias de GTK."""
import os
import json
import hashlib
import urllib.request
import urllib.parse
import urllib.error
from translations import _, _f

CACHE_DIR = os.path.expanduser("~/.cache/tunepod/lyrics")
os.makedirs(CACHE_DIR, exist_ok=True)


def get_lyrics(artist, title):
    """Devuelve (texto, None) si hay letra, o (None, motivo) si no.
    Esta función es BLOQUEANTE. Llamarla siempre desde un hilo."""
    if not artist or not title:
        return None, _("Sin artista o título para buscar la letra.")

    # 1. Caché
    key = hashlib.sha1(f"{artist}|{title}".encode("utf-8")).hexdigest()
    cache_file = os.path.join(CACHE_DIR, key + ".txt")
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                return f.read(), None
        except Exception:
            pass  # caché corrupta, seguimos a la red

    # 2. Red: lyrics.ovh
    url = (
        "https://api.lyrics.ovh/v1/"
        + urllib.parse.quote(artist, safe="")
        + "/"
        + urllib.parse.quote(title, safe="")
    )
    req = urllib.request.Request(url, headers={"User-Agent": "TunePod/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = (data.get("lyrics") or "").strip()
        if not text:
            return None, _("No se encontró letra.")
        # 3. Guardar caché
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass
        return text, None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, _("No se encontró letra.")
        return None, _f("Error HTTP {} al buscar la letra.", e.code)
    except urllib.error.URLError as e:
        return None, _f("Sin conexión: {}", e.reason)
    except Exception as e:
        return None, _f("Error: {}", e)