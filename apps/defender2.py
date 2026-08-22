# defender320.py - Defender for RP2350 / Fruit Jam / 320x240 DVI, dual-core
# Ported from defender240.py (ST7796 240x160, ADC pots, PIOPWM sound).
# Sprites: /defender8.bin, 8x8 standard RGB565 (NOT byte swapped).
# Controls: analog X/Y = ship, RIGHT = fire, LEFT = smart bomb,
#           DOWN = restart after game over, SELECT = quit
from dvi_rp2_hstx_frame_sync_v2 import DVI_RP2_HSTX
from gamepadfast import Gamepad
from draw_numberdvi import Draw_number
import colors as rv_colors
from random import randint
import machine, _thread, gc, framebuf, array
from time import sleep_ms, ticks_ms, ticks_diff
from math import sin, cos, radians
from sys import exit


MAXSCREEN_X = const(320)
MAXSCREEN_Y = const(240)
SCALE  = const(13)
CENTER = const(28672)                      # 3.5 * (1 << SCALE)
EXTRA  = const(5000)
SOUND_ON = const(1)               

machine.freq(246_000_000)
machine.mem32[0x40010058] = 2 << 16        # HSTX CLK / 2
machine.mem32[0x40010054] = 1 << 11        # HSTX CLK use SYS CLK
machine.mem32[0x40010048] = 1 << 11        # enable peri_ctrl clock

fb = bytearray(MAXSCREEN_X * MAXSCREEN_Y * 2)     # scanout
fb2 = bytearray(MAXSCREEN_X * MAXSCREEN_Y * 2)    # draw

# ---- shared core status -----------------------------------------------------
GAME_EXIT = const(0)
G_RDY     = const(1)
GAME = array.array('i', [0, 0])

FPS_CORE0 = const(0)                       # Draw_number slots
FPS_CORE1 = const(1)

# ---- input ------------------------------------------------------------------
GAMEPAD_RIGHT  = const(0b0100000)
GAMEPAD_LEFT   = const(0b0000100)
GAMEPAD_UP     = const(0b1000000)
GAMEPAD_DOWN   = const(0b0000010)
GAMEPAD_SELECT = const(0b0000001)
BTN_FIRE       = const(GAMEPAD_RIGHT)
BTN_BOMB       = const(GAMEPAD_LEFT)
BTN_RESTART    = const(GAMEPAD_DOWN)
DEADZONE       = const(60)                 # raw stick counts, -512..512
STICK_SHIFT    = const(6)                  # 512 >> 6 = 8, matches old ADC >>12
INVERT_Y       = const(1)                  # 1 = stick up is negative

I_X       = const(0)
I_Y       = const(1)
I_FIRE    = const(2)
I_BOMB    = const(3)
I_RESTART = const(4)
I_DPREV   = const(5)
INPUT = array.array('i', [0, 0, 0, 0, 0, 0])

# ---- playfield --------------------------------------------------------------
RIGHT_LIM  = const(MAXSCREEN_X - 50)       # 270
LEFT_LIM   = const(50)
SKY_TOP    = const(35)                     # first flyable row (below HUD)
SKY_BOT    = const(230)                    # last flyable row
TER_OFF    = const(MAXSCREEN_Y - 108)      # 132 -> terrain y 168..238

# ---- HUD geometry (band y 0..32) --------------------------------------------
HUD_H       = const(32)
MINI_X      = const(96)                    # minimap content 96..223 (64 cells x 2px)
MINI_BOX_X  = const(95)                    # outline 95..225
MINI_POS    = const(37)                    # terrain cell bias, keeps strip aligned
MINI_WRAP   = const(864)                   # enemy wrap bias, 864>>4 + 96 = 150
MINI_VIEW_X = const(150)                   # left edge of view window in minimap
VIEW_W      = const(MAXSCREEN_X >> 4)      # 20 px of minimap == one screen
LIVES_X     = const(0)
LIVES_Y     = const(0)
LIVES_STEP  = const(17)
LIVES_MAX   = const(5)
BOMB_X      = const(232)
BOMB_Y      = const(1)
BOMB_STEP   = const(5)
BOMB_MAX    = const(5)
SCORE_X     = const(78)                    # left edge of rightmost digit, grows left
SCORE_Y     = const(13)
SCORE_SIZE  = const(1)
DBG_X       = const(312)
FPS_X       = const(296)
FPS_Y       = const(34)

# ---- colors (standard RGB565, NOT byte swapped) -----------------------------
BROWN   = const(0x9260)
BLUE    = const(0x007C)
GREEN   = const(0x07E0)
YELLOW  = const(0xFF00)
PINK    = const(0xF81F)
PURPLE  = const(0x8811)
RED     = const(0xE003)
GREY    = const(0xC618)
WHITE   = const(0xFFFF)
LT_BLUE = const(0xA57C)
BLACK   = const(0x0000)

# score500 sprite colour keys (were 0xe0ff / 0x00f8 / 0x1f00 byte swapped)
KEY_YELLOW = const(0xFFE0)
KEY_RED    = const(0xF800)
KEY_BLUE   = const(0x001F)

# ---- sprite positions -------------------------------------------------------
MUTANT   = const(4)
BOMBER   = const(10)
MOTHER   = const(14)
LANDER   = const(17)
HUMANOID = const(26)
POD      = const(28)
BAITER1  = const(30)
BAITER2  = const(33)
MINE     = const(36)
SCORE50  = const(39)
SCORE25  = const(40)
SCORE0   = const(41)
SBOMB_SPRITE = const(29)
EXHAUST_SPRITE = const(38)

# ---- lander states ----------------------------------------------------------
LANDER_HUNTING    = const(0)
LANDER_DESCENDING = const(1)
LANDER_ASCENDING  = const(2)
LANDER_MUTATING   = const(3)
HUMANOID_FALLING  = const(4)
HUMANOID_CAUGHT   = const(5)

PLAYER_PARAMS = const(17)
PLAYER_X   = const(0)
PLAYER_Y   = const(1)
PLAYER_DIR = const(2)
MAP_X      = const(3)
PLAYER_EXP = const(4)
SCORE      = const(5)
LIVES      = const(6)
SBOMBS     = const(7)
HYPER      = const(8)
SBOMB_RDY  = const(9)
THRUST     = const(10)
WAVE       = const(11)
ENEMY_REMAIN = const(12)
HUMAN_REMAIN = const(13)
INTERMISSION = const(14)
SCORE_500    = const(15)
WAVE_TIME    = const(16)

NUM_ENEMY = const(100)
ENEMY_PARAMS = const(11)
ENEMY_X      = const(0)
ENEMY_Y      = const(1)
ENEMY_VX     = const(2)
ENEMY_VY     = const(3)
ENEMY_SPRITE   = const(4)
ENEMY_POS_ANI  = const(5)
ENEMY_MAX_ANI  = const(6)
ENEMY_ALIVE    = const(7)
ENEMY_ONSCREEN = const(8)
ENEMY_STATE    = const(9)
ENEMY_TARGET   = const(10)

NUM_FIRE = const(6)
FIRE_PARAMS = const(4)
FIRE_START  = const(0)
FIRE_X      = const(1)
FIRE_Y      = const(2)
FIRE_DIR    = const(3)

NUM_EXP = const(200)
SHIP_EXP_PARAMS = const(5)
EXP_X     = const(0)
EXP_Y     = const(1)
EXP_VX    = const(2)
EXP_VY    = const(3)
EXP_ALIVE = const(4)

NUM_EN_EXP = const(640)
ENEMY_EXP_PARAMS = const(6)
EN_EXP_X     = const(0)
EN_EXP_Y     = const(1)
EN_EXP_VX    = const(2)
EN_EXP_VY    = const(3)
EN_EXP_ALIVE = const(4)
EN_EXP_COLOR = const(5)

NUM_STARS    = const(200)
STARS_PARAMS = const(4)
STAR_X       = const(0)
STAR_Y       = const(1)
STAR_COLOR   = const(2)
STAR_DIR     = const(3)

NUM_MISSILES = const(20)
MISSILE_PARAMS = const(6)
MISSILE_X    = const(0)
MISSILE_Y    = const(1)
MISSILE_VX   = const(2)
MISSILE_VY   = const(3)
MISSILE_ACTIVE = const(4)
MISSILE_COLOR = const(5)

NUM_SCORE500 = const(10)
SCORE500_PARAMS = const(4)
SCORE_SX     = const(0)
SCORE_SY     = const(1)
SCORE_ACTIVE = const(2)
SCORE_SPRITE = const(3)

COLORS = array.array('H', [0])
STAR_COLORS = array.array('H', [0])

