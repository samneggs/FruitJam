# RP2350 (Cortex-M33 @ 246 MHz) measured timing model
Measured 2026-10-29 with DWT CYCCNT, IRQs off, code in SRAM. Raw data: timing_table.json, results.jsonl.

| Rule | Cost |
|---|---|
| vadd/vsub/vmul/vcmp issue | 1 |
| FP result consumed by the *next* instruction | +1 stall (one instruction between hides it) |
| vmov core->s, then FP use | no stall |
| vmov s->core after FP write, or core use right after vmov s->core | +1 stall each |
| vmrs APSR_nzcv, FPSCR | 2, always (not a dependency stall, gaps don't help) |
| vmrs -> conditional branch | no stall |
| taken branch | 2; **+1 if the target is a 32-bit instr that isn't word-aligned** -> put `align(4)` before loop labels |
| not-taken branch | 1 |
| some 16-bit pairs (e.g. nop) dual-issue | 0.5 each |

Mandelbrot inner iteration (bit-exact vs float32 ref):
v0 naive 19 (+1 misaligned = 20) -> v1 rescheduled 15 -> v2 integer escape test (vmov+cmp vs 0x40800000 instead of vcmp+vmrs) 14.
Image kernel adds ~27 cycles/pixel (setup + partial escape iteration).

## Memory system (scanout = 640x480 RGB332 byte-DMA from striped SRAM, live)
SRAM baseline: ldr->use 2 cycles; back-to-back ldr/str ~1.4; APB peripheral 4.
Scanout DMA contention: +0.05-0.15 cycles per back-to-back access, latency unaffected. Ignore it.

XIP (cycles added per access over SRAM; cache = 16 KB, 8-byte lines):
| pattern | PSRAM | flash |
|---|---|---|
| cache hit | +2 | +2-5 |
| miss, next sequential line | +36/line | +67 |
| miss, non-sequential line | +69 | +117 |
| store miss (fill + writeback) | +122/line | - |
| uncached 0x14/0x15 alias, sequential words | +19.5 | +35 |
| uncached, non-sequential | +53 | +86 |
| uncached store | +16 seq / +37 | - |
-> streaming PSRAM writes (clears, render targets): uncached alias ~6x cheaper than cached (mind stale cache lines).

SRAM banks: 0x2000xxxx region is 4-way word-striped (same bank every 16 bytes). Scratch X 0x20080000 = SRAM8.
Core1 hammering the SAME bank: core0 back-to-back ld/st 1.375 -> ~1.98 (+44%); dependent loads unaffected.
Different bank: no effect. Core1 XIP traffic: no effect on core0 SRAM data.

**asm/viper code placed in PSRAM**: fine alone (1.40), but when the other core streams XIP the
code is evicted from the shared cache: 9.8x (PSRAM stream) / 16x (flash stream) slower.
Heap is nearly full with a 300 KB SRAM framebuffer, so code compiled late can land in PSRAM silently.
Check: `hex(machine.mem32[id(fn) + 8])` (fun_data of an asm_thumb function) must be 0x2xxxxxxx.

## FP load/store (measured 2026-10-29, aligned loops)
vldr: 1 cycle, **no load-use stall** (unlike ldr's 2). vstr: 1; vstr of an FP result produced by the
previous instruction +1 (one instr between hides it). vstr -> vldr (same or other address): no stall.
Up to 4 16-bit `add rX, #imm` between FP ops cost ~1 cycle total (dual issue).

## Allocation traps (MicroPython, 8 MB PSRAM heap)
- GC only runs when an allocation fails in *every* heap area. With PSRAM available it almost never
  fails, so garbage fragments SRAM and later big buffers land in PSRAM silently -> kernels 4-9x slower.
  Allocate big buffers first, assert `uctypes.addressof(b) >= 0x20000000`.
- `array('f', bytearray(n))` makes a full temporary copy; use bytearrays + a uctypes FLOAT32 view.
- Display driver (dvi_rp2_hstx_audio, 320x240) needs ~16 KB contiguous SRAM: call begin() right after
  allocating the fb. Afterwards ~307 KB SRAM is left, but only 5 x 48 KB contiguous blocks.
- Data in upper SRAM (0x2004xxxx+) ran divergence at the model's 16.1 cyc/cell; in lower SRAM (where
  code/interp live) 17.2; inside the full bench up to 19.5. Not bank offset, not data values, not DMA.
  Open question. Code in PSRAM vs SRAM: <=1 cycle difference.

## Fluid kernels v0a (128x96, bit-exact vs float32 ref), cycles per cell
advect 56, divergence 16-19 (v1a), pressure GS 21.4/iter, project 21.6, colorize 16.6, expand 2.1/px.
App: 32.6 fps with 12 pressure iterations; Python splats cost 6.5 ms/frame.

## Fluid round 2 (2026-10-29): 32.6 -> 62.7 fps
| step | change | before -> after (ms/frame) |
|---|---|---|
| splats | Python -> asm kernel (47 cyc/cell, 486 cyc call overhead) | 6.5 -> 1.3 (rest is Python math/pack_into) |
| pressure 12 it | lexicographic GS -> red-black, 2 cores + spin barrier w/ dmb | 12.5 -> 6.2-6.8 (1 core RB 11.7) |
| advection | fused u+v (one back-trace, 62 cyc/cell vs 2x56) + rows split over 2 cores | 7.8 -> 3.6 |
Red-black and fused advect are bit-exact vs the float32 host reference.

Traps found:
- rp2 has **no GIL**: a core1 thread reading module globals while core0 defines new ones can get a
  spurious NameError (dict resize). Pass everything to the thread as arguments; start it last.
- A core stuck in an asm spin can't be interrupted by Ctrl-C and USB dies with it -> needs a physical
  reset. Every cross-core spin gets a timeout.
- Allocating buffers in a separate first exec (clean SRAM) pushes the big script's compiled asm into
  PSRAM. Then core1's interpreter traffic on the shared XIP cache halves core0's kernel speed.
  Fix: reserve an SRAM CODE buffer in stage 1 and copy the kernels into it (fun_data at id(fn)+8).
- MicroPython float literal parsing isn't correctly rounded: use exactly representable test constants.
- Timing tiny kernels via Python call: asm call overhead ~486 cycles; one harness case showed 31k (artefact).

## Fluid round 3 (2026-10-29): 62.7 -> 78.0 fps (predicted 78)
- Kernels precompiled: fluid_k_build.py -> fluid_k.mpy (15 KB source -> 2 KB, mpy-cross 1.27.0.post2,
  -march=armv7emsp, board mpy v6.3 arch 7). Imported first after soft reset -> code in SRAM, no relocation.
- core1 runs `worker`, one asm function that never returns: wfe until core0 bumps JOB seq + sev,
  then blx to the kernel's fun_data|1 with a0..a3, dmb, done = seq, sev. No Python on core1 at all.
- Every stage split by rows (top 47 / bottom 47), half-height kernels called with shifted bases.
- Frame 12.55 ms: splat 1.40 (Python) adv uv 1.56 dye 1.37 div 0.73 press(12 it) 5.97 proj 0.59 render 0.93.
- SRAM limit: 128x96 float32 x 5 fields is the largest grid that fits with a 320x240 fb + display.

## Float16 fields, 160x120 (2026-10-29)
- Five f16 fields (38.4 KB) + f32 pressure (76.8 KB, warm start) fit; 128x96 f32 was the f32 limit.
- No f16 load on the M33: (unaligned) ldr of 2 cells -> vmov -> vcvtb/vcvtt.f32.f16; store via
  vcvtb.f16.f32 (writes the bottom half only) -> vmov -> strh. Unaligned ldr from SRAM works.
- Python struct 'e' gives the bit-exact reference for VCVTB/VCVTT (round to nearest even).
- Cost per cell f16 vs f32: advect u+v 78.7 vs 62.1, dye 61.6 vs 57.6, divergence 28.8 vs ~17,
  pressure (f32 p, f16 div) 22.7 vs 19.8, project 32 vs 21.6. All 9 kernels bit-exact first time.
- App: 48.1 fps (2x2 block render) / 46.4 fps (bilinear) with 12 pressure iterations; pressure 10.4 ms
  is half the frame. Every stage within ~5% of single-kernel prediction.
- Bilinear render: uhadd8 does 4 byte averages per instruction; palette lookup per pixel =
  uxtb(ror) + ldrb[reg] + orr. Predicted 4 cyc/px, measured 4.99 -> 4.70 after reordering.
  Cause: **back-to-back loads cost ~1.375 each** (already in the table; forgot to apply it).
  Rejected on the way: code-fetch/data sharing of the SRAM port (PSRAM code was slower: 5.01).
- mpy-cross catches "branch not in range": row loops with long bodies need bge/b pairs.

## Advection tightening (2026-10-30), 160x120 f16, pressure default 12 -> 6 iterations
- Pressure is linear at 0.87 ms/iteration (2 cores). 6 iterations with warm start look the same: 46.4 -> 61.1 fps.
- Both advection kernels already ran at ~1 instruction/cycle, so reordering alone barely helped
  (u+v 79.8 -> 79.3; dye 63.4 -> 58.1, where a serial FP tail had real stalls to fill).
  The gains came from removing instructions:
  - index: `mla r7, r7, lr, r6` + `add.w r7, r3, r7, lsl #1` (lr = 160 via movw; asm_thumb's prologue
    saves lr, so it's a free register) replaces lsl/add.w/add/lsl/add.
  - register-offset `ldrh.w r7, [r1, r10]`, post-indexed `strh.w r6, [r0], #2`, `strh.w r8, [r0, r11]`.
  - result: u+v 73.2, dye 55.1 cyc/cell, both bit-exact. App 61.1 -> 63.2 fps bilinear, 66.3 block.
- `bne_w(label)` is accepted by MicroPython's asm_thumb (emits bne.w T3); use it instead of beq/b pairs.
  Taken bne.w costs the same as not-taken beq + taken b here (no gain, no loss).

## HDMI audio + FM synth (2026-10-30), fluid_fm.py
- v1 driver dvi_rp2_hstx_audio at 246 MHz / HSTX div 2: ACR derived from the real pixel clock (N 4096, CTS 24600), 32 kHz.
- SRAM is the constraint (need 389 KB of 426 KB free). hdmi_audio import leaves 370 + 37 KB blocks:
  control blocks (24.6 KB) first into the 37 KB hole, then one arena (fb, pressure, 5 fields, audio slots,
  synth buffers), then the video driver. Driver's bytearray() redirected to arena slices during begin().
- The fields rely on zero edge cells (kernels write the interior only): reusing a field as scratch needs the
  edges re-zeroed, else NaN velocity.
- DMA_IRQ_0 was enabled on core0's NVIC too: both cores ran the hard IRQ handler. Clear it on core0 (NVIC_ICER0).
- Audio IRQ on core1, 4x per frame: island encoding 235 us + synth/glue ~160 us. Synth buffers in PSRAM made
  it 533 us and slowed core0's Python (shared XIP cache): 50.6 -> 52.9 fps after moving them to SRAM.
- fm_render 2-op FM: ~42 cyc/sample/voice incl. call, bit-exact vs Python model (fm_bench.py).
- Row shift toward core0 made it slower: the loss is XIP-cache contention, not core1 load.

## Core0 Python trimmed + spectrum strip (fluid_fm.py)
- Measured per frame with audio: jets 1.3 ms (float math + struct packing), gamepad 0.6 ms average
  (2.4 ms every 4th frame: the driver sleeps 2 x 500 us for the ADC), synth mapping 0.9 ms.
- Jets: splat params for the whole orbit precomputed (419 steps, JTAB in PSRAM), 3 field pointers written per frame.
  Advect params: floats packed once, pointers via mem32. Gamepad: one I2C step per frame (gpio + start x / read x + start y /
  read y), never sleeps. Synth: fm_map kernel maps all 4 drones from the stats_q sums; tune() only on chord/preset/note change.
  Result 51.9 -> 59.6 fps (splat 1.92 -> 0.54, synth 0.92 -> 0.22 ms).
- spec kernel (core1): draws the last bars, Hann-windowed mono from the HDMI ring (last 1024 samples), bit-reversed
  via rbit, 1024-point float DIT FFT with twiddles from the synth sine table, 64 log bars (60 Hz - 8 kHz), log2 from the
  float bits. FFT rel err 9e-5 vs numpy, bars exact (spec_bench.py). ~0.95 ms. SRAM is full, so the 8 KB FFT scratch lives
  in the free old-dye field (offset 20480) and the edge cells are re-zeroed afterwards. Job overlaps the next frame's
  jets/pad/synth: core0 waits ~0.1 ms. 58.5 fps with the spectrum.
- Tearing: drawing bars after the render left bare-fluid rows visible when the render overtook the scanline (black lines
  across the bars). Bars drawn per row pair in bilin_h still showed a few. Final: strip row pairs are rendered into a
  640 B line buffer, the bars overlaid there, then copied to the fb: no visible lines (user confirmed). +~13 us on core1.
