# QIX - RP2350 DVI 320x240, dual core, no sound
# Analog stick moves player. Move into open field to draw. Close loop to claim.
# Qix (bouncing line) touching your trail = death. Sparx patrol the edges.
# 75% claimed = level clear. SELECT = exit.

from dvi_rp2_hstx_frame_sync_v2 import DVI_RP2_HSTX
from gamepadfast import Gamepad
from draw_numberdvi import Draw_number
import colors as rv_colors
import gc, array, framebuf, _thread, machine
from time import sleep_ms, ticks_ms
from random import randint
from sys import exit
from uctypes import addressof


SCREEN_W  = const(320)
SCREEN_H  = const(240)
FPS_CORE0 = const(0)
FPS_CORE1 = const(1)
NUM_PCT   = const(2)     # draw_number slots
NUM_LIVES = const(3)

machine.freq(246_000_000)
machine.mem32[0x40010058] = 2<<16 # HSTX CLK / 2
machine.mem32[0x40010054] = 1<<11 # HSTX CLK use SYS CLK

fb = bytearray(SCREEN_W * SCREEN_H * 2)

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
FILL_RED     = const(0x9122)
FILL_BLUE    = const(0x03ef)

GAMEPAD = array.array('i', [0,0,0,0])
GAMEPAD_X = const(0)
GAMEPAD_Y = const(1)
GAMEPAD_DEBOUNCE = const(2)
GAMEPAD_BTN = const(3)

GAMEPAD_RIGHT  = const(0b0100000)
GAMEPAD_LEFT   = const(0b0000100)
GAMEPAD_UP     = const(0b1000000)
GAMEPAD_DOWN   = const(0b0000010)
GAMEPAD_START  = const(0)
GAMEPAD_SELECT = const(0b0000001)

display = DVI_RP2_HSTX()
display.begin(
    fb,
    rv_colors.COLOR_MODE_BGR565,
    height=SCREEN_H,
    width=SCREEN_W,
    bytes_per_pixel=2,
)
FLAG_ADDR = addressof(display._frame_flag)
gamepad = Gamepad()

fb2 = bytearray(SCREEN_W * SCREEN_H * 2)
SCREEN = framebuf.FrameBuffer(fb2, SCREEN_W, SCREEN_H, framebuf.RGB565)
draw_num = Draw_number(fb2,SCREEN_W,2)

# ── Playfield grid ───────────────────────────────────────────────────────────
CELL      = const(2)               # 2x2 px per cell
HUD_H     = const(16)              # top HUD strip in px
GRID_W    = const(160)             # 320/2
GRID_H    = const(112)             # (240-16)/2
ROW_WORDS = const(160)             # fb row pitch in 32-bit words
INTERIOR  = const((GRID_W-2)*(GRID_H-2))   # 17380 claimable cells

ST_EMPTY = const(0)
ST_EDGE  = const(1)
ST_TRAIL = const(2)
ST_FILL  = const(3)
ST_VISIT = const(4)                # flood-fill temp mark
ST_FILL_SLOW = const(5)            # slow-draw claim (red)

MAP   = bytearray(GRID_W * GRID_H)
QUEUE = array.array('H', [0]*(GRID_W * GRID_H))   # BFS queue, cell index fits u16

def _dup(c):
    return c | (c << 16)
CELL_COLORS = array.array('I', [_dup(BLACK), _dup(WHITE), _dup(RED),
                                _dup(FILL_BLUE), _dup(BLACK), _dup(FILL_RED)])

# ── Game state ───────────────────────────────────────────────────────────────
GAME = array.array('i', [0]*20)
GAME_EXIT = const(0)
G_PX      = const(1)     # player grid pos
G_PY      = const(2)
G_TRAIL   = const(3)     # trail length (0 = on edge, not drawing)
G_SX      = const(4)     # trail start (respawn point)
G_SY      = const(5)
G_LIVES   = const(6)
G_PCT     = const(7)
G_FILLED  = const(8)
G_STATE   = const(9)
G_TIMER   = const(10)    # frames remaining in DYING/OVER/CLEAR
G_LEVEL   = const(11)
G_HIST    = const(12)    # qix history ring pos
G_SPARXT  = const(13)    # sparx spawn countdown (sparx ticks)
G_QFRAME  = const(14)    # qix history frame divider
G_QSPEED  = const(15)    # qix base speed for this level (8.8)
G_QLINE   = const(16)    # qix color-cycle line counter (7 lines per color)
G_SLOW    = const(17)    # slow-draw mode active (red fill)
G_ANIMN   = const(18)    # spawn/death animation length (ring steps = frames)

