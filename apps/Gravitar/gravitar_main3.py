# Gravitar-style planet level — 640x480 DVI/gamepad port for RP2350 HSTX
# Converted from lunarlander640DVI.py, same DVI/gamepad structure.
# First level only: fly the zooming landscape, shoot turrets, tractor fuel cells.
# Controls: stick = rotate, RIGHT = thrust, DOWN = fire, UP = tractor, LEFT = restart, SELECT = exit.

import sys
# Add the directory containing your module
sys.path.append('/Gravitar')
from shared_state import fb, display, SCREEN, FLAG_ADDR

from gamepadfast import Gamepad

import machine, _thread, array, micropython, gc
from uctypes import addressof
from time import sleep_ms, ticks_ms, ticks_diff
from sys import exit
from micropython import const
from random import randint
from math import sin, cos, radians

# Level data authored in the planet editor, dropped in as export.py.
# Design-space (640x480) coords + fixed-point scale/offset; see init_terrain.
import export as level

# ── Display / fixed point ─────────────────────────────────────────────────────
MAXSCREEN_X = const(640)
MAXSCREEN_Y = const(480)
SCREEN_W    = const(640)
SCREEN_H    = const(480)

SCALE        = const(13)
WORLD_W      = const(1920)      # 3x DVI screen width
WORLD_H      = const(1600)      # taller: room to fly under the planet

# Closed-polygon planet, authored in the editor's 640x480 design space and
# mapped to world coords via export.SCALE_FP/OFF_X/OFF_Y at init.
# NUM_* are viper loop bounds, so they must stay const and match export.py;
# init-time asserts catch any mismatch after a re-export.
NUM_TVERTS    = const(50)
THRUST_TOK = 0

# ── Physics / game tuning ─────────────────────────────────────────────────────
GRAVITY      = const(22)        # gentler than lander, Gravitar planet pull
THRUST_SHIFT = const(4) #6
MAX_FUEL     = const(999)
SHIP_RADIUS  = const(8)         # terrain kill margin, px (any direction now)

# Camera zoom: 256 = 1x, 128 = 0.5x. Driven by distance to nearest surface.
ZOOM_NEAR_D  = const(110)       # px from surface -> fully zoomed in
ZOOM_FAR_D   = const(430)       # px from surface -> fully zoomed out
ZOOM_RANGE_D = const(320)       # ZOOM_FAR_D - ZOOM_NEAR_D
ZOOM_MIN     = const(128)
ZOOM_MAX     = const(256)
ZOOM_DIFF    = const(128)

# Core timing
PAD_READ_MS   = const(20)
LOGIC_MOVE_MS = const(18)

# ── Missiles (flat arrays) ────────────────────────────────────────────────────
M_X      = const(0)             # world px << SCALE
M_Y      = const(1)
M_VX     = const(2)
M_VY     = const(3)
M_LIFE   = const(4)             # 0 = free slot
M_STRIDE = const(5)

MAX_PMISL  = const(8)
MAX_EMISL  = const(12)
PM_SPEED   = const(9)           # px per logic tick
PM_LIFE    = const(80)
PM_HIT_R   = const(14)          # px, missile vs turret
FIRE_TICKS = const(7)           # logic ticks between shots

EM_SPEED   = const(4)           # px per logic tick
EM_LIFE    = const(300)
EM_HIT_R   = const(10)          # px, missile vs ship

# ── Turrets (flat array) ──────────────────────────────────────────────────────
T_X      = const(0)             # world px (static, not fixed point)
T_Y      = const(1)
T_ALIVE  = const(2)
T_TMR    = const(3)             # fire countdown, logic ticks
T_EXP    = const(4)             # explosion countdown
T_BX     = const(5)             # cached barrel dir x, px*16
T_BY     = const(6)             # cached barrel dir y, px*16
T_NX     = const(7)             # outward surface normal x, *256
T_NY     = const(8)             # outward surface normal y, *256
T_TGX    = const(9)             # surface tangent x, *256
T_TGY    = const(10)            # surface tangent y, *256
T_STRIDE = const(12)

NUM_TURRETS   = const(7)
TURRET_RANGE  = const(800)      # px, fire when ship closer than this
TURRET_RELOAD = const(70)       # base ticks between shots
TURRET_SCORE  = const(250)
CLEAR_BONUS   = const(500)

# ── Fuel cells (flat array) ───────────────────────────────────────────────────
F_X      = const(0)             # world px << SCALE (they get pulled, so fixed point)
F_Y      = const(1)
F_ALIVE  = const(2)
F_STRIDE = const(4)

#NUM_FUELS     = const(12)
TRACTOR_RANGE = const(90)       # px, beam pull radius
TRACTOR_KILL  = const(40)       # px, enemy shots destroyed inside this
COLLECT_R     = const(12)       # px, cell absorbed
FUEL_PER_CELL = const(300)
PULL_SHIFT    = const(3)        # pull speed = dist >> PULL_SHIFT per tick

# ── RGB332 palette, matching the DVI project convention ──────────────────────
BLACK      = const(0x00)
WHITE      = const(0b111_111_11)
GREEN      = const(0b000_111_00)
YELLOW     = const(0b111_111_00)
ORANGE     = const(0b111_100_00)
RED        = const(0b111_000_00)
BROWN      = const(0b101_011_00)
SKYBLUE    = const(0b100_100_11)
CYAN       = const(0b000_111_11)
DIM_WHITE  = const(0b010_010_01)
BACKGROUND = const(BLACK)

# ── Gamepad, active low buttons ───────────────────────────────────────────────
GAMEPAD = array.array('i', [0, 0, 1, 0, 0, 0])  # x, y, restart_ready, thrust, fire, tractor
GAMEPAD_X       = const(0)
GAMEPAD_Y       = const(1)
GAMEPAD_DELAY   = const(2)
GAMEPAD_THRUST  = const(3)
GAMEPAD_FIRE    = const(4)
GAMEPAD_TRACTOR = const(5)

GAMEPAD_SELECT = const(0b0000001)
GAMEPAD_DOWN   = const(0b0000010)
GAMEPAD_LEFT   = const(0b0000100)
GAMEPAD_RIGHT  = const(0b0100000)
GAMEPAD_UP     = const(0b1000000)

GAME_CTL      = array.array('i', [0])
GAME_CTL_EXIT = const(0)

# ── Game state array ──────────────────────────────────────────────────────────
GAME_EXIT    = const(0)
GAME_LIVES   = const(2)
GAME_FPS2    = const(4)
FUEL         = const(5)
SCORE        = const(6)
CAM_X        = const(7)
CAM_Y        = const(8)
STATE        = const(9)       # 0=fly, 2=level clear, 3=game over
TIMER        = const(10)
SURF_D       = const(11)      # ship distance to nearest terrain edge, px
INSIDE       = const(12)      # 1 = ship point is inside the planet polygon
ZOOM         = const(14)
TLEFT        = const(15)      # turrets remaining
RNG          = const(16)      # LCG seed for turret jitter
FIRE_CD      = const(17)      # player fire cooldown
ANIM         = const(18)      # frame counter for beam flicker
GAME_PARAMS  = const(20)

