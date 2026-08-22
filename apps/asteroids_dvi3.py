# Asteroids RP2350 — DVI 640x480 BGR233 
# Display  : DVI_RP2_HSTX, BGR233 8-bit framebuffer, 640x480
# Input    : Gamepad (x=rotate, y<0=thrust, LEFT button=fire)
# Core 0   : game logic + input
# Core 1   : draw loop

from dvi_rp2_hstx_frame_sync_v2 import DVI_RP2_HSTX
from gamepadfast import Gamepad
import machine, math
from machine import Pin
from uctypes import addressof
from time import sleep, ticks_us, ticks_diff, ticks_ms, sleep_ms
import gc, array, framebuf, _thread
from sys import exit
from micropython import const
from random import randint
from math import sin, cos, radians
import colors as rv_colors

# ── Screen setup ────────────────────────────────────────────────────────────
SCREEN_W = const(640)
SCREEN_H = const(480)

machine.freq(246_000_000)
machine.mem32[0x40010058] = 2 << 16         # HSTX CLK / 2
machine.mem32[0x40010054] = 1 << 11         # HSTX CLK use SYS CLK

fb     = bytearray(SCREEN_W * SCREEN_H)
screen = framebuf.FrameBuffer(fb, SCREEN_W, SCREEN_H, framebuf.GS8)

# ── BGR233 8-bit color constants ────────────────────────────────────────────
BLACK  = const(0b000_000_00)
WHITE  = const(0b111_111_11)
RED    = const(0b111_000_00)
GREEN  = const(0b000_111_00)
BLUE   = const(0b000_000_11)
YELLOW = const(0b111_111_00)
CYAN   = const(0b000_111_11)

# ── Gamepad button masks ─────────────────────────────────────────────────────
GAMEPAD_RIGHT  = const(0b0100000)
GAMEPAD_LEFT   = const(0b0000100)  # fire
GAMEPAD_UP     = const(0b1000000)
GAMEPAD_DOWN   = const(0b0000010)
GAMEPAD_START  = const(0)
GAMEPAD_SELECT = const(0b0000001)

# ── Inter-core exit flag ─────────────────────────────────────────────────────
CTRL      = array.array('i', [0])
CTRL_EXIT = const(0)

# ── Palette size ─────────────────────────────────────────────────────────────
# BGR233: R=3-bit, G=3-bit, B=2-bit.  B is the bottleneck for true grays (4
# levels), but 8 steps gives smooth-enough ramps while matching the 3-bit R/G
# channel depth — 2 palette entries per B step, acceptable banding.
PALETTE_STEPS = const(8)


# ── Fast screen fill (48 bytes × 8 stmia = 192 bytes per loop) ───────────────
@micropython.asm_thumb
def fill_asm(r0, r1):  # (buffer_addr, 8-bit_color)
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


