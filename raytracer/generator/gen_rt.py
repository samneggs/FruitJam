#!/usr/bin/env python3
"""Build-time generator for bb_rt.py (raytraced Ballblazer -- breakaway gauntlet).

Two asm functions: rt_main (core 1, never returns: game + per-frame setup + even rows)
and rt_rows (called from Python on core 0 each frame: odd rows, then returns).
Row/pixel code lives in gen_rt_rows.py and is emitted into both; PACK and GIANT
subroutines in gen_rt_subs.py.

Writes the whole asm_thumb renderer + game from Python so that:
  - FP table offsets are computed, never hand-typed (r7 points 512 bytes into the
    table so vldr/vstr reach +-1020 bytes; out-of-range-for-native offsets are encoded)
  - instructions MicroPython lacks are encoded once via encode.py
  - per-defender code is unrolled at build time (NR defenders)
  - conditional branches out of 16-bit range become b<cc>_w; far jumps become bl

Usage: python3 gen_rt.py [--no-wide]   -> bb_rt.py   (16:9 correction: WIDE_PIXELS in bb_rt_header.py)
"""
import math, re, subprocess, sys, os, json

ENCODER = os.environ.get('ENCODER', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'encode.py'))   # Thumb-2 encoder for data() lines
OUT = 'bb_rt.py'                       # also compiled to bb_rt.mpy: on the board, import bb_rt; bb_rt.main()
RW, RH = 160, 120
FOV_DEG = 62.0
NR = 3                                 # defenders (chrome rotofoils)
ND = 0                                 # spotter drones (0 = off; set 1 to bring the drone back)
NO_WIDE = '--no-wide' in sys.argv      # if your firmware rejects beq_w etc.
BIAS = 1020                            # r7 = FP + BIAS (table reachable at -1020..+1020)

def norm(v):
    n = math.sqrt(sum(c * c for c in v)); return [c / n for c in v]

tn = math.tan(math.radians(FOV_DEG / 2))
L = norm((-0.45, 1.0, -0.35))
AMB = 0.22
LIT = AMB + (1 - AMB) * L[1]
# default colors (the header's PALETTES overwrite these at startup)
TILE_A = [c * LIT for c in (0.04, 0.10, 0.32)]
TILE_B = [c * LIT for c in (0.62, 0.70, 0.86)]
HOR = [0.42, 0.18, 0.50]
TOP = [0.02, 0.02, 0.10]
ORB_R = 0.30
ORB_EDGE = [1.0, 0.72, 0.30]
ORB_CORE = [1.0, 0.98, 0.85]
GLOWC = [1.0, 0.85, 0.45]
TINT = [1.0, 0.55, 0.78]
CYAN = [0.2, 0.9, 1.0]
RAD = [0.85, 0.30, 0.85]
du = dv = 2 * tn / RW                  # square pixels; WIDE_PIXELS (header) widens du at runtime

# ── game layout ──────────────────────────────────────────────────────────────
FW = 10.0                              # half width of the field
ZSTART, GOALZ = -30.0, 30.0            # kickoff line, goal line
ZMIN, ZMAX = -34.0, 33.0
LANES = (-14.0, 0.0, 14.0)             # defender home z
HOMEX = (-3.0, 4.0, -1.0)              # defender home x
LEADS = (6.0, 12.0, 18.0)              # frames of lead when pursuing the player

FP = []
def fp(name, n=1, init=None):
    if init is not None and not isinstance(init, (list, tuple)):
        init = [init]
    FP.append((name, n, init))

# camera / player
fp('CAMX', 1, 0.0); fp('CAMY', 1, 0.42); fp('CAMZ', 1, ZSTART)
fp('FX', 1, 0.0); fp('FZ', 1, 1.0)
fp('PVX', 1, 0.0); fp('PVZ', 1, 0.0); fp('PDAMP', 1, 0.96)
fp('TURN', 1, 0.035 / 512); fp('SPEED', 1, -0.010 / 512)    # stick up (y<0) = forward
fp('STRAFE', 1, 0.006)                                       # side thrust per frame (top ~0.15)
# orb
fp('ORBX', 1, 0.0); fp('ORBY', 1, 0.30); fp('ORBZ', 1, ZSTART + 1.8); fp('ORBR2', 1, ORB_R ** 2)
fp('OVX', 1, 0.0); fp('OVZ', 1, 0.0); fp('ODAMP', 1, 0.992)
fp('SHOT', 1, 0.50); fp('HOLDD', 1, 2.2); fp('AIMK', 1, 0.8)    # on target: predicted miss < AIMK * GHW
fp('CAPR2', 1, 0.8 ** 2)
# orb is drawn smaller and lower while carried so it doesn't block the view
ORB_RH = 0.15
fp('R2F', 1, ORB_R ** 2); fp('R2H', 1, ORB_RH ** 2); fp('OYF', 1, 0.30); fp('OYH', 1, ORB_RH)
fp('GKF', 1, 0.22 * ORB_R ** 2); fp('GKH', 1, 0.22 * ORB_RH ** 2)
fp('GCUTF', 1, 9 * ORB_R ** 2); fp('GCUTH', 1, 9 * ORB_RH ** 2)
fp('KORBF', 1, 1.0); fp('KORBH', 1, 1.0)
# defenders (shape shared)
fp('ROY', 1, 0.40)
fp('IRX', 1, 1 / RAD[0]); fp('IRY', 1, 1 / RAD[1]); fp('IRZ', 1, 1 / RAD[2])
fp('EDAMP', 1, 0.95); fp('OACC', 1, 0.0075); fp('DIFF', 1, 1.05); fp('ACTR', 1, 14.0)
fp('COLR', 1, 1.1); fp('COLR2', 1, 1.1 ** 2); fp('BUMP', 1, 0.10); fp('FLY', 1, 0.20)
for k in range(NR):
    fp(f'ROX{k}', 1, HOMEX[k]); fp(f'ROZ{k}', 1, LANES[k]); fp(f'EVX{k}', 1, 0.0); fp(f'EVZ{k}', 1, 0.0)
    fp(f'LANE{k}', 1, LANES[k]); fp(f'HX{k}', 1, HOMEX[k]); fp(f'LEAD{k}', 1, LEADS[k])
# field
fp('FW', 1, FW); fp('GOALZ', 1, GOALZ); fp('ZSTART', 1, ZSTART); fp('ZMIN', 1, ZMIN); fp('ZMAX', 1, ZMAX)
fp('EPS', 1, 1e-6)
GW = 0.012
fp('GC', 1, 1.0); fp('GS', 1, 0.0); fp('GCW', 1, math.cos(GW)); fp('GSW', 1, math.sin(GW))
fp('GAMP', 1, 4.0); fp('GHW', 1, 1.6); fp('GXA'); fp('GVX')
# projection
fp('U0', 1, -du * (RW / 2 - 0.5)); fp('DU', 1, du)
fp('V0', 1, dv * (RH / 2 - 0.5)); fp('DV', 1, dv)
fp('FADE', 1, 1 / 100); fp('SKYK', 1, 2.2)
fp('LX', 3, L)
fp('OFF', 1, 256.0); fp('ONE', 1, 1.0); fp('HALF', 1, 0.5); fp('ZERO', 1, 0.0)
fp('TWO', 1, 2.0); fp('BIG', 1, 1e30); fp('TFMAX', 1, 100.0)
# colors (palette-driven)
fp('TA', 3, TILE_A); fp('TB', 3, TILE_B)                      # adjacent: parity*12
fp('DT', 3, [b - a for a, b in zip(TILE_A, TILE_B)])
fp('HOR', 3, HOR); fp('DH', 3, [t - h for t, h in zip(TOP, HOR)])
fp('OE', 3, ORB_EDGE); fp('OD', 3, [c - e for c, e in zip(ORB_CORE, ORB_EDGE)])
fp('OCORE', 3, ORB_CORE)
fp('GLOW', 3, GLOWC); fp('GK', 1, 0.22 * ORB_R ** 2); fp('GMAX', 1, 0.6)
fp('GCUT', 1, 9 * ORB_R ** 2); fp('GOFF', 1, 0.22 / 9)
fp('TINT', 3, TINT); fp('RIM', 3, [c * 0.6 for c in CYAN])
fp('STRIPE', 3, [c * 1.4 for c in CYAN]); fp('STHW', 1, RAD[1] * 0.18)
fp('SPECK', 1, 1.3)
fp('MREF', 3, [t * (0.5 * (a + b) + h) * 0.6 for t, a, b, h in zip(TINT, TILE_A, TILE_B, HOR)])
fp('OLIGHT', 3, [c * 0.9 for c in GLOWC]); fp('OLC', 1, 1.5); fp('OLCUT', 1, 6.0)
fp('RS2', 1, (RAD[0] * 0.8) ** 2); fp('SHK2', 1, 0.3 * 2 * RAD[0] * 0.8); fp('SHR2', 1, 1.4 ** 2)
fp('SHD', 1, (1 - AMB) * L[1] / LIT)
fp('FR0', 1, 0.10); fp('FR1', 1, 0.65); fp('CW', 1, 1.0)
fp('SC', 3, [31.99, 63.99, 31.99])
# goal posts
PR = 0.12
fp('PR2', 1, PR * PR); fp('PH', 1, 2.5)
fp('POSTC', 3, [1.0, 0.45, 0.08]); fp('PSH0', 1, 0.6); fp('PSH1', 1, 0.9)
fp('POSTREF', 3, [0.8, 0.36, 0.06])
for k in range(2):
    fp(f'PCX{k}'); fp(f'PCZ{k}'); fp(f'PCC{k}')
