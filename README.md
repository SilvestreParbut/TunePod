# TunePod 🎵

**TunePod** es una aplicación de código abierto para Linux que funciona como reproductor de música, gestor de podcasts, editor de metadatos y sincronizador y restaurador de firmware para el iPod. Está diseñado para ser ligero, rápido y fácil de usar, con soporte para múltiples idiomas (español e inglés).

## Características

*   **Reproductor de música:** Reproduce tus archivos locales (.mp3, .m4a, .flac, .ogg, .wav).
*   **Gestor de Podcasts:** Busca, suscríbete y descarga episodios directamente desde la app.
*   **Editor de metadatos:** Edita títulos, artistas, álbumes y carátulas de tus canciones.
*   **Sincronización con iPod:** Copia música, gestiona listas de reproducción y sincroniza estadísticas (reproducciones y valoraciones).
*   **Restauración de firmware:** Incluye herramientas para restaurar iPods de 1ª a 4ª generación y Minis.
*   **Interfaz adaptable:** Modo oscuro/claro y traducciones automáticas (Español/Inglés).

## Instalación (desde el código fuente)

TunePod requiere Python 3, GTK, GStreamer y las librerías de desarrollo de `libgpod`.

1.  Clona el repositorio:
    git clone https://github.com/SilvestreParbut/TunePod.git
    cd TunePod

2.  Ejecuta la aplicación:
    python3 player.py

## Crear el instalador .deb

Si prefieres instalar TunePod como un paquete en tu sistema, puedes generar el archivo `.deb` manualmente:

1.  Ve a la carpeta del empaquetado:
    cd tunepod-package

2.  Ejecuta el script de construcción:
    bash build_deb.sh

3.  Instala el paquete generado:
    sudo apt install ./tunepod_*.deb

## Contribuir

Si encuentras algún error o tienes una idea para mejorar TunePod, siéntete libre de abrir un **Issue** o enviar un **Pull Request**. ¡Toda ayuda es bienvenida!

## Licencia

Este proyecto se distribuye bajo los términos de la licencia **GPL-3.0**. Consulta el archivo `LICENSE` para más detalles.
