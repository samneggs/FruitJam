# arkanoid.py — Arkanoid for RP2350 / 320x240 DVI, dual-core
# 35 rounds loaded from /Arkanoid/levels.bin (ARK1). Sound via audio_mixer2.Mixer.
# Controls: analog X = paddle (dpad L/R fallback), UP = launch/fire/release, SELECT = quit
from dvi_rp2_hstx_frame_sync_v2 import DVI_RP2_HSTX
from gamepadfast import Gamepad
from draw_numberdvi import Draw_number
import colors as rv_colors
import gc, array, framebuf, _thread, machine, sys
from time import sleep_ms, ticks_ms, ticks_diff, sleep_us, ticks_us
from random import randint
from machine import I2C, I2S, Pin
from sys import exit


SCREEN_W  = const(320)
SCREEN_H  = const(240)
FPS_CORE0 = const(0)
FPS_CORE1 = const(1)

DEMO_MODE = const(1)           # 1 = demo mode (paddle plays itself), 0 = normal play

machine.freq(246_000_000)
machine.mem32[0x40010058] = 2 << 16   # HSTX CLK / 2
machine.mem32[0x40010054] = 1 << 11   # HSTX CLK use SYS CLK

fb = bytearray(SCREEN_W * SCREEN_H * 2)

# ── colors ───────────────────────────────────────────────────────────────────
BLACK        = const(0x0000)
DARK_RED     = const(0x6000)
LIGHT_GRAY   = const(0xAD55)
DARK_GRAY    = const(0x4208)
GREEN        = const(0x07E0)
CYAN         = const(0x07FF)
RED          = const(0xF800)
MAGENTA      = const(0xF81F)
YELLOW       = const(0xFFE0)
WHITE        = const(0xFFFF)
ORANGE       = const(0xFC80)
BLUE_BRK     = const(0x033F)   # arcade brick blue
PINK         = const(0xF98F)
SILVER       = const(0xBDF7)
GOLD         = const(0xD500)
WALL_C       = const(0x8C71)   # blue-gray wall
DOOR_C       = const(0x2124)

# ── playfield geometry ───────────────────────────────────────────────────────
PF_X0     = const(82)          # inner playfield [82, 236)
PF_X1     = const(236)
PF_W      = const(154)         # 11 cols * 14 px  (arcade playfield is 11 wide)
PF_Y0     = const(8)
WALL_L    = const(74)          # walls 8 px thick
WALL_R    = const(236)
GRID_COLS = const(11)
GRID_ROWS = const(18)
GRID_Y0   = const(26)
GRID_Y1   = const(134)         # GRID_Y0 + 18*6
BRICK_W   = const(14)
BRICK_H   = const(6)
PADDLE_Y  = const(226)
PADDLE_H  = const(5)
PAD_NORM  = const(26)
PAD_WIDE  = const(38)
BALL_SZ   = const(4)
LOSE_Y    = const(242)
DOOR1_X   = const(110)
DOOR2_X   = const(192)
DOOR_W    = const(16)

# ── brick types ──────────────────────────────────────────────────────────────
BT_GOLD   = const(10)          # 1..8 colors, 9 silver, 10 gold
BT_SILVER = const(9)

# ── ball array layout ────────────────────────────────────────────────────────
NUM_BALLS   = const(3)
BALL_STRIDE = const(8)
B_ACT   = const(0)
B_X     = const(1)             # 8.8 fixed
B_Y     = const(2)
B_VX    = const(3)
B_VY    = const(4)
B_STK   = const(5)             # stuck offset+1, 0 = free
B_STIME = const(6)             # auto-release frame

# ── paddle array layout ──────────────────────────────────────────────────────
P_X  = const(0)
P_W  = const(1)
P_FX = const(2)                # 8.8 fixed x

# ── GAME array layout ────────────────────────────────────────────────────────
GAME_EXIT  = const(0)
G_STATE    = const(1)
G_TIMER    = const(2)
G_LEVEL    = const(3)
G_LIVES    = const(4)
G_TIER     = const(5)
G_POWER    = const(6)
G_LEFT     = const(7)          # destructible bricks remaining
G_CAPCNT   = const(8)
G_CAPNEXT  = const(9)
G_SCORE    = const(10)
G_HISCORE  = const(11)
G_BREAK    = const(12)
G_FRAME    = const(13)
G_SPDT     = const(14)
G_ETIMER   = const(15)
G_NEXTLIFE = const(16)
G_RDY      = const(17)

ST_INTRO = const(0)
ST_PLAY  = const(1)
ST_CLEAR = const(2)
ST_OVER  = const(3)

PW_NONE    = const(0)
PW_LASER   = const(1)
PW_ENLARGE = const(2)
PW_CATCH   = const(3)

# capsule types: index into "LECSDBP"
CT_L = const(0) # Laser
CT_E = const(1) # Expand
CT_C = const(2) # Catch
CT_S = const(3) # Slow
CT_D = const(4) # Disrupt
CT_B = const(5) # Break
CT_P = const(6) # Player

# ── ball step event codes ────────────────────────────────────────────────────
EV_LOST = const(1)
EV_PAD  = const(2)             # >= 0x8000: brick hit, type<<9 | row<<4 | col

# ── input ────────────────────────────────────────────────────────────────────
GAMEPAD_RIGHT  = const(0b0100000)
GAMEPAD_LEFT   = const(0b0000100)
GAMEPAD_UP     = const(0b1000000)
GAMEPAD_DOWN   = const(0b0000010)
GAMEPAD_SELECT = const(0b0000001)
BTN_FIRE       = const(GAMEPAD_UP)   # remap to your pad's A button bit if wired
DEADZONE       = const(40)

I_X    = const(0)
I_EDGE = const(1)
I_PREV = const(2)
INPUT = array.array('i', [0, 0, 0])

# ── state buffers (pre-allocated, no alloc in loop) ──────────────────────────
GRID    = bytearray(GRID_COLS * GRID_ROWS)   # hp<<4 | type, 0 = empty
BALLS   = array.array('i', [0] * (NUM_BALLS * BALL_STRIDE))
PAD     = array.array('i', [0, PAD_NORM, 0])
GAME    = array.array('i', [0] * 20)
CAP     = array.array('i', [0, 0, 0, 0, 0])  # act, x8.8, y8.8, type, phase
LASERS  = array.array('i', [0] * 6)          # 2 x (act, x, y)
ENEMIES = array.array('i', [0] * 24)         # 4 x (act, x8.8, y8.8, phase, dir, type)
E_STRIDE = const(6)
E_SZ     = const(16)           # sprite is 16x16
E_ALIVE  = const(1)
E_DYING  = const(2)
# sheet layout: 16x16 RGB565, 256 words per frame, frames stacked in one column
#   cone 8 | triangle 11 | spheres 24 | cube 10 | death 6
E_NFRAMES = bytearray((8, 11, 24, 10, 6))
E_FBASE   = bytearray((0, 8, 19, 43, 53))
E_DEATH   = const(4)           # index into the two tables above
E_ASHIFT  = const(2)           # game frames per animation frame (>>2 = 15 fps)
E_DTICKS  = const(24)          # 6 death frames << E_ASHIFT
SPHERE = array.array('H',[
     16936, 52825, 42260,  8324,  #4228
    52825, 65535, 48631, 27501,
    38066, 48631, 33808, 16936,
     14823, 25388, 14823, 8324])


