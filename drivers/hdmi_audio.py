# hdmi_audio.py - HDMI data-island packets for DVI_RP2_HSTX (RP2350, MicroPython)
#
# Builds the 36-word HSTX RAW sequence for one data island:
#   2 guard band + 32 TERC4 packet chars + 2 guard band
# Word layout (same as driver): xx 2222222222 1111111111 0000000000 (lane2..lane0)
#
# Packet -> TERC4 mapping (HDMI 1.4 5.2.3):
#   lane0 nibble: b0 HSYNC, b1 VSYNC, b2 header bit i, b3 0 on first char else 1
#   lane1 nibble: bit k = subpacket k bit 2i
#   lane2 nibble: bit k = subpacket k bit 2i+1
# BCH ECC: G(x)=1+x^6+x^7+x^8, LSB first -> reflected mask 0x83.
#
# Islands are placed inside HSYNC (H=0 in the TERC4 bits), one packet per island,
# one island on every even line: island j = line 2j, j = 0..262 per frame.
# Storage: 133 slots of 36 words, island j -> slot j % 132 (VSYNC island 5 -> 132).
# The frame is split
# into 4 quarters (Q_BOUNDS); quarters 0/2 share slots 0..65, 1/3 share 66..131.
# After quarter q is sent, quarter q+2 is encoded into the same slots.
# Island schedule: 0 = AVI InfoFrame, 1 = Audio InfoFrame, j%16==8 = ACR,
# everything else = audio sample packet (0..4 stereo samples, Bresenham-paced
# against the pixel clock, so consumption is locked to the HDMI clock).

import array
import micropython
from micropython import const

SLOT_W    = const(36)       # words per island slot (2 GB + 32 + 2 GB)
N_ISL     = const(263)      # islands per frame (even lines 0..524)
N_SLOT    = const(133)      # island slots: island j -> slot j % 132, VSYNC island -> 132
VS_ISL    = const(5)        # line 10 = first VSYNC line (V=0), own slot so a late
VS_SLOT   = const(132)      # encode never puts V=0 sync bits on a V=1 line
Q_BOUNDS  = (0, 66, 132, 198, 263)   # island quarters
_QB       = array.array('i', Q_BOUNDS)
RING_N    = const(1024)
RING_MASK = const(1023)

_DI_GB12  = const(0x1334CC00)   # lane1/lane2 data-island guard band 0x133
_C_AVI    = const(0)
_C_AIF    = const(36)
_C_ACR    = const(72)

# state indices
S_ACC   = const(0)          # pacing accumulator
S_RD    = const(1)          # ring read index  (service core)
S_WR    = const(2)          # ring write index (producer)
S_F     = const(3)          # IEC60958 frame 0..191
S_UNDER = const(4)          # samples sent as silence (ring empty)
S_LATE  = const(5)          # service called too late (both halves pending)
S_LR    = const(6)          # pixel clock (Hz)
S_FS    = const(7)          # fs * H_TOTAL
# context for the asm packet builder (byte offset = index * 4, see _build_jobs)
S_S     = const(8)          # current slot
S_S1    = const(9)          # end slot (exclusive)
S_NT    = const(10)         # scratch: samples in current packet
S_SLOTS = const(11)         # addresses ...
S_RING  = const(12)
S_TAB8  = const(13)
S_CONST = const(14)
S_JOBS  = const(15)
S_LV1   = const(16)
S_LV0   = const(17)
N_ST    = const(18)

TERC4 = (0b1010011100, 0b1001100011, 0b1011100100, 0b1011100010,
         0b0101110001, 0b0100011110, 0b0110001110, 0b0100111100,
         0b1011001100, 0b0100111001, 0b0110011100, 0b1011000110,
         0b1010001110, 0b1001110001, 0b0101100011, 0b1011000011)

_T0  = array.array('I', TERC4)
_T12 = array.array('I', [(TERC4[n & 15] << 10) | (TERC4[n >> 4] << 20) for n in range(256)])

# byte of a subpacket (4 chars x 2 bits) -> char index bits for subpacket 0:
# char m: bit 2m -> bit 8m (lane1 nibble), bit 2m+1 -> bit 8m+4 (lane2 nibble)
_SP = array.array('I', [sum((((x >> (2*m)) & 1) << (8*m)) | (((x >> (2*m+1)) & 1) << (8*m+4))
                            for m in range(4)) for x in range(256)])

_TAB8 = bytearray(256 + 256 + 192)          # ECC | PAR | CS, one base register in asm
_ECC = memoryview(_TAB8)[0:256]
_PAR = memoryview(_TAB8)[256:512]
_CS  = memoryview(_TAB8)[512:704]
for _i in range(256):
    _v = _i
    for _ in range(8):
        _v = (_v >> 1) ^ 0x83 if _v & 1 else _v >> 1
    _ECC[_i] = _v
    _p = 0
    _v = _i
    while _v:
        _p ^= _v & 1
        _v >>= 1
    _PAR[_i] = _p
_ST    = array.array('i', [0] * N_ST)
JOB_W  = const(11)                          # dst, hdr, lo0,hi0 .. lo3,hi3, lane0 set
_JOBS  = bytearray(67 * JOB_W * 4)          # max 66 jobs per quarter + terminator (asm only)
_LANE0 = array.array('I', [0] * 10)         # per hv (V=1, V=0): gb, z0, z0^z1, f0, f0^f1
_TABS  = array.array('I', [0, 0])           # T12 addr, SP addr (for asm)
_CONST = array.array('I', [0] * 108)        # AVI, AIF, ACR pre-encoded (V=1)
_SLOTS = None
_RING  = None

# ── reference (pure Python) encoder, used for constant packets ───────────────
def ecc(bs):
    e = 0
    for b in bs:
        e = _ECC[e ^ b]
    return e

def encode_ref(hb, sp, v):
    """hb: 3 header bytes, sp: 4 lists of 7 bytes, v: VSYNC level. -> 36 words"""
    hdr = hb[0] | (hb[1] << 8) | (hb[2] << 16) | (ecc(hb) << 24)
    subs = []
    for s in sp:
        x = 0
        for i in range(7):
            x |= s[i] << (8 * i)
        subs.append(x | (ecc(s) << 56))
    hv = v << 1
    gb = _T0[0xC | hv] | _DI_GB12
    out = [gb, gb]
    for i in range(32):
        n = 0
        for k in range(4):
            n |= ((subs[k] >> (2 * i)) & 1) << k
            n |= ((subs[k] >> (2 * i + 1)) & 1) << (k + 4)
        out.append(_T12[n] | _T0[hv | (((hdr >> i) & 1) << 2) | (8 if i else 0)])
    out += [gb, gb]
    return out

def infoframe(typ, ver, body):
    """body = PB1..PBn. Returns (hb, sp) with checksum in PB0."""
    hb = [typ, ver, len(body)]
    pb = [0] + list(body) + [0] * (27 - len(body))
    pb[0] = (-(sum(hb) + sum(pb))) & 0xFF
    return hb, [pb[0:7], pb[7:14], pb[14:21], pb[21:28]]

