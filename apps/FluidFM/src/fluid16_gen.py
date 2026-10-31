#!/usr/bin/env python3
"""Stable fluids at 160x120 with float16 fields (u, v, dye, div) and a float32 pressure field.
The M33 has no half-precision load: two cells come in as one (possibly unaligned) word via
ldr -> vmov -> vcvtb/vcvtt, results go out via vcvtb.f16.f32 -> vmov -> strh.
Builds fluid16_k.mpy (kernels + core1 worker); --upload copies it to the board."""
import json, os, shutil, subprocess, sys

W, H = 160, 120
R16, R32 = 2 * W, 4 * W                 # row bytes: float16 / float32
assert W == 160                         # y*W is computed as (y << 5) * 5 below
HROWS = (H - 2) // 2                    # 59 interior rows per core


def mul160(r):
    """r = r * 160, as (r << 5) + (r << 7)."""
    enc = {7: 'data(2, 0xEB07, 0x0787)     # add.w r7, r7, r7, lsl #2',
           6: 'data(2, 0xEB06, 0x0686)     # add.w r6, r6, r6, lsl #2',
           3: 'data(2, 0xEB03, 0x0383)     # add.w r3, r3, r3, lsl #2'}[r]
    return f"    lsl(r{r}, r{r}, 5)\n    {enc}\n"

CLAMP = """    data(2, 0xFE80, 0x0A2B)     # vmaxnm.f32 s0, s0, s23
    data(2, 0xFEC0, 0x0AAB)     # vmaxnm.f32 s1, s1, s23
    data(2, 0xFE80, 0x0A4C)     # vminnm.f32 s0, s0, s24
    data(2, 0xFEC0, 0x0AEC)     # vminnm.f32 s1, s1, s25
"""
BACKTRACE = CLAMP + """    vcvt_s32_f32(s2, s0)
    vcvt_s32_f32(s3, s1)
    vmov(r6, s2)
    vmov(r7, s3)
    vcvt_f32_s32(s2, s2)
    vcvt_f32_s32(s3, s3)
    data(2, 0xFB07, 0x670E)     # mla r7, r7, lr, r6 (lr = W)
    vsub(s0, s0, s2)            # fx
    data(2, 0xEB03, 0x0747)     # add.w r7, r3, r7, lsl #1: src at j (2-byte aligned: ldr is unaligned)
    vsub(s1, s1, s3)            # fy
"""

# fused u+v advect. P: U, V-U, F1, F2-F1, &row counter (first row, in SRAM), y1 | decay, 0.5, W-1.5, H-1.5, 1.0
ADVECT_UV = f"""
@micropython.asm_thumb
def advect_uv(r0):              # P
    mov(r2, r0)
    data(2, 0xE92D, 0x0F00)     # push {{r8, r9, r10, r11}}
    data(2, 0xF8D2, 0xA004)     # ldr.w r10, [r2, #4]
    data(2, 0xF8D2, 0xB00C)     # ldr.w r11, [r2, #12]
    data(2, 0xF1AB, 0x0B02)     # sub.w r11, r11, #2
    ldr(r3, [r2, 0])
    vldr(s26, [r2, 24])
    vldr(s23, [r2, 28])
    vldr(s24, [r2, 32])
    vldr(s25, [r2, 36])
    vldr(s22, [r2, 40])
    data(2, 0xF240, 0x0EA0)     # movw lr, #160 (lr is saved by the asm_thumb prologue)
    mov(r6, 0)
    vmov(s27, r6)
    label(YL)
    ldr(r0, [r2, 16])           # row counter (SRAM: ldrex/strex)
    label(YR)                   # claim the next row: both cores share the counter, so neither waits on the other
    data(2, 0xE850, 0x5F00)     # ldrex r5, [r0, #0]
    add(r6, r5, 1)
    data(2, 0xE840, 0x6100)     # strex r1, r6, [r0, #0]
    cmp(r1, 0)
    bne(YR)
    ldr(r6, [r2, 20])
    cmp(r5, r6)
    bge_w(YD)
    mov(r6, r5)
{mul160(6)}    add(r6, 1)
    lsl(r6, r6, 1)
    ldr(r0, [r2, 8])
    add(r0, r0, r6)             # F1 at i
    add(r1, r3, r6)             # U at i
    vmov(s21, r5)
    vcvt_f32_s32(s21, s21)
    vadd(s20, s22, s27)
    mov(r4, {W - 2})
    align(4)
    label(XL)
    ldrh(r6, [r1, 0])
    data(2, 0xF831, 0x700A)     # ldrh.w r7, [r1, r10]
    vmov(s0, r6)
    data(2, 0xEEB2, 0x0A40)     # vcvtb.f32.f16 s0, s0
    vmov(s1, r7)                # one instruction after its load: no load-use stall
    data(2, 0xEEF2, 0x0A60)     # vcvtb.f32.f16 s1, s1
    vsub(s0, s20, s0)
    vsub(s1, s21, s1)
{BACKTRACE}    ldr(r6, [r7, 0])
    data(2, 0xEB07, 0x0C0A)     # add.w r12, r7, r10
    data(2, 0xF8D7, 0x8140)     # ldr.w r8, [r7, #320]
    vmov(s8, r6)                # loads interleaved with the moves/converts
    data(2, 0xF8DC, 0x9000)     # ldr.w r9, [r12, #0]
    data(2, 0xEEF2, 0x2AC4)     # vcvtt.f32.f16 s5, s8
    data(2, 0xF8DC, 0xC140)     # ldr.w r12, [r12, #320]
    data(2, 0xEE04, 0x8A90)     # vmov s9, r8
    data(2, 0xEEB2, 0x2A44)     # vcvtb.f32.f16 s4, s8
    data(2, 0xEE05, 0x9A10)     # vmov s10, r9
    data(2, 0xEEF2, 0x3AE4)     # vcvtt.f32.f16 s7, s9
    data(2, 0xEE05, 0xCA90)     # vmov s11, r12
    data(2, 0xEEB2, 0x3A64)     # vcvtb.f32.f16 s6, s9
    data(2, 0xEEF2, 0x6AC5)     # vcvtt.f32.f16 s13, s10
    data(2, 0xEEB2, 0x6A45)     # vcvtb.f32.f16 s12, s10
    data(2, 0xEEF2, 0x7AE5)     # vcvtt.f32.f16 s15, s11
    data(2, 0xEEB2, 0x7A65)     # vcvtb.f32.f16 s14, s11
    vsub(s5, s5, s4)
    vsub(s13, s13, s12)
    vsub(s7, s7, s6)
    vsub(s15, s15, s14)
    vmul(s5, s5, s0)
    vmul(s13, s13, s0)
    vmul(s7, s7, s0)
    vmul(s15, s15, s0)
    vadd(s4, s4, s5)
    vadd(s12, s12, s13)
    vadd(s6, s6, s7)
    vadd(s14, s14, s15)
    vsub(s6, s6, s4)
    vsub(s14, s14, s12)
    vmul(s6, s6, s1)
    vmul(s14, s14, s1)
    vadd(s4, s4, s6)
    vadd(s12, s12, s14)
    vmul(s4, s4, s26)
    vmul(s12, s12, s26)
    data(2, 0xEEB3, 0x2A42)     # vcvtb.f16.f32 s4, s4
    data(2, 0xEEB3, 0x6A46)     # vcvtb.f16.f32 s12, s12
    vmov(r6, s4)
    add(r1, 2)
    data(2, 0xEE16, 0x8A10)     # vmov r8, s12
    data(2, 0xF820, 0x6B02)     # strh.w r6, [r0], #2
    data(2, 0xF820, 0x800B)     # strh.w r8, [r0, r11] (r0 already advanced: r11 = F2-F1-2)
    vadd(s20, s20, s22)
    sub(r4, 1)
    bne_w(XL)                   # wide conditional branch: the body is too long for bne
    b(YL)
    label(YD)
    data(2, 0xE8BD, 0x0F00)     # pop {{r8, r9, r10, r11}}
"""

