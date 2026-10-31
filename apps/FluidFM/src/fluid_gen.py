"""Stable-fluids kernels for the board (asm_thumb source, grid size baked in).
Fields are float32 W*H arrays; interior cells are 1..W-2 x 1..H-2, border cells stay as they are.
Velocity is in cells per step (dt folded in)."""

W, H = 128, 96
SHIFT = W.bit_length() - 1
assert 1 << SHIFT == W
ROW = 4 * W

# advect: dst[i] = bilinear(src, x - u[i], y - v[i]) * decay
# P (bytes): u addr, v addr, decay, 0.5, W-1.5, H-1.5, 1.0
ADVECT_V0 = f"""
@micropython.asm_thumb
def advect(r0, r1, r2):         # dst, src, P
    mov(r3, r1)
    mov(r7, r2)
    ldr(r1, [r7, 0])
    ldr(r2, [r7, 4])
    vldr(s26, [r7, 8])
    vldr(s23, [r7, 12])
    vldr(s24, [r7, 16])
    vldr(s25, [r7, 20])
    vldr(s22, [r7, 24])
    mov(r6, 0)
    vmov(s27, r6)               # 0.0
    vadd(s21, s22, s27)         # yf = 1.0
    movwt(r6, {ROW + 4})
    add(r0, r0, r6)
    add(r1, r1, r6)
    add(r2, r2, r6)
    mov(r5, {H - 2})
    label(YL)
    vadd(s20, s22, s27)         # xf = 1.0
    mov(r4, {W - 2})
    label(XL)
    vldr(s0, [r1, 0])
    vldr(s1, [r2, 0])
    vsub(s0, s20, s0)           # px
    vsub(s1, s21, s1)           # py
    data(2, 0xFE80, 0x0A2B)     # vmaxnm.f32 s0, s0, s23
    data(2, 0xFE80, 0x0A4C)     # vminnm.f32 s0, s0, s24
    data(2, 0xFEC0, 0x0AAB)     # vmaxnm.f32 s1, s1, s23
    data(2, 0xFEC0, 0x0AEC)     # vminnm.f32 s1, s1, s25
    vcvt_s32_f32(s2, s0)
    vcvt_s32_f32(s3, s1)
    vmov(r6, s2)                # ix
    vmov(r7, s3)                # iy
    vcvt_f32_s32(s2, s2)
    vcvt_f32_s32(s3, s3)
    vsub(s0, s0, s2)            # fx
    vsub(s1, s1, s3)            # fy
    lsl(r7, r7, {SHIFT})
    add(r7, r7, r6)
    lsl(r7, r7, 2)
    add(r7, r7, r3)
    vldr(s4, [r7, 0])
    vldr(s5, [r7, 4])
    vldr(s6, [r7, {ROW}])
    vldr(s7, [r7, {ROW + 4}])
    vsub(s5, s5, s4)
    vmul(s5, s5, s0)
    vadd(s4, s4, s5)            # a
    vsub(s7, s7, s6)
    vmul(s7, s7, s0)
    vadd(s6, s6, s7)            # b
    vsub(s6, s6, s4)
    vmul(s6, s6, s1)
    vadd(s4, s4, s6)
    vmul(s4, s4, s26)
    vstr(s4, [r0, 0])
    add(r0, 4)
    add(r1, 4)
    add(r2, 4)
    vadd(s20, s20, s22)
    sub(r4, 1)
    bne(XL)
    add(r0, 8)
    add(r1, 8)
    add(r2, 8)
    vadd(s21, s21, s22)
    sub(r5, 1)
    bne(YL)
"""