STATE_PLAY  = const(0)
STATE_DYING = const(1)
STATE_OVER  = const(2)
STATE_CLEAR = const(3)
STATE_SPAWN = const(4)   # broken-diamond materialize (respawn / level start)

DEAD_ZONE   = const(200)   # analog threshold of +/-512
PLAYER_MS   = const(20)    # ms per player cell step
PLAYER_MS_SLOW = const(55) # ms per player cell step in slow-draw (red) mode
SPARX_MS    = const(35)    # ms per sparx cell step
WIN_PCT     = const(75)
SPARX_DELAY = const(140)   # sparx ticks (~5 s) before spawn
QIX_MAX2    = const(1600)  # max qix segment length^2 in cells (40^2)

# Qix: [x1,y1,x2,y2, vx1,vy1,vx2,vy2] fixed point 8.8 in grid cells
QIX      = array.array('i', [0]*8)
QIX_HIST = array.array('i', [0]*32)             # 8 segments x (x1,y1,x2,y2) px
QIX_HIST_COL = array.array('i', [0]*8)          # per-segment color
QIX_PALETTE  = array.array('i', [RED,MAGENTA,BLUE,CYAN,GREEN,YELLOW,WHITE])

RNG = array.array('I', [0x12345678])   # xorshift32 state

# Sparx: 2 x [x, y, dir, active]
SPARX = array.array('i', [0]*8)
DIR_X = array.array('i', [0,1,0,-1])            # up,right,down,left
DIR_Y = array.array('i', [-1,0,1,0])

# ── Broken-diamond spawn/death effect ────────────────────────────────────────
# Ring j is a diamond of radius (center->tip) PR0 + PSTEP*j, but each edge is
# only lengthened by 2px/ring instead of the sqrt(2)*PSTEP it needs to stay
# closed, so the 4 corners open up by a widening gap -> "broken diamond".
# gp = per-axis inset of each edge endpoint from the ideal vertex:
#   gp = PSTEP*j/2 - (edge0 + 2j)/(2*sqrt2) = j*(PSTEP/2 - 1/sqrt2 - ...) ~= 1.7929*j
# A sliding window of 8 rings is on screen at once (oldest = outermost).
PR0   = const(4)         # player tip radius (matches player diamond, +/-4px)
PSTEP = const(5)         # radius growth per ring
ANIM_CAP = const(120)    # max ring steps (bounds animation length ~2s worst case)
RING_R  = array.array('i', [0]*128)   # radius per ring index
RING_GP = array.array('i', [0]*128)   # per-axis corner inset per ring index
for _j in range(128):
    RING_R[_j]  = PR0 + PSTEP*_j
    RING_GP[_j] = int(1.7929*_j + 0.5)
del _j


# ── Fast screen fill (48 bytes × 8 stmia = 192 bytes per loop) ───────────────
@micropython.asm_thumb
def fill_asm(r0, r1, r2):  # (buffer_addr, 16-bit_color)
    label(WAIT)
    ldr(r3, [r2, 0])
    cmp(r3, 0)
    beq(WAIT)
    
    
    
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
def copy_fb(r0, r1):                # r0=source, r1=dest
    movwt(r2, 4800)                 # 153600 bytes / 32 bytes per iter = 4800 exact
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


# ── Map / field ──────────────────────────────────────────────────────────────
@micropython.viper
def init_map():
    field_map = ptr8(MAP)
    i = 0
    total = GRID_W * GRID_H
    while i < total:
        field_map[i] = ST_EMPTY
        i += 1
    x = 0
    while x < GRID_W:
        field_map[x] = ST_EDGE
        field_map[(GRID_H-1)*GRID_W + x] = ST_EDGE
        x += 1
    y = 0
    while y < GRID_H:
        field_map[y*GRID_W] = ST_EDGE
        field_map[y*GRID_W + GRID_W-1] = ST_EDGE
        y += 1

