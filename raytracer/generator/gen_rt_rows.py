# emitted twice: HALF = 0 (rt_main, even rows) and HALF = 1 (rt_rows, odd rows)
ld('s14', 'V0')
a('vmov(r0, s31)'); a(f'ldr(r4, [r0, {C_RBCUR}])')
if HALF:
    ld('s0', 'DV'); a('vsub(s14, s14, s0)')
    a(f'movwt(r1, {RW * 2})'); a('add(r4, r4, r1)')
a(f'mov(r6, {RH // 2})')

# ── row setup ───────────────────────────────────────────────────────────────
lab('ROW')
_iv = [0]
def row_interval(o):
    g = GROUP[o]
    n = _iv[0] = _iv[0] + 1
    if g == '0':
        fmov('s6', 's14')
    else:
        ld('s6', 'IRY'); a('vmul(s6, s14, s6)')                 # vy
    ld('s7', 'A0F' + g); mla('s7', 's6', 's6')                  # A0 = A0F + vy^2
    ld('s0', 'BF_' + o); ld('s1', 'OCY_' + o); mla('s0', 's1', 's6')    # b0
    ld('s1', 'B1_' + o); a('vmul(s2, s0, s1)'); ld('s3', 'CC_' + o); ld('s4', 'A1' + g)
    mls('s2', 's3', 's4')                                        # q1
    a('vmul(s4, s0, s0)'); mls('s4', 's3', 's7')                # q0
    cmpz('s3'); br('le', f'IV_FULL{n}')                          # camera inside: no bound
    ld('s5', 'Q2_' + o); cmpz('s5'); br('ge', f'IV_FULL{n}')
    a('vmul(s0, s2, s2)'); mls('s0', 's5', 's4')                 # q1^2 - q2 q0
    cmpz('s0'); br('lt', f'IV_EMPTY{n}')
    a('vsqrt(s0, s0)'); a('vneg(s2, s2)')
    a('vadd(s1, s2, s0)'); a('vdiv(s1, s1, s5)')                 # i_lo (q2 < 0)
    a('vsub(s2, s2, s0)'); a('vdiv(s2, s2, s5)')                 # i_hi
    ld('s3', 'ONE'); a('vsub(s1, s1, s3)'); a('vadd(s2, s2, s3)')    # 1 px margin
    ld('s3', 'ZERO'); fmax('s1', 's1', 's3'); ld('s3', 'LASTX'); fmin('s2', 's2', 's3')
    cmpf('s1', 's2'); br('gt', f'IV_EMPTY{n}')
    a('vcvt_s32_f32(s1, s1)'); a('vcvt_s32_f32(s2, s2)'); a('vmov(r0, s1)'); a('vmov(r1, s2)')
    a('sub(r0, r1, r0)')                                         # W = hi - lo
    a(f'mov(r2, {RW})'); a('sub(r2, r2, r1)')                    # LO (in r5 units) = RW - hi
    jmp(f'IV_SET{n}')
    lab(f'IV_FULL{n}'); a('mov(r2, 1)'); a(f'mov(r0, {RW - 1})'); jmp(f'IV_SET{n}')
    lab(f'IV_EMPTY{n}'); a('movwt(r2, 1000)'); a('mov(r0, 0)')
    lab(f'IV_SET{n}')
    if o == 'orb':
        E('mov.w r8, r2'); E('mov.w r9, r0')
    else:
        stri('r2', 'LO_' + o); stri('r0', 'W_' + o)
for o in OBJS:
    row_interval(o)
fabs('s0', 's14'); ld('s1', 'SKYK'); a('vmul(s3, s0, s1)'); ld('s1', 'ONE'); fmin('s3', 's3', 's1')
for k in range(3):
    ld('s0', 'HOR', k); ld('s4', 'DH', k); mla('s0', 's4', 's3'); st('s0', 'RSKY', k)
cmpz('s14'); br('lt', 'FLOOR_ROW')
a('mov(r3, 0)')
for s in ('s15', 's11', 's10', 's9'):
    ld(s, 'ZERO')
jmp('ROW_GO')
lab('FLOOR_ROW')
a('mov(r3, 1)')
a('vneg(s0, s22)'); a('vdiv(s8, s0, s14)')                     # t = -h/v
a('vmul(s10, s8, s20)'); a('vmul(s9, s8, s21)')
ld('s0', 'PHX'); a('vmul(s15, s8, s28)'); a('vadd(s15, s15, s0)')
ld('s0', 'PHZ'); a('vmul(s11, s8, s29)'); a('vadd(s11, s11, s0)')
ld('s0', 'FADE'); a('vmul(s3, s8, s0)'); ld('s0', 'ONE'); fmin('s3', 's3', 's0'); st('s3', 'RFOG')
fabs('s0', 's14'); a('vmul(s1, s0, s0)'); ld('s2', 'ONE'); a('vadd(s1, s1, s2)'); a('vsqrt(s1, s1)')
a('vdiv(s0, s0, s1)')                                          # cos = |v| / sqrt(1 + v^2)
a('vsub(s1, s2, s0)'); a('vmul(s3, s1, s1)'); a('vmul(s3, s3, s3)'); a('vmul(s3, s3, s1)')
ld('s4', 'FR1'); a('vmul(s3, s3, s4)'); ld('s4', 'FR0'); a('vadd(s3, s3, s4)'); st('s3', 'RF')
a('vsqrt(s1, s0)'); ld('s2', 'DU'); a('vmul(s3, s8, s2)'); ld('s2', 'CW'); a('vmul(s3, s3, s2)')
a('vdiv(s3, s3, s1)'); ld('s4', 'HALF'); a('vmul(s4, s3, s4)'); st('s4', 'RHW')   # checker filter width
ld('s4', 'TWO'); a('vdiv(s4, s4, s3)'); st('s4', 'RI2W')
lab('ROW_GO')
fmov('s12', 's28'); fmov('s13', 's29')
a(f'mov(r5, {RW})')
a('vmov(r0, s31)'); a(f'ldr(r1, [r0, {C_RBCUR}])'); a('sub(r1, r4, r1)'); a('lsr(r1, r1, 1)')
E('mov.w r12, r1', 'r12 = pixel index into the light layers')

