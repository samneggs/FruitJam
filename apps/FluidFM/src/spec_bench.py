#!/usr/bin/env python3
"""Check + timing of spec (fluid16_k.mpy) against a numpy model: FFT output, bar heights, drawn bars.
usage: spec_bench.py"""
import math, struct, ast
import numpy as np
from rp import Board

NB, HS, TOP, RANGE, FALL = 64, 48, 74.0, 20.0, 24
FS, NFFT = 32000, 1024
SIN = [int(32767 * math.sin(2 * math.pi * i / 1024)) for i in range(1024)]

def bins(nb=NB, f0=60.0, f1=8000.0):
    e = [f0 * (f1 / f0) ** (b / nb) * NFFT / FS for b in range(nb + 1)]
    out = [int(e[0])]
    for x in e[1:]: out.append(max(int(x), out[-1] + 1))                        # strictly increasing
    return out

def consts():
    k = 16 * HS / RANGE
    return k * 2.0 ** -23, -k * (127 + TOP - RANGE), 16.0 * HS

rng = np.random.default_rng(1)
t = np.arange(NFFT)
L = (6000 * np.sin(2 * np.pi * 220 * t / FS) + 3000 * np.sin(2 * np.pi * 1500 * t / FS) + rng.integers(-200, 200, NFFT)).astype(int)
R = (5000 * np.sin(2 * np.pi * 330 * t / FS + 1) + rng.integers(-200, 200, NFFT)).astype(int)
WR = 300
RING = [0] * 1024
for i in range(NFFT):                     # sample i (oldest first) sits at ring[(WR + i) & 1023]
    RING[(WR + i) & 1023] = (int(L[i]) & 0xffff) | (int(R[i]) & 0xffff) << 16

BOARD = r'''
import array, uctypes, machine, struct
from machine import mem32
import sys
if '/FluidFM' not in sys.path: sys.path.insert(0, '/FluidFM')   # the demo's modules live in /FluidFM
from fluid16_k import spec, bilin_h
A = uctypes.addressof
sin = array.array('h', SIN); ring = array.array('I', RING); st = array.array('i', [0, 0, WR, 0])
buf = bytearray(8192); band = array.array('i', [0, 8, 0, 40, 64, 0] + [0] * 11)
bars = array.array('i', [0] * NB); bn = array.array('i', BINS); cols = bytearray(range(1, HS + 1))
ctl = bytearray(struct.pack('<9I3fI2If', A(ring), A(st), A(sin), A(buf), A(band), A(bars), A(bn), NB,
                            A(cols), CA, CB, CH, FALL, 0, 0, 1 / 32767))
c = []
for _ in range(2):
    s = machine.disable_irq(); t0 = mem32[0xE0001004]
    spec(ctl)
    c.append((mem32[0xE0001004] - t0) & 0xffffffff); machine.enable_irq(s)
    if not c[1:]: b1 = list(bars); X = list(struct.unpack('<2048f', buf))
fb2 = bytearray(320 * 120); ix = bytearray(80 * 160 + 164); pal = bytes(256)   # black half-frame + bars
lb = bytearray(640); bc = array.array('i', [HS // 2, A(bars), A(cols), 60, A(lb), 0])
s = machine.disable_irq(); t0 = mem32[0xE0001004]; bilin_h(fb2, ix, pal, bc); c.append((mem32[0xE0001004] - t0) & 0xffffffff); machine.enable_irq(s)
bc[0] = 0
s = machine.disable_irq(); t0 = mem32[0xE0001004]; bilin_h(fb2, ix, pal, bc); c.append((mem32[0xE0001004] - t0) & 0xffffffff); machine.enable_irq(s)
fb2 = bytearray(320 * 120); bc[0] = HS // 2; bilin_h(fb2, ix, pal, bc)
cnt = []; colok = True
for b in range(NB):
    n = 0
    for y in range(120):
        px = fb2[y * 320 + 5 * b: y * 320 + 5 * b + 5]
        if px[0]:
            n += 1; colok &= px[0] == cols[119 - y] and px[1] == px[2] == px[3] == px[0] and not px[4]
    cnt.append(n)
print('RESULT', (c, b1, list(bars), cnt, colok, list(band[:6]), X))
'''

def main():
    ca, cb, ch = consts(); bn = bins()
    b = Board(); b.soft_reset()
    out, err = b.exec(f'SIN, RING, WR, NB, HS, FALL = {SIN!r}, {RING!r}, {WR}, {NB}, {HS}, {FALL}\n'
                      f'BINS, CA, CB, CH = {bn!r}, {ca!r}, {cb!r}, {ch!r}\n' + BOARD, timeout=60)
    b.close()
    if err: raise SystemExit(out + err)
    cyc, b1, b2, cnt, colok, band, X = ast.literal_eval(out.split('RESULT ', 1)[1].strip())
    # model: same integer window and mono as the kernel, FFT in float64
    w = np.array([(SIN[i >> 1] ** 2) >> 15 for i in range(NFFT)])
    x = ((L + R) * w).astype(np.float64)
    F = np.fft.fft(x)
    got = np.array(X[0::2]) + 1j * np.array(X[1::2])
    rel = np.max(np.abs(got - F)) / np.max(np.abs(F))
    p = np.abs(got) ** 2
    pb = np.array([p[bn[k]:bn[k + 1]].max() for k in range(NB)], dtype=np.float32)
    bits = pb.view(np.uint32).astype(np.float64)
    want = np.clip(ca * bits + cb, 0, ch).astype(int)
    dif = np.abs(np.array(b1) - want).max()
    decay = all(b2[k] == max(b1[k] - FALL, b1[k]) for k in range(NB))     # same input twice: no fall
    print(f'spec cycles {cyc[:2]}  ({cyc[1] / 246:.0f} us at 246 MHz)   bilin_h half with bars {cyc[2]}, without {cyc[3]}')
    print(f'FFT max rel err {rel:.2e}   bars max diff {dif} (Q4)   steady {decay}   drawn rows ok {cnt == [h >> 4 for h in b2]} colours ok {colok}')
    print('band sums ok', band[2] == sum(b2[0:8]) and band[5] == sum(b2[40:64]), band)
    print('bars (rows):', [h >> 4 for h in b1])
    pk = sorted(range(NFFT // 2), key=lambda k: -p[k])[:4]
    print('peak bins', pk, 'Hz', [k * FS / NFFT for k in pk])

if __name__ == '__main__':
    main()