fp('T_PB'); fp('T_PA2'); fp('T_PRT')
# edge AA
fp('KORB', 1, 1.0); fp('KROT', 1, 1.0); fp('KPOST', 1, 1.0); fp('EC', 3)
fp('RSIL', 3, [t * h + 0.6 * c for t, h, c in zip(TINT, HOR, CYAN)])
# per frame (asm)
for n in ('PHX', 'PHZ', 'OCO2', 'OCMY', 'CCOM', 'KOX', 'KOZ', 'CUR_OCRX', 'CUR_OCRY', 'CUR_OCRZ'):
    fp(n)
for k in range(NR):
    for n in ('KRX', 'KRZ', 'OCRX', 'OCRY', 'OCRZ', 'CCR', 'OCRMY', 'CCRM'):
        fp(f'{n}{k}')
# per row / per pixel scratch (asm)
fp('RSKY', 3); fp('RF'); fp('RFOG'); fp('RHW'); fp('RI2W')
fp('T_RIM'); fp('T_PY'); fp('T_SPEC'); fp('T_SA')
# row-interval culling: per scale group g (0 = world, 1 = defender-scaled) and per object
fp('LASTX', 1, RW - 1.0)
for g in '01':
    for n in ('A0F', 'A1', 'SX', 'SZ', 'DX', 'DZ'):
        fp(n + g)

# ── split cores ──────────────────────────────────────────────────────────────
fp('DV2', 1, 2 * dv)
for n in ('FR16', 'FR17', 'FR18', 'FR19', 'FR20', 'FR21', 'FR22', 'FR28', 'FR29'):
    fp(n)
fp('FEATF', 1, 15.0)        # palette feature bits: 1 gas giant, 2 stars, 4 walls, 8 drones (16 = sparks, runtime)
fp('MONE', 1, -1.0); fp('EIGHT', 1, 8.0); fp('FOUR', 1, 4.0)
fp('T_FC', 3)
# ── gas giant + rings (sky object at infinity: rays start at the origin) ─────
GDIR = norm((-0.42, 0.30, 1.0)); GD = 100.0; GR = GD * math.sin(math.radians(10))
GAX = norm((0.35, 1.0, -0.75)); GSUN = norm((0.9, 0.35, 0.2))
GPC = [c * GD for c in GDIR]
RR1, RR2 = 1.35 ** 2, 2.35 ** 2
fp('PCX', 1, GPC[0]); fp('PCY', 1, GPC[1]); fp('PCZ', 1, GPC[2]); fp('PCMY', 1, -GPC[1])
fp('CCB', 1, GD * GD - (2.4 * GR) ** 2); fp('CCP', 1, GD * GD - GR * GR)
fp('PCA', 1, sum(p * x for p, x in zip(GPC, GAX)))
fp('GAXV', 3, GAX); fp('GSUN', 3, GSUN)
fp('INVR', 1, 1 / GR); fp('INVR2', 1, 1 / GR ** 2); fp('GR2', 1, GR * GR)
fp('RR1', 1, RR1); fp('RR2', 1, RR2); fp('RK', 1, 63.99 / (RR2 - RR1))
fp('BK', 1, 31.99); fp('BT', 1, 0.06); fp('G06', 1, 0.06); fp('G94', 1, 0.94); fp('SHAD', 1, 0.15)
# ── stars ────────────────────────────────────────────────────────────────────
fp('STV', 1, 0.10); fp('STS', 1, 70.0); fp('STB', 1, 0.75 / 255)
# ── arena walls at x = +-FW ──────────────────────────────────────────────────
fp('WX', 1, FW); fp('WH', 1, 1.6); fp('WC', 3, [0.3, 0.9, 1.0])
fp('WKEEP', 1, 0.85); fp('WA', 1, 0.12); fp('WB', 1, 0.9); fp('WPK', 1, 1.2); fp('WMIN', 1, 0.03)
fp('WGYK', 1, 0.6); fp('WGYI', 1, 2.5); fp('WGYS', 1, 0.4); fp('WOFF', 1, 64.0)
fp('T_WG'); fp('T_WT'); fp('T_WGM')
fp('WAP'); fp('WAN'); fp('WHMC', 1, 1.6 - 0.42); fp('WHPC', 1, 1.6 + 0.42)
# ── spotter drone (camera-facing vertical billboard, 16x8 texture, 4 frames) ─
# Loose orb: hovers over it with a pulsing beam down to it. You carry the orb: it hovers
# over the goal mouth; its lamp turns green when your heading would score (goal slide
# included). Billboard grows with distance so it stays readable across the field.
DHW, DHH = 0.70, 0.35
fp('DHW', 1, DHW); fp('DSZ', 1, 0.045)               # half width = max(DHW, distance * DSZ)
fp('DRL', 1, 0.12); fp('DVMAX', 1, 0.9); fp('DBOB', 1, 0.15); fp('BTOP', 1, 0.45)   # beam starts BTOP below the drone
fp('DOH', 1, 2.0); fp('DGH', 1, 3.2)                 # hover height over the orb / over the goal
fp('BEAMI', 1, 150.0); fp('BEAMH', 1, 0.25); fp('BEAMP', 1, 0.05)   # beam brightness, halo, pulse speed
NBEAM = 12
for k in range(ND):
    fp(f'DBX{k}', 1, 0.0); fp(f'DBY{k}', 1, 2.0); fp(f'DBZ{k}', 1, ZSTART + 6.0)
    for n in ('DOX', 'DOY', 'DOZ', 'DOMY', 'DL2', 'DKU', 'DKV', 'CCD', 'CCDM', 'DTY'):
        fp(f'{n}{k}')
fp('T_DC', 3)
# ── collision sparks (state in TEX: NSP x 12 floats) and stars (TEX table) ──
# Both are drawn by splatting into byte light layers once per frame (rt_main);
# the pixel loop just adds layer[pixel] -- stable, cheap, no per-pixel search.
NSP = 24
NSTAR = 96
fp('SPX'); fp('SPZ'); fp('SPY', 1, 0.45)
fp('SGRAV', 1, 0.005); fp('SBNC', 1, -0.45); fp('SDAMP', 1, 0.988); fp('SLIFE0', 1, 80.0)
fp('SVH', 1, 0.22); fp('SVY0', 1, 0.08); fp('SVYR', 1, 0.12); fp('THREEHALF', 1, 1.5)
fp('SPV', 1, 1.0); fp('SFWD', 1, 0.12)               # sparks inherit your velocity + a forward kick
fp('SKB', 1, 255 * 2.4); fp('SHALO', 1, 0.45); fp('SRING', 1, 0.45); fp('SMIR', 1, 0.45)
fp('SPKK', 1, 1.5 / 255); fp('SPC', 3, [1.0, 0.75, 0.35])
fp('ZNEAR', 1, 0.3); fp('IDU'); fp('IU0'); fp('IDV'); fp('JV0')   # projection (header fills these)
fp('XMAX', 1, 157.0); fp('JSTAR', 1, 58.0); fp('JSPK', 1, 118.0)
OBJS = ['orb', 'glow', 'morb'] + [f'roto{k}' for k in range(NR)] + [f'mroto{k}' for k in range(NR)]
OBJS += ['giant', 'mgiant'] + [f'drone{k}' for k in range(ND)] + [f'mdrone{k}' for k in range(ND)]
for o in OBJS:
    fp('B1_' + o); fp('BF_' + o); fp('OCY_' + o); fp('CC_' + o); fp('Q2_' + o)
for o in OBJS[1:] + ['goal']:
    fp('LO_' + o); fp('W_' + o)          # int32 bits; the orb's range lives in r8/r9
fp('FEATI'); fp('DF0')       # int32: feature bits, drone texture frame byte offsets
fp('STARA'); fp('SPKA')                 # int32: star / spark light-layer addresses

OFFS = {}
_o = 0
for name, n, init in FP:
    OFFS[name] = _o; _o += 4 * n
FP_LEN = _o // 4
assert _o - BIAS <= 1020 + 4, f'table too large for r7 +-1020 ({_o} bytes)'
# TEX array (float32, base in r10): spark state, planet band table, ring table, drone frames
TEX_SPARK, TEX_STAR, TEX_BAND, TEX_RING, TEX_DRONE = 0, 1280, 2816, 3584, 4608
TEX_FRAME = 16 * 8 * 12
TEX_LEN = (TEX_DRONE + 4 * TEX_FRAME) // 4
SP_STRIDE = 48       # per spark: px py pz vx vy vz life (+ spare)

def O(name, k=0): return OFFS[name] + 4 * k

