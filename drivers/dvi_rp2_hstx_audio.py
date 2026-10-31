#-------------------------------------------------------------------------------
# SPDX-License-Identifier: MIT
#
# Copyright (c) 2025 SparkFun Electronics
#-------------------------------------------------------------------------------
# DVI/HDMI display driver using the RP2350 HSTX interface, with optional HDMI
# data-island audio. Derived from red_vision/displays/dvi_rp2_hstx.py, itself
# partially derived from Adafruit CircuitPython picodvi Framebuffer_RP2350.c
# (MIT, Copyright (c) 2023 Scott Shawcroft for Adafruit Industries).
#
# Changes vs dvi_rp2_hstx_frame_sync_v2.py:
#   begin(..., audio_rate=0, pixel_hz=None)
#     audio_rate = 0      -> plain DVI, identical signal to v2
#     audio_rate = 32000 / 44100 / 48000 -> HDMI mode:
#       - every active line gets video preamble (8) + video guard band (2)
#         at the end of the back porch
#       - every even line (263 per frame) carries one data-island packet
#         inside HSYNC: 8 preamble + 2 GB + 32 TERC4 + 2 GB
#       - even line = 3 control blocks: shared head (FP, preamble, RAW cmd),
#         its 36-word island, shared tail; islands live in 132 SRAM slots
#         (island j -> slot j % 132, VSYNC island -> slot 132; 19 KB)
#       - 4 DMA markers (one per island quarter) tell the CPU when a slot set
#         is free; it then encodes the quarter two ahead (~4.3 ms deadline).
#         Markers run on a 4th DMA channel ("notify") whose completion can
#         raise a hard IRQ: audio_irq(handler) -> fully interrupt-driven audio
#   audio_service()  call from core1 loop (>= every ~3 ms); re-encodes a quarter
#   audio_write(buf_h, n) / audio_free() / audio_stats()
#
# Line layout (front porch / sync first, active pixels last):
#   FP 16 | HSYNC 96 [pre 8 | island 36 | ctrl 52] | BP 48 [ctrl 38 | vpre 8 | vgb 2] | active 640
#-------------------------------------------------------------------------------

import gc
import rp2
from micropython import const
import machine
import array
from uctypes import addressof
import colors as rv_colors
import memory as rv_memory
import hdmi_audio as _ha

# TMDS control symbols, index = (C1 << 1) | C0  (lane0: C1=VSYNC, C0=HSYNC)
_TMDS_CTRL = (0x354, 0x0AB, 0x154, 0x2AB)
_VGB = (0x2CC << 20) | (0x133 << 10) | 0x2CC          # video guard band

# HSTX command opcodes (RP2350 datasheet 12.11.5)
_HSTX_CMD_RAW         = 0x0 << 12
_HSTX_CMD_RAW_REPEAT  = 0x1 << 12
_HSTX_CMD_TMDS        = 0x2 << 12
_HSTX_CMD_TMDS_REPEAT = 0x3 << 12
_HSTX_CMD_NOP         = 0xF << 12

def _sync(v, h, c1=0, c2=0):
    """Control word: lane0 = sync symbol, lane1/lane2 = control symbols c1/c2."""
    return (_TMDS_CTRL[c2] << 20) | (_TMDS_CTRL[c1] << 10) | _TMDS_CTRL[(v << 1) | h]