X      = const(0)
Y      = const(1)
DEG    = const(2)
VX     = const(3)
VY     = const(4)
AX     = const(5)
AY     = const(6)
DEAD   = const(7)
MAP_X  = const(8)
MAP_Y  = const(9)
SHIELD = const(10)
OLD_X  = const(11)
OLD_Y  = const(12)
MISL   = const(13)
S_EXP  = const(14)
SEGS   = const(15)
TYPE   = const(16)
BUTTON = const(17)
SLOW   = const(18)
SHIP_PARAMS = const(20)

NUM_STARS = const(100)

# ── Hardware init ─────────────────────────────────────────────────────────────
machine.freq(246_000_000)
machine.mem32[0x40010058] = 2 << 16
machine.mem32[0x40010054] = 1 << 11

gamepad = Gamepad()

char_map = array.array('b', (
    0x0, 0x7e, 0x66, 0x6e, 0x76, 0x66, 0x7e, 0x0,   # 0
    0x0, 0x38, 0x18, 0x18, 0x7e, 0x7e, 0x7e, 0x0,   # 1
    0x0, 0x7e, 0x2, 0x7e, 0x60, 0x66, 0x7e, 0x0,    # 2
    0x0, 0x1e, 0x2, 0x3e, 0x6, 0x6, 0x7e, 0x0,      # 3
    0x0, 0x40, 0x40, 0x40, 0x4c, 0x7e, 0xc, 0x0,    # 4
    0x0, 0x7e, 0x60, 0x7e, 0x6, 0x66, 0x7e, 0x0,    # 5
    0x0, 0x7c, 0x40, 0x7e, 0x62, 0x62, 0x7e, 0x0,   # 6
    0x0, 0x7e, 0x6, 0x6, 0x1e, 0x18, 0x18, 0x0,     # 7
    0x0, 0x3c, 0x24, 0x24, 0x7e, 0x66, 0x7e, 0x0,   # 8
    0x0, 0x7e, 0x42, 0x42, 0x7e, 0x6, 0x6, 0x0))    # 9


@micropython.viper
def show_num_viper(num: int, x_offset: int, y_offset: int, color: int):
    char_ptr = ptr8(char_map)
    screen_ptr = ptr8(SCREEN)
    size = 3
    char = 0
    offset = MAXSCREEN_X * y_offset + x_offset
    first = 1
    while num > 0 or first:
        first = 0
        total = num // 10
        digit = num - (total * 10)
        num = total
        for y in range(8):
            row_data = char_ptr[digit * 8 + y]
            for x in range(8):
                if row_data & (1 << x) > 0:
                    addr = size * y * MAXSCREEN_X + (7 - x) - (char * 8) + offset
                    screen_ptr[addr] = color
                    if size > 1:
                        screen_ptr[MAXSCREEN_X + addr] = color
                        if size > 2:
                            screen_ptr[2 * MAXSCREEN_X + addr] = color
        char += 1


# ── Wait-for-VBlank + fast framebuffer clear, from Star Castle DVI pattern ────
@micropython.asm_thumb
def fill_asm(r0, r1, r2):    # (buffer_addr, 8-bit_color, flag_addr)
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


class Ship:
    def __init__(self):
        self.size = 2
        self.segments = 30
        self.P = array.array('i', [0] * SHIP_PARAMS)
        self.coords = array.array('i', [0] * ((self.segments + 2) * 4))
        self.P[X] = 80 << SCALE
        self.P[Y] = 64 << SCALE
        self.P[DEG] = 90
        self.P[VX] = 0
        self.P[VY] = 0
        self.P[DEAD] = 0
        self.P[TYPE] = 0
        self.P[SEGS] = 6

        # Gravitar-style dart, nose at local angle 180 (nose direction = deg+180,
        # same convention as the lander: thrust is -cos/-sin of DEG).
        # Outline: nose -> right wing tip -> tail notch -> left wing tip -> nose,
        # plus a short center spine.
        self.ship_deg = array.array('H',
            [5, 180, 43, 0, 317, 180, 180] + [0] * 23)
        self.ship_radius = array.array('H',
            [5, 12, 10, 3, 10, 12, 4] + [0] * 23)

    def calc_coords(self):
        segs = self.segments
        size = self.size
        s_deg = self.P[DEG]
        s_type = self.P[TYPE]
        self.P[SEGS] = self.ship_radius[s_type * 30]
        index_coords = 0

        start = 1 + (segs * s_type)
        end = (segs * s_type) + segs
        for i in range(start, end):
            pt_deg = (self.ship_deg[i] + s_deg) % 360

            self.coords[index_coords] = (self.ship_radius[i] * size * ICOS[pt_deg]) >> 14
            self.coords[index_coords + 1] = (self.ship_radius[i] * size * ISIN[pt_deg]) >> 14
            if index_coords > 0:
                self.coords[index_coords + 2] = self.coords[index_coords]
                self.coords[index_coords + 3] = self.coords[index_coords + 1]
                index_coords += 2
            index_coords += 2

    @micropython.viper
    def draw_coords(self):
        game = ptr32(GAME)
        p_ptr = ptr32(self.P)
        pallet = ptr8(PALETTE)
        coords = ptr32(self.coords)
        sip_deg_ptr = ptr16(self.ship_deg)
        isin = ptr32(ISIN)
        icos = ptr32(ICOS)

        zoom = game[ZOOM]
        sx = (((p_ptr[X] >> SCALE) - game[CAM_X]) * zoom) >> 8
        sy = (((p_ptr[Y] >> SCALE) - game[CAM_Y]) * zoom) >> 8
        exp = p_ptr[S_EXP]
        s_deg = p_ptr[DEG]

        if p_ptr[DEAD] == 1:
            return

        color = WHITE
        if exp > 0:
            idx = exp // 10
            if idx < 0:
                idx = 0
            if idx > 31:
                idx = 31
            color = pallet[idx]

        segs = p_ptr[SEGS] * 4 - 4
        for i in range(0, segs, 4):
            if exp > 0:
                if exp == 1:
                    p_ptr[DEAD] = 1
                    p_ptr[S_EXP] = 0
                    return

                p_ptr[S_EXP] = exp - 1
                e_seg = i // 2
                pt_deg = (sip_deg_ptr[e_seg] + s_deg + 180) % 360
                coords[i] += icos[pt_deg] >> 12
                coords[i + 1] += isin[pt_deg] >> 12
                coords[i + 2] += icos[pt_deg] >> 12
                coords[i + 3] += isin[pt_deg] >> 12

            x1 = ((coords[i] * zoom) >> 8) + sx
            y1 = ((coords[i + 1] * zoom) >> 8) + sy
            x2 = ((coords[i + 2] * zoom) >> 8) + sx
            y2 = ((coords[i + 3] * zoom) >> 8) + sy
            if -24 < x1 < MAXSCREEN_X + 24 and -24 < x2 < MAXSCREEN_X + 24 and -24 < y1 < MAXSCREEN_Y + 24 and -24 < y2 < MAXSCREEN_Y + 24:
                SCREEN.line(x1, y1, x2, y2, color)


