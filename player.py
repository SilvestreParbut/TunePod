#!/usr/bin/env python3
"""TunePod — Reproductor de música y gestor de ipod para linux."""
import os
import re
import glob
import socket
import random
import datetime
import hashlib
import subprocess
import shutil
import urllib.request
import unicodedata
import threading
import concurrent.futures
import cairo
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gst", "1.0")
gi.require_version("GstPbutils", "1.0")
gi.require_version("Pango", "1.0")
gi.require_version("Gio", "2.0")
gi.require_version("Notify", "0.7")
from gi.repository import (Gtk, Gst, GstPbutils, GLib, Pango, Gdk,
                            GdkPixbuf, Gio, Notify)
import dbus
import dbus.service
import dbus.mainloop.glib

from lyrics import get_lyrics
import library
from translations import _, _f
import podcasts
import playlists as playlists_mod
import preferences
import tags
import ipod
import ipod_write
import ipod_sync
import ipod_copy
APP_VERSION = "1.0.6"  # <--- ACTUALIZAR CON CADA LANZAMIENTO
Gst.init(None)
try:
    Notify.init("TunePod")
except Exception as e:
    print(f"[notify] No se pudo iniciar el sistema de notificaciones: {e}")
GLib.set_prgname("tunepod")
GLib.set_application_name("TunePod")

AUDIO_EXT = (".mp3", ".m4a", ".flac", ".ogg", ".wav")
LYRICS_DEFAULT = "Reproduce una canción para ver su letra aquí."
TRACK_NUM_RE = re.compile(r"^(\d{1,3})\s*[-._\s]")
PODCAST_DIR = os.path.expanduser("~/Podcasts")
SOCKET_PATH = "/tmp/tunepod.sock"

REPEAT_MODES = ["off", "all", "one"]

COL_TITLE = 0
COL_ARTIST = 1
COL_ALBUM = 2
COL_DURATION = 3
COL_URI = 4
COL_TRACK_NUM = 5
COL_POSITION = 6
COL_RATING = 7
COL_PLAY_COUNT = 8
COL_LAST_PLAYED = 9
COL_DATE_ADDED = 10
COL_YEAR = 11
COL_GENRE = 12
COL_ALBUMARTIST = 13
COL_CD_NUM = 14

VCOL_TITLE = 0
VCOL_ARTIST = 1
VCOL_ALBUM = 2
VCOL_DURATION = 3
VCOL_URI = 4
VCOL_TRACK_NUM = 5
VCOL_POSITION = 6
VCOL_RATING = 7
VCOL_PLAY_COUNT = 8


def infer_track_num(filename):
    m = TRACK_NUM_RE.match(os.path.basename(filename))
    return int(m.group(1)) if m else 0


def fmt_size(n):
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def sort_key_artist(s):
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii')
    s = s.lower().strip()
    if s.startswith("the "):
        s = s[4:]
    return s


def normalize_text(s):
    if not s:
        return ""
    s = s.replace('’', "'").replace('‘', "'").replace('“', '"').replace('”', '"')
    s = s.replace('–', '-').replace('—', '-')
    s = s.replace('Я', 'R').replace('я', 'r')
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii')
    return s.lower().strip()

def _pub_date_to_timestamp(pub_date):
    """Convierte 'YYYY-MM-DD' a timestamp Unix (segundos desde 1970).
    Si el formato no es válido, devuelve 0."""
    if not pub_date:
        return 0
    try:
        dt = datetime.datetime.strptime(pub_date.strip(), "%Y-%m-%d")
        return int(dt.timestamp())
    except (ValueError, AttributeError):
        return 0

class Mpris2Adapter(dbus.service.Object):
    def __init__(self, bus_name, player_window):
        super().__init__(bus_name, "/org/mpris/MediaPlayer2")
        self.player = player_window
        self.status = "Stopped"
        
        self.metadata = dbus.Dictionary({
            "mpris:trackid": dbus.ObjectPath("/org/tunepod/track"),
            "xesam:title": dbus.String("Ninguna canción"),
            "xesam:artist": dbus.Array([dbus.String("")], signature="s"),
            "xesam:album": dbus.String("")
        }, signature="sv")

    def update_status(self, status):
        self.status = status
        self.PropertiesChanged("org.mpris.MediaPlayer2.Player", 
                               dbus.Dictionary({"PlaybackStatus": dbus.String(status)}, signature="sv"), 
                               dbus.Array([], signature="s"))

    def update_metadata(self, title, artist, album):
        self.metadata["xesam:title"] = dbus.String(title or "")
        self.metadata["xesam:artist"] = dbus.Array([dbus.String(artist or "")], signature="s")
        self.metadata["xesam:album"] = dbus.String(album or "")
        
        self.PropertiesChanged("org.mpris.MediaPlayer2.Player", 
                               dbus.Dictionary({"Metadata": self.metadata}, signature="sv"), 
                               dbus.Array([], signature="s"))

    @dbus.service.method("org.mpris.MediaPlayer2", in_signature="", out_signature="")
    def Raise(self):
        self.player.present()

    @dbus.service.method("org.mpris.MediaPlayer2", in_signature="", out_signature="")
    def Quit(self):
        self.player.on_destroy()

    @dbus.service.method("org.mpris.MediaPlayer2.Player", in_signature="", out_signature="")
    def PlayPause(self):
        self.player.on_play_pause(None)

    @dbus.service.method("org.mpris.MediaPlayer2.Player", in_signature="", out_signature="")
    def Play(self):
        self.player.playbin.set_state(Gst.State.PLAYING)
        self.update_status("Playing")

    @dbus.service.method("org.mpris.MediaPlayer2.Player", in_signature="", out_signature="")
    def Pause(self):
        self.player.playbin.set_state(Gst.State.PAUSED)
        self.update_status("Paused")

    @dbus.service.method("org.mpris.MediaPlayer2.Player", in_signature="", out_signature="")
    def Next(self):
        self.player.play_relative(+1)

    @dbus.service.method("org.mpris.MediaPlayer2.Player", in_signature="", out_signature="")
    def Previous(self):
        self.player.play_relative(-1)

    @dbus.service.method("org.mpris.MediaPlayer2.Player", in_signature="", out_signature="")
    def Stop(self):
        self.player.playbin.set_state(Gst.State.NULL)
        self.update_status("Stopped")

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="ss", out_signature="v")
    def Get(self, interface, prop):
        if interface == "org.mpris.MediaPlayer2":
            if prop == "Identity": return dbus.String("TunePod")
            elif prop == "DesktopEntry": return dbus.String("tunepod")
            elif prop == "CanQuit": return dbus.Boolean(True)
            elif prop == "CanRaise": return dbus.Boolean(False)
            elif prop == "HasTrackList": return dbus.Boolean(False)
            elif prop == "SupportedUriSchemes": return dbus.Array([], signature="s")
            elif prop == "SupportedMimeTypes": return dbus.Array(["audio/mpeg", "audio/mp4", "audio/x-m4a", "audio/flac", "audio/ogg", "audio/wav"], signature="s")
        elif interface == "org.mpris.MediaPlayer2.Player":
            if prop == "PlaybackStatus": return dbus.String(self.status)
            elif prop == "Metadata": return self.metadata
            elif prop == "CanPlay": return dbus.Boolean(True)
            elif prop == "CanPause": return dbus.Boolean(True)
            elif prop == "CanGoNext": return dbus.Boolean(True)
            elif prop == "CanGoPrevious": return dbus.Boolean(True)
        return dbus.String("")

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface == "org.mpris.MediaPlayer2":
            return dbus.Dictionary({
                "Identity": dbus.String("TunePod"),
                "DesktopEntry": dbus.String("tunepod"),
                "CanQuit": dbus.Boolean(True),
                "CanRaise": dbus.Boolean(False),
                "HasTrackList": dbus.Boolean(False),
                "SupportedUriSchemes": dbus.Array([], signature="s"),
                "SupportedMimeTypes": dbus.Array(["audio/mpeg", "audio/mp4", "audio/x-m4a", "audio/flac", "audio/ogg", "audio/wav"], signature="s")
            }, signature="sv")
        elif interface == "org.mpris.MediaPlayer2.Player":
            return dbus.Dictionary({
                "PlaybackStatus": dbus.String(self.status),
                "Metadata": self.metadata,
                "CanPlay": dbus.Boolean(True),
                "CanPause": dbus.Boolean(True),
                "CanGoNext": dbus.Boolean(True),
                "CanGoPrevious": dbus.Boolean(True)
            }, signature="sv")
        return dbus.Dictionary({}, signature="sv")

    @dbus.service.signal("org.freedesktop.DBus.Properties", signature="sa{sv}as")
    def PropertiesChanged(self, interface, changed, invalidated):
        pass

class PlaylistWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="TunePod")
        self.set_icon_name("tunepod")
        self.set_default_size(1250, 750)
        self.connect("destroy", self.on_destroy)

        # CSS para el botón de Play redondo
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(b"""
            button.circular-play {
                border-radius: 9999px;
                min-width: 56px;
                min-height: 56px;
                padding: 0;
                border: 2px solid #888888;
                outline: none;
            }
            button.circular-play:focus {
                outline: none;
                box-shadow: none;
                border: 2px solid #888888;
            }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        try:
            Gtk.binding_entry_remove(Gtk.TreeView, Gdk.KEY_space, 0)
        except Exception:
            pass

        self.prefs = preferences.load()
        self.shuffle_enabled = bool(self.prefs.get("shuffle", False))
        self.repeat_mode = self.prefs.get("repeat", "off")
        if self.repeat_mode not in REPEAT_MODES:
            self.repeat_mode = "off"
        self.shuffle_history = []
        self.shuffle_pool = []
        self.play_queue = []

        self.playbin = Gst.ElementFactory.make("playbin", "player")
        self.playbin.set_property("volume", float(self.prefs.get("volume", 1.0)))
        bus = self.playbin.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.on_bus_message)

        self.duration_ns = 0
        self.user_dragging = False
        self.current_index = -1
        self.current_uri = None
        self._last_play_counted_uri = None
        self.discoverer = GstPbutils.Discoverer.new(5 * Gst.SECOND)

        self.import_in_progress = False
        self.import_cancelled = False
        self.import_total = 0
        self.import_done = 0
        self.import_skipped = 0
        self.shutting_down = False

        self.lyrics_request_id = 0
        self.known_uris = set()
        self.search_timeout_id = 0
        self.search_query = ""

        self.playlists = playlists_mod.load()
        self.current_playlist_id = None
        self.current_playlist_uris = None
        self.artist_tree_dirty = True
        self.albums_grid_dirty = True
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

        self.podcast_search_running = False
        self.podcast_feed_running = False
        self.download_in_progress = False
        self.download_queue = []
        self.current_feed_data = None
        self.subscriptions = podcasts.load_subscriptions()

        # Estado del iPod
        self.ipod_mount = None
        self.ipod_data = None
        self.ipod_read_running = False

        self._sock = None
        self._start_command_server()

        self._init_mpris()
        self.watched_dirs = self.prefs.get("watched_dirs", [])
        self.file_monitors = []
        self._monitored_paths = set()
        self.playback_context = []
        
        #Detectar y añadir la carpeta de música estándar del sistema
        music_dir = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_MUSIC)
        if not music_dir:
            # Respaldo por si el sistema no la tiene configurada
            music_dir = os.path.expanduser("~/Música")
            if not os.path.isdir(music_dir):
                music_dir = os.path.expanduser("~/Music")
                
        if music_dir and os.path.isdir(music_dir):
            if music_dir not in self.watched_dirs:
                self.watched_dirs.append(music_dir)
                self.prefs["watched_dirs"] = self.watched_dirs
                preferences.save(self.prefs)

        self._setup_watchers()
        self._setup_auto_detect()


        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_border_width(8)
        self.add(outer)

        # Topbar con Overlay: botón a la izquierda, search a la derecha,
        # status centrado geométricamente encima. Se empaqueta con (False, False)
        # para que tome solo su altura natural (no expandirse verticalmente).
        topbar_overlay = Gtk.Overlay()
        outer.pack_start(topbar_overlay, False, False, 0)

        topbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        topbar_overlay.add(topbar)

        self.btn_add_music = Gtk.Button(label=_('Agregar música'))
        self.btn_add_music.connect("clicked", self.on_add_music)
        topbar.pack_start(self.btn_add_music, False, False, 0)

        # Spacer que empuja todo a la derecha
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        topbar.pack_start(spacer, True, True, 0)

        # 1. Menú de hamburguesa (SE EMPAQUETA PRIMERO PARA QUE QUEDE EN EL EXTREMO DERECHO)
        self.menu_btn = Gtk.MenuButton()
        self.menu_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.menu_btn.set_image(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        self.menu_btn.set_tooltip_text(_('Menú principal'))
        
        menu = Gtk.Menu()
        item_manual = Gtk.MenuItem(label=_("Manual de usuario"))
        item_manual.connect("activate", self.on_manual_clicked)
        menu.append(item_manual)
        
        item_about = Gtk.MenuItem(label=_("Acerca de TunePod"))
        item_about.connect("activate", self.on_about_clicked)
        menu.append(item_about)
        
        item_update = Gtk.MenuItem(label=_("Buscar actualizaciones…"))
        item_update.connect("activate", self.on_check_updates)
        menu.append(item_update)
        
        self.menu_btn.set_popup(menu)
        menu.show_all()
        topbar.pack_end(self.menu_btn, False, False, 0)

        # 2. Buscador (SE EMPAQUETA AL FINAL, QUEDA A LA IZQUIERDA DEL MODO OSCURO)
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(_('Buscar…'))
        self.search_entry.set_width_chars(20)
        self.search_entry.connect("search-changed", self.on_search_changed)
        topbar.pack_end(self.search_entry, False, False, 0)

        # 3. Botón de Modo Oscuro/Claro (SE EMPAQUETA DESPUÉS, QUEDA A LA IZQUIERDA DEL MENÚ)
        self.dark_mode = self.prefs.get("dark_mode", False)
        self.dark_btn = Gtk.Button()
        self.dark_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.dark_btn.set_tooltip_text(_('Alternar Modo Oscuro'))
        self.dark_btn.connect("clicked", self.on_toggle_dark_mode)
        topbar.pack_end(self.dark_btn, False, False, 0)

        # Aplicar el estado inicial (claro u oscuro) al arrancar
        self._apply_theme(self.dark_mode)

        # Status: capa superior del Overlay, centrado
        self.status_label = Gtk.Label(label=_('0 pistas'))
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.status_label.set_max_width_chars(60)
        self.status_label.set_halign(Gtk.Align.CENTER)
        self.status_label.set_valign(Gtk.Align.CENTER)
        topbar_overlay.add_overlay(self.status_label)
        topbar_overlay.set_overlay_pass_through(self.status_label, True)

        # Stack para Canciones/Artistas (el switcher se colocará dentro de la pestaña Playlists)
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(150)
        # Botones de navegación propios (RadioButtons agrupados) en lugar
        # de Gtk.StackSwitcher. Necesitamos detectar el clic incluso cuando
        # la pestaña "Álbumes" ya está activa, para forzar la cuadrícula
        # completa de todos los álbumes.
        self.stack_switcher = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.stack_switcher.get_style_context().add_class("linked")
        self.stack_switcher.set_halign(Gtk.Align.CENTER)
        self.stack_switcher.set_margin_bottom(4)

        self._stack_buttons = {}
        _stack_items = [
            ("artists", _("Artistas")),
            ("albums", _("Álbumes")),
            ("songs", _("Canciones")),
            ("ipod", "iPod"),
        ]
        _prev_btn = None
        for _name, _label in _stack_items:
            if _prev_btn is None:
                _btn = Gtk.RadioButton(label=_label)
            else:
                _btn = Gtk.RadioButton.new_with_label_from_widget(_prev_btn, _label)
            _btn.set_mode(False)  # Modo "toggle button", no radio con círculo
            _btn.connect("clicked", lambda w, n=_name: self._on_stack_button_clicked(n))
            self.stack_switcher.pack_start(_btn, False, False, 0)
            self._stack_buttons[_name] = _btn
            _prev_btn = _btn
        self._stack_buttons["artists"].set_active(True)

        # Label "fantasma" para compatibilidad con el código existente
        self.now_playing_label = Gtk.Label(label="—")
        self.now_playing_label.set_no_show_all(True)
        self.now_playing_label.set_visible(False)

        self.import_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.import_box.set_no_show_all(True)
        outer.pack_start(self.import_box, False, False, 0)

        self.import_bar = Gtk.ProgressBar()
        self.import_bar.set_hexpand(True)
        self.import_bar.set_show_text(True)
        self.import_box.pack_start(self.import_bar, True, True, 0)

        self.cancel_btn = Gtk.Button(label=_('Cancelar'))
        self.cancel_btn.connect("clicked", self.on_cancel_import)
        self.import_box.pack_start(self.cancel_btn, False, False, 0)

        # Contenedor con spacing=0 para que el separador quede pegado al
        # borde superior de las pestañas y de la carátula, sin hueco.
        content_wrapper = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.pack_start(content_wrapper, True, True, 0)

        # Línea horizontal continua que une las pestañas con la carátula.
        content_wrapper.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
            False, False, 0)

        self.main_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        content_wrapper.pack_start(self.main_paned, True, True, 0)

        self.left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.main_paned.pack1(self.left_box, True, False)

        self.notebook = Gtk.Notebook()
        self.left_box.pack_start(self.notebook, True, True, 0)

        # ----- Pestaña 1: Biblioteca -----
        # 1. Vista de Canciones
        scroll_songs = Gtk.ScrolledWindow()
        scroll_songs.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        self.store = Gtk.ListStore(str, str, str, str, str, int, int, int, int, str, str, int, str, str, int)
        self.view_store = Gtk.ListStore(str, str, str, str, str, int, int, int, int)

        self.tree = Gtk.TreeView(model=self.view_store)
        self.tree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.tree.connect("row-activated", self.on_row_activated)
        self.tree.connect("button-press-event", self.on_tree_button_press)
        self.tree.connect("key-press-event", self.on_tree_key_press)
        self.tree.set_search_column(0)

        self._column_sort_ids = {}
        for name, col_idx, expand, min_w in (
            (_("Artista"), VCOL_ARTIST, True, 80),
            (_("Álbum"), VCOL_ALBUM, True, 80),
            (_("Título"), VCOL_TITLE, True, 120),
        ):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
            col = Gtk.TreeViewColumn(name, renderer, text=col_idx)
            col.set_resizable(True)
            col.set_min_width(min_w)
            col.set_expand(expand)
            col.set_sort_column_id(col_idx)
            self.tree.append_column(col)
            self._column_sort_ids[col] = col_idx

        rating_renderer = Gtk.CellRendererText()
        self.rating_column = Gtk.TreeViewColumn("★", rating_renderer)
        self.rating_column.set_cell_data_func(rating_renderer, self._render_rating)
        self.rating_column.set_resizable(False)
        self.rating_column.set_min_width(90)
        self.rating_column.set_sort_column_id(VCOL_RATING)
        self.tree.append_column(self.rating_column)

        play_renderer = Gtk.CellRendererText()
        play_renderer.set_property("xalign", 0.5)
        play_col = Gtk.TreeViewColumn("Repr.", play_renderer, text=VCOL_PLAY_COUNT)
        play_col.set_resizable(False)
        play_col.set_min_width(50)
        play_col.set_sort_column_id(VCOL_PLAY_COUNT)
        self.tree.append_column(play_col)

        dur_renderer = Gtk.CellRendererText()
        dur_renderer.set_property("xalign", 1.0)
        dur_col = Gtk.TreeViewColumn("Dur.", dur_renderer, text=VCOL_DURATION)
        dur_col.set_resizable(False)
        dur_col.set_min_width(50)
        dur_col.set_sort_column_id(VCOL_DURATION)
        self.tree.append_column(dur_col)
        self._column_sort_ids[self.rating_column] = VCOL_RATING
        self._column_sort_ids[play_col] = VCOL_PLAY_COUNT
        self._column_sort_ids[dur_col] = VCOL_DURATION
        self.tree.set_reorderable(False)
        self.view_store.connect("rows-reordered", self._on_view_rows_reordered)

        scroll_songs.add(self.tree)

        # 2. Vista de Artistas (NUEVA)
        self.artists_stack = Gtk.Stack()
        
        # 2.1 Lista de artistas
        scroll_artists = Gtk.ScrolledWindow()
        scroll_artists.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.artists_listbox = Gtk.ListBox()
        self.artists_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.artists_listbox.connect("row-activated", self.on_artist_row_activated)
        scroll_artists.add(self.artists_listbox)
        self.artists_stack.add_titled(scroll_artists, "list", "Lista")
        
        # 2.2 Detalle del artista (Discos)
        artist_detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        artist_detail_box.set_border_width(8)
        
        back_artist_btn = Gtk.Button()
        back_artist_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        back_artist_img = Gtk.Image.new_from_icon_name("go-previous-symbolic", Gtk.IconSize.BUTTON)
        back_artist_box.pack_start(back_artist_img, False, False, 0)
        back_artist_box.pack_start(Gtk.Label(label=_('Volver a Artistas')), False, False, 0)
        back_artist_btn.add(back_artist_box)
        back_artist_btn.set_halign(Gtk.Align.START)
        back_artist_btn.connect("clicked", lambda w: self.artists_stack.set_visible_child_name("list"))
        artist_detail_box.pack_start(back_artist_btn, False, False, 0)
        
        self.artist_detail_title = Gtk.Label()
        self.artist_detail_title.set_markup(_("<b><span size='x-large'>Artista</span></b>"))
        self.artist_detail_title.set_halign(Gtk.Align.START)
        artist_detail_box.pack_start(self.artist_detail_title, False, False, 0)
        
        scroll_artist_albums = Gtk.ScrolledWindow()
        scroll_artist_albums.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.artist_albums_flowbox = Gtk.FlowBox()
        self.artist_albums_flowbox.set_valign(Gtk.Align.START)
        self.artist_albums_flowbox.set_max_children_per_line(6)
        self.artist_albums_flowbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scroll_artist_albums.add(self.artist_albums_flowbox)
        artist_detail_box.pack_start(scroll_artist_albums, True, True, 0)
        
        self.artists_stack.add_titled(artist_detail_box, "detail", "Discos")

        # 3. Vista de Álbumes (Cuadrícula y Detalle)
        self.albums_inner_stack = Gtk.Stack()
        
        scroll_albums = Gtk.ScrolledWindow()
        scroll_albums.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.album_flowbox = Gtk.FlowBox()
        self.album_flowbox.set_valign(Gtk.Align.START)
        self.album_flowbox.set_max_children_per_line(6)
        self.album_flowbox.set_row_spacing(15)
        self.album_flowbox.set_column_spacing(15)
        self.album_flowbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.album_flowbox.connect("child-activated", self.on_album_activated)
        scroll_albums.add(self.album_flowbox)
        self.albums_inner_stack.add_titled(scroll_albums, "grid", "Cuadrícula")

        album_detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        album_detail_box.set_border_width(8)
        
        back_btn = Gtk.Button()
        back_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        back_img = Gtk.Image.new_from_icon_name("go-previous-symbolic", Gtk.IconSize.BUTTON)
        back_box.pack_start(back_img, False, False, 0)
        self._album_back_label = Gtk.Label(label=_('Volver a Álbumes'))
        self._album_back_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._album_back_label.set_max_width_chars(40)
        back_box.pack_start(self._album_back_label, False, False, 0)
        back_btn.add(back_box)
        back_btn.set_halign(Gtk.Align.START)
        back_btn.connect("clicked", self._on_album_back_clicked)
        album_detail_box.pack_start(back_btn, False, False, 0)

        self.album_detail_title = Gtk.Label()
        self.album_detail_title.set_markup(_("<b><span size='x-large'>Álbum</span></b>"))
        self.album_detail_title.set_halign(Gtk.Align.START)
        album_detail_box.pack_start(self.album_detail_title, False, False, 0)

        self.album_tracks_store = Gtk.ListStore(str, str, str)
        self.album_tracks_view = Gtk.TreeView(model=self.album_tracks_store)
        self.album_tracks_view.connect("row-activated", self.on_album_track_activated)
        self.album_tracks_view.connect("button-press-event", self.on_album_tracks_button_press)
        
        r_title = Gtk.CellRendererText()
        c_title = Gtk.TreeViewColumn(_("Título"), r_title, text=1)
        c_title.set_expand(True)
        self.album_tracks_view.append_column(c_title)
        
        r_dur = Gtk.CellRendererText()
        r_dur.set_property("xalign", 1.0)
        c_dur = Gtk.TreeViewColumn(_("Dur."), r_dur, text=2)
        self.album_tracks_view.append_column(c_dur)
        
        scroll_tracks = Gtk.ScrolledWindow()
        scroll_tracks.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll_tracks.add(self.album_tracks_view)
        album_detail_box.pack_start(scroll_tracks, True, True, 0)
        
        self.albums_inner_stack.add_titled(album_detail_box, "detail", "Detalle")

        # 4. Pestaña central para el iPod
        self.ipod_stack_page = self._build_ipod_stack_page()

        # 5. Agregar al Stack en el nuevo orden
        self.stack.add_titled(self.artists_stack, "artists", _("Artistas"))
        self.stack.add_titled(self.albums_inner_stack, "albums", _("Álbumes"))
        self.stack.add_titled(scroll_songs, "songs", _("Canciones"))
        self.stack.add_titled(self.ipod_stack_page, "ipod", "iPod")

        # Vista inicial: Artistas
        self.artists_stack.show_all()
        self.stack.set_visible_child_name("artists")

        self.stack.connect("notify::visible-child", self.on_stack_view_changed)

        # Sidebar: split vertical con la lista de playlists arriba y el
        # panel de información del iPod abajo (mitad y mitad).
        sidebar_paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        sidebar_paned.set_position(280)

        # --- Arriba: lista de playlists ---
        playlists_sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        playlists_sidebar.set_border_width(4)

        side_header = Gtk.Label(label=_('Listas'))
        side_header.set_halign(Gtk.Align.START)
        side_header.set_margin_top(2)
        side_header.set_margin_bottom(6)
        side_header.set_margin_start(4)
        playlists_sidebar.pack_start(side_header, False, False, 0)

        self.playlists_listbox = Gtk.ListBox()
        self.playlists_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.playlists_listbox.set_activate_on_single_click(False)
        self.playlists_listbox.connect("row-selected", self.on_playlist_selected)
        self.playlists_listbox.connect("row-activated", self.on_playlist_activated)
        side_scroll = Gtk.ScrolledWindow()
        side_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        side_scroll.add(self.playlists_listbox)
        playlists_sidebar.pack_start(side_scroll, True, True, 0)

        pl_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        pl_toolbar.set_margin_top(4)
        new_pl_btn = Gtk.Button(label="+")
        new_pl_btn.set_tooltip_text(_('Nueva lista'))
        new_pl_btn.connect("clicked", self.on_new_playlist)
        pl_toolbar.pack_start(new_pl_btn, True, True, 0)

        imp_pl_btn = Gtk.Button(label=_('Importar'))
        imp_pl_btn.set_tooltip_text("Importar .pls, .m3u o .m3u8")
        imp_pl_btn.connect("clicked", self.on_import_playlist)
        pl_toolbar.pack_start(imp_pl_btn, True, True, 0)

        smart_btn = Gtk.Button(label=_('Inteligente'))
        smart_btn.set_tooltip_text(_('Nueva lista inteligente (con reglas)'))
        smart_btn.connect("clicked", self.on_new_smart_playlist)
        pl_toolbar.pack_start(smart_btn, True, True, 0)
        playlists_sidebar.pack_start(pl_toolbar, False, False, 0)

        sidebar_paned.pack1(playlists_sidebar, True, True)

        # --- Abajo: información del iPod ---
        ipod_info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        ipod_info_box.set_border_width(8)

        ipod_info_header = Gtk.Label()
        ipod_info_header.set_markup("<b>iPod</b>")
        ipod_info_header.set_halign(Gtk.Align.START)
        ipod_info_box.pack_start(ipod_info_header, False, False, 0)

        self.ipod_info_model = Gtk.Label()
        self.ipod_info_model.set_halign(Gtk.Align.START)
        self.ipod_info_model.set_markup(_('<b>No conectado</b>'))
        ipod_info_box.pack_start(self.ipod_info_model, False, False, 0)

        self.ipod_info_capacity = Gtk.Label()
        self.ipod_info_capacity.set_halign(Gtk.Align.START)
        self.ipod_info_capacity.set_line_wrap(True)
        self.ipod_info_capacity.set_text(
            _('Conecta el iPod por USB y se detectará automáticamente'))
        ipod_info_box.pack_start(self.ipod_info_capacity, False, False, 0)

        self.ipod_info_bar = Gtk.ProgressBar()
        self.ipod_info_bar.set_fraction(0)
        ipod_info_box.pack_start(self.ipod_info_bar, False, False, 0)

        self.ipod_info_usage = Gtk.Label()
        self.ipod_info_usage.set_halign(Gtk.Align.START)
        self.ipod_info_usage.set_line_wrap(True)
        self.ipod_info_usage.set_text("")
        ipod_info_box.pack_start(self.ipod_info_usage, False, False, 0)

        # Botón único de sincronización (siempre automática)
        sync_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        sync_box.set_margin_top(6)
        
        self.btn_sync_all = Gtk.Button()
        sync_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        sync_hbox.set_halign(Gtk.Align.CENTER)
        sync_img = Gtk.Image.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        sync_hbox.pack_start(sync_img, False, False, 0)
        sync_hbox.pack_start(Gtk.Label(label=_('Sincronización global')), False, False, 0)
        self.btn_sync_all.add(sync_hbox)
        self.btn_sync_all.connect("clicked", self.on_sync_entire_library)
        self.btn_sync_all.set_sensitive(False)
        sync_box.pack_start(self.btn_sync_all, False, False, 0)

        self.btn_sync_selection = Gtk.Button()
        sync_sel_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        sync_sel_hbox.set_halign(Gtk.Align.CENTER)
        sync_sel_img = Gtk.Image.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON)
        sync_sel_hbox.pack_start(sync_sel_img, False, False, 0)
        sync_sel_hbox.pack_start(Gtk.Label(label=_('Sincronización selectiva…')), False, False, 0)
        self.btn_sync_selection.add(sync_sel_hbox)
        self.btn_sync_selection.connect("clicked", self.on_sync_selection_to_ipod)
        self.btn_sync_selection.set_sensitive(False)
        sync_box.pack_start(self.btn_sync_selection, False, False, 0)
        
        ipod_info_box.pack_start(sync_box, False, False, 0)

        # Casilla para sincronización automática
        self.chk_auto_sync = Gtk.CheckButton(label=_('Sincronizar automáticamente al conectar'))
        self.chk_auto_sync.set_active(self.prefs.get("auto_sync", False))
        self.chk_auto_sync.connect("toggled", self.on_auto_sync_toggled)
        self.chk_auto_sync.set_margin_top(4)
        ipod_info_box.pack_start(self.chk_auto_sync, False, False, 0)


        # Botones de acción
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.ipod_content_btn = Gtk.Button(label=_('Ver contenido…'))
        self.ipod_content_btn.set_sensitive(False)
        self.ipod_content_btn.connect("clicked", self.on_show_ipod_content)
        btn_box.pack_start(self.ipod_content_btn, True, True, 0)

        self.ipod_eject_btn = Gtk.Button(label=_('Expulsar'))
        self.ipod_eject_btn.set_sensitive(False)
        self.ipod_eject_btn.connect("clicked", self.on_eject_ipod)
        btn_box.pack_start(self.ipod_eject_btn, True, True, 0)

        ipod_info_box.pack_start(btn_box, False, False, 0)

        # Nuevo botón de Restauración (Mantenimiento)
        self.btn_restore_ipod = Gtk.Button(label=_('Restaurar Firmware…'))
        self.btn_restore_ipod.set_sensitive(True)  # <--- AHORA ESTÁ SIEMPRE ACTIVO
        self.btn_restore_ipod.connect("clicked", self.on_restore_ipod_clicked)
        self.btn_restore_ipod.set_margin_top(10)
        ipod_info_box.pack_start(self.btn_restore_ipod, False, False, 0)

        sidebar_paned.pack2(ipod_info_box, False, False)

        # Envolver el stack con el switcher arriba dentro de la pestaña
        playlists_view = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        playlists_view.pack_start(self.stack_switcher, False, False, 0)
        playlists_view.pack_start(self.stack, True, True, 0)

        library_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        library_paned.set_position(220)
        library_paned.pack1(sidebar_paned, False, False)
        library_paned.pack2(playlists_view, True, False)

        self.notebook.append_page(library_paned, Gtk.Label(label=_('Música')))

        # ----- Pestaña 2: Cola -----
        queue_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        queue_page.set_border_width(8)

        queue_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.queue_info_label = Gtk.Label(label=_('La cola está vacía'))
        self.queue_info_label.set_halign(Gtk.Align.START)
        queue_header.pack_start(self.queue_info_label, True, True, 0)
        self.queue_clear_btn = Gtk.Button(label=_('Vaciar cola'))
        self.queue_clear_btn.set_sensitive(False)
        self.queue_clear_btn.connect("clicked", self.on_clear_queue)
        queue_header.pack_end(self.queue_clear_btn, False, False, 0)
        queue_page.pack_start(queue_header, False, False, 0)

        self.queue_store = Gtk.ListStore(str, str, str, str)
        self.queue_view = Gtk.TreeView(model=self.queue_store)
        self.queue_view.connect("row-activated", self.on_queue_double_click)
        self.queue_view.connect("button-press-event", self.on_queue_button_press)
        for i, name in enumerate((_("Título"), _("Artista"), _("Álbum"))):
            r = Gtk.CellRendererText()
            if i == 0:
                r.set_property("ellipsize", Pango.EllipsizeMode.END)
            col = Gtk.TreeViewColumn(name, r, text=i+1)
            col.set_resizable(True)
            if i == 0:
                col.set_expand(True)
            self.queue_view.append_column(col)

        queue_scroll = Gtk.ScrolledWindow()
        queue_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        queue_scroll.add(self.queue_view)
        queue_page.pack_start(queue_scroll, True, True, 0)

        self.notebook.append_page(queue_page, Gtk.Label(label=_('Cola')))

        # ----- Pestaña 3: Podcasts -----
        self.podcasts_page = self._build_podcasts_page()
        self.notebook.append_page(self.podcasts_page, Gtk.Label(label=_('Podcasts')))

        # ----- Panel derecho -----
        self.now_playing_box = self._build_now_playing()
        self.main_paned.pack2(self.now_playing_box, False, False)

        # Los controles de reproducción ahora viven en el panel derecho
        # (se crean dentro de _build_now_playing)
        self._update_shuffle_btn()
        self._update_repeat_btn()

        # Context menu principal
        self.context_menu = Gtk.Menu()

        item_add = Gtk.MenuItem(label=_('Añadir a lista…'))
        item_add.connect("activate", lambda w: self.on_add_to_playlist_clicked(None))
        self.context_menu.append(item_add)

        item_play_next = Gtk.MenuItem(label=_('Reproducir siguiente'))
        item_play_next.connect("activate", self.on_play_next)
        self.context_menu.append(item_play_next)

        item_add_queue = Gtk.MenuItem(label=_('Añadir a la cola'))
        item_add_queue.connect("activate", self.on_add_to_queue)
        self.context_menu.append(item_add_queue)
        item_delete = Gtk.MenuItem(label=_('Eliminar'))
        item_delete.connect("activate", self.on_delete_local_songs)
        self.context_menu.append(item_delete)

        self.context_menu.append(Gtk.SeparatorMenuItem())

        # Mover arriba/abajo solo visible en playlists manuales
        self.ctx_move_up = Gtk.MenuItem(label=_('↑ Mover arriba'))
        self.ctx_move_up.connect("activate", self.on_move_up)
        self.context_menu.append(self.ctx_move_up)

        self.ctx_move_down = Gtk.MenuItem(label=_('↓ Mover abajo'))
        self.ctx_move_down.connect("activate", self.on_move_down)
        self.context_menu.append(self.ctx_move_down)

        self.context_menu.append(Gtk.SeparatorMenuItem())

        self.ctx_song_info = Gtk.MenuItem(label=_('Información de la canción…'))
        self.ctx_song_info.connect("activate", self.on_show_song_info)
        self.context_menu.append(self.ctx_song_info)

        self.ctx_album_info = Gtk.MenuItem(label=_('Información del álbum…'))
        self.ctx_album_info.connect("activate", self.on_show_album_info)
        self.context_menu.append(self.ctx_album_info)

        self.ctx_send_to_ipod = Gtk.MenuItem(label=_('Enviar al iPod…'))
        self.ctx_send_to_ipod.connect("activate", self.on_send_to_ipod_clicked)
        self.context_menu.append(self.ctx_send_to_ipod)
        self.ctx_send_to_ipod.show()

        self.context_menu.show_all()

        # Context menu para la sidebar de listas
        self.pl_context_menu = Gtk.Menu()

        self.pl_menu_edit = Gtk.MenuItem(label=_('Editar reglas'))
        self.pl_menu_edit.connect("activate", lambda w: self.on_edit_smart_playlist(None))
        self.pl_context_menu.append(self.pl_menu_edit)

        self.pl_menu_export = Gtk.MenuItem(label=_('Exportar a .pls…'))
        self.pl_menu_export.connect("activate", self.on_export_playlist_pls)
        self.pl_context_menu.append(self.pl_menu_export)

        self.pl_menu_sync_ipod = Gtk.MenuItem(label=_('Sincronizar al iPod…'))
        self.pl_menu_sync_ipod.connect("activate", self.on_sync_playlist_to_ipod)
        self.pl_context_menu.append(self.pl_menu_sync_ipod)

        self.pl_menu_clear_library = Gtk.MenuItem(label=_('Vaciar biblioteca'))
        self.pl_menu_clear_library.connect("activate", lambda w: self.on_clear(None))
        self.pl_context_menu.append(self.pl_menu_clear_library)

        self.pl_context_menu.append(Gtk.SeparatorMenuItem())

        self.pl_menu_delete = Gtk.MenuItem(label=_('Eliminar lista'))
        self.pl_menu_delete.connect("activate", lambda w: self.on_delete_playlist(None))
        self.pl_context_menu.append(self.pl_menu_delete)

        self.pl_context_menu.show_all()
        self.playlists_listbox.connect("button-press-event", self.on_playlist_button_press)

        # Context menu de la cola
        self.queue_context_menu = Gtk.Menu()
        item_remove = Gtk.MenuItem(label=_('Quitar de la cola'))
        item_remove.connect("activate", self.on_remove_from_queue)
        self.queue_context_menu.append(item_remove)
        self.queue_context_menu.show_all()

        # Context menu del iPod
        self.ipod_context_menu = Gtk.Menu()
        
        self.ipod_info_item = Gtk.MenuItem(label=_('Ver información…'))
        self.ipod_info_item.connect("activate", self.on_show_song_info)
        self.ipod_context_menu.append(self.ipod_info_item)
        
        self.ipod_copy_pc_item = Gtk.MenuItem(label=_('Copiar al PC…'))
        self.ipod_copy_pc_item.connect("activate", self.on_copy_from_ipod)
        self.ipod_context_menu.append(self.ipod_copy_pc_item)
        
        self.ipod_del_item = Gtk.MenuItem(label=_('Eliminar del iPod'))
        self.ipod_del_item.connect("activate", self.on_delete_from_ipod)
        self.ipod_context_menu.append(self.ipod_del_item)
        
        self.ipod_context_menu.show_all()

        # Context menu de episodios de podcast
        self.episodes_context_menu = Gtk.Menu()
        self.episodes_menu_download = Gtk.MenuItem(
            label=_('Descargar selección'))
        self.episodes_menu_download.connect(
            "activate", lambda w: self.on_download_selected())
        self.episodes_context_menu.append(self.episodes_menu_download)
        self.episodes_context_menu.show_all()

        self._load_library()
        self._apply_filters()
        self._rebuild_playlists_listbox()
        self._refresh_queue_view()

        GLib.timeout_add(300, self.update_position)
        GLib.timeout_add(2000, self._scan_system_music_folder)

    # ---------- Agregar música ----------

    def on_add_music(self, btn):
        if self.import_in_progress:
            return
        dialog = Gtk.Dialog(
            title=_('Agregar música'),
            transient_for=self,
            modal=True,
        )
        dialog.add_buttons("Cancelar", Gtk.ResponseType.CANCEL)
        dialog.set_default_size(320, -1)

        box = dialog.get_content_area()
        box.set_spacing(10)
        box.set_border_width(20)

        lbl = Gtk.Label()
        lbl.set_markup(_('<b>¿Qué quieres agregar?</b>'))
        box.pack_start(lbl, False, False, 0)

        btn_files = Gtk.Button(label=_('Archivos de audio'))
        btn_files.set_size_request(-1, 42)
        box.pack_start(btn_files, False, False, 0)

        btn_folder = Gtk.Button(label=_('Carpeta completa'))
        btn_folder.set_size_request(-1, 42)
        box.pack_start(btn_folder, False, False, 0)

        choice = [None]

        def choose_files(w):
            choice[0] = "files"
            dialog.response(Gtk.ResponseType.OK)

        def choose_folder(w):
            choice[0] = "folder"
            dialog.response(Gtk.ResponseType.OK)

        btn_files.connect("clicked", choose_files)
        btn_folder.connect("clicked", choose_folder)

        dialog.show_all()
        response = dialog.run()
        dialog.destroy()

        if response != Gtk.ResponseType.OK or choice[0] is None:
            return

        if choice[0] == "files":
            self.on_add_files(None)
        elif choice[0] == "folder":
            self.on_add_folder(None)

    def _apply_theme(self, use_dark):
        """Aplica el tema oscuro o claro según la preferencia."""
        settings = Gtk.Settings.get_default()
        if settings:
            settings.set_property("gtk-application-prefer-dark-theme", use_dark)
        
        # Crear el proveedor de CSS si no existe
        if not hasattr(self, "_dark_css_provider"):
            self._dark_css_provider = Gtk.CssProvider()
            css = b"""
            window, .background { background-color: #2e3436; color: #eeeeee; }
            .view, treeview.view { background-color: #232729; color: #eeeeee; }
            treeview.view:selected { background-color: #3584e4; color: #ffffff; }
            headerbar, .titlebar, .toolbar { background-color: #232729; color: #eeeeee; border-color: #1b1b1b; }
            button { background-image: none; background-color: #35393b; color: #eeeeee; border-color: #1b1b1b; }
            button:hover { background-color: #43494c; }
            entry, entry.search { background-color: #1e1e1e; color: #eeeeee; border-color: #111111; }
            label { color: #eeeeee; }
            menu, popover, .menu { background-color: #2e3436; color: #eeeeee; border-color: #1b1b1b; }
            menuitem:hover { background-color: #3584e4; color: #ffffff; }
            button.flat { background-color: transparent; border: none; box-shadow: none; }
            button.flat:hover { background-color: rgba(255, 255, 255, 0.1); }
            """
            self._dark_css_provider.load_from_data(css)

        screen = Gdk.Screen.get_default()
        if use_dark:
            # Inyectar el CSS oscuro forzado
            Gtk.StyleContext.add_provider_for_screen(
                screen, self._dark_css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        else:
            # Quitar el CSS forzado para volver al tema claro del sistema
            Gtk.StyleContext.remove_provider_for_screen(screen, self._dark_css_provider)

        # Actualizar el icono del botón
        self.dark_btn.set_label("☀️" if use_dark else "🌙")

    def on_toggle_dark_mode(self, btn):
        """Se ejecuta al pulsar el botón de la luna/sol."""
        self.dark_mode = not self.dark_mode
        self.prefs["dark_mode"] = self.dark_mode
        preferences.save(self.prefs)
        
        self._apply_theme(self.dark_mode)

    def on_about_clicked(self, widget):
        """Muestra la ventana Acerca de."""
        dialog = Gtk.AboutDialog(transient_for=self, modal=True)
        dialog.set_program_name("TunePod")
        dialog.set_version("1.0.6") # <-- Puedes cambiar esto con cada release
        dialog.set_comments(_("Reproductor de música y gestor de iPod para Linux"))
        dialog.set_license_type(Gtk.License.GPL_3_0)
        dialog.set_website("https://github.com/SilvestreParbut/TunePod")
        dialog.set_logo_icon_name("tunepod")
        dialog.run()
        dialog.destroy()

    def on_manual_clicked(self, widget):
        """Muestra el manual de usuario en una ventana."""
        dialog = Gtk.Dialog(title=_("Manual de usuario"), transient_for=self, modal=True)
        dialog.set_default_size(750, 650)
        dialog.add_button(_("Cerrar"), Gtk.ResponseType.CLOSE)
        
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        
        textview = Gtk.TextView()
        textview.set_wrap_mode(Gtk.WrapMode.WORD)
        textview.set_editable(False)
        # Márgenes para que el texto respire y no esté pegado a los bordes
        textview.set_left_margin(20)
        textview.set_right_margin(20)
        textview.set_top_margin(20)
        textview.set_bottom_margin(20)
        
        manual_text = _(
            "¡Bienvenido a TunePod!\n\n"
            "TunePod es tu centro de música y iPod para Linux. Aquí tienes una guía rápida de todo lo que puedes hacer:\n\n"
            "=== BIBLIOTECA DE MÚSICA ===\n"
            "• Añadir música: Usa el botón 'Agregar música' para importar archivos individuales o carpetas enteras.\n"
            "• Organización: Explora tu música por Artistas, Álbumes o Canciones.\n"
            "• Listas de reproducción: Crea listas manuales o listas 'inteligentes' basadas en reglas (ej. valoración > 4 estrellas).\n"
            "• Búsqueda: Usa la barra superior para encontrar canciones al instante.\n\n"
            "=== REPRODUCCIÓN ===\n"
            "• Controles: Play/Pausa, Siguiente/Anterior, Aleatorio y Repetir.\n"
            "• Cola de reproducción: Añade canciones a la cola para escucharlas después.\n"
            "• Letras: Pulsa 'Mostrar letra' para buscar la letra de la canción actual en internet.\n"
            "• Notificaciones: Recibirás notificaciones de escritorio con la carátula al cambiar de canción.\n\n"
            "=== METADATOS Y CARÁTULAS ===\n"
            "• Edición: Haz clic derecho en una canción o álbum y selecciona 'Información' para editar título, artista, etc.\n"
            "• Buscar carátulas: Usa el botón 'Buscar carátula' para descargar arte desde iTunes o Deezer.\n"
            "• Aplicar a todo el álbum: Al editar un álbum, puedes aplicar los cambios a todas sus canciones de golpe.\n\n"
            "=== GESTIÓN DEL IPOD ===\n"
            "• Conexión: Conecta tu iPod por USB y TunePod lo detectará automáticamente.\n"
            "• Sincronización: Usa 'Sincronización global' para mantener tu iPod idéntico a tu PC, o 'Sincronización selectiva' para elegir qué enviar.\n"
            "• Exploración: Haz clic en 'Ver contenido' para examinar los archivos que ya están en el iPod.\n"
            "• Copiar al PC: Puedes extraer música de tu iPod a tu computadora.\n"
            "• Restaurar: Si tu iPod falla, usa 'Restaurar Firmware' (requiere poner el ipod en modo disco, para los modelos de primera a cuarta generación, incluyendo iPod Photo, se requiere un cargador firewire, ).\n\n"
            "=== PODCASTS ===\n"
            "• Descubrir: Busca podcasts por nombre en la pestaña Podcasts.\n"
            "• Suscripciones: Suscríbete y descarga episodios con un solo clic.\n"
            "• Enviar al iPod: Los episodios descargados se pueden transferir directamente a tu iPod.\n\n"
            "=== PERSONALIZACIÓN ===\n"
            "• Modo oscuro: Usa el botón de la luna en la barra superior.\n"
            "• Actualizaciones: Usa el menú (☰) para comprobar si hay nuevas versiones de TunePod.\n"
        )
        textview.get_buffer().set_text(manual_text)
        
        scroll.add(textview)
        dialog.get_content_area().pack_start(scroll, True, True, 0)
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def on_check_updates(self, widget):
        """Busca actualizaciones en GitHub."""
        import urllib.request
        import json
        
        self.status_label.set_text(_("Buscando actualizaciones en GitHub…"))
        
        def worker():
            try:
                url = "https://api.github.com/repos/SilvestreParbut/TunePod/releases/latest"
                req = urllib.request.Request(url, headers={"User-Agent": "TunePod"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read().decode())
                
                latest_tag = data.get("tag_name", "Desconocida")
                html_url = data.get("html_url", "")
                release_name = data.get("name", "")
                
                GLib.idle_add(self._show_update_dialog, latest_tag, html_url, release_name)
            except Exception as e:
                GLib.idle_add(self.status_label.set_text, _f("Error al buscar actualizaciones: {}", str(e)))
        
        threading.Thread(target=worker, daemon=True).start()

    def _show_update_dialog(self, latest_tag, html_url, release_name):
        """Muestra el diálogo con la información de la actualización."""
        self.status_label.set_text(_("Búsqueda de actualizaciones finalizada"))
        
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text=_f("Última versión disponible: {}", latest_tag)
        )
        
        if release_name:
            dialog.format_secondary_text(_f("{} \n\n¿Deseas abrir la página de descarga en tu navegador?", release_name))
        else:
            dialog.format_secondary_text(_("¿Deseas abrir la página de descarga en tu navegador?"))
        
        if dialog.run() == Gtk.ResponseType.OK:
            try:
                subprocess.run(["xdg-open", html_url])
            except Exception:
                pass
        dialog.destroy()

    # ---------- Reordenar con drag & drop ----------

    def _update_move_menu_sensitivity(self):
        stack_child = self.stack.get_visible_child_name()
        pl = None
        if self.current_playlist_id is not None:
            pl = self._get_playlist(self.current_playlist_id)
        reorderable = (stack_child == "songs" and pl is not None and not playlists_mod.is_smart(pl))
        try:
            self.ctx_move_up.set_visible(reorderable)
            self.ctx_move_down.set_visible(reorderable)
        except AttributeError:
            pass

        try:
            if stack_child == "ipod":
                self.ctx_album_info.set_visible(False)
                if self.ipod_notebook.get_current_page() in (0, 1, 2, 3):
                    self.ctx_song_info.set_sensitive(True)
                else:
                    self.ctx_song_info.set_sensitive(False)
            else:
                has_song = self._get_single_selected_uri() is not None
                self.ctx_song_info.set_sensitive(has_song)
                
                artist, album = self._get_selected_album_data()
                if not artist and hasattr(self, "_ctx_artist"):
                    artist = getattr(self, "_ctx_artist", None)
                
                if artist:
                    self.ctx_album_info.set_visible(True)
                    self.ctx_album_info.set_sensitive(True)
                else:
                    self.ctx_album_info.set_visible(False)
        except AttributeError:
            pass

    def _get_selected_album_data(self):
        stack_child = self.stack.get_visible_child_name()
        if stack_child == "artists":
            sel = self.artists_listbox.get_selected_row()
            if sel and hasattr(sel, "artist_name"):
                return sel.artist_name, ""
        elif stack_child == "albums" and self.albums_inner_stack.get_visible_child_name() == "detail":
            album = self.album_detail_title.get_text()
            for row in self.store:
                if row[COL_ALBUM] == album:
                    return row[COL_ARTIST], album
        elif stack_child == "songs":
            uri = self._get_single_selected_uri()
            if uri:
                for row in self.store:
                    if row[COL_URI] == uri:
                        return row[COL_ARTIST], row[COL_ALBUM]
        return None, None

    def on_move_up(self, menu_item):
        if self.current_playlist_id is None:
            return
        pl = self._get_playlist(self.current_playlist_id)
        if pl is None or playlists_mod.is_smart(pl):
            return
        sel = self.tree.get_selection()
        model, paths = sel.get_selected_rows()
        if not paths:
            return
        indices = sorted([int(p.to_string()) for p in paths])
        moved = 0
        for idx in indices:
            it = self.view_store.get_iter(Gtk.TreePath.new_from_string(str(idx)))
            uri = self.view_store.get_value(it, VCOL_URI)
            if uri not in pl["uris"]:
                continue
            old_pos = pl["uris"].index(uri)
            if old_pos == 0:
                continue
            pl["uris"].pop(old_pos)
            pl["uris"].insert(old_pos - 1, uri)
            moved += 1
        if moved:
            playlists_mod.save(self.playlists)
            self._apply_filters()
            self.status_label.set_text(f"{moved} canción(es) movida(s) arriba")

    def on_move_down(self, menu_item):
        if self.current_playlist_id is None:
            return
        pl = self._get_playlist(self.current_playlist_id)
        if pl is None or playlists_mod.is_smart(pl):
            return
        sel = self.tree.get_selection()
        model, paths = sel.get_selected_rows()
        if not paths:
            return
        indices = sorted([int(p.to_string()) for p in paths], reverse=True)
        moved = 0
        for idx in indices:
            it = self.view_store.get_iter(Gtk.TreePath.new_from_string(str(idx)))
            uri = self.view_store.get_value(it, VCOL_URI)
            if uri not in pl["uris"]:
                continue
            old_pos = pl["uris"].index(uri)
            if old_pos >= len(pl["uris"]) - 1:
                continue
            pl["uris"].pop(old_pos)
            pl["uris"].insert(old_pos + 1, uri)
            moved += 1
        if moved:
            playlists_mod.save(self.playlists)
            self._apply_filters()
            self.status_label.set_text(f"{moved} canción(es) movida(s) abajo")

    def _on_view_rows_reordered(self, model, path, it, new_order):
        """Se dispara cuando el usuario reordena filas con drag & drop."""
        if self.current_playlist_id is None:
            return
        pl = self._get_playlist(self.current_playlist_id)
        if pl is None or playlists_mod.is_smart(pl):
            return
        new_uris = []
        row_it = model.get_iter_first()
        while row_it is not None:
            new_uris.append(model.get_value(row_it, VCOL_URI))
            row_it = model.iter_next(row_it)
        if len(new_uris) != len(pl.get("uris", [])):
            return
        if new_uris == pl["uris"]:
            return
        pl["uris"] = new_uris
        playlists_mod.save(self.playlists)
        self.status_label.set_text(f"Lista «{pl['name']}» reordenada")

    def _configure_reorder(self):
        """Modo playlist manual: solo drag & drop, sin sort por cabecera.
        Modo 'Toda la música' o smart playlist: sort por cabecera normal."""
        pl = None
        if self.current_playlist_id is not None:
            pl = self._get_playlist(self.current_playlist_id)
        reorderable = (
            pl is not None
            and not playlists_mod.is_smart(pl)
            and not self.search_query
        )
        if reorderable:
            # ListStore: -2 = UNSORTED (sin default_sort_func)
            self.view_store.set_sort_column_id(-2, Gtk.SortType.ASCENDING)
            # TreeViewColumn: -1 = "usar el sort del modelo" (que es unsorted)
            # -2 NO es válido para columnas; el mínimo es -1.
            for col in self._column_sort_ids.keys():
                col.set_sort_column_id(-1)
            self.tree.set_reorderable(True)
        else:
            for col, orig_id in self._column_sort_ids.items():
                col.set_sort_column_id(orig_id)
            self.view_store.set_sort_column_id(VCOL_POSITION, Gtk.SortType.ASCENDING)
            self.tree.set_reorderable(False)

    def _refresh_queue_view(self):
        self.queue_store.clear()
        
        # OPTIMIZACIÓN: Creamos un diccionario para buscar rápido los datos de cada URI
        store_map = {row[COL_URI]: row for row in self.store}
        
        for uri in self.play_queue:
            row = store_map.get(uri)
            if row:
                self.queue_store.append([
                    uri, row[COL_TITLE], row[COL_ARTIST], row[COL_ALBUM]
                ])
            else:
                self.queue_store.append([uri, "", "", ""])
                
        n = len(self.play_queue)
        if n > 0:
            self.queue_info_label.set_text(f"Cola: {n} canción(es)")
            self.queue_clear_btn.set_sensitive(True)
        else:
            self.queue_info_label.set_text(_('La cola está vacía'))
            self.queue_clear_btn.set_sensitive(False)
    
    def _get_selected_uris(self):
        """Devuelve una lista con las URIs de todas las canciones seleccionadas."""
        stack_child = self.stack.get_visible_child_name()
        uris = []
        if stack_child == "songs":
            sel = self.tree.get_selection()
            model, paths = sel.get_selected_rows()
            for path in paths:
                it = self.view_store.get_iter(path)
                uris.append(self.view_store.get_value(it, VCOL_URI))
        elif stack_child == "albums" and self.albums_inner_stack.get_visible_child_name() == "detail":
            sel = self.album_tracks_view.get_selection()
            model, paths = sel.get_selected_rows()
            for path in paths:
                it = self.album_tracks_store.get_iter(path)
                uris.append(self.album_tracks_store.get_value(it, 0))
        elif stack_child == "albums" and self.albums_inner_stack.get_visible_child_name() == "grid":
            if hasattr(self, "_ctx_artist") and hasattr(self, "_ctx_album"):
                for row in self.store:
                    if row[COL_ARTIST] == self._ctx_artist and row[COL_ALBUM] == self._ctx_album:
                        uris.append(row[COL_URI])
        return uris

    def _get_single_selected_uri(self):
        """Devuelve la URI de la única canción seleccionada, o None."""
        stack_child = self.stack.get_visible_child_name()
        if stack_child == "songs":
            sel = self.tree.get_selection()
            model, paths = sel.get_selected_rows()
            if len(paths) != 1:
                return None
            it = self.view_store.get_iter(paths[0])
            return self.view_store.get_value(it, VCOL_URI)
        elif stack_child == "artists":
            # En la vista de artistas no hay una canción individual seleccionada
            return None
        elif stack_child == "albums" and self.albums_inner_stack.get_visible_child_name() == "detail":
            sel = self.album_tracks_view.get_selection()
            model, paths = sel.get_selected_rows()
            if len(paths) != 1:
                return None
            it = self.album_tracks_store.get_iter(paths[0])
            return self.album_tracks_store.get_value(it, 0)
        return None


    def play_uris(self, uris, start_idx=0):
        if not uris or start_idx >= len(uris):
            return
        self.playback_context = list(uris)
        self.play_queue = list(uris[start_idx+1:])
        self._refresh_queue_view()
        self.play_uri(uris[start_idx])

    def on_play_next(self, menu_item):
        uris = self._get_selected_uris()
        if not uris:
            self.status_label.set_text(_('Selecciona alguna canción primero'))
            return
        for uri in reversed(uris):
            self.play_queue.insert(0, uri)
        self._refresh_queue_view()
        self.status_label.set_text(
            f"{len(uris)} canción(es) se reproducirán a continuación")

    def on_add_to_queue(self, menu_item):
        uris = self._get_selected_uris()
        if not uris:
            self.status_label.set_text(_('Selecciona alguna canción primero'))
            return
        for uri in uris:
            self.play_queue.append(uri)
        self._refresh_queue_view()
        self.status_label.set_text(
            f"{len(uris)} canción(es) añadida(s) a la cola")

    def on_clear_queue(self, btn):
        self.play_queue = []
        self._refresh_queue_view()
        self.status_label.set_text(_('Cola vaciada'))

    def on_queue_double_click(self, tree, path, column):
        it = self.queue_store.get_iter(path)
        uri = self.queue_store.get_value(it, 0)
        idx = self.find_index_by_uri(uri)
        if idx < 0:
            return
        if uri in self.play_queue:
            self.play_queue.remove(uri)
        self._refresh_queue_view()
        self._play_index_raw(idx)

    def on_queue_button_press(self, widget, event):
        if event.button == 3:
            path_info = widget.get_path_at_pos(int(event.x), int(event.y))
            if path_info is not None:
                path = path_info[0]
                sel = widget.get_selection()
                if not sel.path_is_selected(path):
                    sel.unselect_all()
                    sel.select_path(path)
            self.queue_context_menu.popup_at_pointer(event)
            return True
        return False

    def on_remove_from_queue(self, menu_item):
        sel = self.queue_view.get_selection()
        model, paths = sel.get_selected_rows()
        removed = 0
        for path in paths:
            it = self.queue_store.get_iter(path)
            uri = self.queue_store.get_value(it, 0)
            if uri in self.play_queue:
                self.play_queue.remove(uri)
                removed += 1
        self._refresh_queue_view()
        self.status_label.set_text(f"{removed} canción(es) quitada(s) de la cola")

    # ---------- Rating ----------

    def _render_rating(self, col, cell, model, it, data):
        try:
            rating = model.get_value(it, VCOL_RATING)
        except Exception:
            rating = 0
        if rating < 0:
            rating = 0
        if rating > 5:
            rating = 5
        cell.set_property("text", "★" * rating + "☆" * (5 - rating))

    def _set_rating_for_uri(self, uri, rating):
        it = self.store.get_iter_first()
        while it is not None:
            if self.store.get_value(it, COL_URI) == uri:
                self.store.set_value(it, COL_RATING, int(rating))
                break
            it = self.store.iter_next(it)
        it = self.view_store.get_iter_first()
        while it is not None:
            if self.view_store.get_value(it, VCOL_URI) == uri:
                self.view_store.set_value(it, VCOL_RATING, int(rating))
                break
            it = self.view_store.iter_next(it)
        self._save_library()
        stars = "★" * rating + "☆" * (5 - rating) if rating > 0 else "Sin valorar"
        self.status_label.set_text(f"Valoración: {stars}")

    def on_rate_selected(self, menu_item, rating):
        stack_child = self.stack.get_visible_child_name()
        if stack_child != "songs":
            self.status_label.set_text(_('Valorar solo desde la vista Canciones'))
            return
        sel = self.tree.get_selection()
        model, paths = sel.get_selected_rows()
        if not paths:
            return
        for path in paths:
            it = self.view_store.get_iter(path)
            uri = self.view_store.get_value(it, VCOL_URI)
            self._set_rating_for_uri(uri, rating)

    # ---------- Controles ----------

    def _update_shuffle_btn(self):
        self.shuffle_btn.set_image(Gtk.Image.new_from_icon_name("media-playlist-shuffle-symbolic", Gtk.IconSize.BUTTON))
        self.shuffle_btn.set_opacity(1.0 if self.shuffle_enabled else 0.4)
        self.shuffle_btn.set_tooltip_text("Aleatorio: activado" if self.shuffle_enabled else "Aleatorio: desactivado")

    def _update_repeat_btn(self):
        if self.repeat_mode == "one":
            icon_name = "media-playlist-repeat-song-symbolic"
            opacity = 1.0
        elif self.repeat_mode == "all":
            icon_name = "media-playlist-repeat-symbolic"
            opacity = 1.0
        else:
            icon_name = "media-playlist-repeat-symbolic"
            opacity = 0.4
        self.repeat_btn.set_image(Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON))
        self.repeat_btn.set_opacity(opacity)


    def on_shuffle_toggled(self, btn):
        self.shuffle_enabled = not self.shuffle_enabled
        self.shuffle_history = []
        self.shuffle_pool = []
        self.prefs["shuffle"] = self.shuffle_enabled
        preferences.save(self.prefs)
        self._update_shuffle_btn()
        self.status_label.set_text(
            "Shuffle activado" if self.shuffle_enabled else "Shuffle desactivado")

    def on_repeat_toggled(self, btn):
        idx = REPEAT_MODES.index(self.repeat_mode)
        self.repeat_mode = REPEAT_MODES[(idx + 1) % len(REPEAT_MODES)]
        self.prefs["repeat"] = self.repeat_mode
        preferences.save(self.prefs)
        self._update_repeat_btn()
        labels = {"off": "Repetir: desactivado",
                  "all": "Repetir: toda la lista",
                  "one": "Repetir: una canción"}
        self.status_label.set_text(labels[self.repeat_mode])

    # ---------- Now Playing ----------

    def _build_now_playing(self):
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_size_request(320, -1)
        scroll.set_hexpand(False)
        scroll.set_shadow_type(Gtk.ShadowType.NONE)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(12)
        scroll.add(box)

        self.cover_image = Gtk.Image()
        self.cover_image.set_size_request(296, 296)
        placeholder = self._make_square_placeholder(296)
        if placeholder:
            self.cover_image.set_from_pixbuf(placeholder)
        else:
            self.cover_image.set_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
        box.pack_start(self.cover_image, False, False, 0)

        self.np_title = Gtk.Label()
        self.np_title.set_markup(_("<b><span size='large'>—</span></b>"))
        self.np_title.set_ellipsize(Pango.EllipsizeMode.END)
        box.pack_start(self.np_title, False, False, 0)

        # Artista (Clickable)
        self.np_artist_box = Gtk.EventBox()
        self.np_artist = Gtk.Label()
        self.np_artist.set_markup(_("<span size='medium'>—</span>"))
        self.np_artist.set_ellipsize(Pango.EllipsizeMode.END)
        self.np_artist_box.add(self.np_artist)
        self.np_artist_box.connect("button-release-event", self.on_np_artist_clicked)
        box.pack_start(self.np_artist_box, False, False, 0)

        # Álbum (Clickable)
        self.np_album_box = Gtk.EventBox()
        self.np_album = Gtk.Label()
        self.np_album.set_markup(_("<span size='small'>—</span>"))
        self.np_album.set_ellipsize(Pango.EllipsizeMode.END)
        self.np_album_box.add(self.np_album)
        self.np_album_box.connect("button-release-event", self.on_np_album_clicked)
        box.pack_start(self.np_album_box, False, False, 0)

        progress_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.pack_start(progress_box, False, False, 0)

        self.pos_label = Gtk.Label(label="0:00")
        self.pos_label.set_width_chars(5)
        self.pos_label.set_xalign(0)
        progress_box.pack_start(self.pos_label, False, False, 0)

        self.progress = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, 100, 0.1)
        self.progress.set_draw_value(False)
        self.progress.set_hexpand(True)
        self.progress.connect("button-press-event", self.on_progress_pressed)
        self.progress.connect("button-release-event", self.on_progress_released)
        progress_box.pack_start(self.progress, True, True, 0)

        self.dur_label = Gtk.Label(label="0:00")
        self.dur_label.set_width_chars(5)
        self.dur_label.set_xalign(1)
        progress_box.pack_start(self.dur_label, False, False, 0)

        # Controles de reproducción estilo moderno (Shuffle - Prev - Play - Next - Repeat)
        play_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        play_controls.set_margin_top(8)
        play_controls.set_margin_bottom(8)
        box.pack_start(play_controls, False, False, 0)

        self.shuffle_btn = Gtk.Button()
        self.shuffle_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.shuffle_btn.connect("clicked", self.on_shuffle_toggled)
        play_controls.pack_start(self.shuffle_btn, False, False, 0)

        spacer_l = Gtk.Box()
        spacer_l.set_hexpand(True)
        play_controls.pack_start(spacer_l, True, True, 0)

        center_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        center_box.set_halign(Gtk.Align.CENTER)
        play_controls.pack_start(center_box, False, False, 0)

        self.prev_btn = Gtk.Button()
        self.prev_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.prev_btn.set_image(Gtk.Image.new_from_icon_name("media-skip-backward-symbolic", Gtk.IconSize.BUTTON))
        self.prev_btn.connect("clicked", lambda *_: self.play_relative(-1))
        center_box.pack_start(self.prev_btn, False, False, 0)

        self.play_btn = Gtk.Button()
        self.play_btn.get_style_context().add_class("circular-play")
        self.play_btn.set_valign(Gtk.Align.CENTER)
        self.play_btn.set_can_focus(False)
        self._set_play_icon(False)
        self.play_btn.connect("clicked", self.on_play_pause)
        center_box.pack_start(self.play_btn, False, False, 0)

        self.next_btn = Gtk.Button()
        self.next_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.next_btn.set_image(Gtk.Image.new_from_icon_name("media-skip-forward-symbolic", Gtk.IconSize.BUTTON))
        self.next_btn.connect("clicked", lambda *_: self.play_relative(+1))
        center_box.pack_start(self.next_btn, False, False, 0)

        spacer_r = Gtk.Box()
        spacer_r.set_hexpand(True)
        play_controls.pack_start(spacer_r, True, True, 0)

        self.repeat_btn = Gtk.Button()
        self.repeat_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.repeat_btn.connect("clicked", self.on_repeat_toggled)
        play_controls.pack_start(self.repeat_btn, False, False, 0)

        box.pack_start(Gtk.Separator(), False, False, 0)

        self.toggle_lyrics_btn = Gtk.Button(label=_('Mostrar letra'))
        self.toggle_lyrics_btn.connect("clicked", self.on_toggle_lyrics)
        box.pack_start(self.toggle_lyrics_btn, False, False, 0)

        self.lyrics_revealer = Gtk.Revealer()
        self.lyrics_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.lyrics_revealer.set_transition_duration(200)
        self.lyrics_revealer.set_reveal_child(False)

        lyrics_scroll = Gtk.ScrolledWindow()
        lyrics_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        lyrics_scroll.set_size_request(-1, 220)
        self.lyrics_view = Gtk.TextView()
        self.lyrics_view.set_editable(False)
        self.lyrics_view.set_cursor_visible(False)
        self.lyrics_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.lyrics_view.set_left_margin(4)
        self.lyrics_view.set_right_margin(4)
        self.lyrics_view.get_buffer().set_text(LYRICS_DEFAULT)
        lyrics_scroll.add(self.lyrics_view)
        self.lyrics_revealer.add(lyrics_scroll)
        box.pack_start(self.lyrics_revealer, False, False, 0)

        self.np_add_to_pl_btn = Gtk.Button(label=_('Añadir a lista…'))
        self.np_add_to_pl_btn.connect("clicked", self.on_add_current_to_playlist)
        box.pack_start(self.np_add_to_pl_btn, False, False, 0)

        return scroll

    def on_np_artist_clicked(self, widget, event):
        """Muestra los discos del artista actual en la vista de Álbumes,
        sin dejar el nombre en el buscador y sin reconstruir toda la biblioteca."""
        if not self.current_uri:
            return
            
        artist_name = ""
        it = self.store.get_iter_first()
        while it is not None:
            if self.store.get_value(it, COL_URI) == self.current_uri:
                artist_name = self.store.get_value(it, COL_ARTIST) or "(Sin artista)"
                break
            it = self.store.iter_next(it)
        
        if not artist_name:
            return
            
        # 1. Limpiar la barra de búsqueda silenciosamente (sin disparar el filtro global)
        self.search_entry.handler_block_by_func(self.on_search_changed)
        self.search_entry.set_text("")
        self.search_query = ""
        self.search_entry.handler_unblock_by_func(self.on_search_changed)

        # 2. TRUCO DE OPTIMIZACIÓN: Evitar que GTK reconstruya TODA la biblioteca al cambiar de pestaña
        self.albums_grid_dirty = False

        # 3. Cambiar a la pestaña de Álbumes
        self.stack.set_visible_child_name("albums")
        
        # 4. Reconstruir la cuadrícula directamente, pero solo con los discos del artista
        self._rebuild_albums_grid(query=artist_name)
        self.albums_inner_stack.set_visible_child_name("grid")
        
        # 5. Volver a marcar como "sucia" para que si el usuario cambia de pestaña y vuelve, 
        # la cuadrícula se reconstruya mostrando TODOS los discos de nuevo.
        self.albums_grid_dirty = True
        
        # Mensaje informativo en la barra inferior
        self.status_label.set_text(f"Mostrando discos de: {artist_name}")

    def on_np_album_clicked(self, widget, event):
        """Abre directamente el detalle del álbum actual (optimizado para ser instantáneo)."""
        if not self.current_uri:
            return
            
        album_name = ""
        artist_name = ""
        
        # 1. Buscar a qué álbum pertenece la canción actual
        it = self.store.get_iter_first()
        while it is not None:
            if self.store.get_value(it, COL_URI) == self.current_uri:
                album_name = self.store.get_value(it, COL_ALBUM) or "(Sin álbum)"
                artist_name = self.store.get_value(it, COL_ARTIST) or "(Sin artista)"
                break
            it = self.store.iter_next(it)
            
        if not album_name:
            return

        # 2. TRUCO DE OPTIMIZACIÓN: Evitar que GTK reconstruya la cuadrícula entera ahora
        self.albums_grid_dirty = False
        
        # 3. Limpiar el buscador silenciosamente (bloqueando la señal para no disparar búsquedas)
        self.search_entry.handler_block_by_func(self.on_search_changed)
        self.search_entry.set_text("")
        self.search_query = ""
        self.search_entry.handler_unblock_by_func(self.on_search_changed)

        # 4. Cambiar a la pestaña de Álbumes (ahora será instantáneo)
        self.stack.set_visible_child_name("albums")
        
        # 5. Rellenar la lista de canciones de ESTE álbum específico
        self.album_detail_title.set_markup(f"<b><span size='x-large'>{GLib.markup_escape_text(album_name)}</span></b>")
        self.album_tracks_store.clear()
        
        tracks = []
        for row in self.store:
            if (row[COL_ALBUM] or "(Sin álbum)") == album_name and (row[COL_ARTIST] or "(Sin artista)") == artist_name:
                tracks.append(row)
                
        # Ordenar por pista
        tracks.sort(key=lambda r: (int(r[COL_TRACK_NUM]) if int(r[COL_TRACK_NUM]) > 0 else 9999, r[COL_TITLE].lower()))
        
        for r in tracks:
            self.album_tracks_store.append([r[COL_URI], r[COL_TITLE], r[COL_DURATION]])
            
        # 6. Mostrar la vista de detalle inmediatamente
        self.albums_inner_stack.set_visible_child_name("detail")
        self._highlight_playing_in_album_detail()
        
        # 7. Marcar la cuadrícula como "sucia" en segundo plano para cuando el usuario vuelva
        GLib.timeout_add_seconds(1, self._mark_albums_grid_dirty)

    def _mark_albums_grid_dirty(self):
        """Pequeña función auxiliar para que la cuadrícula se reconstruya si el usuario vuelve a ella."""
        self.albums_grid_dirty = True
        return False

    def _set_play_icon(self, is_playing):
        icon = "media-playback-pause-symbolic" if is_playing else "media-playback-start-symbolic"
        img = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
        img.set_pixel_size(36)
        img.set_halign(Gtk.Align.CENTER)
        img.set_valign(Gtk.Align.CENTER)
        self.play_btn.set_image(img)

    def on_toggle_lyrics(self, btn):
        showing = self.lyrics_revealer.get_reveal_child()
        self.lyrics_revealer.set_reveal_child(not showing)
        if showing:
            self.toggle_lyrics_btn.set_label(_("Mostrar letra"))
        else:
            self.toggle_lyrics_btn.set_label(_("Ocultar letra"))
    def _make_square_placeholder(self, size=296):
        """Crea una imagen cuadrada gris con el icono de música centrado.
        Cachea el resultado por tamaño para no regenerarlo en cada rebuild."""
        if not hasattr(self, "_placeholder_cache"):
            self._placeholder_cache = {}
        cached = self._placeholder_cache.get(size)
        if cached is not None:
            return cached
        try:
            pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, size, size)
            pixbuf.fill(0x3A3A3AFF)  # Gris oscuro neutro

            theme = Gtk.IconTheme.get_default()
            icon_info = theme.lookup_icon("audio-x-generic", size // 2, 0)
            if icon_info:
                icon_pixbuf = icon_info.load_icon()
                icon_size = size // 2
                x = (size - icon_size) // 2
                y = (size - icon_size) // 2
                icon_pixbuf.composite(
                    pixbuf, x, y, icon_size, icon_size,
                    x, y, 1.0, 1.0, GdkPixbuf.InterpType.BILINEAR, 255
                )
            self._placeholder_cache[size] = pixbuf
            return pixbuf
        except Exception:
            return None

    def _load_cover_art(self, uri, artist="", album=""):
        fname = ""
        try:
            fname = GLib.filename_from_uri(uri)[0]
        except Exception:
            pass
        if not fname or not os.path.exists(fname):
            GLib.idle_add(self._set_cover_art, None)
            return

        cache_dir = os.path.expanduser("~/.cache/tunepod/covers")
        os.makedirs(cache_dir, exist_ok=True)
        
        # MAGIA: Usar Artista y Álbum como nombre de archivo de caché.
        if artist and album:
            cache_key = f"{artist} - {album}".lower().strip()
        else:
            cache_key = fname
            
        h = hashlib.sha1(cache_key.encode("utf-8")).hexdigest()
        cache_path = os.path.join(cache_dir, h + ".jpg")

        # 1. Intentar cargar desde el caché (es instantáneo)
        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    cache_path, 296, 296, True)
                GLib.idle_add(self._set_cover_art, pixbuf)
                return
            except Exception:
                pass

        # 2. Si NO es el iPod, usar GStreamer (rápido en discos locales)
        is_ipod = False
        if self.ipod_mount and fname.startswith(self.ipod_mount):
            is_ipod = True

        if not is_ipod:
            try:
                info = self.discoverer.discover_uri(uri)
                if info:
                    tags = info.get_tags()
                    if tags:
                        for tag_name in ("image", "preview-image"):
                            if tags.has_tag(tag_name):
                                sample = tags.get_sample(tag_name)
                                if sample:
                                    buf = sample.get_buffer()
                                    success, map_info = buf.map(Gst.MapFlags.READ)
                                    if success:
                                        try:
                                            with open(cache_path, "wb") as f:
                                                f.write(map_info.data)
                                            buf.unmap(map_info)
                                            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                                                cache_path, 296, 296, True)
                                            GLib.idle_add(self._set_cover_art, pixbuf)
                                            return
                                        except Exception:
                                            try: buf.unmap(map_info)
                                            except: pass
            except Exception:
                pass

        # 3. Delegar a _load_cover_pixbuf_for_uri (usa ffmpeg y caché por ruta)
        # Esto garantiza que si el diálogo de información puede extraerla, el reproductor también.
        pixbuf = self._load_cover_pixbuf_for_uri(uri, 296)
        if pixbuf:
            GLib.idle_add(self._set_cover_art, pixbuf)
            # Guardar también en el caché de artista-álbum para el futuro
            try:
                h_path = hashlib.sha1(fname.encode("utf-8")).hexdigest()
                cache_path_path = os.path.join(cache_dir, h_path + ".jpg")
                if os.path.exists(cache_path_path) and not os.path.exists(cache_path):
                    import shutil
                    shutil.copy2(cache_path_path, cache_path)
            except Exception:
                pass
            return

        GLib.idle_add(self._set_cover_art, None)

    def _set_cover_art(self, pixbuf):
        if self.shutting_down:
            return False
        if pixbuf:
            w, h = pixbuf.get_width(), pixbuf.get_height()
            scale = min(296 / w, 296 / h)
            nw, nh = int(w * scale), int(h * scale)
            scaled = pixbuf.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR)
            self.cover_image.set_from_pixbuf(scaled)
        else:
            placeholder = self._make_square_placeholder(296)
            if placeholder:
                self.cover_image.set_from_pixbuf(placeholder)
            else:
                self.cover_image.set_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
    def _load_podcast_cover(self, podcast_title):
        """Carga la carátula del podcast. Corre en un hilo secundario."""
        if not podcast_title:
            GLib.idle_add(self._set_cover_art, None)
            return

        artwork_url = None

        # 1. Buscar en suscripciones comparando por título normalizado
        #    (strip + lowercase). Itera sobre una copia para evitar
        #    problemas si el usuario suscribe/desuscribe mientras tanto.
        pt_norm = (podcast_title or "").strip().lower()
        for s in list(self.subscriptions):
            title = (s.get("title") or "").strip().lower()
            if title == pt_norm:
                artwork_url = s.get("artwork_url") or None
                if artwork_url:
                    break

        # 2. Si no hay en suscripciones, usar el feed cargado actualmente
        #    (sin comparar título: si estamos reproduciendo un episodio,
        #    este es el feed que el usuario está viendo).
        if not artwork_url and self.current_feed_data:
            artwork_url = self.current_feed_data.get("image_url") or None

        if not artwork_url:
            print(f"[podcast_cover] Sin artwork_url para '{podcast_title}'")
            GLib.idle_add(self._set_cover_art, None)
            return

        cache_dir = os.path.expanduser("~/.cache/tunepod/podcast_covers")
        os.makedirs(cache_dir, exist_ok=True)
        h = hashlib.sha1(artwork_url.encode("utf-8")).hexdigest()
        cache_path = os.path.join(cache_dir, h + ".jpg")

        # 3. Intentar desde caché local (rápido, sin red)
        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    cache_path, 296, 296, True)
                GLib.idle_add(self._set_cover_art, pixbuf)
                return
            except Exception:
                pass

        # 4. Descargar la carátula (esto corre en hilo, no bloquea GTK)
        try:
            req = urllib.request.Request(
                artwork_url, headers={"User-Agent": "TunePod/0.1"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            with open(cache_path, "wb") as f:
                f.write(data)
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                cache_path, 296, 296, True)
            GLib.idle_add(self._set_cover_art, pixbuf)
        except Exception as e:
            print(f"[podcast_cover] Error descargando {artwork_url}: {e}")
            GLib.idle_add(self._set_cover_art, None)

    # ---------- Socket ----------

    def _start_command_server(self):
        try:
            if os.path.exists(SOCKET_PATH):
                os.remove(SOCKET_PATH)
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.bind(SOCKET_PATH)
            self._sock.listen(2)
            self._sock.settimeout(0.5)
            threading.Thread(target=self._socket_loop, daemon=True).start()
        except Exception as e:
            print(f"[socket] No se pudo iniciar: {e}")
            self._sock = None

    def _socket_loop(self):
        while not self.shutting_down:
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                data = conn.recv(128).decode("utf-8", "replace").strip()
                if data:
                    GLib.idle_add(self._handle_command, data)
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def _handle_command(self, cmd):
        if self.shutting_down:
            return False
        if cmd == "play-pause":
            self.on_play_pause(None)
        elif cmd == "play":
            if not self.current_uri:
                uris = self._visible_uris()
                if uris:
                    self.play_uri(uris[0])
            else:
                self.playbin.set_state(Gst.State.PLAYING)
                self._set_play_icon(True)
                self._update_mpris_status("Playing")
        elif cmd == "pause":
            self.playbin.set_state(Gst.State.PAUSED)
            self._set_play_icon(False)
            self._update_mpris_status("Paused")
        elif cmd == "next":
            self.play_relative(+1)
        elif cmd == "prev":
            self.play_relative(-1)
        elif cmd == "stop":
            self.playbin.set_state(Gst.State.NULL)
            self._set_play_icon(False)
        return False


    def on_tree_key_press(self, widget, event):
        if event.keyval == Gdk.KEY_space:
            mods = event.state & (
                Gdk.ModifierType.CONTROL_MASK |
                Gdk.ModifierType.MOD1_MASK
            )
            if not mods:
                self.on_play_pause(None)
                return True
        # Alt+↑ / Alt+↓ para mover canciones en playlists manuales
        if event.state & Gdk.ModifierType.MOD1_MASK:
            if event.keyval == Gdk.KEY_Up:
                self.on_move_up(None)
                return True
            if event.keyval == Gdk.KEY_Down:
                self.on_move_down(None)
                return True
        return False

    # ---------- Filtros ----------

    def _get_playlist(self, pl_id):
        for pl in self.playlists:
            if pl["id"] == pl_id:
                return pl
        return None

    def _rebuild_view_store(self):
        # Desactivar sort ANTES de limpiar y rellenar (si no, se reordena solo)
        self.view_store.set_sort_column_id(-2, Gtk.SortType.ASCENDING)
        self.view_store.clear()
        q = self.search_query
        self.current_playlist_uris = None
        pl_uris = None
        smart_rules = None

        if self.current_playlist_id is not None:
            pl = self._get_playlist(self.current_playlist_id)
            if pl:
                if playlists_mod.is_smart(pl):
                    smart_rules = pl.get("rules", [])
                else:
                    pl_uris = pl.get("uris", [])
                    self.current_playlist_uris = set(pl_uris)

        if smart_rules is not None:
            # Obtener el límite de minutos de la lista actual
            limit_minutes = 0
            if self.current_playlist_id is not None:
                pl_actual = self._get_playlist(self.current_playlist_id)
                if pl_actual:
                    limit_minutes = pl_actual.get("limit_minutes", 0)
            
            limit_seconds = limit_minutes * 60
            current_seconds = 0
            
            # Primero recopilamos todas las canciones que cumplen las reglas
            matching_rows = []
            for row in self.store:
                if not playlists_mod.matches_smart(row, smart_rules):
                    continue
                if q:
                    t = (row[COL_TITLE] or "").lower()
                    a = (row[COL_ARTIST] or "").lower()
                    al = (row[COL_ALBUM] or "").lower()
                    if q not in t and q not in a and q not in al:
                        continue
                matching_rows.append(row)

            # Ahora las añadimos hasta llegar al límite de tiempo (si hay límite)
            i = 0
            for row in matching_rows:
                if limit_seconds > 0:
                    dur_str = row[COL_DURATION] # Formato "3:45" o "1:02:03"
                    secs = 0
                    if ":" in dur_str:
                        parts = dur_str.split(":")
                        if len(parts) == 2:
                            secs = int(parts[0]) * 60 + int(parts[1])
                        elif len(parts) == 3:
                            secs = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                    
                    if current_seconds + secs > limit_seconds:
                        break # Cortar si la siguiente canción excede el límite
                    current_seconds += secs

                self.view_store.append([
                    row[COL_TITLE], row[COL_ARTIST], row[COL_ALBUM],
                    row[COL_DURATION], row[COL_URI], row[COL_TRACK_NUM],
                    i, int(row[COL_RATING]), int(row[COL_PLAY_COUNT]),
                ])
                i += 1
        elif pl_uris is not None:
            store_map = {row[COL_URI]: row for row in self.store}
            for i, uri in enumerate(pl_uris):
                if uri in store_map:
                    row = store_map[uri]
                    if q:
                        t = (row[COL_TITLE] or "").lower()
                        a = (row[COL_ARTIST] or "").lower()
                        al = (row[COL_ALBUM] or "").lower()
                        if q not in t and q not in a and q not in al:
                            continue
                    self.view_store.append([
                        row[COL_TITLE], row[COL_ARTIST], row[COL_ALBUM],
                        row[COL_DURATION], row[COL_URI], row[COL_TRACK_NUM],
                        i, int(row[COL_RATING]), int(row[COL_PLAY_COUNT]),
                    ])
        else:
            for i, row in enumerate(self.store):
                if q:
                    t = (row[COL_TITLE] or "").lower()
                    a = (row[COL_ARTIST] or "").lower()
                    al = (row[COL_ALBUM] or "").lower()
                    if q not in t and q not in a and q not in al:
                        continue
                self.view_store.append([
                    row[COL_TITLE], row[COL_ARTIST], row[COL_ALBUM],
                    row[COL_DURATION], row[COL_URI], row[COL_TRACK_NUM],
                    i, int(row[COL_RATING]), int(row[COL_PLAY_COUNT]),
                ])

        self._configure_reorder()

    def _apply_filters(self):
        self._rebuild_view_store()
        self._update_status_label()
        
        view_name = self.stack.get_visible_child_name()
        if view_name == "artists":
            self._rebuild_artists_view(query=self.search_query)
            self.artist_tree_dirty = False
        else:
            self.artist_tree_dirty = True
            
        self.albums_grid_dirty = True
        if view_name == "albums":
            self._rebuild_albums_grid(query=self.search_query)
            self.albums_grid_dirty = False

        # Restaurar la selección de la canción que está sonando
        if view_name == "songs" and self.current_uri:
            self._select_song_row_by_uri(self.current_uri)

    def _on_stack_button_clicked(self, name):
        """Handler de los botones superiores (Artistas/Álbumes/Canciones/iPod).
        El botón 'Álbumes' SIEMPRE muestra la cuadrícula completa con todos
        los álbumes de la biblioteca, incluso si ya estábamos viendo el
        detalle de un álbum."""
        # Guard: si estamos dentro de _sync_stack_buttons, no hacer nada.
        # GTK puede emitir 'clicked' al llamar set_active(True), lo que
        # reentraría aquí y provocaría RecursionError.
        if getattr(self, "_syncing_stack_buttons", False):
            return

        if name == "albums":
            # Limpiar búsqueda silenciosamente (sin disparar filtro global)
            self.search_entry.handler_block_by_func(self.on_search_changed)
            self.search_entry.set_text("")
            self.search_query = ""
            self.search_entry.handler_unblock_by_func(self.on_search_changed)

            # Forzar la cuadrícula con TODOS los álbumes
            self.albums_inner_stack.set_visible_child_name("grid")
            self._rebuild_albums_grid(query="")
            self.albums_grid_dirty = False
            self.status_label.set_text(_('Mostrando todos los álbumes'))

            # Resetear el contexto del botón interno "Volver a [artista]"
            self._album_back_context = None
            if hasattr(self, "_album_back_label"):
                self._album_back_label.set_text(_("Volver a Álbumes"))

        if self.stack.get_visible_child_name() != name:
            self.stack.set_visible_child_name(name)
        # NO llamamos _sync_stack_buttons aquí: el clic ya activa el botón
        # visualmente por GTK. La sincronización se hace en
        # on_stack_view_changed, que es el punto único para cambios
        # programáticos del stack.

    def _sync_stack_buttons(self, active_name):
        """Sincroniza visualmente los botones superiores cuando el stack
        cambia programáticamente (p. ej. desde Now Playing o al abrir un
        álbum). Usa un guard para evitar recursión si set_active emite
        'clicked'."""
        if not hasattr(self, "_stack_buttons"):
            return
        if getattr(self, "_syncing_stack_buttons", False):
            return
        btn = self._stack_buttons.get(active_name)
        if btn is None or btn.get_active():
            return
        self._syncing_stack_buttons = True
        try:
            btn.set_active(True)
        finally:
            self._syncing_stack_buttons = False

    def on_stack_view_changed(self, stack, pspec):
        name = stack.get_visible_child_name()
        self._sync_stack_buttons(name)
        if name == "artists" and self.artist_tree_dirty:
            self._rebuild_artists_view(query=self.search_query)
            self.artist_tree_dirty = False
        elif name == "albums" and self.albums_grid_dirty:
            self._rebuild_albums_grid(query=self.search_query)
            self.albums_grid_dirty = False
        # Marcar la canción que está sonando al cambiar de vista
        if name == "songs" and self.current_uri:
            self._select_song_row_by_uri(self.current_uri)
        elif (name == "albums"
                and self.albums_inner_stack.get_visible_child_name() == "detail"):
            self._highlight_playing_in_album_detail()

    def _update_status_label(self):
        n_total = len(self.store)
        n_visible = len(self.view_store)
        if self.current_playlist_id is not None:
            pl = self._get_playlist(self.current_playlist_id)
            if pl:
                self.status_label.set_text(
                    _f("Lista «{}»: {}/{} pistas", pl['name'], n_visible, n_total))
                return
        if self.search_query:
            self.status_label.set_text(_f("{}/{} pistas", n_visible, n_total))
        else:
            self.status_label.set_text(_f("{} pistas", n_total))

    def on_search_changed(self, entry):
        if self.search_timeout_id:
            GLib.source_remove(self.search_timeout_id)
        self.search_timeout_id = GLib.timeout_add(250, self._apply_search)

    def _apply_search(self):
        self.search_timeout_id = 0
        self.search_query = self.search_entry.get_text().lower().strip()
        self._apply_filters()
        if self.ipod_data:
            self._refresh_ipod_views() # <--- NUEVO: Refrescar el iPod también
        return False

    # ---------- Sidebar listas ----------

    def _rebuild_playlists_listbox(self, select_id=None):
        for row in self.playlists_listbox.get_children():
            self.playlists_listbox.remove(row)

        row = Gtk.ListBoxRow()
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        hbox.set_margin_start(4)
        hbox.set_margin_end(4)
        hbox.set_margin_top(4)
        hbox.set_margin_bottom(4)
        img = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic", Gtk.IconSize.MENU)
        hbox.pack_start(img, False, False, 0)
        hbox.pack_start(Gtk.Label(label=_('Toda la música'), xalign=0), True, True, 0)
        row.add(hbox)
        row.playlist_id = None
        row.set_tooltip_text(_('Toda la biblioteca'))
        self.playlists_listbox.add(row)

        for pl in self.playlists:
            row = Gtk.ListBoxRow()
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            hbox.set_margin_start(4)
            hbox.set_margin_end(4)
            hbox.set_margin_top(4)
            hbox.set_margin_bottom(4)
            
            if playlists_mod.is_smart(pl):
                icon_name = "starred-symbolic"
                tooltip = "Lista inteligente (reglas automáticas)"
            else:
                icon_name = "view-list-symbolic"
                tooltip = f"{len(pl.get('uris', []))} pistas"
                
            img = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU)
            hbox.pack_start(img, False, False, 0)
            hbox.pack_start(Gtk.Label(label=pl['name'], xalign=0), True, True, 0)
            
            row.add(hbox)
            row.playlist_id = pl["id"]
            row.is_smart = playlists_mod.is_smart(pl)
            row.set_tooltip_text(tooltip)
            self.playlists_listbox.add(row)

            # NUEVO: Seleccionar automáticamente si es la que acabamos de crear
            if select_id is not None and pl["id"] == select_id:
                self.playlists_listbox.select_row(row)

        # NUEVO: Forzar a GTK a dibujar los widgets nuevos inmediatamente
        self.playlists_listbox.show_all()

    def on_playlist_button_press(self, widget, event):
        if event.button != 3:
            return False
        row = widget.get_row_at_y(int(event.y))
        if row is None:
            return False
        pl_id = getattr(row, "playlist_id", None)

        widget.select_row(row)

        if pl_id is None:
            # "Toda la música"
            self.pl_menu_edit.set_visible(False)
            self.pl_menu_delete.set_visible(False)
            self.pl_menu_export.set_visible(True)
            self.pl_menu_export.set_sensitive(len(self.store) > 0)
            self.pl_menu_clear_library.set_visible(True)
            self.pl_menu_clear_library.set_sensitive(len(self.store) > 0)
            self.pl_menu_sync_ipod.set_visible(False)
        else:
            pl = self._get_playlist(pl_id)
            if pl is None:
                return False
            is_smart = playlists_mod.is_smart(pl)
            self.pl_menu_edit.set_visible(True)
            self.pl_menu_edit.set_sensitive(is_smart)
            self.pl_menu_export.set_visible(True)
            self.pl_menu_export.set_sensitive(True)
            self.pl_menu_delete.set_visible(True)
            self.pl_menu_delete.set_sensitive(True)
            self.pl_menu_clear_library.set_visible(False)

            # Sincronizar al iPod: solo para playlists manuales,
            # solo si hay un iPod cargado, y solo si tiene canciones.
        self.pl_menu_sync_ipod.set_visible(True)
        if is_smart:
            can_sync = self.ipod_data is not None
        else:
            can_sync = (
                self.ipod_data is not None
                and len(pl.get("uris", [])) > 0
            )
        self.pl_menu_sync_ipod.set_sensitive(can_sync)

        self.pl_context_menu.popup_at_pointer(event)
        return True

    def _get_playlist_uris_for_sync(self, pl):
        """Devuelve la lista de URIs actual de una playlist.
        Para listas manuales, las uris guardadas.
        Para listas inteligentes, las canciones que cumplen las reglas
        AHORA MISMO, respetando el límite de minutos si lo hay."""
        if not playlists_mod.is_smart(pl):
            return list(pl.get("uris", []))
        rules = pl.get("rules", [])
        limit_minutes = pl.get("limit_minutes", 0)
        limit_seconds = limit_minutes * 60
        current_seconds = 0
        result = []
        for row in self.store:
            if not playlists_mod.matches_smart(row, rules):
                continue
            if limit_seconds > 0:
                dur_str = row[COL_DURATION]
                secs = 0
                if ":" in dur_str:
                    parts = dur_str.split(":")
                    if len(parts) == 2:
                        secs = int(parts[0]) * 60 + int(parts[1])
                    elif len(parts) == 3:
                        secs = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                if current_seconds + secs > limit_seconds:
                    break
                current_seconds += secs
            result.append(row[COL_URI])
        return result


    def on_sync_playlist_to_ipod(self, menu_item):
        """Sincroniza la playlist actual al iPod, recreándola desde cero.
        Nunca hace append ciego. Backup automático antes de escribir."""
        if self.ipod_data is None or self.ipod_mount is None:
            self.status_label.set_text(
                _("Primero conecta el iPod y pulsa 'Buscar iPod' en la pestaña iPod"))
            return

        pl = self._get_playlist(self.current_playlist_id)
        if pl is None:
            self.status_label.set_text(_('Selecciona una playlist primero'))
            return

        uris = self._get_playlist_uris_for_sync(pl)
        if not uris:
            self.status_label.set_text(_('La playlist está vacía'))
            return

        # Recopilar metadatos locales de cada URI
        local_tracks = []
        store_by_uri = {}
        for row in self.store:
            store_by_uri[row[COL_URI]] = row

        for uri in uris:
            row = store_by_uri.get(uri)
            if row is None:
                continue
            local_tracks.append({
                "uri": uri,
                "title": row[COL_TITLE] or "",
                "artist": row[COL_ARTIST] or "",
                "album": row[COL_ALBUM] or "",
                "duration_str": row[COL_DURATION] or "",
            })

        if not local_tracks:
            self.status_label.set_text(
                _('Ninguna de las canciones de esta playlist está en la biblioteca'))
            return

        self.status_label.set_text(
            f"Buscando coincidencias en el iPod ({len(local_tracks)} canciones)…")

        def worker():
            ipod_tracks = self.ipod_data.get("tracks", [])
            matched, unmatched = ipod_sync.match_tracks(
                local_tracks, ipod_tracks)
            
            # NUEVO: Pasar por el convertidor antes de mostrar la confirmación
            GLib.idle_add(
                self._check_and_convert_tracks, unmatched, 
                lambda new_unmatched: self._show_sync_confirmation(pl, matched, new_unmatched)
            )

        threading.Thread(target=worker, daemon=True).start()

    def _show_sync_confirmation(self, playlist, matched, unmatched):
        """Diálogo de confirmación. Ofrece copiar al iPod si hay canciones
        que no están en él todavía."""
        name = playlist["name"]
        n_matched = len(matched)
        n_unmatched = len(unmatched)
        n_total = n_matched + n_unmatched

        dialog = Gtk.Dialog(
            title=_('Sincronizar al iPod'),
            transient_for=self,
            modal=True,
        )
        dialog.set_default_size(600, -1)

        # Botones: variable según haya o no canciones no encontradas
        if n_unmatched > 0 and n_matched > 0:
            dialog.add_buttons(
                "Cancelar", Gtk.ResponseType.CANCEL,
                "Solo las que ya están", Gtk.ResponseType.NO,
                f"Copiar {n_unmatched} y sincronizar", Gtk.ResponseType.YES,
            )
        elif n_unmatched > 0 and n_matched == 0:
            dialog.add_buttons(
                "Cancelar", Gtk.ResponseType.CANCEL,
                f"Copiar {n_unmatched} al iPod", Gtk.ResponseType.YES,
            )
        else:
            dialog.add_buttons(
                "Cancelar", Gtk.ResponseType.CANCEL,
                "Sincronizar", Gtk.ResponseType.OK,
            )

        area = dialog.get_content_area()
        area.set_spacing(10)
        area.set_border_width(14)

        hdr = Gtk.Label()
        hdr.set_markup(
            f"<b>Sincronizar «{GLib.markup_escape_text(name)}» al iPod</b>")
        hdr.set_halign(Gtk.Align.START)
        area.pack_start(hdr, False, False, 0)

        summary = Gtk.Label()
        summary.set_markup(
            f"<b>{n_matched}</b> de <b>{n_total}</b> canciones están ya en el iPod.\n"
            f"<b>{n_unmatched}</b> no están y se pueden copiar.")
        summary.set_halign(Gtk.Align.START)
        summary.set_line_wrap(True)
        area.pack_start(summary, False, False, 0)

        # Resumen por extensión de las no encontradas
        if n_unmatched > 0:
            exts = {}
            for lt in unmatched:
                uri = lt.get("uri", "")
                ext = uri.rsplit(".", 1)[-1].lower() if "." in uri else "?"
                exts[ext] = exts.get(ext, 0) + 1
            ext_summary = ", ".join(
                f"{n} {ext.upper()}" for ext, n in sorted(exts.items()))
            ext_lbl = Gtk.Label()
            ext_lbl.set_markup(
                f"<span size='small'>A copiar: {ext_summary}</span>")
            ext_lbl.set_halign(Gtk.Align.START)
            area.pack_start(ext_lbl, False, False, 0)

        if n_unmatched > 0:
            warning = Gtk.Label()
            warning.set_markup(
                "<span foreground='#c06000'>Las canciones copiadas se añadirán "
                "a la biblioteca del iPod y a esta playlist.</span>")
            warning.set_halign(Gtk.Align.START)
            warning.set_line_wrap(True)
            warning.set_max_width_chars(70)
            area.pack_start(warning, False, False, 0)

        # Expandible con la lista de no encontradas
        if n_unmatched > 0:
            expander = Gtk.Expander(
                label=f"Ver las {n_unmatched} canciones a copiar")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_border_width(6)
            for lt in unmatched[:60]:
                lbl = Gtk.Label(
                    label=f"  {lt['artist']} — {lt['title']}",
                    xalign=0)
                lbl.set_ellipsize(Pango.EllipsizeMode.END)
                box.pack_start(lbl, False, False, 0)
            if n_unmatched > 60:
                box.pack_start(
                    Gtk.Label(label=f"  … y {n_unmatched - 60} más", xalign=0),
                    False, False, 0)
            expander.add(box)
            area.pack_start(expander, False, False, 0)

        # Nota de idempotencia
        sec = Gtk.Label()
        sec.set_markup(
            "<span size='small'>La playlist en el iPod se borrará y se "
            "recreará desde cero. Es idempotente: sincronizar varias veces "
            "no duplica canciones.</span>")
        sec.set_halign(Gtk.Align.START)
        sec.set_line_wrap(True)
        sec.set_max_width_chars(70)
        area.pack_start(sec, False, False, 0)

        dialog.show_all()
        response = dialog.run()
        dialog.destroy()

        if response == Gtk.ResponseType.CANCEL:
            return

        if response == Gtk.ResponseType.YES:
            # Copiar las no encontradas al iPod, luego sincronizar todas
            self._do_copy_and_sync(playlist, matched, unmatched)
            return

        if response in (Gtk.ResponseType.OK, Gtk.ResponseType.NO):
            # Solo sincronizar las que ya están
            self._do_sync_playlist(playlist, matched)
            return

    def _do_copy_and_sync(self, playlist, matched_before, unmatched):
        mount = self.ipod_mount
        name = playlist["name"]

        resp = self._ask_backup()
        if resp == Gtk.ResponseType.CANCEL: 
            return
            
        stamp = ""
        if resp == Gtk.ResponseType.YES:
            import shutil, datetime
            backup_dir = os.path.expanduser("~/tunepod/backups")
            os.makedirs(backup_dir, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            try:
                src_db = os.path.join(mount, "iPod_Control", "iTunes", "iTunesDB")
                shutil.copy2(src_db, os.path.join(backup_dir, f"iTunesDB-antes-sync-{stamp}"))
            except Exception as e:
                self.status_label.set_text(f"Error backup: {e}. Abortando.")
                return

        tracks_to_copy = []
        for lt in unmatched:
            uri = lt["uri"]
            if uri.lower().endswith((".flac", ".ogg", ".wma")):
                try:
                    fname = GLib.filename_from_uri(uri)[0]
                    new_path = self._convert_to_alac(fname)
                    if new_path:
                        uri = GLib.filename_to_uri(new_path, None)
                    else:
                        continue
                except Exception:
                    continue
                    
            meta = ipod_copy.build_track_dict(uri, playlist_name=name)
            if meta:
                meta["local_path"] = GLib.filename_from_uri(uri)[0]
                tracks_to_copy.append(meta)
            else:
                print(f"[sync] no se pudo preparar {lt['uri']}")

        if not tracks_to_copy:
            self.status_label.set_text(
                _('No se pudo preparar ninguna canción para copiar'))
            return

        self.status_label.set_text(
            _f("Sincronizando {} canciones al iPod…", len(tracks_to_copy)))

        def worker():
            ok, out, err = ipod_copy.copy_tracks(
                mount, tracks_to_copy, dry_run=False,
                progress_callback=lambda line: GLib.idle_add(
                    self.status_label.set_text, f"Sincronizando: {line}"))
            GLib.idle_add(
                self._on_copy_done, playlist, matched_before, out, err, stamp)

        threading.Thread(target=worker, daemon=True).start()

    def _on_copy_done(self, playlist, matched_before, out, err, backup_stamp):
        """Se ejecuta cuando termina la copia. Relee el iPod y sincroniza."""
        if err:
            self.status_label.set_text(
                f"Error al copiar: {err[:250]}")
            return False

        self.status_label.set_text(
            _('Copia terminada. Releyendo iPod y sincronizando playlist…'))

        # Forzar sync del filesystem
        try:
            os.sync()
        except Exception:
            pass

        def reload_and_sync():
            data, rerr = ipod.read_ipod(self.ipod_mount)
            GLib.idle_add(
                self._on_reload_for_sync, playlist, data, rerr, backup_stamp)

        threading.Thread(target=reload_and_sync, daemon=True).start()
        return False

    def _on_reload_for_sync(self, playlist, data, err, backup_stamp):
        """Ya tenemos el iPod releído. Rehacer matching y sincronizar."""
        if err:
            self.status_label.set_text(
                f"Copia hecha, pero falló releer el iPod: {err}")
            return False

        # Actualizar estado del iPod
        self.ipod_data = data

        # Rehacer matching
        name = playlist["name"]
        uris = self._get_playlist_uris_for_sync(playlist)
        store_by_uri = {}
        for row in self.store:
            store_by_uri[row[COL_URI]] = row

        local_tracks = []
        for uri in uris:
            row = store_by_uri.get(uri)
            if row is None:
                continue
            local_tracks.append({
                "uri": uri,
                "title": row[COL_TITLE] or "",
                "artist": row[COL_ARTIST] or "",
                "album": row[COL_ALBUM] or "",
                "duration_str": row[COL_DURATION] or "",
            })

        matched, still_unmatched = ipod_sync.match_tracks(
            local_tracks, data.get("tracks", []))

        if still_unmatched:
            self.status_label.set_text(
                f"Aviso: {len(still_unmatched)} canciones no se pudieron "
                f"identificar tras la copia. Sincronizando solo las "
                f"{len(matched)} encontradas.")
        else:
            self.status_label.set_text(
                f"Todas las canciones identificadas. "
                f"Sincronizando «{name}» ({len(matched)} pistas)…")

        # Sincronizar la playlist con las matched
        self._do_sync_playlist_after_copy(playlist, matched, backup_stamp)
        return False

    def _do_sync_playlist(self, playlist, matched, backup_stamp=None):
        """Ejecuta la sincronización real: backup (si no hay uno ya) + write."""
        import shutil
        import datetime

        mount = self.ipod_mount
        name = playlist["name"]
        track_ids = [tid for (_uri, tid) in matched]

        # Backup automático solo si no se hizo ya
        if backup_stamp is None:
            backup_dir = os.path.expanduser("~/tunepod/backups")
            os.makedirs(backup_dir, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            try:
                src_db = os.path.join(
                    mount, "iPod_Control", "iTunes", "iTunesDB")
                dst_db = os.path.join(
                    backup_dir, f"iTunesDB-antes-sync-{stamp}")
                shutil.copy2(src_db, dst_db)

                src_pc = os.path.join(
                    mount, "iPod_Control", "iTunes", "Play Counts")
                if os.path.exists(src_pc):
                    dst_pc = os.path.join(
                        backup_dir, f"PlayCounts-antes-sync-{stamp}")
                    shutil.copy2(src_pc, dst_pc)
            except Exception as e:
                self.status_label.set_text(f"Error al hacer backup: {e}. Abortando.")
                return
        else:
            stamp = backup_stamp

        self.status_label.set_text(
            f"Sincronizando «{name}» al iPod ({len(track_ids)} pistas)…")

        def worker():
            out, err = ipod_write.sync_playlist(mount, name, track_ids)
            try:
                os.sync()
            except Exception:
                pass
            GLib.idle_add(self._on_sync_done, playlist, len(track_ids),
                          out, err, stamp)

        threading.Thread(target=worker, daemon=True).start()

    def _do_sync_playlist_after_copy(self, playlist, matched, backup_stamp):
        """Sincroniza sin volver a hacer backup (ya se hizo en la copia)."""
        name = playlist["name"]
        track_ids = [tid for (_uri, tid) in matched]
        mount = self.ipod_mount

        def worker():
            out, err = ipod_write.sync_playlist(mount, name, track_ids)
            try:
                os.sync()
            except Exception:
                pass
            GLib.idle_add(self._on_sync_done, playlist, len(track_ids),
                          out, err, backup_stamp)

        threading.Thread(target=worker, daemon=True).start()

    def _on_sync_done(self, playlist, n_tracks, out, err, backup_stamp):
        name = playlist["name"]
        if err:
            self.status_label.set_text(f"Error al sincronizar: {err[:200]}")
            return False

        # Refrescar el estado del iPod
        def reload():
            data, rerr = ipod.read_ipod(self.ipod_mount)
            if rerr:
                GLib.idle_add(
                    self.status_label.set_text,
                    f"Sincronizado, pero falló releer el iPod: {rerr}")
                return
            GLib.idle_add(self._on_ipod_read,
                          self.ipod_mount, data, None)

        self.status_label.set_text(
            f"«{name}» sincronizada al iPod ({n_tracks} pistas). "
            f"Backup: {backup_stamp}")
        threading.Thread(target=reload, daemon=True).start()
        return False

    def on_export_playlist_pls(self, menu_item):
        """Exporta la lista actual (o lo visible en ella) a un archivo .pls."""
        # Determinar nombre y uris
        if self.current_playlist_id is None:
            playlist_name = "Toda la música"
            # Exportar lo que está visible en view_store (respeta búsqueda)
            uris = self._visible_uris()
        else:
            pl = self._get_playlist(self.current_playlist_id)
            if pl is None:
                return
            playlist_name = pl["name"]
            if playlists_mod.is_smart(pl):
                # Smart playlist: exportar el resultado actual
                uris = self._visible_uris()
            else:
                # Manual: exportar uris tal cual
                uris = list(pl.get("uris", []))

        if not uris:
            self.status_label.set_text(_('La lista está vacía, no hay nada que exportar'))
            return

        # Construir lookup {uri: (titulo, duracion_ms_aprox)} para enriquecer el .pls
        lookup = {}
        for row in self.store:
            uri = row[COL_URI]
            title = row[COL_TITLE]
            # Intentar convertir duration_display ("3:45") a milisegundos
            dur_ms = None
            try:
                d = row[COL_DURATION]
                if ":" in d:
                    parts = d.split(":")
                    if len(parts) == 2:
                        dur_ms = (int(parts[0]) * 60 + int(parts[1])) * 1000
                    elif len(parts) == 3:
                        dur_ms = (int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])) * 1000
            except Exception:
                dur_ms = None
            lookup[uri] = (title, dur_ms)

        # File chooser para el destino
        chooser = Gtk.FileChooserDialog(
            title="Exportar lista a .pls",
            parent=self,
            action=Gtk.FileChooserAction.SAVE,
        )
        chooser.add_buttons("Cancelar", Gtk.ResponseType.CANCEL,
                            "Exportar", Gtk.ResponseType.OK)
        chooser.set_do_overwrite_confirmation(True)
        # Nombre sugerido, saneado
        safe_name = "".join(c if c.isalnum() or c in " -_" else "_" for c in playlist_name)
        chooser.set_current_name(safe_name.strip() + ".pls")

        filt = Gtk.FileFilter()
        filt.set_name("Listas de reproducción (.pls)")
        filt.add_pattern("*.pls")
        chooser.add_filter(filt)

        if chooser.run() == Gtk.ResponseType.OK:
            paths = chooser.get_filenames()
            chooser.destroy()
            self.start_import(paths)

    def on_playlist_selected(self, listbox, row):
        if row is None:
            return
        new_id = getattr(row, "playlist_id", None)
        if new_id == self.current_playlist_id:
            return
        self.current_playlist_id = new_id
        self.shuffle_history = []
        self.shuffle_pool = []
        self.playback_context = []
        self._apply_filters()

    def on_playlist_activated(self, listbox, row):
        """Se ejecuta al hacer doble clic en una lista de la barra lateral."""
        if row is None:
            return

        # 1. Nos aseguramos de que la lista esté seleccionada para que la vista se actualice
        self.playlists_listbox.select_row(row)
        
        # 2. Obtener todas las canciones visibles de esta lista
        uris = self._visible_uris()
        if not uris:
            self.status_label.set_text(_('La lista está vacía o no tiene canciones para reproducir.'))
            return
            
        # 3. Iniciar la reproducción desde la primera canción
        self.play_uris(uris, 0)
        
        # Mensaje de confirmación
        pl_id = getattr(row, "playlist_id", None)
        if pl_id is None:
            self.status_label.set_text(f"Reproduciendo toda la biblioteca ({len(uris)} canciones)")
        else:
            pl = self._get_playlist(pl_id)
            if pl:
                self.status_label.set_text(f"Reproduciendo lista «{pl['name']}» ({len(uris)} canciones)")

    def on_new_playlist(self, btn):
        dialog = Gtk.Dialog(
            title=_('Nueva lista de reproducción'),
            transient_for=self, modal=True,
        )
        dialog.add_buttons("Cancelar", Gtk.ResponseType.CANCEL,
                           "Crear", Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(8)
        box.set_border_width(10)
        label = Gtk.Label(label=_('Nombre de la lista:'))
        label.set_halign(Gtk.Align.START)
        box.pack_start(label, False, False, 0)
        entry = Gtk.Entry()
        entry.set_activates_default(True)
        box.pack_start(entry, False, False, 0)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            name = entry.get_text().strip()
            if name:
                pl = playlists_mod.new_playlist(name)
                self.playlists.append(pl)
                playlists_mod.save(self.playlists)
                self._rebuild_playlists_listbox(select_id=pl["id"])
                self.status_label.set_text(f"Lista «{name}» creada")
        dialog.destroy()

    def _resolve_imported_path(self, p, pls_dir):
        if p.startswith("file://") or "://" in p:
            return p
        if os.path.isabs(p) and os.path.exists(p):
            return GLib.filename_to_uri(p, None)

        p_norm = normalize_text(p)
        base_norm = normalize_text(os.path.basename(p))

        for row in self.store:
            uri = row[COL_URI]
            try:
                fname = GLib.filename_from_uri(uri)[0]
                fname_norm = normalize_text(fname)
                if fname_norm.endswith(p_norm) or p_norm.endswith(fname_norm):
                    return uri
            except Exception:
                pass

        candidates = []
        for row in self.store:
            uri = row[COL_URI]
            try:
                fname = GLib.filename_from_uri(uri)[0]
                if normalize_text(os.path.basename(fname)) == base_norm:
                    candidates.append(uri)
            except Exception:
                pass
        if len(candidates) == 1:
            return candidates[0]
        elif len(candidates) > 1:
            for uri in candidates:
                fname = GLib.filename_from_uri(uri)[0]
                fname_norm = normalize_text(fname)
                if fname_norm.endswith(p_norm) or p_norm.endswith(fname_norm):
                    return uri
            return candidates[0]

        home = os.path.expanduser("~")
        for base_dir in (home, pls_dir):
            abs_path = os.path.join(base_dir, p)
            if os.path.exists(abs_path):
                return GLib.filename_to_uri(abs_path, None)

        music_dir = os.path.join(home, "Música")
        if os.path.isdir(music_dir):
            for root, _dir, files in os.walk(music_dir):
                for f in files:
                    if normalize_text(f) == base_norm:
                        return GLib.filename_to_uri(os.path.join(root, f), None)

        return None

    def on_import_playlist(self, btn):
        chooser = Gtk.FileChooserDialog(
            title=_('Importar lista de reproducción'),
            parent=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        chooser.add_buttons("Cancelar", Gtk.ResponseType.CANCEL,
                            "Importar", Gtk.ResponseType.OK)
        chooser.set_select_multiple(True)
        filt = Gtk.FileFilter()
        filt.set_name("Listas (.pls, .m3u, .m3u8)")
        for pat in ("*.pls", "*.PLS", "*.m3u", "*.M3U", "*.m3u8"):
            filt.add_pattern(pat)
        chooser.add_filter(filt)
        if chooser.run() == Gtk.ResponseType.OK:
            paths = chooser.get_filenames()
            chooser.destroy()
            imported = 0
            last_id = None
            errors = []
            for path in paths:
                name, raw_uris, err = playlists_mod.import_playlist(path)
                if err:
                    errors.append(f"{os.path.basename(path)}: {err}")
                    continue

                pls_dir = os.path.dirname(os.path.abspath(path))
                uris = []
                for u in raw_uris:
                    resolved = self._resolve_imported_path(u, pls_dir)
                    if resolved:
                        if resolved not in self.known_uris:
                            fname = GLib.filename_from_uri(resolved)[0]
                            resolved = self.add_single_track_to_library(fname)
                        uris.append(resolved)

                pl = playlists_mod.new_playlist(name, uris=uris)
                self.playlists.append(pl)
                imported += 1
                last_id = pl["id"]
            if imported:
                playlists_mod.save(self.playlists)
                self._rebuild_playlists_listbox(select_id=last_id)
            msg = f"{imported} lista(s) importada(s)"
            if errors:
                msg += f" — {len(errors)} error(es)"
                for e in errors:
                    print(f"[import] {e}")
            self.status_label.set_text(msg)
        else:
            chooser.destroy()

    def on_delete_playlist(self, btn):
        if self.current_playlist_id is None:
            return
        pl = self._get_playlist(self.current_playlist_id)
        if pl is None:
            return
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"¿Eliminar la lista «{pl['name']}»?",
        )
        dialog.format_secondary_text(
            "Se elimina solo la lista. Las canciones permanecen en tu biblioteca.")
        if dialog.run() == Gtk.ResponseType.YES:
            self.playlists = [p for p in self.playlists
                              if p["id"] != self.current_playlist_id]
            playlists_mod.save(self.playlists)
            self.current_playlist_id = None
            self.shuffle_history = []
            self.shuffle_pool = []
            self._rebuild_playlists_listbox()
        dialog.destroy()

    def on_tree_button_press(self, widget, event):
        # Clic derecho: menú contextual con selección
        if event.button == 3:
            path_info = widget.get_path_at_pos(int(event.x), int(event.y))
            if path_info is not None:
                path = path_info[0]
                sel = widget.get_selection()
                if not sel.path_is_selected(path):
                    sel.unselect_all()
                    sel.select_path(path)
            self._update_move_menu_sensitivity()
            self.context_menu.popup_at_pointer(event)
            return True
        # Clic izquierdo: solo interceptar el rating
        if event.button == 1:
            path_info = widget.get_path_at_pos(int(event.x), int(event.y))
            if path_info:
                path, column, cell_x, cell_y = path_info
                if column == self.rating_column:
                    star_width = 15
                    offset = 4
                    star_index = int((cell_x - offset) / star_width)
                    star_index = max(0, min(4, star_index))
                    new_rating = star_index + 1
                    model = widget.get_model()
                    it = model.get_iter(path)
                    current = model.get_value(it, VCOL_RATING)
                    if current == new_rating:
                        new_rating = 0
                    uri = model.get_value(it, VCOL_URI)
                    self._set_rating_for_uri(uri, new_rating)
                    return True
        return False

    def _make_circular_pixbuf(self, pixbuf, size):
        """Recorta un pixbuf en forma de círculo."""
        if not pixbuf:
            return None
        try:
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
            ctx = cairo.Context(surface)
            ctx.arc(size/2, size/2, size/2, 0, 2*3.14159)
            ctx.clip()
            Gdk.cairo_set_source_pixbuf(ctx, pixbuf, (size - pixbuf.get_width())/2, (size - pixbuf.get_height())/2)
            ctx.paint()
            return Gdk.pixbuf_get_from_surface(surface, 0, 0, size, size)
        except Exception:
            return pixbuf

    def _load_artist_image_worker(self, uri, img_widget):
        if not hasattr(self, "_artist_cover_mem"):
            self._artist_cover_mem = {}
        circular = self._artist_cover_mem.get(uri)
        if circular is None:
            pixbuf = self._load_cover_pixbuf_for_uri(uri, 64)
            if pixbuf:
                circular = self._make_circular_pixbuf(pixbuf, 64)
                self._artist_cover_mem[uri] = circular
        if circular:
            GLib.idle_add(self._update_artist_image, img_widget, circular)

    def _update_artist_image(self, img_widget, pixbuf):
        if img_widget.get_parent() is not None:
            img_widget.set_from_pixbuf(pixbuf)
        return False

    def on_artist_row_activated(self, listbox, row):
        """Se ejecuta al hacer clic en un artista de la lista."""
        artist = getattr(row, "artist_name", None)
        if not artist:
            return
        self.artist_detail_title.set_markup(f"<b><span size='xx-large'>{GLib.markup_escape_text(artist)}</span></b>")
        
        for child in self.artist_albums_flowbox.get_children():
            self.artist_albums_flowbox.remove(child)
            
        tracks = row.artist_tracks
        albums = {}
        for r in tracks:
            alb = r[COL_ALBUM] or "(Sin álbum)"
            if alb not in albums:
                albums[alb] = []
            albums[alb].append(r)
            
        for album in sorted(albums.keys(), key=sort_key_artist):
            alb_tracks = albums[album]
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.set_size_request(150, -1)
            
            img = Gtk.Image()
            placeholder = self._make_square_placeholder(140)
            img.set_from_pixbuf(placeholder)
            box.pack_start(img, False, False, 0)
            
            lbl_title = Gtk.Label()
            lbl_title.set_markup(f"<b>{GLib.markup_escape_text(album)}</b>")
            lbl_title.set_ellipsize(Pango.EllipsizeMode.END)
            lbl_title.set_max_width_chars(20)
            lbl_title.set_xalign(0.5)
            lbl_title.set_halign(Gtk.Align.FILL)
            lbl_title.set_justify(Gtk.Justification.CENTER)
            box.pack_start(lbl_title, False, False, 0)
            
            event_box = Gtk.EventBox()
            event_box.add(box)
            # Pasamos 'alb_tracks' para poder reproducirlo después
            event_box.connect("button-press-event", self.on_album_box_button_press, artist, album, alb_tracks)
            self.artist_albums_flowbox.add(event_box)
            
            uri_portada = alb_tracks[0][COL_URI]
            self.executor.submit(self._load_album_cover_worker, uri_portada, img)
            
        self.artist_albums_flowbox.show_all()
        self.artists_stack.set_visible_child_name("detail")

    def _open_album_detail(self, artist, album, tracks):
        """Abre la vista de detalle de un álbum sin reproducir."""
        # Detectar el origen ANTES de cambiar de pestaña:
        # si el stack muestra "artists", venimos de los discos de un artista.
        if self.stack.get_visible_child_name() == "artists" and artist:
            self._album_back_context = ("artists", artist)
            if hasattr(self, "_album_back_label"):
                self._album_back_label.set_text(_f("Volver a {}", artist))
        else:
            self._album_back_context = None
            if hasattr(self, "_album_back_label"):
                self._album_back_label.set_text(_("Volver a Álbumes"))

        self.search_entry.handler_block_by_func(self.on_search_changed)
        self.search_entry.set_text("")
        self.search_query = ""
        self.search_entry.handler_unblock_by_func(self.on_search_changed)
        
        self.stack.set_visible_child_name("albums")
        
        self.album_detail_title.set_markup(f"<b><span size='x-large'>{GLib.markup_escape_text(album)}</span></b>")
        self.album_tracks_store.clear()
        
        tracks_sorted = sorted(tracks, key=lambda r: (int(r[COL_TRACK_NUM]) if int(r[COL_TRACK_NUM]) > 0 else 9999, r[COL_TITLE].lower()))
        for r in tracks_sorted:
            self.album_tracks_store.append([r[COL_URI], r[COL_TITLE], r[COL_DURATION]])
            
        self.albums_inner_stack.set_visible_child_name("detail")
        self._highlight_playing_in_album_detail()
        
        if hasattr(self, "_album_click_timeout"):
            del self._album_click_timeout
        return False

    def _play_album_tracks(self, artist, album, tracks):
        """Reproduce el álbum completo."""
        self._open_album_detail(artist, album, tracks)
        uris_to_play = [r[COL_URI] for r in sorted(tracks, key=lambda r: (int(r[COL_TRACK_NUM]) if int(r[COL_TRACK_NUM]) > 0 else 9999, r[COL_TITLE].lower()))]
        if uris_to_play:
            self.play_uris(uris_to_play, 0)

    def _load_cover_pixbuf_for_uri(self, uri, size=200):
        """Carga la carátula de una URI como pixbuf.
        Si no está en caché, la extrae (ffmpeg / cover.jpg) y la cachea."""
        try:
            fname = GLib.filename_from_uri(uri)[0]
        except Exception:
            return None
        if not fname or not os.path.exists(fname):
            return None

        cache_dir = os.path.expanduser("~/.cache/tunepod/covers")
        os.makedirs(cache_dir, exist_ok=True)
        h = hashlib.sha1(fname.encode("utf-8")).hexdigest()
        cache_path = os.path.join(cache_dir, h + ".jpg")

        # 1. Intentar caché (lo normal si la canción ya se reprodujo)
        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
            try:
                return GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    cache_path, size, size, True)
            except Exception:
                pass

        # 2. Extraer con ffmpeg (rápido, síncrono, timeout corto)
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", fname, "-an", "-vcodec", "copy",
                 "-map", "0:v:0", cache_path],
                capture_output=True, timeout=5,
            )
            if (result.returncode == 0
                    and os.path.exists(cache_path)
                    and os.path.getsize(cache_path) > 0):
                return GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    cache_path, size, size, True)
        except Exception:
            pass

        # 3. Fallback: cover.jpg / folder.jpg en la carpeta
        folder = os.path.dirname(fname)
        for name in ("cover.jpg", "folder.jpg", "front.jpg", "album.jpg",
                     "Cover.jpg", "Folder.jpg", "Front.jpg", "Album.jpg",
                     "cover.png", "folder.png"):
            p = os.path.join(folder, name)
            if os.path.exists(p):
                try:
                    pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                        p, size, size, True)
                    # Cachear para no repetir el trabajo
                    try:
                        pixbuf.savev(cache_path, "jpeg", [], [])
                    except Exception:
                        pass
                    return pixbuf
                except Exception:
                    pass
        return None

    def on_show_song_info(self, menu_item):
        stack_child = self.stack.get_visible_child_name()
        is_ipod = (stack_child == "ipod" and self.ipod_data)
        track_id = None
        info = {}
        track_uri = None

        if is_ipod:
            current_page = self.ipod_notebook.get_current_page()
            if current_page == 0:
                sel = self.ipod_songs_scroll.get_child().get_selection()
                model, paths = sel.get_selected_rows()
                if len(paths) == 1: track_id = model.get_value(model.get_iter(paths[0]), 6)
            elif current_page == 1:
                sel = self.ipod_artists_view.get_selection()
                model, paths = sel.get_selected_rows()
                if len(paths) == 1:
                    it = model.get_iter(paths[0])
                    if model.get_value(it, 2) == "track": track_id = model.get_value(it, 1)
            elif current_page == 2:
                sel = self.ipod_pls_tracks_view.get_selection()
                model, paths = sel.get_selected_rows()
                if len(paths) == 1: track_id = model.get_value(model.get_iter(paths[0]), 3)
            elif current_page == 3:
                sel = self.ipod_podcasts_view.get_selection()
                model, paths = sel.get_selected_rows()
                if len(paths) == 1: track_id = model.get_value(model.get_iter(paths[0]), 4)
            
            if not track_id:
                self.status_label.set_text(_('Selecciona exactamente una canción del iPod'))
                return
            t_data = next((t for t in self.ipod_data.get("tracks", []) if t.get("id") == track_id), None)
            if not t_data: return
            info = {
                "title": t_data.get("title", ""), "artist": t_data.get("artist", ""),
                "album": t_data.get("album", ""), "albumartist": t_data.get("albumartist", ""),
                "genre": t_data.get("genre", ""), "track_nr": str(t_data.get("track_nr", 0)),
                "cd_nr": str(t_data.get("cd_nr", 0)), "year": str(t_data.get("year", 0)),
                "cover_uri": ipod.ipod_track_full_path(self.ipod_mount, t_data.get("ipod_path", "")) if self.ipod_mount else None
            }
        else:
            uri = self._get_single_selected_uri()
            if not uri:
                self.status_label.set_text(_('Selecciona exactamente una canción'))
                return
            track_uri = uri
            it = self.store.get_iter_first()
            while it is not None:
                if self.store.get_value(it, COL_URI) == uri:
                    info = {
                        "title": self.store.get_value(it, COL_TITLE), "artist": self.store.get_value(it, COL_ARTIST),
                        "album": self.store.get_value(it, COL_ALBUM), "albumartist": self.store.get_value(it, COL_ALBUMARTIST),
                        "genre": self.store.get_value(it, COL_GENRE), "track_nr": str(self.store.get_value(it, COL_TRACK_NUM)),
                        "cd_nr": str(self.store.get_value(it, COL_CD_NUM)), "year": str(self.store.get_value(it, COL_YEAR)),
                        "cover_uri": uri
                    }
                    break
                it = self.store.iter_next(it)
            if not info: return

        # --- VISTA DE SOLO LECTURA ---
        dialog = Gtk.Dialog(title=_('Información de la canción'), transient_for=self, modal=True)
        dialog.set_default_size(680, 400)
        dialog.add_buttons(_("Cerrar"), Gtk.ResponseType.CLOSE, _("Editar metadatos"), Gtk.ResponseType.OK)
        area = dialog.get_content_area()
        area.set_spacing(12); area.set_border_width(16)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        area.pack_start(hbox, True, True, 0)

        cover_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        hbox.pack_start(cover_box, False, False, 0)
        cover = Gtk.Image(); cover.set_size_request(200, 200)
        if info["cover_uri"]:
            uri_c = info["cover_uri"]
            if not uri_c.startswith("file://"): uri_c = GLib.filename_to_uri(uri_c, None)
            pixbuf = self._load_cover_pixbuf_for_uri(uri_c, 200)
            if pixbuf: cover.set_from_pixbuf(pixbuf)
            else: cover.set_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG); cover.set_pixel_size(200)
        cover_box.pack_start(cover, False, False, 0)

        grid = Gtk.Grid(); grid.set_row_spacing(8); grid.set_column_spacing(12)
        hbox.pack_start(grid, True, True, 0)
        
        def add_row(row, label, value):
            lbl = Gtk.Label(); lbl.set_markup(f"<b>{label}</b>")
            lbl.set_halign(Gtk.Align.START); lbl.set_valign(Gtk.Align.START)
            grid.attach(lbl, 0, row, 1, 1)
            val = Gtk.Label(); val.set_text(value if value else "—")
            val.set_halign(Gtk.Align.START); val.set_valign(Gtk.Align.START)
            val.set_ellipsize(Pango.EllipsizeMode.END); val.set_max_width_chars(60)
            val.set_selectable(True); val.set_line_wrap(True)
            grid.attach(val, 1, row, 1, 1)

        add_row(0, _("Título:"), info["title"]); add_row(1, _("Artista:"), info["artist"])
        add_row(2, _("Álbum:"), info["album"]); add_row(3, _("Artista del Álbum:"), info["albumartist"])
        add_row(4, _("Género:"), info["genre"]); add_row(5, _("Número de Pista:"), info["track_nr"])
        add_row(6, _("Número de Disco:"), info["cd_nr"]); add_row(7, _("Año:"), info["year"])

        dialog.show_all()
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            self._show_edit_metadata_dialog(info, is_ipod, track_id if is_ipod else track_uri)

    def _show_edit_metadata_dialog(self, info, is_ipod, track_id_or_uri):
        dialog = Gtk.Dialog(title=_('Editar Información'), transient_for=self, modal=True)
        dialog.set_default_size(680, 500)
        dialog.add_buttons(_("Cancelar"), Gtk.ResponseType.CANCEL, _("Guardar"), Gtk.ResponseType.OK)
        area = dialog.get_content_area()
        area.set_spacing(12); area.set_border_width(16)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        area.pack_start(hbox, True, True, 0)

        cover_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        hbox.pack_start(cover_box, False, False, 0)
        cover = Gtk.Image()
        cover.set_size_request(150, 150)
        if not is_ipod and track_id_or_uri:
            pixbuf = self._load_cover_pixbuf_for_uri(track_id_or_uri, 150)
            if pixbuf:
                cover.set_from_pixbuf(pixbuf)
            else:
                cover.set_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
                cover.set_pixel_size(150)
        else:
            cover.set_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
            cover.set_pixel_size(150)
        cover_box.pack_start(cover, False, False, 0)

        if not is_ipod and track_id_or_uri:
            cover_btn = Gtk.Button(label=_('🖼️ Buscar carátula…'))
            cover_btn.connect("clicked", lambda w: self._on_fetch_cover_clicked(w, track_id_or_uri, cover))
            cover_box.pack_start(cover_btn, False, False, 0)

        grid = Gtk.Grid(); grid.set_row_spacing(8); grid.set_column_spacing(12)
        hbox.pack_start(grid, True, True, 0)
        
        entries = {}
        def add_edit_row(row, label, key, value, editable=True):
            lbl = Gtk.Label(); lbl.set_markup(f"<b>{label}</b>")
            lbl.set_halign(Gtk.Align.START)
            grid.attach(lbl, 0, row, 1, 1)
            entry = Gtk.Entry(); entry.set_text(value or "")
            entry.set_hexpand(True)
            entry.set_sensitive(editable)
            grid.attach(entry, 1, row, 1, 1)
            entries[key] = entry

        add_edit_row(0, _("Título:"), "title", info["title"])
        add_edit_row(1, _("Artista:"), "artist", info["artist"])
        add_edit_row(2, _("Álbum:"), "album", info["album"])
        add_edit_row(3, _("Artista del Álbum:"), "albumartist", info["albumartist"])
        add_edit_row(4, _("Género:"), "genre", info["genre"], editable=is_ipod)
        add_edit_row(5, _("Número de Pista:"), "track_nr", info["track_nr"], editable=is_ipod)
        add_edit_row(6, _("Número de Disco:"), "cd_nr", info["cd_nr"], editable=is_ipod)
        add_edit_row(7, _("Año:"), "year", info["year"], editable=is_ipod)

        apply_album_check = None
        if not is_ipod:
            current_artist = info["artist"]
            current_album = info["album"]
            album_uris = [r[COL_URI] for r in self.store if r[COL_ARTIST] == current_artist and r[COL_ALBUM] == current_album]
            if len(album_uris) > 1:
                apply_album_check = Gtk.CheckButton(label=_f("Aplicar a todo el álbum «{}» ({} canciones)", current_album, len(album_uris)))
                area.pack_start(apply_album_check, False, False, 0)

        search_btn = Gtk.Button(label=_('🔍 Buscar información en línea…'))
        search_btn.connect("clicked", self._on_search_metadata_clicked, entries)
        area.pack_start(search_btn, False, False, 0)

        dialog.show_all()
        response = dialog.run()
        
        if response == Gtk.ResponseType.OK:
            new_data = {}
            for key, entry in entries.items():
                val = entry.get_text().strip()
                if key in ("track_nr", "cd_nr", "year"):
                    try: new_data[key] = int(val) if val else 0
                    except: new_data[key] = 0
                else:
                    new_data[key] = val
            
            if is_ipod:
                new_data["id"] = track_id_or_uri
                self.status_label.set_text(_('Guardando en el iPod…'))
                def worker():
                    out, err = ipod_write.update_metadata(self.ipod_mount, [new_data])
                    if err: GLib.idle_add(self.status_label.set_text, f"Error: {err[:100]}")
                    else:
                        GLib.idle_add(self.status_label.set_text, "Metadatos guardados en el iPod")
                        data, rerr = ipod.read_ipod(self.ipod_mount)
                        if not rerr: GLib.idle_add(self._on_ipod_read, self.ipod_mount, data, None)
                threading.Thread(target=worker, daemon=True).start()
            else:
                apply_to_all = apply_album_check and apply_album_check.get_active()
                it = self.store.get_iter_first()
                while it is not None:
                    uri_match = self.store.get_value(it, COL_URI) == track_id_or_uri
                    artist_match = self.store.get_value(it, COL_ARTIST) == info["artist"]
                    album_match = self.store.get_value(it, COL_ALBUM) == info["album"]
                    
                    if uri_match:
                        self.store.set_value(it, COL_TITLE, new_data["title"])
                        self.store.set_value(it, COL_TRACK_NUM, new_data["track_nr"])
                    if uri_match or (apply_to_all and artist_match and album_match):
                        self.store.set_value(it, COL_ARTIST, new_data["artist"])
                        self.store.set_value(it, COL_ALBUM, new_data["album"])
                        self.store.set_value(it, COL_ALBUMARTIST, new_data["albumartist"])
                        # Aplicar también género, año y cd_num si es "aplicar a todo"
                        if not uri_match and apply_to_all:
                            self.store.set_value(it, COL_YEAR, new_data["year"])
                            self.store.set_value(it, COL_GENRE, new_data["genre"])
                            self.store.set_value(it, COL_CD_NUM, new_data["cd_nr"])
                    it = self.store.iter_next(it)
                self._save_library()

                archivos_a_actualizar = []
                if apply_to_all:
                    for row in self.store:
                        if row[COL_ARTIST] == info["artist"] and row[COL_ALBUM] == info["album"]:
                            try:
                                fname_tmp = GLib.filename_from_uri(row[COL_URI])[0]
                                archivos_a_actualizar.append(fname_tmp)
                            except Exception:
                                pass
                else:
                    try:
                        fname_tmp = GLib.filename_from_uri(track_id_or_uri)[0]
                        archivos_a_actualizar.append(fname_tmp)
                    except Exception:
                        pass

                errores_tags = []
                for fname_tmp in archivos_a_actualizar:
                    ok_tags, err_tags = tags.write_tags(fname_tmp, new_data)
                    if not ok_tags:
                        errores_tags.append(os.path.basename(fname_tmp))

                self._apply_filters()
                if errores_tags:
                    self.status_label.set_text(
                        "Metadatos guardados, pero no se pudieron escribir los tags en: " + ", ".join(errores_tags[:3]))
                else:
                    self.status_label.set_text(_('Metadatos guardados en la biblioteca y en los tags físicos'))
        dialog.destroy()

    def on_show_album_info(self, menu_item):
        artist, album = self._get_selected_album_data()
        if not artist and hasattr(self, "_ctx_artist"):
            artist = getattr(self, "_ctx_artist", None)
            album = getattr(self, "_ctx_album", None)

        if not artist:
            self.status_label.set_text(_('No se pudo identificar el álbum.'))
            return

        album_uris = []
        it = self.store.get_iter_first()
        while it is not None:
            if self.store.get_value(it, COL_ARTIST) == artist and (not album or self.store.get_value(it, COL_ALBUM) == album):
                album_uris.append(self.store.get_value(it, COL_URI))
            it = self.store.iter_next(it)

        if not album_uris:
            self.status_label.set_text(_('No se encontraron canciones para este álbum.'))
            return
        if not album:
            for row in self.store:
                if row[COL_URI] == album_uris[0]:
                    album = row[COL_ALBUM]
                    break

        # Obtener género y año del primer track del álbum
        genre = ""
        year = 0
        for row in self.store:
            if row[COL_URI] in album_uris:
                genre = row[COL_GENRE]
                year = row[COL_YEAR]
                break

        # --- VISTA DE SOLO LECTURA PARA ÁLBUM ---
        dialog = Gtk.Dialog(title=_("Información del Álbum"), transient_for=self, modal=True)
        dialog.set_default_size(550, 300) # Un poco más ancho para que quepa la imagen
        dialog.add_buttons(_("Cerrar"), Gtk.ResponseType.CLOSE, _("Editar metadatos del álbum"), Gtk.ResponseType.OK)
        area = dialog.get_content_area()
        area.set_spacing(12); area.set_border_width(16)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        area.pack_start(hbox, True, True, 0)

        # Carátula del álbum
        cover_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        hbox.pack_start(cover_box, False, False, 0)
        
        cover = Gtk.Image()
        cover.set_size_request(200, 200)
        
        if album_uris:
            pixbuf = self._load_cover_pixbuf_for_uri(album_uris[0], 200)
            if pixbuf:
                cover.set_from_pixbuf(pixbuf)
            else:
                cover.set_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
                cover.set_pixel_size(200)
        cover_box.pack_start(cover, False, False, 0)

        # Datos del álbum
        grid = Gtk.Grid()
        grid.set_row_spacing(8)
        grid.set_column_spacing(12)
        hbox.pack_start(grid, True, True, 0)

        def add_row(row, label, value):
            lbl = Gtk.Label()
            lbl.set_markup(f"<b>{label}</b>")
            lbl.set_halign(Gtk.Align.START)
            lbl.set_valign(Gtk.Align.START)
            grid.attach(lbl, 0, row, 1, 1)

            val = Gtk.Label()
            val.set_text(value if value else "—")
            val.set_halign(Gtk.Align.START)
            val.set_valign(Gtk.Align.START)
            val.set_ellipsize(Pango.EllipsizeMode.END)
            val.set_max_width_chars(60)
            val.set_selectable(True)
            val.set_line_wrap(True)
            grid.attach(val, 1, row, 1, 1)

        add_row(0, _("Artista:"), artist)
        add_row(1, _("Álbum:"), album)
        add_row(2, _("Género:"), genre)
        add_row(3, _("Año:"), str(year) if year else "")
        add_row(4, _("Canciones:"), str(len(album_uris)))

        dialog.show_all()
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            self._show_edit_album_dialog(artist, album, album_uris)

    def _show_edit_album_dialog(self, artist, album, album_uris):
        # Obtener género y año del primer track para inicializar los campos
        genre = ""
        year = 0
        for row in self.store:
            if row[COL_URI] in album_uris:
                genre = row[COL_GENRE]
                year = row[COL_YEAR]
                break

        dialog = Gtk.Dialog(title=_('Editar Álbum'), transient_for=self, modal=True)
        dialog.set_default_size(600, 400)
        dialog.add_buttons(_("Cancelar"), Gtk.ResponseType.CANCEL, _("Guardar en todas"), Gtk.ResponseType.OK)
        area = dialog.get_content_area()
        area.set_spacing(12); area.set_border_width(16)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        area.pack_start(hbox, True, True, 0)

        cover_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        hbox.pack_start(cover_box, False, False, 0)
        cover = Gtk.Image()
        cover.set_size_request(150, 150)
        if album_uris:
            pixbuf = self._load_cover_pixbuf_for_uri(album_uris[0], 150)
            if pixbuf:
                cover.set_from_pixbuf(pixbuf)
            else:
                cover.set_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
                cover.set_pixel_size(150)
        else:
            cover.set_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
            cover.set_pixel_size(150)
        cover_box.pack_start(cover, False, False, 0)

        if album_uris:
            cover_btn = Gtk.Button(label=_('🖼️ Buscar carátula…'))
            cover_btn.connect("clicked", lambda w: self._on_fetch_cover_clicked(w, album_uris[0], cover))
            cover_box.pack_start(cover_btn, False, False, 0)

        grid = Gtk.Grid(); grid.set_row_spacing(8); grid.set_column_spacing(12)
        hbox.pack_start(grid, True, True, 0)
        
        entries = {}
        def add_edit_row(row, label, key, value):
            lbl = Gtk.Label(); lbl.set_markup(f"<b>{label}</b>")
            lbl.set_halign(Gtk.Align.START)
            grid.attach(lbl, 0, row, 1, 1)
            entry = Gtk.Entry(); entry.set_text(value or "")
            entry.set_hexpand(True)
            grid.attach(entry, 1, row, 1, 1)
            entries[key] = entry

        add_edit_row(0, _("Artista:"), "artist", artist)
        add_edit_row(1, _("Álbum:"), "album", album)
        add_edit_row(2, _("Género:"), "genre", genre)
        add_edit_row(3, _("Año:"), "year", str(year) if year else "")

        search_btn = Gtk.Button(label=_('🔍 Buscar información en línea…'))
        search_btn.connect("clicked", self._on_search_metadata_clicked, entries)
        area.pack_start(search_btn, False, False, 0)

        dialog.show_all()
        response = dialog.run()
        
        if response == Gtk.ResponseType.OK:
            new_artist = entries["artist"].get_text()
            new_album = entries["album"].get_text()
            new_genre = entries["genre"].get_text()
            new_year = entries["year"].get_text()
            
            it = self.store.get_iter_first()
            updated_count = 0
            while it is not None:
                if self.store.get_value(it, COL_URI) in album_uris:
                    self.store.set_value(it, COL_ARTIST, new_artist)
                    self.store.set_value(it, COL_ALBUM, new_album)
                    self.store.set_value(it, COL_ALBUMARTIST, new_artist)
                    self.store.set_value(it, COL_GENRE, new_genre)
                    try:
                        self.store.set_value(it, COL_YEAR, int(new_year) if new_year else 0)
                    except:
                        pass
                    updated_count += 1
                it = self.store.iter_next(it)
            
            self._save_library()

            errores_tags = []
            for uri_tmp in album_uris:
                try:
                    fname_tmp = GLib.filename_from_uri(uri_tmp)[0]
                except Exception:
                    continue
                meta_tags = {
                    "artist": new_artist,
                    "album": new_album,
                    "albumartist": new_artist,
                    "genre": new_genre,
                    "year": int(new_year) if new_year else 0,
                }
                ok_tags, err_tags = tags.write_tags(fname_tmp, meta_tags)
                if not ok_tags:
                    errores_tags.append(os.path.basename(fname_tmp))

            self._apply_filters()
            if errores_tags:
                self.status_label.set_text(
                    f"Álbum actualizado en {updated_count} canciones, pero falló en: " + ", ".join(errores_tags[:3]))
            else:
                self.status_label.set_text(f"Álbum actualizado en {updated_count} canciones y tags físicos.")
        dialog.destroy()

    def _on_search_metadata_clicked(self, btn, entries):
        artist = entries["artist"].get_text().strip()
        album = entries["album"].get_text().strip()
        if not artist or not album:
            self.status_label.set_text(_('Escribe al menos Artista y Álbum para buscar'))
            return

        self.status_label.set_text(f"Buscando «{artist} - {album}» en la web…")
        btn.set_sensitive(False)
        
        def worker():
            import urllib.request
            import urllib.parse
            import json
            
            results = []
            try:
                query = urllib.parse.quote(f"{artist} {album}")
                url = f"https://itunes.apple.com/search?term={query}&entity=album&limit=5"
                with urllib.request.urlopen(url, timeout=10) as r:
                    data = json.loads(r.read().decode())
                    for item in data.get("results", []):
                        results.append({
                            "source": "iTunes",
                            "album": item.get("collectionName"),
                            "artist": item.get("artistName"),
                            "year": item.get("releaseDate", "")[:4],
                            "genre": item.get("primaryGenreName", "")
                        })
            except Exception as e:
                print(f"[iTunes] Error: {e}")

            try:
                query = urllib.parse.quote(f"{artist} {album}")
                url = f"https://api.deezer.com/search/album?q={query}"
                with urllib.request.urlopen(url, timeout=10) as r:
                    data = json.loads(r.read().decode())
                    for item in data.get("data", [])[:5]:
                        results.append({
                            "source": "Deezer",
                            "album": item.get("title"),
                            "artist": item.get("artist", {}).get("name"),
                            "year": item.get("release_date", "")[:4],
                            "genre": ""
                        })
            except Exception as e:
                print(f"[Deezer] Error: {e}")

            GLib.idle_add(self._show_metadata_suggestions, results, entries, btn)

        threading.Thread(target=worker, daemon=True).start()

    def _show_metadata_suggestions(self, results, entries, btn):
        btn.set_sensitive(True)
        if not results:
            self.status_label.set_text(_('No se encontró información en línea.'))
            return

        dialog = Gtk.Dialog(title=_('Sugerencias encontradas'), transient_for=self, modal=True)
        dialog.set_default_size(600, 300)
        dialog.add_buttons(_("Cancelar"), Gtk.ResponseType.CANCEL, _("Usar esta información"), Gtk.ResponseType.OK)
        
        area = dialog.get_content_area()
        scroll = Gtk.ScrolledWindow()
        listbox = Gtk.ListBox()
        scroll.add(listbox)
        area.pack_start(scroll, True, True, 0)

        rows = []
        for r in results:
            row = Gtk.ListBoxRow()
            hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            hb.set_border_width(6)
            lbl = Gtk.Label(label=f"[{r['source']}] {r['artist']} - {r['album']} ({r['year']})")
            lbl.set_halign(Gtk.Align.START)
            hb.pack_start(lbl, True, True, 0)
            row.add(hb)
            row._result = r
            listbox.add(row)
            rows.append(row)
        
        if rows: listbox.select_row(rows[0])

        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            sel = listbox.get_selected_row()
            if sel:
                r = sel._result
                entries["album"].set_text(r["album"] or "")
                entries["artist"].set_text(r["artist"] or "")
                if "albumartist" in entries:
                    entries["albumartist"].set_text(r["artist"] or "")
                if "genre" in entries:
                    entries["genre"].set_text(r["genre"] or "")
                if "year" in entries:
                    entries["year"].set_text(r["year"] or "")
                self.status_label.set_text(f"Información de {r['source']} aplicada. Revisa y haz clic en Guardar.")
        dialog.destroy()

    def _get_album_uris(self, artist, album):
        """Devuelve las URIs de todas las canciones del mismo artista+álbum."""
        if not artist or not album:
            return []
        uris = []
        it = self.store.get_iter_first()
        while it is not None:
            if (self.store.get_value(it, COL_ARTIST) == artist
                    and self.store.get_value(it, COL_ALBUM) == album):
                uris.append(self.store.get_value(it, COL_URI))
            it = self.store.iter_next(it)
        return uris

    def _on_fetch_cover_clicked(self, btn, uri, cover_widget):
        """Busca carátulas en todas las fuentes disponibles."""
        import cover_art

        info = None
        it = self.store.get_iter_first()
        while it is not None:
            if self.store.get_value(it, COL_URI) == uri:
                info = {
                    "artist": self.store.get_value(it, COL_ARTIST),
                    "album": self.store.get_value(it, COL_ALBUM),
                }
                break
            it = self.store.iter_next(it)

        if not info or not info["artist"] or not info["album"]:
            self.status_label.set_text(
                _('La canción no tiene artista o álbum para buscar'))
            return

        btn.set_sensitive(False)
        self.status_label.set_text(_('Buscando carátulas en iTunes y Deezer…'))

        def worker():
            results, errors = cover_art.search_all_sources(
                info["artist"], info["album"])
            GLib.idle_add(done, results, errors)

        def done(results, errors):
            btn.set_sensitive(True)
            if not results:
                if errors:
                    detail = "; ".join(f"{k}: {v}" for k, v in errors.items())
                    self.status_label.set_text(
                        f"No se encontraron carátulas ({detail})")
                else:
                    self.status_label.set_text(
                        _('No se encontraron carátulas en iTunes ni Deezer'))
                return False
            self._show_cover_results_dialog(uri, results, cover_widget)
            return False

        threading.Thread(target=worker, daemon=True).start()

    def _show_cover_results_dialog(self, uri, results, cover_widget):
        import cover_art

        # Datos del álbum actual
        artist = ""
        album = ""
        it = self.store.get_iter_first()
        while it is not None:
            if self.store.get_value(it, COL_URI) == uri:
                artist = self.store.get_value(it, COL_ARTIST)
                album = self.store.get_value(it, COL_ALBUM)
                break
            it = self.store.iter_next(it)
        album_uris = self._get_album_uris(artist, album)

        dialog = Gtk.Dialog(
            title=_('Seleccionar carátula'),
            transient_for=self, modal=True,
        )
        dialog.set_default_size(640, 520)
        dialog.add_buttons(
            _("Usar archivo local…"), Gtk.ResponseType.APPLY,
            _("Cancelar"), Gtk.ResponseType.CANCEL,
            _("Usar esta carátula"), Gtk.ResponseType.OK,
        )
        area = dialog.get_content_area()
        area.set_spacing(8)
        area.set_border_width(12)

        lbl = Gtk.Label()
        lbl.set_markup(
            _f("<b>{}</b> resultados en {}. Elige una carátula:",
               len(results),
               ', '.join(sorted(set(r['source'] for r in results)))))
        lbl.set_halign(Gtk.Align.START)
        area.pack_start(lbl, False, False, 0)

        # Checkbox "Aplicar a todo el álbum" (solo si hay más de una canción)
        apply_album_check = None
        if len(album_uris) > 1:
            apply_album_check = Gtk.CheckButton(
                label=_f("Aplicar a todo el álbum «{}» ({} canciones)", album, len(album_uris)))
            apply_album_check.set_active(False)
            apply_album_check.set_margin_bottom(6)
            area.pack_start(apply_album_check, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        area.pack_start(scroll, True, True, 0)

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        scroll.add(listbox)

        rows = []
        for r in results:
            row = Gtk.ListBoxRow()
            hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            hb.set_border_width(6)

            img = Gtk.Image.new_from_icon_name(
                "image-x-generic", Gtk.IconSize.DIALOG)
            img.set_size_request(80, 80)
            hb.pack_start(img, False, False, 0)

            vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            t = Gtk.Label()
            t.set_markup(f"<b>{GLib.markup_escape_text(r['title'])}</b>")
            t.set_halign(Gtk.Align.START)
            t.set_ellipsize(Pango.EllipsizeMode.END)
            vb.pack_start(t, False, False, 0)

            a = Gtk.Label(label=r["artist"])
            a.set_halign(Gtk.Align.START)
            a.set_ellipsize(Pango.EllipsizeMode.END)
            vb.pack_start(a, False, False, 0)

            src = Gtk.Label()
            src.set_markup(f"<small>{r['source']}</small>")
            src.set_halign(Gtk.Align.START)
            vb.pack_start(src, False, False, 0)
            hb.pack_start(vb, True, True, 0)

            row.add(hb)
            row._result = r
            row._img_widget = img
            listbox.add(row)
            rows.append(row)

        if rows:
            listbox.select_row(rows[0])

        def load_thumb(url, img_widget):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "TunePod/0.1"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = resp.read()
                loader = GdkPixbuf.PixbufLoader()
                loader.write(data)
                loader.close()
                pix = loader.get_pixbuf()
                if pix:
                    scaled = pix.scale_simple(
                        80, 80, GdkPixbuf.InterpType.BILINEAR)
                    GLib.idle_add(img_widget.set_from_pixbuf, scaled)
            except Exception:
                pass

        for row in rows:
            threading.Thread(
                target=load_thumb,
                args=(row._result["artwork_url"], row._img_widget),
                daemon=True,
            ).start()

        dialog.show_all()
        response = dialog.run()

        if response == Gtk.ResponseType.APPLY:
            dialog.destroy()
            apply_album = (apply_album_check is not None
                           and apply_album_check.get_active())
            self._use_local_cover_file(uri, cover_widget, album_uris,
                                       apply_album)
            return

        if response == Gtk.ResponseType.OK:
            sel = listbox.get_selected_row()
            if sel is not None:
                r = sel._result
                # Si el álbum tiene más de una canción, aplicar siempre a todo el álbum
                if len(album_uris) > 1:
                    targets = album_uris
                else:
                    targets = [uri]

                # Descargar la carátula UNA SOLA VEZ y copiarla localmente a las
                # demás canciones (copiar es instantáneo; descargar 22 veces
                # congelaba la interfaz).
                first_uri = targets[0]
                path, err = cover_art.download_cover(
                    r["artwork_url"], first_uri, overwrite=True)
                ok_count = 0
                err_count = 0
                if err:
                    err_count = len(targets)
                    path = None
                else:
                    ok_count = 1
                    for other_uri in targets[1:]:
                        try:
                            other_path, other_err = cover_art.copy_local_cover(
                                path, other_uri, overwrite=True)
                            if other_err:
                                err_count += 1
                            else:
                                ok_count += 1
                        except Exception:
                            err_count += 1

                # Actualizar el widget de portada si la canción actual está afectada
                if path and uri in targets:
                    try:
                        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                            path, 200, 200, True)
                        cover_widget.set_from_pixbuf(pixbuf)
                    except Exception:
                        pass

                if len(targets) > 1:
                    msg = f"Carátula aplicada a {ok_count} canciones"
                    if err_count:
                        msg += f" ({err_count} errores)"
                    self.status_label.set_text(msg)
                else:
                    self.status_label.set_text(
                        f"Carátula guardada ({r['source']}): "
                        f"{r['title']} — {r['artist']}")

                # Refrescar panel Now Playing si la canción actual está afectada
                if self.current_uri in targets:
                    threading.Thread(
                        target=self._load_cover_art,
                        args=(self.current_uri,),
                        daemon=True,
                    ).start()

        dialog.destroy()

    def _use_local_cover_file(self, uri, cover_widget, album_uris=None,
                              apply_album=False):
        """Permite elegir una imagen local como carátula.
        Si apply_album=True, se aplica a todas las uris de album_uris."""
        import cover_art

        chooser = Gtk.FileChooserDialog(
            title=_('Elegir imagen de carátula'),
            parent=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        chooser.add_buttons(
            "Cancelar", Gtk.ResponseType.CANCEL,
            "Usar esta imagen", Gtk.ResponseType.OK,
        )
        filt = Gtk.FileFilter()
        filt.set_name("Imágenes")
        for ext in ("*.jpg", "*.jpeg", "*.png", "*.JPG", "*.JPEG", "*.PNG"):
            filt.add_pattern(ext)
        chooser.add_filter(filt)

        if chooser.run() != Gtk.ResponseType.OK:
            chooser.destroy()
            return

        src = chooser.get_filename()
        chooser.destroy()

        targets = album_uris if (apply_album and album_uris) else [uri]
        ok_count = 0
        err_count = 0
        for target_uri in targets:
            path, err = cover_art.copy_local_cover(
                src, target_uri, overwrite=True)
            if err:
                err_count += 1
            else:
                ok_count += 1
                if target_uri == uri:
                    try:
                        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                            path, 200, 200, True)
                        cover_widget.set_from_pixbuf(pixbuf)
                    except Exception:
                        pass

        if apply_album and album_uris:
            msg = f"Carátula aplicada a {ok_count} canciones"
            if err_count:
                msg += f" ({err_count} errores)"
            self.status_label.set_text(msg)
        else:
            if err_count:
                self.status_label.set_text(f_('Error: no se pudo copiar la imagen'))
            else:
                self.status_label.set_text(
                    f"Carátula copiada desde {os.path.basename(src)}")

        if self.current_uri in targets:
            threading.Thread(
                target=self._load_cover_art,
                args=(self.current_uri,),
                daemon=True,
            ).start()

    def on_add_current_to_playlist(self, btn):
        if not self.current_uri:
            self.status_label.set_text(_('No hay ninguna canción reproduciéndose'))
            return

        # Buscar info de la canción actual
        title = ""
        it = self.store.get_iter_first()
        while it is not None:
            if self.store.get_value(it, COL_URI) == self.current_uri:
                title = self.store.get_value(it, COL_TITLE)
                break
            it = self.store.iter_next(it)

        dialog = Gtk.Dialog(
            title=_('Añadir a lista'),
            transient_for=self, modal=True,
        )
        dialog.add_buttons("Cancelar", Gtk.ResponseType.CANCEL,
                           "Añadir", Gtk.ResponseType.OK)
        dialog.set_default_size(400, -1)
        box = dialog.get_content_area()
        box.set_spacing(8)
        box.set_border_width(10)

        lbl = Gtk.Label(label=f"Canción: {title}" if title else "Canción actual")
        lbl.set_halign(Gtk.Align.START)
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        box.pack_start(lbl, False, False, 0)

        box.pack_start(Gtk.Separator(), False, False, 0)

        existing_label = Gtk.Label(label=_('Añadir a una lista existente:'))
        existing_label.set_halign(Gtk.Align.START)
        box.pack_start(existing_label, False, False, 0)

        combo = Gtk.ComboBoxText()
        manual_playlists = [pl for pl in self.playlists if not playlists_mod.is_smart(pl)]
        for pl in manual_playlists:
            combo.append(pl["id"], pl["name"])
        if manual_playlists:
            combo.set_active(0)
        else:
            combo.set_sensitive(False)
            combo.append("__none__", "(no hay listas manuales)")
            combo.set_active(0)
        box.pack_start(combo, False, False, 0)

        box.pack_start(Gtk.Separator(), False, False, 0)

        new_label = Gtk.Label(label=_('O crear una nueva lista:'))
        new_label.set_halign(Gtk.Align.START)
        box.pack_start(new_label, False, False, 0)

        new_entry = Gtk.Entry()
        new_entry.set_placeholder_text(_('Nombre de la nueva lista (opcional)'))
        box.pack_start(new_entry, False, False, 0)

        dialog.show_all()
        response = dialog.run()

        if response == Gtk.ResponseType.OK:
            new_name = new_entry.get_text().strip()
            if new_name:
                pl = playlists_mod.new_playlist(new_name, uris=[self.current_uri])
                self.playlists.append(pl)
                playlists_mod.save(self.playlists)
                self._rebuild_playlists_listbox(select_id=pl["id"])
                self.status_label.set_text(_f("Canción añadida a nueva lista «{}»", new_name))
            elif manual_playlists:
                pl_id = combo.get_active_id()
                pl = self._get_playlist(pl_id)
                if pl:
                    if self.current_uri not in pl["uris"]:
                        pl["uris"].append(self.current_uri)
                        playlists_mod.save(self.playlists)
                        self._rebuild_playlists_listbox(select_id=self.current_playlist_id)
                        self.status_label.set_text(_f("Canción añadida a «{}»", pl['name']))
                    else:
                        self.status_label.set_text(_f("La canción ya está en «{}»", pl['name']))
            else:
                self.status_label.set_text(_('No se seleccionó lista ni se creó una nueva'))

        dialog.destroy()

    def on_add_to_playlist_clicked(self, btn):
        if self.notebook.get_current_page() != 0:
            self.status_label.set_text(_('Solo desde la pestaña Biblioteca'))
            return
        uris = self._get_selected_uris()
        if not uris:
            self.status_label.set_text(_('Selecciona alguna canción primero'))
            return
        if not self.playlists:
            self.status_label.set_text(
                _('No tienes listas. Crea una primero con el botón +'))
            return

        dialog = Gtk.Dialog(
            title=_('Añadir a lista'),
            transient_for=self, modal=True,
        )
        dialog.add_buttons("Cancelar", Gtk.ResponseType.CANCEL,
                           "Añadir", Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(8)
        box.set_border_width(10)
        label = Gtk.Label(
            label=f"{len(uris)} canción(es) → ¿a qué lista?")
        label.set_halign(Gtk.Align.START)
        box.pack_start(label, False, False, 0)
        combo = Gtk.ComboBoxText()
        for pl in self.playlists:
            combo.append(pl["id"], pl["name"])
        combo.set_active(0)
        box.pack_start(combo, False, False, 0)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            pl_id = combo.get_active_id()
            pl = self._get_playlist(pl_id)
            if pl:
                existing = set(pl["uris"])
                added = 0
                for u in uris:
                    if u not in existing:
                        pl["uris"].append(u)
                        existing.add(u)
                        added += 1
                playlists_mod.save(self.playlists)
                self._rebuild_playlists_listbox(select_id=self.current_playlist_id)
                self.status_label.set_text(
                    f"{added} canción(es) añadida(s) a «{pl['name']}»")
        dialog.destroy()

    def _collect_track_uris(self, parent_iter, out):
        child = self.tree_store.iter_children(parent_iter)
        while child:
            kind = self.tree_store.get_value(child, 2)
            if kind == "track":
                out.append(self.tree_store.get_value(child, 1))
            else:
                self._collect_track_uris(child, out)
            child = self.tree_store.iter_next(child)

    # ---------- Biblioteca ----------

    def add_single_track_to_library(self, p):
        uri = GLib.filename_to_uri(os.path.abspath(p), None)
        if uri in self.known_uris:
            return uri
        info = self.discoverer.discover_uri(uri)
        title = os.path.basename(p)
        artist = ""
        album = ""
        dur_ns = 0
        track_num = 0
        year = 0
        genre = ""
        albumartist = ""
        cd_num = 0
        
        if info is not None:
            tags = info.get_tags()
            if tags is not None:
                exists, val = tags.get_string_index("title", 0)
                if exists and val: title = val
                exists, val = tags.get_string_index("artist", 0)
                if exists and val: artist = val
                exists, val = tags.get_string_index("album", 0)
                if exists and val: album = val
                exists, tn = tags.get_uint_index("track-number", 0)
                if exists and tn: track_num = int(tn)
                else:
                    exists, ts = tags.get_string_index("track-number", 0)
                    if exists and ts:
                        try: track_num = int(ts.split("/")[0].strip())
                        except Exception: pass
                
                exists, val = tags.get_string_index("date", 0)
                if exists and val:
                    try: year = int(val[:4])
                    except: pass
                exists, val = tags.get_string_index("genre", 0)
                if exists and val: genre = val
                exists, val = tags.get_string_index("album-artist", 0)
                if exists and val: albumartist = val
                exists, cn = tags.get_uint_index("album-disc-number", 0)
                if exists and cn: cd_num = int(cn)
                
            if track_num == 0:
                track_num = infer_track_num(p)
            dur_ns = info.get_duration()
            
        now = datetime.datetime.now().isoformat(timespec="seconds")
        self.store.append([
            title, artist, album, self.fmt_time(dur_ns), uri, track_num,
            len(self.store), 0, 0, "", now, year, genre, albumartist, cd_num,
        ])
        self.known_uris.add(uri)
        self._save_library()
        return uri

    def _load_library(self):
        tracks = library.load()
        enriched = 0
        missing = 0
        valid_tracks = []
        
        for i, t in enumerate(tracks):
            uri = t.get("uri", "")
            if not uri or uri in self.known_uris:
                continue
            
            # Verificación automática: ¿el archivo existe en el disco?
            fname = ""
            try:
                fname = GLib.filename_from_uri(uri)[0]
            except Exception:
                pass
            
            if not fname or not os.path.exists(fname):
                missing += 1
                continue  # Saltar archivos que ya no existen
                
            tn = int(t.get("track_number", 0) or 0)
            if tn == 0:
                inferred = infer_track_num(fname)
                if inferred:
                    tn = inferred
                    enriched += 1
                    
            self.store.append([
                t.get("title", ""),
                t.get("artist", ""),
                t.get("album", ""),
                t.get("duration_display", "0:00"),
                uri,
                tn,
                i,
                int(t.get("rating", 0)),
                int(t.get("play_count", 0)),
                t.get("last_played", ""),
                t.get("date_added", ""),
                int(t.get("year", 0)),
                t.get("genre", ""),
                t.get("albumartist", ""),
                int(t.get("cd_num", 0)),
            ])

            self.known_uris.add(uri)
            valid_tracks.append(t)
            
        n = len(self.store)
        self.status_label.set_text(f"{n} pistas" if n else "0 pistas")
        
        if enriched:
            print(f"[library] {enriched} pistas: track-number inferido del nombre")
            
        if missing > 0:
            print(f"[library] {missing} pistas eliminadas de la lista (archivos no encontrados).")
            # Guardar la biblioteca sin las pistas faltantes
            self._save_library()
            
            # Limpiar las pistas faltantes de las playlists
            for pl in self.playlists:
                if "uris" in pl:
                    pl["uris"] = [u for u in pl["uris"] if u in self.known_uris]
            playlists_mod.save(self.playlists)

    def _save_library(self):
        tracks = []
        for row in self.store:
            tracks.append({
                "title": row[COL_TITLE],
                "artist": row[COL_ARTIST],
                "album": row[COL_ALBUM],
                "duration_display": row[COL_DURATION],
                "uri": row[COL_URI],
                "track_number": int(row[COL_TRACK_NUM]),
                "rating": int(row[COL_RATING]),
                "play_count": int(row[COL_PLAY_COUNT]),
                "last_played": row[COL_LAST_PLAYED],
                "date_added": row[COL_DATE_ADDED],
                "year": int(row[COL_YEAR]),
                "genre": row[COL_GENRE],
                "albumartist": row[COL_ALBUMARTIST],
                "cd_num": int(row[COL_CD_NUM]),
            })
        library.save(tracks)

    def _rebuild_albums_grid(self, query=""):
        for child in self.album_flowbox.get_children():
            self.album_flowbox.remove(child)

        q = (query or "").lower().strip()
        groups = {}
        
        # 1. Agrupar las canciones por (Artista, Álbum)
        for row in self.store:
            title = row[COL_TITLE]
            artist = row[COL_ARTIST] or "(Sin artista)"
            album = row[COL_ALBUM] or "(Sin álbum)"
            uri = row[COL_URI]
            
            if self.current_playlist_uris is not None and uri not in self.current_playlist_uris:
                continue
            if q and (q not in title.lower() and q not in artist.lower() and q not in album.lower()):
                continue
                
            key = (artist, album)
            if key not in groups:
                groups[key] = []
            groups[key].append(row)

        # 2. Crear un widget por cada álbum
        for (artist, album), tracks in sorted(groups.items(), key=lambda x: sort_key_artist(x[0][1])):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.set_size_request(150, -1)
            
            # Creamos la imagen con un ícono temporal para que la UI cargue al instante
            img = Gtk.Image()
            placeholder = self._make_square_placeholder(140)
            if placeholder:
                img.set_from_pixbuf(placeholder)
            else:
                img.set_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
            
            box.pack_start(img, False, False, 0)
            
            lbl_title = Gtk.Label()
            lbl_title.set_markup(f"<b>{GLib.markup_escape_text(album)}</b>")
            lbl_title.set_ellipsize(Pango.EllipsizeMode.END)
            lbl_title.set_max_width_chars(20)
            lbl_title.set_xalign(0.5)
            lbl_title.set_halign(Gtk.Align.FILL)
            lbl_title.set_justify(Gtk.Justification.CENTER)
            box.pack_start(lbl_title, False, False, 0)
            
            lbl_artist = Gtk.Label()
            lbl_artist.set_markup(f"<small>{GLib.markup_escape_text(artist)}</small>")
            lbl_artist.set_ellipsize(Pango.EllipsizeMode.END)
            lbl_artist.set_max_width_chars(20)
            lbl_artist.set_xalign(0.5)
            lbl_artist.set_halign(Gtk.Align.FILL)
            lbl_artist.set_justify(Gtk.Justification.CENTER)
            box.pack_start(lbl_artist, False, False, 0)
            
            event_box = Gtk.EventBox()
            event_box.add(box)
            event_box.album_name = album
            event_box.album_artist = artist
            event_box.album_tracks = tracks
            event_box.connect("button-press-event", self.on_album_box_button_press, artist, album, tracks)
            
            self.album_flowbox.add(event_box)
            
           
            # Lanzamos la carga real de la carátula en segundo plano
            uri_portada = tracks[0][COL_URI]
            self.executor.submit(self._load_album_cover_worker, uri_portada, img)

        self.album_flowbox.show_all()

    def on_album_box_button_press(self, widget, event, artist, album, tracks):
        if event.button == 3: # Clic derecho
            self._ctx_artist = artist
            self._ctx_album = album
            self._update_move_menu_sensitivity()
            self.context_menu.popup_at_pointer(event)
            return True
        elif event.button == 1: # Clic izquierdo
            if event.type == Gdk.EventType._2BUTTON_PRESS:
                # Doble clic: cancelar la acción del clic simple y reproducir
                if hasattr(self, "_album_click_timeout"):
                    GLib.source_remove(self._album_click_timeout)
                    del self._album_click_timeout
                self._play_album_tracks(artist, album, tracks)
            elif event.type == Gdk.EventType.BUTTON_PRESS:
                # Clic simple: esperar 250ms por si es un doble clic
                if hasattr(self, "_album_click_timeout"):
                    GLib.source_remove(self._album_click_timeout)
                self._album_click_timeout = GLib.timeout_add(250, self._open_album_detail, artist, album, tracks)
            return True
        return False

    def _load_album_cover_worker(self, uri, img_widget):
        if not hasattr(self, "_album_cover_mem"):
            self._album_cover_mem = {}
        cached = self._album_cover_mem.get(uri)
        if cached is not None:
            GLib.idle_add(self._update_album_cover, img_widget, cached)
            return
        pixbuf = self._load_cover_pixbuf_for_uri(uri, 140)
        if pixbuf:
            self._album_cover_mem[uri] = pixbuf
            GLib.idle_add(self._update_album_cover, img_widget, pixbuf)

    def _update_album_cover(self, img_widget, pixbuf):
        # Esta función vuelve al hilo principal para dibujar la imagen de forma segura
        if img_widget.get_parent() is not None:
            img_widget.set_from_pixbuf(pixbuf)
        return False

    def _on_album_back_clicked(self, widget):
        """Vuelve a la vista anterior: a los discos del artista si abrimos
        este álbum desde la vista de artistas, o a la cuadrícula de álbumes."""
        ctx = getattr(self, "_album_back_context", None)
        if ctx and ctx[0] == "artists":
            self.stack.set_visible_child_name("artists")
            self.artists_stack.set_visible_child_name("detail")
        else:
            self.albums_inner_stack.set_visible_child_name("grid")

    def on_album_activated(self, flowbox, child):
        # GTK envuelve nuestro widget en un FlowBoxChild. 
        # get_child() devuelve la caja real con nuestros datos.
        real_box = child.get_child()
        if not real_box or not hasattr(real_box, "album_name"):
            return

        album_name = real_box.album_name
        tracks = real_box.album_tracks
        
        self.album_detail_title.set_markup(f"<b><span size='x-large'>{GLib.markup_escape_text(album_name)}</span></b>")
        self.album_tracks_store.clear()
        
        # Ordenar las canciones del álbum por número de pista
        tracks_sorted = sorted(tracks, key=lambda r: (int(r[COL_TRACK_NUM]) if int(r[COL_TRACK_NUM]) > 0 else 9999, r[COL_TITLE].lower()))
        
        uris_to_play = []
        for r in tracks_sorted:
            self.album_tracks_store.append([r[COL_URI], r[COL_TITLE], r[COL_DURATION]])
            uris_to_play.append(r[COL_URI])
            
        # Mostramos la lista de canciones del álbum
        self.albums_inner_stack.set_visible_child_name("detail")
        
        # Reproducimos y encolamos el álbum
        if uris_to_play:
            self.play_uris(uris_to_play, 0)

        # Marcar la canción que está sonando en la lista del álbum
        self._highlight_playing_in_album_detail()

    def on_album_tracks_button_press(self, widget, event):
        if event.button == 3:
            path_info = widget.get_path_at_pos(int(event.x), int(event.y))
            if path_info is not None:
                path = path_info[0]
                sel = widget.get_selection()
                if not sel.path_is_selected(path):
                    sel.unselect_all()
                    sel.select_path(path)
            self._update_move_menu_sensitivity()
            self.context_menu.popup_at_pointer(event)
            return True
        return False

    def on_album_track_activated(self, tree, path, column):
        it = self.album_tracks_store.get_iter(path)
        uri = self.album_tracks_store.get_value(it, 0)
        self.play_uri(uri)

    def _highlight_playing_in_album_detail(self):
        """Marca con la selección del sistema la fila de la canción que está sonando
        en la vista detalle del álbum (cuando se abre un disco desde las carátulas)."""
        if not self.current_uri:
            return
        it = self.album_tracks_store.get_iter_first()
        while it is not None:
            if self.album_tracks_store.get_value(it, 0) == self.current_uri:
                path = self.album_tracks_store.get_path(it)
                self.album_tracks_view.get_selection().unselect_all()
                self.album_tracks_view.get_selection().select_path(path)
                self.album_tracks_view.scroll_to_cell(path, None, False, 0, 0)
                return
            it = self.album_tracks_store.iter_next(it)


    def _rebuild_artists_view(self, query=""):
        for row in self.artists_listbox.get_children():
            self.artists_listbox.remove(row)
            
        q = (query or "").lower().strip()
        groups = {}
        for row in self.store:
            artist = row[COL_ARTIST] or "(Sin artista)"
            uri = row[COL_URI]
            
            if self.current_playlist_uris is not None and uri not in self.current_playlist_uris:
                continue
            if q and q not in artist.lower():
                continue
                
            if artist not in groups:
                groups[artist] = []
            groups[artist].append(row)
            
        for artist in sorted(groups.keys(), key=sort_key_artist):
            tracks = groups[artist]
            row = Gtk.ListBoxRow()
            
            # Caja horizontal con más espaciado
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
            hbox.set_margin_start(12)
            hbox.set_margin_end(12)
            hbox.set_margin_top(10)
            hbox.set_margin_bottom(10)
            
            # Imagen un poco más grande (80x80)
            img = Gtk.Image()
            img.set_size_request(80, 80)
            img.set_halign(Gtk.Align.CENTER)
            img.set_valign(Gtk.Align.CENTER)
            placeholder = self._make_square_placeholder(80)
            if placeholder:
                img.set_from_pixbuf(placeholder)
            
            uri_cover = tracks[0][COL_URI]
            self.executor.submit(self._load_artist_image_worker, uri_cover, img)
            
            hbox.pack_start(img, False, False, 0)
            
            # Caja vertical para el texto
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            vbox.set_valign(Gtk.Align.CENTER) # Centrar verticalmente respecto a la imagen
            
            # Nombre del artista en negrita y grande
            lbl_name = Gtk.Label(xalign=0)
            lbl_name.set_markup(f"<span size='x-large' weight='bold'>{GLib.markup_escape_text(artist)}</span>")
            lbl_name.set_ellipsize(Pango.EllipsizeMode.END)
            vbox.pack_start(lbl_name, False, False, 0)
            
            # Subtítulo con el número de discos y canciones
            n_albums = len(set(r[COL_ALBUM] for r in tracks))
            n_tracks = len(tracks)
            lbl_info = Gtk.Label(xalign=0)
            lbl_info.set_markup(_f("<span size='small' foreground='#888888'>{} álbum(es) · {} canciones</span>", n_albums, n_tracks))
            vbox.pack_start(lbl_info, False, False, 0)
            
            hbox.pack_start(vbox, True, True, 0)
            
            row.add(hbox)
            row.artist_name = artist
            row.artist_tracks = tracks
            self.artists_listbox.add(row)
            
        self.artists_listbox.show_all()

    # ---------- Letras ----------

    def request_lyrics(self, artist, title):
        self.lyrics_request_id += 1
        my_id = self.lyrics_request_id
        buf = self.lyrics_view.get_buffer()
        if not artist or not title:
            buf.set_text(_('Esta pista no tiene artista o título; no se puede buscar la letra.'))
            return
        buf.set_text(f"Buscando letra para {artist} — {title}…")
        def worker():
            text, err = get_lyrics(artist, title)
            GLib.idle_add(finish, text, err)
        def finish(text, err):
            if my_id != self.lyrics_request_id or self.shutting_down:
                return False
            buf.set_text(err if err else text)
            return False
        threading.Thread(target=worker, daemon=True).start()

    def reset_lyrics(self):
        self.lyrics_request_id += 1
        self.lyrics_view.get_buffer().set_text(LYRICS_DEFAULT)

    # ---------- Importar ----------

    def on_add_files(self, btn):
        if self.import_in_progress:
            return
        chooser = Gtk.FileChooserDialog(
            title=_('Añadir archivos de audio'), parent=self,
            action=Gtk.FileChooserAction.OPEN)
        chooser.add_buttons("Cancelar", Gtk.ResponseType.CANCEL,
                            "Añadir", Gtk.ResponseType.OK)
        chooser.set_select_multiple(True)
        filt = Gtk.FileFilter()
        filt.set_name("Audio")
        for ext in AUDIO_EXT:
            filt.add_pattern("*" + ext)
        chooser.add_filter(filt)
        if chooser.run() == Gtk.ResponseType.OK:
            paths = chooser.get_filenames()
            chooser.destroy()
            self.start_import(paths)
        else:
            chooser.destroy()

    def _scan_system_music_folder(self):
        """Escanea en segundo plano la carpeta de música del sistema por si
        hay archivos nuevos que importar (sin congelar la interfaz)."""
        if self.shutting_down:
            return False
            
        music_dir = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_MUSIC)
        if not music_dir:
            music_dir = os.path.expanduser("~/Música")
            if not os.path.isdir(music_dir):
                music_dir = os.path.expanduser("~/Music")
                
        if not music_dir or not os.path.isdir(music_dir):
            return False

        paths = []
        for root, _, files in os.walk(music_dir):
            for f in sorted(files):
                if f.lower().endswith(AUDIO_EXT):
                    paths.append(os.path.join(root, f))
                    
        if paths:
            self.start_import(paths)
            
        return False

    def _setup_watchers(self):
        for d in self.watched_dirs:
            self._watch_dir_recursive(d)

    def _watch_dir_recursive(self, path):
        if not os.path.isdir(path): return
        try:
            if path in self._monitored_paths:
                return
            self._monitored_paths.add(path)

            gfile = Gio.File.new_for_path(path)
            monitor = gfile.monitor_directory(Gio.FileMonitorFlags.NONE, None)
            monitor.connect("changed", self._on_dir_changed, path)
            self.file_monitors.append(monitor)
            for root, dirs, files in os.walk(path):
                for d in dirs:
                    full_d = os.path.join(root, d)
                    if full_d in self._monitored_paths:
                        continue
                    self._monitored_paths.add(full_d)
                    gfile_d = Gio.File.new_for_path(full_d)
                    mon_d = gfile_d.monitor_directory(Gio.FileMonitorFlags.NONE, None)
                    mon_d.connect("changed", self._on_dir_changed, full_d)
                    self.file_monitors.append(mon_d)
        except Exception as e:
            print(f"Error al vigilar {path}: {e}")

    def _on_dir_changed(self, monitor, file, other_file, event_type, watched_path):
        if event_type == Gio.FileMonitorEvent.CREATED:
            path = file.get_path()
            if not path:
                return
            if os.path.isdir(path):
                # Es una CARPETA nueva: esperar 2 segundos a que se copie el contenido
                GLib.timeout_add_seconds(2, self._process_new_dir, path)
            elif path.lower().endswith(AUDIO_EXT):
                # Es un ARCHIVO de audio nuevo
                GLib.timeout_add_seconds(2, self._process_new_file, path)

    def _process_new_file(self, path):
        if os.path.exists(path):
            self.add_single_track_to_library(path)
            self._apply_filters()
            self.status_label.set_text(f"Nuevo archivo detectado: {os.path.basename(path)}")
        return False

    def _process_new_dir(self, path):
        """Se ejecuta cuando se crea una carpeta nueva. La vigila y
        busca música dentro de ella."""
        if self.shutting_down or not os.path.isdir(path):
            return False
        
        # 1. Empezar a vigilar esta nueva carpeta (y sus subcarpetas)
        self._watch_dir_recursive(path)
        
        # 2. Recopilar los archivos de audio que haya dentro
        paths = []
        for root, _dir, files in os.walk(path):
            for f in sorted(files):
                if f.lower().endswith(AUDIO_EXT):
                    paths.append(os.path.join(root, f))
                    
        # 3. Importarlos (la función start_import ya evita duplicados)
        if paths:
            self.start_import(paths)
            
        self.status_label.set_text(f"Nueva carpeta detectada e importada: {os.path.basename(path)}")
        return False


    def on_add_folder(self, btn):
        if self.import_in_progress:
            return
        chooser = Gtk.FileChooserDialog(
            title=_('Añadir carpeta de música'), parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER)
        chooser.add_buttons("Cancelar", Gtk.ResponseType.CANCEL,
                            "Añadir", Gtk.ResponseType.OK)
        if chooser.run() == Gtk.ResponseType.OK:
            folder = chooser.get_filename()
            chooser.destroy()
            paths = []
            for root, _dir, files in os.walk(folder):
                for f in sorted(files):
                    if f.lower().endswith(AUDIO_EXT):
                        paths.append(os.path.join(root, f))
            self.start_import(paths)
            if folder not in self.watched_dirs:
                self.watched_dirs.append(folder)
                self.prefs["watched_dirs"] = self.watched_dirs
                preferences.save(self.prefs)
                self._watch_dir_recursive(folder)
        else:
            chooser.destroy()

    def start_import(self, paths):
        if not paths or self.import_in_progress:
            return
        new_paths = []
        for p in paths:
            uri = GLib.filename_to_uri(os.path.abspath(p), None)
            if uri not in self.known_uris:
                new_paths.append(p)
        skipped = len(paths) - len(new_paths)
        if not new_paths:
            self.status_label.set_text(
                _f("{} pistas (todas ya estaban en la lista)", len(self.store)))
            return
        self.import_in_progress = True
        self.import_cancelled = False
        self.import_total = len(new_paths)
        self.import_done = 0
        self.import_skipped = skipped
        msg = f"Importando 0/{self.import_total}"
        if skipped:
            msg += f" (saltando {skipped} ya importadas)"
        self.import_bar.set_fraction(0.0)
        self.import_bar.set_text(msg + "…")
        self.import_box.show_all()
        self.btn_add_music.set_sensitive(False)
        self.status_label.set_text(f"Importando {self.import_total} pistas…")
        threading.Thread(target=self._import_worker,
                         args=(new_paths,), daemon=True).start()

    def _import_worker(self, paths):
        for p in paths:
            if self.import_cancelled or self.shutting_down:
                break
            try:
                uri = GLib.filename_to_uri(os.path.abspath(p), None)
                info = self.discoverer.discover_uri(uri)
            except Exception as e:
                print(f"Error con {p}: {e}")
                info = None
                uri = GLib.filename_to_uri(os.path.abspath(p), None)
            title = os.path.basename(p)
            artist = ""
            album = ""
            dur_ns = 0
            track_num = 0
            year = 0
            genre = ""
            albumartist = ""
            cd_num = 0
            
            if info is not None:
                tags = info.get_tags()
                if tags is not None:
                    exists, val = tags.get_string_index("title", 0)
                    if exists and val: title = val
                    exists, val = tags.get_string_index("artist", 0)
                    if exists and val: artist = val
                    exists, val = tags.get_string_index("album", 0)
                    if exists and val: album = val
                    exists, tn = tags.get_uint_index("track-number", 0)
                    if exists and tn: track_num = int(tn)
                    else:
                        exists, ts = tags.get_string_index("track-number", 0)
                        if exists and ts:
                            try: track_num = int(ts.split("/")[0].strip())
                            except Exception: pass
                    
                    exists, val = tags.get_string_index("date", 0)
                    if exists and val:
                        try: year = int(val[:4])
                        except: pass
                    exists, val = tags.get_string_index("genre", 0)
                    if exists and val: genre = val
                    exists, val = tags.get_string_index("album-artist", 0)
                    if exists and val: albumartist = val
                    exists, cn = tags.get_uint_index("album-disc-number", 0)
                    if exists and cn: cd_num = int(cn)
                    
                if track_num == 0:
                    track_num = infer_track_num(p)
                dur_ns = info.get_duration()
                
            GLib.idle_add(self._add_row_safe, title, artist, album, dur_ns,
                          uri, track_num, year, genre, albumartist, cd_num)
        GLib.idle_add(self._import_finished)

    def _add_row_safe(self, title, artist, album, dur_ns, uri, track_num, year=0, genre="", albumartist="", cd_num=0):
        if self.shutting_down:
            return False
        now = datetime.datetime.now().isoformat(timespec="seconds")
        self.store.append([
            title, artist, album, self.fmt_time(dur_ns), uri, track_num,
            len(self.store), 0, 0, "", now, year, genre, albumartist, cd_num,
        ])
        self.known_uris.add(uri)
        self.import_done += 1
        frac = self.import_done / self.import_total if self.import_total else 0
        self.import_bar.set_fraction(frac)
        msg = f"Importando {self.import_done}/{self.import_total}"
        if self.import_skipped:
            msg += f" (saltando {self.import_skipped} ya importadas)"
        self.import_bar.set_text(msg + "…")
        return False

    def _import_finished(self):
        if self.shutting_down:
            return False
        self.import_in_progress = False
        self.import_box.hide()
        self.btn_add_music.set_sensitive(True)
        self._apply_filters()
        self._save_library()
        return False

    def on_cancel_import(self, btn):
        self.import_cancelled = True
        self.import_bar.set_text(_('Cancelando…'))

    def on_clear(self, btn):
        if self.import_in_progress:
            self.import_cancelled = True
        if len(self.store) == 0:
            return
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=_('¿Vaciar la biblioteca?'))
        dialog.format_secondary_text(
            f"Se eliminarán {len(self.store)} pistas de la lista.\n"
            "Los archivos de audio originales no se tocan.\n"
            "Las listas de reproducción no se eliminan.")
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.YES:
            return
        self.playbin.set_state(Gst.State.NULL)
        self.store.clear()
        self.view_store.clear()
        self.tree_store.clear()
        self.known_uris.clear()
        self.current_index = -1
        self.current_uri = None
        self.shuffle_history = []
        self.shuffle_pool = []
        self.play_queue = []
        self.playback_context = []
        self._refresh_queue_view()
        self._set_play_icon(False)
        self.now_playing_label.set_text("—")
        self.np_title.set_markup(_("<b><span size='large'>—</span></b>"))
        self.np_artist.set_markup(_("<span size='medium'>—</span>"))
        self.np_album.set_markup(_("<span size='small'>—</span>"))
        self.cover_image.set_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
        self.cover_image.set_pixel_size(296)
        self.progress.set_value(0)
        self.pos_label.set_text("0:00")
        self.dur_label.set_text("0:00")
        self.search_entry.set_text("")
        self.search_query = ""
        self.reset_lyrics()
        self._save_library()
        self._apply_filters()

    # ---------- Reproducción ----------

    def _visible_uris(self):
        uris = []
        it = self.view_store.get_iter_first()
        while it is not None:
            uris.append(self.view_store.get_value(it, VCOL_URI))
            it = self.view_store.iter_next(it)
        return uris

    def _next_shuffle(self):
        uris = self.playback_context if self.playback_context else self._visible_uris()
        if not uris:
            return False
        if self.current_uri and (not self.shuffle_history or
                                 self.shuffle_history[-1] != self.current_uri):
            self.shuffle_history.append(self.current_uri)
        if not self.shuffle_pool:
            self.shuffle_pool = [u for u in uris if u != self.current_uri]
            random.shuffle(self.shuffle_pool)
        if not self.shuffle_pool:
            return False
        next_uri = self.shuffle_pool.pop(0)
        self.shuffle_history.append(next_uri)
        idx = self.find_index_by_uri(next_uri)
        if idx >= 0:
            self._play_index_raw(idx)
            return True
        return False

    def _prev_shuffle(self):
        if len(self.shuffle_history) < 2:
            return False
        self.shuffle_history.pop()
        prev_uri = self.shuffle_history.pop()
        if self.current_uri:
            self.shuffle_pool.insert(0, self.current_uri)
        idx = self.find_index_by_uri(prev_uri)
        if idx >= 0:
            self._play_index_raw(idx)
            self.shuffle_history.append(prev_uri)
            return True
        return False

    def on_row_activated(self, tree, path, column):
        it = self.view_store.get_iter(path)
        self.playback_context = self._visible_uris()
        self.play_uri(self.view_store.get_value(it, VCOL_URI))

    def find_index_by_uri(self, uri):
        for i, row in enumerate(self.store):
            if row[COL_URI] == uri:
                return i
        return -1

    def play_uri(self, uri):
        idx = self.find_index_by_uri(uri)
        if idx >= 0:
            self.play_index(idx)

    def _select_song_row_by_uri(self, uri):
        it = self.view_store.get_iter_first()
        while it is not None:
            if self.view_store.get_value(it, VCOL_URI) == uri:
                path = self.view_store.get_path(it)
                self.tree.get_selection().unselect_all()
                self.tree.get_selection().select_path(path)
                self.tree.scroll_to_cell(path, None, False, 0, 0)
                
                # Dar foco al árbol de canciones para que la selección
                # se pinte con el color azul del sistema y no en gris.
                # Solo se lo quitamos al buscador si el usuario está escribiendo.
                focused = self.get_focus()
                if focused != self.search_entry:
                    self.tree.grab_focus()
                return True
            it = self.view_store.iter_next(it)
        return False

    def _show_notification_with_cover(self, title, artist, album, uri):
        """Carga la carátula en segundo plano y muestra la notificación."""
        # 1. Intentar extraer la carátula (esto puede tardar un poco)
        pixbuf = self._load_cover_pixbuf_for_uri(uri, 64) # 64px es buen tamaño para notificaciones
        
        def show():
            try:
                notif_title = title if title else "Reproduciendo"
                notif_body = f"{artist} — {album}" if artist and album else (artist or album or "Música")
                
                # Creamos la notificación SIN icono por defecto
                notification = Notify.Notification.new(notif_title, notif_body)
                
                # Si se encontró la carátula, se la ponemos
                if pixbuf:
                    notification.set_image_from_pixbuf(pixbuf)
                else:
                    # Si no hay carátula, no ponemos imagen.
                    # (Si prefieres que salga el icono genérico cuando no hay carátula, 
                    # cambia la línea de arriba por: notification.set_image_from_pixbuf(Gtk.IconTheme.get_default().load_icon("audio-x-generic", 64, 0)))
                    pass
                
                notification.set_timeout(3000)
                notification.show()
            except Exception as e:
                print(f"[notify] Error al mostrar notificación: {e}")
            return False

        # Volver al hilo principal de GTK para dibujar la notificación
        GLib.idle_add(show)

    def _play_index_raw(self, idx):
        if idx < 0 or idx >= len(self.store):
            return
        it = self.store.get_iter(Gtk.TreePath.new_from_string(str(idx)))
        uri = self.store.get_value(it, COL_URI)
        title = self.store.get_value(it, COL_TITLE)
        artist = self.store.get_value(it, COL_ARTIST)
        album = self.store.get_value(it, COL_ALBUM)

        self.current_index = idx
        self.current_uri = uri
        self.duration_ns = 0
        self.pos_label.set_text("0:00")
        self.dur_label.set_text("0:00")
        self.progress.set_value(0)

        self.playbin.set_state(Gst.State.NULL)
        self.playbin.set_property("uri", uri)
        self.playbin.set_state(Gst.State.PLAYING)
        self._set_play_icon(True)
        self._update_mpris_status("Playing")
        self._update_mpris_metadata(title, artist, album)

        self.now_playing_label.set_text(f"{artist} — {title}" if artist else title)
        self.status_label.set_text(_f("Reproduciendo: {} — {}", artist, title) if artist else _f("Reproduciendo: {}", title))

        self.np_title.set_markup(
            f"<b><span size='large'>{GLib.markup_escape_text(title)}</span></b>")
        self.np_artist.set_markup(
            f"<span size='medium'>{GLib.markup_escape_text(artist)}</span>")
        self.np_album.set_markup(
            f"<span size='small'>{GLib.markup_escape_text(album)}</span>")

        self.cover_image.set_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
        self.cover_image.set_pixel_size(296)
        threading.Thread(
            target=self._load_cover_art, 
            args=(uri,), 
            kwargs={"artist": artist, "album": album}, 
            daemon=True
        ).start()

        self._select_song_row_by_uri(uri)
        if (self.stack.get_visible_child_name() == "albums"
                and self.albums_inner_stack.get_visible_child_name() == "detail"):
            self._highlight_playing_in_album_detail()
        self.request_lyrics(artist, title)

        # Lanzar la notificación con carátula en segundo plano
        threading.Thread(target=self._show_notification_with_cover, args=(title, artist, album, uri), daemon=True).start()

        if self._last_play_counted_uri != uri:
            self._last_play_counted_uri = uri
            pc = self.store.get_value(it, COL_PLAY_COUNT)
            self.store.set_value(it, COL_PLAY_COUNT, pc + 1)
            self.store.set_value(
                it, COL_LAST_PLAYED,
                datetime.datetime.now().isoformat(timespec="seconds"))
            self._save_library()
            vit = self.view_store.get_iter_first()
            while vit is not None:
                if self.view_store.get_value(vit, VCOL_URI) == uri:
                    self.view_store.set_value(vit, VCOL_PLAY_COUNT, pc + 1)
                    break
                vit = self.view_store.iter_next(vit)


    def play_index(self, idx):
        if self.shuffle_enabled and idx >= 0 and idx < len(self.store):
            it = self.store.get_iter(Gtk.TreePath.new_from_string(str(idx)))
            uri = self.store.get_value(it, COL_URI)
            if not self.shuffle_history or self.shuffle_history[-1] != uri:
                self.shuffle_history.append(uri)
            if uri in self.shuffle_pool:
                self.shuffle_pool.remove(uri)
        self._play_index_raw(idx)

    def play_relative(self, offset):
        if offset > 0 and self.play_queue and not self.shuffle_enabled:
            next_uri = self.play_queue.pop(0)
            self._refresh_queue_view()
            idx = self.find_index_by_uri(next_uri)
            if idx >= 0:
                self._play_index_raw(idx)
                return True
            return self.play_relative(offset)

        if self.shuffle_enabled:
            if offset > 0:
                return self._next_shuffle()
            elif offset < 0:
                return self._prev_shuffle()
            return False

        uris = self._visible_uris()
        if not uris:
            return False
        if self.current_uri not in uris:
            self.play_uri(uris[0])
            return True
        idx = uris.index(self.current_uri)
        new_idx = idx + offset
        if 0 <= new_idx < len(uris):
            self.play_uri(uris[new_idx])
            return True
        if self.repeat_mode == "all":
            self.play_uri(uris[0])
            return True
        return False

    def on_play_pause(self, btn):
        if not self.current_uri:
            uris = self._visible_uris()
            if uris:
                if self.shuffle_enabled:
                    random.shuffle(uris)
                self.play_uris(uris, 0)
            return
        _, state, _ = self.playbin.get_state(0)
        if state == Gst.State.PLAYING:
            self.playbin.set_state(Gst.State.PAUSED)
            self._set_play_icon(False)
            self._update_mpris_status("Paused")
        else:
            self.playbin.set_state(Gst.State.PLAYING)
            self._set_play_icon(True)
            self._update_mpris_status("Playing")

    def on_progress_pressed(self, widget, event):
        self.user_dragging = True

    def on_progress_released(self, widget, event):
        self.user_dragging = False
        if self.duration_ns > 0:
            pos = int(self.progress.get_value() / 100.0 * self.duration_ns)
            self.playbin.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, pos)

    def update_position(self):
        if self.shutting_down:
            return False
        if self.current_uri is None:
            return True

        # IMPORTANTE: durante buffering / transición de estado, GStreamer
        # puede bloquear el hilo de GTK si llamamos query_duration /
        # query_position. Solo consultamos cuando el pipeline está estable.
        try:
            _, state, pending = self.playbin.get_state(0)
        except Exception:
            return True
        if pending != Gst.State.VOID_PENDING:
            # El pipeline está en transición (buffering, cambio de estado…)
            return True
        if state not in (Gst.State.PLAYING, Gst.State.PAUSED):
            return True

        if self.duration_ns == 0:
            ok, dur = self.playbin.query_duration(Gst.Format.TIME)
            if ok and dur > 0:
                self.duration_ns = dur
                self.dur_label.set_text(self.fmt_time(dur))
        if not self.user_dragging:
            ok, pos = self.playbin.query_position(Gst.Format.TIME)
            if ok:
                self.pos_label.set_text(self.fmt_time(pos))
                if self.duration_ns > 0:
                    self.progress.set_value(pos / self.duration_ns * 100.0)
        return True

    def fmt_time(self, ns):
        if not ns:
            return "0:00"
        s = ns // Gst.SECOND
        return f"{s // 60}:{s % 60:02d}"

    def on_bus_message(self, bus, msg):
        t = msg.type
        if t == Gst.MessageType.EOS:
            if self.play_queue:
                self.play_relative(+1)
                return
            if self.repeat_mode == "one" and self.current_index >= 0:
                self._play_index_raw(self.current_index)
                return
            if not self.play_relative(+1):
                self.playbin.set_state(Gst.State.NULL)
                self._set_play_icon(False)
                self.progress.set_value(0)
                self.pos_label.set_text("0:00")
        elif t == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            print(f"ERROR GStreamer: {err.message}\n{debug}")
            self.playbin.set_state(Gst.State.NULL)
        elif t == Gst.MessageType.DURATION_CHANGED:
            ok, dur = self.playbin.query_duration(Gst.Format.TIME)
            if ok and dur > 0:
                self.duration_ns = dur
                self.dur_label.set_text(self.fmt_time(dur))

    # ---------- Podcasts UI ----------

    def _build_podcasts_page(self):
        nb = Gtk.Notebook()
        discover = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        discover.set_border_width(8)
        searchbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.podcast_search = Gtk.SearchEntry()
        self.podcast_search.set_placeholder_text(_('Buscar podcasts en iTunes…'))
        self.podcast_search.connect("activate", self.on_podcast_search)
        searchbar.pack_start(self.podcast_search, True, True, 0)
        self.podcast_search_btn = Gtk.Button(label=_('Buscar'))
        self.podcast_search_btn.connect("clicked", self.on_podcast_search)
        searchbar.pack_start(self.podcast_search_btn, False, False, 0)
        discover.pack_start(searchbar, False, False, 0)
        self.podcast_status = Gtk.Label(label="")
        self.podcast_status.set_halign(Gtk.Align.START)
        discover.pack_start(self.podcast_status, False, False, 0)
        self.podcast_results_store = Gtk.ListStore(str, str, str, str)
        self.podcast_results_view = Gtk.TreeView(model=self.podcast_results_store)
        for i, name in enumerate((_("Título"), _("Autor"))):
            r = Gtk.CellRendererText()
            if i == 0:
                r.set_property("ellipsize", Pango.EllipsizeMode.END)
            col = Gtk.TreeViewColumn(name, r, text=i)
            col.set_resizable(True)
            if i == 0:
                col.set_expand(True)
            self.podcast_results_view.append_column(col)
        self.podcast_results_view.connect("row-activated", self.on_podcast_result_activated)
        sc = Gtk.ScrolledWindow()
        sc.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sc.add(self.podcast_results_view)
        discover.pack_start(sc, True, True, 0)
        sub_btn = Gtk.Button(label=_('Suscribirse al seleccionado'))
        sub_btn.connect("clicked", self.on_subscribe_selected)
        discover.pack_start(sub_btn, False, False, 0)
        nb.append_page(discover, Gtk.Label(label=_('Descubrir')))

        mine = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        mine.set_position(260)
        self.subs_store = Gtk.ListStore(str, str, str)
        self.subs_view = Gtk.TreeView(model=self.subs_store)
        r = Gtk.CellRendererText()
        r.set_property("ellipsize", Pango.EllipsizeMode.END)
        col = Gtk.TreeViewColumn(_("Mis podcasts"), r, text=0)
        col.set_expand(True)
        self.subs_view.append_column(col)
        self.subs_view.get_selection().connect("changed", self.on_sub_selected)
        subs_scroll = Gtk.ScrolledWindow()
        subs_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        subs_scroll.add(self.subs_view)
        mine.pack1(subs_scroll, False, False)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        right.set_border_width(8)
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.sub_title_label = Gtk.Label(label=_('Selecciona un podcast'))
        self.sub_title_label.set_halign(Gtk.Align.START)
        self.sub_title_label.set_ellipsize(Pango.EllipsizeMode.END)
        toolbar.pack_start(self.sub_title_label, True, True, 0)
        self.refresh_btn = Gtk.Button(label=_('Actualizar'))
        self.refresh_btn.connect("clicked", self.on_refresh_feed)
        toolbar.pack_start(self.refresh_btn, False, False, 0)
        self.unsub_btn = Gtk.Button(label=_('Desuscribirse'))
        self.unsub_btn.connect("clicked", self.on_unsubscribe)
        toolbar.pack_start(self.unsub_btn, False, False, 0)
        self.send_podcast_btn = Gtk.Button(label=_('Enviar al iPod'))
        self.send_podcast_btn.connect("clicked", self.on_send_podcast_to_ipod)
        toolbar.pack_start(self.send_podcast_btn, False, False, 0)
        right.pack_start(toolbar, False, False, 0)
        self.download_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.download_box.set_no_show_all(True)
        right.pack_start(self.download_box, False, False, 0)
        self.download_bar = Gtk.ProgressBar()
        self.download_bar.set_hexpand(True)
        self.download_bar.set_show_text(True)
        self.download_box.pack_start(self.download_bar, True, True, 0)
        self.episodes_status = Gtk.Label(label="")
        self.episodes_status.set_halign(Gtk.Align.START)
        right.pack_start(self.episodes_status, False, False, 0)
        self.episodes_store = Gtk.ListStore(str, str, str, str, int, str, bool, bool)
        self.episodes_view = Gtk.TreeView(model=self.episodes_store)
        self.episodes_view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        for i, name in enumerate((_("Episodio"), _("Fecha"), _("Dur."))):
            r = Gtk.CellRendererText()
            if i == 0:
                r.set_property("ellipsize", Pango.EllipsizeMode.END)
            col = Gtk.TreeViewColumn(name, r, text=i)
            col.set_resizable(True)
            if i == 0:
                col.set_expand(True)
            self.episodes_view.append_column(col)
        col_status = Gtk.TreeViewColumn(_("Estado"))
        col_status.set_resizable(True)
        col_status.set_min_width(160)
        r_prog = Gtk.CellRendererProgress()
        col_status.pack_start(r_prog, True)
        col_status.add_attribute(r_prog, "visible", 6)
        col_status.add_attribute(r_prog, "value", 4)
        col_status.add_attribute(r_prog, "text", 3)
        r_text = Gtk.CellRendererText()
        col_status.pack_start(r_text, True)
        col_status.add_attribute(r_text, "visible", 7)
        col_status.add_attribute(r_text, "text", 3)
        self.episodes_view.append_column(col_status)
        self.episodes_view.connect("row-activated", self.on_episode_activated)
        self.episodes_view.connect("button-press-event", self.on_episodes_button_press)
        ep_scroll = Gtk.ScrolledWindow()
        ep_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        ep_scroll.add(self.episodes_view)
        right.pack_start(ep_scroll, True, True, 0)
        mine.pack2(right, True, False)
        nb.append_page(mine, Gtk.Label(label=_('Mis podcasts')))
        self._refresh_subs_store()
        return nb

    def _refresh_subs_store(self):
        self.subs_store.clear()
        for s in self.subscriptions:
            self.subs_store.append([s.get("title", "?"), s.get("author", ""), s.get("feed_url", "")])

    def on_podcast_search(self, *args):
        if self.podcast_search_running:
            return
        q = self.podcast_search.get_text().strip()
        if not q:
            return
        self.podcast_search_running = True
        self.podcast_search_btn.set_sensitive(False)
        self.podcast_status.set_text(_f("Buscando «{}» en iTunes…", q))
        def worker():
            results, err = podcasts.search_itunes(q)
            GLib.idle_add(done, results, err)
        def done(results, err):
            if self.shutting_down:
                return False
            self.podcast_search_running = False
            self.podcast_search_btn.set_sensitive(True)
            self.podcast_results_store.clear()
            if err:
                self.podcast_status.set_text(err)
                return False
            for r in results:
                self.podcast_results_store.append([r["title"], r["author"], r["feed_url"], r["artwork_url"]])
            self.podcast_status.set_text(_f("{} resultados", len(results)))
            return False
        threading.Thread(target=worker, daemon=True).start()

    def _already_subscribed(self, feed_url):
        return any(s.get("feed_url") == feed_url for s in self.subscriptions)

    def on_subscribe_selected(self, *args):
        sel = self.podcast_results_view.get_selection()
        model, it = sel.get_selected()
        if it is None:
            self.podcast_status.set_text(_('Selecciona un podcast primero'))
            return
        title = model.get_value(it, 0)
        author = model.get_value(it, 1)
        feed_url = model.get_value(it, 2)
        artwork_url = model.get_value(it, 3)
        if self._already_subscribed(feed_url):
            self.podcast_status.set_text(_f("«{}» ya estaba suscrito", title))
            return
        self.subscriptions.append({"title": title, "author": author, "feed_url": feed_url, "artwork_url": artwork_url})
        podcasts.save_subscriptions(self.subscriptions)
        self._refresh_subs_store()
        self.podcast_status.set_text(_f("Suscrito a «{}»", title))

    def on_podcast_result_activated(self, tree, path, column):
        self.on_subscribe_selected()

    def on_sub_selected(self, sel):
        model, it = sel.get_selected()
        if it is None:
            self.sub_title_label.set_text(_('Selecciona un podcast'))
            self.episodes_store.clear()
            return
        title = model.get_value(it, 0)
        self.sub_title_label.set_text(title)
        # Al seleccionar, mostramos los episodios descargados localmente.
        # No requiere internet.
        local_eps = self._scan_local_episodes(title)
        n_down = self._populate_episodes_store(local_eps)
        if n_down > 0:
            self.episodes_status.set_text(
                _f("{} episodio(s) descargado(s). Pulsa 'Actualizar' para buscar nuevos en línea.",
                   n_down))
        else:
            self.episodes_status.set_text(
                _("No hay episodios descargados. Pulsa 'Actualizar' para buscarlos en línea."))

    def _current_feed_url(self):
        sel = self.subs_view.get_selection()
        model, it = sel.get_selected()
        if it is None:
            return None
        return model.get_value(it, 2)

    def _current_podcast_title(self):
        sel = self.subs_view.get_selection()
        model, it = sel.get_selected()
        if it is None:
            return None
        return model.get_value(it, 0)

    def _is_downloaded(self, podcast_title, episode_title):
        folder = os.path.join(PODCAST_DIR, podcasts.sanitize_filename(podcast_title))
        safe = podcasts.sanitize_filename(episode_title)
        pattern = os.path.join(folder, safe + ".*")
        for m in glob.glob(pattern):
            if not m.endswith(".part"):
                return True
        return False

    def _scan_local_episodes(self, podcast_title):
        """Devuelve los episodios descargados del podcast en disco.
        No requiere conexión a internet."""
        if not podcast_title:
            return []
        base_dir = os.path.join(
            PODCAST_DIR, podcasts.sanitize_filename(podcast_title))
        if not os.path.isdir(base_dir):
            return []
        episodes = []
        for path in sorted(glob.glob(os.path.join(base_dir, "*"))):
            if path.endswith(".part"):
                continue
            ext = os.path.splitext(path)[1].lower()
            if ext not in AUDIO_EXT:
                continue
            title = os.path.splitext(os.path.basename(path))[0]
            episodes.append({
                "title": title,
                "pub_date": "",
                "duration": "",
                "audio_url": GLib.filename_to_uri(path, None),
                "downloaded": True,
            })
        return episodes

    def _populate_episodes_store(self, combined):
        """Rellena el episodes_store con una lista de dicts que tengan
        {title, pub_date, duration, audio_url, downloaded}."""
        self.episodes_store.clear()
        n_downloaded = 0
        for ep in combined:
            if ep.get("downloaded"):
                status_text = _("Descargado")
                progress = 100
                has_prog = True
                no_prog = False
                n_downloaded += 1
            else:
                status_text = ""
                progress = -1
                has_prog = False
                no_prog = True
            self.episodes_store.append([
                ep.get("title", ""),
                ep.get("pub_date", ""),
                ep.get("duration", ""),
                status_text,
                progress,
                ep.get("audio_url", ""),
                has_prog,
                no_prog,
            ])
        return n_downloaded

    def on_refresh_feed(self, *args):
        if self.podcast_feed_running:
            return
        feed_url = self._current_feed_url()
        podcast_title = self._current_podcast_title()
        if not feed_url:
            self.episodes_status.set_text(_('Selecciona un podcast primero'))
            return
        self.podcast_feed_running = True
        self.refresh_btn.set_sensitive(False)

        # Mostrar locales inmediatamente mientras se consulta la red
        local_eps = self._scan_local_episodes(podcast_title)
        self._populate_episodes_store(local_eps)
        self.episodes_status.set_text(
            _('Buscando episodios nuevos en línea… (los descargados ya se muestran)'))

        def worker():
            data, err = podcasts.fetch_feed(feed_url)
            GLib.idle_add(done, data, err, podcast_title, local_eps)

        def done(data, err, podcast_title, local_eps):
            if self.shutting_down:
                return False
            self.podcast_feed_running = False
            self.refresh_btn.set_sensitive(True)

            # Sin conexión (o feed caído): quedarnos con los locales
            if err:
                self._populate_episodes_store(local_eps)
                self.episodes_status.set_text(
                    _f("Sin conexión o feed inaccesible. Mostrando {} episodio(s) descargado(s) localmente.",
                       len(local_eps)))
                return False

            self.current_feed_data = data
            remote_eps = data.get("episodes", [])

            # Comparar títulos ignorando mayúsculas, espacios y puntuación
            def _norm(t):
                return re.sub(r'[^a-z0-9]', '', (t or '').lower())

            local_by_norm = {_norm(e["title"]): e for e in local_eps}
            combined = []
            seen_norms = set()

            # 1. Iterar sobre los episodios remotos (usar título bonito)
            for ep in remote_eps:
                title = ep.get("title", "")
                norm = _norm(title)
                if not title or not norm:
                    continue
                seen_norms.add(norm)

                if norm in local_by_norm:
                    # Ya lo tenemos descargado
                    local_ep = local_by_norm[norm]
                    combined.append({
                        "title": title,  # Título bonito del feed
                        "pub_date": ep.get("pub_date", ""),
                        "duration": ep.get("duration", ""),
                        "audio_url": local_ep["audio_url"],  # Ruta local para reproducir
                        "downloaded": True,
                    })
                else:
                    # No descargado
                    combined.append({
                        "title": title,
                        "pub_date": ep.get("pub_date", ""),
                        "duration": ep.get("duration", ""),
                        "audio_url": ep.get("audio_url", ""),
                        "downloaded": False,
                    })

            # 2. Añadir episodios locales huérfanos (que ya no estén en el feed)
            for e in local_eps:
                norm = _norm(e["title"])
                if norm not in seen_norms:
                    combined.append(e)

            n_down = self._populate_episodes_store(combined)
            self.episodes_status.set_text(
                _f("{} episodios ({} descargados)", len(combined), n_down))
            return False

        threading.Thread(target=worker, daemon=True).start()

    def on_unsubscribe(self, *args):
        feed_url = self._current_feed_url()
        if not feed_url:
            return
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.YES_NO, text=_('¿Desuscribirse de este podcast?'))
        dialog.format_secondary_text("Se eliminará de la lista de suscripciones.\nLos episodios ya descargados en ~/Podcasts no se tocan.")
        if dialog.run() != Gtk.ResponseType.YES:
            dialog.destroy()
            return
        dialog.destroy()
        self.subscriptions = [s for s in self.subscriptions if s.get("feed_url") != feed_url]
        podcasts.save_subscriptions(self.subscriptions)
        self._refresh_subs_store()
        self.episodes_store.clear()
        self.sub_title_label.set_text(_('Selecciona un podcast'))
        self.episodes_status.set_text("")

    def on_episodes_button_press(self, widget, event):
        """Clic derecho sobre episodios: menú contextual de descarga."""
        if event.button != 3:
            return False
        path_info = widget.get_path_at_pos(int(event.x), int(event.y))
        if path_info is None:
            return False
        path = path_info[0]
        sel = widget.get_selection()
        if not sel.path_is_selected(path):
            # Si no se usó Ctrl, seleccionar solo esta fila
            if not (event.state & Gdk.ModifierType.CONTROL_MASK):
                sel.unselect_all()
            sel.select_path(path)
        self._update_episodes_context_menu()
        self.episodes_context_menu.popup_at_pointer(event)
        return True

    def _update_episodes_context_menu(self):
        """Habilita 'Descargar' solo si al menos uno está pendiente."""
        sel = self.episodes_view.get_selection()
        model, paths = sel.get_selected_rows()
        if not paths:
            self.episodes_menu_download.set_sensitive(False)
            return
        has_pending = False
        for path in paths:
            it = model.get_iter(path)
            progress = model.get_value(it, 4)
            if progress != 100:
                has_pending = True
                break
        self.episodes_menu_download.set_sensitive(has_pending)

    def on_episode_activated(self, tree, path, column):
        model = tree.get_model()
        it = model.get_iter(path)
        audio_url = model.get_value(it, 5)
        title = model.get_value(it, 0)
        if not audio_url:
            return

        self.current_index = -1
        self.current_uri = audio_url
        self.duration_ns = 0
        self.pos_label.set_text("0:00")
        self.dur_label.set_text("0:00")
        self.progress.set_value(0)

        # Cambio de stream: GStreamer maneja la transición internamente.
        # No hace falta esperar; set_state es asíncrono.
        self.playbin.set_state(Gst.State.NULL)
        self.playbin.set_property("uri", audio_url)
        self.playbin.set_state(Gst.State.PLAYING)
        self._set_play_icon(True)

        self.now_playing_label.set_text(f"[Podcast] {title}")
        self.np_title.set_markup(
            f"<b><span size='large'>{GLib.markup_escape_text(title)}</span></b>")
        self.np_artist.set_markup(_("<span size='medium'>Podcast</span>"))
        self.np_album.set_markup(_("<span size='small'></span>"))

        # Placeholder cuadrado mientras llega la carátula real
        placeholder = self._make_square_placeholder(296)
        if placeholder:
            self.cover_image.set_from_pixbuf(placeholder)
        else:
            self.cover_image.set_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
            self.cover_image.set_pixel_size(296)

        podcast_title = self._current_podcast_title()
        if podcast_title:
            threading.Thread(
                target=self._load_podcast_cover,
                args=(podcast_title,),
                daemon=True,
            ).start()

        self.reset_lyrics()
        self.lyrics_view.get_buffer().set_text(
            _('Este es un episodio de podcast, no tiene letra.'))

    def on_download_selected(self, *args):
        sel = self.episodes_view.get_selection()
        model, paths = sel.get_selected_rows()
        if not paths:
            self.episodes_status.set_text(_('Selecciona uno o más episodios (Ctrl+clic o Shift+clic)'))
            return
        podcast_title = self._current_podcast_title() or "Podcast"
        base_dir = os.path.join(PODCAST_DIR, podcasts.sanitize_filename(podcast_title))
        added = 0
        already = 0
        for path in paths:
            it = model.get_iter(path)
            audio_url = model.get_value(it, 5)
            title = model.get_value(it, 0)
            progress = model.get_value(it, 4)
            if progress == 100:
                already += 1
                continue
            if not audio_url:
                continue
            ref = Gtk.TreeRowReference.new(model, path)
            self.download_queue.append({"title": title, "audio_url": audio_url, "ref": ref, "base_dir": base_dir})
            model.set_value(it, 3, "En cola")
            model.set_value(it, 4, 0)
            model.set_value(it, 6, False)
            model.set_value(it, 7, True)
            added += 1
        if added == 0:
            self.episodes_status.set_text(f"Nada que descargar ({already} ya descargados)")
            return
        self.episodes_status.set_text(f"{added} en cola" + (f", {already} ya descargados" if already else ""))
        if not self.download_in_progress:
            self._process_next_download()

    def _process_next_download(self):
        if not self.download_queue:
            self.download_in_progress = False
            self.refresh_btn.set_sensitive(True)
            self.download_box.hide()
            return
        item = self.download_queue.pop(0)
        ref = item["ref"]
        if not ref.valid():
            self._process_next_download()
            return
        model = self.episodes_view.get_model()
        try:
            it = model.get_iter(ref.get_path())
        except Exception:
            self._process_next_download()
            return
        self.download_in_progress = True
        self.refresh_btn.set_sensitive(False)
        self.download_box.show_all()
        self.download_bar.set_fraction(0.0)
        remaining = len(self.download_queue) + 1
        self.download_bar.set_text(f"En cola: {remaining} restante(s). Descargando: {item['title'][:50]}")
        model.set_value(it, 3, _("Iniciando…"))
        model.set_value(it, 4, 0)
        model.set_value(it, 6, True)
        model.set_value(it, 7, False)
        def on_progress(downloaded, total):
            GLib.idle_add(self._update_download_progress, ref, downloaded, total)
        def worker():
            path, err = podcasts.download_episode(item["audio_url"], item["base_dir"], item["title"], on_progress=on_progress)
            GLib.idle_add(done, ref, err)
        def done(ref, err):
            if self.shutting_down:
                return False
            if ref.valid():
                try:
                    it = model.get_iter(ref.get_path())
                    if err:
                        model.set_value(it, 3, _f("Error: {}", err[:40]))
                        model.set_value(it, 4, -1)
                        model.set_value(it, 6, False)
                        model.set_value(it, 7, True)
                    else:
                        model.set_value(it, 3, _("Descargado"))
                        model.set_value(it, 4, 100)
                        model.set_value(it, 6, True)
                        model.set_value(it, 7, False)
                except Exception:
                    pass
            self._process_next_download()
            return False
        threading.Thread(target=worker, daemon=True).start()

    def _update_download_progress(self, ref, downloaded, total):
        if self.shutting_down or not ref.valid():
            return False
        try:
            model = self.episodes_view.get_model()
            it = model.get_iter(ref.get_path())
        except Exception:
            return False
        if total:
            frac = downloaded / total
            self.download_bar.set_fraction(frac)
            self.download_bar.set_text(f"{int(frac * 100)}% ({fmt_size(downloaded)} / {fmt_size(total)})")
            pct = int(frac * 100)
            model.set_value(it, 3, _f("Descargando {}%", pct))
            model.set_value(it, 4, pct)
            model.set_value(it, 6, True)
            model.set_value(it, 7, False)
        else:
            self.download_bar.pulse()
            self.download_bar.set_text(f"Descargando: {fmt_size(downloaded)}")
            model.set_value(it, 3, _f("Descargando ({})", fmt_size(downloaded)))
            model.set_value(it, 4, 0)
            model.set_value(it, 6, True)
            model.set_value(it, 7, False)
        return False

    # ---------- Smart playlists ----------

    def on_new_smart_playlist(self, btn):
        self._open_smart_playlist_dialog(playlist=None)

    def on_edit_smart_playlist(self, btn):
        if self.current_playlist_id is None:
            return
        pl = self._get_playlist(self.current_playlist_id)
        if pl is None or not playlists_mod.is_smart(pl):
            return
        self._open_smart_playlist_dialog(playlist=pl)

    def _open_smart_playlist_dialog(self, playlist=None):
        dialog = Gtk.Dialog(
            title="Nueva lista inteligente" if playlist is None
                  else f"Editar «{playlist['name']}»",
            transient_for=self,
            modal=True,
        )
        dialog.set_default_size(500, 500)
        dialog.add_buttons("Cancelar", Gtk.ResponseType.CANCEL,
                           "Guardar", Gtk.ResponseType.OK)
        area = dialog.get_content_area()
        area.set_spacing(8)
        area.set_border_width(10)

        name_label = Gtk.Label(label=_('Nombre:'))
        name_label.set_halign(Gtk.Align.START)
        area.pack_start(name_label, False, False, 0)
        name_entry = Gtk.Entry()
        if playlist is not None:
            name_entry.set_text(playlist["name"])
        area.pack_start(name_entry, False, False, 0)

        rules_label = Gtk.Label()
        rules_label.set_markup(_('<b>Reglas (todas deben cumplirse)</b>'))
        rules_label.set_halign(Gtk.Align.START)
        rules_label.set_margin_top(6)
        area.pack_start(rules_label, False, False, 0)

        rules_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        rules_box.set_margin_top(4)

        def add_rule_row(rule=None):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

            field_combo = Gtk.ComboBoxText()
            field_keys = list(playlists_mod.SMART_FIELDS.keys())
            for key in field_keys:
                field_combo.append(key, playlists_mod.SMART_FIELDS[key]["label"])
            if rule:
                field_combo.set_active_id(rule.get("field", "rating"))
            else:
                field_combo.set_active_id("rating")
            row.pack_start(field_combo, False, False, 0)

            op_combo = Gtk.ComboBoxText()
            value_entry = Gtk.Entry()
            value_entry.set_width_chars(15)

            def refresh_ops(*_):
                field = field_combo.get_active_id()
                info = playlists_mod.SMART_FIELDS.get(field)
                if not info:
                    return
                op_combo.remove_all()
                if info["type"] == "int":
                    ops = playlists_mod.SMART_OPERATORS_INT
                elif info["type"] == "text":
                    ops = playlists_mod.SMART_OPERATORS_TEXT
                else:
                    ops = playlists_mod.SMART_OPERATORS_DATE
                for op_key, op_label in ops:
                    op_combo.append(op_key, op_label)
                if rule and rule.get("field") == field:
                    op_combo.set_active_id(rule.get("op"))
                else:
                    op_combo.set_active_id(ops[0][0])

            field_combo.connect("changed", refresh_ops)
            refresh_ops()
            row.pack_start(op_combo, False, False, 0)

            if rule:
                value_entry.set_text(str(rule.get("value", "")))
            row.pack_start(value_entry, True, True, 0)

            remove_btn = Gtk.Button(label="✕")
            remove_btn.set_relief(Gtk.ReliefStyle.NONE)
            remove_btn.connect("clicked", lambda w: rules_box.remove(row))
            row.pack_start(remove_btn, False, False, 0)

            row._field_combo = field_combo
            row._op_combo = op_combo
            row._value_entry = value_entry

            rules_box.pack_start(row, False, False, 0)
            rules_box.show_all()

        if playlist is not None:
            for r in playlist.get("rules", []):
                add_rule_row(rule=r)
        else:
            add_rule_row()

        add_btn = Gtk.Button(label=_('+ Añadir regla'))
        add_btn.connect("clicked", lambda w: add_rule_row())
        rules_box.pack_start(add_btn, False, False, 0)

        # --- Límite de duración ---
        limit_label = Gtk.Label()
        limit_label.set_markup(_('<b>Límite de duración (opcional)</b>'))
        limit_label.set_halign(Gtk.Align.START)
        limit_label.set_margin_top(10)
        area.pack_start(limit_label, False, False, 0)

        limit_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        limit_entry = Gtk.Entry()
        limit_entry.set_width_chars(5)
        limit_entry.set_placeholder_text(_('Ej: 30'))
        if playlist is not None and playlist.get("limit_minutes"):
            limit_entry.set_text(str(playlist["limit_minutes"]))
        limit_box.pack_start(limit_entry, False, False, 0)
        limit_box.pack_start(Gtk.Label(label=_('minutos')), False, False, 0)
        area.pack_start(limit_box, False, False, 0)
        # ---------------------------------

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(rules_box)
        area.pack_start(scroll, True, True, 0)

        dialog.show_all()
        response = dialog.run()

        if response == Gtk.ResponseType.OK:
            name = name_entry.get_text().strip()
            if not name:
                self.status_label.set_text(_('El nombre no puede estar vacío'))
                dialog.destroy()
                return

            rules = []
            for row in rules_box.get_children():
                if not hasattr(row, "_field_combo"):
                    continue
                field = row._field_combo.get_active_id()
                op = row._op_combo.get_active_id()
                value = row._value_entry.get_text().strip()
                if not field or not op:
                    continue
                rules.append({"field": field, "op": op, "value": value})

            # Guardar límite de tiempo
            limit_val = limit_entry.get_text().strip()
            limit_minutes = int(limit_val) if limit_val.isdigit() else 0

            if playlist is None:
                pl = playlists_mod.new_smart_playlist(name, rules=rules)
                if limit_minutes > 0:
                    pl["limit_minutes"] = limit_minutes
                self.playlists.append(pl)
                playlists_mod.save(self.playlists)
                self._rebuild_playlists_listbox(select_id=pl["id"])
                self.status_label.set_text(f"Lista inteligente «{name}» creada")
            else:
                playlist["name"] = name
                playlist["rules"] = rules
                if limit_minutes > 0:
                    playlist["limit_minutes"] = limit_minutes
                else:
                    playlist.pop("limit_minutes", None)
                playlists_mod.save(self.playlists)
                self._rebuild_playlists_listbox(select_id=playlist["id"])
                self.status_label.set_text(f"Lista «{name}» actualizada")

        dialog.destroy()

    # ---------- iPod ----------

    def _update_ipod_info_box(self):
        if self.ipod_data is None or self.ipod_mount is None:
            self.ipod_info_model.set_markup(_('<b>No conectado</b>'))
            self.ipod_info_capacity.set_text(
                _('Conecta el iPod por USB y se detectará automáticamente'))
            self.ipod_info_bar.set_fraction(0)
            self.ipod_info_usage.set_text("")
            self.ipod_content_btn.set_sensitive(False)
            self.ipod_eject_btn.set_sensitive(False)
            self.btn_sync_all.set_sensitive(False)
            self.btn_sync_selection.set_sensitive(False)
            return
        try:
            usage = shutil.disk_usage(self.ipod_mount)
            total_gb = usage.total / (1024**3)
            used_gb = usage.used / (1024**3)
            free_gb = usage.free / (1024**3)
            frac = usage.used / usage.total if usage.total > 0 else 0
            n_tracks = self.ipod_data.get("track_count", 0)
            n_pl = self.ipod_data.get("playlist_count", 0)
            
            self.ipod_info_model.set_markup(_("<b>Conectado</b>"))
            self.ipod_info_capacity.set_text(
                _f("{} canciones · {} listas", n_tracks, n_pl))
            self.ipod_info_bar.set_fraction(frac)
            self.ipod_info_usage.set_text(
                _f("{} GB usados · {} GB libres\nTotal: {} GB",
                   f"{used_gb:.1f}", f"{free_gb:.1f}", f"{total_gb:.1f}"))
                
            self.ipod_content_btn.set_sensitive(True)
            self.ipod_eject_btn.set_sensitive(True)
            self.btn_sync_all.set_sensitive(True)
            self.btn_sync_selection.set_sensitive(True)
        except Exception as e:
            self.ipod_info_model.set_markup("<b>Error</b>")
            self.ipod_info_capacity.set_text(f"Error: {e}")
            self.ipod_info_bar.set_fraction(0)
            self.ipod_info_usage.set_text("")
            self.ipod_content_btn.set_sensitive(False)
            self.ipod_eject_btn.set_sensitive(False)
            self.btn_sync_all.set_sensitive(False)

    def on_eject_ipod(self, btn):
        """Desmonta y expulsa el iPod con seguridad, liberando recursos primero."""
        if self.ipod_mount is None:
            return
        
        mount = self.ipod_mount
        block = self._block_device_for_mount(mount)
        if block is None:
            self.status_label.set_text(_('No se pudo identificar el dispositivo físico.'))
            return

        import re
        base_device = re.sub(r'\d+$', '', block)

        # 1. Si se está reproduciendo algo desde el iPod, detenerlo y liberar el archivo
        if self.current_uri and self.ipod_mount and self.current_uri.startswith("file://" + self.ipod_mount):
            self.playbin.set_state(Gst.State.NULL)
            self._set_play_icon(False)
            self.current_uri = None
            self.current_index = -1
            self.status_label.set_text(_('Reproducción detenida para expulsar el iPod…'))
            import time; time.sleep(0.5)

        self.status_label.set_text(_('Expulsando iPod…'))

        def worker():
            import subprocess
            import time
            try:
                # Intento 1: Expulsión limpia y silenciosa (sin contraseña)
                subprocess.run(["udisksctl", "unmount", "-b", f"{base_device}1"], capture_output=True)
                subprocess.run(["udisksctl", "unmount", "-b", f"{base_device}2"], capture_output=True)
                subprocess.run(["udisksctl", "unmount", "-b", base_device], capture_output=True)
                
                res_poweroff = subprocess.run(["udisksctl", "power-off", "-b", base_device], capture_output=True)
                
                if res_poweroff.returncode != 0:
                    # Intento 2: Si falla, usamos eject normal
                    res_eject = subprocess.run(["eject", base_device], capture_output=True)
                    if res_eject.returncode != 0:
                        # Intento 3: Si de plano está bloqueado, ahí sí pedimos contraseña
                        GLib.idle_add(self.status_label.set_text, "Expulsión normal bloqueada. Pidiendo permisos...")
                        subprocess.run(["pkexec", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ipod_eject_helper.sh"), base_device, mount])

                # Actualizar la interfaz
                GLib.idle_add(self._on_ipod_disconnected)
                GLib.idle_add(self.status_label.set_text, "iPod expulsado. Puedes desconectarlo con seguridad.")
                
            except Exception as e:
                GLib.idle_add(self.status_label.set_text, f"Error al expulsar: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def on_restore_ipod_clicked(self, btn):
        import subprocess
        import re

        # 1. Detectar si hay un iPod montado o pedir que se elija un disco
        block_device = None
        model_id = None

        if self.ipod_mount:
            block_device = self._block_device_for_mount(self.ipod_mount)
            # Intentar detectar modelo automáticamente si está montado
            gen_str = self.ipod_data.get("generation", "unknown") if self.ipod_data else "unknown"
            gen_lower = gen_str.lower()
            if "4th" in gen_lower or "fourth" in gen_lower: model_id = "4g"
            elif "mini" in gen_lower and ("2" in gen_lower or "second" in gen_lower): model_id = "mini2"
            elif "mini" in gen_lower: model_id = "mini1"
            elif "3rd" in gen_lower: model_id = "3g"
            elif "2nd" in gen_lower: model_id = "2g"
            elif "1st" in gen_lower: model_id = "1g"

        # Si no hay iPod montado (disco en blanco), mostrar ventana de selección manual
        if not block_device or not model_id:
            dialog = Gtk.Dialog(title=_('Restauración Manual de iPod'), transient_for=self, modal=True)
            dialog.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Continuar", Gtk.ResponseType.OK)
            box = dialog.get_content_area()
            box.set_spacing(10)
            box.set_border_width(15)

            # Listar discos disponibles
            lbl1 = Gtk.Label(label=_('Selecciona el disco físico del iPod (¡ten cuidado de no elegir tu disco duro!):'))
            lbl1.set_halign(Gtk.Align.START)
            box.pack_start(lbl1, False, False, 0)

            combo_disk = Gtk.ComboBoxText()
            try:
                # Obtener discos (solo discos, no particiones)
                out = subprocess.check_output(["lsblk", "-d", "-n", "-o", "NAME,SIZE,MODEL"], text=True)
                for line in out.strip().split("\n"):
                    parts = line.split(None, 2)
                    if len(parts) >= 2:
                        dev_name = parts[0]
                        size = parts[1]
                        model = parts[2] if len(parts) > 2 else "Desconocido"
                        combo_disk.append(f"/dev/{dev_name}", f"/dev/{dev_name} ({size}) - {model}")
            except Exception:
                pass
            combo_disk.set_active(0)
            box.pack_start(combo_disk, False, False, 0)

            # Seleccionar modelo
            lbl2 = Gtk.Label(label=_('Selecciona el modelo de tu iPod:'))
            lbl2.set_halign(Gtk.Align.START)
            lbl2.set_margin_top(10)
            box.pack_start(lbl2, False, False, 0)

            combo_model = Gtk.ComboBoxText()
            modelos = [
                ("1g", "iPod 1ª Generación"), ("2g", "iPod 2ª Generación"),
                ("3g", "iPod 3ª Generación"), ("4g", "iPod 4ª Generación (Monocromático)"),
                ("photo", "iPod Photo (4ª Gen Color)"), ("mini1", "iPod Mini 1ª Gen"),
                ("mini2", "iPod Mini 2ª Gen")
            ]
            for m_id, m_name in modelos:
                combo_model.append(m_id, m_name)
            combo_model.set_active_id("4g") # Por defecto 4G
            box.pack_start(combo_model, False, False, 0)

            dialog.show_all()
            if dialog.run() != Gtk.ResponseType.OK:
                dialog.destroy()
                return
            
            block_device = combo_disk.get_active_id()
            model_id = combo_model.get_active_id()
            dialog.destroy()

        base_device = re.sub(r'\d+$', '', block_device)

        # Verificar que el firmware existe
        fw_filename = f"firmware_{model_id}.ipod"
        fw_image_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "firmwares", fw_filename)

        if not os.path.exists(fw_image_path):
            dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.OK, text=f"Falta el archivo de firmware para {model_id.upper()}.")
            dialog.format_secondary_text(f"El programa esperaba encontrar:\n{fw_image_path}")
            dialog.run()
            dialog.destroy()
            return

        # Advertencia final
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
            text=_f("⚠️ ADVERTENCIA: RESTAURACIÓN COMPLETA ({})", model_id.upper())
        )
        
        texto_aviso = _("Estás a punto de BORRAR COMPLETAMENTE el iPod en:") + \
            f"\n{base_device}\n\n" + \
            _("Se perderán TODAS las canciones y configuraciones.") + "\n" + \
            _("¿Estás absolutamente seguro de que {} es tu iPod?").format(base_device) + \
            "\n\n" + \
            _("📌 NOTA IMPORTANTE: Tu iPod debe estar en 'Modo Disco'.") + "\n"
        # Advertencia especial solo para modelos que REQUIEREN FireWire
        if model_id in ("1g", "2g", "3g", "4g", "photo"):
            texto_aviso += "\n\n" + \
                _("⚠️ ATENCIÓN (Requiere FireWire):") + "\n" + \
                _("Por diseño del hardware, esta generación de iPod requiere un "
                  "cargador Firewire para completar el proceso de restauración de firmware. "
                  "Procede sólo si cuentas con uno, de lo contrario el iPod "
                  "quedará en modo de recuperación al desconectarlo.")

        # ¡ESTA LÍNEA FALTABA! Es la que aplica el texto al diálogo
        dialog.format_secondary_text(texto_aviso)
        
        dialog.add_button(_("Cancelar"), Gtk.ResponseType.CANCEL)
        dialog.add_button(_("Sí, borrar y restaurar"), Gtk.ResponseType.YES)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)

        if dialog.run() != Gtk.ResponseType.YES:
            dialog.destroy()
            return
        dialog.destroy()

        self.status_label.set_text(_f("Iniciando restauración ({})…", model_id.upper()))
        self._execute_restore_process(base_device, fw_image_path)

    def _execute_restore_process(self, base_device, fw_image_path):
        """Ejecuta la restauración a través de pkexec para pedir contraseña."""
        def worker():
            import subprocess
            try:
                process = subprocess.Popen(
                    ["pkexec", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ipod_restore_helper.sh"), base_device, fw_image_path],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                )
                for line in process.stdout:
                    line = line.strip()
                    if line:
                        GLib.idle_add(self.status_label.set_text, line)
                process.wait()
                
                if process.returncode != 0:
                    GLib.idle_add(self.status_label.set_text, _("Restauración cancelada o fallida."))
            except Exception as e:
                GLib.idle_add(self.status_label.set_text, f"Error inesperado: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _block_device_for_mount(self, mount_path):
        try:
            with open("/proc/mounts", "r") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] == mount_path:
                        return parts[0]
        except Exception:
            pass
        return None

    def on_show_ipod_content(self, btn):
        if self.ipod_data is None:
            self.status_label.set_text(_('No hay iPod conectado.'))
            return
        self.stack.set_visible_child_name("ipod")
        self._refresh_ipod_views()

    def _build_ipod_stack_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.ipod_notebook = Gtk.Notebook()
        
        self.ipod_songs_scroll = self._build_ipod_songs_page()
        self.ipod_notebook.append_page(self.ipod_songs_scroll, Gtk.Label(label=_('Canciones')))
        
        self.ipod_artists_scroll = self._build_ipod_artists_page()
        self.ipod_notebook.append_page(self.ipod_artists_scroll, Gtk.Label(label=_('Artistas')))
        
        self.ipod_playlists_page = self._build_ipod_playlists_page()
        self.ipod_notebook.append_page(self.ipod_playlists_page, Gtk.Label(label=_('Playlists')))
        
        self.ipod_podcasts_scroll = self._build_ipod_podcasts_page()
        self.ipod_notebook.append_page(self.ipod_podcasts_scroll, Gtk.Label(label=_('Podcasts')))
        
        box.pack_start(self.ipod_notebook, True, True, 0)
        return box

    def _build_ipod_songs_page(self):
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.ipod_songs_store = Gtk.ListStore(str, str, str, str, int, str, int)
        view = Gtk.TreeView(model=self.ipod_songs_store)
        view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        view.connect("button-press-event", self.on_ipod_button_press)
        view.connect("row-activated", self.on_ipod_song_activated)
        
        cols_info = [
            (_("Artista"), 0, True, 80, 0.0),
            (_("Álbum"), 1, True, 80, 0.0),
            (_("Título"), 2, True, 120, 0.0),
            (_("Dur."), 3, False, 50, 1.0),
            ("Repr.", 4, False, 50, 0.5),
            ("★", 5, False, 90, 0.0)
        ]
        
        for name, col_idx, expand, min_w, xalign in cols_info:
            r = Gtk.CellRendererText()
            r.set_property("ellipsize", Pango.EllipsizeMode.END)
            r.set_property("xalign", xalign)
            col = Gtk.TreeViewColumn(name, r, text=col_idx)
            col.set_resizable(True)
            col.set_min_width(min_w)
            col.set_expand(expand)
            col.set_sort_column_id(col_idx)  # <--- NUEVO: permite ordenar haciendo clic
            view.append_column(col)

            
        scroll.add(view)
        return scroll

    def _build_ipod_artists_page(self):
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        
        # La estructura es: (Texto, ID, Tipo)
        self.ipod_artists_store = Gtk.TreeStore(str, int, str)
        self.ipod_artists_view = Gtk.TreeView(model=self.ipod_artists_store)
        self.ipod_artists_view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.ipod_artists_view.connect("button-press-event", self.on_ipod_artist_tree_button_press)
        self.ipod_artists_view.connect("row-activated", self.on_ipod_song_activated)

        renderer = Gtk.CellRendererText()
        renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        col = Gtk.TreeViewColumn("Artista / Álbum / Canción", renderer, text=0)
        col.set_expand(True)
        self.ipod_artists_view.append_column(col)

        scroll.add(self.ipod_artists_view)
        return scroll

    def on_ipod_artist_tree_button_press(self, widget, event):
        if event.button == 3:
            path_info = widget.get_path_at_pos(int(event.x), int(event.y))
            if path_info is not None:
                path = path_info[0]
                sel = widget.get_selection()
                if not sel.path_is_selected(path):
                    sel.unselect_all()
                    sel.select_path(path)
            self.ipod_context_menu.popup_at_pointer(event)
            return True
        return False


    def _build_ipod_playlists_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        left_scroll = Gtk.ScrolledWindow()
        left_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        left_scroll.set_size_request(280, -1)

        self.ipod_playlists_store = Gtk.ListStore(str, int)
        self.ipod_pls_view = Gtk.TreeView(model=self.ipod_playlists_store)
        r1 = Gtk.CellRendererText()
        r1.set_property("ellipsize", Pango.EllipsizeMode.END)
        c1 = Gtk.TreeViewColumn("Playlist", r1, text=0)
        c1.set_expand(True)
        self.ipod_pls_view.append_column(c1)
        r2 = Gtk.CellRendererText()
        r2.set_property("xalign", 1.0)
        c2 = Gtk.TreeViewColumn("Pistas", r2, text=1)
        self.ipod_pls_view.append_column(c2)
        left_scroll.add(self.ipod_pls_view)
        box.pack_start(left_scroll, False, False, 0)

        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.ipod_pls_right_label = Gtk.Label()
        self.ipod_pls_right_label.set_markup(_('<i>Selecciona una playlist</i>'))
        self.ipod_pls_right_label.set_halign(Gtk.Align.START)
        right_box.pack_start(self.ipod_pls_right_label, False, False, 0)

        right_scroll = Gtk.ScrolledWindow()
        right_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        
        # CORREGIDO: Usamos self.ipod_pls_tracks_store y self.ipod_pls_tracks_view
        self.ipod_pls_tracks_store = Gtk.ListStore(str, str, str, int)
        self.ipod_pls_tracks_view = Gtk.TreeView(model=self.ipod_pls_tracks_store)
        
        # Columnas con expansión y ancho mínimo
        for i, name in enumerate((_("Artista"), _("Álbum"), _("Título"))):
            r = Gtk.CellRendererText()
            r.set_property("ellipsize", Pango.EllipsizeMode.END)
            col = Gtk.TreeViewColumn(name, r, text=i)
            col.set_resizable(True)
            col.set_min_width(80 if i < 2 else 120)
            col.set_expand(True)
            self.ipod_pls_tracks_view.append_column(col) # <-- Cambiado a self
            
        self.ipod_pls_tracks_view.connect("row-activated", self.on_ipod_song_activated)
        # NUEVO: Añadir clic derecho para ver información en las playlists
        self.ipod_pls_tracks_view.connect("button-press-event", self.on_ipod_button_press)
        
        right_scroll.add(self.ipod_pls_tracks_view) # <-- Cambiado a self
        right_box.pack_start(right_scroll, True, True, 0)
        box.pack_start(right_box, True, True, 0)

        def on_pl_selected(sel):
            model, it = sel.get_selected()
            if it is None:
                return
            idx = int(model.get_path(it).to_string())
            pls = self.ipod_data.get("playlists", []) if self.ipod_data else []
            if idx >= len(pls):
                return
            pl = pls[idx]
            track_ids = set(pl.get("track_ids", []))
            self.ipod_pls_tracks_store.clear()
            for t in self.ipod_data.get("tracks", []):
                if track_ids and t.get("id") not in track_ids:
                    continue
                self.ipod_pls_tracks_store.append([
                    t.get("artist", "") or "(sin artista)",
                    t.get("album", "") or "(sin álbum)",
                    t.get("title", "") or "(sin título)",
                    int(t.get("id", 0))
                ])
            self.ipod_pls_right_label.set_markup(
                f"<b>{GLib.markup_escape_text(pl.get('name', '?'))}</b> "
                f"({len(self.ipod_pls_tracks_store)} canciones)")

        self.ipod_pls_view.get_selection().connect("changed", on_pl_selected)
        return box

    def _build_ipod_podcasts_page(self):
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.ipod_podcasts_store = Gtk.ListStore(str, str, str, str, int)
        self.ipod_podcasts_view = Gtk.TreeView(model=self.ipod_podcasts_store)
        self.ipod_podcasts_view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.ipod_podcasts_view.connect("button-press-event", self.on_ipod_button_press)
        self.ipod_podcasts_view.connect("row-activated", self.on_ipod_song_activated)
        
        for i, name in enumerate((_("Artista"), _("Álbum"), _("Título"), "Dur.")):
            r = Gtk.CellRendererText()
            r.set_property("ellipsize", Pango.EllipsizeMode.END)
            if i == 3:
                r.set_property("xalign", 1.0)
            col = Gtk.TreeViewColumn(name, r, text=i)
            col.set_resizable(True)
            col.set_min_width(50 if i == 3 else 80)
            col.set_expand(i != 3)
            self.ipod_podcasts_view.append_column(col)
            
        scroll.add(self.ipod_podcasts_view)
        return scroll

    def _refresh_ipod_views(self):
        if not self.ipod_data:
            return
        
        q = self.search_query
        self.ipod_songs_store.clear()
        for t in self.ipod_data.get("tracks", []):
            title = t.get("title", "") or ""
            artist = t.get("artist", "") or ""
            album = t.get("album", "") or ""
            
            # NUEVO: Filtrar por la búsqueda
            if q and (q not in title.lower() and q not in artist.lower() and q not in album.lower()):
                continue

            dur_ms = t.get("duration_ms", 0)
            dur_str = f"{dur_ms // 60000}:{(dur_ms // 1000) % 60:02d}"
            stars = ipod.rating_to_stars(t.get("rating", 0))
            stars_str = "★" * stars + "☆" * (5 - stars)
            self.ipod_songs_store.append([
                t.get("artist", "") or "(sin artista)",
                t.get("album", "") or "(sin álbum)",
                t.get("title", "") or "(sin título)",
                dur_str, int(t.get("playcount", 0)), stars_str,
                int(t.get("id", 0))
            ])

        self.ipod_playlists_store.clear()
        for p in self.ipod_data.get("playlists", []):
            name = p.get("name", "") or "(sin nombre)"
            if p.get("is_master"):
                name = f"[Maestra] {name}"
            elif p.get("is_smart"):
                name = f"[Smart] {name}"
            self.ipod_playlists_store.append([name, p.get("track_count", 0)])
        
        first = self.ipod_playlists_store.get_iter_first()
        if first:
            self.ipod_pls_view.get_selection().select_iter(first)

        self.ipod_podcasts_store.clear()
        MEDIATYPE_PODCAST = 0x04
        for t in self.ipod_data.get("tracks", []):
            title = t.get("title", "") or ""
            artist = t.get("artist", "") or ""
            album = t.get("album", "") or ""
            
            # NUEVO: Filtrar por la búsqueda
            if q and (q not in title.lower() and q not in artist.lower() and q not in album.lower()):
                continue

            if not (int(t.get("mediatype", 0)) & MEDIATYPE_PODCAST):
                continue
            dur_ms = t.get("duration_ms", 0)
            dur_str = f"{dur_ms // 60000}:{(dur_ms // 1000) % 60:02d}"
            self.ipod_podcasts_store.append([
                t.get("artist", "") or "(sin artista)",
                t.get("album", "") or "(sin álbum)",
                t.get("title", "") or "(sin título)", dur_str,
                int(t.get("id", 0))
            ])
        # Poblar el árbol de Artistas
        self.ipod_artists_store.clear()
        groups = {}
        for t in self.ipod_data.get("tracks", []):
            title = t.get("title", "") or "(sin título)"
            artist = t.get("artist", "") or "(sin artista)"
            album = t.get("album", "") or "(sin álbum)"
            track_id = int(t.get("id", 0))

            if q and (q not in title.lower() and q not in artist.lower() and q not in album.lower()):
                continue

            if artist not in groups:
                groups[artist] = {}
            if album not in groups[artist]:
                groups[artist][album] = []
            groups[artist][album].append((title, track_id))

        for artist in sorted(groups.keys(), key=sort_key_artist):
            artist_iter = self.ipod_artists_store.append(None, [artist, 0, "artist"])
            for album in sorted(groups[artist].keys(), key=sort_key_artist):
                album_iter = self.ipod_artists_store.append(artist_iter, [album, 0, "album"])
                for title, tid in groups[artist][album]:
                    self.ipod_artists_store.append(album_iter, [title, tid, "track"])

    def _on_ipod_read(self, mount_point, data, err, just_connected=False):
        self.ipod_read_running = False
        if err:
            if "no se pudo leer iTunesDB" in err:
                GLib.idle_add(self._prompt_init_ipod, mount_point)
                return False
            self.status_label.set_text(f"Error leyendo iPod: {err}")
            self._update_ipod_info_box()
            return False
        self.ipod_mount = mount_point
        self.ipod_data = data
        n_tracks = data.get("track_count", 0)
        n_pls = data.get("playlist_count", 0)
        self.status_label.set_text(
            _f("iPod cargado: {} canciones, {} playlists", n_tracks, n_pls))
        self._update_ipod_info_box()
        self._refresh_ipod_views()

        if just_connected and self.prefs.get("auto_sync", False):
            self.status_label.set_text(_('iPod conectado. Iniciando sincronización automática en 3 segundos…'))
            GLib.timeout_add_seconds(3, self._run_auto_sync)

        return False

    def _prompt_init_ipod(self, mount_point):
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=_('Se ha detectado un iPod sin inicializar.')
        )
        dialog.format_secondary_text(
            "El iPod se ha restaurado, pero su base de datos está vacía.\n"
            "¿Deseas inicializarlo ahora para poder añadir música?"
        )
        if dialog.run() == Gtk.ResponseType.YES:
            self.status_label.set_text(_('Inicializando iPod…'))
            def worker():
                out, err = ipod_write.init_ipod(mount_point)
                if err:
                    GLib.idle_add(self.status_label.set_text, f"Error al inicializar: {err}")
                else:
                    GLib.idle_add(self.status_label.set_text, "iPod inicializado. Releyendo…")
                    data, rerr = ipod.read_ipod(mount_point)
                    GLib.idle_add(self._on_ipod_read, mount_point, data, rerr)
            threading.Thread(target=worker, daemon=True).start()
        dialog.destroy()
        return False

    # ---------- Fin iPod ----------

    # ---------- Auto-detección de iPod ----------

    def _setup_auto_detect(self):
        """Inicia la detección automática de iPods por USB."""
        try:
            self._volume_monitor = Gio.VolumeMonitor.get()
            self._volume_monitor.connect("mount-added", self._on_mount_added)
            self._volume_monitor.connect("mount-removed",
                                          self._on_mount_removed)
            # Por si ya hay un iPod conectado al arrancar la app
            GLib.timeout_add(300, self._check_existing_ipods)
        except Exception as e:
            print(f"[ipod-auto] VolumeMonitor no disponible: {e}. "
                  f"Usando polling cada 5s.")
            GLib.timeout_add_seconds(5, self._poll_for_ipod)

    def _is_ipod_path(self, path):
        if os.path.exists(os.path.join(path, "iPod_Control", "iTunes", "iTunesDB")):
            return True
        if "IPOD" in os.path.basename(path).upper():
            return True
        block = self._block_device_for_mount(path)
        if block:
            base = re.sub(r'\d+$', '', block)
            try:
                out = subprocess.check_output(["udevadm", "info", "-q", "property", "-n", base], text=True)
                if "ID_VENDOR_ID=05ac" in out:
                    return True
            except Exception:
                pass
        return False

    def _on_mount_added(self, monitor, mount):
        root = mount.get_root()
        if root is None:
            return
        path = root.get_path()
        if path and self._is_ipod_path(path):
            GLib.idle_add(self._auto_load_ipod, path)

    def _on_mount_removed(self, monitor, mount):
        if self.ipod_mount is None:
            return
        root = mount.get_root()
        if root is None:
            return
        path = root.get_path()
        if path and path == self.ipod_mount:
            GLib.idle_add(self._on_ipod_disconnected)

    def _check_existing_ipods(self):
        """Al iniciar, comprueba si ya hay un iPod montado."""
        mounts = ipod.find_mounted_ipods()
        if mounts:
            self._auto_load_ipod(mounts[0])
        return False

    def _poll_for_ipod(self):
        """Fallback si VolumeMonitor no está disponible."""
        if self.shutting_down:
            return False
        if self.ipod_data is None:
            mounts = ipod.find_mounted_ipods()
            if mounts:
                self._auto_load_ipod(mounts[0])
        return True

    def _auto_load_ipod(self, path):
        if self.ipod_read_running:
            return False
        if self.ipod_mount == path and self.ipod_data is not None:
            return False
        self.ipod_read_running = True
        self.status_label.set_text(f"iPod detectado en {path}, leyendo…")

        def worker():
            data, err = ipod.read_ipod(path)
            GLib.idle_add(self._on_ipod_read, path, data, err, True)

        threading.Thread(target=worker, daemon=True).start()
        return False

    def on_ipod_button_press(self, widget, event):
        if event.button == 3:
            path_info = widget.get_path_at_pos(int(event.x), int(event.y))
            if path_info is not None:
                path = path_info[0]
                sel = widget.get_selection()
                if not sel.path_is_selected(path):
                    sel.unselect_all()
                    sel.select_path(path)
            self.ipod_context_menu.popup_at_pointer(event)
            return True
        return False

    def on_ipod_song_activated(self, tree, path, column):
        """Se ejecuta al hacer doble clic en una canción del iPod."""
        if not self.ipod_data or not self.ipod_mount:
            return

        model = tree.get_model()
        it = model.get_iter(path)
        
        # Detectar de qué lista viene para obtener el ID correctamente
        if model == self.ipod_artists_store:
            kind = model.get_value(it, 2)
            if kind != "track":
                return # Solo reproducir pistas individuales del árbol
            track_id = model.get_value(it, 1)
        elif model == self.ipod_podcasts_store:
            track_id = model.get_value(it, 4)
        elif model == self.ipod_pls_tracks_store:
            track_id = model.get_value(it, 3)  # <--- NUEVO: ID desde la playlist
        else:
            track_id = model.get_value(it, 6) # Vista de Canciones
            
        # 1. Buscar los datos completos de la pista en la base del iPod
        track_data = None
        for t in self.ipod_data.get("tracks", []):
            if t.get("id") == track_id:
                track_data = t
                break
                
        if not track_data:
            self.status_label.set_text(_('No se encontraron datos de la pista.'))
            return

        ipod_path = track_data.get("ipod_path")
        if not ipod_path:
            self.status_label.set_text(_('Esta pista no tiene ruta física en el iPod.'))
            return

        # 2. Obtener la ruta real en el disco usando la función de ipod.py
        full_path = ipod.ipod_track_full_path(self.ipod_mount, ipod_path)
        
        if not full_path or not os.path.exists(full_path):
            self.status_label.set_text(f"Archivo no encontrado en el iPod: {full_path}")
            return

        # 3. Convertir la ruta a URI para que GStreamer la entienda
        try:
            uri = GLib.filename_to_uri(full_path, None)
        except Exception as e:
            self.status_label.set_text(f"Error convirtiendo ruta a URI: {e}")
            return

        # 4. Preparar el reproductor
        self.current_uri = uri
        self.current_index = -1  # -1 indica que no está en la biblioteca local
        self.duration_ns = 0
        self.pos_label.set_text("0:00")
        self.dur_label.set_text("0:00")
        self.progress.set_value(0)

        # 5. Actualizar la interfaz (Panel derecho)
        title = track_data.get("title", "Desconocido")
        artist = track_data.get("artist", "Desconocido")
        album = track_data.get("album", "")
        
        self.now_playing_label.set_text(f"{artist} — {title}" if artist else title)
        self.np_title.set_markup(f"<b><span size='large'>{GLib.markup_escape_text(title)}</span></b>")
        self.np_artist.set_markup(f"<span size='medium'>{GLib.markup_escape_text(artist)}</span>")
        self.np_album.set_markup(f"<span size='small'>{GLib.markup_escape_text(album)}</span>")

        self.cover_image.set_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
        self.cover_image.set_pixel_size(296)
        
        # Cargar carátula en segundo plano (pasando artista y álbum para el caché)
        threading.Thread(
            target=self._load_cover_art, 
            args=(uri,), 
            kwargs={"artist": artist, "album": album}, 
            daemon=True
        ).start()

        # 6. ¡A reproducir!
        self.playbin.set_state(Gst.State.NULL)
        self.playbin.set_property("uri", uri)
        self.playbin.set_state(Gst.State.PLAYING)
        self._set_play_icon(True)
        
        # Actualizar letras
        self.request_lyrics(artist, title)
        self.status_label.set_text(f"Reproduciendo desde el iPod: {title}")

    def _ask_backup(self):
        # El usuario prefiere hacer los backups manualmente.
        # Retornamos NO (No hacer backup) sin mostrar diálogo.
        return Gtk.ResponseType.NO

    def on_delete_from_ipod(self, menu_item):
        current_page = self.ipod_notebook.get_current_page()
        track_ids = []
        
        def get_ids_from_iter(model, it):
            """Función auxiliar para recorrer el árbol de artistas."""
            ids = []
            kind = model.get_value(it, 2)
            if kind == "track":
                ids.append(model.get_value(it, 1))
            else:
                child = model.iter_children(it)
                while child:
                    ids.extend(get_ids_from_iter(model, child))
                    child = model.iter_next(child)
            return ids

        # Recopilar IDs según la pestaña activa
        if current_page == 0: # Canciones
            sel = self.ipod_songs_scroll.get_child().get_selection()
            model, paths = sel.get_selected_rows()
            for path in paths:
                track_ids.append(model.get_value(model.get_iter(path), 6))
                
        elif current_page == 1: # Artistas
            sel = self.ipod_artists_view.get_selection()
            model, paths = sel.get_selected_rows()
            for path in paths:
                track_ids.extend(get_ids_from_iter(model, model.get_iter(path)))
                
        elif current_page == 2: # Playlists
            sel = self.ipod_pls_tracks_view.get_selection()
            model, paths = sel.get_selected_rows()
            for path in paths:
                track_ids.append(model.get_value(model.get_iter(path), 3))
                
        elif current_page == 3: # Podcasts
            sel = self.ipod_podcasts_view.get_selection()
            model, paths = sel.get_selected_rows()
            for path in paths:
                track_ids.append(model.get_value(model.get_iter(path), 4))
        else:
            return
            
        if not track_ids:
            return
            
        n = len(track_ids)
        
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"¿Eliminar {n} elemento(s) del iPod?"
        )
        dialog.format_secondary_text("Se borrarán de la base de datos y del disco del iPod.")
        
        if dialog.run() == Gtk.ResponseType.YES:
            dialog.destroy()
            resp_backup = self._ask_backup()
            if resp_backup == Gtk.ResponseType.CANCEL: 
                return # Cancelado
            
            self.status_label.set_text(f"Eliminando {n} pistas del iPod…")
            
            def worker():
                if resp_backup == Gtk.ResponseType.YES:
                    import shutil, datetime
                    backup_dir = os.path.expanduser("~/tunepod/backups")
                    os.makedirs(backup_dir, exist_ok=True)
                    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                    try:
                        src_db = os.path.join(self.ipod_mount, "iPod_Control", "iTunes", "iTunesDB")
                        shutil.copy2(src_db, os.path.join(backup_dir, f"iTunesDB-antes-borrar-{stamp}"))
                    except Exception as e:
                        GLib.idle_add(self.status_label.set_text, f"Error backup: {e}")
                        return
                        
                out, err = ipod_write.delete_tracks(self.ipod_mount, track_ids)
                try: os.sync()
                except: pass
                
                if err:
                    GLib.idle_add(self.status_label.set_text, f"Error: {err[:150]}")
                else:
                    GLib.idle_add(self.status_label.set_text, f"{n} elemento(s) eliminados")
                    data, rerr = ipod.read_ipod(self.ipod_mount)
                    if not rerr:
                        GLib.idle_add(self._on_ipod_read, self.ipod_mount, data, None)
            
            threading.Thread(target=worker, daemon=True).start()
        else:
            dialog.destroy()

    def on_copy_from_ipod(self, menu_item):
        """Copia las canciones seleccionadas del iPod al PC, organizándolas por Artista/Álbum."""
        if not self.ipod_data or not self.ipod_mount:
            return

        # Detectar en qué pestaña del iPod estamos (Canciones o Podcasts)
        current_page = self.ipod_notebook.get_current_page()
        current_page = self.ipod_notebook.get_current_page()
        if current_page == 0:
            view = self.ipod_songs_scroll.get_child()
            model = self.ipod_songs_store
        elif current_page == 1:
            view = self.ipod_artists_view
            model = self.ipod_artists_store
        elif current_page == 3:
            view = self.ipod_podcasts_view
            model = self.ipod_podcasts_store
        else:
            return

        sel = view.get_selection()
        model_sel, paths = sel.get_selected_rows()
        if not paths:
            self.status_label.set_text(_('Selecciona algo primero.'))
            return

        def get_ids_from_iter(model, it):
            ids = []
            if current_page == 1: # Artistas
                kind = model.get_value(it, 2)
                if kind == "track":
                    ids.append(model.get_value(it, 1))
                else:
                    child = model.iter_children(it)
                    while child:
                        ids.extend(get_ids_from_iter(model, child))
                        child = model.iter_next(child)
            elif current_page == 0: # Canciones
                ids.append(model.get_value(it, 6))
            elif current_page == 3: # Podcasts
                ids.append(model.get_value(it, 4))
            return ids

        track_ids = []
        for path in paths:
            it = model.get_iter(path)
            track_ids.extend(get_ids_from_iter(model, it))
        
        # Pedir la carpeta de destino
        chooser = Gtk.FileChooserDialog(
            title=_('Seleccionar carpeta de destino para la copia'),
            parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        chooser.add_buttons("Cancelar", Gtk.ResponseType.CANCEL,
                            "Copiar aquí", Gtk.ResponseType.OK)
        
        if chooser.run() != Gtk.ResponseType.OK:
            chooser.destroy()
            return
            
        dest_dir = chooser.get_filename()
        chooser.destroy()

        self.status_label.set_text(f"Copiando {len(track_ids)} canciones al PC…")

        def worker():
            import shutil
            import re
            
            def sanitize(name):
                # Limpiar caracteres no válidos para nombres de archivo
                if not name: return "Desconocido"
                return re.sub(r'[\\/*?:"<>|]', "_", name).strip()

            copiados = 0
            omitidos = 0  # <--- NUEVO CONTADOR
            errores = 0
            copied_paths = []
            
            # Crear un mapa rápido de ID a datos de la pista
            tracks_by_id = {t.get("id"): t for t in self.ipod_data.get("tracks", [])}

            for tid in track_ids:
                track = tracks_by_id.get(tid)
                if not track or not track.get("ipod_path"):
                    errores += 1
                    continue

                ipod_path = track.get("ipod_path")
                full_ipod_path = ipod.ipod_track_full_path(self.ipod_mount, ipod_path)
                
                if not full_ipod_path or not os.path.exists(full_ipod_path):
                    errores += 1
                    continue

                # Extraer metadatos para crear las carpetas
                artist = sanitize(track.get("artist", "Artista Desconocido"))
                album = sanitize(track.get("album", "Álbum Desconocido"))
                title = sanitize(track.get("title", "Pista"))
                
                # Obtener extensión del archivo original
                ext = os.path.splitext(full_ipod_path)[1]
                if not ext:
                    ext = ".mp3"

                # Función para normalizar nombres (ignorar tildes, símbolos, etc.)
                def _norm(s):
                    return re.sub(r'[^a-z0-9]', '', (s or '').lower())

                norm_artist = _norm(artist)
                norm_album = _norm(album)

                # 1. Comprobar si el artista ya existe
                real_artist = artist
                if os.path.isdir(dest_dir):
                    for d in os.listdir(dest_dir):
                        if os.path.isdir(os.path.join(dest_dir, d)) and _norm(d) == norm_artist:
                            real_artist = d
                            break
                
                artist_dir = os.path.join(dest_dir, real_artist)
                os.makedirs(artist_dir, exist_ok=True)

                # 2. Comprobar si el álbum ya existe dentro de la carpeta del artista
                real_album = album
                if os.path.isdir(artist_dir):
                    for d in os.listdir(artist_dir):
                        if os.path.isdir(os.path.join(artist_dir, d)) and _norm(d) == norm_album:
                            real_album = d
                            break

                # Construir la ruta destino final
                target_folder = os.path.join(artist_dir, real_album)
                os.makedirs(target_folder, exist_ok=True)
                
                target_file = os.path.join(target_folder, f"{title}{ext}")

                # --- LÓGICA ANTI-DUPLICADOS DE ARCHIVOS ---
                ya_existe = False
                norm_title = _norm(title)
                            
                # 1. Comprobar si existe un archivo con el nombre exacto y mismo peso
                if os.path.exists(target_file):
                    if os.path.getsize(full_ipod_path) == os.path.getsize(target_file):
                        ya_existe = True
                
                # 2. Si no, revisar TODOS los archivos de la carpeta
                if not ya_existe and os.path.isdir(target_folder):
                    for existing_file in os.listdir(target_folder):
                        if not existing_file.lower().endswith(ext.lower()):
                            continue
                            
                        existing_path = os.path.join(target_folder, existing_file)
                        
                        # Si el peso es EXACTAMENTE el mismo, es muy probable que sea la misma canción
                        if os.path.getsize(full_ipod_path) == os.path.getsize(existing_path):
                            norm_existing = _norm(existing_file)
                            # Si el título normalizado está dentro del nombre del archivo existente
                            if norm_title in norm_existing:
                                ya_existe = True
                                break
                                
                if ya_existe:
                    omitidos += 1
                    continue
                # ---------------------------------------------------------

                # Si el archivo ya existe pero pesa distinto, le añadimos un número
                counter = 1
                base_target = target_file
                while os.path.exists(target_file):
                    target_file = f"{os.path.splitext(base_target)[0]} ({counter}){ext}"
                    counter += 1

                try:
                    # Copiar manteniendo metadatos de fecha
                    shutil.copy2(full_ipod_path, target_file)
                    copiados += 1
                    copied_paths.append(target_file)
                    GLib.idle_add(self.status_label.set_text, 
                                  _f("Copiando: {}/{} - {}…", copiados, len(track_ids), title[:30]))
                except Exception as e:
                    print(f"[copy] Error Copiando {title}: {e}")
                    errores += 1

            # Terminado
            msg = _f("¡Copia terminada! {} exitosas", copiados)
            if omitidos > 0:
                msg += _f(", {} omitidas (ya estaban en el PC)", omitidos)
            if errores > 0:
                msg += _f(", {} errores", errores)
            msg += "."
                
            GLib.idle_add(self.status_label.set_text, msg)
            
            # Preguntar si queremos agregar a la biblioteca local
            # (Solo preguntamos si hubo al menos una copia nueva)
            if copiados > 0:
                GLib.idle_add(self._ask_add_to_library, copied_paths)

        threading.Thread(target=worker, daemon=True).start()

    def _ask_add_to_library(self, file_paths):
        """Pregunta al usuario si desea añadir los archivos copiados a la biblioteca."""
        if not file_paths:
            return
            
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=_f("¿Añadir {} canciones a tu biblioteca?", len(file_paths))
        )
        dialog.format_secondary_text(
            _("Se agregarán a la biblioteca de TunePod para que puedas "
              "reproducirlas sin el iPod conectado.\n\n"
              "Si solo estás rescatando música para un amigo, elige 'No'. "
              "Los archivos ya se habrán copiado a la carpeta que elegiste.")
        )
        
        if dialog.run() == Gtk.ResponseType.YES:
            self.start_import(file_paths)
            
        dialog.destroy()

    def _check_and_convert_tracks(self, tracks, callback):
        """Verifica formatos incompatibles y ofrece convertirlos a ALAC."""
        import subprocess
        import threading
        import hashlib

        compatible_exts = (".mp3", ".m4a", ".aac", ".wav", ".aiff")
        
        def get_ext(track):
            uri = track.get("uri", "")
            try:
                fname = GLib.filename_from_uri(uri)[0]
                return os.path.splitext(fname)[1].lower()
            except Exception:
                return ""

        incompatible = [t for t in tracks if get_ext(t) not in compatible_exts]

        if not incompatible:
            callback(tracks)
            return

        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.YES_NO,
            text=_('TunePod ha detectado archivos no compatibles')
        )
        dialog.format_secondary_text(
            _f("Se encontraron {} archivos (FLAC, OGG, etc.) que el iPod no puede reproducir.\n"
               "¿Deseas convertirlos automáticamente a ALAC (M4A sin pérdida) para poder enviarlos?", len(incompatible))
        )
        
        if dialog.run() != Gtk.ResponseType.YES:
            dialog.destroy()
            valid_tracks = [t for t in tracks if t not in incompatible]
            callback(valid_tracks)
            return
        dialog.destroy()

        self.status_label.set_text(f"Convirtiendo {len(incompatible)} archivos a ALAC…")

        cache_dir = os.path.expanduser("~/.cache/tunepod/converted")
        os.makedirs(cache_dir, exist_ok=True)

        def worker():
            converted_tracks = []
            for t in tracks:
                if t not in incompatible:
                    converted_tracks.append(t)
                    continue

                uri = t.get("uri", "")
                try:
                    fname = GLib.filename_from_uri(uri)[0]
                except Exception:
                    continue

                h = hashlib.sha1(fname.encode()).hexdigest()[:10]
                base = os.path.splitext(os.path.basename(fname))[0]
                out_path = os.path.join(cache_dir, f"{base}_{h}.m4a")

                cmd = ["ffmpeg", "-y", "-i", fname, "-c:a", "alac", "-c:v", "copy", out_path]
                try:
                    subprocess.run(cmd, capture_output=True, check=True, timeout=300)
                    new_t = dict(t)
                    new_t["uri"] = GLib.filename_to_uri(out_path, None)
                    new_t["local_path"] = out_path
                    converted_tracks.append(new_t)
                except Exception as e:
                    print(f"[conversion] Error convirtiendo {fname}: {e}")

            def finish():
                self.status_label.set_text(_('Conversión lista. Continuando sincronización…'))
                callback(converted_tracks)
                return False

            GLib.idle_add(finish)

        threading.Thread(target=worker, daemon=True).start()

    def _convert_to_alac(self, filepath):
        """Convierte un archivo a ALAC (.m4a) y lo guarda en la caché."""
        import subprocess
        import hashlib
        
        cache_dir = os.path.expanduser("~/.cache/tunepod/converted")
        os.makedirs(cache_dir, exist_ok=True)
        h = hashlib.sha1(filepath.encode()).hexdigest()[:10]
        base = os.path.splitext(os.path.basename(filepath))[0]
        out_path = os.path.join(cache_dir, f"{base}_{h}.m4a")
        
        if os.path.exists(out_path):
            return out_path
            
        cmd = ["ffmpeg", "-y", "-i", filepath, "-c:a", "alac", "-c:v", "copy", out_path]
        try:
            subprocess.run(cmd, capture_output=True, check=True, timeout=300)
            return out_path
        except Exception as e:
            print(f"[conversion] Error convirtiendo {filepath}: {e}")
            return None

    def _on_ipod_disconnected(self):
        self.ipod_mount = None
        self.ipod_data = None
        self._update_ipod_info_box()
        self.status_label.set_text(_('iPod desconectado'))
        return False

    def on_sync_stats_only(self, btn):
        """Trae los ratings y play counts del iPod hacia la biblioteca del PC."""
        if not self.ipod_data:
            return
            
        self.status_label.set_text(_('Buscando coincidencias para sincronizar estadísticas…'))
        
        local_tracks = []
        for row in self.store:
            local_tracks.append({
                "uri": row[COL_URI],
                "title": row[COL_TITLE] or "",
                "artist": row[COL_ARTIST] or "",
                "album": row[COL_ALBUM] or "",
                "duration_str": row[COL_DURATION] or "",
            })
            
        ipod_tracks = self.ipod_data.get("tracks", [])
        matched, _ = ipod_sync.match_tracks(local_tracks, ipod_tracks)
        
        updated = self._sync_ipod_stats_to_pc(matched)
        self.status_label.set_text(f"Sincronizadas {updated} estadísticas (estrellas y reproducciones).")

    def _sync_ipod_stats_to_pc(self, matched):
        """Actualiza el store del PC con los datos del iPod."""
        ipod_tracks_by_id = {t["id"]: t for t in self.ipod_data.get("tracks", [])}
        
        store_iters = {}
        it = self.store.get_iter_first()
        while it is not None:
            uri = self.store.get_value(it, COL_URI)
            store_iters[uri] = it
            it = self.store.iter_next(it)
            
        updated = 0
        for uri, ipod_id in matched:
            if ipod_id not in ipod_tracks_by_id or uri not in store_iters:
                continue
            
            ipod_track = ipod_tracks_by_id[ipod_id]
            it = store_iters[uri]
            
            pc_play = int(self.store.get_value(it, COL_PLAY_COUNT))
            ipod_play = int(ipod_track.get("playcount", 0))
            if ipod_play > pc_play:
                self.store.set_value(it, COL_PLAY_COUNT, ipod_play)
                updated += 1
            
            pc_rating = int(self.store.get_value(it, COL_RATING))
            ipod_rating = ipod.rating_to_stars(ipod_track.get("rating", 0))
            if ipod_rating != pc_rating:
                self.store.set_value(it, COL_RATING, ipod_rating)
                updated += 1
        
        if updated > 0:
            self._save_library()
            self._apply_filters()
            
        return updated

    def _sync_ipod_stats_to_pc(self, matched):
        """Actualiza el store del PC con estrellas y reproducciones del iPod.
        Devuelve cuántos campos se actualizaron."""
        ipod_tracks_by_id = {t["id"]: t for t in self.ipod_data.get("tracks", [])}
        
        store_iters = {}
        it = self.store.get_iter_first()
        while it is not None:
            uri = self.store.get_value(it, COL_URI)
            store_iters[uri] = it
            it = self.store.iter_next(it)
            
        updated = 0
        for uri, ipod_id in matched:
            if ipod_id not in ipod_tracks_by_id or uri not in store_iters:
                continue
            
            ipod_track = ipod_tracks_by_id[ipod_id]
            it = store_iters[uri]
            
            # Reproducciones: solo actualizar si el iPod tiene más
            pc_play = int(self.store.get_value(it, COL_PLAY_COUNT))
            ipod_play = int(ipod_track.get("playcount", 0))
            if ipod_play > pc_play:
                self.store.set_value(it, COL_PLAY_COUNT, ipod_play)
                updated += 1
            
            # Estrellas: sobrescribir si son diferentes (el iPod manda)
            pc_rating = int(self.store.get_value(it, COL_RATING))
            ipod_rating = ipod.rating_to_stars(ipod_track.get("rating", 0))
            if ipod_rating != pc_rating:
                self.store.set_value(it, COL_RATING, ipod_rating)
                updated += 1
        
        if updated > 0:
            self._save_library()
            self._apply_filters()
            
        return updated

    def on_auto_sync_toggled(self, btn):
        self.prefs["auto_sync"] = btn.get_active()
        preferences.save(self.prefs)
        if btn.get_active():
            self.status_label.set_text(_('Sincronización automática activada'))
        else:
            self.status_label.set_text(_('Sincronización automática desactivada'))

    def _run_auto_sync(self):
        if self.ipod_data is None:
            return False
        self.status_label.set_text(_('Sincronización automática iniciando…'))
        self.on_sync_entire_library(None)
        return False


    def on_sync_entire_library(self, btn):
        if not self.ipod_data:
            return
            
        self.status_label.set_text(_('Calculando diferencias entre PC y iPod…'))
        
        # 1. Escanear biblioteca musical
        local_tracks = []
        for row in self.store:
            local_tracks.append({
                "uri": row[COL_URI],
                "title": row[COL_TITLE] or "",
                "artist": row[COL_ARTIST] or "",
                "album": row[COL_ALBUM] or "",
                "duration_str": row[COL_DURATION] or "",
            })
            
        ipod_tracks_all = self.ipod_data.get("tracks", [])
        ipod_tracks_music = [t for t in ipod_tracks_all if not (int(t.get("mediatype", 0)) & 0x04)]
        
        matched, unmatched_pc = ipod_sync.match_tracks(local_tracks, ipod_tracks_music)
        
        def continue_sync(new_unmatched_pc):
            stats_updated = self._sync_ipod_stats_to_pc(matched)
            
            # 2. Escanear podcasts descargados
            import re as _re
            def _ep_num(t):
                m = _re.search(r"(?:episodio|episode)\s*(\d+)", t, re.IGNORECASE)
                if m: return int(m.group(1))
                m = _re.search(r"[Ee]\s*(\d+)", t)
                if m: return int(m.group(1))
                nums = _re.findall(r"\d+", t)
                if nums: return int(nums[-1])
                return 0

            local_pods = []
            if os.path.isdir(PODCAST_DIR):
                for folder in os.listdir(PODCAST_DIR):
                    folder_path = os.path.join(PODCAST_DIR, folder)
                    if not os.path.isdir(folder_path): continue
                    files = [f for f in glob.glob(os.path.join(folder_path, "*")) 
                             if not f.endswith(".part") and f.lower().endswith(AUDIO_EXT)]
                    if not files: continue
                    
                    files.sort(key=lambda f: (_ep_num(os.path.basename(f)), os.path.basename(f)))
                    
                    sub = next((s for s in self.subscriptions if s.get("title") == folder), None)
                    author = sub.get("author", folder) if sub else folder
                    rss = sub.get("feed_url", "") if sub else ""
                    
                    for i, fpath in enumerate(files):
                        local_pods.append({
                            "path": fpath,
                            "title": os.path.splitext(os.path.basename(fpath))[0],
                            "album": folder,
                            "artist": author,
                            "track_nr": i + 1,
                            "podcastrss": rss
                        })

            # 3. Calcular IDs coincidentes
            matched_ids = set(tid for (uri, tid) in matched)
            ipod_pods = [t for t in ipod_tracks_all if int(t.get("mediatype", 0)) & 0x04]
            expected_pod_titles = set(p["title"] for p in local_pods)
            ipod_pods_by_title = {t["title"]: t for t in ipod_pods if t.get("title") in expected_pod_titles}
            matched_pod_ids = set(int(t["id"]) for t in ipod_pods_by_title.values())

            # 4. Calcular qué borrar (espejo real)
            to_delete_tracks = []
            for t in ipod_tracks_all:
                tid = int(t["id"])
                if tid not in matched_ids and tid not in matched_pod_ids:
                    to_delete_tracks.append(t)
                    
            # 5. Calcular qué podcasts copiar/actualizar
            pods_to_copy = [p for p in local_pods if p["title"] not in ipod_pods_by_title]
            pods_to_update = []
            for p in local_pods:
                if p["title"] in ipod_pods_by_title:
                    ipod_t = ipod_pods_by_title[p["title"]]
                    if int(ipod_t.get("track_nr", 0)) != p["track_nr"]:
                        pods_to_update.append((int(ipod_t["id"]), p["track_nr"]))
                        # Calcular espacio necesario vs espacio libre
            total_bytes_needed = 0
            for lt in new_unmatched_pc:
                try:
                    fname = GLib.filename_from_uri(lt["uri"])[0]
                    if os.path.exists(fname):
                        total_bytes_needed += os.path.getsize(fname)
                except Exception:
                    pass
            for p in pods_to_copy:
                try:
                    total_bytes_needed += os.path.getsize(p["path"])
                except Exception:
                    pass

            free_bytes = 0
            if self.ipod_mount:
                try:
                    usage = shutil.disk_usage(self.ipod_mount)
                    free_bytes = usage.free
                except Exception:
                    pass

            needed_gb = total_bytes_needed / (1024**3)
            free_gb = free_bytes / (1024**3)

            if total_bytes_needed > free_bytes and total_bytes_needed > 0:
                dialog_err = Gtk.MessageDialog(
                    transient_for=self, modal=True,
                    message_type=Gtk.MessageType.ERROR,
                    buttons=Gtk.ButtonsType.NONE,
                    text=_('No hay suficiente espacio en el iPod')
                )
                dialog_err.format_secondary_text(
                    f"Necesitas copiar {needed_gb:.2f} GB, pero solo hay {free_gb:.2f} GB libres en el iPod.\n\n"
                    f"Para poder sincronizar, puedes:\n"
                    f"• Eliminar música del iPod.\n"
                    f"• Usar 'Sincronización selectiva' para elegir solo algunos artistas, listas o podcasts."
                )
                dialog_err.add_button("Entendido", Gtk.ResponseType.OK)
                dialog_err.run()
                dialog_err.destroy()
                return



            # 6. Mostrar resumen exacto y desglosado
            dialog = Gtk.Dialog(title=_('Sincronización con iPod'), transient_for=self, modal=True)
            dialog.set_default_size(550, -1)
            dialog.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Sincronizar Ahora", Gtk.ResponseType.OK)
            area = dialog.get_content_area()
            area.set_spacing(10); area.set_border_width(16)
            
            lbl = Gtk.Label()
            lbl.set_markup(
                f"<b>--- MÚSICA ---</b>\n"
                f"PC: {len(local_tracks)} pistas | iPod: {len(ipod_tracks_music)} pistas\n"
                f"A copiar: {len(new_unmatched_pc)} | A eliminar: {len(to_delete_tracks) - len([t for t in to_delete_tracks if int(t.get('mediatype',0)) & 0x04])}\n\n"
                f"<b>--- PODCASTS ---</b>\n"
                f"PC (descargados): {len(local_pods)} | iPod: {len(ipod_pods)}\n"
                f"A copiar/actualizar: {len(pods_to_copy) + len(pods_to_update)} | A eliminar: {len([t for t in to_delete_tracks if int(t.get('mediatype',0)) & 0x04])}\n\n"
                f"Estadísticas actualizadas en el PC: {stats_updated}\n\n"
                f"Espacio necesario: {needed_gb:.2f} GB | Espacio libre: {free_gb:.2f} GB"
            )
            lbl.set_halign(Gtk.Align.START)
            lbl.set_line_wrap(True)
            area.pack_start(lbl, False, False, 0)

            # Desplegable para ver exactamente qué se va a borrar
            if to_delete_tracks:
                expander = Gtk.Expander(label=f"Ver las {len(to_delete_tracks)} pistas que se eliminarán del iPod")
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                box.set_border_width(6)
                for t in to_delete_tracks:
                    tipo = "[Podcast]" if int(t.get("mediatype", 0)) & 0x04 else "[Música]"
                    lbl_t = Gtk.Label(label=f"  {tipo} {t.get('artist', '?')} — {t.get('title', '?')}", xalign=0)
                    lbl_t.set_ellipsize(Pango.EllipsizeMode.END)
                    box.pack_start(lbl_t, False, False, 0)
                expander.add(box)
                area.pack_start(expander, False, False, 0)
            
            dialog.show_all()
            if dialog.run() != Gtk.ResponseType.OK:
                dialog.destroy()
                return
            dialog.destroy()
            
            resp = self._ask_backup()
            if resp == Gtk.ResponseType.CANCEL: return
            
            self.status_label.set_text(_('Iniciando sincronización…'))
            
            def worker():
                if resp == Gtk.ResponseType.YES:
                    import shutil, datetime
                    backup_dir = os.path.expanduser("~/tunepod/backups")
                    os.makedirs(backup_dir, exist_ok=True)
                    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                    try:
                        src_db = os.path.join(self.ipod_mount, "iPod_Control", "iTunes", "iTunesDB")
                        shutil.copy2(src_db, os.path.join(backup_dir, f"iTunesDB-antes-global-{stamp}"))
                    except Exception as e:
                        GLib.idle_add(self.status_label.set_text, f"Error backup: {e}")
                        return

                # 1. Borrar
                if to_delete_tracks:
                    delete_ids = [int(t["id"]) for t in to_delete_tracks]
                    GLib.idle_add(self.status_label.set_text, f"Eliminando {len(delete_ids)} pistas del iPod…")
                    out, err = ipod_write.delete_tracks(self.ipod_mount, delete_ids)
                    try: os.sync()
                    except: pass
                    
                    if err:
                        GLib.idle_add(self.status_label.set_text, f"Error al borrar: {err[:200]}")
                        return
                    
                # 2. Copiar música nueva
                if new_unmatched_pc:
                    GLib.idle_add(self.status_label.set_text, f"Sincronizando {len(new_unmatched_pc)} pistas…")
                    tracks_to_copy = []
                    for lt in new_unmatched_pc:
                        meta = ipod_copy.build_track_dict(lt["uri"])
                        if meta: tracks_to_copy.append(meta)
                    
                    if tracks_to_copy:
                        ok, cout, cerr = ipod_copy.copy_tracks(
                            self.ipod_mount, tracks_to_copy, dry_run=False,
                            progress_callback=lambda line: GLib.idle_add(self.status_label.set_text, f"Sincronizando: {line}")
                        )
                        try: os.sync()
                        except: pass
                        
                        if cerr:
                            GLib.idle_add(self.status_label.set_text, f"Error al copiar: {cerr[:200]}")
                            return

                # 3. Copiar podcasts nuevos
                if pods_to_copy:
                    GLib.idle_add(self.status_label.set_text, f"Sincronizando {len(pods_to_copy)} episodios…")
                    tracks_to_copy = []
                    for p in pods_to_copy:
                        meta = ipod_copy.build_track_dict(
                            GLib.filename_to_uri(p["path"], None),
                            extra={
                                "mediatype": 4,
                                "podcastrss": p["podcastrss"],
                                "track_nr": p["track_nr"],
                                "cd_nr": 1,
                                "title": p["title"],
                                "artist": p["artist"],
                                "album": p["album"],
                                "albumartist": p["artist"],
                                "playlist_name": p["album"],
                            }
                        )
                        if meta: tracks_to_copy.append(meta)
                    if tracks_to_copy:
                        ipod_copy.copy_tracks(
                            self.ipod_mount, tracks_to_copy, dry_run=False,
                            progress_callback=lambda line: GLib.idle_add(self.status_label.set_text, f"Sincronizando podcast: {line}")
                        )
                        try: os.sync()
                        except: pass
                        
                # 4. Actualizar el orden de los podcasts
                if pods_to_update:
                    GLib.idle_add(self.status_label.set_text, f"Actualizando orden de {len(pods_to_update)} podcasts…")
                    ipod_write.update_track_nr(self.ipod_mount, pods_to_update)
                    try: os.sync()
                    except: pass
                        
                GLib.idle_add(self.status_label.set_text, "Sincronización completada. Releyendo iPod…")
                data, rerr = ipod.read_ipod(self.ipod_mount)
                if not rerr:
                    GLib.idle_add(self._on_ipod_read, self.ipod_mount, data, None)
                    
            threading.Thread(target=worker, daemon=True).start()

        # 2. Llamamos al verificador de formatos.
        self._check_and_convert_tracks(unmatched_pc, continue_sync)

    def on_delete_local_songs(self, menu_item):
        uris = self._get_selected_uris()
        if not uris:
            return

        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"¿Eliminar {len(uris)} canción(es) de la biblioteca?"
        )
        dialog.format_secondary_text(
            "Las canciones desaparecerán de la biblioteca y de todas las listas.\n"
            "Los archivos de audio se enviarán a la papelera del sistema, "
            "por lo que podrás recuperarlos si te equivocas."
        )
        dialog.set_default_response(Gtk.ResponseType.NO)
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.YES:
            return

        moved = 0
        failed = []
        for uri in uris:
            try:
                fname = GLib.filename_from_uri(uri)[0]
            except Exception:
                continue
            if not os.path.exists(fname):
                moved += 1
                continue
            try:
                r = subprocess.run(
                    ["gio", "trash", "--", fname],
                    capture_output=True, timeout=10)
                if r.returncode == 0:
                    moved += 1
                    continue
            except Exception:
                pass
            try:
                r = subprocess.run(
                    ["trash-put", "--", fname],
                    capture_output=True, timeout=10)
                if r.returncode == 0:
                    moved += 1
                    continue
            except Exception:
                pass
            failed.append(fname)

        permanently_deleted = 0
        if failed:
            msg = Gtk.MessageDialog(
                transient_for=self, modal=True,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.YES_NO,
                text=f"{len(failed)} archivo(s) no se pudieron mover a la papelera"
            )
            msg.format_secondary_text(
                "¿Eliminarlos PERMANENTEMENTE del disco?\n\n"
                "Si eliges 'No', desaparecerán de la biblioteca de la aplicación "
                "pero los archivos seguirán existiendo en el disco."
            )
            msg.set_default_response(Gtk.ResponseType.NO)
            delete_perm = msg.run() == Gtk.ResponseType.YES
            msg.destroy()
            if delete_perm:
                for fname in failed:
                    try:
                        os.remove(fname)
                        permanently_deleted += 1
                    except Exception as e:
                        print(f"[delete] no se pudo eliminar {fname}: {e}")

        self.play_queue = [u for u in self.play_queue if u not in uris]
        self._refresh_queue_view()

        if self.current_uri in uris:
            self.playbin.set_state(Gst.State.NULL)
            self._set_play_icon(False)
            self.current_uri = None
            self.current_index = -1
            self._last_play_counted_uri = None
            self.progress.set_value(0)
            self.pos_label.set_text("0:00")
            self.dur_label.set_text("0:00")
            self.np_title.set_markup(_("<b><span size='large'>—</span></b>"))
            self.np_artist.set_markup(_("<span size='medium'>—</span>"))
            self.np_album.set_markup(_("<span size='small'>—</span>"))
            self.cover_image.set_from_icon_name(
                "audio-x-generic", Gtk.IconSize.DIALOG)
            self.cover_image.set_pixel_size(296)
            self.reset_lyrics()

        it = self.store.get_iter_first()
        while it is not None:
            uri = self.store.get_value(it, COL_URI)
            if uri in uris:
                next_it = self.store.iter_next(it)
                self.store.remove(it)
                self.known_uris.discard(uri)
                it = next_it
            else:
                it = self.store.iter_next(it)

        for pl in self.playlists:
            if "uris" in pl:
                pl["uris"] = [u for u in pl["uris"] if u not in uris]

        self._save_library()
        playlists_mod.save(self.playlists)
        self._apply_filters()
        self._rebuild_playlists_listbox(select_id=self.current_playlist_id)

        parts = [f"{moved} canción(es) eliminada(s) de la biblioteca"]
        if failed and permanently_deleted == 0:
            parts.append(f"{len(failed)} archivo(s) siguen en disco")
        elif permanently_deleted > 0:
            parts.append(f"{permanently_deleted} archivo(s) eliminados permanentemente")
        self.status_label.set_text(". ".join(parts) + ".")

    def on_send_podcast_to_ipod(self, btn):
        """Envía al iPod todos los episodios descargados del podcast
        actualmente seleccionado, ordenados por fecha ascendente
        (más antiguo = track_nr 1)."""
        if self.ipod_data is None or self.ipod_mount is None:
            self.episodes_status.set_text(
                _('Primero conecta el iPod'))
            return

        podcast_title = self._current_podcast_title()
        if not podcast_title:
            self.episodes_status.set_text(_('Selecciona un podcast primero'))
            return

        sub = None
        for s in self.subscriptions:
            if s.get("title") == podcast_title:
                sub = s
                break
        if sub is None:
            self.episodes_status.set_text(_('Suscripción no encontrada'))
            return
        author = sub.get("author") or podcast_title

        base_dir = os.path.join(
            PODCAST_DIR, podcasts.sanitize_filename(podcast_title))
        if not os.path.isdir(base_dir):
            self.episodes_status.set_text(
                _('No hay episodios descargados de este podcast'))
            return

        # Recolectar episodios descargados con su fecha (del episodes_store)
        episodes = []
        for row in self.episodes_store:
            title = row[0]
            date_str = row[1] or ""
            safe = podcasts.sanitize_filename(title)
            candidates = [
                p for p in glob.glob(os.path.join(base_dir, safe + ".*"))
                if not p.endswith(".part")
            ]
            if not candidates:
                continue
            episodes.append({
                "title": title,
                "pub_date": date_str,
                "path": candidates[0],
            })

        if not episodes:
            self.episodes_status.set_text(
                _('No hay episodios descargados de este podcast'))
            return

        # Orden ascendente. Primero intentamos por número del título
        # (para que "Episodio 10" vaya después de "Episodio 2"), y
        # como desempate usamos la fecha de publicación.
        import re as _re
        def _episode_number(title):
            t = title or ""
            m = _re.search(r"(?:episodio|episode)\s*(\d+)", t, re.IGNORECASE)
            if m:
                return int(m.group(1))
            m = _re.search(r"[Ee]\s*(\d+)", t)
            if m:
                return int(m.group(1))
            nums = _re.findall(r"\d+", t)
            if nums:
                return int(nums[-1])
            return 0

        def _podcast_sort_key(e):
            num = _episode_number(e.get("title", ""))
            date = e.get("pub_date") or "9999-99-99"
            # Si hay número, manda el número. Si no, manda la fecha.
            if num > 0:
                return (0, num, date)
            return (1, 0, date)

        episodes.sort(key=_podcast_sort_key)

        # Cruzar con lo que hay en el iPod
        ipod_tracks = self.ipod_data.get("tracks", [])
        ipod_album_tracks = [
            t for t in ipod_tracks
            if t.get("album", "") == podcast_title
        ]
        expected_titles = set(e["title"] for e in episodes)

        ipod_by_title = {}
        for t in ipod_album_tracks:
            title = t.get("title", "")
            if title in expected_titles:
                ipod_by_title[title] = t

        to_copy = []      # (episode, track_nr)
        to_update = []    # (id, track_nr)
        to_delete = []    # id

        for i, ep in enumerate(episodes):
            expected_tnr = i + 1
            ipod_t = ipod_by_title.get(ep["title"])
            if ipod_t is None:
                to_copy.append((ep, expected_tnr))
            else:
                if int(ipod_t.get("track_nr", 0)) != expected_tnr:
                    to_update.append((int(ipod_t["id"]), expected_tnr))

        for t in ipod_album_tracks:
            title = t.get("title", "")
            if title in expected_titles:
                continue
            # Huérfano: en el iPod (con mediatype podcast) pero ya no
            # está descargado. Solo borramos si tiene el bit podcast.
            if int(t.get("mediatype", 0)) & 0x04:
                to_delete.append(int(t["id"]))

        n_new = len(to_copy)
        n_update = len(to_update)
        n_delete = len(to_delete)

        if n_new == 0 and n_update == 0 and n_delete == 0:
            self.episodes_status.set_text(
                _('El iPod ya está sincronizado con este podcast'))
            return

        # Diálogo de confirmación
        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"Enviar «{podcast_title}» al iPod",
        )
        dlg.format_secondary_text(
            f"Episodios descargados: {len(episodes)}\n"
            f"A copiar (nuevos): {n_new}\n"
            f"A renumerar: {n_update}\n"
            f"A eliminar del iPod (huérfanos): {n_delete}\n\n"
            f"Se ordenarán de más antiguo a más nuevo.\n"
            f"¿Continuar?"
        )
        dlg.set_default_response(Gtk.ResponseType.YES)
        ok = dlg.run() == Gtk.ResponseType.YES
        dlg.destroy()
        if not ok:
            return

        resp_backup = self._ask_backup()
        if resp_backup == Gtk.ResponseType.CANCEL:
            return

        self.episodes_status.set_text(_('Enviando podcast al iPod…'))
        self.status_label.set_text(f"Enviando «{podcast_title}» al iPod. Por favor, espera…")

        def worker():
            try:
                # Backup
                if resp_backup == Gtk.ResponseType.YES:
                    import shutil as _sh, datetime as _dt
                    backup_dir = os.path.expanduser(
                        "~/tunepod/backups")
                    os.makedirs(backup_dir, exist_ok=True)
                    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                    src_db = os.path.join(
                        self.ipod_mount,
                        "iPod_Control", "iTunes", "iTunesDB")
                    _sh.copy2(src_db, os.path.join(
                        backup_dir,
                        f"iTunesDB-antes-podcast-{stamp}"))

                # 1. Borrar huérfanos
                if to_delete:
                    out, err = ipod_write.delete_tracks(
                        self.ipod_mount, to_delete)
                    if err:
                        GLib.idle_add(
                            self.episodes_status.set_text,
                            f"Error eliminando huérfanos: {err[:150]}")
                        return
                    try:
                        os.sync()
                    except Exception:
                        pass

                # 2. Copiar nuevos
                if to_copy:
                    tracks = []
                    for ep, tnr in to_copy:
                        meta = ipod_copy.build_track_dict(
                            GLib.filename_to_uri(ep["path"], None),
                            extra={
                                "mediatype": 4,
                                "podcastrss": sub.get("feed_url", ""),
                                "track_nr": tnr,
                                "cd_nr": 1,
                                "title": ep["title"],
                                "artist": author,
                                "album": podcast_title,
                                "albumartist": author,
                                "playlist_name": podcast_title,
                            },
                        )
                        if meta:
                            tracks.append(meta)
                    if tracks:
                        ok2, cout, cerr = ipod_copy.copy_tracks(
                            self.ipod_mount, tracks, dry_run=False,
                            progress_callback=lambda line: GLib.idle_add(
                                self.status_label.set_text, _f("Sincronizando podcast: {}", line)))
                        if cerr:
                            GLib.idle_add(
                                self.episodes_status.set_text,
                                f"Error Sincronizando: {cerr[:1500]}")
                            return
                        try:
                            os.sync()
                        except Exception:
                            pass

                # 3. Renumerar existentes
                if to_update:
                    out, err = ipod_write.update_track_nr(
                        self.ipod_mount, to_update)
                    if err:
                        GLib.idle_add(
                            self.episodes_status.set_text,
                            f"Error renumerando: {err[:150]}")
                        return
                    try:
                        os.sync()
                    except Exception:
                        pass

                # 4. Releer iPod
                data, rerr = ipod.read_ipod(self.ipod_mount)
                if rerr:
                    GLib.idle_add(
                        self.episodes_status.set_text,
                        f"Sincronizado, pero falló releer: {rerr[:100]}")
                    return
                GLib.idle_add(self._on_ipod_read,
                              self.ipod_mount, data, None)
                GLib.idle_add(
                    self.episodes_status.set_text,
                    f"«{podcast_title}» sincronizado al iPod")
            except Exception as e:
                GLib.idle_add(
                    self.episodes_status.set_text,
                    f"Error inesperado: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def on_sync_selection_to_ipod(self, btn):
        if self.ipod_data is None or self.ipod_mount is None:
            self.status_label.set_text(_('Primero conecta el iPod'))
            return

        dialog = Gtk.Dialog(title=_('Sincronización selectiva al iPod'),
                            transient_for=self, modal=True)
        dialog.set_default_size(750, 650)
        dialog.add_buttons("Cancelar", Gtk.ResponseType.CANCEL,
                           _("Sincronizar selección"), Gtk.ResponseType.OK)

        area = dialog.get_content_area()
        area.set_spacing(8)
        area.set_border_width(10)

        nb = Gtk.Notebook()
        area.pack_start(nb, True, True, 0)

        # --- Pestaña Música ---
        music_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        music_box.set_border_width(6)
        music_store = Gtk.TreeStore(bool, str, str, str)
        groups = {}
        for row in self.store:
            artist = row[COL_ARTIST] or "(Sin artista)"
            album = row[COL_ALBUM] or "(Sin álbum)"
            groups.setdefault(artist, {}).setdefault(album, []).append(row)
        for artist in sorted(groups.keys(), key=sort_key_artist):
            artist_iter = music_store.append(None, [False, artist, "", "artist"])
            for album in sorted(groups[artist].keys(), key=sort_key_artist):
                album_iter = music_store.append(artist_iter, [False, album, "", "album"])
                for row in groups[artist][album]:
                    music_store.append(album_iter,
                                       [False, row[COL_TITLE], row[COL_URI], "track"])
        music_tree = Gtk.TreeView(model=music_store)
        music_tree.get_selection().set_mode(Gtk.SelectionMode.NONE)
        toggle_music = Gtk.CellRendererToggle()
        toggle_music.set_property("activatable", True)
        music_tree.append_column(Gtk.TreeViewColumn("", toggle_music, active=0))
        r_text = Gtk.CellRendererText()
        col_text = Gtk.TreeViewColumn(_("Artista / Álbum / Canción"), r_text, text=1)
        col_text.set_expand(True)
        music_tree.append_column(col_text)
        scroll_music = Gtk.ScrolledWindow()
        scroll_music.add(music_tree)
        music_box.pack_start(scroll_music, True, True, 0)
        nb.append_page(music_box, Gtk.Label(label=_('Música')))

        # --- Pestaña Listas ---
        pl_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        pl_box.set_border_width(6)
        pl_store = Gtk.ListStore(bool, str, str)
        for pl in self.playlists:
            if playlists_mod.is_smart(pl):
                continue
            pl_store.append([False, pl["name"], pl["id"]])
        pl_tree = Gtk.TreeView(model=pl_store)
        toggle_pl = Gtk.CellRendererToggle()
        toggle_pl.set_property("activatable", True)
        pl_tree.append_column(Gtk.TreeViewColumn("", toggle_pl, active=0))
        pl_tree.append_column(Gtk.TreeViewColumn(_("Lista"), Gtk.CellRendererText(), text=1))
        scroll_pl = Gtk.ScrolledWindow()
        scroll_pl.add(pl_tree)
        pl_box.pack_start(scroll_pl, True, True, 0)
        nb.append_page(pl_box, Gtk.Label(label=_('Listas')))

        # --- Pestaña Podcasts ---
        pod_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        pod_box.set_border_width(6)
        pod_store = Gtk.ListStore(bool, str, str)
        if os.path.isdir(PODCAST_DIR):
            for folder in sorted(os.listdir(PODCAST_DIR)):
                folder_path = os.path.join(PODCAST_DIR, folder)
                if os.path.isdir(folder_path):
                    pod_store.append([False, folder, folder_path])
        pod_tree = Gtk.TreeView(model=pod_store)
        toggle_pod = Gtk.CellRendererToggle()
        toggle_pod.set_property("activatable", True)
        pod_tree.append_column(Gtk.TreeViewColumn("", toggle_pod, active=0))
        pod_tree.append_column(Gtk.TreeViewColumn("Podcast", Gtk.CellRendererText(), text=1))
        scroll_pod = Gtk.ScrolledWindow()
        scroll_pod.add(pod_tree)
        pod_box.pack_start(scroll_pod, True, True, 0)
        nb.append_page(pod_box, Gtk.Label(label=_('Podcasts')))

        summary_lbl = Gtk.Label(label=_('Nada seleccionado'))
        summary_lbl.set_halign(Gtk.Align.START)
        area.pack_start(summary_lbl, False, False, 0)

        space_bar = Gtk.ProgressBar()
        space_bar.set_show_text(False)
        area.pack_start(space_bar, False, False, 0)

        space_lbl = Gtk.Label(label="")
        space_lbl.set_halign(Gtk.Align.START)
        space_lbl.set_line_wrap(True)
        area.pack_start(space_lbl, False, False, 0)

        def _walk_tree(store, parent=None):
            it = store.iter_children(parent) if parent else store.get_iter_first()
            while it:
                yield it
                yield from _walk_tree(store, it)
                it = store.iter_next(it)

        def _selection_size_bytes(uris_set, pod_folders_list):
            total = 0
            for uri in uris_set:
                try:
                    fname = GLib.filename_from_uri(uri)[0]
                    if os.path.exists(fname):
                        total += os.path.getsize(fname)
                except Exception:
                    pass
            for folder in pod_folders_list:
                for f in glob.glob(os.path.join(folder, "*")):
                    if f.endswith(".part"):
                        continue
                    try:
                        total += os.path.getsize(f)
                    except Exception:
                        pass
            return total

        def update_summary():
            n_music = 0
            sel_uris = set()
            for it in _walk_tree(music_store):
                if music_store.get_value(it, 0) and music_store.get_value(it, 3) == "track":
                    n_music += 1
                    sel_uris.add(music_store.get_value(it, 2))
            n_pl = 0
            for row in pl_store:
                if row[0]:
                    n_pl += 1
                    pl = self._get_playlist(row[2])
                    if pl:
                        sel_uris.update(pl.get("uris", []))
            n_pod = 0
            sel_pods = []
            for row in pod_store:
                if row[0]:
                    n_pod += 1
                    sel_pods.append(row[2])
            summary_lbl.set_text(
                _f("Seleccionado: {} canciones, {} listas, {} podcasts", n_music, n_pl, n_pod))

            if self.ipod_mount:
                try:
                    usage = shutil.disk_usage(self.ipod_mount)
                    used_gb = usage.used / (1024**3)
                    free_gb = usage.free / (1024**3)
                    total_gb = usage.total / (1024**3)
                    sel_bytes = _selection_size_bytes(sel_uris, sel_pods)
                    sel_gb = sel_bytes / (1024**3)
                    needed = used_gb + sel_gb
                    frac = needed / total_gb if total_gb > 0 else 0
                    if frac > 1.0:
                        frac = 1.0
                    space_bar.set_fraction(frac)
                    if sel_gb > free_gb:
                        space_lbl.set_markup(
                            f"<span foreground='red'><b>¡No cabe!</b> "
                            f"Se necesitan {sel_gb:.2f} GB, hay {free_gb:.2f} GB libres.</span>")
                    else:
                        space_lbl.set_text(
                            _f("iPod: {} GB usados / {} GB libres. ", f"{used_gb:.2f}", f"{free_gb:.2f}") +
                            _f("Selección: {} GB.", f"{sel_gb:.2f}"))
                except Exception:
                    space_lbl.set_text(_('No se pudo calcular el espacio'))

        def on_toggle_music(cell, path):
            it = music_store.get_iter(path)
            new_val = not music_store.get_value(it, 0)
            music_store.set_value(it, 0, new_val)
            for child in _walk_tree(music_store, it):
                music_store.set_value(child, 0, new_val)
            update_summary()

        def on_toggle_pl(cell, path):
            it = pl_store.get_iter(path)
            pl_store.set_value(it, 0, not pl_store.get_value(it, 0))
            update_summary()

        def on_toggle_pod(cell, path):
            it = pod_store.get_iter(path)
            pod_store.set_value(it, 0, not pod_store.get_value(it, 0))
            update_summary()

        toggle_music.connect("toggled", on_toggle_music)
        toggle_pl.connect("toggled", on_toggle_pl)
        toggle_pod.connect("toggled", on_toggle_pod)

        update_summary()

        dialog.show_all()
        response = dialog.run()

        if response != Gtk.ResponseType.OK:
            dialog.destroy()
            return

        # Leer selecciones ANTES de destruir el diálogo
        direct_uris = set()
        for it in _walk_tree(music_store):
            if music_store.get_value(it, 0) and music_store.get_value(it, 3) == "track":
                direct_uris.add(music_store.get_value(it, 2))

        lists_selected = []
        for row in pl_store:
            if row[0]:
                pl = self._get_playlist(row[2])
                if pl:
                    lists_selected.append((pl["name"], list(pl.get("uris", []))))

        all_uris = set(direct_uris)
        for _name, uris in lists_selected:
            all_uris.update(uris)

        selected_podcasts = []
        for row in pod_store:
            if row[0]:
                selected_podcasts.append(row[2])

        dialog.destroy()

        if not all_uris and not selected_podcasts:
            self.status_label.set_text(_('No seleccionaste nada'))
            return

        self._execute_sync_selection(all_uris, lists_selected, selected_podcasts)


    def _execute_sync_selection(self, all_uris, lists_selected, selected_podcasts):
        store_by_uri = {row[COL_URI]: row for row in self.store}
        local_tracks = []
        for uri in all_uris:
            row = store_by_uri.get(uri)
            if row is None:
                continue
            local_tracks.append({
                "uri": uri,
                "title": row[COL_TITLE] or "",
                "artist": row[COL_ARTIST] or "",
                "album": row[COL_ALBUM] or "",
                "duration_str": row[COL_DURATION] or "",
            })

        if not local_tracks and not selected_podcasts:
            self.status_label.set_text(_('Las canciones seleccionadas no están en la biblioteca'))
            return

        self.status_label.set_text(_('Buscando coincidencias en el iPod…'))

        def worker():
            ipod_tracks = self.ipod_data.get("tracks", [])
            matched, unmatched = ipod_sync.match_tracks(local_tracks, ipod_tracks)
            GLib.idle_add(self._sync_selection_continue,
                          matched, unmatched, selected_podcasts,
                          local_tracks, lists_selected)

        threading.Thread(target=worker, daemon=True).start()

    def _sync_selection_continue(self, matched, unmatched, selected_podcasts,
                                  local_tracks, lists_selected):
        def after_convert(new_unmatched):
            self._sync_selection_copy(matched, new_unmatched, selected_podcasts,
                                       local_tracks, lists_selected)
        self._check_and_convert_tracks(unmatched, after_convert)

    def _sync_selection_copy(self, matched, unmatched, selected_podcasts,
                              local_tracks, lists_selected):
        mount = self.ipod_mount

        tracks_to_copy = []
        for lt in unmatched:
            meta = ipod_copy.build_track_dict(lt["uri"])
            if meta:
                tracks_to_copy.append(meta)

        pod_episodes = []
        for folder_path in selected_podcasts:
            folder_name = os.path.basename(folder_path)
            sub = next((s for s in self.subscriptions if s.get("title") == folder_name), None)
            author = sub.get("author", folder_name) if sub else folder_name
            rss = sub.get("feed_url", "") if sub else ""
            files = [f for f in glob.glob(os.path.join(folder_path, "*"))
                     if not f.endswith(".part") and f.lower().endswith(AUDIO_EXT)]
            files.sort()
            for i, fpath in enumerate(files):
                pod_episodes.append({
                    "path": fpath,
                    "title": os.path.splitext(os.path.basename(fpath))[0],
                    "album": folder_name,
                    "artist": author,
                    "track_nr": i + 1,
                    "podcastrss": rss,
                })

        self.status_label.set_text(
            _f("Sincronizando {} canciones y {} podcasts…", len(tracks_to_copy), len(pod_episodes)))

        def worker():
            if tracks_to_copy:
                ok, out, err = ipod_copy.copy_tracks(
                    mount, tracks_to_copy, dry_run=False,
                    progress_callback=lambda line: GLib.idle_add(
                        self.status_label.set_text, _f("Sincronizando al iPod: {}", line)))
                try:
                    os.sync()
                except Exception:
                    pass
                if err:
                    GLib.idle_add(self.status_label.set_text,
                                  f"Error Sincronizando al iPod: {err[:150]}")
                    return

            if pod_episodes:
                pod_dicts = []
                for ep in pod_episodes:
                    meta = ipod_copy.build_track_dict(
                        GLib.filename_to_uri(ep["path"], None),
                        extra={
                            "mediatype": 4,
                            "podcastrss": ep["podcastrss"],
                            "track_nr": ep["track_nr"],
                            "cd_nr": 1,
                            "title": ep["title"],
                            "artist": ep["artist"],
                            "album": ep["album"],
                            "albumartist": ep["artist"],
                            "playlist_name": ep["album"],
                        }
                    )
                    if meta:
                        pod_dicts.append(meta)
                if pod_dicts:
                    ok, out, err = ipod_copy.copy_tracks(
                        mount, pod_dicts, dry_run=False,
                        progress_callback=lambda line: GLib.idle_add(
                            self.status_label.set_text, _f("Sincronizando podcast: {}", line)))
                    try:
                        os.sync()
                    except Exception:
                        pass

            data, rerr = ipod.read_ipod(mount)
            if rerr:
                GLib.idle_add(self.status_label.set_text,
                              f"Copia hecha, pero falló releer: {rerr}")
                return

            ipod_tracks = data.get("tracks", [])
            matched_new, _ = ipod_sync.match_tracks(local_tracks, ipod_tracks)
            matched_map = dict(matched_new)

            n_playlists_created = 0
            for name, uris in lists_selected:
                track_ids = []
                for uri in uris:
                    tid = matched_map.get(uri)
                    if tid is not None:
                        track_ids.append(tid)
                if not track_ids:
                    continue
                out, err = ipod_write.sync_playlist(mount, name, track_ids)
                if err:
                    print(f"[sync_selection] error creando playlist '{name}': {err}")
                else:
                    n_playlists_created += 1
                try:
                    os.sync()
                except Exception:
                    pass

            data2, _ = ipod.read_ipod(mount)
            GLib.idle_add(self._on_ipod_read, mount, data2, None)
            msg = _f("Sincronización selectiva completada: {} canciones", len(matched_new))
            if n_playlists_created:
                msg += _f(", {} lista(s) creada(s)", n_playlists_created)
            GLib.idle_add(self.status_label.set_text, msg)

        threading.Thread(target=worker, daemon=True).start()

    def on_send_to_ipod_clicked(self, menu_item):
        """Envía al iPod las canciones seleccionadas (sin borrar nada)."""
        if self.ipod_data is None or self.ipod_mount is None:
            self.status_label.set_text(
                _('Primero conecta el iPod y espera a que se detecte'))
            return

        uris = self._get_selected_uris()
        if not uris:
            self.status_label.set_text(_('Selecciona alguna canción primero'))
            return

        store_by_uri = {row[COL_URI]: row for row in self.store}
        local_tracks = []
        for uri in uris:
            row = store_by_uri.get(uri)
            if row is None:
                continue
            local_tracks.append({
                "uri": uri,
                "title": row[COL_TITLE] or "",
                "artist": row[COL_ARTIST] or "",
                "album": row[COL_ALBUM] or "",
                "duration_str": row[COL_DURATION] or "",
            })

        if not local_tracks:
            self.status_label.set_text(
                _('Las canciones seleccionadas no están en la biblioteca'))
            return

        self.status_label.set_text(
            _f("Comprobando {} canciones frente al iPod…", len(local_tracks)))

        def worker():
            ipod_tracks = self.ipod_data.get("tracks", [])
            matched, unmatched = ipod_sync.match_tracks(
                local_tracks, ipod_tracks)
            GLib.idle_add(self._send_to_ipod_continue, matched, unmatched)

        threading.Thread(target=worker, daemon=True).start()

    def _send_to_ipod_continue(self, matched, unmatched):
        if not unmatched:
            self.status_label.set_text(
                f"Las {len(matched)} canciones seleccionadas ya están en el iPod")
            return

        def after_convert(new_unmatched):
            if not new_unmatched:
                self.status_label.set_text(
                    f"Las {len(matched)} canciones ya están en el iPod")
                return
            self._send_to_ipod_copy(matched, new_unmatched)

        self._check_and_convert_tracks(unmatched, after_convert)

    def _send_to_ipod_copy(self, matched, unmatched):
        mount = self.ipod_mount
        tracks_to_copy = []
        for lt in unmatched:
            meta = ipod_copy.build_track_dict(lt["uri"])
            if meta:
                tracks_to_copy.append(meta)

        if not tracks_to_copy:
            self.status_label.set_text(
                _('No se pudo preparar ninguna canción para copiar'))
            return

        n_new = len(tracks_to_copy)
        n_already = len(matched)
        self.status_label.set_text(
            _f("Enviando {} canciones al iPod ({} ya estaban)…", n_new, n_already))

        def worker():
            ok, out, err = ipod_copy.copy_tracks(
                mount, tracks_to_copy, dry_run=False,
                progress_callback=lambda line: GLib.idle_add(
                    self.status_label.set_text, f"Enviando: {line}"))
            try:
                os.sync()
            except Exception:
                pass
            if err:
                GLib.idle_add(self.status_label.set_text,
                              f"Error al enviar al iPod: {err[:200]}")
                return
            data, rerr = ipod.read_ipod(mount)
            if rerr:
                GLib.idle_add(self.status_label.set_text,
                              f"Copiado, pero falló releer el iPod: {rerr}")
                return
            GLib.idle_add(self._on_ipod_read, mount, data, None)
            GLib.idle_add(
                self.status_label.set_text,
                _f("Enviadas {} canciones al iPod ({} ya estaban)", n_new, n_already))

        threading.Thread(target=worker, daemon=True).start()


    def _init_mpris(self):
        try:
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            bus = dbus.SessionBus()
            
            # 1. Nombre principal (TunePod)
            bus_name = dbus.service.BusName("org.mpris.MediaPlayer2.tunepod", bus, replace_existing=True, allow_replacement=True)
            
            # 2. Nombre antiguo (iPodSuite) como ALIAS para que las teclas del MacBook sigan funcionando
            try:
                self.bus_name_old = dbus.service.BusName("org.mpris.MediaPlayer2.ipodsuite", bus, replace_existing=False, allow_replacement=True)
            except Exception:
                pass # Si por alguna razón falla, no importa, el nuevo funciona bien
                
            self.mpris = Mpris2Adapter(bus_name, self)
        except Exception as e:
            print(f"[MPRIS] No disponible: {e}")

    def _update_mpris_status(self, status):
        if hasattr(self, 'mpris') and self.mpris:
            try:
                self.mpris.update_status(status)
            except Exception:
                pass

    def _update_mpris_metadata(self, title, artist, album):
        if hasattr(self, 'mpris') and self.mpris:
            try:
                self.mpris.update_metadata(title, artist, album)
            except Exception:
                pass

    def on_destroy(self, *args):
        self.shutting_down = True
        self.playbin.set_state(Gst.State.NULL)
        self.import_cancelled = True
        self.lyrics_request_id += 1
        self._save_library()
        try:
            preferences.save(self.prefs)
        except Exception:
            pass
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass
        try:
            if os.path.exists(SOCKET_PATH):
                os.remove(SOCKET_PATH)
        except OSError:
            pass
        Gtk.main_quit()
        import sys
        sys.exit(0)

if __name__ == "__main__":
    w = PlaylistWindow()
    w.show_all()
    Gtk.main()
