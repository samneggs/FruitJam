# Lunar Lander — 640x480 DVI/gamepad port for RP2350 HSTX
# Converted from lunarlander2.py to the same DVI/gamepad structure used by Star Castle.
from dvi_rp2_hstx_frame_sync_v2 import DVI_RP2_HSTX
import colors as rv_colors
from gamepadfast import Gamepad

import machine, framebuf, gc, _thread, array, micropython
from uctypes import addressof
from time import sleep_ms, ticks_ms, ticks_diff
from sys import exit
from micropython import const
from random import randint
from math import sin, cos, radians

# ── Display / fixed point ─────────────────────────────────────────────────────
MAXSCREEN_X = const(640)
MAXSCREEN_Y = const(480)
SCREEN_W    = const(640)
SCREEN_H    = const(480)

SCALE        = const(13)
WORLD_W      = const(1920)      # 3x DVI screen width, like the original 3x LCD world
WORLD_H      = const(1120)
TERRAIN_STEP = const(8)
NUM_TERRAIN  = const(241)       # WORLD_W // TERRAIN_STEP + 1

# ── Physics / game tuning ─────────────────────────────────────────────────────
GRAVITY      = const(35)
THRUST_SHIFT = const(6)
MAX_FUEL     = const(999)
LAND_VSPEED  = const(3500)
LAND_HSPEED  = const(2500)
LAND_ANGLE   = const(15)
NUM_PADS     = const(3)
SHIP_BOTTOM  = const(10)

# Camera zoom: 256 = 1x, 128 = 0.5x
ZOOM_OUT_Y   = const(360)
ZOOM_IN_Y    = const(720)
ZOOM_Y_RANGE = const(360)
ZOOM_MIN     = const(128)
ZOOM_MAX     = const(256)
ZOOM_DIFF    = const(128)

# Core timing
PAD_READ_MS   = const(20) #20
LOGIC_MOVE_MS = const(18)

# ── RGB332 palette, matching the DVI project convention ──────────────────────
BLACK      = const(0x00)
WHITE      = const(0b111_111_11)
GREEN      = const(0b000_111_00)
YELLOW     = const(0b111_111_00)
ORANGE     = const(0b111_100_00)
RED        = const(0b111_000_00)
BROWN      = const(0b101_011_00)
SKYBLUE    = const(0b100_100_11)
DIM_WHITE  = const(0b010_010_01)
BACKGROUND = const(BLACK)

# ── Gamepad, active low buttons ───────────────────────────────────────────────
GAMEPAD = array.array('i', [0, 0, 1, 0])    # x, y, restart_ready, thrust_on
GAMEPAD_X      = const(0)
GAMEPAD_Y      = const(1)
GAMEPAD_DELAY  = const(2)
GAMEPAD_THRUST = const(3)

GAMEPAD_SELECT = const(0b0000001)
GAMEPAD_DOWN   = const(0b0000010)
GAMEPAD_LEFT   = const(0b0000100)
GAMEPAD_RIGHT  = const(0b0100000)
GAMEPAD_UP     = const(0b1000000)

GAME_CTL      = array.array('i', [0])
GAME_CTL_EXIT = const(0)

# ── Game state arrays ─────────────────────────────────────────────────────────
GAME_EXIT    = const(0)
GAME_LIVES   = const(2)
GAME_FPS2    = const(4)
FUEL         = const(5)
SCORE        = const(6)
CAM_X        = const(7)
CAM_Y        = const(8)
STATE        = const(9)       # 0=fly, 1=landed, 2=unused/crash, 3=game over
TIMER        = const(10)
ALT          = const(11)
HSPEED_D     = const(12)
VSPEED_D     = const(13)
ZOOM         = const(14)
GAME_PARAMS  = const(16)

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

fb = bytearray(SCREEN_W * SCREEN_H)
SCREEN = framebuf.FrameBuffer(fb, SCREEN_W, SCREEN_H, framebuf.GS8)
gamepad = Gamepad()

