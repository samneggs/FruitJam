# ── PACK: s0,s1,s2 -> r0 RGB565 (saturating). Clobbers r1, s0-s2, s6
lab('PACK')
ld('s6', 'SC'); a('vmul(s0, s0, s6)'); a('vcvt_s32_f32(s0, s0)'); a('vmov(r0, s0)')
E('usat r0, #5, r0')
ld('s6', 'SC', 1); a('vmul(s1, s1, s6)'); a('vcvt_s32_f32(s1, s1)'); a('vmov(r1, s1)')
E('usat r1, #6, r1')
a('lsl(r0, r0, 11)'); a('lsl(r1, r1, 5)'); a('orr(r0, r1)')
ld('s6', 'SC', 2); a('vmul(s2, s2, s6)'); a('vcvt_s32_f32(s2, s2)'); a('vmov(r1, s2)')
E('usat r1, #5, r1')
a('orr(r0, r1)')
a('bx(lr)')

# ── GIANT: gas giant + rings for direction d = (s4, s5, s6), any length.
# in/out: base colour s0-s2. Uses s3, s7, s8-s15 (saved), r0, r1, r10 (TEX base). Called with bl.
lab('GIANT')
H(0xB500, 'push {lr}'); E('vpush {s8-s15}')   # 16-bit forms: push.w/pop.w with one register is UNPREDICTABLE
dot3('s8', ('s4', 's5', 's6'), ('s4', 's5', 's6'))                       # a
ld('s3', 'PCX'); a('vmul(s9, s4, s3)'); ld('s3', 'PCY'); mla('s9', 's5', 's3'); ld('s3', 'PCZ'); mla('s9', 's6', 's3')   # b
cmpz('s9'); br('le', 'G_RET')
ld('s3', 'CCB'); a('vmul(s10, s9, s9)'); mls('s10', 's8', 's3'); cmpz('s10'); br('le', 'G_RET')   # outside ring bound
ld('s15', 'BIG')
ld('s3', 'CCP'); a('vmul(s10, s9, s9)'); mls('s10', 's8', 's3'); cmpz('s10'); br('le', 'G_RINGS')
a('vsqrt(s10, s10)'); a('vsub(s10, s9, s10)'); a('vdiv(s15, s10, s8)')   # tp
for c, (dr, pc) in enumerate((('s4', 'PCX'), ('s5', 'PCY'), ('s6', 'PCZ'))):
    r = f's{11 + c}'
    a(f'vmul({r}, {dr}, s15)'); ld('s3', pc); a(f'vsub({r}, {r}, s3)'); ld('s3', 'INVR'); a(f'vmul({r}, {r}, s3)')
ld('s7', 'GAXV'); a('vmul(s3, s11, s7)'); ld('s7', 'GAXV', 1); mla('s3', 's12', 's7'); ld('s7', 'GAXV', 2); mla('s3', 's13', 's7')
ld('s7', 'BT'); mla('s3', 's11', 's7')                                    # latitude (+ slight tilt)
ld('s7', 'ONE'); a('vadd(s3, s3, s7)'); ld('s7', 'BK'); a('vmul(s3, s3, s7)'); a('vcvt_s32_f32(s3, s3)'); a('vmov(r0, s3)')
E('usat r0, #6, r0')
a('lsl(r1, r0, 3)'); a('lsl(r0, r0, 2)'); a('add(r0, r0, r1)'); E('add.w r0, r0, r10')
a(f'movwt(r1, {TEX_BAND})'); a('add(r0, r0, r1)')
a('vldr(s0, [r0, 0])'); a('vldr(s1, [r0, 4])'); a('vldr(s2, [r0, 8])')
ld('s7', 'GSUN'); a('vmul(s3, s11, s7)'); ld('s7', 'GSUN', 1); mla('s3', 's12', 's7'); ld('s7', 'GSUN', 2); mla('s3', 's13', 's7')
ld('s7', 'ZERO'); fmax('s3', 's3', 's7'); ld('s7', 'G06'); ld('s10', 'G94'); mla('s7', 's10', 's3')
for c in range(3):
    a(f'vmul(s{c}, s{c}, s7)')
lab('G_RINGS')
ld('s3', 'GAXV'); a('vmul(s10, s4, s3)'); ld('s3', 'GAXV', 1); mla('s10', 's5', 's3'); ld('s3', 'GAXV', 2); mla('s10', 's6', 's3')
ld('s3', 'PCA'); a('vdiv(s10, s3, s10)')                                 # t to the ring plane
cmpz('s10'); br('le', 'G_RET')
cmpf('s10', 's15'); br('ge', 'G_RET')                                    # planet in front of the ring
for c, (dr, pc) in enumerate((('s4', 'PCX'), ('s5', 'PCY'), ('s6', 'PCZ'))):
    r = f's{11 + c}'
    a(f'vmul({r}, {dr}, s10)'); ld('s3', pc); a(f'vsub({r}, {r}, s3)')