def acr_n_cts(pix, fs):
    """Pick N near the recommended value that gives an integer CTS."""
    n0 = {32000: 4096, 44100: 6272, 48000: 6144}.get(fs, 128 * fs // 1000)
    lo, hi, den = 128 * fs // 1500, 128 * fs // 300, 128 * fs
    for d in range(max(n0 - lo, hi - n0) + 1):
        for n in (n0 - d, n0 + d):
            if lo <= n <= hi and (pix * n) % den == 0:
                return n, pix * n // den
    return n0, (pix * n0 + den // 2) // den

def init(slots, pix_hz, fs, h_total, vic=1, aspect=1):
    """slots: driver's SRAM island slot buffer (N_SLOT * SLOT_W words, DMA-read)."""
    global _SLOTS, _RING
    from uctypes import addressof
    _SLOTS = slots
    _RING = bytearray(RING_N * 4)                     # L | R<<16 per frame, CPU only
    for i in range(N_ST):
        _ST[i] = 0
    for k, hv in ((0, 2), (5, 0)):                    # V=1 set, V=0 set (H=0 in HSYNC)
        _LANE0[k] = _T0[0xC | hv] | _DI_GB12
        _LANE0[k + 1] = _T0[hv | 8]
        _LANE0[k + 2] = _T0[hv | 8] ^ _T0[hv | 12]
        _LANE0[k + 3] = _T0[hv]
        _LANE0[k + 4] = _T0[hv] ^ _T0[hv | 4]
    _TABS[0] = addressof(_T12)
    _TABS[1] = addressof(_SP)
    _ST[S_SLOTS] = addressof(slots)
    _ST[S_RING] = addressof(_RING)
    _ST[S_TAB8] = addressof(_TAB8)
    _ST[S_CONST] = addressof(_CONST)
    _ST[S_JOBS] = addressof(_JOBS)
    _ST[S_LV1] = addressof(_LANE0)
    _ST[S_LV0] = addressof(_LANE0) + 20
    _ST[S_LR] = pix_hz
    _ST[S_FS] = fs * h_total

    # channel status: consumer, PCM, copy permitted, fs code, 16-bit words
    for i in range(192):
        _CS[i] = 0
    _CS[2] = 1
    sf = {44100: 0b0000, 48000: 0b0010, 32000: 0b0011,
          88200: 0b0001, 96000: 0b0101}.get(fs, 0b0001)   # 0001 = not indicated
    for b in range(4):
        _CS[24 + b] = (sf >> b) & 1
    _CS[33] = 1                                           # word length 16

    n, cts = acr_n_cts(pix_hz, fs)
    avi = infoframe(0x82, 2, [0x10,                       # RGB, active fmt present
                              (aspect << 4) | 0x08,       # aspect, same as picture
                              0x08,                       # full-range RGB
                              vic, 0])
    aif = infoframe(0x84, 1, [0x01, 0, 0, 0x00, 0])       # 2ch, refer to stream
    acr_sp = [0, (cts >> 16) & 0x0F, (cts >> 8) & 0xFF, cts & 0xFF,
              (n >> 16) & 0x0F, (n >> 8) & 0xFF, n & 0xFF]
    acr = ([0x01, 0, 0], [acr_sp] * 4)
    for off, (hb, sp) in ((_C_AVI, avi), (_C_AIF, aif), (_C_ACR, acr)):
        w = encode_ref(hb, sp, 1)
        for i in range(36):
            _CONST[off + i] = w[i]

    fill_slots(Q_BOUNDS[0], Q_BOUNDS[1])    # quarters 0 and 1 occupy all slots
    fill_slots(Q_BOUNDS[1], Q_BOUNDS[2])
    # slot 5 (island 137, V=1) is first written by the quarter-2 encode; until
    # then hold a valid V=1 island there (copy of island 4)
    slots[5 * SLOT_W * 4:6 * SLOT_W * 4] = slots[4 * SLOT_W * 4:5 * SLOT_W * 4]
    _ST[S_ACC] = 0
    _ST[S_UNDER] = 0
    _ST[S_F] = 0
    return n, cts

# ── slot filler (runs twice per frame, behind the beam) ──────────────────────
# _build_jobs (asm) builds each packet (pacing, samples, parity, ECC, header)
# into a job entry and copies constant packets; _encode_jobs (asm) then does
# the TERC4 island encoding for the whole job list.
@micropython.asm_thumb
def _encode_jobs(r0, r1):
    # r0 = job list, r1 = [T12 addr, SP addr]. Job (11 words):
    #   dst, hdr, lo0, hi0, lo1, hi1, lo2, hi2, lo3, hi3, lane0_set_addr
    # dst = 0 terminates. Writes 36 words per job at dst.
    # Uses r8-r11 (saved), r12, lr (lr already saved by the asm_thumb prologue).
    data(2, 0xE92D, 0x0F00)  # push.w {r8, r9, r10, r11}
    data(2, 0xF8D1, 0xC000)  # ldr.w r12, [r1, #0]
    # r12 = T12 (256 words: TERC4 lane1|lane2 by nibble pair)
    data(2, 0xF8D1, 0xB004)  # ldr.w r11, [r1, #4]
    # r11 = SP  (256 words: byte -> 4 char-index bit pairs)
    mov(r1, r0)
    # r1 = job pointer
    label(LOOP)
    ldr(r3, [r1, 0])
    # r3 = dst (island start), 0 = end of list
    cmp(r3, 0)
    bne(JOB)
    b(END)
    label(JOB)
    data(2, 0xF8D1, 0x8004)  # ldr.w r8, [r1, #4]
    # r8 = header (24 bits + ECC), bit i -> lane0 bit2 of char i
    ldr(r0, [r1, 40])
    # r0 = lane0 set: gb, z0, z0^z1, f0, f0^f1
    ldr(r2, [r0, 0])
    str(r2, [r3, 0])
    # guard band x2 leading
    str(r2, [r3, 4])
    data(2, 0xF8C3, 0x2088)  # str.w r2, [r3, #136]
    # guard band x2 trailing
    data(2, 0xF8C3, 0x208C)  # str.w r2, [r3, #140]
    data(2, 0xF8D0, 0x900C)  # ldr.w r9, [r0, #12]
    # char 0 uses lane0 bit3 = 0 values
    data(2, 0xF8D0, 0xA010)  # ldr.w r10, [r0, #16]
    add(r3, 8)
    # subpackets 0..3, lo words
    ldr(r4, [r1, 8])
    ldr(r5, [r1, 16])
    ldr(r6, [r1, 24])
    ldr(r7, [r1, 32])
    # group 0: chars 0..3
    data(2, 0xF3C4, 0x0007)  # ubfx r0, r4, #0, #8
    data(2, 0xF85B, 0x2020)  # ldr.w r2, [r11, r0, lsl #2]
    data(2, 0xF3C5, 0x0007)  # ubfx r0, r5, #0, #8
    data(2, 0xF85B, 0x0020)  # ldr.w r0, [r11, r0, lsl #2]
    data(2, 0xEA42, 0x0240)  # orr.w r2, r2, r0, lsl #1
    data(2, 0xF3C6, 0x0007)  # ubfx r0, r6, #0, #8
    data(2, 0xF85B, 0x0020)  # ldr.w r0, [r11, r0, lsl #2]
    data(2, 0xEA42, 0x0280)  # orr.w r2, r2, r0, lsl #2
    data(2, 0xF3C7, 0x0007)  # ubfx r0, r7, #0, #8
    data(2, 0xF85B, 0x0020)  # ldr.w r0, [r11, r0, lsl #2]
    data(2, 0xEA42, 0x02C0)  # orr.w r2, r2, r0, lsl #3
    data(2, 0xF348, 0x0E00)  # sbfx lr, r8, #0, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x0007)  # ubfx r0, r2, #0, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    ldr(r0, [r1, 40])
    data(2, 0xF8D0, 0x9004)  # ldr.w r9, [r0, #4]
    data(2, 0xF8D0, 0xA008)  # ldr.w r10, [r0, #8]
    data(2, 0xF348, 0x0E40)  # sbfx lr, r8, #1, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x2007)  # ubfx r0, r2, #8, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    data(2, 0xF348, 0x0E80)  # sbfx lr, r8, #2, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x4007)  # ubfx r0, r2, #16, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    data(2, 0xF348, 0x0EC0)  # sbfx lr, r8, #3, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x6007)  # ubfx r0, r2, #24, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    # group 1: chars 4..7
    data(2, 0xF3C4, 0x2007)  # ubfx r0, r4, #8, #8
    data(2, 0xF85B, 0x2020)  # ldr.w r2, [r11, r0, lsl #2]
    data(2, 0xF3C5, 0x2007)  # ubfx r0, r5, #8, #8
    data(2, 0xF85B, 0x0020)  # ldr.w r0, [r11, r0, lsl #2]
    data(2, 0xEA42, 0x0240)  # orr.w r2, r2, r0, lsl #1
    data(2, 0xF3C6, 0x2007)  # ubfx r0, r6, #8, #8
    data(2, 0xF85B, 0x0020)  # ldr.w r0, [r11, r0, lsl #2]
    data(2, 0xEA42, 0x0280)  # orr.w r2, r2, r0, lsl #2
    data(2, 0xF3C7, 0x2007)  # ubfx r0, r7, #8, #8
    data(2, 0xF85B, 0x0020)  # ldr.w r0, [r11, r0, lsl #2]
    data(2, 0xEA42, 0x02C0)  # orr.w r2, r2, r0, lsl #3
    data(2, 0xF348, 0x1E00)  # sbfx lr, r8, #4, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x0007)  # ubfx r0, r2, #0, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    data(2, 0xF348, 0x1E40)  # sbfx lr, r8, #5, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x2007)  # ubfx r0, r2, #8, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    data(2, 0xF348, 0x1E80)  # sbfx lr, r8, #6, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x4007)  # ubfx r0, r2, #16, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    data(2, 0xF348, 0x1EC0)  # sbfx lr, r8, #7, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x6007)  # ubfx r0, r2, #24, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    # group 2: chars 8..11
    data(2, 0xF3C4, 0x4007)  # ubfx r0, r4, #16, #8
    data(2, 0xF85B, 0x2020)  # ldr.w r2, [r11, r0, lsl #2]
    data(2, 0xF3C5, 0x4007)  # ubfx r0, r5, #16, #8
    data(2, 0xF85B, 0x0020)  # ldr.w r0, [r11, r0, lsl #2]
    data(2, 0xEA42, 0x0240)  # orr.w r2, r2, r0, lsl #1
    data(2, 0xF3C6, 0x4007)  # ubfx r0, r6, #16, #8
    data(2, 0xF85B, 0x0020)  # ldr.w r0, [r11, r0, lsl #2]
    data(2, 0xEA42, 0x0280)  # orr.w r2, r2, r0, lsl #2
    data(2, 0xF3C7, 0x4007)  # ubfx r0, r7, #16, #8
    data(2, 0xF85B, 0x0020)  # ldr.w r0, [r11, r0, lsl #2]
    data(2, 0xEA42, 0x02C0)  # orr.w r2, r2, r0, lsl #3
    data(2, 0xF348, 0x2E00)  # sbfx lr, r8, #8, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x0007)  # ubfx r0, r2, #0, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    data(2, 0xF348, 0x2E40)  # sbfx lr, r8, #9, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x2007)  # ubfx r0, r2, #8, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    data(2, 0xF348, 0x2E80)  # sbfx lr, r8, #10, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x4007)  # ubfx r0, r2, #16, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    data(2, 0xF348, 0x2EC0)  # sbfx lr, r8, #11, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x6007)  # ubfx r0, r2, #24, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    # group 3: chars 12..15
    data(2, 0xF3C4, 0x6007)  # ubfx r0, r4, #24, #8
    data(2, 0xF85B, 0x2020)  # ldr.w r2, [r11, r0, lsl #2]
    data(2, 0xF3C5, 0x6007)  # ubfx r0, r5, #24, #8
    data(2, 0xF85B, 0x0020)  # ldr.w r0, [r11, r0, lsl #2]
    data(2, 0xEA42, 0x0240)  # orr.w r2, r2, r0, lsl #1
    data(2, 0xF3C6, 0x6007)  # ubfx r0, r6, #24, #8
    data(2, 0xF85B, 0x0020)  # ldr.w r0, [r11, r0, lsl #2]
    data(2, 0xEA42, 0x0280)  # orr.w r2, r2, r0, lsl #2
    data(2, 0xF3C7, 0x6007)  # ubfx r0, r7, #24, #8
    data(2, 0xF85B, 0x0020)  # ldr.w r0, [r11, r0, lsl #2]
    data(2, 0xEA42, 0x02C0)  # orr.w r2, r2, r0, lsl #3
    data(2, 0xF348, 0x3E00)  # sbfx lr, r8, #12, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x0007)  # ubfx r0, r2, #0, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    data(2, 0xF348, 0x3E40)  # sbfx lr, r8, #13, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x2007)  # ubfx r0, r2, #8, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    data(2, 0xF348, 0x3E80)  # sbfx lr, r8, #14, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x4007)  # ubfx r0, r2, #16, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    data(2, 0xF348, 0x3EC0)  # sbfx lr, r8, #15, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x6007)  # ubfx r0, r2, #24, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    # subpackets 0..3, hi words
    ldr(r4, [r1, 12])
    ldr(r5, [r1, 20])
    ldr(r6, [r1, 28])
    ldr(r7, [r1, 36])
    # group 4: chars 16..19
    data(2, 0xF3C4, 0x0007)  # ubfx r0, r4, #0, #8
    data(2, 0xF85B, 0x2020)  # ldr.w r2, [r11, r0, lsl #2]
    data(2, 0xF3C5, 0x0007)  # ubfx r0, r5, #0, #8
    data(2, 0xF85B, 0x0020)  # ldr.w r0, [r11, r0, lsl #2]
    data(2, 0xEA42, 0x0240)  # orr.w r2, r2, r0, lsl #1
    data(2, 0xF3C6, 0x0007)  # ubfx r0, r6, #0, #8
    data(2, 0xF85B, 0x0020)  # ldr.w r0, [r11, r0, lsl #2]
    data(2, 0xEA42, 0x0280)  # orr.w r2, r2, r0, lsl #2
    data(2, 0xF3C7, 0x0007)  # ubfx r0, r7, #0, #8
    data(2, 0xF85B, 0x0020)  # ldr.w r0, [r11, r0, lsl #2]
    data(2, 0xEA42, 0x02C0)  # orr.w r2, r2, r0, lsl #3
    data(2, 0xF348, 0x4E00)  # sbfx lr, r8, #16, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x0007)  # ubfx r0, r2, #0, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    data(2, 0xF348, 0x4E40)  # sbfx lr, r8, #17, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x2007)  # ubfx r0, r2, #8, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    data(2, 0xF348, 0x4E80)  # sbfx lr, r8, #18, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x4007)  # ubfx r0, r2, #16, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    data(2, 0xF348, 0x4EC0)  # sbfx lr, r8, #19, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x6007)  # ubfx r0, r2, #24, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    # group 5: chars 20..23
    data(2, 0xF3C4, 0x2007)  # ubfx r0, r4, #8, #8
    data(2, 0xF85B, 0x2020)  # ldr.w r2, [r11, r0, lsl #2]
    data(2, 0xF3C5, 0x2007)  # ubfx r0, r5, #8, #8
    data(2, 0xF85B, 0x0020)  # ldr.w r0, [r11, r0, lsl #2]
    data(2, 0xEA42, 0x0240)  # orr.w r2, r2, r0, lsl #1
    data(2, 0xF3C6, 0x2007)  # ubfx r0, r6, #8, #8
    data(2, 0xF85B, 0x0020)  # ldr.w r0, [r11, r0, lsl #2]
    data(2, 0xEA42, 0x0280)  # orr.w r2, r2, r0, lsl #2
    data(2, 0xF3C7, 0x2007)  # ubfx r0, r7, #8, #8
    data(2, 0xF85B, 0x0020)  # ldr.w r0, [r11, r0, lsl #2]
    data(2, 0xEA42, 0x02C0)  # orr.w r2, r2, r0, lsl #3
    data(2, 0xF348, 0x5E00)  # sbfx lr, r8, #20, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x0007)  # ubfx r0, r2, #0, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    data(2, 0xF348, 0x5E40)  # sbfx lr, r8, #21, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x2007)  # ubfx r0, r2, #8, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    data(2, 0xF348, 0x5E80)  # sbfx lr, r8, #22, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x4007)  # ubfx r0, r2, #16, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    data(2, 0xF348, 0x5EC0)  # sbfx lr, r8, #23, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x6007)  # ubfx r0, r2, #24, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    # group 6: chars 24..27
    data(2, 0xF3C4, 0x4007)  # ubfx r0, r4, #16, #8
    data(2, 0xF85B, 0x2020)  # ldr.w r2, [r11, r0, lsl #2]
    data(2, 0xF3C5, 0x4007)  # ubfx r0, r5, #16, #8
    data(2, 0xF85B, 0x0020)  # ldr.w r0, [r11, r0, lsl #2]
    data(2, 0xEA42, 0x0240)  # orr.w r2, r2, r0, lsl #1
    data(2, 0xF3C6, 0x4007)  # ubfx r0, r6, #16, #8
    data(2, 0xF85B, 0x0020)  # ldr.w r0, [r11, r0, lsl #2]
    data(2, 0xEA42, 0x0280)  # orr.w r2, r2, r0, lsl #2
    data(2, 0xF3C7, 0x4007)  # ubfx r0, r7, #16, #8
    data(2, 0xF85B, 0x0020)  # ldr.w r0, [r11, r0, lsl #2]
    data(2, 0xEA42, 0x02C0)  # orr.w r2, r2, r0, lsl #3
    data(2, 0xF348, 0x6E00)  # sbfx lr, r8, #24, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x0007)  # ubfx r0, r2, #0, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    data(2, 0xF348, 0x6E40)  # sbfx lr, r8, #25, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x2007)  # ubfx r0, r2, #8, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    data(2, 0xF348, 0x6E80)  # sbfx lr, r8, #26, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x4007)  # ubfx r0, r2, #16, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    data(2, 0xF348, 0x6EC0)  # sbfx lr, r8, #27, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x6007)  # ubfx r0, r2, #24, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    # group 7: chars 28..31
    data(2, 0xF3C4, 0x6007)  # ubfx r0, r4, #24, #8
    data(2, 0xF85B, 0x2020)  # ldr.w r2, [r11, r0, lsl #2]
    data(2, 0xF3C5, 0x6007)  # ubfx r0, r5, #24, #8
    data(2, 0xF85B, 0x0020)  # ldr.w r0, [r11, r0, lsl #2]
    data(2, 0xEA42, 0x0240)  # orr.w r2, r2, r0, lsl #1
    data(2, 0xF3C6, 0x6007)  # ubfx r0, r6, #24, #8
    data(2, 0xF85B, 0x0020)  # ldr.w r0, [r11, r0, lsl #2]
    data(2, 0xEA42, 0x0280)  # orr.w r2, r2, r0, lsl #2
    data(2, 0xF3C7, 0x6007)  # ubfx r0, r7, #24, #8
    data(2, 0xF85B, 0x0020)  # ldr.w r0, [r11, r0, lsl #2]
    data(2, 0xEA42, 0x02C0)  # orr.w r2, r2, r0, lsl #3
    data(2, 0xF348, 0x7E00)  # sbfx lr, r8, #28, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x0007)  # ubfx r0, r2, #0, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    data(2, 0xF348, 0x7E40)  # sbfx lr, r8, #29, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x2007)  # ubfx r0, r2, #8, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    data(2, 0xF348, 0x7E80)  # sbfx lr, r8, #30, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x4007)  # ubfx r0, r2, #16, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    data(2, 0xF348, 0x7EC0)  # sbfx lr, r8, #31, #1
    data(2, 0xEA0E, 0x0E0A)  # and.w lr, lr, r10
    data(2, 0xEA8E, 0x0E09)  # eor.w lr, lr, r9
    data(2, 0xF3C2, 0x6007)  # ubfx r0, r2, #24, #8
    data(2, 0xF85C, 0x0020)  # ldr.w r0, [r12, r0, lsl #2]
    data(2, 0xEA40, 0x000E)  # orr.w r0, r0, lr
    data(2, 0xF843, 0x0B04)  # str.w r0, [r3], #4
    add(r1, 44)
    # next job (11 words)
    b(LOOP)
    label(END)
    data(2, 0xE8BD, 0x0F00)  # pop.w {r8, r9, r10, r11}


@micropython.asm_thumb
def _build_jobs(r0):
    # r0 = _ST context. Builds one job per island j in [S, S1) into JOBS
    # (dst, header, 8 subpacket words, lane0 set) and copies the pre-encoded
    # AVI / Audio InfoFrame / ACR islands directly into their slots.
    # Mirrors the former viper fill_slots packet builder bit for bit.
    data(2, 0xE92D, 0x0F00)  # push.w {r8, r9, r10, r11}
    data(2, 0xF8D0, 0x3004)  # ldr r3, [r0, #4]
    # r3 = ring read index
    data(2, 0xF8D0, 0x500C)  # ldr r5, [r0, #12]
    # r5 = IEC60958 frame 0..191
    data(2, 0xF8D0, 0x8010)  # ldr.w r8, [r0, #16]
    data(2, 0xF8D0, 0xB034)  # ldr.w r11, [r0, #52]
    # r11 = TAB8: ECC[256] PAR[256] CS[192]
    data(2, 0xF8D0, 0x603C)  # ldr r6, [r0, #60]
    # r6 = job write pointer
    data(2, 0xF8D0, 0x4008)  # ldr r4, [r0, #8]
    data(2, 0xEBB4, 0x0403)  # subs r4, r4, r3
    data(2, 0xF3C4, 0x0409)  # ubfx r4, r4, #0, #10
    # r4 = samples available in ring (1024)
    label(SLOT)
    data(2, 0xF8D0, 0x1020)  # ldr r1, [r0, #32]
    data(2, 0xF8D0, 0x7024)  # ldr r7, [r0, #36]
    data(2, 0xEBB1, 0x0F07)  # cmp r1, r7
    bge_w(DONE)
    # pacing accumulator: +2*fs per slot (last slot covers one line)
    data(2, 0xF8D0, 0x7000)  # ldr r7, [r0, #0]
    data(2, 0xF8D0, 0x901C)  # ldr.w r9, [r0, #28]
    data(2, 0xF5B1, 0x7F83)  # cmp.w r1, #262
    bne(LASTN)
    data(2, 0xEB07, 0x0709)  # add r7, r7, r9
    b(ACCD)
    label(LASTN)
    data(2, 0xEB07, 0x0749)  # add.w r7, r7, r9, lsl #1
    label(ACCD)
    data(2, 0xF8C0, 0x7000)  # str r7, [r0, #0]
    # r10 = island address = slots + slot(j) * 144, slot = j mod 132 (j = 5 -> 132)
    data(2, 0xEA4F, 0x0201)  # mov r2, r1
    data(2, 0xF1B2, 0x0F84)  # cmp r2, #132
    blt(NOWRAP)
    data(2, 0xF1B2, 0x0284)  # subs r2, r2, #132
    label(NOWRAP)
    data(2, 0xF1B1, 0x0F05)  # cmp r1, #5
    # VSYNC-line island has its own slot (132): never shared with a V=1 line
    bne(NOVS)
    data(2, 0xF05F, 0x0284)  # movs r2, #132
    label(NOVS)
    data(2, 0xF8D0, 0xA02C)  # ldr.w r10, [r0, #44]
    data(2, 0xF04F, 0x0C90)  # mov.w r12, #144
    data(2, 0xFB02, 0xAA0C)  # mla r10, r2, r12, r10
    data(2, 0xF1B1, 0x0F00)  # cmp r1, #0
    beq_w(C_AVI)
    data(2, 0xF1B1, 0x0F01)  # cmp r1, #1
    beq_w(C_AIF)
    data(2, 0xF001, 0x0C0F)  # and.w r12, r1, #15
    data(2, 0xF1BC, 0x0F08)  # cmp.w r12, #8
    beq_w(C_ACR)
    # samples due this slot (0..4)
    data(2, 0xF8D0, 0x9018)  # ldr.w r9, [r0, #24]
    data(2, 0xF05F, 0x0200)  # movs r2, #0
    label(NLOOP)
    data(2, 0xEBB7, 0x0F09)  # cmp r7, r9
    blt(NDONE)
    data(2, 0xF1B2, 0x0F04)  # cmp r2, #4
    bge(NDONE)
    data(2, 0xEBB7, 0x0709)  # subs r7, r7, r9
    data(2, 0xF112, 0x0201)  # adds r2, r2, #1
    b(NLOOP)
    label(NDONE)
    data(2, 0xF8C0, 0x7000)  # str r7, [r0, #0]
    data(2, 0xF8C0, 0x2028)  # str r2, [r0, #40]
    data(2, 0xF8C6, 0xA000)  # str.w r10, [r6, #0]
    data(2, 0xF05F, 0x0200)  # movs r2, #0
    # r2 = HB2 (B flags)
    # ---- sample 0
    data(2, 0xF8D0, 0xC028)  # ldr.w r12, [r0, #40]
    data(2, 0xF1BC, 0x0F01)  # cmp.w r12, #1
    blt_w(Z0)
    data(2, 0xF1B4, 0x0F00)  # cmp r4, #0
    beq(UND0)
    data(2, 0xF8D0, 0xE030)  # ldr.w lr, [r0, #48]
    data(2, 0xF85E, 0xC023)  # ldr.w r12, [lr, r3, lsl #2]
    data(2, 0xF113, 0x0301)  # adds r3, r3, #1
    data(2, 0xF3C3, 0x0309)  # ubfx r3, r3, #0, #10
    data(2, 0xF1B4, 0x0401)  # subs r4, r4, #1
    b(GOT0)
    label(UND0)
    data(2, 0xF05F, 0x0C00)  # movs r12, #0
    data(2, 0xF108, 0x0801)  # add.w r8, r8, #1
    label(GOT0)
    data(2, 0xFA1F, 0xF78C)  # uxth r7, r12
    # r7 = L
    data(2, 0xEA4F, 0x491C)  # lsr.w r9, r12, #16
    # r9 = R
    data(2, 0xF50B, 0x7E00)  # add.w lr, r11, #512
    data(2, 0xF81E, 0xA005)  # ldrb.w r10, [lr, r5]
    # r10 = channel status bit C
    data(2, 0xFA5F, 0xFC87)  # uxtb r12, r7
    data(2, 0xF50C, 0x7C80)  # add.w r12, r12, #256
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xEA4F, 0x2C17)  # lsr.w r12, r7, #8
    data(2, 0xF50C, 0x7C80)  # add.w r12, r12, #256
    data(2, 0xF81B, 0xC00C)  # ldrb.w r12, [r11, r12]
    data(2, 0xEA8E, 0x0E0C)  # eor.w lr, lr, r12
    data(2, 0xEA8E, 0x0E0A)  # eor.w lr, lr, r10
    # lr = P_L
    data(2, 0xEA4F, 0x018A)  # lsl.w r1, r10, #2
    data(2, 0xEA41, 0x01CE)  # orr.w r1, r1, lr, lsl #3
    data(2, 0xFA5F, 0xFC89)  # uxtb r12, r9
    data(2, 0xF50C, 0x7C80)  # add.w r12, r12, #256
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xEA4F, 0x2C19)  # lsr.w r12, r9, #8
    data(2, 0xF50C, 0x7C80)  # add.w r12, r12, #256
    data(2, 0xF81B, 0xC00C)  # ldrb.w r12, [r11, r12]
    data(2, 0xEA8E, 0x0E0C)  # eor.w lr, lr, r12
    data(2, 0xEA8E, 0x0E0A)  # eor.w lr, lr, r10
    # lr = P_R
    data(2, 0xEA41, 0x118A)  # orr.w r1, r1, r10, lsl #6
    data(2, 0xEA41, 0x11CE)  # orr.w r1, r1, lr, lsl #7
    # r1 = SB6 flags
    data(2, 0xF1B5, 0x0F00)  # cmp r5, #0
    bne(NB0)
    data(2, 0xF042, 0x0210)  # orr.w r2, r2, #16
    label(NB0)
    data(2, 0xF115, 0x0501)  # adds r5, r5, #1
    data(2, 0xF1B5, 0x0FC0)  # cmp r5, #192
    bne(NF0)
    data(2, 0xF05F, 0x0500)  # movs r5, #0
    label(NF0)
    # ECC over SB0..SB6 = 0, L lo, L hi, 0, R lo, R hi, flags
    data(2, 0xFA5F, 0xFC87)  # uxtb r12, r7
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xEA4F, 0x2C17)  # lsr.w r12, r7, #8
    data(2, 0xEA8C, 0x0C0E)  # eor.w r12, r12, lr
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xF81B, 0xE00E)  # ldrb.w lr, [r11, lr]
    data(2, 0xFA5F, 0xFC89)  # uxtb r12, r9
    data(2, 0xEA8C, 0x0C0E)  # eor.w r12, r12, lr
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xEA4F, 0x2C19)  # lsr.w r12, r9, #8
    data(2, 0xEA8C, 0x0C0E)  # eor.w r12, r12, lr
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xEA81, 0x0C0E)  # eor.w r12, r1, lr
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xEA4F, 0x2C07)  # lsl.w r12, r7, #8
    data(2, 0xF8C6, 0xC008)  # str.w r12, [r6, #8]
    data(2, 0xEA49, 0x4C01)  # orr.w r12, r9, r1, lsl #16
    data(2, 0xEA4C, 0x6C0E)  # orr.w r12, r12, lr, lsl #24
    data(2, 0xF8C6, 0xC00C)  # str.w r12, [r6, #12]
    # ---- sample 1
    data(2, 0xF8D0, 0xC028)  # ldr.w r12, [r0, #40]
    data(2, 0xF1BC, 0x0F02)  # cmp.w r12, #2
    blt_w(Z1)
    data(2, 0xF1B4, 0x0F00)  # cmp r4, #0
    beq(UND1)
    data(2, 0xF8D0, 0xE030)  # ldr.w lr, [r0, #48]
    data(2, 0xF85E, 0xC023)  # ldr.w r12, [lr, r3, lsl #2]
    data(2, 0xF113, 0x0301)  # adds r3, r3, #1
    data(2, 0xF3C3, 0x0309)  # ubfx r3, r3, #0, #10
    data(2, 0xF1B4, 0x0401)  # subs r4, r4, #1
    b(GOT1)
    label(UND1)
    data(2, 0xF05F, 0x0C00)  # movs r12, #0
    data(2, 0xF108, 0x0801)  # add.w r8, r8, #1
    label(GOT1)
    data(2, 0xFA1F, 0xF78C)  # uxth r7, r12
    # r7 = L
    data(2, 0xEA4F, 0x491C)  # lsr.w r9, r12, #16
    # r9 = R
    data(2, 0xF50B, 0x7E00)  # add.w lr, r11, #512
    data(2, 0xF81E, 0xA005)  # ldrb.w r10, [lr, r5]
    # r10 = channel status bit C
    data(2, 0xFA5F, 0xFC87)  # uxtb r12, r7
    data(2, 0xF50C, 0x7C80)  # add.w r12, r12, #256
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xEA4F, 0x2C17)  # lsr.w r12, r7, #8
    data(2, 0xF50C, 0x7C80)  # add.w r12, r12, #256
    data(2, 0xF81B, 0xC00C)  # ldrb.w r12, [r11, r12]
    data(2, 0xEA8E, 0x0E0C)  # eor.w lr, lr, r12
    data(2, 0xEA8E, 0x0E0A)  # eor.w lr, lr, r10
    # lr = P_L
    data(2, 0xEA4F, 0x018A)  # lsl.w r1, r10, #2
    data(2, 0xEA41, 0x01CE)  # orr.w r1, r1, lr, lsl #3
    data(2, 0xFA5F, 0xFC89)  # uxtb r12, r9
    data(2, 0xF50C, 0x7C80)  # add.w r12, r12, #256
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xEA4F, 0x2C19)  # lsr.w r12, r9, #8
    data(2, 0xF50C, 0x7C80)  # add.w r12, r12, #256
    data(2, 0xF81B, 0xC00C)  # ldrb.w r12, [r11, r12]
    data(2, 0xEA8E, 0x0E0C)  # eor.w lr, lr, r12
    data(2, 0xEA8E, 0x0E0A)  # eor.w lr, lr, r10
    # lr = P_R
    data(2, 0xEA41, 0x118A)  # orr.w r1, r1, r10, lsl #6
    data(2, 0xEA41, 0x11CE)  # orr.w r1, r1, lr, lsl #7
    # r1 = SB6 flags
    data(2, 0xF1B5, 0x0F00)  # cmp r5, #0
    bne(NB1)
    data(2, 0xF042, 0x0220)  # orr.w r2, r2, #32
    label(NB1)
    data(2, 0xF115, 0x0501)  # adds r5, r5, #1
    data(2, 0xF1B5, 0x0FC0)  # cmp r5, #192
    bne(NF1)
    data(2, 0xF05F, 0x0500)  # movs r5, #0
    label(NF1)
    # ECC over SB0..SB6 = 0, L lo, L hi, 0, R lo, R hi, flags
    data(2, 0xFA5F, 0xFC87)  # uxtb r12, r7
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xEA4F, 0x2C17)  # lsr.w r12, r7, #8
    data(2, 0xEA8C, 0x0C0E)  # eor.w r12, r12, lr
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xF81B, 0xE00E)  # ldrb.w lr, [r11, lr]
    data(2, 0xFA5F, 0xFC89)  # uxtb r12, r9
    data(2, 0xEA8C, 0x0C0E)  # eor.w r12, r12, lr
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xEA4F, 0x2C19)  # lsr.w r12, r9, #8
    data(2, 0xEA8C, 0x0C0E)  # eor.w r12, r12, lr
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xEA81, 0x0C0E)  # eor.w r12, r1, lr
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xEA4F, 0x2C07)  # lsl.w r12, r7, #8
    data(2, 0xF8C6, 0xC010)  # str.w r12, [r6, #16]
    data(2, 0xEA49, 0x4C01)  # orr.w r12, r9, r1, lsl #16
    data(2, 0xEA4C, 0x6C0E)  # orr.w r12, r12, lr, lsl #24
    data(2, 0xF8C6, 0xC014)  # str.w r12, [r6, #20]
    # ---- sample 2
    data(2, 0xF8D0, 0xC028)  # ldr.w r12, [r0, #40]
    data(2, 0xF1BC, 0x0F03)  # cmp.w r12, #3
    blt_w(Z2)
    data(2, 0xF1B4, 0x0F00)  # cmp r4, #0
    beq(UND2)
    data(2, 0xF8D0, 0xE030)  # ldr.w lr, [r0, #48]
    data(2, 0xF85E, 0xC023)  # ldr.w r12, [lr, r3, lsl #2]
    data(2, 0xF113, 0x0301)  # adds r3, r3, #1
    data(2, 0xF3C3, 0x0309)  # ubfx r3, r3, #0, #10
    data(2, 0xF1B4, 0x0401)  # subs r4, r4, #1
    b(GOT2)
    label(UND2)
    data(2, 0xF05F, 0x0C00)  # movs r12, #0
    data(2, 0xF108, 0x0801)  # add.w r8, r8, #1
    label(GOT2)
    data(2, 0xFA1F, 0xF78C)  # uxth r7, r12
    # r7 = L
    data(2, 0xEA4F, 0x491C)  # lsr.w r9, r12, #16
    # r9 = R
    data(2, 0xF50B, 0x7E00)  # add.w lr, r11, #512
    data(2, 0xF81E, 0xA005)  # ldrb.w r10, [lr, r5]
    # r10 = channel status bit C
    data(2, 0xFA5F, 0xFC87)  # uxtb r12, r7
    data(2, 0xF50C, 0x7C80)  # add.w r12, r12, #256
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xEA4F, 0x2C17)  # lsr.w r12, r7, #8
    data(2, 0xF50C, 0x7C80)  # add.w r12, r12, #256
    data(2, 0xF81B, 0xC00C)  # ldrb.w r12, [r11, r12]
    data(2, 0xEA8E, 0x0E0C)  # eor.w lr, lr, r12
    data(2, 0xEA8E, 0x0E0A)  # eor.w lr, lr, r10
    # lr = P_L
    data(2, 0xEA4F, 0x018A)  # lsl.w r1, r10, #2
    data(2, 0xEA41, 0x01CE)  # orr.w r1, r1, lr, lsl #3
    data(2, 0xFA5F, 0xFC89)  # uxtb r12, r9
    data(2, 0xF50C, 0x7C80)  # add.w r12, r12, #256
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xEA4F, 0x2C19)  # lsr.w r12, r9, #8
    data(2, 0xF50C, 0x7C80)  # add.w r12, r12, #256
    data(2, 0xF81B, 0xC00C)  # ldrb.w r12, [r11, r12]
    data(2, 0xEA8E, 0x0E0C)  # eor.w lr, lr, r12
    data(2, 0xEA8E, 0x0E0A)  # eor.w lr, lr, r10
    # lr = P_R
    data(2, 0xEA41, 0x118A)  # orr.w r1, r1, r10, lsl #6
    data(2, 0xEA41, 0x11CE)  # orr.w r1, r1, lr, lsl #7
    # r1 = SB6 flags
    data(2, 0xF1B5, 0x0F00)  # cmp r5, #0
    bne(NB2)
    data(2, 0xF042, 0x0240)  # orr.w r2, r2, #64
    label(NB2)
    data(2, 0xF115, 0x0501)  # adds r5, r5, #1
    data(2, 0xF1B5, 0x0FC0)  # cmp r5, #192
    bne(NF2)
    data(2, 0xF05F, 0x0500)  # movs r5, #0
    label(NF2)
    # ECC over SB0..SB6 = 0, L lo, L hi, 0, R lo, R hi, flags
    data(2, 0xFA5F, 0xFC87)  # uxtb r12, r7
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xEA4F, 0x2C17)  # lsr.w r12, r7, #8
    data(2, 0xEA8C, 0x0C0E)  # eor.w r12, r12, lr
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xF81B, 0xE00E)  # ldrb.w lr, [r11, lr]
    data(2, 0xFA5F, 0xFC89)  # uxtb r12, r9
    data(2, 0xEA8C, 0x0C0E)  # eor.w r12, r12, lr
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xEA4F, 0x2C19)  # lsr.w r12, r9, #8
    data(2, 0xEA8C, 0x0C0E)  # eor.w r12, r12, lr
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xEA81, 0x0C0E)  # eor.w r12, r1, lr
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xEA4F, 0x2C07)  # lsl.w r12, r7, #8
    data(2, 0xF8C6, 0xC018)  # str.w r12, [r6, #24]
    data(2, 0xEA49, 0x4C01)  # orr.w r12, r9, r1, lsl #16
    data(2, 0xEA4C, 0x6C0E)  # orr.w r12, r12, lr, lsl #24
    data(2, 0xF8C6, 0xC01C)  # str.w r12, [r6, #28]
    # ---- sample 3
    data(2, 0xF8D0, 0xC028)  # ldr.w r12, [r0, #40]
    data(2, 0xF1BC, 0x0F04)  # cmp.w r12, #4
    blt_w(Z3)
    data(2, 0xF1B4, 0x0F00)  # cmp r4, #0
    beq(UND3)
    data(2, 0xF8D0, 0xE030)  # ldr.w lr, [r0, #48]
    data(2, 0xF85E, 0xC023)  # ldr.w r12, [lr, r3, lsl #2]
    data(2, 0xF113, 0x0301)  # adds r3, r3, #1
    data(2, 0xF3C3, 0x0309)  # ubfx r3, r3, #0, #10
    data(2, 0xF1B4, 0x0401)  # subs r4, r4, #1
    b(GOT3)
    label(UND3)
    data(2, 0xF05F, 0x0C00)  # movs r12, #0
    data(2, 0xF108, 0x0801)  # add.w r8, r8, #1
    label(GOT3)
    data(2, 0xFA1F, 0xF78C)  # uxth r7, r12
    # r7 = L
    data(2, 0xEA4F, 0x491C)  # lsr.w r9, r12, #16
    # r9 = R
    data(2, 0xF50B, 0x7E00)  # add.w lr, r11, #512
    data(2, 0xF81E, 0xA005)  # ldrb.w r10, [lr, r5]
    # r10 = channel status bit C
    data(2, 0xFA5F, 0xFC87)  # uxtb r12, r7
    data(2, 0xF50C, 0x7C80)  # add.w r12, r12, #256
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xEA4F, 0x2C17)  # lsr.w r12, r7, #8
    data(2, 0xF50C, 0x7C80)  # add.w r12, r12, #256
    data(2, 0xF81B, 0xC00C)  # ldrb.w r12, [r11, r12]
    data(2, 0xEA8E, 0x0E0C)  # eor.w lr, lr, r12
    data(2, 0xEA8E, 0x0E0A)  # eor.w lr, lr, r10
    # lr = P_L
    data(2, 0xEA4F, 0x018A)  # lsl.w r1, r10, #2
    data(2, 0xEA41, 0x01CE)  # orr.w r1, r1, lr, lsl #3
    data(2, 0xFA5F, 0xFC89)  # uxtb r12, r9
    data(2, 0xF50C, 0x7C80)  # add.w r12, r12, #256
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xEA4F, 0x2C19)  # lsr.w r12, r9, #8
    data(2, 0xF50C, 0x7C80)  # add.w r12, r12, #256
    data(2, 0xF81B, 0xC00C)  # ldrb.w r12, [r11, r12]
    data(2, 0xEA8E, 0x0E0C)  # eor.w lr, lr, r12
    data(2, 0xEA8E, 0x0E0A)  # eor.w lr, lr, r10
    # lr = P_R
    data(2, 0xEA41, 0x118A)  # orr.w r1, r1, r10, lsl #6
    data(2, 0xEA41, 0x11CE)  # orr.w r1, r1, lr, lsl #7
    # r1 = SB6 flags
    data(2, 0xF1B5, 0x0F00)  # cmp r5, #0
    bne(NB3)
    data(2, 0xF042, 0x0280)  # orr.w r2, r2, #128
    label(NB3)
    data(2, 0xF115, 0x0501)  # adds r5, r5, #1
    data(2, 0xF1B5, 0x0FC0)  # cmp r5, #192
    bne(NF3)
    data(2, 0xF05F, 0x0500)  # movs r5, #0
    label(NF3)
    # ECC over SB0..SB6 = 0, L lo, L hi, 0, R lo, R hi, flags
    data(2, 0xFA5F, 0xFC87)  # uxtb r12, r7
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xEA4F, 0x2C17)  # lsr.w r12, r7, #8
    data(2, 0xEA8C, 0x0C0E)  # eor.w r12, r12, lr
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xF81B, 0xE00E)  # ldrb.w lr, [r11, lr]
    data(2, 0xFA5F, 0xFC89)  # uxtb r12, r9
    data(2, 0xEA8C, 0x0C0E)  # eor.w r12, r12, lr
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xEA4F, 0x2C19)  # lsr.w r12, r9, #8
    data(2, 0xEA8C, 0x0C0E)  # eor.w r12, r12, lr
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xEA81, 0x0C0E)  # eor.w r12, r1, lr
    data(2, 0xF81B, 0xE00C)  # ldrb.w lr, [r11, r12]
    data(2, 0xEA4F, 0x2C07)  # lsl.w r12, r7, #8
    data(2, 0xF8C6, 0xC020)  # str.w r12, [r6, #32]
    data(2, 0xEA49, 0x4C01)  # orr.w r12, r9, r1, lsl #16
    data(2, 0xEA4C, 0x6C0E)  # orr.w r12, r12, lr, lsl #24
    data(2, 0xF8C6, 0xC024)  # str.w r12, [r6, #36]
    b(HDR)
    label(Z0)
    data(2, 0xF05F, 0x0C00)  # movs r12, #0
    data(2, 0xF8C6, 0xC008)  # str.w r12, [r6, #8]
    data(2, 0xF8C6, 0xC00C)  # str.w r12, [r6, #12]
    label(Z1)
    data(2, 0xF05F, 0x0C00)  # movs r12, #0
    data(2, 0xF8C6, 0xC010)  # str.w r12, [r6, #16]
    data(2, 0xF8C6, 0xC014)  # str.w r12, [r6, #20]
    label(Z2)
    data(2, 0xF05F, 0x0C00)  # movs r12, #0
    data(2, 0xF8C6, 0xC018)  # str.w r12, [r6, #24]
    data(2, 0xF8C6, 0xC01C)  # str.w r12, [r6, #28]
    label(Z3)
    data(2, 0xF05F, 0x0C00)  # movs r12, #0
    data(2, 0xF8C6, 0xC020)  # str.w r12, [r6, #32]
    data(2, 0xF8C6, 0xC024)  # str.w r12, [r6, #36]
    label(HDR)
    data(2, 0xF8D0, 0xC028)  # ldr.w r12, [r0, #40]
    data(2, 0xF1BC, 0x0F00)  # cmp.w r12, #0
    beq(HNULL)
    data(2, 0xF05F, 0x0701)  # movs r7, #1
    data(2, 0xFA07, 0xF70C)  # lsl.w r7, r7, r12
    data(2, 0xF1B7, 0x0701)  # subs r7, r7, #1
    # r7 = HB1 sample_present
    data(2, 0xF89B, 0xE002)  # ldrb.w lr, [r11, #2]
    data(2, 0xEA8E, 0x0E07)  # eor.w lr, lr, r7
    data(2, 0xF81B, 0xE00E)  # ldrb.w lr, [r11, lr]
    data(2, 0xEA8E, 0x0E02)  # eor.w lr, lr, r2
    data(2, 0xF81B, 0xE00E)  # ldrb.w lr, [r11, lr]
    data(2, 0xF05F, 0x0C02)  # movs r12, #2
    data(2, 0xEA4C, 0x2C07)  # orr.w r12, r12, r7, lsl #8
    data(2, 0xEA4C, 0x4C02)  # orr.w r12, r12, r2, lsl #16
    data(2, 0xEA4C, 0x6C0E)  # orr.w r12, r12, lr, lsl #24
    label(HNULL)
    data(2, 0xF8C6, 0xC004)  # str.w r12, [r6, #4]
    data(2, 0xF8D0, 0x1020)  # ldr r1, [r0, #32]
    data(2, 0xF1B1, 0x0F05)  # cmp r1, #5
    bne(LV1)
    data(2, 0xF8D0, 0xC044)  # ldr.w r12, [r0, #68]
    b(LSET)
    label(LV1)
    data(2, 0xF8D0, 0xC040)  # ldr.w r12, [r0, #64]
    label(LSET)
    data(2, 0xF8C6, 0xC028)  # str.w r12, [r6, #40]
    data(2, 0xF116, 0x062C)  # adds r6, r6, #44
    label(NEXT)
    data(2, 0xF8D0, 0x1020)  # ldr r1, [r0, #32]
    data(2, 0xF111, 0x0101)  # adds r1, r1, #1
    data(2, 0xF8C0, 0x1020)  # str r1, [r0, #32]
    b(SLOT)
    # constant packets: copy 36 pre-encoded words
    label(C_AVI)
    data(2, 0xF05F, 0x0C00)  # movs r12, #0
    b(CCOPY)
    label(C_AIF)
    data(2, 0xF05F, 0x0C90)  # movs r12, #144
    b(CCOPY)
    label(C_ACR)
    data(2, 0xF44F, 0x7C90)  # mov.w r12, #288
    label(CCOPY)
    data(2, 0xF8D0, 0x9038)  # ldr.w r9, [r0, #56]
    data(2, 0xEB09, 0x090C)  # add r9, r9, r12
    data(2, 0xF05F, 0x0C24)  # movs r12, #36
    label(CC)
    data(2, 0xF859, 0xEB04)  # ldr.w lr, [r9], #4
    data(2, 0xF84A, 0xEB04)  # str.w lr, [r10], #4
    data(2, 0xF1BC, 0x0C01)  # subs.w r12, r12, #1
    bne(CC)
    b(NEXT)
    label(DONE)
    data(2, 0xF05F, 0x0C00)  # movs r12, #0
    data(2, 0xF8C6, 0xC000)  # str.w r12, [r6, #0]
    # job list terminator
    data(2, 0xF8C0, 0x3004)  # str r3, [r0, #4]
    data(2, 0xF8C0, 0x500C)  # str r5, [r0, #12]
    data(2, 0xF8C0, 0x8010)  # str.w r8, [r0, #16]
    data(2, 0xE8BD, 0x0F00)  # pop.w {r8, r9, r10, r11}


