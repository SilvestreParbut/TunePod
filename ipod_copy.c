/* ipod_copy.c — Copia archivos de audio al iPod y crea sus tracks. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <gpod/itdb.h>
#include <glib.h>
#include <glib-object.h>
#include <json-glib/json-glib.h>

static void usage(void) {
    fprintf(stderr, "uso: ipod_copy [--dry-run] --copy <json_file> <mount>\n");
}

static Itdb_Playlist *find_or_create_playlist(Itdb_iTunesDB *db, const char *name, int *created) {
    *created = 0;
    if (!name || !*name) return NULL;
    for (GList *it = db->playlists; it != NULL; it = it->next) {
        Itdb_Playlist *p = (Itdb_Playlist *)it->data;
        if (p->name && strcmp(p->name, name) == 0) return p;
    }
    Itdb_Playlist *pl = itdb_playlist_new(name, FALSE);
    if (pl) {
        itdb_playlist_add(db, pl, -1);
        *created = 1;
    }
    return pl;
}

static char *json_get_string(JsonObject *obj, const char *key) {
    if (!json_object_has_member(obj, key)) return g_strdup("");
    JsonNode *node = json_object_get_member(obj, key);
    if (!JSON_NODE_HOLDS_VALUE(node)) return g_strdup("");
    const char *s = json_node_get_string(node);
    return g_strdup(s ? s : "");
}

static int json_get_int(JsonObject *obj, const char *key) {
    if (!json_object_has_member(obj, key)) return 0;
    JsonNode *node = json_object_get_member(obj, key);
    if (!JSON_NODE_HOLDS_VALUE(node)) return 0;
    return (int)json_node_get_int(node);
}

static int copy_one_track(Itdb_iTunesDB *db, const char *mount, JsonObject *obj, int dry_run) {
    char *local_path = json_get_string(obj, "local_path");
    char *title = json_get_string(obj, "title");
    char *artist = json_get_string(obj, "artist");
    char *album = json_get_string(obj, "album");
    char *albumartist = json_get_string(obj, "albumartist");
    char *filetype = json_get_string(obj, "filetype");
    char *playlist_name = json_get_string(obj, "playlist_name");
    char *podcastrss = json_get_string(obj, "podcastrss");
    
    int track_nr = json_get_int(obj, "track_nr");
    int cd_nr = json_get_int(obj, "cd_nr");
    int release_date = json_get_int(obj, "release_date");
    int mediatype = json_get_int(obj, "mediatype");
    int duration_ms = json_get_int(obj, "duration_ms");
    int bitrate = json_get_int(obj, "bitrate");
    int samplerate = json_get_int(obj, "samplerate");
    int start_time_ms = json_get_int(obj, "start_time_ms");

    int rc = 0;
    
    /* NUEVO: Enviar la información al instante a Python */
    printf("  → «%s» — «%s» [%s]\n", artist, title, filetype);
    fflush(stdout); 
    
    printf("    local: %s\n", local_path);
    fflush(stdout);

    FILE *f = fopen(local_path, "rb");
    if (!f) {
        fprintf(stderr, "    ERROR: no se puede abrir el archivo local\n");
        rc = 1;
        goto cleanup;
    }
    fseek(f, 0, SEEK_END);
    long local_size = ftell(f);
    fseek(f, 0, SEEK_SET);
    fclose(f);

    if (dry_run) {
        printf("    [DRY-RUN] copiaría %ld bytes\n", local_size);
        fflush(stdout);
        goto cleanup;
    }

    Itdb_Track *track = itdb_track_new();
    if (!track) {
        fprintf(stderr, "    ERROR: itdb_track_new falló\n");
        rc = 1;
        goto cleanup;
    }

    if (*title)       { g_free(track->title); track->title = g_strdup(title); }
    if (*artist)      { g_free(track->artist); track->artist = g_strdup(artist); }
    if (*album)       { g_free(track->album); track->album = g_strdup(album); }
    if (*albumartist) { g_free(track->albumartist); track->albumartist = g_strdup(albumartist); }
    
    track->track_nr = track_nr;
    track->cd_nr = cd_nr;
    
    if (mediatype > 0) track->mediatype = (guint32)mediatype;
    if (*podcastrss) { g_free(track->podcastrss); track->podcastrss = g_strdup(podcastrss); }
    
    track->tracklen = duration_ms;
    if (release_date > 0) track->time_released = (guint32)release_date;
    track->bitrate = bitrate;
    track->samplerate = samplerate;
    track->size = (gint32)local_size;
    if (start_time_ms > 0) track->starttime = (guint32)start_time_ms;

    if (track->mediatype == ITDB_MEDIATYPE_PODCAST) {
        track->skip_when_shuffling = 1;
    }

    if (g_ascii_strcasecmp(filetype, "m4a") == 0 || g_ascii_strcasecmp(filetype, "aac") == 0) {
        track->filetype = g_strdup("AAC audio file");
        track->filetype_marker = 0x00000000; 
    } else {
        track->filetype = g_strdup("MPEG audio file");
        track->filetype_marker = 0x4d504733; 
    }

    itdb_track_add(db, track, -1);

    if (!itdb_cp_track_to_ipod(track, local_path, NULL)) {
        fprintf(stderr, "    ERROR: itdb_cp_track_to_ipod falló\n");
        itdb_track_remove(track);
        rc = 1;
        goto cleanup;
    }
    
    /* NUEVO: Enviar confirmación al instante a Python */
    printf("    ✓ copiado a %s\n", track->ipod_path);
    fflush(stdout);

    /* LÓGICA DE PLAYLISTS */
    if (track->mediatype == ITDB_MEDIATYPE_PODCAST) {
        Itdb_Playlist *podcasts_pl = itdb_playlist_podcasts(db);
        if (!podcasts_pl) {
            podcasts_pl = itdb_playlist_new("Podcasts", FALSE);
            if (podcasts_pl) {
                podcasts_pl->podcastflag = 1;
                itdb_playlist_add(db, podcasts_pl, -1);
            }
        }
        if (podcasts_pl) {
            if (g_list_find(podcasts_pl->members, track) == NULL) {
                itdb_playlist_add_track(podcasts_pl, track, -1);
                printf("    ✓ añadido a la lista de Podcasts\n");
                fflush(stdout);
            }
        }
    } else {
        Itdb_Playlist *mpl = itdb_playlist_mpl(db);
        if (mpl) {
            if (g_list_find(mpl->members, track) == NULL) {
                itdb_playlist_add_track(mpl, track, -1);
                printf("    ✓ añadido a la maestra\n");
                fflush(stdout);
            }
        }
    }

    /* AÑADIR A LISTA DE REPRODUCCIÓN PERSONALIZADA */
    if (*playlist_name) {
        int created = 0;
        Itdb_Playlist *pl = find_or_create_playlist(db, playlist_name, &created);
        if (pl) {
            if (g_list_find(pl->members, track) == NULL) {
                itdb_playlist_add_track(pl, track, -1);
            }
            if (created) {
                printf("    ✓ playlist «%s» creada y añadida\n", playlist_name);
                fflush(stdout);
            } else {
                printf("    ✓ añadido a playlist existente «%s»\n", playlist_name);
                fflush(stdout);
            }
        }
    }