char_map2=array.array('b',(
     0x3E, 0x63, 0x73, 0x7B, 0x6F, 0x67, 0x3E, 0x00,   # U+0030 (0)
     0x0C, 0x0E, 0x0C, 0x0C, 0x0C, 0x0C, 0x3F, 0x00,   # U+0031 (1)
     0x1E, 0x33, 0x30, 0x1C, 0x06, 0x33, 0x3F, 0x00,   # U+0032 (2)
     0x1E, 0x33, 0x30, 0x1C, 0x30, 0x33, 0x1E, 0x00,   # U+0033 (3)
     0x38, 0x3C, 0x36, 0x33, 0x7F, 0x30, 0x78, 0x00,   # U+0034 (4)
     0x3F, 0x03, 0x1F, 0x30, 0x30, 0x33, 0x1E, 0x00,   # U+0035 (5)
     0x1C, 0x06, 0x03, 0x1F, 0x33, 0x33, 0x1E, 0x00,   # U+0036 (6)
     0x3F, 0x33, 0x30, 0x18, 0x0C, 0x0C, 0x0C, 0x00,   # U+0037 (7)
     0x1E, 0x33, 0x33, 0x1E, 0x33, 0x33, 0x1E, 0x00,   # U+0038 (8)
     0x1E, 0x33, 0x33, 0x3E, 0x30, 0x18, 0x0E, 0x00))  # U+0039 (9)

char_map=array.array('b',(
    0x0 , 0x7e , 0x66 , 0x6e , 0x76 , 0x66 , 0x7e , 0x0 ,  #  0
    0x0 , 0x38 , 0x18 , 0x18 , 0x7e , 0x7e , 0x7e , 0x0 ,  #  1
    0x0 , 0x7e , 0x2 , 0x7e , 0x60 , 0x66 , 0x7e , 0x0 ,  #  2
    0x0 , 0x1e , 0x2 , 0x3e , 0x6 , 0x6 , 0x7e , 0x0 ,  #  3
    0x0 , 0x40 , 0x40 , 0x40 , 0x4c , 0x7e , 0xc , 0x0 ,  #  4
    0x0 , 0x7e , 0x60 , 0x7e , 0x6 , 0x66 , 0x7e , 0x0 ,  #  5
    0x0 , 0x7c , 0x40 , 0x7e , 0x62 , 0x62 , 0x7e , 0x0 ,  #  6
    0x0 , 0x7e , 0x6 , 0x6 , 0x1e , 0x18 , 0x18 , 0x0 ,  #  7
    0x0 , 0x3c , 0x24 , 0x24 , 0x7e , 0x66 , 0x7e , 0x0 ,  #  8
    0x0 , 0x7e , 0x42 , 0x42 , 0x7e , 0x6 , 0x6 , 0x0 ))  #  9

