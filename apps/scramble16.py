from dvi_rp2_hstx_frame_sync_v2 import DVI_RP2_HSTX
from gamepadfast import Gamepad
from draw_numberdvi import Draw_number
import colors as rv_colors
import gc, array, framebuf, _thread, sys, machine
from time import sleep_ms, ticks_ms, ticks_diff, sleep_us
from random import randint
from TLV320 import TLV320DAC3100
from machine import I2C,I2S,Pin
from uctypes import addressof
from sys import exit
sys.path.append('/Scramble')


SCREEN_W  = const(320)
SCREEN_H  = const(240)
FPS_CORE0 = const(0)     # for draw_number
FPS_CORE1 = const(1)
SCORE     = const(2)
LIVES     = const(3)
BYTES_PER_PIXEL = const(1)

MAP_W      = const(1429)   # very wide map, tiles, row-major (row 0 = first 1429 tiles)
MAP_H      = const(25)
TILE_W     = const(8)
TILE_H     = const(8)
NUM_TILES  = const(34)

COLOR = randint(0,0xff)
RADIUS = randint(10,SCREEN_H//2)


machine.freq(246_000_000)
machine.mem32[0x40010058] = 2<<16 # HSTX CLK / 2
machine.mem32[0x40010054] = 1<<11 # HSTX CLK use SYS CLK

fb = bytearray(SCREEN_W * SCREEN_H * 2)

BLACK  = const(0x00)
NAVY   = const(0x01)
BLUE   = const(0x03)
GREEN  = const(0x34)
ORANGE = const(0xC8)
RED    = const(0xC0)
PURPLE = const(0x83)
YELLOW = const(0xD8)
MAGENTA= const(0xC3)

COLORS = bytearray(
    [YELLOW, ORANGE, BLUE, BLACK]+
    [RED, BLUE,   PURPLE, BLACK]+
    [YELLOW, GREEN,  MAGENTA,BLACK]+
    [PURPLE, YELLOW, RED,   BLACK]+
    [YELLOW,  ORANGE, BLUE, NAVY]+
    [RED,  BLUE,   PURPLE,NAVY]+
    [YELLOW,  GREEN,  MAGENTA,NAVY]+
    [PURPLE,  YELLOW, RED,   NAVY]
)

BACKGROUND = BLACK


GAMEPAD = array.array('i', [
     0,0,0   # x,y,debounce
])
GAMEPAD_X = const(0)
GAMEPAD_Y = const(1)
GAMEPAD_DEBOUNCE = const(2)

GAMEPAD_RIGHT  = const(0b0100000)
GAMEPAD_LEFT   = const(0b0000100)
GAMEPAD_UP     = const(0b1000000)
GAMEPAD_DOWN   = const(0b0000010)
GAMEPAD_START  = const(0)
GAMEPAD_SELECT = const(0b0000001)

display = DVI_RP2_HSTX()
display.begin(
    fb,
    rv_colors.COLOR_MODE_BGR233,
    height=SCREEN_H,
    width=SCREEN_W,
    bytes_per_pixel=BYTES_PER_PIXEL,
)
FLAG_ADDR = addressof(display._frame_flag)
gamepad = Gamepad()

GAME = array.array('i', [0]*10)
GAME_EXIT      = const(0)
GAME_CAMERA_X  = const(1)   # pixel offset into the map; stays 0 for now
GAME_ESTART    = const(3)   # running enemy-window start index (ENEMY sorted by X)
GAME_RAND      = const(4)   # 32-bit LCG state for rocket launch jitter
GAME_FUEL      = const(5)   # remaining fuel in ms, 0..FUEL_MAX
GAME_SCORE     = const(6)   # mirror of on-screen score, for bonus/game-over tests
GAME_LIVES     = const(7)   # mirror of on-screen lives
GAME_BONUS     = const(8)   # 1 once the single 10,000-pt bonus ship is awarded

# ── Scoring (arcade Scramble point table) ────────────────────────────────────
PTS_ROCKET_GROUND = const(50)     # missile on ground
PTS_ROCKET_AIR    = const(80)     # missile in flight
PTS_UFO           = const(100)    # OTHER_TILE
PTS_FUEL          = const(150)    # fuel tank
PTS_PER_SEC       = const(10)     # flying: 10 pts per second alive
BONUS_SCORE       = const(10_000) # one bonus ship only
START_LIVES       = const(5)

# ── Control array for render_map_asm ─────────────────────────────────────────
# Word indices (byte offset = index * 4 inside the asm function)
CTRL = array.array('i', [0]*8)
CTRL_SCREEN   = const(0)   # addressof(fb2)              byte offset  0
CTRL_MAP      = const(1)   # addressof(MAP)              byte offset  4
CTRL_TILES    = const(2)   # addressof(TILETEXTURES)     byte offset  8
CTRL_COLORS   = const(3)   # addressof(COLORS)           byte offset 12
CTRL_CAMERA_X = const(4)   # camera_x, updated per frame byte offset 16
CTRL_Y        = const(5)   # VARIABLE: current row, owned by asm      20

# ── Enemies, loaded from MAP2 ────────────────────────────────────────────────
# MAP2.BIN: 6-byte header (TILE2_W, TILE2_H, NUM_ENTRIES as uint16 LE),
# then NUM_ENTRIES * (tile, x, y) uint16 triples. x/y are map-pixel coords
# of the sprite's upper-left corner.
ENEMY_STRIDE = const(4)
ENEMY_X      = const(0)   # map pixel x
ENEMY_Y      = const(1)   # map pixel y
ENEMY_STATUS = const(2)
ENEMY_TILE   = const(3)   # 16x16 tile index into ENEMYTEXTURES

ENEMY_IDLE      = const(0)
ENEMY_LAUNCHED  = const(1)
ENEMY_DESTROYED = const(2)
ENEMY_EXPLODE   = const(3)   # status 3..6 = explosion frame 0..3, then DESTROYED

ROCKET_FLY_BASE      = const(3)   # launched rocket toggles ENEMYTEXTURES idx 3,4 (frames 4-5)
ENEMY_EXPLODE_BASE   = const(5)   # explosion = ENEMYTEXTURES idx 5..8 (frames 6-9)
ENEMY_EXPLODE_FRAMES = const(4)

PLAYER_STRIDE = const(4)
PLAYER_X      = const(0)   # map pixel x
PLAYER_Y      = const(1)   # map pixel y
PLAYER_STATUS = const(2)

ENEMY_W     = const(16)
ENEMY_H     = const(16)
MAP_TOP     = const(SCREEN_H - MAP_H * TILE_H)   # 40: playfield rows 40..239, flush with screen bottom
NUM_ENEMIES = const(238)

GAME_FRAME = const(2)              # animation frame index in GAME

PLAYER_W       = const(32)
PLAYER_H       = const(16)
PLAYER_FRAMES  = const(6)
PLAYER_TRANS   = const(0x00)       # RGB332 transparent key; delete test in render_player if sprite is opaque
PLAYER_MAX_X   = const(SCREEN_W - PLAYER_W)                  # 288
PLAYER_MIN_Y   = const(MAP_TOP)                              # 40
PLAYER_MAX_Y   = const(MAP_TOP + MAP_H * TILE_H - PLAYER_H)  # 224 (SCREEN_H - PLAYER_H)
ANIM_MS        = const(100)        # ms per animation frame

# ── Player death / explosion ─────────────────────────────────────────────────
PLAYER_ALIVE     = const(0)               # PLAYER_STATUS: 0 = alive, 1..7 = explosion frame+1
EXPLOSION_FRAMES = const(7)
EXPLOSION_BASE   = const(PLAYER_FRAMES)   # frames 6..12 of PLAYERTEXTURES, same 32x16 RGB332 format
PLAYER_START_X   = const(16)
PLAYER_START_Y   = const(50) # need to make adjustable
SPAWN_PAD        = const(4)  # extra clearance (px) above/below spawn rect
SPAWN_STEP       = const(4)  # y search granularity
PLAYFIELD_H      = const(MAP_H * TILE_H)                 # 200
SPAWN_Y_MAX      = const(PLAYFIELD_H - PLAYER_H - SPAWN_PAD)  # world y upper bound

MAP_PIXEL_W  = const(MAP_W * TILE_W)              # 11432
NUM_SECTIONS = const(6)
SECTION_W    = const(MAP_PIXEL_W // NUM_SECTIONS) # 1905 px per section

ENEMY = array.array('i', [0] * (NUM_ENEMIES * ENEMY_STRIDE))
PLAYER = array.array('i', [0] * (PLAYER_STRIDE))

# ── Player bullets ───────────────────────────────────────────────────────────
MAX_BULLETS   = const(4)
BULLET_STRIDE = const(3)
BULLET_X      = const(0)   # map pixel x (world coords, no MAP_TOP)
BULLET_Y      = const(1)   # map pixel y
BULLET_ACTIVE = const(2)
BULLET_W      = const(1)
BULLET_H      = const(1)
BULLET_SPEED  = const(6)   # px per frame
BULLET_COLOR  = const(0xFF)                          # white RGB332
FIRE_LATCH    = const(MAX_BULLETS * BULLET_STRIDE)   # prev button state, edge detect

BULLETS = array.array('i', [0] * (MAX_BULLETS * BULLET_STRIDE + 1))

# ── Rockets (ENEMY entries with tile 0) ──────────────────────────────────────
ROCKET_TILE      = const(0)    # ENEMY[e + ENEMY_TILE] == 0 is a rocket
ROCKET_SPEED     = const(1)    # px per frame, straight up
ROCKET_TRIG_MIN  = const(64)   # nearest possible launch distance from player
ROCKET_TRIG_SPAN = const(128)  # per-rocket spread: trigger = 64..191 px ahead
ROCKET_ODDS_MASK = const(7)    # 1-in-8 launch chance per frame inside trigger
MAX_ROCKETS      = const(3)    # max LAUNCHED at one time
# max launched-rocket x ahead of camera: player <=288 + trigger <192 -> 480
ROCKET_SCAN_W    = const(PLAYER_MAX_X + ROCKET_TRIG_MIN + ROCKET_TRIG_SPAN)

# ── Player bombs (GAMEPAD_DOWN) ──────────────────────────────────────────────
# 16x16 direct-RGB332 frames in MISCTEXTURES (256 B each, no COLORS[] lookup):
# idx 0..4 = flight arc (hold on 4), idx 5..8 = explosion.
# Y and VY are 8.8 fixed point so gravity can arc the bomb downward.
MAX_BOMBS_FLYING = const(2)  # max FLYING at one time
BOMB_SLOTS       = const(4)  # extra slots let explosions finish while 2 fly
BOMB_STRIDE      = const(5)
BOMB_X           = const(0)  # map pixel x (world coords, no MAP_TOP)
BOMB_Y           = const(1)  # map pixel y, 8.8 fixed point
BOMB_VY          = const(2)  # vertical speed, 8.8 fixed point
BOMB_STATUS      = const(3)
BOMB_FRAME       = const(4)  # flight anim frame 0..4, advanced on anim tick

BOMB_INACTIVE       = const(0)
BOMB_FLYING         = const(1)
BOMB_EXPLODE        = const(2)   # status 2..5 = explosion frame 0..3
BOMB_EXPLODE_FRAMES = const(4)
BOMB_FLY_FRAMES     = const(5)   # MISCTEXTURES idx 0..4
BOMB_EXPLODE_BASE   = const(5)   # MISCTEXTURES idx 5..8 (frames 6-9)

BOMB_W       = const(16)
BOMB_H       = const(16)
BOMB_VX      = const(2)          # px per frame; camera scrolls 1, so bomb outruns player
BOMB_GRAVITY = const(12)         # 8.8 fixed: ~0.05 px/frame^2
BOMB_TRANS   = const(0x00)       # RGB332 transparent key, same as player sprite
BOMB_LATCH   = const(BOMB_SLOTS * BOMB_STRIDE)   # prev button state, edge detect

BOMBS = array.array('i', [0] * (BOMB_SLOTS * BOMB_STRIDE + 1))

OTHER_TILE = const(1) # other enemy
# ── Fuel ─────────────────────────────────────────────────────────────────────
# Fuel is stored in GAME[GAME_FUEL] as milliseconds remaining and drained by
# wall-clock delta in core0, so full-to-empty is FUEL_MAX ms regardless of FPS.
# Destroying a fuel enemy (tile 2) with a bullet or bomb adds FUEL_ADD (10%).
# Empty tank kills the player; respawn refills to FUEL_MAX.
FUEL_TILE      = const(2)                              # ENEMY[e + ENEMY_TILE] == 2
FUEL_MAX       = const(40_000)                         # ~40 s full to empty
FUEL_ADD       = const(FUEL_MAX // 10)                 # 4000 ms per fuel enemy
# Bargraph: half screen width, centered, at the very bottom (rows 230..239).
# The playfield now ends flush at SCREEN_H (row 239), so the bar sits INSIDE
# the playfield; render_fuel_bar() runs last in draw() and repaints over the
# map/sprites every frame.
# 40 segments of 2 px yellow line + 2 px gap = 160 px; 1 segment = 1 second.
FUEL_BAR_W     = const(SCREEN_W // 2)                  # 160
FUEL_BAR_X     = const((SCREEN_W - FUEL_BAR_W) // 2)   # 80
FUEL_BAR_H     = const(10)
FUEL_BAR_Y     = const(SCREEN_H - FUEL_BAR_H)          # 230
FUEL_SEG_W     = const(2)                              # yellow line width
FUEL_SEG_PITCH = const(4)                              # line + gap
FUEL_SEGMENTS  = const(FUEL_BAR_W // FUEL_SEG_PITCH)   # 40 lines
FUEL_SEG_MS    = const(FUEL_MAX // FUEL_SEGMENTS)      # 1000 ms per line

# ── Twinkling background stars ───────────────────────────────────────────────
# Static in screen space (arcade-style, no scrolling). Re-plotted every frame
# right after render_map_asm, which repaints all playfield pixels — no erase
# needed. Visibility comes from SKYLINE: one byte per map tile column holding
# the tile row of the first solid tile from the top (a tile is solid if any
# texel != palette index 3, same definition as the collision code). Cave
# columns have a ceiling at row 0 → SKYLINE = 0 → no star passes. Open
# columns hide any star at or below the terrain ridge, so stars never draw
# over mountains. Per star per frame: one SKYLINE read + compare + pixel write.
NUM_STARS   = const(48)
STAR_STRIDE = const(3)
STAR_X      = const(0)   # screen x, 0..319
STAR_Y      = const(1)   # world/playfield y, 0..199 (add MAP_TOP to plot)
STAR_PHASE  = const(2)   # random 0..255, desyncs the twinkle
GAME_STARTICK = const(9) # star twinkle counter in GAME, +1 per ANIM_MS tick

STARS      = array.array('i', [0] * (NUM_STARS * STAR_STRIDE))
SKYLINE    = bytearray(MAP_W)      # first solid tile row per column, 0..MAP_H
TILE_SOLID = bytearray(NUM_TILES)  # scratch for build_skyline
# 4-step twinkle cycle indexed by (tick + phase) & 3; 0 = off (skip write,
# map background shows through — stays correct when palette flips to NAVY)
STAR_LEVELS = bytearray([0xFF, 0x92, 0x49, 0x00])

fb2 = bytearray(SCREEN_W * SCREEN_H * BYTES_PER_PIXEL)
SCREEN = framebuf.FrameBuffer(fb2, SCREEN_W, SCREEN_H, framebuf.GS8)
draw_num = Draw_number(fb2,SCREEN_W,BYTES_PER_PIXEL)
draw_num.set_speed(10)

# ── Wait-for-VBlank + fast fill, copied from Star Castle template ────────────
@micropython.asm_thumb
def fill_asm(r0, r1, r2):   # (buffer, 8-bit_color, flag_addr)
    label(WAIT)
    ldr(r3, [r2, 0])
    cmp(r3, 0)
    beq(WAIT)
    mov(r3, 0)
    str(r3, [r2, 0])

    mov(r3, r1)
    lsl(r2, r1, 8)
    orr(r3, r2)
    lsl(r2, r3, 16)
    orr(r3, r2)
    mov(r1, r0)
    mov(r2, r3)
    mov(r4, r3)
    mov(r5, r3)
    mov(r6, r3)
    mov(r7, r3)
    movwt(r0, SCREEN_W * SCREEN_H // 192)
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
    movwt(r2, 2400)                 # 320*240 bytes / 32 bytes per iter = 2400 exact
    label(COPY_LOOP)

    # --- Load 8 words (32 bytes) from source ---
    # Group loads first so the bus/cache can pipeline ahead of stores
    ldr(r3, [r0, 0])
    ldr(r4, [r0, 4])
    ldr(r5, [r0, 8])
    ldr(r6, [r0, 12])
    ldr(r7, [r0, 16])
    str(r3, [r1, 0])                # store first 5 while loading is cached
    str(r4, [r1, 4])
    str(r5, [r1, 8])
    str(r6, [r1, 12])
    str(r7, [r1, 16])
    ldr(r3, [r0, 20])               # remaining 3 words
    ldr(r4, [r0, 24])
    ldr(r5, [r0, 28])
    str(r3, [r1, 20])
    str(r4, [r1, 24])
    str(r5, [r1, 28])

    add(r0, 32)                     # advance src pointer
    add(r1, 32)                     # advance dst pointer
    sub(r2, 1)
    bne(COPY_LOOP)

def load_files():
    global MAP, MAP2, TILETEXTURES, ENEMYTEXTURES, PLAYERTEXTURES, MISCTEXTURES
    with open('/Scramble/MAP.BIN', "rb") as f:
        MAP = f.read()
        #print('map size:',len(MAP)) # 35725

    # Entries: tile index, pixel x, pixel y (upper-left of match)
    with open('/Scramble/MAP2.BIN', "rb") as f:
        MAP2 = f.read()
        #print('enemy size:',len(MAP2))
    with open('/Scramble/Scramble8x8_2.bin', "rb") as f:
        header = f.read(1028)
        TILETEXTURES = f.read()
    # indexed 16x16 sprites, 9 frames
    with open('/Scramble/Scramble16x16_2.bin', "rb") as f:
        header = f.read(1028)
        ENEMYTEXTURES = f.read()
    with open('/Scramble/ship32x16.bin', "rb") as f:
        header = f.read(4)
        PLAYERTEXTURES = f.read()
    # bombs 5 frames, exposions 4 frames, small enemy 4 frames
    with open('/Scramble/Misc8x8.bin', "rb") as f:
        header = f.read(4)
        MISCTEXTURES = f.read()

def init_ctrl():
    # Pointers are stable after load_files(); camera_x is refreshed each frame in draw()
    CTRL[CTRL_SCREEN] = addressof(fb2)
    CTRL[CTRL_MAP]    = addressof(MAP)
    CTRL[CTRL_TILES]  = addressof(TILETEXTURES)
    CTRL[CTRL_COLORS] = addressof(COLORS)


@micropython.viper
def build_skyline():
    # One-time, after load_files(). Pass 1: mark each of the 34 tiles solid
    # if it has any non-background texel. Pass 2: scan each map column top
    # down for the first solid tile. 1429 x 25 reads, negligible at load.
    tiles: ptr8 = ptr8(TILETEXTURES)
    solid: ptr8 = ptr8(TILE_SOLID)
    t = 0
    while t < NUM_TILES:
        base = t << 6                     # 64 texels per 8x8 tile
        flag = 0
        j = 0
        while j < 64:
            if tiles[base + j] != 3:      # index 3 = background
                flag = 1
                break
            j += 1
        solid[t] = flag
        t += 1
    tilemap: ptr8 = ptr8(MAP)
    sky: ptr8 = ptr8(SKYLINE)
    col = 0
    while col < MAP_W:
        row = 0
        while row < MAP_H:
            if solid[tilemap[row * MAP_W + col]]:
                break
            row += 1
        sky[col] = row                    # 0 = ceiling at top = cave column
        col += 1


def init_stars():
    for i in range(NUM_STARS):
        s = i * STAR_STRIDE
        STARS[s + STAR_X]     = randint(0, SCREEN_W - 1)
        STARS[s + STAR_Y]     = randint(0, PLAYFIELD_H - 1)
        STARS[s + STAR_PHASE] = randint(0, 255)


@micropython.viper
def render_stars():
    scr: ptr8 = ptr8(fb2)
    stars: ptr32 = ptr32(STARS)
    sky: ptr8 = ptr8(SKYLINE)
    levels: ptr8 = ptr8(STAR_LEVELS)
    game: ptr32 = ptr32(GAME)
    camera_x = game[GAME_CAMERA_X]
    tick = game[GAME_STARTICK]
    i = 0
    while i < NUM_STARS:
        s = i * STAR_STRIDE
        i += 1
        star_x = stars[s + STAR_X]
        star_y = stars[s + STAR_Y]
        # camera_x wraps at 11112, +319 max = 11431 < MAP_PIXEL_W: no wrap
        if (star_y >> 3) >= sky[(camera_x + star_x) >> 3]:
            continue                      # cave column, or at/below the ridge
        color = levels[(tick + stars[s + STAR_PHASE]) & 3]
        if color:                         # 0 = twinkle-off frame
            scr[(star_y + MAP_TOP) * SCREEN_W + star_x] = color


def load_enemies():
    global NUM_ENEMIES
    n = NUM_ENEMIES
    for i in range(n):
        o = 6 + i * 6                   # skip header, 6 bytes per triple
        e = i * ENEMY_STRIDE
        ENEMY[e + ENEMY_TILE]   = MAP2[o]     | (MAP2[o + 1] << 8)
        ENEMY[e + ENEMY_X]      = MAP2[o + 2] | (MAP2[o + 3] << 8)
        ENEMY[e + ENEMY_Y]      = MAP2[o + 4] | (MAP2[o + 5] << 8)
        ENEMY[e + ENEMY_STATUS] = ENEMY_IDLE
    print('enemies:', n)


@micropython.viper
def render_enemy():
    scr: ptr8 = ptr8(fb2)
    enemy: ptr32 = ptr32(ENEMY)
    tex: ptr8 = ptr8(ENEMYTEXTURES)
    colors: ptr8 = ptr8(COLORS)
    game: ptr32 = ptr32(GAME)
    camera_x = game[GAME_CAMERA_X]
    num = int(NUM_ENEMIES)
    i = game[GAME_ESTART]               # window start: skips all off-left enemies
    while i < num:
        base = i * ENEMY_STRIDE
        i += 1
        screen_x = enemy[base + ENEMY_X] - camera_x
        if screen_x >= SCREEN_W:        # sorted by X: rest are off right edge
            break
        status = enemy[base + ENEMY_STATUS]
        if status == ENEMY_DESTROYED:
            continue
        col0 = 0 - screen_x if screen_x < 0 else 0            # left clip
        col1 = SCREEN_W - screen_x if screen_x > SCREEN_W - ENEMY_W else ENEMY_W
        if status >= ENEMY_EXPLODE:                           # explosion frames 6-9
            src = (ENEMY_EXPLODE_BASE + status - ENEMY_EXPLODE) << 8
        elif status == ENEMY_LAUNCHED:                        # only rockets launch
            src = (ROCKET_FLY_BASE + (game[GAME_FRAME] & 1)) << 8
        else:
            src = enemy[base + ENEMY_TILE] << 8               # 256 B per 16x16 tile
        dst = (enemy[base + ENEMY_Y] + MAP_TOP) * SCREEN_W + screen_x
        row = 0
        while row < ENEMY_H:
            col = col0
            while col < col1:
                c = tex[src + col]
                if c != 3:                                    # index 3 = transparent
                    scr[dst + col] = colors[c]
                col += 1
            src += ENEMY_W
            dst += SCREEN_W
            row += 1


@micropython.viper
def read_gamepad():
    global BOMB1TOKEN, BOMB2TOKEN
    player = ptr32(PLAYER)
    gamepad.read() # read all I/O
    buttons = int(gamepad.buttons)
    if not (buttons & GAMEPAD_SELECT) : # select pushed
        shutdown()
    if player[PLAYER_STATUS] != PLAYER_ALIVE:
        return                          # no movement/fire during explosion
    x = int(gamepad.x) # -512 to + 512 analog
    y = int(gamepad.y) # -512 to + 512 analog
    px = player[PLAYER_X]
    py = player[PLAYER_Y]
    if x > 128 or x < -128:            # deadzone
        px += x >> 7                   # arithmetic shift: -4..+3 px per poll
    if y > 128 or y < -128:
        py += y >> 8                   # flip sign here if axis is inverted
    if px < 0: px = 0
    if px > PLAYER_MAX_X: px = PLAYER_MAX_X
    if py < PLAYER_MIN_Y: py = PLAYER_MIN_Y
    if py > PLAYER_MAX_Y: py = PLAYER_MAX_Y
    player[PLAYER_X] = px
    player[PLAYER_Y] = py
    # fire: one bullet per GAMEPAD_RIGHT press, max MAX_BULLETS in flight
    game = ptr32(GAME)
    bullets = ptr32(BULLETS)
    fire = 1 if (buttons & GAMEPAD_RIGHT) == 0 else 0   # buttons active-low
    if fire and bullets[FIRE_LATCH] == 0:
        slot = 0
        while slot < MAX_BULLETS:
            b = slot * BULLET_STRIDE
            if bullets[b + BULLET_ACTIVE] == 0:
                bullets[b + BULLET_X] = game[GAME_CAMERA_X] + px + PLAYER_W
                bullets[b + BULLET_Y] = py - MAP_TOP + (PLAYER_H >> 1) - (BULLET_H >> 1)
                bullets[b + BULLET_ACTIVE] = 1
                slot = MAX_BULLETS
                snd.play(BULLETSND, vol=220)
            slot += 1
    bullets[FIRE_LATCH] = fire
    # bomb: one per GAMEPAD_DOWN press, max MAX_BOMBS_FLYING in flight
    bombs = ptr32(BOMBS)
    drop = 1 if (buttons & GAMEPAD_DOWN) == 0 else 0    # buttons active-low
    if drop and bombs[BOMB_LATCH] == 0:
        flying = 0
        free = -1
        slot = 0
        while slot < BOMB_SLOTS:
            b = slot * BOMB_STRIDE
            status = bombs[b + BOMB_STATUS]
            if status == BOMB_FLYING:
                flying += 1
            elif status == BOMB_INACTIVE and free < 0:
                free = b
            slot += 1
        if flying < MAX_BOMBS_FLYING and free >= 0:
            # mid-width of player, bottom of sprite
            bombs[free + BOMB_X] = game[GAME_CAMERA_X] + px + (PLAYER_W >> 1) - (BOMB_W >> 1)
            bombs[free + BOMB_Y] = (py - MAP_TOP + PLAYER_H - BOMB_H+8) << 8
            bombs[free + BOMB_VY] = 0
            bombs[free + BOMB_FRAME] = 0
            bombs[free + BOMB_STATUS] = BOMB_FLYING
            if free == 0:
                BOMB1TOKEN = int(snd.play(BOMBDROP, vol=220))
            else:
                BOMB2TOKEN = int(snd.play(BOMBDROP, vol=220))

    bombs[BOMB_LATCH] = drop


@micropython.viper
def enemy_window():
    # Maintain GAME_ESTART: index of the first enemy not fully left of the
    # camera. ENEMY is sorted by X and X never changes, so this creeps
    # forward 0-1 steps per frame; the rewind loop only does work after a
    # respawn or camera wrap. LAUNCHED rockets are killed as they scroll off
    # the left edge so they don't hold a MAX_ROCKETS slot forever.
    enemy = ptr32(ENEMY)
    game = ptr32(GAME)
    cam = game[GAME_CAMERA_X]
    start = game[GAME_ESTART]
    while start > 0:                                  # camera moved backward
        if enemy[(start - 1) * ENEMY_STRIDE + ENEMY_X] + ENEMY_W <= cam:
            break
        start -= 1
    while start < NUM_ENEMIES:                        # normal forward creep
        ei = start * ENEMY_STRIDE
        if enemy[ei + ENEMY_X] + ENEMY_W > cam:
            break
        if enemy[ei + ENEMY_STATUS] == ENEMY_LAUNCHED:
            enemy[ei + ENEMY_STATUS] = ENEMY_DESTROYED
        start += 1
    game[GAME_ESTART] = start


@micropython.viper
def update_rockets():
    # Pass 1: move LAUNCHED rockets up, destroy on top-of-playfield or ceiling
    #         hit, count survivors.
    # Pass 2: launch IDLE rockets ahead of the player up to MAX_ROCKETS.
    # Both scan the sorted window [GAME_ESTART, first enemy past the bound).
    enemy = ptr32(ENEMY)
    game = ptr32(GAME)
    player = ptr32(PLAYER)
    tilemap = ptr8(MAP)
    tiles = ptr8(TILETEXTURES)
    cam = game[GAME_CAMERA_X]
    launched = 0
    e = game[GAME_ESTART]
    while e < NUM_ENEMIES:
        ei = e * ENEMY_STRIDE
        e += 1
        if enemy[ei + ENEMY_X] >= cam + ROCKET_SCAN_W:
            break                                     # sorted: rest are farther right
        if enemy[ei + ENEMY_TILE] != ROCKET_TILE:
            continue
        if enemy[ei + ENEMY_STATUS] != ENEMY_LAUNCHED:
            continue
        ey = enemy[ei + ENEMY_Y] - ROCKET_SPEED
        if ey <= 0:                                   # top of playfield: vanish
            enemy[ei + ENEMY_STATUS] = ENEMY_DESTROYED
            continue
        # ceiling (cave roof): map texel at rocket top-center, index 3 = background
        ex = enemy[ei + ENEMY_X] + (ENEMY_W >> 1)
        tile = tilemap[(ey >> 3) * MAP_W + (ex >> 3)]
        if tiles[(tile << 6) + ((ey & 7) << 3) + (ex & 7)] != 3:
            enemy[ei + ENEMY_STATUS] = ENEMY_EXPLODE        # 4-frame explosion
            snd.play(ROCKETSND, vol=220)
            continue
        enemy[ei + ENEMY_Y] = ey
        launched += 1
    if launched >= MAX_ROCKETS:
        return
    px = cam + player[PLAYER_X]                       # player world x
    rand = game[GAME_RAND]
    e = game[GAME_ESTART]
    while e < NUM_ENEMIES:
        ei = e * ENEMY_STRIDE
        e += 1
        ex = enemy[ei + ENEMY_X]
        dx = ex - px
        if dx >= ROCKET_TRIG_MIN + ROCKET_TRIG_SPAN:
            break                                     # sorted: rest are farther
        if dx <= 0:
            continue
        if enemy[ei + ENEMY_TILE] != ROCKET_TILE:
            continue
        if enemy[ei + ENEMY_STATUS] != ENEMY_IDLE:
            continue
        # per-rocket trigger distance hashed from map x: fixed 64..191 px
        if dx >= ROCKET_TRIG_MIN + ((ex * 40503) >> 7 & (ROCKET_TRIG_SPAN - 1)):
            continue
        rand = rand * 1664525 + 1013904223            # LCG, wraps in 32-bit
        if (rand >> 16) & ROCKET_ODDS_MASK:           # ~1-in-8 per frame in range
            continue
        enemy[ei + ENEMY_STATUS] = ENEMY_LAUNCHED
        launched += 1
        if launched >= MAX_ROCKETS:
            break
    game[GAME_RAND] = rand


@micropython.viper
def update_enemy_explosions():
    # Advance status 3..6 one frame per anim tick; past last frame -> DESTROYED
    enemy = ptr32(ENEMY)
    e = 0
    while e < NUM_ENEMIES:
        ei = e * ENEMY_STRIDE
        e += 1
        status = enemy[ei + ENEMY_STATUS]
        if status < ENEMY_EXPLODE:
            continue
        status += 1
        if status >= ENEMY_EXPLODE + ENEMY_EXPLODE_FRAMES:
            status = ENEMY_DESTROYED
        enemy[ei + ENEMY_STATUS] = status


@micropython.viper
def check_player_collision() -> int:
    # Returns 1 on player vs enemy (AABB) or player vs playfield (pixel-perfect) hit
    player = ptr32(PLAYER)
    game = ptr32(GAME)
    tilemap = ptr8(MAP)
    tiles = ptr8(TILETEXTURES)
    tex = ptr8(PLAYERTEXTURES)
    enemy = ptr32(ENEMY)
    cam = game[GAME_CAMERA_X]
    px = cam + player[PLAYER_X]           # world x of sprite upper-left
    py = player[PLAYER_Y] - MAP_TOP       # world y (enemy/map space, no border)
    # ── enemies: 32x16 vs 16x16 AABB, anything not destroyed ──
    # window scan: start past everything left of camera, stop at first enemy
    # right of the player's collision edge (ENEMY sorted by X)
    e = game[GAME_ESTART]
    while e < NUM_ENEMIES:
        ei = e * ENEMY_STRIDE
        e += 1
        ex = enemy[ei + ENEMY_X]
        if ex >= px + PLAYER_W//2:                           # sorted: rest are right of player
            break
        if enemy[ei + ENEMY_STATUS] >= ENEMY_DESTROYED:      # destroyed or exploding
            continue
        if px >= ex + ENEMY_W//2:                            # added //2
            continue
        ey = enemy[ei + ENEMY_Y]
        if py + PLAYER_H//2 <= ey or py >= ey + ENEMY_H//2:  # added //2
            continue
        if enemy[ei + ENEMY_TILE] == ROCKET_TILE:            # rocket dies with player
            enemy[ei + ENEMY_STATUS] = ENEMY_EXPLODE
        snd.play(MULTI, vol=220)

        return 1
    # ── playfield: opaque ship texel over non-background (index 3) map texel ──
    src = game[GAME_FRAME] << 9           # 512 B per frame
    row = 0
    while row < PLAYER_H:
        wy = py + row
        map_row = (wy >> 3) * MAP_W
        tex_row = (wy & 7) << 3
        col = 0
        while col < PLAYER_W:
            if tex[src + col] != PLAYER_TRANS:
                wx = px + col
                tile = tilemap[map_row + (wx >> 3)]
                if tiles[(tile << 6) + tex_row + (wx & 7)] != 3:
                    snd.play(MULTI, vol=220)
                    return 1
            col += 1
        src += PLAYER_W
        row += 1
    return 0


@micropython.viper
def spawn_rect_clear(wx: int, wy: int, h: int) -> int:
    # 1 if the PLAYER_W x h rect at world (wx, wy) is all background
    # (palette index 3) playfield texels. Full-rect test, ignores ship
    # transparency, so any animation frame fits the cleared spot.
    tilemap = ptr8(MAP)
    tiles = ptr8(TILETEXTURES)
    row = 0
    while row < h:
        y = wy + row
        map_row = (y >> 3) * MAP_W
        tex_row = (y & 7) << 3
        col = 0
        while col < PLAYER_W:
            x = wx + col
            tile = tilemap[map_row + (x >> 3)]
            if tiles[(tile << 6) + tex_row + (x & 7)] != 3:
                return 0
            col += 1
        row += 1
    return 1


@micropython.viper
def find_spawn_y(cam: int) -> int:
    # Screen y for a respawn at PLAYER_START_X that does not overlap the
    # playfield. Searches outward from PLAYER_START_Y (down, then up) with
    # SPAWN_PAD px of vertical clearance. Runs once per respawn only.
    wx = cam + PLAYER_START_X
    base = PLAYER_START_Y - MAP_TOP            # preferred world y
    dist = 0
    while dist <= PLAYFIELD_H:
        y = base + dist
        if y >= SPAWN_PAD and y <= SPAWN_Y_MAX:
            if int(spawn_rect_clear(wx, y - SPAWN_PAD, PLAYER_H + 2 * SPAWN_PAD)):
                return y + MAP_TOP
        y = base - dist
        if dist and y >= SPAWN_PAD and y <= SPAWN_Y_MAX:
            if int(spawn_rect_clear(wx, y - SPAWN_PAD, PLAYER_H + 2 * SPAWN_PAD)):
                return y + MAP_TOP
        dist += SPAWN_STEP
    return PLAYER_START_Y                      # nothing clear: original behavior


@micropython.viper
def render_player():
    scr: ptr8 = ptr8(fb2)
    player: ptr32 = ptr32(PLAYER)
    tex: ptr8 = ptr8(PLAYERTEXTURES)
    game: ptr32 = ptr32(GAME)
    status = player[PLAYER_STATUS]
    if status == PLAYER_ALIVE:
        src = game[GAME_FRAME] << 9                   # 32*16 = 512 B per frame
    else:
        src = (EXPLOSION_BASE + status - 1) << 9      # explosion frames 6..12
    dst = player[PLAYER_Y] * SCREEN_W + player[PLAYER_X]
    row = 0
    while row < PLAYER_H:
        col = 0
        while col < PLAYER_W:
            c = tex[src + col]
            if c != PLAYER_TRANS:
                scr[dst + col] = c                    # direct RGB332, no COLORS[]
            col += 1
        src += PLAYER_W
        dst += SCREEN_W
        row += 1


@micropython.viper
def add_score(pts: int):
    # GAME mirror + on-screen counter; award the single bonus ship the
    # first time the score reaches BONUS_SCORE (once per game, per spec).
    game = ptr32(GAME)
    score = game[GAME_SCORE] + pts
    game[GAME_SCORE] = score
    draw_num.add(SCORE, pts)
    if score >= BONUS_SCORE and game[GAME_BONUS] == 0:
        game[GAME_BONUS] = 1
        game[GAME_LIVES] += 1
        draw_num.add(LIVES, 1)


@micropython.viper
def kill_points(tile: int, status: int) -> int:
    # Arcade Scramble point table. status is the enemy's status BEFORE the
    # kill: a LAUNCHED rocket (in air) is worth 80, on the ground 50.
    pts = PTS_UFO                               # OTHER_TILE
    if tile == ROCKET_TILE:
        if status == ENEMY_LAUNCHED:
            pts = PTS_ROCKET_AIR
        else:
            pts = PTS_ROCKET_GROUND
    elif tile == FUEL_TILE:
        pts = PTS_FUEL
    return pts


@micropython.viper
def lose_life():
    game = ptr32(GAME)
    game[GAME_LIVES] -= 1
    draw_num.subtract(LIVES, 1)


@micropython.viper
def move_bullets():
    bullets = ptr32(BULLETS)
    game = ptr32(GAME)
    tilemap = ptr8(MAP)
    tiles = ptr8(TILETEXTURES)
    enemy = ptr32(ENEMY)
    cam = game[GAME_CAMERA_X]
    slot = 0
    while slot < MAX_BULLETS:
        b = slot * BULLET_STRIDE
        slot += 1
        if bullets[b + BULLET_ACTIVE] == 0:
            continue
        bx = bullets[b + BULLET_X] + BULLET_SPEED
        bullets[b + BULLET_X] = bx
        by = bullets[b + BULLET_Y]
        tip = bx + BULLET_W - 1                       # leading edge
        # off right of screen or map
        if tip - cam >= SCREEN_W or tip >= MAP_W * TILE_W:
            bullets[b + BULLET_ACTIVE] = 0
            continue
        # playfield: pixel test at tip, palette index 3 = background
        tile = tilemap[(by >> 3) * MAP_W + (tip >> 3)]
        texel = tiles[(tile << 6) + ((by & 7) << 3) + (tip & 7)]
        if texel != 3:
            bullets[b + BULLET_ACTIVE] = 0
            continue
        # enemies: 16x16 AABB, anything not destroyed (windowed, sorted by X)
        e = game[GAME_ESTART]
        while e < NUM_ENEMIES:
            ei = e * ENEMY_STRIDE
            e += 1
            ex = enemy[ei + ENEMY_X]
            if ex > tip:                                      # sorted: rest are right of bullet
                break
            if enemy[ei + ENEMY_STATUS] >= ENEMY_DESTROYED:   # destroyed or exploding
                continue
            if bx >= ex + ENEMY_W:
                continue
            ey = enemy[ei + ENEMY_Y]
            if by + BULLET_H <= ey or by >= ey + ENEMY_H:     # collision
                continue
            tile = enemy[ei + ENEMY_TILE]
            add_score(int(kill_points(tile, enemy[ei + ENEMY_STATUS])))
            if tile == ROCKET_TILE:
                snd.play(ROCKETSND, vol=220)
            if tile == OTHER_TILE:
                snd.play(MULTI, vol=220)
            enemy[ei + ENEMY_STATUS] = ENEMY_EXPLODE          # 4-frame explosion
            if tile == FUEL_TILE:                             # fuel enemy: +10%
                fuel = game[GAME_FUEL] + FUEL_ADD
                game[GAME_FUEL] = FUEL_MAX if fuel > FUEL_MAX else fuel
                snd.play(MULTI, vol=220)
            bullets[b + BULLET_ACTIVE] = 0
            break


@micropython.viper
def render_bullets():
    scr: ptr8 = ptr8(fb2)
    bullets = ptr32(BULLETS)
    game = ptr32(GAME)
    cam = game[GAME_CAMERA_X]
    slot = 0
    while slot < MAX_BULLETS:
        b = slot * BULLET_STRIDE
        slot += 1
        if bullets[b + BULLET_ACTIVE] == 0:
            continue
        sx = bullets[b + BULLET_X] - cam
        if sx >= SCREEN_W:
            continue
        x0 = 0 if sx < 0 else sx
        x1 = sx + BULLET_W
        if x1 > SCREEN_W:
            x1 = SCREEN_W
        dst = (bullets[b + BULLET_Y] + MAP_TOP) * SCREEN_W
        row = 0
        while row < BULLET_H:
            x = x0
            while x < x1:
                scr[dst + x] = BULLET_COLOR
                x += 1
            dst += SCREEN_W
            row += 1


@micropython.viper
def move_bombs():
    global BOMB1TOKEN, BOMB2TOKEN
    # Per game frame: advance FLYING bombs (vx constant, vy += gravity),
    # explode on playfield or enemy hit. Enemy hit also triggers the
    # enemy's own explosion; the bomb explosion plays at the bomb's coords.
    bombs = ptr32(BOMBS)
    game = ptr32(GAME)
    tilemap = ptr8(MAP)
    tiles = ptr8(TILETEXTURES)
    enemy = ptr32(ENEMY)
    slot = 0
    while slot < BOMB_SLOTS:
        b = slot * BOMB_STRIDE
        slot += 1
        if bombs[b + BOMB_STATUS] != BOMB_FLYING:
            continue
        if bombs[b + BOMB_FRAME] == 4:
            b_vx = 1
        else:
            b_vx = BOMB_VX
        bx = bombs[b + BOMB_X] + b_vx
        vy = bombs[b + BOMB_VY] + BOMB_GRAVITY
        yfixed = bombs[b + BOMB_Y] + vy
        by = yfixed >> 8
        bombs[b + BOMB_X] = bx
        bombs[b + BOMB_VY] = vy
        bombs[b + BOMB_Y] = yfixed
        # off bottom of playfield or off right of map: vanish
        if by + BOMB_H >= MAP_H * TILE_H or bx + BOMB_W >= MAP_W * TILE_W:
            bombs[b + BOMB_STATUS] = BOMB_INACTIVE
            continue
        # playfield: bottom-center texel, then front-center; index 3 = background
        tx = bx + (BOMB_W >> 1)
        ty = by + BOMB_H - 1
        tile = tilemap[(ty >> 3) * MAP_W + (tx >> 3)]
        hit = 1 if tiles[(tile << 6) + ((ty & 7) << 3) + (tx & 7)] != 3 else 0
        if hit == 0:
            tx = bx + BOMB_W - 1
            ty = by + (BOMB_H >> 1)
            tile = tilemap[(ty >> 3) * MAP_W + (tx >> 3)]
            hit = 1 if tiles[(tile << 6) + ((ty & 7) << 3) + (tx & 7)] != 3 else 0
        if hit:
            bombs[b + BOMB_STATUS] = BOMB_EXPLODE
            if slot == 1:
                snd.stop(BOMB1TOKEN)
            elif slot == 2 :
                snd.stop(BOMB2TOKEN)
            snd.play(BOMBEXPLD, vol=220)
            continue
        # enemies: 16x16 vs 16x16 AABB, anything not destroyed (windowed, sorted by X)
        e = game[GAME_ESTART]
        while e < NUM_ENEMIES:
            ei = e * ENEMY_STRIDE
            e += 1
            ex = enemy[ei + ENEMY_X]
            if ex >= bx + BOMB_W//2:                          # sorted: rest are right of bomb
                break
            if enemy[ei + ENEMY_STATUS] >= ENEMY_DESTROYED:   # destroyed or exploding
                continue
            if bx >= ex + ENEMY_W:
                continue
            ey = enemy[ei + ENEMY_Y]
            if by + BOMB_H//2 <= ey or by >= ey + ENEMY_H:    # no collision, next
                continue
            if slot == 1:
                snd.stop(BOMB1TOKEN)
            elif slot == 2 :
                snd.stop(BOMB2TOKEN)
            tile = enemy[ei + ENEMY_TILE]
            add_score(int(kill_points(tile, enemy[ei + ENEMY_STATUS])))
            if tile == ROCKET_TILE:
                snd.play(ROCKETSND, vol=220)
            if tile == OTHER_TILE:
                snd.play(MULTI, vol=220)
            enemy[ei + ENEMY_STATUS] = ENEMY_EXPLODE          # enemy explosion
            if tile == FUEL_TILE:                             # fuel enemy: +10%
                fuel = game[GAME_FUEL] + FUEL_ADD
                game[GAME_FUEL] = FUEL_MAX if fuel > FUEL_MAX else fuel
                snd.play(MULTI, vol=220)
            bombs[b + BOMB_STATUS] = BOMB_EXPLODE             # bomb explosion here
            break


@micropython.viper
def update_bomb_anim():
    # Anim tick: flight frame climbs 0..4 and holds on 4 (arc pose);
    # explosion status 2..5 advances one frame, past last -> INACTIVE
    bombs = ptr32(BOMBS)
    slot = 0
    while slot < BOMB_SLOTS:
        b = slot * BOMB_STRIDE
        slot += 1
        status = bombs[b + BOMB_STATUS]
        if status == BOMB_FLYING:
            f = bombs[b + BOMB_FRAME] + 1
            if f >= BOMB_FLY_FRAMES:
                f = BOMB_FLY_FRAMES - 1
            bombs[b + BOMB_FRAME] = f
        elif status >= BOMB_EXPLODE:
            status += 1
            if status >= BOMB_EXPLODE + BOMB_EXPLODE_FRAMES:
                status = BOMB_INACTIVE
            bombs[b + BOMB_STATUS] = status


@micropython.viper
def render_bombs():
    scr: ptr8 = ptr8(fb2)
    bombs = ptr32(BOMBS)
    tex: ptr8 = ptr8(MISCTEXTURES)
    game = ptr32(GAME)
    cam = game[GAME_CAMERA_X]
    slot = 0
    while slot < BOMB_SLOTS:
        b = slot * BOMB_STRIDE
        slot += 1
        status = bombs[b + BOMB_STATUS]
        if status == BOMB_INACTIVE:
            continue
        sx = bombs[b + BOMB_X] - cam
        if sx <= 0 - BOMB_W:            # fully off left edge (stationary explosion)
            continue
        if sx >= SCREEN_W:
            continue
        col0 = 0 - sx if sx < 0 else 0
        col1 = SCREEN_W - sx if sx > SCREEN_W - BOMB_W else BOMB_W
        if status == BOMB_FLYING:
            src = bombs[b + BOMB_FRAME] << 8                  # 256 B per 16x16 frame
        else:
            src = (BOMB_EXPLODE_BASE + status - BOMB_EXPLODE) << 8
        dst = ((bombs[b + BOMB_Y] >> 8) + MAP_TOP) * SCREEN_W + sx
        row = 0
        while row < BOMB_H:
            col = col0
            while col < col1:
                c = tex[src + col]
                if c != BOMB_TRANS:
                    scr[dst + col] = c                        # direct RGB332, no COLORS[]
                col += 1
            src += BOMB_W
            dst += SCREEN_W
            row += 1


@micropython.viper
def render_fuel_bar():
    # Blue backing rect drawn first, then one 2px yellow line per FUEL_SEG_MS
    # remaining (round up: last line only vanishes at empty). Spent segments
    # reveal the blue behind them. Rows 230..239 are now inside the playfield;
    # this runs LAST in draw() so it repaints over map/sprite pixels each frame.
    # Keep it after render_bombs()/render_enemy() or sprites will draw on top.
    scr: ptr8 = ptr8(fb2)
    game = ptr32(GAME)
    dst = FUEL_BAR_Y * SCREEN_W + FUEL_BAR_X
    row = 0
    while row < FUEL_BAR_H:
        x = 0
        while x < FUEL_BAR_W:
            scr[dst + x] = BLUE
            x += 1
        dst += SCREEN_W
        row += 1
    segs = (game[GAME_FUEL] + FUEL_SEG_MS - 1) // FUEL_SEG_MS
    if segs > FUEL_SEGMENTS:
        segs = FUEL_SEGMENTS
    seg = 0
    while seg < segs:
        dst = FUEL_BAR_Y * SCREEN_W + FUEL_BAR_X + seg * FUEL_SEG_PITCH
        row = 0
        while row < FUEL_BAR_H:
            col = 0
            while col < FUEL_SEG_W:
                scr[dst + col] = YELLOW
                col += 1
            dst += SCREEN_W
            row += 1
        seg += 1


# ── Tilemap renderer in Thumb assembly ────────────────────────────────────────
# r0 = CTRL array address (only parameter).
#
# Register map inside the row loop:
#   r0 = CTRL pointer (row setup) → pushed, then reused as world_x end sentinel
#   r1 = screen write pointer (fb2 row, increments per pixel)
#   r2 = map row base       = MAP + (y >> 3) * MAP_W
#   r3 = texture row base   = TILETEXTURES + ((y & 7) << 3)
#   r4 = COLORS base
#   r5 = world_x            = camera_x + x (increments per pixel)
#   r6, r7 = scratch
#
# Registers ran out for the y counter, so CTRL[CTRL_Y] (byte offset 20)
# is used as the row variable — loaded/incremented/stored once per row.
@micropython.asm_thumb
def render_map_asm(r0):
    # y = 0
    mov(r5, 0)
    str(r5, [r0, 20])               # CTRL[CTRL_Y] = 0

    label(YLOOP)
    # ---- per-row setup (r0 = CTRL here) ----
    ldr(r5, [r0, 20])               # r5 = y

    # r2 = MAP + (y >> 3) * MAP_W
    lsr(r2, r5, 3)                  # tile_row = y // 8
    movwt(r3, MAP_W)
    mul(r2, r3)                     # tile_row * MAP_W
    ldr(r3, [r0, 4])                # CTRL[CTRL_MAP]
    add(r2, r2, r3)

    # r3 = TILETEXTURES + ((y & 7) << 3)
    mov(r3, 7)
    and_(r3, r5)                    # y & 7
    lsl(r3, r3, 3)                  # * TILE_W
    ldr(r6, [r0, 8])                # CTRL[CTRL_TILES]
    add(r3, r3, r6)

    # r1 = fb2 + (MAP_TOP + y) * SCREEN_W
    mov(r6, r5)
    add(r6, MAP_TOP)                # 40-row top border, playfield flush with bottom
    movwt(r7, SCREEN_W)
    mul(r6, r7)
    ldr(r1, [r0, 0])                # CTRL[CTRL_SCREEN]
    add(r1, r1, r6)

    ldr(r4, [r0, 12])               # r4 = CTRL[CTRL_COLORS]
    ldr(r5, [r0, 16])               # r5 = world_x = camera_x

    push({r0})                      # free r0 — registers in short supply
    movwt(r0, SCREEN_W)
    add(r0, r0, r5)                 # r0 = camera_x + 320 = world_x end

    # ---- inner loop: one pixel per pass ----
    label(XLOOP)
    lsr(r6, r5, 3)                  # tile_col = world_x >> 3
    add(r6, r6, r2)
    ldrb(r6, [r6, 0])               # tile_id = MAP[row_off + tile_col]
    lsl(r6, r6, 6)                  # tile_id * 64
    add(r6, r6, r3)                 # + texture row base (row_in_tile folded in)
    mov(r7, 7)
    and_(r7, r5)                    # col_in_tile = world_x & 7
    add(r6, r6, r7)
    ldrb(r7, [r6, 0])               # palette index
    add(r7, r7, r4)
    ldrb(r7, [r7, 0])               # color = COLORS[index]
    strb(r7, [r1, 0])               # write pixel
    add(r1, 1)
    add(r5, 1)                      # world_x += 1
    cmp(r5, r0)
    bne(XLOOP)

    pop({r0})                       # restore CTRL pointer

    # ---- y += 1 in CTRL, loop until MAP_H*TILE_H rows drawn ----
    ldr(r5, [r0, 20])
    add(r5, 1)
    str(r5, [r0, 20])
    cmp(r5, MAP_H * TILE_H)         # 200, fits imm8
    beq(DONE)
    b(YLOOP)                        # unconditional b: bigger branch range
    label(DONE)


@micropython.viper
def draw():
    #fill_asm(fb2, BACKGROUND, FLAG_ADDR)
    display.wait_frame()
    ctrl = ptr32(CTRL)
    game = ptr32(GAME)
    camera_x = game[GAME_CAMERA_X]
    ctrl[CTRL_COLORS] = int(addressof(COLORS))+(camera_x // 1389) * 4
    ctrl[CTRL_CAMERA_X] = camera_x   # refresh camera for this frame
    render_map_asm(CTRL)
    render_stars()
    render_enemy()
    render_player()
    render_bullets()
    render_bombs()
    render_fuel_bar()
    SCREEN.rect(0,0,50,10,BLACK,1)
    draw_num.draw(SCORE, 40, 0)
    SCREEN.rect(200,0,10,10,BLACK,1)
    draw_num.draw(LIVES, 200, 0)
    #draw_num.draw(FPS_CORE0, 290, 200)
    #draw_num.draw(FPS_CORE1, 290, 220)


@micropython.viper
def core0():
    sleep_ms(200)
    game = ptr32(GAME)
    player = ptr32(PLAYER)
    gc.collect()
    pot_ticks = 0
    scroll_ticks = 0
    anim_ticks = 0
    star_ticks = 0
    fuel_ticks = int(ticks_ms())
    score_ticks = int(ticks_ms())
    ambient_ticks = int(ticks_ms())
    ambient_playing = 0
    while not game[GAME_EXIT]:
        ticks = int(ticks_ms())
        if ticks - ambient_ticks > 5000 and ambient_playing == 0:
            snd.play(AMBIENTSND, vol=220, loop=True)
            ambient_playing = 1
        # fuel: wall-clock drain while alive; always eat the delta so the
        # explosion pause doesn't dump a big dt into the tank after respawn
        fuel_dt = ticks - fuel_ticks
        fuel_ticks = ticks
        if player[PLAYER_STATUS] == PLAYER_ALIVE:
            fuel = game[GAME_FUEL] - fuel_dt
            if fuel <= 0:                        # empty tank: same death path
                fuel = 0                         # as a collision
                player[PLAYER_STATUS] = 1
                lose_life()
                anim_ticks = ticks
            game[GAME_FUEL] = fuel
        if ticks - pot_ticks > 30:
            pot_ticks = ticks
            read_gamepad()
            
        if ticks - star_ticks > 1000:
            star_ticks = ticks
            game[GAME_STARTICK] = game[GAME_STARTICK] + 1   # stars keep twinkling while dead    
        if ticks - anim_ticks > ANIM_MS:
            anim_ticks = ticks
            update_enemy_explosions()          # runs during player death too
            update_bomb_anim()                 # flight arc + bomb explosions
            
            status = player[PLAYER_STATUS]
            if status == PLAYER_ALIVE:
                f = game[GAME_FRAME] + 1
                game[GAME_FRAME] = 0 if f >= PLAYER_FRAMES else f
            else:
                status += 1
                if status > EXPLOSION_FRAMES:
                    if game[GAME_LIVES] <= 0:
                        # game over: fresh game from map start
                        game[GAME_LIVES] = START_LIVES
                        game[GAME_SCORE] = 0
                        game[GAME_BONUS] = 0
                        draw_num.set(LIVES, START_LIVES)
                        draw_num.set(SCORE, 0)
                        game[GAME_CAMERA_X] = 0
                    # respawn at start of current 1/6 map section, at a y
                    # scanned clear of the playfield (never spawn in terrain)
                    game[GAME_CAMERA_X] = (game[GAME_CAMERA_X] // SECTION_W) * SECTION_W
                    score_ticks = ticks          # no flying points while dead
                    player[PLAYER_X] = PLAYER_START_X
                    player[PLAYER_Y] = int(find_spawn_y(game[GAME_CAMERA_X]))
                    player[PLAYER_STATUS] = PLAYER_ALIVE
                    game[GAME_FRAME] = 0
                    game[GAME_FUEL] = FUEL_MAX   # full tank on respawn
                else:
                    player[PLAYER_STATUS] = status
        if player[PLAYER_STATUS] == PLAYER_ALIVE:
            if ticks - score_ticks > 1000:       # arcade: 10 pts/sec flying
                score_ticks = ticks
                add_score(PTS_PER_SEC)
            if True or ticks - scroll_ticks >20: # max for testing
                scroll_ticks = ticks
                game[GAME_CAMERA_X] = (game[GAME_CAMERA_X]+1) % (8 * 1389)
            enemy_window()                       # after camera moves, before scans
            update_rockets()
            if check_player_collision():
                player[PLAYER_STATUS] = 1        # start explosion at frame 0
                lose_life()
                anim_ticks = ticks               # full ANIM_MS on first frame
        move_bullets()
        move_bombs()
        draw_num.update_all()
        draw()
        draw_num.set(FPS_CORE0, ticks)
    game[GAME_EXIT] = True
    print('core0 done')

@micropython.viper
def core1():
    sleep_ms(500)
    game = ptr32(GAME)
    pot_ticks = 0
    while not game[GAME_EXIT]:
        ticks = int(ticks_ms())
        display.wait_frame()
        copy_fb(fb2,fb)
        draw_num.set(FPS_CORE1, ticks)
    print('core1 done')

def shutdown():
    GAME[GAME_EXIT] = True
    sleep_ms(100)
    display.deinit()
    machine.freq(150_000_000)
    print('shutdown...')
    sleep_ms(300)
    exit()

def main():
    global snd,START,BULLETSND,BOMBDROP,MULTI,ROCKETSND,AMBIENTSND,BOMBEXPLD
    global PLAYER,GAME,BOMB1TOKEN, BOMB2TOKEN
    from audio_mixer2 import Mixer
    snd = Mixer()
    START     = snd.load("/Scramble/start.wav")
    BULLETSND = snd.load("/Scramble/bullet_.wav")
    BOMBDROP  = snd.load("/Scramble/bombdrop.wav")
    MULTI     = snd.load("/Scramble/multi.wav")
    ROCKETSND = snd.load("/Scramble/rocket.wav")
    AMBIENTSND= snd.load("/Scramble/ambient.wav")
    BOMBEXPLD = snd.load("/Scramble/explode.wav")
    snd.play(START, vol=255)
    BOMB1TOKEN, BOMB2TOKEN = 0,0
    load_files()
    init_ctrl()
    load_enemies()
    build_skyline()
    init_stars()
    PLAYER[PLAYER_X] = 16
    PLAYER[PLAYER_Y] = SCREEN_H // 2
    GAME[GAME_RAND] = ticks_ms() | 1     # seed rocket-launch LCG
    GAME[GAME_FUEL] = FUEL_MAX           # start with a full tank
    GAME[GAME_LIVES] = START_LIVES       # 5 lives, mirrored on screen
    draw_num.set(LIVES, START_LIVES)
    draw_num.set(SCORE, 0)
    _thread.start_new_thread(core1, ())
    core0()
    
if __name__ == '__main__':
    main()
