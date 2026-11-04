#!/usr/bin/env bash
set -euo pipefail

SRC="$HOME/tunepod"
PKG="$HOME/tunepod/tunepod-package"
LIBDEST="$PKG/usr/lib/tunepod"

echo "=== 1. Copiando archivos actualizados desde $SRC ==="

# Copiar solo los .py que forman parte de la app
APP_PY=(
    "player.py"
    "translations.py"
    "library.py"
    "playlists.py"
    "podcasts.py"
    "preferences.py"
    "tags.py"
    "ipod.py"
    "ipod_sync.py"
    "ipod_copy.py"
    "ipod_write.py"
    "lyrics.py"
    "cover_art.py"
)
for f in "${APP_PY[@]}"; do
    if [[ -f "$SRC/$f" ]]; then
        cp -v "$SRC/$f" "$LIBDEST/"
    else
        echo "⚠️  Falta $SRC/$f"
    fi
done

# Copiar scripts .sh
for f in ipod_eject_helper.sh ipod_restore_helper.sh; do
    if [[ -f "$SRC/$f" ]]; then
        cp -v "$SRC/$f" "$LIBDEST/"
    fi
done

# Copiar firmwares (solo si no están ya)
if [[ -d "$SRC/firmwares" ]]; then
    mkdir -p "$LIBDEST/firmwares"
    for fw in "$SRC/firmwares"/firmware_*.ipod; do
        [[ -f "$fw" ]] || continue
        cp -v "$fw" "$LIBDEST/firmwares/"
    done
    if [[ -f "$SRC/firmwares/lista de nombres de ipods.txt" ]]; then
        cp -v "$SRC/firmwares/lista de nombres de ipods.txt" "$LIBDEST/firmwares/"
    fi
fi

echo ""
echo "=== 2. Compilando binarios C si hace falta ==="
cd "$LIBDEST"
for cfile in ipod_read.c ipod_write.c ipod_copy.c ipod_clean.c; do
    base="${cfile%.c}"
    if [[ -f "$cfile" ]]; then
        if [[ ! -x "$base" ]] || [[ "$cfile" -nt "$base" ]]; then
            echo "   Compilando $cfile → $base"
            if [[ "$base" == "ipod_read" || "$base" == "ipod_clean" ]]; then
                gcc -O2 -o "$base" "$cfile" $(pkg-config --cflags --libs glib-2.0 libgpod-1.0 2>/dev/null) || \
                gcc -O2 -o "$base" "$cfile" $(pkg-config --cflags --libs glib-2.0)
            else
                gcc -O2 -o "$base" "$cfile" $(pkg-config --cflags --libs glib-2.0 libgpod-1.0 json-glib-1.0 2>/dev/null) || \
                gcc -O2 -o "$base" "$cfile" $(pkg-config --cflags --libs glib-2.0 libgpod-1.0)
            fi
        else
            echo "   $base ya está compilado"
        fi
    fi
done

echo ""
echo "=== 3. Ajustando permisos ==="
chmod +x "$LIBDEST"/ipod_*.sh 2>/dev/null || true
chmod +x "$LIBDEST"/ipod_read "$LIBDEST"/ipod_write "$LIBDEST"/ipod_copy "$LIBDEST"/ipod_clean 2>/dev/null || true
chmod +x "$PKG/usr/bin/tunepod" 2>/dev/null || true
chmod +x "$PKG/DEBIAN/postinst" 2>/dev/null || true

echo ""
echo "=== 4. Verificando archivos críticos ==="
missing=0
for f in player.py translations.py library.py playlists.py podcasts.py preferences.py tags.py ipod.py ipod_sync.py ipod_copy.py ipod_write.py lyrics.py cover_art.py; do
    if [[ ! -f "$LIBDEST/$f" ]]; then
        echo "❌ Falta $LIBDEST/$f"
        missing=$((missing+1))
    fi
done
if [[ $missing -gt 0 ]]; then
    echo "❌ Faltan $missing archivos. Abortando."
    exit 1
fi

echo ""
echo "=== 5. Construyendo .deb ==="
VERSION=$(grep '^Version:' "$PKG/DEBIAN/control" | awk '{print $2}')
OUTPUT="$HOME/tunepod_${VERSION}_all.deb"
cd "$PKG"
dpkg-deb --build --root-owner-group . "$OUTPUT"

echo ""
echo "✅ .deb creado: $OUTPUT"
ls -lh "$OUTPUT"
echo ""
echo "Para instalar:  sudo dpkg -i $OUTPUT"
echo "Para ver info:  dpkg-deb -I $OUTPUT"
