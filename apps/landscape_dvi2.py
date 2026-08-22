# Voxel-space column renderer (Comanche-style)
# Target: RP2350 / MicroPython  320x240  RGB565
#
# Casts one ray per screen column (320 rays). Each column marches near→far,
# painting terrain strips upward and filling remaining rows with sky.
# Runtime rendering uses integer fixed-point arithmetic — no floats in the
# hot path.
#
# Camera yaw: CAM_YAW holds a high-resolution angle index into SIN_LUT /
# COS_LUT. TRIG_SIZE controls angular resolution, e.g. 1024 entries gives
# 0.35156° per step and 2048 entries gives 0.17578° per step.
# SIN_LUT / COS_LUT values remain fp8-scaled ×256, so cam_x_fp and cam_y_fp
# advance in the direction the camera faces using integer sin/cos values.


from dvi_rp2_hstx_frame_sync_v2 import DVI_RP2_HSTX
import colors as rv_colors
from gamepadfast import Gamepad
from machine import freq, Pin
import time, _thread, gc, array
from time import sleep_ms
from draw_number import Draw_number
import random, framebuf
import array

machine.freq(246_000_000)
machine.mem32[0x40010058] = 2<<16 # HSTX CLK / 2
machine.mem32[0x40010054] = 1<<11 # HSTX CLK use SYS CLK

WIDTH = 320
HEIGHT = 240
BYTES_PER_PIXEL = 2

fb = bytearray(WIDTH * HEIGHT * BYTES_PER_PIXEL)
SCREEN = framebuf.FrameBuffer(fb, WIDTH, HEIGHT, framebuf.RGB565)

display = DVI_RP2_HSTX()
display.begin(
    fb,
    rv_colors.COLOR_MODE_BGR565,
    height=HEIGHT,
    width=WIDTH,
    bytes_per_pixel=BYTES_PER_PIXEL,
)


draw_num = Draw_number(fb,WIDTH)
gamepad = Gamepad()

