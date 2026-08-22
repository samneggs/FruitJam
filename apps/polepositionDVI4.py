# pole_position320.py — Pole Position core driving experience
# Built on template320x240.py: DVI HSTX 320x240 RGB565, core0=logic/draw, core1=blit
# Rectangle placeholder sprites. All per-frame code is Viper, writing fb2 via ptr16/ptr32.
# No sound, no boot/intermission screens. SELECT = quit.
# Controls: analog stick X = steer, RIGHT button = accelerate, DOWN = brake.

from dvi_rp2_hstx_frame_sync_v2 import DVI_RP2_HSTX
from gamepadfast import Gamepad
from draw_numberdvi import Draw_number
import colors as rv_colors
import gc, array, framebuf, _thread, machine, uctypes
from time import sleep_ms, ticks_ms
from sys import exit


SCREEN_W  = const(320)
SCREEN_H  = const(240)
CENTER_X  = const(160)
FPS_CORE0 = const(0)
FPS_CORE1 = const(1)

machine.freq(246_000_000)
machine.mem32[0x40010058] = 2 << 16   # HSTX CLK / 2
machine.mem32[0x40010054] = 1 << 11   # HSTX CLK use SYS CLK

fb = bytearray(SCREEN_W * SCREEN_H * 2)

# ── Palette (template RGB565 convention) ─────────────────────────────────────
BLACK        = const(0x0000)
RED          = const(0xF800)
YELLOW       = const(0xFFE0)
WHITE        = const(0xFFFF)
BLUE         = const(0x001F)
MAGENTA      = const(0xF81F)
CYAN         = const(0x07FF)
GREEN        = const(0x07E0)

SKY          = const(0x651F)   # light blue (flip R/B nibbles if BGR order looks off)
GRASS_LIGHT  = const(0x0560)
GRASS_DARK   = const(0x0380)
ROAD_LIGHT   = const(0x9CD3)
ROAD_DARK    = const(0x8C51)
PLAYER_COLOR = const(0xF9E7)   # PP-red player car
MTN_FAR      = const(0x6B54)   # hazy blue-gray distant peaks
MTN_NEAR     = const(0x19C5)   # dark green foothills

# ── Gamepad ──────────────────────────────────────────────────────────────────
GAMEPAD = array.array('i', [0, 0, 0, 0x7FFFFFFF])   # x, y, debounce, buttons
GAMEPAD_X        = const(0)
GAMEPAD_Y        = const(1)
GAMEPAD_DEBOUNCE = const(2)
GAMEPAD_BTN      = const(3)

GAMEPAD_RIGHT  = const(0b0100000)
GAMEPAD_LEFT   = const(0b0000100)
GAMEPAD_UP     = const(0b1000000)
GAMEPAD_DOWN   = const(0b0000010)
GAMEPAD_SELECT = const(0b0000001)

# ── Pseudo-3D geometry ───────────────────────────────────────────────────────
HORIZON   = const(100)                 # sky rows 0..HORIZON
ROWS      = const(140)                 # road rows HORIZON+1..239
ZK        = const(1048576)             # z = ZK // dy ; z_near(dy=140)=7489
FP8       = const(8)                   # player_x fixed point
FPX       = const(16)                  # road-center accumulator fixed point
STRIPE_SH = const(12)                  # stripe length = 4096 z-units
ROAD_HALF_BOT = const(140)             # road half-width at bottom row

# ── Tuning ───────────────────────────────────────────────────────────────────
MAXSPD        = const(1040) #520            # z-units per frame
ACCEL         = const(2)
BRAKE         = const(8)
DRAG          = const(1)
STEER_FP      = const(768)             # max steer, fp8 px/frame (3 px)
DEADZONE      = const(30)              # analog stick deadzone (of +/-512)
DRIFT_SH      = const(9)               # centrifugal: px -= (curve*speed)>>DRIFT_SH
CURVE_STEP    = const(8)               # curve easing per frame
OFFROAD_LIMIT = const(120)             # |px| px beyond this = on grass
OFFROAD_MAX   = const(140)
OFFROAD_DECEL = const(6)
PX_LIM        = const(43520)           # +/-170 px, fp8
CRASH_FRAMES  = const(90)
COLLIDE_LO    = const(6200)            # traffic z-window overlapping player sprite
COLLIDE_HI    = const(8600)
COLLIDE_X     = const(40)              # lateral overlap, px at bottom row
DY_CAP        = const(220)             # sprite projection cap while passing

# ── Game state ───────────────────────────────────────────────────────────────
ST_SPEED = const(0)
ST_PX    = const(1)                    # player x offset from road center, fp8 px
ST_POS   = const(2)                    # camera track position, z-units
ST_CURVE = const(3)                    # current curvature (fpx accum units)
ST_CRASH = const(4)
ST_LAP   = const(5)
ST_BG    = const(6)                    # background scroll offset, fp8 px
STATE = array.array('i', [0] * 8)

GAME = array.array('i', [0] * 10)
GAME_EXIT  = const(0)
GAME_FRAME = const(1)                  # 1 = fb2 complete, core1 may copy

