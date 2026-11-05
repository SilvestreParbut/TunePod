# TunePod 🎵

**TunePod** is an open-source application for Linux that functions as a music player, podcast manager, and metadata editor. What sets it apart is its ability to not only handle synchronization but also restore the firmware for 1st through 4th generation iPod Classic models, as well as 1st and 2nd generation iPod Mini models. It is designed to be lightweight, fast, and user-friendly, with bilingual support for Spanish and English.

## Features

*   **Music Player:** Play your local files (.mp3, .m4a, .flac, .ogg, .wav).
*   **Podcast Manager:** Search for, subscribe to, and download episodes directly from the app using the iTunes database.
*   **Metadata Editor:** Edit song titles, artists, albums, and cover art.
*   **iPod Synchronization:** Copy music (iPod to PC, PC to ipod), manage playlists, and sync statistics (play counts and ratings).
*   **Firmware Restoration:** Includes tools to restore 1st–4th/photo generation iPods and iPod Minis.
*   **Adaptable Interface:** Dark/light mode and bilingual support (Spanish/English).

## Supported and tested iPods

Currently, TunePod offers theoretical support for the 1st, 2nd, 3rd, 4th, Photo, Mini 1, and Mini 2 models.
It has been physically tested for synchronization and firmware restoration on:
-iPod 4th gen: usb and firewire

## Podcast

The iPod’s firmware sorts podcasts from newest to oldest and plays only one at a time, which is inconvenient for listening to audio series. To address this, TunePod sorts podcasts in the iPod menu from oldest to newest; it also creates a playlist for each podcast —updated with every synchronization— that arranges episodes from oldest to newest and allows for continuous, sequential playback.

## Run from Python script

TunePod requires Python 3, GTK, GStreamer, and the `libgpod` development libraries. 1.  Clone the repository:
git clone https://github.com/SilvestreParbut/TunePod.git
cd TunePod

2.  Run the application:
python3 player.py

## Universal installer by terminal

Copy and run this into your terminal to install Tunepod on your linux system:

curl -sL https://raw.githubusercontent.com/SilvestreParbut/TunePod/main/instalar_tunepod.sh | bash

## Creating the .deb installer

If you prefer to install TunePod as a package on your system, you can generate the `.deb` file manually:

1.  Go to the packaging folder:
cd tunepod-package

2.  Run the build script:
bash build_deb.sh

3.  Install the generated package:
sudo apt install ./tunepod_*.deb

## Why is there no Flatpak version?

TunePod requires direct access to the hardware in order to safely eject the iPod and restore its firmware. The Flatpak sandbox prevents these critical operations

## Contributing

If you find any errors, have an idea to improve TunePod, or have tested TunePod with a generation not included in the list above, please feel free to open an **issue** or submit a **pull request**. Any help is welcome!

## License

This project is distributed under the terms of the **GPL-3.0** license. See the `LICENSE` file for more details.