MAP_SIZE  = const(256)#128
MAP_MASK  = const(255)#127
SCREEN_W  = const(320)
SCREEN_H  = const(240)
FP_SHIFT  = const(8)                 # fp8: value × 256, extract with >> 8
HALF_W    = const(SCREEN_W // 2)     # screen centre column

# ── Renderer constants ────────────────────────────────────────────────────────
HORIZON_Y = const(60)      # screen row where flat terrain at cam_z appears
FOCAL_LEN = const(200)     # perspective focal length (pixels)
SCALE_H   = const(80)      # vertical scale: (cam_z - h) * SCALE_H // d → rows
MIN_DIST  = const(2)       # nearest ray step (map units)
MAX_DIST  = const(220)#130     # farthest ray step (map units)
CAM_CLEAR = const(25)      # default camera clearance above terrain (height units)

# ── Sky gradient LUT ─────────────────────────────────────────────────────────
# 160 uint16 RGB565 entries — one per screen row.
SKY_LUT = array.array('H', [0] * SCREEN_H)

# ── Reciprocal LUT (OPT-2) ───────────────────────────────────────────────────
# RECIP_LUT[d] = (SCALE_H << 8) // d  →  eliminates division in the hot loop.
# Projection: scr_y = HORIZON_Y + ((cam_z - h) * RECIP_LUT[dist]) >> 8
# Sized MAX_DIST+1; index 0 unused (dist starts at MIN_DIST=2).
RECIP_LUT = array.array('i', [0] * (MAX_DIST + 1))

# ── Trig LUTs──────────────────────────────

TRIG_BITS = const(10)
TRIG_SIZE = const(1 << TRIG_BITS)     # 1024
TRIG_MASK = const(TRIG_SIZE - 1)

SIN_LUT = array.array('i', [0] * TRIG_SIZE)
COS_LUT = array.array('i', [0] * TRIG_SIZE)

# ── GAME ───────────────────────────────────────────────────────────────────
GAME = array.array('I', [0]*15)

GAME_ROUGH = const(0)
GAME_SEED  = const(1)
GAME_DECAY = const(2)
GAME_RANGELIM = const(3)
GAME_CURSOR = const(4)
GAME_EXIT  = const(5)
GAME_EDIT  = const(6)
GAME_MODE  = const(7)  # 0 = design, 1 = fly
GAME_AUX1  = const(8)
GAME_AUX2  = const(9)
GAME_AUX3  = const(10)
GAME_AUX4  = const(11)
DESIGN_MODE = const(0)
FLY_MODE = const(1)

# ── draw_num slot indices ─────────────────────────────────────────────────────
FPS_CORE0 = const(0)
FPS_CORE1 = const(1)

# ── Fixed-point projection for the Phase 1 top-down view ─────────────────────
HMAP_XSCALE = const(136)
HMAP_YSCALE = const(204)

# ── Phase 1 camera pan (unused in Phase 2 main loop, kept for reference) ─────
CAM_XSPEED  = const(30)
CAM_YSPEED  = const(19)
MAP_FP_WRAP = const(32768)   # 128 << 8

# CAM array parameters
CAM_PARAMS   = const(10)
CAM_X_FP     = const(0)
CAM_Y_FP     = const(1)
CAM_Z        = const(2)
CAM_YAW_COS  = const(3)
CAM_YAW_SIN  = const(4)
CAM_YAW      = const(5)      # angle index 0-255 into SIN_LUT / COS_LUT


# ── Terrain colour palette — RGB565  ──────────────────────────────────────────
COL_DEEP    = const(0x1917)
COL_SHALLOW = const(0x0Ab4)
COL_SAND    = const(0xCD6d)
COL_LOGRASS = const(0x54c5)
COL_HIGRASS = const(0x43e4)
COL_ROCK    = const(0x838b)
COL_HIROCK  = const(0xAD33)
COL_SNOW    = const(0b_11111_111111_11111)
WHITE = const(0xffff)
GREY  = const(0b01111_011111_01111)
LT_BLUE = const(0b10000_100000_11111)

T_DEEP    = const(45)
T_SHALLOW = const(65)
T_SAND    = const(100)
T_LOGRASS = const(150)
T_HIGRASS = const(200)
T_ROCK    = const(220)
T_HIROCK  = const(250)

POT = array.array('i', [
     0,0,0   # x,y,debounce
])
POT_X = const(0)
POT_Y = const(1)
POT_DEBOUNCE = const(2)
GAMEPAD_RIGHT  = const(0b0100000)
GAMEPAD_LEFT   = const(0b0000100)
GAMEPAD_UP     = const(0b1000000)
GAMEPAD_DOWN   = const(0b0000010)
GAMEPAD_START  = const(0)
GAMEPAD_SELECT = const(0b0000001)


# ── Heightmap buffer ─────────────────────────────────────────────────────────
HMAP = bytearray(MAP_SIZE * MAP_SIZE)

# ── CTRL array for _draw_voxel_asm ───────────────────────────────────────────
# 17 signed int32 slots.  CC_ constants are byte offsets used as immediates
# in ldr/str [r0, CC_*].  Max offset 64 → imm5=16 in word-load encoding
# (hardware limit 124).  All slots accessible without range violation.
#
# Layout:
#   [0..3]  raw buffer pointers (fbuf, hmap, sky, recip) — set by Viper shim
#   [4..8]  camera scalars from CAM array
#   [9]     outer col counter (0..SCREEN_W-1)
#   [10..11] per-column step vector (OPT-1)
#   [12..13] running ray position (sx_fp, sy_fp)
#   [14]    horizon (top of painted terrain for this column)
#   [15]    inner dist counter
#   [16]    scr_y scratch (saved before strip painter clobbers r4)
CTRL_SIZE  = const(17)
CC_FBUF    = const( 0)   # ptr → SCREEN  (uint16 pixel buffer)
CC_HMAP    = const( 4)   # ptr → HMAP        (uint8  heightmap)
CC_SKY     = const( 8)   # ptr → SKY_LUT     (uint16 sky gradient)
CC_RECIP   = const(12)   # ptr → RECIP_LUT   (int32  reciprocal table)
CC_CAMXFP  = const(16)   # cam_x_fp
CC_CAMYFP  = const(20)   # cam_y_fp
CC_CAMZ    = const(24)   # cam_z
CC_COS     = const(28)   # yaw_cos  (fp8 facing cosine)
CC_SIN     = const(32)   # yaw_sin  (fp8 facing sine)
CC_COL     = const(36)   # col      (outer loop counter)
CC_STEPX   = const(40)   # step_x   (OPT-1 per-column ray X direction)
CC_STEPY   = const(44)   # step_y   (OPT-1 per-column ray Y direction)
CC_SXFP    = const(48)   # sx_fp    (running ray X, fp8)
CC_SYFP    = const(52)   # sy_fp    (running ray Y, fp8)
CC_HORIZ   = const(56)   # horizon  (topmost painted row for this column)
CC_DIST    = const(60)   # dist     (inner loop counter)
CC_SCRY    = const(64)   # scr_y    (projected row — spilled before strip paint)

# Enemy
ENM_COUNT     = const(4)
ENM_STRIDE    = const(3)    # slots per enemy: wx, wy, alive
ENM_X         = const(0)
ENM_Y         = const(1)
ENM_ALIVE     = const(2)
ENEMY_WORLD_W = const(3)    # projected width  source (map units)
ENEMY_WORLD_H = const(8)    # projected height source (height units)
ENEMY_MIN_PX  = const(2)    # minimum rendered size in px
ENEMY_MAX_W   = const(120)  # max rendered width  clamp
ENEMY_MAX_H   = const(80)   # max rendered height clamp
COL_ENEMY     = const(0xFFe0)   # yellow — RGB565(255,255,0) byte-swapped

# ── Bullets ──────────────────────────────────────────────────────────────────
# Each bullet is a live projectile with position + velocity, advanced per frame.
# It flies straight from where the camera was, hits terrain or enemy on contact.
MAX_BULLETS    = const(20)      # max simultaneous in-flight bullets (tune this)
BULL_STRIDE    = const(7)       # slots per bullet: x_fp, y_fp, z, dx_fp, dy_fp, alive, ttl
BULL_X_FP      = const(0)       # fp8 world X position
BULL_Y_FP      = const(1)       # fp8 world Y position
BULL_Z         = const(2)       # integer height (constant altitude throughout flight)
BULL_DX_FP     = const(3)       # fp8 X velocity per frame
BULL_DY_FP     = const(4)       # fp8 Y velocity per frame
BULL_ALIVE     = const(5)       # 0=free  1=active
BULL_TTL       = const(6)       # frames remaining — despawns when 0
BULL_SPEED     = const(1500)#768     # bullet speed in fp8 units/frame (3 map-units/frame)
FIRE_COOLDOWN  = const(4)       # min frames between shots while button held (tune this)
HIT_RADIUS     = const(4)       # Manhattan world-unit hit radius vs enemies
BULL_MAX_LIFE  = const(150) #120    # max frames alive — despawn beyond this (safety net)
COL_BULLET     = const(0b11111_100000_10000)  # 

# Explosion
EXPLO_COUNT   = const(20)       # independent of MAX_BULLETS — ring buffer
EXPLO_STRIDE  = const(5)        # slots per explosion: wx, wy, frames_left, hit_flag, wz
EXPLO_WX      = const(0)
EXPLO_WY      = const(1)
EXPLO_FR      = const(2)
EXPLO_HIT     = const(3)        # 1 = enemy hit (red), 0 = miss/terrain (yellow)
EXPLO_WZ      = const(4)        # world Z (terrain height at impact point)
EXPLO_FRAMES  = const(20)       # display duration in frames
EXPLO_WORLD_R = const(6)        # world-space explosion radius
EXPLO_MAX_R   = const(40)       # pixel radius clamp
COL_EXPLO_OUT = const(0b11111_000000_00000)   # red    — RGB565(255,  0, 0) 
COL_EXPLO_IN  = const(0b11111_100000_00000)   # orange — RGB565(255,128, 0) 
COL_EXPLO_MISS= const(0b11111_111111_00000)   # yellow — RGB565(255,255, 0) byte-swapped (miss)

# ── HUD constants ─────────────────────────────────────────────────────────────
# Compass tape
HUD_TAPE_Y   = const(5)      # tape baseline y (leaves y=1..4 for caret above)
HUD_TAPE_X0  = const(72)     # tape left  clip → 88° visible window (176px / 2px per °)
HUD_TAPE_X1  = const(248)    # tape right clip
HUD_TAPE_CX  = const(160)    # tape centre x = always your current heading
HUD_PX_DEG   = const(2)      # pixels per degree
HUD_LBL_Y    = const(16)     # cardinal label y (clears the 8px major ticks at y=6..13)
# AGL
HUD_AGL_X    = const(2)      # AGL readout left edge
HUD_AGL_Y    = const(225)    # AGL readout y (bottom strip)
HUD_AGL_WARN = const(20)     # AGL units below this → switch to red
# Crosshair
HUD_CROSS_CX = const(160)    # centre x
HUD_CROSS_CY = const(120)    # centre y
HUD_CROSS_GAP = const(3)     # gap half-width (px each side of centre)
HUD_CROSS_LEN = const(7)     # arm length (px)
# Colours
COL_HUD_GRN  = const(0x07E0) # bright green
COL_HUD_DIM  = const(0x0320) # dim green  (tape bar, minor ticks, labels)
COL_HUD_WARN = const(0b11111_000000_00000)  # red  (matches COL_EXPLO_OUT)

# ── SECTION 2: ARRAYS ────────────────────────────────────────────────────────

# ENM_COUNT enemies × ENM_STRIDE int32 slots
ENEMIES    = array.array('i', [0] * (ENM_COUNT * ENM_STRIDE))

# MAX_BULLETS × BULL_STRIDE int32 slots — live projectiles
BULLETS    = array.array('i', [0] * (MAX_BULLETS * BULL_STRIDE))

# Fire cooldown timer — counts down each frame; fire allowed when 0
FIRE_TIMER = array.array('i', [0])

# Explosion ring buffer — EXPLO_COUNT × EXPLO_STRIDE
EXPLOSIONS = array.array('i', [0] * (EXPLO_COUNT * EXPLO_STRIDE))


CTRL = array.array('i', [0] * CTRL_SIZE)


# ── Terrain generation (Phase 1 — diamond-square) ────────────────────────────

@micropython.viper
def generate():
    game = ptr32(GAME)
    hmap = ptr8(HMAP)
    
    """
    Fill `hmap` (pre-allocated bytearray, MAP_SIZE * MAP_SIZE bytes) with
    height values 0-255.

    terrain_seed : PRNG seed - change to get different terrain shapes.
    roughness    : initial noise amplitude (0-255).  Higher values produce
                   more dramatic, jagged mountains.  

    The map tiles seamlessly: hmap[y][x] == hmap[y][x + MAP_SIZE] etc.
    Heights are normalised to the full 0-255 range after generation so that
    every seed produces maximum visual contrast.
    """
    random.seed(game[GAME_SEED])
    roughness = game[GAME_ROUGH]
    decay = game[GAME_DECAY]
    range_lim = game[GAME_RANGELIM]
    N  = MAP_SIZE
    M  = MAP_MASK

    i = 0
    while i < N * N:
        hmap[i] = 0
        i += 1

    hmap[0] = 128

    rand_range = roughness
    step = N

    while step > 1:
        half = step >> 1

        # Diamond step
        y = 0
        while y < N:
            x = 0
            while x < N:
                s = (int(hmap[( y       & M) * N + ( x       & M)]) +
                     int(hmap[( y       & M) * N + ((x+step) & M)]) +
                     int(hmap[((y+step) & M) * N + ( x       & M)]) +
                     int(hmap[((y+step) & M) * N + ((x+step) & M)]))
                v = (s >> 2) + int(random.randint(-rand_range, rand_range))
                hmap[((y+half) & M) * N + ((x+half) & M)] = (
                    0 if v < 0 else (255 if v > 255 else v))
                x += step
            y += step

        # Square step
        y = 0
        while y < N:
            x_start = half if ((y // half) & 1) == 0 else 0
            x = x_start
            while x < N:
                ym = y & M
                xm = x & M
                s = (int(hmap[ ym         * N + ((xm - half) & M)]) +
                     int(hmap[ ym         * N + ((xm + half) & M)]) +
                     int(hmap[((ym - half) & M) * N +  xm        ]) +
                     int(hmap[((ym + half) & M) * N +  xm        ]))
                v = (s >> 2) + int(random.randint(-rand_range, rand_range))
                hmap[ym * N + xm] = 0 if v < 0 else (255 if v > 255 else v)
                x += step
            y += half

        step = half
        rand_range = rand_range * decay // 100 # 
        if rand_range < range_lim:
            rand_range = range_lim

    # Normalise to full 0-255 range
    lo = 255
    hi = 0
    i = 0
    while i < N * N:
        v = hmap[i]
        if v < lo: lo = v
        if v > hi: hi = v
        i += 1

    span = hi - lo
    if span > 0:
        i = 0
        while i < N * N:
            hmap[i] = (int(hmap[i]) - lo) * 255 // span
            i += 1

def init_cam():
    global CAM
    CAM = array.array('i',[0] * CAM_PARAMS)
    CAM[CAM_X_FP]    = 64 * 256
    CAM[CAM_Y_FP]    = 64 * 256
    CAM[CAM_Z]       = HMAP[64 * MAP_SIZE + 64] + CAM_CLEAR + 150
    CAM[CAM_YAW]     = 0             # start facing +X  (angle index 0)
    CAM[CAM_YAW_COS] = COS_LUT[0]   # 256 in fp8 = 1.0
    CAM[CAM_YAW_SIN] = SIN_LUT[0]   # 0

def init_sky():
    i = 0
    while i < SCREEN_H:
        t = (i * 256) // SCREEN_H   # 0 at top → ~255 at bottom
        r = 12 + (t * 14) // 256    # 12 → 26
        g = 3  + (t * 41) // 256    #  3 → 44
        b =      (t * 17) // 256    #  0 → 17
        SKY_LUT[i] = (b << 11) | (g << 5) | r
        i += 1


def init_recip():
    """
    OPT-2: Populate RECIP_LUT so draw_voxel can replace the per-step integer
    division with a multiply + right-shift.

    RECIP_LUT[d] = (SCALE_H << 8) // d

    Projection in the renderer:
        scr_y = HORIZON_Y + ((cam_z - h) * RECIP_LUT[dist]) >> 8
    is algebraically identical to the original:
        scr_y = HORIZON_Y + (cam_z - h) * SCALE_H // dist
    but uses only a multiply and a shift — no division at runtime.
    """
    d = MIN_DIST
    while d <= MAX_DIST:
        RECIP_LUT[d] = (SCALE_H << 8) // d
        d += 1


def init_trig():
    import math
    i = 0
    while i < TRIG_SIZE:
        a = i * 2.0 * math.pi / TRIG_SIZE
        s = math.sin(a)
        c = math.cos(a)
        SIN_LUT[i] = int(s * 256.0 + 0.5) if s >= 0 else -int(-s * 256.0 + 0.5)
        COS_LUT[i] = int(c * 256.0 + 0.5) if c >= 0 else -int(-c * 256.0 + 0.5)
        i += 1

def init_enemies():
    """
    Pre-place ENM_COUNT enemies at fixed world coordinates.
    Call once from __main__ after init_trig().
    Initialisation only — plain Python is fine here.
    """
    ENEMIES[0 * ENM_STRIDE + ENM_X]     = 80
    ENEMIES[0 * ENM_STRIDE + ENM_Y]     = 80
    ENEMIES[0 * ENM_STRIDE + ENM_ALIVE] = 1

    ENEMIES[1 * ENM_STRIDE + ENM_X]     = 150
    ENEMIES[1 * ENM_STRIDE + ENM_Y]     = 100
    ENEMIES[1 * ENM_STRIDE + ENM_ALIVE] = 1

    ENEMIES[2 * ENM_STRIDE + ENM_X]     = 60
    ENEMIES[2 * ENM_STRIDE + ENM_Y]     = 180
    ENEMIES[2 * ENM_STRIDE + ENM_ALIVE] = 1

    ENEMIES[3 * ENM_STRIDE + ENM_X]     = 200
    ENEMIES[3 * ENM_STRIDE + ENM_Y]     = 200
    ENEMIES[3 * ENM_STRIDE + ENM_ALIVE] = 1

@micropython.viper
def read_pot_design():
    game  = ptr32(GAME)
    pot   = ptr32(POT)
    cam   = ptr32(CAM)
    if game[GAME_EDIT] == 1: return
    old_button = pot[POT_DEBOUNCE]    
    gamepad.read() # read all I/O 
    buttons = int(gamepad.buttons)
    if old_button == buttons: return
    pot[POT_DEBOUNCE] = buttons
    if not (buttons & GAMEPAD_SELECT) :
        game[GAME_MODE] = FLY_MODE
        game[GAME_EDIT] = 1
    if not (buttons & GAMEPAD_RIGHT) :
        game[game[GAME_CURSOR]] += 5
        game[GAME_EDIT] = 1
        generate()
    if not (buttons & GAMEPAD_LEFT) :
        value = game[game[GAME_CURSOR]]
        if value >= 0:
            game[game[GAME_CURSOR]] -= 5
            game[GAME_EDIT] = 1
            generate()
    if not (buttons & GAMEPAD_UP) :
        game[GAME_CURSOR] = (game[GAME_CURSOR] - 1 ) % 4
    if not (buttons & GAMEPAD_DOWN) :
        game[GAME_CURSOR] = (game[GAME_CURSOR] + 1 ) % 4

@micropython.viper
def read_pot_fly():
    game  = ptr32(GAME)
    pot   = ptr32(POT)
    cam   = ptr32(CAM)
    sin_lut = ptr32(SIN_LUT)
    cos_lut = ptr32(COS_LUT)
    old_button = pot[POT_DEBOUNCE]    
    gamepad.read() # read all I/O 
    buttons = int(gamepad.buttons)
    if old_button == buttons and not(buttons & GAMEPAD_SELECT): return
    pot[POT_DEBOUNCE] = buttons
    #print(bin(buttons),buttons & GAMEPAD_RIGHT)
    if not (buttons & GAMEPAD_RIGHT) : shutdown()
    if not (buttons & GAMEPAD_SELECT) : game[GAME_MODE] = DESIGN_MODE 
    pot_x = int(gamepad.x)  # -500 to 500 with 0 = center
    pot_y = int(gamepad.y)  # -500 to 500 with 0 = center
    if pot_x <= -25: pot_x >>= 7
    elif pot_x > 25: pot_x >>= 7
    else: pot_x = 0
    if pot_y <= -25: pot_y >>= 7
    elif pot_y > 25: pot_y >>= 7
    else: pot_y = 0
    game[GAME_AUX1] = pot_x
    game[GAME_AUX2] = pot_y
    game[GAME_AUX3] = int(gamepad.left)
    # Update yaw angle index, then derive cos/sin from LUT — stays on unit circle
    
    yaw = (cam[CAM_YAW] + pot_x) & TRIG_MASK
    cam[CAM_YAW]     = yaw
    cam[CAM_YAW_COS] = cos_lut[yaw]
    cam[CAM_YAW_SIN] = sin_lut[yaw]

    cam_z = cam[CAM_Z]
    cam_z += pot_y
    if 0 < cam_z < 512:
        cam[CAM_Z] = cam_z

@micropython.viper
def fire_check():
    """
    Called every frame from main().

    While FIRE_BUTTON is held (active-low), spawns one bullet every
    FIRE_COOLDOWN frames.  Each bullet inherits the camera's current
    position and yaw as a velocity vector (BULL_SPEED magnitude).
    Finds the first free slot in BULLETS[]; does nothing if all slots full.
    """
    game   = ptr32(GAME)
    cam    = ptr32(CAM)
    bulls  = ptr32(BULLETS)
    ftimer = ptr32(FIRE_TIMER)

    # ── Tick cooldown toward zero every frame ──────────────────────────────
    cd = ftimer[0]
    if cd > 0:
        cd -= 1
        ftimer[0] = cd

    # ── Button held? (active-low: 0 = pressed) ────────────────────────────

    btn = game[GAME_AUX3]

    if btn == 0:
        return                           # not pressed — nothing to do
    if cd > 0:
        return                           # still cooling down
    # ── Find a free bullet slot ───────────────────────────────────────────
    slot = -1
    k = 0
    while k < MAX_BULLETS:
        if bulls[k * BULL_STRIDE + BULL_ALIVE] == 0:
            slot = k
            k = MAX_BULLETS              # exit (no break in Viper)
        k += 1
    if slot < 0:
        return                           # all slots occupied

    # ── Spawn bullet ──────────────────────────────────────────────────────
    base = slot * BULL_STRIDE
    bulls[base + BULL_X_FP]  = cam[CAM_X_FP]
    bulls[base + BULL_Y_FP]  = cam[CAM_Y_FP]
    bulls[base + BULL_Z]     = cam[CAM_Z]+10#2

    # Velocity = unit direction (yaw_cos, yaw_sin are fp8 unit) scaled by speed
    # yaw_cos is fp8 (x256), BULL_SPEED is fp8 → product is fp16 → >>8 gives fp8
    yaw_cos = cam[CAM_YAW_COS] 
    yaw_sin = cam[CAM_YAW_SIN]
    bulls[base + BULL_DX_FP] = yaw_cos * BULL_SPEED >> FP_SHIFT
    bulls[base + BULL_DY_FP] = yaw_sin * BULL_SPEED >> FP_SHIFT
    bulls[base + BULL_ALIVE] = 1
    bulls[base + BULL_TTL]  = BULL_MAX_LIFE

    # Reset cooldown
    ftimer[0] = FIRE_COOLDOWN

@micropython.viper
def update_bullets():
    """
    Called every frame from main(), after fire_check().

    For each alive bullet:
      • Advance position by velocity (two fp8 adds, toroidal wrap).
      • Convert to map coords; check terrain height — if bullet_z <= terrain_h,
        it hit the ground: spawn terrain explosion, kill bullet.
      • Manhattan-distance check against all alive enemies — if within
        HIT_RADIUS, spawn hit explosion, kill enemy, kill bullet.
      • TTL countdown — despawn silently after BULL_MAX_LIFE frames.

    Also ticks down the frame counter on every live explosion.
    """
    bulls   = ptr32(BULLETS)
    enemies = ptr32(ENEMIES)
    explos  = ptr32(EXPLOSIONS)
    hmap    = ptr8(HMAP)

    i = 0
    while i < MAX_BULLETS:
        base = i * BULL_STRIDE
        if bulls[base + BULL_ALIVE] != 0:

            # ── Advance position (toroidal wrap like camera) ─────────────
            bx = (bulls[base + BULL_X_FP] + bulls[base + BULL_DX_FP]) & 0xffff
            by = (bulls[base + BULL_Y_FP] + bulls[base + BULL_DY_FP]) & 0xffff
            bz = bulls[base + BULL_Z] - 8 # 1
            bulls[base + BULL_X_FP] = bx
            bulls[base + BULL_Y_FP] = by
            bulls[base + BULL_Z] = bz
            

            # ── Map coordinates ───────────────────────────────────────────
            mx = (bx >> FP_SHIFT) & MAP_MASK
            my = (by >> FP_SHIFT) & MAP_MASK
            terrain_h = int(hmap[my * MAP_SIZE + mx])

            # ── Terrain collision ─────────────────────────────────────────
            if bz <= terrain_h:
                # Spawn terrain explosion (yellow)
                bulls[base + BULL_ALIVE] = 0
                e = 0
                #print('boom1',i)
                while e < EXPLO_COUNT:
                    eb = e * EXPLO_STRIDE
                    if explos[eb + EXPLO_FR] == 0:
                        explos[eb + EXPLO_WX]  = mx
                        explos[eb + EXPLO_WY]  = my
                        explos[eb + EXPLO_FR]  = EXPLO_FRAMES
                        explos[eb + EXPLO_HIT] = 0      # miss (terrain hit)
                        explos[eb + EXPLO_WZ]  = terrain_h
                        e = EXPLO_COUNT                  # exit
                        #print('boom2',terrain_h,mx,my)
                    e += 1
            else:  #TODO
                # ── Enemy collision ───────────────────────────────────────
                wx = bx >> FP_SHIFT
                wy = by >> FP_SHIFT
                hit = 0
                j = 0
                while j < ENM_COUNT:
                    if enemies[j * ENM_STRIDE + ENM_ALIVE] != 0:
                        ex = enemies[j * ENM_STRIDE + ENM_X]
                        ey = enemies[j * ENM_STRIDE + ENM_Y]
                        ddx = wx - ex
                        ddy = wy - ey
                        ddx = ddx if ddx >= 0 else -ddx
                        ddy = ddy if ddy >= 0 else -ddy
                        if ddx + ddy <= HIT_RADIUS:
                            enemies[j * ENM_STRIDE + ENM_ALIVE] = 0
                            hit = 1
                            j = ENM_COUNT                # exit
                    j += 1

                if hit != 0:
                    # Spawn hit explosion (red)
                    bulls[base + BULL_ALIVE] = 0
                    e = 0
                    while e < EXPLO_COUNT:
                        eb = e * EXPLO_STRIDE
                        if explos[eb + EXPLO_FR] == 0:
                            explos[eb + EXPLO_WX]  = wx
                            explos[eb + EXPLO_WY]  = wy
                            explos[eb + EXPLO_FR]  = EXPLO_FRAMES
                            explos[eb + EXPLO_HIT] = 1  # enemy hit
                            explos[eb + EXPLO_WZ]  = terrain_h
                            e = EXPLO_COUNT              # exit
                        e += 1

                # ── TTL countdown — despawn if expired ────────────────────
                else:
                    ttl = bulls[base + BULL_TTL] - 1
                    bulls[base + BULL_TTL] = ttl
                    if ttl <= 0:
                        bulls[base + BULL_ALIVE] = 0

        i += 1

    # ── Tick live explosions ──────────────────────────────────────────────────
    e = 0
    while e < EXPLO_COUNT:
        eb = e * EXPLO_STRIDE
        fr = explos[eb + EXPLO_FR]
        if fr > 0:
            explos[eb + EXPLO_FR] = fr - 1
        e += 1


# ── Viper shim: capture buffer pointers + camera scalars into CTRL ────────────
# int(ptr*(x)) extracts the raw buffer address as a machine word so the
# asm renderer can use it as a base pointer directly.
@micropython.viper
def draw_voxel():
    ctrl = ptr32(CTRL)
    cam  = ptr32(CAM)
    ctrl[0] = int(ptr16(SCREEN))    # CC_FBUF   — uint16 framebuffer
    ctrl[1] = int(ptr8(HMAP))           # CC_HMAP   — uint8  heightmap
    ctrl[2] = int(ptr16(SKY_LUT))       # CC_SKY    — uint16 sky gradient
    ctrl[3] = int(ptr32(RECIP_LUT))     # CC_RECIP  — int32  reciprocal LUT
    ctrl[4] = cam[CAM_X_FP]             # CC_CAMXFP
    ctrl[5] = cam[CAM_Y_FP]             # CC_CAMYFP
    ctrl[6] = cam[CAM_Z]                # CC_CAMZ
    ctrl[7] = cam[CAM_YAW_COS]          # CC_COS
    ctrl[8] = cam[CAM_YAW_SIN]          # CC_SIN
    ctrl[9] = 0                          # CC_COL    — col starts at 0
    _draw_voxel_asm(CTRL)

# ── Inline-assembly voxel renderer ───────────────────────────────────────────
# r0 = CTRL base ptr — pinned for the entire function; never clobbered.
#      All ldr/str use [r0, CC_*] with byte offsets 0..64 (within imm5 limit).
# r1-r7 = free scratch; spilled to CTRL when all 7 are simultaneously needed.
#
# Outer loop: col  0..SCREEN_W-1    (CC_COL)
# Inner loop: dist MIN_DIST..MAX_DIST (CC_DIST)
#
# Per-column setup  (OPT-1):
#   col_off = col - HALF_W
#   step_x  = yaw_cos - (col_off * yaw_sin) // FOCAL_LEN
#   step_y  = yaw_sin + (col_off * yaw_cos) // FOCAL_LEN
#   sx_fp   = cam_x_fp + MIN_DIST * step_x
#   sy_fp   = cam_y_fp + MIN_DIST * step_y
#
# Per-dist step:
#   map_x  = (sx_fp >> 8) & MAP_MASK
#   map_y  = (sy_fp >> 8) & MAP_MASK
#   h      = hmap[map_y*MAP_SIZE + map_x]
#   scr_y  = HORIZON_Y + ((cam_z - h) * recip[dist]) >> 10   (OPT-2)
#   if scr_y < horizon: paint strip scr_y..horizon-1, update horizon
#   sx_fp += step_x;  sy_fp += step_y;  dist++
#
# Sky fill: py = 0..horizon-1  →  fbuf[py*SCREEN_W + col] = sky[py]
@micropython.asm_thumb
def _draw_voxel_asm(r0):

    # ══════════════════════════════════════════════════════════════════════════
    # COL_LOOP — outer column loop
    # ══════════════════════════════════════════════════════════════════════════
    label(COL_LOOP)
    ldr(r1, [r0, CC_COL])               # r1 = col
    #mov(r2, SCREEN_W)                   # 240 — fits 8-bit immediate
    movwt(r2,320)
    cmp(r1, r2)
    blt(DONT_EXIT)                       # col >= SCREEN_W → done
    b(ASM_EXIT)
    label(DONT_EXIT)
    # col_off = col - HALF_W  →  r1 = col_off
    # col stays in CTRL[CC_COL] for all per-pixel address calculations
    mov(r2, HALF_W)                     # 120
    sub(r1, r1, r2)                     # r1 = col_off (signed)

    # ── step_x = yaw_cos - (col_off * yaw_sin) // FOCAL_LEN ──────────────────
    ldr(r2, [r0, CC_SIN])               # r2 = yaw_sin
    mul(r2, r1)                         # r2 = col_off * yaw_sin
    mov(r3, FOCAL_LEN)                  # FOCAL_LEN — fits 8-bit
    sdiv(r2, r2, r3)                    # r2 = (col_off * yaw_sin) // FOCAL_LEN
    ldr(r3, [r0, CC_COS])               # r3 = yaw_cos
    sub(r3, r3, r2)                     # r3 = step_x
    str(r3, [r0, CC_STEPX])

    # ── step_y = yaw_sin + (col_off * yaw_cos) // FOCAL_LEN ──────────────────
    ldr(r2, [r0, CC_COS])               # r2 = yaw_cos
    mul(r2, r1)                         # r2 = col_off * yaw_cos  (r1 still col_off)
    mov(r3, FOCAL_LEN)
    sdiv(r2, r2, r3)                    # r2 = (col_off * yaw_cos) // FOCAL_LEN
    ldr(r3, [r0, CC_SIN])               # r3 = yaw_sin
    add(r3, r3, r2)                     # r3 = step_y
    str(r3, [r0, CC_STEPY])

    # ── sx_fp = cam_x_fp + MIN_DIST * step_x  (MIN_DIST=2 → lsl 1) ───────────
    ldr(r2, [r0, CC_STEPX])
    lsl(r3, r2, 1)                      # r3 = 2 * step_x
    ldr(r4, [r0, CC_CAMXFP])
    add(r3, r3, r4)                     # r3 = sx_fp
    str(r3, [r0, CC_SXFP])

    # ── sy_fp = cam_y_fp + MIN_DIST * step_y ──────────────────────────────────
    ldr(r2, [r0, CC_STEPY])
    lsl(r3, r2, 1)                      # r3 = 2 * step_y
    ldr(r4, [r0, CC_CAMYFP])
    add(r3, r3, r4)                     # r3 = sy_fp
    str(r3, [r0, CC_SYFP])

    # ── horizon = SCREEN_H,  dist = MIN_DIST ──────────────────────────────────
    mov(r1, SCREEN_H)                   # 160
    str(r1, [r0, CC_HORIZ])
    mov(r1, MIN_DIST)                   # 2
    str(r1, [r0, CC_DIST])

    # ══════════════════════════════════════════════════════════════════════════
    # DIST_LOOP — inner ray-march loop
    # ══════════════════════════════════════════════════════════════════════════
    label(DIST_LOOP)
    ldr(r1, [r0, CC_DIST])
    mov(r2, MAX_DIST)                   # 130
    cmp(r1, r2)
    bgt(SKY_FILL)                       # dist > MAX_DIST → done with column

    # ── map_x = (sx_fp >> FP_SHIFT) & MAP_MASK ───────────────────────────────
    ldr(r1, [r0, CC_SXFP])             # r1 = sx_fp
    ldr(r2, [r0, CC_SYFP])             # r2 = sy_fp
    asr(r3, r1, FP_SHIFT)              # r3 = sx_fp >> 8
    asr(r4, r2, FP_SHIFT)              # r4 = sy_fp >> 8
    mov(r5, MAP_MASK)                  # 255
    and_(r3, r5)                       # r3 = map_x
    and_(r4, r5)                       # r4 = map_y

    # ── h = hmap[map_y * MAP_SIZE + map_x]  (MAP_SIZE=256 → lsl 8) ───────────
    lsl(r5, r4, 8)                     # r5 = map_y * 256
    add(r5, r5, r3)                    # r5 = hmap index
    ldr(r6, [r0, CC_HMAP])             # r6 = hmap base ptr
    add(r6, r6, r5)                    # r6 → hmap[index]
    ldrb(r3, [r6, 0])                  # r3 = h  (0..255)

    # ── scr_y = HORIZON_Y + ((cam_z - h) * recip[dist]) >> 10  (OPT-2) ───────
    ldr(r4, [r0, CC_CAMZ])             # r4 = cam_z
    sub(r4, r4, r3)                    # r4 = cam_z - h
    ldr(r5, [r0, CC_DIST])             # r5 = dist
    lsl(r7, r5, 2)                     # r7 = dist * 4  (byte offset; ptr32)
    ldr(r6, [r0, CC_RECIP])            # r6 = recip base ptr
    add(r6, r6, r7)                    # r6 → recip[dist]
    ldr(r6, [r6, 0])                   # r6 = recip[dist]
    mul(r4, r6)                        # r4 = (cam_z - h) * recip[dist]
    asr(r4, r4, 10)                    # >> 10 — less spiky than >> 8
    add(r4, HORIZON_Y)                 # r4 = scr_y  (HORIZON_Y=60 fits 8-bit)

    # ── clamp scr_y >= 0 ──────────────────────────────────────────────────────
    cmp(r4, 0)
    it(lt)
    mov(r4, 0)

    # ── if scr_y >= horizon: no new strip visible — advance ray ───────────────
    ldr(r5, [r0, CC_HORIZ])            # r5 = current horizon
    cmp(r4, r5)
    bge(DIST_ADVANCE)

    # ── Color select  r3=h → r6=col_val ──────────────────────────────────────
    # Spill scr_y so r4 is free scratch during the branch table below.
    str(r4, [r0, CC_SCRY])

    cmp(r3, T_DEEP)                    # 45
    bge(C_SHALLOW)
    movwt(r6, COL_DEEP)
    b(DO_PAINT)
    label(C_SHALLOW)
    cmp(r3, T_SHALLOW)                 # 65
    bge(C_SAND)
    movwt(r6, COL_SHALLOW)
    b(DO_PAINT)
    label(C_SAND)
    cmp(r3, T_SAND)                    # 100
    bge(C_LOGRASS)
    movwt(r6, COL_SAND)
    b(DO_PAINT)
    label(C_LOGRASS)
    cmp(r3, T_LOGRASS)                 # 150
    bge(C_HIGRASS)
    movwt(r6, COL_LOGRASS)
    b(DO_PAINT)
    label(C_HIGRASS)
    cmp(r3, T_HIGRASS)                 # 200
    bge(C_ROCK)
    movwt(r6, COL_HIGRASS)
    b(DO_PAINT)
    label(C_ROCK)
    cmp(r3, T_ROCK)                    # 220
    bge(C_HIROCK)
    movwt(r6, COL_ROCK)
    b(DO_PAINT)
    label(C_HIROCK)
    cmp(r3, T_HIROCK)                  # 250
    bge(C_SNOW)
    movwt(r6, COL_HIROCK)
    b(DO_PAINT)
    label(C_SNOW)
    movwt(r6, COL_SNOW)

    # ── Paint vertical strip  py = scr_y .. horizon-1 ────────────────────────
    # Register map inside STRIP_LOOP:
    #   r1 = fbuf base ptr   (constant — loaded once before loop)
    #   r2 = py              (starts at scr_y, increments to horizon)
    #   r3 = horizon         (exclusive upper bound = old r5)
    #   r4 = col_val         (was r6 from color select)
    #   r5 = col             (reloaded from CC_COL)
    #   r6, r7 = scratch     (address arithmetic)
    label(DO_PAINT)
    ldr(r1, [r0, CC_FBUF])             # r1 = fbuf base ptr
    ldr(r4, [r0, CC_SCRY])             # r4 = scr_y  (reload from spill)
    mov(r2, r4)                        # r2 = py = scr_y
    mov(r4, r6)                        # r4 = col_val  (r6 held color)
    mov(r3, r5)                        # r3 = horizon  (old r5 = CC_HORIZ value)
    ldr(r5, [r0, CC_COL])              # r5 = col

    label(STRIP_LOOP)
    cmp(r2, r3)
    bge(STRIP_DONE)
    #mov(r6, SCREEN_W)                  # r6 = 240  (fits 8-bit mov)
    movwt(r6,320)
    mul(r6, r2)                        # r6 = SCREEN_W * py
    add(r6, r6, r5)                    # r6 = SCREEN_W*py + col
    lsl(r6, r6, 1)                     # × 2  (uint16 element = 2 bytes)
    add(r6, r6, r1)                    # r6 = &fbuf[py*SCREEN_W + col]
    strh(r4, [r6, 0])                  # store col_val
    add(r2, 1)                         # py++
    b(STRIP_LOOP)
    label(STRIP_DONE)

    # ── Update horizon; bail early when terrain fills column to the top ────────
    ldr(r4, [r0, CC_SCRY])
    str(r4, [r0, CC_HORIZ])            # horizon = scr_y
    cmp(r4, 0)
    beq(SKY_FILL)                      # horizon==0 → sky fill paints 0 rows

    # ── DIST_ADVANCE: sx_fp += step_x,  sy_fp += step_y,  dist++ ─────────────
    # Also the target of bge(DIST_ADVANCE) when scr_y >= horizon (strip skipped).
    label(DIST_ADVANCE)
    ldr(r1, [r0, CC_SXFP])
    ldr(r2, [r0, CC_STEPX])
    add(r1, r1, r2)
    str(r1, [r0, CC_SXFP])
    ldr(r1, [r0, CC_SYFP])
    ldr(r2, [r0, CC_STEPY])
    add(r1, r1, r2)
    str(r1, [r0, CC_SYFP])
    ldr(r1, [r0, CC_DIST])
    add(r1, 1)
    str(r1, [r0, CC_DIST])
    b(DIST_LOOP)

    # ══════════════════════════════════════════════════════════════════════════
    # SKY_FILL — fill rows 0..horizon-1 with the sky gradient
    # Register map inside SKY_LOOP:
    #   r1 = py              (0 → horizon)
    #   r2 = scratch         (sky pixel value, then address)
    #   r3 = col             (constant for this column)
    #   r4 = scratch         (fbuf address arithmetic)
    #   r5 = horizon         (loop bound)
    #   r6 = sky base ptr    (ptr16)
    #   r7 = fbuf base ptr
    # ══════════════════════════════════════════════════════════════════════════
    
    label(SKY_FILL)
    ldr(r5, [r0, CC_HORIZ])            # r5 = horizon
    cmp(r5, 0)
    beq(COL_DONE)                      # horizon==0 → no sky rows to paint
    ldr(r6, [r0, CC_SKY])             # r6 = sky base ptr  (ptr16)
    ldr(r7, [r0, CC_FBUF])            # r7 = fbuf base ptr
    ldr(r3, [r0, CC_COL])             # r3 = col
    mov(r1, 0)                         # r1 = py = 0

    label(SKY_LOOP)
    cmp(r1, r5)
    bge(COL_DONE)
    lsl(r2, r1, 1)                     # r2 = py * 2  (byte offset in ptr16 sky)
    add(r2, r2, r6)                    # r2 → sky[py]
    ldrh(r2, [r2, 0])                  # r2 = sky[py]
    #mov(r4, SCREEN_W)                  # r4 = 240
    movwt(r4,SCREEN_W)
    mul(r4, r1)                        # r4 = SCREEN_W * py
    add(r4, r4, r3)                    # r4 = SCREEN_W*py + col
    lsl(r4, r4, 1)                     # × 2
    add(r4, r4, r7)                    # r4 = &fbuf[py*SCREEN_W + col]
    strh(r2, [r4, 0])                  # store sky pixel
    add(r1, 1)                         # py++
    b(SKY_LOOP)

    # ── Advance column and repeat ─────────────────────────────────────────────
    label(COL_DONE)
    ldr(r1, [r0, CC_COL])
    add(r1, 1)
    str(r1, [r0, CC_COL])
    b(COL_LOOP)

    label(ASM_EXIT)


# ── Phase 1: top-down colour map  ────────────

@micropython.viper
def draw_terrain():
    fbuf = ptr16(SCREEN)
    hmap = ptr8(HMAP)
    cam = ptr32(CAM)
    cam_x_fp = 0#cam[CAM_X_FP]
    cam_y_fp = 0#cam[CAM_Y_FP]
    cam_x_base = 0#cam_x_fp >> FP_SHIFT
    cam_y_base = 0#cam_y_fp >> FP_SHIFT
    
    py = 0
    while py < SCREEN_H:
        map_y   = (cam_y_base + ((py * HMAP_YSCALE) >> (FP_SHIFT-1))) & MAP_MASK
        row_ofs = map_y * MAP_SIZE
        fb_row  = py * SCREEN_W
        px = 0
        while px < MAP_SIZE: #SCREEN_W
            map_x = (cam_x_base + ((px * HMAP_XSCALE) >> (FP_SHIFT-1))) & MAP_MASK
            h     = (hmap[row_ofs + map_x])
            if h < T_DEEP:
                col = int(COL_DEEP)
            elif h < T_SHALLOW:
                col = int(COL_SHALLOW)
            elif h < T_SAND:
                col = int(COL_SAND)
            elif h < T_LOGRASS:
                col = int(COL_LOGRASS)
            elif h < T_HIGRASS:
                col = int(COL_HIGRASS)
            elif h < T_ROCK:
                col = int(COL_ROCK)
            elif h < T_HIROCK:
                col = int(COL_HIROCK)
            else:
                col = int(COL_SNOW)
            fbuf[fb_row + px] = col
            px += 1
        py += 1

@micropython.viper
def draw_enemies():
    """
    Projects each alive enemy to screen space and draws a filled rectangle.

    Projection uses the same fp8 dot-product math as _draw_voxel_asm:
      dist_fp8  = dx*yaw_cos + dy*yaw_sin        (forward; must be > 0)
      lat_fp8   = dy*yaw_cos - dx*yaw_sin        (right-positive lateral)
      screen_x  = HALF_W + lat_fp8*FOCAL_LEN // dist_fp8
      screen_y  = HORIZON_Y + (cam_z - eh)*SCALE_H // dist

    Rectangle base sits at screen_y (terrain level), extends upward by ph.
    Width and height are perspective-scaled from world constants.
    Fully clipped to screen bounds; culled if behind camera or beyond MAX_DIST.
    """
    fbuf    = ptr16(SCREEN)
    enemies = ptr32(ENEMIES)
    hmap    = ptr8(HMAP)
    cam     = ptr32(CAM)

    cam_x   = cam[CAM_X_FP] >> FP_SHIFT
    cam_y   = cam[CAM_Y_FP] >> FP_SHIFT
    cam_z   = cam[CAM_Z]
    yaw_cos = cam[CAM_YAW_COS]
    yaw_sin = cam[CAM_YAW_SIN]
    col     = int(COL_ENEMY)

    i = 0
    while i < ENM_COUNT:
        alive = enemies[i * ENM_STRIDE + ENM_ALIVE]
        if alive != 0:
            ex = enemies[i * ENM_STRIDE + ENM_X]
            ey = enemies[i * ENM_STRIDE + ENM_Y]
            dx = ex - cam_x
            dy = ey - cam_y

            dist_fp8 = dx * yaw_cos + dy * yaw_sin
            if dist_fp8 > 0:
                dist = dist_fp8 >> FP_SHIFT
                if dist > 0 and dist <= MAX_DIST:

                    # Sample terrain height at enemy map cell
                    eh = int(hmap[(ey & MAP_MASK) * MAP_SIZE + (ex & MAP_MASK)])

                    # Screen position
                    lat_fp8 = dy * yaw_cos - dx * yaw_sin
                    sx = HALF_W + lat_fp8 * FOCAL_LEN // dist_fp8
                    sy = HORIZON_Y + (cam_z - eh) * SCALE_H // dist

                    # Perspective-scaled pixel dimensions, clamped
                    pw = ENEMY_WORLD_W * FOCAL_LEN // dist
                    ph = ENEMY_WORLD_H * SCALE_H   // dist
                    pw = pw if pw >= ENEMY_MIN_PX else ENEMY_MIN_PX
                    ph = ph if ph >= ENEMY_MIN_PX else ENEMY_MIN_PX
                    pw = pw if pw <= ENEMY_MAX_W   else ENEMY_MAX_W
                    ph = ph if ph <= ENEMY_MAX_H   else ENEMY_MAX_H

                    # Rectangle: base at sy, top at sy-ph, centred on sx
                    x0 = sx - pw // 2
                    x1 = sx + pw // 2
                    y0 = sy - ph
                    y1 = sy

                    # Screen clip
                    x0 = x0 if x0 >= 0       else 0
                    x1 = x1 if x1 < SCREEN_W else SCREEN_W - 1
                    y0 = y0 if y0 >= 0       else 0
                    y1 = y1 if y1 < SCREEN_H else SCREEN_H - 1

                    if x0 <= x1 and y0 <= y1:
                        py = y0
                        while py <= y1:
                            row = py * SCREEN_W
                            px  = x0
                            while px <= x1:
                                fbuf[row + px] = col
                                px += 1
                            py += 1
        i += 1


@micropython.viper
def draw_projectiles():
    """
    Draws each alive bullet as a dot projected into screen space.

    Each bullet's world position is stored directly in BULLETS[] as fp8 X/Y
    and integer Z — no interpolation needed since update_bullets() advances
    the position every frame.

    Projection uses the same fp8 dot-product math as draw_enemies.
    Bullets behind the camera, beyond MAX_DIST, or off-screen are culled.
    """
    fbuf  = ptr16(SCREEN)
    bulls = ptr32(BULLETS)
    cam   = ptr32(CAM)

    cam_x   = cam[CAM_X_FP] >> FP_SHIFT
    cam_y   = cam[CAM_Y_FP] >> FP_SHIFT
    cam_z   = cam[CAM_Z]
    yaw_cos = cam[CAM_YAW_COS]
    yaw_sin = cam[CAM_YAW_SIN]
    i = 0
    while i < MAX_BULLETS:
        base = i * BULL_STRIDE
        if bulls[base + BULL_ALIVE] != 0:
            # World position in integer map units
            bx = bulls[base + BULL_X_FP] >> FP_SHIFT
            by = bulls[base + BULL_Y_FP] >> FP_SHIFT
            bz = bulls[base + BULL_Z]
            
            rdx = bx - cam_x
            if rdx >  128: rdx -= 256
            elif rdx < -128: rdx += 256
            rdy = by - cam_y
            if rdy >  128: rdy -= 256
            elif rdy < -128: rdy += 256
            
            dist_fp8 = rdx * yaw_cos + rdy * yaw_sin
            if dist_fp8 > 0:
                dist = dist_fp8 >> FP_SHIFT
                if dist > 0 and dist <= MAX_DIST:
                    lat_fp8 = rdy * yaw_cos - rdx * yaw_sin
                    scr_x = HALF_W + lat_fp8 * FOCAL_LEN // dist_fp8
                    scr_y = HORIZON_Y + (cam_z - bz) * SCALE_H // dist

                    # Draw a 3x3 white dot, clipped to screen bounds
                    col = COL_BULLET
                    ody = 0#-1
                    while ody <= 1:
                        odx = 0#-1
                        while odx <= 1:
                            px = scr_x + odx
                            py = scr_y + ody
                            if px >= 0 and px < SCREEN_W and py >= 0 and py < SCREEN_H:
                                fbuf[py * SCREEN_W + px] = 0#col
                            odx += 1
                        ody += 1
        i += 1


# @micropython.viper
# def draw_explosions2():
#     fbuf   = ptr16(SCREEN)
#     explos = ptr32(EXPLOSIONS)
#     cam    = ptr32(CAM)
# 
#     cam_x   = cam[CAM_X_FP] >> FP_SHIFT
#     cam_y   = cam[CAM_Y_FP] >> FP_SHIFT
#     cam_z   = cam[CAM_Z]
#     yaw_cos = cam[CAM_YAW_COS]
#     yaw_sin = cam[CAM_YAW_SIN]
# 
#     i = 0
#     while i < EXPLO_COUNT:
#         eb = i * EXPLO_STRIDE
#         fr = explos[eb + EXPLO_FR]
#         if fr != 0:
#             wx = explos[eb + EXPLO_WX]
#             wy = explos[eb + EXPLO_WY]
# 
#             # ── Toroidal shortest-path wrap ───────────────────────────────
#             dx = wx - cam_x
#             if dx >  128: dx -= 256
#             elif dx < -128: dx += 256
#             dy = wy - cam_y
#             if dy >  128: dy -= 256
#             elif dy < -128: dy += 256
#             # ─────────────────────────────────────────────────────────────
# 
#             dist_fp8 = dx * yaw_cos + dy * yaw_sin
#             if dist_fp8 > 0:
#                 dist = dist_fp8 >> FP_SHIFT
#                 if dist > 0 and dist <= MAX_DIST:
#                     eh = explos[eb + EXPLO_WZ]
#                     lat_fp8 = dy * yaw_cos - dx * yaw_sin
#                     cx = HALF_W + lat_fp8 * FOCAL_LEN // dist_fp8
#                     cy = HORIZON_Y + (cam_z - eh) * SCALE_H // dist
# 
#                     r = EXPLO_WORLD_R * SCALE_H // dist
#                     r = r if r >= 1           else 10
#                     r = r if r <= EXPLO_MAX_R else EXPLO_MAX_R
# 
#                     r2      = r * r
#                     r_half  = r // 2
#                     r_half2 = r_half * r_half
# 
#                     if explos[eb + EXPLO_HIT] != 0:
#                         col_out = int(COL_EXPLO_OUT)
#                         col_in  = int(COL_EXPLO_IN)
#                     else:
#                         col_out = int(COL_EXPLO_MISS)
#                         col_in  = int(COL_EXPLO_MISS)
# 
#                     SCREEN.ellipse(cx, cy, r,      r,      COL_EXPLO_OUT, 1)
#                     SCREEN.ellipse(cx, cy, r >> 1, r >> 1, COL_EXPLO_IN,  1)
#         i += 1

@micropython.viper
def draw_explosions():
    """
    Projects each active explosion to screen space and draws a filled circle.

    EXPLO_WZ now stores terrain height at impact point (not cam_z), so
    explosions sit on the ground where the bullet actually hit.

    Inner half-radius: orange (COL_EXPLO_IN).
    Outer ring:        red   (COL_EXPLO_OUT).
    Miss/terrain:      yellow (COL_EXPLO_MISS).

    Uses d2 = odx2+ody2 <= r2 to fill — no sqrt needed.
    All pixels clipped to screen bounds.
    """
    fbuf   = ptr16(SCREEN)
    explos = ptr32(EXPLOSIONS)
    cam    = ptr32(CAM)

    cam_x   = cam[CAM_X_FP] >> FP_SHIFT
    cam_y   = cam[CAM_Y_FP] >> FP_SHIFT
    cam_z   = cam[CAM_Z]
    yaw_cos = cam[CAM_YAW_COS]
    yaw_sin = cam[CAM_YAW_SIN]

    i = 0
    while i < EXPLO_COUNT:
        eb = i * EXPLO_STRIDE
        fr = explos[eb + EXPLO_FR]
        if fr != 0:
            wx = explos[eb + EXPLO_WX]
            wy = explos[eb + EXPLO_WY]           
            dx = (wx - cam_x)
            if dx >  128: dx -= 256
            elif dx < -128: dx += 256
            dy = (wy - cam_y)
            if dy >  128: dy -= 256
            elif dy < -128: dy += 256
#            
            dist_fp8 = dx * yaw_cos + dy * yaw_sin
            #print('exp',dist_fp8,wx,wy,dx,dy)
            #print(dist_fp8,dx,wx,cam_x)
            if dist_fp8 > 0:
                dist = dist_fp8 >> FP_SHIFT
                if dist > 0 and dist <= MAX_DIST:

                    eh = explos[eb + EXPLO_WZ]   # terrain height at impact
                    lat_fp8 = dy * yaw_cos - dx * yaw_sin
                    cx = HALF_W + lat_fp8 * FOCAL_LEN // dist_fp8
                    cy = HORIZON_Y + (cam_z - eh) * SCALE_H // dist

                    # Perspective-scaled radius, clamped 1..EXPLO_MAX_R
                    r = EXPLO_WORLD_R * SCALE_H // dist
                    r = r if r >= 1          else 10
                    r = r if r <= EXPLO_MAX_R else EXPLO_MAX_R

                    r2      = r * r
                    r_half  = r // 2
                    r_half2 = r_half * r_half

                    # Choose colors: red/orange for hit, yellow for miss
                    if explos[eb + EXPLO_HIT] != 1:
                        col_out = int(COL_EXPLO_OUT)   # red
                        col_in  = int(COL_EXPLO_IN)    # orange
                    else:
                        col_out = int(COL_EXPLO_MISS)  # yellow
                        col_in  = int(COL_EXPLO_MISS)  # yellow
                    
                    SCREEN.ellipse(cx,cy,r,r,COL_EXPLO_OUT,1)
                    SCREEN.ellipse(cx,cy,r>>1,r>>1,COL_EXPLO_IN,1)

        i += 1

@micropython.viper
def draw_crosshair():
    SCREEN.hline(HUD_CROSS_CX - HUD_CROSS_GAP - HUD_CROSS_LEN, HUD_CROSS_CY, HUD_CROSS_LEN, COL_HUD_GRN)
    SCREEN.hline(HUD_CROSS_CX + HUD_CROSS_GAP + 1,              HUD_CROSS_CY, HUD_CROSS_LEN, COL_HUD_GRN)
    SCREEN.vline(HUD_CROSS_CX, HUD_CROSS_CY - HUD_CROSS_GAP - HUD_CROSS_LEN, HUD_CROSS_LEN, COL_HUD_GRN)
    SCREEN.vline(HUD_CROSS_CX, HUD_CROSS_CY + HUD_CROSS_GAP + 1,              HUD_CROSS_LEN, COL_HUD_GRN)
    SCREEN.pixel(HUD_CROSS_CX, HUD_CROSS_CY, WHITE)


@micropython.viper
def draw_agl():
    cam    = ptr32(CAM)
    hmap   = ptr8(HMAP)
    cam_z  = cam[CAM_Z]
    cam_xi = (cam[CAM_X_FP] >> FP_SHIFT) & MAP_MASK
    cam_yi = (cam[CAM_Y_FP] >> FP_SHIFT) & MAP_MASK
    ground = (hmap[cam_yi * MAP_SIZE + cam_xi])  
    agl    = cam_z - ground
    if agl < 0: agl = 0
    if agl > HUD_AGL_WARN:
        col = COL_HUD_GRN
    else:
        col = COL_HUD_WARN
    SCREEN.text('ALT', HUD_AGL_X, HUD_AGL_Y, col)
    draw_num.draw_viper(agl, 52, HUD_AGL_Y, col, 1)
    agl_bar = 220-(220 * agl // 500)
    SCREEN.rect(0,agl_bar,5,220-agl_bar,col,1)



@micropython.viper
def draw_compass():
    cam = ptr32(CAM)
    yaw = cam[CAM_YAW]
    hdg = (yaw * 45) >> 7          # 0..1023 → 0..359°

    # ── Heading caret (▼) above tape bar ─────────────────────────────────────
    SCREEN.hline(HUD_TAPE_CX - 3, HUD_TAPE_Y - 4, 7, WHITE)   # base  7 px
    SCREEN.hline(HUD_TAPE_CX - 2, HUD_TAPE_Y - 3, 5, WHITE)
    SCREEN.hline(HUD_TAPE_CX - 1, HUD_TAPE_Y - 2, 3, WHITE)
    SCREEN.pixel(HUD_TAPE_CX,     HUD_TAPE_Y - 1,    WHITE)    # tip

    # ── Tape baseline ─────────────────────────────────────────────────────────
    SCREEN.hline(HUD_TAPE_X0, HUD_TAPE_Y, HUD_TAPE_X1 - HUD_TAPE_X0, COL_HUD_DIM)

    # ── Tick marks every 10° ─────────────────────────────────────────────────
    t = 0
    while t < 360:
        diff = t - hdg
        if   diff >  180: diff -= 360
        elif diff < -180: diff += 360

        px = HUD_TAPE_CX + diff * HUD_PX_DEG
        if px >= HUD_TAPE_X0 and px <= HUD_TAPE_X1:
            if t % 90 == 0:                          # cardinal — tall white
                SCREEN.vline(px, HUD_TAPE_Y + 1, 8, WHITE)
                if   t == 0:   SCREEN.text('N',  px - 2, HUD_LBL_Y, WHITE)
                elif t == 90:  SCREEN.text('E',  px - 2, HUD_LBL_Y, WHITE)
                elif t == 180: SCREEN.text('S',  px - 2, HUD_LBL_Y, WHITE)
                elif t == 270: SCREEN.text('W',  px - 2, HUD_LBL_Y, WHITE)
            elif t % 45 == 0:                        # diagonal — medium green
                SCREEN.vline(px, HUD_TAPE_Y + 1, 5, COL_HUD_GRN)
                if   t == 45:  SCREEN.text('NE', px - 6, HUD_LBL_Y, COL_HUD_GRN)
                elif t == 135: SCREEN.text('SE', px - 6, HUD_LBL_Y, COL_HUD_GRN)
                elif t == 225: SCREEN.text('SW', px - 6, HUD_LBL_Y, COL_HUD_GRN)
                elif t == 315: SCREEN.text('NW', px - 6, HUD_LBL_Y, COL_HUD_GRN)
            else:                                    # minor — short dim
                SCREEN.vline(px, HUD_TAPE_Y + 1, 3, COL_HUD_DIM)
        t += 10

    # ── Numeric heading — top-left, outside tape zone ─────────────────────────
    SCREEN.text('HDG', 2, 0, COL_HUD_DIM)
    draw_num.draw_viper(hdg, 52, 0, COL_HUD_GRN, 1)

@micropython.viper
def draw_design():
    game = ptr32(GAME)
    display.wait_frame()
    if game[GAME_EDIT] == 1:
        game[GAME_EDIT] = 0
        SCREEN.rect(256,0,320-256,240,0b01000,1)
    draw_terrain()
    cursor = game[GAME_CURSOR]
    SCREEN.text("Rough",256,30,WHITE)
    SCREEN.text("Seed",256,40,WHITE)
    SCREEN.text("Decay",256,50,WHITE)
    SCREEN.text("Limit",256,60,WHITE)
    draw_num.draw_viper(game[GAME_ROUGH],310,30,WHITE if cursor == GAME_ROUGH else GREY ,1)
    draw_num.draw_viper(game[GAME_SEED],310,40,WHITE if cursor == GAME_SEED else GREY,1)
    draw_num.draw_viper(game[GAME_DECAY],310,50,WHITE if cursor == GAME_DECAY else GREY,1)
    draw_num.draw_viper(game[GAME_RANGELIM],310,60,WHITE if cursor == GAME_RANGELIM else GREY,1)
    sleep_ms(1000)

@micropython.viper
def draw_flight():
    game = ptr32(GAME)
    display.wait_frame()
    draw_voxel()
    draw_enemies()
    draw_projectiles()
    draw_explosions()
    draw_crosshair()        # 
    draw_agl()              # 
    draw_compass()          # 
    draw_num.draw(FPS_CORE0, 310, 0)
    draw_num.draw(FPS_CORE1, 310, 10)
   


@micropython.viper
def main2():
    game = ptr32(GAME)
    init_cam()
    cam = ptr32(CAM)
    gc.collect()
    pot_ticks = 0
    while not game[GAME_EXIT]:
        ticks = int(time.ticks_ms())
        fire_check()
        update_bullets()
        if ticks - pot_ticks > 0:
            pot_ticks = ticks
            if game[GAME_MODE] == FLY_MODE:
                read_pot_fly()
            else:
                read_pot_design()
        draw_num.update_all()
        draw_num.set(FPS_CORE0, ticks)
        time.sleep_ms(1)
    game[GAME_EXIT] = True
    print('core0 done')
        

@micropython.viper
def core1():
    sleep_ms(500)
    game = ptr32(GAME)
    cam = ptr32(CAM)
    pot_ticks = 0
    while not game[GAME_EXIT]:
        ticks = int(time.ticks_ms())
        cam[CAM_X_FP] = ((cam[CAM_X_FP] + (cam[CAM_YAW_COS]))) & 0xffff
        cam[CAM_Y_FP] = ((cam[CAM_Y_FP] + (cam[CAM_YAW_SIN]))) & 0xffff
        if game[GAME_MODE] == FLY_MODE:
            draw_flight()
        else:
            draw_design()
        draw_num.set(FPS_CORE1, ticks)
        time.sleep_ms(1)
    print('core1 done') 

def shutdown():
    display.deinit()
    GAME[GAME_EXIT] = True
    machine.freq(150_000_000)
    print('shutdown...')

def main():
    global EXIT
    EXIT = False
    print('Generating terrain...')
    t0 = time.ticks_ms()
    GAME[GAME_SEED] = 30
    GAME[GAME_ROUGH] = 50
    GAME[GAME_DECAY] = 50
    GAME[GAME_RANGELIM] = 0
    GAME[GAME_EDIT] == 1
    generate()
    print('terrain done in', time.ticks_diff(time.ticks_ms(), t0), 'ms')

    init_sky()
    print('sky LUT ready')

    init_recip()                           # OPT-2: build reciprocal table
    print('recip LUT ready')

    init_trig()                            # build sin/cos LUTs for yaw steering
    print('trig LUT ready')

    print('mem_free:', gc.mem_free())
    init_enemies()
    
    _thread.start_new_thread(core1, ())
    sleep_ms(200)
    try:
        main2()
    except KeyboardInterrupt:
        display.deinit()
        GAME[GAME_EXIT] = True
        machine.freq(150_000_000)
        print('shutdown done')
        
if __name__ == '__main__':
    main()