# ── per pixel: find nearest hit ─────────────────────────────────────────────
lab('PIX')
dot3('s30', D, D)
a('mov(r2, r3)'); ld('s23', 'BIG'); ld('s26', 'ZERO')
a('cmp(r3, 0)'); br('eq', 'P_NOFL'); fmov('s23', 's8'); ld('s0', 'BIG'); st('s0', 'T_PRT'); lab('P_NOFL')
def copy_ec(src, tmp):
    for k in range(3):
        ld(tmp, src, k); st(tmp, 'EC', k)
# orb
E('sub.w r0, r5, r8'); E('cmp.w r0, r9'); br('hi', 'P_ORB_NO')
dot3('s24', OC, D)
cmpz('s24'); br('le', 'P_ORB_NO')
a('vmul(s25, s24, s24)'); mls('s25', 's30', 's19')
cmpz('s25'); br('le', 'P_ORB_EDGE')
a('vsqrt(s0, s25)'); a('vsub(s0, s24, s0)'); a('vdiv(s0, s0, s30)')
cmpf('s0', 's23'); br('ge', 'P_ORB_NO')
fmov('s23', 's0'); a(f'mov(r2, {ORB})'); jmp('P_ORB_NO')
# near miss: coverage = 1 - (distance outside the silhouette) / (pixel footprint)
#   = 1 + disc * KORB / b   (disc < 0 here; KORB = 1 / (2 r DU))
lab('P_ORB_EDGE')
a('vdiv(s0, s25, s24)'); ld('s1', 'KORB'); a('vmul(s0, s0, s1)'); ld('s1', 'ONE'); a('vadd(s0, s0, s1)')
cmpf('s0', 's26'); br('le', 'P_ORB_NO')
a('vdiv(s1, s24, s30)'); fmov('s26', 's0'); fmov('s27', 's1')
copy_ec('OE', 's2')
lab('P_ORB_NO')
# defenders: intersect the unit sphere in scaled space; ds = d * ir shared
ld('s5', 'IRX'); a('vmul(s1, s12, s5)'); ld('s5', 'IRY'); a('vmul(s2, s14, s5)')
ld('s5', 'IRZ'); a('vmul(s3, s13, s5)')
for k in range(NR):
    nl = f'P_RO_NO{k}'
    in_range(f'roto{k}', nl)
    ld('s5', f'OCRX{k}'); a('vmul(s4, s5, s1)'); ld('s5', f'OCRY{k}'); mla('s4', 's5', 's2')
    ld('s5', f'OCRZ{k}'); mla('s4', 's5', 's3')
    cmpz('s4'); br('le', nl)
    dot3('s6', ('s1', 's2', 's3'), ('s1', 's2', 's3'))
    ld('s5', f'CCR{k}'); a('vmul(s7, s4, s4)'); mls('s7', 's6', 's5')
    cmpz('s7'); br('le', f'P_RO_EDGE{k}')
    a('vsqrt(s7, s7)'); a('vsub(s7, s4, s7)'); a('vdiv(s7, s7, s6)')
    cmpf('s7', 's23'); br('ge', nl)
    fmov('s23', 's7'); a(f'mov(r2, {ROTO0 + k})'); jmp(nl)
    lab(f'P_RO_EDGE{k}')
    a('vdiv(s0, s7, s4)'); ld('s5', 'KROT'); a('vmul(s0, s0, s5)'); ld('s5', 'ONE'); a('vadd(s0, s0, s5)')
    cmpf('s0', 's26'); br('le', nl)
    a('vdiv(s5, s4, s6)'); fmov('s26', 's0'); fmov('s27', 's5')
    copy_ec('RSIL', 's5')
    lab(nl)
E('tst.w r11, #8'); br('eq', 'P_DR_END')
def drone_texel(u, v, flip, dst, lab_no):   # u,v in [-1,1] regs -> texel rgb in dst (r<0 = transparent)
    ld('s7', 'ONE')
    a(f'vadd({u}, {u}, s7)'); ld('s7', 'EIGHT'); a(f'vmul({u}, {u}, s7)'); a(f'vcvt_s32_f32({u}, {u})'); a(f'vmov(r0, {u})')
    E('usat r0, #4, r0')
    ld('s7', 'ONE')
    a(f'vadd({v}, s7, {v})' if flip else f'vsub({v}, s7, {v})')
    ld('s7', 'FOUR'); a(f'vmul({v}, {v}, s7)'); a(f'vcvt_s32_f32({v}, {v})'); a(f'vmov(r1, {v})')
    E('usat r1, #3, r1')
    a('lsl(r1, r1, 4)'); a('add(r0, r0, r1)'); a('lsl(r1, r0, 3)'); a('lsl(r0, r0, 2)'); a('add(r0, r0, r1)')
    E('add.w r0, r0, r10')
    ldri('r1', f'DF{k}'); a('add(r0, r0, r1)')
    a(f'vldr({dst[0]}, [r0, 0])'); cmpz(dst[0]); br('lt', lab_no)
    a(f'vldr({dst[1]}, [r0, 4])'); a(f'vldr({dst[2]}, [r0, 8])')