@micropython.viper
def show_num_viper(num:int,x_offset:int,y_offset:int,color:int):
    char_ptr = ptr8(char_map)
    screen_ptr = ptr8(SCREEN)
    size = 3 # 1,2,3
    char = 0
    offset = MAXSCREEN_X*y_offset+x_offset
    first = 1
    while num > 0 or first:
        first = 0
        total = num//10
        digit = num - (total * 10)
        num = total
        for y in range(8):
            row_data = char_ptr[digit*8+y]
            for x in range(8):
                if row_data & (1<<x) > 0:
                    addr = size*y*MAXSCREEN_X+(7-x)-(char*8)+offset
                    screen_ptr[addr] = color
                    if size>1:
                        screen_ptr[MAXSCREEN_X+addr] = color
                        if size>2:
                            screen_ptr[2*MAXSCREEN_X+addr] = color
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
        self.P[DEG] = 180
        self.P[VX] = 0
        self.P[VY] = 0
        self.P[DEAD] = 0
        self.P[TYPE] = 0
        self.P[SEGS] = 30
        self.P[MAP_X] = 0
        self.P[MAP_Y] = 30 << SCALE
        self.P[SLOW] = 0

        self.ship_deg = array.array('H',
            [16, 0, 306, 180, 54, 0, 333, 349, 333, 189, 333, 0, 27, 11, 27, 171, 27] + [0] * 13 +
            [27, 172, 188, 207, 219, 233, 236, 225, 270, 270, 256, 330, 270, 270, 351, 9, 90, 90, 30, 104, 90, 90, 135, 124, 127, 141, 153, 172] + [0] * 2 +
            [16, 90, 56, 67, 45, 45, 0, 323, 292, 248, 217, 180, 135, 135, 113, 124, 90] + [0] * 13 +
            [6, 180, 320, 323, 37, 40, 180] + [0] * 23 +
            [25, 180, 117, 124, 149, 162, 198, 211, 236, 243, 252, 315, 307, 304, 333, 326, 34, 27, 56, 45, 53, 108, 252, 304, 56, 108] + [0] * 5 +
            [0] * 30 +
            [12, 180, 220, 255, 290, 315, 340, 20, 45, 70, 105, 140, 180] + [0] * 17)

        self.ship_radius = array.array('H',
            [16, 0, 17, 8, 17, 0, 4, 10, 9, 12, 4, 0, 4, 10, 9, 12, 4] + [0] * 13 +
            [27, 14, 14, 13, 13, 10, 7, 3, 2, 8, 8, 16, 8, 2, 12, 12, 2, 8, 16, 8, 8, 2, 3, 7, 10, 13, 13, 13] + [0] * 2 +
            [16, 2, 7, 15, 8, 3, 8, 10, 11, 11, 10, 8, 3, 8, 15, 7, 2] + [0] * 13 +
            [6, 14, 16, 10, 10, 16, 14] + [0] * 23 +
            [25, 2, 4, 7, 12, 13, 13, 12, 7, 4, 6, 14, 10, 7, 4, 7, 7, 4, 7, 14, 10, 6, 6, 7, 7, 6] + [0] * 5 +
            [0] * 30 +
            [12, 12, 9, 7, 7, 14, 6, 6, 14, 7, 7, 9, 12] + [0] * 17)

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
    global TEXT_SCORE,TEXT_FUEL,TEXT_ALT,TEXT_HSPD,TEXT_VSPD,TEXT_LIVES,TEXT_1X,TEXT_2X,TEXT_4X
    vtext8.bind_screen(fb)
    font = vtext8.Font8("/fonts/COMPUTER.FNT", bind=True)

    TEXT_SCORE = font.make_text("SCORE")
    TEXT_FUEL = font.make_text("FUEL")
    TEXT_ALT = font.make_text("ALT")
    TEXT_HSPD = font.make_text("HSPD")
    TEXT_VSPD = font.make_text("VSPD")
    TEXT_LIVES = font.make_text("LIVES")
    TEXT_1X = font.make_text("1X")
    TEXT_2X = font.make_text("2X")
    TEXT_4X = font.make_text("4X")
    

def init_imath():
    global ISIN, ICOS
    scale = 1 << SCALE
    ISIN = array.array('i', (int(sin(radians(i)) * scale) for i in range(360)))
    ICOS = array.array('i', (int(cos(radians(i)) * scale) for i in range(360)))


def init_palette():
    global PALETTE
    # 32-step crash palette, indexed by S_EXP//10. Bright at the start, red near the end.
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