# div[i] = 0.5 * ((u[i+1] - u[i-1]) + (v[i+W] - v[i-W]))
DIVERGENCE_V0 = f"""
@micropython.asm_thumb
def divergence(r0, r1, r2):     # div, u, v
    movwt(r6, 0x3f000000)
    vmov(s15, r6)               # 0.5
    movwt(r6, {ROW + 4})
    add(r0, r0, r6)             # div at i
    add(r1, r1, r6)
    sub(r1, 4)                  # u at i-1
    movwt(r6, {2 * ROW})
    add(r3, r2, r6)
    add(r3, 4)                  # v at i+W
    add(r2, 4)                  # v at i-W
    mov(r5, {H - 2})
    label(YL)
    mov(r4, {W - 2})
    label(XL)
    vldr(s0, [r1, 0])
    vldr(s1, [r1, 8])
    vldr(s2, [r2, 0])
    vldr(s3, [r3, 0])
    vsub(s1, s1, s0)
    vsub(s3, s3, s2)
    vadd(s1, s1, s3)
    vmul(s1, s1, s15)
    vstr(s1, [r0, 0])
    add(r0, 4)
    add(r1, 4)
    add(r2, 4)
    add(r3, 4)
    sub(r4, 1)
    bne(XL)
    add(r0, 8)
    add(r1, 8)
    add(r2, 8)
    add(r3, 8)
    sub(r5, 1)
    bne(YL)
"""

# Gauss-Seidel, lexicographic, in place: p[i] = ((pl + pr) + (pu + pd) - div[i]) * 0.25
PRESSURE_GS_V0 = f"""
@micropython.asm_thumb
def pressure(r0, r1, r2):       # p, div, iterations
    movwt(r6, 0x3e800000)
    vmov(s15, r6)               # 0.25
    mov(r6, r0)
    mov(r7, r1)
    label(IT)
    movwt(r4, {ROW})
    add(r0, r6, r4)             # p at i-1
    add(r3, r6, 4)              # p at i-W
    add(r1, r7, r4)
    add(r1, 4)                  # div at i
    mov(r5, {H - 2})
    label(YL)
    mov(r4, {W - 2})
    label(XL)
    vldr(s0, [r0, 0])
    vldr(s1, [r0, 8])
    vldr(s2, [r3, 0])
    vldr(s3, [r0, {ROW + 4}])
    vldr(s4, [r1, 0])
    vadd(s0, s0, s1)
    vadd(s2, s2, s3)
    vadd(s0, s0, s2)
    vsub(s0, s0, s4)
    vmul(s0, s0, s15)
    vstr(s0, [r0, 4])
    add(r0, 4)
    add(r3, 4)
    add(r1, 4)
    sub(r4, 1)
    bne(XL)
    add(r0, 8)
    add(r3, 8)
    add(r1, 8)
    sub(r5, 1)
    bne(YL)
    sub(r2, 1)
    bne(IT)
"""

# u[i] -= 0.5 * (pr - pl); v[i] -= 0.5 * (pd - pu)
PROJECT_V0 = f"""
@micropython.asm_thumb
def project(r0, r1, r2):        # u, v, p
    movwt(r6, 0x3f000000)
    vmov(s15, r6)
    movwt(r6, {ROW})
    add(r3, r2, 4)              # p at i-W
    add(r2, r2, r6)             # p at i-1
    add(r6, 4)
    add(r0, r0, r6)             # u at i
    add(r1, r1, r6)             # v at i
    mov(r5, {H - 2})
    label(YL)
    mov(r4, {W - 2})
    label(XL)
    vldr(s0, [r2, 0])
    vldr(s1, [r2, 8])
    vldr(s2, [r3, 0])
    vldr(s3, [r2, {ROW + 4}])
    vldr(s4, [r0, 0])
    vldr(s5, [r1, 0])
    vsub(s1, s1, s0)
    vsub(s3, s3, s2)
    vmul(s1, s1, s15)
    vmul(s3, s3, s15)
    vsub(s4, s4, s1)
    vsub(s5, s5, s3)
    vstr(s4, [r0, 0])
    vstr(s5, [r1, 0])
    add(r0, 4)
    add(r1, 4)
    add(r2, 4)
    add(r3, 4)
    sub(r4, 1)
    bne(XL)
    add(r0, 8)
    add(r1, 8)
    add(r2, 8)
    add(r3, 8)
    sub(r5, 1)
    bne(YL)
"""