for k in range(ND):
    nl = f'P_DR_NO{k}'
    in_range(f'drone{k}', nl)
    ld('s0', f'DOX{k}'); ld('s1', f'DOZ{k}'); a('vmul(s2, s12, s0)'); mla('s2', 's13', 's1')   # d.n (horizontal normal)
    cmpz('s2'); br('le', nl)
    ld('s3', f'DL2{k}'); a('vdiv(s3, s3, s2)')                                                 # t
    cmpf('s3', 's23'); br('ge', nl)
    a('vmul(s4, s12, s3)'); a('vsub(s4, s4, s0)')                                              # q
    a('vmul(s5, s13, s3)'); a('vsub(s5, s5, s1)')
    a('vmul(s6, s14, s3)'); ld('s7', f'DOY{k}'); a('vsub(s6, s6, s7)')
    a('vmul(s4, s4, s1)'); mls('s4', 's5', 's0'); ld('s7', f'DKU{k}'); a('vmul(s4, s4, s7)')    # u
    ld('s7', f'DKV{k}'); a('vmul(s6, s6, s7)')                                                 # v
    ld('s7', 'ONE'); fabs('s5', 's4'); cmpf('s5', 's7'); br('ge', nl); fabs('s5', 's6'); cmpf('s5', 's7'); br('ge', nl)
    drone_texel('s4', 's6', False, ('s0', 's1', 's2'), nl)
    for c in range(3):
        st(f's{c}', 'T_DC', c)
    fmov('s23', 's3'); a(f'mov(r2, {DRONE})')
    lab(nl)
lab('P_DR_END')
def post_test(k):
    nl = f'PT_NO{k}'
    ld('s0', f'PCX{k}'); a('vmul(s2, s0, s12)'); ld('s1', f'PCZ{k}'); mla('s2', 's1', 's13')   # b
    cmpz('s2'); br('le', nl)
    a('vmul(s3, s12, s12)'); mla('s3', 's13', 's13')                                        # a2 (xz)
    ld('s4', f'PCC{k}'); a('vmul(s5, s2, s2)'); mls('s5', 's3', 's4'); cmpz('s5'); br('le', f'PT_EDGE{k}')
    a('vsqrt(s5, s5)'); a('vsub(s5, s2, s5)'); a('vdiv(s5, s5, s3)')                         # t
    a('vmul(s6, s5, s14)'); a('vadd(s6, s6, s22)')                                           # hit y
    cmpz('s6'); br('lt', f'PT_REF{k}')
    ld('s7', 'PH'); cmpf('s6', 's7'); br('gt', nl)
    cmpf('s5', 's23'); br('ge', nl)
    fmov('s23', 's5'); a(f'mov(r2, {POST})'); st('s2', 'T_PB'); st('s3', 'T_PA2'); jmp(nl)
    lab(f'PT_REF{k}')                        # below the floor = the post's mirror image
    a('cmp(r3, 0)'); br('eq', nl)
    ld('s7', 'PH'); a('vneg(s7, s7)'); cmpf('s6', 's7'); br('lt', nl)
    ld('s7', 'T_PRT'); cmpf('s5', 's7'); br('ge', nl); st('s5', 'T_PRT'); jmp(nl)
    lab(f'PT_EDGE{k}')                       # near miss of a thin post: partial coverage
    a('vdiv(s0, s5, s2)'); ld('s1', 'KPOST'); a('vmul(s0, s0, s1)'); ld('s1', 'ONE'); a('vadd(s0, s0, s1)')
    cmpf('s0', 's26'); br('le', nl)
    a('vdiv(s1, s2, s3)'); a('vmul(s6, s1, s14)'); a('vadd(s6, s6, s22)')
    cmpz('s6'); br('lt', nl); ld('s7', 'PH'); cmpf('s6', 's7'); br('gt', nl)
    fmov('s26', 's0'); fmov('s27', 's1')
    ld('s6', 'FADE'); a('vmul(s6, s1, s6)'); ld('s7', 'ONE'); fmin('s6', 's6', 's7')
    for c in range(3):
        ld('s4', 'POSTC', c); ld('s7', 'HOR', c); a('vsub(s7, s7, s4)'); mla('s4', 's7', 's6'); st('s4', 'EC', c)
    lab(nl)