def init_vtext8():
    global TEXT_SCORE, TEXT_FUEL, TEXT_LIVES, TEXT_ENEMY
    vtext8.bind_screen(fb)
    font = vtext8.Font8("/fonts/COMPUTER.FNT", bind=True)

    TEXT_SCORE = font.make_text("SCORE")
    TEXT_FUEL = font.make_text("FUEL")
    TEXT_LIVES = font.make_text("LIVES")
    TEXT_ENEMY = font.make_text("ENEMY")


def init_imath():
    global ISIN, ICOS
    scale = 1 << SCALE
    ISIN = array.array('i', (int(sin(radians(i)) * scale) for i in range(360)))
    ICOS = array.array('i', (int(cos(radians(i)) * scale) for i in range(360)))


def init_palette():
    global PALETTE
    # 32-step explosion palette, indexed by countdown//10. Bright start, red end.
    PALETTE = array.array('B', [0] * 32)
    for i in range(32):
        if i > 24:
            PALETTE[i] = WHITE
        elif i > 16:
            PALETTE[i] = YELLOW
        elif i > 8:
            PALETTE[i] = ORANGE
        else:
            PALETTE[i] = RED


def init_game():
    global GAME
    GAME = array.array('i', [0] * GAME_PARAMS)
    GAME[GAME_LIVES] = 3
    GAME[FUEL] = MAX_FUEL
    GAME[STATE] = 0
    GAME[ZOOM] = ZOOM_MIN
    GAME[RNG] = 12345


# Planet outline (level.TERRAIN_BASE), turret and fuel tables all come from
# export.py — see init_terrain / init_turrets / init_fuel_cells.


def _inside_py(px, py):
    # Even-odd point-in-polygon, init-time float version.
    inside = False
    j = NUM_TVERTS - 1
    for i in range(NUM_TVERTS):
        xi = TERRAIN_PTS[i * 2]
        yi = TERRAIN_PTS[i * 2 + 1]
        xj = TERRAIN_PTS[j * 2]
        yj = TERRAIN_PTS[j * 2 + 1]
        if (yi > py) != (yj > py) and px < xj + (xi - xj) * (py - yj) / (yi - yj):
            inside = not inside
        j = i
    return inside


def _to_world(dx, dy):
    # Editor's design -> world map: world = (design * SCALE_FP >> 8) + OFF.
    return (dx * level.SCALE_FP >> 8) + level.OFF_X, (dy * level.SCALE_FP >> 8) + level.OFF_Y


def init_terrain():
    global TERRAIN_PTS, STARS

    assert len(level.TERRAIN_BASE) == NUM_TVERTS, 'export NUM_TVERTS != const NUM_TVERTS'
    assert level.WORLD_W == WORLD_W and level.WORLD_H == WORLD_H, 'export WORLD size mismatch'

    TERRAIN_PTS = array.array('i', [0] * (NUM_TVERTS * 2))
    for i in range(NUM_TVERTS):
        bx, by = level.TERRAIN_BASE[i]
        wx, wy = _to_world(bx, by)
        TERRAIN_PTS[i * 2] = wx
        TERRAIN_PTS[i * 2 + 1] = wy

    STARS = array.array('i', [0] * (NUM_STARS * 2))
    for i in range(0, NUM_STARS * 2, 2):
        sx = randint(0, WORLD_W)
        sy = randint(0, WORLD_H)
        while _inside_py(sx, sy):
            sx = randint(0, WORLD_W)
            sy = randint(0, WORLD_H)
        STARS[i] = sx
        STARS[i + 1] = sy


def init_missiles():
    global PMISL, EMISL
    PMISL = array.array('i', [0] * (MAX_PMISL * M_STRIDE))
    EMISL = array.array('i', [0] * (MAX_EMISL * M_STRIDE))


def clear_missiles():
    for i in range(MAX_PMISL * M_STRIDE):
        PMISL[i] = 0
    for i in range(MAX_EMISL * M_STRIDE):
        EMISL[i] = 0


def init_turrets():
    global TURRETS
    assert len(level.TURRETS_BASE) == NUM_TURRETS, 'export NUM_TURRETS != const NUM_TURRETS'
    TURRETS = array.array('i', [0] * (NUM_TURRETS * T_STRIDE))
    for t in range(NUM_TURRETS):
        # (x, y, nx256, ny256, tx256, ty256, type): pos design-space, dirs *256.
        # Uniform scale keeps the design-space normal/tangent valid in world.
        dx, dy, nx256, ny256, tgx256, tgy256, _typ = level.TURRETS_BASE[t]
        wx, wy = _to_world(dx, dy)
        b = t * T_STRIDE
        TURRETS[b + T_X] = wx
        TURRETS[b + T_Y] = wy
        TURRETS[b + T_ALIVE] = 1
        TURRETS[b + T_TMR] = TURRET_RELOAD + randint(0, 60)
        TURRETS[b + T_EXP] = 0
        TURRETS[b + T_NX] = nx256
        TURRETS[b + T_NY] = ny256
        TURRETS[b + T_TGX] = tgx256
        TURRETS[b + T_TGY] = tgy256
        TURRETS[b + T_BX] = nx256 >> 4      # unit*16, barrel rests on the normal
        TURRETS[b + T_BY] = ny256 >> 4
    GAME[TLEFT] = NUM_TURRETS


def init_fuel_cells():
    global FUELS, NUM_FUELS
    #assert len(level.FUELS_BASE) == NUM_FUELS, 'export NUM_FUELS != const NUM_FUELS'
    NUM_FUELS = int(len(level.FUELS_BASE))
    FUELS = array.array('i', [0] * (NUM_FUELS * F_STRIDE))
    for f in range(NUM_FUELS):
        # (x, y, amount): design-space hover position, off-surface offset baked in.
        # `amount` unused — the game grants FUEL_PER_CELL on collect.
        dx, dy, _amt = level.FUELS_BASE[f]
        wx, wy = _to_world(dx, dy)
        b = f * F_STRIDE
        FUELS[b + F_X] = wx << SCALE
        FUELS[b + F_Y] = wy << SCALE
        FUELS[b + F_ALIVE] = 1


def init_ship():
    global ships, SHIP_P
    ships = [Ship()]
    SHIP_P = ships[0].P
    reset_ship()


def reset_ship():
    ship = ships[0]
    ship.P[X] = (WORLD_W >> 1) << SCALE
    ship.P[Y] = 160 << SCALE
    ship.P[VX] = 0
    ship.P[VY] = 0
    ship.P[DEG] = 90
    ship.P[TYPE] = 0
    ship.P[DEAD] = 0
    ship.P[S_EXP] = 0
    ship.size = 2
    ship.calc_coords()

    clear_missiles()
    GAME[STATE] = 0
    GAME[FIRE_CD] = 0
    GAME[ZOOM] = ZOOM_MIN
    GAME[CAM_X] = 0
    GAME[CAM_Y] = 0
    surface_query()
    update_zoom()
    update_camera()
  


