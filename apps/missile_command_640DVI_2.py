# Missile Command — DVI port (640×480 RGB332, RP2350 HSTX)
# Ported from missile_command.py using the Star Castle DVI/gamepad structure.
from dvi_rp2_hstx_frame_sync_v2 import DVI_RP2_HSTX
import colors as rv_colors
from gamepadfast import Gamepad

import machine, framebuf, gc, _thread, array, micropython
from machine import Pin
from uctypes import addressof
from random import randint
from sys import exit
from time import sleep, ticks_ms, ticks_diff, ticks_us, sleep_ms
from micropython import const


# ── Screen / fixed point ─────────────────────────────────────────────────────
MAXSCREEN_X  = const(640)
MAXSCREEN_Y  = const(480)
SCREEN_W     = const(640)
SCREEN_H     = const(480)
SCALE        = const(10)

GROUND_Y     = const(456)
CITY_TOP     = const(431)      # 24x18 cities sit on the same baseline as before
TARGET_Y     = const(425) 
HUD_Y        = const(100)


# ── RGB332 palette: rrr_ggg_bb ───────────────────────────────────────────────
BLACK       = const(0x00)
WHITE       = const(0b111_111_11)
BLUE        = const(0b000_000_11)
SKYBLUE     = const(0b100_100_11)
GREEN       = const(0b000_111_00)
YELLOW      = const(0b111_111_00)
ORANGE      = const(0b111_100_00)
RED         = const(0b111_000_00)
DIMRED      = const(0b011_000_00)
BACKGROUND  = const(BLACK)

# Explosion palette, indexed by radius band.
EXP_COLORS = array.array('B', (YELLOW, ORANGE, RED, WHITE))


# ── Gamepad array layout, same button meanings as Star Castle ────────────────
GAMEPAD = array.array('i', [0, 0, 0])   # x, y, fire-ready latch
GAMEPAD_X         = const(0)
GAMEPAD_Y         = const(1)
GAMEPAD_DELAY     = const(2)
GAMEPAD_RIGHT     = const(0b0100000)    # fire, active low
GAMEPAD_LEFT      = const(0b0000100)
GAMEPAD_UP        = const(0b1000000)
GAMEPAD_DOWN      = const(0b0000010)    # restart, active low
GAMEPAD_SELECT    = const(0b0000001)    # quit, active low


# ── Game state arrays ────────────────────────────────────────────────────────
GAME_PARAMS       = const(10)
GAME_SCORE        = const(0)
GAME_CITIES       = const(1)
GAME_WAVE         = const(2)
GAME_ENEMY_LEFT   = const(3)
GAME_OVER         = const(4)
GAME_FPS          = const(5)
GAME_FIRST_RUN    = const(6)
INTERMISSION      = const(7)

GAME_CTL          = array.array('i', [0] * 4)
GAME_CTL_EXIT     = const(0)


# ── Object layouts ───────────────────────────────────────────────────────────
PLAYER_PARAMS = const(4)
PX            = const(0)
PY            = const(1)
PBLINK        = const(2)

NUM_ENEMY     = const(10)
NUM_ASSET     = const(10)
NUM_EXPLODE   = const(20)

TRAIL_POINTS  = const(768)
TRAIL_STRIDE  = const(TRAIL_POINTS * 2)

ENEMY_PARAMS  = const(8)
EX            = const(0)
EY            = const(1)
ELIFE         = const(2)     # 0 = inactive, 1 = active
EPOS          = const(3)     # current trail point index
ELEN          = const(4)     # last valid trail point index
ETX           = const(5)
ETY           = const(6)
ETARGET       = const(7)

ASSET_PARAMS  = const(8)
AX            = const(0)
AY            = const(1)
ALIFE         = const(2)     # 0 = inactive, 1 = active
APOS          = const(3)
ALEN          = const(4)
ACROSS_X      = const(5)
ACROSS_Y      = const(6)
ALAUNCH       = const(7)

EXP_PARAMS    = const(5)
EXP_X         = const(0)
EXP_Y         = const(1)
EXP_RADIUS    = const(2)
EXP_DIR       = const(3)
EXP_ACTIVE    = const(4)

EXP_MAX_RADIUS = const(52)
EXP_STEP       = const(3)
PLAYER_STEP    = const(5)     # trail points per player-missile update

