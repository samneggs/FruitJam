#!/usr/bin/env python3
"""Bit-exact check + timing of the float16 kernels in fluid16_k.mpy (160x120).
Inputs are generated here (exact in float16/float32), uploaded, and every kernel's output CRC
is compared with a float32/float16 reference. usage: fluid16_bench.py [kernel ...]"""
import struct, sys, ast, zlib, json, time
from rp import Board
from fluid16_gen import W, H, R16, R32, HROWS

N = W * H
PIT = 4
OMEGA = 1.7                             # SOR factor for the pressure test (1.0 = Gauss-Seidel)
SCALE = 48.0
SPL = (37.25, 41.75, 0.875, -0.625, 0.8125, 0.109375)
PAL = bytes((i * 7 + 3) & 0xff for i in range(256))
def f(x): return struct.unpack('<f', struct.pack('<f', x))[0]
def h(x): return struct.unpack('<e', struct.pack('<e', x))[0]
def fbits(x): return struct.unpack('<I', struct.pack('<f', x))[0]
def sbits(x): return struct.unpack('<i', struct.pack('<f', x))[0]   # for array('i') slots
def fma32(a, b, c):
    """a * b + c with one float32 rounding (vfma): exact rational, nearest float32, ties to even."""
    from fractions import Fraction
    q = Fraction(a) * Fraction(b) + Fraction(c)
    x = f(float(q)); bx = fbits(x)
    cand = [x] + [struct.unpack('<f', struct.pack('<I', (bx + d) & 0xffffffff))[0] for d in (-1, 1)]
    return min(cand, key=lambda y: (abs(Fraction(y) - q), fbits(y) & 1))
W4, OM1 = f(OMEGA / 4), f(1 - OMEGA)

def xs(seed, n):
    s = seed
    for _ in range(n):
        s ^= (s << 13) & 0xffffffff; s ^= s >> 17; s ^= (s << 5) & 0xffffffff
        yield s
IN16 = {'u': [((s & 2047) - 1024) / 256 for s in xs(1, N)],      # velocity in [-4, 4)
        'v': [((s & 2047) - 1024) / 256 for s in xs(2, N)],
        'a': [(s & 1023) / 256 for s in xs(3, N)],               # dye in [0, 4)
        'b': [((s & 2047) - 1024) / 1024 for s in xs(4, N)]}     # div in [-1, 1)
P32 = [((s & 8191) - 4096) / 4096 for s in xs(5, N)]
def b16(a): return struct.pack('<%de' % len(a), *a)
def b32(a): return struct.pack('<%df' % len(a), *a)

def interior(y0=1, y1=H - 1):
    for y in range(y0, y1):
        for x in range(1, W - 1):
            yield x, y, y * W + x

def ref_adv(src, u, v, decay):
    out = [0.0] * N
    for x, y, i in interior():
        px = min(max(f(x - u[i]), 0.5), W - 1.5)
        py = min(max(f(y - v[i]), 0.5), H - 1.5)
        ix, iy = int(px), int(py)
        fx, fy = f(px - ix), f(py - iy)
        j = iy * W + ix
        a = f(src[j] + f(f(src[j + 1] - src[j]) * fx))
        b = f(src[j + W] + f(f(src[j + W + 1] - src[j + W]) * fx))
        out[i] = h(f(f(a + f(f(b - a) * fy)) * decay))
    return out

def ref_div(u, v):
    out = [0.0] * N
    for x, y, i in interior():
        out[i] = h(f(f(f(u[i + 1] - u[i - 1]) + f(v[i + W] - v[i - W])) * 0.5))
    return out

def ref_rb(p, div, iters):
    p = p[:]
    for _ in range(iters):
        for c in (0, 1):
            for x, y, i in interior():
                if (x + y) & 1 == c:
                    p[i] = f(f(p[i] * OM1) + f(f(f(f(p[i - 1] + p[i + 1]) + f(p[i - W] + p[i + W])) - div[i]) * W4))
    return p

def ref_proj(u, v, p):
    u, v = u[:], v[:]
    for x, y, i in interior():
        u[i] = h(f(u[i] - f(f(p[i + 1] - p[i - 1]) * 0.5)))
        v[i] = h(f(v[i] - f(f(p[i + W] - p[i - W]) * 0.5)))
    return u, v

def ref_render(d):
    fb = bytearray(320 * 240)
    for y in range(H):
        for x in range(W):
            c = PAL[max(0, min(255, int(f(d[y * W + x] * SCALE))))]
            for dy in (0, 1):
                fb[(2 * y + dy) * 320 + 2 * x] = fb[(2 * y + dy) * 320 + 2 * x + 1] = c
    return bytes(fb)

def ref_index(d):                       # + one zero pad row (and 4 bytes) for the bilinear reads
    return bytes(max(0, min(255, int(f(x * SCALE)))) for x in d) + bytes(W + 4)

