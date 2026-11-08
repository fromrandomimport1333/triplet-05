"""Walk local headers of a truncated zip; decompress whatever members (or partial member) are present."""
import sys, struct, zlib, os
buf = open(sys.argv[1], 'rb').read(); outdir = sys.argv[2]; p = 0
while p + 30 <= len(buf) and buf[p:p+4] == b'PK\x03\x04':
    flag, meth = struct.unpack('<HH', buf[p+6:p+10]); csz, usz = struct.unpack('<II', buf[p+18:p+26])
    fnl, exl = struct.unpack('<HH', buf[p+26:p+30]); name = buf[p+30:p+30+fnl].decode('cp437'); s = p+30+fnl+exl
    if name.endswith('/'): p = s; continue
    d = zlib.decompressobj(-15); data = d.decompress(buf[s:s+csz] if csz else buf[s:])
    complete = d.eof
    fn = os.path.join(outdir, os.path.basename(name) + ('' if complete else '.partial'))
    open(fn, 'wb').write(data); print(name, 'meth', meth, 'flag', flag, 'csz', csz, 'got', len(data), 'complete', complete)
    if not complete: break
    s2 = s + (csz if csz else len(buf[s:]) - len(d.unused_data))
    if flag & 8: s2 += 16 if buf[s2:s2+4] == b'PK\x07\x08' else 12
    p = s2