def fill_slots(s0, s1):
    """Rebuild islands j in [s0, s1) (max 66): packets in asm, then TERC4 in asm."""
    st = _ST
    st[S_S] = s0
    st[S_S1] = s1
    _build_jobs(st)
    _encode_jobs(_JOBS, _TABS)

@micropython.viper
def service_flags(fl: ptr32) -> int:
    """fl: the driver's 4 quarter flags (set by DMA). For each quarter q that
    has been sent, encode quarter q+2 into the freed slots. No heap allocation:
    safe in a hard IRQ handler. Returns quarters encoded."""
    st = ptr32(_ST)
    qb = ptr32(_QB)
    done = 0
    q = 0
    while q < 4:
        if fl[q]:
            fl[q] = 0
            if fl[(q + 1) & 3]:
                st[S_LATE] = st[S_LATE] + 1     # next quarter already done too: we were late
            n = (q + 2) & 3
            st[S_S] = qb[n]
            st[S_S1] = qb[n + 1]
            _build_jobs(_ST)
            _encode_jobs(_JOBS, _TABS)
            done += 1
        q += 1
    return done

# ── producer side ────────────────────────────────────────────────────────────
@micropython.viper
def audio_free() -> int:
    st = ptr32(_ST)
    return (st[S_RD] - st[S_WR] - 1) & RING_MASK

