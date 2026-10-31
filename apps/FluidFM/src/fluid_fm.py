#!/usr/bin/env python3
"""Fluid demo at 160x120 (as fluid16_run.py) with HDMI audio: a 2-operator FM synth driven by the fluid.
Four drone voices, one per screen quadrant: dye in the quadrant -> loudness, kinetic energy -> FM index
(brightness). The chord changes every half orbit of the jets. A fifth lead voice follows the gamepad jet.
The synth runs in the HDMI audio hard IRQ on core1 (fm_render/fm_pack in fluid16_k.mpy).
usage: fluid_fm.py [seconds] [pressure_iters] [block|bilin] [row_shift] [timbre progression] [--omega=1.7]
       fluid_fm.py --install   (standalone files in /FluidFM on the board: run /FluidFM/fluidfm.py)
       fluid_fm.py --build DIR (the same files written to DIR, for copying by hand)
Sound presets and mappings: SOUND SETTINGS in APP below."""
import sys, struct
from rp import Board
from fluid16_gen import W, H, R16, R32, HROWS

ARGV = [a for i, a in enumerate(sys.argv) if not a.startswith('--') and sys.argv[i - 1] != '--build']
SECONDS = int(ARGV[1]) if len(ARGV) > 1 else 60
ITERS = int(ARGV[2]) if len(ARGV) > 2 else 4
# pressure: red-black SOR, p += w * (gs - p). w = 1 is plain Gauss-Seidel (the old solver, 6 iterations)
OMEGA = float(next((a[8:] for a in sys.argv if a.startswith('--omega=')), 1.7))
def _sbits(x): return struct.unpack('<i', struct.pack('<f', x))[0]
SORB = (_sbits(OMEGA / 4), _sbits(1 - OMEGA))     # pressure_rb P[9], P[10]: w/4, 1-w (float bits)
RENDER = ARGV[3] if len(ARGV) > 3 else 'bilin'
SHIFT = int(ARGV[4]) if len(ARGV) > 4 else 0   # rows moved from core1 (audio IRQ) to core0
START = (int(ARGV[5]), int(ARGV[6])) if len(ARGV) > 6 else None   # starting timbre, progression

# right after the soft reset: kernels first (their code lands in SRAM), then fb, display, fields
AUDIO_RATE = 32000
SLOTS_B, CB_B = 19152, 24592            # the driver's two big bytearrays with audio (asserted below)
SYN_B = 2048 + 2048 + 1024 + 512 + 1024 + 1792 + 16  # synth (voice state for up to 12 voices), spectrum blocks, background tables, row counters

# right after the soft reset: kernels first (their code lands in SRAM), then one arena for every big
# buffer, then the drivers. Imported first, the drivers fragment SRAM and the fifth field won't fit.
STAGE1 = r'''
import sys
if '/FluidFM' not in sys.path: sys.path.insert(0, '/FluidFM')   # the demo's modules live in /FluidFM
import fluid16_k as fluid_k
import machine, gc, uctypes
from machine import mem32, mem16
gc.collect()
N = W * H
machine.freq(246_000_000)
mem32[0x40010058] = 2 << 16             # HSTX clk / 2 (as the template)
mem32[0x40010054] = 1 << 11
import hdmi_audio as _ha                  # its tables in SRAM (in PSRAM the IRQ takes ~3x longer)
gc.collect()
CBB = bytearray(CB_B)                    # first fit: the ~37 KB hole hdmi_audio leaves low in SRAM
SYNB = bytearray(SYN_B)                  # ... and the rest of that hole
ARENA = bytearray(320 * 240 + 4 * N + 5 * 2 * N + SLOTS_B)   # the ~365 KB block above it
_a = uctypes.addressof(ARENA)
assert min(_a, uctypes.addressof(CBB), uctypes.addressof(SYNB)) >= 0x20000000, 'arena landed in PSRAM'
def _cut(n):
    global _a
    b = uctypes.bytearray_at(_a, n); _a += n
    return b
FB = _cut(320 * 240)
POOL = {SLOTS_B: _cut(SLOTS_B), CB_B: CBB}
PB = _cut(4 * N)                         # float32 pressure, kept between frames (warm start)
BUF = [_cut(2 * N) for _ in range(5)]    # float16 u, v, dye, and the two scratch fields
SYNA = uctypes.addressof(SYNB)            # synth: sine table, accumulators, PCM, voice state (PSRAM: IRQ ~2x slower)
import dvi_rp2_hstx_audio as drv
from dvi_rp2_hstx_audio import DVI_RP2_HSTX
import colors as rv_colors
drv.bytearray = lambda n: POOL.pop(n) if n in POOL else bytearray(n)   # driver's slots + control blocks
display = DVI_RP2_HSTX()
display.begin(FB, rv_colors.COLOR_MODE_BGR233, height=240, width=320, bytes_per_pixel=1, audio_rate=AUDIO_RATE)
del drv.bytearray
assert not POOL, 'driver did not take the arena slices: %r' % list(POOL)
code = mem32[id(fluid_k.advect_uv) + 8] & 0xffffffff
assert code >= 0x20000000, 'kernel code in PSRAM'
print('code', hex(code), 'arena', hex(uctypes.addressof(ARENA)), 'cb', hex(uctypes.addressof(CBB)), 'syn', hex(SYNA), 'audio code', hex(mem32[id(_ha._encode_jobs) + 8] & 0xffffffff), 'acr', display.audio_stats()[3])
print('line cmds', [hex(uctypes.addressof(getattr(display, n))) for n in ('_hstx_line_porch', '_hstx_line_vsync', '_hstx_line_active', '_cb_restart_frame_nested')])
'''