def new_level():
    init_terrain()
    init_turrets()
    init_fuel_cells()
    reset_ship()


def restart_game():
    GAME[GAME_LIVES] = 3
    GAME[SCORE] = 0
    GAME[STATE] = 0
    GAME[TIMER] = 0
    GAME[FUEL] = MAX_FUEL
    new_level()


# ── Input ─────────────────────────────────────────────────────────────────────
def read_gamepad():
    global THRUST_TOK
    gamepad.read()
    buttons = int(gamepad.buttons)

    if not (buttons & GAMEPAD_SELECT):
        GAME[GAME_EXIT] = 1
        return

    # LEFT = start over (debounced)
    if not (buttons & GAMEPAD_LEFT):
        if GAMEPAD[GAMEPAD_DELAY]:
            GAMEPAD[GAMEPAD_DELAY] = 0
            restart_game()
        return
    else:
        GAMEPAD[GAMEPAD_DELAY] = 1

    ship = ships[0]
    thrust_old = GAMEPAD[GAMEPAD_THRUST]
    GAMEPAD[GAMEPAD_THRUST] = 0
    GAMEPAD[GAMEPAD_FIRE] = 0
    tractor_old = GAMEPAD[GAMEPAD_TRACTOR]
    GAMEPAD[GAMEPAD_TRACTOR] = 0

    if GAME[STATE] != 0 or ship.P[S_EXP] > 0 or ship.P[DEAD] == 1:
        return

    x_inc = int(gamepad.x) >> 7
    if -2 < x_inc < 2:
        x_inc = 0
    if x_inc > 7:
        x_inc = 7
    if x_inc < -7:
        x_inc = -7

    GAMEPAD[GAMEPAD_X] = x_inc
    GAMEPAD[GAMEPAD_Y] = int(gamepad.y) >> 6

    deg = ship.P[DEG] + x_inc
    if deg >= 360 or deg < 0:
        deg %= 360
    ship.P[DEG] = deg

    if not (buttons & GAMEPAD_RIGHT) and GAME[FUEL] > 0:
        GAMEPAD[GAMEPAD_THRUST] = 1
        GAME[FUEL] -= 1
        ship.P[VX] -= ICOS[deg] >> THRUST_SHIFT
        ship.P[VY] -= ISIN[deg] >> THRUST_SHIFT
        if thrust_old == 0:
            THRUST_TOK = snd.play(THRUST, vol=220,loop=True)
    elif thrust_old == 1:
        snd.stop(THRUST_TOK)

    if not (buttons & GAMEPAD_DOWN):
        GAMEPAD[GAMEPAD_FIRE] = 1

    if not (buttons & GAMEPAD_UP) and GAME[FUEL] > 0:
        GAMEPAD[GAMEPAD_TRACTOR] = 1
        GAME[FUEL] -= 1
        if tractor_old == 0:
            snd.play(TRACTOR, vol=220)

    ship.calc_coords()


# ── Player fire, flat-array missile spawn ─────────────────────────────────────
@micropython.viper
def fire_missile():
    game = ptr32(GAME)
    gpad = ptr32(GAMEPAD)
    misl = ptr32(PMISL)
    ship = ptr32(SHIP_P)
    icos = ptr32(ICOS)
    isin = ptr32(ISIN)

    if game[FIRE_CD] > 0:
        game[FIRE_CD] -= 1
    if gpad[GAMEPAD_FIRE] == 0:
        return
    if game[FIRE_CD] > 0:
        return
    if game[STATE] != 0 or ship[S_EXP] > 0 or ship[DEAD] == 1:
        return

    # Find a free slot.
    slot = -1
    i = 0
    while i < MAX_PMISL:
        if misl[i * M_STRIDE + M_LIFE] <= 0:
            slot = i * M_STRIDE
            i = MAX_PMISL
        i += 1
    if slot < 0:
        return

    deg = ship[DEG]
    # Nose direction is (-cos, -sin), spawn just off the nose (26 px out).
    off_x = (icos[deg] * 26) >> SCALE
    off_y = (isin[deg] * 26) >> SCALE
    misl[slot + M_X] = ship[X] - (off_x << SCALE)
    misl[slot + M_Y] = ship[Y] - (off_y << SCALE)
    misl[slot + M_VX] = ship[VX] - icos[deg] * PM_SPEED
    misl[slot + M_VY] = ship[VY] - isin[deg] * PM_SPEED
    misl[slot + M_LIFE] = PM_LIFE
    game[FIRE_CD] = FIRE_TICKS
    snd.play(SHOOT1, vol=220)
    #print('player missile',slot)


# ── Closed-polygon terrain queries ────────────────────────────────────────────
@micropython.viper
def pt_inside(px: int, py: int) -> int:
    # Even-odd ray cast against the closed planet outline.
    poly = ptr32(TERRAIN_PTS)
    inside = 0
    j = NUM_TVERTS - 1
    i = 0
    while i < NUM_TVERTS:
        yi = poly[i * 2 + 1]
        yj = poly[j * 2 + 1]
        above_i = 1 if yi > py else 0
        above_j = 1 if yj > py else 0
        if above_i != above_j:
            xi = poly[i * 2]
            xj = poly[j * 2]
            xc = xj + (xi - xj) * (py - yj) // (yi - yj)
            if px < xc:
                inside ^= 1
        j = i
        i += 1
    return inside


@micropython.viper
def surface_query():
    # Nearest distance (octagonal approx) from the ship to any terrain edge,
    # plus inside/outside flag. Drives both zoom and hull collision.
    ship = ptr32(SHIP_P)
    poly = ptr32(TERRAIN_PTS)
    game = ptr32(GAME)

    px = ship[X] >> SCALE
    py = ship[Y] >> SCALE

    best = int(1 << 30)
    i = 0
    while i < NUM_TVERTS:
        j = i + 1
        if j == NUM_TVERTS:
            j = 0
        x1 = poly[i * 2]
        y1 = poly[i * 2 + 1]
        ex = poly[j * 2] - x1
        ey = poly[j * 2 + 1] - y1
        rx = px - x1
        ry = py - y1
        dot = rx * ex + ry * ey
        cx = x1
        cy = y1
        if dot > 0:
            len2 = ex * ex + ey * ey
            if dot >= len2:
                cx = x1 + ex
                cy = y1 + ey
            else:
                cx = x1 + ex * dot // len2
                cy = y1 + ey * dot // len2
        dx = px - cx
        dy = py - cy
        if dx < 0:
            dx = -dx
        if dy < 0:
            dy = -dy
        hi = dx if dx > dy else dy
        lo = dx if dx < dy else dy
        d = hi + (lo >> 1)
        if d < best:
            best = d
        i += 1

    game[SURF_D] = best
    game[INSIDE] = int(pt_inside(px, py))