def init_terrain():
    global TERRAIN, PAD_MARKERS, PAD_START, PAD_END, PAD_MULT, STARS

    TERRAIN = array.array('i', [0] * NUM_TERRAIN)
    PAD_MARKERS = bytearray(NUM_TERRAIN)
    PAD_START = array.array('i', [0] * NUM_PADS)
    PAD_END = array.array('i', [0] * NUM_PADS)
    PAD_MULT = array.array('i', [1, 2, 4])

    height = 820
    for i in range(NUM_TERRAIN):
        TERRAIN[i] = height
        height += randint(-24, 24)
        if height < 610:
            height = 610
        if height > 1040:
            height = 1040

    pad_widths = [16, 9, 5]        # large / medium / small, in terrain steps
    pad_zones = [44, 122, 200]     # approximate thirds of the 1920-wide world

    for p in range(NUM_PADS):
        center = pad_zones[p] + randint(-14, 14)
        half_w = pad_widths[p] // 2
        start = center - half_w
        end = center + half_w

        if start < 1:
            start = 1
        if end >= NUM_TERRAIN - 1:
            end = NUM_TERRAIN - 2

        pad_y = TERRAIN[center]
        for i in range(start, end + 1):
            TERRAIN[i] = pad_y
            PAD_MARKERS[i] = p + 1

        PAD_START[p] = start
        PAD_END[p] = end

    TERRAIN[0] = TERRAIN[1]
    TERRAIN[NUM_TERRAIN - 1] = TERRAIN[NUM_TERRAIN - 2]

    STARS = array.array('i', [0] * (NUM_STARS * 2))
    for i in range(0, NUM_STARS * 2, 2):
        STARS[i] = randint(0, WORLD_W)
        STARS[i + 1] = randint(0, 580)


def init_lander():
    global ships
    ships = [Ship()]
    reset_lander()


def reset_lander():
    ship = ships[0]
    ship.P[X] = (WORLD_W >> 1) << SCALE
    ship.P[Y] = 80 << SCALE
    ship.P[VX] = 0
    ship.P[VY] = 0
    ship.P[DEG] = 90
    ship.P[TYPE] = 4
    ship.P[DEAD] = 0
    ship.P[S_EXP] = 0
    ship.P[SLOW] = 0
    ship.size = 2
    ship.calc_coords()

    GAME[STATE] = 0
    GAME[FUEL] = MAX_FUEL
    GAME[ZOOM] = ZOOM_MIN
    GAME[CAM_X] = 0
    GAME[CAM_Y] = 0
    update_zoom()
    update_camera()


def restart_game():
    GAME[GAME_LIVES] = 3
    GAME[SCORE] = 0
    GAME[STATE] = 0
    GAME[TIMER] = 0
    GAME[FUEL] = MAX_FUEL
    init_terrain()
    reset_lander()


def read_gamepad():
    gamepad.read()
    buttons = int(gamepad.buttons)

    if not (buttons & GAMEPAD_SELECT):
        GAME[GAME_EXIT] = 1
        return

    if not (buttons & GAMEPAD_DOWN):
        if GAMEPAD[GAMEPAD_DELAY]:
            GAMEPAD[GAMEPAD_DELAY] = 0
            restart_game()
        return
    else:
        GAMEPAD[GAMEPAD_DELAY] = 1

    ship = ships[0]
    GAMEPAD[GAMEPAD_THRUST] = 0

    if GAME[STATE] != 0 or ship.P[S_EXP] > 0 or ship.P[DEAD] == 1:
        return

    x_inc = int(gamepad.x) >> 7 # 6
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

    ship.calc_coords()


