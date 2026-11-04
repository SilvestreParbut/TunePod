/* ipod_read.c — Lee un iPod con libgpod y emite JSON. Solo lectura. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <gpod/itdb.h>

static void json_string(const char *s) {
    if (!s) { printf("\"\""); return; }
    putchar('"');
    for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
        unsigned char c = *p;
        switch (c) {
            case '"':  printf("\\\""); break;
            case '\\': printf("\\\\"); break;
            case '\n': printf("\\n"); break;
            case '\r': printf("\\r"); break;
            case '\t': printf("\\t"); break;
            case '\b': printf("\\b"); break;
            case '\f': printf("\\f"); break;
            default:
                if (c < 0x20) printf("\\u%04x", c);
                else putchar(c);
        }
    }
    putchar('"');
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "uso: %s <mount_point>\n", argv[0]);
        return 1;
    }

    Itdb_iTunesDB *db = itdb_parse(argv[1], NULL);
    if (!db) {
        fprintf(stderr, "error: no se pudo leer iTunesDB en %s\n", argv[1]);
        return 2;
    }

    int n_tracks = g_list_length(db->tracks);
    int n_playlists = g_list_length(db->playlists);

    /* --- NUEVA FORMA DE OBTENER LA GENERACIÓN --- */
    const char *gen_str = "unknown";
    /* Creamos un dispositivo temporal y le asignamos el punto de montaje */
    Itdb_Device *device = itdb_device_new();
    if (device) {
        itdb_device_set_mountpoint(device, argv[1]);
        const Itdb_IpodInfo *ipod_info = itdb_device_get_ipod_info(device);
        if (ipod_info) {
            gen_str = itdb_info_get_ipod_generation_string(ipod_info->ipod_generation);
        }
        itdb_device_free(device);
    }

    printf("{\n");
    printf("  \"generation\": ");
    json_string(gen_str);
    printf(",\n");
    printf("  \"track_count\": %d,\n", n_tracks);
    printf("  \"playlist_count\": %d,\n", n_playlists);
    printf("  \"tracks\": [\n");

    int first = 1;
    for (GList *it = db->tracks; it != NULL; it = it->next) {
        Itdb_Track *t = (Itdb_Track *)it->data;
        if (!first) printf(",\n");
        first = 0;
        printf("    {\"id\": %u", (unsigned)t->id);
        printf(", \"title\": ");      json_string(t->title);
        printf(", \"artist\": ");     json_string(t->artist);
        printf(", \"album\": ");      json_string(t->album);
        printf(", \"albumartist\": ");json_string(t->albumartist);
        printf(", \"track_nr\": %d", (int)t->track_nr);
        printf(", \"cd_nr\": %d",    (int)t->cd_nr);
        printf(", \"duration_ms\": %d", (int)t->tracklen);
        printf(", \"rating\": %d",   (int)t->rating);
        printf(", \"playcount\": %d",(int)t->playcount);
        printf(", \"size\": %d",     (int)t->size);
        printf(", \"ipod_path\": ");  json_string(t->ipod_path);
        printf(", \"mediatype\": %d", (int)t->mediatype);
        printf("}");
    }
    printf("\n  ],\n");
    printf("  \"playlists\": [\n");

    first = 1;
    for (GList *pl = db->playlists; pl != NULL; pl = pl->next) {
        Itdb_Playlist *p = (Itdb_Playlist *)pl->data;
        if (!first) printf(",\n");
        first = 0;
        printf("    {\"name\": ");
        json_string(p->name);
        printf(", \"is_master\": %s", itdb_playlist_is_mpl(p) ? "true" : "false");
        printf(", \"is_smart\": %s",  p->is_spl ? "true" : "false");
        printf(", \"track_count\": %u", (unsigned)g_list_length(p->members));
        printf(", \"track_ids\": [");
        int first_track = 1;
        for (GList *tr = p->members; tr != NULL; tr = tr->next) {
            Itdb_Track *t = (Itdb_Track *)tr->data;
            if (!first_track) printf(", ");
            printf("%u", (unsigned)t->id);
            first_track = 0;
        }
        printf("]}");
    }
    printf("\n  ]\n}\n");

    itdb_free(db);
    return 0;
}