@micropython.viper
def clear_trail():
    field_map = ptr8(MAP)
    i = 0
    total = GRID_W * GRID_H
    while i < total:
        if field_map[i] == ST_TRAIL:
            field_map[i] = ST_EMPTY
        i += 1

# Map cell -> 2x2 px blit. 4 cells unrolled per iter, dual row pointers so both
# pixel rows are immediate-offset stores. r0=MAP, r1=fb2, r2=CELL_COLORS (u32 LUT).
@micropython.asm_thumb
def render_map_asm(r0, r1, r2):
    movwt(r3, HUD_H * SCREEN_W * 2)     # skip HUD strip (10240 bytes)
    add(r1, r1, r3)
    movwt(r3, GRID_H)                   # row counter
    label(ROW)
    movwt(r5, SCREEN_W * 2)             # 640 = one pixel row
    add(r7, r1, r5)                     # r7 = second pixel row of this cell row
    movwt(r4, GRID_W // 4)              # 40 iterations of 4 cells
    label(COL)
    ldrb(r5, [r0, 0])                   # cell 0
    lsl(r5, r5, 2)
    add(r5, r5, r2)
    ldr(r6, [r5, 0])
    str(r6, [r1, 0])
    str(r6, [r7, 0])
    ldrb(r5, [r0, 1])                   # cell 1
    lsl(r5, r5, 2)
    add(r5, r5, r2)
    ldr(r6, [r5, 0])
    str(r6, [r1, 4])
    str(r6, [r7, 4])
    ldrb(r5, [r0, 2])                   # cell 2
    lsl(r5, r5, 2)
    add(r5, r5, r2)
    ldr(r6, [r5, 0])
    str(r6, [r1, 8])
    str(r6, [r7, 8])
    ldrb(r5, [r0, 3])                   # cell 3
    lsl(r5, r5, 2)
    add(r5, r5, r2)
    ldr(r6, [r5, 0])
    str(r6, [r1, 12])
    str(r6, [r7, 12])
    add(r0, 4)
    add(r1, 16)
    add(r7, 16)
    sub(r4, 1)
    bne(COL)
    movwt(r5, SCREEN_W * 2)             # r1 ended at 2nd row start; skip it
    add(r1, r1, r5)
    sub(r3, 1)
    bne(ROW)

# ── Loop close: flood fill from qix, claim everything it can't reach ─────────
@micropython.viper
def close_loop():
    field_map = ptr8(MAP)
    queue = ptr16(QUEUE)
    game = ptr32(GAME)
    qix = ptr32(QIX)
    seed = (qix[1] >> 8) * GRID_W + (qix[0] >> 8)
    if field_map[seed] != ST_EMPTY:
        seed = (qix[3] >> 8) * GRID_W + (qix[2] >> 8)
    if field_map[seed] == ST_EMPTY:
        field_map[seed] = ST_VISIT
        queue[0] = seed
        head = 0
        tail = 1
        while head < tail:
            cell = queue[head]
            head += 1
            nbr = cell - 1                     # border ring guards bounds
            if field_map[nbr] == ST_EMPTY:
                field_map[nbr] = ST_VISIT
                queue[tail] = nbr
                tail += 1
            nbr = cell + 1
            if field_map[nbr] == ST_EMPTY:
                field_map[nbr] = ST_VISIT
                queue[tail] = nbr
                tail += 1
            nbr = cell - GRID_W
            if field_map[nbr] == ST_EMPTY:
                field_map[nbr] = ST_VISIT
                queue[tail] = nbr
                tail += 1
            nbr = cell + GRID_W
            if field_map[nbr] == ST_EMPTY:
                field_map[nbr] = ST_VISIT
                queue[tail] = nbr
                tail += 1
    fill_state = ST_FILL                       # slow draw claims in red
    if game[G_SLOW] != 0:
        fill_state = ST_FILL_SLOW
    filled = 0
    i = 0
    total = GRID_W * GRID_H
    while i < total:
        state = field_map[i]
        if state == ST_EMPTY:
            field_map[i] = fill_state
            filled += 1
        elif state == ST_VISIT:
            field_map[i] = ST_EMPTY
        elif state == ST_TRAIL:
            field_map[i] = ST_EDGE
        elif state == ST_FILL:
            filled += 1
        elif state == ST_FILL_SLOW:
            filled += 1
        i += 1
    game[G_FILLED] = filled
    game[G_PCT] = filled * 100 // INTERIOR
    game[G_TRAIL] = 0
    game[G_SLOW] = 0
    if game[G_PCT] >= WIN_PCT:
        game[G_STATE] = STATE_CLEAR
        game[G_TIMER] = 150

# ── Player ───────────────────────────────────────────────────────────────────
@micropython.viper
def move_player(dx: int, dy: int):
    game = ptr32(GAME)
    field_map = ptr8(MAP)
    x = game[G_PX] + dx
    y = game[G_PY] + dy
    if x < 0 or x >= GRID_W or y < 0 or y >= GRID_H:
        return
    idx = y * GRID_W + x
    state = field_map[idx]
    if state == ST_FILL or state == ST_TRAIL or state == ST_FILL_SLOW:
        return
    if state == ST_EMPTY:
        if game[G_TRAIL] == 0:                 # leaving the edge - remember start
            game[G_SX] = game[G_PX]
            game[G_SY] = game[G_PY]
            gpad = ptr32(GAMEPAD)              # RIGHT held (active-low) -> slow red draw
            if (gpad[GAMEPAD_BTN] & GAMEPAD_RIGHT) == 0:
                game[G_SLOW] = 1
            else:
                game[G_SLOW] = 0
        field_map[idx] = ST_TRAIL
        game[G_TRAIL] = game[G_TRAIL] + 1
        game[G_PX] = x
        game[G_PY] = y
    else:                                      # ST_EDGE
        game[G_PX] = x
        game[G_PY] = y
        if game[G_TRAIL] != 0:
            close_loop()

@micropython.viper
def do_move():
    gpad = ptr32(GAMEPAD)
    x = gpad[GAMEPAD_X]                        # -512..512, +x right +y down
    y = gpad[GAMEPAD_Y]
    ax = x if x >= 0 else -1 * x
    ay = y if y >= 0 else -1 * y
    dx = 0
    dy = 0
    if ax > ay:
        if ax > DEAD_ZONE:
            dx = 1 if x > 0 else -1
    else:
        if ay > DEAD_ZONE:
            dy = 1 if y > 0 else -1
    if (dx | dy) != 0:
        move_player(dx, dy)

# ── RNG (xorshift32, viper-safe) ─────────────────────────────────────────────
@micropython.viper
def rnd() -> int:
    r = ptr32(RNG)
    s = r[0]
    s ^= s << 13
    s ^= (s >> 17) & 0x7FFF          # mask = logical shift (>> is arithmetic)
    s ^= s << 5
    r[0] = s
    return s & 0x3FFFFFFF

@micropython.viper
def rnd_vel(old: int) -> int:                  # random magnitude, flipped sign
    game = ptr32(GAME)
    half = game[G_QSPEED] >> 1
    m = half + int(rnd()) % (half + 1)
    return -m if old > 0 else m

# ── Qix ──────────────────────────────────────────────────────────────────────
@micropython.viper
def update_qix() -> int:                       # 1 = hit trail (death)
    qix = ptr32(QIX)
    field_map = ptr8(MAP)
    game = ptr32(GAME)
    i = 0
    while i < 2:                               # bounce each endpoint per axis
        pb = i << 1
        vb = 4 + (i << 1)
        newx = qix[pb] + qix[vb]
        state = field_map[(qix[pb+1] >> 8) * GRID_W + (newx >> 8)]
        if state == ST_TRAIL:
            return 1
        if state == ST_EMPTY:
            qix[pb] = newx
        else:
            qix[vb] = int(rnd_vel(qix[vb]))
        newy = qix[pb+1] + qix[vb+1]
        state = field_map[(newy >> 8) * GRID_W + (qix[pb] >> 8)]
        if state == ST_TRAIL:
            return 1
        if state == ST_EMPTY:
            qix[pb+1] = newy
        else:
            qix[vb+1] = int(rnd_vel(qix[vb+1]))
        i += 1
    r = int(rnd())                             # wander: ~1/8 frames re-roll one
    if (r & 7) == 0:                           # velocity comp, random sign+mag
        k = 4 + ((r >> 3) & 3)
        half = game[G_QSPEED] >> 1
        m = half + ((r >> 6) & 0xFFFFF) % (half + 1)
        qix[k] = -m if (r & 0x20) != 0 else m
    dx = (qix[2] - qix[0]) >> 8                # keep segment from stretching
    dy = (qix[3] - qix[1]) >> 8
    if dx*dx + dy*dy > QIX_MAX2:
        vel = qix[4]
        avel = vel if vel >= 0 else 0-vel
        qix[4] = avel if dx > 0 else 0-avel
        vel = qix[5]
        avel = vel if vel >= 0 else 0-vel
        qix[5] = avel if dy > 0 else 0-avel
        vel = qix[6]
        avel = vel if vel >= 0 else 0-vel
        qix[6] = -avel if dx > 0 else avel
        vel = qix[7]
        avel = vel if vel >= 0 else 0-vel
        qix[7] = -avel if dy > 0 else avel
    sx = qix[0]                                # sample 16 points for trail hit
    sy = qix[1]
    stepx = (qix[2] - qix[0]) >> 4
    stepy = (qix[3] - qix[1]) >> 4
    j = 0
    while j < 16:
        if field_map[(sy >> 8) * GRID_W + (sx >> 8)] == ST_TRAIL:
            return 1
        sx += stepx
        sy += stepy
        j += 1
    game[G_QFRAME] = game[G_QFRAME] + 1        # shimmer history every 3rd frame
    if game[G_QFRAME] >= 3:
        game[G_QFRAME] = 0
        hist = ptr32(QIX_HIST)
        histcol = ptr32(QIX_HIST_COL)
        palette = ptr32(QIX_PALETTE)
        slot = game[G_HIST]
        pos = slot << 2
        hist[pos]   = (qix[0] >> 8) << 1
        hist[pos+1] = HUD_H + ((qix[1] >> 8) << 1)
        hist[pos+2] = (qix[2] >> 8) << 1
        hist[pos+3] = HUD_H + ((qix[3] >> 8) << 1)
        line = game[G_QLINE]                   # 7 lines per palette color
        histcol[slot] = palette[(line // 7) % 7]
        game[G_QLINE] = line + 1
        game[G_HIST] = (slot + 1) & 7
    return 0

# ── Sparx ────────────────────────────────────────────────────────────────────
# Sparx patrol the LIVE perimeter: edge cells bordering unclaimed space
# (8-neighborhood, so convex corners count). A sparx enclosed by a claim
# keeps walking the dead edges (with randomized turn bias so it can't orbit
# a dead loop forever) until it reaches the live perimeter again - the edge
# graph is always connected, since every trail starts and ends on an edge.
@micropython.viper
def is_live(x: int, y: int) -> int:            # edge cell touching ST_EMPTY?
    field_map = ptr8(MAP)
    if field_map[y*GRID_W + x] != ST_EDGE:
        return 0
    y0 = y-1 if y > 0 else 0
    y1 = y+1 if y < GRID_H-1 else GRID_H-1
    x0 = x-1 if x > 0 else 0
    x1 = x+1 if x < GRID_W-1 else GRID_W-1
    yy = y0
    while yy <= y1:
        row = yy * GRID_W
        xx = x0
        while xx <= x1:
            if field_map[row + xx] == ST_EMPTY:
                return 1
            xx += 1
        yy += 1
    return 0

@micropython.viper
def find_far_edge() -> int:                    # live edge farthest from player
    field_map = ptr8(MAP)
    game = ptr32(GAME)
    px = game[G_PX]
    py = game[G_PY]
    best = 0
    bestd = -1
    y = 0
    while y < GRID_H:
        row = y * GRID_W
        x = 0
        while x < GRID_W:
            if field_map[row + x] == ST_EDGE:
                if int(is_live(x, y)):
                    dx = x - px
                    dx = dx if dx >= 0 else -dx
                    dy = y - py
                    dy = dy if dy >= 0 else -dy
                    if dx + dy > bestd:
                        bestd = dx + dy
                        best = row + x
            x += 1
        y += 1
    return best

@micropython.viper
def find_top_edge() -> int:                    # live edge nearest top-center (inside line)
    cx = GRID_W >> 1
    y = 0
    while y < GRID_H:
        off = 0
        while off <= cx:
            x = cx + off
            if x < GRID_W:
                if int(is_live(x, y)):
                    return y*GRID_W + x
            x = cx - off
            if x >= 0:
                if int(is_live(x, y)):
                    return y*GRID_W + x
            off += 1
        y += 1
    return GRID_W >> 1

@micropython.viper
def move_sparx() -> int:                       # 1 = caught player
    sparx = ptr32(SPARX)
    field_map = ptr8(MAP)
    game = ptr32(GAME)
    dirx = ptr32(DIR_X)
    diry = ptr32(DIR_Y)
    hit = 0
    i = 0
    while i < 2:
        base = i << 2
        if sparx[base+3] != 0:
            x = sparx[base]
            y = sparx[base+1]
            d = sparx[base+2]
            moved = 0
            j = 0
            while j < 4:                       # straight, turn, turn, reverse
                nd = d
                if j == 1:
                    nd = (d+1) & 3 if i == 0 else (d+3) & 3
                elif j == 2:
                    nd = (d+3) & 3 if i == 0 else (d+1) & 3
                elif j == 3:
                    nd = (d+2) & 3
                nx = x + dirx[nd]
                ny = y + diry[nd]
                if nx >= 0 and nx < GRID_W and ny >= 0 and ny < GRID_H:
                    if int(is_live(nx, ny)):
                        sparx[base] = nx
                        sparx[base+1] = ny
                        sparx[base+2] = nd
                        moved = 1
                        j = 4
                j += 1
            if moved == 0:                     # enclosed: walk dead edges
                chir = i ^ (int(rnd()) & 1)    # random chirality breaks loops
                j = 0
                while j < 4:
                    nd = d
                    if j == 1:
                        nd = (d+1) & 3 if chir == 0 else (d+3) & 3
                    elif j == 2:
                        nd = (d+3) & 3 if chir == 0 else (d+1) & 3
                    elif j == 3:
                        nd = (d+2) & 3
                    nx = x + dirx[nd]
                    ny = y + diry[nd]
                    if nx >= 0 and nx < GRID_W and ny >= 0 and ny < GRID_H:
                        if field_map[ny*GRID_W + nx] == ST_EDGE:
                            sparx[base] = nx
                            sparx[base+1] = ny
                            sparx[base+2] = nd
                            j = 4
                    j += 1
            if game[G_TRAIL] == 0:             # only catches player on edges
                ddx = sparx[base] - game[G_PX]
                ddx = ddx if ddx >= 0 else -ddx
                ddy = sparx[base+1] - game[G_PY]
                ddy = ddy if ddy >= 0 else -ddy
                if ddx <= 1 and ddy <= 1:
                    hit = 1
        i += 1
    return hit

def spawn_sparx():
    idx = find_far_edge()                      # spawn on live perimeter
    x = idx % GRID_W
    y = idx // GRID_W
    SPARX[0] = x; SPARX[1] = y; SPARX[2] = 1; SPARX[3] = 1
    SPARX[4] = x; SPARX[5] = y; SPARX[6] = 3; SPARX[7] = 1

# ── Lifecycle ────────────────────────────────────────────────────────────────
def init_level():
    init_map()
    GAME[G_PX] = GRID_W//2
    GAME[G_PY] = GRID_H-1
    GAME[G_TRAIL] = 0
    GAME[G_PCT] = 0
    GAME[G_FILLED] = 0
    GAME[G_STATE] = STATE_PLAY
    GAME[G_TIMER] = 0
    GAME[G_HIST] = 0
    GAME[G_QFRAME] = 0
    GAME[G_QLINE] = 0
    GAME[G_SLOW] = 0
    GAME[G_SPARXT] = SPARX_DELAY
    for i in range(8):
        SPARX[i] = 0
    for i in range(32):
        QIX_HIST[i] = 0
    for i in range(8):
        QIX_HIST_COL[i] = 0
    QIX[0] = (GRID_W//2 - 12) << 8
    QIX[1] = (GRID_H//2) << 8
    QIX[2] = (GRID_W//2 + 12) << 8
    QIX[3] = (GRID_H//2) << 8
    speed = 70 + GAME[G_LEVEL]*20
    GAME[G_QSPEED] = speed
    RNG[0] = ticks_ms() | 1
    for i in range(4,8):
        vel = randint(speed//2, speed)
        QIX[i] = vel if randint(0,1) else -vel
    gc.collect()
    start_spawn()                              # player materializes at level start

def new_game():
    GAME[G_LIVES] = 3
    GAME[G_LEVEL] = 1
    init_level()

def next_level():
    GAME[G_LEVEL] += 1
    init_level()

# Frames to run so the innermost visible ring clears the whole screen from
# (cx,cy) in px: window trails 7 rings, so need PR0 + PSTEP*(n-7) > L1 dist to
# farthest corner. Capped so extreme-corner deaths stay bounded.
def _anim_frames():
    cx = GAME[G_PX] << 1
    cy = HUD_H + (GAME[G_PY] << 1)
    mx = cx if cx > SCREEN_W-1-cx else SCREEN_W-1-cx
    my = cy if cy > SCREEN_H-1-cy else SCREEN_H-1-cy
    n = (mx + my - PR0)//PSTEP + 9
    return n if n < ANIM_CAP else ANIM_CAP

def start_death():
    GAME[G_ANIMN] = _anim_frames()
    GAME[G_TIMER] = GAME[G_ANIMN]
    GAME[G_STATE] = STATE_DYING

def start_spawn():
    GAME[G_ANIMN] = _anim_frames()
    GAME[G_TIMER] = GAME[G_ANIMN]
    GAME[G_STATE] = STATE_SPAWN

def die():
    GAME[G_LIVES] -= 1
    if GAME[G_LIVES] < 0:
        GAME[G_STATE] = STATE_OVER
        GAME[G_TIMER] = 180
    else:
        start_death()

def respawn():
    if GAME[G_TRAIL]:
        clear_trail()
        GAME[G_PX] = GAME[G_SX]
        GAME[G_PY] = GAME[G_SY]
        GAME[G_TRAIL] = 0
    GAME[G_SLOW] = 0
    if SPARX[3] or SPARX[7]:                   # both sparx -> top-center inside line
        idx = find_top_edge()
        x = idx % GRID_W
        y = idx // GRID_W
        SPARX[0] = x; SPARX[1] = y; SPARX[2] = 1
        SPARX[4] = x; SPARX[5] = y; SPARX[6] = 3
    start_spawn()

# ── Input / render / cores ───────────────────────────────────────────────────
@micropython.viper
def read_gamepad():
    gpad = ptr32(GAMEPAD)
    gamepad.read()
    buttons = int(gamepad.buttons)
    if not (buttons & GAMEPAD_SELECT):
        shutdown()
    gpad[GAMEPAD_X] = int(gamepad.x)           # -512..512 analog
    gpad[GAMEPAD_Y] = int(gamepad.y)
    gpad[GAMEPAD_BTN] = buttons                 # active-low button bitmask

@micropython.viper
def draw_broken(cx: int, cy: int, lead: int):
    # Draw the 8-ring window ending at ring `lead` (outermost). Each ring = 4
    # centered edges; corners left open by RING_GP. framebuf.line clips off-screen.
    ring_r  = ptr32(RING_R)
    ring_gp = ptr32(RING_GP)
    lo = lead - 7
    if lo < 1:
        lo = 1
    j = lo
    while j <= lead:
        r  = ring_r[j]
        gp = ring_gp[j]
        SCREEN.line(cx + gp,     cy - r + gp, cx + r - gp, cy - gp,     WHITE)  # top-right
        SCREEN.line(cx + r - gp, cy + gp,     cx + gp,     cy + r - gp, WHITE)  # bottom-right
        SCREEN.line(cx - gp,     cy + r - gp, cx - r + gp, cy + gp,     WHITE)  # bottom-left
        SCREEN.line(cx - r + gp, cy - gp,     cx - gp,     cy - r + gp, WHITE)  # top-left
        j += 1

@micropython.viper
def draw():
    display.wait_frame()
    fill_asm(fb2, BLACK,FLAG_ADDR)
    render_map_asm(MAP, fb2, CELL_COLORS)
    game = ptr32(GAME)
    hist = ptr32(QIX_HIST)
    histcol = ptr32(QIX_HIST_COL)
    palette = ptr32(QIX_PALETTE)
    k = 0
    while k < 8:                               # qix shimmer trail
        pos = k << 2
        if hist[pos] != 0:
            SCREEN.line(hist[pos], hist[pos+1], hist[pos+2], hist[pos+3], histcol[k])
        k += 1
    curcol = palette[(game[G_QLINE] // 7) % 7]
    qix = ptr32(QIX)
    SCREEN.line((qix[0] >> 8) << 1, HUD_H + ((qix[1] >> 8) << 1),
                (qix[2] >> 8) << 1, HUD_H + ((qix[3] >> 8) << 1), curcol)
    sparx = ptr32(SPARX)
    i = 0
    while i < 2:
        base = i << 2
        if sparx[base+3] != 0:                 # spark: small cloud of random dots
            cx = sparx[base] << 1
            cy = HUD_H + (sparx[base+1] << 1)
            n = 0
            while n < 8:
                rr = int(rnd())
                sel = (rr >> 6) & 3
                c = WHITE
                if sel == 0:
                    c = YELLOW
                elif sel == 1:
                    c = RED
                SCREEN.pixel(cx + ((rr & 7) - 3), cy + (((rr >> 3) & 7) - 3), c)
                n += 1
        i += 1
    state = game[G_STATE]
    cx = game[G_PX] << 1
    cy = HUD_H + (game[G_PY] << 1)
    if state == STATE_DYING:                    # expand: lead ring grows 1 -> ANIMN
        draw_broken(cx, cy, game[G_ANIMN] - game[G_TIMER] + 1)
    elif state == STATE_SPAWN:                  # contract: lead ring shrinks ANIMN -> 1
        draw_broken(cx, cy, game[G_TIMER])
    else:                                       # solid diamond outline (PLAY/OVER/CLEAR)
        SCREEN.line(cx, cy - 4, cx + 4, cy, RED)
        SCREEN.line(cx + 4, cy, cx, cy + 4, RED)
        SCREEN.line(cx, cy + 4, cx - 4, cy, RED)
        SCREEN.line(cx - 4, cy, cx, cy - 4, RED)
    if state == STATE_OVER:
        SCREEN.text('GAME OVER', 124, 120, RED)
    elif state == STATE_CLEAR:
        SCREEN.text('LEVEL CLEAR', 116, 120, GREEN)
    draw_num.draw(NUM_PCT, 10, 7)
    draw_num.draw(NUM_LIVES, 70, 7)
    #draw_num.draw(FPS_CORE0, 290, 10)
    #draw_num.draw(FPS_CORE1, 290, 20)

@micropython.viper
def core0():
    sleep_ms(200)
    game = ptr32(GAME)
    new_game()
    gc.collect()
    pot_ticks = 0
    move_ticks = 0
    sparx_ticks = 0
    while not game[GAME_EXIT]:
        ticks = int(ticks_ms())
        if ticks - pot_ticks > 30: #30
            pot_ticks = ticks
            read_gamepad()
            #print(gc.mem_free())
        state = game[G_STATE]
        if state == STATE_PLAY:
            if int(update_qix()):
                die()
            else:
                pm = PLAYER_MS
                if game[G_SLOW] != 0:
                    pm = PLAYER_MS_SLOW
                if ticks - move_ticks > pm:
                    move_ticks = ticks
                    do_move()
                if ticks - sparx_ticks > SPARX_MS:
                    sparx_ticks = ticks
                    if game[G_SPARXT] > 0:
                        game[G_SPARXT] = game[G_SPARXT] - 1
                        if game[G_SPARXT] == 0:
                            spawn_sparx()
                    elif int(move_sparx()):
                        die()
        else:
            game[G_TIMER] = game[G_TIMER] - 1
            if game[G_TIMER] <= 0:
                if state == STATE_DYING:
                    respawn()               # -> STATE_SPAWN (materialize)
                elif state == STATE_SPAWN:
                    game[G_STATE] = STATE_PLAY
                elif state == STATE_CLEAR:
                    next_level()
                else:
                    new_game()
        lives = game[G_LIVES]
        draw_num.set(NUM_PCT, game[G_PCT])
        draw_num.set(NUM_LIVES, lives if lives > 0 else 0)
        draw_num.update_all()
        draw()
        draw_num.set(FPS_CORE0, ticks)
    print('core0 done')

@micropython.viper
def core1():
    sleep_ms(500)
    game = ptr32(GAME)
    while not game[GAME_EXIT]:
        ticks = int(ticks_ms())
        copy_fb(fb2, fb)
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
    _thread.start_new_thread(core1, ())
    core0()
    
if __name__ == '__main__':
    main()