@micropython.viper
def audio_write(src: ptr16, n: int) -> int:
    """src: interleaved int16 L,R (array 'h'), n stereo frames. Returns frames taken."""
    st = ptr32(_ST)
    ring = ptr32(_RING)
    wr = st[S_WR]
    free = (st[S_RD] - wr - 1) & RING_MASK
    if n > free:
        n = free
    i = 0
    j = 0
    while i < n:
        ring[wr] = (src[j] & 0xFFFF) | (src[j + 1] << 16)
        wr = (wr + 1) & RING_MASK
        j += 2
        i += 1
    st[S_WR] = wr
    return n

@micropython.viper
def audio_write_packed(src: ptr32, n: int) -> int:
    """src: array 'I' of (L & 0xFFFF) | (R << 16). Returns frames taken."""
    st = ptr32(_ST)
    ring = ptr32(_RING)
    wr = st[S_WR]
    free = (st[S_RD] - wr - 1) & RING_MASK
    if n > free:
        n = free
    i = 0
    while i < n:
        ring[wr] = src[i]
        wr = (wr + 1) & RING_MASK
        i += 1
    st[S_WR] = wr
    return n

def stats():
    """(underrun samples, late services, ring fill)"""
    return _ST[S_UNDER], _ST[S_LATE], (_ST[S_WR] - _ST[S_RD]) & RING_MASK