dot3('s14', ('s11', 's12', 's13'), ('s11', 's12', 's13'))                # |q|^2
ld('s3', 'INVR2'); a('vmul(s3, s14, s3)')                                # (r/R)^2
ld('s7', 'RR1'); cmpf('s3', 's7'); br('lt', 'G_RET')
ld('s7', 'RR2'); cmpf('s3', 's7'); br('gt', 'G_RET')
ld('s7', 'RR1'); a('vsub(s3, s3, s7)'); ld('s7', 'RK'); a('vmul(s3, s3, s7)'); a('vcvt_s32_f32(s3, s3)'); a('vmov(r0, s3)')
E('usat r0, #6, r0')
a('lsl(r0, r0, 4)'); E('add.w r0, r0, r10'); a(f'movwt(r1, {TEX_RING})'); a('add(r0, r0, r1)')
ld('s9', 'ONE')                                                           # shadow factor
ld('s7', 'GSUN'); a('vmul(s8, s11, s7)'); ld('s7', 'GSUN', 1); mla('s8', 's12', 's7'); ld('s7', 'GSUN', 2); mla('s8', 's13', 's7')
cmpz('s8'); br('ge', 'G_NOSH')
mls('s14', 's8', 's8'); ld('s7', 'GR2'); cmpf('s14', 's7'); br('ge', 'G_NOSH')
ld('s9', 'SHAD')
lab('G_NOSH')
a('vldr(s7, [r0, 12])'); ld('s3', 'ONE'); a('vsub(s3, s3, s7)')           # 1 - alpha
for c in range(3):
    a(f'vmul(s{c}, s{c}, s3)'); a(f'vldr(s10, [r0, {4 * c}])'); mla(f's{c}', 's10', 's9')
lab('G_RET')
E('vpop {s8-s15}'); H(0xBD00, 'pop {pc}')

# ── SPLAT (rt_main only): add a soft point into a byte light layer (160 wide).
# in: s0 = column, s1 = row (floats, pixel centres at integers), s2 = intensity (0-255
# scale), s6 = row limit, s7 = halo fraction, r1 = layer base. Bilinear 2x2 core plus an
# 8-pixel ring; saturating byte adds. Uses r0, r2, r3, s0-s8 (s8 is free during per-frame
# setup, the only caller). Returns with bx lr.
if HALF == 0:
    lab('SPLAT')
    ld('s3', 'ONE'); cmpf('s0', 's3'); br('lt', 'SPL_RET'); ld('s3', 'XMAX'); cmpf('s0', 's3'); br('ge', 'SPL_RET')
    ld('s3', 'ONE'); cmpf('s1', 's3'); br('lt', 'SPL_RET'); cmpf('s1', 's6'); br('ge', 'SPL_RET')
    a('vcvt_s32_f32(s3, s0)'); a('vmov(r0, s3)'); a('vcvt_f32_s32(s3, s3)'); a('vsub(s4, s0, s3)')   # fx
    a('vcvt_s32_f32(s3, s1)'); a('vmov(r2, s3)'); a('vcvt_f32_s32(s3, s3)'); a('vsub(s5, s1, s3)')   # fy
    a('lsl(r3, r2, 7)'); a('lsl(r2, r2, 5)'); a('add(r3, r3, r2)'); a('add(r0, r0, r3)'); a('add(r0, r0, r1)')
    ld('s3', 'ONE'); a('vsub(s6, s3, s5)'); a('vsub(s3, s3, s4)')     # s3 = 1-fx, s6 = 1-fy
    a('vmul(s3, s3, s2)'); a('vmul(s4, s4, s2)'); a('vmul(s7, s7, s2)')
    def addb(off, w):
        a(f'vcvt_s32_f32(s0, {w})'); a('vmov(r2, s0)')
        a(f'ldrb(r3, [r0, {off}])'); a('add(r3, r3, r2)'); E('usat r3, #8, r3'); a(f'strb(r3, [r0, {off}])')
    # core = bilinear share + halo level (solid 2x2 for sparks; stars have halo 0 = pure
    # bilinear, so they glide between pixels); ring = 0.6 x halo level
    ld('s8', 'SRING'); a('vmul(s8, s8, s7)')
    a('vmul(s1, s3, s6)'); a('vadd(s1, s1, s7)'); a('vmul(s2, s4, s6)'); a('vadd(s2, s2, s7)')   # w00, w10
    a('sub(r0, 160)'); addb(0, 's8'); addb(1, 's8')
    a('add(r0, 159)'); addb(0, 's8'); addb(1, 's1'); addb(2, 's2'); addb(3, 's8')
    a('vmul(s1, s3, s5)'); a('vadd(s1, s1, s7)'); a('vmul(s2, s4, s5)'); a('vadd(s2, s2, s7)')   # w01, w11
    a('add(r0, 160)'); addb(0, 's8'); addb(1, 's1'); addb(2, 's2'); addb(3, 's8')
    a('add(r0, 160)'); addb(1, 's8'); addb(2, 's8')
    lab('SPL_RET')
    a('bx(lr)')