TERRAIN = bytearray([
    0x2A, 0xAA, 0xAA, 0xAA, 0xAA, 0xAA, 0xAB, 0xA1, 0xD5, 0x55, 0x55, 0x55, 0x55, 0x55, 0xAA, 0xBF,
    0xFF, 0xFF, 0xFF, 0xC0, 0x00, 0x00, 0x00, 0x55, 0x55, 0x57, 0xFF, 0xC0, 0x01, 0x55, 0x55, 0x55,
    0x55, 0x55, 0x55, 0x5F, 0xE0, 0x15, 0x55, 0x55, 0x57, 0xFF, 0xF0, 0x00, 0x15, 0x55, 0x5F, 0xFF,
    0xFF, 0xFF, 0xFF, 0x00, 0x00, 0x00, 0x00, 0x05, 0x55, 0x7F, 0xFF, 0xE0, 0x00, 0x05, 0x55, 0x55,
    0x55, 0x55, 0xFC, 0x05, 0x55, 0x55, 0x50, 0x01, 0xFF, 0xFF, 0xFF, 0xC0, 0x00, 0x0A, 0xAA, 0xAA,
    0xAA, 0xFF, 0x00, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xF0, 0x00, 0x00, 0x1F, 0xE0, 0x00, 0x55, 0x55,
    0x55, 0x40, 0xAA, 0xAA, 0xAA, 0xAA, 0xAA, 0xAA, 0xB5, 0x57, 0xAA, 0xAA, 0xAA, 0xF5, 0x7F, 0xD5,
    0x55, 0x55, 0x57, 0xFF, 0x80, 0x07, 0xE0, 0x7F, 0xF1, 0x55, 0x7F, 0xFF, 0xFF, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x0F, 0xEF, 0x76, 0x91, 0x11, 0x11, 0x5E, 0xDB, 0xE9, 0x84, 0x77, 0xEC, 0xC4, 0x87,
    0x47, 0x98, 0x08, 0x98, 0x3F, 0xC3, 0xCB, 0xDB, 0x9F, 0xC7, 0x5F, 0x2F, 0xC7, 0x7D, 0xEF, 0xBF,
    0xFA, 0x4C, 0x57, 0x2B, 0x61, 0xEF, 0xEF, 0xFB, 0xF7, 0xE8, 0x00, 0x20, 0x40, 0x00, 0x14, 0x04,
    0x04, 0x3C, 0x06, 0x00, 0x1D, 0x07, 0x3C, 0xE1, 0xA5, 0x55, 0x55, 0x45, 0x2A, 0xAA, 0xAA, 0xAA,
    0xA8, 0x55, 0x55, 0x55, 0x55, 0x55, 0x55, 0x55, 0x55, 0x55, 0x55, 0x56, 0xAA, 0xAA, 0xFE, 0xAA,
    0xAA, 0xAA, 0xAA, 0xAA, 0xAA, 0xAA, 0xAA, 0xEA, 0xAA, 0xAA, 0xA8, 0x02, 0xAA, 0xAA, 0xAA, 0xAA,
    0xBF, 0xBE, 0x3E, 0x63, 0xFF, 0xE0, 0xD8, 0x1C, 0x18, 0x2A, 0xAB, 0x1E, 0x77, 0x7A, 0xAF, 0xA8,
    0x40, 0x70, 0x7D, 0x40, 0x0B, 0xFB, 0xFA, 0xFF, 0xC1, 0x53, 0x54, 0x75, 0x70, 0x03, 0x00, 0x00
])

# Mini terrain data
MINI_TERRAIN_DATA = bytearray([
    0x25,0x70,0x07,0x26,0x77,0x00,0x26,0x07,0x70,0x24,0x07,0x70,
    0x23,0x07,0x70,0x23,0x70,0x07,0x24,0x07,0x70,0x25,0x70,0x07,
    0x26,0x77,0x00,0x25,0x07,0x70,0x24,0x07,0x70,0x23,0x07,0x70,
    0x21,0x07,0x70,0x22,0x70,0x07,0x24,0x77,0x00,0x24,0x70,0x07,
    0x26,0x77,0x00,0x26,0x77,0x00,0x25,0x77,0x00,0x25,0x70,0x07,
    0x26,0x77,0x00,0x24,0x07,0x70,0x23,0x70,0x07,0x25,0x77,0x00,
    0x26,0x70,0x07,0x26,0x77,0x00,0x26,0x77,0x00,0x25,0x07,0x70,
    0x23,0x07,0x70,0x22,0x07,0x70,0x21,0x77,0x00,0x21,0x70,0x07,
    0x23,0x70,0x07,0x25,0x70,0x07,0x25,0x07,0x70,0x25,0x77,0x00,
    0x25,0x77,0x00,0x24,0x77,0x00,0x22,0x07,0x70,0x20,0x07,0x70,
    0x1E,0x07,0x70,0x1C,0x07,0x70,0x1D,0x70,0x07,0x1F,0x70,0x07,
    0x21,0x70,0x07,0x22,0x70,0x07,0x24,0x70,0x07,0x26,0x70,0x07,
    0x26,0x77,0x00,0x26,0x77,0x00,0x26,0x77,0x00,0x26,0x77,0x00,
    0x26,0x77,0x00,0x25,0x77,0x00,0x25,0x70,0x07,0x26,0x77,0x00,
    0x24,0x07,0x70,0x23,0x77,0x00,0x24,0x77,0x00,0x22,0x07,0x70,
    0x23,0x70,0x07,0x22,0x07,0x70,0x21,0x70,0x07,0x23,0x70,0x07])

char_map = array.array('b', (
     0x00, 0x3e, 0x67, 0x67, 0x67, 0x67, 0x7f, 0x3e,   # U+0030 (0)
     0x00, 0x18, 0x1c, 0x1c, 0x18, 0x18, 0x7e, 0x7e,   # U+0031 (1)
     0x00, 0x3e, 0x73, 0x38, 0x1c, 0x0e, 0x7f, 0x7f,   # U+0032 (2)
     0x00, 0x7e, 0x30, 0x18, 0x30, 0x67, 0x7f, 0x3e,   # U+0033 (3)
     0x00, 0x03, 0x03, 0x3b, 0x7f, 0x38, 0x38, 0x38,   # U+0034 (4)
     0x00, 0x7f, 0x07, 0x3f, 0x60, 0x67, 0x7f, 0x3e,   # U+0035 (5)
     0x00, 0x3e, 0x07, 0x3f, 0x67, 0x67, 0x7f, 0x3e,   # U+0036 (6)
     0x00, 0x7f, 0x71, 0x38, 0x1c, 0x0e, 0x07, 0x07,   # U+0037 (7)
     0x00, 0x3e, 0x67, 0x3e, 0x67, 0x67, 0x7f, 0x3e,   # U+0038 (8)
     0x00, 0x3e, 0x67, 0x67, 0x7e, 0x38, 0x1c, 0x0e,   # U+0039 (9)
))

# ---- display / gamepad ------------------------------------------------------
display = DVI_RP2_HSTX()
display.begin(fb, rv_colors.COLOR_MODE_BGR565, height=MAXSCREEN_Y,
              width=MAXSCREEN_X, bytes_per_pixel=2)
gamepad = Gamepad()

SCREEN = framebuf.FrameBuffer(fb2, MAXSCREEN_X, MAXSCREEN_Y, framebuf.RGB565)
draw_num = Draw_number(fb2, MAXSCREEN_X, 2)


