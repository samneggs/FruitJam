# pacmanDVI.py -- Pac-Man for Adafruit Fruit Jam / RP2350, 320x240 DVI, dual-core
# Ported from pacman14.py (240x160 ST7796). Hardware scaffolding from arkanoid7.py:
#   246 MHz overclock, DVI/HSTX framebuffer, Gamepad, audio_mixer2.Mixer.
#
# Maze is 28x31 tiles of 8x8 = 224x248 px.
#   Horizontal: centred, MAZE_X0 = 48  -> maze occupies x 48..271, 48px HUD panel each side.
#   Vertical:   4 px shaved off the top tile row and 4 px off the bottom tile row,
#               248 - 8 = 240 exactly.  No scrolling: GAME_MAZEY is gone.
#               screen_y = world_y - MAZE_YCROP
#
# Controls: analog X/Y = direction, SELECT = quit
import sys
from dvi_rp2_hstx_frame_sync_v2 import DVI_RP2_HSTX
from gamepadfast import Gamepad
from draw_numberdvi import Draw_number
import colors as rv_colors
import gc, array, framebuf, _thread, machine
from random import randint
from time import sleep_ms, ticks_ms, ticks_us
from sys import exit

SCREEN_W = const(320)
SCREEN_H = const(240)

machine.freq(246_000_000)
machine.mem32[0x40010058] = 2 << 16          # HSTX CLK / 2
machine.mem32[0x40010054] = 1 << 11          # HSTX CLK use SYS CLK

fb = bytearray(SCREEN_W * SCREEN_H * 2)      # DVI scanout buffer

# -- maze geometry --------------------------------------------------------------
TILE          = const(8)
MAZE_W_TILES  = const(28)
MAZE_H_TILES  = const(31)
MAZE_PX_W     = const(224)                   # 28 * 8
MAZE_X0       = const(48)                    # screen x of maze column 0
MAZE_X1       = const(MAZE_X0 + MAZE_PX_W)   # 272, exclusive
MAZE_YCROP    = const(4)                     # source rows shaved off top (and bottom)
# world -> screen: sx = wx + MAZE_X0, sy = wy - MAZE_YCROP
# 16x16 sprites are drawn centred, so fold the -8/-8 in:
SPR_XOFF      = const(MAZE_X0 - 8)           # 40
SPR_YOFF      = const(-(MAZE_YCROP + 8))     # -12
ROW32         = const(SCREEN_W >> 1)         # 160 words per screen row
TILE_W32      = const(4)                     # 8 px = 4 words
TILE_SZ32     = const(32)                    # 8x8 px = 32 words

# -- colours (originals were byte-swapped for the ST7796; these are native RGB565) -
BLUE     = const(0b00000_000000_11111)
BLACK    = const(0x0000)
WHITE    = const(0xFFFF)
YELLOW   = const(0xFF00)                     # original 0x00ff swapped
RED      = const(0xE007)                     # original 0x07e0 swapped
ORANGE   = const(0xFC80)
CYAN     = const(0x07FF)
LT_GRAY  = const(0xAD55)

# -- draw_number slots ----------------------------------------------------------
FPS_CORE0 = const(0)
FPS_CORE1 = const(1)
SCORE     = const(2)
LIVES     = const(3)
NUM_VALUES = const(6)

SHOW_FPS  = const(0)                         # 1 = show the two diagnostic counters

# -- player ---------------------------------------------------------------------
PLAYER_X     = const(0)
PLAYER_Y     = const(1)
PLAYER_DIR   = const(2)                      # current movement direction
PLAYER_NEXT  = const(3)                      # desired direction from the stick
PLAYER_ANIM  = const(4)
PLAYER_AINC  = const(5)                      # animation increment +1 or -1
PLAYER_APOS  = const(6)                      # animation position/frame
PLAYER_STATE = const(7)                      # 0=off,1=stopped,2=moving,3=dieing
PLAYER_PARAMS = const(8)
DIR_LEFT  = const(0)
DIR_RIGHT = const(1)
DIR_DOWN  = const(2)
DIR_UP    = const(3)
DIR_NONE  = const(4)

# -- ghosts ---------------------------------------------------------------------
GHOST_X        = const(0)
GHOST_Y        = const(1)
GHOST_DIR      = const(2)
GHOST_ANIM     = const(3)
GHOST_STATE    = const(4)
GHOST_TARGET_X = const(5)
GHOST_TARGET_Y = const(6)
GHOST_TICKS    = const(7)
GHOST_SLOW     = const(8)
GHOST_PARAMS   = const(10)
NUM_GHOSTS   = const(4)
GHOST_BLINKY = const(0)
GHOST_PINKY  = const(1)
GHOST_INKY   = const(2)
GHOST_CLYDE  = const(3)
MODE_HOME    = const(0)
MODE_CHASE   = const(1)
MODE_SCATTER = const(2)
MODE_FRIGHT  = const(3)
MODE_EATEN   = const(4)
MODE_WAITING = const(5)
FRUIT_NONE    = const(0)
FRUIT_VISIBLE = const(1)
FRUIT_SCORE   = const(2)
GHOST_SPRITE_BASE = const(32)

SCATTER_TARGETS = bytearray([25, 0, 2, 0, 27, 34, 0, 34])
FRUIT_POINTS = array.array('H', [100, 300, 500, 700, 1000])
WAVE_TIMES   = array.array('i', [7000, 20000, 7000, 20000, 5000, 20000, 5000, 0])

# -- game state -----------------------------------------------------------------
GAME_BLINK         = const(1)
GAME_MODE          = const(2)
GAME_MODE_TIMER    = const(3)
GAME_FRIGHT_TIMER  = const(4)
GAME_WAVE          = const(5)
GAME_RELEASE_TIMER = const(6)
GAME_EAT_COMBO     = const(7)
GAME_SCORE_TIMER   = const(8)
GAME_SCORE_X       = const(9)
GAME_SCORE_Y       = const(10)
GAME_SCORE_IDX     = const(11)
GAME_DEATH_TIMER   = const(12)
GAME_FRUIT_STATE   = const(13)
GAME_FRUIT_TIMER   = const(14)
GAME_PELLETS_EATEN = const(15)
GAME_FRUIT_SHOWN   = const(16)
GAME_LEVEL         = const(17)
GAME_PAUSE_TIMER   = const(18)
GAME_HISCORE       = const(19)               # added
GAME_OVER_TIMER    = const(20)               # added
GAME_EXIT          = const(21)               # added, core handshake
GAME_RDY           = const(22)               # added, core handshake
GAME_FRAME         = const(23)               # added, free-running frame counter
GAME_PARAMS        = const(24)

RESTART_ON_GAMEOVER = const(1)

# -- fixed timestep -------------------------------------------------------------
FRAME_US = const(30_000)#16667
FRAME_MS = const(17) #17                        # ms consumed by one update()
ANIM_FRAMES  = const(3)                      # 50 ms  -> every 3 frames
DEATH_FRAMES = const(4)#7                      # 120 ms -> every 7 frames
BLINK_FRAMES = const(9)                      # 150 ms -> every 9 frames

# -- input ----------------------------------------------------------------------
GAMEPAD_SELECT = const(0b0000001)
DEADZONE_X = const(64)                       # gamepad is -512..512
DEADZONE_Y = const(160)                      # matches the original's asymmetric pot thresholds
Y_SIGN     = const(1)                        # flip to -1 if up/down come out inverted
I_DIR = const(0)
INPUT = array.array('i', [DIR_NONE])