def ref_bilin(ix):
    fb = bytearray(320 * 240)
    for y in range(H):
        for x in range(W):
            i = y * W + x
            a, a1, b, b1 = ix[i], ix[i + 1], ix[i + W], ix[i + W + 1]
            h0, h1, v0 = (a + a1) >> 1, (b + b1) >> 1, (a + b) >> 1
            c = (h0 + h1) >> 1
            o = 2 * y * 320 + 2 * x
            fb[o], fb[o + 1], fb[o + 320], fb[o + 321] = PAL[a], PAL[h0], PAL[v0], PAL[c]
    return bytes(fb)

def ref_splat(u, v, d):
    u, v, d = u[:], v[:], d[:]
    cx, cy, fx, fy, amt, inv = SPL
    for y in range(int(cy) - 3, int(cy) + 4):
        dy2 = f(f(y - cy) * f(y - cy))
        for x in range(int(cx) - 3, int(cx) + 4):
            dx = f(x - cx)
            w = max(0.0, f(1.0 - f(f(f(dx * dx) + dy2) * inv)))
            i = y * W + x
            u[i] = h(f(u[i] + f(fx * w))); v[i] = h(f(v[i] + f(fy * w))); d[i] = h(f(d[i] + f(amt * w)))
    return u, v, d

BOARD_SETUP = f'''
import sys
if '/FluidFM' not in sys.path: sys.path.insert(0, '/FluidFM')   # the demo's modules live in /FluidFM
import machine, gc, uctypes, struct, array, binascii, fluid16_k as K
from machine import mem32
mem32[0xE000EDFC] |= 1 << 24
mem32[0xE0001000] |= 1
gc.collect()
N = {N}
B = {{}}
for k, sz in (('ct', 16), ('p', 4 * N), ('fb', 320 * 240), ('ix', {N + W + 4}), ('u', 2 * N), ('v', 2 * N), ('a', 2 * N), ('b', 2 * N), ('d1', 2 * N), ('d2', 2 * N)):
    B[k] = bytearray(sz)
    assert uctypes.addressof(B[k]) >= 0x20000000, k + ' not in SRAM'
B['z'] = bytearray(2 * N); B['pc'] = bytearray(4 * N)     # untimed helpers: may land in PSRAM
A = {{k: uctypes.addressof(x) for k, x in B.items()}}
'''

BOARD_RUN = r'''
def timed(fn, prep):
    best = 1 << 30
    for _ in range(3):
        prep()
        st = machine.disable_irq(); t0 = mem32[0xE0001004]; fn(); t1 = mem32[0xE0001004]; machine.enable_irq(st)
        best = min(best, (t1 - t0) & 0xffffffff)
    return best
crc = binascii.crc32
R = {}
OFF16, OFF32 = HROWS * R16, HROWS * R32
def z(*ks):
    for k in ks: B[k][:] = B['z']
def cp(dst, src): B[dst][:] = B[src]
CTA = A['ct']                           # row counters for the advect kernels (SRAM: ldrex/strex)
def rows(): mem32[CTA] = mem32[CTA + 4] = mem32[CTA + 8] = mem32[CTA + 12] = 1
PUV = struct.pack('<IiIi2i5f', A['u'], A['v'] - A['u'], A['d1'], A['d2'] - A['d1'], CTA, H - 1, 0.99, 0.5, W - 1.5, H - 1.5, 1.0)
PDA = bytearray(struct.pack('<II5f3i', A['u'], A['v'], 0.99, 0.5, W - 1.5, H - 1.5, 1.0, CTA + 4, H - 1, 0))
FL = array.array('i', [0])
PR = array.array('i', [1, H - 1, PIT, uctypes.addressof(FL), uctypes.addressof(FL), 0, 0, 0, 0, W4B, OM1B])
cx, cy, fx, fy, amt, inv = SPL
B['pc'][:] = B['p']
PS = struct.pack('<3I4i9f', A['d1'], A['d2'], A['b'], int(cx) - 3, int(cx) + 4, int(cy) - 3, int(cy) + 4, cx, cy, fx, fy, amt, inv, 1.0, 1.0, 1.0)
SC = struct.unpack('<I', struct.pack('<f', SCALE))[0]
def prep_rb():
    cp('p', 'pc'); FL[0] = 0; PR[8] = 0
pal = bytes(PAL)
BC = array.array('i', [0, 0, 0, 60])        # bilin_h ctl: no spectrum strip, 60 grid rows
CASES = {
 'advect_uv': (lambda: K.advect_uv(PUV), lambda: (z('d1', 'd2'), rows()), ('d1', 'd2')),
 'advect_r': (lambda: K.advect_r(B['d1'], B['a'], PDA), lambda: (z('d1'), rows()), ('d1',)),
 'divergence_h': (lambda: K.divergence_h(B['d1'], B['u'], B['v'], CTA + 8), lambda: (z('d1'), rows()), ('d1',)),
 'pressure_rb': (lambda: K.pressure_rb(B['p'], B['b'], PR), prep_rb, ('p',)),
 'project_h': (lambda: K.project_h(B['d1'], B['d2'], B['p'], CTA + 12), lambda: (cp('d1', 'u'), cp('d2', 'v'), cp('p', 'pc'), rows()), ('d1', 'd2')),
 'render_h': (lambda: (K.render_h(B['fb'], B['a'], pal, SC), K.render_h(A['fb'] + 120 * 320, A['a'] + 60 * R16, pal, SC)), lambda: None, ('fb',)),
 'index_h': (lambda: (K.index_h(B['ix'], B['a'], SC), K.index_h(A['ix'] + 60 * W, A['a'] + 60 * R16, SC)), lambda: None, ('ix',)),
 'bilin_h': (lambda: (K.bilin_h(B['fb'], B['ix'], pal, BC), K.bilin_h(A['fb'] + 120 * 320, A['ix'] + 60 * W, pal, BC)),
             lambda: (K.index_h(B['ix'], B['a'], SC), K.index_h(A['ix'] + 60 * W, A['a'] + 60 * R16, SC)), ('fb',)),
 'splat': (lambda: K.splat(PS), lambda: (cp('d1', 'u'), cp('d2', 'v'), cp('b', 'a')), ('d1', 'd2', 'b')),   # run last: clobbers b
}
def to_psram(fn):                       # same code, fetched through the XIP cache (code bus) instead
    global _cb
    _cb = bytearray(300000)                 # too big for SRAM -> PSRAM
    src = mem32[id(fn) + 8] & 0xffffffff
    dst = ((uctypes.addressof(_cb) + 255) & ~255) + (src & 31)
    for i in range(0, 1024, 4):
        mem32[(dst & ~3) + i] = mem32[(src & ~3) + i]
    mem32[id(fn) + 8] = dst
    return hex(dst)
for k in NAMES:
    if k.endswith('@psram'):
        print('relocated', to_psram(getattr(K, k.split('@')[0])))
        CASES[k] = CASES[k.split('@')[0]]
    fn, prep, outs = CASES[k]
    c = timed(fn, prep)
    cc = 0
    for o in outs: cc = crc(B[o], cc)
    R[k] = (c, cc)
print('RESULT', R)
'''

