import ipod

mounts = ipod.find_mounted_ipods()
if not mounts:
    print('No hay iPod conectado')
    raise SystemExit

data, err = ipod.read_ipod(mounts[0])
if err:
    print('Error:', err)
    raise SystemExit

print(f'Total tracks en el iPod: {len(data["tracks"])}')
print()

# Ver qué álbumes tienen tracks con mediatype podcast
albumes = {}
for t in data['tracks']:
    a = t.get('album', '')
    if a and (int(t.get('mediatype', 0)) & 0x04):
        albumes[a] = albumes.get(a, 0) + 1

print('Álbumes con al menos un track de tipo podcast:')
for a in sorted(albumes):
    print(f'  - {a!r}  ({albumes[a]} tracks)')
print()

# Mostrar todos los episodios de cada álbum, ordenados por track_nr
for alb in sorted(albumes):
    print(f'=== {alb!r} ===')
    eps = [t for t in data['tracks'] if t.get('album') == alb]
    for t in sorted(eps, key=lambda x: x['track_nr']):
        print(f"  track_nr={t['track_nr']:3d}  id={t['id']:5d}  {t['title']}")
    print()