# col[i] = pal[usat8(int(dye[i] * scale))] for every cell (border included)
COLORIZE_V0 = f"""
@micropython.asm_thumb
def colorize(r0, r1, r2, r3):   # col bytes, dye, palette, scale (float bits)
    vmov(s15, r3)
    movwt(r5, {W * H})
    label(L)
    vldr(s0, [r1, 0])
    vmul(s0, s0, s15)
    vcvt_s32_f32(s0, s0)
    vmov(r4, s0)
    data(2, 0xF384, 0x0408)     # usat r4, #8, r4
    add(r4, r4, r2)
    ldrb(r4, [r4, 0])
    strb(r4, [r0, 0])
    add(r0, 1)
    add(r1, 4)
    sub(r5, 1)
    bne(L)
"""

# nearest-neighbour 2.5x: cells alternate 3,2 pixels wide and 3,2 rows tall -> 320x240
EXPAND_V0 = f"""
@micropython.asm_thumb
def expand(r0, r1):             # fb, col
    mov(r5, 0)
    label(GY)
    mov(r2, r0)                 # start of this fb row
    mov(r4, {W // 2})
    label(PX)
    ldrb(r6, [r1, 0])
    ldrb(r7, [r1, 1])
    strb(r6, [r0, 0])
    strb(r6, [r0, 1])
    strb(r6, [r0, 2])
    strb(r7, [r0, 3])
    strb(r7, [r0, 4])
    add(r0, 5)
    add(r1, 2)
    sub(r4, 1)
    bne(PX)
    mov(r7, 1)
    and_(r7, r5)
    mov(r6, 2)
    sub(r7, r6, r7)             # extra rows: 2 for even gy, 1 for odd
    label(RW)
    mov(r3, r2)
    mov(r4, {W * 5 // 8})
    label(CW)
    ldr(r6, [r3, 0])
    str(r6, [r0, 0])
    add(r3, 4)
    add(r0, 4)
    sub(r4, 1)
    bne(CW)
    sub(r7, 1)
    bne(RW)
    add(r5, 1)
    cmp(r5, {H})
    blt(GY)
"""

KERNELS = {'advect': ADVECT_V0, 'divergence': DIVERGENCE_V0, 'pressure': PRESSURE_GS_V0,
           'project': PROJECT_V0, 'colorize': COLORIZE_V0, 'expand': EXPAND_V0}

# v1: pointer increments moved into the FP dependency gaps
DIVERGENCE_V1 = DIVERGENCE_V0.replace("""    vsub(s1, s1, s0)
    vsub(s3, s3, s2)
    vadd(s1, s1, s3)
    vmul(s1, s1, s15)
    vstr(s1, [r0, 0])
    add(r0, 4)
    add(r1, 4)
    add(r2, 4)
    add(r3, 4)""", """    vsub(s1, s1, s0)
    vsub(s3, s3, s2)
    add(r1, 4)
    vadd(s1, s1, s3)
    add(r2, 4)
    vmul(s1, s1, s15)
    add(r3, 4)
    vstr(s1, [r0, 0])
    add(r0, 4)""")
assert DIVERGENCE_V1 != DIVERGENCE_V0

def aligned(src):
    """align(4) before every inner-loop label (taken branch to a misaligned 32-bit instr = +1)."""
    for lab in ('XL', 'L', 'PX', 'CW'):
        src = src.replace(f"    label({lab})\n", f"    align(4)\n    label({lab})\n")
    return src

VARIANTS = {k: {'v0': v, 'v0a': aligned(v)} for k, v in KERNELS.items()}
VARIANTS['divergence'].update(v1=DIVERGENCE_V1, v1a=aligned(DIVERGENCE_V1))