cleanup:
    g_free(local_path); g_free(title); g_free(artist); g_free(album);
    g_free(albumartist); g_free(filetype); g_free(playlist_name); g_free(podcastrss);
    return rc;
}

int main(int argc, char *argv[]) {
    int dry_run = 0;
    int argi = 1;
    const char *json_file = NULL;
    const char *mount = NULL;

    if (argc < 2) { usage(); return 1; }
    if (strcmp(argv[argi], "--dry-run") == 0) { dry_run = 1; argi++; }
    if (argi >= argc) { usage(); return 1; }
    if (strcmp(argv[argi], "--copy") != 0) { usage(); return 1; }
    argi++;
    if (argi + 1 >= argc) { usage(); return 1; }
    json_file = argv[argi++];
    mount = argv[argi++];

    printf("Modo: %s\n", dry_run ? "solo lectura (dry-run)" : "ESCRITURA");
    printf("iPod: %s\nJSON: %s\n\n", mount, json_file);
    fflush(stdout);

    JsonParser *parser = json_parser_new();
    GError *err = NULL;
    if (!json_parser_load_from_file(parser, json_file, &err)) {
        fprintf(stderr, "error leyendo JSON: %s\n", err->message);
        g_error_free(err); g_object_unref(parser); return 1;
    }
    JsonNode *root = json_parser_get_root(parser);
    if (!JSON_NODE_HOLDS_ARRAY(root)) {
        fprintf(stderr, "error: el JSON debe ser un array\n");
        g_object_unref(parser); return 1;
    }
    JsonArray *arr = json_node_get_array(root);
    guint n = json_array_get_length(arr);
    printf("Canciones a procesar: %u\n\n", n);
    fflush(stdout);

    Itdb_iTunesDB *db = itdb_parse(mount, NULL);
    if (!db) {
        fprintf(stderr, "error: no se pudo leer iTunesDB\n");
        g_object_unref(parser); return 2;
    }

    int total_ok = 0, total_err = 0;
    for (guint i = 0; i < n; i++) {
        JsonObject *obj = json_array_get_object_element(arr, i);
        if (!obj) continue;
        int rc = copy_one_track(db, mount, obj, dry_run);
        if (rc == 0) total_ok++; else total_err++;
    }

    printf("\n=== RESUMEN ===\nOK: %d, Errores: %d\n", total_ok, total_err);
    fflush(stdout);

    if (!dry_run && total_ok > 0) {
        printf("Guardando iTunesDB…\n");
        fflush(stdout);
        GError *err = NULL;
        if (!itdb_write(db, &err)) {
            fprintf(stderr, "ERROR DETALLADO: %s\n", err ? err->message : "sin mensaje de error");
            if (err) g_error_free(err);
            itdb_free(db); g_object_unref(parser); return 3;
        }
        printf("✓ iTunesDB guardado\n");
        fflush(stdout);
    }
    itdb_free(db); g_object_unref(parser);
    return total_err > 0 ? 1 : 0;
}