# ── Game state ───────────────────────────────────────────────────────────────
class Game:
    _MAXSCREEN_X = const(640)
    _MAXSCREEN_Y = const(480)
    _SCALE       = const(13)
    _GAME_LIVES  = const(2)
    _GAME_FPS1   = const(3)
    _GAME_FPS2   = const(4)
    _AST_REMAIN  = const(5)
    _SCORE       = const(6)
    _GAME_PARAMS = const(10)

    def __init__(self):
        # 3600-entry LUT: 0.1° steps for smooth rotation
        self.isin = array.array('i', int(sin(radians(i / 10)) * (1 << _SCALE)) for i in range(3600))
        self.icos = array.array('i', int(cos(radians(i / 10)) * (1 << _SCALE)) for i in range(3600))
        self.P    = array.array('i', 0 for _ in range(_GAME_PARAMS))
        self.fps_array = bytearray(35)
        self.P[_GAME_LIVES] = 3
        self.string1 = '           '
        self.string2 = '            '
        self.score   = 0

    @staticmethod
    @micropython.asm_thumb
    def num_to_str2(r0, r1):
        mov(r2, r1)
        mov(r5, r1)
        mov(r3, r0)
        mov(r4, 0)
        label(COUNT_DIGITS)
        mov(r1, 10)
        mov(r0, r2)
        bl(DIVIDE)
        mov(r2, r0)
        add(r4, r4, 1)
        cmp(r0, 0)
        bne(COUNT_DIGITS)
        add(r3, r3, r4)
        mov(r2, r5)
        label(CONVERT_DIGITS)
        mov(r1, 10)
        mov(r0, r2)
        bl(DIVIDE)
        mov(r2, r0)
        add(r1, 0x30)
        sub(r3, r3, 1)
        strb(r1, [r3, 0])
        cmp(r0, 0)
        bne(CONVERT_DIGITS)
        b(EXIT)
        label(DIVIDE)
        sdiv(r6, r0, r1)
        mul(r1, r6)
        sub(r0, r0, r1)
        mov(r1, r0)
        mov(r0, r6)
        bx(lr)
        label(EXIT)

    @staticmethod
    @micropython.asm_thumb
    def int_to_ascii(r0, r1):
        mov(r2, 0x20)
        strb(r2, [r0, 1])
        strb(r2, [r0, 2])
        cmp(r1, 0)
        bne(NOT_ZERO)
        mov(r2, 0x30)
        strb(r2, [r0, 0])
        b(EXIT)
        label(NOT_ZERO)
        cmp(r1, 0)
        bgt(NOT_NEGATIVE)
        neg(r1, r1)
        mov(r2, 0x2d)
        strb(r2, [r0, 0])
        add(r0, 1)
        label(NOT_NEGATIVE)
        mov(r2, 10)
        push({r0, r1})
        mov(r3, 0)
        label(COUNT)
        add(r3, r3, 1)
        udiv(r4, r1, r2)
        mov(r1, r4)
        cmp(r1, 0)
        bne(COUNT)
        pop({r0, r1})
        add(r0, r0, r3)
        sub(r0, r0, 1)
        label(CONVERT)
        udiv(r4, r1, r2)
        mov(r5, r4)
        mul(r5, r2)
        sub(r5, r1, r5)
        add(r5, 0x30)
        strb(r5, [r0, 0])
        mov(r1, r4)
        sub(r0, r0, 1)
        cmp(r1, 0)
        bne(CONVERT)
        label(EXIT)

    @staticmethod
    @micropython.asm_thumb
    def avg_fps_asm(r0, r1):
        ldrb(r2, [r0, 0])
        add(r2, r2, 1)
        cmp(r2, 33)
        blt(LT_32)
        mov(r2, 1)
        label(LT_32)
        strb(r2, [r0, 0])
        add(r2, r2, r0)
        strb(r1, [r2, 0])
        mov(r2, 1)
        mov(r3, 0)
        label(LOOP)
        add(r0, r0, 1)
        ldrb(r4, [r0, 0])
        add(r3, r3, r4)
        add(r2, r2, 1)
        cmp(r2, 33)
        blt(LOOP)
        asr(r0, r3, 5)


