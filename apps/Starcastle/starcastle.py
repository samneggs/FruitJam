# Star Castle — DVI port (640×480 BGR233, RP2350 HSTX)
#from dvi_rp2_hstx_frame_sync_v2 import DVI_RP2_HSTX
#import colors as rv_colors
from uctypes import addressof

import sys
# Add the directory containing your module
sys.path.append('/Starcastle')
from shared_state import fb, display, SCREEN, FLAG_ADDR

SCREEN_W     = const(640)
SCREEN_H     = const(480)

# fb  = bytearray(SCREEN_W * SCREEN_H)   # single DVI framebuffer (BGR233)
# display = DVI_RP2_HSTX()
# display.begin(fb, rv_colors.COLOR_MODE_BGR233,
#               height=SCREEN_H, width=SCREEN_W, bytes_per_pixel=1)
# FLAG_ADDR = addressof(display._frame_flag)


from gamepadfast import Gamepad
import machine, framebuf
from machine import Pin, PWM
from time import sleep, ticks_us, ticks_diff, ticks_ms, sleep_ms
import gc, _thread, array
from sys import exit
from micropython import const
from random import randint
from math import sin,cos,tan,radians,sqrt,atan2


MAXSCREEN_X  = const(640)
MAXSCREEN_Y  = const(480)
HALFSCREEN_X = const(MAXSCREEN_X//2)
HALFSCREEN_Y = const(MAXSCREEN_Y//2)
SCALE       = const(13)
NUM_STARS   = const(100)
NUM_MISSILES= const(3)

# ── BGR233 palette ────────────────────────────────────────────────────────────
BLACK       = const(0x00)
WHITE       = const(0xFF)
BLUE        = const(0b000_000_11)
SKYBLUE     = const(0b100_100_11)
YELLOW      = const(0b111_111_00)
RED         = const(0b111_000_00)
ORANGE      = const(0b111_100_00)
BACKGROUND  = const(BLACK)

# ── Star dimming levels (RGB332: rrr_ggg_bb), 4 shades of white ──────────────
# Level 0 (dim) → Level 3 (bright)
STAR_LUT    = array.array('B', (0b010_010_00,   # 0x48  ~25% white
                                0b100_100_01,   # 0x91  ~50% white
                                0b110_110_10,   # 0xDA  ~75% white
                                0b111_111_11))  # 0xFF  100% white

# ── Gamepad array layout (shared with DVI template) ──────────────────────────
GAMEPAD = array.array('i', [0, 0, 0])   # x, y, delay fire
GAMEPAD_X         = const(0)
GAMEPAD_Y         = const(1)
GAMEPAD_DELAY     = const(2)
GAMEPAD_RIGHT     = const(0b0100000)
GAMEPAD_LEFT      = const(0b0000100)
GAMEPAD_UP        = const(0b1000000)
GAMEPAD_DOWN      = const(0b0000010)
GAMEPAD_SELECT    = const(0b0000001)

PLAYER_HIT_RADIUS = const(5)
PLAYER_HIT_RADIUS2 = const(PLAYER_HIT_RADIUS * PLAYER_HIT_RADIUS)
CANNON_HIT_RADIUS = const(12)
CANNON_HIT_RADIUS2 = const(CANNON_HIT_RADIUS * CANNON_HIT_RADIUS)

PLAYER_PARAMS = const(10)
X   = const(0)
Y   = const(1)
DEG = const(2)
VX  = const(3)
VY  = const(4)
AX  = const(5)
AY  = const(6)
M   = const(7)
ANI = const(8) # animation frame
ANI_DIR = const(9)  # 0 = startup mode, -1/+1 = pulse direction

GAME_PARAMS = const(10)
FPS         = const(0)
LIVES       = const(1)
HIT_CANNON  = const(2)
DEG_CANNON  = const(3)
FIRE_CANNON = const(4)
SCORE       = const(5)
INTERMISSION= const(6)
FIRST_RUN   = const(7)
CANNON_ANI  = const(8)

SHIELD_PARAMS  = const(10)
SHIELD_LIFE    = const(3)
SHIELD_DIR     = const(4)
SHIELD_RAD     = const(5)

MISSILE_PARAMS = const(10)
MISS_LIFE      = const(2)

STARS_PARAMS = const(10)
STAR_COLOR   = const(2)
STAR_DIR     = const(3)

EXPLODE_PARAMS = const(10)
EXP_LIFE       = const(2)
EXP_COLOR      = const(5)

MINE_SHIELD    = const(5)


# ── GAME control array ────────────────────────────────────────────────────────
GAME_CTL       = array.array('i', [0] * 10)
GAME_CTL_EXIT  = const(0)

# ── Hardware init ─────────────────────────────────────────────────────────────
machine.freq(246_000_000)
machine.mem32[0x40010058] = 2 << 16   # HSTX CLK / 2
machine.mem32[0x40010054] = 1 << 11   # HSTX CLK uses SYS CLK


#SCREEN   = framebuf.FrameBuffer(fb, SCREEN_W, SCREEN_H, framebuf.GS8)

gamepad  = Gamepad()

#SHIP_POINTS = const(16)
#SHIP_DEG    = array.array('H',(0, 306, 180, 54, 0, 333, 349, 333, 189, 333, 0, 27, 11, 27, 171, 27) )
#SHIP_RADIUS = array.array('H',(0, 17, 8, 17, 0, 4, 10, 9, 12, 4, 0, 4, 10, 9, 12, 4) )

SHIP_OFFSET = const(-90)
SHIP_POINTS = const(14)
SHIP_DEG    = array.array('H',(0,262, 124, 270, 149, 270, 225, 270, 315, 270, 31, 270, 56, 278,
                                0, 262, 124, 270, 149, 270, 225, 0, 315, 270, 31, 270, 56, 278,
                                 0,262, 124, 270, 149, 270, 225, 90, 315, 270, 31, 270, 56, 278,
                                 0,262, 124, 270, 149, 270, 225, 90, 315, 270, 31, 270, 56, 278,
                                 0,262, 124, 270, 149, 270, 225, 90, 315, 270, 31, 270, 56, 278,
                                 0,262, 124, 270, 149, 270, 225, 90, 315, 270, 31, 270, 56, 278,
                                 0,262, 124, 270, 149, 270, 225, 90, 315, 270, 31, 270, 56, 278,
                                 0,262, 124, 270, 149, 270, 225, 90, 315, 270, 31, 270, 56, 278))
SHIP_RADIUS = array.array('H',(0, 14,   7,  10,  12,   4,   3,   4,   3,   4, 12,  10,  7,  14,
                               0, 14, 7, 10, 12, 4, 3, 0, 3, 4, 12, 10, 7, 14,
                               0,14, 7, 10, 12, 4, 3, 2, 3, 4, 12, 10, 7, 14,
                               0,14, 7, 10, 12, 4, 3, 4, 3, 4, 12, 10, 7, 14,
                               0,14, 7, 10, 12, 4, 3, 6, 3, 4, 12, 10, 7, 14,
                               0,14, 7, 10, 12, 4, 3, 8, 3, 4, 12, 10, 7, 14,
                               0,14, 7, 10, 12, 4, 3, 10, 3, 4, 12, 10, 7, 14,
                               0,14, 7, 10, 12, 4, 3, 12, 3, 4, 12, 10, 7, 14))

#--- Line Set Polar, 4 lines, 5 frames
CANNON_FRAMES = const(5)
CANNON_LINES = const(4)
CANNON_LINE_DEG = array.array('H',( 127, 90, 90, 53, 207, 270, 333, 270,
                                    135, 90, 90, 45, 225, 270, 315, 270,
                                    135, 0, 0, 45, 252, 270, 288, 270, 153,
                                    270, 270, 27, 256, 270, 284, 270, 180,
                                    270, 270, 0, 259, 270, 281, 270))
CANNON_LINE_RAD = array.array('H',( 10, 4, 4, 10, 4, 6, 4, 6,
                                    8, 2, 2, 8, 6, 8, 6, 8,
                                    6, 0, 0, 6, 6, 10, 6, 10,
                                    4, 2, 2, 4, 8, 12, 8, 12,
                                    4, 4, 4, 4, 10, 14, 10, 14))

CANNON_OFFSET = const(-90)
CANNON_POINTS = const(15)
CANNON_DEG =    array.array('H',(261, 180, 146, 191, 243, 121, 90, 59, 297, 349, 34, 0, 279, 270, 261))
CANNON_RADIUS = array.array('H',(12, 4, 14, 10, 9, 12, 6, 12, 9, 10, 14, 4, 12, 14, 12))


char_map=array.array('b',(
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

@micropython.viper
def show_num_viper(num:int,x_offset:int,y_offset:int,color:int):
    char_ptr = ptr8(char_map)
    screen_ptr = ptr8(SCREEN)
    size = 1 # 1,2,3
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
                    addr = size*y*MAXSCREEN_X+x-(char*8)+offset
                    screen_ptr[addr] = color
                    if size>1:
                        screen_ptr[MAXSCREEN_X+addr] = color
                        if size>2:
                            screen_ptr[2*MAXSCREEN_X+addr] = color
        char += 1
        
def init_imath():
    global ISIN, ICOS
    scale = 1 << SCALE
    ISIN = array.array('i', (int(sin(radians(i)) * scale) for i in range(360)))
    ICOS = array.array('i', (int(cos(radians(i)) * scale) for i in range(360)))

# ── Wait-for-VBlank + Fast fill (asm_thumb, 192 bytes per iteration) ──────────
@micropython.asm_thumb
def fill_asm(r0, r1, r2):   # (buffer_addr, 8-bit_color, flag_addr)
    # ── Phase 1: spin until DMA frame marker sets _frame_flag[0] != 0 ────────
    label(WAIT)
    ldr(r3, [r2, 0])        # r3 = _frame_flag[0]
    cmp(r3, 0)
    beq(WAIT)               # keep spinning while flag == 0
    mov(r3, 0)
    str(r3, [r2, 0])        # clear flag (re-arm for next frame)
    # ── Phase 2: expand 8-bit color to 32-bit fill word ──────────────────────
    mov(r3, r1)             # r3 = color
    lsl(r2, r1, 8)
    orr(r3, r2)             # r3 = color | (color << 8)
    lsl(r2, r3, 16)
    orr(r3, r2)             # r3 = 32-bit fill word (color replicated 4x)
    mov(r1, r0)             # r1 = write ptr (STM base)
    mov(r2, r3)
    mov(r4, r3)
    mov(r5, r3)
    mov(r6, r3)
    mov(r7, r3)
    movwt(r0, SCREEN_W * SCREEN_H // 192)
    # ── Phase 3: store 192 bytes per iteration via 8x STM r1!,{r2-r7} ────────
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
        

@micropython.viper
def read_pot():
    isin   = ptr32(ISIN)
    icos   = ptr32(ICOS)
    player = ptr32(PLAYER)
    g_pad  = ptr32(GAMEPAD)
    ctrl   = ptr32(GAME_CTL)
    game   = ptr32(GAME)
    if game[INTERMISSION] > 0: return
    gamepad.read()
    buttons = int(gamepad.buttons)
    if not (buttons & GAMEPAD_DOWN):  # DOWN = restart
        restart()
    if not (buttons & GAMEPAD_SELECT):  # SELECT = quit
        ctrl[GAME_CTL_EXIT] = 1
    x_inc = int(gamepad.x) >> 6 
    y_inc = int(gamepad.y) >> 5
    if (g_pad[GAMEPAD_DELAY] == 1) and (not (buttons & GAMEPAD_RIGHT)): # RIGHT = fire
        init_missile()
        g_pad[GAMEPAD_DELAY] = 0
    # Clamp analogue deadband (same threshold, values from gamepad.x/y)
    x_dead = x_inc if x_inc >= 0 else (-1 * x_inc)
    if x_dead < 2: x_inc = 0
    if y_inc > 2: y_inc = 0 # no reverse
    g_pad[GAMEPAD_X] = x_inc
    g_pad[GAMEPAD_Y] = y_inc
    player[DEG] += x_inc
    if player[DEG] >= 360: player[DEG] -= 360
    if player[DEG] < 0   : player[DEG] += 360
    deg = int(player[DEG])
    x   = (600*y_inc * int(icos[deg]))>>SCALE
    y   = (600*y_inc * int(isin[deg]))>>SCALE
    player[VX] += x
    player[VY] += y

def init_player():
    global PLAYER , SHIP_COORDS, MISSILES, CANNON_COORDS, CANNON_LINE_COORDS
    PLAYER = array.array('i',0 for _ in range(PLAYER_PARAMS))
    SHIP_COORDS = array.array('i',0 for _ in range(SHIP_POINTS*2))
    CANNON_COORDS = array.array('i',0 for _ in range((CANNON_POINTS)*2))
    CANNON_LINE_COORDS = array.array('i',0 for _ in range(CANNON_LINES * CANNON_FRAMES *2))
    MISSILES = array.array('i',0 for _ in range(MISSILE_PARAMS*NUM_MISSILES))
    player_reset()
    
    global SHIP_MV
    SHIP_MV = memoryview(SHIP_COORDS)


@micropython.viper
def player_reset():
    game = ptr32(GAME)
    miss = ptr32(MISSILES)
    player = ptr32(PLAYER)
    cannon_misl = ptr32(CANNON_MISL)
    player[X]  = 10<<SCALE
    player[Y]  = 10<<SCALE
    player[VX] = 0
    player[VY] = 0
    player[DEG]= 220
    player[ANI] = 0
    player[ANI_DIR] = 0
    if game[FIRST_RUN] > 0:
        game[INTERMISSION] = 200

@micropython.viper
def clear_missiles():
    miss = ptr32(MISSILES)
    cannon_misl = ptr32(CANNON_MISL)
    for index in range(NUM_MISSILES): # clear missiles
        i = index * MISSILE_PARAMS
        miss[i + MISS_LIFE] = 0
    cannon_misl[MISS_LIFE] = 0 # clear cannon
    
    
def init_game():
    global GAME, SHIELD, STARS, COLORS, EXPLODE, FPS_ARRY, CANNON_MISL, MINES
    global TEXT_SCORE
    GAME   = array.array('i',0 for _ in range(GAME_PARAMS))
    SHIELD = array.array('i',0 for _ in range(SHIELD_PARAMS*12*3))
    STARS  = array.array('i',0 for _ in range(STARS_PARAMS*NUM_STARS))
    COLORS = array.array('H',(YELLOW,ORANGE,RED))
    EXPLODE = array.array('i',0 for _ in range(EXPLODE_PARAMS*20))
    CANNON_MISL = array.array('i',0 for _ in range(MISSILE_PARAMS))
    MINES   = array.array('i',0 for _ in range(MISSILE_PARAMS*3))
    FPS_ARRY = bytearray(100)
    GAME[FPS] = 0
    GAME[LIVES]  = 3
    TEXT_SCORE = 'SCORE'
    for index in range(NUM_STARS):
        i = index * STARS_PARAMS
        STARS[i + X] = randint(0,MAXSCREEN_X-1)
        STARS[i + Y] = randint(0,MAXSCREEN_Y-1)
        STARS[i + STAR_COLOR] = randint(0,3)
        if STARS[i + STAR_COLOR] > 1:
            STARS[i + STAR_DIR] = -1
        else:
            STARS[i + STAR_DIR] = 1

def restart():
    GAME[LIVES] = 3
    GAME[SCORE] = 0
    init_shields()
    init_mine()
    player_reset()
    
            
def init_shields():
    global SHIELD
    for index in range(12*3):
        i = index * SHIELD_PARAMS
        deg = (index % 12) * 30
        radius = 40 + (20 * (index//12))
        SHIELD[i + X] = int(cos(radians(deg))*radius + MAXSCREEN_X//2)
        SHIELD[i + Y] = int(sin(radians(deg))*radius + MAXSCREEN_Y//2)
        SHIELD[i + DEG] = deg
        SHIELD[i + SHIELD_LIFE] = 2
        SHIELD[i + SHIELD_DIR]  = -5
        if index>11 and index<24:
            SHIELD[i + SHIELD_DIR] = 5    
        SHIELD[i + SHIELD_RAD] = radius

@micropython.viper
def init_missile():
    miss = ptr32(MISSILES)
    player = ptr32(PLAYER)
    isin = ptr32(ISIN)
    icos = ptr32(ICOS)
    game = ptr32(GAME)
    if game[LIVES] < 1: return
    x  = player[X]
    y  = player[Y]
    vx = player[VX]
    vy = player[VY]
    deg = player[DEG] + 180
    if deg >= 360: deg -= 360
    for index in range(NUM_MISSILES):
        i = index * MISSILE_PARAMS
        if miss[i + MISS_LIFE] == 0:
            miss[i + MISS_LIFE] = 2000
            miss[i + X] = icos[deg]*10 + x
            miss[i + Y] = isin[deg]*10 + y
            miss[i + VX] = ((icos[deg])>>2) + int(randint(-100,100)) 
            miss[i + VY] = ((isin[deg])>>2) + int(randint(-100,100))
            snd.play(fire, vol=220)
            return        


@micropython.viper
def init_cannon():
    miss = ptr32(CANNON_MISL)
    game = ptr32(GAME)
    isin = ptr32(ISIN)
    icos = ptr32(ICOS)
    if game[LIVES] < 1 or game[HIT_CANNON] > 0 or game[INTERMISSION] > 0:
        return
    x  = HALFSCREEN_X << SCALE
    y  = HALFSCREEN_Y << SCALE
    deg = game[DEG_CANNON] + 180
    if deg >= 360: deg -= 360
    if miss[MISS_LIFE] == 0:
        miss[MISS_LIFE] = 2000
        miss[X] = icos[deg]*10 + x
        miss[Y] = isin[deg]*10 + y
        miss[VX] = (icos[deg])>>2
        miss[VY] = (isin[deg])>>2
    snd.play(cfire, vol=220)

@micropython.viper
def init_mine():
    mine = ptr32(MINES)
    mine[(0*MISSILE_PARAMS) + MINE_SHIELD] = 1
    mine[(1*MISSILE_PARAMS) + MINE_SHIELD] = 4
    mine[(2*MISSILE_PARAMS) + MINE_SHIELD] = 8

@micropython.viper
def init_explode(x:int,y:int,life:int,speed:int):
    exp = ptr32(EXPLODE)
    game = ptr32(GAME)
    exp[X] = x
    exp[Y] = y
    for index in range(1,20):
        i = index * EXPLODE_PARAMS
        exp[i + X] = x
        exp[i + Y] = y
        exp[i + VX] = int(randint(-1*speed,speed))
        exp[i + VY] = int(randint(-1*speed,speed))
        exp[i + EXP_LIFE] = life
        exp[i + EXP_COLOR] = 0


@micropython.viper
def move_mine():
    shield = ptr32(SHIELD)
    mine = ptr32(MINES)
    isin = ptr32(ISIN)
    icos = ptr32(ICOS)
    player = ptr32(PLAYER)
    game = ptr32(GAME)
    for index in range(3):
        i  = index * MISSILE_PARAMS
        shield_pos = mine[i + MINE_SHIELD]
        shield_index = shield_pos * SHIELD_PARAMS
        if shield_pos < 24 and shield[shield_index + DEG] == shield[(12*SHIELD_PARAMS+shield_index) + DEG] and int(randint(1,3))==3:
            shield_pos += 12    
            mine[i + MINE_SHIELD] = shield_pos
            if shield[(12*SHIELD_PARAMS+shield_index) + SHIELD_LIFE] == 0:
                pass
                #mine[i + MINE_SHIELD] = 36 #    
        if shield_pos < 36:
            shield_index =  shield_pos * SHIELD_PARAMS
            mine[i + X] = shield[shield_index + X] << SCALE
            mine[i + Y] = shield[shield_index + Y] << SCALE
        if shield_pos > 23 and shield_pos < 36 and int(randint(1,20))==20:  # launch mine to space
            mine[i + MINE_SHIELD] = 36
            deg = shield[shield_index + DEG]
            rvx = -(icos[deg]>>1)
            rvy = -(isin[deg]>>1)
            tvx = -(isin[deg]<<2)
            tvy =   icos[deg]<<2
            mine[i + VX] = rvx + tvx
            mine[i + VY] = rvy + tvy
            mine[i + MISS_LIFE] = 400
            #snd.play(star, vol=220, loop=True)
        if mine[i + MINE_SHIELD] == 36 and mine[i + MISS_LIFE] > 0 :
            mine[i + MISS_LIFE] -= 1
            if mine[i + MISS_LIFE] < 1:
                mine[(index*MISSILE_PARAMS) + MINE_SHIELD] = 1
                continue
            mine[i + X] += mine[i + VX]
            mine[i + Y] += mine[i + VY]
            if mine[i + X] < player[X] and mine[i + VX] <  20000: mine[i + VX] += 1000 # 400
            if mine[i + X] > player[X] and mine[i + VX] > -20000: mine[i + VX] -= 1000
            if mine[i + Y] < player[Y] and mine[i + VY] <  20000: mine[i + VY] += 1000
            if mine[i + Y] > player[Y] and mine[i + VY] > -20000: mine[i + VY] -= 1000
            # --- ADD: bleed off tangential velocity (breaks orbit, preserves loops) ---
            mine[i + VX] -= mine[i + VX] >> 5   # ~3% drag per tick
            mine[i + VY] -= mine[i + VY] >> 5
            if mine[i + X] < 0 or mine[i + X] > MAXSCREEN_X<<SCALE or mine[i + Y] < 0 or mine[i + Y] > MAXSCREEN_Y<<SCALE:
                mine[i + MINE_SHIELD] = 1
            if game[LIVES] < 1: continue

            mx = mine[i + X] >> SCALE
            my = mine[i + Y] >> SCALE
            px = player[X] >> SCALE
            py = player[Y] >> SCALE
            dx = mx - px
            dy = my - py
            if dx * dx + dy * dy <= PLAYER_HIT_RADIUS2:
            #if mine[i + X] > player[X] - 20000 and mine[i + X] < player[X] + 20000 and mine[i + Y] > player[Y]-20000 and mine[i + Y] < player[Y]+20000:
                init_explode(player[X],player[Y],30,10000)
                snd.play(lexplode, vol=220)
                game[LIVES] -= 1
                init_mine()
                clear_missiles()
                player_reset()
                game[INTERMISSION] = 250

@micropython.viper
def move_missile():
    miss = ptr32(MISSILES)
    isin = ptr32(ISIN)
    icos = ptr32(ICOS)
    shield = ptr32(SHIELD)
    game = ptr32(GAME)
    mine = ptr32(MINES)
    cannon_misl = ptr32(CANNON_MISL)
    for index in range(NUM_MISSILES):  # calc missile positions
        i = index * MISSILE_PARAMS
        if miss[i + MISS_LIFE] > 0:
            miss[i + MISS_LIFE] -= 1
            x = miss[i + X]
            y = miss[i + Y]
            x += miss[i + VX]
            y += miss[i + VY]
            if x < 0 or y < 0 or x > MAXSCREEN_X<<SCALE or y > MAXSCREEN_Y<<SCALE:
                miss[i + MISS_LIFE] = 0
                continue
            miss[i + X] = x
            miss[i + Y] = y
            x >>= SCALE
            y >>= SCALE
            dx = x - HALFSCREEN_X
            dy = y - HALFSCREEN_Y
            if dx * dx + dy * dy <= CANNON_HIT_RADIUS2:      # hit cannon
            #if x > HALFSCREEN_X-3 and x < HALFSCREEN_X+3 and y > HALFSCREEN_Y-4 and y < HALFSCREEN_Y+4:  # hit cannon
                miss[i + MISS_LIFE] = 0
                init_explode(miss[i + X],miss[i + Y],100,10000)
                snd.play(lexplode, vol=220)
                game[HIT_CANNON] = 150 #100
                player_reset()
                clear_missiles()
                game[SCORE] += 1440
                game[LIVES] += 1
                game[INTERMISSION] = 250
                return                   
            for index2 in range(12*3):        # missile to shield collision
                i2 = index2 * SHIELD_PARAMS
                if shield[i2 + SHIELD_LIFE] > 0:
                    s_x1 = shield[i2 + X]
                    s_y1 = shield[i2 + Y]
                    if index2 == 11 or index2 == 23 or index2 == 35:
                        s_x2 = shield[(index2-11) * SHIELD_PARAMS  + X ]
                        s_y2 = shield[(index2-11) * SHIELD_PARAMS  + Y ]
                    else:
                        s_x2 = shield[i2 + X + SHIELD_PARAMS]
                        s_y2 = shield[i2 + Y + SHIELD_PARAMS]                       
                    if s_x1 > s_x2: s_x1,s_x2 = s_x2,s_x1
                    if s_y1 > s_y2: s_y1,s_y2 = s_y2,s_y1
                    if x > s_x1-1 and x < s_x2+1 and y > s_y1-1 and y < s_y2+1:  # hit shield
                        shield[i2 + SHIELD_LIFE] -= 1
                        miss[i + MISS_LIFE] = 0
                        if shield[i2 + SHIELD_LIFE] == 0:
                            init_explode(miss[i + X],miss[i + Y],20,5000) # 10, 10000
                            snd.play(sexplode, vol=220)
                            score(index2)
                            break
            for index3 in range(3):
                i  = index3 * MISSILE_PARAMS
                mine_x = mine[i + X] >> SCALE
                mine_y = mine[i + Y] >> SCALE
                if x > mine_x-3 and x < mine_x+3 and y > mine_y-3 and y < mine_y+3:  # hit mine
                    mine[i + MINE_SHIELD] = 1
                    init_explode(mine[i + X],mine[i + Y],20,5000)
                    snd.play(sexplode, vol=220)


@micropython.viper
def score(shield:int):
    game = ptr32(GAME)
    if shield > 23:
        game[SCORE] += 10
    elif shield < 12:
        game[SCORE] += 30                         
    else:
        game[SCORE] += 20    

@micropython.viper
def move_cannon_missile():
    miss = ptr32(CANNON_MISL)
    game = ptr32(GAME)
    player = ptr32(PLAYER)
    player_x = player[X]>>SCALE
    player_y = player[Y]>>SCALE
    if game[INTERMISSION] > 0:
        miss[MISS_LIFE] = 0
        return
    if miss[MISS_LIFE] > 0:
        miss[MISS_LIFE] -= 1
        x = miss[X]
        y = miss[Y]
        x += miss[VX]
        y += miss[VY]
        if x < 0 or y < 0 or x > MAXSCREEN_X<<SCALE or y > MAXSCREEN_Y<<SCALE:
            miss[MISS_LIFE] = 0
            return
        miss[X] = x
        miss[Y] = y
        x >>= SCALE
        y >>= SCALE
        if x > player_x-4 and x < player_x+4 and y > player_y-4 and y < player_y+4:  # hit player
            miss[MISS_LIFE] = 0
            init_explode(miss[X],miss[Y],30,10000)
            snd.play(lexplode, vol=220)
            game[LIVES] -= 1
            clear_missiles()
            player_reset()
            game[INTERMISSION] = 250
            

@micropython.viper
def move_cannon():
    game   = ptr32(GAME)
    player = ptr32(PLAYER)
    isin   = ptr32(ISIN)
    icos   = ptr32(ICOS)
    # Vector from cannon center to player.
    x = (player[X] >> SCALE) - HALFSCREEN_X
    y = (player[Y] >> SCALE) - HALFSCREEN_Y
    deg = game[DEG_CANNON]
    # DEG_CANNON is the BACK angle. init_cannon() fires at DEG_CANNON + 180.
    # So the cannon is correctly locked on when dot < 0.
    cross = x * isin[deg] - y * icos[deg]
    dot   = x * icos[deg] + y * isin[deg]
    abs_x = x
    if abs_x < 0: abs_x = -abs_x
    abs_y = y
    if abs_y < 0: abs_y = -abs_y
    # About a half-degree aiming deadband. 
    deadband = (abs_x + abs_y) << 6
    # Already aimed close enough and on the correct 180-degree side.
    if dot < 0 and cross < deadband and cross > -deadband:
        game[DEG_CANNON] = deg
        game[FIRE_CANNON] = 1
        return
    else:
        game[FIRE_CANNON] = 0
    if cross > 0:
        deg += 1
    elif cross < 0:
        deg -= 1
    else:
        # exactly pointed the wrong way; pick a direction to break the tie
        deg += 1
    if deg > 359: deg -= 360
    if deg < 0:   deg += 360
    game[DEG_CANNON] = deg
    
@micropython.viper
def move():
    player = ptr32(PLAYER)
    ship_deg    = ptr16(SHIP_DEG)
    ship_radius = ptr16(SHIP_RADIUS)
    cannon_deg    = ptr16(CANNON_DEG)
    cannon_radius = ptr16(CANNON_RADIUS)   
    coords = ptr32(SHIP_COORDS)
    cannon = ptr32(CANNON_COORDS)
    cannon_line_coords = ptr32(CANNON_LINE_COORDS)
    cannon_lines_deg = ptr16(CANNON_LINE_DEG)
    cannon_lines_rad = ptr16(CANNON_LINE_RAD)
    isin   = ptr32(ISIN)
    icos   = ptr32(ICOS)
    shield = ptr32(SHIELD)
    stars  = ptr32(STARS)
    game   = ptr32(GAME)
    cannon_ani = game[CANNON_ANI]
    cannon_size = 3
    ship_size = 2
    player[X] += player[VX]
    player[Y] += player[VY]
    player[VX] -= player[VX]>>3  # auto slow to stop
    player[VY] -= player[VY]>>3    
    if player[X] < 0: player[X] = MAXSCREEN_X<<SCALE
    if player[Y] < 0: player[Y] = MAXSCREEN_Y<<SCALE
    if player[X] > MAXSCREEN_X<<SCALE : player[X] = 0
    if player[Y] > MAXSCREEN_Y<<SCALE : player[Y] = 0
    x = player[X]
    y = player[Y]
    player_offset = player[ANI] * SHIP_POINTS
    for index in range(SHIP_POINTS):       # calc ship points
        i = index * 2
        shipdeg = ship_deg[index + player_offset]
        shipradius = ship_radius[index + player_offset]
        pt_deg = shipdeg + player[DEG] + SHIP_OFFSET
        if pt_deg >= 360: pt_deg -= 360
        if pt_deg < 0   : pt_deg += 360
        coords[i]   = ((shipradius * ship_size * icos[pt_deg])>>14)
        coords[i+1] = ((shipradius * ship_size * isin[pt_deg])>>14) 
    for index in range(CANNON_POINTS):       # calc cannon points
        i = index * 2
        pt_deg = cannon_deg[index] + game[DEG_CANNON] + CANNON_OFFSET
        #pt_deg = cannon_deg[index] +90
        if pt_deg >= 360: pt_deg -= 360
        if pt_deg < 0   : pt_deg += 360
        cannon[i]   = ((cannon_radius[index] * cannon_size * icos[pt_deg])>>14)
        cannon[i+1] = ((cannon_radius[index] * cannon_size * isin[pt_deg])>>14)
    for index in range(CANNON_LINES * 2): # calc cannon lines (second animation)
        i = index * 2
        frame = index + (cannon_ani*8)
        pt_deg = cannon_lines_deg[frame] + game[DEG_CANNON] + CANNON_OFFSET
        if pt_deg >= 360: pt_deg -= 360
        if pt_deg < 0   : pt_deg += 360
        cannon_line_coords[i]   = ((cannon_lines_rad[frame] * cannon_size * icos[pt_deg])>>14)        
        cannon_line_coords[i+1] = ((cannon_lines_rad[frame] * cannon_size * isin[pt_deg])>>14)
    for index in range(12*3):              # calc shield points
        i = index * SHIELD_PARAMS
        shield_rad = shield[i + SHIELD_RAD]
        shield_deg = shield[i + DEG] + shield[i + SHIELD_DIR]
        if shield_deg <    0: shield_deg += 360
        if shield_deg >= 360: shield_deg -= 360
        shield[i + DEG] = shield_deg
        shield[i + X] = ((icos[shield_deg]*shield_rad)>>SCALE) + MAXSCREEN_X//2
        shield[i + Y] = ((isin[shield_deg]*shield_rad)>>SCALE) + MAXSCREEN_Y//2
    x >>= SCALE
    y >>= SCALE
    for index in range(12*3):             # ship/shield collision
        i = index * SHIELD_PARAMS        
        if shield[i + SHIELD_LIFE] > 0:
            s_x1 = shield[i + X]
            s_y1 = shield[i + Y]
            if index == 11 or index == 23 or index == 35:
                s_x2 = shield[(index-11) * SHIELD_PARAMS  + X ]
                s_y2 = shield[(index-11) * SHIELD_PARAMS  + Y ]
            else:
                s_x2 = shield[i + X + SHIELD_PARAMS]
                s_y2 = shield[i + Y + SHIELD_PARAMS]              
            if s_x1 > s_x2: s_x1,s_x2 = s_x2,s_x1
            if s_y1 > s_y2: s_y1,s_y2 = s_y2,s_y1
            if x > s_x1 and x < s_x2 and y > s_y1 and y < s_y2:  # hit
                player_deg = player[DEG] + 180
                if player_deg >= 360: player_deg -= 360
                if player_deg < 0   : player_deg += 360
                player[DEG] = player_deg
                player[VX] *= -1
                player[VY] *= -1
                player[X] += player[VX]<<3
                player[Y] += player[VY]<<3

@micropython.viper
def check_cannon():
    shield = ptr32(SHIELD)
    game   = ptr32(GAME)
    cannon_deg = game[DEG_CANNON]
    first  = 0
    second = 0
    third  = 0
    for index in range(0,36): # 0-12, 13-24, 25-36
        i = index * SHIELD_PARAMS
        deg = (index % 12) * 30
        shield_deg  = shield[i + DEG] + 180
        if shield_deg > 359: shield_deg -= 360
        shield_life = shield[i + SHIELD_LIFE]
        if shield_life > 0 or shield_deg > cannon_deg + 15 or shield_deg < cannon_deg - 15: continue
        if index < 12:
            first = 1
        elif index > 24:
            third = 1
        else:
            second = 1
    if first+second+third == 3 and game[FIRE_CANNON] == 1:
        #game[FIRE_CANNON] = 1
        init_cannon()

@micropython.viper
def twinkle_stars():
    stars  = ptr32(STARS)
    for index in range(NUM_STARS):              
        i = index * STARS_PARAMS
        dim = int(stars[i + STAR_COLOR])
        dim += stars[i + STAR_DIR]
        if dim > 3 or dim < 0:
            stars[i + STAR_DIR] *= -1
        else:
            stars[i + STAR_COLOR] = dim
     

    
@micropython.viper
def explode():
    exp = ptr32(EXPLODE)
    game = ptr32(GAME)
    if game[HIT_CANNON] > 0:
       game[HIT_CANNON] -= 1
       if game[HIT_CANNON] == 0:
           init_shields()
           init_mine()
    for index in range(1,20):
        i = index * EXPLODE_PARAMS
        if exp[i + EXP_LIFE] > 0:
            exp[i + X] += exp[i + VX]
            exp[i + Y] += exp[i + VY]
            exp[i + EXP_LIFE] -= 1

@micropython.viper
def draw():
    game   = ptr32(GAME)
    player = ptr32(PLAYER)
    coords = ptr32(SHIP_COORDS)
    cannon_lines_coords = ptr32(CANNON_LINE_COORDS)
    can_miss = ptr32(CANNON_MISL)
    stars  = ptr32(STARS)
    shield = ptr32(SHIELD)
    colors = ptr16(COLORS)
    miss   = ptr32(MISSILES)
    exp    = ptr32(EXPLODE)
    mine   = ptr32(MINES)
    star_lut = ptr8(STAR_LUT)
    screen  = ptr8(fb)
    fill_asm(fb, BACKGROUND,FLAG_ADDR)
    for index in range(NUM_STARS):                 # draw stars (white, 4 dim levels)
        i = index * STARS_PARAMS
        addr = stars[i+Y] * MAXSCREEN_X + stars[i+X]
        screen[addr] = star_lut[stars[i + STAR_COLOR]]
        
    if 0 < game[INTERMISSION]:
        game[INTERMISSION] -= 1
        if game[INTERMISSION] < 170:
            hershey.text('   SCORE',240,40,900,SKYBLUE)
            hershey.number(GAME[SCORE],325,75,1000,SKYBLUE)
            hershey.text('SHIPS LEFT',255,125,900,SKYBLUE)
            hershey.number(GAME[LIVES],325,170,1000,SKYBLUE)
            return
        
    if game[HIT_CANNON] == 0:
        for index in range(12*3):                      # draw shield
            i = index * SHIELD_PARAMS
            if shield[i + SHIELD_LIFE] > 0:
                x1 = shield[i + X]
                y1 = shield[i + Y]
                if index == 11 or index == 23 or index == 35:
                    x2 = shield[(index-11) * SHIELD_PARAMS  + X ]
                    y2 = shield[(index-11) * SHIELD_PARAMS  + Y ]
                else:
                    x2 = shield[i + X + SHIELD_PARAMS]
                    y2 = shield[i + Y + SHIELD_PARAMS]
                SCREEN.line(x1,y1,x2,y2,colors[index//12])
    for index in range(NUM_MISSILES):                 # draw missiles
        i = index * MISSILE_PARAMS
        if miss[i + MISS_LIFE] > 0:
            addr = ((miss[i + Y]>>SCALE) * MAXSCREEN_X + (miss[i + X]>>SCALE))
            screen[addr] = WHITE 
    if can_miss[MISS_LIFE] > 0:                       # draw cannon missile
        cannon_misl_x = can_miss[X]>>SCALE
        cannon_misl_y = can_miss[Y]>>SCALE
        color = colors[int(randint(0,2))]
        for i in range(10):
            x = int(randint(-8,8))
            y = int(randint(-8,8))
            SCREEN.line(cannon_misl_x,cannon_misl_y, cannon_misl_x+x,cannon_misl_y+y,color)
    e_x = exp[X]
    e_y = exp[Y]
    for index in range(1,20):                          # draw explosion    
        i = index * EXPLODE_PARAMS
        if exp[i + EXP_LIFE] > 0:
            SCREEN.line(e_x>>SCALE,e_y>>SCALE,exp[i + X]>>SCALE,exp[i + Y]>>SCALE,colors[(exp[i + EXP_LIFE]//12) % 3])
    for index in range(3):
        i = index * MISSILE_PARAMS                     # draw mines   
        mine_x = mine[i + X] >> SCALE
        mine_y = mine[i + Y] >> SCALE
        for i in range(10):
            x = int(randint(-3,3))
            y = int(randint(-3,3))
            SCREEN.line(mine_x,mine_y, mine_x+x,mine_y+y,0b011_011_11)              
    if game[LIVES] == 0:
        hershey.text('GAME OVER',240,120,900,SKYBLUE)
    x = player[X] >> SCALE
    y = player[Y] >> SCALE
    if game[LIVES] > 0 and game[INTERMISSION] == 0:
        SCREEN.poly(x,y,SHIP_COORDS,SKYBLUE)                      # draw ship         
    if game[HIT_CANNON] == 0:
        SCREEN.poly(HALFSCREEN_X,HALFSCREEN_Y,CANNON_COORDS,YELLOW) # draw cannon
        for i in range(0,CANNON_LINES * 4,4):
            x1 = cannon_lines_coords[i] + HALFSCREEN_X
            y1 = cannon_lines_coords[i+1] + HALFSCREEN_Y
            x2 = cannon_lines_coords[i+2] + HALFSCREEN_X
            y2 = cannon_lines_coords[i+3] + HALFSCREEN_Y
            SCREEN.line(x1,y1,x2,y2,YELLOW)
        
    if game[FIRST_RUN] < 0:
        game[FIRST_RUN] += 1
        print('FPS:',game[FPS])


@micropython.viper
def core0():
    init_imath()
    init_game()
    init_shields()
    init_player()
    init_mine()
    game  = ptr32(GAME)
    g_pad = ptr32(GAMEPAD)
    ctrl  = ptr32(GAME_CTL)
    player = ptr32(PLAYER)
    pot_ticks  = 0
    move_ticks = 0
    init_miss_ticks = 0
    #move_miss_ticks = 0
    twinkle_ticks = 0
    explode_ticks = 0
    cannon_ticks = 0
    animation_ticks = 0
    animation_ticks2 = 0
    while not ctrl[GAME_CTL_EXIT]:
        sleep(0.001)
        gticks = int(ticks_ms())
        if gticks-pot_ticks>30:
            pot_ticks = int(ticks_ms())
            read_pot()
        if gticks-move_ticks>40: #40
            move_ticks = gticks           
            move()
            move_mine()
            check_cannon()
        if gticks-init_miss_ticks>100:  #300
            init_miss_ticks = gticks           
            g_pad[GAMEPAD_DELAY] = 1
        if gticks-twinkle_ticks>1000: #1000
            twinkle_ticks = gticks
            twinkle_stars()
        if gticks-explode_ticks>10:
            explode_ticks = gticks
            explode()
        if gticks-cannon_ticks>15:
            cannon_ticks = gticks           
            move_cannon()
        if gticks-animation_ticks2>50:
            animation_ticks2 = gticks
            game[CANNON_ANI] = (game[CANNON_ANI]+1) % 5
        if gticks-animation_ticks>20:
            animation_ticks = gticks
            if g_pad[GAMEPAD_Y] < 0:
                if player[ANI_DIR] == 0:
                    if player[ANI] < 7:
                        player[ANI] += 1
                    if player[ANI] == 7:
                        player[ANI_DIR] = -1
                else:
                    player[ANI] += player[ANI_DIR]

                    if player[ANI] <= 4: #5
                        player[ANI] = 4
                        player[ANI_DIR] = 1
                    elif player[ANI] >= 7:
                        player[ANI] = 7
                        player[ANI_DIR] = -1
            else:
                if player[ANI] > 0:
                    player[ANI] -= 1
                player[ANI_DIR] = 0
        move_missile()
        move_cannon_missile()
    ctrl[GAME_CTL_EXIT] = 1
    print('core0 done')

# ── Core 1: draw loop ─────────────────────────────────────────────────────────
@micropython.viper
def core1():
    sleep_ms(1000)
    ctrl = ptr32(GAME_CTL)
    game = ptr32(GAME)
    while not ctrl[GAME_CTL_EXIT]:
        #gticks = int(ticks_ms())
        draw()
        #game[FPS] = 1000 // int(ticks_diff(ticks_ms(),gticks))
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
    machine.soft_reset()


def main():
    global snd,drone,fire,sexplode,lexplode,cfire,star,hum_tok,hershey
    from audio_mixer import Mixer
    snd = Mixer()
    drone = snd.load("/Starcastle/drone_mono.wav")
    fire  = snd.load("/Starcastle/fire_mono.wav")
    sexplode  = snd.load("/Starcastle/sexplode_mono.wav")
    lexplode  = snd.load("/Starcastle/lexplode_mono.wav")
    cfire  = snd.load("/Starcastle/cfire_mono.wav")
    star  = snd.load("/Starcastle/star_mono.wav")
    hum_tok = snd.play(drone, vol=160, loop=True)
    from hersheyDVI2 import Hershey
    hershey = Hershey(SCREEN,center_numbers=True)
    hershey._desc_shift = 7
    hershey.slow = False
    gc.collect()
    print('mem free:', gc.mem_free())
    _thread.start_new_thread(core1, ())
    sleep_ms(200)
    core0() # need for debug
    try:
        core0()
        shutdown()
    except KeyboardInterrupt:
        shutdown()

#if __name__=='__main__':
#    main()
    