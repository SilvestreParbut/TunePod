"""Gestión de listas: normales (.pls, .m3u) y smart playlists con reglas."""
import os
import json
import uuid
import gi
gi.require_version("GLib", "2.0")
from gi.repository import GLib
from translations import _, _f

PLAYLISTS_DIR = os.path.expanduser("~/.local/share/tunepod")
PLAYLISTS_FILE = os.path.join(PLAYLISTS_DIR, "playlists.json")


def load():
    if not os.path.exists(PLAYLISTS_FILE):
        return []
    try:
        with open(PLAYLISTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        pls = data.get("playlists", [])
        return pls if isinstance(pls, list) else []
    except Exception as e:
        print(f"[playlists] Error cargando: {e}")
        return []


def save(playlists):
    os.makedirs(PLAYLISTS_DIR, exist_ok=True)
    try:
        tmp = PLAYLISTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 2, "playlists": playlists},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, PLAYLISTS_FILE)
    except Exception as e:
        print(f"[playlists] Error guardando: {e}")


def new_playlist(name, uris=None):
    return {"id": str(uuid.uuid4()), "name": name, "uris": uris or []}


def new_smart_playlist(name, rules=None):
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "smart": True,
        "rules": rules or [],
    }


def is_smart(pl):
    return bool(pl.get("smart", False))


# ---------- Parse / import ----------

def to_uri(path_or_uri):
    if not path_or_uri:
        return path_or_uri
    if path_or_uri.startswith("file://") or "://" in path_or_uri:
        return path_or_uri
    return GLib.filename_to_uri(os.path.abspath(path_or_uri), None)


def parse_pls(path):
    uris = []
    title = None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith(";") or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip().lower()
            val = val.strip()
            if not val:
                continue
            if key.startswith("file"):
                uris.append(val)
            elif key in ("x-gnome-title", "title") and title is None:
                title = val
    return title, uris


def parse_m3u(path):
    uris = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            uris.append(line)
    return None, uris


def import_playlist(path):
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pls":
            title, uris = parse_pls(path)
        elif ext in (".m3u", ".m3u8"):
            title, uris = parse_m3u(path)
        else:
            return None, None, _f("Formato no soportado: {}", ext)
    except Exception as e:
        return None, None, _f("Error leyendo {}: {}", os.path.basename(path), e)
    if not title:
        title = os.path.splitext(os.path.basename(path))[0]
    return title, uris, None


# ---------- Evaluación de smart playlists ----------

SMART_FIELDS = {
    "rating":       {"label": _("Rating"),            "type": "int",      "col": 7},
    "play_count":   {"label": _("Reproducciones"),    "type": "int",      "col": 8},
    "last_played":  {"label": _("Última vez"),        "type": "date",     "col": 9},
    "artist":       {"label": _("Artista"),           "type": "text",     "col": 1},
    "genre":        {"label": _("Género"),            "type": "text",     "col": 12},
    "year":         {"label": _("Año"),               "type": "int",      "col": 11},
}

SMART_OPERATORS_INT = [
    ("eq", "="),
    ("ne", "≠"),
    ("lt", "<"),
    ("le", "≤"),
    ("gt", ">"),
    ("ge", "≥"),
]
SMART_OPERATORS_TEXT = [
    ("contains", _("contiene")),
    ("not_contains", _("no contiene")),
    ("eq", _("es igual a")),
    ("ne", _("no es")),
    ("startswith", _("empieza por")),
]
SMART_OPERATORS_DATE = [
    ("days_ago_lt", _("hace menos de N días")),
    ("days_ago_gt", _("hace más de N días")),
    ("is_empty", _("no tiene fecha")),
    ("not_empty", _("tiene fecha")),
]


def rule_matches(row, rule):
    """Evalúa una regla contra una fila del store (15 columnas)."""
    field = rule.get("field", "")
    op = rule.get("op", "")
    val = rule.get("value", "")
    info = SMART_FIELDS.get(field)
    if info is None:
        return False
    cell = row[info["col"]]
    t = info["type"]

    if t == "int":
        try:
            cv = int(cell)
            vv = int(val)
        except (ValueError, TypeError):
            return False
        return {
            "eq": cv == vv, "ne": cv != vv,
            "lt": cv < vv, "le": cv <= vv,
            "gt": cv > vv, "ge": cv >= vv,
        }.get(op, False)

    if t == "text":
        cs = (cell or "").lower()
        vs = (val or "").lower()
        if op == "contains":
            return vs in cs
        if op == "not_contains":
            return vs not in cs
        if op == "eq":
            return cs == vs
        if op == "ne":
            return cs != vs
        if op == "startswith":
            return cs.startswith(vs)
        return False

    if t == "duration":
        # La celda viene como "3:45" o "1:02:03"
        try:
            parts = str(cell).split(":")
            if len(parts) == 2:
                cv = int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 3:
                cv = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            else:
                cv = 0
            vv = int(val)
        except (ValueError, TypeError):
            return False
        return {
            "eq": cv == vv, "ne": cv != vv,
            "lt": cv < vv, "le": cv <= vv,
            "gt": cv > vv, "ge": cv >= vv,
        }.get(op, False)

    if t == "date":
        import datetime
        if op == "is_empty":
            return not cell
        if op == "not_empty":
            return bool(cell)
        if not cell:
            return False
        try:
            dt = datetime.datetime.fromisoformat(cell)
        except Exception:
            return False
        try:
            days = int(val)
        except (ValueError, TypeError):
            return False
        delta = (datetime.datetime.now() - dt).days
        if op == "days_ago_lt":
            return delta < days
        if op == "days_ago_gt":
            return delta > days
        return False

    return False


def matches_smart(row, rules):
    """True si la fila cumple TODAS las reglas."""
    if not rules:
        return True
    return all(rule_matches(row, r) for r in rules)


def export_pls(path, name, uris, library_lookup=None):
    lines = ["[playlist]", f"NumberOfEntries={len(uris)}"]
    for i, uri in enumerate(uris, 1):
        lines.append(f"File{i}={uri}")
        if library_lookup and uri in library_lookup:
            title, dur_ms = library_lookup[uri]
            if title:
                lines.append(f"Title{i}={title}")
            if dur_ms:
                lines.append(f"Length{i}={dur_ms // 1000}")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return True, None
    except Exception as e:
        return False, str(e)