# Movement timing.  Player and enemy missile update intervals are doubled
# from the first DVI port to make all missiles about half speed.
ASSET_MOVE_MS   = const(10)    # was 5
ENEMY_MOVE_MS   = const(60)    # was 30
EXPLODE_MOVE_MS = const(50)    # was 25; slower explosion growth/decay

# Between-wave intermission.  GAME[INTERMISSION] counts down from
# INTERMISSION_START to 0, decrementing by one every INTERMISSION_TICK_MS.
# The next wave is held off until it reaches 0; the stats HUD in draw()
# is shown for as long as GAME[INTERMISSION] > 0.
INTERMISSION_START   = const(300)
INTERMISSION_TICK_MS = const(20)


# Target slots match the original 9-position layout:
# 0,4,8 are launch bases. 1,2,3,5,6,7 are cities.
BASE0 = const(0)
BASE1 = const(4)
BASE2 = const(8)
TARGET_X = array.array('H', (48, 126, 198, 270, 320, 370, 442, 514, 592))
TARGET_KIND = array.array('B', (0, 1, 1, 1, 0, 1, 1, 1, 0))  # 0=base, 1=city

# 8×9 city silhouette downsampled from the original 16×9 data.
# Drawn at 3×2 pixels per source cell, this makes each city 24×18 pixels:
# half the previous 48×36 size without changing the target/base spacing.
CITY_W_BITS = const(8)
CITY_H_BITS = const(9)
CITY_X_SCALE = const(3)
CITY_Y_SCALE = const(2)
CITY_W = const(CITY_W_BITS * CITY_X_SCALE)
CITY_H = const(CITY_H_BITS * CITY_Y_SCALE)

# Values are palette selectors: 0=transparent, 1=green, 2=yellow.
_city_rows = (
    "00100000",
    "00100100",
    "01111100",
    "11111110",
    "11211210",
    "12212221",
    "22222221",
    "22222222",
    "22222222",
)
CITY_BITMAP = array.array('B', (0 if c == '0' else (1 if c == '1' else 2)
                                for row in _city_rows for c in row))


# ── Hardware init, same structure as Star Castle ─────────────────────────────
machine.freq(246_000_000)
machine.mem32[0x40010058] = 2 << 16   # HSTX CLK / 2
machine.mem32[0x40010054] = 1 << 11   # HSTX CLK uses SYS CLK

fb = bytearray(SCREEN_W * SCREEN_H)
gamepad = Gamepad()
SCREEN = framebuf.FrameBuffer(fb, SCREEN_W, SCREEN_H, framebuf.GS8)


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