# ── Player missiles: move, terrain kill, turret hits ──────────────────────────
@micropython.viper
def move_pmissiles():
    misl = ptr32(PMISL)
    turrets = ptr32(TURRETS)
    game = ptr32(GAME)

    m = 0
    while m < MAX_PMISL:
        b = m * M_STRIDE
        life = misl[b + M_LIFE]
        if life > 0:
            misl[b + M_LIFE] = life - 1
            mx = misl[b + M_X] + misl[b + M_VX]
            my = misl[b + M_Y] + misl[b + M_VY]
            misl[b + M_X] = mx
            misl[b + M_Y] = my

            px = mx >> SCALE
            py = my >> SCALE

            # Off-world or into terrain: gone.
            if px < 0 or px >= WORLD_W or py < 0 or py >= WORLD_H:
                misl[b + M_LIFE] = 0
            else:
                if int(pt_inside(px, py)) == 1:
                    misl[b + M_LIFE] = 0
                else:
                    # Turret hits: box around the bunker body, 5 px out along
                    # the surface normal.
                    t = 0
                    while t < NUM_TURRETS:
                        tb = t * T_STRIDE
                        if turrets[tb + T_ALIVE] == 1:
                            dx = px - (turrets[tb + T_X] + ((turrets[tb + T_NX] * 5) >> 8))
                            dy = py - (turrets[tb + T_Y] + ((turrets[tb + T_NY] * 5) >> 8))
                            if dx < 0:
                                dx = -dx
                            if dy < 0:
                                dy = -dy
                            if dx < PM_HIT_R and dy < PM_HIT_R:
                                turrets[tb + T_ALIVE] = 0
                                turrets[tb + T_EXP] = 50
                                misl[b + M_LIFE] = 0
                                game[SCORE] += TURRET_SCORE
                                game[TLEFT] -= 1
                                t = NUM_TURRETS
                                snd.play(EXPLODE, vol=220)
                                
                        t += 1
        m += 1


# ── Enemy missiles: move, terrain kill, ship hit, tractor destroy ─────────────
@micropython.viper
def move_emissiles():
    global THRUST_TOK
    misl = ptr32(EMISL)
    ship = ptr32(SHIP_P)
    game = ptr32(GAME)
    gpad = ptr32(GAMEPAD)

    sx = ship[X] >> SCALE
    sy = ship[Y] >> SCALE
    ship_ok = 1 if (game[STATE] == 0 and ship[S_EXP] == 0 and ship[DEAD] == 0) else 0
    tractor = gpad[GAMEPAD_TRACTOR]

    m = 0
    while m < MAX_EMISL:
        b = m * M_STRIDE
        life = misl[b + M_LIFE]
        if life > 0:
            misl[b + M_LIFE] = life - 1
            mx = misl[b + M_X] + misl[b + M_VX]
            my = misl[b + M_Y] + misl[b + M_VY]
            misl[b + M_X] = mx
            misl[b + M_Y] = my

            px = mx >> SCALE
            py = my >> SCALE

            if px < 0 or px >= WORLD_W or py < 0 or py >= WORLD_H:
                misl[b + M_LIFE] = 0
            else:
                if int(pt_inside(px, py)) == 1:
                    misl[b + M_LIFE] = 0
                elif ship_ok:
                    dx = px - sx
                    dy = py - sy
                    if dx < 0:
                        dx = -dx
                    if dy < 0:
                        dy = -dy
                    # Tractor beam doubles as a shield: absorb close shots.
                    if tractor == 1 and dx < TRACTOR_KILL and dy < TRACTOR_KILL:
                        misl[b + M_LIFE] = 0
                    elif dx < EM_HIT_R and dy < EM_HIT_R:
                        misl[b + M_LIFE] = 0
                        ship[S_EXP] = 300
                        snd.stop(THRUST_TOK)
                        snd.play(EXPLODE, vol=220)
        m += 1


# ── Turrets: reload timers, aim cache, fire at the ship ───────────────────────
@micropython.viper
def update_turrets():
    turrets = ptr32(TURRETS)
    misl = ptr32(EMISL)
    ship = ptr32(SHIP_P)
    game = ptr32(GAME)

    sx = ship[X] >> SCALE
    sy = ship[Y] >> SCALE
    ship_ok = 1 if (game[STATE] == 0 and ship[S_EXP] == 0 and ship[DEAD] == 0) else 0

    t = 0
    while t < NUM_TURRETS:
        b = t * T_STRIDE

        exp = turrets[b + T_EXP]
        if exp > 0:
            turrets[b + T_EXP] = exp - 1

        if turrets[b + T_ALIVE] == 1:
            dx = sx - turrets[b + T_X]
            dy = sy - turrets[b + T_Y]
            adx = dx if dx >= 0 else -dx
            ady = dy if dy >= 0 else -dy
            hi = adx if adx > ady else ady
            lo = adx if adx < ady else ady
            dist = hi + (lo >> 1)          # fast octagonal distance approx
            if dist < 1:
                dist = 1

            # Ship on the outward side of this bunker's surface?
            outward = dx * turrets[b + T_NX] + dy * turrets[b + T_NY]

            # Cache barrel direction for the renderer, px*16. Track the ship
            # only when it's on the outward side, else rest on the normal.
            if outward > 0:
                turrets[b + T_BX] = (dx * 16) // dist
                turrets[b + T_BY] = (dy * 16) // dist
            else:
                turrets[b + T_BX] = turrets[b + T_NX] >> 4
                turrets[b + T_BY] = turrets[b + T_NY] >> 4

            tmr = turrets[b + T_TMR]
            if tmr > 0:
                turrets[b + T_TMR] = tmr - 1
            elif ship_ok == 1 and outward > 0 and dist < TURRET_RANGE:
                # Find a free enemy missile slot.
                slot = -1
                i = 0
                while i < MAX_EMISL:
                    if misl[i * M_STRIDE + M_LIFE] <= 0:
                        slot = i * M_STRIDE
                        i = MAX_EMISL
                    i += 1
                if slot >= 0:
                    misl[slot + M_X] = (turrets[b + T_X] + ((turrets[b + T_NX] * 12) >> 8)) << SCALE
                    misl[slot + M_Y] = (turrets[b + T_Y] + ((turrets[b + T_NY] * 12) >> 8)) << SCALE
                    misl[slot + M_VX] = ((dx << SCALE) * EM_SPEED) // dist
                    misl[slot + M_VY] = ((dy << SCALE) * EM_SPEED) // dist
                    misl[slot + M_LIFE] = EM_LIFE
                    snd.play(SHOOT2, vol=220)
                    #print('enemy missile',slot)
                # Reload with LCG jitter.
                #seed = game[RNG] * 1103515245 + 12345
                seed = int(randint(-1<<29,1<<29))
                game[RNG] = seed
                turrets[b + T_TMR] = TURRET_RELOAD + ((seed >> 16) & 0x3F)
        t += 1


