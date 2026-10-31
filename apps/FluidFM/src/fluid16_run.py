#!/usr/bin/env python3
"""Fluid demo at 160x120: float16 u/v/dye/div fields, float32 pressure with warm start,
kernels from fluid16_k.mpy, asm worker on core1, every stage split by rows.
usage: fluid16_run.py [seconds] [pressure_iters] [block|bilin]"""
import sys, struct
from rp import Board
from fluid16_gen import W, H, R16, R32, HROWS

SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 60
ITERS = int(sys.argv[2]) if len(sys.argv) > 2 else 4
# pressure: red-black SOR, p += w * (gs - p). w = 1 is plain Gauss-Seidel (the old solver, 6 iterations)
OMEGA = float(next((a[8:] for a in sys.argv if a.startswith('--omega=')), 1.7))
def _sbits(x): return struct.unpack('<i', struct.pack('<f', x))[0]
SORB = (_sbits(OMEGA / 4), _sbits(1 - OMEGA))     # pressure_rb P[9], P[10]: w/4, 1-w (float bits)
RENDER = sys.argv[3] if len(sys.argv) > 3 else 'bilin'

# right after the soft reset: kernels first (their code lands in SRAM), then fb, display, fields
STAGE1 = r'''
import sys
if '/FluidFM' not in sys.path: sys.path.insert(0, '/FluidFM')   # the demo's modules live in /FluidFM
import fluid16_k as fluid_k
import machine, gc, uctypes
from machine import mem32
N = W * H
machine.freq(246_000_000)
mem32[0x40010058] = 2 << 16             # HSTX clk / 2 (as the template)
mem32[0x40010054] = 1 << 11
gc.collect()
FB = bytearray(320 * 240)
from dvi_rp2_hstx_audio import DVI_RP2_HSTX
import colors as rv_colors
display = DVI_RP2_HSTX()                 # control blocks need contiguous SRAM: begin() before the fields
display.begin(FB, rv_colors.COLOR_MODE_BGR233, height=240, width=320, bytes_per_pixel=1, audio_rate=0)
gc.collect()
PB = bytearray(4 * N)                    # float32 pressure, kept between frames (warm start)
BUF = [bytearray(2 * N) for _ in range(5)]   # float16 u, v, dye, and the two scratch fields
IX = bytearray(N + W + 4)                # palette index per cell + zero pad row for bilinear reads
CT = bytearray(16)                       # row counters (adv uv, adv dye, div, proj): ldrex/strex need SRAM
for b in BUF + [FB, PB, IX, CT]:
    assert uctypes.addressof(b) >= 0x20000000, 'buffer landed in PSRAM'
code = mem32[id(fluid_k.advect_uv) + 8] & 0xffffffff
assert code >= 0x20000000, 'kernel code in PSRAM'
print('code', hex(code), 'buffers', [hex(uctypes.addressof(b)) for b in BUF])
'''