# ── Initialization ───────────────────────────────────────────────────────────
def init_arrays():
    global GAME, PLAYER, CITY, ENEMY, ASSET, EXPLODE, TRAIL, TRAIL2
    GAME    = array.array('i', [0] * GAME_PARAMS)
    PLAYER  = array.array('i', [MAXSCREEN_X // 2, MAXSCREEN_Y // 2, 0, 0])
    CITY    = array.array('B', [1] * 9)
    ENEMY   = array.array('i', [0] * (NUM_ENEMY * ENEMY_PARAMS))
    ASSET   = array.array('i', [0] * (NUM_ASSET * ASSET_PARAMS))
    EXPLODE = array.array('i', [0] * (NUM_EXPLODE * EXP_PARAMS))
    TRAIL   = array.array('H', [0] * (NUM_ASSET * TRAIL_STRIDE))
    TRAIL2  = array.array('H', [0] * (NUM_ENEMY * TRAIL_STRIDE))
    GAMEPAD[GAMEPAD_DELAY] = 1


def init_trail(t, x1, y1, x2, y2, index):
    """Fill an H-array trail with x,y point pairs and return the last point index."""
    dx = x2 - x1
    dy = y2 - y1
    length = max(abs(dx), abs(dy))
    if length < 1:
        base = index * TRAIL_STRIDE
        t[base] = x1
        t[base + 1] = y1
        return 0
    if length >= TRAIL_POINTS:
        length = TRAIL_POINTS - 1

    base = index * TRAIL_STRIDE
    for step in range(length + 1):
        p = base + (step * 2)
        t[p]     = x1 + ((dx * step) // length)
        t[p + 1] = y1 + ((dy * step) // length)
    return length


def reset_assets():
    for i in range(NUM_ASSET):
        ASSET[i * ASSET_PARAMS + ALIFE] = 0
    for i in range(NUM_ENEMY):
        ENEMY[i * ENEMY_PARAMS + ELIFE] = 0
    for i in range(NUM_EXPLODE):
        EXPLODE[i * EXP_PARAMS + EXP_ACTIVE] = 0


def init_enemy():
    live_targets = [i for i in range(9) if CITY[i]]
    if not live_targets:
        GAME[GAME_OVER] = 1
        return

    GAME[GAME_WAVE] += 1
    GAME[GAME_ENEMY_LEFT] = NUM_ENEMY

    for n in range(NUM_ENEMY):
        i = n * ENEMY_PARAMS
        target = live_targets[randint(0, len(live_targets) - 1)]
        x1 = randint(20, MAXSCREEN_X - 20)
        y1 = randint(0, 24)
        x2 = TARGET_X[target]
        y2 = TARGET_Y

        ENEMY[i + EX] = x1
        ENEMY[i + EY] = y1
        ENEMY[i + ELIFE] = 1
        ENEMY[i + EPOS] = 0
        ENEMY[i + ELEN] = init_trail(TRAIL2, x1, y1, x2, y2, n)
        ENEMY[i + ETX] = x2
        ENEMY[i + ETY] = y2
        ENEMY[i + ETARGET] = target


def restart():
    GAME[GAME_SCORE] = 0
    GAME[GAME_CITIES] = 6
    GAME[GAME_WAVE] = 0
    GAME[GAME_ENEMY_LEFT] = 0
    GAME[GAME_OVER] = 0
    GAME[GAME_FIRST_RUN] = 0
    GAME[INTERMISSION] = 0
    PLAYER[PX] = MAXSCREEN_X // 2
    PLAYER[PY] = MAXSCREEN_Y // 2
    PLAYER[PBLINK] = 0
    for i in range(9):
        CITY[i] = 1
    reset_assets()
    init_enemy()


# ── Input / firing ───────────────────────────────────────────────────────────
def read_gamepad():
    gamepad.read()
    buttons = int(gamepad.buttons)

    if not (buttons & GAMEPAD_DOWN):
        restart()
        return

    if not (buttons & GAMEPAD_SELECT):
        GAME_CTL[GAME_CTL_EXIT] = 1
        return

    x_inc = int(gamepad.x) >> 6
    y_inc = int(gamepad.y) >> 6

    x_abs = x_inc if x_inc >= 0 else -x_inc
    y_abs = y_inc if y_inc >= 0 else -y_inc
    if x_abs < 2:
        x_inc = 0
    if y_abs < 2:
        y_inc = 0

    GAMEPAD[GAMEPAD_X] = x_inc
    GAMEPAD[GAMEPAD_Y] = y_inc

    x = PLAYER[PX] + x_inc
    y = PLAYER[PY] + y_inc

    if x < 18:
        x = 18
    if x > MAXSCREEN_X - 18:
        x = MAXSCREEN_X - 18
    if y < 42:
        y = 42
    if y > GROUND_Y - 30:
        y = GROUND_Y - 30

    PLAYER[PX] = x
    PLAYER[PY] = y

    if GAMEPAD[GAMEPAD_DELAY] and not (buttons & GAMEPAD_RIGHT):
        fire_missile()
        GAMEPAD[GAMEPAD_DELAY] = 0


def fire_missile():
    if GAME[GAME_OVER]:
        return

    x = PLAYER[PX]
    y = PLAYER[PY]

    best_slot = -1
    best_dist = 1 << 30

    # Nearest living base from slots 0,4,8.
    for slot in (BASE0, BASE1, BASE2):
        if not CITY[slot]:
            continue
        dx = x - TARGET_X[slot]
        dy = y - TARGET_Y
        d = dx * dx + dy * dy
        if d < best_dist:
            best_dist = d
            best_slot = slot

    if best_slot < 0:
        return

    for n in range(NUM_ASSET):
        i = n * ASSET_PARAMS
        if ASSET[i + ALIFE] == 0:
            launch_x = TARGET_X[best_slot]
            launch_y = TARGET_Y
            ASSET[i + AX] = launch_x
            ASSET[i + AY] = launch_y
            ASSET[i + ALIFE] = 1
            ASSET[i + APOS] = 0
            ASSET[i + ALEN] = init_trail(TRAIL, launch_x, launch_y, x, y, n)
            ASSET[i + ACROSS_X] = x
            ASSET[i + ACROSS_Y] = y
            ASSET[i + ALAUNCH] = best_slot
            snd.play(SHOOT, vol=220)
            return
 


# ── Game movement / collision ────────────────────────────────────────────────
@micropython.viper
def start_explode(x:int, y:int):
    snd.play(EXP, vol=220)
    exp = ptr32(EXPLODE)
    for index in range(NUM_EXPLODE):
        i = index * EXP_PARAMS
        if exp[i + EXP_ACTIVE] == 0:
            exp[i + EXP_X] = x
            exp[i + EXP_Y] = y
            exp[i + EXP_RADIUS] = 2
            exp[i + EXP_DIR] = 1
            exp[i + EXP_ACTIVE] = 1
            return


@micropython.viper
def move_assets():
    asset = ptr32(ASSET)
    trail = ptr16(TRAIL)
    for index in range(NUM_ASSET):
        i = index * ASSET_PARAMS
        if asset[i + ALIFE] == 0:
            continue

        pos = asset[i + APOS] + PLAYER_STEP
        end = asset[i + ALEN]
        base = index * TRAIL_STRIDE

        if pos >= end:
            pos = end
            p = base + (pos * 2)
            x = trail[p]
            y = trail[p + 1]
            asset[i + AX] = x
            asset[i + AY] = y
            asset[i + ALIFE] = 0
            start_explode(x, y)
            continue

        p = base + (pos * 2)
        asset[i + AX] = trail[p]
        asset[i + AY] = trail[p + 1]
        asset[i + APOS] = pos


@micropython.viper
def move_enemies():
    enemy = ptr32(ENEMY)
    trail = ptr16(TRAIL2)
    city = ptr8(CITY)
    game = ptr32(GAME)

    wave = game[GAME_WAVE]
    step = 1 + (wave // 3)
    if step > 5:
        step = 5

    for index in range(NUM_ENEMY):
        i = index * ENEMY_PARAMS
        if enemy[i + ELIFE] == 0:
            continue

        pos = enemy[i + EPOS] + step
        end = enemy[i + ELEN]
        base = index * TRAIL_STRIDE

        if pos >= end:
            pos = end
            p = base + (pos * 2)
            x = trail[p]
            y = trail[p + 1]
            enemy[i + EX] = x
            enemy[i + EY] = y
            enemy[i + ELIFE] = 0
            game[GAME_ENEMY_LEFT] -= 1
            target = enemy[i + ETARGET]

            if city[target] != 0:
                city[target] = 0
                if target != BASE0 and target != BASE1 and target != BASE2:
                    game[GAME_CITIES] -= 1
                    if game[GAME_CITIES] <= 0:
                        game[GAME_OVER] = 1

            start_explode(x, y)
            continue

        p = base + (pos * 2)
        enemy[i + EX] = trail[p]
        enemy[i + EY] = trail[p + 1]
        enemy[i + EPOS] = pos


@micropython.viper
def update_explosions():
    exp = ptr32(EXPLODE)
    enemy = ptr32(ENEMY)
    game = ptr32(GAME)

    for index in range(NUM_EXPLODE):
        i = index * EXP_PARAMS
        if exp[i + EXP_ACTIVE] == 0:
            continue

        r = exp[i + EXP_RADIUS] + (exp[i + EXP_DIR] * EXP_STEP)
        if r >= EXP_MAX_RADIUS:
            r = EXP_MAX_RADIUS
            exp[i + EXP_DIR] = -1
        elif r <= 0:
            exp[i + EXP_ACTIVE] = 0
            continue
        exp[i + EXP_RADIUS] = r

        x0 = exp[i + EXP_X]
        y0 = exp[i + EXP_Y]
        r2 = r * r

        for e_index in range(NUM_ENEMY):
            e = e_index * ENEMY_PARAMS
            if enemy[e + ELIFE] == 0:
                continue

            dx = enemy[e + EX] - x0
            dy = enemy[e + EY] - y0
            if (dx * dx + dy * dy) <= r2:
                enemy[e + ELIFE] = 0
                game[GAME_ENEMY_LEFT] -= 1
                game[GAME_SCORE] += 25
                start_explode(enemy[e + EX], enemy[e + EY])


def maybe_next_wave():
    if GAME[GAME_OVER]:
        return
    if GAME[GAME_ENEMY_LEFT] > 0:
        return
    for n in range(NUM_EXPLODE):
        if EXPLODE[n * EXP_PARAMS + EXP_ACTIVE]:
            return
    # Wave cleared: start the between-wave intermission instead of jumping
    # straight into init_enemy().  core0() counts GAME[INTERMISSION] down
    # and calls init_enemy() itself once it reaches 0.
    GAME[INTERMISSION] = INTERMISSION_START


# ── Drawing helpers ──────────────────────────────────────────────────────────
@micropython.viper
def draw_city_block(x0:int, y0:int, color:int):
    screen = ptr8(fb)
    for yy in range(CITY_Y_SCALE):
        y = y0 + yy
        if y < 0 or y >= MAXSCREEN_Y:
            continue
        addr = y * MAXSCREEN_X + x0
        for xx in range(CITY_X_SCALE):
            x = x0 + xx
            if x >= 0 and x < MAXSCREEN_X:
                screen[addr + xx] = color


@micropython.viper
def draw_scene():
    screen = ptr8(fb)
    city = ptr8(CITY)
    target_x = ptr16(TARGET_X)
    bitmap = ptr8(CITY_BITMAP)
    enemy = ptr32(ENEMY)
    asset = ptr32(ASSET)
    player = ptr32(PLAYER)
    trail = ptr16(TRAIL)
    trail2 = ptr16(TRAIL2)

    # Ground line       
    SCREEN.rect(0,449,640,480-449,YELLOW,1)
    
    # Cities and bases.
    for slot in range(9):
        cx = target_x[slot]
        alive = city[slot]
        
        if slot == BASE0 or slot == BASE1 or slot == BASE2:
            if alive:
                # Pyramid / missile battery.
                base_top = GROUND_Y - 24
                for yy in range(24):
                    half = 5 + ((yy * 30) // 24)
                    y = base_top + yy
                    x0 = cx - half
                    x1 = cx + half
                    if y >= 0 and y < MAXSCREEN_Y:
                        addr = y * MAXSCREEN_X
                        for xx in range(x0, x1 + 1):
                            if xx >= 0 and xx < MAXSCREEN_X:
                                screen[addr + xx] = YELLOW
                # Silo slot.
                for yy in range(5):
                    y = GROUND_Y - 28 + yy
                    addr = y * MAXSCREEN_X
                    for xx in range(cx - 3, cx + 4):
                        if xx >= 0 and xx < MAXSCREEN_X:
                            screen[addr + xx] = WHITE
            else:
                # Dead-base rubble.
                y = GROUND_Y - 6
                addr = y * MAXSCREEN_X
                for xx in range(cx - 22, cx + 23):
                    if xx >= 0 and xx < MAXSCREEN_X:
                        screen[addr + xx] = DIMRED
            continue

        if alive:
            x_base = cx - (CITY_W // 2)
            for by in range(CITY_H_BITS):
                for bx in range(CITY_W_BITS):
                    val = bitmap[by * CITY_W_BITS + bx]
                    if val:
                        color = BLUE
                        if val == 2:
                            color = SKYBLUE
                        block_x = x_base + (bx * CITY_X_SCALE)
                        block_y = CITY_TOP + (by * CITY_Y_SCALE)
                        for yy in range(CITY_Y_SCALE):
                            y = block_y + yy
                            if y < 0 or y >= MAXSCREEN_Y:
                                continue
                            addr = y * MAXSCREEN_X
                            for xx in range(CITY_X_SCALE):
                                x = block_x + xx
                                if x >= 0 and x < MAXSCREEN_X:
                                    screen[addr + x] = color
        else:
            # Dead-city rubble.
            y = GROUND_Y - 8
            addr = y * MAXSCREEN_X
            for xx in range(cx - (CITY_W // 2), cx + (CITY_W // 2) + 1):
                if xx >= 0 and xx < MAXSCREEN_X:
                    screen[addr + xx] = DIMRED

    # Player missile trails and target crosses.
    for index in range(NUM_ASSET):
        i = index * ASSET_PARAMS
        if asset[i + ALIFE] == 0:
            continue

        base = index * TRAIL_STRIDE
        pos = asset[i + APOS]
        if pos > asset[i + ALEN]:
            pos = asset[i + ALEN]

        p = 0
        while p <= pos:
            j = base + (p * 2)
            x = trail[j]
            y = trail[j + 1]
            if x >= 0 and x < MAXSCREEN_X and y >= 0 and y < MAXSCREEN_Y:
                screen[y * MAXSCREEN_X + x] = SKYBLUE 
            p += 2

        x = asset[i + AX]
        y = asset[i + AY]
        if x >= 1 and x < MAXSCREEN_X - 1 and y >= 1 and y < MAXSCREEN_Y - 1:
            addr = y * MAXSCREEN_X + x
            screen[addr] = WHITE
            screen[addr - 1] = WHITE
            screen[addr + 1] = WHITE
            screen[addr - MAXSCREEN_X] = WHITE
            screen[addr + MAXSCREEN_X] = WHITE

        cx = asset[i + ACROSS_X]
        cy = asset[i + ACROSS_Y]
        for d in range(-8, 9):
            if d < -3 or d > 3:
                xh = cx + d
                yv = cy + d
                if xh >= 0 and xh < MAXSCREEN_X and cy >= 0 and cy < MAXSCREEN_Y:
                    screen[cy * MAXSCREEN_X + xh] = WHITE
                if cx >= 0 and cx < MAXSCREEN_X and yv >= 0 and yv < MAXSCREEN_Y:
                    screen[yv * MAXSCREEN_X + cx] = WHITE

    # Enemy missile trails.
    for index in range(NUM_ENEMY):
        i = index * ENEMY_PARAMS
        if enemy[i + ELIFE] == 0:
            continue

        base = index * TRAIL_STRIDE
        pos = enemy[i + EPOS]
        if pos > enemy[i + ELEN]:
            pos = enemy[i + ELEN]

        p = 0
        while p <= pos:
            j = base + (p * 2)
            x = trail2[j]
            y = trail2[j + 1]
            if x >= 0 and x < MAXSCREEN_X and y >= 0 and y < MAXSCREEN_Y:
                screen[y * MAXSCREEN_X + x] = RED
            p += 2

        x = enemy[i + EX]
        y = enemy[i + EY]
        if x >= 1 and x < MAXSCREEN_X - 1 and y >= 1 and y < MAXSCREEN_Y - 1:
            addr = y * MAXSCREEN_X + x
            screen[addr] = WHITE
            screen[addr - 1] = WHITE
            screen[addr + 1] = WHITE
            screen[addr - MAXSCREEN_X] = WHITE
            screen[addr + MAXSCREEN_X] = WHITE

    # Explosions and cursor are drawn after this viper pass so the blast uses
    # framebuf.ellipse() and the cursor can still sit on top of explosions.


def draw_explosions():
    # framebuf.ellipse() uses center x/y and x/y radii. 
    for index in range(NUM_EXPLODE):
        i = index * EXP_PARAMS
        if EXPLODE[i + EXP_ACTIVE] == 0:
            continue
        r = EXPLODE[i + EXP_RADIUS]
        band = (r // 13) & 3
        SCREEN.ellipse(EXPLODE[i + EXP_X], EXPLODE[i + EXP_Y],
                       r, r, EXP_COLORS[band], True)


@micropython.viper
def draw_cursor():
    screen = ptr8(fb)
    player = ptr32(PLAYER)

    x = player[PX]
    y = player[PY]
    blink = player[PBLINK] + 1
    if blink > 30:
        blink = 0
    player[PBLINK] = blink

    if blink < 24:
        for d in range(-13, 14):
            if d < -4 or d > 4:
                xh = x + d
                yv = y + d
                if xh >= 0 and xh < MAXSCREEN_X:
                    screen[y * MAXSCREEN_X + xh] = WHITE
                if yv >= 0 and yv < MAXSCREEN_Y:
                    screen[yv * MAXSCREEN_X + x] = WHITE

@micropython.viper
def draw():
    game = ptr32(GAME)
    fill_asm(fb, BACKGROUND, FLAG_ADDR)
    draw_scene()
    draw_explosions()
    draw_cursor()
   
    # Hershey HUD.
    if game[INTERMISSION] > 0 or game[GAME_OVER]:
        hershey.text('SCORE', 12, HUD_Y, 650, SKYBLUE)
        hershey.number(GAME[GAME_SCORE], 110, HUD_Y + 1, 700, WHITE)
        
        hershey.text('WAVE', 280, HUD_Y, 650, SKYBLUE)
        hershey.number(GAME[GAME_WAVE], 350, HUD_Y + 1, 700, WHITE)

        hershey.text('CITIES', 480, HUD_Y, 650, SKYBLUE)
        hershey.number(GAME[GAME_CITIES], 585, HUD_Y + 1, 700, WHITE)
      

    if game[GAME_OVER]:
        hershey.text('GAME OVER', 220, 150, 1100, RED)
        hershey.text('DOWN TO RESTART', 200, 220, 900, SKYBLUE)
        
    #draw_scene()


# ── Core loops ───────────────────────────────────────────────────────────────
def core0():
    restart()

    pad_ticks = ticks_ms()
    fire_latch_ticks = ticks_ms()
    enemy_ticks = ticks_ms()
    asset_ticks = ticks_ms()
    explode_ticks = ticks_ms()
    wave_ticks = ticks_ms()
    intermission_ticks = ticks_ms()

    while not GAME_CTL[GAME_CTL_EXIT]:
        sleep_ms(1)
        now = ticks_ms()

        if ticks_diff(now, pad_ticks) > 25:
            pad_ticks = now
            read_gamepad()

        # Holding RIGHT can continue firing, but not every frame.
        if ticks_diff(now, fire_latch_ticks) > 150:
            fire_latch_ticks = now
            GAMEPAD[GAMEPAD_DELAY] = 1

        if not GAME[GAME_OVER]:
            if ticks_diff(now, asset_ticks) > ASSET_MOVE_MS:
                asset_ticks = now
                move_assets()

            if GAME[INTERMISSION] > 0:
                if ticks_diff(now, intermission_ticks) > INTERMISSION_TICK_MS:
                    intermission_ticks = now
                    GAME[INTERMISSION] -= 1
                    if GAME[INTERMISSION] == 0:
                        init_enemy()
            elif ticks_diff(now, wave_ticks) > 250:
                wave_ticks = now
                maybe_next_wave()
                
        if ticks_diff(now, enemy_ticks) > ENEMY_MOVE_MS:
            enemy_ticks = now
            move_enemies()
                
        if ticks_diff(now, explode_ticks) > EXPLODE_MOVE_MS:
            explode_ticks = now
            update_explosions()

    GAME_CTL[GAME_CTL_EXIT] = 1
    print('core0 done')


def core1():
    sleep_ms(500)
    while not GAME_CTL[GAME_CTL_EXIT]:
        draw()
    print('core1 done')


def shutdown():
    global hershey
    GAME_CTL[GAME_CTL_EXIT] = 1
    display.deinit()
    snd.deinit()
    del hershey
    machine.freq(150_000_000)
    print('shutdown...')
    sleep_ms(500)
    exit()


def main():
    global display,FLAG_ADDR,snd,BEGIN,EXP,EXPBIG,SHOOT,hershey
    display = DVI_RP2_HSTX()
    display.begin(fb, rv_colors.COLOR_MODE_BGR233,
                  height=SCREEN_H, width=SCREEN_W, bytes_per_pixel=1)
    FLAG_ADDR = addressof(display._frame_flag)
    from audio_mixer import Mixer
    snd = Mixer()
    BEGIN  = snd.load("/MissileCommand/begin.wav")
    EXP    = snd.load("/MissileCommand/exp.wav")
    EXPBIG = snd.load("/MissileCommand/expbig.wav")
    SHOOT  = snd.load("/MissileCommand/shoot.wav")
    from hersheyDVI2a import Hershey
    hershey = Hershey(SCREEN, center_numbers=True)
    hershey._desc_shift = 7
    hershey.slow = False

    init_arrays()
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