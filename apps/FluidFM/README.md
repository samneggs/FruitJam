# Fluid FM

A real-time fluid simulation driven by an FM synthesizer, on an RP2350 in MicroPython. Coloured smoke and music go out over one HDMI cable, picture and sound together, at 62 frames per second.

- 160 × 120 "stable fluids" simulation (float16 fields, red-black SOR pressure solve), two orbiting jets
- 10-voice two-operator FM synthesizer, 32 kHz stereo, playing MIDI files; audio sent as HDMI data islands
- 64-band spectrum analyser; bass pushes the jets, treble adds dye, every note fires a jet
- Both Cortex-M33 cores at 246 MHz; every per-cell and per-sample loop is hand-written Thumb-2 assembly

`docs/fluid-fm-explained.html` explains how it works at beginner, intermediate and expert level (open it in a browser).

## Running it

Hardware: an RP2350 board with 8 MB PSRAM and HSTX wired to an HDMI/DVI connector (developed on a SparkFun IoT RedBoard RP2350), MicroPython 1.27. A gamepad (`gamepadfast`, I2C) is optional.

1. Copy the drivers to the board's root or `/lib`: `drivers/dvi_rp2_hstx_audio.py`, `drivers/hdmi_audio.py`, `drivers/colors.py`, `drivers/gamepadfast.py`. Precompiling the two HDMI drivers with `mpy-cross -march=armv7emsp` keeps more SRAM free.
2. Copy everything in `board/` to a `/FluidFM` folder on the board, including `board/tunes/`.
3. Soft-reset the board (Ctrl-D), then run `/FluidFM/fluidfm.py` (in Thonny: open it and press Run; at the REPL: `exec(open('/FluidFM/fluidfm.py').read())`).

Start-up takes about 18 seconds while the palettes are built and the tunes are loaded. Run it right after a soft reset: it needs about 365 KB of contiguous SRAM.

Gamepad: START cycles tune + bars / tune / drones + bars / drones / silent; up/down changes colours; left/right changes tune or drone timbre; the stick steers your own jet; hold SELECT about a second to exit.

## Building from source

The `src/` tools run on a Linux host (developed on a Raspberry Pi 5) with Python 3, `pyserial` and `mpy-cross` 1.27 on the path, talking to the board's raw REPL over USB (`rp.py` uses the first `/dev/ttyACM*`).

| File | Purpose |
|---|---|
| `fluid_fm.py` | The program. `python3 fluid_fm.py 60` runs it for 60 s from the host; `--install` writes the standalone files into `/FluidFM` on the board; `--build DIR` writes them to a local folder (what `board/` contains). `--omega=1.7` sets the SOR factor, the second number the pressure iterations. |
| `fluid16_gen.py` | Generates the assembly kernels and builds `fluid16_k.mpy`; `--upload` copies it to the board. |
| `enc_cache.json` | Raw Thumb-2 encodings for instructions MicroPython's assembler lacks, so building needs no encoder. |
| `fluid16_k.py` | The generated kernel source, for reading. |
| `fluid_k_build.py`, `fluid_gen.py` | The earlier float32 version's generator; `fluid16_gen.py` takes the core1 worker from it. |
| `fmidi.py` | Standard MIDI file loader (runs on the board and the host). |
| `make_tunes.py` | Writes the four MIDI files in `board/tunes/` from note lists; `--upload` copies them to the board. |
| `fluid16_bench.py` | Runs each kernel on the board and checks it bit-exact against a Python float32/float16 reference, with cycle counts. |
| `fluid16_run.py` | The fluid alone, without audio. |
| `spec_bench.py`, `fm_bench.py` | Bit-exact checks of the spectrum and synth kernels. |
| `rp.py` | Minimal raw-REPL runner and file uploader. |
| `MODEL.md` | Measured Cortex-M33 timing rules used while scheduling the kernels. |

## Music

The tunes are arrangements written in `make_tunes.py`: J.S. Bach, Prelude in C major (BWV 846); J. Pachelbel, Canon in D; G. Holst, from *The Planets*; and "Fluid Drive", an original groove. Any standard MIDI files placed in `/FluidFM/tunes` are played in name order.