# brick shading tables, index = type 0..10
BRICK_BASE  = array.array('H', [0] * 12)
BRICK_LIGHT = array.array('H', [0] * 12)
BRICK_DARK  = array.array('H', [0] * 12)

# speed tiers, 8.8 px/frame magnitude
TIERS = (352, 424, 496, 568, 640)
# paddle zone deflection vectors (8 zones, never vertical, never flat)
SINT = (-224, -181, -112, -48, 48, 112, 181, 224)
COST = (124, 181, 230, 251, 251, 230, 181, 124)

CAP_TABLE   = b'\x00\x01\x02\x03\x01\x02\x03\x00\x04\x03\x02\x01\x00\x05\x04\x06'
# ── capsule sprite sheet: /Arkanoid/powerups.bin ─────────────────────────────
#   16x7 RGB565, 112 words per frame, 8 frames per capsule, one column
#   sheet order S,C,L,E,D,B,P  ->  CAP_SLOT maps CT_* to sheet block
CAP_W       = const(16)
CAP_H       = const(7)
CAP_FWORDS  = const(112)       # 16 * 7 words per frame
CAP_NFRAMES = const(8)
CAP_ASHIFT  = const(2)         # game frames per animation frame (>>2 = 15 fps)
CAP_KEY     = const(0xAD00)    # transparent color
CAP_SLOT    = b'\x02\x03\x01\x00\x04\x05\x06'   # L,E,C,S,D,B,P -> block index
SCORES      = (0, 50, 60, 70, 80, 90, 100, 110, 120, 0, 0)
WOB         = array.array('i', [0, 48, 96, 48, 0, -48, -96, -48])

# ── level file: /Arkanoid/levels.bin, ARK1 ───────────────────────────────────
#   hdr 16B: 'ARK1' ver n_levels cols rows n_colors meta_size rec_size:u16
#            data_off:u16 rsvd:u16
#   palette: n_colors * u16 RGB565      (index 0 = empty)
#   record : data_off + (n-1)*rec_size
#            meta 8B: [0]=enemy_type [1]=enemy_max(0=auto) [2]=bg [3]=flags
#            cells rows*cols bytes, row-major, 0=empty, 1..10 = brick type
LEVEL_FILE  = '/Arkanoid/levels.bin'

NUM_LEVELS  = 0                              # from header, not a const
LVL_DATOFF  = 0
LVL_RECSZ   = 0
LVL_PAL     = array.array('H', [0] * 12)
LVL_META    = bytearray(8)
LVL_CELLS   = bytearray(GRID_COLS * GRID_ROWS)
_LVL_HDR    = bytearray(16)

def read_level_header():
    global NUM_LEVELS, LVL_DATOFF, LVL_RECSZ
    with open(LEVEL_FILE, 'rb') as f:
        f.readinto(_LVL_HDR)
        h = _LVL_HDR
        if h[0] != 65 or h[1] != 82 or h[2] != 75 or h[3] != 49:
            raise ValueError('levels.bin: bad magic')
        if h[6] != GRID_COLS or h[7] != GRID_ROWS:
            raise ValueError('levels.bin: grid mismatch')
        if h[9] != len(LVL_META):
            raise ValueError('levels.bin: meta size')
        NUM_LEVELS = h[5]
        LVL_RECSZ  = h[10] | (h[11] << 8)
        LVL_DATOFF = h[12] | (h[13] << 8)
        n = h[8]
        pal = bytearray(n * 2)
        f.readinto(pal)
    for i in range(n):
        LVL_PAL[i] = pal[i * 2] | (pal[i * 2 + 1] << 8)

read_level_header()

def read_level(n):
    with open(LEVEL_FILE, 'rb') as f:
        f.seek(LVL_DATOFF + (n - 1) * LVL_RECSZ)
        f.readinto(LVL_META)
        f.readinto(LVL_CELLS)

# cells -> GRID (hp<<4 | type); returns destructible brick count
@micropython.viper
def expand_level(shp: int) -> int:
    src  = ptr8(LVL_CELLS)
    g    = ptr8(GRID)
    n    = GRID_COLS * GRID_ROWS
    left = 0
    i    = 0
    while i < n:
        t = int(src[i])
        if t == 0:
            g[i] = 0
        elif t == BT_GOLD:
            g[i] = 16 | BT_GOLD                  # hp 1, never decremented
        elif t == BT_SILVER:
            g[i] = (shp << 4) | BT_SILVER
            left += 1
        else:
            g[i] = 16 | t
            left += 1
        i += 1
    return left

# ── display / gamepad ────────────────────────────────────────────────────────
display = DVI_RP2_HSTX()
display.begin(fb, rv_colors.COLOR_MODE_BGR565, height=SCREEN_H,
              width=SCREEN_W, bytes_per_pixel=2)
gamepad = Gamepad()

fb2 = bytearray(SCREEN_W * SCREEN_H * 2)
SCREEN = framebuf.FrameBuffer(fb2, SCREEN_W, SCREEN_H, framebuf.RGB565)
draw_num = Draw_number(fb2, SCREEN_W, 2)

# ── 3x5 font, rows as 3-bit patterns ─────────────────────────────────────────
FONT = {
    '0': b'\x07\x05\x05\x05\x07', '1': b'\x02\x06\x02\x02\x07',
    '2': b'\x07\x01\x07\x04\x07', '3': b'\x07\x01\x03\x01\x07',
    '4': b'\x05\x05\x07\x01\x01', '5': b'\x07\x04\x07\x01\x07',
    '6': b'\x07\x04\x07\x05\x07', '7': b'\x07\x01\x02\x02\x02',
    '8': b'\x07\x05\x07\x05\x07', '9': b'\x07\x05\x07\x01\x07',
    'A': b'\x02\x05\x07\x05\x05', 'B': b'\x06\x05\x06\x05\x06',
    'C': b'\x07\x04\x04\x04\x07', 'D': b'\x06\x05\x05\x05\x06',
    'E': b'\x07\x04\x06\x04\x07', 'G': b'\x07\x04\x05\x05\x07',
    'H': b'\x05\x05\x07\x05\x05', 'I': b'\x07\x02\x02\x02\x07',
    'L': b'\x04\x04\x04\x04\x07', 'M': b'\x05\x07\x07\x05\x05',
    'N': b'\x06\x05\x05\x05\x05', 'O': b'\x07\x05\x05\x05\x07',
    'P': b'\x07\x05\x07\x04\x04', 'R': b'\x07\x05\x06\x05\x05',
    'S': b'\x07\x04\x07\x01\x07', 'U': b'\x05\x05\x05\x05\x07',
    'V': b'\x05\x05\x05\x05\x02', 'Y': b'\x05\x05\x02\x02\x02',
}

def draw_text(x, y, s, color, sc=1):
    for ch in s:
        if ch != ' ':
            pat = FONT[ch]
            for r in range(5):
                row = pat[r]
                if row & 4: SCREEN.fill_rect(x, y + r * sc, sc, sc, color)
                if row & 2: SCREEN.fill_rect(x + sc, y + r * sc, sc, sc, color)
                if row & 1: SCREEN.fill_rect(x + 2 * sc, y + r * sc, sc, sc, color)
        x += 4 * sc

def draw_number_right(x1, y, n, color, sc):
    # right-aligned integer, no allocation
    if n == 0:
        draw_text(x1 - 4 * sc, y, '0', color, sc)
        return
    x = x1
    while n:
        x -= 4 * sc
        d = n % 10
        n //= 10
        draw_text(x, y, '0123456789'[d], color, sc)


