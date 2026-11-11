"""Búsqueda de carátulas de álbum en múltiples fuentes públicas.

Fuentes actuales:
  - iTunes Search API (sin clave, JSON, alta cobertura mainstream).
  - Deezer API (sin clave, JSON, buena cobertura europea/latinoamericana).

Para añadir una fuente nueva en el futuro:
  1. Implementar `search_album_covers_<fuente>(artist, album, limit)`
     que devuelva la misma estructura que las existentes.
  2. Añadir la función a `search_all_sources`.
"""
import os
import json
import hashlib
import urllib.request
import urllib.parse
import urllib.error
import gi
gi.require_version("GLib", "2.0")
from gi.repository import GLib
from translations import _, _f

CACHE_DIR = os.path.expanduser("~/.cache/tunepod/covers")
os.makedirs(CACHE_DIR, exist_ok=True)


def _cache_path_for_uri(uri):
    """Calcula la ruta en caché para una URI. Usa la ruta del archivo
    (sin file://) como clave, igual que player.py al leer la caché."""
    try:
        fname = GLib.filename_from_uri(uri)[0]
    except Exception:
        fname = uri
    h = hashlib.sha1(fname.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, h + ".jpg")

ITUNES_SEARCH = "https://itunes.apple.com/search"
DEEZER_SEARCH = "https://api.deezer.com/search/album"
USER_AGENT = "TunePod/0.1"


# ---------- iTunes ----------

def search_album_covers_itunes(artist, album, limit=10):
    if not artist or not album:
        return [], _("Faltan artista o álbum.")
    term = f"{artist} {album}".strip()
    params = {
        "term": term,
        "entity": "album",
        "limit": str(limit),
        "media": "music",
    }
    url = ITUNES_SEARCH + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        return [], _f("iTunes sin conexión: {}", e.reason)
    except Exception as e:
        return [], _f("iTunes error: {}", e)

    results = []
    for r in data.get("results", []):
        art = r.get("artworkUrl100") or r.get("artworkUrl60")
        if not art:
            continue
        art_big = art.replace("100x100bb", "600x600bb").replace(
            "60x60bb", "600x600bb")
        results.append({
            "title": r.get("collectionName", ""),
            "artist": r.get("artistName", ""),
            "artwork_url": art_big,
            "source": "iTunes",
        })
    return results, None


# ---------- Deezer ----------

def search_album_covers_deezer(artist, album, limit=10):
    if not artist or not album:
        return [], _("Faltan artista o álbum.")
    q = f"{artist} {album}".strip()
    params = {"q": q, "limit": str(limit)}
    url = DEEZER_SEARCH + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        return [], _f("Deezer sin conexión: {}", e.reason)
    except Exception as e:
        return [], _f("Deezer error: {}", e)

    results = []
    for r in data.get("data", []):
        # Deezer tiene cover_small, cover_medium, cover_big, cover_xl
        cover = r.get("cover_xl") or r.get("cover_big") or r.get("cover_medium")
        if not cover:
            continue
        results.append({
            "title": r.get("title", ""),
            "artist": (r.get("artist") or {}).get("name", ""),
            "artwork_url": cover,
            "source": "Deezer",
        })
    return results, None


# ---------- Combinador ----------

def search_all_sources(artist, album, limit=10):
    """Consulta todas las fuentes disponibles y combina los resultados.
    Devuelve (lista, dict_de_errores). dict_de_errores puede estar vacío
    si al menos una fuente respondió sin fallar."""
    all_results = []
    errors = {}

    r_itunes, err_itunes = search_album_covers_itunes(artist, album, limit)
    if err_itunes:
        errors["iTunes"] = err_itunes
    else:
        all_results.extend(r_itunes)

    r_deezer, err_deezer = search_album_covers_deezer(artist, album, limit)
    if err_deezer:
        errors["Deezer"] = err_deezer
    else:
        all_results.extend(r_deezer)

    return all_results, errors


# ---------- Descarga y caché ----------

def download_cover(artwork_url, cache_key, timeout=15, overwrite=False):
    """Descarga la carátula y la guarda en caché.
    cache_key puede ser una URI file:// o una ruta; se normaliza igual
    que player.py para que la caché se lea correctamente después.
    Si overwrite=True, sobreescribe aunque ya exista una carátula."""
    if not artwork_url:
        return None, _("URL vacía.")

    cache_path = _cache_path_for_uri(cache_key)

    if not overwrite and os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
        return cache_path, None

    try:
        req = urllib.request.Request(
            artwork_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        with open(cache_path, "wb") as f:
            f.write(data)
        return cache_path, None
    except urllib.error.URLError as e:
        return None, _f("Sin conexión: {}", e.reason)
    except Exception as e:
        return None, _f("Error descargando: {}", e)


def copy_local_cover(source_path, cache_key, overwrite=False):
    """Copia una imagen local a la caché, asociándola a la canción.
    cache_key puede ser una URI file:// o una ruta.
    Si overwrite=True, sobreescribe aunque ya exista una carátula."""
    if not os.path.exists(source_path):
        return None, _("El archivo no existe.")

    cache_path = _cache_path_for_uri(cache_key)

    if not overwrite and os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
        return cache_path, None

    try:
        with open(source_path, "rb") as f:
            data = f.read()
        with open(cache_path, "wb") as f:
            f.write(data)
        return cache_path, None
    except Exception as e:
        return None, _f("Error copiando: {}", e)
