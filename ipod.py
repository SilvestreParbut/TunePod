"""Lectura del iPod con libgpod. Solo lectura, nunca escribe."""
import os
import json
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELPER_SRC = HERE / "ipod_read.c"
HELPER_BIN = Path(os.path.expanduser("~/.cache/tunepod/bin/ipod_read"))


def ensure_helper_compiled():
    """Compila el helper si hace falta. Devuelve (ok, error)."""
    if not HELPER_SRC.exists():
        return False, f"Falta el archivo {HELPER_SRC}"
    if HELPER_BIN.exists():
        try:
            if HELPER_BIN.stat().st_mtime >= HELPER_SRC.stat().st_mtime:
                return True, None
        except OSError:
            pass
    try:
        HELPER_BIN.parent.mkdir(parents=True, exist_ok=True)
        import shlex
        cflags = subprocess.check_output(
            ["pkg-config", "--cflags", "--libs", "libgpod-1.0", "glib-2.0"],
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
        return False, f"Error compilando ipod_read.c:\n{r.stderr}"
    return True, None


def _get_block_device(mount_path):
    """Lee /proc/mounts para saber qué dispositivo físico está montado aquí."""
    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == mount_path:
                    return parts[0]
    except Exception:
        pass
    return None

def _is_apple_usb(mount_path):
    """Comprueba si el dispositivo montado es de Apple (Vendor ID 05ac)."""
    block = _get_block_device(mount_path)
    if not block:
        return False
    import re
    base_dev = re.sub(r'\d+$', '', block).split('/')[-1]
    vendor_path = f"/sys/class/block/{base_dev}/device/../idVendor"
    try:
        if os.path.exists(vendor_path):
            with open(vendor_path, "r") as f:
                return f.read().strip() == "05ac"
    except Exception:
        pass
    return False

def find_mounted_ipods():
    """Devuelve lista de paths donde parece haber un iPod montado."""
    candidates = []
    user = os.environ.get("USER", "user")
    # Rutas estándar de montaje en cualquier Linux
    bases = [f"/media/{user}", "/media", "/mnt", f"/run/media/{user}", "/run/media"]
    
    seen = set()
    for base in bases:
        if not os.path.isdir(base) or base in seen:
            continue
        seen.add(base)
        try:
            entries = os.listdir(base)
        except PermissionError:
            continue
        for entry in entries:
            full = os.path.join(base, entry)
            if not os.path.isdir(full) or full in candidates:
                continue
                
            # Comprobación 1: ¿Tiene la base de datos de iTunes?
            itunes_db = os.path.join(full, "iPod_Control", "iTunes", "iTunesDB")
            if os.path.exists(itunes_db):
                candidates.append(full)
                continue
                
            # Comprobación 2: ¿Se llama IPOD?
            if "IPOD" in entry.upper():
                candidates.append(full)
                continue
                
            # Comprobación 3 (Definitiva): ¿Es un USB de Apple (05ac)?
            if _is_apple_usb(full):
                candidates.append(full)
                
    return candidates

def read_ipod(mount_point):
    """Lee el iPod en modo solo-lectura. Devuelve (dict, error).
    El dict tiene 'tracks', 'playlists', 'track_count', 'playlist_count'."""
    ok, err = ensure_helper_compiled()
    if not ok:
        return None, err
    try:
        r = subprocess.run(
            [str(HELPER_BIN), mount_point],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return None, "Timeout leyendo el iPod (>120s)"
    except Exception as e:
        return None, f"Error ejecutando helper: {e}"
    if r.returncode != 0:
        return None, (
            f"Helper devolvió error (código {r.returncode}):\n"
            f"{r.stderr.strip()}"
        )
    try:
        return json.loads(r.stdout), None
    except json.JSONDecodeError as e:
        return None, (
            f"Salida JSON inválida: {e}\n"
            f"Primeros 200 caracteres: {r.stdout[:200]}"
        )


def ipod_track_full_path(mount_point, ipod_path):
    """Convierte ':iPod_Control:Music:F00:ABCD.mp3' al path absoluto real."""
    if not ipod_path:
        return None
    rel = ipod_path.lstrip(":").replace(":", "/")
    return os.path.join(mount_point, rel)


def rating_to_stars(rating):
    """Convierte rating de libgpod (0-100) a estrellas (0-5)."""
    if not rating:
        return 0
    return max(0, min(5, round(rating / 20)))


def stars_to_rating(stars):
    """Convierte estrellas (0-5) a rating de libgpod (0-100)."""
    return int(max(0, min(5, stars)) * 20)