# ── Track: (length, curvature) segments ──────────────────────────────────────
_SEGS = (
    (60000,    0),
    (45000,  300),
    (30000,    0),
    (50000, -340),
    (25000,    0),
    (35000,  460),
    (15000,    0),
    (35000, -300),
    (30000,  120),
    (35000,    0),
)
NSEG = len(_SEGS)
SEG_END   = array.array('i', [0] * NSEG)
SEG_CURVE = array.array('i', [0] * NSEG)
_acc = 0
for _i, (_l, _c) in enumerate(_SEGS):
    _acc += _l
    SEG_END[_i] = _acc
    SEG_CURVE[_i] = _c
TRACK_LEN = _acc                       # 360000

# ── Traffic cars: pos, lane(fp8 of half-width), speed, color ─────────────────
NUM_CARS   = const(4)
CAR_STRIDE = const(4)
CAR_POS    = const(0)
CAR_LANE   = const(1)
CAR_SPD    = const(2)
CAR_COL    = const(3)
CARS = array.array('i', [
     30000, -150, 300, BLUE,
     80000,   60, 340, YELLOW,
    140000, -50,  380, MAGENTA,
    200000,  150, 430, CYAN,
])
CAR_REL   = array.array('i', [0] * NUM_CARS)   # scratch: rel z this frame
CAR_ORDER = array.array('i', [0] * NUM_CARS)   # scratch: draw order
CAR_HALF   = const(26)                 # half-width at bottom row
CAR_HEIGHT = const(34)                 # height at bottom row