# splat: add (fx, fy, amt) * w to (u, v, dye) over a disc, w = max(0, 1 - d^2 * inv_r2)
# P: U, V, D addr | x0, x1, y0, y1 (ints, end exclusive) | cx, cy, fx, fy, amt, inv_r2, 1.0
SPLAT = f"""
@micropython.asm_thumb
def splat(r0):                  # P
    vldr(s0, [r0, 28])
    vldr(s1, [r0, 32])
    vldr(s2, [r0, 36])
    vldr(s3, [r0, 40])
    vldr(s4, [r0, 44])
    vldr(s5, [r0, 48])
    vldr(s6, [r0, 52])
    mov(r1, 0)
    vmov(s7, r1)                # 0.0
    ldr(r2, [r0, 20])           # y
    label(YL)
    ldr(r1, [r0, 12])           # x
    lsl(r3, r2, {SHIFT})
    add(r3, r3, r1)
    lsl(r3, r3, 2)
    ldr(r4, [r0, 0])
    add(r4, r4, r3)
    ldr(r5, [r0, 4])
    add(r5, r5, r3)
    ldr(r6, [r0, 8])
    add(r6, r6, r3)
    vmov(s9, r2)
    vcvt_f32_s32(s9, s9)
    vsub(s9, s9, s1)
    vmul(s9, s9, s9)            # dy^2
    vmov(s8, r1)
    vcvt_f32_s32(s8, s8)        # xf
    ldr(r7, [r0, 16])
    align(4)
    label(XL)
    vsub(s10, s8, s0)
    vmul(s10, s10, s10)
    vadd(s10, s10, s9)
    vmul(s10, s10, s5)
    vsub(s10, s6, s10)
    data(2, 0xFE85, 0x5A23)     # vmaxnm.f32 s10, s10, s7
    vldr(s11, [r4, 0])
    vmul(s12, s2, s10)
    vadd(s11, s11, s12)
    vstr(s11, [r4, 0])
    vldr(s11, [r5, 0])
    vmul(s12, s3, s10)
    vadd(s11, s11, s12)
    vstr(s11, [r5, 0])
    vldr(s11, [r6, 0])
    vmul(s12, s4, s10)
    vadd(s11, s11, s12)
    vstr(s11, [r6, 0])
    add(r4, 4)
    add(r5, 4)
    add(r6, 4)
    vadd(s8, s8, s6)
    add(r1, 1)
    cmp(r1, r7)
    blt(XL)
    add(r2, 1)
    ldr(r3, [r0, 24])
    cmp(r2, r3)
    blt(YL)
"""