in_range('goal', 'P_GOAL_NO')
post_test(0); post_test(1)
lab('P_GOAL_NO')
def wall_grid(yabs):                 # -> s3 = grid intensity; uses s4-s7
    ld('s4', 'DU'); a('vmul(s4, s4, s0)'); ld('s5', 'WPK'); a('vmul(s4, s4, s5)'); ld('s5', 'WMIN'); fmax('s4', 's4', 's5')
    ld('s7', 'ONE')
    ld('s5', 'HALF'); a('vmul(s5, s2, s5)'); a('vcvt_s32_f32(s6, s5)'); a('vcvt_f32_s32(s6, s6)'); a('vsub(s5, s5, s6)')
    ld('s6', 'HALF'); a('vsub(s5, s5, s6)'); fabs('s5', 's5'); a('vadd(s5, s5, s5)')      # |mod(z,2)-1|
    a('vdiv(s5, s5, s4)'); a('vsub(s3, s7, s5)')                                         # gz
    ld('s5', 'WH'); a(f'vsub(s5, s5, {yabs})'); fabs('s5', 's5'); a('vdiv(s5, s5, s4)'); a('vsub(s5, s7, s5)')
    fmax('s3', 's3', 's5')                                                               # top edge
    ld('s6', 'WGYI'); a(f'vmul(s5, {yabs}, s6)'); a('vcvt_s32_f32(s6, s5)'); a('vcvt_f32_s32(s6, s6)'); a('vsub(s5, s5, s6)')
    ld('s6', 'HALF'); a('vsub(s5, s5, s6)'); fabs('s5', 's5'); ld('s6', 'WGYS'); a('vmul(s5, s5, s6)')   # |mod(y,0.4)-0.2|
    a('vdiv(s5, s5, s4)'); a('vsub(s5, s7, s5)'); ld('s6', 'WGYK'); a('vmul(s5, s5, s6)')
    fmax('s3', 's3', 's5')
    ld('s5', 'ZERO'); fmax('s3', 's3', 's5'); fmin('s3', 's3', 's7')
E('tst.w r11, #4'); br('eq', 'P_NOWALL')
# Wall planes x = +-FW. With A = distance to the wall along x (>0) and B = |dx|, the hit
# distance is t = A / B, so every visibility test is done with multiplies; the one divide
# happens only on pixels that actually show a wall or its reflection.
ld('s0', 'MONE'); st('s0', 'T_WG'); st('s0', 'T_WGM')
fabs('s1', 's12')                                                          # B
ld('s0', 'WAP'); cmpz('s12'); br('ge', 'P_WPOS'); ld('s0', 'WAN')
lab('P_WPOS')                                                              # A
a('vmul(s2, s23, s1)'); cmpf('s0', 's2'); br('ge', 'P_WBEHIND')          # wall in front of the hit?
a('cmp(r3, 0)'); br('ne', 'P_WHIT')                                        # floor rows: height is always in range
a('vmul(s2, s0, s14)'); ld('s3', 'WHMC'); a('vmul(s3, s3, s1)'); cmpf('s2', 's3'); br('gt', 'P_NOWALL')
lab('P_WHIT')
a('vdiv(s0, s0, s1)')                                                      # t
a('vmul(s1, s0, s14)'); a('vadd(s1, s1, s22)')                             # y at the wall
a('vmul(s2, s0, s13)'); ld('s3', 'CAMZ'); a('vadd(s2, s2, s3)'); ld('s3', 'WOFF'); a('vadd(s2, s2, s3)')
wall_grid('s1'); st('s3', 'T_WG'); st('s0', 'T_WT'); jmp('P_NOWALL')
lab('P_WBEHIND')                                                           # its reflection, past the floor point
a('cmp(r3, 0)'); br('eq', 'P_NOWALL')
a('vmul(s2, s8, s1)'); cmpf('s0', 's2'); br('le', 'P_NOWALL')
fabs('s2', 's14'); a('vmul(s2, s0, s2)'); ld('s3', 'WHPC'); a('vmul(s3, s3, s1)'); cmpf('s2', 's3'); br('gt', 'P_NOWALL')
a('vdiv(s0, s0, s1)')
a('vmul(s1, s0, s14)'); a('vadd(s1, s1, s22)'); a('vneg(s1, s1)')         # mirrored height
a('vmul(s2, s0, s13)'); ld('s3', 'CAMZ'); a('vadd(s2, s2, s3)'); ld('s3', 'WOFF'); a('vadd(s2, s2, s3)')
wall_grid('s1'); st('s3', 'T_WGM')
lab('P_NOWALL')
cmpz('s26'); br('le', 'P_EDGE_OK')                      # edge only counts if in front of the hit
cmpf('s27', 's23'); br('lt', 'P_EDGE_OK'); ld('s26', 'ZERO')
lab('P_EDGE_OK')
a(f'cmp(r2, {FLOOR})'); br('eq', 'SH_FLOOR')
a(f'cmp(r2, {ORB})'); br('eq', 'SH_ORB')
a(f'cmp(r2, {POST})'); br('eq', 'SH_POST')
for k in range(NR):
    a(f'cmp(r2, {ROTO0 + k})'); br('eq', f'SH_RO{k}')
a(f'cmp(r2, {DRONE})'); br('eq', 'SH_DRONE')
# sky: row gradient, stars (projected onto y = 1, hashed per cell), gas giant
for k in range(3):
    ld(f's{k}', 'RSKY', k)
E('tst.w r11, #2'); br('eq', 'SK_NOST')                        # stars: pre-splatted sky layer
ldri('r0', 'STARA'); E('add.w r0, r0, r12'); a('ldrb(r0, [r0, 0])'); a('cmp(r0, 0)'); br('eq', 'SK_NOST')
a('vmov(s5, r0)'); a('vcvt_f32_s32(s5, s5)'); ld('s6', 'STB'); a('vmul(s5, s5, s6)')
for k in range(3):
    a(f'vadd(s{k}, s{k}, s5)')
lab('SK_NOST')
E('tst.w r11, #1'); br('eq', 'SH_DONE')
in_range('giant', 'SH_DONE')
fmov('s4', 's12'); fmov('s5', 's14'); fmov('s6', 's13'); a('bl(GIANT)')
jmp('SH_DONE')
lab('SH_DRONE')
for k in range(3):
    ld(f's{k}', 'T_DC', k)