# -- HUD placement (right-aligned x for numbers) --------------------------------
HUD_SCORE_X  = const(30)
HUD_SCORE_Y  = const(16)
HUD_HI_X     = const(40)
HUD_HI_Y     = const(42)
HUD_LIVES_X  = const(278)
HUD_LIVES_Y  = const(16)
HUD_FRUIT_X  = const(276)
HUD_FRUIT_Y  = const(SCREEN_H - 40)          # clears the FPS box at y 222+
READY_X      = const(MAZE_X0 + 88)           # tile col 11
READY_Y      = const(17 * TILE - MAZE_YCROP)
OVER_X       = const(MAZE_X0 + 76)
OVER_Y       = const(17 * TILE - MAZE_YCROP)

# -- pre-allocated state --------------------------------------------------------
PLAYER = array.array('i', [0] * PLAYER_PARAMS)
GAME   = array.array('i', [0] * GAME_PARAMS)
GHOSTS = array.array('i', [0] * (GHOST_PARAMS * NUM_GHOSTS))

MAZE_SPRITES = None
MAZE_DATA    = None
MAZE_ORIGINAL = None
CHAR_SPRITES = None
PELLET_COUNT = 0
BG_HANDLE    = None
CURRENT_BG_SOUND = 0

def load_files():
    global MAZE_SPRITES, MAZE_DATA, CHAR_SPRITES, MAZE_ORIGINAL, PELLET_COUNT
    with open('/Pacman/PM_maze2.bin', 'rb') as f:
        f.read(4)                            # skip header
        MAZE_SPRITES = bytearray(f.read())
    with open('/Pacman/pm_maze.bin', 'rb') as f:
        MAZE_DATA = bytearray(f.read())
    MAZE_ORIGINAL = bytes(MAZE_DATA)
    PELLET_COUNT = 0
    for tile in MAZE_DATA:
        if tile == 47 or tile == 48 or tile == 49:
            PELLET_COUNT += 1
    with open('/Pacman/PM_Sprites5.bin', 'rb') as f:
        f.read(4)
        CHAR_SPRITES = bytearray(f.read())

# -- display / gamepad ----------------------------------------------------------
display = DVI_RP2_HSTX()
display.begin(fb, rv_colors.COLOR_MODE_BGR565, height=SCREEN_H,
              width=SCREEN_W, bytes_per_pixel=2)
gamepad = Gamepad()

fb2 = bytearray(SCREEN_W * SCREEN_H * 2)     # draw buffer
SCREEN = framebuf.FrameBuffer(fb2, SCREEN_W, SCREEN_H, framebuf.RGB565)
draw_num = Draw_number(fb2, SCREEN_W, 2)

# -- 3x5 font for the static HUD labels (from arkanoid7) ------------------------
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
    if n == 0:
        draw_text(x1 - 4 * sc, y, '0', color, sc)
        return
    x = x1
    while n:
        x -= 4 * sc
        d = n % 10
        n //= 10
        draw_text(x, y, '0123456789'[d], color, sc)

# -- template asm: fast fill + framebuffer copy (from arkanoid7) -----------------
@micropython.asm_thumb
def fill_asm(r0, r1):                        # r0=buffer addr, r1=16-bit colour
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
def copy_fb(r0, r1):                         # r0=source, r1=dest
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

# -- sound ----------------------------------------------------------------------
def init_sounds():
    global SND_START, SND_FAIL, SND_DOT, SND_GHOST1
    global SND_RETURN_HOME, SND_TURN_BLUE, SND_EAT_FRUIT
    SND_START       = snd.load('/Pacman/start_.wav')
    SND_FAIL        = snd.load('/Pacman/fail_.wav')
    SND_DOT         = snd.load('/Pacman/eat_dot2_.wav')
    SND_GHOST1      = snd.load('/Pacman/ghost_1_.wav')
    SND_RETURN_HOME = snd.load('/Pacman/return_home_.wav')
    SND_TURN_BLUE   = snd.load('/Pacman/turn_blue_.wav')
    SND_EAT_FRUIT   = snd.load('/Pacman/eat_fruit_.wav')
    gc.collect()

def stop_background():
    global BG_HANDLE, CURRENT_BG_SOUND
    if BG_HANDLE is not None:
        snd.stop(BG_HANDLE)
        BG_HANDLE = None
    CURRENT_BG_SOUND = 0

def update_background_sound():
    # priority: any ghost eaten > any ghost frightened > normal siren
    global CURRENT_BG_SOUND, BG_HANDLE
    ghosts = GHOSTS
    need_sound = 1
    for i in range(NUM_GHOSTS):
        state = ghosts[i * GHOST_PARAMS + GHOST_STATE]
        if state == MODE_EATEN:
            need_sound = 4
            break
        elif state == MODE_FRIGHT and need_sound < 3:
            need_sound = 3
    if GAME[GAME_PAUSE_TIMER] > 0 or GAME[GAME_DEATH_TIMER] > 0 or GAME[GAME_OVER_TIMER] > 0:
        need_sound = 0
    if need_sound == CURRENT_BG_SOUND:
        return
    if BG_HANDLE is not None:
        snd.stop(BG_HANDLE)
        BG_HANDLE = None
    CURRENT_BG_SOUND = need_sound
    if need_sound == 4:
        BG_HANDLE = snd.play(SND_RETURN_HOME, vol=200, loop=True)
    elif need_sound == 3:
        BG_HANDLE = snd.play(SND_TURN_BLUE, vol=200, loop=True)
    elif need_sound == 1:
        BG_HANDLE = snd.play(SND_GHOST1, vol=200, loop=True)

# -- input ----------------------------------------------------------------------
def read_gamepad():
    gamepad.read()
    if not (gamepad.buttons & GAMEPAD_SELECT):
        shutdown()
    x = gamepad.x
    y = gamepad.y * Y_SIGN
    # same priority ladder as the original pot code: X wins over Y
    if x < -DEADZONE_X:
        INPUT[I_DIR] = DIR_LEFT
    elif x > DEADZONE_X:
        INPUT[I_DIR] = DIR_RIGHT
    elif y > DEADZONE_Y:
        INPUT[I_DIR] = DIR_DOWN
    elif y < -DEADZONE_Y:
        INPUT[I_DIR] = DIR_UP
    else:
        INPUT[I_DIR] = DIR_NONE