# single-field advect, rows claimed from a shared counter up to y1-1.
# P: u, v | decay, 0.5, W-1.5, H-1.5, 1.0 | &row counter (first row, in SRAM), y1, dst (written here)
ADVECT_R = f"""
@micropython.asm_thumb
def advect_r(r0, r1, r2):       # dst, src, P
    mov(r3, r1)
    mov(r7, r2)
    data(2, 0xE92D, 0x0300)     # push {{r8, r9}}
    ldr(r1, [r7, 0])
    ldr(r2, [r7, 4])
    vldr(s26, [r7, 8])
    vldr(s23, [r7, 12])
    vldr(s24, [r7, 16])
    vldr(s25, [r7, 20])
    vldr(s22, [r7, 24])
    data(2, 0xF240, 0x0EA0)     # movw lr, #160 (lr is saved by the asm_thumb prologue)
    mov(r6, 0)
    vmov(s27, r6)
    str(r0, [r7, 36])           # dst base, reloaded every row
    vmov(s28, r7)               # P: r7 is a temporary in the row body
    label(YL)
    vmov(r7, s28)
    ldr(r0, [r7, 28])           # row counter (SRAM: ldrex/strex)
    label(YR)                   # claim the next row: both cores share the counter, so neither waits on the other
    data(2, 0xE850, 0x5F00)     # ldrex r5, [r0, #0]
    add(r6, r5, 1)
    data(2, 0xE840, 0x6100)     # strex r1, r6, [r0, #0]
    cmp(r1, 0)
    bne(YR)
    ldr(r6, [r7, 32])
    cmp(r5, r6)
    bge_w(YD)
    vmov(s21, r5)
    vcvt_f32_s32(s21, s21)
    mov(r6, r5)
{mul160(6)}    add(r6, 1)
    lsl(r6, r6, 1)
    ldr(r0, [r7, 36])
    add(r0, r0, r6)
    ldr(r1, [r7, 0])
    add(r1, r1, r6)
    ldr(r2, [r7, 4])
    add(r2, r2, r6)
    vadd(s20, s22, s27)
    mov(r4, {W - 2})
    align(4)
    label(XL)
    ldrh(r6, [r1, 0])
    ldrh(r7, [r2, 0])
    vmov(s0, r6)
    data(2, 0xEEB2, 0x0A40)     # vcvtb.f32.f16 s0, s0
    vmov(s1, r7)                # one instruction after its load: no load-use stall
    data(2, 0xEEF2, 0x0A60)     # vcvtb.f32.f16 s1, s1
    vsub(s0, s20, s0)
    vsub(s1, s21, s1)
{BACKTRACE}    ldr(r6, [r7, 0])
    data(2, 0xF8D7, 0x8140)     # ldr.w r8, [r7, #320]
    vmov(s8, r6)
    data(2, 0xEEF2, 0x2AC4)     # vcvtt.f32.f16 s5, s8
    data(2, 0xEE04, 0x8A90)     # vmov s9, r8
    data(2, 0xEEB2, 0x2A44)     # vcvtb.f32.f16 s4, s8
    data(2, 0xEEF2, 0x3AE4)     # vcvtt.f32.f16 s7, s9
    data(2, 0xEEB2, 0x3A64)     # vcvtb.f32.f16 s6, s9
    vsub(s5, s5, s4)
    vsub(s7, s7, s6)
    vmul(s5, s5, s0)
    vmul(s7, s7, s0)
    vadd(s4, s4, s5)
    vadd(s6, s6, s7)
    vsub(s6, s6, s4)            # serial tail: pointer/counter updates fill the FP stalls
    add(r1, 2)
    vmul(s6, s6, s1)
    add(r2, 2)
    vadd(s4, s4, s6)
    vadd(s20, s20, s22)
    vmul(s4, s4, s26)
    sub(r4, 1)                  # last flag-setting instruction before beq
    data(2, 0xEEB3, 0x2A42)     # vcvtb.f16.f32 s4, s4
    vmov(r6, s4)
    data(2, 0xF820, 0x6B02)     # strh.w r6, [r0], #2
    bne_w(XL)                   # wide conditional branch: the body is too long for bne
    b(YL)
    label(YD)
    data(2, 0xE8BD, 0x0300)     # pop {{r8, r9}}
"""

