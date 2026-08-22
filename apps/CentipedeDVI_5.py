# Centipede — DVI port (320×240 BGR233, RP2350 HSTX)
# Migrated from centipede.py (160×128 LCD RGB565)
# Tile step doubled to 16px; all entity coords ×2; sprite blits unchanged (8×8 / 16×16)

from dvi_rp2_hstx_frame_sync_v2 import DVI_RP2_HSTX
from gamepadfast import Gamepad
from draw_numberdvi import Draw_number
import colors as rv_colors
import gc, array, framebuf, _thread
from time import sleep_ms, ticks_ms, sleep
from random import randint
import machine, sys
from machine import Pin
from sys import exit
sys.path.append("/Centipede")
#from centi_sound import Centi_Sound


# ── Screen / field constants ──────────────────────────────────────────────────
SCREEN_W       = const(320)
SCREEN_H       = const(240)
SCALE          = const(13)           # Q13 fixed point — unchanged
TEXTURE_WIDTH  = const(16)
TEXTURE_HEIGHT = const(16)
NUM_TEXTURES   = const(68)
FIELD_WIDTH    = const(SCREEN_W // 16)   # 20  (was MAXSCREEN_X//8 = 20)
FIELD_HEIGHT   = const(SCREEN_H // 16)   # 15  (was 16; 15*16=240 fills screen)
TILE_STEP      = const(16)               # pixels per field tile (was 8)

FPS_CORE0 = const(0)
FPS_CORE1 = const(1)

# ── BGR233 palette ────────────────────────────────────────────────────────────
BLACK        = const(0x00)
WHITE        = const(0xFF)
BACKGROUND   = const(BLACK)

# ── Gamepad array layout (shared with DVI template) ──────────────────────────
GAMEPAD = array.array('i', [0, 0, 0])   # x, y, debounce
GAMEPAD_X         = const(0)
GAMEPAD_Y         = const(1)
GAMEPAD_DEBOUNCE  = const(2)
GAMEPAD_RIGHT     = const(0b0100000)
GAMEPAD_LEFT      = const(0b0000100)
GAMEPAD_UP        = const(0b1000000)
GAMEPAD_DOWN      = const(0b0000010)
GAMEPAD_SELECT    = const(0b0000001)

# ── Game entity param slot indices ───────────────────────────────────────────
PLAYER_PARAMS = const(10)
X             = const(0)
Y             = const(1)
FIRED         = const(2)
SPRITE_IND    = const(3)
MISS_X        = const(4)
MISS_Y        = const(5)
PLAYER_DEAD   = const(6)

GAME_PARAMS   = const(10)
FPS           = const(0)
LIVES         = const(1)
SIZE          = const(2)
NUM_MUSH      = const(3)
C_SEGMENTS    = const(4)
CENTI_DEAD    = const(5)
EXPLODE_DONE  = const(6)
SCORE         = const(7)

CENTI_PARAMS  = const(10)
# X, Y reuse slots 0,1
DIRECTION     = const(2)
# SPRITE_IND reuses slot 3
SPRITE_START  = const(4)
UP_DOWN       = const(5)
REACH_BOT     = const(6)

FLEA_PARAMS   = const(10)
# X, Y reuse slots 0,1
FLEA_SCORE    = const(2)
FLEA_SCORE_Y  = const(3)

SPID_PARAMS   = const(10)
# X, Y reuse slots 0,1
SPID_SCORE    = const(2)
SPID_SCORE_X  = const(3)
SPID_POINTS   = const(4)
SPID_XDIR     = const(5)
SPID_XINC     = const(6)
SPID_YINC     = const(7)
SPID_YDIST    = const(8)

EXPLODE_PARAMS = const(10)
# X, Y reuse slots 0,1
EXP_SPRITE    = const(2)
EXP_COUNT     = const(3)

# ── Centipede direction constants ─────────────────────────────────────────────
# 0=dead,1=right/down,2=left/down,3=down/right,4=down/left,
# 5=right/up,6=left/up,7=up/right,8=up/left
DEAD       = const(0)
RIGHT_DOWN = const(1)
LEFT_DOWN  = const(2)
DOWN_RIGHT = const(3)
DOWN_LEFT  = const(4)
RIGHT_UP   = const(5)
LEFT_UP    = const(6)
UP_RIGHT   = const(7)
UP_LEFT    = const(8)

# ── GAME control array ────────────────────────────────────────────────────────
GAME_CTL       = array.array('i', [0] * 10)
GAME_CTL_EXIT  = const(0)

# ── Hardware init ─────────────────────────────────────────────────────────────
machine.freq(246_000_000)
machine.mem32[0x40010058] = 2 << 16   # HSTX CLK / 2
machine.mem32[0x40010054] = 1 << 11   # HSTX CLK uses SYS CLK

fb  = bytearray(SCREEN_W * SCREEN_H)   # DVI display buffer (BGR233)
fb2 = bytearray(SCREEN_W * SCREEN_H)   # render buffer

display = DVI_RP2_HSTX()
display.begin(fb, rv_colors.COLOR_MODE_BGR233,
              height=SCREEN_H, width=SCREEN_W, bytes_per_pixel=1)

gamepad  = Gamepad()
SCREEN   = framebuf.FrameBuffer(fb2, SCREEN_W, SCREEN_H, framebuf.GS8)
draw_num = Draw_number(fb2, SCREEN_W, 1)


# ── Fast fill (asm_thumb, 192 bytes per iteration) ────────────────────────────
@micropython.asm_thumb
def fill_asm(r0, r1):   # (buffer_addr, 8-bit_color)
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


# ── Fast framebuffer copy (32 bytes / iter) ───────────────────────────────────
@micropython.asm_thumb
def copy_fb(r0, r1):                    # r0=source, r1=dest
    movwt(r2, 2400)                     # 76800 / 32 = 2400
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


# ── File loader ───────────────────────────────────────────────────────────────
def load_files():
    global SPLASH, TEXTURES
    with open('/Centipede/centipede_back.bin', "rb") as f:
        f.read(4)
        SPLASH = f.read()
    # TEXTURES are pre-converted BGR233 (1 byte/pixel, 16×16×68)
    with open('/Centipede/Centipede_4.bin', "rb") as f:
        f.read(4)
        TEXTURES = f.read()


# ── Input ─────────────────────────────────────────────────────────────────────
@micropython.viper
def read_gamepad():
    player  = ptr32(PLAYER)
    gamepad.read()
    buttons = int(gamepad.buttons)
    x_inc   = int(gamepad.x)
    y_inc   = int(gamepad.y)
    if not (buttons & GAMEPAD_SELECT):  # SELECT = quit
        shutdown()

    # Fire: GAMEPAD_RIGHT button
    if not (buttons & GAMEPAD_RIGHT) and not player[FIRED]:
        #CENTI_SOUND.SOUND = int(CENTI_SOUND.SOUND) | 1 << 1
        player[FIRED]  = 1
        player[MISS_X] = (player[X] >> SCALE) + 8
        player[MISS_Y] = (player[Y] >> SCALE) + 8

    # Clamp analogue deadband (same threshold, values from gamepad.x/y)
    x_dead = x_inc if x_inc >= 0 else (-1 * x_inc)
    y_dead = y_inc if y_inc >= 0 else (-1 * y_inc)
    if x_dead < 2: x_inc = 0
    if y_dead < 2: y_inc = 0
    
    # Q13 movement — coords are ×2 so effective range is (SCREEN_W)×(SCREEN_H)
    test_x = player[X] + (x_inc << 7)
    test_y = player[Y] + (y_inc << 7)
    if test_x < 0: test_x = 0
    if test_y < 0: test_y = 0
    if test_x > (SCREEN_W - 12) << SCALE: test_x = (SCREEN_W - 12) << SCALE
    if test_y > (SCREEN_H - 12) << SCALE: test_y = (SCREEN_H - 12) << SCALE

    # Field collision (tile = 16px so divide by 16)
    px = ((8 << SCALE) + test_x) >> 17      # >>17 = >>SCALE then //16 = >>4
    py = ((8 << SCALE) + test_y) >> 17
    field  = ptr8(FIELD)
    sprite = field[py * FIELD_WIDTH + px]
    if sprite == 99:
        player[X] = test_x
        player[Y] = test_y


# ── Initialisers ──────────────────────────────────────────────────────────────
def init_player():
    global PLAYER, CENTI, DIR_INC, START_IND
    # DIR_INC: all step values doubled (coords are ×2) to keep same visual speed
    DIR_INC  = array.array('i', (0, 0, 2, 0, -2, 0, 0, 2, 0, 2, 2, 0, -2, 0, 0, -2, 0, -2))
    START_IND = bytearray([0])
    segments  = GAME[C_SEGMENTS]
    PLAYER = array.array('i', (0 for _ in range(PLAYER_PARAMS)))
    CENTI  = array.array('i', (0 for _ in range(1 + segments * CENTI_PARAMS)))
    PLAYER[X] = 160 << SCALE   # was 80<<SCALE — proportional ×2
    PLAYER[Y] = 200 << SCALE   # was 100<<SCALE — proportional ×2


@micropython.viper
def init_centi():
    field = ptr8(FIELD)
    centi = ptr32(CENTI)
    game  = ptr32(GAME)
    # Clear top tile row (20 entries) for centi entry
    i = 0
    while i < FIELD_WIDTH:
        field[i] = 99
        i += 1
    index = 0
    segs  = game[C_SEGMENTS]
    while index < segs:
        i = index * CENTI_PARAMS
        centi[i + X]           = 200 - (8 * index)  # was 100-(8*index)
        centi[i + Y]           = 0
        centi[i + DIRECTION]   = 1
        centi[i + SPRITE_IND]  = index % 4
        centi[i + SPRITE_START] = 4
        centi[i + REACH_BOT]   = 0
        index += 1
    centi[0 + SPRITE_START] = 0   # head


def init_game():
    global GAME, TEXTURES_BA, FIELD, FLEA, SPIDER, EXPLODE, SPREAD_LUT
    GAME       = array.array('i', (0 for _ in range(GAME_PARAMS)))
    # TEXTURES stays as loaded bytes (ptr8); allocate a mutable bytearray copy
    TEXTURES_BA = bytearray(TEXTURE_WIDTH * TEXTURE_HEIGHT * NUM_TEXTURES)  # BGR233: 1 byte/pixel
    FIELD      = bytearray([99 for _ in range(FIELD_WIDTH * FIELD_HEIGHT)])
    FLEA       = array.array('i', (0 for _ in range(FLEA_PARAMS)))
    SPIDER     = array.array('i', (0 for _ in range(SPID_PARAMS)))
    EXPLODE    = array.array('i', (0 for _ in range(10 * EXPLODE_PARAMS)))
    # Quadratic ease-out spread LUT: 20 entries, indexed as spread_lut[19-count].
    # count=19 at start → index 0 (divisor=128, tight cluster).
    # count=1  at end   → index 18 (divisor=1,  full spread).
    # Particles start tight and accelerate outward, easing into full spread.
    #SPREAD_LUT = bytearray([128, 90, 64, 46, 34, 25, 19, 14, 11, 8, 6, 5, 4, 3, 3, 2, 2, 1, 1, 1])
    SPREAD_LUT = bytearray([128, 90, 64, 46, 34, 25, 19, 14, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1])
    SPIDER[SPID_XDIR] = 1
    GAME[FPS]         = 0
    GAME[LIVES]       = 3
    GAME[SIZE]        = 16
    GAME[NUM_MUSH]    = 20
    GAME[C_SEGMENTS]  = 10
    # Copy loaded TEXTURES bytes into mutable buffer
    tex_src = TEXTURES
    tex_dst = TEXTURES_BA
    num = TEXTURE_WIDTH * TEXTURE_HEIGHT * NUM_TEXTURES
    i = 0
    while i < num:
        tex_dst[i] = tex_src[i]
        i += 1


@micropython.viper
def init_field():
    field = ptr8(FIELD)
    game  = ptr32(GAME)
    mush_count = 0
    while mush_count < game[NUM_MUSH]:
        pos = int(randint(FIELD_WIDTH, FIELD_WIDTH * (FIELD_HEIGHT - 4)))
        if field[pos] == 99:
            field[pos] = 49   # mushroom sprite index
            mush_count += 1


@micropython.viper
def reset_field():
    field = ptr8(FIELD)
    game  = ptr32(GAME)
    if game[LIVES] == 0:
        #CENTI_SOUND.SOUND = 0
        return
    fx = 0
    while fx < FIELD_WIDTH:
        fy = 0
        while fy < FIELD_HEIGHT:
            cell = field[fy * FIELD_WIDTH + fx]
            if 49 < cell < 53:
                field[fy * FIELD_WIDTH + fx] = 49
                #CENTI_SOUND.SOUND = int(CENTI_SOUND.SOUND) | 1 << 1
                draw_field()
                sleep_ms(100)
                #CENTI_SOUND.SOUND = 0
                sleep_ms(100)
            fy += 1
        fx += 1


# ── Explosion helpers ─────────────────────────────────────────────────────────
@micropython.viper
def init_explode(x: int, y: int, sprite: int):
    explode_arr = ptr32(EXPLODE)
    game        = ptr32(GAME)
    index = 0
    while index < 10:
        i = index * EXPLODE_PARAMS
        if explode_arr[i + EXP_COUNT] == 0:
            explode_arr[i + X]          = x
            explode_arr[i + Y]          = y
            explode_arr[i + EXP_SPRITE] = sprite
            explode_arr[i + EXP_COUNT]  = 100
            game[EXPLODE_DONE]          = 0
            return
        index += 1


# ── Missile movement ──────────────────────────────────────────────────────────
@micropython.viper
def move_miss():
    player     = ptr32(PLAYER)
    field      = ptr8(FIELD)
    centi      = ptr32(CENTI)
    game       = ptr32(GAME)
    flea       = ptr32(FLEA)
    spider     = ptr32(SPIDER)

    if not player[FIRED]:
        #CENTI_SOUND.reset_fire()
        #CENTI_SOUND.SOUND = int(CENTI_SOUND.SOUND) & 0b1101
        return

    player[MISS_Y] -= 5
    if player[MISS_Y] < 0:
        player[FIRED] = 0
        return

    # Field tile under missile tip — tile step is 16px so >>4
    miss_x_tile = (player[MISS_X] - 1) >> 4
    miss_y_tile = player[MISS_Y] >> 4
    sprite      = field[miss_y_tile * FIELD_WIDTH + miss_x_tile]

    if sprite == 52:                        # last mushroom hit — destroy
        field[miss_y_tile * FIELD_WIDTH + miss_x_tile] = 99
        player[FIRED] = 0
        game[SCORE]  += 1
        init_explode(miss_x_tile * 16 + 4, (miss_y_tile * 16)-4, 49)
        return
    if 48 < sprite < 52:                    # mushroom takes a hit
        field[miss_y_tile * FIELD_WIDTH + miss_x_tile] = sprite + 1
        player[FIRED] = 0

    m_x = player[MISS_X]
    m_y = player[MISS_Y]

    # Flea collision
    f_x = flea[X]
    f_y = flea[Y]
    if flea[Y] > 0 and m_x < f_x + 8 and m_x > f_x and m_y < f_y + 8 and m_y > f_y:
        flea[FLEA_SCORE_Y] = flea[Y]
        flea[FLEA_SCORE]   = 50
        game[SCORE]       += 300
        init_explode(flea[X], flea[Y], 30)
        flea[Y] = 0

    # Spider collision
    s_x = spider[X]
    s_y = spider[Y]
    if 0 < s_y < (SCREEN_H - 16) and m_x < s_x + 16 and m_x > s_x and m_y < s_y + 8 and m_y > s_y:
        spider[SPID_SCORE]   = 30
        game[SCORE]         += 900
        init_explode(s_x, s_y, 24)
        spider[SPID_SCORE_X] = spider[X]
        spider[X]            = 0

    # Centipede collision
    segments = game[C_SEGMENTS]
    index = 0
    while index < segments:
        i = index * CENTI_PARAMS
        if centi[i + DIRECTION] != DEAD:
            c_x = centi[i + X]
            c_y = centi[i + Y]
            if m_x < c_x + 8 and m_x > c_x and m_y < c_y + 8 and m_y > c_y:
                ss = centi[i + SPRITE_START]
                if ss == 0 or ss == 8:
                    game[SCORE] += 100
                else:
                    game[SCORE] += 10
                if index < segments - 1 and centi[i + DIRECTION + CENTI_PARAMS] > 0:
                    centi[i + SPRITE_START + CENTI_PARAMS] = 0   # promote new head
                centi[i + DIRECTION] = DEAD
                player[FIRED]        = 0
                field[miss_y_tile * FIELD_WIDTH + miss_x_tile] = 49
                init_explode(centi[i + X], centi[i + Y], centi[i + SPRITE_START])
                return
        index += 1


# ── Centipede movement ────────────────────────────────────────────────────────
@micropython.viper
def move_centi():
    field    = ptr8(FIELD)
    centi    = ptr32(CENTI)
    game     = ptr32(GAME)
    dir_inc  = ptr32(DIR_INC)
    player   = ptr32(PLAYER)
    segments  = game[C_SEGMENTS]
    alive_segs = 0
    index = 0
    while index < segments:
        i = index * CENTI_PARAMS
        if centi[i + DIRECTION] == DEAD:
            index += 1
            continue
        alive_segs += 1
        direction = centi[i + DIRECTION]
        inc_x  = dir_inc[direction * 2]
        inc_y  = dir_inc[direction * 2 + 1]
        test_x = centi[i + X] + inc_x
        test_y = centi[i + Y] + inc_y
        f_x    = test_x >> 4   # //16 — tile lookup
        f_y    = test_y >> 4

        # Erase trap tile when moving through
        if field[(f_y + 1) * FIELD_WIDTH + f_x] < 99:
            if centi[i + UP_DOWN] == 1:
                field[(f_y + 1) * FIELD_WIDTH + f_x] = 99

        # Turning logic — UP_DOWN counts steps while moving down/up.
        # Each DOWN/UP step moves 2px; a tile is 16px → 8 steps to cross one tile.
        # Threshold must be > 7 (same as original), NOT > TILE_STEP-1 (=15 = 2 tiles).
        if (direction == DOWN_LEFT or direction == UP_LEFT) and centi[i + UP_DOWN] > 7:
            centi[i + UP_DOWN] = 0
            if test_y > SCREEN_H - TILE_STEP:
                direction              = LEFT_UP
                centi[i + REACH_BOT]  = 1
            if test_y < SCREEN_H - (TILE_STEP * 5) or centi[i + REACH_BOT] == 0:
                direction              = LEFT_DOWN
                centi[i + REACH_BOT]  = 0
        elif (direction == DOWN_RIGHT or direction == UP_RIGHT) and centi[i + UP_DOWN] > 7:
            centi[i + UP_DOWN] = 0
            if test_y > SCREEN_H - TILE_STEP:
                direction              = RIGHT_UP
                centi[i + REACH_BOT]  = 1
            if test_y < SCREEN_H - (TILE_STEP * 5) or centi[i + REACH_BOT] == 0:
                direction              = RIGHT_DOWN
                centi[i + REACH_BOT]  = 0
        elif (field[f_y * FIELD_WIDTH + f_x] < 99 or test_x > SCREEN_W - 8 or test_x < 0) \
                and direction != DOWN_RIGHT and direction != DOWN_LEFT \
                and direction != UP_RIGHT   and direction != UP_LEFT:
            if   direction == RIGHT_DOWN: direction = DOWN_LEFT
            elif direction == LEFT_DOWN:  direction = DOWN_RIGHT
            elif direction == RIGHT_UP:   direction = UP_LEFT
            elif direction == LEFT_UP:    direction = UP_RIGHT
        else:
            centi[i + X]         = test_x
            centi[i + Y]         = test_y
            centi[i + SPRITE_IND] += 1
            if centi[i + SPRITE_IND] == 4: centi[i + SPRITE_IND] = 0
            if (2 < direction < 5) or (6 < direction):
                centi[i + UP_DOWN] += 1
            # Flip sprite direction bit
            centi[i + SPRITE_START] ^= (((direction + 1) % 2) ^ ((centi[i + SPRITE_START] >> 3) & 1)) << 3

            # Check if centipede hit player
            m_x = (player[X] >> SCALE) + 4
            m_y = (player[Y] >> SCALE) + 12
            if m_x < test_x + 8 and m_x > test_x and m_y < test_y + 8 and m_y > test_y:
                init_explode(m_x-4, m_y-8, 67)
                player_dies()

        centi[i + DIRECTION] = direction
        index += 1

    if alive_segs == 0:
        init_centi()


# ── Flea movement ─────────────────────────────────────────────────────────────
@micropython.viper
def move_flea():
    centi  = ptr32(CENTI)
    field  = ptr8(FIELD)
    flea   = ptr32(FLEA)
    game   = ptr32(GAME)
    player = ptr32(PLAYER)

    if flea[FLEA_SCORE] > 0:
        flea[FLEA_SCORE] -= 1
        if not flea[FLEA_SCORE]:
            init_explode(flea[X], flea[FLEA_SCORE_Y], 36)

    # Spawn new flea
    if flea[Y] == 0 and int(randint(0, 300)) == 300 and flea[FLEA_SCORE] == 0:
        #CENTI_SOUND.SOUND = int(CENTI_SOUND.SOUND) | 1 << 2
        flea[X] = 16 * int(randint(0, FIELD_WIDTH))   # was 8*randint(0,20)
        flea[Y] = 16                                   # was 8

    if flea[Y] == 0:
        #CENTI_SOUND.reset_flea()
        #CENTI_SOUND.SOUND = int(CENTI_SOUND.SOUND) & 0b1011
        return

    flea[Y] += 2          # 2px/tick — proportional to doubled coords (was 1)
    if flea[Y] > SCREEN_H:
        flea[Y] = 0
        return

    f_x = flea[X] >> 4    # tile col  (was //8)
    f_y = flea[Y] >> 4    # tile row  (was //8)

    if int(randint(0, 30)) == 30:   # chance to plant mushroom
        index = 0
        segs  = game[C_SEGMENTS]
        while index < segs:
            i   = index * CENTI_PARAMS
            if centi[i + DIRECTION] != DEAD:
                c_x = centi[i + X] >> 4
                c_y = centi[i + Y] >> 4
                if f_x == c_x and f_y == c_y:
                    return
            index += 1
        field[f_y * FIELD_WIDTH + f_x] = 49

    # Flea–player collision
    m_x = (player[X] >> SCALE) + 8
    m_y = (player[Y] >> SCALE) + 4
    fl_x = flea[X]
    fl_y = flea[Y]
    if fl_y > 0 and m_x < fl_x + 8 and m_x > fl_x and m_y < fl_y + 8 and m_y > fl_y:
        init_explode(m_x, m_y, 67)
        player_dies()


# ── Spider movement ───────────────────────────────────────────────────────────
@micropython.viper
def move_spider():
    spider = ptr32(SPIDER)
    field  = ptr8(FIELD)
    player = ptr32(PLAYER)
    game   = ptr32(GAME)

    if spider[SPID_SCORE] > 0:
        spider[SPID_SCORE] -= 1
        if not spider[SPID_SCORE]:
            init_explode(spider[SPID_SCORE_X], spider[Y], 34)

    # Spawn new spider — X range doubles: 0..304 (was 0..144)
    spx = spider[X]
    if (spx == 0 or spx > (SCREEN_W - 16)) and int(randint(0, 200)) == 200 and spider[SPID_SCORE] == 0:
        #CENTI_SOUND.SOUND = int(CENTI_SOUND.SOUND) | 1 << 3
        spider[X]         = (SCREEN_W - 16) if spider[SPID_XDIR] == -1 else 1   # was 144 or 1
        spider[Y]         = int(randint(120, 224))   # was randint(60,112), scaled ×2
        spider[SPID_YDIST] = int(randint(10, 40))
        spider[SPID_XINC]  = spider[SPID_XDIR] if int(randint(0, 1)) else 0
        spider[SPID_YINC]  = -1 if int(randint(0, 1)) else 1

    if spider[X] < 1 or spider[X] > (SCREEN_W - 16):
        #CENTI_SOUND.SOUND = int(CENTI_SOUND.SOUND) & 0b0111
        return

    if spider[SPID_YDIST] > 0:
        spider[SPID_YDIST] -= 1
        spider[X] += spider[SPID_XINC]
        spider[Y] += spider[SPID_YINC]
        s_x = spider[X] >> 4    # tile col  (was //8)
        s_y = spider[Y] >> 4
        field[s_y * FIELD_WIDTH + s_x]     = 99
        field[s_y * FIELD_WIDTH + s_x + 1] = 99
        if spider[Y] < 120 or spider[Y] > 224: spider[SPID_YINC] *= -1   # was 60/112
        if spider[X] == 0 or spider[X] > (SCREEN_W - 16): spider[SPID_XDIR] *= -1
    else:
        spider[SPID_YDIST] = int(randint(10, 40))
        spider[SPID_XINC]  = spider[SPID_XDIR] if int(randint(0, 1)) else 0
        spider[SPID_YINC]  = -1 if int(randint(0, 1)) else 1

    # Spider–player collision
    m_x = (player[X] >> SCALE) + 4
    m_y = (player[Y] >> SCALE) + 12
    s_x = spider[X]
    s_y = spider[Y]
    if 0 < s_x < (SCREEN_W - 15) and m_x < s_x + 14 and m_x > s_x and m_y < s_y + 14 and m_y > s_y:
        init_explode(m_x-4, m_y-8, 67)
        player_dies()


# ── Player death ──────────────────────────────────────────────────────────────
@micropython.viper
def player_dies():
    game   = ptr32(GAME)
    player = ptr32(PLAYER)
    spider = ptr32(SPIDER)
    flea   = ptr32(FLEA)
    game[LIVES]        -= 1
    player[PLAYER_DEAD] = 100
    player[X]           = 160 << SCALE   # was 80<<SCALE
    player[Y]           = 200 << SCALE   # was 100<<SCALE
    flea[Y]             = 0
    spider[X]           = 0
    #CENTI_SOUND.SOUND   = 1

# ── Explosion draw ────────────────────────────────────────────────────────────
# Option 4: quadratic ease-out via SPREAD_LUT — particles accelerate smoothly
#            outward rather than the original hyperbolic (fast-then-plateau) curve.
# Option 1: trail rendering — each particle draws 3 steps along its velocity
#            vector (full, half-dim, quarter-dim) for denser, smoother look.
@micropython.viper
def explode():
    screen      = ptr8(fb2)
    textures    = ptr8(TEXTURES_BA)
    explode_arr = ptr32(EXPLODE)
    spread_lut  = ptr8(SPREAD_LUT)
    game        = ptr32(GAME)
    fb_size     = SCREEN_W * SCREEN_H
    total_alive = 0
    index = 0
    while index < 10:
        i = index * EXPLODE_PARAMS
        if explode_arr[i + EXP_COUNT] == 0:
            index += 1
            continue
        total_alive += 1
        explode_arr[i + EXP_COUNT] -= 1
        count = explode_arr[i + EXP_COUNT] // 5    # 0..19 (19=just started, 0=done)
        if count == 0:
            index += 1
            continue
        x1     = explode_arr[i + X]
        y1     = explode_arr[i + Y]
        sprite = explode_arr[i + EXP_SPRITE] << 8
        # count=19 at start, count=1 at end. Invert so early=tight, late=wide.
        divisor = spread_lut[19 - count]
        t_y = 0
        while t_y < TEXTURE_HEIGHT:
            t_x = 0
            while t_x < TEXTURE_WIDTH:
                color = textures[t_y * 16 + t_x + sprite]
                if color:
                    # Displacement at the particle's current frame position
                    x_off = (t_x - 8) * 32 // divisor
                    y_off = (t_y - 8) * 32 // divisor
                    # Option 1: trail — previous divisor gives a slightly smaller
                    # displacement (particle was closer to centre one step ago).
                    # Use divisor+4 as a cheap approximation of "last frame" position.
                    prev_div = divisor + 4
                    x_off_prev = (t_x - 8) * 32 // prev_div
                    y_off_prev = (t_y - 8) * 32 // prev_div

                    # Step 2 (mid-trail): midpoint between prev and current position
                    mid_x = (x_off + x_off_prev) >> 1
                    mid_y = (y_off + y_off_prev) >> 1

                    # Dim colour: half-brightness (rough BGR233 attenuation)
                    dim   = (color >> 1) & 0x6D   # mask keeps valid BGR233 bits
                    dimmer = (dim   >> 1) & 0x6D

                    # Trail step 1 — oldest (quarter brightness, prev position)
                    addr = (y1 + t_y + y_off_prev) * SCREEN_W + x1 + t_x + x_off_prev
                    if 0 < addr < fb_size:
                        screen[addr] = dimmer

                    # Trail step 2 — mid (half brightness, midpoint)
                    addr = (y1 + t_y + mid_y) * SCREEN_W + x1 + t_x + mid_x
                    if 0 < addr < fb_size:
                        screen[addr] = dim

                    # Trail step 3 — head (full brightness, current position)
                    addr = (y1 + t_y + y_off) * SCREEN_W + x1 + t_x + x_off
                    if 0 < addr < fb_size:
                        screen[addr] = color
                t_x += 1
            t_y += 1
        index += 1
    if total_alive == 0:
        game[EXPLODE_DONE] = 1



# ── Field draw ────────────────────────────────────────────────────────────────
@micropython.viper
def draw_field():
    textures   = ptr8(TEXTURES_BA)
    screen     = ptr8(fb2)
    field      = ptr8(FIELD)
    f_y = 0
    while f_y < FIELD_HEIGHT:
        f_x = 0
        while f_x < FIELD_WIDTH:
            sprite = field[f_y * FIELD_WIDTH + f_x]
            if sprite < 99:
                field_pos     = f_y * (TILE_STEP * SCREEN_W) + (f_x * TILE_STEP) + 8
                sprite_offset = (sprite << 8)+(4*16) + 4 
                t_y = 0
                while t_y < TEXTURE_HEIGHT//2:  
                    t_x = 0
                    while t_x < TEXTURE_WIDTH//2:   
                        screen[t_y * SCREEN_W + t_x + field_pos] = textures[t_y * 16 + t_x + sprite_offset]
                        t_x += 1
                    t_y += 1
            f_x += 1
        f_y += 1


# ── Score popup draw ──────────────────────────────────────────────────────────
@micropython.viper
def draw_score(x: int, y: int, sprite: int, offset: int):
    textures = ptr8(TEXTURES_BA)
    screen   = ptr8(fb2)
    t_y = 0
    while t_y < TEXTURE_HEIGHT // 2:
        t_x = 0
        while t_x < TEXTURE_WIDTH:
            screen[(y + t_y) * SCREEN_W + x + t_x] = textures[(4 + t_y) * 16 + t_x + offset + (sprite << 8)]
            t_x += 1
        t_y += 1


# ── Main draw ─────────────────────────────────────────────────────────────────
@micropython.viper
def draw():
    display.wait_frame()
    fill_asm(fb2, int(BACKGROUND))

    game     = ptr32(GAME)
    player   = ptr32(PLAYER)
    textures = ptr8(TEXTURES_BA)
    screen   = ptr8(fb2)
    centi    = ptr32(CENTI)
    flea     = ptr32(FLEA)
    spider   = ptr32(SPIDER)

    p_x = player[X] >> SCALE
    p_y = player[Y] >> SCALE

    draw_field()

    # Missile — draw a 2-pixel-wide vertical line for visibility on larger screen
    if player[FIRED]:
        m_x = player[MISS_X]
        m_y = player[MISS_Y]
        i = 0
        while i < 6:
            addr = (m_y + i) * SCREEN_W + m_x
            if 0 < addr < SCREEN_W * SCREEN_H:
                screen[addr-1]     = int(WHITE)
                #screen[addr + 1] = int(WHITE)
            i += 1

    # Player sprite (full 16×16)
    if not player[PLAYER_DEAD]:
        sprite = 67
        t_y = 0
        while t_y < TEXTURE_HEIGHT:
            t_x = 0
            while t_x < TEXTURE_WIDTH:
                color = textures[t_y * 16 + t_x + (sprite << 8)]
                if color:
                    screen[(p_y + t_y) * SCREEN_W + p_x + t_x] = color
                t_x += 1
            t_y += 1

    # Centipede segments
    segments = game[C_SEGMENTS]
    index = 0
    while index < segments:
        i = index * CENTI_PARAMS
        if centi[i + DIRECTION] != DEAD:
            c_x    = centi[i + X] - 0
            c_y    = centi[i + Y]
            sprite = centi[i + SPRITE_IND] + centi[i + SPRITE_START]
            t_y = 0
            while t_y < TEXTURE_HEIGHT//2:
                t_x = 0
                while t_x < TEXTURE_WIDTH//2:
                    screen[(c_y + t_y) * SCREEN_W + c_x + t_x] = textures[(4+t_y) * 16 + 4+t_x + (sprite << 8)]
                    t_x += 1
                t_y += 1
        index += 1

    # Flea (8×8)
    if flea[Y] > 0:
        f_x    = flea[X] + 8
        f_y    = flea[Y]
        sprite = 30 + (f_y % 4)
        t_y = 0
        while t_y < TEXTURE_HEIGHT // 2:
            t_x = 0
            while t_x < TEXTURE_WIDTH // 2:
                screen[(f_y + t_y) * SCREEN_W + f_x + t_x] = textures[(4 + t_y) * 16 + t_x + 4 + (sprite << 8)]
                t_x += 1
            t_y += 1

    # Score popups
    if flea[FLEA_SCORE] > 0:
        draw_score(flea[X], flea[FLEA_SCORE_Y], 36, 2)
    if spider[SPID_SCORE] > 0:
        draw_score(spider[SPID_SCORE_X], spider[Y], 34, 2)

    # Spider (full 16×16, transparent)
    spx = spider[X]
    if 0 < spx < (SCREEN_W - 15):
        s_x    = spx
        s_y    = spider[Y]
        sprite = 24 + (s_y % 6)
        t_y = 0
        while t_y < TEXTURE_HEIGHT // 2:
            t_x = 0
            while t_x < TEXTURE_WIDTH:
                color = textures[(4 + t_y) * 16 + t_x + 0 + (sprite << 8)]
                if color:
                    screen[(s_y + t_y) * SCREEN_W + s_x + t_x] = color
                t_x += 1
            t_y += 1

    # HUD — use draw_num for FPS, score, lives
    draw_num.draw_viper8(game[SCORE], 50, 10, int(WHITE), 1)
    draw_num.draw_viper8(game[LIVES], 105, 10, int(WHITE), 1)
    #draw_num.draw(FPS_CORE0, 290, 0)
    #draw_num.draw(FPS_CORE1, 290, 10)

    # Game over overlay
    if game[LIVES] == 0:
        SCREEN.rect(130,104,50,50,0,1)
        SCREEN.text('GAME',130,104,0xff)
        SCREEN.text('OVER',130,115,0xff)


# ── Core 0: game logic + render ───────────────────────────────────────────────
@micropython.viper
def _core0_loop():
    game_ctl          = ptr32(GAME_CTL)
    pot_ticks         = 0
    move_centi_ticks  = 0
    move_flea_ticks   = 0
    move_spider_ticks = 0
    game   = ptr32(GAME)
    player = ptr32(PLAYER)

    while not game_ctl[GAME_CTL_EXIT]:
        ticks = int(ticks_ms())

        if ticks - pot_ticks > 20:
            pot_ticks = ticks
            read_gamepad()

        if not player[PLAYER_DEAD]:
            move_miss()
            if ticks - move_centi_ticks > 10:
                move_centi_ticks = ticks
                move_centi()
            if ticks - move_flea_ticks > 10:
                move_flea_ticks = ticks
                move_flea()
            if ticks - move_spider_ticks > 20:
                move_spider_ticks = ticks
                move_spider()
        else:
            player[PLAYER_DEAD] -= 1
            if game[LIVES] == 0:
                player[PLAYER_DEAD] = 1
                game_over()
            if player[PLAYER_DEAD] == 0:
                reset_field()
                init_centi()

        explode()
        draw_num.update_all()
        draw()
        draw_num.set(FPS_CORE0, ticks)

    game_ctl[GAME_CTL_EXIT] = 1


def core0():
    sleep_ms(200)
    gc.collect()
    #CENTI_SOUND.SOUND = 1    # set before entering Viper loop
    _core0_loop()
    print('core0 done')


# ── Core 1: display copy ──────────────────────────────────────────────────────
@micropython.viper
def core1():
    sleep_ms(500)
    game_ctl = ptr32(GAME_CTL)
    while not game_ctl[GAME_CTL_EXIT]:
        copy_fb(fb2, fb)
        draw_num.set(FPS_CORE1, int(ticks_ms()))
    print('core1 done')


# ── Game over / shutdown ──────────────────────────────────────────────────────
def game_over():
    #CENTI_SOUND.SOUND = 0
    #CENTI_SOUND.OFF   = 1
    pass

 
def shutdown():
    GAME_CTL[GAME_CTL_EXIT] = 1
    sleep_ms(100)
    #snd.deinit()
    display.deinit()
    machine.freq(150_000_000)
    print('shutdown...')
    sleep_ms(300)
    exit()


def main():
    load_files()
    init_game()
    init_player()
    init_field()
    init_centi()
    # Core 1 handles display copy
    _thread.start_new_thread(core1, ())
    # Sound init (Centi_Sound manages its own PWM/timer; runs on core 0)
    #CENTI_SOUND = Centi_Sound()
    #CENTI_SOUND.OFF = 0
    sleep_ms(500)
    core0()    

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    main()
