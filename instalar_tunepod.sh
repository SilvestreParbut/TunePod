#!/bin/bash
# =================================================================
# INSTALADOR AUTOMÁTICO TODO-EN-UNO DE TUNEPOD PARA FEDORA / RED HAT
# =================================================================

set -e

echo "=== 1. DESCARGANDO EL CÓDIGO DESDE GITHUB ==="
# Descargamos en /tmp para no dejar basura en la carpeta del usuario
rm -rf /tmp/tunepod_build
git clone https://github.com/SilvestreParbut/TunePod.git /tmp/tunepod_build
cd /tmp/tunepod_build

echo "=== 2. INSTALANDO DEPENDENCIAS (RPMFUSION) ==="
sudo dnf install -y https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm https://mirrors.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-$(rpm -E %fedora).noarch.rpm

echo "Reemplazando ffmpeg-free por el completo de RPMFusion..."
sudo dnf install -y ffmpeg --allowerasing

echo "Instalando librerías de Python, C y GStreamer..."
sudo dnf install -y python3 python3-gobject python3-mutagen gtk3 libnotify libgpod-devel glib2-devel json-glib-devel pkg-config gstreamer1-plugins-base gstreamer1-plugins-good gstreamer1-plugins-bad-free gstreamer1-plugins-ugly gstreamer1-libav python3-dbus python3-cairo udisks2 polkit gcc make gobject-introspection python3-gobject-base

echo "=== 3. COMPILANDO LOS PROGRAMAS EN C ==="
gcc ipod_read.c -o ipod_read $(pkg-config --cflags --libs libgpod-1.0 glib-2.0 json-glib-1.0)
gcc ipod_write.c -o ipod_write $(pkg-config --cflags --libs libgpod-1.0 glib-2.0 json-glib-1.0)
gcc ipod_copy.c -o ipod_copy $(pkg-config --cflags --libs libgpod-1.0 glib-2.0 json-glib-1.0)
gcc ipod_clean.c -o ipod_clean $(pkg-config --cflags --libs libgpod-1.0 glib-2.0 json-glib-1.0)

echo "=== 4. INSTALANDO EN EL SISTEMA ==="
sudo rm -rf /opt/tunepod
sudo mkdir -p /opt/tunepod
sudo cp -r /tmp/tunepod_build/* /opt/tunepod/

sudo chmod +x /opt/tunepod/player.py
sudo chmod +x /opt/tunepod/ipod_read
sudo chmod +x /opt/tunepod/ipod_write
sudo chmod +x /opt/tunepod/ipod_copy
sudo chmod +x /opt/tunepod/ipod_clean
sudo chmod +x /opt/tunepod/ipod_eject_helper.sh
sudo chmod +x /opt/tunepod/ipod_restore_helper.sh

echo "=== 5. CREANDO COMANDO Y ACCESO DIRECTO ==="
# Comando en la terminal
sudo bash -c 'echo -e "#!/bin/sh\npython3 /opt/tunepod/player.py \"\$@\"" > /usr/local/bin/tunepod && chmod +x /usr/local/bin/tunepod'

# Acceso directo en el menú de aplicaciones (para tu usuario)
mkdir -p ~/.local/share/applications/
cat > ~/.local/share/applications/tunepod.desktop << 'EOF'
[Desktop Entry]
Type=Application
Name=TunePod
GenericName=Reproductor de música y gestor de iPod
Comment=Reproductor de música y gestor de iPod para Linux
Exec=python3 /opt/tunepod/player.py
Icon=/opt/tunepod/ipod.png
Terminal=false
Categories=AudioVideo;Audio;Player;GTK;
Keywords=music;audio;player;ipod;podcast;
EOF

# Copiar también al directorio del sistema (respaldo para GNOME Classic)
sudo cp ~/.local/share/applications/tunepod.desktop /usr/share/applications/tunepod.desktop

# Dar permisos y actualizar las bases de datos de los menús
chmod +x ~/.local/share/applications/tunepod.desktop
sudo chmod +x /usr/share/applications/tunepod.desktop
sudo update-desktop-database /usr/share/applications/
update-desktop-database ~/.local/share/applications/

# Forzar a GNOME Classic a recargar el menú (esto evita tener que reiniciar)
if command -v dbus-send &> /dev/null; then
    dbus-send --session --dest=org.gnome.Shell --type=method_call /org/gnome/Shell org.gnome.Shell.Eval string:'global.reexec_self()' &> /dev/null || true
fi