# ── Ship / Asteroid ──────────────────────────────────────────────────────────
class Ship:
    _X      = const(0)
    _Y      = const(1)
    _DEG    = const(2)
    _VX     = const(3)
    _VY     = const(4)
    _AX     = const(5)
    _AY     = const(6)
    _DEAD   = const(7)
    _MAP_X  = const(8)
    _MAP_Y  = const(9)
    _SHIELD = const(10)
    _OLD_X  = const(11)
    _OLD_Y  = const(12)
    _MISL   = const(13)
    _S_EXP  = const(14)
    _SEGS   = const(15)
    _TYPE   = const(16)
    _BUTTON = const(17)
    _SLOW   = const(18)
    _SHIP_PARAMS = const(20)

    def __init__(self):
        self.size     = 2
        self.points   = const(16)
        self.segments = const(30)
        self.P      = array.array('i', 0 for _ in range(_SHIP_PARAMS))
        self.coords = array.array('i', 0 for _ in range((self.segments + 2) * 4))
        self.P[_X]     = 80  << _SCALE
        self.P[_Y]     = 64  << _SCALE
        self.P[_DEG]   = 1800                          # was 180; now 0.1° units
        self.P[_VX]    = 0
        self.P[_VY]    = 0
        self.P[_DEAD]  = 0
        self.P[_TYPE]  = 0
        self.P[_SEGS]  = 30
        self.P[_MAP_X] = 0  << _SCALE
        self.P[_MAP_Y] = 30 << _SCALE
        self.P[_SLOW]  = 1
        self.ship_deg = array.array('H',
            [16, 0, 306, 180, 54, 0, 333, 349, 333, 189, 333, 0, 27, 11, 27, 171, 27] + [0]*13 +
            [27, 14, 14, 13, 13, 10, 7, 3, 2, 8, 8, 16, 8, 2, 12, 12,
              2, 8, 16, 8, 8, 2, 3, 7, 10, 13, 13, 13] + [0]*2 +
            [16, 90, 56, 67, 45, 45, 0, 323, 292, 248, 217, 180, 135, 135, 113, 124, 90] + [0]*13 +
            [6, 180, 320, 323, 37, 40, 180] + [0]*23 +
            [0]*30)
        self.ship_radius = array.array('H',
            [16, 0, 17, 8, 17, 0, 4, 10, 9, 12, 4, 0, 4, 10, 9, 12, 4] + [0]*13 +
            [27, 14, 14, 13, 13, 10, 7, 3, 2, 8, 8, 16, 8, 2, 12, 12,
              2, 8, 16, 8, 8, 2, 3, 7, 10, 13, 13, 13] + [0]*2 +
            [16, 2, 7, 15, 8, 3, 8, 10, 11, 11, 10, 8, 3, 8, 15, 7, 2] + [0]*13 +
            [6, 14, 16, 10, 10, 16, 14] + [0]*23 +
            [0]*30)

    @micropython.viper
    def calc_coords(self):
        segs   = int(self.segments)
        size   = int(self.size)
        isin   = ptr32(GAME.isin)
        icos   = ptr32(GAME.icos)
        deg    = ptr16(self.ship_deg)
        radius = ptr16(self.ship_radius)
        coords = ptr32(self.coords)
        index_coords = 0
        s_deg  = int(self.P[_DEG])
        s_type = int(self.P[_TYPE])
        self.P[_SEGS] = radius[s_type * 30]
        for i in range(1 + (segs * s_type), (segs * s_type) + segs):
            pt_deg = deg[i] * 10 + s_deg          # ship_deg stores 0-359; scale to 0-3599
            if pt_deg >= 3600: pt_deg -= 3600
            if pt_deg < 0:     pt_deg += 3600
            coords[index_coords]   = (radius[i] * size * icos[pt_deg]) >> 14
            coords[index_coords+1] = (radius[i] * size * isin[pt_deg]) >> 14
            if index_coords > 0:
                coords[index_coords+2] = coords[index_coords]
                coords[index_coords+3] = coords[index_coords+1]
            index_coords += 2
        index_coords += 2

    @micropython.viper
    def draw_coords(self,color:int):
        isin    = ptr32(GAME.isin)
        icos    = ptr32(GAME.icos)
        deg     = ptr16(self.ship_deg)
        coords  = ptr32(self.coords)
        palette = ptr8(PALETTE)                     # BGR233: ptr8 (was ptr16 for RGB565)
        segs    = int(self.P[_SEGS]) * 4 - 4
        x       = int(self.P[_X]) >> _SCALE
        y       = int(self.P[_Y]) >> _SCALE
        exp     = int(self.P[_S_EXP])
        s_deg   = int(self.P[_DEG])
        if int(self.P[_DEAD]) == 1: return
        # Map exp (1..300) → palette index (0..PALETTE_STEPS-1)
        if exp > 0: color = int(palette[(exp - 1) * int(PALETTE_STEPS) // 300])
        for i in range(0, segs, 4):
            if exp > 0:
                if exp == 1:
                    self.P[_DEAD]  = 1
                    self.P[_S_EXP] = 0
                    return
                self.P[_S_EXP] = exp - 1
                if not (exp % 1):
                    e_seg  = i // 2
                    pt_deg = deg[e_seg] * 10 + s_deg + 1800   # +1800 = +180° in 0.1° units
                    pt_deg %= 3600
                    coords[i+0] += icos[pt_deg] >> 12
                    coords[i+1] += isin[pt_deg] >> 12
                    coords[i+2] += icos[pt_deg] >> 12
                    coords[i+3] += isin[pt_deg] >> 12
            x1 = coords[i+0] + x
            y1 = coords[i+1] + y
            x2 = coords[i+2] + x
            y2 = coords[i+3] + y
            if -20 < x1 < _MAXSCREEN_X+20 and -20 < x2 < _MAXSCREEN_X+20 \
               and -20 < y1 < _MAXSCREEN_Y+20 and -20 < y2 < _MAXSCREEN_Y+20:
                screen.line(x1, y1, x2, y2, color)


# ── Missile / circular collision bodies ──────────────────────────────────────
class Missile:
    _MISS_LIFE    = const(7)
    _MISS_START   = const(8)
    _MISS_PARAMS  = const(10)
    _NUM_MISSILES = const(20)
    _MISSILE_PARAMS = const(20)
    _NUM_HITBOXES   = const(31)
    _HITBOX_PARAMS  = const(10)
    _HITBOX_X  = const(0)
    _HITBOX_Y  = const(1)
    _HITBOX_R  = const(2)   # circle radius in screen pixels
    _HITBOX_ON = const(3)
    _MISSILE_R = const(2)   # missile collision radius in screen pixels
    _SHIP_R    = const(12)  # ship collision radius in screen pixels

    def __init__(self):
        self.P        = array.array('i', 0 for _ in range(_MISSILE_PARAMS * _NUM_MISSILES))
        self.HITBOXES = array.array('i', 0 for _ in range(_HITBOX_PARAMS * _NUM_HITBOXES))
        for hb in range(_NUM_HITBOXES):
            i = hb * _HITBOX_PARAMS
            self.HITBOXES[i + _HITBOX_R]  = 8
            self.HITBOXES[i + _HITBOX_ON] = 0

    @micropython.viper
    def init_missile(self):
        miss = ptr32(self.P)
        isin = ptr32(GAME.isin)
        icos = ptr32(GAME.icos)
        x   = int(self.P[_X])
        y   = int(self.P[_Y])
        vx  = int(self.P[_VX])
        vy  = int(self.P[_VY])
        deg = int(self.P[_DEG]) + 1800             # +1800 = +180° in 0.1° units
        if deg >= 3600: deg -= 3600
        for index in range(1, _NUM_MISSILES):
            i = index * _MISSILE_PARAMS
            if miss[i + _MISS_LIFE] == 0:
                miss[i + _MISS_LIFE] = 200
                miss[i + _X] = x
                miss[i + _Y] = y
                abs_vx = (vx + icos[deg]) >> 1
                abs_vy = (vy + isin[deg]) >> 1
                abs_vx = abs_vx if abs_vx > 0 else abs_vx * -1
                abs_vy = abs_vy if abs_vy > 0 else abs_vy * -1
                if abs_vx < 5000 and abs_vy < 5000 or 1:
                    miss[i + _VX] = (icos[deg]) << 3
                    miss[i + _VY] = (isin[deg]) << 3
                else:
                    miss[i + _VX] = (vx + icos[deg]) << 1
                    miss[i + _VY] = (vy + isin[deg]) << 1
                return

    @micropython.viper
    def move_missile(self) -> int:
        miss   = ptr32(self.P)
        hitbox = ptr32(self.HITBOXES)
        hit_index = 0
        for index in range(_NUM_MISSILES):
            i = index * _MISSILE_PARAMS
            if miss[i + _MISS_LIFE] > 0:
                miss[i + _MISS_LIFE] -= 1
                x = miss[i + _X] + miss[i + _VX]
                y = miss[i + _Y] + miss[i + _VY]
                miss[i + _X] = x
                miss[i + _Y] = y
                x = x >> _SCALE
                y = y >> _SCALE
                if not (0 < x < _MAXSCREEN_X and 0 < y < _MAXSCREEN_Y):
                    miss[i + _MISS_LIFE] = 0    # kill off-screen missile
                for hb in range(1, _NUM_HITBOXES):
                    hb_idx = hb * _HITBOX_PARAMS
                    if hitbox[hb_idx + _HITBOX_ON]:
                        hb_r = hitbox[hb_idx + _HITBOX_R] + _MISSILE_R
                        dx = x - hitbox[hb_idx + _HITBOX_X]
                        dy = y - hitbox[hb_idx + _HITBOX_Y]
                        if dx * dx + dy * dy <= hb_r * hb_r:
                            hit_index = hb + 1
                            hitbox[hb_idx + _HITBOX_ON] = 0
                            miss[i + _MISS_LIFE] = 0
                            return hit_index
        return 0


# ── 7-segment LED number display ─────────────────────────────────────────────
class Led_number:
    def __init__(self, screen_width):
        self.screen_width = screen_width
        self.segment_patterns = bytearray((
            0b01111110,  # 0
            0b00110000,  # 1
            0b01101101,  # 2
            0b01111001,  # 3
            0b00110011,  # 4
            0b01011011,  # 5
            0b01011111,  # 6
            0b01110000,  # 7
            0b01111111,  # 8
            0b01111011)) # 9

    @micropython.viper
    def draw_digit(self, framebuffer, digit: int, x: int, y: int, size: int, color: int, width: int):
        patterns  = ptr8(self.segment_patterns)
        pattern   = int(patterns[digit])
        thick     = size >> 3
        if thick < 1: thick = 1
        seg_len   = size >> 1
        half_size = size >> 1
        if pattern & 0b01000000:
            self.draw_horizontal(framebuffer, x + thick, y, seg_len - thick, thick, color, width)
        if pattern & 0b00100000:
            self.draw_vertical(framebuffer, x + seg_len, y + thick, half_size - thick, thick, color, width)
        if pattern & 0b00010000:
            self.draw_vertical(framebuffer, x + seg_len, y + half_size + thick, half_size - thick, thick, color, width)
        if pattern & 0b00001000:
            self.draw_horizontal(framebuffer, x + thick, y + size, seg_len - thick, thick, color, width)
        if pattern & 0b00000100:
            self.draw_vertical(framebuffer, x, y + half_size + thick, half_size - thick, thick, color, width)
        if pattern & 0b00000010:
            self.draw_vertical(framebuffer, x, y + thick, half_size - thick, thick, color, width)
        if pattern & 0b00000001:
            self.draw_horizontal(framebuffer, x + thick, y + half_size, seg_len - thick, thick, color, width)

    @micropython.viper
    def draw_horizontal(self, fb_ptr: ptr8, x: int, y: int, length: int, thick: int, color: int, width: int):
        # ptr8: 1 byte per pixel (BGR233)
        start_offset = y * width + x
        i = 0
        while i < thick:
            offset = start_offset + i * width
            j = 0
            while j < length:
                fb_ptr[offset + j] = color
                j += 1
            i += 1

    @micropython.viper
    def draw_vertical(self, fb_ptr: ptr8, x: int, y: int, length: int, thick: int, color: int, width: int):
        # ptr8: 1 byte per pixel (BGR233)
        start_offset = y * width + x
        i = 0
        while i < length:
            offset = start_offset + i * width
            j = 0
            while j < thick:
                fb_ptr[offset + j] = color
                j += 1
            i += 1

    @micropython.viper
    def draw_number(self, framebuffer, num: int, x: int, y: int, size: int, color: int, width: int):
        digit_width = size - (size >> 2)
        temp_num    = num
        digit_count = 0
        if temp_num == 0:
            digit_count = 1
        else:
            while temp_num > 0:
                temp_num    //= 10
                digit_count += 1
        current_x = x + (digit_count - 1) * digit_width
        temp_num  = num
        first     = 1
        while temp_num > 0 or first:
            first   = 0
            digit   = temp_num % 10
            self.draw_digit(framebuffer, digit, current_x, y, size, color, width)
            temp_num  //= 10
            current_x -= digit_width

    def draw(self, framebuffer, number, x, y, size, color=0xFF):
        self.draw_number(framebuffer, number, x, y, size, color, int(self.screen_width))


# ── Explosion palette ─────────────────────────────────────────────────────────
# BGR233 packing: R[7:5] G[4:2] B[1:0]
# B is the bottleneck for true neutral grays (only 4 levels), but 8 steps
# gives smooth-enough ramps while matching the 3-bit R/G channel depth.
# steps=8 → 2 palette entries per B step, acceptable banding.
#
# Works between any two BGR233 colors — pass BLACK→WHITE for the default
# explosion fade, or any pair for colored effects.
def fade(color1_val, color2_val, palette, steps=PALETTE_STEPS):
    r1 = (color1_val >> 5) & 0x07;  r2 = (color2_val >> 5) & 0x07
    g1 = (color1_val >> 2) & 0x07;  g2 = (color2_val >> 2) & 0x07
    b1 =  color1_val       & 0x03;  b2 =  color2_val       & 0x03
    n  = steps - 1
    for i in range(steps):
        r = r1 + (r2 - r1) * i // n
        g = g1 + (g2 - g1) * i // n
        b = b1 + (b2 - b1) * i // n
        palette.append(((r & 0x07) << 5) | ((g & 0x07) << 2) | (b & 0x03))


# ── Input: Gamepad replaces ADC pots + bare Pins ─────────────────────────────
@micropython.viper
def read_gamepad():
    miss   = ptr32(missile.P)
    ship   = ptr32(ships[0].P)
    isin   = ptr32(GAME.isin)
    icos   = ptr32(GAME.icos)
    game   = ptr32(GAME.P)

    # Respawn ship after explosion
    if ship[_DEAD] == 1:
        ship[_DEAD] = 0
        ship[_VX]   = 0
        ship[_VY]   = 0
        ship[_X]    = _MAXSCREEN_X << (_SCALE - 1)   # centre X = 320
        ship[_Y]    = _MAXSCREEN_Y << (_SCALE - 1)   # centre Y = 240
        game[_GAME_LIVES] = game[_GAME_LIVES] - 1
    gamepad.read()
    buttons = int(gamepad.buttons)
    if not (buttons & GAMEPAD_SELECT) : # select pushed
        shutdown()
    if ship[_S_EXP] > 0: return
    x_inc = int(gamepad.x)
    y_inc = int(gamepad.y)
    if -2 < x_inc < 2: x_inc = 0

    # Fire: LEFT button
    if not (buttons & GAMEPAD_RIGHT) and miss[_MISS_START] == 1:
        miss[_MISS_START] = 0
        miss[_X]   = ship[_X]
        miss[_Y]   = ship[_Y]
        miss[_VX]  = ship[_VX]
        miss[_VY]  = ship[_VY]
        miss[_DEG] = ship[_DEG]
        missile.init_missile()

    # Friction / slow stop
    if -400 < ship[_VX] < 400: ship[_VX] = 0
    if -400 < ship[_VY] < 400: ship[_VY] = 0
    if ship[_SLOW]:
        ship[_VX] -= ship[_VX] >> 5
        ship[_VY] -= ship[_VY] >> 5

    # Rotate
    deg = int(ship[_DEG]) + (x_inc >> 2)
    if not (0 <= deg < 3600): deg %= 3600          # was 360
    ship[_DEG] = deg

    # Move and wrap
    ship[_X] += ship[_VX]
    ship[_Y] += ship[_VY]
    if ship[_X] < 0:                       ship[_X] = _MAXSCREEN_X << _SCALE
    if ship[_Y] < 0:                       ship[_Y] = _MAXSCREEN_Y << _SCALE
    if ship[_X] > _MAXSCREEN_X << _SCALE: ship[_X] = 0
    if ship[_Y] > _MAXSCREEN_Y << _SCALE: ship[_Y] = 0

    # Thrust: y axis negative = forward
    if y_inc < -50:
        ship[_VX] -= (icos[deg] >> 1)
        ship[_VY] -= (isin[deg] >> 1)

    ships[0].calc_coords()


# ── Asteroid movement + collision ────────────────────────────────────────────
@micropython.viper
def move_asteroids():
    hitbox = ptr32(missile.HITBOXES)
    game   = ptr32(GAME.P)
    ship   = ptr32(ships[0].P)
    total  = 0
    ship_x = ship[_X] >> _SCALE
    ship_y = ship[_Y] >> _SCALE
    for i in range(1, 31):
        ihbx = i * _HITBOX_PARAMS
        if int(ships[i].P[_S_EXP]) > 0 or int(ships[i].P[_DEAD]) == 1: continue
        total += 1
        ships[i].P[_X] += ships[i].P[_VX]
        ships[i].P[_Y] += ships[i].P[_VY]
        if int(ships[i].P[_X]) < 0: ships[i].P[_X] = _MAXSCREEN_X << _SCALE
        if int(ships[i].P[_Y]) < 0: ships[i].P[_Y] = _MAXSCREEN_Y << _SCALE
        if int(ships[i].P[_X]) > _MAXSCREEN_X << _SCALE: ships[i].P[_X] = 0
        if int(ships[i].P[_Y]) > _MAXSCREEN_Y << _SCALE: ships[i].P[_Y] = 0
        rotate = int(ships[i].P[_DEG])
        if ships[i].P[_VX] > ships[i].P[_VY]:
            rotate += 20                               # was 2; 20 × 0.1° = same 2° per tick
            if rotate > 3599: rotate -= 3600           # was 359/360
        else:
            rotate -= 20                               # was 2
            if rotate < 0: rotate += 3600              # was 360
        ships[i].P[_DEG] = rotate
        ships[i].calc_coords()
        hitbox[ihbx + _HITBOX_X] = int(ships[i].P[_X]) >> _SCALE
        hitbox[ihbx + _HITBOX_Y] = int(ships[i].P[_Y]) >> _SCALE
        hb_r = hitbox[ihbx + _HITBOX_R] + _SHIP_R
        dx = ship_x - hitbox[ihbx + _HITBOX_X]
        dy = ship_y - hitbox[ihbx + _HITBOX_Y]
        if dx * dx + dy * dy <= hb_r * hb_r and ship[_S_EXP] == 0:
            hit_asteroid(i + 1)
            ship[_S_EXP] = 300
    game[_AST_REMAIN] = total
    if total == 0: init_level()


def hit_asteroid(hit):
    x    = int(ships[hit-1].P[_X])
    y    = int(ships[hit-1].P[_Y])
    ihbx = (hit - 1) * _HITBOX_PARAMS
    hb_r = int(missile.HITBOXES[ihbx + _HITBOX_R])
    if hb_r >= 20:      # large → split into two medium circles, radius=10
        init_asteroid(30, 10, 3, x, y)
        init_asteroid(30, 10, 3, x + 50_000, y + 50_000)
        GAME.P[_SCORE] += 20
    elif hb_r >= 10:    # medium → split into two small circles, radius=5
        init_asteroid(10, 5, 1, x, y)
        init_asteroid(10, 5, 1, x + 50_000, y + 50_000)
        GAME.P[_SCORE] += 50
    else:               # small → destroyed
        GAME.P[_SCORE] += 100
    ships[hit-1].P[_S_EXP] = 300


def init_ships():
    global ships, missile
    missile = Missile()
    ships   = []
    new_ship = Ship()
    ships.append(new_ship)
    ships[0].P[_X]    = 320 << _SCALE
    ships[0].P[_Y]    = 240 << _SCALE
    ships[0].P[_TYPE] = 3
    for i in range(1, 31):
        new_ship = Ship()
        ships.append(new_ship)
        ships[i].P[_DEAD] = 1
    init_level()


def init_level():
    for i in range(4):
        left, top, right, bottom = 0, 0, 639, 479
        width     = right  - left
        height    = bottom - top
        perimeter = 2 * (width + height)
        pos = randint(0, perimeter - 1)
        if pos < width:
            x = left + pos
            y = top
        elif pos < width + height:
            x = right
            y = top + (pos - width)
        elif pos < 2 * width + height:
            x = right - (pos - width - height)
            y = bottom
        else:
            x = left
            y = bottom - (pos - 2 * width - height)
        init_asteroid(30, 20, 5, x << _SCALE, y << _SCALE)


NVERTS = const(14)

def init_asteroid(segs, radius, rnd, x, y):
    for i in range(1, 31):
        if ships[i].P[_DEAD] == 1:
            ships[i].P[_DEAD] = 0
            ships[i].P[_X]    = x
            ships[i].P[_Y]    = y
            ships[i].P[_VX]   = int(randint(1, 1) * (randint(0, 1) * 2 - 1)) << _SCALE
            ships[i].P[_VY]   = int(randint(1, 1) * (randint(0, 1) * 2 - 1)) << _SCALE
            ships[i].P[_TYPE] = 4
            deg_inc = 360 // NVERTS

            vdeg = [0] * NVERTS
            vrad = [0] * NVERTS
            deg  = 0
            for j in range(NVERTS):
                vdeg[j] = deg
                vrad[j] = radius + randint(-rnd, rnd)
                deg += deg_inc

            draw_count = NVERTS + 1
            base = 30 * 4
            ships[i].ship_deg[base]    = draw_count
            ships[i].ship_radius[base] = draw_count

            slot = 1
            ships[i].ship_deg[base + slot]    = vdeg[0]
            ships[i].ship_radius[base + slot] = vrad[0]
            slot += 1

            for j in range(NVERTS):
                nxt = (j + 1) % NVERTS
                ships[i].ship_deg[base + slot]    = vdeg[nxt]
                ships[i].ship_radius[base + slot] = vrad[nxt]
                slot += 1
                if j < NVERTS - 1:
                    ships[i].ship_deg[base + slot]    = vdeg[nxt]
                    ships[i].ship_radius[base + slot] = vrad[nxt]
                    slot += 1

            ihbx = i * _HITBOX_PARAMS
            missile.HITBOXES[ihbx + _HITBOX_R]  = radius
            missile.HITBOXES[ihbx + _HITBOX_ON] = 1
            ships[i].calc_coords()
            return


# ── Draw missile pixels (core1 only, BGR233 ptr8) ────────────────────────────
@micropython.viper
def draw_missiles():
    miss = ptr32(missile.P)
    buf  = ptr8(fb)
    for index in range(_NUM_MISSILES):
        i = index * _MISSILE_PARAMS
        if miss[i + _MISS_LIFE] > 0:
            x = miss[i + _X] >> _SCALE
            y = miss[i + _Y] >> _SCALE
            if 0 < x < _MAXSCREEN_X and 0 < y < _MAXSCREEN_Y:
                buf[y * SCREEN_W + x] = int(WHITE)


# ── Draw (core1) ─────────────────────────────────────────────────────────────
@micropython.viper
def draw():
    display.wait_frame()
    fill_asm(fb, BLACK)
    game = ptr32(GAME.P)
    screen.text('LIVES:', 540, 0, GREEN)
    led_num.draw(fb, game[_GAME_LIVES], 540, 10, 20, GREEN)
    screen.text('SCORE:', 50, 0, YELLOW)
    led_num.draw(fb, game[_SCORE], 45, 10, 20, YELLOW)
    for i in range(1, 31):
        ships[i].draw_coords(WHITE)
    draw_missiles()
    if game[_GAME_LIVES] == 0:
        screen.text('GAME OVER', 265, 225, RED)
    else:
        ships[0].draw_coords(GREEN)


# ── Core 0: game logic + input ───────────────────────────────────────────────
@micropython.viper
def core0():
    gc.collect()
    miss = ptr32(missile.P)
    ctrl = ptr32(CTRL)
    pot_ticks       = 0
    miss_ticks      = 0
    asteroids_ticks = 0
    missile_ticks   = 0
    while not ctrl[CTRL_EXIT]:
        gticks = int(ticks_ms())
        sleep_ms(1)
        if gticks - pot_ticks > 30:
            pot_ticks = gticks
            read_gamepad()
        if gticks - miss_ticks > 200:
            miss[_MISS_START] = 1
            miss_ticks = gticks
        if gticks - asteroids_ticks > 20:
            asteroids_ticks = gticks
            move_asteroids()
        if gticks - missile_ticks > 20:
            missile_ticks = gticks       
            hit = int(missile.move_missile())
            if hit > 0:
                hit_asteroid(hit)
    ctrl[CTRL_EXIT] = 1
    print('core0 done')


# ── Core 1: draw loop ────────────────────────────────────────────────────────
@micropython.viper
def core1():
    sleep_ms(500)
    ctrl = ptr32(CTRL)
    while not ctrl[CTRL_EXIT]:
        draw()
    print('core1 done')


# ── Shutdown ─────────────────────────────────────────────────────────────────
def shutdown():
    CTRL[CTRL_EXIT] = 1
    sleep_ms(200)
    display.deinit()
    machine.freq(150_000_000)
    print('shutdown...')
    sleep_ms(300)
    exit()


# ── Entry point ──────────────────────────────────────────────────────────────
def main():
    global gamepad,PALETTE,GAME,led_num,display
    gamepad = Gamepad()

    # BGR233 8-bit palette — 8 steps BLACK→WHITE for explosion fade
    # To use a colored explosion, e.g. BLACK→RED:
    #   PALETTE = array.array('B', ())
    #   fade(int(BLACK), int(RED), PALETTE)
    PALETTE = array.array('B', ())
    fade(int(BLACK), int(WHITE), PALETTE)

    display = DVI_RP2_HSTX()
    display.begin(fb, rv_colors.COLOR_MODE_BGR233, height=SCREEN_H, width=SCREEN_W, bytes_per_pixel=1)

    led_num = Led_number(_MAXSCREEN_X)
    screen.fill(int(BLACK))
    GAME = Game()
    sleep_ms(200)
    init_ships()
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
