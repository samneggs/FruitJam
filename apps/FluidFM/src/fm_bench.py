#!/usr/bin/env python3
"""Bit-exact check + timing of fm_render / fm_pack (fluid16_k.mpy) against a Python model.
usage: fm_bench.py"""
import math, struct, ast
from rp import Board

N_SIN = 1024
SIN = [int(round(32767 * math.sin(2 * math.pi * i / N_SIN))) for i in range(N_SIN)]
M32 = 0xffffffff
def s32(x): x &= M32; return x - (1 << 32) if x >> 31 else x
def s16(x): x &= 0xffff; return x - 0x10000 if x >> 15 else x
def sat16(x): return max(-32768, min(32767, x))

# cph, cinc, mph, minc, idx, tidx, a, t, pan
VOICES = [[0, 0x01234567, 0x10000000, 0x02468ACE, 0, 150000, 0, 12000, 256 | 64 << 16],
          [0x80000000, 0x00ABCDEF, 0, 0x00ABCDEF, 90000, 20000, 9000, 0, 128 | 128 << 16],
          [0x7FFFFFFF, 0x0F000000, 0x40000000, 0x1E000000, 300000, 300000, 30000, 30000, 0 | 256 << 16]]
CALLS = (200, 37)

def model(voices, n, acc):
    for v in voices:
        cph, cinc, mph, minc, idx, tidx, a, t, pan = v
        pl, pr = s16(pan), s16(pan >> 16)
        for i in range(n):
            mph = (mph + minc) & M32
            m = SIN[mph >> 22]
            idx = s32(idx + (s32(tidx - idx) >> 9))
            cph = (cph + cinc) & M32
            s = SIN[((m * idx + cph) & M32) >> 22]
            a = s32(a + (s32(t - a) >> 9))
            x = s32(s * a) >> 15
            acc[2 * i] = s32(acc[2 * i] + s16(x) * pl)
            acc[2 * i + 1] = s32(acc[2 * i + 1] + s16(x) * pr)
        v[:] = [cph, cinc, mph, minc, idx, tidx, a, t, pan]

def pack(acc, n):
    out = [(sat16(acc[2 * i] >> 8) & 0xffff) | (sat16(acc[2 * i + 1] >> 8) & 0xffff) << 16 for i in range(n)]
    acc[:] = [0] * len(acc)
    return out

BOARD = r'''
import array, uctypes, machine, struct
from machine import mem32
import sys
if '/FluidFM' not in sys.path: sys.path.insert(0, '/FluidFM')   # the demo's modules live in /FluidFM
from fluid16_k import fm_render, fm_pack
sin = array.array('h', SIN)
st = array.array('i', [uctypes.addressof(sin), len(VOICES)] + [x for v in VOICES for x in (v + [0])])
acc = array.array('i', [0] * 512)
out = array.array('I', [0] * 256)
OUT = []
for n in CALLS:
    st_ = machine.disable_irq(); t0 = mem32[0xE0001004]
    fm_render(acc, n, st)
    t1 = mem32[0xE0001004]
    fm_pack(out, acc, n)
    t2 = mem32[0xE0001004]; machine.enable_irq(st_)
    OUT.append((list(out[:n]), (t1 - t0) & 0xffffffff, (t2 - t1) & 0xffffffff))
print('RESULT', OUT, [st[i] & 0xffffffff for i in range(2, len(st))])
'''

def main():
    b = Board(); b.soft_reset()
    voices = [[s32(x) for x in v] for v in VOICES]
    out, err = b.exec(f'SIN = {SIN!r}\nVOICES = {voices!r}\nCALLS = {CALLS!r}\n' + BOARD)
    b.close()
    if err: raise SystemExit(out + err)
    res, stw = ast.literal_eval(out.split('RESULT ', 1)[1].strip().replace('] [', '], ['))
    mv = [v[:] for v in VOICES]; acc = [0] * 512
    for (got, c_r, c_p), n in zip(res, CALLS):
        model(mv, n, acc); want = pack(acc, n)
        bad = [i for i in range(n) if got[i] != want[i]]
        print(f'n={n:3d}  render {c_r / n / len(VOICES):5.1f} cyc/sample/voice  pack {c_p / n:4.1f} cyc/sample  '
              f'exact={not bad}' + (f'  first bad {bad[0]}: {got[bad[0]]:08x} vs {want[bad[0]]:08x}' if bad else ''))
    flat = [x & M32 for v in mv for x in (v + [0])]
    print('state exact', flat == stw)

if __name__ == '__main__':
    main()