# -- player movement (was read_pot) ---------------------------------------------
@micropython.viper
def move_player():
    inp = ptr32(INPUT)
    player = ptr32(PLAYER)
    game = ptr32(GAME)
    maze = ptr8(MAZE_DATA)
    player_x = player[PLAYER_X]
    player_y = player[PLAYER_Y]
    current_dir = player[PLAYER_DIR]
    next_dir = player[PLAYER_NEXT]
    input_dir = inp[I_DIR]
    if input_dir != DIR_NONE:                    # store new desired direction
        next_dir = input_dir
        player[PLAYER_NEXT] = next_dir
    tile_x = player_x >> 3                       # current tile position
    tile_y = player_y >> 3
    center_x = (tile_x << 3) + 4                 # center of current tile
    center_y = (tile_y << 3) + 4
    at_center_x = (player_x == center_x)
    at_center_y = (player_y == center_y)
    if next_dir != DIR_NONE and at_center_x and at_center_y:   # try turn at intersection
        check_x = tile_x
        check_y = tile_y
        if next_dir == DIR_LEFT:
            check_x = tile_x - 1
        elif next_dir == DIR_RIGHT:
            check_x = tile_x + 1
        elif next_dir == DIR_DOWN:
            check_y = tile_y + 1
        elif next_dir == DIR_UP:
            check_y = tile_y - 1
        maze_addr = check_y * 28 + check_x
        wall = maze[maze_addr]
        if 0 < check_x < 27 and 0 < check_y < 30 and wall > 45:
            current_dir = next_dir
            player[PLAYER_DIR] = current_dir
    new_x = player_x
    new_y = player_y
    x_offset = 0
    y_offset = 0
    if current_dir == DIR_LEFT:
        new_x = player_x - 2
        x_offset = -4
    elif current_dir == DIR_RIGHT:
        new_x = player_x + 2
        x_offset = 3
    elif current_dir == DIR_DOWN:
        new_y = player_y + 2
        y_offset = 3
    elif current_dir == DIR_UP:
        new_y = player_y - 2
        y_offset = -4
    if current_dir != DIR_NONE:
        check_tile_x = (new_x + x_offset) >> 3
        check_tile_y = (new_y + y_offset) >> 3
        maze_addr = check_tile_y * 28 + check_tile_x
        wall = maze[maze_addr]
        if (tile_y == 14 and (new_x < 20 or new_x > 200)) or (11 < new_x < (28 * 8 - 4) and 11 < new_y < (31 * 8 - 4) and wall > 45):
            if new_x < -8:                       # tunnel wrap left
                new_x = 230
            if new_x > 230:                      # tunnel wrap right
                new_x = -8
            player[PLAYER_X] = new_x
            player[PLAYER_Y] = new_y
            if new_x == (check_tile_x << 3) + 4 and new_y == (check_tile_y << 3) + 4 and 0 < new_x < 220:
                tile_val = maze[maze_addr]
                if tile_val == 47 or tile_val == 48:        # regular pellet
                    pellets = game[GAME_PELLETS_EATEN] + 1
                    game[GAME_PELLETS_EATEN] = pellets
                    draw_num.add(SCORE, 10)
                    if pellets % 2 == 0:  #3
                        play_dot()
                elif tile_val == 49:                        # power pellet
                    game[GAME_PELLETS_EATEN] = game[GAME_PELLETS_EATEN] + 1
                    draw_num.add(SCORE, 50)
                    start_frightened_mode()
                    update_background_sound()
                if tile_val == 47 or tile_val == 48 or tile_val == 49:
                    maze[maze_addr] = 46
        else:
            player[PLAYER_DIR] = DIR_NONE        # hit wall, stop moving

def play_dot():
    snd.play(SND_DOT, vol=200)

# -- animation ------------------------------------------------------------------
@micropython.viper
def animation():
    player = ptr32(PLAYER)
    animate_base = 0
    frames = 2
    if player[PLAYER_STATE] == 3:                # dying: one-way through frames 0-11
        pos = player[PLAYER_APOS]
        if pos < 12:
            player[PLAYER_APOS] = pos + 1
        player[PLAYER_ANIM] = player[PLAYER_APOS]
        return
    elif player[PLAYER_DIR] == DIR_NONE:
        if player[PLAYER_NEXT] == DIR_RIGHT:
            player[PLAYER_ANIM] = 13
        elif player[PLAYER_NEXT] == DIR_LEFT:
            player[PLAYER_ANIM] = 16
        elif player[PLAYER_NEXT] == DIR_UP:
            player[PLAYER_ANIM] = 19
        elif player[PLAYER_NEXT] == DIR_DOWN:
            player[PLAYER_ANIM] = 22
        return
    elif player[PLAYER_DIR] == DIR_RIGHT:
        animate_base = 12
    elif player[PLAYER_DIR] == DIR_LEFT:
        animate_base = 15
    elif player[PLAYER_DIR] == DIR_UP:
        animate_base = 18
    elif player[PLAYER_DIR] == DIR_DOWN:
        animate_base = 21
    if player[PLAYER_APOS] >= frames:
        player[PLAYER_AINC] = -1
    if player[PLAYER_APOS] <= 0:
        player[PLAYER_AINC] = 1
    player[PLAYER_APOS] = (player[PLAYER_APOS] + player[PLAYER_AINC])
    player[PLAYER_ANIM] = animate_base + player[PLAYER_APOS]

@micropython.viper
def animate_ghosts():
    ghosts = ptr32(GHOSTS)
    for i in range(NUM_GHOSTS):
        ghosts[i * GHOST_PARAMS + GHOST_ANIM] ^= 1

@micropython.viper
def start_frightened_mode():
    game = ptr32(GAME)
    ghosts = ptr32(GHOSTS)
    game[GAME_FRIGHT_TIMER] = 6000
    game[GAME_EAT_COMBO] = 0
    for i in range(NUM_GHOSTS):
        base = i * GHOST_PARAMS
        state = ghosts[base + GHOST_STATE]
        if state != MODE_HOME and state != MODE_EATEN and state != MODE_WAITING:
            ghosts[base + GHOST_STATE] = MODE_FRIGHT
            d = ghosts[base + GHOST_DIR]         # reverse direction
            if d == DIR_LEFT:
                ghosts[base + GHOST_DIR] = DIR_RIGHT
            elif d == DIR_RIGHT:
                ghosts[base + GHOST_DIR] = DIR_LEFT
            elif d == DIR_UP:
                ghosts[base + GHOST_DIR] = DIR_DOWN
            elif d == DIR_DOWN:
                ghosts[base + GHOST_DIR] = DIR_UP

@micropython.viper
def check_collisions() -> int:
    player = ptr32(PLAYER)
    ghosts = ptr32(GHOSTS)
    state = player[PLAYER_STATE]
    if state != 1 and state != 2:
        return 0
    px = player[PLAYER_X]
    py = player[PLAYER_Y]
    for i in range(NUM_GHOSTS):
        base = i * GHOST_PARAMS
        gstate = ghosts[base + GHOST_STATE]
        if gstate == MODE_HOME or gstate == MODE_WAITING:
            continue
        gx = ghosts[base + GHOST_X]
        gy = ghosts[base + GHOST_Y]
        dx = px - gx
        dy = py - gy
        if dx < 0:
            dx = -dx
        if dy < 0:
            dy = -dy
        if dx < 7 and dy < 7:
            if gstate == MODE_FRIGHT:
                return i + 1                     # positive = eat
            elif gstate != MODE_EATEN:
                return -(i + 1)                  # negative = player dies
    return 0

@micropython.viper
def eat_ghost(ghost_idx: int):
    ghosts = ptr32(GHOSTS)
    game = ptr32(GAME)
    base = ghost_idx * GHOST_PARAMS
    ghosts[base + GHOST_STATE] = MODE_EATEN
    combo = game[GAME_EAT_COMBO]
    score_sprite = 72 + combo                    # 72=200, 73=400, 74=800, 75=1600
    if combo < 3:
        game[GAME_EAT_COMBO] = combo + 1
    game[GAME_SCORE_TIMER] = 500
    game[GAME_SCORE_X] = ghosts[base + GHOST_X]
    game[GAME_SCORE_Y] = ghosts[base + GHOST_Y]
    game[GAME_SCORE_IDX] = score_sprite
    points = 200 << combo
    draw_num.add(SCORE, points)
    update_background_sound()