ld('s6', 'FADE'); a('vmul(s6, s23, s6)'); ld('s7', 'ONE'); fmin('s6', 's6', 's7')
for k in range(3):
    ld('s7', 'HOR', k); a(f'vsub(s7, s7, s{k})'); mla(f's{k}', 's7', 's6')
jmp('SH_DONE')
for k in range(NR):                  # select the defender, then shared chrome shading
    lab(f'SH_RO{k}')
    for c, n in enumerate(('OCRX', 'OCRY', 'OCRZ')):
        ld('s4', f'{n}{k}'); st('s4', f'CUR_{n}')
    jmp('SH_ROTO')

# ── goal post (emissive cylinder): brighter facing the viewer, fogged with distance
lab('SH_POST')
ld('s2', 'T_PB'); ld('s3', 'T_PA2')
a('vmul(s6, s23, s3)'); a('vsub(s6, s2, s6)'); a('vmul(s6, s6, s6)')
ld('s7', 'PR2'); a('vmul(s7, s7, s30)'); a('vdiv(s6, s6, s7)'); ld('s7', 'ONE'); fmin('s6', 's6', 's7')
ld('s7', 'PSH0'); ld('s5', 'PSH1'); mla('s7', 's5', 's6')
for k in range(3):
    ld(f's{k}', 'POSTC', k); a(f'vmul(s{k}, s{k}, s7)')
ld('s6', 'FADE'); a('vmul(s6, s23, s6)'); ld('s7', 'ONE'); fmin('s6', 's6', 's7')
for k in range(3):
    ld('s7', 'HOR', k); a(f'vsub(s7, s7, s{k})'); mla(f's{k}', 's7', 's6')
jmp('SH_DONE')

# ── orb (emissive): edge -> core by cos^2,  n.d = t a - b  (no normal needed)
lab('SH_ORB')
a('vmul(s6, s23, s30)'); a('vsub(s6, s6, s24)'); a('vmul(s6, s6, s6)')
ld('s7', 'ORBR2'); a('vmul(s7, s7, s30)'); a('vdiv(s6, s6, s7)')
ld('s7', 'ONE'); fmin('s6', 's6', 's7')
for k in range(3):
    ld(f's{k}', 'OE', k); ld('s7', 'OD', k); mla(f's{k}', 's7', 's6')
jmp('SH_NOGLOW')

# ── defender (chrome) ───────────────────────────────────────────────────────
lab('SH_ROTO')
ld('s4', 'IRX'); a('vmul(s1, s12, s4)'); ld('s4', 'IRY'); a('vmul(s2, s14, s4)')
ld('s4', 'IRZ'); a('vmul(s3, s13, s4)')
for k, nm in enumerate(('CUR_OCRX', 'CUR_OCRY', 'CUR_OCRZ')):    # ns = t ds - ocr ; n = ns * ir
    r = f's{k + 1}'
    a(f'vmul({r}, s23, {r})'); ld('s4', nm); a(f'vsub({r}, {r}, s4)')
    ld('s4', ('IRX', 'IRY', 'IRZ')[k]); a(f'vmul({r}, {r}, s4)')
dot3('s4', D, ('s1', 's2', 's3'))                          # dn
dot3('s5', ('s1', 's2', 's3'), ('s1', 's2', 's3'))         # nn
a('vdiv(s6, s4, s5)'); a('vadd(s6, s6, s6)')               # k = 2 dn / nn
for k in range(3):                                           # R = d - k n
    r = f's{k + 1}'
    a(f'vmul({r}, s6, {r})'); a(f'vsub({r}, {D[k]}, {r})')
a('vmul(s5, s5, s30)'); a('vsqrt(s5, s5)'); a('vdiv(s4, s4, s5)'); a('vneg(s4, s4)')   # cos
ld('s5', 'ONE'); a('vsub(s5, s5, s4)'); a('vmul(s4, s5, s5)'); a('vmul(s4, s4, s5)')
st('s4', 'T_RIM')
a('vmul(s5, s23, s14)'); a('vadd(s5, s5, s22)'); st('s5', 'T_PY')         # hit y
a('vsqrt(s0, s30)'); st('s0', 'T_SA')
ld('s6', 'LX'); a('vmul(s7, s1, s6)'); ld('s6', 'LX', 1); mla('s7', 's2', 's6')
ld('s6', 'LX', 2); mla('s7', 's3', 's6'); a('vdiv(s7, s7, s0)')
ld('s6', 'ZERO'); fmax('s7', 's7', 's6')
for _ in range(5):
    a('vmul(s7, s7, s7)')
ld('s6', 'SPECK'); a('vmul(s7, s7, s6)'); st('s7', 'T_SPEC')
for k in range(3):                                           # oc2 = oc - t d  (orb seen from the hit)
    a(f'vmul(s{4 + k}, s23, {D[k]})'); a(f'vsub(s{4 + k}, {OC[k]}, s{4 + k})')
dot3('s7', ('s4', 's5', 's6'), ('s1', 's2', 's3'))
cmpz('s7'); br('le', 'ENV_NO_ORB')
dot3('s0', ('s4', 's5', 's6'), ('s4', 's5', 's6')); ld('s4', 'ORBR2'); a('vsub(s0, s0, s4)')
a('vmul(s4, s7, s7)'); mls('s4', 's30', 's0')
cmpz('s4'); br('le', 'ENV_NO_ORB')
for k in range(3):
    ld(f's{4 + k}', 'OCORE', k)