# ── Car sprites: indexed pixel art built at import, palette per car ──────────
# Index scheme: 0 transparent | 1 black | 2 tire dark | 3 tire hilite
# 4 metal gray | 5 white | 6 body dark | 7 body mid | 8 body light
# 9 tail red | 10 ground shadow | 11 carbon
TRAF_W = const(80)                     # traffic source sprite (scaled at draw)
TRAF_H = const(52)
PLY_W  = const(84)                     # player sprite (blitted 1:1)
PLY_H  = const(40)
PLY_X  = const(CENTER_X - PLY_W // 2)
PLY_Y  = const(196)

TRAF_SPR = bytearray(TRAF_W * TRAF_H)
PLY_SPR  = bytearray(PLY_W * PLY_H)
TRAF_PALS = array.array('H', [0] * (16 * NUM_CARS))   # 16 colors per car
PLY_PALS  = array.array('H', [0] * 32)                # [0:16] normal, [16:32] crash flash

def _rc(g, gw, x, y, w, h, c):         # filled rect, top-left x,y
    for yy in range(y, y + h):
        b = yy * gw
        for xx in range(x, x + w):
            g[b + xx] = c

def _hl(g, gw, x, y, w, c):            # horizontal line
    b = y * gw
    for xx in range(x, x + w):
        g[b + xx] = c

def _vl(g, gw, x, y, h, c):            # vertical line
    for yy in range(y, y + h):
        g[yy * gw + x] = c

def _mir(g, gw, gh):                   # mirror left half onto right half
    for yy in range(gh):
        b = yy * gw
        for xx in range(gw >> 1):
            g[b + gw - 1 - xx] = g[b + xx]

def _tire(g, gw, x, y, w, h):          # rounded tire w/ tread band + rim light
    _rc(g, gw, x, y + 1, w, h - 2, 1)
    _hl(g, gw, x + 2, y,         w - 4, 1)
    _hl(g, gw, x + 2, y + h - 1, w - 4, 1)
    _hl(g, gw, x + 3, y + 1, w - 6, 3)
    _rc(g, gw, x + 4, y + 3, 3, h - 6, 2)
    _vl(g, gw, x + 5, y + 4, h - 8, 3)

def _build_traffic(g):
    """Rear-view GP car. Left half drawn, mirrored. Centerline x=39/40."""
    W = TRAF_W
    _rc(g, W, 3, 50, 37, 2, 10)                     # ground shadow
    _tire(g, W, 0, 15, 18, 35)                      # rear tire y15..49
    _rc(g, W, 1, 0, 3, 15, 11)                      # wing endplate x1..3
    _vl(g, W, 1, 0, 15, 1)
    _hl(g, W, 1, 0, 3, 5)
    _vl(g, W, 3, 8, 7, 1)
    _rc(g, W, 4, 1, 36, 8, 11)                      # wing main plane y1..8
    _hl(g, W, 4, 1, 36, 5)                          # white top edge
    _rc(g, W, 4, 2, 36, 2, 7)                       # body-color livery stripe
    _hl(g, W, 4, 5, 36, 12)                         # flap gap
    _hl(g, W, 4, 8, 36, 1)                          # dark underside
    _rc(g, W, 37, 9, 3, 2, 1)                       # wing struts
    _rc(g, W, 35, 11, 5, 4, 1)                      # airbox intake ring
    _rc(g, W, 37, 12, 3, 2, 11)                     # airbox core
    _hl(g, W, 33, 15, 7, 1)                         # cowl dome shoulder
    _hl(g, W, 34, 15, 5, 6)
    _hl(g, W, 37, 15, 3, 7)
    for i in range(32):                             # cowl y16..47, half-w 9->18
        y = 16 + i
        x0 = 40 - (9 + (i * 10) // 32)
        _hl(g, W, x0, y, 1, 1)                      # black edge
        _hl(g, W, x0 + 1, y, 2, 6)                  # dark side
        _hl(g, W, x0 + 3, y, 34 - x0, 7)            # mid body to x36
        _hl(g, W, 37, y, 3, 8)                      # center highlight
    _rc(g, W, 20, 42, 4, 6, 6)                      # sidepod / floor hint
    _vl(g, W, 20, 42, 6, 1)
    _hl(g, W, 20, 42, 4, 1)
    _rc(g, W, 36, 30, 4, 4, 1)                      # exhaust
    _rc(g, W, 37, 31, 3, 2, 2)
    _rc(g, W, 38, 38, 2, 7, 9)                      # rain light
    _hl(g, W, 39, 40, 1, 5)
    _rc(g, W, 23, 44, 17, 4, 11)                    # diffuser
    for x in (25, 29, 33, 37):
        _vl(g, W, x, 44, 4, 1)
    _hl(g, W, 23, 48, 17, 1)
    _hl(g, W, 18, 24, 11, 4)                        # suspension arms
    _hl(g, W, 18, 25, 11, 3)
    _hl(g, W, 18, 40, 9, 4)
    _mir(g, W, TRAF_H)

def _build_player(g):
    """Rear-view player car, drawn 1:1. Centerline x=41/42."""
    W = PLY_W
    _rc(g, W, 3, 38, 39, 2, 10)                     # ground shadow
    _tire(g, W, 0, 10, 18, 28)                      # rear tire y10..37
    _rc(g, W, 1, 0, 3, 13, 11)                      # wing endplate
    _vl(g, W, 1, 0, 13, 1)
    _hl(g, W, 1, 0, 3, 5)
    _vl(g, W, 3, 7, 6, 1)
    _rc(g, W, 4, 0, 38, 8, 11)                      # wing plane y0..7
    _hl(g, W, 4, 0, 38, 5)
    _rc(g, W, 4, 1, 38, 2, 7)                       # livery stripe
    _hl(g, W, 4, 4, 38, 12)
    _hl(g, W, 4, 7, 38, 1)
    _rc(g, W, 39, 8, 3, 2, 1)                       # wing struts
    _hl(g, W, 39, 9, 3, 5)                          # helmet crown (tapered)
    _rc(g, W, 38, 10, 4, 2, 5)
    _hl(g, W, 38, 12, 4, 1)                         # visor band
    _hl(g, W, 38, 13, 4, 5)
    _hl(g, W, 38, 14, 4, 4)                         # chin shade
    _hl(g, W, 34, 15, 8, 1)                         # cowl dome shoulder
    _hl(g, W, 35, 15, 4, 6)
    _hl(g, W, 39, 15, 3, 7)
    for i in range(20):                             # cowl y16..35, half-w 11->19
        y = 16 + i
        x0 = 42 - (11 + (i * 9) // 20)
        _hl(g, W, x0, y, 1, 1)
        _hl(g, W, x0 + 1, y, 2, 6)
        _hl(g, W, x0 + 3, y, 36 - x0, 7)
        _hl(g, W, 39, y, 3, 8)
    _hl(g, W, 35, 14, 3, 6)                         # cockpit shoulders
    _rc(g, W, 21, 31, 4, 5, 6)                      # sidepod / floor hint
    _vl(g, W, 21, 31, 5, 1)
    _hl(g, W, 21, 31, 4, 1)
    _rc(g, W, 38, 22, 4, 4, 1)                      # exhaust
    _rc(g, W, 39, 23, 3, 2, 2)
    _rc(g, W, 40, 28, 2, 6, 9)                      # rain light
    _hl(g, W, 41, 30, 1, 5)
    _rc(g, W, 25, 32, 17, 4, 11)                    # diffuser
    for x in (27, 31, 35, 39):
        _vl(g, W, x, 32, 4, 1)
    _hl(g, W, 25, 36, 17, 1)
    _hl(g, W, 18, 18, 12, 4)                        # suspension arms
    _hl(g, W, 18, 19, 12, 3)
    _hl(g, W, 18, 30, 10, 4)
    _mir(g, W, PLY_H)

def _fill_pal(pals, off, base):
    """entry 0 stays 0 (transparent key); 6/7/8 = shades of base color"""
    r = (base >> 11) & 31
    g = (base >> 5) & 63
    b = base & 31
    fixed = (0, 0x0000, 0x2965, 0x5AEB, 0x94B2, 0xFFFF, 0, 0, 0,
             0xF800, 0x10A2, 0x39E7, 0x630C, 0, 0, 0)
    for k in range(16):
        pals[off + k] = fixed[k]
    pals[off + 6] = ((r * 5 // 8) << 11) | ((g * 5 // 8) << 5) | (b * 5 // 8)
    pals[off + 7] = base
    pals[off + 8] = ((r + (31 - r) * 2 // 5) << 11) \
                  | ((g + (63 - g) * 2 // 5) << 5) | (b + (31 - b) * 2 // 5)

_build_traffic(TRAF_SPR)
_build_player(PLY_SPR)
for _i in range(NUM_CARS):
    _fill_pal(TRAF_PALS, _i * 16, CARS[_i * CAR_STRIDE + CAR_COL])
_fill_pal(PLY_PALS, 0, PLAYER_COLOR)
_fill_pal(PLY_PALS, 16, YELLOW)        # crash flash palette

# ── Roadside billboards: 3 styles, projected with the road ──────────────────
# Sign palette: 0 trans | 1 black | 2 white | 3 red | 4 yellow | 5 blue
# 6 post gray | 7 post dark | 8 green | 9 shadow
SIGN_W      = const(72)                # source sprite size
SIGN_H      = const(60)
SIGN_HALF   = const(38)                # half-width at bottom row (dy=ROWS)
SIGN_HEIGHT = const(63)                # height at bottom row (keeps 72:60 aspect)
SIGN_DY_CAP = const(240)
SIGN_MIN    = const(3800)              # cull window (near)
SIGN_LANE   = const(340)               # ~1.33x road half-width, fp8
NSIGNS      = const(6) #24               # every 15000 z, alternating sides
SIGN_STRIDE = const(2)                 # pos, lane(signed fp8)
SIGN_SZ     = const(SIGN_W * SIGN_H)   # bytes per style in SIGN_SPR
SIGN_SPR  = bytearray(SIGN_W * SIGN_H * 3)
SIGN_PAL  = array.array('H', [0, 0x0000, 0xFFFF, 0xF800, 0xFFE0, 0x001F,
                              0x8410, 0x4208, 0x07E0, 0x10A2, 0, 0, 0, 0, 0, 0])
SIGNS = array.array('i', [0] * (NSIGNS * SIGN_STRIDE))

_LFONT = {
    'P': (7,5,7,4,4), 'I': (7,2,2,2,7), 'C': (7,4,4,4,7), 'O': (7,5,5,5,7),
    'T': (7,2,2,2,2), 'U': (5,5,5,5,7), 'R': (7,5,7,6,5), 'B': (6,5,6,5,6),
}

def _text(g, gw, off, x, y, s, sc, c):
    for n in range(len(s)):
        rows = _LFONT[s[n]]
        cx = x + n * 4 * sc
        for r in range(5):
            bits = rows[r]
            for col in range(3):
                if bits & (4 >> col):
                    for yy in range(y + r * sc, y + r * sc + sc):
                        b = off + yy * gw
                        for xx in range(cx + col * sc, cx + col * sc + sc):
                            g[b + xx] = c

def _sign_frame(g, off, panel_c, border_c):
    W = SIGN_W
    for yy in range(36):                           # outline / border / face
        b = off + yy * W
        for xx in range(W):
            edge = yy < 1 or yy > 34 or xx < 1 or xx > 70
            inner = 3 <= yy <= 32 and 3 <= xx <= 68
            g[b + xx] = 1 if edge else (panel_c if inner else border_c)
    for px in (14, 52):                            # posts y36..57
        for yy in range(36, 58):
            b = off + yy * W
            for xx in range(px, px + 6):
                g[b + xx] = 7 if (xx == px or xx == px + 5) else 6
    for yy in (58, 59):                            # ground shadow
        b = off + yy * W
        for xx in range(8, 64):
            g[b + xx] = 9

def _build_signs(g):
    sz = SIGN_W * SIGN_H
    _sign_frame(g, 0, 3, 2)                        # style 0: PICO white on red
    _text(g, SIGN_W, 0, (SIGN_W - (4*4*4 - 4)) // 2, 8, 'PICO', 4, 2)
    _sign_frame(g, sz, 5, 2)                       # style 1: TURBO yellow on blue
    _text(g, SIGN_W, sz, (SIGN_W - (5*4*3 - 3)) // 2, 10, 'TURBO', 3, 4)
    _sign_frame(g, sz * 2, 2, 3)                   # style 2: checkered
    for cy in range(5):
        for cx in range(11):
            if (cx + cy) & 1:
                for yy in range(3 + cy * 6, 9 + cy * 6):
                    b = sz * 2 + yy * SIGN_W
                    for xx in range(3 + cx * 6, 9 + cx * 6):
                        g[b + xx] = 1

_build_signs(SIGN_SPR)
for _i in range(NSIGNS):               # place: every 15000 z, alternate sides
    SIGNS[_i * SIGN_STRIDE] = _i * (TRACK_LEN // NSIGNS) + 7000
    SIGNS[_i * SIGN_STRIDE + 1] = SIGN_LANE if (_i & 1) else -SIGN_LANE
del _rc, _hl, _vl, _mir, _tire, _build_traffic, _build_player, _fill_pal
del _text, _sign_frame, _build_signs, _LFONT

# ── Row lookup tables ────────────────────────────────────────────────────────
ZMAP = array.array('i', [0] * SCREEN_H)   # world z per row
WMAP = array.array('i', [0] * SCREEN_H)   # road half-width per row
ROWX = array.array('i', [0] * SCREEN_H)   # road center per row (filled each frame)
for _y in range(HORIZON + 1, SCREEN_H):
    _dy = _y - HORIZON
    ZMAP[_y] = ZK // _dy
    WMAP[_y] = ROAD_HALF_BOT * _dy // ROWS

# ── Scrolling background skyline ─────────────────────────────────────────────
# Two parallax layers of mountains above the horizon. Scroll offset is driven
# by the SAME eased curvature as the road (curve * speed), so scenery pans
# left through right turns and right through left turns — it stays anchored
# to the world instead of appearing randomly.
BG_SIZE   = const(512)                 # profile length, power of 2 for wrap
BG_MASK   = const(511)
BG_SH     = const(9)                   # scroll rate: (curve*speed)>>BG_SH fp8/frame
BG_WRAP   = const((BG_SIZE << FP8) - 1)
SNOW_Y    = const(30)                  # far-peak height above which caps are snow

BG_FAR  = bytearray(BG_SIZE)           # column heights, px above horizon
BG_NEAR = bytearray(BG_SIZE)

def _tri(x, period, amp, phase):       # seamless triangle wave (period | 512)
    p = (x + phase) % period
    h = period >> 1
    v = p if p < h else period - p
    return v * amp // h

for _x in range(BG_SIZE):
    _hf = 4 + _tri(_x, 256, 20, 0) + _tri(_x, 128, 12, 41) + _tri(_x, 64, 6, 90)
    _hn = 3 + _tri(_x, 128, 9, 17) + _tri(_x, 64, 6, 5) + _tri(_x, 32, 3, 11)
    BG_FAR[_x]  = _hf
    BG_NEAR[_x] = _hn
del _tri

# ── 3x5 digit font (3 bits per row, msb = left) ──────────────────────────────
FONT = bytearray([
    7,5,5,5,7,  2,6,2,2,7,  7,1,7,4,7,  7,1,7,1,7,  5,5,7,1,1,
    7,4,7,1,7,  7,4,7,5,7,  7,1,1,1,1,  7,5,7,5,7,  7,5,7,1,7,
])

display = DVI_RP2_HSTX()
fb2 = bytearray(SCREEN_W * SCREEN_H * 2)
display.begin(
    fb,
    rv_colors.COLOR_MODE_BGR565,
    height=SCREEN_H,
    width=SCREEN_W,
    bytes_per_pixel=2,
)
FB2_ADDR = uctypes.addressof(fb2)

gamepad = Gamepad()
draw_num = Draw_number(fb2, SCREEN_W, 2)


# ── Fast screen fill (from template) ─────────────────────────────────────────
@micropython.asm_thumb
def fill_asm(r0, r1):  # (buffer_addr, 16-bit_color)
    mov(r3, r1)
    lsl(r2, r1, 16)
    orr(r3, r2)
    mov(r1, r0)
    mov(r2, r3)
    mov(r4, r3)
    mov(r5, r3)
    mov(r6, r3)
    mov(r7, r3)
    movwt(r0, (SCREEN_W * SCREEN_H * 2) // (192))
    label(LOOP)
    data(2, 0b11000_001_11111100)
    data(2, 0b11000_001_11111100)
    data(2, 0b11000_001_11111100)
    data(2, 0b11000_001_11111100)
    data(2, 0b11000_001_11111100)
    data(2, 0b11000_001_11111100)
    data(2, 0b11000_001_11111100)
    data(2, 0b11000_001_11111100)
    sub(r0, 1)
    bne(LOOP)

@micropython.asm_thumb
def copy_fb(r0, r1):                # r0=source, r1=dest (from template)
    movwt(r2, 4800)
    label(COPY_LOOP)
    ldr(r3, [r0, 0])
    ldr(r4, [r0, 4])
    ldr(r5, [r0, 8])
    ldr(r6, [r0, 12])
    ldr(r7, [r0, 16])
    str(r3, [r1, 0])
    str(r4, [r1, 4])
    str(r5, [r1, 8])
    str(r6, [r1, 12])
    str(r7, [r1, 16])
    ldr(r3, [r0, 20])
    ldr(r4, [r0, 24])
    ldr(r5, [r0, 28])
    str(r3, [r1, 20])
    str(r4, [r1, 24])
    str(r5, [r1, 28])
    add(r0, 32)
    add(r1, 32)
    sub(r2, 1)
    bne(COPY_LOOP)


# ── Background: two-layer parallax skyline, columns down to the horizon ──────
# Near layer scrolls at full offset, far layer at half — sky is already filled
# by fill_asm, so only mountain pixels are written. Far layer skips whatever
# the near silhouette will cover, so every pixel is written at most once.
@micropython.viper
def render_background():
    st = ptr32(STATE)
    fbp = ptr16(fb2)
    far = ptr8(BG_FAR)
    near = ptr8(BG_NEAR)
    off = st[ST_BG] >> FP8             # 0..511 (accumulator kept masked)
    off_far = off >> 1
    snow = HORIZON + 1 - SNOW_Y        # screen row of the snowline
    x = 0
    while x < SCREEN_W:
        hn = near[(x + off) & BG_MASK]
        hf = far[(x + off_far) & BG_MASK]
        if hf > hn:                    # far peaks visible above near hills
            y = HORIZON + 1 - hf
            stop = HORIZON + 1 - hn
            p = y * SCREEN_W + x
            while y < stop:
                fbp[p] = WHITE if y < snow else MTN_FAR
                p += SCREEN_W
                y += 1
        y = HORIZON + 1 - hn           # near hills meet the road at HORIZON
        p = y * SCREEN_W + x
        while y <= HORIZON:
            fbp[p] = MTN_NEAR
            p += SCREEN_W
            y += 1
        x += 1


# ── Road renderer: bottom-up curve accumulation, fills ROWX for sprites ──────
# Each row is one continuous left-to-right sweep over monotone clamped
# boundaries — every pixel 0..319 written exactly once, no gaps possible.
@micropython.viper
def render_road():
    st = ptr32(STATE)
    zmap = ptr32(ZMAP)
    wmap = ptr32(WMAP)
    rowx = ptr32(ROWX)
    fbp = ptr16(fb2)
    pos = st[ST_POS]
    curve = st[ST_CURVE]
    x_fp = (CENTER_X << FPX) - (st[ST_PX] << (FPX - FP8))
    dx_fp = 0
    y = SCREEN_H - 1
    base = (SCREEN_H - 1) * SCREEN_W
    while y > HORIZON:
        xc = x_fp >> FPX
        rowx[y] = xc
        w = wmap[y]
        rw = (w >> 3) + 2
        sel = ((zmap[y] + pos) >> STRIPE_SH) & 1
        if sel:
            gcol = GRASS_LIGHT
            bcol = RED
            dcol = ROAD_LIGHT
        else:
            gcol = GRASS_DARK
            bcol = WHITE
            dcol = ROAD_DARK
        b1 = xc - w - rw               # grass|rumble
        b2 = xc - w                    # rumble|road
        b3 = xc + w                    # road|rumble
        b4 = xc + w + rw               # rumble|grass
        if b1 < 0:
            b1 = 0
        if b1 > SCREEN_W:
            b1 = SCREEN_W
        if b2 < b1:
            b2 = b1
        if b2 > SCREEN_W:
            b2 = SCREEN_W
        if b3 < b2:
            b3 = b2
        if b3 > SCREEN_W:
            b3 = SCREEN_W
        if b4 < b3:
            b4 = b3
        if b4 > SCREEN_W:
            b4 = SCREEN_W
        x = 0
        while x < b1:
            fbp[base + x] = gcol
            x += 1
        while x < b2:
            fbp[base + x] = bcol
            x += 1
        while x < b3:
            fbp[base + x] = dcol
            x += 1
        while x < b4:
            fbp[base + x] = bcol
            x += 1
        while x < SCREEN_W:
            fbp[base + x] = gcol
            x += 1
        if not sel:                    # dashed center line, clamped to road
            cl = (w >> 4) + 1
            x = xc - cl
            if x < b2:
                x = b2
            e = xc + cl
            if e > b3:
                e = b3
            while x < e:
                fbp[base + x] = WHITE
                x += 1
        x_fp += dx_fp
        dx_fp += curve
        y -= 1
        base -= SCREEN_W


# ── Billboards: descending-rel order via rotation (positions are sorted) ────
@micropython.viper
def render_signs():
    st = ptr32(STATE)
    signs = ptr32(SIGNS)
    rowx = ptr32(ROWX)
    wmap = ptr32(WMAP)
    fbp = ptr16(fb2)
    spr = ptr8(SIGN_SPR)
    pal = ptr16(SIGN_PAL)
    tl = int(TRACK_LEN)
    pos = st[ST_POS]
    first = 0                          # first sign ahead of camera
    while first < NSIGNS:
        if signs[first * SIGN_STRIDE] > pos:
            break
        first += 1
    j = 0
    while j < NSIGNS:                  # idx walks backwards -> rel descends
        idx = first - 1 - j
        if idx < 0:
            idx += NSIGNS
        j += 1
        rel = signs[idx * SIGN_STRIDE] - pos
        if rel < 0:
            rel += tl
        if rel >= ZK:
            continue
        if rel < SIGN_MIN:
            break                      # everything after is even nearer
        dy = ZK // rel
        if dy > SIGN_DY_CAP:
            dy = SIGN_DY_CAP
        ybase = HORIZON + dy
        hw = (SIGN_HALF * dy) // ROWS
        hh = (SIGN_HEIGHT * dy) // ROWS
        yt = ybase - hh
        if hw > 0:
            if yt <= SCREEN_H - 1:
                ylook = ybase
                if ylook > SCREEN_H - 1:
                    ylook = SCREEN_H - 1
                sx = rowx[ylook] + ((signs[idx * SIGN_STRIDE + 1] * wmap[ylook]) >> 8)
                soff = (idx % 3) * SIGN_SZ         # style cycles 0,1,2
                sxstep = (SIGN_W << 12) // (hw * 2)
                systep = (SIGN_H << 12) // (hh + 1)
                yl = ybase
                if yl > SCREEN_H - 1:
                    yl = SCREEN_H - 1
                x0 = sx - hw
                x1 = sx + hw
                sxbase = 0
                if x0 < 0:
                    sxbase = (0 - x0) * sxstep
                    x0 = 0
                if x1 > SCREEN_W:
                    x1 = SCREEN_W
                yy = yt
                syfp = 0
                row = yt * SCREEN_W
                while yy <= yl:
                    srow = soff + (syfp >> 12) * SIGN_W
                    x = x0
                    sxfp = sxbase
                    while x < x1:
                        ci = spr[srow + (sxfp >> 12)]
                        if ci:
                            fbp[row + x] = pal[ci]
                        x += 1
                        sxfp += sxstep
                    syfp += systep
                    row += SCREEN_W
                    yy += 1


# ── Traffic sprites: far-to-near, nearest-neighbor scaled from 80x52 source ──
@micropython.viper
def render_cars():
    st = ptr32(STATE)
    cars = ptr32(CARS)
    rowx = ptr32(ROWX)
    wmap = ptr32(WMAP)
    crel = ptr32(CAR_REL)
    ordr = ptr32(CAR_ORDER)
    fbp = ptr16(fb2)
    spr = ptr8(TRAF_SPR)
    pals = ptr16(TRAF_PALS)
    tl = int(TRACK_LEN)
    pos = st[ST_POS]
    i = 0
    while i < NUM_CARS:
        rel = cars[i * CAR_STRIDE + CAR_POS] - pos
        if rel < 0:
            rel += tl
        crel[i] = rel
        ordr[i] = i
        i += 1
    i = 0                              # bubble sort indices, farthest first
    while i < NUM_CARS - 1:
        j = 0
        while j < NUM_CARS - 1 - i:
            if crel[ordr[j]] < crel[ordr[j + 1]]:
                t = ordr[j]
                ordr[j] = ordr[j + 1]
                ordr[j + 1] = t
            j += 1
        i += 1
    k = 0
    while k < NUM_CARS:
        idx = ordr[k]
        rel = crel[idx]
        k += 1
        if rel >= 4700:
            if rel < ZK:
                dy = ZK // rel
                if dy > DY_CAP:
                    dy = DY_CAP
                ybase = HORIZON + dy           # true baseline, may be > 239
                hw = (CAR_HALF * dy) // ROWS
                hh = (CAR_HEIGHT * dy) // ROWS
                yt = ybase - hh
                if hw > 0:
                    if yt <= SCREEN_H - 1:
                        ylook = ybase          # table row: clamp to last row
                        if ylook > SCREEN_H - 1:
                            ylook = SCREEN_H - 1
                        sx = rowx[ylook] + ((cars[idx * CAR_STRIDE + CAR_LANE] * wmap[ylook]) >> 8)
                        pal = idx << 4         # this car's palette base
                        sxstep = (TRAF_W << 12) // (hw * 2)
                        systep = (TRAF_H << 12) // (hh + 1)   # yt..ybase = hh+1 rows
                        yl = ybase
                        if yl > SCREEN_H - 1:
                            yl = SCREEN_H - 1  # clip while sliding off bottom
                        x0 = sx - hw
                        x1 = sx + hw
                        sxbase = 0
                        if x0 < 0:
                            sxbase = (0 - x0) * sxstep
                            x0 = 0
                        if x1 > SCREEN_W:
                            x1 = SCREEN_W
                        yy = yt
                        syfp = 0
                        row = yt * SCREEN_W
                        while yy <= yl:
                            srow = (syfp >> 12) * TRAF_W
                            x = x0
                            sxfp = sxbase
                            while x < x1:
                                ci = spr[srow + (sxfp >> 12)]
                                if ci:
                                    fbp[row + x] = pals[pal + ci]
                                x += 1
                                sxfp += sxstep
                            syfp += systep
                            row += SCREEN_W
                            yy += 1


# ── Player car: 84x40 sprite, 1:1 blit, palette-swap flash on crash ──────────
@micropython.viper
def render_player():
    st = ptr32(STATE)
    fbp = ptr16(fb2)
    spr = ptr8(PLY_SPR)
    pals = ptr16(PLY_PALS)
    pal = 0
    crash = st[ST_CRASH]
    if crash:
        if (crash >> 2) & 1:
            pal = 16                   # yellow-body flash palette
    si = 0
    row = PLY_Y * SCREEN_W + PLY_X
    y = 0
    while y < PLY_H:
        x = 0
        while x < PLY_W:
            ci = spr[si + x]
            if ci:
                fbp[row + x] = pals[pal + ci]
            x += 1
        si += PLY_W
        row += SCREEN_W
        y += 1


# ── HUD digits: 3x5 font at 2x, right-padded fixed width ─────────────────────
@micropython.viper
def draw_number(val: int, x: int, y: int, ndig: int, c: int):
    fa = int(FB2_ADDR)
    f = ptr8(FONT)
    d = ndig - 1
    while d >= 0:
        digit = val % 10
        val //= 10
        dx = x + d * 8
        r = 0
        while r < 5:
            bits = f[digit * 5 + r]
            p = ptr16(fa + (((y + r * 2) * SCREEN_W + dx) << 1))
            col = 0
            while col < 3:
                if bits & (4 >> col):
                    p[col * 2] = c
                    p[col * 2 + 1] = c
                    p[col * 2 + SCREEN_W] = c
                    p[col * 2 + SCREEN_W + 1] = c
                col += 1
            r += 1
        d -= 1


# ── Game logic ───────────────────────────────────────────────────────────────
@micropython.viper
def update():
    st = ptr32(STATE)
    gp = ptr32(GAMEPAD)
    cars = ptr32(CARS)
    se = ptr32(SEG_END)
    sc = ptr32(SEG_CURVE)
    tl = int(TRACK_LEN)
    btn = gp[GAMEPAD_BTN]
    speed = st[ST_SPEED]
    px = st[ST_PX]
    pos = st[ST_POS]
    crash = st[ST_CRASH]

    if crash:
        st[ST_CRASH] = crash - 1
        speed = 0
    else:
        if not (btn & GAMEPAD_RIGHT):     # RIGHT button = accelerate
            speed += ACCEL
        elif not (btn & GAMEPAD_DOWN):    # DOWN = brake
            speed -= BRAKE
        else:
            speed -= DRAG
        if speed < 0:
            speed = 0
        if speed > MAXSPD:
            speed = MAXSPD
        jx = gp[GAMEPAD_X]                # analog steer, -512..+512
        ajx = jx if jx >= 0 else -jx
        if ajx < DEADZONE:
            jx = 0
        steer = (STEER_FP * speed) // MAXSPD
        px += (jx * steer) >> 9           # full deflection = full steer

    i = 0                              # current segment -> target curvature
    while se[i] <= pos:
        i += 1
    target = sc[i]
    cur = st[ST_CURVE]
    if cur < target:
        cur += CURVE_STEP
        if cur > target:
            cur = target
    elif cur > target:
        cur -= CURVE_STEP
        if cur < target:
            cur = target
    st[ST_CURVE] = cur

    # pan skyline with the road's eased curvature: right turn -> scenery left
    st[ST_BG] = (st[ST_BG] + ((cur * speed) >> BG_SH)) & BG_WRAP

    px -= (cur * speed) >> DRIFT_SH    # centrifugal drift to outside

    apx = px if px >= 0 else -px       # off-road slowdown
    if (apx >> FP8) > OFFROAD_LIMIT:
        if speed > OFFROAD_MAX:
            speed -= OFFROAD_DECEL
    if px > PX_LIM:
        px = PX_LIM
    if px < -PX_LIM:
        px = -PX_LIM

    pos += speed
    if pos >= tl:
        pos -= tl
        st[ST_LAP] = st[ST_LAP] + 1
    st[ST_POS] = pos
    st[ST_SPEED] = speed
    st[ST_PX] = px

    i = 0                              # traffic movement + collision
    while i < NUM_CARS:
        cp = cars[i * CAR_STRIDE + CAR_POS] + cars[i * CAR_STRIDE + CAR_SPD]
        if cp >= tl:
            cp -= tl
        cars[i * CAR_STRIDE + CAR_POS] = cp
        if crash == 0:
            rel = cp - pos
            if rel < 0:
                rel += tl
            if COLLIDE_LO < rel:
                if rel < COLLIDE_HI:
                    dxl = ((cars[i * CAR_STRIDE + CAR_LANE] * ROAD_HALF_BOT) >> 8) - (px >> FP8)
                    if dxl < 0:
                        dxl = -dxl
                    if dxl < COLLIDE_X:
                        st[ST_CRASH] = CRASH_FRAMES
                        st[ST_SPEED] = 0
        i += 1


@micropython.viper
def read_gamepad():
    gpad = ptr32(GAMEPAD)
    gamepad.read()
    buttons = int(gamepad.buttons)
    gpad[GAMEPAD_BTN] = buttons
    gpad[GAMEPAD_X] = int(gamepad.x)   # analog steer, -512..+512
    if not (buttons & GAMEPAD_SELECT):
        shutdown()


@micropython.viper
def draw():
    display.wait_frame()
    fill_asm(fb2, SKY)
    render_background()
    render_road()
    render_signs()
    render_cars()
    render_player()
    st = ptr32(STATE)
    draw_number((st[ST_SPEED] * 250) // MAXSPD, 8, 8, 3, WHITE)   # speed
    draw_number(st[ST_POS] >> 8, 8, 22, 4, WHITE)                 # distance
    draw_number(st[ST_LAP], 8, 36, 2, YELLOW)                     # laps
    draw_num.draw(FPS_CORE0, 290, 10)
    draw_num.draw(FPS_CORE1, 290, 20)


@micropython.viper
def core0():
    sleep_ms(200)
    game = ptr32(GAME)
    gc.collect()
    pot_ticks = 0
    while not game[GAME_EXIT]:
        ticks = int(ticks_ms())
        if ticks - pot_ticks > 30:
            pot_ticks = ticks
            read_gamepad()
        update()
        draw_num.update_all()
        while game[GAME_FRAME]:        # core1 still copying previous frame
            pass
        draw()
        game[GAME_FRAME] = 1           # frame complete, release to core1
        draw_num.set(FPS_CORE0, ticks)
    game[GAME_EXIT] = 1
    print('core0 done')


@micropython.viper
def core1():
    sleep_ms(500)
    game = ptr32(GAME)
    while not game[GAME_EXIT]:
        if game[GAME_FRAME]:           # one copy per completed frame
            ticks = int(ticks_ms())
            copy_fb(fb2, fb)
            game[GAME_FRAME] = 0
            draw_num.set(FPS_CORE1, ticks)
    print('core1 done')


def shutdown():
    GAME[GAME_EXIT] = 1
    sleep_ms(100)
    display.deinit()
    machine.freq(150_000_000)
    print('shutdown...')
    sleep_ms(300)
    exit()


def main():
    _thread.start_new_thread(core1, ())
    core0()
    
if __name__ == '__main__':
    main()