# red-black Gauss-Seidel, rows y0..y1-1, with a spin barrier after every colour pass so two
# cores can split the rows. P (ints): y0, y1, iters, &my_flag, &other_flag,
# scratch: y, colour, iters left, pass count.  Single core: &other_flag = &my_flag.
PRESSURE_RB_V0 = f"""
@micropython.asm_thumb
def pressure_rb(r0, r1, r2):    # p, div, P
    movwt(r6, 0x3e800000)
    vmov(s15, r6)               # 0.25
    ldr(r6, [r2, 8])
    str(r6, [r2, 28])           # iters left
    mov(r6, 0)
    str(r6, [r2, 24])           # colour 0
    label(PASS)
    ldr(r6, [r2, 0])
    str(r6, [r2, 20])           # y = y0
    label(ROWL)
    ldr(r7, [r2, 20])
    ldr(r6, [r2, 24])
    add(r6, r6, r7)
    add(r6, 1)
    mov(r3, 1)
    and_(r6, r3)
    add(r6, 1)                  # x0 = 1 + ((1 + y + c) & 1)
    lsl(r7, r7, {SHIFT})
    add(r7, r7, r6)
    lsl(r7, r7, 2)              # byte offset of (x0, y)
    add(r5, r1, r7)             # div at i
    add(r3, r0, r7)
    sub(r3, 4)                  # p at i-1
    movwt(r6, {ROW - 4})
    sub(r4, r3, r6)             # p at i-W
    mov(r6, {(W - 2) // 2})
    align(4)
    label(XL)
    vldr(s0, [r3, 0])
    vldr(s1, [r3, 8])
    vldr(s2, [r4, 0])
    vldr(s3, [r3, {ROW + 4}])
    vldr(s4, [r5, 0])
    vadd(s0, s0, s1)
    vadd(s2, s2, s3)
    add(r4, 8)
    vadd(s0, s0, s2)
    add(r5, 8)
    vsub(s0, s0, s4)
    vmul(s0, s0, s15)
    vstr(s0, [r3, 4])
    add(r3, 8)
    sub(r6, 1)
    bne(XL)
    ldr(r7, [r2, 20])
    add(r7, 1)
    str(r7, [r2, 20])
    ldr(r6, [r2, 4])
    cmp(r7, r6)
    blt(ROWL)
    data(2, 0xF3BF, 0x8F5F)     # dmb sy
    ldr(r6, [r2, 32])
    add(r6, 1)
    str(r6, [r2, 32])           # pass count
    ldr(r7, [r2, 12])
    str(r6, [r7, 0])            # publish
    ldr(r7, [r2, 16])
    movwt(r5, 100000000)        # ~1.6 s: give up instead of hanging the board (Ctrl-C can't stop asm)
    label(SPIN)
    ldr(r3, [r7, 0])
    cmp(r3, r6)
    bge(GO)
    sub(r5, 1)
    bne(SPIN)
    mov(r6, 0)
    sub(r6, 1)
    str(r6, [r2, 28])           # iters left = -1 marks a barrier timeout
    b(END)
    label(GO)
    data(2, 0xF3BF, 0x8F5F)     # dmb sy
    ldr(r6, [r2, 24])
    mov(r7, 1)
    eor(r6, r7)
    str(r6, [r2, 24])           # colour ^= 1
    cmp(r6, 0)
    bne(PASS)
    ldr(r6, [r2, 28])
    sub(r6, 1)
    str(r6, [r2, 28])
    bne(PASS)
    label(END)
"""
KERNELS.update(splat=SPLAT, pressure_rb=PRESSURE_RB_V0)
VARIANTS.update(splat={'v0': SPLAT}, pressure_rb={'v0': PRESSURE_RB_V0})