def load_files():
    global ENEMIES_TEXTURE, POWERUP_TEXTURES, BACKGROUND_TEXTURES
    with open('/Arkanoid/Enemies.bin', "rb") as f:
        header = f.read(4)
        ENEMIES_TEXTURE = bytearray(f.read())
    with open('/Arkanoid/powerups.bin', "rb") as f:
        header = f.read(4)
        POWERUP_TEXTURES = bytearray(f.read())
    with open('/Arkanoid/backgrounds.bin', "rb") as f:
        header = f.read(4)
        BACKGROUND_TEXTURES = bytearray(f.read())

# ── brick shade tables ───────────────────────────────────────────────────────
def _shade_init():
    # base colors come from the levels.bin palette (arcade RGB -> RGB565)
    for t in range(11):
        c = LVL_PAL[t]
        r = (c >> 11) & 31
        g = (c >> 5) & 63
        b = c & 31
        BRICK_BASE[t] = c
        lr = r + ((31 - r) * 3 >> 2)
        lg = g + ((63 - g) * 3 >> 2)
        lb = b + ((31 - b) * 3 >> 2)
        BRICK_LIGHT[t] = (lr << 11) | (lg << 5) | lb
        BRICK_DARK[t] = ((r >> 1) << 11) | ((g >> 1) << 5) | (b >> 1)
_shade_init()

# ── template asm: fast fill + framebuffer copy ───────────────────────────────
@micropython.asm_thumb
def fill_asm(r0, r1):  # (buffer_addr, 16-bit color)
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
def copy_fb(r0, r1):                # r0=source, r1=dest
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


# 5 packed word patterns per type: top, midL, midM, midR, bottom
BRICK_PAT = array.array('I', [0] * (11 * 5))

def _pat_init():
    for t in range(11):
        cb = BRICK_BASE[t]; cl = BRICK_LIGHT[t]; cd = BRICK_DARK[t]
        p = t * 5
        BRICK_PAT[p]   = cl | (cl << 16)
        BRICK_PAT[p+1] = cl | (cb << 16)
        BRICK_PAT[p+2] = cb | (cb << 16)
        BRICK_PAT[p+3] = cb | (cd << 16)
        BRICK_PAT[p+4] = cd | (cd << 16)
_pat_init()

STRIDE32 = const(SCREEN_W >> 1)     # 160
BW32     = const(BRICK_W >> 1)      # 7

@micropython.viper
def draw_bricks():
    grid = ptr8(GRID)
    fbp  = ptr32(fb2)
    pat  = ptr32(BRICK_PAT)
    row = 0
    while row < GRID_ROWS:
        rb = ((GRID_Y0 + row * BRICK_H) * SCREEN_W + PF_X0) >> 1
        go = row * GRID_COLS
        col = 0
        while col < GRID_COLS:
            v = grid[go + col]
            if v:
                p  = (v & 15) * 5
                wt = pat[p]
                wl = pat[p + 1]
                wm = pat[p + 2]
                wr = pat[p + 3]
                wb = pat[p + 4]
                d = rb + col * BW32
                fbp[d]=wt; fbp[d+1]=wt; fbp[d+2]=wt; fbp[d+3]=wt
                fbp[d+4]=wt; fbp[d+5]=wt; fbp[d+6]=wt
                d += STRIDE32
                yy = 0
                while yy < BRICK_H - 2:
                    fbp[d]=wl; fbp[d+1]=wm; fbp[d+2]=wm; fbp[d+3]=wm
                    fbp[d+4]=wm; fbp[d+5]=wm; fbp[d+6]=wr
                    d += STRIDE32
                    yy += 1
                fbp[d]=wb; fbp[d+1]=wb; fbp[d+2]=wb; fbp[d+3]=wb
                fbp[d+4]=wb; fbp[d+5]=wb; fbp[d+6]=wb
            col += 1
        row += 1


# ── viper: one ball physics step (2 substeps, axis-separated) ────────────────
@micropython.viper
def step_ball(bi: int) -> int:
    b = ptr32(BALLS)
    g = ptr8(GRID)
    pad = ptr32(PAD)
    o = bi * BALL_STRIDE
    if not b[o + B_ACT]:
        return 0
    if b[o + B_STK]:
        return 0
    k = 0
    while k < 2:
        k += 1
        # ---- X axis ----
        x = b[o + B_X] + (b[o + B_VX] >> 1)
        px = x >> 8
        if px < PF_X0:
            px = PF_X0
            x = px << 8
            b[o + B_VX] = 0 - b[o + B_VX]          # SFX: wall
        elif px + BALL_SZ > PF_X1:
            px = PF_X1 - BALL_SZ
            x = px << 8
            b[o + B_VX] = 0 - b[o + B_VX]          # SFX: wall
        b[o + B_X] = x
        py = b[o + B_Y] >> 8
        if py + BALL_SZ > GRID_Y0 and py < GRID_Y1:
            if b[o + B_VX] > 0:
                tx = px + BALL_SZ - 1
            else:
                tx = px
            c = (tx - PF_X0) // BRICK_W
            if c >= 0 and c < GRID_COLS:
                r0 = (py - GRID_Y0) // BRICK_H
                if r0 < 0:
                    r0 = 0
                r1 = (py + BALL_SZ - 1 - GRID_Y0) // BRICK_H
                if r1 >= GRID_ROWS:
                    r1 = GRID_ROWS - 1
                r = r0
                while r <= r1:
                    v = g[r * GRID_COLS + c]
                    if v:
                        t = v & 15
                        if b[o + B_VX] > 0:
                            px = PF_X0 + c * BRICK_W - BALL_SZ
                        else:
                            px = PF_X0 + (c + 1) * BRICK_W
                        b[o + B_X] = px << 8
                        b[o + B_VX] = 0 - b[o + B_VX]
                        if t != BT_GOLD:
                            hp = (v >> 4) - 1
                            if hp <= 0:
                                g[r * GRID_COLS + c] = 0
                            else:
                                g[r * GRID_COLS + c] = (hp << 4) | t
                        return 0x8000 | (t << 9) | (r << 4) | c
                    r += 1
        # ---- Y axis ----
        y = b[o + B_Y] + (b[o + B_VY] >> 1)
        py = y >> 8
        if py < PF_Y0:
            py = PF_Y0
            y = py << 8
            b[o + B_VY] = 0 - b[o + B_VY]          # SFX: wall
        b[o + B_Y] = y
        px = b[o + B_X] >> 8
        if b[o + B_VY] > 0:
            if py + BALL_SZ >= PADDLE_Y and py < PADDLE_Y + PADDLE_H:
                if px + BALL_SZ > pad[P_X] and px < pad[P_X] + pad[P_W]:
                    b[o + B_Y] = (PADDLE_Y - BALL_SZ) << 8
                    return EV_PAD
            if py > LOSE_Y:
                b[o + B_ACT] = 0
                return EV_LOST
        if py + BALL_SZ > GRID_Y0 and py < GRID_Y1:
            if b[o + B_VY] > 0:
                ty = py + BALL_SZ - 1
            else:
                ty = py
            r = (ty - GRID_Y0) // BRICK_H
            if r >= 0 and r < GRID_ROWS:
                c0 = (px - PF_X0) // BRICK_W
                c1 = (px + BALL_SZ - 1 - PF_X0) // BRICK_W
                if c0 < 0:
                    c0 = 0
                if c1 >= GRID_COLS:
                    c1 = GRID_COLS - 1
                c = c0
                while c <= c1:
                    v = g[r * GRID_COLS + c]
                    if v:
                        t = v & 15
                        if b[o + B_VY] > 0:
                            py = GRID_Y0 + r * BRICK_H - BALL_SZ
                        else:
                            py = GRID_Y0 + (r + 1) * BRICK_H
                        b[o + B_Y] = py << 8
                        b[o + B_VY] = 0 - b[o + B_VY]
                        if t != BT_GOLD:
                            hp = (v >> 4) - 1
                            if hp <= 0:
                                g[r * GRID_COLS + c] = 0
                            else:
                                g[r * GRID_COLS + c] = (hp << 4) | t
                        return 0x8000 | (t << 9) | (r << 4) | c
                    c += 1
    return 0

