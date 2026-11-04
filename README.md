# TunePod 🎵

**TunePod** is an open-source application for Linux that functions as a music player, podcast manager, and metadata editor. What sets it apart is its ability to not only handle synchronization but also restore the firmware for 1st through 4th generation iPod Classic models, as well as 1st and 2nd generation iPod Mini models. It is designed to be lightweight, fast, and user-friendly, with bilingual support for Spanish and English.

## Features

*   **Music Player:** Play your local files (.mp3, .m4a, .flac, .ogg, .wav).
*   **Podcast Manager:** Search for, subscribe to, and download episodes directly from the app.
*   **Metadata Editor:** Edit song titles, artists, albums, and cover art.
*   **iPod Synchronization:** Copy music, manage playlists, and sync statistics (play counts and ratings).
*   **Firmware Restoration:** Includes tools to restore 1st–4th generation iPods and iPod Minis.
*   **Adaptable Interface:** Dark/light mode and bilingual support (Spanish/English).

## Installation (from source code)

TunePod requires Python 3, GTK, GStreamer, and the `libgpod` development libraries. 1.  Clone the repository:
git clone https://github.com/SilvestreParbut/TunePod.git
cd TunePod

2.  Run the application:
python3 player.py

## Installation for non-Debian systems

Copy and run this into your terminal:

curl -sL https://raw.githubusercontent.com/SilvestreParbut/TunePod/main/instalar_tunepod.sh | bash

## Creating the .deb installer

If you prefer to install TunePod as a package on your system, you can generate the `.deb` file manually:

1.  Go to the packaging folder:
cd tunepod-package

2.  Run the build script:
bash build_deb.sh

3.  Install the generated package:
sudo apt install ./tunepod_*.deb

##¿Por qué no hay una versión Flatpak?

TunePod necesita acceso directo al hardware para expulsar el iPod de forma segura y restaurar el firmware. El aislamiento (sandbox) de Flatpak impide estas operaciones críticas, por lo que la versión oficial y recomendada es el paquete 

## Contributing

If you encounter any bugs or have an idea to improve TunePod, feel free to open an **Issue** or submit a **Pull Request**. Any help is welcome!

## License

This project is distributed under the terms of the **GPL-3.0** license. See the `LICENSE` file for more details.