REFS = {
 'advect_uv': lambda I: b16(ref_adv(I['u'], I['u'], I['v'], f(0.99))) + b16(ref_adv(I['v'], I['u'], I['v'], f(0.99))),
 'advect_r': lambda I: b16(ref_adv(I['a'], I['u'], I['v'], f(0.99))),
 'divergence_h': lambda I: b16(ref_div(I['u'], I['v'])),
 'pressure_rb': lambda I: b32(ref_rb(P32, I['b'], PIT)),
 'project_h': lambda I: b''.join(b16(x) for x in ref_proj(I['u'], I['v'], P32)),
 'render_h': lambda I: ref_render(I['a']),
 'index_h': lambda I: ref_index(I['a']),
 'bilin_h': lambda I: ref_bilin(ref_index(I['a'])),
 'splat': lambda I: b''.join(b16(x) for x in ref_splat(I['u'], I['v'], I['a'])),
}
WORK = {'index_h': N, 'bilin_h': 320 * 240, 'advect_uv': (W - 2) * (H - 2), 'advect_r': (W - 2) * (H - 2), 'divergence_h': (W - 2) * (H - 2),
        'pressure_rb': (W - 2) * (H - 2) * PIT, 'project_h': (W - 2) * (H - 2), 'render_h': N, 'splat': 49}

def upload(b, name, data, chunk=3072):
    for o in range(0, len(data), chunk):
        out, err = b.exec(f"B[{name!r}][{o}:{o + len(data[o:o + chunk])}] = bytes.fromhex({data[o:o + chunk].hex()!r})")
        if err: raise RuntimeError(err)

def main():
    names = sys.argv[1:] or list(REFS)
    b = Board(); b.soft_reset()
    out, err = b.exec(BOARD_SETUP)
    if err: sys.exit(out + err)
    for k in ('u', 'v', 'a', 'b'):
        upload(b, k, b16(IN16[k]))
    upload(b, 'p', b32(P32))
    hdr = f'W, H, R16, R32, HROWS, PIT, SCALE, W4B, OM1B = {W}, {H}, {R16}, {R32}, {HROWS}, {PIT}, {SCALE}, {sbits(W4)}, {sbits(OM1)}\nSPL = {SPL!r}\nPAL = {PAL!r}\nNAMES = {names!r}\n'
    out, err = b.exec(hdr + BOARD_RUN, timeout=300)
    b.close()
    if err: sys.exit(out + err)
    R = ast.literal_eval(out.split('RESULT ', 1)[1].strip())
    for k in names:
        c, bc = R[k]
        kb = k.split('@')[0]
        ok = bc == zlib.crc32(REFS[kb](IN16))
        per = c / WORK[kb]
        print(f"{k:13s} {c:9d} cyc {per:7.2f}/{'cell-iter' if k == 'pressure_rb' else 'px' if k == 'bilin_h' else 'cell'}  {c / 246e3:7.3f} ms  exact={ok}")
        open('fluid16_results.jsonl', 'a').write(json.dumps(dict(t=time.strftime('%F %T'), kernel=k, cycles=c, per=round(per, 2), exact=ok)) + '\n')

if __name__ == '__main__':
    main()
