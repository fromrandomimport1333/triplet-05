"""List a remote zip via HTTP Range and optionally extract one member (stdlib only)."""
import sys, struct, zlib, urllib.request
url = sys.argv[1]; want = sys.argv[2] if len(sys.argv) > 2 else None; out = sys.argv[3] if len(sys.argv) > 3 else None
def rng(a, b):
    req = urllib.request.Request(url, headers={'Range': f'bytes={a}-{b}', 'User-Agent': 'Mozilla/5.0'})
    return urllib.request.urlopen(req, timeout=300).read()
size = int(urllib.request.urlopen(urllib.request.Request(url, method='HEAD'), timeout=60).headers['Content-Length'])
tail = rng(size - 65536, size - 1); i = tail.rfind(b'PK\x05\x06')
n, cdsize, cdoff = struct.unpack('<HII', tail[i+10:i+20])
cd = rng(cdoff, cdoff + cdsize - 1); p = 0; ents = []
while p < len(cd):
    (_, _, _, _, meth, _, _, crc, csz, usz, fnl, exl, cml, _, _, _, lho) = struct.unpack('<IHHHHHHIIIHHHHHII', cd[p:p+46])
    name = cd[p+46:p+46+fnl].decode('cp437'); ents.append((name, csz, usz, lho, meth)); p += 46 + fnl + exl + cml
for e in ents: print(f'{e[0]:45s} comp={e[1]:>10} uncomp={e[2]:>11}')
if want:
    e = next(x for x in ents if x[0].lower().endswith(want.lower()))
    h = rng(e[3], e[3] + 29); fnl, exl = struct.unpack('<HH', h[26:30]); start = e[3] + 30 + fnl + exl
    data = rng(start, start + e[1] - 1)
    open(out, 'wb').write(zlib.decompress(data, -15) if e[4] == 8 else data); print('wrote', out)
