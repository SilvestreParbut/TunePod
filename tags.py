"""Escritura de tags físicos en archivos de audio usando mutagen."""
import os
import mutagen
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TPE2, TCON, TRCK, TPOS, TDRC
from mutagen.flac import FLAC
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis


def write_tags(filepath, metadata):
    """Escribe los metadatos en el archivo físico.
    metadata: dict con claves title, artist, album, albumartist,
    genre, track_nr, cd_nr, year.
    Devuelve (ok, error)."""
    if not os.path.exists(filepath):
        return False, "El archivo no existe."

    ext = os.path.splitext(filepath)[1].lower()
    try:
        if ext == ".mp3":
            _write_mp3(filepath, metadata)
        elif ext == ".flac":
            _write_flac(filepath, metadata)
        elif ext in (".m4a", ".aac", ".mp4"):
            _write_mp4(filepath, metadata)
        elif ext == ".ogg":
            _write_ogg(filepath, metadata)
        else:
            return False, f"Formato no soportado para escribir tags: {ext}"
        return True, None
    except Exception as e:
        return False, f"Error escribiendo tags: {e}"


def _write_mp3(filepath, m):
    try:
        tags = ID3(filepath)
    except Exception:
        tags = ID3()
    if m.get("title") is not None:
        tags.add(TIT2(encoding=3, text=m["title"]))
    if m.get("artist") is not None:
        tags.add(TPE1(encoding=3, text=m["artist"]))
    if m.get("album") is not None:
        tags.add(TALB(encoding=3, text=m["album"]))
    if m.get("albumartist") is not None:
        tags.add(TPE2(encoding=3, text=m["albumartist"]))
    if m.get("genre") is not None:
        tags.add(TCON(encoding=3, text=m["genre"]))
    if m.get("track_nr") is not None:
        tags.add(TRCK(encoding=3, text=str(m["track_nr"])))
    if m.get("cd_nr") is not None:
        tags.add(TPOS(encoding=3, text=str(m["cd_nr"])))
    if m.get("year") is not None:
        tags.add(TDRC(encoding=3, text=str(m["year"])))
    tags.save(filepath)


def _write_flac(filepath, m):
    audio = FLAC(filepath)
    if m.get("title") is not None:
        audio["title"] = m["title"]
    if m.get("artist") is not None:
        audio["artist"] = m["artist"]
    if m.get("album") is not None:
        audio["album"] = m["album"]
    if m.get("albumartist") is not None:
        audio["albumartist"] = m["albumartist"]
    if m.get("genre") is not None:
        audio["genre"] = m["genre"]
    if m.get("track_nr") is not None:
        audio["tracknumber"] = str(m["track_nr"])
    if m.get("cd_nr") is not None:
        audio["discnumber"] = str(m["cd_nr"])
    if m.get("year") is not None:
        audio["date"] = str(m["year"])
    audio.save()


def _write_mp4(filepath, m):
    audio = MP4(filepath)
    if m.get("title") is not None:
        audio["\xa9nam"] = [m["title"]]
    if m.get("artist") is not None:
        audio["\xa9ART"] = [m["artist"]]
    if m.get("album") is not None:
        audio["\xa9alb"] = [m["album"]]
    if m.get("albumartist") is not None:
        audio["aART"] = [m["albumartist"]]
    if m.get("genre") is not None:
        audio["\xa9gen"] = [m["genre"]]
    if m.get("track_nr") is not None:
        audio["trkn"] = [(int(m["track_nr"]), 0)]
    if m.get("cd_nr") is not None:
        audio["disk"] = [(int(m["cd_nr"]), 0)]
    if m.get("year") is not None:
        audio["\xa9day"] = [str(m["year"])]
    audio.save()


def _write_ogg(filepath, m):
    audio = OggVorbis(filepath)
    if m.get("title") is not None:
        audio["title"] = m["title"]
    if m.get("artist") is not None:
        audio["artist"] = m["artist"]
    if m.get("album") is not None:
        audio["album"] = m["album"]
    if m.get("albumartist") is not None:
        audio["albumartist"] = m["albumartist"]
    if m.get("genre") is not None:
        audio["genre"] = m["genre"]
    if m.get("track_nr") is not None:
        audio["tracknumber"] = str(m["track_nr"])
    if m.get("cd_nr") is not None:
        audio["discnumber"] = str(m["cd_nr"])
    if m.get("year") is not None:
        audio["date"] = str(m["year"])
    audio.save()
