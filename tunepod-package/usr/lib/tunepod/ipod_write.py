"""Wrapper Python para el helper ipod_write.c."""
import os
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELPER_SRC = HERE / "ipod_write.c"
HELPER_BIN = Path(os.path.expanduser("~/.cache/tunepod/bin/ipod_write"))


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
        # AÑADIDO: json-glib-1.0
        cflags = subprocess.check_output(
            ["pkg-config", "--cflags", "--libs", "libgpod-1.0", "glib-2.0", "gobject-2.0", "json-glib-1.0"],
            text=True
        ).strip().split()
        r = subprocess.run(
            ["gcc", "-O2", "-o", str(HELPER_BIN), str(HELPER_SRC)] + cflags,
            capture_output=True, text=True, timeout=90,
        )
    except FileNotFoundError:
        return False, "gcc no encontrado. Instala build-essential."
    except Exception as e:
        return False, f"Error ejecutando gcc: {e}"
    if r.returncode != 0:
        return False, f"Error compilando ipod_write.c:\n{r.stderr}"
    return True, None


def _run(args, timeout=180):
    ok, err = ensure_helper_compiled()
    if not ok:
        return None, err
    try:
        r = subprocess.run(
            [str(HELPER_BIN)] + args,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, "Timeout del helper"
    except Exception as e:
        return None, f"Error ejecutando helper: {e}"
    if r.returncode != 0:
        return None, (
            f"Helper error (código {r.returncode}):\n"
            f"stderr: {r.stderr}\nstdout: {r.stdout}"
        )
    return r.stdout, None


def list_playlists(mount):
    return _run(["--list-playlists", mount])

def create_playlist(mount, name, dry_run=False):
    args = ["--create-playlist", name, mount]
    if dry_run: args.insert(0, "--dry-run")
    return _run(args)

def delete_playlist(mount, name, dry_run=False):
    args = ["--delete-playlist", name, mount]
    if dry_run: args.insert(0, "--dry-run")
    return _run(args)

def sync_playlist(mount, name, track_ids, dry_run=False):
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".txt", prefix="ipod_ids_")
    try:
        with os.fdopen(fd, "w") as f:
            for tid in track_ids: f.write(f"{tid}\n")
        args = ["--sync-playlist", name, tmp, mount]
        if dry_run: args.insert(0, "--dry-run")
        return _run(args)
    finally:
        try: os.remove(tmp)
        except OSError: pass

def delete_tracks(mount, track_ids, dry_run=False):
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".txt", prefix="ipod_del_")
    try:
        with os.fdopen(fd, "w") as f:
            for tid in track_ids: f.write(f"{tid}\n")
        args = ["--delete-tracks", tmp, mount]
        if dry_run: args.insert(0, "--dry-run")
        return _run(args)
    finally:
        try: os.remove(tmp)
        except OSError: pass

def update_track_nr(mount, updates, dry_run=False):
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".txt", prefix="ipod_tnr_")
    try:
        with os.fdopen(fd, "w") as f:
            for tid, tnr in updates: f.write(f"{tid} {tnr}\n")
        args = ["--update-track-nr", tmp, mount]
        if dry_run: args.insert(0, "--dry-run")
        return _run(args)
    finally:
        try: os.remove(tmp)
        except OSError: pass

def init_ipod(mount):
    return _run(["--init", mount])

def update_metadata(mount, tracks_data, dry_run=False):
    """Actualiza metadatos de varias pistas."""
    import tempfile
    import json
    fd, tmp = tempfile.mkstemp(suffix=".json", prefix="ipod_meta_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(tracks_data, f, ensure_ascii=False, indent=2)
        args = ["--update-metadata", tmp, mount]
        if dry_run: args.insert(0, "--dry-run")
        return _run(args)
    finally:
        try: os.remove(tmp)
        except OSError: pass
