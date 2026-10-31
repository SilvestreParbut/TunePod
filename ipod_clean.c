/* ipod_clean.c — Elimina las playlists no-maestras de un iPod.
 *
 * Solo toca las playlists. NO toca las canciones ni la maestra.
 * El archivo del iPod se modifica ÚNICAMENTE si se pasa --write.
 *
 * Compilar:
 *   gcc -O2 -o ipod_clean ipod_clean.c $(pkg-config --cflags --libs libgpod-1.0 glib-2.0)
 *
 * Uso:
 *   ./ipod_clean --dry-run <mount_point>   # solo muestra qué haría
 *   ./ipod_clean --write   <mount_point>   # aplica los cambios
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <gpod/itdb.h>

int main(int argc, char *argv[]) {
    if (argc < 3) {
        fprintf(stderr,
            "uso: %s --dry-run|--write <mount_point>\n", argv[0]);
        return 1;
    }

    int do_write = 0;
    if (strcmp(argv[1], "--write") == 0) {
        do_write = 1;
    } else if (strcmp(argv[1], "--dry-run") == 0) {
        do_write = 0;
    } else {
        fprintf(stderr, "opción desconocida: %s\n", argv[1]);
        return 1;
    }

    const char *mount = argv[2];

    Itdb_iTunesDB *db = itdb_parse(mount, NULL);
    if (!db) {
        fprintf(stderr, "error: no se pudo leer iTunesDB en %s\n", mount);
        return 2;
    }

    printf("Modo: %s\n", do_write ? "ESCRITURA" : "solo lectura (dry-run)");
    printf("iPod: %s\n", mount);
    printf("Playlists totales: %d\n\n", g_list_length(db->playlists));

    GList *to_remove = NULL;
    int protected_mpl = 0;

    for (GList *it = db->playlists; it != NULL; it = it->next) {
        Itdb_Playlist *p = (Itdb_Playlist *)it->data;
        if (itdb_playlist_is_mpl(p)) {
            protected_mpl++;
            printf("[PRESERVAR] MAESTRA: «%s» (%u pistas)\n",
                p->name ? p->name : "(sin nombre)",
                (unsigned)g_list_length(p->members));
        } else {
            printf("[ELIMINAR]  «%s» (%u pistas)\n",
                p->name ? p->name : "(sin nombre)",
                (unsigned)g_list_length(p->members));
            to_remove = g_list_append(to_remove, p);
        }
    }

    printf("\nTotal a eliminar: %d playlists\n", g_list_length(to_remove));
    printf("Playlist maestra preservada: %d\n", protected_mpl);

    if (protected_mpl != 1) {
        fprintf(stderr,
            "\nERROR: se esperaba exactamente 1 playlist maestra, "
            "se encontraron %d.\nAbortando por seguridad.\n", protected_mpl);
        g_list_free(to_remove);
        itdb_free(db);
        return 3;
    }

    if (!do_write) {
        printf("\n[DRY-RUN] No se ha modificado nada.\n");
        printf("Para aplicar los cambios, ejecuta con --write\n");
        g_list_free(to_remove);
        itdb_free(db);
        return 0;
    }

    printf("\nAplicando eliminación…\n");
    for (GList *it = to_remove; it != NULL; it = it->next) {
        Itdb_Playlist *p = (Itdb_Playlist *)it->data;
        itdb_playlist_remove(p);
    }
    g_list_free(to_remove);

    printf("Guardando iTunesDB…\n");
    if (!itdb_write(db, NULL)) {
        fprintf(stderr, "ERROR: itdb_write falló. El iPod podría estar "
                        "inconsistente. Restaura el backup.\n");
        itdb_free(db);
        return 4;
    }

    printf("✓ Hecho. iTunesDB guardado.\n");
    itdb_free(db);
    return 0;
}
