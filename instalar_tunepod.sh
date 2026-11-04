#!/bin/bash
# =================================================================
# INSTALADOR AUTOMÁTICO TODO-EN-UNO DE TUNEPOD (MULTIDISTRO)
# =================================================================

set -e

# 1. DETECTAR SI YA ESTAMOS EN LA CARPETA DEL PROYECTO
if [ -f "player.py" ] && [ -f "ipod_read.c" ]; then
    echo "=== 1. DETECTADO: Ya estás dentro de la carpeta TunePod ==="
    echo "Usando el código local. Omitiendo descarga desde GitHub."
    BUILD_DIR="$(pwd)"
else
    echo "=== 1. DESCARGANDO EL CÓDIGO DESDE GITHUB ==="
    # Verificar si git está instalado
    if ! command -v git &> /dev/null; then
        echo "Error: 'git' no está instalado. Instalándolo primero..."
        if [ -f /etc/debian_version ]; then sudo apt update && sudo apt install -y git
        elif [ -f /etc/arch-release ]; then sudo pacman -S --noconfirm --needed git
        elif [ -f /etc/fedora-release ]; then sudo dnf install -y git
        elif [ -f /etc/os-release ] && grep -qi suse /etc/os-release; then sudo zypper install -y git
        fi
    fi
    rm -rf /tmp/tunepod_build
    git clone https://github.com/SilvestreParbut/TunePod.git /tmp/tunepod_build
    BUILD_DIR="/tmp/tunepod_build"
fi
cd "$BUILD_DIR"

# 2. DETECTAR DISTRIBUCIÓN E INSTALAR DEPENDENCIAS
echo "=== 2. INSTALANDO DEPENDENCIAS (DETECTANDO DISTRO) ==="

if [ -f /etc/debian_version ]; then
    # FAMILIA DEBIAN (Ubuntu, Mint, Pop!_OS, etc.)
    echo "-> Detectada familia Debian/Ubuntu"
    sudo apt update
    sudo apt install -y python3 python3-gi python3-mutagen gir1.2-gtk-3.0 gir1.2-notify-0.7 libgpod-dev libglib2.0-dev libjson-glib-dev pkg-config gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav python3-dbus python3-cairo ffmpeg udisks2 policykit-1 gcc make libgirepository1.0-dev

elif [ -f /etc/arch-release ]; then
    # FAMILIA ARCH (Manjaro, EndeavourOS, etc.)
    echo "-> Detectada familia Arch Linux"
    sudo pacman -S --noconfirm --needed python python-gobject python-mutagen gtk3 libnotify libgpod glib2 json-glib pkg-config gstreamer gst-plugins-base gst-plugins-good gst-plugins-bad gst-plugins-ugly gst-libav python-dbus python-cairo ffmpeg udisks2 polkit gcc make

elif [ -f /etc/fedora-release ] || grep -qi 'rhel\|centos\|rocky\|almalinux' /etc/os-release 2>/dev/null; then
    # FAMILIA RED HAT (Fedora, RHEL, etc.)
    echo "-> Detectada familia Red Hat / Fedora"
    sudo dnf install -y https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm https://mirrors.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-$(rpm -E %fedora).noarch.rpm || true
    sudo dnf install -y ffmpeg --allowerasing || true
    sudo dnf install -y python3 python3-gobject python3-mutagen gtk3 libnotify libgpod-devel glib2-devel json-glib-devel pkg-config gstreamer1-plugins-base gstreamer1-plugins-good gstreamer1-plugins-bad-free gstreamer1-plugins-ugly gstreamer1-libav python3-dbus python3-cairo udisks2 polkit gcc make gobject-introspection python3-gobject-base

elif grep -qi 'suse' /etc/os-release 2>/dev/null; then
    # FAMILIA SUSE (openSUSE Leap, Tumbleweed, SLES)
    echo "-> Detectada familia openSUSE / SUSE"
    # Añadir repositorio Packman para códecs
    sudo zypper ar -cfp 90 https://ftp.gwdg.de/pub/linux/misc/packman/suse/openSUSE_Tumbleweed/ packman || true
    sudo zypper --gpg-auto-import-keys refresh || true
    sudo zypper install -y python3 python3-gobject python3-mutagen gtk3 libnotify-tools libgpod-devel glib2-devel json-glib-devel pkg-config gstreamer gstreamer-plugins-base gstreamer-plugins-good gstreamer-plugins-bad gstreamer-plugins-ugly gstreamer-plugins-libav python3-dbus-python python3-cairo ffmpeg udisks2 polkit gcc make