# ---- template asm: fast fill + framebuffer copy ------------------------------
@micropython.asm_thumb
def fill_asm(r0, r1):                       # (buffer_addr, 16-bit color)
    mov(r3, r1)
    lsl(r2, r1, 16)
    orr(r3, r2)
    mov(r1, r0)
    mov(r2, r3)
    mov(r4, r3)
    mov(r5, r3)
    mov(r6, r3)
    mov(r7, r3)
    movwt(r0, (MAXSCREEN_X * MAXSCREEN_Y * 2) // (192))
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
def copy_fb(r0, r1):                        # r0=source, r1=dest
    movwt(r2, (MAXSCREEN_X * MAXSCREEN_Y * 2) // 32)
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


@micropython.asm_thumb
def calc_length(r0, r1) -> uint:            # sqrt(dx*dx + dy*dy)
    mov(r2, r0)
    mul(r2, r0)
    mov(r3, r1)
    mul(r3, r1)
    add(r0, r2, r3)
    vmov(s0, r0)
    vcvt_f32_s32(s0, s0)
    vsqrt(s0, s0)
    vcvt_s32_f32(s0, s0)
    vmov(r0, s0)


@micropython.viper
def show_num_viper(num: int, x_offset: int, y_offset: int, color: int, size: int):
    # right aligned: x_offset is the left edge of the rightmost digit, grows left
    if num < 0: return
    char_ptr = ptr8(char_map)
    screen_ptr = ptr16(fb2)
    char = 0
    first = 1
    step = 8 * size
    while num > 0 or first:
        first = 0
        total = num // 10
        digit = num - (total * 10)
        num = total
        x0 = x_offset - char * step
        if x0 < 0: return                   # clip instead of wrapping the row
        for y in range(8):
            row_data = char_ptr[digit * 8 + y]
            if row_data == 0: continue
            for x in range(8):
                if row_data & (1 << x):
                    px = x0 + x * size
                    py = y_offset + y * size
                    for sy in range(size):
                        addr = (py + sy) * MAXSCREEN_X + px
                        for sx in range(size):
                            screen_ptr[addr + sx] = color
        char += 1


def init_sprite(filename):
    with open(filename, "rb") as file:
        file.read(4)                        # skip header
        sprite = file.read()
    return sprite


def fade(input_color1, input_color2, palette):
    red1   = input_color1 >> 11 & 0b11111
    green1 = input_color1 >> 6  & 0b11111
    blue1  = input_color1       & 0b11111
    red2   = input_color2 >> 11 & 0b11111
    green2 = input_color2 >> 6  & 0b11111
    blue2  = input_color2       & 0b11111
    inc_red   = (red2 - red1) / 31
    inc_green = (green2 - green1) / 31
    inc_blue  = (blue2 - blue1) / 31
    for i in range(0, 32):
        red3   = red1   + int(i * inc_red)
        green3 = green1 + int(i * inc_green)
        blue3  = blue1  + int(i * inc_blue)
        palette.append(red3 << 11 | green3 << 6 | blue3)   # plain RGB565 now


def init_game():
    global PLAYER, FIRE, SHIP_EXP, STARS, ISIN, ICOS, ENEMY, ENEMY_EXP
    global SCORING, MINI_COLORS, MISSILES, SCORE500, SCORE500_COLORS
    PLAYER = array.array('i', [0] * PLAYER_PARAMS)
    SHIP_EXP = array.array('i', [0] * SHIP_EXP_PARAMS * NUM_EXP)
    ENEMY_EXP = array.array('i', [0] * ENEMY_EXP_PARAMS * NUM_EN_EXP)
    ENEMY = array.array('i', [0] * ENEMY_PARAMS * NUM_ENEMY)
    STARS = array.array('i', (0 for _ in range(STARS_PARAMS * NUM_STARS)))
    MISSILES = array.array('i', [0] * (NUM_MISSILES * MISSILE_PARAMS))
    SCORING = array.array('H', [0] * 100)
    MINI_COLORS = array.array('H', [0] * 100)
    SCORE500 = array.array('i', [0] * NUM_SCORE500 * SCORE500_PARAMS)
    SCORE500_COLORS = array.array('H', (YELLOW, RED, LT_BLUE))
    FIRE = array.array('i', [0] * FIRE_PARAMS * NUM_FIRE)   # int32: x now > 255
    ISIN = array.array('i', (int(sin(radians(i)) * (1 << SCALE)) for i in range(360)))
    ICOS = array.array('i', (int(cos(radians(i)) * (1 << SCALE)) for i in range(360)))
    start_game()


def start_game():
    PLAYER[PLAYER_X] = LEFT_LIM
    PLAYER[PLAYER_Y] = 90
    PLAYER[HYPER] = 100
    PLAYER[SBOMBS] = 3
    PLAYER[SBOMB_RDY] = 1
    PLAYER[LIVES] = 3
    PLAYER[WAVE] = 1
    PLAYER[INTERMISSION] = 0
    PLAYER[SCORE] = 0
    for ind in range(NUM_ENEMY):
        i = ind * ENEMY_PARAMS
        ENEMY[i + ENEMY_ALIVE] = 0
        ENEMY[i + ENEMY_STATE] = 0
    for ind in range(NUM_FIRE):
        FIRE[ind * FIRE_PARAMS + FIRE_DIR] = 0
    clear_missiles()
    get_wave_enemies(1)


def init_stars():
    for index in range(NUM_STARS):
        i = index * STARS_PARAMS
        x = randint(0, 2047)
        STARS[i + STAR_X] = x
        max_y = TER[x] + TER_OFF                   # stars above terrain
        STARS[i + STAR_Y] = randint(SKY_TOP, max_y)
        STARS[i + STAR_COLOR] = randint(0, 31)
        if STARS[i + STAR_COLOR] > 16:
            STARS[i + STAR_DIR] = -1
        else:
            STARS[i + STAR_DIR] = 1


def init_enemy(number_landers=1, number_bombers=0, number_mother=0):
    number_mutants = 0
    number_pods    = 0
    number_humanoids = 10
    number_baiter1 = 0
    number_baiter2 = 0
    # sprite pos, max animations, quantity, points, mini_color1, mini_color2
    enemies = [(MUTANT, 5, number_mutants, 150, PINK, GREEN),
               (BOMBER, 4, number_bombers, 250, PINK, YELLOW),
               (MOTHER, 3, number_mother, 1000, PINK, RED),
               (POD, 1, number_pods, 150, YELLOW, RED),
               (LANDER, 9, number_landers, 150, YELLOW, GREEN),
               (HUMANOID, 2, number_humanoids, 0, GREY, GREY),
               (BAITER1, 3, number_baiter1, 200, GREEN, GREEN),
               (BAITER2, 3, number_baiter2, 0, GREEN, GREEN)]
    total = 0
    for enemy in enemies:
        sprite_pos, max_anim, quantity, score, mini_color1, mini_color2 = enemy
        SCORING[sprite_pos] = score
        MINI_COLORS[sprite_pos] = mini_color1
        MINI_COLORS[sprite_pos + 1] = mini_color2
        if sprite_pos != BAITER2:                  # skip second half of Baiter
            for ind in range(quantity):
                i = total * ENEMY_PARAMS
                ENEMY[i + ENEMY_SPRITE] = sprite_pos
                ENEMY[i + ENEMY_X] = randint(0, 2047)
                if sprite_pos == HUMANOID:
                    ENEMY[i + ENEMY_STATE] = -1
                else:
                    ENEMY[i + ENEMY_STATE] = 0
                ENEMY[i + ENEMY_Y] = randint(SKY_TOP, 160)
                ENEMY[i + ENEMY_MAX_ANI] = max_anim
                ENEMY[i + ENEMY_POS_ANI] = randint(0, max_anim)
                ENEMY[i + ENEMY_ALIVE] = 1

                if sprite_pos == BOMBER:
                    ENEMY[i + ENEMY_VX] = (int(randint(0, 1)) * 2 - 1) * 3

                if sprite_pos == BAITER1:
                    ENEMY[i + ENEMY_VX] = (int(randint(0, 1)) * 2 - 1) * 10
                    ENEMY[i + ENEMY_VY] = (int(randint(0, 1)) * 2 - 1) * 3
                if sprite_pos == LANDER:
                    ENEMY[i + ENEMY_VX] = (int(randint(0, 1)) * 2 - 1) * 1
                    ENEMY[i + ENEMY_VY] = (int(randint(0, 1)) * 2 - 1) * 1
                    ENEMY[i + ENEMY_STATE] = LANDER_HUNTING
                    ENEMY[i + ENEMY_TARGET] = -1
                if sprite_pos == BAITER1:           # first half Baiter
                    j = total * ENEMY_PARAMS + ENEMY_PARAMS
                    ENEMY[j + ENEMY_X] = ENEMY[j + ENEMY_X - ENEMY_PARAMS] + 8
                    ENEMY[j + ENEMY_Y] = ENEMY[j + ENEMY_Y - ENEMY_PARAMS]
                    ENEMY[j + ENEMY_SPRITE] = sprite_pos + max_anim
                    ENEMY[j + ENEMY_MAX_ANI] = max_anim
                    ENEMY[j + ENEMY_POS_ANI] = ENEMY[j + ENEMY_POS_ANI - ENEMY_PARAMS]
                    ENEMY[j + ENEMY_ALIVE] = 1
                    total += 1
                total += 1


@micropython.viper
def init_mines(x: int, y: int):
    enemy = ptr32(ENEMY)
    for ind in range(NUM_ENEMY):
        i = ind * ENEMY_PARAMS
        if enemy[i + ENEMY_ALIVE] == 0:
            enemy[i + ENEMY_ALIVE] = 1
            enemy[i + ENEMY_SPRITE] = MINE
            enemy[i + ENEMY_MAX_ANI] = 2
            enemy[i + ENEMY_X] = x
            enemy[i + ENEMY_Y] = y
            enemy[i + ENEMY_VX] = 0
            enemy[i + ENEMY_VY] = 0
            return


@micropython.viper
def init_pods(x: int, y: int):
    enemy = ptr32(ENEMY)
    number_pods = int(randint(4, 6))
    for count in range(number_pods):
        for ind in range(NUM_ENEMY):
            i = ind * ENEMY_PARAMS
            if enemy[i + ENEMY_ALIVE] == 0:
                enemy[i + ENEMY_ALIVE] = 1
                enemy[i + ENEMY_SPRITE] = POD
                enemy[i + ENEMY_MAX_ANI] = 1
                enemy[i + ENEMY_X] = x
                enemy[i + ENEMY_Y] = y
                enemy[i + ENEMY_VX] = (int(randint(0, 1)) * 2 - 1) * 4
                enemy[i + ENEMY_VY] = (int(randint(0, 1)) * 2 - 1) * 4
                break


@micropython.viper
def init_score500(sprite_pos: int, x_pos: int, y_pos: int):
    score500 = ptr32(SCORE500)
    for ind in range(NUM_SCORE500):
        i = ind * SCORE500_PARAMS
        if score500[i + SCORE_ACTIVE] == 0:
            score500[i + SCORE_ACTIVE] = 100
            score500[i + SCORE_SX] = x_pos
            score500[i + SCORE_SY] = y_pos
            score500[i + SCORE_SPRITE] = sprite_pos
            return


def get_wave_enemies(wave):
    effective_wave = min(wave, 4)
    wave_configs = {
        1: (15, 0, 0),                      # (LANDERS, BOMBERS, MOTHERS)
        2: (20, 3, 1),
        3: (20, 4, 3),
        4: (20, 5, 4)}
    number_landers, number_bombers, number_mother = wave_configs[effective_wave]
    init_enemy(number_landers, number_bombers, number_mother)


@micropython.viper
def twinkle_stars():
    stars = ptr32(STARS)
    for index in range(NUM_STARS):
        i = index * STARS_PARAMS
        dim = int(stars[i + STAR_COLOR])
        dim += stars[i + STAR_DIR]
        if dim > 31 or dim < 1:
            stars[i + STAR_DIR] *= -1
        else:
            stars[i + STAR_COLOR] = dim


# ---- input ------------------------------------------------------------------
def read_gamepad():
    gamepad.read()
    b = gamepad.buttons
    if not (b & GAMEPAD_SELECT):
        shutdown()
    x = gamepad.x                           # -512..512 analog
    y = gamepad.y
    if -DEADZONE < x < DEADZONE:
        x = 0
    if -DEADZONE < y < DEADZONE:
        y = 0
    if INVERT_Y:
        y = -y
    INPUT[I_X] = x >> STICK_SHIFT           # -8..8, same range as the old pots
    INPUT[I_Y] = y >> (STICK_SHIFT-1)
    INPUT[I_FIRE] = 0 if (b & BTN_FIRE) else 1
    INPUT[I_BOMB] = 0 if (b & BTN_BOMB) else 1
    d = 0 if (b & BTN_RESTART) else 1
    if d and not INPUT[I_DPREV]:
        INPUT[I_RESTART] = 1
    INPUT[I_DPREV] = d


@micropython.viper
def move_player():
    player = ptr32(PLAYER)
    if player[PLAYER_EXP] > 0 or player[LIVES] < 1 or player[INTERMISSION]: return
    enemy = ptr32(ENEMY)
    scoring = ptr16(SCORING)
    inp = ptr32(INPUT)
    x_inc = inp[I_X]
    y_inc = inp[I_Y]
    player[THRUST] = 1
    if -2 < x_inc < 2:
        x_inc = 0
        player[THRUST] = 0
    if -2 < y_inc < 2:
        y_inc = 0
    p_x = player[PLAYER_X]
    p_y = player[PLAYER_Y]
    m_x = player[MAP_X]
    p_dir = player[PLAYER_DIR]
    p_x += x_inc >> 1
    p_y -= y_inc >> 1
    if p_x < RIGHT_LIM and p_dir == 1:              # shift player to right
        player[PLAYER_X] += 5
        m_x -= 5
    elif p_x > LEFT_LIM and p_dir == 0:             # shift player to left
        player[PLAYER_X] -= 5
        m_x += 5
    if p_x > LEFT_LIM - 6 and x_inc > 1:            # scroll map right
        m_x += x_inc
    elif p_x < RIGHT_LIM + 6 and x_inc < 1:         # scroll map left
        m_x += x_inc
    player[MAP_X] = m_x & 2047
    if x_inc > 0: player[PLAYER_DIR] = 0
    if x_inc < 0: player[PLAYER_DIR] = 1
    if SKY_TOP < p_y < MAXSCREEN_Y - 8:
        player[PLAYER_Y] = p_y
    screen_x = player[MAP_X]
    if player[HYPER]: return                        # impervious during hyper
    for enemy_index in range(NUM_ENEMY):            # player vs enemy collision
        e_i = enemy_index * ENEMY_PARAMS
        if enemy[e_i + ENEMY_ALIVE] and enemy[e_i + ENEMY_ONSCREEN] and enemy[e_i + ENEMY_SPRITE] != HUMANOID:
            e_x = (2048 + enemy[e_i + ENEMY_X] - screen_x) & 2047
            e_y = enemy[e_i + ENEMY_Y]
            if p_x + 16 > e_x and p_x < e_x + 8 and p_y + 8 > e_y and p_y < e_y + 8:
                init_ship_exp()
                enemy[e_i + ENEMY_ALIVE] = 0
                sprite_pos = enemy[e_i + ENEMY_SPRITE]
                if sprite_pos == BAITER1: enemy[e_i + ENEMY_ALIVE + ENEMY_PARAMS] = 0
                if sprite_pos == BAITER2: enemy[e_i + ENEMY_ALIVE - ENEMY_PARAMS] = 0
                add_score(scoring[sprite_pos])
                sprite_ani = sprite_pos + enemy[e_i + ENEMY_POS_ANI]
                init_enemy_exp(sprite_pos, sprite_ani, e_x, e_y)
                if sprite_pos == LANDER and enemy[e_i + ENEMY_STATE] == LANDER_ASCENDING:
                    target = enemy[e_i + ENEMY_TARGET]
                    if target >= 0:
                        enemy[target * ENEMY_PARAMS + ENEMY_STATE] = HUMANOID_FALLING
                return


@micropython.viper
def check_fire():
    fire = ptr32(FIRE)
    player = ptr32(PLAYER)
    inp = ptr32(INPUT)
    p_x = player[PLAYER_X]
    p_y = player[PLAYER_Y]
    p_dir = player[PLAYER_DIR]
    if player[PLAYER_EXP] > 0 or player[LIVES] < 1 or player[INTERMISSION]: return
    if player[SBOMB_RDY] and player[SBOMBS] and inp[I_BOMB]:
        player[SBOMB_RDY] = 0
        player[SBOMBS] -= 1
        smart_bomb()
    if not player[SBOMB_RDY] and not inp[I_BOMB]:
        player[SBOMB_RDY] = 1
    if inp[I_FIRE]:
        snd.play(FIRE_SOUND, vol=150)
        for ind in range(NUM_FIRE):
            i = ind * FIRE_PARAMS
            if fire[i + FIRE_DIR] == 0:
                if p_dir == 1:                  # right
                    fire[i + FIRE_X] = p_x - 10
                    fire[i + FIRE_START] = p_x - 10
                    fire[i + FIRE_Y] = p_y + 4
                    fire[i + FIRE_DIR] = 1
                else:                           # left
                    fire[i + FIRE_X] = p_x + 20
                    fire[i + FIRE_START] = p_x + 20
                    fire[i + FIRE_Y] = p_y + 4
                    fire[i + FIRE_DIR] = 255
                break


@micropython.viper
def add_score(points: int):
    player = ptr32(PLAYER)
    cur_score = player[SCORE]
    new_score = cur_score + points
    if new_score // EXTRA > cur_score // EXTRA:     # extra life + sbomb
        player[LIVES] += 1
        player[SBOMBS] += 1
    player[SCORE] += points


@micropython.viper
def smart_bomb():
    player = ptr32(PLAYER)
    if player[PLAYER_EXP] > 0 or player[LIVES] < 1: return
    scoring = ptr16(SCORING)
    screen_x = player[MAP_X]
    enemy = ptr32(ENEMY)
    for enemy_index in range(NUM_ENEMY):
        e_i = enemy_index * ENEMY_PARAMS
        if enemy[e_i + ENEMY_ALIVE] and enemy[e_i + ENEMY_ONSCREEN] and enemy[e_i + ENEMY_SPRITE] != HUMANOID:
            enemy[e_i + ENEMY_ALIVE] = 0
            sprite_pos = enemy[e_i + ENEMY_SPRITE]
            add_score(scoring[sprite_pos])
            e_x = (2048 + enemy[e_i + ENEMY_X] - screen_x) & 2047
            e_y = enemy[e_i + ENEMY_Y]
            target_index = enemy[e_i + ENEMY_TARGET]
            sprite_ani = sprite_pos + enemy[e_i + ENEMY_POS_ANI]
            init_enemy_exp(sprite_pos, sprite_ani, e_x, e_y)
            if sprite_pos == LANDER and target_index >= 0:
                if enemy[e_i + ENEMY_STATE] == LANDER_ASCENDING:
                    enemy[target_index * ENEMY_PARAMS + ENEMY_STATE] = HUMANOID_FALLING
                else:
                    enemy[target_index * ENEMY_PARAMS + ENEMY_STATE] = -1


@micropython.viper
def init_ship_exp():
    # return # god mode
    player = ptr32(PLAYER)
    explode = ptr32(SHIP_EXP)
    isin = ptr32(ISIN)
    icos = ptr32(ICOS)
    player[PLAYER_EXP] = 100
    player[LIVES] -= 1
    p_x = (player[PLAYER_X] + 8) << SCALE
    p_y = (player[PLAYER_Y] + 4) << SCALE
    snd.play(PLAYEREXP_SOUND, vol=220)
    for ind in range(NUM_EXP):
        i = ind * SHIP_EXP_PARAMS
        explode[i + EXP_X] = p_x
        explode[i + EXP_Y] = p_y
        deg = int(randint(0, 359))
        explode[i + EXP_VX] = (int(randint(-50000, 50000)) * icos[deg]) >> SCALE
        explode[i + EXP_VY] = (int(randint(-50000, 50000)) * isin[deg]) >> SCALE
        explode[i + EXP_ALIVE] = 1


@micropython.viper
def init_enemy_exp(sprite_num: int, sprite_ani: int, e_x: int, e_y: int):
    sprite = ptr16(SPRITES)
    exp    = ptr32(ENEMY_EXP)
    isin   = ptr32(ISIN)
    icos   = ptr32(ICOS)
    player = ptr32(PLAYER)
    screen_x = player[MAP_X]
    offset = sprite_ani * 8 * 8
    if sprite_num == LANDER:
        snd.play(LANDDIE_SOUND, vol=220)
    for y in range(8):
        for x in range(8):
            color = sprite[y * 8 + x + offset]
            for ind in range(NUM_EN_EXP):
                i = ind * ENEMY_EXP_PARAMS
                if exp[i + EN_EXP_ALIVE]: continue
                exp[i + EN_EXP_ALIVE] = 150
                exp[i + EN_EXP_COLOR] = color
                exp[i + EN_EXP_X] = (screen_x + e_x) << SCALE
                exp[i + EN_EXP_Y] = e_y << SCALE
                deg = int(randint(0, 359))
                exp[i + EN_EXP_VX] = (int(randint(-30000, 30000)) * icos[deg]) >> SCALE
                exp[i + EN_EXP_VY] = (int(randint(-30000, 30000)) * isin[deg]) >> SCALE
                break


# ---- HUD --------------------------------------------------------------------
@micropython.viper
def draw_hud():
    screen = ptr16(fb2)
    sprite = ptr16(SPRITES)
    player = ptr32(PLAYER)
    # smart bombs: vertical stack, right cluster
    num_bombs = player[SBOMBS]
    if num_bombs > BOMB_MAX: num_bombs = BOMB_MAX
    hud_x = BOMB_X
    hud_y = BOMB_Y
    offset = SBOMB_SPRITE * 8 * 8
    for num in range(num_bombs):
        for y in range(8):
            for x in range(8):
                color = sprite[y * 8 + x + offset]
                if color:
                    screen[(hud_y + y) * MAXSCREEN_X + hud_x + x] = color
        hud_y += BOMB_STEP
    # lives: single row of ships, top left
    lives = player[LIVES]
    if lives > LIVES_MAX: lives = LIVES_MAX
    hud_x = LIVES_X
    hud_y = LIVES_Y
    while lives > 0:
        player_offset = hud_y * MAXSCREEN_X + hud_x
        for y in range(8):
            for x in range(8):
                sprite_color = sprite[y * 8 + x]
                if sprite_color:
                    screen[y * MAXSCREEN_X + x + player_offset] = sprite_color
                sprite_color = sprite[y * 8 + x + 64]
                if sprite_color:
                    screen[y * MAXSCREEN_X + x + player_offset + 8] = sprite_color
        lives -= 1
        hud_x += LIVES_STEP


@micropython.viper
def draw_stars():
    player = ptr32(PLAYER)
    if player[INTERMISSION]: return
    stars = ptr32(STARS)
    colors = ptr16(STAR_COLORS)
    screen = ptr16(fb2)
    map_x = player[MAP_X]
    for index in range(NUM_STARS):
        i = index * STARS_PARAMS
        x = stars[i + STAR_X]
        if map_x < x < map_x + MAXSCREEN_X or x < (MAXSCREEN_X + map_x - 2048):
            x = (2048 + x - map_x) & 2047
            if x >= MAXSCREEN_X: continue
            y = stars[i + STAR_Y]
            color = 32 - stars[i + STAR_COLOR]
            addr = y * MAXSCREEN_X + x
            if 0 < addr < MAXSCREEN_X * MAXSCREEN_Y:
                screen[addr] = colors[color]


@micropython.viper
def draw_fire():
    colors = ptr16(COLORS)
    screen = ptr16(fb2)
    enemy = ptr32(ENEMY)
    fire = ptr32(FIRE)
    player = ptr32(PLAYER)
    scoring = ptr16(SCORING)
    screen_x = player[MAP_X]
    start = 0
    max_color = int(MAX_COLORS) - 1
    for ind in range(NUM_FIRE):
        i = ind * FIRE_PARAMS
        inc = fire[i + FIRE_DIR]
        if inc != 0:
            y1 = fire[i + FIRE_Y]
            y = y1 * MAXSCREEN_X
            x = fire[i + FIRE_X]
            start = fire[i + FIRE_START]
            for enemy_index in range(NUM_ENEMY):
                e_i = enemy_index * ENEMY_PARAMS
                if enemy[e_i + ENEMY_ALIVE] and enemy[e_i + ENEMY_ONSCREEN]:
                    y2 = y1
                    e_x = (2048 + enemy[e_i + ENEMY_X] - screen_x) & 2047
                    e_y = enemy[e_i + ENEMY_Y]
                    start_x = start
                    end_x = x
                    if end_x < start_x:
                        start_x, end_x = end_x, start_x
                    if y2 >= e_y and y2 < e_y + 8:
                        sprite_num = enemy[e_i + ENEMY_SPRITE]
                        if end_x >= e_x and start_x < e_x + 8 and sprite_num != MINE:
                            enemy[e_i + ENEMY_ALIVE] = 0
                            if sprite_num == BAITER1:
                                enemy[e_i + ENEMY_ALIVE + ENEMY_PARAMS] = 0
                            if sprite_num == BAITER2:
                                enemy[e_i + ENEMY_ALIVE - ENEMY_PARAMS] = 0
                            add_score(scoring[sprite_num])
                            inc = 0
                            fire[i + FIRE_DIR] = 0
                            sprite_ani = sprite_num + enemy[e_i + ENEMY_POS_ANI]
                            init_enemy_exp(sprite_num, sprite_ani, e_x, e_y)
                            if sprite_num == MOTHER:
                                init_pods(enemy[e_i + ENEMY_X], e_y)
                            if sprite_num == LANDER:
                                target = enemy[e_i + ENEMY_TARGET]
                                if target >= 0:
                                    if enemy[e_i + ENEMY_STATE] == LANDER_ASCENDING:
                                        enemy[target * ENEMY_PARAMS + ENEMY_STATE] = HUMANOID_FALLING
                                    elif enemy[e_i + ENEMY_STATE] == LANDER_HUNTING:
                                        enemy[target * ENEMY_PARAMS + ENEMY_STATE] = -1

            if inc == 255:                          # fire right
                while start < x:
                    color = max_color - ((x - start) // 3)
                    if color < 0: color = 0
                    screen[y + start] = colors[color]
                    start += 1
                if start > MAXSCREEN_X - 15:        # end fire
                    fire[i + FIRE_DIR] = 0
                else:
                    fire[i + FIRE_X] += 15
                    fire[i + FIRE_START] += 5
            if inc == 1:                            # fire left
                while start > x:
                    color = max_color - ((start - x) // 3)
                    if color < 0: color = 0
                    screen[y + start] = colors[color]
                    start -= 1
                if start < 15:                      # end fire
                    fire[i + FIRE_DIR] = 0
                else:
                    fire[i + FIRE_X] -= 15
                    fire[i + FIRE_START] -= 5


@micropython.viper
def draw_player():
    player = ptr32(PLAYER)
    sprite = ptr16(SPRITES)
    screen = ptr16(fb2)
    if player[PLAYER_EXP] > 0 or player[LIVES] < 1 or player[HYPER] != 0 or player[ENEMY_REMAIN] == 0: return
    player_direction = player[PLAYER_DIR]
    thrust = player[THRUST]
    sprite_offset = player_direction * 8 * 8 * 2
    p_x = player[PLAYER_X]
    p_y = player[PLAYER_Y]
    player_offset = p_y * MAXSCREEN_X + p_x
    if player_direction:
        exhaust_offset = 16 + int(randint(-3, 0))
    else:
        exhaust_offset = -8 + int(randint(0, 3))
    hud_addr = ((p_y >> 3) * MAXSCREEN_X - MAXSCREEN_X) + (p_x >> 4) + MINI_VIEW_X
    if 0 < hud_addr < MAXSCREEN_X * (HUD_H - 2):
        screen[hud_addr] = WHITE
        screen[hud_addr + MAXSCREEN_X] = WHITE
    for y in range(8):
        for x in range(8):
            if thrust:
                if player_direction:
                    exhaust_addr = y * 8 + (8 - x) + (EXHAUST_SPRITE * 8 * 8)
                else:
                    exhaust_addr = y * 8 + x + (EXHAUST_SPRITE * 8 * 8)
                exhaust_color = sprite[exhaust_addr]
                if exhaust_color:
                    screen_addr = y * MAXSCREEN_X + x + player_offset + exhaust_offset
                    if 0 < screen_addr < MAXSCREEN_X * MAXSCREEN_Y:
                        screen[screen_addr] = exhaust_color

            sprite_color = sprite[y * 8 + x + sprite_offset]
            if sprite_color:
                screen_addr = y * MAXSCREEN_X + x + player_offset
                if 0 < screen_addr < MAXSCREEN_X * MAXSCREEN_Y:
                    screen[screen_addr] = sprite_color
            sprite_color = sprite[y * 8 + x + sprite_offset + 8 * 8]   # second half of ship
            if sprite_color:
                screen_addr = y * MAXSCREEN_X + x + player_offset + 8
                if 0 < screen_addr < MAXSCREEN_X * MAXSCREEN_Y:
                    screen[screen_addr] = sprite_color


@micropython.viper
def draw_enemy():
    player = ptr32(PLAYER)
    if player[INTERMISSION]: return
    enemy = ptr32(ENEMY)
    screen = ptr16(fb2)
    sprite = ptr16(SPRITES)
    mini_colors = ptr16(MINI_COLORS)
    screen_x = player[MAP_X]
    total_enemy = 0
    total_human = 0
    for ind in range(NUM_ENEMY):
        i = ind * ENEMY_PARAMS
        if not enemy[i + ENEMY_ALIVE]: continue
        e_x = enemy[i + ENEMY_X]
        e_y = enemy[i + ENEMY_Y]
        sprite_pos = enemy[i + ENEMY_SPRITE]
        if sprite_pos != HUMANOID and sprite_pos != MINE and sprite_pos != BAITER2:
            total_enemy += 1
        if sprite_pos == HUMANOID:
            total_human += 1
        addr = ((e_y >> 3) * MAXSCREEN_X - MAXSCREEN_X) + (((e_x - screen_x + MINI_WRAP) & 2047) >> 4) + MINI_X
        if 0 < addr < MAXSCREEN_X * (HUD_H - 2):
            screen[addr] = mini_colors[sprite_pos]
            screen[addr + MAXSCREEN_X] = mini_colors[sprite_pos + 1]
        if not (screen_x < e_x < screen_x + MAXSCREEN_X - 8 or e_x < (MAXSCREEN_X - 8 + screen_x - 2048)):
            enemy[i + ENEMY_ONSCREEN] = 0
            if sprite_pos == MINE:
                enemy[i + ENEMY_ALIVE] = 0
            continue
        enemy[i + ENEMY_ONSCREEN] = 1
        e_x = (2048 + e_x - screen_x) & 2047
        animate = enemy[i + ENEMY_POS_ANI]
        offset = (sprite_pos + animate) * 8 * 8
        for y in range(8):
            for x in range(8):
                color = sprite[y * 8 + x + offset]
                addr = (e_y + y) * MAXSCREEN_X + e_x + x
                if color and 0 < addr < MAXSCREEN_Y * MAXSCREEN_X:
                    screen[addr] = color
        if sprite_pos == HUMANOID and enemy[i + ENEMY_STATE] == HUMANOID_CAUGHT:
            enemy[i + ENEMY_X] = (2048 + player[PLAYER_X] + player[MAP_X]) & 2047
            enemy[i + ENEMY_Y] = player[PLAYER_Y] + 8
            if player[PLAYER_EXP]:
                enemy[i + ENEMY_ALIVE] = 0      # kill human when player dies
    player[ENEMY_REMAIN] = total_enemy
    player[HUMAN_REMAIN] = total_human
    if total_enemy == 0:
        clear_missiles()
        add_score(total_human * 100 * player[WAVE])
        player[WAVE] += 1
        player[INTERMISSION] = 200
        get_wave_enemies(player[WAVE])


@micropython.viper
def draw_hyper():
    player = ptr32(PLAYER)
    if player[INTERMISSION] or player[LIVES] < 1: return
    frame = player[HYPER]
    if frame == 0: return
    if frame == 100:
        snd.play(SPAWN_SOUND, vol=100)    
    player[HYPER] = frame - 2
    screen = ptr16(fb2)
    sprite = ptr16(SPRITES)
    sprite_x = player[PLAYER_X]
    sprite_y = player[PLAYER_Y]
    sprite_offset = player[PLAYER_DIR] * 8 * 8 * 2
    for i in range(64):
        if sprite[i + sprite_offset] != 0:
            src_x = i % 8
            src_y = i // 8
            dx = -(src_x << SCALE)
            dy = (src_y << SCALE) - CENTER
            dest_x = src_x + (dx * frame) >> SCALE
            dest_y = src_y + (dy * frame) >> SCALE
            screen_x = sprite_x + int(dest_x)
            screen_y = sprite_y + int(dest_y)
            if 0 <= screen_x < MAXSCREEN_X and 0 <= screen_y < MAXSCREEN_Y:
                screen[screen_y * MAXSCREEN_X + screen_x] = sprite[i + sprite_offset]
        if sprite[i + sprite_offset + 64] != 0:
            src_x = i % 8
            src_y = i // 8
            dx = (src_x << SCALE)
            dy = (src_y << SCALE) - CENTER
            dest_x = src_x + (dx * frame) >> SCALE
            dest_y = src_y + (dy * frame) >> SCALE
            screen_x = sprite_x + 8 + int(dest_x)
            screen_y = sprite_y + int(dest_y)
            if 0 <= screen_x < MAXSCREEN_X and 0 <= screen_y < MAXSCREEN_Y:
                screen[screen_y * MAXSCREEN_X + screen_x] = sprite[i + sprite_offset + 64]


@micropython.viper
def draw_human_intermission():
    player = ptr32(PLAYER)
    if not player[INTERMISSION]: return
    screen = ptr16(fb2)
    sprite = ptr16(SPRITES)
    num_humans = player[HUMAN_REMAIN]
    if num_humans == 0: return
    sprite_width = 8
    spacing = 4
    total_width = (sprite_width + spacing) * num_humans - spacing
    start_x = (MAXSCREEN_X - total_width) // 2
    y_pos = 150
    offset = HUMANOID * 8 * 8
    for h in range(num_humans):
        x_pos = start_x + (sprite_width + spacing) * h
        for y in range(8):
            for x in range(8):
                color = sprite[y * 8 + x + offset]
                addr = (y_pos + y) * MAXSCREEN_X + x_pos + x
                if color and 0 < addr < MAXSCREEN_X * MAXSCREEN_Y:
                    screen[addr] = color


@micropython.viper
def draw_score500():
    player = ptr32(PLAYER)
    if player[INTERMISSION]: return
    screen = ptr16(fb2)
    sprite = ptr16(SPRITES)
    score500 = ptr32(SCORE500)
    score500_colors = ptr16(SCORE500_COLORS)
    offset_0 = SCORE0 * 64
    for ind in range(NUM_SCORE500):
        i = ind * SCORE500_PARAMS
        if score500[i + SCORE_ACTIVE]:
            score500[i + SCORE_ACTIVE] -= 1
            x_pos = (2048 + score500[i + SCORE_SX] - player[MAP_X]) & 2047
            y_pos = score500[i + SCORE_SY]
            if x_pos > MAXSCREEN_X - 16:
                score500[i + SCORE_ACTIVE] = 0
            sprite_pos = score500[i + SCORE_SPRITE]
            offset = sprite_pos * 64
            active = score500[i + SCORE_ACTIVE] >> 3
            color2 = 0
            for y in range(8):
                for x in range(8):
                    color = sprite[y * 8 + x + offset]
                    if color == KEY_YELLOW:
                        color2 = score500_colors[active % 3]
                    elif color == KEY_RED:
                        color2 = score500_colors[(active + 1) % 3]
                    addr = (y_pos + y) * MAXSCREEN_X + x_pos + x
                    if color and 0 < addr < MAXSCREEN_Y * MAXSCREEN_X:
                        screen[addr] = color2
                    color = sprite[y * 8 + x + offset_0]
                    if color == KEY_BLUE:
                        color2 = score500_colors[(active + 2) % 3]
                    addr = (y_pos + y) * MAXSCREEN_X + x_pos + x + 8
                    if color and 0 < addr < MAXSCREEN_Y * MAXSCREEN_X:
                        screen[addr] = color2


@micropython.viper
def launch_missile(start_x: int, start_y: int, target_x: int, target_y: int) -> int:
    missiles = ptr32(MISSILES)
    target_x += 4
    target_y += 4
    for ind in range(NUM_MISSILES):
        i = ind * MISSILE_PARAMS
        if not missiles[i + MISSILE_ACTIVE]:
            dx = (2048 + target_x - start_x) & 2047
            if dx > 1024:
                dx -= 2048
            dy = target_y - start_y
            length = int(calc_length(dx, dy))
            if length == 0:
                return 0
            speed = 8000
            missiles[i + MISSILE_VX] = (dx * speed) // length
            missiles[i + MISSILE_VY] = (dy * speed) // length
            missiles[i + MISSILE_X] = start_x << SCALE
            missiles[i + MISSILE_Y] = start_y << SCALE
            missiles[i + MISSILE_ACTIVE] = 1
            missiles[i + MISSILE_COLOR] = WHITE
            snd.play(LANDFIRE_SOUND, vol=220)
            return 1
    return 0


@micropython.viper
def clear_missiles():
    missiles = ptr32(MISSILES)
    for ind in range(NUM_MISSILES):
        missiles[ind * MISSILE_PARAMS + MISSILE_ACTIVE] = 0


@micropython.viper
def update_missiles():
    missiles = ptr32(MISSILES)
    player = ptr32(PLAYER)
    if player[INTERMISSION]: return
    screen = ptr16(fb2)
    fire = ptr32(FIRE)
    screen_x = player[MAP_X]
    sbomb_off = player[SBOMB_RDY]
    for ind in range(NUM_MISSILES):
        i = ind * MISSILE_PARAMS
        if not missiles[i + MISSILE_ACTIVE]:
            continue
        missiles[i + MISSILE_X] += missiles[i + MISSILE_VX]
        missiles[i + MISSILE_Y] += missiles[i + MISSILE_VY]
        x = (missiles[i + MISSILE_X] >> SCALE) - screen_x
        y = missiles[i + MISSILE_Y] >> SCALE
        if x < 0 or x >= MAXSCREEN_X - 1 or y < 0 or y >= MAXSCREEN_Y - 1 or not sbomb_off:
            missiles[i + MISSILE_ACTIVE] = 0
            continue
        for f_ind in range(NUM_FIRE):                   # fire hits missiles
            f_i = f_ind * FIRE_PARAMS
            if fire[f_i + FIRE_DIR] != 0:
                fire_x = fire[f_i + FIRE_X]
                fire_y = fire[f_i + FIRE_Y]
                if fire_x - 8 < x < fire_x + 8 and fire_y - 4 < y < fire_y + 4:
                    missiles[i + MISSILE_ACTIVE] = 0
                    continue
        color = missiles[i + MISSILE_COLOR]
        addr = y * MAXSCREEN_X + x
        if 0 < addr < MAXSCREEN_X * MAXSCREEN_Y:
            screen[addr] = color
            screen[addr + 1] = color
            screen[addr + MAXSCREEN_X] = color
            screen[addr + MAXSCREEN_X + 1] = color      # 2x2 missile
        p_x = player[PLAYER_X]
        p_y = player[PLAYER_Y]
        if not player[PLAYER_EXP] and not player[HYPER] and player[LIVES] > 0:
            if p_x < x < p_x + 16 and p_y < y < p_y + 8:
                init_ship_exp()
                missiles[i + MISSILE_ACTIVE] = 0


@micropython.viper
def find_nearest_humanoid(lander_x: int, enemy: ptr32) -> int:
    nearest_dist = 2048
    nearest_index = -1
    for ind in range(NUM_ENEMY):
        i = ind * ENEMY_PARAMS
        if enemy[i + ENEMY_SPRITE] == HUMANOID and enemy[i + ENEMY_ALIVE] and enemy[i + ENEMY_STATE] == -1:
            humanoid_x = enemy[i + ENEMY_X]
            dist = int(abs((2048 + humanoid_x - lander_x) & 2047))
            if dist < nearest_dist:
                nearest_dist = dist
                nearest_index = ind
    return nearest_index


@micropython.viper
def move_enemy():
    player = ptr32(PLAYER)
    if player[INTERMISSION]: return
    terrain = ptr8(TER)
    enemy = ptr32(ENEMY)
    player_x = (2048 + player[PLAYER_X] + player[MAP_X]) & 2047
    player_y = player[PLAYER_Y]
    wave_time = player[WAVE_TIME]
    missile_percent = 100 - (wave_time >> 2)
    if missile_percent < 50: missile_percent = 50
    for ind in range(NUM_ENEMY):
        i = ind * ENEMY_PARAMS
        if not enemy[i + ENEMY_ALIVE]: continue
        animate = enemy[i + ENEMY_POS_ANI] + 1
        max_anim = enemy[i + ENEMY_MAX_ANI]
        if animate >= max_anim: animate = 0
        enemy[i + ENEMY_POS_ANI] = animate
        sprite_pos = enemy[i + ENEMY_SPRITE]
        enemy_x = enemy[i + ENEMY_X]
        enemy_y = enemy[i + ENEMY_Y]
        if sprite_pos == HUMANOID:
            if enemy[i + ENEMY_STATE] == LANDER_ASCENDING:
                enemy[i + ENEMY_VX] = 0
            elif enemy[i + ENEMY_STATE] == HUMANOID_CAUGHT:
                if enemy_y > terrain[enemy_x] + TER_OFF - 8:        # put on ground
                    enemy[i + ENEMY_STATE] = -1
                    player[SCORE] += 500
                    init_score500(SCORE50, enemy_x, enemy_y)
                else:
                    continue
            elif enemy[i + ENEMY_STATE] == HUMANOID_FALLING:
                if -12 < player_x - enemy_x < 12 and -8 < player_y - enemy_y < 10:
                    enemy[i + ENEMY_STATE] = HUMANOID_CAUGHT
                    player[SCORE] += 500
                    init_score500(SCORE50, enemy_x, enemy_y)
                if enemy_y < terrain[enemy_x] + TER_OFF - 8:        # free fall
                    enemy[i + ENEMY_VY] = 1
                    enemy[i + ENEMY_VX] = 0
                else:
                    enemy[i + ENEMY_STATE] = -1                     # touch ground
                    player[SCORE] += 250
                    if enemy[i + ENEMY_ONSCREEN]:
                        init_score500(SCORE25, enemy_x, enemy_y)
            else:
                enemy[i + ENEMY_Y] = terrain[enemy_x] + TER_OFF - 8  # walking
                if int(randint(0, 100)) > 80:
                    enemy[i + ENEMY_VX] = int(randint(-1, 1))
        if sprite_pos == BAITER1:
            if int(randint(0, 100)) > 98:
                enemy[i + ENEMY_VX] = (int(randint(0, 1)) * 2 - 1) * 10
                enemy[i + ENEMY_VY] = (int(randint(0, 1)) * 2 - 1) * 3
        elif sprite_pos == LANDER:
            move_lander(i)
        elif sprite_pos == BOMBER:
            if enemy[i + ENEMY_ONSCREEN] and int(randint(0, 100)) > 90:
                init_mines(enemy_x, enemy_y)
            if int(randint(0, 100)) > 99:
                enemy[i + ENEMY_VX] = (int(randint(0, 1)) * 2 - 1) * 3
            if int(randint(0, 100)) > 50:
                enemy[i + ENEMY_VY] = (int(randint(0, 1)) * 2 - 1) * 1
        elif sprite_pos == MOTHER:
            if int(randint(0, 100)) > 99:
                enemy[i + ENEMY_VX] = (int(randint(0, 1)) * 2 - 1) * 3
            if int(randint(0, 100)) > 50:
                enemy[i + ENEMY_VY] = (int(randint(0, 1)) * 2 - 1) * 1
        elif sprite_pos == MUTANT or sprite_pos == POD:
            if enemy_x < player_x:
                enemy[i + ENEMY_VX] = int(randint(5, 10))
            if enemy_x > player_x:
                enemy[i + ENEMY_VX] = int(randint(-10, -5))
            if -5 < enemy_x - player_x < 5:
                enemy[i + ENEMY_VY] = (int(randint(0, 1)) * 2 - 1) * 2
        if sprite_pos == LANDER and enemy[i + ENEMY_ONSCREEN] and int(randint(0, 100)) > missile_percent:
            launch_missile(enemy_x, enemy_y, player_x, player_y)
        x = enemy[i + ENEMY_X] + enemy[i + ENEMY_VX]
        y = enemy[i + ENEMY_Y] + enemy[i + ENEMY_VY]
        if sprite_pos == BAITER2:                       # second half baiter
            x = enemy[i + ENEMY_X - ENEMY_PARAMS] + 8
            y = enemy[i + ENEMY_Y - ENEMY_PARAMS]
        enemy[i + ENEMY_X] = x & 2047
        if y < SKY_TOP: y = SKY_BOT
        if y > SKY_BOT: y = SKY_TOP
        enemy[i + ENEMY_Y] = y


@micropython.viper
def move_lander(i: int):
    enemy = ptr32(ENEMY)
    enemy_x = enemy[i + ENEMY_X]
    state = enemy[i + ENEMY_STATE]
    target_index = enemy[i + ENEMY_TARGET]
    if state == LANDER_HUNTING:
        if target_index == -1:                          # find nearest Humanoid
            target_index = int(find_nearest_humanoid(enemy_x, enemy))
            enemy[i + ENEMY_TARGET] = target_index
            if target_index >= 0:
                enemy[target_index * ENEMY_PARAMS + ENEMY_STATE] = LANDER_DESCENDING
        if target_index >= 0:
            target_x = enemy[target_index * ENEMY_PARAMS + ENEMY_X]
            dx = (2048 + target_x - enemy_x) & 2047
            enemy[i + ENEMY_VY] = (int(randint(0, 1)) * 2 - 1) * 1
            if dx < 1024:
                enemy[i + ENEMY_VX] = 4
            else:
                enemy[i + ENEMY_VX] = -4
            if int(abs(dx)) < 5:                        # nearly above, descend
                enemy[i + ENEMY_STATE] = LANDER_DESCENDING
                enemy[i + ENEMY_VX] = 0
                enemy[i + ENEMY_VY] = 1
    elif state == LANDER_DESCENDING:
        if target_index >= 0:
            target_y = enemy[target_index * ENEMY_PARAMS + ENEMY_Y]
            enemy[i + ENEMY_X] = enemy[target_index * ENEMY_PARAMS + ENEMY_X]
            if int(abs(enemy[i + ENEMY_Y] - target_y)) < 5:     # capture
                enemy[i + ENEMY_STATE] = LANDER_ASCENDING
                enemy[i + ENEMY_VY] = -1
                enemy[target_index * ENEMY_PARAMS + ENEMY_STATE] = LANDER_ASCENDING
                snd.play(HUMAN1_SOUND, vol=100)
    elif state == LANDER_ASCENDING:
        if target_index < 0:
            enemy[i + ENEMY_STATE] = LANDER_HUNTING
            return
        enemy[target_index * ENEMY_PARAMS + ENEMY_Y] = enemy[i + ENEMY_Y] + 9
        if enemy[target_index * ENEMY_PARAMS + ENEMY_ALIVE] == 0:   # human died
            enemy[i + ENEMY_VX] = (int(randint(0, 1)) * 2 - 1) * 2
            enemy[i + ENEMY_STATE] = LANDER_HUNTING
            enemy[i + ENEMY_TARGET] = -1
            return
        if enemy[i + ENEMY_Y] <= SKY_TOP:                           # become Mutant
            enemy[i + ENEMY_SPRITE] = MUTANT
            enemy[i + ENEMY_MAX_ANI] = 5
            enemy[i + ENEMY_POS_ANI] = 0
            enemy[i + ENEMY_STATE] = LANDER_HUNTING
            enemy[i + ENEMY_VX] = (int(randint(0, 1)) * 2 - 1) * 3
            enemy[i + ENEMY_VY] = 0
            enemy[target_index * ENEMY_PARAMS + ENEMY_ALIVE] = 0


@micropython.viper
def ship_explode():
    player = ptr32(PLAYER)
    if player[PLAYER_EXP] == 0: return
    explode = ptr32(SHIP_EXP)
    screen = ptr16(fb2)
    if player[PLAYER_EXP] == 1 and player[LIVES] > 0:
        player[HYPER] = 100
    player[PLAYER_EXP] -= 1
    for ind in range(NUM_EXP):
        i = ind * SHIP_EXP_PARAMS
        if not explode[i + EXP_ALIVE]: continue
        e_x = explode[i + EXP_X] + explode[i + EXP_VX]
        e_y = explode[i + EXP_Y] + explode[i + EXP_VY]
        explode[i + EXP_X] = e_x
        explode[i + EXP_Y] = e_y
        x = e_x >> SCALE
        y = e_y >> SCALE
        if x < 0 or y < 0 or x >= MAXSCREEN_X or y >= MAXSCREEN_Y:
            explode[i + EXP_ALIVE] = 0
            continue
        screen[y * MAXSCREEN_X + x] = WHITE


@micropython.viper
def enemy_explode():
    player = ptr32(PLAYER)
    screen = ptr16(fb2)
    exp = ptr32(ENEMY_EXP)
    screen_x = player[MAP_X]
    for ind in range(NUM_EN_EXP):
        i = ind * ENEMY_EXP_PARAMS
        if not exp[i + EN_EXP_ALIVE]: continue
        exp[i + EN_EXP_ALIVE] -= 1
        color = exp[i + EN_EXP_COLOR]
        e_x = exp[i + EN_EXP_X] + exp[i + EN_EXP_VX]
        e_y = exp[i + EN_EXP_Y] + exp[i + EN_EXP_VY]
        exp[i + EN_EXP_X] = e_x
        exp[i + EN_EXP_Y] = e_y
        x = (e_x >> SCALE) - screen_x
        y = e_y >> SCALE
        if x < 0 or y < 0 or x >= MAXSCREEN_X or y >= MAXSCREEN_Y:
            exp[i + EN_EXP_ALIVE] = 0
            continue
        screen[y * MAXSCREEN_X + x] = color


def init_terrain():
    global TER
    TER = bytearray(2050)
    y = 98
    x = 0
    for byte in range(256):
        data = TERRAIN[byte]
        for bit in range(8):
            if data & (1 << bit):
                y -= 1
            else:
                y += 1
            TER[x] = y
            x += 1


@micropython.viper
def draw_terrain(pos: int):
    player = ptr32(PLAYER)
    if player[INTERMISSION]: return
    terrain = ptr8(TER)
    screen = ptr16(fb2)
    for x in range(MAXSCREEN_X):                # full screen width, 1:1 scale
        x1 = (x + pos) & 2047
        y = terrain[x1] + TER_OFF
        addr = y * MAXSCREEN_X + x
        if 0 < addr < MAXSCREEN_X * MAXSCREEN_Y:
            screen[addr] = BROWN


@micropython.viper
def draw_mini(pos: int):
    terrain = ptr8(MINI_TERRAIN_DATA)
    screen = ptr16(fb2)
    pos = pos >> 5
    pos += MINI_POS
    for x in range(64):
        x1 = (x + pos) & 63
        y = terrain[x1 * 3] - 10
        x2 = terrain[x1 * 3 + 1]
        x3 = terrain[x1 * 3 + 2]
        addr = y * MAXSCREEN_X + (x * 2) + MINI_X
        if 0 < addr < MAXSCREEN_X * (HUD_H - 2):
            screen[addr + 1] = BROWN
            if x2 == 0x77:
                screen[addr + 0] = BROWN
            if x3 == 0x07:
                screen[addr - MAXSCREEN_X] = BROWN
            if x3 == 0x70:
                screen[addr + MAXSCREEN_X] = BROWN


# ---- render / cores ---------------------------------------------------------
R_COLOR = const(0)                          # rainbow score colour
R_RAIN  = const(1)                          # rainbow timer
R_START = const(2)                          # wave start ticks
RSTATE = array.array('i', [WHITE, 0, 0])


def render():
    player = PLAYER
    ticks = ticks_ms()
    if ticks - RSTATE[R_RAIN] > 150:
        RSTATE[R_RAIN] = ticks
        RSTATE[R_COLOR] = randint(16, 0xffff)
    score_color = RSTATE[R_COLOR]

    fill_asm(fb2, BLACK)
    SCREEN.rect(MINI_BOX_X, 0, 130, HUD_H, BLUE)
    SCREEN.line(0, HUD_H, MAXSCREEN_X - 1, HUD_H, BLUE)
    SCREEN.rect(MINI_VIEW_X - 1, 0, VIEW_W + 2, HUD_H, WHITE)
    SCREEN.rect(MINI_VIEW_X - 1, 2, VIEW_W + 2, HUD_H - 4, BLACK)
    draw_hud()
    draw_stars()
    draw_player()
    draw_enemy()
    draw_hyper()
    show_num_viper(player[SCORE], SCORE_X, SCORE_Y, score_color, SCORE_SIZE)
    show_num_viper(player[ENEMY_REMAIN], DBG_X, 2, BLUE, 1)
    show_num_viper(player[WAVE], DBG_X, 12, BLUE, 1)

    restart = INPUT[I_RESTART]
    INPUT[I_RESTART] = 0
    if player[LIVES] < 1:
        SCREEN.text('GAME OVER', 124, 110, score_color)
        if restart:
            start_game()
    if player[INTERMISSION]:
        RSTATE[R_START] = ticks
        player[INTERMISSION] -= 1
        SCREEN.text('ATTACK WAVE', 108, 80, LT_BLUE)
        show_num_viper(player[WAVE] - 1, 203, 80, LT_BLUE, 1)
        SCREEN.text('COMPLEATED', 108, 90, LT_BLUE)
        SCREEN.text('BONUS X', 113, 115, LT_BLUE)
        show_num_viper(player[HUMAN_REMAIN] * 100, 198, 115, LT_BLUE, 1)
        draw_human_intermission()

    draw_fire()
    map_x = player[MAP_X]
    draw_terrain(map_x)
    draw_mini(map_x)
    ship_explode()
    enemy_explode()
    update_missiles()
    draw_score500()
    draw_num.draw(FPS_CORE0, FPS_X, FPS_Y)
    draw_num.draw(FPS_CORE1, FPS_X, FPS_Y + 8)


@micropython.viper
def core0():
    player = ptr32(PLAYER)
    game   = ptr32(GAME)
    rstate = ptr32(RSTATE)
    twinkle_ticks = 0
    fire_ticks    = 0
    animate_ticks = 0
    rstate[R_START] = int(ticks_ms())
    snd.play(START_SOUND, vol=100)
    sleep_ms(4000)
    while not game[GAME_EXIT]:
        while game[G_RDY] and not game[GAME_EXIT]:
            pass                            # last frame not yet copied out
        ticks = int(ticks_ms())
        player[WAVE_TIME] = int(ticks_diff(ticks, rstate[R_START])) >> 10
        if ticks - twinkle_ticks > 100:
            twinkle_ticks = ticks
            twinkle_stars()
        if ticks - fire_ticks > 120:
            fire_ticks = ticks
            check_fire()
        if ticks - animate_ticks > 100:
            animate_ticks = ticks
            move_enemy()
        read_gamepad()
        move_player()
        render()
        draw_num.set(FPS_CORE0, ticks)
        draw_num.update_all()
        game[G_RDY] = 1                     # publish
    print('core0 done')


@micropython.viper
def core1():
    sleep_ms(500)
    game = ptr32(GAME)
    while not game[GAME_EXIT]:
        ticks = int(ticks_ms())
        display.wait_frame()
        if game[G_RDY]:                         # skip frame if core0 ran long
            copy_fb(fb2, fb)
            game[G_RDY] = 0
        draw_num.set(FPS_CORE1, ticks)
    print('core1 done')


def shutdown():
    GAME[GAME_EXIT] = 1
    sleep_ms(100)
    snd.deinit()
    display.deinit()
    machine.freq(150_000_000)
    print('shutdown...')
    sleep_ms(300)
    exit()


def main():
    global snd, SPRITES,FIRE_SOUND,SPAWN_SOUND,PLAYEREXP_SOUND,LANDDIE_SOUND
    global HUMAN1_SOUND,LANDFIRE_SOUND,START_SOUND,MAX_COLORS
    from audio_mixer2 import Mixer
    snd = Mixer()
    SPRITES = init_sprite("/Defender/defender9.bin")      # standard RGB565, 8x8
    FIRE_SOUND      = snd.load("/Defender/pfire3_2.wav")  
    SPAWN_SOUND     = snd.load("/Defender/landerspawn2.wav")  
    PLAYEREXP_SOUND = snd.load("/Defender/pexplode2.wav") 
    LANDDIE_SOUND   = snd.load("/Defender/landerdie2.wav") 
    HUMAN1_SOUND    = snd.load("/Defender/human2.wav") 
    LANDFIRE_SOUND  = snd.load("/Defender/landershoot2.wav")
    START_SOUND     = snd.load("/Defender/start2.wav")    
    fade(0, RED, COLORS)
    fade(RED, WHITE, COLORS)
    MAX_COLORS = len(COLORS)
    fade(0, WHITE, STAR_COLORS)
    gc.collect()
    print('free', gc.mem_free())
    init_game()
    init_terrain()
    init_stars()
    fill_asm(fb2, BLACK)
    fill_asm(fb, BLACK)
    _thread.start_new_thread(core1, ())
    try:
        core0()
        shutdown()
    except KeyboardInterrupt:
        shutdown()
        
if __name__ == '__main__':
    main()