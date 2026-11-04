#!/bin/bash
# =================================================================
# INSTALADOR AUTOMÁTICO TODO-EN-UNO DE TUNEPOD PARA FEDORA / RED HAT
# =================================================================

set -e

# 1. DETECTAR SI YA ESTAMOS EN LA CARPETA DEL PROYECTO
if [ -f "player.py" ] && [ -f "ipod_read.c" ]; then
    echo "=== 1. DETECTADO: Ya estás dentro de la carpeta TunePod ==="
    echo "Usando el código local. Omitiendo descarga desde GitHub."
    BUILD_DIR="$(pwd)"
else
    echo "=== 1. DESCARGANDO EL CÓDIGO DESDE GITHUB ==="
    rm -rf /tmp/tunepod_build
    git clone https://github.com/SilvestreParbut/TunePod.git /tmp/tunepod_build
    BUILD_DIR="/tmp/tunepod_build"
fi
cd "$BUILD_DIR"

# 2. INSTALAR DEPENDENCIAS
echo "=== 2. INSTALANDO DEPENDENCIAS (RPMFUSION) ==="
sudo dnf install -y https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm https://mirrors.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-$(rpm -E %fedora).noarch.rpm
sudo dnf install -y ffmpeg --allowerasing
sudo dnf install -y python3 python3-gobject python3-mutagen gtk3 libnotify libgpod-devel glib2-devel json-glib-devel pkg-config gstreamer1-plugins-base gstreamer1-plugins-good gstreamer1-plugins-bad-free gstreamer1-plugins-ugly gstreamer1-libav python3-dbus python3-cairo udisks2 polkit gcc make gobject-introspection python3-gobject-base

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

sudo chmod +x /opt/tunepod/player.py /opt/tunepod/ipod_read /opt/tunepod/ipod_write /opt/tunepod/ipod_copy /opt/tunepod/ipod_clean /opt/tunepod/ipod_eject_helper.sh /opt/tunepod/ipod_restore_helper.sh

# 5. CREAR COMANDO Y ACCESO DIRECTO CON ÍCONO
echo "=== 5. CREANDO COMANDO Y ACCESO DIRECTO ==="
# Comando en la terminal
sudo bash -c 'echo -e "#!/bin/sh\npython3 /opt/tunepod/player.py \"\$@\"" > /usr/local/bin/tunepod && chmod +x /usr/local/bin/tunepod'

# Instalar el ícono en la ubicación estándar del sistema
sudo mkdir -p /usr/share/icons/hicolor/256x256/apps/
sudo cp /opt/tunepod/ipod.png /usr/share/icons/hicolor/256x256/apps/tunepod.png
sudo gtk-update-icon-cache /usr/share/icons/hicolor/ -f -t &> /dev/null || true

# Crear el acceso directo en el menú de aplicaciones
mkdir -p ~/.local/share/applications/
cat > ~/.local/share/applications/tunepod.desktop << 'DESKTOP'
[Desktop Entry]
Type=Application
Name=TunePod
GenericName=Reproductor de música y gestor de iPod
Comment=Reproductor de música y gestor de iPod para Linux
Exec=python3 /opt/tunepod/player.py
Icon=tunepod
Terminal=false
Categories=AudioVideo;Audio;Player;GTK;
Keywords=music;audio;player;ipod;podcast;
DESKTOP

# Copiar también al directorio del sistema (respaldo)
sudo cp ~/.local/share/applications/tunepod.desktop /usr/share/applications/tunepod.desktop

# Dar permisos y actualizar las bases de datos
chmod +x ~/.local/share/applications/tunepod.desktop
sudo chmod +x /usr/share/applications/tunepod.desktop
sudo update-desktop-database /usr/share/applications/
update-desktop-database ~/.local/share/applications/

# Forzar a GNOME a recargar el menú
if command -v dbus-send &> /dev/null; then
    dbus-send --session --dest=org.gnome.Shell --type=method_call /org/gnome/Shell org.gnome.Shell.Eval string:'global.reexec_self()' &> /dev/null || true
fi

# 6. LIMPIAR (solo si descargamos a /tmp)
if [ "$BUILD_DIR" == "/tmp/tunepod_build" ]; then
    echo "=== LIMPIANDO ARCHIVOS TEMPORALES ==="
    rm -rf /tmp/tunepod_build
fi

echo "================================================================="
echo "¡INSTALACIÓN COMPLETADA!"
echo "Puedes abrir TunePod desde el menú de aplicaciones o escribiendo 'tunepod' en la terminal."
echo "================================================================="