jmp('ENV_DONE')
lab('ENV_NO_ORB')
cmpz('s2'); br('ge', 'ENV_SKY')
ld('s0', 'T_PY'); a('vneg(s0, s0)'); a('vdiv(s0, s0, s2)'); ld('s4', 'TFMAX'); fmin('s0', 's0', 's4')
a('vmul(s4, s23, s12)'); ld('s5', 'PHX'); a('vadd(s4, s4, s5)'); mla('s4', 's0', 's1')
a('vmul(s5, s23, s13)'); ld('s6', 'PHZ'); a('vadd(s5, s5, s6)'); mla('s5', 's0', 's3')
a('vcvt_s32_f32(s4, s4)'); a('vmov(r0, s4)'); a('vcvt_s32_f32(s5, s5)'); a('vmov(r1, s5)')
a('eor(r0, r1)'); a('mov(r1, 1)'); a('and_(r0, r1)')
a('lsl(r1, r0, 3)'); a('lsl(r0, r0, 2)'); a('add(r0, r0, r1)'); a('add(r0, r0, r7)')   # r0 = r7 + parity*12
for k in range(3):
    ldb(f's{4 + k}', 'r0', 'TA', k)
ld('s7', 'FADE'); a('vmul(s0, s0, s7)'); ld('s7', 'ONE'); fmin('s0', 's0', 's7')
for k in range(3):
    ld('s7', 'HOR', k); a(f'vsub(s7, s7, s{4 + k})'); mla(f's{4 + k}', 's7', 's0')
jmp('ENV_DONE')
lab('ENV_SKY')
ld('s0', 'T_SA'); a('vdiv(s0, s2, s0)'); ld('s4', 'SKYK'); a('vmul(s0, s0, s4)')
ld('s4', 'ONE'); fmin('s7', 's0', 's4')
fmov('s4', 's1'); fmov('s5', 's2'); fmov('s6', 's3')            # R -> GIANT input
for k in range(3):
    ld(f's{k}', 'HOR', k); ld('s3', 'DH', k); mla(f's{k}', 's3', 's7')
E('tst.w r11, #1'); br('eq', 'ENV_NOG')
a('bl(GIANT)')
lab('ENV_NOG')
for k in range(3):
    fmov(f's{4 + k}', f's{k}')
lab('ENV_DONE')
ld('s3', 'T_SPEC')
for k in range(3):
    ld('s7', 'TINT', k); a(f'vmul(s{k}, s{4 + k}, s7)'); a(f'vadd(s{k}, s{k}, s3)')
ld('s3', 'T_RIM')
for k in range(3):
    ld('s7', 'RIM', k); mla(f's{k}', 's7', 's3')
ld('s3', 'T_PY'); ld('s4', 'ROY'); a('vsub(s3, s3, s4)'); fabs('s3', 's3')       # stripe, AA by footprint
ld('s4', 'STHW'); a('vsub(s3, s4, s3)'); ld('s4', 'DV'); a('vmul(s4, s4, s23)'); a('vdiv(s3, s3, s4)')
ld('s4', 'HALF'); a('vadd(s3, s3, s4)')
ld('s4', 'ZERO'); fmax('s3', 's3', 's4'); ld('s4', 'ONE'); fmin('s3', 's3', 's4')
for k in range(3):
    ld('s7', 'STRIPE', k); mla(f's{k}', 's7', 's3')
jmp('SH_DONE')

# ── floor ───────────────────────────────────────────────────────────────────
lab('SH_FLOOR')
ld('s6', 'RHW'); ld('s7', 'HALF')
def fpart_abs(r):                 # r = |frac(r) - 0.5| ; uses s2
    a(f'vcvt_s32_f32(s2, {r})'); a('vcvt_f32_s32(s2, s2)'); a(f'vsub({r}, {r}, s2)')
    a(f'vsub({r}, {r}, s7)'); fabs(r, r)
for p, g in (('s15', 's3'), ('s11', 's4')):              # box-filtered checker
    a(f'vsub(s0, {p}, s6)'); a('vmul(s0, s0, s7)'); a('vadd(s1, s0, s6)')
    fpart_abs('s0'); fpart_abs('s1')
    a(f'vsub({g}, s0, s1)'); ld('s2', 'RI2W'); a(f'vmul({g}, {g}, s2)')
a('vmul(s3, s3, s4)'); a('vmul(s3, s3, s7)'); a('vsub(s3, s7, s3)')    # ch
for k in range(3):
    ld(f's{k}', 'TA', k); ld('s4', 'DT', k); mla(f's{k}', 's4', 's3')
# contact shadows (soft, squared-distance form): cheap xz gate first
for k in range(NR):
    nl = f'F_NOSH{k}'
    ld('s3', f'KRX{k}'); a('vsub(s3, s3, s15)'); ld('s5', f'KRZ{k}'); a('vsub(s5, s5, s11)')
    a('vmul(s6, s3, s3)'); mla('s6', 's5', 's5'); ld('s7', 'SHR2'); cmpf('s6', 's7'); br('ge', nl)
    ld('s4', 'ROY')
    ld('s7', 'LX'); a('vmul(s6, s3, s7)'); ld('s7', 'LX', 1); mla('s6', 's4', 's7')
    ld('s7', 'LX', 2); mla('s6', 's5', 's7')
    cmpz('s6'); br('le', nl)
    dot3('s7', ('s3', 's4', 's5'), ('s3', 's4', 's5')); mls('s7', 's6', 's6')
    ld('s3', 'RS2'); a('vsub(s7, s7, s3)')
    ld('s3', 'SHK2'); a('vmul(s6, s6, s3)'); ld('s4', 'HALF'); a('vmul(s5, s6, s4)')
    cmpf('s7', 's5'); br('ge', nl)
    a('vdiv(s7, s7, s6)'); a('vadd(s7, s7, s4)'); ld('s5', 'ZERO'); fmax('s7', 's7', 's5')
    ld('s5', 'ONE'); a('vsub(s5, s5, s7)'); ld('s6', 'SHD'); a('vmul(s5, s5, s6)')
    ld('s6', 'ONE'); a('vsub(s5, s6, s5)')
    for c in range(3):
        a(f'vmul(s{c}, s{c}, s5)')
    lab(nl)