APP = r'''
import struct, math, time, array, _thread
from fluid16_k import (advect_uv, advect_r, divergence_h, pressure_rb, project_h, render_h,
                       index_h, bilin_h, splat, worker, sev, fm_render, fm_pack, fm_map, stats_q, zfill, zcols, spec, bg_h)
try:
    from gamepadfast import Gamepad
except ImportError:
    Gamepad = None
def newpad():                                # gamepad is optional and may be plugged in later
    try:
        return Gamepad() if Gamepad else None
    except Exception:
        return None
pad = newpad()
print('gamepad', 'found' if pad else 'not found')

def rgb332(r, g, b): return (min(255, r) >> 5) << 5 | (min(255, g) >> 5) << 2 | (min(255, b) >> 6)
def hsv(h, s, v):                        # -> r, g, b 0..255
    h = (h % 1.0) * 6; i = int(h); f = h - i; v *= 255
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    return [(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)][i % 6]
def stops(t, st):                        # linear gradient through (t, (r, g, b)) stops
    for k in range(1, len(st)):
        if t <= st[k][0]:
            (t0, c0), (t1, c1) = st[k - 1], st[k]; u = (t - t0) / (t1 - t0)
            return [c0[j] + (c1[j] - c0[j]) * u for j in range(3)]
    return st[-1][1]
def palcolor(name, t, rot):              # smoke colour for dye level t (0..1), hue rotation rot (0..1)
    if name == 'ember': return 255 * min(1, max(0, 2.2 * t - 0.4)), 255 * max(0, 1.6 * t - 0.6), 255 * min(1, 3 * t) * (1 - 0.7 * t)
    if name == 'rainbow': return hsv(0.8 - 0.85 * t + rot, 1 - max(0, t - 0.85) * 3, min(1, 3 * t))
    if name == 'neon bands': return hsv(2.5 * t + rot, 1.0, min(1, 4 * t))
    if name == 'fire': return stops(t, ((0, (0, 0, 0)), (0.25, (170, 0, 0)), (0.5, (255, 90, 0)), (0.75, (255, 210, 0)), (1, (255, 255, 230))))
    if name == 'ocean': return stops(t, ((0, (0, 0, 0)), (0.3, (0, 40, 170)), (0.6, (0, 170, 230)), (1, (230, 255, 255))))
    return stops(t, ((0, (0, 0, 0)), (0.25, (0, 150, 60)), (0.5, (0, 200, 200)), (0.75, (150, 60, 255)), (1, (255, 150, 230))))
SCALE = struct.unpack('<I', struct.pack('<f', 48.0))[0]
A = uctypes.addressof
U, V, DYE, F1, F2 = [A(b) for b in BUF]
P, FBA = A(PB), A(FB)
OFF, OFF32 = HROWS * R16, HROWS * R32    # byte shift of the bottom half: f16 / f32 fields
MIDR = 1 + HROWS
SPL = MIDR + SHIFT                       # pressure: core0 rows 1..SPL-1, core1 SPL..H-2
CTR = SYNA + 8448                        # row counters (adv uv, adv dye, div, proj): both cores claim rows with ldrex/strex, SRAM only

def fa(fn): return (mem32[id(fn) + 8] & 0xffffffff) | 1
F_UV, F_R, F_DIV, F_RB, F_PROJ, F_REN, F_IDX, F_BIL, F_SPEC, F_BG = [fa(f) for f in
    (advect_uv, advect_r, divergence_h, pressure_rb, project_h, render_h, index_h, bilin_h, spec, bg_h)]

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
PR0 = array.array('i', [1, SPL, ITERS, fl, fl + 4, 0, 0, 0, 0, SORB[0], SORB[1]])
PR1 = array.array('i', [SPL, H - 1, ITERS, fl + 4, fl, 0, 0, 0, 0, SORB[0], SORB[1]])
PUV = [bytearray(struct.pack('<4I2i5f', 0, 0, 0, 0, CTR, H - 1, 0.999, 0.5, W - 1.5, H - 1.5, 1.0)) for _ in range(2)]
PDR = [bytearray(struct.pack('<II5f3i', 0, 0, 0.994, 0.5, W - 1.5, H - 1.5, 1.0, CTR + 4, H - 1, 0)) for _ in range(2)]
SP = bytearray(64)
struct.pack_into('<2f', SP, 56, 1.0, 1.0)      # cursor jet: no sound gains
PUA, PDA = [A(b) for b in PUV], [A(b) for b in PDR]
ORBIT = 419                              # frames per orbit of the two jets (2 pi / 419 ~ 0.015 rad per frame)
JTAB = bytearray(2 * 64 * ORBIT)         # splat params for both jets at every step; field pointers set per frame
for s in range(ORBIT):
    for k in (0, 1):
        a = 2 * math.pi * s / ORBIT + k * math.pi
        cx, cy = W / 2 + 35 * math.cos(a), H / 2 + 27 * math.sin(a); ix, iy = int(cx), int(cy)
        struct.pack_into('<3I4i9f', JTAB, (2 * s + k) * 64, 0, 0, 0, max(1, ix - 4), min(W - 1, ix + 5),
                         max(1, iy - 4), min(H - 1, iy + 5), cx, cy, -0.9 * math.sin(a + 0.6), 0.9 * math.cos(a + 0.6), 0.8, 1 / 16, 1.0, 1.0, 1.0)
JT = A(JTAB)

FLD = array.array('i', [0, 0, 0])            # current u, v, dye (the loop keeps it up to date)
def jet(cx, cy, fx, fy, amt, r=4):
    ix, iy = int(cx), int(cy)
    struct.pack_into('<3I4i7f', SP, 0, FLD[0], FLD[1], FLD[2], max(1, ix - r), min(W - 1, ix + r + 1),
                     max(1, iy - r), min(H - 1, iy + r + 1), cx, cy, fx, fy, amt, 1 / (r * r), 1.0)
    splat(SP)

T = [0] * 11; NAMES = ('splat', 'adv uv', 'adv dye', 'div', 'press', 'proj', 'render', 'frame', 'pad', 'synth', 'spec wait')
IXA = F2; frames = 0; stot = 0; cur = [W / 2, H / 2]; px = py = 0
# ---- FM synth: state for fm_render (see fluid16_gen.py), refilled from the hard IRQ on core1
NV = 5
SINT, ACC, PCM, VS = SYNA, SYNA + 2048, SYNA + 4096, SYNA + 5120    # all in the SRAM arena (zeroed)
for i in range(1024): mem16[SINT + 2 * i] = int(32767 * math.sin(2 * math.pi * i / 1024)) & 0xffff
mem32[VS] = SINT; mem32[VS + 4] = NV
AST = array.array('i', [0, 0, 0, 0, 0])     # IRQs, max IRQ us, core0 DMA_IRQ_0 enabled at start, total us, mute
INC = 2 ** 32 / AUDIO_RATE

# ================================ SOUND SETTINGS ================================
# Gamepad: START cycles tune + bars / tune / drones + bars / drones / silent. Hold SELECT ~1 s to exit.
# Up/down: smoke colours. Tunes: left/right = previous / next tune. Drones: left/right = timbre.
PLAY_TUNES = True       # start with the MIDI player (all .mid files in TUNE_DIR, in name order, looping)
TUNE_DIR = '/FluidFM/tunes'
TUNE_VOICES = 10        # polyphony for tunes (max 12); instruments: fmidi.INSTR / fmidi.DRUMS
TUNE_GAP = 3.0          # seconds of quiet between tunes
NOTE_PUSH = 1.0         # each note fires a jet up from above the bars: x = pitch, strength = velocity
NOTE_DYE = 1.0
NOTE_JETS = 3           # at most this many note jets per frame
TPANS = tuple((128 + d) | (128 - d) << 16 for d in (0, 40, -40, 80, -80, 60, -60, 100, -100, 20, -20, 0))
# Voices 0-3 are drones, one per screen quadrant (TL, TR, BL, BR); voice 4 is the gamepad lead.
DRONE_VOL = 5200        # one drone at full dye (4 drones + lead must stay below 32767)
LEAD_VOL = 7000         # lead at full stick
DYE_FULL = 1.5          # quadrant mean dye that gives full drone volume
KE_FULL = 1.0           # quadrant mean u^2 + v^2 that gives full brightness
LOW_F, LOW_BOOST = 250.0, 2.5   # drones below LOW_F get up to +LOW_BOOST FM index (small speakers)
CHORD_TURN = 0.5        # the chord changes every CHORD_TURN orbits of the jets (one orbit ~6.6 s)
TIMBRE, PROG = 0, 2     # starting presets
# FM: ratio = modulator / carrier frequency; beta = modulation index (0 = sine, 2-3 = bright, 6 = buzzy)
#            name      ratio L/R drones  beta: still  full KE  lead ratio
TIMBRES = (('warm',    1.0, 2.0,         0.19,  3.07,    2.0),
           ('hollow',  2.0, 2.0,         0.5,   2.5,     2.0),     # 1:2 -> odd harmonics, clarinet-like
           ('bell',    3.5, 1.4,         1.0,   3.0,     3.5),     # inharmonic ratios -> bells
           ('glass',   7.0, 5.0,         0.3,   1.5,     4.0),
           ('growl',   1.0, 1.0,         1.5,   6.0,     1.0))     # high index -> brassy / buzzy
PROGRESSIONS = (('Am9 F C G', ((110.0, 164.81, 196.0, 246.94), (87.31, 130.81, 164.81, 220.0),
                               (130.81, 196.0, 246.94, 329.63), (98.0, 146.83, 196.0, 246.94))),
                ('D dorian',  ((73.42, 110.0, 130.81, 164.81), (98.0, 146.83, 174.61, 220.0),
                               (82.41, 123.47, 146.83, 196.0), (87.31, 130.81, 174.61, 220.0))),
                ('E minor',   ((82.41, 123.47, 196.0, 246.94), (65.41, 98.0, 164.81, 246.94),
                               (73.42, 110.0, 185.0, 220.0), (61.74, 92.5, 146.83, 246.94))),
                ('drone D',   ((73.42, 110.0, 146.83, 220.0),)))
LEAD_SCALE = (220.0, 261.63, 293.66, 329.63, 392.0, 440.0, 523.25, 587.33, 659.25, 783.99)  # A minor pentatonic, low -> high = bottom -> top of screen
SPECTRUM = True        # start with the bar spectrum on (the START button cycles sound + bars / sound / silent)
SPEC_BARS, SPEC_ROWS = 64, 48       # 5 px per bar (64 x 5 = 320)
SPEC_F0, SPEC_F1 = 60.0, 8000.0     # Hz at the left / right edge (log scale)
SPEC_TOP, SPEC_RANGE = 74.0, 20.0   # log2(|X|^2) at full height, and the range shown (1 unit = 3 dB)
SPEC_FALL = 1.5                     # rows a bar may fall per frame
SPEC_HUES = (0.78, 0.0)              # bar colour (hue) at the bottom, at the top: violet ... red
BACKGROUND = True       # plasma nebula behind the smoke (render only; drawn from the low end of the palette)
BG_LEVEL = 24           # brightest background palette index (dye reaches 255)
BG_SPEED = (0.35, -0.225, 0.275)   # drift of the three waves, cells per frame
BG_BASS = 4.0           # drift speeds up by 1 + BG_BASS * bass
PALETTES = ('rainbow', 'neon bands', 'ember', 'fire', 'ocean', 'aurora')   # smoke colours: up/down cycles them
PALETTE = 0             # starting palette
HUE_STEPS = 32          # rainbow / neon bands rotate their hues: steps per turn
NOTE_HUE = 1            # ... by this many steps per note
HUE_DRIFT = 12          # ... and one step every this many frames
# sound -> fluid (needs SPECTRUM): bass pushes the orbiting jets harder, a sudden rise in bass kicks them,
# treble adds dye. Levels are the mean bar height of the band, 0..1.
SOUND_TO_FLUID = True
BASS_BARS, TREBLE_BARS = (0, 20), (40, 64)   # bar ranges: ~60-270 Hz, ~1.3-8 kHz
BASS_FORCE = 1.0        # jet force x (1 + BASS_FORCE * bass)
KICK_FORCE = 6.0        # ... + KICK_FORCE * (bass - its slow average), when positive
KICK_SLOW = 0.03        # how fast the slow average follows (per frame)
TREBLE_DYE = 2.0        # jet dye x (1 + TREBLE_DYE * treble)
PANS = (230 | 70 << 16, 70 | 230 << 16, 200 | 110 << 16, 110 | 200 << 16, 160 | 160 << 16)   # Q8 left | right << 16
# ================================================================================
if START: TIMBRE, PROG = START
BETA = 2 ** 17 / (2 * math.pi)          # fm_render index units per radian of modulation index
def tune(k, f, ratio):
    o = VS + 8 + 40 * k
    mem32[o + 4] = int(f * INC)                 # carrier increment
    mem32[o + 12] = int(f * ratio * INC)        # modulator increment
for k in range(NV): tune(k, 110.0, 1.0); mem32[VS + 8 + 40 * k + 32] = PANS[k]
LV = VS + 8 + 40 * 4                        # lead voice state
SQ = array.array('f', [0.0] * 8)
CTL = bytearray(struct.pack('<II9f', VS + 8, A(SQ), 1 / (300 * KE_FULL), 1 / (300 * DYE_FULL), 0, 0, DRONE_VOL, 0, 0, 0, 0))
CHORD_FR = ORBIT * CHORD_TURN               # frames per chord
# ---- spectrum (spec in fluid16_gen.py): control block, bars, bins and colours in the SRAM arena
SPC = SYNA + 5632; SBARS, SBINS, SCOLS = SPC + 64, SPC + 320, SPC + 640
BC0, BC1 = SPC + 704, SPC + 736            # bilin_h ctl per half: strip row pairs, bars, colours, grid rows,
                                             # line buffer (640 B, set per frame), scratch
for o, sp in ((BC0, 0), (BC1, SPEC_ROWS // 2 if SPECTRUM else 0)):   # bars are drawn as the bottom rows are rendered
    mem32[o] = sp; mem32[o + 4] = SBARS; mem32[o + 8] = SCOLS; mem32[o + 12] = H // 2
SBAND = SPC + 768                          # spec band sums: lo, hi, out x 2
for i, v in enumerate(BASS_BARS + (0,) + TREBLE_BARS + (0,)): mem32[SBAND + 4 * i] = v
_fb = SOUND_TO_FLUID
struct.pack_into('<9f', uctypes.bytearray_at(SBAND + 24, 36), 0, 1 / ((BASS_BARS[1] - BASS_BARS[0]) * 16 * SPEC_ROWS),
                 1 / ((TREBLE_BARS[1] - TREBLE_BARS[0]) * 16 * SPEC_ROWS), 0.0, KICK_SLOW, BASS_FORCE * _fb, KICK_FORCE * _fb,
                 TREBLE_DYE * _fb, 1.0, 1.0)   # spec computes the jet gains (1.0 until its first run)
SBV = uctypes.bytearray_at(SBAND + 52, 16)
BGT = SYNA + 6656; BT1, BT2, BT3 = BGT, BGT + 512, BGT + 1024   # background wave tables (512, 512, 768 bytes)
for i in range(768):
    a = 2 * math.pi * i / 256
    if i < 512:
        machine.mem8[BT1 + i] = int(BG_LEVEL / 3 * (0.5 + 0.5 * math.sin(2 * a)))
        machine.mem8[BT2 + i] = int(BG_LEVEL / 3 * (0.5 + 0.5 * math.sin(3 * a + 1)))
    machine.mem8[BT3 + i] = int(BG_LEVEL / 3 * (0.5 + 0.5 * math.sin(2 * a + 2)))
BGC0, BGC1 = SPC + 848, SPC + 880            # bg_h ctl per half: rows, T1, T2 + y0, T3 + y0, speeds x 3
for o, y0 in ((BGC0, 0), (BGC1, H // 2)):
    mem32[o] = H // 2; mem32[o + 4] = BT1; mem32[o + 8] = BT2 + y0; mem32[o + 12] = BT3 + y0
    for k in range(3): mem32[o + 16 + 4 * k] = int(256 * BG_SPEED[k])
BGB = int(256 * BG_BASS * 256 / ((BASS_BARS[1] - BASS_BARS[0]) * 16 * SPEC_ROWS))   # phase units per bass-sum unit, Q8
bgt = 0
SPOFF = 20480                                # FFT scratch inside the free old-dye field, past the index bytes
ZROWS = max((N + W + 4) // R16, (SPOFF + 8192) // R16 + 1)   # rows whose edge cells need re-zeroing
_e = [SPEC_F0 * (SPEC_F1 / SPEC_F0) ** (b / SPEC_BARS) * 1024 / AUDIO_RATE for b in range(SPEC_BARS + 1)]
_b = [max(1, int(_e[0]))]
for x in _e[1:]: _b.append(max(int(x), _b[-1] + 1))
for i, x in enumerate(_b): mem32[SBINS + 4 * i] = x
for y in range(SPEC_ROWS):
    t = y / (SPEC_ROWS - 1)
    machine.mem8[SCOLS + y] = rgb332(*[int(c) for c in hsv(SPEC_HUES[0] + (SPEC_HUES[1] - SPEC_HUES[0]) * t, 1.0, 1.0)])
PALS = []                                    # (name, palettes, rotations): rotating ones hold HUE_STEPS palettes
for name in PALETTES:
    nr = HUE_STEPS if name in ('rainbow', 'neon bands') else 1
    b = bytearray(256 * nr)
    for r in range(nr):
        for i in range(256): b[256 * r + i] = rgb332(*[int(c) for c in palcolor(name, i / 255, r / nr)])
    PALS.append((name, b, nr))
pal_i = PALETTE % len(PALS); hue = 0
_k = 16 * SPEC_ROWS / SPEC_RANGE
_st = A(_ha._ST)
struct.pack_into('<9I3fI2If', uctypes.bytearray_at(SPC, 64), 0, mem32[_st + 48], _st, SINT, 0, SBAND, SBARS, SBINS,
                 SPEC_BARS, SCOLS, _k * 2.0 ** -23, -_k * (127 + SPEC_TOP - SPEC_RANGE), 16.0 * SPEC_ROWS, int(16 * SPEC_FALL), 0, 0, 1 / 32767)
def retune(ci):
    tim = TIMBRES[TIMBRE]; chord = PROGRESSIONS[PROG][1][ci]
    for q in range(4):
        f = chord[q]; tune(q, f, tim[1 + (q & 1)])
        boost = LOW_BOOST * (1 - f / LOW_F) if f < LOW_F else 0.0   # low drones: harmonics carry the pitch
        struct.pack_into('<f', CTL, 28 + 4 * q, (tim[3] + boost) * BETA)   # fm_map: index when still, per voice
    struct.pack_into('<f', CTL, 20, (tim[4] - tim[3]) * BETA)            # ... plus this much at full KE

SQS = array.array('i', [0, 0, 0, 0, 0, 1] + [0] * 13)   # clock, row, rows, end, rows addr, idle, -, decay shift x 12
@micropython.viper
def seq_step(sq: ptr32, vs: ptr32, n: int):
    # audio IRQ: start / release the notes due in the next n samples, then decay every voice's amplitude target
    if sq[5]: return
    ev = ptr32(sq[4])
    c = sq[0]; i = sq[1]; ne = sq[2]; e = c + n
    while i < ne:
        r = i * 8
        if ev[r] >= e: break
        v = ev[r + 1]; o = 2 + 10 * v
        if ev[r + 5] >= 0:
            vs[o + 1] = ev[r + 2]; vs[o + 3] = ev[r + 3]; vs[o + 5] = ev[r + 4]; vs[o + 7] = ev[r + 5]
        sq[7 + v] = ev[r + 6]
        i += 1
    sq[1] = i
    v = 0; nv = vs[1]
    while v < nv:
        s = sq[7 + v]
        if s:
            o = 9 + 10 * v
            t = vs[o]; vs[o] = t - (t >> s)
        v += 1
    sq[0] = e
    if e >= sq[3]: sq[5] = 1                 # finished: core0 starts the next tune

TUNES = []
if PLAY_TUNES:
    import os, fmidi
    try: names = sorted(f for f in os.listdir(TUNE_DIR) if f.lower().endswith('.mid'))
    except OSError: names = []
    for f in names:
        try:
            rows, n, end = fmidi.load(TUNE_DIR + '/' + f, AUDIO_RATE, TUNE_VOICES)
            TUNES.append((f, rows, n, end)); print('tune', f, n, 'rows', '%.0f s' % (end / AUDIO_RATE))
        except Exception as e:
            print('tune', f, 'skipped:', e)
tune_i = 0; last_note = 0; SOUND = None
def start_tune(k):
    global last_note
    f, rows, n, end = TUNES[k]
    SQS[5] = 1                                   # idle while the voices are set up
    for v in range(TUNE_VOICES):
        o = VS + 8 + 40 * v; mem32[o + 28] = 0; mem32[o + 32] = TPANS[v]; SQS[7 + v] = 0
    mem32[VS + 4] = TUNE_VOICES
    SQS[4] = A(rows); SQS[2] = n; SQS[3] = end + int(TUNE_GAP * AUDIO_RATE); SQS[0] = SQS[1] = 0; last_note = 0
    SQS[5] = 0
    LOG.append('%.1fs tune %s' % (time.ticks_diff(time.ticks_ms(), t_start) / 1000, f))
NOTE_Y = H - 2 - SPEC_ROWS // 2 - 6
def splash(info):
    note = info & 127; kind = (info >> 8) & 255; g = ((info >> 16) & 127) / 127
    y = NOTE_Y if SPECTRUM else H - 8
    if kind == 0:
        x = min(W - 7, max(6, 6 + (note - 28) * (W - 12) / 68))
        jet(x, y, 0.0, -NOTE_PUSH * (0.6 + 1.2 * g), NOTE_DYE * (0.4 + 0.8 * g), 3 if note >= 48 else 5)
    elif kind == 1: jet(W / 2, y, 0.0, -2.5 * NOTE_PUSH * g, 1.5 * NOTE_DYE, 7)
    elif kind == 2:
        jet(W / 2 - 30, y - 8, -1.5 * NOTE_PUSH * g, -0.8, NOTE_DYE, 4); jet(W / 2 + 30, y - 8, 1.5 * NOTE_PUSH * g, -0.8, NOTE_DYE, 4)
    elif kind == 4: jet(W / 2, H / 2, 0.0, -1.5 * NOTE_PUSH, 2.0 * NOTE_DYE, 9)
    elif kind == 5: jet(min(W - 9, max(8, (note - 38) * 12)), y, 0.0, -1.5 * NOTE_PUSH * g, NOTE_DYE, 5)

def isr(_, svc=_ha.service_flags, fl=display._aflag, free=_ha.audio_free, wr=_ha.audio_write_packed,
        rend=fm_render, pk=fm_pack, acc=ACC, pcm=PCM, vs=VS, ast=AST, m32=mem32, us=time.ticks_us,
        td=time.ticks_diff, sq=seq_step, sqs=SQS):
    if m32[0xd0000000]: pass                 # SIO CPUID: core1 owns the audio
    else: return
    t0 = us()
    svc(fl)
    n = 448 - (1023 - free())                  # keep ~14 ms queued
    if n > 0:
        if n > 256: n = 256
        if ast[4]: pass                      # muted: the cleared accumulators pack to silence
        else: sq(sqs, vs, n); rend(acc, n, vs)
        pk(pcm, acc, n); wr(pcm, n)
    t = td(us(), t0)
    if t > ast[1]: ast[1] = t
    ast[3] += t
    ast[0] += 1

def core1(job, disp, handler, run):
    disp.audio_irq(handler)                  # the IRQ belongs to the core that installs it
    run(job)
    disp.audio_irq(None)

QOFF = [((2 + 60 * (q >> 1)) * W + 2 + 80 * (q & 1)) * 2 for q in range(4)]
lead_a = 0.0; pbt = 0x1ff; LOG = []; pci = pli = -1; perr = 0; nl = len(LEAD_SCALE); pfail = 0; sel = 0
def bindpad(p): return p.i2c, p.addr, p.read_gpio, p.read_adc0, p.read_adc1, p.gpio, p.adc0, p.adc1
if pad: I2C, PADDR, RG, RA0, RA1, GB, AB0, AB1 = bindpad(pad)
MODES = (('tune + bars', 'tune', True), ('tune', 'tune', False), ('drones + bars', 'drones', True),
         ('drones', 'drones', False), ('silent', None, False))
if not TUNES: MODES = MODES[2:]
mode = 0 if (SPECTRUM and PLAY_TUNES) or not TUNES else 1; fade = 0
def setmode(m):
    global SPECTRUM, fade, SOUND, pci, pli
    name, snd, bars = MODES[m]
    SPECTRUM = bars
    mem32[BC1] = SPEC_ROWS // 2 if bars else 0          # bilin_h draws the strip only with bars
    if not bars: mem32[SBAND + 52] = mem32[SBAND + 56] = 0x3f800000   # jet gains back to 1.0
    if snd != SOUND:
        SQS[5] = 1                                   # sequencer idle
        for k in range(mem32[VS + 4]): mem32[VS + 8 + 40 * k + 28] = 0   # every voice fades out
        if snd == 'tune': start_tune(tune_i)
        elif snd == 'drones':
            mem32[VS + 4] = NV
            for k in range(NV): mem32[VS + 8 + 40 * k + 32] = PANS[k]
            pci = pli = -1                           # retune the drones and the lead
    SOUND = snd
    if snd: AST[4] = 0; fade = 0
    else: fade = 8                                   # then mute: fm_render stops
TS = [0] * 14; PAIRS = ((0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (0, 9), (10, 11), (8, 9), (12, 13))
_thread.start_new_thread(core1, (JOB, display, isr, worker))
time.sleep_ms(20)
AST[2] = (mem32[0xE000E100] >> 10) & 1    # NVIC_ISER0 bit 10 = DMA_IRQ_0, this core's NVIC
mem32[0xE000E180] = 1 << 10               # NVIC_ICER0: core0 must not run the audio handler too
time.sleep_ms(5)
t_end = time.ticks_add(time.ticks_ms(), SECONDS * 1000); t_rep = t_start = time.ticks_ms()
setmode(mode)
us = time.ticks_us; m32 = mem32; td = time.ticks_diff
IXA = F2; I2C = PADDR = RG = RA0 = RA1 = GB = AB0 = AB1 = None
if pad: I2C, PADDR, RG, RA0, RA1, GB, AB0, AB1 = bindpad(pad)
FLD[0], FLD[1], FLD[2] = U, V, DYE
# the main loop runs in a function: locals (and these defaults) are much faster than module globals
PR1A = A(PR1)
def run(CTR=CTR, PR1A=PR1A, U=U, V=V, DYE=DYE, F1=F1, F2=F2, IXA=IXA, frames=frames, stot=stot, cur=cur, px=px, py=py, pad=pad, I2C=I2C, PADDR=PADDR, RG=RG, RA0=RA0, RA1=RA1, GB=GB, AB0=AB0, AB1=AB1, sel=sel, pbt=pbt, pal_i=pal_i, pfail=pfail, perr=perr, hue=hue, bgt=bgt, lead_a=lead_a, T=T, t_rep=t_rep, us=us, m32=m32, td=td, TS=TS, JT=JT, ORBIT=ORBIT, SBAND=SBAND, splat=splat, MODES=MODES, PALS=PALS, TUNES=TUNES, PUA=PUA, PDA=PDA, wait1=wait1, zfill=zfill, R16=R16, ZROWS=ZROWS, go1=go1, F_UV=F_UV, F_R=F_R, F_DIV=F_DIV, F_RB=F_RB, F_PROJ=F_PROJ, F_IDX=F_IDX, F_BG=F_BG, F_BIL=F_BIL, F_REN=F_REN, F_SPEC=F_SPEC, advect_uv=advect_uv, advect_r=advect_r, divergence_h=divergence_h, pressure_rb=pressure_rb, project_h=project_h, index_h=index_h, bg_h=bg_h, bilin_h=bilin_h, render_h=render_h, OFF=OFF, OFF32=OFF32, P=P, PR0=PR0, PR1=PR1, A=A, N=N, SCALE=SCALE, BGB=BGB, BGC0=BGC0, BGC1=BGC1, BC0=BC0, BC1=BC1, SPOFF=SPOFF, FBA=FBA, FB=FB, SPC=SPC, QOFF=QOFF, SQ=SQ, AST=AST, SQS=SQS, CTL=CTL, PAIRS=PAIRS, W=W, H=H, RENDER=RENDER, BACKGROUND=BACKGROUND, NOTE_JETS=NOTE_JETS, NOTE_HUE=NOTE_HUE, HUE_DRIFT=HUE_DRIFT, CHORD_FR=CHORD_FR, splash=splash, jet=jet, stats_q=stats_q, fm_map=fm_map, FLD=FLD, setmode=setmode, start_tune=start_tune, newpad=newpad, bindpad=bindpad, LOG=LOG):
    global fade, SOUND, SPECTRUM, pci, pli, last_note, tune_i, TIMBRE, PROG, mode
    while not SECONDS or time.ticks_diff(t_end, time.ticks_ms()) > 0:
        TS[0] = us()
        o = JT + 128 * (stot % ORBIT); stot += 1          # the two orbiting jets
        g, d = m32[SBAND + 52], m32[SBAND + 56]           # sound gains from spec (float bits)
        m32[o] = U; m32[o + 4] = V; m32[o + 8] = DYE; m32[o + 56] = g; m32[o + 60] = d; splat(o)
        m32[o + 64] = U; m32[o + 68] = V; m32[o + 72] = DYE; m32[o + 120] = g; m32[o + 124] = d; splat(o + 64)
        TS[10] = us()
        if pad:                                   # one I2C step per frame, never waits on the ADC
            ph = frames & 3
            try:
                if ph == 0:
                    I2C.writeto(PADDR, RG); I2C.readfrom_into(PADDR, GB)
                    I2C.writeto(PADDR, RA0)               # x conversion runs until the next frame
                    bt = GB[3] | (GB[1] & 1) << 8         # START is gpio 16
                    sel = sel + 1 if not bt & 1 else 0    # SELECT held ~1 s: exit
                    if sel >= 15: break
                    hit = pbt & ~bt; pbt = bt                # active low: bits that just went down
                    if hit & 0x100:
                        mode = (mode + 1) % len(MODES); setmode(mode)
                        LOG.append('%.1fs mode %s' % (td(time.ticks_ms(), t_start) / 1000, MODES[mode][0]))
                    if hit & 0x42:                            # up / down: smoke colours
                        pal_i = (pal_i + (1 if hit & 0x40 else -1)) % len(PALS)
                        LOG.append('%.1fs colours %s' % (td(time.ticks_ms(), t_start) / 1000, PALS[pal_i][0]))
                    if hit & 0x24 and SOUND == 'tune':
                        tune_i = (tune_i + (1 if hit & 0x20 else -1)) % len(TUNES); start_tune(tune_i)
                    elif hit & 0x24 and SOUND == 'drones':
                        if hit & 0x20: TIMBRE = (TIMBRE + 1) % len(TIMBRES)
                        if hit & 0x04: TIMBRE = (TIMBRE - 1) % len(TIMBRES)
                        pci = pli = -1
                        LOG.append('%.1fs %s / %s' % (td(time.ticks_ms(), t_start) / 1000, TIMBRES[TIMBRE][0], PROGRESSIONS[PROG][0]))
                elif ph == 1:
                    I2C.readfrom_into(PADDR, AB0); I2C.writeto(PADDR, RA1)
                    x = pad.x_offset - ((AB0[0] << 8) | AB0[1])
                    px = 0 if -20 < x < 20 else x / 512
                elif ph == 2:
                    I2C.readfrom_into(PADDR, AB1)
                    y = ((AB1[0] << 8) | AB1[1]) - pad.y_offset
                    py = 0 if -20 < y < 20 else -y / 512
                pfail = 0
            except OSError:
                perr += 1; pfail += 1
                if pfail >= 8: pad = None; px = py = 0; pbt = 0x1ff; sel = 0   # unplugged: let go of the stick
        elif frames % 128 == 64:                  # look for a gamepad every ~2 s
            pad = newpad()
            if pad: I2C, PADDR, RG, RA0, RA1, GB, AB0, AB1 = bindpad(pad); pfail = 0
        TS[11] = us()
        if px or py:
            cur[0] = min(W - 5, max(4, cur[0] + 1.5 * px)); cur[1] = min(H - 5, max(4, cur[1] + 1.5 * py))
            jet(cur[0], cur[1], 1.5 * px, 1.5 * py, 1.2)
        for a in PUA: m32[a] = U; m32[a + 4] = V - U; m32[a + 8] = F1; m32[a + 12] = F2 - F1
        for a in PDA: m32[a] = U; m32[a + 4] = V
        TS[12] = us()
        wait1()                                   # last frame's spectrum job, run behind the jets / pad / synth work
        zfill(IXA, W // 2); zcols(IXA + R16, ZROWS)   # the fields rely on zero edge cells
        TS[1] = TS[13] = us()
        m32[CTR] = m32[CTR + 4] = m32[CTR + 8] = m32[CTR + 12] = 1   # every shared-row kernel starts at row 1
        go1(F_UV, PUA[1]); advect_uv(PUA[0]); wait1(); TS[2] = us()
        go1(F_R, U, DYE, PDA[1]); advect_r(U, DYE, PDA[0]); wait1(); TS[3] = us()   # dst aliases u: safe
        go1(F_DIV, V, F1, F2, CTR + 8); divergence_h(V, F1, F2, CTR + 8); wait1(); TS[4] = us()
        go1(F_RB, P, V, PR1A); pressure_rb(P, V, PR0); wait1()
        if PR0[7] == -1: raise RuntimeError('pressure barrier timeout')
        TS[5] = us()
        go1(F_PROJ, F1, F2, P, CTR + 12); project_h(F1, F2, P, CTR + 12); wait1(); TS[6] = us()
        _, pb, nr = PALS[pal_i]; PALA = A(pb) + 256 * (hue % nr)
        if RENDER == 'bilin':             # index both halves first: the bilinear rows read one row ahead
            IXA = DYE                     # old dye is free by now: palette indices + a zero pad row
            zfill(IXA + N, (W + 4) // 4)
            go1(F_IDX, IXA + (H // 2) * W, U + (H // 2) * R16, SCALE); index_h(IXA, U, SCALE); wait1()
            if BACKGROUND:                # phase: 256 per frame, faster with bass (ints only: no allocation)
                bgt = (bgt + 256 + (m32[SBAND + 8] * BGB >> 8 if SPECTRUM else 0)) & 0xffffff
                go1(F_BG, IXA + (H // 2) * W, BGC1, bgt); bg_h(IXA, BGC0, bgt); wait1()
            m32[BC1 + 16] = IXA + SPOFF            # strip line buffer: free part of the old dye field
            go1(F_BIL, FBA + 120 * 320, IXA + (H // 2) * W, PALA, BC1); bilin_h(FB, IXA, PALA, BC0); wait1()
        else:
            IXA = DYE
            go1(F_REN, FBA + 120 * 320, U + (H // 2) * R16, PALA, SCALE); render_h(FB, U, PALA, SCALE); wait1()
        TS[7] = us()
        if SPECTRUM: m32[SPC + 12] = IXA + SPOFF; go1(F_SPEC, SPC)   # core1, overlapping the synth work below
        U, V, DYE, F1, F2 = F1, F2, U, V, DYE
        FLD[0] = U; FLD[1] = V; FLD[2] = DYE
        TS[8] = us()
        q = frames & 3                            # one quadrant's stats per frame; fm_map maps all four
        stats_q(U + QOFF[q], V + QOFF[q], DYE + QOFF[q], A(SQ) + 8 * q)
        if fade:
            fade -= 1
            if not fade: AST[4] = 1                   # voices faded out: stop rendering
        elif SOUND == 'tune':
            if SQS[5]: tune_i = (tune_i + 1) % len(TUNES); start_tune(tune_i)   # finished: next tune
            i = SQS[1]; rows = TUNES[tune_i][1]
            if i < last_note: last_note = 0
            j = max(last_note, i - NOTE_JETS)          # notes started since the last frame (newest few)
            while j < i:
                if rows[8 * j + 5] >= 0: splash(rows[8 * j + 7]); hue += NOTE_HUE
                j += 1
            last_note = i
        elif SOUND == 'drones':
            if not AST[4]: fm_map(CTL)
            prog = PROGRESSIONS[PROG][1]
            ci = int(stot / CHORD_FR) % len(prog)
            if ci != pci: pci = ci; retune(ci)
        if SOUND == 'drones' and (px or py or lead_a) and not (fade or AST[4]):
            lead_a += 0.2 * (min(1.0, math.sqrt(px * px + py * py)) - lead_a)
            if lead_a < 0.002: lead_a = 0.0
            li = min(nl - 1, max(0, int((H - cur[1]) * nl / H)))
            if li != pli: pli = li; tune(4, LEAD_SCALE[li], TIMBRES[TIMBRE][5])
            m32[LV + 20] = int((1.5 + 2.5 * lead_a) * BETA); m32[LV + 28] = int(LEAD_VOL * lead_a)
        TS[9] = us()
        for k, (a, b) in enumerate(PAIRS):
            T[k] += td(TS[b], TS[a])
        if frames % HUE_DRIFT == 0: hue += 1
        frames += 1
        if time.ticks_diff(time.ticks_ms(), t_rep) >= 5000:
            el = time.ticks_diff(time.ticks_ms(), t_rep); t_rep = time.ticks_ms()
            print('fps %.1f  ' % (frames * 1000 / el) + '  '.join('%s %.2f' % (n, T[k] / frames / 1000) for k, n in enumerate(NAMES)) + ' ms')
            print('  audio irqs %d max %d avg %d us core0-was-on %d  under/late/fill %s  ke %s  dye %s' % (AST[0], AST[1], AST[3] // max(1, AST[0]), AST[2], display.audio_stats()[:3],
                  ' '.join('%.3f' % (SQ[2 * q] / 300) for q in range(4)), ' '.join('%.2f' % (SQ[2 * q + 1] / 300) for q in range(4))), ' pad errs', perr, ' force x%.2f dye x%.2f bass %.2f treble %.2f' % struct.unpack('<4f', SBV), ' note', SQS[1], '/', SQS[2], 'voices', mem32[VS + 4])
            AST[1] = 0
            T = [0] * 11; frames = 0
try:
    run()
finally:                                  # also on Ctrl-C / errors
    wait1()
    JOB[1] = 0; JOB[0] += 1; sev()        # worker returns
    time.sleep_ms(50)                     # core1 removes its IRQ handler
    display.deinit()                      # else the islands keep repeating the last audio
print('preset changes:', LOG or 'none', ' final:', TIMBRES[TIMBRE][0], '/', PROGRESSIONS[PROG][0])
print('done', ITERS, 'iters omega', OMEGA_, RENDER)
'''