def spawn_fruit():
    GAME[GAME_FRUIT_STATE] = FRUIT_VISIBLE
    GAME[GAME_FRUIT_TIMER] = 9000

@micropython.viper
def check_fruit():
    game = ptr32(GAME)
    player = ptr32(PLAYER)
    state = game[GAME_FRUIT_STATE]
    if state == FRUIT_NONE:
        pellets = game[GAME_PELLETS_EATEN]
        shown = game[GAME_FRUIT_SHOWN]
        if pellets >= 70 and (shown & 1) == 0:
            game[GAME_FRUIT_SHOWN] = shown | 1
            spawn_fruit()
        elif pellets >= 170 and (shown & 2) == 0:
            game[GAME_FRUIT_SHOWN] = shown | 2
            spawn_fruit()
        return
    fruit_x = 14 * 8 + 4                         # tile 14, 17 (below the ghost house)
    fruit_y = 17 * 8 + 4
    if state == FRUIT_VISIBLE:
        px = player[PLAYER_X]
        py = player[PLAYER_Y]
        dx = px - fruit_x
        dy = py - fruit_y
        if dx < 0:
            dx = -dx
        if dy < 0:
            dy = -dy
        if dx < 7 and dy < 7:
            game[GAME_FRUIT_STATE] = FRUIT_SCORE
            game[GAME_FRUIT_TIMER] = 1000
            level = game[GAME_LEVEL]
            score_idx = level if level < 5 else 4
            points_table = ptr16(FRUIT_POINTS)
            draw_num.add(SCORE, int(points_table[score_idx]))
            play_fruit()

def play_fruit():
    snd.play(SND_EAT_FRUIT, vol=210)

def reset_positions():
    PLAYER[PLAYER_X] = 14 * 8 + 4
    PLAYER[PLAYER_Y] = 23 * 8 + 4
    PLAYER[PLAYER_DIR] = DIR_NONE
    PLAYER[PLAYER_NEXT] = DIR_NONE
    PLAYER[PLAYER_ANIM] = 13
    PLAYER[PLAYER_AINC] = 1
    PLAYER[PLAYER_APOS] = 0
    PLAYER[PLAYER_STATE] = 1
    INPUT[I_DIR] = DIR_NONE
    GAME[GAME_MODE] = MODE_SCATTER
    GAME[GAME_MODE_TIMER] = 7000
    GAME[GAME_WAVE] = 0
    GAME[GAME_FRIGHT_TIMER] = 0
    GAME[GAME_EAT_COMBO] = 0
    GAME[GAME_DEATH_TIMER] = 0
    GAME[GAME_SCORE_TIMER] = 0
    blinky = GHOST_BLINKY * GHOST_PARAMS         # Blinky starts outside the house
    GHOSTS[blinky + GHOST_X] = 14 * 8 + 4
    GHOSTS[blinky + GHOST_Y] = 11 * 8 + 4
    GHOSTS[blinky + GHOST_DIR] = DIR_LEFT
    GHOSTS[blinky + GHOST_STATE] = MODE_SCATTER
    for i in range(1, NUM_GHOSTS):
        base = i * GHOST_PARAMS
        GHOSTS[base + GHOST_X] = (12 + i) * 8 + 4
        GHOSTS[base + GHOST_Y] = 14 * 8 + 4
        GHOSTS[base + GHOST_DIR] = DIR_UP
        GHOSTS[base + GHOST_ANIM] = 0
        GHOSTS[base + GHOST_STATE] = MODE_WAITING
    GAME[GAME_RELEASE_TIMER] = 7000
    GAME[GAME_PAUSE_TIMER] = 3000
    stop_background()

def start_new_level():
    game = GAME
    for i in range(len(MAZE_DATA)):
        MAZE_DATA[i] = MAZE_ORIGINAL[i]
    game[GAME_PELLETS_EATEN] = 0
    game[GAME_FRUIT_STATE] = FRUIT_NONE
    game[GAME_FRUIT_SHOWN] = 0
    game[GAME_LEVEL] = game[GAME_LEVEL] + 1
    reset_positions()
    snd.play(SND_START, vol=210)
    draw_hud_fruit()

@micropython.viper
def check_level_complete() -> int:
    game = ptr32(GAME)
    if game[GAME_PELLETS_EATEN] >= int(PELLET_COUNT):
        return 1
    return 0

@micropython.viper
def player_death() -> int:
    player = ptr32(PLAYER)
    game = ptr32(GAME)
    state = player[PLAYER_STATE]
    if state != 3:                               # start the death sequence
        play_fail()
        player[PLAYER_STATE] = 3
        player[PLAYER_ANIM] = 0
        player[PLAYER_APOS] = 0
        player[PLAYER_AINC] = 1
        game[GAME_DEATH_TIMER] = 1500
        update_background_sound()
        return 0
    if game[GAME_DEATH_TIMER] > 0:
        return 0
    draw_num.add(LIVES, -1)
    lives = int(draw_num.values[LIVES])
    if lives <= 0:
        return -1                                # game over
    reset_positions()
    draw_hud_lives()
    return 1

def play_fail():
    stop_background()
    snd.play(SND_FAIL, vol=210)

@micropython.viper
def get_ghost_target(ghost_idx: int, mode: int) -> int:
    player = ptr32(PLAYER)
    ghosts = ptr32(GHOSTS)
    scatter = ptr8(SCATTER_TARGETS)
    base = ghost_idx * GHOST_PARAMS
    px = player[PLAYER_X] >> 3
    py = player[PLAYER_Y] >> 3
    pdir = player[PLAYER_DIR]
    gx = ghosts[base + GHOST_X] >> 3
    gy = ghosts[base + GHOST_Y] >> 3
    target_x = 0
    target_y = 0
    if mode == MODE_SCATTER:
        target_x = int(scatter[ghost_idx * 2])
        target_y = int(scatter[ghost_idx * 2 + 1])
    elif mode == MODE_CHASE:
        if ghost_idx == GHOST_BLINKY:            # target pacman directly
            target_x = px
            target_y = py
        elif ghost_idx == GHOST_PINKY:           # 4 tiles ahead
            target_x = px
            target_y = py
            if pdir == DIR_LEFT:
                target_x = px - 4
            elif pdir == DIR_RIGHT:
                target_x = px + 4
            elif pdir == DIR_UP:
                target_x = px - 4
                target_y = py - 4
            elif pdir == DIR_DOWN:
                target_y = py + 4
        elif ghost_idx == GHOST_INKY:            # vector from blinky, doubled
            ahead_x = px
            ahead_y = py
            if pdir == DIR_LEFT:
                ahead_x = px - 2
            elif pdir == DIR_RIGHT:
                ahead_x = px + 2
            elif pdir == DIR_UP:
                ahead_x = px - 2
                ahead_y = py - 2
            elif pdir == DIR_DOWN:
                ahead_y = py + 2
            blinky_x = ghosts[GHOST_X] >> 3
            blinky_y = ghosts[GHOST_Y] >> 3
            target_x = ahead_x + (ahead_x - blinky_x)
            target_y = ahead_y + (ahead_y - blinky_y)
        elif ghost_idx == GHOST_CLYDE:           # chase if far, scatter if close
            dx = gx - px
            dy = gy - py
            dist_sq = dx * dx + dy * dy
            if dist_sq > 64:
                target_x = px
                target_y = py
            else:
                target_x = int(scatter[GHOST_CLYDE * 2])
                target_y = int(scatter[GHOST_CLYDE * 2 + 1])
    return (target_x & 0xff) | ((target_y & 0xff) << 8)