# ── Tractor beam: pull and collect fuel cells ─────────────────────────────────
@micropython.viper
def update_tractor():
    gpad = ptr32(GAMEPAD)
    fuels = ptr32(FUELS)
    ship = ptr32(SHIP_P)
    game = ptr32(GAME)

    if gpad[GAMEPAD_TRACTOR] == 0:
        return
    if game[STATE] != 0 or ship[S_EXP] > 0 or ship[DEAD] == 1:
        return

    sfx = ship[X]
    sfy = ship[Y]
    sx = sfx >> SCALE
    sy = sfy >> SCALE

    f = 0
    while f < int(NUM_FUELS):
        b = f * F_STRIDE
        if fuels[b + F_ALIVE] == 1:
            dx = sx - (fuels[b + F_X] >> SCALE)
            dy = sy - (fuels[b + F_Y] >> SCALE)
            adx = dx if dx >= 0 else -dx
            ady = dy if dy >= 0 else -dy
            if adx < TRACTOR_RANGE and ady < TRACTOR_RANGE:
                if adx < COLLECT_R and ady < COLLECT_R:
                    fuels[b + F_ALIVE] = 0
                    fuel = game[FUEL] + FUEL_PER_CELL
                    if fuel > MAX_FUEL:
                        fuel = MAX_FUEL
                    game[FUEL] = fuel
                    snd.play(BEEP3, vol=220)
                else:
                    fuels[b + F_X] += (sfx - fuels[b + F_X]) >> PULL_SHIFT
                    fuels[b + F_Y] += (sfy - fuels[b + F_Y]) >> PULL_SHIFT
        f += 1


# ── Ship physics: gravity, terrain and turret collisions ──────────────────────
def move_ship():
    global THRUST_TOK
    ship = ships[0]
    state = GAME[STATE]

    if state == 3:
        return

    if state == 2:                     # level clear
        GAME[TIMER] -= 1
        if GAME[TIMER] <= 0:
            new_level()
        return

    if ship.P[S_EXP] > 0:
        return

    if ship.P[DEAD] == 1:
        GAME[GAME_LIVES] -= 1
        if GAME[GAME_LIVES] <= 0:
            GAME[STATE] = 3
            return
        reset_ship()
        return

    ship.P[VY] += GRAVITY
    ship.P[X] += ship.P[VX]
    ship.P[Y] += ship.P[VY]

    if ship.P[X] < 0:
        ship.P[X] = 0
        ship.P[VX] = 0
    if ship.P[X] > WORLD_W << SCALE:
        ship.P[X] = WORLD_W << SCALE
        ship.P[VX] = 0
    if ship.P[Y] < 0:
        ship.P[Y] = 0
        ship.P[VY] = 0
    if ship.P[Y] > WORLD_H << SCALE:
        ship.P[Y] = WORLD_H << SCALE
        ship.P[VY] = 0

    world_x = ship.P[X] >> SCALE
    world_y = ship.P[Y] >> SCALE

    # Touch the planet anywhere and you die.
    surface_query()
    if GAME[INSIDE] == 1 or GAME[SURF_D] < SHIP_RADIUS:
        ship.P[S_EXP] = 300
        ship.calc_coords()
        snd.stop(THRUST_TOK)
        snd.play(EXPLODE, vol=220)
        return

    # Clipping a bunker is also fatal.
    for t in range(NUM_TURRETS):
        b = t * T_STRIDE
        if TURRETS[b + T_ALIVE] == 1:
            dx = world_x - (TURRETS[b + T_X] + (TURRETS[b + T_NX] * 5 >> 8))
            dy = world_y - (TURRETS[b + T_Y] + (TURRETS[b + T_NY] * 5 >> 8))
            if -14 < dx < 14 and -14 < dy < 14:
                ship.P[S_EXP] = 300
                ship.calc_coords()
                return

    # All bunkers destroyed: planet cleared.
    if GAME[TLEFT] <= 0:
        GAME[SCORE] += CLEAR_BONUS
        GAME[STATE] = 2
        GAME[TIMER] = 180

    ship.calc_coords()


def update_zoom():
    # Distance to the nearest terrain edge (any direction), from surface_query.
    d = GAME[SURF_D]

    if d <= ZOOM_NEAR_D:
        zoom = ZOOM_MAX
    elif d >= ZOOM_FAR_D:
        zoom = ZOOM_MIN
    else:
        zoom = ZOOM_MAX - ((d - ZOOM_NEAR_D) * ZOOM_DIFF) // ZOOM_RANGE_D

    GAME[ZOOM] += (zoom - GAME[ZOOM]) >> 2


def update_camera():
    zoom = GAME[ZOOM]
    if zoom < 64:
        zoom = 64

    vis_w = (MAXSCREEN_X * 256) // zoom
    vis_h = (MAXSCREEN_Y * 256) // zoom

    target_x = (ships[0].P[X] >> SCALE) - (vis_w >> 1)
    target_y = (ships[0].P[Y] >> SCALE) - (vis_h >> 1)

    if target_x < 0:
        target_x = 0
    if target_y < 0:
        target_y = 0

    max_cx = WORLD_W - vis_w
    max_cy = WORLD_H - vis_h
    if max_cx < 0:
        max_cx = 0
    if max_cy < 0:
        max_cy = 0

    if target_x > max_cx:
        target_x = max_cx
    if target_y > max_cy:
        target_y = max_cy

    GAME[CAM_X] += (target_x - GAME[CAM_X]) >> 2
    GAME[CAM_Y] += (target_y - GAME[CAM_Y]) >> 2


# ── Rendering ─────────────────────────────────────────────────────────────────
@micropython.viper
def draw_stars():
    stars = ptr32(STARS)
    game = ptr32(GAME)
    screen = ptr8(fb)

    cam_x = game[CAM_X]
    cam_y = game[CAM_Y]
    zoom = game[ZOOM]

    i = 0
    limit = int(NUM_STARS * 2)
    while i < limit:
        sx = ((stars[i] - cam_x) * zoom) >> 8
        sy = ((stars[i + 1] - cam_y) * zoom) >> 8
        if sx >= 0 and sx < MAXSCREEN_X and sy >= 0 and sy < MAXSCREEN_Y:
            screen[sy * MAXSCREEN_X + sx] = WHITE
        i += 2


@micropython.viper
def draw_terrain():
    poly = ptr32(TERRAIN_PTS)
    game = ptr32(GAME)

    cam_x = game[CAM_X]
    cam_y = game[CAM_Y]
    zoom = game[ZOOM]

    i = 0
    while i < NUM_TVERTS:
        j = i + 1
        if j == NUM_TVERTS:
            j = 0
        x1 = ((poly[i * 2] - cam_x) * zoom) >> 8
        y1 = ((poly[i * 2 + 1] - cam_y) * zoom) >> 8
        x2 = ((poly[j * 2] - cam_x) * zoom) >> 8
        y2 = ((poly[j * 2 + 1] - cam_y) * zoom) >> 8

        # Trivial reject: both endpoints off the same screen edge.
        rej = 0
        if x1 < 0 and x2 < 0:
            rej = 1
        if x1 >= MAXSCREEN_X and x2 >= MAXSCREEN_X:
            rej = 1
        if y1 < 0 and y2 < 0:
            rej = 1
        if y1 >= MAXSCREEN_Y and y2 >= MAXSCREEN_Y:
            rej = 1
        if rej == 0:
            SCREEN.line(x1, y1, x2, y2, GREEN)
        i += 1


