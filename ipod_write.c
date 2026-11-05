/* ipod_write.c — Modifica playlists, borra pistas, inicializa y actualiza metadatos. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <gpod/itdb.h>
#include <json-glib/json-glib.h>

static void usage(void) {
    fprintf(stderr,
        "uso: ipod_write [--dry-run] <comando> <args...>\n"
        "\n"
        "Comandos:\n"
        "  --init <mount>\n"
        "  --list-playlists <mount>\n"
        "  --create-playlist <name> <mount>\n"
        "  --delete-playlist <name> <mount>\n"
        "  --sync-playlist <name> <ids_file> <mount>\n"
        "  --delete-tracks <ids_file> <mount>\n"
        "  --update-track-nr <pairs_file> <mount>\n"
        "  --update-metadata <json_file> <mount>\n"
        "\n"
        "  --dry-run  no modifica nada, solo muestra qué haría\n");
}

static Itdb_Playlist *find_playlist_by_name(Itdb_iTunesDB *db, const char *name) {
    for (GList *it = db->playlists; it != NULL; it = it->next) {
        Itdb_Playlist *p = (Itdb_Playlist *)it->data;
        if (p->name && strcmp(p->name, name) == 0) {
            return p;
        }
    }
    return NULL;
}

static int do_list_playlists(Itdb_iTunesDB *db) {
    printf("Playlists (%d):\n", g_list_length(db->playlists));
    for (GList *it = db->playlists; it != NULL; it = it->next) {
        Itdb_Playlist *p = (Itdb_Playlist *)it->data;
        const char *kind = itdb_playlist_is_mpl(p) ? "MAESTRA" :
                           (p->is_spl ? "smart" : "normal");
        printf("  [%s] «%s» → %u pistas\n", kind,
               p->name ? p->name : "(sin nombre)",
               (unsigned)g_list_length(p->members));
    }
    return 0;
}

static int do_create_playlist(Itdb_iTunesDB *db, const char *name, int dry_run) {
    if (!name || !*name) {
        fprintf(stderr, "error: nombre vacío\n");
        return 1;
    }
    if (find_playlist_by_name(db, name)) {
        fprintf(stderr, "error: ya existe una playlist llamada «%s»\n", name);
        return 1;
    }
    printf("[CREATE] «%s»\n", name);
    if (dry_run) {
        printf("[DRY-RUN] no se ha creado\n");
        return 0;
    }
    Itdb_Playlist *pl = itdb_playlist_new(name, FALSE);
    if (!pl) {
        fprintf(stderr, "error: itdb_playlist_new falló\n");
        return 1;
    }
    itdb_playlist_add(db, pl, -1);
    if (!itdb_write(db, NULL)) {
        fprintf(stderr, "error: itdb_write falló\n");
        return 1;
    }
    printf("✓ creada\n");
    return 0;
}

static int do_delete_playlist(Itdb_iTunesDB *db, const char *name, int dry_run) {
    Itdb_Playlist *pl = find_playlist_by_name(db, name);
    if (!pl) {
        fprintf(stderr, "error: no existe playlist «%s»\n", name);
        return 1;
    }
    if (itdb_playlist_is_mpl(pl)) {
        fprintf(stderr, "error: no se puede eliminar la playlist maestra\n");
        return 1;
    }
    printf("[DELETE] «%s» (%u pistas)\n", name,
           (unsigned)g_list_length(pl->members));
    if (dry_run) {
        printf("[DRY-RUN] no se ha eliminado\n");
        return 0;
    }
    itdb_playlist_remove(pl);
    if (!itdb_write(db, NULL)) {
        fprintf(stderr, "error: itdb_write falló\n");
        return 1;
    }
    printf("✓ eliminada\n");
    return 0;
}

static int do_sync_playlist(Itdb_iTunesDB *db, const char *name,
                            const char *ids_file, int dry_run) {
    if (!name || !*name) {
        fprintf(stderr, "error: nombre vacío\n");
        return 1;
    }
    if (strcmp(name, "ipod") == 0) {
        fprintf(stderr, "error: no se puede modificar la maestra\n");
        return 1;
    }

    FILE *f = fopen(ids_file, "r");
    if (!f) {
        fprintf(stderr, "error: no se puede abrir %s\n", ids_file);
        return 1;
    }

    guint32 *ids = NULL;
    int n_ids = 0, cap = 0;
    char line[64];
    while (fgets(line, sizeof(line), f)) {
        char *p = line;
        while (*p == ' ' || *p == '\t') p++;
        if (*p == '\0' || *p == '\n' || *p == '#') continue;
        guint32 id = strtoul(p, NULL, 10);
        if (id == 0) continue;
        int found = 0;
        for (int i = 0; i < n_ids; i++) {
            if (ids[i] == id) { found = 1; break; }
        }
        if (found) continue;
        if (n_ids >= cap) {
            cap = cap ? cap * 2 : 64;
            ids = realloc(ids, cap * sizeof(guint32));
        }
        ids[n_ids++] = id;
    }
    fclose(f);

    printf("[SYNC-PLAYLIST] «%s» ← %d track IDs únicos\n", name, n_ids);

    Itdb_Playlist *existing = find_playlist_by_name(db, name);
    if (existing) {
        if (itdb_playlist_is_mpl(existing)) {
            fprintf(stderr, "error: no se puede sincronizar sobre la maestra\n");
            free(ids);
            return 1;
        }
        printf("  Eliminando playlist existente «%s»\n", name);
        if (!dry_run) {
            itdb_playlist_remove(existing);
        }
    }

    if (dry_run) {
        int found = 0, notfound = 0;
        for (int i = 0; i < n_ids; i++) {
            Itdb_Track *t = itdb_track_by_id(db, ids[i]);
            if (t) found++; else notfound++;
        }
        printf("  [DRY-RUN] encontraría %d tracks, %d no encontrados\n",
               found, notfound);
        free(ids);
        return 0;
    }

    Itdb_Playlist *pl = itdb_playlist_new(name, FALSE);
    if (!pl) {
        fprintf(stderr, "error: itdb_playlist_new falló\n");
        free(ids);
        return 1;
    }
    itdb_playlist_add(db, pl, -1);

    int added = 0, notfound = 0;
    for (int i = 0; i < n_ids; i++) {
        Itdb_Track *t = itdb_track_by_id(db, ids[i]);
        if (t) {
            if (g_list_find(pl->members, t) == NULL) {
                itdb_playlist_add_track(pl, t, -1);
                added++;
            }
        } else {
            notfound++;
        }
    }
    free(ids);

    printf("  Añadidos %d tracks, %d no encontrados\n", added, notfound);

    if (!itdb_write(db, NULL)) {
        fprintf(stderr, "error: itdb_write falló\n");
        return 1;
    }
    printf("✓ playlist sincronizada\n");
    return 0;
}

static int do_update_track_nr(Itdb_iTunesDB *db, const char *file, int dry_run) {
    FILE *f = fopen(file, "r");
    if (!f) {
        fprintf(stderr, "error: no se puede abrir %s\n", file);
        return 1;
    }

    int updated = 0, notfound = 0;
    unsigned id;
    int tn;
    while (fscanf(f, "%u %d", &id, &tn) == 2) {
        Itdb_Track *t = itdb_track_by_id(db, id);
        if (t) {
            printf("[UPDATE] id=%u: track_nr %d -> %d\n", id, (int)t->track_nr, tn);
            if (!dry_run) t->track_nr = tn;
            updated++;
        } else {
            notfound++;
        }
    }
    fclose(f);

    printf("Actualizados: %d, no encontrados: %d\n", updated, notfound);
    if (!dry_run && updated > 0) {
        if (!itdb_write(db, NULL)) {
            fprintf(stderr, "ERROR: itdb_write falló\n");
            return 2;
        }
        printf("✓ iTunesDB guardado\n");
    }
    return 0;
}

static int do_delete_tracks(Itdb_iTunesDB *db, const char *ids_file, int dry_run, const char *mount) {
    FILE *f = fopen(ids_file, "r");
    if (!f) {
        fprintf(stderr, "error: no se puede abrir %s\n", ids_file);
        return 1;
    }

    guint32 *ids = NULL;
    int n_ids = 0, cap = 0;
    guint32 id;
    while (fscanf(f, "%u", &id) == 1) {
        if (n_ids >= cap) {
            cap = cap ? cap * 2 : 64;
            ids = realloc(ids, cap * sizeof(guint32));
        }
        ids[n_ids++] = id;
    }
    fclose(f);

    printf("Procesando %d IDs…\n", n_ids);

    char **paths_to_delete = calloc(n_ids, sizeof(char *));
    int deleted = 0, notfound = 0;

    for (int i = 0; i < n_ids; i++) {
        Itdb_Track *t = itdb_track_by_id(db, ids[i]);
        if (t) {
            printf("[DELETE] %u: %s\n", ids[i], t->title ? t->title : "(sin título)");
            if (!dry_run) {
                if (t->ipod_path) {
                    char physical[2048];
                    strncpy(physical, t->ipod_path, sizeof(physical));
                    physical[sizeof(physical)-1] = '\0';
                    for (char *p = physical; *p; p++) {
                        if (*p == ':') *p = '/';
                    }
                    char full_path[4096];
                    snprintf(full_path, sizeof(full_path), "%s%s", mount, physical);
                    paths_to_delete[deleted] = strdup(full_path);
                }
                
                for (GList *pl = db->playlists; pl != NULL; pl = pl->next) {
                    Itdb_Playlist *p = (Itdb_Playlist *)pl->data;
                    if (g_list_find(p->members, t) != NULL) {
                        itdb_playlist_remove_track(p, t);
                    }
                }
                itdb_track_remove(t);
            }
            deleted++;
        } else {
            notfound++;
        }
    }

    printf("Borrados de la base de datos: %d, No encontrados: %d\n", deleted, notfound);

    if (!dry_run && deleted > 0) {
        if (!itdb_write(db, NULL)) {
            fprintf(stderr, "ERROR: itdb_write falló.\n");
            for(int i=0; i<deleted; i++) free(paths_to_delete[i]);
            free(paths_to_delete); free(ids);
            return 2;
        }
        printf("✓ iTunesDB guardado.\n");

        for (int i = 0; i < deleted; i++) {
            if (paths_to_delete[i]) {
                if (remove(paths_to_delete[i]) == 0) {
                    printf("  ✓ archivo borrado: %s\n", paths_to_delete[i]);
                } else {
                    printf("  ⚠ no se pudo borrar archivo: %s\n", paths_to_delete[i]);
                }
                free(paths_to_delete[i]);
            }
        }
    }
    free(paths_to_delete); free(ids);
    return 0;
}

static int do_update_metadata(Itdb_iTunesDB *db, const char *json_file, int dry_run) {
    JsonParser *parser = json_parser_new();
    GError *error = NULL;
    if (!json_parser_load_from_file(parser, json_file, &error)) {
        fprintf(stderr, "error leyendo JSON: %s\n", error->message);
        g_error_free(error); g_object_unref(parser); return 1;
    }

    JsonNode *root = json_parser_get_root(parser);
    if (!JSON_NODE_HOLDS_ARRAY(root)) {
        fprintf(stderr, "error: el JSON debe ser un array\n");
        g_object_unref(parser); return 1;
    }

    JsonArray *arr = json_node_get_array(root);
    guint n = json_array_get_length(arr);
    printf("Actualizando metadatos de %u pistas…\n", n);

    int updated = 0;
    for (guint i = 0; i < n; i++) {
        JsonObject *obj = json_array_get_object_element(arr, i);
        
        guint32 track_id = 0;
        if (json_object_has_member(obj, "id")) {
            JsonNode *node = json_object_get_member(obj, "id");
            if (JSON_NODE_HOLDS_VALUE(node)) track_id = json_node_get_int(node);
        }
        if (track_id == 0) continue;

        Itdb_Track *t = itdb_track_by_id(db, track_id);
        if (!t) continue;

        if (json_object_has_member(obj, "title")) { g_free(t->title); t->title = g_strdup(json_object_get_string_member(obj, "title")); }
        if (json_object_has_member(obj, "artist")) { g_free(t->artist); t->artist = g_strdup(json_object_get_string_member(obj, "artist")); }
        if (json_object_has_member(obj, "album")) { g_free(t->album); t->album = g_strdup(json_object_get_string_member(obj, "album")); }
        if (json_object_has_member(obj, "albumartist")) { g_free(t->albumartist); t->albumartist = g_strdup(json_object_get_string_member(obj, "albumartist")); }
        if (json_object_has_member(obj, "genre")) { g_free(t->genre); t->genre = g_strdup(json_object_get_string_member(obj, "genre")); }
        
        if (json_object_has_member(obj, "track_nr")) {
            JsonNode *node = json_object_get_member(obj, "track_nr");
            if (JSON_NODE_HOLDS_VALUE(node)) t->track_nr = (gint32)json_node_get_int(node);
        }
        if (json_object_has_member(obj, "cd_nr")) {
            JsonNode *node = json_object_get_member(obj, "cd_nr");
            if (JSON_NODE_HOLDS_VALUE(node)) t->cd_nr = (gint32)json_node_get_int(node);
        }
        if (json_object_has_member(obj, "year")) {
            JsonNode *node = json_object_get_member(obj, "year");
            if (JSON_NODE_HOLDS_VALUE(node)) t->time_released = (guint32)json_node_get_int(node);
        }
        if (json_object_has_member(obj, "start_time_ms")) {
            JsonNode *node = json_object_get_member(obj, "start_time_ms");
            if (JSON_NODE_HOLDS_VALUE(node)) t->starttime = (guint32)json_node_get_int(node);
        }

        updated++;
    }

    g_object_unref(parser);

    if (!dry_run && updated > 0) {
        if (!itdb_write(db, &error)) {
            fprintf(stderr, "ERROR al guardar iTunesDB: %s\n", error ? error->message : "desconocido");
            if (error) g_error_free(error);
            return 2;
        }
        printf("✓ iTunesDB guardado\n");
    }
    return 0;
}

int main(int argc, char *argv[]) {
    int dry_run = 0;
    int argi = 1;

    if (argc < 2) { usage(); return 1; }
    if (strcmp(argv[argi], "--dry-run") == 0) { dry_run = 1; argi++; }
    if (argi >= argc) { usage(); return 1; }

    const char *cmd = argv[argi++];
    const char *mount = NULL;
    const char *name = NULL;
    const char *ids_file = NULL;
    const char *json_file = NULL;

    if (strcmp(cmd, "--init") == 0) {
        if (argi >= argc) { usage(); return 1; }
        mount = argv[argi++];
    } else if (strcmp(cmd, "--list-playlists") == 0) {
        if (argi >= argc) { usage(); return 1; }
        mount = argv[argi++];
    } else if (strcmp(cmd, "--create-playlist") == 0) {
        if (argi + 1 >= argc) { usage(); return 1; }
        name = argv[argi++]; mount = argv[argi++];
    } else if (strcmp(cmd, "--delete-playlist") == 0) {
        if (argi + 1 >= argc) { usage(); return 1; }
        name = argv[argi++]; mount = argv[argi++];
    } else if (strcmp(cmd, "--sync-playlist") == 0) {
        if (argi + 2 >= argc) { usage(); return 1; }
        name = argv[argi++]; ids_file = argv[argi++]; mount = argv[argi++];
    } else if (strcmp(cmd, "--delete-tracks") == 0) {
        if (argi + 1 >= argc) { usage(); return 1; }
        ids_file = argv[argi++]; mount = argv[argi++];
    } else if (strcmp(cmd, "--update-track-nr") == 0) {
        if (argi + 1 >= argc) { usage(); return 1; }
        ids_file = argv[argi++]; mount = argv[argi++];
    } else if (strcmp(cmd, "--update-metadata") == 0) {
        if (argi + 1 >= argc) { usage(); return 1; }
        json_file = argv[argi++]; mount = argv[argi++];
    } else {
        fprintf(stderr, "comando desconocido: %s\n", cmd); usage(); return 1;
    }

    printf("Modo: %s\n", dry_run ? "solo lectura (dry-run)" : "ESCRITURA");
    printf("iPod: %s\n\n", mount);

    if (strcmp(cmd, "--init") == 0) {
        GError *err = NULL;
        gboolean success = itdb_init_ipod(mount, NULL, "iPod", &err);
        if (success) { printf("✓ iPod inicializado correctamente\n"); return 0; }
        else { fprintf(stderr, "error: no se pudo inicializar: %s\n", err ? err->message : "desconocido"); return 1; }
    }

    Itdb_iTunesDB *db = itdb_parse(mount, NULL);
    if (!db) { fprintf(stderr, "error: no se pudo leer iTunesDB en %s\n", mount); return 2; }

    int rc = 0;
    if (strcmp(cmd, "--list-playlists") == 0) rc = do_list_playlists(db);
    else if (strcmp(cmd, "--create-playlist") == 0) rc = do_create_playlist(db, name, dry_run);
    else if (strcmp(cmd, "--delete-playlist") == 0) rc = do_delete_playlist(db, name, dry_run);
    else if (strcmp(cmd, "--sync-playlist") == 0) rc = do_sync_playlist(db, name, ids_file, dry_run);
    else if (strcmp(cmd, "--delete-tracks") == 0) rc = do_delete_tracks(db, ids_file, dry_run, mount);
    else if (strcmp(cmd, "--update-track-nr") == 0) rc = do_update_track_nr(db, ids_file, dry_run);
    else if (strcmp(cmd, "--update-metadata") == 0) rc = do_update_metadata(db, json_file, dry_run);

    itdb_free(db);
    return rc;
}