# orb light on the floor
ld('s3', 'KOX'); a('vsub(s3, s3, s15)'); ld('s4', 'ORBY'); ld('s5', 'KOZ'); a('vsub(s5, s5, s11)')
dot3('s6', ('s3', 's4', 's5'), ('s3', 's4', 's5'))
ld('s7', 'OLCUT'); cmpf('s6', 's7'); br('ge', 'F_NOOL')
ld('s7', 'OLC'); a('vmul(s6, s6, s7)'); ld('s7', 'ONE'); a('vadd(s6, s6, s7)')
a('vdiv(s6, s7, s6)'); a('vmul(s6, s6, s6)')
for k in range(3):
    ld('s7', 'OLIGHT', k); mla(f's{k}', 's7', 's6')
lab('F_NOOL')
# reflection: same primary ray against objects mirrored through the floor
REF = ('s3', 's4', 's5')
in_range('morb', 'F_NOMO')
ld('s6', 'OCMY'); a('vmul(s3, s16, s12)'); mla('s3', 's6', 's14'); mla('s3', 's18', 's13')
cmpz('s3'); br('le', 'F_NOMO')
ld('s7', 'CCOM'); a('vmul(s4, s3, s3)'); mls('s4', 's30', 's7')
cmpz('s4'); br('le', 'F_NOMO')
a('vsqrt(s4, s4)'); a('vsub(s4, s3, s4)'); a('vdiv(s4, s4, s30)')
a('vmul(s6, s4, s30)'); a('vsub(s6, s6, s3)'); a('vmul(s6, s6, s6)')
ld('s7', 'ORBR2'); a('vmul(s7, s7, s30)'); a('vdiv(s6, s6, s7)'); ld('s7', 'ONE'); fmin('s6', 's6', 's7')
for k in range(3):
    ld(REF[k], 'OE', k); ld('s7', 'OD', k); mla(REF[k], 's7', 's6')
jmp('F_MIX')
lab('F_NOMO')
E('tst.w r11, #8'); br('eq', 'F_MDR_END')
for k in range(ND):
    nl = f'F_NOMD{k}'
    in_range(f'mdrone{k}', nl)
    ld('s3', f'DOX{k}'); ld('s4', f'DOZ{k}'); a('vmul(s5, s12, s3)'); mla('s5', 's13', 's4')
    cmpz('s5'); br('le', nl)
    ld('s6', f'DL2{k}'); a('vdiv(s6, s6, s5)')                       # t
    cmpf('s6', 's8'); br('le', nl)                                  # mirror image lies beyond the floor point
    a('vmul(s7, s12, s6)'); a('vsub(s7, s7, s3)')                   # qx
    a('vmul(s5, s13, s6)'); a('vsub(s5, s5, s4)')                   # qz
    a('vmul(s7, s7, s4)'); mls('s7', 's5', 's3'); ld('s5', f'DKU{k}'); a('vmul(s7, s7, s5)')   # u
    a('vmul(s6, s14, s6)'); ld('s5', f'DOMY{k}'); a('vsub(s6, s6, s5)'); ld('s5', f'DKV{k}'); a('vmul(s6, s6, s5)')   # v
    ld('s5', 'ONE'); fabs('s3', 's7'); cmpf('s3', 's5'); br('ge', nl); fabs('s3', 's6'); cmpf('s3', 's5'); br('ge', nl)
    drone_texel('s7', 's6', True, ('s3', 's4', 's5'), nl)
    jmp('F_MIX')
    lab(nl)
lab('F_MDR_END')
for k in range(NR):
    nl = f'F_NOMR{k}'
    in_range(f'mroto{k}', nl)
    ld('s5', 'IRX'); a('vmul(s3, s12, s5)'); ld('s5', 'IRY'); a('vmul(s4, s14, s5)')
    ld('s5', 'IRZ'); a('vmul(s5, s13, s5)')
    ld('s6', f'OCRX{k}'); a('vmul(s7, s6, s3)'); ld('s6', f'OCRMY{k}'); mla('s7', 's6', 's4')
    ld('s6', f'OCRZ{k}'); mla('s7', 's6', 's5')
    cmpz('s7'); br('le', nl)
    dot3('s6', REF, REF)
    ld('s3', f'CCRM{k}'); a('vmul(s4, s7, s7)'); mls('s4', 's6', 's3')
    cmpz('s4'); br('le', nl)
    a('vsqrt(s4, s4)'); a('vsub(s4, s7, s4)'); a('vdiv(s4, s4, s6)')
    a('vmul(s7, s4, s14)'); a('vadd(s7, s7, s22)'); ld('s6', 'ROY'); a('vadd(s7, s7, s6)'); fabs('s7', 's7')
    ld('s6', 'STHW'); a('vsub(s7, s6, s7)'); ld('s6', 'DV'); a('vmul(s6, s6, s4)'); a('vdiv(s7, s7, s6)')
    ld('s6', 'HALF'); a('vadd(s7, s7, s6)')
    ld('s6', 'ZERO'); fmax('s7', 's7', 's6'); ld('s6', 'ONE'); fmin('s7', 's7', 's6')
    for c in range(3):
        ld(REF[c], 'MREF', c); ld('s6', 'STRIPE', c); mla(REF[c], 's6', 's7')
    jmp('F_MIX')
    lab(nl)
