"""Matching entre la biblioteca local y los tracks del iPod."""
import re
import unicodedata


def _normalize(s):
    """Normaliza un string para comparación."""
    if not s:
        return ""
    s = s.lower()
    s = s.replace("&", " and ")
    s = s.replace("’", "'").replace("‘", "'")
    s = s.replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-")
    s = s.replace("!", "").replace("?", "").replace(".", "").replace(",", "")
    s = s.replace("...", "").replace("…", "")
    s = s.replace("(", "").replace(")", "").replace("[", "").replace("]", "")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = " ".join(s.split())
    if s.startswith("the "):
        s = s[4:]
    return s


def _aggressive_normalize(s):
    """Normalización más agresiva (quita paréntesis)."""
    if not s:
        return ""
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"\[[^\]]*\]", "", s)
    return _normalize(s)


def _duration_str_to_seconds(s):
    """'3:45' → 225, '1:02:03' → 3723, '' → 0."""
    if not s:
        return 0
    try:
        parts = s.split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except (ValueError, TypeError):
        pass
    return 0


def match_tracks(local_tracks, ipod_tracks, duration_tolerance=5):
    """Empareja canciones locales con tracks del iPod evitando reutilizar tracks."""
    index_strict = {}
    index_loose = {}
    for t in ipod_tracks:
        key_s = _normalize(t.get("title", ""))
        if key_s:
            index_strict.setdefault(key_s, []).append(t)
        key_l = _aggressive_normalize(t.get("title", ""))
        if key_l:
            index_loose.setdefault(key_l, []).append(t)

    matched = []
    unmatched = []
    
    # NUEVO: Conjunto para registrar qué canciones del iPod ya han sido usadas
    matched_ipod_ids = set()

    for lt in local_tracks:
        title_s = _normalize(lt.get("title", ""))
        title_l = _aggressive_normalize(lt.get("title", ""))
        lt_artist = _normalize(lt.get("artist", ""))
        lt_album = _normalize(lt.get("album", ""))
        lt_dur = _duration_str_to_seconds(lt.get("duration_str", ""))

        best = None

        def try_pick(candidates, require_duration=False):
            for c in candidates:
                # NUEVO: Si esta canción del iPod ya fue emparejada, la saltamos
                if c["id"] in matched_ipod_ids:
                    continue
                    
                c_artist = _normalize(c.get("artist", ""))
                if c_artist != lt_artist:
                    continue
                c_dur = int(c.get("duration_ms", 0)) // 1000
                dur_ok = (
                    lt_dur == 0 or c_dur == 0 or
                    abs(c_dur - lt_dur) <= duration_tolerance
                )
                if require_duration and not dur_ok:
                    continue
                
                # NUEVO: Registrar que la usamos
                matched_ipod_ids.add(c["id"])
                return c
            return None

        # Niveles de búsqueda
        if title_s:
            best = try_pick(index_strict.get(title_s, []), require_duration=True)
        if best is None and title_s:
            best = try_pick(index_strict.get(title_s, []))
        if best is None and title_l:
            best = try_pick(index_loose.get(title_l, []), require_duration=True)
        if best is None and title_l:
            best = try_pick(index_loose.get(title_l, []))

        if best is None and title_l and len(title_l) >= 4:
            for t in ipod_tracks:
                # NUEVO: Saltar si ya fue emparejada
                if t["id"] in matched_ipod_ids:
                    continue
                    
                c_title_l = _aggressive_normalize(t.get("title", ""))
                if not c_title_l or len(c_title_l) < 4:
                    continue
                if _normalize(t.get("artist", "")) != lt_artist:
                    continue
                c_dur = int(t.get("duration_ms", 0)) // 1000
                if lt_dur > 0 and c_dur > 0 and abs(c_dur - lt_dur) > duration_tolerance:
                    continue
                if title_l in c_title_l or c_title_l in title_l:
                    # NUEVO: Registrar que la usamos
                    matched_ipod_ids.add(t["id"])
                    best = t
                    break

        if best is None:
            unmatched.append(lt)
        else:
            matched.append((lt["uri"], best["id"]))

    return matched, unmatched
