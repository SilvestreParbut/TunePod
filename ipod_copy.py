"""Copia archivos de audio al iPod desde la biblioteca local.

Wrapper de ipod_copy.c. Extrae metadatos con GStreamer, genera el JSON
que espera el helper, ejecuta el helper, y verifica el resultado.
"""
import os
import json
import tempfile
import subprocess
from pathlib import Path
import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstPbutils", "1.0")
from gi.repository import Gst, GstPbutils, GLib

Gst.init(None)

HERE = Path(__file__).resolve().parent
HELPER_SRC = HERE / "ipod_copy.c"
HELPER_BIN = Path(os.path.expanduser("~/.cache/tunepod/bin/ipod_copy"))

_DISCOVERER = None


def _discoverer():
    global _DISCOVERER
    if _DISCOVERER is None:
        _DISCOVERER = GstPbutils.Discoverer.new(5 * Gst.SECOND)
    return _DISCOVERER


def ensure_helper_compiled():
    if not HELPER_SRC.exists():
        return False, f"Falta el archivo {HELPER_SRC}"
    if HELPER_BIN.exists():
        try:
            if HELPER_BIN.stat().st_mtime >= HELPER_SRC.stat().st_mtime:
                return True, None
        except OSError:
            pass
   
    HELPER_BIN.parent.mkdir(parents=True, exist_ok=True)         
    try:
        # Obtenemos las banderas de compilación y enlazado correctas
        cflags = subprocess.check_output(
            ["pkg-config", "--cflags", "libgpod-1.0", "glib-2.0", "gobject-2.0", "json-glib-1.0"],
            text=True
        ).split()
        libs = subprocess.check_output(
            ["pkg-config", "--libs", "libgpod-1.0", "glib-2.0", "gobject-2.0", "json-glib-1.0"],
            text=True
        ).split()
        
        # Unimos todo en un solo comando
        cmd = ["gcc", "-O2", "-o", str(HELPER_BIN), str(HELPER_SRC)] + cflags + libs
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=90,
        )
    except FileNotFoundError:
        return False, "gcc o pkg-config no encontrados."
    except Exception as e:
        return False, f"Error ejecutando pkg-config/gcc: {e}"

    if r.returncode != 0:
        return False, f"Error compilando:\n{r.stderr}"
        
    return True, None

def extract_metadata(local_path):
    """Extrae metadatos de un archivo local con GStreamer."""
    # Codificar la URI correctamente (maneja ?, &, espacios, tildes…)
    try:
        uri = GLib.filename_to_uri(local_path, None)
    except Exception as e:
        # Fallback defensivo: no es URI válida
        result = {
            "title": os.path.splitext(os.path.basename(local_path))[0],
            "artist": "", "album": "", "albumartist": "",
            "track_nr": 0, "cd_nr": 0, "duration_ms": 0,
            "bitrate": 0, "samplerate": 0,
            "filetype": os.path.splitext(local_path)[1].lower().lstrip("."),
        }
        return result

    try:
        info = _discoverer().discover_uri(uri)
    except Exception as e:
        print(f"[ipod_copy] error descubriendo {local_path}: {e}")
        info = None

    result = {
        "title": "",
        "artist": "",
        "album": "",
        "albumartist": "",
        "track_nr": 0,
        "cd_nr": 0,
        "duration_ms": 0,
        "bitrate": 0,
        "samplerate": 0,
        "filetype": "",
    }
    if info is None:
        result["title"] = os.path.splitext(os.path.basename(local_path))[0]
        ext = os.path.splitext(local_path)[1].lower().lstrip(".")
        result["filetype"] = ext
        return result

    tags = info.get_tags()
    if tags is not None:
        exists, val = tags.get_string_index("title", 0)
        if exists and val:
            result["title"] = val
        exists, val = tags.get_string_index("artist", 0)
        if exists and val:
            result["artist"] = val
        exists, val = tags.get_string_index("album", 0)
        if exists and val:
            result["album"] = val
        exists, val = tags.get_string_index("album-artist", 0)
        if exists and val:
            result["albumartist"] = val

        exists, v = tags.get_uint_index("track-number", 0)
        if exists:
            result["track_nr"] = int(v)
        else:
            exists, v = tags.get_string_index("track-number", 0)
            if exists and v:
                try:
                    result["track_nr"] = int(v.split("/")[0].strip())
                except (ValueError, TypeError):
                    pass

        exists, v = tags.get_uint_index("album-disc-number", 0)
        if exists:
            result["cd_nr"] = int(v)

        exists, v = tags.get_uint_index("bitrate", 0)
        if exists:
            result["bitrate"] = int(v) // 1000
        else:
            exists, v = tags.get_uint_index("maximum-bitrate", 0)
            if exists:
                result["bitrate"] = int(v) // 1000

    dur_ns = info.get_duration()
    if dur_ns:
        result["duration_ms"] = dur_ns // Gst.MSECOND

    streams = info.get_stream_list()
    for s in streams:
        if isinstance(s, GstPbutils.DiscovererAudioInfo):
            result["samplerate"] = s.get_sample_rate()
            break

    ext = os.path.splitext(local_path)[1].lower().lstrip(".")
    if ext in ("m4a", "aac", "mp4"):
        result["filetype"] = "m4a"
    elif ext == "mp3":
        result["filetype"] = "mp3"
    else:
        result["filetype"] = ext

    if not result["title"]:
        result["title"] = os.path.splitext(os.path.basename(local_path))[0]

    return result

def copy_tracks(mount, tracks, dry_run=False, progress_callback=None):
    """Copia una lista de canciones al iPod.
    tracks: lista de dicts con local_path + metadatos.
    Devuelve (ok, output, error)."""
    ok, err = ensure_helper_compiled()
    if not ok:
        return False, None, err

    fd, json_path = tempfile.mkstemp(suffix=".json", prefix="ipod_copy_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(tracks, f, ensure_ascii=False, indent=2)
        args = [str(HELPER_BIN)]
        if dry_run:
            args.append("--dry-run")
        args.extend(["--copy", json_path, mount])
        
        # Usamos Popen para leer la salida en tiempo real
        process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        
        stdout_lines = []
        for line in process.stdout:
            stdout_lines.append(line)
            if progress_callback and "→" in line:
                # Extraer solo el nombre de la canción para que se vea bonito
                # La línea es algo como: "  → «Artista» — «Título» [mp3]"
                clean_line = line.strip().replace("  → ", "")
                progress_callback(clean_line)
                
        process.wait()
        stderr_lines = process.stderr.read()
        
        if process.returncode != 0:
            return False, "".join(stdout_lines), (
                f"Helper error (código {process.returncode}):\n"
                f"stdout: {''.join(stdout_lines)}\nstderr: {stderr_lines}")
        return True, "".join(stdout_lines), None
    finally:
        try:
            os.remove(json_path)
        except OSError:
            pass
def build_track_dict(local_uri, playlist_name=None, extra=None):
    """Dada una URI (file:///...), extrae metadatos y devuelve un dict
    listo para pasar a copy_tracks. Si `extra` se pasa, sus claves
    sobreescriben los metadatos extraídos del archivo (útil para podcasts)."""
    from gi.repository import GLib
    try:
        local_path = GLib.filename_from_uri(local_uri)[0]
    except Exception:
        return None
    if not os.path.exists(local_path):
        return None
    meta = extract_metadata(local_path)
    meta["local_path"] = local_path
    if playlist_name:
        meta["playlist_name"] = playlist_name
    if extra:
        meta.update(extra)
    return meta
