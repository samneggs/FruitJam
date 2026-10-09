
# ── Core 1: 160x120 -> 320x240, each source word (2 px) -> 2 words x 2 rows ──
@micropython.asm_thumb
def double_blit(r0, r1):          # r0 src, r1 dst
    movwt(r7, SCREEN_W * 2)
    add(r7, r7, r1)               # r7 = dst row + 1
    mov(r2, RH)
    label(DROW)
    mov(r3, RW // 2)
    label(DCOL)
    ldr(r4, [r0, 0])
    data(2, 0xEAC4, 0x4504)  # pkhbt r5, r4, r4, lsl #16 ; p0 | p0<<16
    data(2, 0xEAC4, 0x4624)  # pkhtb r6, r4, r4, asr #16 ; p1<<16 | p1
    str(r5, [r1, 0])
    str(r6, [r1, 4])
    str(r5, [r7, 0])
    str(r6, [r7, 4])
    add(r0, 4)
    add(r1, 8)
    add(r7, 8)
    sub(r3, 1)
    bne(DCOL)
    movwt(r4, SCREEN_W * 2)       # skip the odd row already written
    add(r1, r1, r4)
    add(r7, r7, r4)
    sub(r2, 1)
    bne(DROW)

DEADZONE = const(40)
PAL_BUTTON = const(0b0000010)     # GAMEPAD_DOWN, active low
RETICLE_HINT = const(0)           # 1: reticle also turns green when the shot would score

@micropython.viper
def set_palette(p: int):          # copy precomputed palette words into FP; no allocation
    fp = ptr32(FP)
    src = ptr32(PAL_DATA[p])
    idx = ptr16(PAL_IDX)
    n = int(len(PAL_IDX))
    for j in range(n):
        fp[idx[j]] = src[j]
SCORE_FB = framebuf.FrameBuffer(fb, SCREEN_W, SCREEN_H, framebuf.RGB565)   # draws onto the doubled frame
DIGITS = ('0', '1', '2', '3', '4', '5', '6', '7', '8', '9')             # no per-frame str allocation

@micropython.viper
def read_gamepad():
    ctrl = ptr32(CTRL)
    gamepad.read()
    buttons = int(gamepad.buttons)
    if not (buttons & 1):         # SELECT -> exit
        ctrl[C_EXIT] = 1
    x = int(gamepad.x)
    y = int(gamepad.y)
    if x < DEADZONE and x > 0 - DEADZONE:
        x = 0
    if y < DEADZONE and y > 0 - DEADZONE:
        y = 0
    ctrl[C_PADX] = x
    ctrl[C_PADY] = y
    ctrl[C_BTN] = buttons

@micropython.viper
def io_loop():                     # gamepad, palette, odd rows (rt_rows), 2x doubling, HUD
    ctrl = ptr32(CTRL)
    last = 0
    pad_ticks = 0
    pal = int(PALETTE)
    npal = int(PAL_COUNT)
    prev_btn = 0xFF
    last_setup = 0
    while not ctrl[C_EXIT]:
        s = ctrl[C_SETUP]
        if s != last_setup:            # core 1 has set up frame s: render the odd rows now
            last_setup = s
            rt_rows(CTRL, FP1)
        ticks = int(ticks_ms())
        if ticks - pad_ticks > 20:
            pad_ticks = ticks
            read_gamepad()
            b = ctrl[C_BTN]
            if (prev_btn & PAL_BUTTON) and not (b & PAL_BUTTON):   # press edge
                pal += 1
                if pal >= npal:
                    pal = 0
                set_palette(pal)
            prev_btn = b
        f = ctrl[C_FRAME]
        if f != last:
            display.wait_frame()
            if (f - 1) & 1:
                double_blit(RB1, fb)
            else:
                double_blit(RB0, fb)
            ctrl[C_CONSUMED] = f
            last = f
            draw_num.update_all()
            draw_num.draw(FPS_CORE0, 290, 10)
            draw_num.set(FPS_CORE0, ticks)
            g = ctrl[C_GOALS]
            t = ctrl[C_TURNOVER]
            SCORE_FB.text(DIGITS[(g // 10) % 10], 8, 6, 0x07FF)        # goals (cyan)
            SCORE_FB.text(DIGITS[g % 10], 16, 6, 0x07FF)
            SCORE_FB.text(DIGITS[t % 10], 32, 6, 0xF81F)               # turnovers (magenta)
            side = ctrl[C_SIDE]                                        # loose orb off-screen
            if side == 1:
                for i in range(8):
                    SCORE_FB.fill_rect(304 + i, 108 + i, 2, 24 - 2 * i, 0xFFE0)
            elif side == -1:
                for i in range(8):
                    SCORE_FB.fill_rect(14 - i, 108 + i, 2, 24 - 2 * i, 0xFFE0)
            if ctrl[C_HOLD]:                                           # aim reticle
                c = 0xFFFF
                if RETICLE_HINT and ctrl[C_AIM]:
                    c = 0x07E0
                SCORE_FB.hline(154, 120, 12, c)
                SCORE_FB.vline(160, 114, 12, c)

# Which core runs what. 1: rt_main (the asm renderer) on core 1, io_loop on core 0.
# 0: the original layout, rt_main on core 0 and io_loop on core 1.
SWAP_CORES = const(1)
RT_RESULT = array.array('i', [0, 0])      # [finished, frames rendered]

def render():
    RT_RESULT[1] = rt_main(CTRL, FP)      # returns only after CTRL[C_EXIT] is set
    RT_RESULT[0] = 1

def main():
    gc.collect()
    if SWAP_CORES:
        _thread.start_new_thread(render, ())
        io_loop()                         # returns on SELECT (sets CTRL[C_EXIT])
        while not RT_RESULT[0]:           # let the renderer finish its frame and return
            sleep_ms(1)
    else:
        _thread.start_new_thread(io_loop, ())
        sleep_ms(100)
        render()
    sleep_ms(100)
    display.deinit()
    machine.freq(150_000_000)
    print('frames:', RT_RESULT[1])

if __name__ == '__main__':
    main()