def move_lander():
    ship = ships[0]
    state = GAME[STATE]

    if state == 3:
        return

    if state == 1:
        GAME[TIMER] -= 1
        if GAME[TIMER] <= 0:
            reset_lander()
        return

    if ship.P[S_EXP] > 0:
        return

    if ship.P[DEAD] == 1:
        GAME[GAME_LIVES] -= 1
        if GAME[GAME_LIVES] <= 0:
            GAME[STATE] = 3
            return
        reset_lander()
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

    world_x = ship.P[X] >> SCALE
    world_y = ship.P[Y] >> SCALE
    vx = ship.P[VX]
    vy = ship.P[VY]
    if vx < 0:
        vx = -vx
    if vy < 0:
        vy = -vy

    GAME[HSPEED_D] = vx >> 8
    GAME[VSPEED_D] = vy >> 8

    tidx = world_x // TERRAIN_STEP
    if tidx < 0:
        tidx = 0
    if tidx >= NUM_TERRAIN - 1:
        tidx = NUM_TERRAIN - 2

    terrain_h = TERRAIN[tidx]
    GAME[ALT] = terrain_h - world_y
    if GAME[ALT] < 0:
        GAME[ALT] = 0

    ship_bottom = world_y + SHIP_BOTTOM
    if ship_bottom >= terrain_h:
        abs_vx = ship.P[VX]
        abs_vy = ship.P[VY]
        if abs_vx < 0:
            abs_vx = -abs_vx
        if abs_vy < 0:
            abs_vy = -abs_vy

        deg = ship.P[DEG]
        angle_off = deg - 90
        if angle_off < 0:
            angle_off = -angle_off
        if angle_off > 180:
            angle_off = 360 - angle_off

        on_pad = PAD_MARKERS[tidx]
        if on_pad > 0 and abs_vy < LAND_VSPEED and abs_vx < LAND_HSPEED and angle_off < LAND_ANGLE:
            ship.P[VX] = 0
            ship.P[VY] = 0
            ship.P[Y] = (terrain_h - SHIP_BOTTOM) << SCALE

            mult = PAD_MULT[on_pad - 1]
            GAME[SCORE] += 50 * mult
            GAME[SCORE] += GAME[FUEL] // 10
            GAME[STATE] = 1
            GAME[TIMER] = 120
            ship.calc_coords()
        else:
            ship.P[S_EXP] = 300

    ship.calc_coords()


def update_zoom():
    world_y = ships[0].P[Y] >> SCALE

    if world_y <= ZOOM_OUT_Y:
        zoom = ZOOM_MIN
    elif world_y >= ZOOM_IN_Y:
        zoom = ZOOM_MAX
    else:
        zoom = ZOOM_MIN + ((world_y - ZOOM_OUT_Y) * ZOOM_DIFF) // ZOOM_Y_RANGE

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
            screen[sy * MAXSCREEN_X + sx] = WHITE #DIM_WHITE
        i += 2


@micropython.viper
def draw_terrain():
    terrain = ptr32(TERRAIN)
    pads = ptr8(PAD_MARKERS)
    game = ptr32(GAME)

    cam_x = game[CAM_X]
    cam_y = game[CAM_Y]
    zoom = game[ZOOM]
    vis_w = (MAXSCREEN_X * 256) // zoom

    start_idx = cam_x // TERRAIN_STEP
    end_idx = (cam_x + vis_w) // TERRAIN_STEP + 1

    if start_idx < 0:
        start_idx = 0
    if end_idx >= NUM_TERRAIN - 1:
        end_idx = NUM_TERRAIN - 2

    i = start_idx
    while i < end_idx:
        x1 = ((i * TERRAIN_STEP - cam_x) * zoom) >> 8
        y1 = ((terrain[i] - cam_y) * zoom) >> 8
        x2 = ((((i + 1) * TERRAIN_STEP) - cam_x) * zoom) >> 8
        y2 = ((terrain[i + 1] - cam_y) * zoom) >> 8

        color = BROWN
        if int(pads[i]) != 0 and int(pads[i + 1]) != 0:
            color = YELLOW

        if y1 > -24 and y1 < MAXSCREEN_Y + 24 and y2 > -24 and y2 < MAXSCREEN_Y + 24:
            SCREEN.line(x1, y1, x2, y2, color)
            if color == YELLOW and x1 > 5 and x1 < MAXSCREEN_X - 5:
                post_h = (5 * zoom) >> 8
                if post_h < 2:
                    post_h = 2
                SCREEN.vline(x1, y1, post_h, YELLOW)
        i += 1


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