APP = r'''
import struct, math, time, array, _thread
from fluid16_k import (advect_uv, advect_r, divergence_h, pressure_rb, project_h, render_h,
                       index_h, bilin_h, splat, worker, sev)
try:
    from gamepadfast import Gamepad
    pad = Gamepad()
except Exception as e:
    print('no gamepad:', e); pad = None

def rgb332(r, g, b): return (min(255, r) >> 5) << 5 | (min(255, g) >> 5) << 2 | (min(255, b) >> 6)
pal = bytearray(256)                     # smoke: black -> deep blue -> magenta -> orange -> yellow
for i in range(256):
    t = i / 255
    pal[i] = rgb332(int(255 * min(1, max(0, 2.2 * t - 0.4))), int(255 * max(0, 1.6 * t - 0.6)),
                    int(255 * min(1, 3 * t) * (1 - 0.7 * t)))
SCALE = struct.unpack('<I', struct.pack('<f', 48.0))[0]
A = uctypes.addressof
PAL = A(pal)
U, V, DYE, F1, F2 = [A(b) for b in BUF]
P, FBA, IXA = A(PB), A(FB), A(IX)
OFF, OFF32 = HROWS * R16, HROWS * R32    # byte shift of the bottom half: f16 / f32 fields
MIDR = 1 + HROWS

def fa(fn): return (mem32[id(fn) + 8] & 0xffffffff) | 1
BC = array.array('i', [0, 0, 0, 60])        # bilin_h ctl: no spectrum strip, 60 grid rows
F_UV, F_R, F_DIV, F_RB, F_PROJ, F_REN, F_IDX, F_BIL = [fa(f) for f in
    (advect_uv, advect_r, divergence_h, pressure_rb, project_h, render_h, index_h, bilin_h)]

# core1 job slot: seq, fn, a0, a1, a2, done, a3
JOB = array.array('i', [0] * 7)
def go1(fn, a0=0, a1=0, a2=0, a3=0):
    JOB[1] = fn; JOB[2] = a0; JOB[3] = a1; JOB[4] = a2; JOB[6] = a3
    JOB[0] += 1
    sev()
def wait1():
    n = 0
    while JOB[5] != JOB[0]:
        n += 1
        if n > 2000000: raise RuntimeError('core1 worker timeout')

FL = array.array('i', [0, 0]); fl = A(FL)
PR0 = array.array('i', [1, MIDR, ITERS, fl, fl + 4, 0, 0, 0, 0, SORB[0], SORB[1]])
PR1 = array.array('i', [MIDR, H - 1, ITERS, fl + 4, fl, 0, 0, 0, 0, SORB[0], SORB[1]])
CTR = A(CT)                              # both cores claim advect rows from these counters
PUV = [bytearray(44), bytearray(44)]
PDR = [bytearray(struct.pack('<II5f3i', 0, 0, 0.994, 0.5, W - 1.5, H - 1.5, 1.0, CTR + 4, H - 1, 0)) for _ in range(2)]
SP = bytearray(56)

def jet(cx, cy, fx, fy, amt, r=4):
    ix, iy = int(cx), int(cy)
    struct.pack_into('<3I4i7f', SP, 0, U, V, DYE, max(1, ix - r), min(W - 1, ix + r + 1),
                     max(1, iy - r), min(H - 1, iy + r + 1), cx, cy, fx, fy, amt, 1 / (r * r), 1.0)
    splat(SP)

T = [0] * 8; NAMES = ('splat', 'adv uv', 'adv dye', 'div', 'press', 'proj', 'render', 'frame')
frames = 0; ang = 0.0; cur = [W / 2, H / 2]; px = py = 0
_thread.start_new_thread(worker, (JOB,))
time.sleep_ms(5)
t_end = time.ticks_add(time.ticks_ms(), SECONDS * 1000); t_rep = time.ticks_ms()
us = time.ticks_us
try:
    while time.ticks_diff(t_end, time.ticks_ms()) > 0:
        t0 = us()
        ang += 0.015
        for k in (0, 1):
            a = ang + k * math.pi
            jet(W / 2 + 35 * math.cos(a), H / 2 + 27 * math.sin(a), -0.9 * math.sin(a + 0.6), 0.9 * math.cos(a + 0.6), 0.8)
        if pad and frames % 4 == 0:
            pad.read()
            if not pad.buttons & 1: break
            px, py = pad.x / 512, -pad.y / 512
        if px or py:
            cur[0] = min(W - 5, max(4, cur[0] + 1.5 * px)); cur[1] = min(H - 5, max(4, cur[1] + 1.5 * py))
            jet(cur[0], cur[1], 1.5 * px, 1.5 * py, 1.2)
        for k in range(2):
            struct.pack_into('<IiIi2i5f', PUV[k], 0, U, V - U, F1, F2 - F1, CTR, H - 1, 0.999, 0.5, W - 1.5, H - 1.5, 1.0)
            struct.pack_into('<II', PDR[k], 0, U, V)
        mem32[CTR] = mem32[CTR + 4] = mem32[CTR + 8] = mem32[CTR + 12] = 1   # every shared-row kernel starts at row 1
        t1 = us()
        go1(F_UV, A(PUV[1])); advect_uv(PUV[0]); wait1(); t2 = us()
        go1(F_R, U, DYE, A(PDR[1])); advect_r(U, DYE, PDR[0]); wait1(); t3 = us()   # dst aliases u: safe
        go1(F_DIV, V, F1, F2, CTR + 8); divergence_h(V, F1, F2, CTR + 8); wait1(); t4 = us()
        go1(F_RB, P, V, A(PR1)); pressure_rb(P, V, PR0); wait1()
        if PR0[7] == -1: raise RuntimeError('pressure barrier timeout')
        t5 = us()
        go1(F_PROJ, F1, F2, P, CTR + 12); project_h(F1, F2, P, CTR + 12); wait1(); t6 = us()
        if RENDER == 'bilin':             # index both halves first: the bilinear rows read one row ahead
            go1(F_IDX, IXA + (H // 2) * W, U + (H // 2) * R16, SCALE); index_h(IX, U, SCALE); wait1()
            go1(F_BIL, FBA + 120 * 320, IXA + (H // 2) * W, PAL, A(BC)); bilin_h(FB, IX, pal, BC); wait1()
        else:
            go1(F_REN, FBA + 120 * 320, U + (H // 2) * R16, PAL, SCALE); render_h(FB, U, pal, SCALE); wait1()
        t7 = us()
        U, V, DYE, F1, F2 = F1, F2, U, V, DYE
        for k, (a, b) in enumerate(((t0, t1), (t1, t2), (t2, t3), (t3, t4), (t4, t5), (t5, t6), (t6, t7), (t0, t7))):
            T[k] += time.ticks_diff(b, a)
        frames += 1
        if time.ticks_diff(time.ticks_ms(), t_rep) >= 5000:
            el = time.ticks_diff(time.ticks_ms(), t_rep); t_rep = time.ticks_ms()
            print('fps %.1f  ' % (frames * 1000 / el) + '  '.join('%s %.2f' % (n, T[k] / frames / 1000) for k, n in enumerate(NAMES)) + ' ms')
            T = [0] * 8; frames = 0
finally:
    JOB[1] = 0; JOB[0] += 1; sev()        # worker returns
print('done', ITERS, RENDER)
'''

def main():
    b = Board(); b.soft_reset()
    out, err = b.exec(f'W, H = {W}, {H}\n' + STAGE1)
    if err: sys.exit(out + err)
    print(out.strip())
    out, err = b.exec(f'SECONDS, ITERS, SORB, R16, R32, HROWS, RENDER = {SECONDS}, {ITERS}, {SORB}, {R16}, {R32}, {HROWS}, {RENDER!r}\n' + APP, timeout=SECONDS + 60)
    b.close()
    print(out, err)

if __name__ == '__main__':
    main()