else
    echo "ADVERTENCIA: No se pudo detectar tu distribución automáticamente."
    echo "Instala manualmente: python3, pygobject, mutagen, gtk3, libgpod-devel, glib2-devel, json-glib-devel, gcc, make."
    exit 1
fi

# 3. COMPILAR LOS PROGRAMAS EN C
echo "=== 3. COMPILANDO LOS PROGRAMAS EN C ==="
gcc ipod_read.c -o ipod_read $(pkg-config --cflags --libs libgpod-1.0 glib-2.0 json-glib-1.0)
gcc ipod_write.c -o ipod_write $(pkg-config --cflags --libs libgpod-1.0 glib-2.0 json-glib-1.0)
gcc ipod_copy.c -o ipod_copy $(pkg-config --cflags --libs libgpod-1.0 glib-2.0 json-glib-1.0)
gcc ipod_clean.c -o ipod_clean $(pkg-config --cflags --libs libgpod-1.0 glib-2.0 json-glib-1.0)

# 4. INSTALAR EN EL SISTEMA
echo "=== 4. INSTALANDO EN EL SISTEMA ==="
sudo rm -rf /opt/tunepod
sudo mkdir -p /opt/tunepod
sudo cp -r "$BUILD_DIR"/* /opt/tunepod/

# Copiar el ícono desde su ruta real
ICON_SRC="$BUILD_DIR/tunepod-package/usr/share/icons/hicolor/256x256/apps/tunepod.png"
if [ -f "$ICON_SRC" ]; then
    sudo cp "$ICON_SRC" /opt/tunepod/tunepod.png
    sudo chmod 644 /opt/tunepod/tunepod.png
fi

sudo chmod +x /opt/tunepod/player.py /opt/tunepod/ipod_read /opt/tunepod/ipod_write /opt/tunepod/ipod_copy /opt/tunepod/ipod_clean /opt/tunepod/ipod_eject_helper.sh /opt/tunepod/ipod_restore_helper.sh

# 5. CREAR COMANDO Y ACCESO DIRECTO CON ÍCONO
echo "=== 5. CREANDO COMANDO Y ACCESO DIRECTO ==="
sudo bash -c 'echo -e "#!/bin/sh\npython3 /opt/tunepod/player.py \"\$@\"" > /usr/local/bin/tunepod && chmod +x /usr/local/bin/tunepod'

mkdir -p ~/.local/share/applications/
cat > ~/.local/share/applications/tunepod.desktop << 'EOF'
[Desktop Entry]
Type=Application
Name=TunePod
GenericName=Reproductor de música y gestor de iPod
Comment=Reproductor de música y gestor de iPod para Linux
Exec=python3 /opt/tunepod/player.py
Icon=/opt/tunepod/tunepod.png
Terminal=false
Categories=AudioVideo;Audio;Player;GTK;
Keywords=music;audio;player;ipod;podcast;
EOF

sudo cp ~/.local/share/applications/tunepod.desktop /usr/share/applications/tunepod.desktop
chmod +x ~/.local/share/applications/tunepod.desktop
sudo chmod +x /usr/share/applications/tunepod.desktop
sudo update-desktop-database /usr/share/applications/ || true
update-desktop-database ~/.local/share/applications/ || true

# Forzar a GNOME a recargar
if command -v dbus-send &> /dev/null; then
    dbus-send --session --dest=org.gnome.Shell --type=method_call /org/gnome/Shell org.gnome.Shell.Eval string:'global.reexec_self()' &> /dev/null || true
fi

# 6. LIMPIAR
if [ "$BUILD_DIR" == "/tmp/tunepod_build" ]; then
    echo "=== LIMPIANDO ARCHIVOS TEMPORALES ==="
    rm -rf /tmp/tunepod_build
fi

echo "================================================================="
echo "¡INSTALACIÓN COMPLETADA!"
echo "Puedes abrir TunePod desde el menú de aplicaciones o escribiendo 'tunepod' en la terminal."
echo "================================================================="