# div[i] = 0.5 * ((u[i+1] - u[i-1]) + (v[i+W] - v[i-W])), rows 1..H-2 claimed from a shared counter
DIVERGENCE_H = f"""
@micropython.asm_thumb
def divergence_h(r0, r1, r2, r3):   # div, u, v (float16), &row counter (first row, in SRAM)
    movwt(r6, 0x3f000000)
    vmov(s15, r6)
    vmov(s16, r0)
    vmov(s17, r1)
    vmov(s18, r2)
    vmov(s19, r3)
    label(YL)
    vmov(r0, s19)               # row counter (SRAM: ldrex/strex)
    label(YR)                   # claim the next row: both cores share the counter
    data(2, 0xE850, 0x5F00)     # ldrex r5, [r0, #0]
    add(r6, r5, 1)
    data(2, 0xE840, 0x6100)     # strex r1, r6, [r0, #0]
    cmp(r1, 0)
    bne(YR)
    cmp(r5, {H - 1})
    bge_w(YD)
    mov(r6, r5)
{mul160(6)}    lsl(r6, r6, 1)              # y * R16
    vmov(r0, s16)
    add(r0, r0, r6)
    add(r0, 2)                  # div at i
    vmov(r1, s17)
    add(r1, r1, r6)             # u at i-1
    vmov(r3, s18)
    add(r3, r3, r6)
    movwt(r7, {R16})
    sub(r2, r3, r7)
    add(r2, 2)                  # v at i-W
    add(r3, r3, r7)
    add(r3, 2)                  # v at i+W
    mov(r4, {W - 2})
    align(4)
    label(XL)
    ldrh(r6, [r1, 0])
    ldrh(r7, [r1, 4])
    vmov(s0, r6)
    vmov(s1, r7)
    ldrh(r6, [r2, 0])
    ldrh(r7, [r3, 0])
    vmov(s2, r6)
    vmov(s3, r7)
    data(2, 0xEEB2, 0x0A40)     # vcvtb.f32.f16 s0, s0
    data(2, 0xEEF2, 0x0A60)     # vcvtb.f32.f16 s1, s1
    data(2, 0xEEB2, 0x1A41)     # vcvtb.f32.f16 s2, s2
    data(2, 0xEEF2, 0x1A61)     # vcvtb.f32.f16 s3, s3
    vsub(s1, s1, s0)
    vsub(s3, s3, s2)
    add(r1, 2)
    vadd(s1, s1, s3)
    add(r2, 2)
    vmul(s1, s1, s15)
    add(r3, 2)
    data(2, 0xEEF3, 0x0A60)     # vcvtb.f16.f32 s1, s1
    vmov(r6, s1)
    strh(r6, [r0, 0])
    add(r0, 2)
    sub(r4, 1)
    bne(XL)
    b(YL)
    label(YD)
"""

# red-black SOR on float32 p with float16 div, rows y0..y1-1, spin barrier after each colour pass.
# p = (1 - w) * p + (w / 4) * (neighbours - div): (1 - w) * p off the critical path. vmul + vadd, not
# vfma: vfma costs 3 cycles on the M33.
# w = 1 is plain Gauss-Seidel, bit for bit.
# P (ints): y0, y1, iters, &my_flag, &other_flag, scratch: y, colour, iters left, pass count | w/4, 1-w (float bits)
PRESSURE_RB = f"""
@micropython.asm_thumb
def pressure_rb(r0, r1, r2):    # p (f32), div (f16), P
    vldr(s15, [r2, 36])         # w / 4
    vldr(s14, [r2, 40])         # 1 - w
    ldr(r6, [r2, 8])
    str(r6, [r2, 28])
    mov(r6, 0)
    str(r6, [r2, 24])
    label(PASS)
    ldr(r6, [r2, 0])
    str(r6, [r2, 20])
    label(ROWL)
    ldr(r7, [r2, 20])
    ldr(r6, [r2, 24])
    add(r6, r6, r7)
    add(r6, 1)
    mov(r3, 1)
    and_(r6, r3)
    add(r6, 1)                  # x0 = 1 + ((1 + y + c) & 1)
{mul160(7)}    add(r7, r7, r6)             # i
    lsl(r3, r7, 1)
    add(r5, r1, r3)             # div at i
    lsl(r3, r7, 2)
    add(r3, r0, r3)
    sub(r3, 4)                  # p at i-1
    movwt(r6, {R32 - 4})
    sub(r4, r3, r6)             # p at i-W
    mov(r6, {(W - 2) // 2})
    align(4)
    label(XL)
    ldrh(r7, [r5, 0])
    vldr(s0, [r3, 0])
    vldr(s1, [r3, 8])
    vldr(s2, [r4, 0])
    vldr(s3, [r3, {R32 + 4}])
    vldr(s5, [r3, 4])           # p at i (old)
    vmov(s4, r7)
    vadd(s0, s0, s1)
    data(2, 0xEEB2, 0x2A42)     # vcvtb.f32.f16 s4, s4
    vadd(s2, s2, s3)
    vmul(s5, s5, s14)           # (1 - w) * p, off the critical path
    add(r4, 8)
    vadd(s0, s0, s2)
    add(r5, 4)
    vsub(s0, s0, s4)
    vmul(s0, s0, s15)
    vadd(s5, s5, s0)
    vstr(s5, [r3, 4])
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
    str(r6, [r2, 32])
    ldr(r7, [r2, 12])
    str(r6, [r7, 0])
    ldr(r7, [r2, 16])
    movwt(r5, 100000000)        # ~1.6 s: give up instead of hanging the board
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
    str(r6, [r2, 24])
    cmp(r6, 0)
    bne(PASS)
    ldr(r6, [r2, 28])
    sub(r6, 1)
    str(r6, [r2, 28])
    bne(PASS)
    label(END)
"""