def draw_pad_labels():
    cam_x = GAME[CAM_X]
    cam_y = GAME[CAM_Y]
    zoom = GAME[ZOOM]

    for p in range(NUM_PADS):
        mid = (PAD_START[p] + PAD_END[p]) >> 1
        px = ((mid * TERRAIN_STEP - cam_x) * zoom) >> 8
        py = ((TERRAIN[mid] - cam_y) * zoom) >> 8
        py += (10 * zoom) >> 8

        if 18 < px < MAXSCREEN_X - 18 and 32 < py < MAXSCREEN_Y - 8:
            mult = PAD_MULT[p]
            if mult == 1:
                vtext8.text(TEXT_1X, px - 12, py + 8, YELLOW)
            elif mult == 2:
                vtext8.text(TEXT_2X, px - 12, py + 8, YELLOW)
            else:
                vtext8.text(TEXT_4X, px - 12, py + 8, YELLOW)
            

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

    vs_color = WHITE
    if game[VSPEED_D] > 10:
        vs_color = ORANGE
    if game[VSPEED_D] > 14:
        vs_color = RED
        
    #SCREEN.text('FUEL', 12, 4,  GREEN)
    vtext8.text(TEXT_FUEL, 12, 4,  GREEN)
    show_num_viper(fuel, 34, 24,  fuel_color)

    #SCREEN.text('ALT', 105, 4,  GREEN)
    vtext8.text(TEXT_ALT, 105, 4,  GREEN)
    show_num_viper(game[ALT], 120, 24,  GREEN)

    #SCREEN.text('HSPD', 195, 4,  GREEN)
    vtext8.text(TEXT_HSPD, 195, 4,  GREEN)

    show_num_viper(game[HSPEED_D], 215, 24,  WHITE)

    #SCREEN.text('VSPD', 300, 4,  GREEN)
    vtext8.text(TEXT_VSPD, 300, 4,  GREEN)
    show_num_viper(game[VSPEED_D], 320, 24,  vs_color)

    #SCREEN.text('SCORE', 415, 4,  WHITE)
    vtext8.text(TEXT_SCORE, 415, 4, WHITE)
    show_num_viper(game[SCORE], 435, 24,  YELLOW)

    #SCREEN.text('LIVES', 565, 4,  WHITE)
    vtext8.text(TEXT_LIVES, 565, 4,  WHITE)
    
    show_num_viper(game[GAME_LIVES], 590, 24,  YELLOW)
    

    if game[STATE] == 1:
        hershey.text('LANDED', 238, 145, 1050, GREEN)
        hershey.text('WELL DONE', 242, 205, 650, GREEN)
    elif game[STATE] == 3:
        hershey.text('GAME OVER', 220, 145, 950, YELLOW)
        hershey.text('DOWN TO RESTART', 230, 210, 520, SKYBLUE)

@micropython.viper
def draw():
    game = ptr32(GAME)
    fill_asm(fb, BACKGROUND, FLAG_ADDR)
    draw_hud()
    draw_stars()
    draw_terrain()
    draw_pad_labels()

    if game[STATE] != 3 and int(ships[0].P[DEAD]) != 1:
        draw_flame()
        ships[0].draw_coords()



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
            move_lander()
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


def main():
    global display,hershey, vtext8, FLAG_ADDR
    display = DVI_RP2_HSTX()
    display.begin(fb, rv_colors.COLOR_MODE_BGR233,
                  height=SCREEN_H, width=SCREEN_W, bytes_per_pixel=1)
    FLAG_ADDR = addressof(display._frame_flag)

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
    init_lander()

    gc.collect()
    print('mem free:', gc.mem_free())

    _thread.start_new_thread(core1, ())
    sleep_ms(200)

    try:
        core0()
        shutdown()
    except KeyboardInterrupt:
        shutdown()
        
if __name__ == '__main__':
    main()
