"""Módulo de podcasts: búsqueda iTunes, parseo RSS, persistencia, descargas. Sin GTK."""
import os
import re
import json
import hashlib
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

SUBS_DIR = os.path.expanduser("~/.local/share/tunepod")
SUBS_FILE = os.path.join(SUBS_DIR, "podcasts.json")
ART_CACHE = os.path.expanduser("~/.cache/tunepod/podcast_art")
os.makedirs(SUBS_DIR, exist_ok=True)
os.makedirs(ART_CACHE, exist_ok=True)

ITUNES_API = "https://itunes.apple.com/search"
NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}

AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".ogg", ".mp4", ".m4b", ".wav", ".flac")


def _http_get(url, timeout=20, want_bytes=False):
    req = urllib.request.Request(url, headers={"User-Agent": "TunePod/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return data if want_bytes else data.decode("utf-8", "replace")


def search_itunes(query, limit=30):
    params = {"media": "podcast", "term": query, "limit": str(limit)}
    url = ITUNES_API + "?" + urllib.parse.urlencode(params)
    try:
        raw = _http_get(url, timeout=15)
        data = json.loads(raw)
    except Exception as e:
        return [], f"Error consultando iTunes: {e}"
    results = []
    for r in data.get("results", []):
        feed = r.get("feedUrl")
        if not feed:
            continue
        results.append({
            "title": r.get("collectionName", "?"),
            "author": r.get("artistName", "?"),
            "feed_url": feed,
            "artwork_url": r.get("artworkUrl600") or r.get("artworkUrl100", ""),
            "genre": r.get("primaryGenreName", ""),
        })
    return results, None


def download_artwork(url):
    if not url:
        return None
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()
    parsed = urllib.parse.urlparse(url)
    ext = os.path.splitext(parsed.path)[1] or ".jpg"
    path = os.path.join(ART_CACHE, h + ext)
    if os.path.exists(path):
        return path
    try:
        data = _http_get(url, timeout=15, want_bytes=True)
        with open(path, "wb") as f:
            f.write(data)
        return path
    except Exception:
        return None


def load_subscriptions():
    if not os.path.exists(SUBS_FILE):
        return []
    try:
        with open(SUBS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        subs = data.get("podcasts", [])
        return subs if isinstance(subs, list) else []
    except Exception as e:
        print(f"[podcasts] Error cargando {SUBS_FILE}: {e}")
        return []


def save_subscriptions(subs):
    try:
        tmp = SUBS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "podcasts": subs}, f, ensure_ascii=False)
        os.replace(tmp, SUBS_FILE)
    except Exception as e:
        print(f"[podcasts] Error guardando {SUBS_FILE}: {e}")


def _text(elem, tag, default=""):
    if elem is None:
        return default
    el = elem.find(tag, NS)
    if el is not None and el.text:
        return el.text.strip()
    return default


def _fmt_duration(s):
    if not s:
        return ""
    s = s.strip()
    if ":" in s:
        return s
    try:
        total = int(float(s))
        h, m, sec = total // 3600, (total % 3600) // 60, total % 60
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
    except ValueError:
        return s


def _fmt_date(s):
    if not s:
        return ""
    try:
        return parsedate_to_datetime(s).strftime("%Y-%m-%d")
    except Exception:
        return s[:10]


def fetch_feed(feed_url):
    try:
        raw = _http_get(feed_url, timeout=25, want_bytes=True)
        root = ET.fromstring(raw)
    except urllib.error.URLError as e:
        return None, f"Sin conexión al feed: {e.reason}"
    except ET.ParseError as e:
        return None, f"XML mal formado: {e}"
    except Exception as e:
        return None, f"Error: {e}"

    channel = root.find("channel")
    if channel is None:
        return None, "El feed no es RSS 2.0 (no tiene <channel>)"

    title = _text(channel, "title") or "(Sin título)"
    author = _text(channel, "itunes:author") or title
    desc = _text(channel, "description")[:400]

    image_url = ""
    img_el = channel.find("itunes:image", NS)
    if img_el is not None:
        image_url = img_el.get("href", "")
    if not image_url:
        img_el = channel.find("image/url")
        if img_el is not None and img_el.text:
            image_url = img_el.text.strip()

    episodes = []
    for item in channel.findall("item"):
        enc = item.find("enclosure")
        audio_url = enc.get("url", "") if enc is not None else ""
        if not audio_url:
            continue
        episodes.append({
            "title": _text(item, "title") or "(Sin título)",
            "pub_date": _fmt_date(_text(item, "pubDate")),
            "duration": _fmt_duration(_text(item, "itunes:duration")),
            "guid": _text(item, "guid"),
            "description": _text(item, "description")[:400],
            "audio_url": audio_url,
        })

    return {
        "title": title,
        "author": author,
        "description": desc,
        "image_url": image_url,
        "feed_url": feed_url,
        "episodes": episodes,
    }, None


# ---------- Descargas ----------

_BAD_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(s, maxlen=120):
    """Limpia un string para usarlo como nombre de archivo/carpeta."""
    s = _BAD_CHARS_RE.sub("_", s or "sin-titulo")
    s = s.strip().strip(".")
    if not s:
        s = "sin-titulo"
    if len(s) > maxlen:
        s = s[:maxlen].rstrip()
    return s


def guess_ext_from_url(url):
    path = urllib.parse.urlparse(url).path
    ext = os.path.splitext(path)[1].lower()
    return ext if ext in AUDIO_EXTS else None


def _ext_from_content_type(ct):
    ct = (ct or "").lower()
    if "mpeg" in ct or "mp3" in ct:
        return ".mp3"
    if "mp4" in ct or "m4a" in ct or "aac" in ct:
        return ".m4a"
    if "ogg" in ct:
        return ".ogg"
    if "flac" in ct:
        return ".flac"
    if "wav" in ct:
        return ".wav"
    return ".mp3"


def download_episode(url, base_dir, episode_title, on_progress=None,
                     cancel_event=None, timeout=30):
    """Descarga url a base_dir/<title>.<ext>.
    Devuelve (path_final, None) o (None, mensaje_error).
    on_progress(bytes_descargados, total_o_None) se llama desde el hilo."""
    os.makedirs(base_dir, exist_ok=True)
    safe = sanitize_filename(episode_title)
    ext = guess_ext_from_url(url)

    req = urllib.request.Request(url, headers={"User-Agent": "TunePod/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if ext is None:
                ext = _ext_from_content_type(resp.headers.get("Content-Type"))
            dest = os.path.join(base_dir, safe + ext)
            partial = dest + ".part"

            total_header = resp.headers.get("Content-Length")
            total = int(total_header) if total_header else None
            downloaded = 0
            chunk_size = 64 * 1024
            try:
                with open(partial, "wb") as f:
                    while True:
                        if cancel_event is not None and cancel_event.is_set():
                            raise RuntimeError("Descarga cancelada")
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if on_progress is not None:
                            on_progress(downloaded, total)
                os.replace(partial, dest)
            except Exception:
                try:
                    if os.path.exists(partial):
                        os.remove(partial)
                except OSError:
                    pass
                raise
            return dest, None
    except urllib.error.URLError as e:
        return None, f"Sin conexión: {e.reason}"
    except Exception as e:
        return None, f"Error: {e}"