def install(build_dir=None):
    """Standalone, everything in /FluidFM: fluid_s1.mpy (allocations, must run first), fluid_app.mpy (the demo,
    runs forever), fluid_boot.mpy (imports both in that order), fmidi.mpy (MIDI loader) and fluidfm.py, the file
    to run: it puts /FluidFM on sys.path and imports fluid_boot. Kernels: fluid16_gen.py --upload.
    Tunes: make_tunes.py --upload. The menu, main.py and /apps are not touched.
    build_dir: write the board files there instead of uploading (no board needed)."""
    import os, shutil, subprocess, tempfile
    mc = shutil.which('mpy-cross') or os.path.expanduser('~/.local/bin/mpy-cross')
    d = tempfile.mkdtemp()
    open(f'{d}/fluid_s1.py', 'w').write(f'W, H, SLOTS_B, CB_B, AUDIO_RATE, SYN_B = {W}, {H}, {SLOTS_B}, {CB_B}, {AUDIO_RATE}, {SYN_B}\n' + STAGE1)
    open(f'{d}/fluid_app.py', 'w').write('from fluid_s1 import *\nfrom fluid_s1 import _ha\n'
        f'SECONDS, ITERS, SORB, OMEGA_, R16, R32, HROWS, RENDER, AUDIO_RATE, SHIFT, START = 0, {ITERS}, {SORB}, {OMEGA}, {R16}, {R32}, {HROWS}, {RENDER!r}, {AUDIO_RATE}, {SHIFT}, {START!r}\n' + APP)
    open(f'{d}/fluid_boot.py', 'w').write('# fluid + FM synth (cyclebench/fluid_fm.py --install): run /FluidFM/fluidfm.py\n'
        'import fluid_s1   # first: kernels + buffers into SRAM\nimport fluid_app  # runs until SELECT is held\n')
    open(f'{d}/fluidfm.py', 'w').write('# Fluid FM: run this file (best right after a soft reset: the demo needs SRAM that nothing else has taken)\n'
        "import sys\nif '/FluidFM' not in sys.path: sys.path.insert(0, '/FluidFM')\nimport fluid_boot\n")
    subprocess.run([mc, '-march=armv7emsp', '-o', f'{d}/fluid_app.mpy', f'{d}/fluid_app.py'], check=True)
    shutil.copy('fmidi.py', d)
    for f in ('fluid_s1', 'fluid_boot', 'fmidi'):   # .mpy: no parse garbage left in SRAM before the arena
        subprocess.run([mc, '-o', f'{d}/{f}.mpy', f'{d}/{f}.py'], check=True)
    files = ('fluid_s1.mpy', 'fluid_app.mpy', 'fluid_boot.mpy', 'fmidi.mpy', 'fluidfm.py')
    if build_dir:
        os.makedirs(build_dir, exist_ok=True)
        for f in files: shutil.copy(f'{d}/{f}', build_dir); print('wrote', os.path.join(build_dir, f))
        return
    b = Board()
    b.exec("import os\ntry: os.mkdir('/FluidFM')\nexcept OSError: pass")
    for f in files:
        print('uploaded', '/FluidFM/' + f, b.put(f'{d}/{f}', '/FluidFM/' + f))
    b.close()

def main():
    if '--install' in sys.argv: return install()
    if '--build' in sys.argv: return install(sys.argv[sys.argv.index('--build') + 1])
    b = Board(); b.soft_reset()
    out, err = b.exec(f'W, H, SLOTS_B, CB_B, AUDIO_RATE, SYN_B = {W}, {H}, {SLOTS_B}, {CB_B}, {AUDIO_RATE}, {SYN_B}\n' + STAGE1)
    if err: sys.exit(out + err)
    print(out.strip())
    out, err = b.exec(f'SECONDS, ITERS, SORB, OMEGA_, R16, R32, HROWS, RENDER, AUDIO_RATE, SHIFT, START = {SECONDS}, {ITERS}, {SORB}, {OMEGA}, {R16}, {R32}, {HROWS}, {RENDER!r}, {AUDIO_RATE}, {SHIFT}, {START!r}\n' + APP, timeout=SECONDS + 60)
    b.close()
    print(out, err)

if __name__ == '__main__':
    main()
