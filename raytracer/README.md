# Raytracer in 53K — Assembly required

A real-time raytraced arcade game for the RP2350 (Adafruit Fruit Jam), written as Thumb-2 assembly inside MicroPython. Inspired by Ballblazer on the Atari 8-bit.

- **Write-up:** https://samneggs.github.io/FruitJam/raytracer/
- **Video:** [YOUTUBE LINK]

![Twilight arena](screenshots/shot_twilight.jpg)

## What it is

- Every pixel at 160x120 RGB565 is raytraced, then doubled to 320x240 DVI. Runs at about 60 fps.
- The renderer and all of the game logic are two `@micropython.asm_thumb` functions:
  - `rt_main` runs on core 1 and never returns. It does the game update and renders the even rows.
  - `rt_rows` is called from Python on core 0 each frame. It renders the odd rows.
- 11,293 machine instructions, 42 KB of machine code, in a 53,194-byte `.mpy`.

## Running it

1. Copy `bb_rt.mpy` to the board.
2. Make sure these modules are on the board too:
   - `dvi_rp2_hstx_frame_sync_v2`, `gamepadfast` and `colors`, from [`drivers/`](../drivers)
   - `draw_numberdvi`, for the HUD digits
3. Run it:

```python
import bb_rt
bb_rt.main()
```

Use the `.mpy`, not `bb_rt.py`. Compiling the 380 KB source on the device fragments SRAM, so the frame buffers can land in PSRAM and the display tears. The game prints a warning at startup if any buffer is outside SRAM.

**Controls:**

| Input | Action |
|---|---|
| Stick | Steer and throttle |
| LEFT / RIGHT | Strafe |
| UP | Shoot |
| DOWN | Next palette |

## Files

| Path | Contents |
|---|---|
| `bb_rt.mpy` | The game. This is the file to run. |
| `bb_rt.py` | Generated source, for reading. |
| `index.html` | The write-up page, served by GitHub Pages at the link above. |
| `generator/` | Python build script that writes `bb_rt.py` and compiles it to `bb_rt.mpy`. |
| `screenshots/` | Emulator frames. |

## Building from source

```
cd generator
python3 gen_rt.py            # writes bb_rt.py and bb_rt.mpy
```

Requirements:

- `mpy-cross` on your PATH. It compiles with `-march=armv7emsp`.
- A Thumb-2 encoder script for the instructions MicroPython's assembler lacks. The build looks for `encode.py` next to `gen_rt.py`; set `ENCODER=/path/to/encode.py` to use one elsewhere. The encoder is not included here.

If your firmware rejects the wide conditional branches (`beq_w` and similar), use `python3 gen_rt.py --no-wide`.

Gameplay and rendering constants are in `bb_rt_header.py`, including the palettes and the `WIDE_PIXELS` switch for 16:9 monitors.