# u[i] -= 0.5 * (pr - pl); v[i] -= 0.5 * (pd - pu). u, v float16, p float32, rows 1..H-2 claimed from a shared counter
PROJECT_H = f"""
@micropython.asm_thumb
def project_h(r0, r1, r2, r3): # u, v (f16), p (f32), &row counter (first row, in SRAM)
    movwt(r6, 0x3f000000)
    vmov(s15, r6)
    vmov(s16, r0)
    vmov(s17, r1)
    vmov(s18, r2)
    vmov(s19, r3)
    label(YL)
    vmov(r0, s19)               # row counter (SRAM: ldrex/strex)
    label(YR)                   # claim the next row: both cores share the counter
    data(2, 0xE850, 0x5F00)     # ldrex r5, [r0, #0]
    add(r6, r5, 1)
    data(2, 0xE840, 0x6100)     # strex r1, r6, [r0, #0]
    cmp(r1, 0)
    bne(YR)
    cmp(r5, {H - 1})
    bge_w(YD)
    mov(r6, r5)
{mul160(6)}    lsl(r6, r6, 1)              # y * R16
    vmov(r0, s16)
    add(r0, r0, r6)
    add(r0, 2)                  # u at i
    vmov(r1, s17)
    add(r1, r1, r6)
    add(r1, 2)                  # v at i
    lsl(r6, r6, 1)              # y * R32
    vmov(r2, s18)
    add(r2, r2, r6)             # p at i-1
    movwt(r7, {R32 - 4})
    sub(r3, r2, r7)             # p at i-W
    mov(r4, {W - 2})
    align(4)
    label(XL)
    ldrh(r6, [r0, 0])
    ldrh(r7, [r1, 0])
    vldr(s0, [r2, 0])
    vldr(s1, [r2, 8])
    vldr(s2, [r3, 0])
    vldr(s3, [r2, {R32 + 4}])
    vmov(s4, r6)
    vmov(s5, r7)
    vsub(s1, s1, s0)
    vsub(s3, s3, s2)
    data(2, 0xEEB2, 0x2A42)     # vcvtb.f32.f16 s4, s4
    data(2, 0xEEF2, 0x2A62)     # vcvtb.f32.f16 s5, s5
    vmul(s1, s1, s15)
    vmul(s3, s3, s15)
    vsub(s4, s4, s1)
    vsub(s5, s5, s3)
    data(2, 0xEEB3, 0x2A42)     # vcvtb.f16.f32 s4, s4
    data(2, 0xEEF3, 0x2A62)     # vcvtb.f16.f32 s5, s5
    vmov(r6, s4)
    vmov(r7, s5)
    strh(r6, [r0, 0])
    strh(r7, [r1, 0])
    add(r0, 2)
    add(r1, 2)
    add(r2, 4)
    add(r3, 4)
    sub(r4, 1)
    bne(XL)
    b(YL)
    label(YD)
"""

# 2x2 pixels per cell straight into the fb: pal[usat8(int(dye * scale))], 60 grid rows (all cells)
RENDER_H = f"""
@micropython.asm_thumb
def render_h(r0, r1, r2, r3):   # fb, dye (f16), palette, scale (float bits)
    vmov(s15, r3)
    mov(r7, {H // 2})
    label(YL)
    mov(r6, {W // 2})
    align(4)
    label(XL)
    ldr(r4, [r1, 0])
    vmov(s0, r4)
    data(2, 0xEEF2, 0x0AC0)     # vcvtt.f32.f16 s1, s0
    data(2, 0xEEB2, 0x0A40)     # vcvtb.f32.f16 s0, s0
    vmul(s0, s0, s15)
    vmul(s1, s1, s15)
    vcvt_s32_f32(s0, s0)
    vcvt_s32_f32(s1, s1)
    vmov(r4, s0)
    vmov(r5, s1)
    data(2, 0xF384, 0x0408)     # usat r4, #8, r4
    data(2, 0xF385, 0x0508)     # usat r5, #8, r5
    add(r4, r4, r2)
    add(r5, r5, r2)
    ldrb(r4, [r4, 0])
    ldrb(r5, [r5, 0])
    data(2, 0xEA44, 0x2404)     # orr.w r4, r4, r4, lsl #8
    data(2, 0xEA45, 0x2505)     # orr.w r5, r5, r5, lsl #8
    data(2, 0xEA44, 0x4405)     # orr.w r4, r4, r5, lsl #16
    str(r4, [r0, 0])
    data(2, 0xF8C0, 0x4140)     # str.w r4, [r0, #320]
    add(r0, 4)
    add(r1, 4)
    sub(r6, 1)
    bne(XL)
    movwt(r4, 320)
    add(r0, r0, r4)             # skip the duplicated fb row
    sub(r7, 1)
    bne(YL)
"""

# splat on float16 u, v, dye. P: U, V, D | x0, x1, y0, y1 | cx, cy, fx, fy, amt, inv_r2, 1.0
def _rmw(p, s):
    return f"""    ldrh(r3, [r{p}, 0])
    vmov(s11, r3)
    data(2, 0xEEF2, 0x5A65)     # vcvtb.f32.f16 s11, s11
    vmul(s12, s{s}, s10)
    vadd(s11, s11, s12)
    data(2, 0xEEF3, 0x5A65)     # vcvtb.f16.f32 s11, s11
    vmov(r3, s11)
    strh(r3, [r{p}, 0])
"""

SPLAT = f"""
@micropython.asm_thumb
def splat(r0):                  # P (... 1.0, force gain, dye gain)
    vldr(s0, [r0, 28])
    vldr(s1, [r0, 32])
    vldr(s2, [r0, 36])
    vldr(s3, [r0, 40])
    vldr(s4, [r0, 44])
    vldr(s13, [r0, 56])
    vmul(s2, s2, s13)
    vmul(s3, s3, s13)
    vldr(s13, [r0, 60])
    vmul(s4, s4, s13)
    vldr(s5, [r0, 48])
    vldr(s6, [r0, 52])
    mov(r1, 0)
    vmov(s7, r1)
    ldr(r2, [r0, 20])
    label(YL)
    ldr(r1, [r0, 12])
    mov(r3, r2)
{mul160(3)}    add(r3, r3, r1)
    lsl(r3, r3, 1)
    ldr(r4, [r0, 0])
    add(r4, r4, r3)
    ldr(r5, [r0, 4])
    add(r5, r5, r3)
    ldr(r6, [r0, 8])
    add(r6, r6, r3)
    vmov(s9, r2)
    vcvt_f32_s32(s9, s9)
    vsub(s9, s9, s1)
    vmul(s9, s9, s9)
    vmov(s8, r1)
    vcvt_f32_s32(s8, s8)
    ldr(r7, [r0, 16])
    align(4)
    label(XL)
    vsub(s10, s8, s0)
    vmul(s10, s10, s10)
    vadd(s10, s10, s9)
    vmul(s10, s10, s5)
    vsub(s10, s6, s10)
    data(2, 0xFE85, 0x5A23)     # vmaxnm.f32 s10, s10, s7
{_rmw(4, 2)}{_rmw(5, 3)}{_rmw(6, 4)}    add(r4, 2)
    add(r5, 2)
    add(r6, 2)
    vadd(s8, s8, s6)
    add(r1, 1)
    cmp(r1, r7)
    blt(XL)
    add(r2, 1)
    ldr(r3, [r0, 24])
    cmp(r2, r3)
    blt(YL)
"""

KERNELS = {'advect_uv': ADVECT_UV, 'advect_r': ADVECT_R, 'divergence_h': DIVERGENCE_H,
           'pressure_rb': PRESSURE_RB, 'project_h': PROJECT_H, 'render_h': RENDER_H, 'splat': SPLAT}