# ── input ────────────────────────────────────────────────────────────────────
def read_gamepad():
    gamepad.read()
    b = gamepad.buttons
    if not (b & GAMEPAD_SELECT):
        shutdown()
    x = gamepad.x                      # -512..512 analog
    if -DEADZONE < x < DEADZONE:
        x = 0
    #if not (b & GAMEPAD_LEFT):         # dpad fallback
    #    x = -400
    elif not (b & GAMEPAD_RIGHT):
        x = x // 4
    INPUT[I_X] = x
    pressed = 0 if (b & BTN_FIRE) else 1
    if pressed: # and not INPUT[I_PREV]:
        INPUT[I_EDGE] = 1
    INPUT[I_PREV] = pressed

# ── HUD (side panels persist in fb2, redraw only on change) ──────────────────
def draw_hud_score():
    SCREEN.fill_rect(2, 22, 70, 12, BLACK)
    draw_number_right(70, 24, GAME[G_SCORE], WHITE, 2)
    SCREEN.fill_rect(248, 22, 70, 12, BLACK)
    draw_number_right(316, 24, GAME[G_HISCORE], WHITE, 2)

def draw_hud_lives():
    SCREEN.fill_rect(2, 214, 70, 8, BLACK)
    n = GAME[G_LIVES]
    if n > 5:
        n = 5
    for i in range(n):
        x = 4 + i * 14
        SCREEN.fill_rect(x, 216, 12, 4, LIGHT_GRAY)
        SCREEN.fill_rect(x, 216, 2, 4, RED)
        SCREEN.fill_rect(x + 10, 216, 2, 4, RED)

def draw_hud_round():
    SCREEN.fill_rect(248, 72, 70, 12, BLACK)
    draw_number_right(300, 72, GAME[G_LEVEL], WHITE, 2)

def draw_panels():
    SCREEN.fill_rect(0, 0, WALL_L, SCREEN_H, BLACK)
    SCREEN.fill_rect(WALL_R + 8, 0, SCREEN_W - WALL_R - 8, SCREEN_H, BLACK)
    SCREEN.text('1UP',30,6,RED)
    SCREEN.text('HIGH', 282, 6, RED)
    SCREEN.text('ROUND', 272, 56, ORANGE)

    draw_hud_score()
    draw_hud_lives()
    draw_hud_round()

def draw_walls():
    # left / right rails with rivet pattern, top rail with enemy doors
    for x0 in (WALL_L, WALL_R):
        SCREEN.fill_rect(x0, 0, 8, SCREEN_H, WALL_C)
        SCREEN.vline(x0, 0, SCREEN_H, LIGHT_GRAY)
        SCREEN.vline(x0 + 7, 0, SCREEN_H, DARK_GRAY)
        y = 12
        while y < SCREEN_H:
            SCREEN.fill_rect(x0 + 2, y, 4, 2, DARK_GRAY)
            y += 16
    SCREEN.fill_rect(WALL_L, 0, PF_W + 16, 8, WALL_C)
    SCREEN.hline(WALL_L, 0, PF_W + 16, LIGHT_GRAY)
    SCREEN.hline(WALL_L, 7, PF_W + 16, DARK_GRAY)
    for dx in (DOOR1_X, DOOR2_X):
        SCREEN.fill_rect(dx, 1, DOOR_W, 6, DOOR_C)
        SCREEN.hline(dx, 6, DOOR_W, BLACK)
    if GAME[G_BREAK]:
        SCREEN.fill_rect(WALL_R, PADDLE_Y - 6, 8, 18, BLACK)
        SCREEN.hline(WALL_R, PADDLE_Y - 7, 8, YELLOW)
        SCREEN.hline(WALL_R, PADDLE_Y + 12, 8, YELLOW)

# ── level loading ────────────────────────────────────────────────────────────
def clear_objects():
    for i in range(NUM_BALLS):
        BALLS[i * BALL_STRIDE + B_ACT] = 0
    CAP[0] = 0
    LASERS[0] = 0
    LASERS[3] = 0
    for i in range(4):
        ENEMIES[i * E_STRIDE] = 0