HERE = os.path.dirname(os.path.abspath(__file__))
ROWS = open(os.path.join(HERE, 'gen_rt_rows.py')).read()
SUBS = open(os.path.join(HERE, 'gen_rt_subs.py')).read()
# ── asm emitter ─────────────────────────────────────────────────────────────
A = []          # items: str | ('E', ins, note) | ('BR', cond, label)
def a(s): A.append(s)
def E(ins, note=''): A.append(('E', ins, note))
def lab(n): a(f'label({n})')
def H(hw, txt): A.append(('H', hw, txt))      # one 16-bit instruction, hand-encoded
def br(cond, label): A.append(('BR', cond, label))
def jmp(label): A.append(('BR', None, label))     # unconditional
def _vls(op, s, base, name, k):
    rel = O(name, k) - BIAS
    if 0 <= rel <= 1020:
        a(f'{op}({s}, [{base}, {rel}])')
    else:
        # VLDR/VSTR T2 with U = 0 (negative offset); the bundled encoder only covers U = 1.
        # hw1 = 1110 1101 U D 0 L Rn, hw2 = Vd 1010 imm8 (Sd = Vd:D). Checked against binutils.
        assert -1020 <= rel < 0 and rel % 4 == 0
        d = int(s[1:]); rn = int(base[1:]); L = 1 if op == 'vldr' else 0
        hw1 = 0xED00 | (d & 1) << 6 | L << 4 | rn
        hw2 = (d >> 1) << 12 | 0x0A00 | (-rel // 4)
        A.append(('D', hw1, hw2, f'{op} {s}, [{base}, #{rel}]'))
def ld(s, name, k=0): _vls('vldr', s, 'r7', name, k)
def st(s, name, k=0): _vls('vstr', s, 'r7', name, k)
def ldb(s, base, name, k=0): _vls('vldr', s, base, name, k)
def ldri(r, name):          # int load from the table
    rel = O(name) - BIAS; assert rel >= -255; E(f'ldr.w {r}, [r7, #{rel}]')
def stri(r, name):
    rel = O(name) - BIAS; assert rel >= -255; E(f'str.w {r}, [r7, #{rel}]')
def cmpz(s): E(f'vcmp.f32 {s}, #0'); a('vmrs(APSR_nzcv, FPSCR)')
def cmpf(x, y): a(f'vcmp({x}, {y})'); a('vmrs(APSR_nzcv, FPSCR)')
def mla(d, x, y): E(f'vmla.f32 {d}, {x}, {y}')
def mls(d, x, y): E(f'vmls.f32 {d}, {x}, {y}')
def fmov(d, x): E(f'vmov.f32 {d}, {x}')
def fabs(d, x): E(f'vabs.f32 {d}, {x}')
def fmin(d, x, y): E(f'vminnm.f32 {d}, {x}, {y}')
def fmax(d, x, y): E(f'vmaxnm.f32 {d}, {x}, {y}')
def dot3(d, x, y):
    a(f'vmul({d}, {x[0]}, {y[0]})'); mla(d, x[1], y[1]); mla(d, x[2], y[2])
def in_range(o, skip):      # skip unless pixel counter r5 is inside object o's span for this row
    ldri('r0', 'LO_' + o); a('sub(r0, r5, r0)'); ldri('r1', 'W_' + o); a('cmp(r0, r1)'); br('hi', skip)

# r4 out ptr  r5 x count  r6 y count  r7 FP+BIAS  r2 hit id  r3 floor-row flag  r8/r9 orb span
# s8 t_floor  s9 dpz  s10 dpx  s11 pz  s15 px  (floor coords, phase-shifted)
# s12 dx  s13 dz  s14 v   (primary ray, unnormalized)   s30 a = d.d
# s16-s18 orb oc  s19 orb cc  s20/s21 ray step  s22 cam height  s23 best t
# s24 orb b  s25 orb disc  s26/s27 edge coverage/depth  s28/s29 row-start dir  s31 CTRL
D = ('s12', 's14', 's13')
OC = ('s16', 's17', 's18')
SKY, FLOOR, ORB, POST, ROTO0, DRONE = 0, 1, 2, 4, 8, 16
C_SETUP, C_HALF, C_RBCUR, C_TEX, C_FP1, C_SEED, C_SPAWN, C_STARL, C_SPKL = 64, 68, 72, 76, 80, 84, 88, 92, 96

E('push.w {r8, r9, r10, r11}', 'callee-saved; r8/r9 = orb x-range')
E('vpush {s16-s31}', 'AAPCS: s16-s31 callee-saved')
a('vmov(s31, r0)')
a('mov(r7, r1)'); a(f'movwt(r1, {BIAS})'); a('add(r7, r7, r1)')
E(f'ldr.w r10, [r0, #{C_TEX}]', 'r10 = TEX base for the whole function')

lab('FRAME_LOOP')
a('vmov(r0, s31)')
lab('WAIT')
a('ldr(r1, [r0, 0])'); a('cmp(r1, 0)'); br('ne', 'DONE')
a('ldr(r1, [r0, 4])'); a('ldr(r2, [r0, 8])'); a('sub(r1, 1)'); a('cmp(r2, r1)')
br('lt', 'WAIT')

# ── game update (once per frame) ────────────────────────────────────────────
# CTRL: 20 buttons  32 HOLD (1 = you carry the orb, 0 = loose)  36 goals  40 turnovers
#       44 your pickup cooldown  48 defenders' pickup cooldown  52 orb-off-screen side  56 aim lock
C_BTN, C_HOLD, C_GOALS, C_TURN, C_CDP, C_CDD, C_SIDE, C_AIM = 20, 32, 36, 40, 44, 48, 52, 56
FIRE_MASK = 0x40                     # GAMEPAD_UP, active low
STR_R, STR_L = 0x20, 0x04            # GAMEPAD_RIGHT / GAMEPAD_LEFT strafe, active low
def clampf(r, lim):                  # r = clamp(r, -lim, lim); uses s3
    ld('s3', lim); fmin(r, r, 's3'); a('vneg(s3, s3)'); fmax(r, r, 's3')
def clampz(r):                       # r = clamp(r, ZMIN, ZMAX); uses s3
    ld('s3', 'ZMAX'); fmin(r, r, 's3'); ld('s3', 'ZMIN'); fmax(r, r, 's3')
def dist2(dst, ax, az, bx, bz):      # dst = |a - b|^2 (FP names); uses s6, s7
    ld('s6', ax); ld('s7', bx); a('vsub(s6, s6, s7)'); a(f'vmul({dst}, s6, s6)')
    ld('s6', az); ld('s7', bz); a('vsub(s6, s6, s7)'); mla(dst, 's6', 's6')
def set_ctrl(off, val):
    if val < 0:
        a(f'movwt(r1, {val})')
    else:
        a(f'mov(r1, {val})')
    a(f'str(r1, [r0, {off}])')

# player: yaw (small-angle rotate + renormalize), throttle with inertia
a('ldr(r1, [r0, 12])'); a('vmov(s0, r1)'); a('vcvt_f32_s32(s0, s0)')
ld('s1', 'TURN'); a('vmul(s0, s0, s1)')
a('vmul(s1, s0, s0)'); ld('s2', 'HALF'); a('vmul(s1, s1, s2)')
ld('s2', 'ONE'); a('vsub(s1, s2, s1)')
ld('s2', 'FX'); ld('s3', 'FZ')
a('vmul(s4, s2, s1)'); mla('s4', 's3', 's0')
a('vmul(s5, s3, s1)'); mls('s5', 's2', 's0')
a('vmul(s6, s4, s4)'); mla('s6', 's5', 's5'); a('vsqrt(s6, s6)')
a('vdiv(s4, s4, s6)'); a('vdiv(s5, s5, s6)')
st('s4', 'FX'); st('s5', 'FZ')
a('ldr(r1, [r0, 16])'); a('vmov(s0, r1)'); a('vcvt_f32_s32(s0, s0)')
ld('s1', 'SPEED'); a('vmul(s0, s0, s1)')
ld('s7', 'PDAMP')
ld('s6', 'PVX'); mla('s6', 's4', 's0'); a('vmul(s6, s6, s7)'); st('s6', 'PVX')
ld('s2', 'PVZ'); mla('s2', 's5', 's0'); a('vmul(s2, s2, s7)')
# strafe along right = (fz, -fx)
a(f'ldr(r1, [r0, {C_BTN}])')
a(f'mov(r2, {STR_R})'); a('tst(r1, r2)'); br('ne', 'STR_NR')
ld('s7', 'STRAFE'); mla('s6', 's5', 's7'); mls('s2', 's4', 's7')
lab('STR_NR')
a(f'mov(r2, {STR_L})'); a('tst(r1, r2)'); br('ne', 'STR_NL')
ld('s7', 'STRAFE'); mls('s6', 's5', 's7'); mla('s2', 's4', 's7')
lab('STR_NL')
st('s6', 'PVX'); st('s2', 'PVZ')
ld('s1', 'CAMX'); a('vadd(s1, s1, s6)'); clampf('s1', 'FW'); st('s1', 'CAMX')
ld('s1', 'CAMZ'); a('vadd(s1, s1, s2)'); clampz('s1'); st('s1', 'CAMZ')
for off in (C_CDP, C_CDD):           # pickup cooldowns: you, defenders
    a(f'ldr(r1, [r0, {off}])'); a('cmp(r1, 0)'); br('eq', f'CD_ZERO{off}')
    a('sub(r1, 1)'); a(f'str(r1, [r0, {off}])'); lab(f'CD_ZERO{off}')

# goal slides side to side: (GC, GS) rotated a fixed angle each frame
ld('s0', 'GC'); ld('s1', 'GS'); ld('s2', 'GCW'); ld('s3', 'GSW')
a('vmul(s4, s0, s2)'); mls('s4', 's1', 's3')
a('vmul(s5, s1, s2)'); mla('s5', 's0', 's3')
a('vmul(s6, s4, s4)'); mla('s6', 's5', 's5'); a('vsqrt(s6, s6)')
a('vdiv(s4, s4, s6)'); a('vdiv(s5, s5, s6)'); st('s4', 'GC'); st('s5', 'GS')
ld('s6', 'GAMP'); a('vmul(s0, s5, s6)'); st('s0', 'GXA')
a('vmul(s0, s4, s6)'); a('vmul(s0, s0, s3)'); st('s0', 'GVX')          # goal x velocity per frame

# defenders: each guards a zone around its lane; inside the zone it pursues a lead
# point on you (or the loose orb), otherwise it drifts home. Ramming you knocks the
# orb loose; touching a loose orb is a turnover.
for k in range(NR):
    a(f'ldr(r2, [r0, {C_HOLD}])'); a('cmp(r2, 1)'); br('ne', f'D{k}_ORB')
    ld('s2', f'LEAD{k}')
    ld('s0', 'CAMX'); ld('s1', 'PVX'); mla('s0', 's1', 's2')
    ld('s1', 'CAMZ'); ld('s3', 'PVZ'); mla('s1', 's3', 's2'); jmp(f'D{k}_ZONE')
    lab(f'D{k}_ORB'); ld('s0', 'ORBX'); ld('s1', 'ORBZ')
    lab(f'D{k}_ZONE')
    ld('s2', f'LANE{k}'); a('vsub(s3, s1, s2)'); fabs('s3', 's3'); ld('s4', 'ACTR'); cmpf('s3', 's4')
    br('le', f'D{k}_GO')
    ld('s0', f'HX{k}'); fmov('s1', 's2')
    lab(f'D{k}_GO')
    ld('s2', f'ROX{k}'); ld('s3', f'ROZ{k}'); a('vsub(s0, s0, s2)'); a('vsub(s1, s1, s3)')
    a('vmul(s6, s0, s0)'); mla('s6', 's1', 's1'); ld('s7', 'EPS'); a('vadd(s6, s6, s7)'); a('vsqrt(s6, s6)')
    a('vdiv(s0, s0, s6)'); a('vdiv(s1, s1, s6)')
    ld('s7', 'OACC'); ld('s6', f'EVX{k}'); mla('s6', 's0', 's7'); ld('s2', f'EVZ{k}'); mla('s2', 's1', 's7')
    ld('s7', 'EDAMP'); a('vmul(s6, s6, s7)'); a('vmul(s2, s2, s7)'); st('s6', f'EVX{k}'); st('s2', f'EVZ{k}')
    ld('s1', f'ROX{k}'); a('vadd(s1, s1, s6)'); clampf('s1', 'FW'); st('s1', f'ROX{k}')
    ld('s1', f'ROZ{k}'); a('vadd(s1, s1, s2)'); clampz('s1'); st('s1', f'ROZ{k}')
    # collision with the player
    dist2('s4', f'ROX{k}', f'ROZ{k}', 'CAMX', 'CAMZ')
    ld('s5', 'COLR2'); cmpf('s4', 's5'); br('ge', f'D{k}_NOHIT')
    ld('s0', 'CAMX'); ld('s1', f'ROX{k}'); a('vsub(s0, s0, s1)')
    ld('s1', 'CAMZ'); ld('s2', f'ROZ{k}'); a('vsub(s1, s1, s2)')
    ld('s7', 'EPS'); a('vadd(s4, s4, s7)'); a('vsqrt(s6, s4)'); a('vdiv(s0, s0, s6)'); a('vdiv(s1, s1, s6)')
    ld('s2', 'COLR')
    ld('s3', 'CAMX'); mls('s3', 's0', 's2'); st('s3', f'ROX{k}')
    ld('s3', 'CAMZ'); mls('s3', 's1', 's2'); st('s3', f'ROZ{k}')
    ld('s2', 'BUMP')
    ld('s3', 'PVX'); mla('s3', 's0', 's2'); st('s3', 'PVX')
    ld('s3', 'PVZ'); mla('s3', 's1', 's2'); st('s3', 'PVZ')
    ld('s3', f'EVX{k}'); mls('s3', 's0', 's2'); st('s3', f'EVX{k}')
    ld('s3', f'EVZ{k}'); mls('s3', 's1', 's2'); st('s3', f'EVZ{k}')
    ld('s3', 'CAMX'); ld('s4', f'ROX{k}'); a('vadd(s3, s3, s4)'); ld('s4', 'HALF'); a('vmul(s3, s3, s4)'); st('s3', 'SPX')
    ld('s3', 'CAMZ'); ld('s5', f'ROZ{k}'); a('vadd(s3, s3, s5)'); a('vmul(s3, s3, s4)'); st('s3', 'SPZ')
    set_ctrl(C_SPAWN, 1)
    a(f'ldr(r2, [r0, {C_HOLD}])'); a('cmp(r2, 1)'); br('ne', f'D{k}_NOHIT')
    ld('s2', 'FLY'); a('vmul(s3, s1, s2)'); a('vneg(s3, s3)'); a('vmul(s5, s0, s2)')   # sideways
    a('ldr(r1, [r0, 4])'); a('mov(r2, 1)'); a('tst(r1, r2)'); br('eq', f'D{k}_FLY')
    a('vneg(s3, s3)'); a('vneg(s5, s5)')
    lab(f'D{k}_FLY')
    st('s3', 'OVX'); st('s5', 'OVZ'); set_ctrl(C_HOLD, 0); set_ctrl(C_CDP, 25); set_ctrl(C_CDD, 45)
    lab(f'D{k}_NOHIT')
    # turnover: a defender reaches the loose orb
    a(f'ldr(r2, [r0, {C_HOLD}])'); a('cmp(r2, 0)'); br('ne', f'D{k}_DONE')
    a(f'ldr(r1, [r0, {C_CDD}])'); a('cmp(r1, 0)'); br('ne', f'D{k}_DONE')
    dist2('s4', f'ROX{k}', f'ROZ{k}', 'ORBX', 'ORBZ'); ld('s5', 'CAPR2'); cmpf('s4', 's5'); br('ge', f'D{k}_DONE')
    a(f'ldr(r1, [r0, {C_TURN}])'); a('add(r1, 1)'); a(f'str(r1, [r0, {C_TURN}])'); jmp('RESET')
    lab(f'D{k}_DONE')

# orb
a(f'ldr(r2, [r0, {C_HOLD}])'); a('cmp(r2, 1)'); br('eq', 'ORB_HP')
ld('s0', 'ORBX'); ld('s2', 'OVX'); a('vadd(s0, s0, s2)')
ld('s1', 'ORBZ'); ld('s3', 'OVZ'); a('vadd(s1, s1, s3)')
ld('s6', 'ODAMP'); a('vmul(s2, s2, s6)'); a('vmul(s3, s3, s6)')
ld('s6', 'FW'); fabs('s7', 's0'); cmpf('s7', 's6'); br('le', 'ORB_XOK')        # side walls: bounce
a('vneg(s2, s2)'); fmin('s0', 's0', 's6'); a('vneg(s6, s6)'); fmax('s0', 's0', 's6')
lab('ORB_XOK')
ld('s6', 'GOALZ'); cmpf('s1', 's6'); br('le', 'ORB_ZA')                       # goal line
ld('s7', 'GXA'); a('vsub(s7, s0, s7)'); fabs('s7', 's7'); ld('s4', 'GHW'); cmpf('s7', 's4'); br('lt', 'SCORE')
a('vneg(s3, s3)'); fmov('s1', 's6'); jmp('ORB_ZDONE')
lab('ORB_ZA')
ld('s6', 'ZMIN'); cmpf('s1', 's6'); br('ge', 'ORB_ZDONE')
a('vneg(s3, s3)'); fmov('s1', 's6')
lab('ORB_ZDONE')
st('s0', 'ORBX'); st('s1', 'ORBZ'); st('s2', 'OVX'); st('s3', 'OVZ')
set_ctrl(C_AIM, 0)
a(f'ldr(r1, [r0, {C_CDP}])'); a('cmp(r1, 0)'); br('ne', 'ORB_DONE')
dist2('s4', 'ORBX', 'ORBZ', 'CAMX', 'CAMZ'); ld('s5', 'CAPR2'); cmpf('s4', 's5'); br('ge', 'ORB_DONE')
set_ctrl(C_HOLD, 1); jmp('ORB_DONE')
# carried: in front of you; the shot goes straight along your heading.
lab('ORB_HP')
ld('s4', 'FX'); ld('s5', 'FZ'); ld('s6', 'HOLDD')
ld('s0', 'CAMX'); mla('s0', 's4', 's6'); st('s0', 'ORBX')
ld('s1', 'CAMZ'); mla('s1', 's5', 's6'); st('s1', 'ORBZ')
# on target? follow the shot line to the goal line and compare with where the
# sliding goal will be when the orb gets there
set_ctrl(C_AIM, 0)
cmpz('s5'); br('le', 'HP_FREE')
ld('s3', 'GOALZ'); a('vsub(s3, s3, s1)'); a('vdiv(s3, s3, s5)')     # shot length to the goal line
a('vmul(s2, s4, s3)'); a('vadd(s2, s2, s0)')                          # x where the shot crosses it
ld('s6', 'SHOT'); a('vdiv(s3, s3, s6)')                               # frames to get there
ld('s6', 'GXA'); ld('s7', 'GVX'); mla('s6', 's7', 's3')               # goal centre then
a('vsub(s2, s2, s6)'); fabs('s2', 's2')
ld('s6', 'GHW'); ld('s7', 'AIMK'); a('vmul(s6, s6, s7)'); cmpf('s2', 's6'); br('ge', 'HP_FREE')
set_ctrl(C_AIM, 1)
lab('HP_FREE')
a(f'ldr(r1, [r0, {C_BTN}])'); a(f'mov(r2, {FIRE_MASK})'); a('tst(r1, r2)'); br('ne', 'ORB_DONE')
ld('s6', 'SHOT'); a('vmul(s2, s4, s6)'); a('vmul(s3, s5, s6)'); st('s2', 'OVX'); st('s3', 'OVZ')
set_ctrl(C_HOLD, 0); set_ctrl(C_CDP, 20); set_ctrl(C_AIM, 0); jmp('ORB_DONE')
# goal, then a new run (defenders get a little faster each goal)
lab('SCORE'); a(f'ldr(r1, [r0, {C_GOALS}])'); a('add(r1, 1)'); a(f'str(r1, [r0, {C_GOALS}])')
ld('s0', 'OACC'); ld('s1', 'DIFF'); a('vmul(s0, s0, s1)'); st('s0', 'OACC')
lab('RESET')
ld('s0', 'ZERO'); ld('s1', 'ONE')
for n in ('CAMX', 'FX', 'PVX', 'PVZ', 'OVX', 'OVZ'):
    st('s0', n)
st('s1', 'FZ'); ld('s2', 'ZSTART'); st('s2', 'CAMZ')
for k in range(NR):
    st('s0', f'EVX{k}'); st('s0', f'EVZ{k}')
    ld('s2', f'HX{k}'); st('s2', f'ROX{k}'); ld('s2', f'LANE{k}'); st('s2', f'ROZ{k}')
set_ctrl(C_HOLD, 1); set_ctrl(C_CDP, 0); set_ctrl(C_CDD, 0)
lab('ORB_DONE')
a(f'ldr(r2, [r0, {C_HOLD}])'); a('cmp(r2, 1)'); br('ne', 'ORB_PFREE')
for dst, src in (('ORBR2', 'R2H'), ('ORBY', 'OYH'), ('GK', 'GKH'), ('GCUT', 'GCUTH'), ('KORB', 'KORBH')):
    ld('s0', src); st('s0', dst)
jmp('ORB_PDONE')
lab('ORB_PFREE')
for dst, src in (('ORBR2', 'R2F'), ('ORBY', 'OYF'), ('GK', 'GKF'), ('GCUT', 'GCUTF'), ('KORB', 'KORBF')):
    ld('s0', src); st('s0', dst)
lab('ORB_PDONE')
# off-screen orb indicator for core 1: side = sign of the orb's right-component
set_ctrl(C_SIDE, 0)
a(f'ldr(r2, [r0, {C_HOLD}])'); a('cmp(r2, 0)'); br('ne', 'IND_DONE')
ld('s4', 'FX'); ld('s5', 'FZ')
ld('s0', 'ORBX'); ld('s1', 'CAMX'); a('vsub(s0, s0, s1)')
ld('s1', 'ORBZ'); ld('s2', 'CAMZ'); a('vsub(s1, s1, s2)')
a('vmul(s2, s0, s4)'); mla('s2', 's1', 's5')                 # forward component
a('vmul(s3, s0, s5)'); mls('s3', 's1', 's4')                 # right component
ld('s6', 'U0'); fabs('s6', 's6'); a('vmul(s6, s6, s2)')      # half-width of view at that depth
fabs('s7', 's3')
cmpz('s2'); br('le', 'IND_OFF')
cmpf('s7', 's6'); br('lt', 'IND_DONE')
lab('IND_OFF')
set_ctrl(C_SIDE, 1); cmpz('s3'); br('ge', 'IND_DONE'); set_ctrl(C_SIDE, -1)
lab('IND_DONE')
ld('s4', 'FX'); ld('s5', 'FZ')


# ── sparks: spawn a burst if a collision asked for one, then integrate ───────
def rnd(dst):                         # dst = uniform [-0.5, 0.5) from an LCG in CTRL; uses r1-r3
    a(f'ldr(r1, [r0, {C_SEED}])'); a('movwt(r2, 1664525)'); a('mul(r1, r2)')
    a('movwt(r2, 1013904223)'); a('add(r1, r1, r2)'); a(f'str(r1, [r0, {C_SEED}])')
    a('lsr(r2, r1, 9)'); a('movwt(r3, 0x3F800000)'); a('orr(r2, r3)'); a(f'vmov({dst}, r2)')
    ld('s7', 'THREEHALF'); a(f'vsub({dst}, {dst}, s7)')
a(f'ldr(r1, [r0, {C_SPAWN}])'); a('cmp(r1, 0)'); br('eq', 'SPAWN_DONE')
set_ctrl(C_SPAWN, 0)
for i in range(NSP):
    E(f'addw r9, r10, #{TEX_SPARK + i * SP_STRIDE}')
    ld('s0', 'SPX'); E('vstr s0, [r9, #0]')
    ld('s0', 'SPY'); E('vstr s0, [r9, #4]')
    ld('s0', 'SPZ'); E('vstr s0, [r9, #8]')
    for c, (pv, fd) in ((0, ('PVX', 'FX')), (2, ('PVZ', 'FZ'))):
        rnd('s0'); ld('s1', 'SVH'); a('vmul(s0, s0, s1)')
        ld('s1', pv); ld('s2', 'SPV'); mla('s0', 's1', 's2')
        ld('s1', fd); ld('s2', 'SFWD'); mla('s0', 's1', 's2')
        E(f'vstr s0, [r9, #{12 + 4 * c}]')
    rnd('s0'); ld('s1', 'SVYR'); a('vmul(s0, s0, s1)'); ld('s1', 'SVY0'); a('vadd(s0, s0, s1)'); E('vstr s0, [r9, #16]')
    ld('s0', 'SLIFE0'); E('vstr s0, [r9, #24]')
lab('SPAWN_DONE')
a('mov(r3, 0)')                       # r3 = 1 if any spark is alive
for i in range(NSP):
    E(f'addw r9, r10, #{TEX_SPARK + i * SP_STRIDE}')
    E('vldr s6, [r9, #24]'); cmpz('s6'); br('le', f'SP_DEAD{i}')
    for c in range(3):
        E(f'vldr s{c}, [r9, #{4 * c}]'); E(f'vldr s{3 + c}, [r9, #{12 + 4 * c}]')
        a(f'vadd(s{c}, s{c}, s{3 + c})')
    ld('s7', 'SGRAV'); a('vsub(s4, s4, s7)')
    cmpz('s1'); br('ge', f'SP_UP{i}')
    ld('s1', 'ZERO'); ld('s7', 'SBNC'); a('vmul(s4, s4, s7)')
    lab(f'SP_UP{i}')
    ld('s7', 'SDAMP'); a('vmul(s3, s3, s7)'); a('vmul(s5, s5, s7)')
    ld('s7', 'ONE'); a('vsub(s6, s6, s7)')
    for c in range(3):
        E(f'vstr s{c}, [r9, #{4 * c}]'); E(f'vstr s{3 + c}, [r9, #{12 + 4 * c}]')
    E('vstr s6, [r9, #24]')
    a('mov(r3, 1)')
    lab(f'SP_DEAD{i}')
ld('s0', 'WX'); ld('s1', 'CAMX'); a('vsub(s2, s0, s1)'); st('s2', 'WAP'); a('vadd(s2, s0, s1)'); st('s2', 'WAN')
# feature bits for this frame -> r11 (and FEATI for rt_rows)
ld('s0', 'FEATF'); a('vcvt_s32_f32(s0, s0)'); a('vmov(r1, s0)')
a('mov(r2, 16)'); a('orr(r1, r2)')                     # glow layer always on (sparks, drone beam)
lab('FEAT_NOSP')
E('mov.w r11, r1'); stri('r11', 'FEATI')
# spotter drone: target = over the loose orb, or over the goal mouth while you carry it
for k in range(ND):
    a(f'ldr(r2, [r0, {C_HOLD}])'); a('cmp(r2, 1)'); br('eq', f'DR_GOAL{k}')
    ld('s0', 'ORBX'); ld('s1', 'ORBZ'); ld('s2', 'DOH'); ld('s3', 'ORBY'); jmp(f'DR_TGT{k}')
    lab(f'DR_GOAL{k}'); ld('s0', 'GXA'); ld('s1', 'GOALZ'); ld('s2', 'DGH'); ld('s3', 'ZERO')
    lab(f'DR_TGT{k}')
    st('s3', f'DTY{k}')                                               # beam ends here
    ld('s3', 'GS'); ld('s4', 'DBOB'); mla('s2', 's3', 's4')            # bob
    ld('s7', 'DRL')
    for c, n in ((0, 'DBX'), (2, 'DBY'), (1, 'DBZ')):                 # step = (target - pos) * DRL
        ld('s3', f'{n}{k}'); a(f'vsub(s{c}, s{c}, s3)'); a(f'vmul(s{c}, s{c}, s7)')
    dot3('s3', ('s0', 's1', 's2'), ('s0', 's1', 's2')); a('vsqrt(s3, s3)')
    ld('s4', 'DVMAX'); cmpf('s3', 's4'); br('le', f'DR_SLOW{k}')      # cap the speed
    a('vdiv(s4, s4, s3)')
    for c in range(3):
        a(f'vmul(s{c}, s{c}, s4)')
    lab(f'DR_SLOW{k}')
    for c, n in ((0, 'DBX'), (2, 'DBY'), (1, 'DBZ')):
        ld('s3', f'{n}{k}'); a(f'vadd(s3, s3, s{c})'); st('s3', f'{n}{k}')
    # texture frame: rotor phase (frame >> 1) & 1, + 2 for the green "on target" lamp
    a('ldr(r1, [r0, 4])'); a('lsr(r1, r1, 1)'); a('mov(r2, 1)'); a('and_(r1, r2)')
    a(f'ldr(r2, [r0, {C_AIM}])'); a('lsl(r2, r2, 1)'); a('add(r1, r1, r2)')
    a(f'movwt(r2, {TEX_FRAME})'); a('mul(r1, r2)'); a(f'movwt(r2, {TEX_DRONE})'); a('add(r1, r1, r2)')
    stri('r1', f'DF{k}')
ld('s4', 'FX'); ld('s5', 'FZ')                                   # per-frame ray setup below expects the heading here

# ── floor phase: cam - 2*trunc(cam/2) + OFF (parity period 2, keeps precision)
ld('s3', 'HALF'); ld('s6', 'OFF')
for c, p in (('CAMX', 'PHX'), ('CAMZ', 'PHZ')):
    ld('s0', c); a('vmul(s1, s0, s3)'); a('vcvt_s32_f32(s1, s1)'); a('vcvt_f32_s32(s1, s1)')
    a('vadd(s1, s1, s1)'); a('vsub(s0, s0, s1)'); a('vadd(s0, s0, s6)'); st('s0', p)

# ── per-frame ray constants. right = (fz, -fx)
ld('s0', 'DU'); a('vmul(s20, s5, s0)'); a('vmul(s21, s4, s0)'); a('vneg(s21, s21)')
ld('s0', 'U0')
fmov('s28', 's4'); mla('s28', 's5', 's0')
fmov('s29', 's5'); mls('s29', 's4', 's0')
ld('s22', 'CAMY')

# orb: oc, cc, |oc|^2, mirrored orb (y -> -y about the floor), floor-relative K
ld('s0', 'ORBX'); ld('s1', 'CAMX'); a('vsub(s16, s0, s1)')
ld('s0', 'ORBY'); a('vsub(s17, s0, s22)')
ld('s0', 'ORBZ'); ld('s1', 'CAMZ'); a('vsub(s18, s0, s1)')
dot3('s3', OC, OC); st('s3', 'OCO2'); ld('s0', 'ORBR2'); a('vsub(s19, s3, s0)')
ld('s0', 'ORBY'); a('vneg(s0, s0)'); a('vsub(s0, s0, s22)'); st('s0', 'OCMY')
dot3('s3', ('s16', 's0', 's18'), ('s16', 's0', 's18'))
ld('s1', 'ORBR2'); a('vsub(s3, s3, s1)'); st('s3', 'CCOM')
ld('s1', 'PHX'); a('vadd(s0, s16, s1)'); st('s0', 'KOX')
ld('s1', 'PHZ'); a('vadd(s0, s18, s1)'); st('s0', 'KOZ')
# defenders (ellipsoids): scaled oc, cc, mirrored, floor-relative K
for k in range(NR):
    ld('s0', f'ROX{k}'); ld('s1', 'CAMX'); a('vsub(s4, s0, s1)')
    ld('s1', 'PHX'); a('vadd(s2, s4, s1)'); st('s2', f'KRX{k}')
    ld('s1', 'IRX'); a('vmul(s4, s4, s1)'); st('s4', f'OCRX{k}')
    ld('s0', f'ROZ{k}'); ld('s1', 'CAMZ'); a('vsub(s6, s0, s1)')
    ld('s1', 'PHZ'); a('vadd(s2, s6, s1)'); st('s2', f'KRZ{k}')
    ld('s1', 'IRZ'); a('vmul(s6, s6, s1)'); st('s6', f'OCRZ{k}')
    ld('s0', 'ROY'); a('vsub(s5, s0, s22)'); ld('s1', 'IRY'); a('vmul(s5, s5, s1)'); st('s5', f'OCRY{k}')
    dot3('s3', ('s4', 's5', 's6'), ('s4', 's5', 's6')); ld('s0', 'ONE'); a('vsub(s3, s3, s0)'); st('s3', f'CCR{k}')
    ld('s0', 'ROY'); a('vneg(s0, s0)'); a('vsub(s0, s0, s22)'); ld('s1', 'IRY'); a('vmul(s7, s0, s1)')
    st('s7', f'OCRMY{k}')
    dot3('s3', ('s4', 's7', 's6'), ('s4', 's7', 's6')); ld('s0', 'ONE'); a('vsub(s3, s3, s0)'); st('s3', f'CCRM{k}')

# ── per-frame setup for row-interval culling ────────────────────────────────
# Pixel i of a row has d(i) = D0 + i S with S = (ddx, 0, ddz) for the frame and
# D0 = (dx0, v, dz0). For an object, disc(i) = q2 i^2 + 2 q1 i + q0 is quadratic in i,
# so each row solves for the pixel span that can hit; pixels outside skip the test.
ld('s0', 'IRX'); ld('s1', 'IRZ')
for g, (sx, sz, dx, dz) in (('0', ('s20', 's21', 's28', 's29')), ('1', ('s2', 's3', 's4', 's5'))):
    if g == '1':
        a('vmul(s2, s20, s0)'); a('vmul(s3, s21, s1)'); a('vmul(s4, s28, s0)'); a('vmul(s5, s29, s1)')
    st(sx, 'SX' + g); st(sz, 'SZ' + g); st(dx, 'DX' + g); st(dz, 'DZ' + g)
    a(f'vmul(s6, {dx}, {dx})'); mla('s6', dz, dz); st('s6', 'A0F' + g)
    a(f'vmul(s6, {dx}, {sx})'); mla('s6', dz, sz); st('s6', 'A1' + g)
GROUP = {o: ('1' if 'roto' in o else '0') for o in OBJS}
def obj_frame(o, ocx, ocz, ocy, cc):   # sources are ('reg', s) or ('fp', name)
    g = GROUP[o]
    def get(dst, src):
        if src[0] == 'reg': fmov(dst, src[1])
        else: ld(dst, src[1])
    get('s0', ocx); get('s1', ocz)
    ld('s2', 'SX' + g); ld('s3', 'SZ' + g); a('vmul(s4, s0, s2)'); mla('s4', 's1', 's3')     # b1
    ld('s2', 'DX' + g); ld('s3', 'DZ' + g); a('vmul(s5, s0, s2)'); mla('s5', 's1', 's3')     # bf
    st('s4', 'B1_' + o); st('s5', 'BF_' + o)
    ld('s2', 'SX' + g); ld('s3', 'SZ' + g); a('vmul(s6, s2, s2)'); mla('s6', 's3', 's3')     # A2
    get('s7', cc); st('s7', 'CC_' + o)
    get('s0', ocy); st('s0', 'OCY_' + o)
    a('vmul(s5, s4, s4)'); mls('s5', 's7', 's6'); st('s5', 'Q2_' + o)                      # q2 = b1^2 - cc A2
R = lambda s: ('reg', s)
F = lambda n: ('fp', n)
obj_frame('orb', R('s16'), R('s18'), R('s17'), R('s19'))
ld('s7', 'OCO2'); ld('s6', 'GCUT'); a('vsub(s7, s7, s6)'); st('s7', 'CC_glow')
obj_frame('glow', R('s16'), R('s18'), R('s17'), F('CC_glow'))
obj_frame('morb', R('s16'), R('s18'), F('OCMY'), F('CCOM'))
for k in range(NR):
    obj_frame(f'roto{k}', F(f'OCRX{k}'), F(f'OCRZ{k}'), F(f'OCRY{k}'), F(f'CCR{k}'))
    obj_frame(f'mroto{k}', F(f'OCRX{k}'), F(f'OCRZ{k}'), F(f'OCRMY{k}'), F(f'CCRM{k}'))

# goal posts: oc in xz, cc; pixel-column interval for the pair (disc(i) has no v term)
_pc = [0]
def post_frame(k, sx):
    ld('s0', 'GXA'); ld('s1', 'GHW'); a('vsub(s0, s0, s1)' if sx < 0 else 'vadd(s0, s0, s1)')
    ld('s2', 'CAMX'); a('vsub(s0, s0, s2)'); st('s0', f'PCX{k}')
    ld('s1', 'GOALZ'); ld('s2', 'CAMZ'); a('vsub(s1, s1, s2)'); st('s1', f'PCZ{k}')
    a('vmul(s2, s0, s0)'); mla('s2', 's1', 's1'); ld('s3', 'PR2'); a('vsub(s2, s2, s3)'); st('s2', f'PCC{k}')
    ld('s3', 'DX0'); a('vmul(s4, s0, s3)'); ld('s3', 'DZ0'); mla('s4', 's1', 's3')      # b0
    ld('s3', 'SX0'); a('vmul(s5, s0, s3)'); ld('s3', 'SZ0'); mla('s5', 's1', 's3')      # b1
    ld('s3', 'SX0'); a('vmul(s6, s3, s3)'); ld('s3', 'SZ0'); mla('s6', 's3', 's3')      # A2
    a('vmul(s7, s5, s5)'); mls('s7', 's2', 's6')                                        # q2
    ld('s3', 'A10'); a('vmul(s6, s4, s5)'); mls('s6', 's2', 's3')                      # q1
    ld('s3', 'A0F0'); a('vmul(s3, s2, s3)'); a('vmul(s5, s4, s4)'); a('vsub(s5, s5, s3)')  # q0
    n = _pc[0] = _pc[0] + 1
    cmpz('s7'); br('ge', f'PC_FULL{n}')
    a('vmul(s0, s6, s6)'); mls('s0', 's7', 's5'); cmpz('s0'); br('lt', f'PC_EMPTY{n}')
    a('vsqrt(s0, s0)'); a('vneg(s6, s6)')
    a('vadd(s1, s6, s0)'); a('vdiv(s1, s1, s7)'); a('vsub(s2, s6, s0)'); a('vdiv(s2, s2, s7)')
    jmp(f'PC_SET{n}')
    lab(f'PC_FULL{n}'); ld('s1', 'ZERO'); ld('s2', 'LASTX'); jmp(f'PC_SET{n}')
    lab(f'PC_EMPTY{n}'); ld('s1', 'BIG'); a('vneg(s2, s1)')
    lab(f'PC_SET{n}')
    if k == 0:
        fmov('s8', 's1'); fmov('s9', 's2')
    else:
        fmin('s8', 's8', 's1'); fmax('s9', 's9', 's2')
post_frame(0, -1); post_frame(1, 1)
ld('s3', 'ONE'); a('vsub(s1, s8, s3)'); a('vadd(s2, s9, s3)')
ld('s3', 'ZERO'); fmax('s1', 's1', 's3'); ld('s3', 'LASTX'); fmin('s2', 's2', 's3')
cmpf('s1', 's2'); br('gt', 'PP_EMPTY')
a('vcvt_s32_f32(s1, s1)'); a('vcvt_s32_f32(s2, s2)'); a('vmov(r0, s1)'); a('vmov(r1, s2)')
a('sub(r0, r1, r0)'); a(f'mov(r2, {RW})'); a('sub(r2, r2, r1)'); jmp('PP_SET')
lab('PP_EMPTY'); a('movwt(r2, 1000)'); a('mov(r0, 0)')
lab('PP_SET')
stri('r2', 'LO_goal'); stri('r0', 'W_goal')

# gas giant spans (sky object: oc = planet centre, independent of camera position)
obj_frame('giant', F('PCX'), F('PCZ'), F('PCY'), F('CCB'))
obj_frame('mgiant', F('PCX'), F('PCZ'), F('PCMY'), F('CCB'))
# drone: relative position, distance-scaled billboard constants, spans
for k in range(ND):
    ld('s0', f'DBX{k}'); ld('s1', 'CAMX'); a('vsub(s0, s0, s1)'); st('s0', f'DOX{k}')
    ld('s1', f'DBY{k}'); a('vsub(s1, s1, s22)'); st('s1', f'DOY{k}')
    ld('s2', f'DBZ{k}'); ld('s3', 'CAMZ'); a('vsub(s2, s2, s3)'); st('s2', f'DOZ{k}')
    ld('s3', f'DBY{k}'); a('vneg(s3, s3)'); a('vsub(s3, s3, s22)'); st('s3', f'DOMY{k}')
    a('vmul(s4, s0, s0)'); mla('s4', 's2', 's2'); st('s4', f'DL2{k}')
    a('vsqrt(s5, s4)')                                                # horizontal distance
    ld('s6', 'DSZ'); a('vmul(s6, s5, s6)'); ld('s7', 'DHW'); fmax('s6', 's6', 's7')   # half width
    a('vmul(s5, s5, s6)'); ld('s7', 'ONE'); a('vdiv(s5, s7, s5)'); st('s5', f'DKU{k}')
    ld('s7', 'HALF'); a('vmul(s5, s6, s7)')                           # half height
    ld('s7', 'ONE'); a('vdiv(s7, s7, s5)'); st('s7', f'DKV{k}')
    a('vmul(s7, s6, s6)'); mla('s7', 's5', 's5')                      # bounding radius^2
    fmov('s5', 's4'); mla('s5', 's1', 's1'); a('vsub(s5, s5, s7)'); st('s5', f'CCD{k}')
    fmov('s5', 's4'); mla('s5', 's3', 's3'); a('vsub(s5, s5, s7)'); st('s5', f'CCDM{k}')
    obj_frame(f'drone{k}', F(f'DOX{k}'), F(f'DOZ{k}'), F(f'DOY{k}'), F(f'CCD{k}'))
    obj_frame(f'mdrone{k}', F(f'DOX{k}'), F(f'DOZ{k}'), F(f'DOMY{k}'), F(f'CCDM{k}'))
# light layers: clear, then splat stars (sky layer) and sparks + their floor mirror images
a('vmov(r0, s31)')
a(f'ldr(r1, [r0, {C_STARL}])'); stri('r1', 'STARA')
a(f'ldr(r1, [r0, {C_SPKL}])'); stri('r1', 'SPKA')
for slot, words in (('STARA', RW * 60 // 4), ('SPKA', RW * RH // 4)):
    ldri('r1', slot); a('mov(r2, 0)'); a(f'movwt(r3, {words})')
    lab(f'CLR_{slot}')
    a('str(r2, [r1, 0])'); a('add(r1, 4)'); a('sub(r3, 1)'); br('ne', f'CLR_{slot}')
_ps = [0]
def project_splat(jmax, halo, slot, by_iz):
    # in: s0, s1, s2 = point relative to the camera (or a direction), s7 = intensity
    n = _ps[0] = _ps[0] + 1
    ld('s4', 'FX'); ld('s5', 'FZ')
    a('vmul(s3, s0, s4)'); mla('s3', 's2', 's5')                      # depth along view
    ld('s6', 'ZNEAR'); cmpf('s3', 's6'); br('le', f'PS_SKIP{n}')
    a('vmul(s6, s0, s5)'); mls('s6', 's2', 's4')                      # sideways
    ld('s4', 'ONE'); a('vdiv(s3, s4, s3)')
    a('vmul(s6, s6, s3)'); ld('s4', 'IDU'); a('vmul(s6, s6, s4)'); ld('s4', 'IU0'); a('vadd(s0, s6, s4)')   # column
    a('vmul(s1, s1, s3)'); ld('s4', 'IDV'); a('vmul(s1, s1, s4)'); ld('s4', 'JV0'); a('vsub(s1, s4, s1)')   # row
    if by_iz:
        a('vmul(s2, s7, s3)')
    else:
        fmov('s2', 's7')
    ld('s6', jmax); ld('s7', halo); ldri('r1', slot); a('bl(SPLAT)')
    lab(f'PS_SKIP{n}')
E('tst.w r11, #2'); br('eq', 'STARS_DONE')
E(f'addw r4, r10, #{TEX_STAR}'); a(f'mov(r5, {NSTAR})')
lab('STAR_LOOP')
a('vldr(s0, [r4, 0])'); a('vldr(s1, [r4, 4])'); a('vldr(s2, [r4, 8])'); a('vldr(s7, [r4, 12])')
project_splat('JSTAR', 'ZERO', 'STARA', False)
a('add(r4, 16)'); a('sub(r5, 1)'); br('ne', 'STAR_LOOP')
lab('STARS_DONE')
for i in range(NSP):
    E(f'addw r9, r10, #{TEX_SPARK + i * SP_STRIDE}')
    for mirror in (0, 1):
        E('vldr s7, [r9, #24]'); cmpz('s7'); br('le', f'SPS_DEAD{i}')
        ld('s6', 'SLIFE0'); a('vdiv(s7, s7, s6)'); ld('s6', 'SKB'); a('vmul(s7, s7, s6)')
        if mirror:
            ld('s6', 'SMIR'); a('vmul(s7, s7, s6)')
        E('vldr s0, [r9, #0]'); ld('s3', 'CAMX'); a('vsub(s0, s0, s3)')
        E('vldr s1, [r9, #4]')
        if mirror:
            a('vneg(s1, s1)')
        a('vsub(s1, s1, s22)')
        E('vldr s2, [r9, #8]'); ld('s3', 'CAMZ'); a('vsub(s2, s2, s3)')
        project_splat('JSPK', 'SHALO', 'SPKA', True)
    lab(f'SPS_DEAD{i}')
# drone beam: NBEAM dots from under the drone down to its target, drifting downward
E('tst.w r11, #8'); br('eq', 'BEAM_DONE')
a('vmov(r0, s31)'); a('ldr(r1, [r0, 4])'); a('vmov(s0, r1)'); a('vcvt_f32_s32(s0, s0)')
ld('s1', 'BEAMP'); a('vmul(s0, s0, s1)'); a('vcvt_s32_f32(s1, s0)'); a('vcvt_f32_s32(s1, s1)'); a('vsub(s9, s0, s1)')   # phase 0..1
for k in range(ND):
    for j in range(NBEAM):
        for mirror in (0, 1):
            ld('s1', f'DBY{k}'); ld('s2', 'BTOP'); a('vsub(s1, s1, s2)'); ld('s2', f'DTY{k}'); a('vsub(s1, s1, s2)')   # beam length
            ld('s3', 'ONE'); a('vsub(s3, s3, s9)')                                  # dots move down
            ld('s4', 'ONE'); a(f'movwt(r1, {j})'); a('vmov(s5, r1)'); a('vcvt_f32_s32(s5, s5)'); a('vadd(s3, s3, s5)')
            a(f'movwt(r1, {NBEAM})'); a('vmov(s5, r1)'); a('vcvt_f32_s32(s5, s5)'); a('vdiv(s3, s3, s5)')
            a('vmul(s1, s1, s3)'); a('vadd(s1, s1, s2)')                            # dot height
            if mirror:
                a('vneg(s1, s1)')
            a('vsub(s1, s1, s22)')
            ld('s0', f'DOX{k}'); ld('s2', f'DOZ{k}')
            ld('s7', 'BEAMI')
            if mirror:
                ld('s3', 'SMIR'); a('vmul(s7, s7, s3)')
            project_splat('JSPK', 'BEAMH', 'SPKA', False)
lab('BEAM_DONE')
# hand the frame to rt_rows: registers -> FP, FP -> FP1, publish
for n, r in (('FR16', 's16'), ('FR17', 's17'), ('FR18', 's18'), ('FR19', 's19'), ('FR20', 's20'),
             ('FR21', 's21'), ('FR22', 's22'), ('FR28', 's28'), ('FR29', 's29')):
    st(r, n)
a('vmov(r0, s31)')
a(f'movwt(r3, {BIAS})'); a('sub(r1, r7, r3)')
a(f'ldr(r2, [r0, {C_FP1}])'); a(f'movwt(r3, {FP_LEN})')
lab('FPCOPY')
a('ldr(r4, [r1, 0])'); a('str(r4, [r2, 0])'); a('add(r1, 4)'); a('add(r2, 4)'); a('sub(r3, 1)'); br('ne', 'FPCOPY')
a('ldr(r1, [r0, 4])'); a('mov(r2, 1)'); a('and_(r1, r2)')
a('lsl(r1, r1, 2)'); a('add(r1, r1, r0)'); a('ldr(r4, [r1, 24])'); a(f'str(r4, [r0, {C_RBCUR}])')
a('ldr(r1, [r0, 4])'); a('add(r1, 1)'); a(f'str(r1, [r0, {C_SETUP}])')    # frame n = FRAME + 1 is set up

HALF = 0
exec(ROWS, globals())

# ── rt_main: wait for rt_rows (core 0) to finish the odd rows, publish the frame
a('vmov(r0, s31)')
lab('WAITH')
a('ldr(r1, [r0, 0])'); a('cmp(r1, 0)'); br('ne', 'DONE')
a(f'ldr(r1, [r0, {C_HALF}])'); a(f'ldr(r2, [r0, {C_SETUP}])'); a('cmp(r1, r2)'); br('ne', 'WAITH')
a(f'str(r2, [r0, 4])')                                                   # FRAME = n
jmp('FRAME_LOOP')
exec(SUBS, globals())
lab('DONE')
E('vpop {s16-s31}')
E('pop.w {r8, r9, r10, r11}')
a('ldr(r0, [r0, 4])')
A_MAIN = A

# ── rt_rows: called from Python on the other core once per frame; returns ────
A = []
E('push.w {r8, r9, r10, r11}')
E('vpush {s16-s31}')
a('vmov(s31, r0)')
a('mov(r7, r1)'); a(f'movwt(r1, {BIAS})'); a('add(r7, r7, r1)')
E(f'ldr.w r10, [r0, #{C_TEX}]')
ldri('r11', 'FEATI')
for n, r in (('FR16', 's16'), ('FR17', 's17'), ('FR18', 's18'), ('FR19', 's19'), ('FR20', 's20'),
             ('FR21', 's21'), ('FR22', 's22'), ('FR28', 's28'), ('FR29', 's29')):
    ld(r, n)
HALF = 1
exec(ROWS, globals())
a('vmov(r0, s31)'); a(f'ldr(r1, [r0, {C_SETUP}])'); a(f'str(r1, [r0, {C_HALF}])')
jmp('RR_DONE')
exec(SUBS, globals())
lab('RR_DONE')
E('vpop {s16-s31}')
E('pop.w {r8, r9, r10, r11}')
A_ROWS = A

# ── encode E() items in one encoder call ────────────────────────────────────
ins = sorted({x[1] for x in A_MAIN + A_ROWS if isinstance(x, tuple) and x[0] == 'E'})
enc = subprocess.run(['python3', ENCODER] + ins, capture_output=True, text=True, check=True).stdout.strip().splitlines()
ENC = dict(zip(ins, [e.split('  #')[0].strip() for e in enc]))
assert len(enc) == len(ins)

INV = {'eq': 'ne', 'ne': 'eq', 'lt': 'ge', 'ge': 'lt', 'gt': 'le', 'le': 'gt',
       'hi': 'ls', 'ls': 'hi', 'mi': 'pl', 'pl': 'mi', 'cs': 'cc', 'cc': 'cs'}

def render_asm(A, fn, long_set):
    out = []
    for i, x in enumerate(A):
        if isinstance(x, str):
            out.append((None, '    ' + x))
        elif x[0] == 'E':
            note = f' ;  {x[2]}' if x[2] else ''
            out.append((None, f'    {ENC[x[1]]}  # {x[1]}{note}'))
        elif x[0] == 'H':
            out.append((None, f'    data(2, 0x{x[1]:04X})  # {x[2]}'))
        elif x[0] == 'D':
            out.append((None, f'    data(2, 0x{x[1]:04X}, 0x{x[2]:04X})  # {x[3]}'))
        else:
            _, c, l = x
            if c is None:     # unconditional: narrow b is +-2KB; bl reaches anywhere and
                              # is safe as a jump here (lr is only live inside PACK)
                out.append(((fn, i), f'    bl({l})' if (fn, i) in long_set else f'    b({l})'))
            elif (fn, i) not in long_set:
                out.append(((fn, i), f'    b{c}({l})'))
            elif NO_WIDE:     # firmware without b<cc>_w: invert over a bl jump
                out.append(((fn, i), f'    b{INV[c]}(_far{i})'))
                out.append(((fn, i), f'    bl({l})'))
                out.append(((fn, i), f'    label(_far{i})'))
            else:             # conditional: narrow +-256B, MicroPython's b<cc>_w is +-1MB
                out.append(((fn, i), f'    b{c}_w({l})'))
    return out

HEADER = open(os.path.join(HERE, 'bb_rt_header.py')).read()
FOOTER = open(os.path.join(HERE, 'bb_rt_footer.py')).read()
EXPORT = ('FEATF', 'WC', 'V0', 'CAMX', 'CAMY', 'CAMZ', 'FX', 'FZ', 'ORBX', 'ORBY', 'ORBZ', 'U0', 'DU', 'DV', 'LX',
          'ORBR2', 'IRY', 'PR2', 'KORB', 'KORBF', 'KORBH', 'KROT', 'KPOST', 'TA', 'TB', 'DT', 'HOR', 'DH',
          'OE', 'OD', 'OCORE', 'GLOW', 'OLIGHT', 'TINT', 'RIM', 'STRIPE', 'MREF',
          'POSTC', 'POSTREF', 'RSIL', 'FR0', 'FR1')

def fp_init_code():
    vals = []
    for name, n, init in FP:
        vals += list(init) if init is not None else [0.0] * n
    lines = ['FP = array.array(\'f\', [']
    for i in range(0, len(vals), 6):
        lines.append('    ' + ', '.join(repr(float(f'{v:.7g}')) for v in vals[i:i + 6]) + ',')
    lines.append('])')
    consts = [f'O_{n} = const({OFFS[n]})' for n in EXPORT]
    consts += [f'TEX_STAR = const({TEX_STAR})', f'NSTAR = const({NSTAR})', 'O_IDU = const(%d)' % OFFS['IDU'],
               'O_IU0 = const(%d)' % OFFS['IU0'], 'O_IDV = const(%d)' % OFFS['IDV'], 'O_JV0 = const(%d)' % OFFS['JV0'],
               f'TEX_SPARK = const({TEX_SPARK})', f'TEX_BAND = const({TEX_BAND})', f'TEX_RING = const({TEX_RING})',
               f'TEX_DRONE = const({TEX_DRONE})', f'TEX_FRAME = const({TEX_FRAME})', f'TEX_LEN = const({TEX_LEN})',
               f'G_RR1 = {RR1!r}', f'G_RK = {63.99 / (RR2 - RR1)!r}']
    return '\n'.join(consts) + f'\nFP_LEN = const({FP_LEN})\n' + '\n'.join(lines) + '\n'

def write(long_set):
    src = HEADER.replace('#@FP@', fp_init_code())
    linemap = {}
    for fn, AA, sig, doc in (('main', A_MAIN, 'def rt_main(r0, r1) -> int:',
                              '# r0 = CTRL (int32[]), r1 = FP (float32[]). Never returns until CTRL[0] != 0; returns frames.'),
                             ('rows', A_ROWS, 'def rt_rows(r0, r1):',
                              '# r0 = CTRL, r1 = FP1 (rt_main copies FP there each frame). Renders the odd rows, then returns.')):
        src += f'\n@micropython.asm_thumb\n{sig}\n    {doc}\n'
        lineno0 = src.count('\n') + 1
        for k, (idx, text) in enumerate(render_asm(AA, fn, long_set)):
            src += text + '\n'
            if idx is not None:
                linemap[lineno0 + k] = idx
    src += FOOTER
    open(OUT, 'w').write(src)
    return linemap

long_set = set()
for it in range(80):
    linemap = write(long_set)
    p = subprocess.run(['mpy-cross', '-march=armv7emsp', '-o', '/tmp/_rt.mpy', OUT], capture_output=True, text=True)
    if p.returncode == 0:
        break
    m = re.search(r'line (\d+)', p.stderr)
    if not ('branch not in range' in p.stderr and m and int(m.group(1)) in linemap
            and linemap[int(m.group(1))] not in long_set):
        sys.exit('compile failed:\n' + p.stderr)
    long_set.add(linemap[int(m.group(1))])
else:
    sys.exit('branch fixup did not converge')
AF = {'main': A_MAIN, 'rows': A_ROWS}
nbr = sum(1 for x in A_MAIN + A_ROWS if isinstance(x, tuple) and x[0] == 'BR')
nlong_u = sum(1 for fn, i in long_set if AF[fn][i][1] is None)
print(f'{OUT}: FP {FP_LEN} floats, {len(ins)} encoded forms, {nbr} branches '
      f'({len(long_set) - nlong_u} conditional -> _w, {nlong_u} unconditional -> bl)')
json.dump(OFFS, open('fp_offsets.json', 'w'))     # for the offline emulator harness
subprocess.run(['mpy-cross', '-march=armv7emsp', '-o', 'bb_rt.mpy', OUT], check=True)