def build():
    from fluid_k_build import EXTRA            # zero, worker, sev
    src = '# generated by fluid16_gen.py - do not edit\n' + ''.join(KERNELS.values()) + EXTRA
    open('fluid16_k.py', 'w').write(src)
    subprocess.run([(shutil.which('mpy-cross') or os.path.expanduser('~/.local/bin/mpy-cross')), '-march=armv7emsp', '-o', 'fluid16_k.mpy', 'fluid16_k.py'], check=True)
    return os.path.getsize('fluid16_k.mpy')


# ---------------------------------------------------------------- bilinear render (option A)
# Thumb-2 encodings for instructions MicroPython's assembler lacks. Every encoding the kernels use is cached
# in enc_cache.json, so building needs no encoder; a new instruction needs an encoder script that prints
# 'data(2, 0x..., 0x...)  # ual' lines (set FLUID_ENCODER to its path).
ENCODER = os.environ.get('FLUID_ENCODER', os.path.expanduser('~/.claude/skills/synced/a8aaa3e1-db8a-4915-8eaa-820ccb9f13a6_'
                         '67affe4d-478f-4caf-91a4-f0bd6889dc85/cortex-m33-assembly/scripts/encode.py'))
ENC_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'enc_cache.json')
try:
    _ENC = json.load(open(ENC_CACHE))
except OSError:
    _ENC = {}

def enc(*ual):
    """UAL lines -> '    data(...)  # ual' lines, from enc_cache.json or the encoder."""
    new = [u for u in ual if u not in _ENC]
    if new:
        out = subprocess.run([sys.executable, ENCODER, *new], capture_output=True, text=True, check=True).stdout
        lines = [l for l in out.splitlines() if l.startswith('data(')]
        assert len(lines) == len(new), out
        _ENC.update(zip(new, lines))
        json.dump(_ENC, open(ENC_CACHE, 'w'), indent=0, sort_keys=True)
    return ''.join('    ' + _ENC[u] + '\n' for u in ual)

# idx[i] = usat8(int(dye[i] * scale)), two cells per word, 60 grid rows
INDEX_H = f"""
@micropython.asm_thumb
def index_h(r0, r1, r2):        # idx bytes, dye (f16), scale (float bits)
    vmov(s15, r2)
    movwt(r5, {(H // 2) * (W // 2)})
    align(4)
    label(L)
    ldr(r3, [r1, 0])
    vmov(s0, r3)
{enc('vcvtt.f32.f16 s1, s0', 'vcvtb.f32.f16 s0, s0')}    vmul(s0, s0, s15)
    vmul(s1, s1, s15)
    vcvt_s32_f32(s0, s0)
    vcvt_s32_f32(s1, s1)
    vmov(r3, s0)
    vmov(r4, s1)
{enc('usat r3, #8, r3', 'usat r4, #8, r4', 'orr.w r3, r3, r4, lsl #8')}    strh(r3, [r0, 0])
    add(r0, 2)
    add(r1, 4)
    sub(r5, 1)
    bne(L)
"""

def _word(srcs):
    """Four palette pixels from bytes of (reg, byte) pairs -> r10. All four extracts, then all four
    loads, then the ORs: no result is used by the very next instruction (ldr->use stalls 1)."""
    t = ['r10', 'r8', 'r9', 'r12']
    out = [f'uxtb.w {t[k]}, {r}' + (f', ror #{8 * b}' if b else '') for k, (r, b) in enumerate(srcs)]
    out += [f'ldrb.w {t[k]}, [r2, {t[k]}]' for k in range(4)]
    out += [f'orr.w r10, r10, {t[k]}, lsl #{8 * k}' for k in (1, 2, 3)]
    return out

def _row(even, mid):            # 8 pixels: even[0] mid[0] even[1] mid[1] ... as two words
    lo = _word([(even, 0), (mid, 0), (even, 1), (mid, 1)])
    hi = _word([(even, 2), (mid, 2), (even, 3), (mid, 3)])
    return lo, hi

_t0, _t1 = _row('r4', 'r5')     # fb row 2y:   a, (a + a1) / 2
_b0, _b1 = _row('r6', 'r7')     # fb row 2y+1: (a + b) / 2, centre
BILIN_H = f"""
@micropython.asm_thumb
def bilin_h(r0, r1, r2, r3):    # fb, idx bytes (one zero pad row after the grid), palette, ctl
{enc('push {r8, r9, r10, r11}', 'ldr.w r11, [r3, #12]', 'mov.w lr, r3')}    label(YL)
{enc('ldr.w r4, [lr, #0]', 'cmp.w r11, r4')}    bhi(NL_)
{enc('str.w r0, [lr, #20]', 'ldr.w r0, [lr, #16]')}    label(NL_)                  # strip rows: render into the line buffer
    mov(r3, {W // 4})
    align(4)
    label(XL)
    ldr(r4, [r1, 0])            # a: cells x..x+3 of row y
{enc('ldr.w r8, [r1, #1]', 'ldr.w r9, [r1, #160]', 'ldr.w r12, [r1, #161]',
     'uhadd8 r5, r4, r8', 'uhadd8 r12, r9, r12', 'uhadd8 r6, r4, r9', 'uhadd8 r7, r5, r12')}{enc(*_t0)}{enc('str.w r10, [r0, #0]')}{enc(*_t1)}{enc('str.w r10, [r0, #4]')}{enc(*_b0)}{enc('str.w r10, [r0, #320]')}{enc(*_b1)}{enc('str.w r10, [r0, #324]')}    add(r0, 8)
    add(r1, 4)
    sub(r3, 1)
    beq(XE)
    b(XL)
    label(XE)
{enc('ldr.w r4, [lr, #0]', 'cmp.w r11, r4')}    bhi(NB_)
{enc('ldr.w r5, [lr, #4]', 'ldr.w r6, [lr, #8]', 'add.w r7, r11, r11', 'sub.w r7, r7, #1', 'ldrb.w r8, [r6, r7]',
     'sub.w r10, r7, #1', 'ldrb.w r9, [r6, r10]', 'orr.w r8, r8, r8, lsl #8', 'orr.w r8, r8, r8, lsl #16',
     'orr.w r9, r9, r9, lsl #8', 'orr.w r9, r9, r9, lsl #16', 'sub.w r12, r0, #320')}    mov(r3, 64)
    label(BB)
{enc('ldr r4, [r5], #4')}    lsr(r4, r4, 4)
    cmp(r4, r7)
    blt(BN)
{enc('str.w r9, [r12, #320]')}    beq(BN)
{enc('str.w r8, [r12, #0]')}    label(BN)
{enc('add.w r12, r12, #5')}    sub(r3, 1)
    bne(BB)
{enc('ldr.w r4, [lr, #16]', 'ldr.w r5, [lr, #20]')}    mov(r3, 40)
    label(CP)                   # finished row pair -> fb: bar pixels never show bare fluid
{enc('ldm r4!, {r6, r7, r8, r9}', 'stm r5!, {r6, r7, r8, r9}')}    sub(r3, 1)
    bne(CP)
{enc('ldr.w r0, [lr, #20]')}    movwt(r4, 320)
    add(r0, r0, r4)
    label(NB_)
    movwt(r4, 320)
    add(r0, r0, r4)             # skip the fb row already written
{enc('subs.w r11, r11, #1')}    beq(YE)
    b(YL)
    label(YE)
{enc('pop {r8, r9, r10, r11}')}"""
KERNELS.update(index_h=INDEX_H, bilin_h=BILIN_H)