@micropython.viper
def draw_turrets():
    turrets = ptr32(TURRETS)
    game = ptr32(GAME)
    pallet = ptr8(PALETTE)

    cam_x = game[CAM_X]
    cam_y = game[CAM_Y]
    zoom = game[ZOOM]

    t = 0
    while t < NUM_TURRETS:
        b = t * T_STRIDE
        alive = turrets[b + T_ALIVE]
        exp = turrets[b + T_EXP]
        if alive == 1 or exp > 0:
            sx = ((turrets[b + T_X] - cam_x) * zoom) >> 8
            sy = ((turrets[b + T_Y] - cam_y) * zoom) >> 8
            if sx > -32 and sx < MAXSCREEN_X + 32 and sy > -32 and sy < MAXSCREEN_Y + 32:
                if alive == 1:
                    w0 = (10 * zoom) >> 8       # base half width
                    w1 = (5 * zoom) >> 8        # top half width
                    h = (9 * zoom) >> 8         # bunker height
                    nx = turrets[b + T_NX]      # outward normal, *256
                    ny = turrets[b + T_NY]
                    tgx = turrets[b + T_TGX]    # surface tangent, *256
                    tgy = turrets[b + T_TGY]
                    # Trapezoid bunker, standing on the local surface.
                    blx = sx - ((tgx * w0) >> 8)
                    bly = sy - ((tgy * w0) >> 8)
                    brx = sx + ((tgx * w0) >> 8)
                    bry = sy + ((tgy * w0) >> 8)
                    topx = sx + ((nx * h) >> 8)
                    topy = sy + ((ny * h) >> 8)
                    tlx = topx - ((tgx * w1) >> 8)
                    tly = topy - ((tgy * w1) >> 8)
                    trx = topx + ((tgx * w1) >> 8)
                    try_ = topy + ((tgy * w1) >> 8)
                    SCREEN.line(blx, bly, tlx, tly, RED)
                    SCREEN.line(tlx, tly, trx, try_, RED)
                    SCREEN.line(trx, try_, brx, bry, RED)
                    SCREEN.line(blx, bly, brx, bry, RED)
                    # Barrel toward the ship (cached px*16 direction).
                    bl = (12 * zoom) >> 8
                    bx = topx + (turrets[b + T_BX] * bl) // 16
                    by = topy + (turrets[b + T_BY] * bl) // 16
                    SCREEN.line(topx, topy, bx, by, ORANGE)
                else:
                    # Expanding 8-spoke burst.
                    idx = exp // 2
                    if idx > 31:
                        idx = 31
                    color = pallet[idx]
                    r = (((50 - exp) * zoom) >> 8) >> 1
                    d = (r * 3) >> 2
                    SCREEN.line(sx - r, sy, sx + r, sy, color)
                    SCREEN.line(sx, sy - r, sx, sy + r, color)
                    SCREEN.line(sx - d, sy - d, sx + d, sy + d, color)
                    SCREEN.line(sx - d, sy + d, sx + d, sy - d, color)
        t += 1


@micropython.viper
def draw_fuel_cells():
    fuels = ptr32(FUELS)
    game = ptr32(GAME)

    cam_x = game[CAM_X]
    cam_y = game[CAM_Y]
    zoom = game[ZOOM]

    f = 0
    while f < int(NUM_FUELS):
        b = f * F_STRIDE
        if fuels[b + F_ALIVE] == 1:
            sx = (((fuels[b + F_X] >> SCALE) - cam_x) * zoom) >> 8
            sy = (((fuels[b + F_Y] >> SCALE) - cam_y) * zoom) >> 8
            if sx > 8 and sx < MAXSCREEN_X - 8 and sy > 8 and sy < MAXSCREEN_Y - 8:
                w = (12 * zoom) >> 8 # 6
                h = (10 * zoom) >> 8 # 5
                if w < 3:
                    w = 3
                if h < 2:
                    h = 2
                SCREEN.rect(sx - w, sy - h, w * 2, h * 2, CYAN)
                SCREEN.line(sx - w + 1, sy, sx + w - 2, sy, SKYBLUE)
        f += 1


@micropython.viper
def draw_missiles():
    pmisl = ptr32(PMISL)
    emisl = ptr32(EMISL)
    game = ptr32(GAME)
    screen = ptr8(fb)

    cam_x = game[CAM_X]
    cam_y = game[CAM_Y]
    zoom = game[ZOOM]

    m = 0
    while m < MAX_PMISL:
        b = m * M_STRIDE
        if pmisl[b + M_LIFE] > 0:
            sx = (((pmisl[b + M_X] >> SCALE) - cam_x) * zoom) >> 8
            sy = (((pmisl[b + M_Y] >> SCALE) - cam_y) * zoom) >> 8
            if sx >= 0 and sx < MAXSCREEN_X - 1 and sy >= 0 and sy < MAXSCREEN_Y - 1:
                addr = sy * MAXSCREEN_X + sx
                screen[addr] = WHITE
                screen[addr + 1] = WHITE
                screen[addr + MAXSCREEN_X] = WHITE
                screen[addr + MAXSCREEN_X + 1] = WHITE
        m += 1

    m = 0
    while m < MAX_EMISL:
        b = m * M_STRIDE
        if emisl[b + M_LIFE] > 0:
            sx = (((emisl[b + M_X] >> SCALE) - cam_x) * zoom) >> 8
            sy = (((emisl[b + M_Y] >> SCALE) - cam_y) * zoom) >> 8
            if sx >= 0 and sx < MAXSCREEN_X - 1 and sy >= 0 and sy < MAXSCREEN_Y - 1:
                addr = sy * MAXSCREEN_X + sx
                screen[addr] = RED
                screen[addr + 1] = RED
                screen[addr + MAXSCREEN_X] = RED
                screen[addr + MAXSCREEN_X + 1] = RED
        m += 1