class DVI_RP2_HSTX():
    _H_FRONT_PORCH   = 16
    _H_SYNC_WIDTH    = 96
    _H_BACK_PORCH    = 48
    _H_ACTIVE_PIXELS = 640

    _V_FRONT_PORCH  = 10
    _V_SYNC_WIDTH   = 2
    _V_BACK_PORCH   = 33
    _V_ACTIVE_LINES = 480

    _H_TOTAL_PIXELS = _H_FRONT_PORCH + _H_SYNC_WIDTH + _H_BACK_PORCH + _H_ACTIVE_PIXELS
    _V_BLANK_LINES  = _V_FRONT_PORCH + _V_SYNC_WIDTH + _V_BACK_PORCH
    _V_TOTAL_LINES  = _V_FRONT_PORCH + _V_SYNC_WIDTH + _V_BACK_PORCH + _V_ACTIVE_LINES

    _PRE_LEN    = 8     # preamble chars
    _ISLAND_LEN = 36    # 2 GB + 32 + 2 GB (one packet)
    _VPRE_LEN   = 10    # video preamble 8 + guard band 2

    def __init__(
            self,
            pin_clk_p  = 13,
            pin_clk_n  = 12,
            pin_d0_p   = 15,
            pin_d0_n   = 14,
            pin_d1_p   = 17,
            pin_d1_n   = 16,
            pin_d2_p   = 19,
            pin_d2_n   = 18,
        ):
        self._pin_clk_p = pin_clk_p
        self._pin_clk_n = pin_clk_n
        self._pin_d0_p = pin_d0_p
        self._pin_d0_n = pin_d0_n
        self._pin_d1_p = pin_d1_p
        self._pin_d1_n = pin_d1_n
        self._pin_d2_p = pin_d2_p
        self._pin_d2_n = pin_d2_n
        self._audio = False

    def begin(self, buffer, color_mode, height=None, width=None, bytes_per_pixel=None,
              audio_rate=0, pixel_hz=None):
        """
        Begins DVI output (or HDMI with audio if audio_rate != 0).

        pixel_hz: TMDS character clock. Default = clk_sys / CLK_HSTX_DIV / 5,
                  assuming clk_hstx is sourced from clk_sys.
        """
        gc.collect()

        self._buffer = buffer
        self._color_mode = color_mode
        if height is None:
            self._height, self._width, self._bytes_per_pixel = self._buffer.shape
        else:
            self._height = height
            self._width = width
            self._bytes_per_pixel = bytes_per_pixel

        self._frame_flag = array.array('I', [0])
        self._frame_one = array.array('I', [1])

        self._width_scale = self._H_ACTIVE_PIXELS // self._width
        self._height_scale = self._V_ACTIVE_LINES // self._height

        self._audio = bool(audio_rate)
        if self._audio:
            if pixel_hz is None:
                div = (machine.mem32[0x40010058] >> 16) & 0xFFFF   # CLK_HSTX_DIV.INT
                pixel_hz = machine.freq() // max(div, 1) // 5
            self._pixel_hz = pixel_hz
            self._audio_rate = audio_rate
            # aflag[q] set by DMA when island quarter q has been sent
            self._aflag = array.array('I', [0, 0, 0, 0])
            gc.collect()
            self._slots = bytearray(_ha.N_SLOT * _ha.SLOT_W * 4)    # zeroed, no temp list
            if rv_memory.is_in_external_ram(self._slots):
                raise MemoryError("not enough space in SRAM for audio slots (%d bytes); "
                                  "allocate display.begin() earlier" % len(self._slots))
            self._acr = _ha.init(self._slots, pixel_hz, audio_rate, self._H_TOTAL_PIXELS)
            # hot tables in PSRAM go through the XIP cache -> slow audio_service()
            for nm in ('_T12', '_SP', '_TAB8', '_JOBS', '_RING'):
                if rv_memory.is_in_external_ram(getattr(_ha, nm)):
                    print('hdmi_audio: %s is in PSRAM (slow)' % nm)

        self._configure_hstx()
        self._configure_dmas()
        self._start()

    # ── audio API ────────────────────────────────────────────────────────────
    def audio_service(self):
        """
        When island quarter q has been sent, encode quarter q+2 into the
        same slots. Non-blocking; returns True if work was done.
        Deadline after each marker: ~132 lines (~4.3 ms at 30.75 kHz).
        """
        return _ha.service_flags(self._aflag) != 0

    def audio_irq(self, handler):
        """Install a hard IRQ handler(dma) that fires 4x per frame when an island
        quarter has been sent (handler must call _ha.service_flags(display._aflag)
        and must not allocate). The IRQ runs on the core that calls this.
        handler=None disables it."""
        self._dma_notify.irq(handler, hard=True)

    def audio_write(self, buf, n):
        """buf: array('h') interleaved L,R. Returns stereo frames accepted."""
        return _ha.audio_write(buf, n)

    def audio_write_packed(self, buf, n):
        """buf: array('I') of (L & 0xFFFF) | (R << 16)."""
        return _ha.audio_write_packed(buf, n)

    def audio_free(self):
        return _ha.audio_free()

    def audio_stats(self):
        """(underrun samples, late services, ring fill, (N, CTS))"""
        return _ha.stats() + (self._acr,)

    # ── unchanged API ────────────────────────────────────────────────────────
    def resolution_default(self):
        return (self._V_ACTIVE_LINES // 2, self._H_ACTIVE_PIXELS // 2)

    def resolution_is_supported(self, height, width):
        width_supported = (self._H_ACTIVE_PIXELS % width == 0)
        height_supported = (self._V_ACTIVE_LINES % height == 0)
        if not width_supported or not height_supported:
            return False
        return self._H_ACTIVE_PIXELS / width <= 32

    def color_mode_default(self):
        return rv_colors.COLOR_MODE_BGR565

    def color_mode_is_supported(self, color_mode):
        return color_mode == rv_colors.COLOR_MODE_BGR565

    def _configure_hstx(self):
        self._hstx = rp2.HSTX()

        expand_shift = self._hstx.pack_expand_shift(
            enc_n_shifts = self._width_scale % 32,
            enc_shift = 0,
            raw_n_shifts = 1,
            raw_shift = 0
        )
        self._hstx.expand_shift(expand_shift)

        if self._color_mode == rv_colors.COLOR_MODE_BGR233:
            expand_tmds = self._hstx.pack_expand_tmds(
                l2_nbits = 2, l2_rot = 0,
                l1_nbits = 2, l1_rot = 29,
                l0_nbits = 1, l0_rot = 26,
            )
        elif self._color_mode == rv_colors.COLOR_MODE_GRAY8:
            expand_tmds = self._hstx.pack_expand_tmds(
                l2_nbits = 7, l2_rot = 0,
                l1_nbits = 7, l1_rot = 0,
                l0_nbits = 7, l0_rot = 0,
            )
        elif self._color_mode == rv_colors.COLOR_MODE_BGR565:
            expand_tmds = self._hstx.pack_expand_tmds(
                l2_nbits = 4, l2_rot = 8,
                l1_nbits = 5, l1_rot = 3,
                l0_nbits = 4, l0_rot = 29,
            )
        elif self._color_mode == rv_colors.COLOR_MODE_BGRA8888:
            expand_tmds = self._hstx.pack_expand_tmds(
                l2_nbits = 7, l2_rot = 16,
                l1_nbits = 7, l1_rot = 8,
                l0_nbits = 7, l0_rot = 0,
            )
        self._hstx.expand_tmds(expand_tmds)

        self._hstx.bit(self._pin_clk_p, self._hstx.pack_bit(clk=1,              inv=0))
        self._hstx.bit(self._pin_clk_n, self._hstx.pack_bit(clk=1,              inv=1))
        self._hstx.bit(self._pin_d0_p,  self._hstx.pack_bit(sel_p= 0, sel_n= 1, inv=0))
        self._hstx.bit(self._pin_d0_n,  self._hstx.pack_bit(sel_p= 0, sel_n= 1, inv=1))
        self._hstx.bit(self._pin_d1_p,  self._hstx.pack_bit(sel_p=10, sel_n=11, inv=0))
        self._hstx.bit(self._pin_d1_n,  self._hstx.pack_bit(sel_p=10, sel_n=11, inv=1))
        self._hstx.bit(self._pin_d2_p,  self._hstx.pack_bit(sel_p=20, sel_n=21, inv=0))
        self._hstx.bit(self._pin_d2_n,  self._hstx.pack_bit(sel_p=20, sel_n=21, inv=1))

        for i in range(12, 20):
            machine.Pin(i, mode=machine.Pin.ALT, alt=machine.Pin.ALT_HSTX)

        csr = self._hstx.pack_csr(
            clk_div = 5,
            n_shifts = 5,
            shift = 2,
            expand_enable = 1,
            enable = 1
        )
        self._hstx.csr(csr)

    def _configure_dmas(self):
        # Dispatcher feeds control blocks to the executer; executer feeds HSTX.
        # Optional streamer copies PSRAM rows via XIP stream into an SRAM row
        # buffer. See dvi_rp2_hstx_frame_sync_v2.py for the full write-up.
        self._dma_dispatcher = rp2.DMA()
        self._dma_executer = rp2.DMA()
        if self._audio:
            self._dma_notify = rp2.DMA()      # quarter markers (+ optional IRQ)

        self._buffer_is_in_psram = rv_memory.is_in_external_ram(self._buffer)

        if self._buffer_is_in_psram:
            self._dma_streamer = rp2.DMA()

            hstx_pixels_per_second = machine.freq() / 5
            psram_bytes_per_second = rv_memory.external_ram_max_bytes_per_second()
            psram_pixels_per_second = psram_bytes_per_second * self._width_scale / self._bytes_per_pixel
            if psram_pixels_per_second < hstx_pixels_per_second:
                raise ValueError("PSRAM transfer speed too low for specified resolution and color mode")

            self._bytes_per_row = self._width * self._bytes_per_pixel
            if self._bytes_per_row & 3:
                raise ValueError("row buffer size must be a multiple of 4 bytes")
            self._row_buffer = array.array('I', [0] * (self._bytes_per_row >> 2))
            if rv_memory.is_in_external_ram(self._row_buffer):
                raise MemoryError("not enough space in SRAM for row buffer")

            # Unlock DMA access to XIP_CTRL (ACCESSCTRL password 0xACCE).
            ACCESSCTRL_BASE = 0x40060000
            XIP_CTRL = ACCESSCTRL_BASE + 0xE0
            machine.mem32[XIP_CTRL] = 0xACCE0000 | 0b11111000

        self._create_dma_ctrl_registers()
        self._create_control_blocks()
        self._assemble_control_blocks()

        self._dma_dispatcher.config(
            read = self._cb_restart_frame_nested[0],
            write = self._cb_restart_frame_nested[1],
            count = self._cb_restart_frame_nested[2],
            ctrl = self._cb_restart_frame_nested[3],
            trigger = False,
        )

    def _create_dma_ctrl_registers(self):
        DREQ_XIP_STREAM = 49
        DREQ_HSTX       = 52
        DREQ_FORCE      = 63

        self._dma_ctrl_cb_dispatcher = self._dma_dispatcher.pack_ctrl(
            ring_size   = 4,
            ring_sel    = True,
            bswap       = False,
        )
        self._dma_ctrl_hstx_commands = self._dma_executer.pack_ctrl(
            inc_write   = False,
            chain_to    = self._dma_dispatcher.channel,
            treq_sel    = DREQ_HSTX,
            bswap       = False,
        )
        self._dma_ctrl_hstx_pixels = self._dma_executer.pack_ctrl(
            size        = {1:0, 2:1, 4:2}[self._bytes_per_pixel],
            inc_write   = False,
            chain_to    = self._dma_dispatcher.channel,
            treq_sel    = DREQ_HSTX,
            bswap       = False,
        )
        self._dma_ctrl_cb_executer_nested_single = self._dma_executer.pack_ctrl(
            bswap       = False,
        )
        self._dma_ctrl_frame_marker = self._dma_executer.pack_ctrl(
            inc_read    = False,
            inc_write   = False,
            chain_to    = self._dma_dispatcher.channel,
            bswap       = False,
        )
        if self._buffer_is_in_psram or self._audio:
            self._dma_ctrl_cb_executer_nested_repeat = self._dma_executer.pack_ctrl(
                chain_to    = self._dma_dispatcher.channel,
                bswap       = False,
            )
        if self._audio:
            # notify channel: one 32-bit flag write, completion raises IRQ (if enabled)
            self._dma_ctrl_notify = self._dma_notify.pack_ctrl(
                inc_read    = False,
                inc_write   = False,
                irq_quiet   = False,
                bswap       = False,
            )
        if self._buffer_is_in_psram:
            self._dma_ctrl_streamer = self._dma_streamer.pack_ctrl(
                inc_read    = False,
                treq_sel    = DREQ_XIP_STREAM,
                bswap       = False,
            )

    def _create_control_blocks(self):
        num_cb = self._V_TOTAL_LINES          # one hblank block per line
        num_cb += self._V_ACTIVE_LINES        # pixel blocks
        num_cb += 1                           # frame marker
        num_cb += 1                           # restart
        if self._audio:
            num_cb += 2 * _ha.N_ISL           # island + tail block on even lines
            num_cb += 4                       # island quarter markers
        if self._buffer_is_in_psram:
            num_cb += 3
            num_cb += self._V_ACTIVE_LINES // self._height_scale
        num_cb *= 4

        gc.collect()
        self._control_blocks = bytearray(num_cb * 4)       # zeroed, no temp list
        self._cb_words = num_cb
        self._cb_addr = addressof(self._control_blocks)
        if rv_memory.is_in_external_ram(self._control_blocks):
            raise MemoryError("not enough space in SRAM for control block array (%d bytes); "
                              "allocate display.begin() earlier" % (num_cb * 4))

        self._create_hstx_commands()

        HSTX_FIFO = 0x50600000 + 0x4
        self._hstx_fifo = HSTX_FIFO
        self._cb_line_porch = array.array('I', [
            addressof(self._hstx_line_porch), HSTX_FIFO,
            len(self._hstx_line_porch), self._dma_ctrl_hstx_commands,
        ])
        self._cb_line_vsync = array.array('I', [
            addressof(self._hstx_line_vsync), HSTX_FIFO,
            len(self._hstx_line_vsync), self._dma_ctrl_hstx_commands,
        ])
        self._cb_line_active = array.array('I', [
            addressof(self._hstx_line_active), HSTX_FIFO,
            len(self._hstx_line_active), self._dma_ctrl_hstx_commands,
        ])
        self._cb_line_pixels = array.array('I', [
            addressof(self._buffer), HSTX_FIFO,
            self._width, self._dma_ctrl_hstx_pixels,
        ])
        self._cb_frame_marker = array.array('I', [
            addressof(self._frame_one), addressof(self._frame_flag),
            1, self._dma_ctrl_frame_marker,
        ])
        if self._audio:
            # executer writes a 4-word block into the notify channel's registers;
            # the notify channel then sets aflag[q] and completes (-> IRQ)
            self._notify_nested = [array.array('I', [
                addressof(self._frame_one), addressof(self._aflag) + 4 * q,
                1, self._dma_ctrl_notify]) for q in range(4)]
            self._cb_audio_q = [array.array('I', [
                addressof(self._notify_nested[q]), addressof(self._dma_notify.registers),
                4, self._dma_ctrl_cb_executer_nested_repeat]) for q in range(4)]

        self._cb_restart_frame_nested = array.array('I', [
            addressof(self._control_blocks), addressof(self._dma_executer.registers),
            4, self._dma_ctrl_cb_dispatcher,
        ])
        self._cb_restart_frame = array.array('I', [
            addressof(self._cb_restart_frame_nested), addressof(self._dma_dispatcher.registers),
            len(self._cb_restart_frame_nested), self._dma_ctrl_cb_executer_nested_single,
        ])

        if self._buffer_is_in_psram:
            STREAM_FIFO = 0x50500000
            self._cb_fill_row_buffer_nested = array.array('I', [
                STREAM_FIFO, addressof(self._row_buffer),
                self._bytes_per_row // 4, self._dma_ctrl_streamer,
            ])
            self._cb_fill_row_buffer = array.array('I', [
                addressof(self._cb_fill_row_buffer_nested), addressof(self._dma_streamer.registers),
                len(self._cb_fill_row_buffer_nested), self._dma_ctrl_cb_executer_nested_repeat,
            ])
            CHAN_ABORT = 0x50000000 + 0x464
            self._cb_streamer_abort_nested = array.array('I', [1 << self._dma_streamer.channel])
            self._cb_streamer_abort = array.array('I', [
                addressof(self._cb_streamer_abort_nested), CHAN_ABORT,
                len(self._cb_streamer_abort_nested), self._dma_ctrl_cb_executer_nested_repeat,
            ])
            STREAM_ADDR = 0x400C8000 + 0x14
            self._cb_xip_stream_start_nested = array.array('I', [
                addressof(self._buffer),
                self._height * self._width * self._bytes_per_pixel,
            ])
            self._cb_xip_stream_start = array.array('I', [
                addressof(self._cb_xip_stream_start_nested), STREAM_ADDR,
                len(self._cb_xip_stream_start_nested), self._dma_ctrl_cb_executer_nested_repeat,
            ])

    def _create_hstx_commands(self):
        fp, sw, bp, act = (self._H_FRONT_PORCH, self._H_SYNC_WIDTH,
                           self._H_BACK_PORCH, self._H_ACTIVE_PIXELS)
        R = _HSTX_CMD_RAW_REPEAT
        self._hstx_line_porch = array.array('I', [
            R | fp, _sync(1, 1),  R | sw, _sync(1, 0),  R | (bp + act), _sync(1, 1)])
        self._hstx_line_vsync = array.array('I', [
            R | fp, _sync(0, 1),  R | sw, _sync(0, 0),  R | (bp + act), _sync(0, 1)])
        if self._audio:
            # HDMI: video preamble + guard band precede every active line
            vpre = [R | (bp - self._VPRE_LEN), _sync(1, 1),
                    R | 8, _sync(1, 1, 1, 0),            # video preamble CTL=1000
                    R | 2, _VGB,
                    _HSTX_CMD_TMDS | act]
            self._hstx_line_active = array.array('I', [
                R | fp, _sync(1, 1),  R | sw, _sync(1, 0)] + vpre)
            # even lines: head + island(36, from slot) + tail
            rest = sw - self._PRE_LEN - self._ISLAND_LEN
            def head(v):
                return array.array('I', [R | fp, _sync(v, 1),
                                         R | self._PRE_LEN, _sync(v, 0, 1, 1),   # DI preamble CTL=1010
                                         _HSTX_CMD_RAW | self._ISLAND_LEN])
            self._hstx_head = (head(0), head(1))
            self._hstx_tail_act = array.array('I', [R | rest, _sync(1, 0)] + vpre)
            self._hstx_tail_blank = tuple(array.array('I', [
                R | rest, _sync(v, 0), R | (bp + act), _sync(v, 1)]) for v in (0, 1))
        else:
            self._hstx_line_active = array.array('I', [
                R | fp, _sync(1, 1),  R | sw, _sync(1, 0),  R | bp, _sync(1, 1),
                _HSTX_CMD_TMDS | act])

    def _assemble_control_blocks(self):
        self._cb_index = 0
        psram = self._buffer_is_in_psram
        vb = self._V_BLANK_LINES
        fifo = self._hstx_fifo
        cmd = self._dma_ctrl_hstx_commands
        slots = addressof(self._slots) if self._audio else 0
        isl_w = _ha.SLOT_W
        vs0 = self._V_FRONT_PORCH
        vs1 = vs0 + self._V_SYNC_WIDTH
        # marker q goes after the odd line that follows the last island of quarter q
        qb = _ha.Q_BOUNDS if self._audio else (0, 0, 0, 0, 0)
        mark = {2 * qb[q + 1] - 1: q for q in range(3)}    # quarter 3 -> frame end

        for L in range(self._V_TOTAL_LINES):
            active = L >= vb
            if L == vb and psram:
                # flush XIP FIFO, then start the stream for this frame
                self._add_control_block(self._cb_fill_row_buffer)
                self._add_control_block(self._cb_streamer_abort)
                self._add_control_block(self._cb_xip_stream_start)

            # horizontal blanking for this line
            if self._audio and not (L & 1):
                v = 0 if vs0 <= L < vs1 else 1
                hd = self._hstx_head[v]
                self._add_cb(addressof(hd), fifo, len(hd), cmd)
                j = L >> 1
                sl = _ha.VS_SLOT if j == _ha.VS_ISL else j % 132
                self._add_cb(slots + sl * isl_w * 4, fifo, isl_w, cmd)
                tl = self._hstx_tail_act if active else self._hstx_tail_blank[v]
                self._add_cb(addressof(tl), fifo, len(tl), cmd)
            elif active:
                self._add_control_block(self._cb_line_active)
            elif self._V_FRONT_PORCH <= L < self._V_FRONT_PORCH + self._V_SYNC_WIDTH:
                self._add_control_block(self._cb_line_vsync)
            else:
                self._add_control_block(self._cb_line_porch)

            if active:
                row = L - vb
                if not psram:
                    self._cb_line_pixels[0] = addressof(self._buffer) + \
                        (row // self._height_scale) * (self._width * self._bytes_per_pixel)
                elif (row % self._height_scale) == 0:
                    self._cb_line_pixels[0] = addressof(self._row_buffer)
                    self._add_control_block(self._cb_fill_row_buffer)
                self._add_control_block(self._cb_line_pixels)

            if self._audio and L in mark:
                self._add_control_block(self._cb_audio_q[mark[L]])

        if self._audio:
            self._add_control_block(self._cb_audio_q[3])
        self._add_control_block(self._cb_frame_marker)
        self._add_control_block(self._cb_restart_frame)
        if self._cb_index != self._cb_words:
            raise RuntimeError('control block count mismatch')

    def _add_cb(self, a, b, c, d):
        m = machine.mem32
        p = self._cb_addr + self._cb_index * 4
        m[p] = a
        m[p + 4] = b
        m[p + 8] = c
        m[p + 12] = d
        self._cb_index += 4

    def _add_control_block(self, block):
        self._add_cb(block[0], block[1], block[2], block[3])

    def _start(self):
        # restart block TRANS_COUNT = 4 (see v2 driver for the stop/restart trick)
        machine.mem32[self._cb_addr + (self._cb_words - 2) * 4] = 4
        self._dma_dispatcher.active(True)

    def _stop(self):
        machine.mem32[self._cb_addr + (self._cb_words - 2) * 4] = 3

    def frame_ready(self):
        return self._frame_flag[0] != 0

    def clear_frame_ready(self):
        self._frame_flag[0] = 0

    def wait_frame(self):
        self._frame_flag[0] = 0
        while self._frame_flag[0] == 0:
            pass

    def deinit(self):
        try:
            self._stop()
        except Exception:
            pass

        try:
            self._dma_notify.irq(None)
        except Exception:
            pass
        mask = 0
        for name in ("_dma_dispatcher", "_dma_executer", "_dma_streamer", "_dma_notify"):
            dma = getattr(self, name, None)
            if dma:
                try:
                    mask |= 1 << dma.channel
                except Exception:
                    pass
        if mask:
            machine.mem32[0x50000000 + 0x464] = mask   # DMA CHAN_ABORT

        try:
            self._hstx.csr(0)
        except Exception:
            pass

        for pin in (
            self._pin_clk_p, self._pin_clk_n,
            self._pin_d0_p, self._pin_d0_n,
            self._pin_d1_p, self._pin_d1_n,
            self._pin_d2_p, self._pin_d2_n,
        ):
            try:
                machine.Pin(pin, machine.Pin.IN)
            except Exception:
                pass

        for name in (
            "_control_blocks", "_row_buffer",
            "_hstx_line_porch", "_hstx_line_vsync", "_hstx_line_active",
            "_frame_flag", "_frame_one", "_cb_frame_marker",
            "_slots", "_aflag", "_cb_audio_q", "_hstx_head", "_hstx_tail_act", "_hstx_tail_blank",
            "_buffer", "_hstx",
            "_dma_dispatcher", "_dma_executer", "_dma_streamer", "_dma_notify", "_notify_nested",
        ):
            if hasattr(self, name):
                setattr(self, name, None)

        gc.collect()

    def close(self):
        self.deinit()