# ---------------------------------------------------------------- 2-operator FM synth
# state: sine table (1024 x int16 Q15), voice count, then 10 words per voice:
#   cph, cinc, mph, minc, idx, tidx, a, t, pan (pL | pR << 16, Q8), pad
# per sample: mph += minc; m = sin[mph >> 22]; idx += (tidx - idx) >> 9; cph += cinc
#             s = sin[(cph + m * idx) >> 22]; a += (t - a) >> 9; v = (s * a) >> 15
#             accL += v * pL; accR += v * pR      (acc: n x (L, R) int32, Q8, added to)
FM_RENDER = f"""
@micropython.asm_thumb
def fm_render(r0, r1, r2):      # acc, n, state
{enc('push.w {r8, r9, r10, r11}', 'ldr.w lr, [r2, #0]')}    ldr(r3, [r2, 4])
    add(r2, 8)
    label(VL)
    push({{r0, r1, r2, r3}})
{enc('ldm.w r2, {r3, r4, r5, r6, r7, r8, r9, r10, r11}')}    align(4)
    label(SL)
{enc('add.w r5, r5, r6', 'lsr.w r12, r5, #22', 'ldrsh.w r12, [lr, r12, lsl #1]', 'add.w r3, r3, r4',
     'sub.w r2, r8, r7', 'add.w r7, r7, r2, asr #9', 'mla r12, r12, r7, r3', 'lsr.w r12, r12, #22',
     'ldrsh.w r12, [lr, r12, lsl #1]', 'sub.w r2, r10, r9', 'add.w r9, r9, r2, asr #9',
     'mul.w r12, r12, r9', 'asr.w r12, r12, #15')}    ldr(r2, [r0, 0])
{enc('smlabb r2, r12, r11, r2', 'str r2, [r0], #4')}    ldr(r2, [r0, 0])
{enc('smlabt r2, r12, r11, r2', 'str r2, [r0], #4')}    sub(r1, 1)
    bne(SL)
{enc('ldr.w r12, [sp, #8]', 'stm.w r12, {r3, r4, r5, r6, r7, r8, r9, r10, r11}')}    pop({{r0, r1, r2, r3}})
    add(r2, 40)
    sub(r3, 1)
    bne(VL)
{enc('pop.w {r8, r9, r10, r11}')}"""

# out[i] = sat16(accL >> 8) | sat16(accR >> 8) << 16, and the accumulators are cleared
FM_PACK = f"""
@micropython.asm_thumb
def fm_pack(r0, r1, r2):        # out (uint32), acc, n
    mov(r3, 0)
    label(PL)
    ldr(r4, [r1, 0])
    ldr(r5, [r1, 4])
{enc('ssat r4, #16, r4, asr #8', 'ssat r5, #16, r5, asr #8', 'pkhbt r4, r4, r5, lsl #16')}    str(r3, [r1, 0])
    str(r3, [r1, 4])
    add(r1, 8)
    str(r4, [r0, 0])
    add(r0, 4)
    sub(r2, 1)
    bne(PL)
"""
KERNELS.update(fm_render=FM_RENDER, fm_pack=FM_PACK)

# one quadrant (80 x 60 cells from the given bases), every 4th cell in x and y:
# out[0] = sum(u^2 + v^2), out[1] = sum(dye)    (20 x 15 = 300 samples)
STATS_Q = f"""
@micropython.asm_thumb
def stats_q(r0, r1, r2, r3):    # u, v, dye (f16 at the quadrant's first cell), out (2 x f32)
    mov(r4, 0)
    vmov(s0, r4)
    vmov(s1, r4)
    movwt(r6, {4 * R16 - 20 * 8})
    mov(r4, 15)
    label(YL)
    mov(r5, 20)
    label(XL)
    ldrh(r7, [r0, 0])
    vmov(s2, r7)
    ldrh(r7, [r1, 0])
    vmov(s3, r7)
    ldrh(r7, [r2, 0])
    vmov(s4, r7)
{enc('vcvtb.f32.f16 s2, s2', 'vcvtb.f32.f16 s3, s3', 'vcvtb.f32.f16 s4, s4')}    vmul(s2, s2, s2)
    vmul(s3, s3, s3)
    vadd(s1, s1, s4)
    vadd(s0, s0, s2)
    vadd(s0, s0, s3)
    add(r0, 8)
    add(r1, 8)
    add(r2, 8)
    sub(r5, 1)
    bne(XL)
    add(r0, r0, r6)
    add(r1, r1, r6)
    add(r2, r2, r6)
    sub(r4, 1)
    bne(YL)
    vstr(s0, [r3, 0])
    vstr(s1, [r3, 4])
"""
KERNELS.update(stats_q=STATS_Q)

# zero r1 words from r0 / zero the left and right edge cells of r1 f16 rows from r0 (a row start)
ZFILL = """
@micropython.asm_thumb
def zfill(r0, r1):
    mov(r2, 0)
    label(ZL)
    str(r2, [r0, 0])
    add(r0, 4)
    sub(r1, 1)
    bne(ZL)
"""
ZCOLS = f"""
@micropython.asm_thumb
def zcols(r0, r1):
    mov(r2, 0)
    movwt(r3, {R16})
    label(ZL)
    strh(r2, [r0, 0])
{enc(f'strh.w r2, [r0, #{R16 - 2}]')}    add(r0, r0, r3)
    sub(r1, 1)
    bne(ZL)
"""
KERNELS.update(zfill=ZFILL, zcols=ZCOLS)