ld('s6', 'T_PRT'); ld('s7', 'BIG'); cmpf('s6', 's7'); br('ge', 'F_REFSKY')
for k in range(3):
    ld(REF[k], 'POSTREF', k)
jmp('F_MIX')
lab('F_REFSKY')
for k in range(3):
    ld(REF[k], 'RSKY', k)
E('tst.w r11, #1'); br('eq', 'F_MIX')
in_range('mgiant', 'F_MIX')
for k in range(3):
    st(f's{k}', 'T_FC', k)
fmov('s0', 's3'); fmov('s1', 's4'); fmov('s2', 's5')
fmov('s4', 's12'); a('vneg(s5, s14)'); fmov('s6', 's13')
a('bl(GIANT)')
fmov('s3', 's0'); fmov('s4', 's1'); fmov('s5', 's2')
for k in range(3):
    ld(f's{k}', 'T_FC', k)
lab('F_MIX')
E('tst.w r11, #4'); br('eq', 'F_NOWM')
ld('s6', 'T_WGM'); cmpz('s6'); br('lt', 'F_NOWM')
ld('s7', 'WB'); a('vmul(s6, s6, s7)'); ld('s7', 'WA'); a('vadd(s6, s6, s7)')
for k in range(3):
    ld('s7', 'WKEEP'); a(f'vmul({REF[k]}, {REF[k]}, s7)'); ld('s7', 'WC', k); mla(REF[k], 's7', 's6')
lab('F_NOWM')
ld('s6', 'RF')
for k in range(3):
    a(f'vsub(s7, {REF[k]}, s{k})'); mla(f's{k}', 's7', 's6')
ld('s6', 'RFOG')
for k in range(3):
    ld('s7', 'HOR', k); a(f'vsub(s7, s7, s{k})'); mla(f's{k}', 's7', 's6')

# ── glow from the orb (closest approach), edge blend, pack ──────────────────
lab('SH_DONE')
E('tst.w r11, #4'); br('eq', 'SH_NOWALL')
ld('s3', 'T_WG'); cmpz('s3'); br('lt', 'SH_NOWALL')
ld('s7', 'WB'); a('vmul(s3, s3, s7)'); ld('s7', 'WA'); a('vadd(s3, s3, s7)')
ld('s4', 'T_WT'); ld('s7', 'FADE'); a('vmul(s4, s4, s7)'); ld('s7', 'ONE'); fmin('s4', 's4', 's7')
a('vsub(s4, s7, s4)'); a('vmul(s3, s3, s4)')                       # fade the wall with distance
for k in range(3):
    ld('s7', 'WKEEP'); a(f'vmul(s{k}, s{k}, s7)'); ld('s7', 'WC', k); mla(f's{k}', 's7', 's3')
lab('SH_NOWALL')
in_range('glow', 'SH_NOGLOW')
dot3('s24', OC, D)
cmpz('s24'); br('le', 'SH_NOGLOW')
ld('s3', 'OCO2'); a('vmul(s3, s3, s30)'); mls('s3', 's24', 's24')      # a |oc|^2 - b^2 = a dmin^2
ld('s4', 'GCUT'); a('vmul(s4, s4, s30)'); cmpf('s3', 's4'); br('ge', 'SH_NOGLOW')
ld('s4', 'GK'); a('vmul(s4, s4, s30)'); a('vdiv(s4, s4, s3)'); ld('s5', 'GOFF'); a('vsub(s4, s4, s5)')
ld('s5', 'GMAX'); fmin('s4', 's4', 's5')
for k in range(3):
    ld('s5', 'GLOW', k); mla(f's{k}', 's5', 's4')
lab('SH_NOGLOW')
E('tst.w r11, #16'); br('eq', 'SH_NOSPK')                      # sparks: pre-splatted glow layer
ldri('r0', 'SPKA'); E('add.w r0, r0, r12'); a('ldrb(r0, [r0, 0])'); a('cmp(r0, 0)'); br('eq', 'SH_NOSPK')
a('vmov(s5, r0)'); a('vcvt_f32_s32(s5, s5)'); ld('s6', 'SPKK'); a('vmul(s5, s5, s6)')
for c in range(3):
    ld('s6', 'SPC', c); mla(f's{c}', 's6', 's5')
lab('SH_NOSPK')
cmpz('s26'); br('le', 'SH_NOEDGE')
for k in range(3):
    ld('s7', 'EC', k); a(f'vsub(s7, s7, s{k})'); mla(f's{k}', 's7', 's26')
lab('SH_NOEDGE')
a('bl(PACK)')
a('strh(r0, [r4, 0])')

a('add(r4, 2)'); E('add.w r12, r12, #1')
a('vadd(s12, s12, s20)'); a('vadd(s13, s13, s21)')
a('vadd(s15, s15, s10)'); a('vadd(s11, s11, s9)')
a('sub(r5, 1)'); br('ne', 'PIX')
a(f'movwt(r1, {RW * 2})'); a('add(r4, r4, r1)')       # skip the other core's row
ld('s0', 'DV2'); a('vsub(s14, s14, s0)')
a('sub(r6, 1)'); br('ne', 'ROW')
