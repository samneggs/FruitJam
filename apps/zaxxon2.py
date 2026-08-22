from dvi_rp2_hstx_frame_sync_v2 import DVI_RP2_HSTX
from gamepadfast import Gamepad
from draw_numberdvi import Draw_number
import colors as rv_colors
import gc, array, framebuf, _thread
from time import sleep_ms, ticks_ms, ticks_diff, sleep_us
from random import randint
from TLV320 import TLV320DAC3100
from machine import I2C,I2S,Pin
from sys import exit


SCREEN_W  = const(320)
SCREEN_H  = const(240)
BYTES_PER_PIXEL = const(1)   # 1 = RGB332, 2 = RGB565
FPS_CORE0 = const(0)     # for draw_number
FPS_CORE1 = const(1)

# 1824 x 200 pixels
MAP_W      = const(228)   # very wide map
MAP_H      = const(25)
TILE_W     = const(8)
TILE_H     = const(8)
NUM_TILES  = const(544)

MAP_PIX_W  = const(MAP_W * TILE_W)   # 1824
MAP_PIX_H  = const(MAP_H * TILE_H)   # 200

# ── pseudo-3D shear ──────────────────────────────────────────────────────────
# Every 2 pixel columns the map is sampled 1 pixel further down, i.e. the image
# steps UP by 1 px every 2 px to the right.  An 8x8 tile therefore steps up 4x.
COL_GROUPS = const(SCREEN_W >> 1)    # 160 two-pixel column groups
ROW_HW1    = const(SCREEN_W >> 1)    # halfwords (2 px) per screen row = 160
ROW_HW2    = const(ROW_HW1 * 2)
ROW_HW3    = const(ROW_HW1 * 3)
ROW_HW4    = const(ROW_HW1 * 4)
ROW_HW5    = const(ROW_HW1 * 5)
ROW_HW6    = const(ROW_HW1 * 6)
ROW_HW7    = const(ROW_HW1 * 7)
ROW_HW8    = const(ROW_HW1 * 8)