@micropython.viper
def can_move(tile_x: int, tile_y: int) -> int:
    maze = ptr8(MAZE_DATA)
    if tile_x < 0 or tile_x > 27 or tile_y < 0 or tile_y > 30:
        if tile_y == 14 and (tile_x < 0 or tile_x > 27):
            return 1                             # tunnel exception
        return 0
    if int(maze[tile_y * 28 + tile_x]) > 45:
        return 1
    return 0

@micropython.viper
def release_next_ghost():
    ghosts = ptr32(GHOSTS)
    for i in range(1, NUM_GHOSTS):               # skip blinky
        base = i * GHOST_PARAMS
        if ghosts[base + GHOST_STATE] == MODE_WAITING:
            ghosts[base + GHOST_STATE] = MODE_HOME
            ghosts[base + GHOST_DIR] = DIR_UP
            return

@micropython.viper
def move_ghosts():
    ghosts = ptr32(GHOSTS)
    game = ptr32(GAME)
    for i in range(NUM_GHOSTS):
        base = i * GHOST_PARAMS
        state = ghosts[base + GHOST_STATE]
        ticks = ghosts[base + GHOST_TICKS] + 1
        ghosts[base + GHOST_TICKS] = ticks
        if state == MODE_FRIGHT and ticks > 2:   # slow
            ghosts[base + GHOST_TICKS] = 0
        elif state == MODE_EATEN:                # fastest
            ghosts[base + GHOST_TICKS] = 0
        elif (state == MODE_HOME or state == MODE_CHASE or state == MODE_SCATTER) and ticks > 0:
            ghosts[base + GHOST_TICKS] = 0
        else:
            continue
        if state == MODE_HOME:                   # leaving the ghost house
            ghost_x = ghosts[base + GHOST_X]
            ghost_y = ghosts[base + GHOST_Y]
            exit_y = 11 * 8 + 4
            center_x = 14 * 8 + 4
            if ghost_y > exit_y:
                ghosts[base + GHOST_Y] = ghost_y - 2
            elif ghost_x < center_x:
                ghosts[base + GHOST_X] = ghost_x + 2
            elif ghost_x > center_x:
                ghosts[base + GHOST_X] = ghost_x - 2
            else:
                ghosts[base + GHOST_STATE] = game[GAME_MODE]
                ghosts[base + GHOST_DIR] = DIR_LEFT
            continue
        if state == MODE_EATEN:                  # eyes returning home
            ghost_x = ghosts[base + GHOST_X]
            ghost_y = ghosts[base + GHOST_Y]
            entrance_x = 14 * 8 + 4
            entrance_y = 11 * 8 + 4
            house_y = 14 * 8 + 4
            speed = 2
            tile_x = ghost_x >> 3
            tile_y = ghost_y >> 3
            center_x = (tile_x << 3) + 4
            center_y = (tile_y << 3) + 4
            at_center = (ghost_x == center_x) and (ghost_y == center_y)
            if ghost_y >= entrance_y and ghost_y < house_y and ghost_x == entrance_x:
                ghosts[base + GHOST_Y] = ghost_y + speed
                ghosts[base + GHOST_DIR] = DIR_DOWN
            elif ghost_y >= house_y and ghost_x == entrance_x:
                ghosts[base + GHOST_Y] = house_y
                ghosts[base + GHOST_STATE] = MODE_HOME
                update_background_sound()
            elif at_center:
                target_x = 14
                target_y = 11
                best_dir = ghosts[base + GHOST_DIR]
                best_dist = 999999
                current_dir = ghosts[base + GHOST_DIR]
                opposite = 4
                if current_dir == DIR_LEFT:
                    opposite = DIR_RIGHT
                elif current_dir == DIR_RIGHT:
                    opposite = DIR_LEFT
                elif current_dir == DIR_UP:
                    opposite = DIR_DOWN
                elif current_dir == DIR_DOWN:
                    opposite = DIR_UP
                for d in range(4):               # priority up, left, down, right
                    check_dir = d
                    if d == 0:
                        check_dir = DIR_UP
                    elif d == 1:
                        check_dir = DIR_LEFT
                    elif d == 2:
                        check_dir = DIR_DOWN
                    else:
                        check_dir = DIR_RIGHT
                    if check_dir == opposite:
                        continue
                    check_x = tile_x
                    check_y = tile_y
                    if check_dir == DIR_LEFT:
                        check_x = tile_x - 1
                    elif check_dir == DIR_RIGHT:
                        check_x = tile_x + 1
                    elif check_dir == DIR_UP:
                        check_y = tile_y - 1
                    elif check_dir == DIR_DOWN:
                        check_y = tile_y + 1
                    if not can_move(check_x, check_y):
                        continue
                    dx = check_x - target_x
                    dy = check_y - target_y
                    dist = dx * dx + dy * dy
                    if dist < best_dist:
                        best_dist = dist
                        best_dir = check_dir
                ghosts[base + GHOST_DIR] = best_dir
                if best_dir == DIR_LEFT:
                    ghosts[base + GHOST_X] = ghost_x - speed
                elif best_dir == DIR_RIGHT:
                    ghosts[base + GHOST_X] = ghost_x + speed
                elif best_dir == DIR_UP:
                    ghosts[base + GHOST_Y] = ghost_y - speed
                elif best_dir == DIR_DOWN:
                    ghosts[base + GHOST_Y] = ghost_y + speed
            else:
                current_dir = ghosts[base + GHOST_DIR]
                if current_dir == DIR_LEFT:
                    ghosts[base + GHOST_X] = ghost_x - speed
                elif current_dir == DIR_RIGHT:
                    ghosts[base + GHOST_X] = ghost_x + speed
                elif current_dir == DIR_UP:
                    ghosts[base + GHOST_Y] = ghost_y - speed
                elif current_dir == DIR_DOWN:
                    ghosts[base + GHOST_Y] = ghost_y + speed
            continue
        gx = ghosts[base + GHOST_X]
        gy = ghosts[base + GHOST_Y]
        tile_x = gx >> 3
        tile_y = gy >> 3
        center_x = (tile_x << 3) + 4
        center_y = (tile_y << 3) + 4
        at_center = (gx == center_x) and (gy == center_y)
        current_dir = ghosts[base + GHOST_DIR]
        if at_center:
            if state == MODE_FRIGHT:             # random direction
                new_dir = (gx + gy + tile_x) & 3
                tries = 0
                while tries < 4:
                    check_x = tile_x
                    check_y = tile_y
                    if new_dir == DIR_LEFT:
                        check_x = tile_x - 1
                    elif new_dir == DIR_RIGHT:
                        check_x = tile_x + 1
                    elif new_dir == DIR_UP:
                        check_y = tile_y - 1
                    elif new_dir == DIR_DOWN:
                        check_y = tile_y + 1
                    opposite = 4
                    if current_dir == DIR_LEFT:
                        opposite = DIR_RIGHT
                    elif current_dir == DIR_RIGHT:
                        opposite = DIR_LEFT
                    elif current_dir == DIR_UP:
                        opposite = DIR_DOWN
                    elif current_dir == DIR_DOWN:
                        opposite = DIR_UP
                    if new_dir != opposite and can_move(check_x, check_y):
                        break
                    new_dir = (new_dir + 1) & 3
                    tries += 1
                ghosts[base + GHOST_DIR] = new_dir
            else:                                # chase / scatter: target based
                target = int(get_ghost_target(i, state))
                target_x = target & 0xff
                target_y = (target >> 8) & 0xff
                best_dir = current_dir
                best_dist = 999999
                opposite = 4
                if current_dir == DIR_LEFT:
                    opposite = DIR_RIGHT
                elif current_dir == DIR_RIGHT:
                    opposite = DIR_LEFT
                elif current_dir == DIR_UP:
                    opposite = DIR_DOWN
                elif current_dir == DIR_DOWN:
                    opposite = DIR_UP
                for d in range(4):               # priority up, left, down, right
                    check_dir = d
                    if d == 0:
                        check_dir = DIR_UP
                    elif d == 1:
                        check_dir = DIR_LEFT
                    elif d == 2:
                        check_dir = DIR_DOWN
                    else:
                        check_dir = DIR_RIGHT
                    if check_dir == opposite:
                        continue
                    check_x = tile_x
                    check_y = tile_y
                    if check_dir == DIR_LEFT:
                        check_x = tile_x - 1
                    elif check_dir == DIR_RIGHT:
                        check_x = tile_x + 1
                    elif check_dir == DIR_UP:
                        check_y = tile_y - 1
                    elif check_dir == DIR_DOWN:
                        check_y = tile_y + 1
                    if not can_move(check_x, check_y):
                        continue
                    dx = check_x - target_x
                    dy = check_y - target_y
                    dist = dx * dx + dy * dy
                    if dist < best_dist:
                        best_dist = dist
                        best_dir = check_dir
                ghosts[base + GHOST_DIR] = best_dir
        new_x = gx
        new_y = gy
        speed = 2
        current_dir = ghosts[base + GHOST_DIR]
        if current_dir == DIR_LEFT:
            new_x = gx - speed
        elif current_dir == DIR_RIGHT:
            new_x = gx + speed
        elif current_dir == DIR_UP:
            new_y = gy - speed
        elif current_dir == DIR_DOWN:
            new_y = gy + speed
        if new_x < -8:                           # tunnel wrap
            new_x = 230
        if new_x > 230:
            new_x = -8
        ghosts[base + GHOST_X] = new_x
        ghosts[base + GHOST_Y] = new_y

