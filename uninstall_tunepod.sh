#!/bin/bash

# ==========================================
# Desinstalador de TunePod
# ==========================================

# Colores para la terminal
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # Sin color

echo -e "${RED}=== Desinstalador de TunePod ===${NC}"

# 1. Verificar que se ejecute con sudo
if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}Por favor, ejecuta este script con sudo:${NC}"
  echo "sudo bash desinstalar_tunepod.sh"
  exit 1
fi

# Obtener el usuario real (el que ejecutó sudo) y su carpeta home
REAL_USER=${SUDO_USER:-$USER}
REAL_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)

# 2. Eliminar archivos del sistema
echo -e "\n${YELLOW}[1/3] Eliminando archivos del sistema...${NC}"

if [ -d "/opt/tunepod" ]; then
    rm -rf /opt/tunepod
    echo "  ✓ Carpeta /opt/tunepod eliminada."
fi

if [ -f "/usr/local/bin/tunepod" ]; then
    rm -f /usr/local/bin/tunepod
    echo "  ✓ Comando 'tunepod' eliminado de /usr/local/bin."
fi

if [ -f "/usr/share/applications/tunepod.desktop" ]; then
    rm -f /usr/share/applications/tunepod.desktop
    echo "  ✓ Acceso directo del sistema eliminado."
fi

if [ -f "/usr/share/polkit-1/actions/org.tunepod.policy" ]; then
    rm -f /usr/share/polkit-1/actions/org.tunepod.policy
    echo "  ✓ Política de permisos (Polkit) eliminada."
fi

# 3. Eliminar accesos directos del usuario
echo -e "\n${YELLOW}[2/3] Eliminando accesos directos del usuario...${NC}"

if [ -f "$REAL_HOME/.local/share/applications/tunepod.desktop" ]; then
    rm -f "$REAL_HOME/.local/share/applications/tunepod.desktop"
    echo "  ✓ Acceso directo del usuario eliminado."
fi

# Actualizar base de datos de aplicaciones
update-desktop-database /usr/share/applications >/dev/null 2>&1
sudo -u "$REAL_USER" update-desktop-database "$REAL_HOME/.local/share/applications" >/dev/null 2>&1

# 4. Preguntar por los datos del usuario (Música, listas, podcasts)
echo -e "\n${YELLOW}[3/3] Datos de usuario${NC}"
echo -e "TunePod guarda tu biblioteca, configuraciones y podcasts en tu carpeta personal."
read -p "¿Deseas eliminar también estos datos? (s/N): " borrar_datos

if [[ "$borrar_datos" =~ ^[Ss]$ ]]; then
    echo "Eliminando datos de usuario..."
    
    # Configuración y listas de reproducción
    if [ -d "$REAL_HOME/.config/tunepod" ]; then
        rm -rf "$REAL_HOME/.config/tunepod"
        echo "  ✓ Configuración y listas eliminadas."
    fi
    
    # Biblioteca (library.json)
    if [ -d "$REAL_HOME/.local/share/tunepod" ]; then
        rm -rf "$REAL_HOME/.local/share/tunepod"
        echo "  ✓ Biblioteca eliminada."
    fi
    
    # Caché (carátulas, binarios compilados, conversiones ALAC)
    if [ -d "$REAL_HOME/.cache/tunepod" ]; then
        rm -rf "$REAL_HOME/.cache/tunepod"
        echo "  ✓ Caché y binarios compilados eliminados."
    fi
    
    # Podcasts descargados
    if [ -d "$REAL_HOME/Podcasts" ]; then
        read -p "  ¿Eliminar también la carpeta de Podcasts descargados (~/Podcasts)? (s/N): " borrar_pods
        if [[ "$borrar_pods" =~ ^[Ss]$ ]]; then
            rm -rf "$REAL_HOME/Podcasts"
            echo "  ✓ Podcasts eliminados."
        fi
    fi

    # Backups del iPod
    if [ -d "$REAL_HOME/tunepod/backups" ]; then
        read -p "  ¿Eliminar los respaldos de iTunesDB (~/tunepod/backups)? (s/N): " borrar_bk
        if [[ "$borrar_bk" =~ ^[Ss]$ ]]; then
            rm -rf "$REAL_HOME/tunepod/backups"
            echo "  ✓ Backups eliminados."
        fi
    fi
else
    echo "  Conservando datos de usuario."
fi

echo -e "\n${GREEN}=== ¡TunePod ha sido desinstalado! ===${NC}"
echo -e "Nota: No se han eliminado las librerías del sistema (libgpod, gstreamer, ffmpeg, etc.)"
echo -e "porque otros programas de tu sistema podrían usarlas."