# drone mapping for all 4 quadrant voices from stats_q sums (float), no Python floats per frame:
# x = min(1, sqrt(max(0, ke_sum * kscale)));  voice.tidx = b0[q] + bd * x
# y = min(1, sqrt(max(0, dye_sum * dscale))); voice.a_target = vol * y
# ctl: voice 0 state ptr, SQ ptr, kscale, dscale, -, bd, vol, b0[4] (f32); voice stride 40, tidx +20, t +28
FM_MAP = f"""
@micropython.asm_thumb
def fm_map(r0):                 # ctl
    vldr(s2, [r0, 8])
    vldr(s3, [r0, 12])
    vldr(s4, [r0, 16])
    vldr(s5, [r0, 20])
    vldr(s6, [r0, 24])
    ldr(r1, [r0, 0])
    ldr(r2, [r0, 4])
    add(r5, r0, 0)
    add(r5, 28)
{enc('vmov.f32 s7, #1.0')}    mov(r3, 0)
    vmov(s8, r3)
    mov(r3, 4)
    label(QL)
    vldr(s4, [r5, 0])
    add(r5, 4)
    vldr(s0, [r2, 0])
    vmul(s0, s0, s2)
{enc('vmaxnm.f32 s0, s0, s8')}    vsqrt(s0, s0)
{enc('vminnm.f32 s0, s0, s7')}    vmul(s0, s0, s5)
    vadd(s0, s0, s4)
    vcvt_s32_f32(s0, s0)
    vmov(r4, s0)
    str(r4, [r1, 20])
    vldr(s1, [r2, 4])
    vmul(s1, s1, s3)
{enc('vmaxnm.f32 s1, s1, s8')}    vsqrt(s1, s1)
{enc('vminnm.f32 s1, s1, s7')}    vmul(s1, s1, s6)
    vcvt_s32_f32(s1, s1)
    vmov(r4, s1)
    str(r4, [r1, 28])
    add(r1, 40)
    add(r2, 8)
    sub(r3, 1)
    bne(QL)
"""
KERNELS.update(fm_map=FM_MAP)

# spectrum strip, run on core1 between render and the next frame's advection. ctl (bytes):
#  0 ring (1024 x L|R<<16)  4 audio state (+8 = write index)  8 sine (1024 x i16)  12 buf (1024 complex f32)
# 16 bands: 2 x (lo, hi, out = sum of bars lo..hi-1), then f32: 24 bass norm 28 treble norm 32 slow bass (state)
#    36 slow rate 40 bass force 44 kick force 48 treble dye -> 52 force gain 56 dye gain 60 bass 64 treble
# 20 bars (NB x int, height in rows Q4)  24 bins (NB+1 x int)
# 28 NB  32 colours (one byte per row from the bottom)  36 A  40 B (f32: height Q4 = A * float(bits(|X|^2)) + B)
# 44 max height Q4 (f32)  48 fall per call (Q4)  52 / 56 scratch  60 1/32767 (f32)
# (bilin_h draws the bars)  1) Hann-windowed mono, bit-reversed into buf
# 2) radix-2 DIT complex FFT, twiddles from the sine table 3) bar b = max |X[k]|^2 over bins[b] <= k < bins[b+1],
#    log2 from the float bits, falls by at most `fall` per call
SPEC = f"""
@micropython.asm_thumb
def spec(r0):                   # ctl
    ldr(r1, [r0, 12])
    ldr(r2, [r0, 0])
    ldr(r3, [r0, 4])
    ldr(r3, [r3, 8])
    ldr(r5, [r0, 8])
    mov(r4, 0)
    vmov(s1, r4)
    label(IN)
    add(r6, r3, r4)
    lsl(r6, r6, 22)
    lsr(r6, r6, 20)
    add(r6, r6, r2)
    ldr(r6, [r6, 0])
    lsl(r7, r6, 16)
    asr(r7, r7, 16)
    asr(r6, r6, 16)
    add(r6, r6, r7)
    lsr(r7, r4, 1)
    add(r7, r7, r7)
    add(r7, r7, r5)
    ldrh(r7, [r7, 0])
    mul(r7, r7)
    lsr(r7, r7, 15)
    mul(r6, r7)
    vmov(s0, r6)
    vcvt_f32_s32(s0, s0)
{enc('rbit r7, r4')}    lsr(r7, r7, 22)
    lsl(r7, r7, 3)
    add(r7, r7, r1)
    vstr(s0, [r7, 0])
    vstr(s1, [r7, 4])
    add(r4, 1)
{enc('cmp.w r4, #1024')}    blt(IN)
    vldr(s13, [r0, 60])
    ldr(r7, [r0, 8])
    mov(r2, 8)
    movwt(r6, 512)
    str(r6, [r0, 52])
    label(ST)
    mov(r3, 0)
    mov(r6, 0)
    str(r6, [r0, 56])
    movwt(r5, 8192)
    add(r5, r5, r1)
    label(KL)
    add(r6, r3, r3)
    add(r6, r6, r7)
{enc('ldrsh.w r4, [r6, #512]')}    vmov(s14, r4)
    vcvt_f32_s32(s14, s14)
    vmul(s14, s14, s13)
    ldrh(r4, [r6, 0])
    neg(r4, r4)
    vmov(s15, r4)
    vcvt_f32_s32(s15, s15)
    vmul(s15, s15, s13)
    ldr(r4, [r0, 56])
    add(r4, r4, r1)
    label(JL)
    add(r6, r4, r2)
    vldr(s0, [r4, 0])
    vldr(s1, [r4, 4])
    vldr(s2, [r6, 0])
    vldr(s3, [r6, 4])
    vmul(s4, s2, s14)
    vmul(s5, s3, s15)
    vmul(s6, s2, s15)
    vmul(s7, s3, s14)
    vsub(s4, s4, s5)
    vadd(s6, s6, s7)
    vadd(s8, s0, s4)
    vadd(s9, s1, s6)
    vsub(s10, s0, s4)
    vsub(s11, s1, s6)
    vstr(s8, [r4, 0])
    vstr(s9, [r4, 4])
    vstr(s10, [r6, 0])
    vstr(s11, [r6, 4])
    add(r4, r4, r2)
    add(r4, r4, r2)
    cmp(r4, r5)
    blt(JL)
    ldr(r4, [r0, 56])
    add(r4, 8)
    str(r4, [r0, 56])
    ldr(r6, [r0, 52])
    add(r3, r3, r6)
{enc('cmp.w r3, #512')}    blt(KL)
    ldr(r6, [r0, 52])
    lsr(r6, r6, 1)
    str(r6, [r0, 52])
    add(r2, r2, r2)
    cmp(r6, 0)
    bne(ST)
    vldr(s20, [r0, 36])
    vldr(s21, [r0, 40])
    vldr(s22, [r0, 44])
    mov(r4, 0)
    vmov(s23, r4)
    ldr(r2, [r0, 24])
    ldr(r3, [r0, 20])
    ldr(r4, [r0, 28])
    label(BL)
    ldr(r5, [r2, 0])
    ldr(r6, [r2, 4])
    add(r2, 4)
    lsl(r5, r5, 3)
    add(r5, r5, r1)
    lsl(r6, r6, 3)
    add(r6, r6, r1)
    vsub(s0, s23, s23)
    label(BK)
    vldr(s1, [r5, 0])
    vldr(s2, [r5, 4])
    vmul(s1, s1, s1)
    vmul(s2, s2, s2)
    vadd(s1, s1, s2)
{enc('vmaxnm.f32 s0, s0, s1')}    add(r5, 8)
    cmp(r5, r6)
    blt(BK)
    vcvt_f32_s32(s0, s0)
    vmul(s0, s0, s20)
    vadd(s0, s0, s21)
{enc('vmaxnm.f32 s0, s0, s23', 'vminnm.f32 s0, s0, s22')}    vcvt_s32_f32(s0, s0)
    vmov(r7, s0)
    ldr(r5, [r3, 0])
    ldr(r6, [r0, 48])
    sub(r5, r5, r6)
    cmp(r5, r7)
    bge(BS)
    mov(r5, r7)
    label(BS)
    str(r5, [r3, 0])
    add(r3, 4)
    sub(r4, 1)
    bne(BL)
    ldr(r1, [r0, 16])
    mov(r6, 2)
    label(BD)                   # two bands: sum of bars lo <= b < hi
    ldr(r4, [r0, 20])
    ldr(r2, [r1, 0])
    lsl(r2, r2, 2)
    add(r2, r2, r4)
    ldr(r3, [r1, 4])
    lsl(r3, r3, 2)
    add(r3, r3, r4)
    mov(r5, 0)
    label(SB)
    ldr(r7, [r2, 0])
    add(r5, r5, r7)
    add(r2, 4)
    cmp(r2, r3)
    blt(SB)
    str(r5, [r1, 8])
    add(r1, 12)
    sub(r6, 1)
    bne(BD)
    ldr(r1, [r0, 16])           # sound -> fluid gains for the orbiting jets
    ldr(r2, [r1, 8])
    vmov(s0, r2)
    vcvt_f32_s32(s0, s0)
    vldr(s1, [r1, 24])
    vmul(s0, s0, s1)
    ldr(r2, [r1, 20])
    vmov(s2, r2)
    vcvt_f32_s32(s2, s2)
    vldr(s3, [r1, 28])
    vmul(s2, s2, s3)
    vstr(s0, [r1, 60])
    vstr(s2, [r1, 64])
    vldr(s4, [r1, 32])
    vsub(s5, s0, s4)
    vldr(s6, [r1, 36])
    vmul(s6, s6, s5)
    vadd(s4, s4, s6)
    vstr(s4, [r1, 32])
    vsub(s5, s0, s4)
{enc('vmaxnm.f32 s5, s5, s23', 'vmov.f32 s7, #1.0', 'vmov.f32 s9, #1.0')}    vldr(s8, [r1, 40])
    vmul(s8, s8, s0)
    vadd(s7, s7, s8)
    vldr(s8, [r1, 44])
    vmul(s8, s8, s5)
    vadd(s7, s7, s8)
    vstr(s7, [r1, 52])
    vldr(s8, [r1, 48])
    vmul(s8, s8, s2)
    vadd(s9, s9, s8)
    vstr(s9, [r1, 56])
"""
KERNELS.update(spec=SPEC)