@micropython.viper
def update_ghost_mode(elapsed_ms: int):
    game = ptr32(GAME)
    ghosts = ptr32(GHOSTS)
    if game[GAME_FRIGHT_TIMER] > 0:
        game[GAME_FRIGHT_TIMER] = game[GAME_FRIGHT_TIMER] - elapsed_ms
        if game[GAME_FRIGHT_TIMER] <= 0:
            game[GAME_FRIGHT_TIMER] = 0
            for i in range(NUM_GHOSTS):
                base = i * GHOST_PARAMS
                if ghosts[base + GHOST_STATE] == MODE_FRIGHT:
                    ghosts[base + GHOST_STATE] = game[GAME_MODE]
            update_background_sound()
        return
    timer = game[GAME_MODE_TIMER] - elapsed_ms
    game[GAME_MODE_TIMER] = timer
    if timer <= 0:
        wave = game[GAME_WAVE]
        if wave < 7:
            wave = wave + 1
            game[GAME_WAVE] = wave
            wave_times = ptr32(WAVE_TIMES)
            new_time = int(wave_times[wave])
            new_mode = MODE_CHASE
            if new_time == 0:                    # permanent chase
                game[GAME_MODE] = MODE_CHASE
                game[GAME_MODE_TIMER] = 999999
            else:
                if (wave & 1) == 0:
                    new_mode = MODE_SCATTER
                game[GAME_MODE] = new_mode
                game[GAME_MODE_TIMER] = new_time
            for i in range(NUM_GHOSTS):          # reverse on mode switch
                base = i * GHOST_PARAMS
                state = ghosts[base + GHOST_STATE]
                if state != MODE_HOME and state != MODE_FRIGHT and state != MODE_WAITING and state != MODE_EATEN:
                    ghosts[base + GHOST_STATE] = new_mode
                    d = ghosts[base + GHOST_DIR]
                    if d == DIR_LEFT:
                        ghosts[base + GHOST_DIR] = DIR_RIGHT
                    elif d == DIR_RIGHT:
                        ghosts[base + GHOST_DIR] = DIR_LEFT
                    elif d == DIR_UP:
                        ghosts[base + GHOST_DIR] = DIR_DOWN
                    elif d == DIR_DOWN:
                        ghosts[base + GHOST_DIR] = DIR_UP

# ── rendering ─────────────────────────────────────────────────────────────────
# Maze: full redraw every frame, 8x8 tiles blitted as 4 x 32-bit words per row.
# MAZE_X0 is even and the row stride is 640 bytes, so every write stays word aligned.
#   tile row 0    -> source rows 4..7, screen rows 0..3
#   tile rows 1-29 -> full 8 rows,     screen rows 4..235
#   tile row 30   -> source rows 0..3, screen rows 236..239
@micropython.viper
def draw_maze():
    scr = ptr32(fb2)
    spr = ptr32(MAZE_SPRITES)
    maze = ptr8(MAZE_DATA)
    game = ptr32(GAME)
    blink = game[GAME_BLINK]
    trow = 0
    while trow < MAZE_H_TILES:
        if trow == 0:
            srow = MAZE_YCROP
            nrow = TILE - MAZE_YCROP
            dsty = 0
        elif trow == MAZE_H_TILES - 1:
            srow = 0
            nrow = MAZE_YCROP
            dsty = SCREEN_H - MAZE_YCROP
        else:
            srow = 0
            nrow = TILE
            dsty = MAZE_YCROP + (trow - 1) * TILE
        mbase = trow * MAZE_W_TILES
        dbase = dsty * ROW32 + (MAZE_X0 >> 1)
        tcol = 0
        while tcol < MAZE_W_TILES:
            tile_idx = int(maze[mbase + tcol]) - 1
            if tile_idx < 0:                     # maze value 0 would index behind the sheet
                tile_idx = 0
            d = dbase + (tcol << 2)
            if tile_idx == 48 and blink:         # power pellet blink: draw black
                r = 0
                while r < nrow:
                    scr[d] = 0
                    scr[d + 1] = 0
                    scr[d + 2] = 0
                    scr[d + 3] = 0
                    d += ROW32
                    r += 1
            else:
                s = (tile_idx * TILE_SZ32) + (srow << 2)
                r = 0
                while r < nrow:
                    scr[d] = spr[s]
                    scr[d + 1] = spr[s + 1]
                    scr[d + 2] = spr[s + 2]
                    scr[d + 3] = spr[s + 3]
                    d += ROW32
                    s += TILE_W32
                    r += 1
            tcol += 1
        trow += 1

@micropython.viper
def draw_player():
    screen = ptr16(fb2)
    sprites = ptr16(CHAR_SPRITES)
    player = ptr32(PLAYER)
    sx = player[PLAYER_X] + SPR_XOFF             # left edge on screen
    sy = player[PLAYER_Y] + SPR_YOFF             # top edge on screen
    c0 = 0                                       # clip to the maze window
    c1 = 16
    if sx < MAZE_X0:
        c0 = MAZE_X0 - sx
    if sx + 16 > MAZE_X1:
        c1 = MAZE_X1 - sx
    if c1 <= c0:
        return
    r0 = 0
    r1 = 16
    if sy < 0:
        r0 = 0 - sy
    if sy + 16 > SCREEN_H:
        r1 = SCREEN_H - sy
    if r1 <= r0:
        return
    src = player[PLAYER_ANIM] << 8               # 256 px per sprite
    r = r0
    while r < r1:
        d = (sy + r) * SCREEN_W + sx
        s = src + (r << 4)
        c = c0
        while c < c1:
            color = int(sprites[s + c])
            if color:
                screen[d + c] = color
            c += 1
        r += 1