# The sheared map spans MAP_PIX_H + (COL_GROUPS-1) = 359 screen rows, taller
# than the 240-row screen.  MAP_Y_BIAS is the screen row that holds map row 0
# at column group 0; it centres the diagonal band vertically.  No wrapping —
# anything outside the band stays BACKGROUND.
MAP_Y_BIAS = const((SCREEN_H - 1 - MAP_PIX_H + COL_GROUPS) // 2)   # 99

SCROLL_STEP = const(2)               # world x px per frame (must stay even)

BACKGROUND = const(0)

machine.freq(246_000_000)
machine.mem32[0x40010058] = 2<<16 # HSTX CLK / 2
machine.mem32[0x40010054] = 1<<11 # HSTX CLK use SYS CLK

fb = bytearray(SCREEN_W * SCREEN_H * BYTES_PER_PIXEL)

display = DVI_RP2_HSTX()
display.begin(
    fb,
    rv_colors.COLOR_MODE_BGR233, #COLOR_MODE_BGR565
    height=SCREEN_H,
    width=SCREEN_W,
    bytes_per_pixel=BYTES_PER_PIXEL,
)

BLACK        = const(0x0000)
DARK_BLUE    = const(0x0015)
DARK_GREEN   = const(0x0320)
DARK_CYAN    = const(0x0335)
DARK_RED     = const(0x6000)
DARK_MAGENTA = const(0x6015)
BROWN        = const(0x6320)
LIGHT_GRAY   = const(0xAD55)

DARK_GRAY    = const(0x4208)
BLUE         = const(0x001F)
GREEN        = const(0x07E0)
CYAN         = const(0x07FF)
RED          = const(0xF800)
MAGENTA      = const(0xF81F)
YELLOW       = const(0xFFE0)
WHITE        = const(0xFFFF)

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

gamepad = Gamepad()

GAME = array.array('i', [0]*10)
GAME_EXIT  = const(0)
GAME_MAP_X = const(1)    # world x of the player / left screen edge, in map pixels

screen_format = framebuf.RGB565 if BYTES_PER_PIXEL == 2 else framebuf.GS8

fb2 = bytearray(SCREEN_W * SCREEN_H * BYTES_PER_PIXEL)
SCREEN = framebuf.FrameBuffer(fb2, SCREEN_W, SCREEN_H, screen_format)
draw_num = Draw_number(fb2,SCREEN_W,BYTES_PER_PIXEL)


# ── Fast screen fill (24 bytes × 8 stmia = 192 bytes per loop) ───────────────
@micropython.asm_thumb
def fill_asm(r0, r1, r2):  # (buffer_addr, color, bytes_per_pixel 1|2)
    # loop count = (W * H * bpp) // 192   -> r3
    movwt(r3, (SCREEN_W * SCREEN_H) // 192)
    mul(r3, r2)

    # build 32-bit fill pattern
    cmp(r2, 2)
    beq(PAT16)
    mov(r4, 0xFF)       # 1 bpp: b -> b | b<<8
    and_(r1, r4)
    mov(r4, r1)
    lsl(r4, r4, 8)
    orr(r1, r4)
    label(PAT16)
    lsl(r1, r1, 16)     # mask to 16 bits
    lsr(r1, r1, 16)
    mov(r4, r1)
    lsl(r4, r4, 16)
    orr(r4, r1)         # r4 = replicated 32-bit word

    mov(r1, r0)         # stmia base = buffer
    mov(r0, r3)         # counter
    mov(r2, r4)
    mov(r3, r4)
    mov(r5, r4)
    mov(r6, r4)
    mov(r7, r4)
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
    movwt(r2, (SCREEN_W * SCREEN_H * BYTES_PER_PIXEL) // 32) # 153600 bytes / 32 bytes per iter = 4800 exact or 2400
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
    global TILEMAP, TILETEXTURES # ENEMYTEXTURES, PLAYERTEXTURES, MISCTEXTURES
    with open('/Zaxxon/MAP.BIN', "rb") as f:
        TILEMAP = f.read()
        #print('map size:',len(TILEMAP)) # 11400
    with open('/Zaxxon/back_sprites1.bin', "rb") as f:
        header = f.read(4)
        TILETEXTURES = f.read()   # 8x8 RBG332 tiles
        #print('Number of tiles:',len(TILETEXTURES)//(8*8)) # 544


@micropython.viper
def read_gamepad():
    gpad = ptr32(GAMEPAD)
    gamepad.read() # read all I/O
    buttons = int(gamepad.buttons)
    if not (buttons & GAMEPAD_SELECT) : # select pushed
        shutdown()
    x = int(gamepad.x) # -512 to + 512 analog
    y = int(gamepad.y) # -512 to + 512 analog


# ── Sheared (pseudo-3D) tilemap renderer ─────────────────────────────────────
# screen(x, y)  <-  map(GAME_MAP_X + x, y + (x >> 1) - MAP_Y_BIAS)
# Rows outside 0..MAP_PIX_H-1 are simply not drawn (no vertical wrap), leaving
# a diagonal band centred on the screen with BACKGROUND above and below it.
#
# Walks the screen in 2-px-wide column groups (halfwords, RGB332) so the shear
# is a per-column-group constant and the tilemap lookup is amortised over 8
# rows.  Source x is kept even (GAME_MAP_X forced even) so both the texture
# read and the framebuffer write stay halfword aligned.
@micropython.viper
def render_map():
    screen   = ptr16(fb2)
    textures = ptr16(TILETEXTURES)
    back_map = ptr16(TILEMAP)
    game     = ptr32(GAME)

    world_x = (int(game[GAME_MAP_X]) >> 1) << 1      # force even

    col_group = 0
    while col_group < COL_GROUPS:
        map_x = world_x + (col_group << 1)
        if map_x >= MAP_PIX_W:                        # wrap the long map
            map_x -= MAP_PIX_W
        tile_col = map_x >> 3
        sub_col  = (map_x & 7) >> 1                   # halfword 0..3 in texel row

        # shear: this column group's map row 0 lands on screen row top_y
        top_y = MAP_Y_BIAS - col_group
        map_y = 0
        screen_y = top_y
        if screen_y < 0:                              # clip top of the band
            map_y = -screen_y
            screen_y = 0
        end_y = top_y + MAP_PIX_H                     # clip bottom of the band
        if end_y > SCREEN_H:
            end_y = SCREEN_H

        dest = col_group + screen_y * ROW_HW1
        while screen_y < end_y:
            row_in_tile = map_y & 7
            rows = 8 - row_in_tile
            if screen_y + rows > end_y:
                rows = end_y - screen_y
            # tile = 64 B = 32 halfwords, texel row = 4 halfwords
            src = (int(back_map[(map_y >> 3) * MAP_W + tile_col]) << 5) + (row_in_tile << 2) + sub_col
            if rows == 8:                             # unrolled full tile row
                screen[dest]           = textures[src]
                screen[dest + ROW_HW1] = textures[src + 4]
                screen[dest + ROW_HW2] = textures[src + 8]
                screen[dest + ROW_HW3] = textures[src + 12]
                screen[dest + ROW_HW4] = textures[src + 16]
                screen[dest + ROW_HW5] = textures[src + 20]
                screen[dest + ROW_HW6] = textures[src + 24]
                screen[dest + ROW_HW7] = textures[src + 28]
                dest += ROW_HW8
            else:
                row = 0
                while row < rows:
                    screen[dest] = textures[src]
                    dest += ROW_HW1
                    src  += 4
                    row  += 1
            screen_y += rows
            map_y    += rows
        col_group += 1


TILE_PITCH = const(9)                 # 8 px + 1 px gap; set to 8 for contiguous
TILES_ACROSS = const(SCREEN_W // TILE_PITCH)   # 35 at pitch 9, 40 at pitch 8

@micropython.viper
def render_textures(first: int):
    screen   = ptr8(fb2)
    textures = ptr8(TILETEXTURES)
    n = first
    ty = 0
    while ty * TILE_PITCH + TILE_H <= SCREEN_H:
        tx = 0
        while tx < TILES_ACROSS:
            if n >= NUM_TILES:
                return
            src = n << 6
            dst = (ty * TILE_PITCH) * SCREEN_W + tx * TILE_PITCH
            for r in range(8):
                s = src + (r << 3)
                d = dst + r * SCREEN_W
                screen[d]   = textures[s]
                screen[d+1] = textures[s+1]
                screen[d+2] = textures[s+2]
                screen[d+3] = textures[s+3]
                screen[d+4] = textures[s+4]
                screen[d+5] = textures[s+5]
                screen[d+6] = textures[s+6]
                screen[d+7] = textures[s+7]
            n += 1
            tx += 1
        ty += 1

@micropython.viper
def draw():
    display.wait_frame()
    #fill_asm(fb2,BACKGROUND,BYTES_PER_PIXEL)   # band no longer covers the screen
    render_map()
    #draw_num.draw(FPS_CORE0, 290, 10)
    #draw_num.draw(FPS_CORE1, 290, 20)


@micropython.viper
def core0():
    sleep_ms(200)
    game = ptr32(GAME)
    gc.collect()
    pot_ticks = 0
    scroll_ticks = 0
    while not game[GAME_EXIT]:
        ticks = int(ticks_ms())
        if ticks - pot_ticks > 30:
            pot_ticks = ticks
            read_gamepad()
        if ticks - scroll_ticks >35:
            scroll_ticks = ticks
            world_x = game[GAME_MAP_X] + SCROLL_STEP    # test scroll
            if world_x >= MAP_PIX_W:
                world_x -= MAP_PIX_W
            game[GAME_MAP_X] = world_x
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
    machine.reset()

def main():
    load_files()
    _thread.start_new_thread(core1, ())
    core0()
    
if __name__ == '__main__':
    main()