# fused u+v advection: one back-trace, two bilinear samples, rows y0..y1-1.
# r10 = V - U and r11 = F2 - F1 (byte distances) let one pointer reach both fields.
# P: U, V-U, F1, F2-F1, y0, y1 (ints) | decay, 0.5, W-1.5, H-1.5, 1.0
ADVECT_UV_V0 = f"""
@micropython.asm_thumb
def advect_uv(r0):              # P
    mov(r2, r0)
    data(2, 0xE92D, 0x0C00)     # push {{r10, r11}}
    data(2, 0xF8D2, 0xA004)     # ldr.w r10, [r2, #4]
    data(2, 0xF8D2, 0xB00C)     # ldr.w r11, [r2, #12]
    ldr(r3, [r2, 0])            # U base
    vldr(s26, [r2, 24])
    vldr(s23, [r2, 28])
    vldr(s24, [r2, 32])
    vldr(s25, [r2, 36])
    vldr(s22, [r2, 40])
    mov(r6, 0)
    vmov(s27, r6)
    ldr(r5, [r2, 16])           # y
    label(YL)
    lsl(r6, r5, {SHIFT})
    add(r6, 1)
    lsl(r6, r6, 2)
    ldr(r0, [r2, 8])
    add(r0, r0, r6)             # F1 at i
    add(r1, r3, r6)             # U at i
    vmov(s21, r5)
    vcvt_f32_s32(s21, s21)      # yf
    vadd(s20, s22, s27)         # xf = 1.0
    mov(r4, {W - 2})
    align(4)
    label(XL)
    vldr(s0, [r1, 0])
    data(2, 0xEB01, 0x0C0A)     # add.w r12, r1, r10
    data(2, 0xEDDC, 0x0A00)     # vldr s1, [r12, #0]
    vsub(s0, s20, s0)
    vsub(s1, s21, s1)
    data(2, 0xFE80, 0x0A2B)     # vmaxnm.f32 s0, s0, s23
    data(2, 0xFEC0, 0x0AAB)     # vmaxnm.f32 s1, s1, s23
    data(2, 0xFE80, 0x0A4C)     # vminnm.f32 s0, s0, s24
    data(2, 0xFEC0, 0x0AEC)     # vminnm.f32 s1, s1, s25
    vcvt_s32_f32(s2, s0)
    vcvt_s32_f32(s3, s1)
    vmov(r6, s2)
    vmov(r7, s3)
    vcvt_f32_s32(s2, s2)
    vcvt_f32_s32(s3, s3)
    lsl(r7, r7, {SHIFT})
    vsub(s0, s0, s2)            # fx
    add(r7, r7, r6)
    vsub(s1, s1, s3)            # fy
    lsl(r7, r7, 2)
    add(r7, r7, r3)             # U at j
    vldr(s4, [r7, 0])
    vldr(s5, [r7, 4])
    vldr(s6, [r7, {ROW}])
    vldr(s7, [r7, {ROW + 4}])
    data(2, 0xEB07, 0x0C0A)     # add.w r12, r7, r10
    data(2, 0xED9C, 0x4A00)     # vldr s8, [r12, #0]
    data(2, 0xEDDC, 0x4A01)     # vldr s9, [r12, #4]
    data(2, 0xED9C, 0x5A80)     # vldr s10, [r12, #512]
    data(2, 0xEDDC, 0x5A81)     # vldr s11, [r12, #516]
    vsub(s5, s5, s4)
    vsub(s9, s9, s8)
    vsub(s7, s7, s6)
    vsub(s11, s11, s10)
    vmul(s5, s5, s0)
    vmul(s9, s9, s0)
    vmul(s7, s7, s0)
    vmul(s11, s11, s0)
    vadd(s4, s4, s5)
    vadd(s8, s8, s9)
    vadd(s6, s6, s7)
    vadd(s10, s10, s11)
    vsub(s6, s6, s4)
    vsub(s10, s10, s8)
    vmul(s6, s6, s1)
    vmul(s10, s10, s1)
    vadd(s4, s4, s6)
    vadd(s8, s8, s10)
    vmul(s4, s4, s26)
    vmul(s8, s8, s26)
    vstr(s4, [r0, 0])
    data(2, 0xEB00, 0x0C0B)     # add.w r12, r0, r11
    data(2, 0xED8C, 0x4A00)     # vstr s8, [r12, #0]
    add(r0, 4)
    add(r1, 4)
    vadd(s20, s20, s22)
    sub(r4, 1)
    beq(XE)
    b(XL)
    label(XE)
    add(r5, 1)
    ldr(r6, [r2, 20])
    cmp(r5, r6)
    blt(YL)
    data(2, 0xE8BD, 0x0C00)     # pop {{r10, r11}}
"""
assert ROW == 512               # the encoded vldr offsets above assume W = 128

# single-field advect over rows y0..y1-1; P as ADVECT_V0 plus y0, y1 at 28, 32
ADVECT_R_V0 = aligned(ADVECT_V0.replace("def advect(", "def advect_r(").replace(f"""    vadd(s21, s22, s27)         # yf = 1.0
    movwt(r6, {ROW + 4})
    add(r0, r0, r6)
    add(r1, r1, r6)
    add(r2, r2, r6)
    mov(r5, {H - 2})""", f"""    ldr(r6, [r7, 28])           # y0
    vmov(s21, r6)
    vcvt_f32_s32(s21, s21)      # yf = y0
    lsl(r6, r6, {SHIFT})
    add(r6, 1)
    lsl(r6, r6, 2)
    add(r0, r0, r6)
    add(r1, r1, r6)
    add(r2, r2, r6)
    ldr(r5, [r7, 32])
    ldr(r6, [r7, 28])
    sub(r5, r5, r6)             # rows"""))
assert 'advect_r' in ADVECT_R_V0 and 'yf = y0' in ADVECT_R_V0
KERNELS.update(advect_uv=ADVECT_UV_V0, advect_r=ADVECT_R_V0)
VARIANTS.update(advect_uv={'v0': ADVECT_UV_V0}, advect_r={'v0': ADVECT_R_V0})