@micropython.viper
def draw_tractor():
    gpad = ptr32(GAMEPAD)
    game = ptr32(GAME)
    ship = ptr32(SHIP_P)
    icos = ptr32(ICOS)
    isin = ptr32(ISIN)

    if gpad[GAMEPAD_TRACTOR] == 0:
        return
    if game[STATE] != 0 or ship[S_EXP] > 0 or ship[DEAD] == 1:
        return

    zoom = game[ZOOM]
    deg = ship[DEG]
    anim = game[ANIM]

    sx = (((ship[X] >> SCALE) - game[CAM_X]) * zoom) >> 8
    sy = (((ship[Y] >> SCALE) - game[CAM_Y]) * zoom) >> 8

    # Beam projects from the ship's tail: +(cos, sin) of DEG.
    beam_len = ((36 + ((anim & 7) << 1)) * zoom) >> 8
    spread = (14 * zoom) >> 8
    if spread < 3:
        spread = 3

    tipx = sx + ((icos[deg] * beam_len) >> SCALE)
    tipy = sy + ((isin[deg] * beam_len) >> SCALE)
    # Perpendicular offset for the V mouth.
    perp = (deg + 90) % 360
    ox = (icos[perp] * spread) >> SCALE
    oy = (isin[perp] * spread) >> SCALE

    if sx > -64 and sx < MAXSCREEN_X + 64 and sy > -64 and sy < MAXSCREEN_Y + 64:
        SCREEN.line(sx, sy, tipx + ox, tipy + oy, SKYBLUE)
        SCREEN.line(sx, sy, tipx - ox, tipy - oy, SKYBLUE)
        if anim & 2:
            SCREEN.line(tipx + ox, tipy + oy, tipx - ox, tipy - oy, CYAN)


FLAME_LENGTH = const(24)
FLAME_MIN    = const(9)
FLAME_START  = const(10)


@micropython.viper
def draw_flame():
    ship = ptr32(ships[0].P)
    game = ptr32(GAME)
    gpad = ptr32(GAMEPAD)
    isin = ptr32(ISIN)
    icos = ptr32(ICOS)

    if game[FUEL] <= 0:
        return
    if game[STATE] != 0:
        return
    if gpad[GAMEPAD_THRUST] == 0:
        return

    zoom = game[ZOOM]
    deg = ship[DEG]

    sx = (((ship[X] >> SCALE) - game[CAM_X]) * zoom) >> 8
    sy = (((ship[Y] >> SCALE) - game[CAM_Y]) * zoom) >> 8

    f_start = (FLAME_START * zoom) >> 8
    noz_x = sx + ((icos[deg] * f_start) >> SCALE)
    noz_y = sy + ((isin[deg] * f_start) >> SCALE)

    flen_raw = FLAME_MIN + int(randint(0, FLAME_LENGTH))
    flen = (flen_raw * zoom) >> 8
    if flen < 4:
        flen = 4

    tip_x = noz_x + ((icos[deg] * flen) >> SCALE)
    tip_y = noz_y + ((isin[deg] * flen) >> SCALE)

    spread = (4 * zoom) >> 8
    if spread < 1:
        spread = 1

    if noz_x > 0 and noz_x < MAXSCREEN_X and noz_y > 0 and noz_y < MAXSCREEN_Y:
        SCREEN.line(noz_x - spread, noz_y, tip_x, tip_y, YELLOW)
        SCREEN.line(noz_x + spread, noz_y, tip_x, tip_y, ORANGE)
        SCREEN.line(noz_x, noz_y, tip_x, tip_y, RED)


@micropython.viper
def draw_hud():
    game = ptr32(GAME)
    fuel = game[FUEL]
    if fuel < 0:
        fuel = 0

    fuel_color = GREEN
    if fuel < 200:
        fuel_color = ORANGE
    if fuel < 50:
        fuel_color = RED

    vtext8.text(TEXT_FUEL, 12, 4, GREEN)
    show_num_viper(fuel, 34, 24, fuel_color)

    vtext8.text(TEXT_ENEMY, 140, 4, RED)
    show_num_viper(game[TLEFT], 165, 24, RED)

    vtext8.text(TEXT_SCORE, 415, 4, WHITE)
    show_num_viper(game[SCORE], 435, 24, YELLOW)

    vtext8.text(TEXT_LIVES, 565, 4, WHITE)
    show_num_viper(game[GAME_LIVES], 590, 24, YELLOW)

    if game[STATE] == 2:
        hershey.text('PLANET CLEARED', 190, 145, 800, GREEN)
        hershey.text('BONUS 500', 250, 205, 600, YELLOW)
    elif game[STATE] == 3:
        hershey.text('GAME OVER', 220, 145, 950, YELLOW)
        hershey.text('LEFT TO RESTART', 230, 210, 520, SKYBLUE)


@micropython.viper
def draw():
    game = ptr32(GAME)
    fill_asm(fb, BACKGROUND, FLAG_ADDR)
    game[ANIM] += 1
    draw_hud()
    draw_stars()
    draw_terrain()
    draw_fuel_cells()
    draw_turrets()
    draw_missiles()

    if game[STATE] != 3 and int(ships[0].P[DEAD]) != 1:
        draw_tractor()
        draw_flame()
        ships[0].draw_coords()


# ── Cores ─────────────────────────────────────────────────────────────────────
@micropython.viper
def core0():
    game = ptr32(GAME)
    pad_ticks = 0
    logic_ticks = 0

    while not game[GAME_EXIT]:
        sleep_ms(1)
        now = int(ticks_ms())

        if now - pad_ticks > PAD_READ_MS:
            pad_ticks = now
            read_gamepad()

        if now - logic_ticks > LOGIC_MOVE_MS:
            logic_ticks = now
            move_ship()
            fire_missile()
            move_pmissiles()
            move_emissiles()
            update_turrets()
            update_tractor()
            update_zoom()
            update_camera()

    game[GAME_EXIT] = 1
    print('core0 done')


@micropython.viper
def core1():
    game = ptr32(GAME)
    sleep_ms(500)
    while not game[GAME_EXIT]:
        draw()
    print('core1 done')


def shutdown():
    global hershey
    GAME[GAME_EXIT] = 1
    sleep_ms(100)
    try:
        display.deinit()
    except Exception:
        pass
    try:
        del hershey
    except Exception:
        pass
    machine.freq(150_000_000)
    print('shutdown...')
    sleep_ms(300)
    exit()


#if __name__ == '__main__':
from audio_mixer import Mixer
snd = Mixer()
SHOOT1 = snd.load("/Gravitar/shoot1_mono.wav")
SHOOT2 = snd.load("/Gravitar/shoot2_mono.wav")
EXPLODE = snd.load("/Gravitar/explode1_mono.wav")
BEEP3 = snd.load("/Gravitar/beep3_mono.wav")
TRACTOR = snd.load("/Gravitar/tractor_mono.wav")
THRUST = snd.load("/Gravitar/thrust_mono.wav")
from hersheyDVI2 import Hershey
hershey = Hershey(SCREEN, center_numbers=True)
hershey._desc_shift = 7
hershey.slow = False
import vtext8
init_vtext8()

init_imath()
init_palette()
init_game()
init_terrain()
init_missiles()
init_turrets()
init_fuel_cells()
init_ship()

gc.collect()
print('mem free:', gc.mem_free())

_thread.start_new_thread(core1, ())
sleep_ms(200)

try:
    core0()
    shutdown()
except KeyboardInterrupt:
    shutdown()