@micropython.viper
def draw_ghosts():
    screen = ptr16(fb2)
    sprites = ptr16(CHAR_SPRITES)
    ghosts = ptr32(GHOSTS)
    game = ptr32(GAME)
    fright_timer = game[GAME_FRIGHT_TIMER]
    for i in range(NUM_GHOSTS):
        base = i * GHOST_PARAMS
        ghost_dir = ghosts[base + GHOST_DIR]
        ghost_anim = ghosts[base + GHOST_ANIM]
        ghost_state = ghosts[base + GHOST_STATE]
        if ghost_state == MODE_EATEN:            # eyes only, sprites 64-67
            sprite_idx = 64 + ghost_dir
        elif ghost_state == MODE_FRIGHT:
            if fright_timer < 2000 and (fright_timer // 200) & 1:
                sprite_idx = 70 + ghost_anim     # flashing white
            else:
                sprite_idx = 68 + ghost_anim     # blue
        else:
            sprite_idx = GHOST_SPRITE_BASE + (i * 8) + (ghost_dir * 2) + ghost_anim
        sx = ghosts[base + GHOST_X] + SPR_XOFF
        sy = ghosts[base + GHOST_Y] + SPR_YOFF
        c0 = 0
        c1 = 16
        if sx < MAZE_X0:
            c0 = MAZE_X0 - sx
        if sx + 16 > MAZE_X1:
            c1 = MAZE_X1 - sx
        if c1 <= c0:
            continue
        r0 = 0
        r1 = 16
        if sy < 0:
            r0 = 0 - sy
        if sy + 16 > SCREEN_H:
            r1 = SCREEN_H - sy
        if r1 <= r0:
            continue
        src = sprite_idx << 8
        r = r0
        while r < r1:
            d = (sy + r) * SCREEN_W + sx
            s = src + (r << 4)
            c = c0
            while c < c1:
                color = int(sprites[s + c])
                if color:
                    screen[d + c] = color
                c += 1
            r += 1
    if game[GAME_SCORE_TIMER] > 0:               # 200/400/800/1600 sprite
        sx = game[GAME_SCORE_X] + SPR_XOFF
        sy = game[GAME_SCORE_Y] + SPR_YOFF
        c0 = 0
        c1 = 16
        if sx < MAZE_X0:
            c0 = MAZE_X0 - sx
        if sx + 16 > MAZE_X1:
            c1 = MAZE_X1 - sx
        if c1 <= c0:
            return
        r0 = 0
        r1 = 16
        if sy < 0:
            r0 = 0 - sy
        if sy + 16 > SCREEN_H:
            r1 = SCREEN_H - sy
        if r1 <= r0:
            return
        src = game[GAME_SCORE_IDX] << 8
        r = r0
        while r < r1:
            d = (sy + r) * SCREEN_W + sx
            s = src + (r << 4)
            c = c0
            while c < c1:
                color = int(sprites[s + c])
                if color:
                    screen[d + c] = color
                c += 1
            r += 1

@micropython.viper
def draw_fruit():
    game = ptr32(GAME)
    state = game[GAME_FRUIT_STATE]
    if state == FRUIT_NONE:
        return
    screen = ptr16(fb2)
    sprites = ptr16(CHAR_SPRITES)
    level = game[GAME_LEVEL]
    if state == FRUIT_VISIBLE:
        fruit_idx = level if level < 8 else 7
        sprite_idx = 24 + fruit_idx              # fruit sprites start at 24
    else:                                        # FRUIT_SCORE: 76..80
        score_idx = level if level < 5 else 4
        sprite_idx = 76 + score_idx
    sx = (14 * 8 + 4) + SPR_XOFF                 # tile 14, 17
    sy = (17 * 8 + 4) + SPR_YOFF
    src = sprite_idx << 8
    r = 0
    while r < 16:
        d = (sy + r) * SCREEN_W + sx
        s = src + (r << 4)
        c = 0
        while c < 16:
            color = int(sprites[s + c])
            if color:
                screen[d + c] = color
            c += 1
        r += 1

# -- HUD (side panels; the maze blit never touches x < 48 or x >= 272) ----------
LIFE_SPRITE = const(81)
LIFE_W      = const(10)
LIFE_H      = const(12)
LIFE_XOFF   = const(3)                           # crop the 16x16 sprite
LIFE_YOFF   = const(2)

@micropython.viper
def draw_hud_lives():
    screen = ptr16(fb2)
    sprites = ptr16(CHAR_SPRITES)
    lives = int(draw_num.values[LIVES])
    if lives > 6:
        lives = 6
    if lives < 0:
        lives = 0
    src = LIFE_SPRITE << 8
    i = 0
    while i < 6:                                 # clear the whole column first
        y = 0
        while y < LIFE_H:
            d = (HUD_LIVES_Y + i * LIFE_H + y) * SCREEN_W + HUD_LIVES_X
            x = 0
            while x < LIFE_W:
                screen[d + x] = 0
                x += 1
            y += 1
        i += 1
    i = 0
    while i < lives:
        y = 0
        while y < LIFE_H:
            s = src + ((y + LIFE_YOFF) << 4) + LIFE_XOFF
            d = (HUD_LIVES_Y + i * LIFE_H + y) * SCREEN_W + HUD_LIVES_X
            x = 0
            while x < LIFE_W:
                color = int(sprites[s + x])
                if color:
                    screen[d + x] = color
                x += 1
            y += 1
        i += 1

@micropython.viper
def draw_hud_fruit():
    # the last few level fruits, stacked upward from the bottom of the right panel
    screen = ptr16(fb2)
    sprites = ptr16(CHAR_SPRITES)
    game = ptr32(GAME)
    level = game[GAME_LEVEL]
    n = level + 1
    if n > 5:
        n = 5
    i = 0
    while i < 5:                                 # clear
        y = 0
        while y < 16:
            d = (HUD_FRUIT_Y - i * 16 + y) * SCREEN_W + HUD_FRUIT_X
            x = 0
            while x < 16:
                screen[d + x] = 0
                x += 1
            y += 1
        i += 1
    i = 0
    while i < n:
        fruit_idx = level - i
        if fruit_idx > 7:
            fruit_idx = 7
        if fruit_idx < 0:
            fruit_idx = 0
        src = (24 + fruit_idx) << 8
        y = 0
        while y < 16:
            s = src + (y << 4)
            d = (HUD_FRUIT_Y - i * 16 + y) * SCREEN_W + HUD_FRUIT_X
            x = 0
            while x < 16:
                color = int(sprites[s + x])
                if color:
                    screen[d + x] = color
                x += 1
            y += 1
        i += 1

def draw_hud_hiscore():
    SCREEN.fill_rect(0, HUD_HI_Y, MAZE_X0 - 2, 10, BLACK)
    draw_number_right(HUD_HI_X, HUD_HI_Y, GAME[GAME_HISCORE], WHITE, 2)

def draw_panels():
    SCREEN.fill_rect(0, 0, MAZE_X0, SCREEN_H, BLACK)
    SCREEN.fill_rect(MAZE_X1, 0, SCREEN_W - MAZE_X1, SCREEN_H, BLACK)
    draw_text(8, 4, '1UP', YELLOW, 2)
    draw_text(8, 28, 'HIGH', RED, 2)
    draw_hud_hiscore()
    draw_hud_lives()
    draw_hud_fruit()

# -- frame assembly -------------------------------------------------------------
def draw():
    draw_maze()
    SCREEN.rect(0,0,320,240,BLUE,0)
    if PLAYER[PLAYER_STATE] != 3:                # no ghosts during the death animation
        draw_ghosts()
        draw_fruit()
    if GAME[GAME_SCORE_TIMER] <= 0 and PLAYER[PLAYER_APOS] != 12:
        draw_player()
    if GAME[GAME_OVER_TIMER] > 0:
        SCREEN.text('GAME OVER', OVER_X, OVER_Y, RED)
    elif GAME[GAME_PAUSE_TIMER] > 0:
        SCREEN.text('READY!', READY_X, READY_Y, YELLOW)
    SCREEN.fill_rect(1, HUD_SCORE_Y, MAZE_X0 - 2, 10, BLACK)
    draw_num.draw(SCORE, HUD_SCORE_X, HUD_SCORE_Y)
    if SHOW_FPS:
        SCREEN.fill_rect(288, SCREEN_H - 18, 32, 18, BLACK)
        draw_num.draw(FPS_CORE0, 300, SCREEN_H - 20)
        draw_num.draw(FPS_CORE1, 300, SCREEN_H - 10)

# -- game flow ------------------------------------------------------------------
def init_game():
    draw_num.set_speed(10)
    draw_num.set(SCORE, 0)
    draw_num.set(LIVES, 5)
    PLAYER[PLAYER_X] = 14 * 8 + 4
    PLAYER[PLAYER_Y] = 23 * 8 + 4
    PLAYER[PLAYER_DIR] = DIR_NONE
    PLAYER[PLAYER_NEXT] = DIR_NONE
    PLAYER[PLAYER_ANIM] = 13
    PLAYER[PLAYER_AINC] = 1
    PLAYER[PLAYER_STATE] = 1
    GAME[GAME_MODE] = MODE_SCATTER
    GAME[GAME_MODE_TIMER] = WAVE_TIMES[0]
    GAME[GAME_WAVE] = 0
    GAME[GAME_LEVEL] = 0
    GAME[GAME_OVER_TIMER] = 0
    snd.play(SND_START, vol=210)
 

def reset_game():
    for i in range(len(MAZE_DATA)):
        MAZE_DATA[i] = MAZE_ORIGINAL[i]
    GAME[GAME_PELLETS_EATEN] = 0
    GAME[GAME_FRUIT_STATE] = FRUIT_NONE
    GAME[GAME_FRUIT_SHOWN] = 0
    GAME[GAME_LEVEL] = 0
    GAME[GAME_OVER_TIMER] = 0
    draw_num.set(SCORE, 0)
    draw_num.set(LIVES, 5)
    reset_positions()
    draw_hud_lives()
    draw_hud_fruit()
    snd.play(SND_START, vol=210)

def game_over():
    stop_background()
    GAME[GAME_OVER_TIMER] = 3000

def update():
    game = GAME
    player = PLAYER
    game[GAME_FRAME] += 1
    frame = game[GAME_FRAME]

    if player[PLAYER_STATE] == 3:
        anim_period = DEATH_FRAMES
    else:
        anim_period = ANIM_FRAMES
    if frame % anim_period == 0:
        animation()
        animate_ghosts()
    if frame % BLINK_FRAMES == 0:
        game[GAME_BLINK] ^= 1

    if game[GAME_OVER_TIMER] > 0:
        game[GAME_OVER_TIMER] -= FRAME_MS
        if game[GAME_OVER_TIMER] <= 0:
            game[GAME_OVER_TIMER] = 0
            if RESTART_ON_GAMEOVER:
                reset_game()
            else:
                game[GAME_EXIT] = 1
        return

    update_ghost_mode(FRAME_MS)
    if game[GAME_RELEASE_TIMER] > 0:
        game[GAME_RELEASE_TIMER] -= FRAME_MS
        if game[GAME_RELEASE_TIMER] <= 0:
            release_next_ghost()
            update_background_sound()
            game[GAME_RELEASE_TIMER] = 3000

    if player[PLAYER_STATE] == 3:                # dying
        if game[GAME_DEATH_TIMER] > 0:
            game[GAME_DEATH_TIMER] -= FRAME_MS
        else:
            if player_death() < 0:
                game_over()
    elif game[GAME_SCORE_TIMER] > 0:             # ghost-eaten score freeze
        game[GAME_SCORE_TIMER] -= FRAME_MS
    elif game[GAME_PAUSE_TIMER] > 0:             # READY!
        game[GAME_PAUSE_TIMER] -= FRAME_MS
    else:
        if game[GAME_PAUSE_TIMER] != 0:
            game[GAME_PAUSE_TIMER] = 0
            update_background_sound()
        move_ghosts()
        move_player()
        check_fruit()
        collision = check_collisions()
        if collision > 0:
            eat_ghost(collision - 1)
        elif collision < 0:
            player_death()
        if game[GAME_FRUIT_STATE] != FRUIT_NONE:
            game[GAME_FRUIT_TIMER] -= FRAME_MS
            if game[GAME_FRUIT_TIMER] <= 0:
                game[GAME_FRUIT_STATE] = FRUIT_NONE
        if check_level_complete():
            start_new_level()

    score = draw_num.values[SCORE]
    if score > game[GAME_HISCORE]:
        game[GAME_HISCORE] = score
        draw_hud_hiscore()

# -- cores ----------------------------------------------------------------------
@micropython.viper
def core0():
    sleep_ms(200)
    game = ptr32(GAME)
    gc.collect()
    pad_ticks = 0
    acc = 0
    prev = int(ticks_us())
    while not game[GAME_EXIT]:
        while game[GAME_RDY] and not game[GAME_EXIT]:
            sleep_ms(1)
        now = int(ticks_us())
        dt = (now - prev) & 0x3FFFFFFF
        prev = now
        if dt > 100000:
            dt = 100000
        acc += dt
        ticks = int(ticks_ms())
        if ticks - pad_ticks > 15:
            pad_ticks = ticks
            read_gamepad()
        n = 0
        while acc >= FRAME_US and n < 4:
            acc -= FRAME_US
            update()
            n += 1
        draw_num.update_all()
        draw()
        draw_num.set(FPS_CORE0, ticks)
        game[GAME_RDY] = 1                       # publish

@micropython.viper
def core1():
    sleep_ms(500)
    game = ptr32(GAME)
    while not game[GAME_EXIT]:
        ticks = int(ticks_ms())
        display.wait_frame()
        if game[GAME_RDY]:                       # skip frame if core0 ran long
            copy_fb(fb2, fb)
            game[GAME_RDY] = 0
        draw_num.set(FPS_CORE1, ticks)

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
    global snd
    load_files()
    from audio_mixer2 import Mixer
    snd = Mixer()
    init_sounds()
    fill_asm(fb2, BLACK)
    init_game()
    reset_positions()
    draw_panels()
    gc.collect()
    print('free:', gc.mem_free())
    _thread.start_new_thread(core1, ())
    core0()
    
if __name__ == '__main__':
    main()