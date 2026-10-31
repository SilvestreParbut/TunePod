#!/usr/bin/env python3
"""Reemplaza textos específicos en player.py y translations.py."""
import re

PLAYER = "/home/franco/tunepod/player.py"
TRANS = "/home/franco/tunepod/translations.py"

# (texto_buscado, texto_reemplazado) — reemplazos exactos en player.py
PLAYER_REPLACEMENTS = [
    # Títulos de ventanas
    ('text=f"Información del Álbum"', 'text=_("Información del Álbum")'),
    # Botones de las ventanas
    ('"Cerrar", Gtk.ResponseType.CLOSE', '_("Cerrar"), Gtk.ResponseType.CLOSE'),
    ('"Editar metadatos", Gtk.ResponseType.OK', '_("Editar metadatos"), Gtk.ResponseType.OK'),
    ('"Editar metadatos del álbum", Gtk.ResponseType.OK', '_("Editar metadatos del álbum"), Gtk.ResponseType.OK'),
    ('"Guardar en todas", Gtk.ResponseType.OK', '_("Guardar en todas"), Gtk.ResponseType.OK'),
    # Etiquetas de las filas (solo las que aparecen sin _())
    ('add_row(0, "Artista:", artist)', 'add_row(0, _("Artista:"), artist)'),
    ('add_row(1, "Álbum:", album)', 'add_row(1, _("Álbum:"), album)'),
    ('add_row(2, "Género:", genre)', 'add_row(2, _("Género:"), genre)'),
    ('add_row(3, "Año:", str(year) if year else "")', 'add_row(3, _("Año:"), str(year) if year else "")'),
    ('add_row(4, "Canciones:", str(len(album_uris)))', 'add_row(4, _("Canciones:"), str(len(album_uris)))'),
    # Checkbox aplicar a todo el álbum
    ('label=f"Aplicar a todo el álbum «{current_album}» ({len(album_uris)} canciones)"',
     'label=_f("Aplicar a todo el álbum «{}» ({} canciones)", current_album, len(album_uris))'),
    # Botones de edición
    ('"🔍 Buscar información en línea…"', '_("🔍 Buscar información en línea…")'),
    # Mensajes de estado
    ('text=f"¿Eliminar la lista «{pl[\'name\']}»?"',
     'text=_f("¿Eliminar la lista «{}»?", pl[\'name\'])'),
    ('text=f"¿Eliminar {len(uris)} canción(es) de la biblioteca?"',
     'text=_f("¿Eliminar {} canción(es) de la biblioteca?", len(uris))'),
    ('text=f"¿Eliminar {n} elemento(s) del iPod?"',
     'text=_f("¿Eliminar {} elemento(s) del iPod?", n)'),
    ('text=f"{len(failed)} archivo(s) no se pudieron mover a la papelera"',
     'text=_f("{} archivo(s) no se pudieron mover a la papelera", len(failed))'),
]

# Claves nuevas para translations.py
NEW_KEYS = {
    "Información del Álbum": "Album information",
    "Cerrar": "Close",
    "Editar metadatos": "Edit metadata",
    "Editar metadatos del álbum": "Edit album metadata",
    "Guardar en todas": "Save to all",
    "Artista:": "Artist:",
    "Álbum:": "Album:",
    "Género:": "Genre:",
    "Año:": "Year:",
    "Canciones:": "Songs:",
    "Aplicar a todo el álbum «{}» ({} canciones)": "Apply to entire album «{}» ({} songs)",
    "🔍 Buscar información en línea…": "🔍 Search info online...",
    "¿Eliminar la lista «{}»?": "Delete playlist «{}»?",
    "¿Eliminar {} canción(es) de la biblioteca?": "Remove {} song(s) from the library?",
    "¿Eliminar {} elemento(s) del iPod?": "Remove {} item(s) from iPod?",
    "{} archivo(s) no se pudieron mover a la papelera": "{} file(s) could not be moved to the trash",
}

def main():
    # --- player.py ---
    with open(PLAYER, "r", encoding="utf-8") as f:
        src = f.read()
    changed = 0
    for old, new in PLAYER_REPLACEMENTS:
        if old in src:
            src = src.replace(old, new)
            changed += 1
        else:
            print(f"  ⚠️  no encontrado en player.py: {old[:60]}...")
    with open(PLAYER, "w", encoding="utf-8") as f:
        f.write(src)
    print(f"✅ player.py: {changed} reemplazos")

    # --- translations.py ---
    with open(TRANS, "r", encoding="utf-8") as f:
        trans = f.read()
    added = 0
    block = "        # === Novena pasada (ventanas info) ===\n"
    for k, v in NEW_KEYS.items():
        key_line = f'        "{k}": "{v}",'
        if key_line not in trans:
            block += key_line + "\n"
            added += 1
    if added:
        marker = "        # === Plantillas con {} (usar _f) ==="
        trans = trans.replace(marker, block + "\n" + marker, 1)
        with open(TRANS, "w", encoding="utf-8") as f:
            f.write(trans)
    print(f"✅ translations.py: {added} claves añadidas")

if __name__ == "__main__":
    main()