def load_level(n):
    GAME[G_LEVEL] = n
    read_level(n)
    # arcade: silver takes 2 hits, +1 every 8 rounds
    GAME[G_LEFT] = int(expand_level(2 + (n - 1) // 8))
    GAME[G_TIER] = 0
    GAME[G_SPDT] = 0
    GAME[G_POWER] = PW_NONE
    #GAME[G_POWER] = PW_LASER ### TEST mode
    GAME[G_BREAK] = 0
    GAME[G_CAPCNT] = 0
    GAME[G_CAPNEXT] = randint(4, 8)
    GAME[G_ETIMER] = randint(400, 600)
    PAD[P_W] = PAD_NORM
    PAD[P_FX] = (PF_X0 + (PF_W - PAD_NORM) // 2) << 8
    PAD[P_X] = PAD[P_FX] >> 8
    clear_objects()
    draw_walls()
    draw_hud_round()
    GAME[G_STATE] = ST_INTRO
    GAME[G_TIMER] = 150
    gc.collect()
    snd.play(STARTSND, 220)

# ── balls ────────────────────────────────────────────────────────────────────
def spawn_stuck_ball():
    o = 0
    BALLS[o + B_ACT] = 1
    off = PAD[P_W] // 2 + 4
    BALLS[o + B_STK] = off + 1
    BALLS[o + B_STIME] = GAME[G_FRAME] + 180
    BALLS[o + B_X] = (PAD[P_X] + off) << 8
    BALLS[o + B_Y] = (PADDLE_Y - BALL_SZ) << 8
    BALLS[o + B_VX] = 0
    BALLS[o + B_VY] = 0

def set_ball_angle(o, zone):
    spd = TIERS[GAME[G_TIER]]
    BALLS[o + B_VX] = (spd * SINT[zone]) >> 8
    BALLS[o + B_VY] = -((spd * COST[zone]) >> 8)

def release_ball(o):
    bx = BALLS[o + B_X] >> 8
    zone = ((bx + 2 - PAD[P_X]) * 8) // PAD[P_W]
    if zone < 0:
        zone = 0
    if zone > 7:
        zone = 7
    BALLS[o + B_STK] = 0
    set_ball_angle(o, zone)             # SFX: launch

def rescale_speed(newtier):
    old = TIERS[GAME[G_TIER]]
    new = TIERS[newtier]
    GAME[G_TIER] = newtier
    for i in range(NUM_BALLS):
        o = i * BALL_STRIDE
        if BALLS[o + B_ACT] and not BALLS[o + B_STK]:
            BALLS[o + B_VX] = BALLS[o + B_VX] * new // old
            BALLS[o + B_VY] = BALLS[o + B_VY] * new // old

def spawn_multiball():
    src = -1
    for i in range(NUM_BALLS):
        o = i * BALL_STRIDE
        if BALLS[o + B_ACT] and not BALLS[o + B_STK]:
            src = o
            break
    if src < 0:
        return
    vx = BALLS[src + B_VX]
    vy = BALLS[src + B_VY]
    sgn = 1
    for i in range(NUM_BALLS):
        o = i * BALL_STRIDE
        if not BALLS[o + B_ACT]:
            BALLS[o + B_ACT] = 1
            BALLS[o + B_STK] = 0
            BALLS[o + B_X] = BALLS[src + B_X]
            BALLS[o + B_Y] = BALLS[src + B_Y]
            # rotate +/- 30 degrees: cos=222/256, sin=128/256
            BALLS[o + B_VX] = (vx * 222 - sgn * vy * 128) >> 8
            BALLS[o + B_VY] = (sgn * vx * 128 + vy * 222) >> 8
            sgn = -sgn

# ── scoring / capsules ───────────────────────────────────────────────────────
def add_score(n):
    GAME[G_SCORE] += n
    if GAME[G_SCORE] >= GAME[G_NEXTLIFE]:
        GAME[G_NEXTLIFE] += 60000
        GAME[G_LIVES] += 1
        snd.play(XLIFESND, vol=220)
        draw_hud_lives()
    if GAME[G_SCORE] > GAME[G_HISCORE]:
        GAME[G_HISCORE] = GAME[G_SCORE]
    draw_hud_score()

def active_ball_count():
    n = 0
    for i in range(NUM_BALLS):
        if BALLS[i * BALL_STRIDE + B_ACT]:
            n += 1
    return n

def on_brick_destroyed(r, c, t, l):
    if t == BT_SILVER:
        add_score(50 * GAME[G_LEVEL])
    else:
        add_score(SCORES[t])
    GAME[G_LEFT] -= 1
    if not l:  # no ping when destroyed by laser
        snd.play(BRICKSND, vol=220)
    # capsule drop: only one falling, never during multiball
    if CAP[0] == 0 and active_ball_count() == 1:
        GAME[G_CAPCNT] += 1
        if GAME[G_CAPCNT] >= GAME[G_CAPNEXT]:
            GAME[G_CAPCNT] = 0
            GAME[G_CAPNEXT] = randint(4, 8)
            x = PF_X0 + c * BRICK_W + ((BRICK_W - CAP_W) >> 1)
            if x < PF_X0:
                x = PF_X0
            elif x > PF_X1 - CAP_W:
                x = PF_X1 - CAP_W
            CAP[0] = 1
            CAP[1] = x << 8
            CAP[2] = (GRID_Y0 + r * BRICK_H) << 8
            CAP[3] = CAP_TABLE[randint(0, 15)]
            CAP[4] = 0

def set_pad_width(w):
    cx = PAD[P_X] + PAD[P_W] // 2
    PAD[P_W] = w
    x = cx - w // 2
    if x < PF_X0:
        x = PF_X0
    if x > PF_X1 - w:
        x = PF_X1 - w
    PAD[P_FX] = x << 8
    PAD[P_X] = x

def apply_capsule(t):
    if GAME[G_POWER] == PW_ENLARGE and t != CT_E:
        set_pad_width(PAD_NORM)
    GAME[G_POWER] = PW_NONE
    if t == CT_L:
        GAME[G_POWER] = PW_LASER
    elif t == CT_E:
        GAME[G_POWER] = PW_ENLARGE
        set_pad_width(PAD_WIDE)
        snd.play(EXPANDSND, vol=220)
    elif t == CT_C:
        GAME[G_POWER] = PW_CATCH
    elif t == CT_S:
        if GAME[G_TIER] > 0:
            rescale_speed(0)
        GAME[G_SPDT] = 0
    elif t == CT_D:
        spawn_multiball()
    elif t == CT_B:
        GAME[G_BREAK] = 1
        draw_walls()
    elif t == CT_P:
        GAME[G_LIVES] += 1
        draw_hud_lives()
        snd.play(XLIFESND, vol=220)

# ── life / level flow ────────────────────────────────────────────────────────
def life_lost():
    snd.play(BOOMSND, vol=220)
    GAME[G_LIVES] -= 1
    draw_hud_lives()
    if GAME[G_POWER] == PW_ENLARGE:
        set_pad_width(PAD_NORM)
    GAME[G_POWER] = PW_NONE
    GAME[G_TIER] = 0
    GAME[G_SPDT] = 0
    GAME[G_BREAK] = 0
    clear_objects()
    draw_walls()
    if GAME[G_LIVES] < 0:
        GAME[G_STATE] = ST_OVER
    else:
        GAME[G_STATE] = ST_INTRO
        GAME[G_TIMER] = 120

def level_clear():
    snd.play(WARPSND, vol=220)
    GAME[G_STATE] = ST_CLEAR
    GAME[G_TIMER] = 120 # 120
    GAME[G_BREAK] = 0


def reset_game():
    GAME[G_SCORE] = 0
    GAME[G_LIVES] = 2
    GAME[G_NEXTLIFE] = 20000
    draw_hud_score()
    draw_hud_lives()
    load_level(1) ##### 1

# ── demo mode AI ─────────────────────────────────────────────────────────────
#   Synthesizes INPUT[I_X] / fire edges so move_paddle() and update_balls()
#   stay untouched. Aim point on the paddle is re-rolled per volley so the
#   deflection zone changes and the ball can't settle into a fixed cycle.
D_TGT   = const(0)             # target paddle-left x, pixels
D_OFF   = const(1)             # aim: ball-center offset into the paddle
D_DESC  = const(2)             # ball was descending last frame
D_FIRE  = const(3)             # frames until the next fire press
D_DRIFT = const(4)             # frames until a forced aim re-roll
D_SERVE = const(5)             # launch spot already chosen for this serve
DEMO    = array.array('i', [0, 0, 0, 0, 0, 0])

def demo_predict(o):
    # x where the ball crosses the paddle plane, folded off the side walls
    vy = BALLS[o + B_VY]
    dy = ((PADDLE_Y - BALL_SZ) << 8) - BALLS[o + B_Y]
    if dy <= 0 or vy <= 0:
        return BALLS[o + B_X] >> 8
    x = BALLS[o + B_X] + BALLS[o + B_VX] * dy // vy - (PF_X0 << 8)
    span = (PF_X1 - BALL_SZ - PF_X0) << 8
    p = span << 1
    x %= p                                     # MicroPython % is already >= 0
    if x > span:
        x = p - x
    return (x >> 8) + PF_X0

def demo_aim():
    # centre of a random deflection zone (SINT/COST index), kept off the tips
    w = PAD[P_W]
    off = (w * ((randint(0, 7) << 1) + 1)) >> 4
    if off < 3:
        off = 3
    elif off > w - 3:
        off = w - 3
    DEMO[D_OFF] = off

def demo_steer():
    t = DEMO[D_TGT]
    lim = PF_X1 - PAD[P_W]                     # never reach the break-out door
    if t < PF_X0:
        t = PF_X0
    elif t > lim:
        t = lim
    ix = ((t << 8) - PAD[P_FX]) >> 1           # move_paddle() applies * 2
    if ix > 512:
        ix = 512
    elif ix < -512:
        ix = -512
    INPUT[I_X] = ix

def demo_move():
    d = DEMO
    best  = -1
    besty = -0x40000000
    stuck = 0
    for i in range(NUM_BALLS):
        o = i * BALL_STRIDE
        if not BALLS[o + B_ACT]:
            continue
        if BALLS[o + B_STK]:
            stuck = 1
        if BALLS[o + B_Y] > besty:             # defend the lowest ball
            besty = BALLS[o + B_Y]
            best  = o
    if best < 0:
        INPUT[I_X] = 0
        return
    if stuck:
        if not d[D_SERVE]:                     # random launch spot each serve
            d[D_SERVE] = 1
            d[D_TGT]   = randint(PF_X0 + 8, PF_X1 - PAD[P_W] - 8)
            d[D_FIRE]  = randint(30, 70)       # travel there before launching
            demo_aim()
        demo_steer()
        return
    d[D_SERVE] = 0
    desc = 1 if BALLS[best + B_VY] > 0 else 0
    d[D_DRIFT] -= 1
    if (desc and not d[D_DESC]) or d[D_DRIFT] <= 0:
        d[D_DRIFT] = randint(45, 120)          # re-roll even on long rallies
        demo_aim()
    d[D_DESC] = desc
    if desc:
        tx = demo_predict(best)
    else:
        tx = BALLS[best + B_X] >> 8            # loiter under it while it climbs
    off = d[D_OFF]
    w = PAD[P_W]
    if off > w - 3:                            # paddle may have shrunk
        off = w - 3
    d[D_TGT] = tx + (BALL_SZ >> 1) - off
    demo_steer()

def demo_fire():
    d = DEMO
    if d[D_FIRE] > 0:
        d[D_FIRE] -= 1
        return 0
    if GAME[G_STATE] == ST_OVER:
        d[D_FIRE] = 90
        return 1
    for i in range(NUM_BALLS):
        o = i * BALL_STRIDE
        if BALLS[o + B_ACT] and BALLS[o + B_STK]:
            d[D_FIRE] = randint(30, 70)        # hold, reposition, then launch
            return 1
    if GAME[G_POWER] == PW_LASER:
        d[D_FIRE] = randint(8, 22)
        return 1
    return 0

# ── per-frame updates ────────────────────────────────────────────────────────
def move_paddle():
    fx = PAD[P_FX] + INPUT[I_X] * 2              # up to ~4 px/frame
    x = fx >> 8
    lim = PF_X1 - PAD[P_W] + (10 if GAME[G_BREAK] else 0)
    if x < PF_X0:
        x = PF_X0
        fx = x << 8
    elif x > lim:
        x = lim
        fx = x << 8
    PAD[P_FX] = fx
    PAD[P_X] = x
    if GAME[G_BREAK] and x >= PF_X1 - PAD[P_W] + 9:
        add_score(10000)                         # level_clear() plays WARPSND
        level_clear()

def update_balls(fire):
    for i in range(NUM_BALLS):
        o = i * BALL_STRIDE
        if not BALLS[o + B_ACT]:
            continue
        stk = BALLS[o + B_STK]
        if stk:
            BALLS[o + B_X] = (PAD[P_X] + stk - 1) << 8
            BALLS[o + B_Y] = (PADDLE_Y - BALL_SZ) << 8
            if fire or GAME[G_FRAME] >= BALLS[o + B_STIME]:
                release_ball(o)
            continue
        ev = step_ball(i)
        if ev == EV_LOST:
            continue
        if ev == EV_PAD:
            snd.play(PADDLESND, vol=220)
            bx = BALLS[o + B_X] >> 8
            zone = ((bx + 2 - PAD[P_X]) * 8) // PAD[P_W]
            if zone < 0:
                zone = 0
            if zone > 7:
                zone = 7
            if GAME[G_POWER] == PW_CATCH:
                BALLS[o + B_STK] = (bx - PAD[P_X]) + 1
                BALLS[o + B_STIME] = GAME[G_FRAME] + 150
                BALLS[o + B_VX] = 0
                BALLS[o + B_VY] = 0
            else:
                set_ball_angle(o, zone)
        elif ev >= 0x8000:
            t = (ev >> 9) & 15
            r = (ev >> 4) & 31
            c = ev & 15
            if t != BT_GOLD and GRID[r * GRID_COLS + c] == 0:
                on_brick_destroyed(r, c, t, 0)     # plays BRICKSND
            else:
                snd.play(HARDSND, vol=220)      # gold, or silver that survived
        # ball vs enemies
        bx = BALLS[o + B_X] >> 8
        by = BALLS[o + B_Y] >> 8
        for e in range(4):
            eo = e * E_STRIDE
            if ENEMIES[eo] == E_ALIVE:
                ex = ENEMIES[eo + 1] >> 8
                ey = ENEMIES[eo + 2] >> 8
                if bx + BALL_SZ > ex and bx < ex + E_SZ and \
                   by + BALL_SZ > ey and by < ey + E_SZ:
                    kill_enemy(eo)
                    add_score(100)
                    BALLS[o + B_VY] = -BALLS[o + B_VY]
                    vx = BALLS[o + B_VX] + randint(-96, 96)
                    if -32 < vx < 32:
                        vx = 64 if vx >= 0 else -64
                    BALLS[o + B_VX] = vx
    if active_ball_count() == 0:
        life_lost()

def update_capsule():
    if not CAP[0]:
        return
    CAP[2] += 208                                # ~0.8 px/frame
    CAP[4] += 1
    y = CAP[2] >> 8
    x = CAP[1] >> 8
    if y > 236:
        CAP[0] = 0
        return
    if y + CAP_H >= PADDLE_Y and y <= PADDLE_Y + PADDLE_H:
        if x + CAP_W > PAD[P_X] and x < PAD[P_X] + PAD[P_W]:
            CAP[0] = 0
            apply_capsule(CAP[3])

def update_lasers(fire):
    if fire and GAME[G_POWER] == PW_LASER:
        for s in (0, 3):
            if not LASERS[s]:
                LASERS[s] = 1
                snd.play(LASERSND, vol=220)
                LASERS[s + 1] = PAD[P_X]
                LASERS[s + 2] = PADDLE_Y - 6
                break
    for s in (0, 3):
        if not LASERS[s]:
            continue
        LASERS[s + 2] -= 6
        y = LASERS[s + 2]
        if y < PF_Y0:
            LASERS[s] = 0
            continue
        # laser vs enemies (twin bolts at paddle edges)
        killed = 0
        for e in range(4):
            eo = e * E_STRIDE
            if ENEMIES[eo] == E_ALIVE:
                ex = ENEMIES[eo + 1] >> 8
                ey = ENEMIES[eo + 2] >> 8
                for lx in (LASERS[s + 1] + 2, LASERS[s + 1] + PAD[P_W] - 4):
                    if ex < lx < ex + E_SZ and ey < y + 6 and ey + E_SZ > y:
                        kill_enemy(eo)
                        add_score(100)
                        killed = 1
        if killed:
            LASERS[s] = 0
            continue
        # laser vs bricks
        if GRID_Y0 <= y < GRID_Y1:
            r = (y - GRID_Y0) // BRICK_H
            hit = 0
            for lx in (LASERS[s + 1] + 2, LASERS[s + 1] + PAD[P_W] - 4):
                c = (lx - PF_X0) // BRICK_W
                if 0 <= c < GRID_COLS:
                    v = GRID[r * GRID_COLS + c]
                    if v:
                        hit = 1
                        t = v & 15
                        if t != BT_GOLD:
                            hp = (v >> 4) - 1
                            if hp <= 0:
                                GRID[r * GRID_COLS + c] = 0
                                on_brick_destroyed(r, c, t, 1)
                            else:
                                GRID[r * GRID_COLS + c] = (hp << 4) | t
                                #snd.play(HARDSND, vol=220)
                        else:
                            pass
                            #snd.play(HARDSND, vol=220)   # gold
            if hit:
                LASERS[s] = 0

def kill_enemy(eo):
    ENEMIES[eo] = E_DYING
    ENEMIES[eo + 3] = 0
    snd.play(BOOMSND, vol=220)

def update_enemies():
    maxe = LVL_META[1]
    if not maxe:
        maxe = 1 + GAME[G_LEVEL] // 2            # meta 0 = auto
    if maxe > 3:
        maxe = 3
    n = 0
    for e in range(4):
        if ENEMIES[e * E_STRIDE]:
            n += 1
    GAME[G_ETIMER] -= 1
    if GAME[G_ETIMER] <= 0:
        GAME[G_ETIMER] = randint(360, 640)
        if n < maxe:
            for e in range(4):
                eo = e * E_STRIDE
                if not ENEMIES[eo]:               # SFX: door open
                    right = GAME[G_FRAME] & 1
                    ENEMIES[eo] = 1
                    ENEMIES[eo + 1] = ((DOOR2_X if right else DOOR1_X) + 3) << 8
                    ENEMIES[eo + 2] = PF_Y0 << 8
                    ENEMIES[eo + 3] = randint(0, 63)
                    ENEMIES[eo + 4] = -1 if right else 1
                    ENEMIES[eo + 5] = LVL_META[0] & 3
                    break
    for e in range(4):
        eo = e * E_STRIDE
        if not ENEMIES[eo]:
            continue
        if ENEMIES[eo] == E_DYING:                # play death animation in place
            ENEMIES[eo + 3] += 1
            if ENEMIES[eo + 3] >= E_DTICKS:
                ENEMIES[eo] = 0
            continue
        ENEMIES[eo + 3] += 1
        ENEMIES[eo + 2] += 96                     # ~0.375 px/frame down
        ENEMIES[eo + 1] += WOB[(ENEMIES[eo + 3] >> 3) & 7]
        x = ENEMIES[eo + 1] >> 8
        y = ENEMIES[eo + 2] >> 8
        if x < PF_X0:
            ENEMIES[eo + 1] = PF_X0 << 8
            ENEMIES[eo + 4] = 1
        elif x > PF_X1 - E_SZ:
            ENEMIES[eo + 1] = (PF_X1 - E_SZ) << 8
            ENEMIES[eo + 4] = -1
        # don't sink into bricks: slide sideways instead
        gy = y + E_SZ
        if GRID_Y0 <= gy < GRID_Y1:
            c = (x + 8 - PF_X0) // BRICK_W
            if 0 <= c < GRID_COLS:
                r = (gy - GRID_Y0) // BRICK_H
                if GRID[r * GRID_COLS + c]:
                    ENEMIES[eo + 2] -= 96
                    ENEMIES[eo + 1] += ENEMIES[eo + 4] * 160
        if y > 236:
            ENEMIES[eo] = 0
            continue
        # paddle destroys enemy
        if y + E_SZ >= PADDLE_Y and y <= PADDLE_Y + PADDLE_H:
            if x + E_SZ > PAD[P_X] and x < PAD[P_X] + PAD[P_W]:
                kill_enemy(eo)
                add_score(100)

def update():
    GAME[G_FRAME] += 1
    if DEMO_MODE:
        demo_move()
        fire = demo_fire()
        INPUT[I_EDGE] = 0
    else:
        fire = INPUT[I_EDGE]
        INPUT[I_EDGE] = 0
    st = GAME[G_STATE]
    move_paddle()
    if st == ST_PLAY:
        update_balls(fire)
        update_capsule()
        update_lasers(fire)
        update_enemies()
        GAME[G_SPDT] += 1
        if GAME[G_SPDT] > 700 and GAME[G_TIER] < 4:   # speed-up ~12 s
            GAME[G_SPDT] = 0
            rescale_speed(GAME[G_TIER] + 1)           # SFX: speed up
        if GAME[G_LEFT] <= 0:
            level_clear()
    elif st == ST_INTRO:
        GAME[G_TIMER] -= 1
        if GAME[G_TIMER] <= 0:
            GAME[G_STATE] = ST_PLAY
            spawn_stuck_ball()
    elif st == ST_CLEAR:
        GAME[G_TIMER] -= 1
        if GAME[G_TIMER] <= 0:
            n = GAME[G_LEVEL] + 1
            if n > NUM_LEVELS:
                n = 1                                 # loop after last round
            load_level(n)
    elif st == ST_OVER:
        if fire:
            reset_game()

# ── rendering ────────────────────────────────────────────────────────────────
def draw_paddle():
    x = PAD[P_X]
    w = PAD[P_W]
    laser = GAME[G_POWER] == PW_LASER
    SCREEN.fill_rect(x + 3, PADDLE_Y, w - 6, PADDLE_H, LIGHT_GRAY)
    SCREEN.hline(x + 3, PADDLE_Y, w - 6, WHITE)
    SCREEN.hline(x + 3, PADDLE_Y + PADDLE_H - 1, w - 6, DARK_GRAY)
    SCREEN.fill_rect(x + 3, PADDLE_Y + 1, 2, PADDLE_H - 2, CYAN)
    SCREEN.fill_rect(x + w - 5, PADDLE_Y + 1, 2, PADDLE_H - 2, CYAN)
    capc = MAGENTA if laser else RED
    SCREEN.fill_rect(x, PADDLE_Y, 3, PADDLE_H, capc)
    SCREEN.fill_rect(x + w - 3, PADDLE_Y, 3, PADDLE_H, capc)
    if laser:
        SCREEN.fill_rect(x + 2, PADDLE_Y - 2, 2, 2, RED)
        SCREEN.fill_rect(x + w - 4, PADDLE_Y - 2, 2, 2, RED)

@micropython.viper
def draw_balls():
    balls = ptr32(BALLS)
    screen = ptr16(SCREEN)
    sphere = ptr16(SPHERE)
    for i in range(NUM_BALLS):
        o = i * BALL_STRIDE
        if balls[o + B_ACT]:
            x = balls[o + B_X] >> 8
            y = balls[o + B_Y] >> 8
            for sphere_y in range(4):
                screen_addr = ((y + sphere_y) * SCREEN_W) + x
                sphere_addr = sphere_y * 4
                screen[screen_addr]   = sphere[sphere_addr]
                screen[screen_addr+1] = sphere[sphere_addr+1]
                screen[screen_addr+2] = sphere[sphere_addr+2]
                screen[screen_addr+3] = sphere[sphere_addr+3]


@micropython.viper
def draw_capsule():
    cap = ptr32(CAP)
    if cap[0] == 0:
        return
    scr  = ptr16(SCREEN)
    tex  = ptr16(POWERUP_TEXTURES)
    slot = ptr8(CAP_SLOT)
    xpos = cap[1] >> 8
    ypos = cap[2] >> 8
    frame = (cap[4] >> CAP_ASHIFT) & (CAP_NFRAMES - 1)     # continuous cycle
    src = ((int(slot[cap[3]]) * CAP_NFRAMES) + frame) * CAP_FWORDS
    rows = SCREEN_H - ypos                                 # bottom clip
    if rows > CAP_H:
        rows = CAP_H
    row = 0
    while row < rows:
        dst = (ypos + row) * SCREEN_W + xpos
        sp  = src + row * CAP_W
        col = 0
        while col < CAP_W:
            pix = int(tex[sp + col])
            if pix != CAP_KEY:
                scr[dst + col] = pix
            col += 1
        row += 1

def draw_lasersorg():
    for s in (0, 3):
        if LASERS[s]:
            y = LASERS[s + 2]
            SCREEN.fill_rect(LASERS[s + 1] + 2, y, 2, 6, RED)
            SCREEN.fill_rect(LASERS[s + 1] + PAD[P_W] - 4, y, 2, 6, RED)

@micropython.viper
def draw_lasers():
    lasers = ptr32(LASERS)
    pad = ptr32(PAD)
    for s2 in (0, 3):
        s = int(s2)
        if lasers[s]:
            y = lasers[s + 2]
            SCREEN.fill_rect(lasers[s + 1] + 2, y, 2, 6, RED)
            SCREEN.fill_rect(lasers[s + 1] + pad[P_W] - 4, y, 2, 6, RED)

@micropython.viper
def draw_enemies():
    en   = ptr32(ENEMIES)
    scr  = ptr16(SCREEN)
    tex  = ptr16(ENEMIES_TEXTURE)
    nfrm = ptr8(E_NFRAMES)
    base = ptr8(E_FBASE)
    for e in range(4):
        eo  = e * E_STRIDE
        act = en[eo]
        if act == 0:
            continue
        ph = en[eo + 3]
        if act == E_ALIVE:
            t = en[eo + 5]
            f = (ph >> E_ASHIFT) % int(nfrm[t])   # loop first->last->first
        else:
            t = E_DEATH
            f = ph >> E_ASHIFT
            if f >= int(nfrm[E_DEATH]):
                continue
        src = (int(base[t]) + f) << 8             # 256 words per frame

        x = en[eo + 1] >> 8
        y = en[eo + 2] >> 8
        c0 = 0                                    # column clip
        c1 = E_SZ
        if x < 0:
            c0 = 0 - x
        if x + E_SZ > SCREEN_W:
            c1 = SCREEN_W - x
        if c1 <= c0:
            continue
        rows = SCREEN_H - y                       # bottom clip
        if rows > E_SZ:
            rows = E_SZ
        r0 = 0
        if y < 0:
            r0 = 0 - y
        if rows <= r0:
            continue

        for r in range(r0, rows):
            d = (y + r) * SCREEN_W + x
            sp = src + (r << 4)
            for c in range(c0, c1):
                p = int(tex[sp + c])
                if p != 0xAD00:                             # 0xAD00 = transparent
                    scr[d + c] = p

def render():
    st = GAME[G_STATE]
    gfx.texture(PF_X0, PF_Y0, PF_W, SCREEN_H - PF_Y0, ((GAME[G_LEVEL]-1)%6))
    if GAME[G_BREAK]:
        SCREEN.fill_rect(WALL_R, PADDLE_Y - 6, 8, 18, BLACK)
    draw_bricks()
    draw_capsule()
    draw_lasers()
    draw_enemies()
    draw_paddle()
    draw_balls()
    if DEMO_MODE and (GAME[G_FRAME] & 32):
        SCREEN.text('DEMO', 140, 200, DARK_GRAY)
    if st == ST_INTRO:
        SCREEN.text( 'ROUND',132, 140, LIGHT_GRAY)
        draw_number_right(184, 140, GAME[G_LEVEL], LIGHT_GRAY, 2)
        if GAME[G_TIMER] < 75:
            SCREEN.text('READY', 140, 158, WHITE)
    elif st == ST_OVER:
        SCREEN.text('GAME OVER',124, 140, RED)
    SCREEN.rect(270,224,30,20,0,1)
    draw_num.draw(FPS_CORE0, 290, 224)
    draw_num.draw(FPS_CORE1, 290, 232)



# ── cores ────────────────────────────────────────────────────────────────────

FRAME_US = const(16667)

@micropython.viper
def core0():
    sleep_ms(200)
    game = ptr32(GAME)
    gc.collect()
    pot_ticks = 0
    acc = 0
    prev = int(ticks_us())
    while not game[GAME_EXIT]:
        while game[G_RDY] and not game[GAME_EXIT]:
            pass                        # last frame not yet copied out
        now = int(ticks_us())
        dt  = (now - prev) & 0x3FFFFFFF
        prev = now
        if dt > 100000:
            dt = 100000
        acc += dt
        ticks = int(ticks_ms())
        if ticks - pot_ticks > 30:
            pot_ticks = ticks
            read_gamepad()
        n = 0
        while acc >= FRAME_US and n < 4:
            acc -= FRAME_US
            update()
            n += 1
        render()
        draw_num.set(FPS_CORE0, ticks)
        draw_num.update_all()
        game[G_RDY] = 1                 # publish

@micropython.viper
def core1():
    sleep_ms(500)
    game = ptr32(GAME)
    while not game[GAME_EXIT]:
        ticks = int(ticks_ms())
        display.wait_frame()
        if game[G_RDY]:                 # skip frame if core0 ran long
            copy_fb(fb2, fb)
            game[G_RDY] = 0
        draw_num.set(FPS_CORE1, ticks)

def shutdown():
    GAME[GAME_EXIT] = True
    sleep_ms(100)
    snd.deinit()
    display.deinit()
    machine.freq(150_000_000)
    print('shutdown...')
    sleep_ms(300)
    exit()

def main():
    global gfx,snd,BOOMSND, BRICKSND,EXPANDSND,HARDSND,LASERSND,PADDLESND,WARPSND,XLIFESND,XLIFESND,STARTSND
    load_files()
    from asmgfx2 import AsmGfx
    gfx = AsmGfx(SCREEN, 320, 240,textures=BACKGROUND_TEXTURES)
    from audio_mixer2 import Mixer
    snd = Mixer()
    BOOMSND   = snd.load("/Arkanoid/boom.wav")   # enemy destroyed
    BRICKSND  = snd.load("/Arkanoid/brick.wav")  # normal brink
    EXPANDSND = snd.load("/Arkanoid/expand.wav") # paddle expand
    HARDSND   = snd.load("/Arkanoid/hard.wav")   # multi-hit or perm brick
    LASERSND  = snd.load("/Arkanoid/laser.wav")  # laser firing
    PADDLESND = snd.load("/Arkanoid/paddle.wav") # ball to paddle
    WARPSND   = snd.load("/Arkanoid/warp.wav")   # warp to next level
    XLIFESND  = snd.load("/Arkanoid/xlife.wav")  # extra life
    STARTSND  = snd.load("/Arkanoid/start.wav")  # extra life
    fill_asm(fb2, BLACK)
    draw_panels()
    GAME[G_HISCORE] = 50000
    reset_game()
    _thread.start_new_thread(core1, ())
    core0()
    
if __name__ == '__main__':
    main()