# plasma background in the palette-index bytes (render only, the simulation never sees it):
# idx = max(idx, satadd(T1[x + p1], T2[y + p2], T3[x + y + p3])) four cells per word (uqadd8, usub8 + sel).
# p_k = ((phase * speed_k) >> 16) & 255.  ctl: rows, T1, T2 + y0, T3 + y0, speed 1..3 (cells per 256 phase units
# x 256). Byte tables long enough for every offset (T1, T2 512, T3 768).
def _bgp(k, base, dst):
    return f"""    ldr(r4, [r1, {16 + 4 * k}])
    mul(r4, r2)
{enc('ubfx r4, r4, #16, #8')}    ldr({dst}, [r1, {base}])
    add({dst}, {dst}, r4)
"""
BG_H = f"""
@micropython.asm_thumb
def bg_h(r0, r1, r2):           # idx bytes (row start), ctl, phase
{_bgp(0, 4, 'r5')}{_bgp(1, 8, 'r6')}{_bgp(2, 12, 'r3')}{enc('push.w {r8, r9, r10, r11}', 'ldr.w r11, [r1, #0]', 'mov.w r12, r6', 'mov.w lr, #0x01010101')}    mov(r2, r5)
    label(YL)
{enc('ldrb r8, [r12], #1', 'mul r8, r8, lr')}    mov(r5, r2)
    mov(r6, r3)
    add(r3, 1)
    mov(r4, {W // 4})
    label(XL)
{enc('ldr r9, [r5], #4', 'ldr r10, [r6], #4', 'uqadd8 r9, r9, r10', 'uqadd8 r9, r9, r8')}    ldr(r7, [r0, 0])
{enc('usub8 r10, r7, r9', 'sel r7, r7, r9', 'str r7, [r0], #4')}    sub(r4, 1)
    bne(XL)
{enc('subs.w r11, r11, #1')}    bne(YL)
{enc('pop.w {r8, r9, r10, r11}')}"""
KERNELS.update(bg_h=BG_H)

if __name__ == '__main__':
    print('mpy', build(), 'bytes')
    if '--upload' in sys.argv:
        from rp import Board
        b = Board(); b.soft_reset()
        b.exec("import os\ntry: os.mkdir('/FluidFM')\nexcept OSError: pass")
        print('uploaded', b.put('fluid16_k.mpy', '/FluidFM/fluid16_k.mpy'))
        b.close()
