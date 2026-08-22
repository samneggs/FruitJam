# audio_mixer2.py
# Self-contained streaming polyphonic SFX mixer for the Fruit Jam
# (RP2350 + TLV320DAC3100).
#
# Public interface (that's all your game needs):
#   m = Mixer()                         # instantiate: DAC + I2S up, streaming silence
#   fire  = m.load("/path/fire.wav")    # -> Sound (carries its own length)
#   drone = m.load("/path/drone.wav")
#   m.play(fire)                        # fire-and-forget one-shot
#   tok = m.play(drone, loop=True)      # looping bed -> returns a token
#   m.stop(tok)                         # stop that loop
#   m.deinit()                          # shut everything down
#
# SOURCE FORMAT: mono, signed 16-bit, matching the mixer's rate (default 22050).
#   ffmpeg -i in.wav -ar 22050 -ac 1 -acodec pcm_s16le out.wav
#
# Hot path (_fill_into) is a single Viper method: voice mix + clamp + stereo
# interleave + accumulator reset in one pass, no Python-level per-voice calls.

from machine import Pin, I2C, I2S
from micropython import const
import array

from TLV320 import TLV320DAC3100, INTERFACE_I2S

VOL_SHIFT = const(8)        # volume is Q8: 256 = unity
SAMPLE_MAX = const(32767)
SAMPLE_MIN = const(-32768)
U16_WRAP = const(65536)


def _load_wav_pcm(path):
    """Walk the RIFF chunks and return the raw 'data' bytes (mono 16-bit assumed)."""
    with open(path, "rb") as f:
        if f.read(12)[0:4] != b"RIFF":
            raise ValueError("not a RIFF/WAVE file: " + path)
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                raise ValueError("no data chunk in " + path)
            size = int.from_bytes(hdr[4:8], "little")
            if hdr[0:4] == b"data":
                return f.read(size)
            f.seek(size + (size & 1), 1)       # skip body, word-align (pad byte if odd)


class Sound:
    """A loaded sound asset. Holds the PCM and its length so play() needs neither."""
    def __init__(self, pcm):
        self.pcm = bytearray(pcm)              # mutable buffer: safe for ptr16 in Viper
        self.length = len(pcm) // 2            # samples = bytes / 2  (16-bit mono)


# ===========================================================================
class Mixer:
    def __init__(self, rate=22050, chunk=256, max_voices=8,
                 i2c_id=0, scl=21, sda=20,
                 i2s_id=0, sck=26, ws=27, sd=24,
                 rst=22, ibuf=10_000, dac_volume_db=-6, headphone=True):
        self._chunk = chunk
        self._max_voices = max_voices
        self._token = 0
        self._use_b = True

        # --- mixer buffers (allocated once) ---
        self.ACC   = array.array('i', [0] * chunk)     # int32 mono accumulator
        self.BUF_A = bytearray(chunk * 4)              # interleaved L,R int16
        self.BUF_B = bytearray(chunk * 4)

        # --- voice table ---
        self.V_DATA  = [None] * max_voices             # pcm bytearray per voice
        self.V_LEN   = array.array('i', [0] * max_voices)
        self.V_POS   = array.array('i', [0] * max_voices)
        self.V_VOL   = array.array('i', [0] * max_voices)
        self.V_TOKEN = array.array('i', [0] * max_voices)
        self.V_LOOP  = bytearray(max_voices)
        self.V_ACT   = bytearray(max_voices)

        # --- DAC bring-up (same sequence that worked for the drone) ---
        Pin(rst, Pin.OUT).value(0)
        Pin(rst, Pin.OUT).value(1)
        self._i2c = I2C(i2c_id, scl=Pin(scl), sda=Pin(sda), freq=400000)
        self._dac = TLV320DAC3100(self._i2c)
        self._dac.init(sample_rate=rate, bits=16, mode=INTERFACE_I2S, master=False)
        self._dac.power_on_dac()
        self._dac.set_dac_volume(dac_volume_db)
        if headphone:
            self._dac.enable_headphone()
        # self._dac.enable_speaker(gain=0)   # mono Class-D; L=R so it works too

        # --- start streaming immediately (silence until something plays) ---
        self._audio = I2S(i2s_id, sck=Pin(sck), ws=Pin(ws), sd=Pin(sd),
                          mode=I2S.TX, bits=16, format=I2S.STEREO,
                          rate=rate, ibuf=ibuf)
        self._audio.irq(self._i2s_cb)
        self._fill_into(self.BUF_A)
        self._audio.write(self.BUF_A)

    # ---- public API -------------------------------------------------------
    def load(self, path):
        """Load a WAV and return a Sound (length is computed and cached here)."""
        return Sound(_load_wav_pcm(path))

    def play(self, sound, vol=256, loop=False):
        """Trigger a sound. Returns a token (use it with stop()); 0 if all voices busy."""
        vi = int(self._find_free())            # Viper return wrapped in int()
        if vi < 0:
            return 0                           # no free voice -> dropped
        self.V_DATA[vi]  = sound.pcm
        self.V_LEN[vi]   = sound.length
        self.V_POS[vi]   = 0
        self.V_VOL[vi]   = vol
        self.V_LOOP[vi]  = 1 if loop else 0
        self._token += 1
        self.V_TOKEN[vi] = self._token
        self.V_ACT[vi]   = 1                   # publish last (irq-safe)
        return self._token

    def stop(self, token):
        """Stop the voice started by play() that returned `token` (no-op if it already ended)."""
        if token > 0:
            self._stop_token(token)

    def deinit(self):
        self.stop_all()
        try:
            self._audio.deinit()
        except Exception:
            pass
        try:
            self._dac.mute_dac()
        except Exception:
            pass

    # ---- Viper methods ----------------------------------------------------
    @micropython.viper
    def _find_free(self) -> int:
        """Return index of first free voice, -1 if none."""
        v_act = ptr8(self.V_ACT)
        nvoice = int(self._max_voices)
        vi = 0
        while vi < nvoice:
            if v_act[vi] == 0:
                return vi
            vi += 1
        return -1

    @micropython.viper
    def _stop_token(self, token: int):
        v_act = ptr8(self.V_ACT)
        v_tok = ptr32(self.V_TOKEN)
        nvoice = int(self._max_voices)
        vi = 0
        while vi < nvoice:
            if v_act[vi]:
                if v_tok[vi] == token:
                    v_act[vi] = 0
                    return
            vi += 1

    @micropython.viper
    def stop_all(self):
        """Silence every voice (e.g. on scene change)."""
        v_act = ptr8(self.V_ACT)
        nvoice = int(self._max_voices)
        vi = 0
        while vi < nvoice:
            v_act[vi] = 0
            vi += 1

    @micropython.viper
    def _fill_into(self, out: ptr16):
        """Mix all active voices into ACC, then clamp + duplicate to stereo out.
        One Viper pass: no per-voice Python calls, no allocation (soft-IRQ safe)."""
        acc    = ptr32(self.ACC)
        v_len  = ptr32(self.V_LEN)
        v_pos  = ptr32(self.V_POS)
        v_vol  = ptr32(self.V_VOL)
        v_act  = ptr8(self.V_ACT)
        v_loop = ptr8(self.V_LOOP)
        chunk  = int(self._chunk)
        nvoice = int(self._max_voices)

        vi = 0
        while vi < nvoice:
            if v_act[vi]:
                src: ptr16 = ptr16(self.V_DATA[int(vi)])   # object read -> pointer, once per voice
                length = v_len[vi]
                pos    = v_pos[vi]
                vol    = v_vol[vi]
                loop   = v_loop[vi]
                off = 0
                while off < chunk:
                    avail = length - pos
                    if avail <= 0:
                        if loop:
                            pos = 0                        # seamless wrap
                            avail = length
                        else:
                            v_act[vi] = 0                  # one-shot done
                            break
                    run = chunk - off
                    if run > avail:
                        run = avail
                    i = 0
                    while i < run:
                        sample = src[pos]
                        if sample > SAMPLE_MAX:
                            sample -= U16_WRAP             # ptr16 reads unsigned -> sign-extend
                        acc[off] = acc[off] + ((sample * vol) >> VOL_SHIFT)
                        off += 1
                        pos += 1
                        i += 1
                v_pos[vi] = pos
            vi += 1

        i = 0
        o = 0
        while i < chunk:
            sample = acc[i]
            if sample > SAMPLE_MAX:
                sample = SAMPLE_MAX
            elif sample < SAMPLE_MIN:
                sample = SAMPLE_MIN
            sample = sample & 0xffff
            out[o] = sample                                # left
            out[o + 1] = sample                            # right (mono duplicated)
            acc[i] = 0                                     # reset accumulator for next chunk
            i += 1
            o += 2

    # ---- IRQ callback (regular Python: just double-buffer bookkeeping) ----
    def _i2s_cb(self, s):
        buf = self.BUF_B if self._use_b else self.BUF_A
        self._fill_into(buf)
        s.write(buf)
        self._use_b = not self._use_b


# ===========================================================================
if __name__ == "__main__":
    
    dummy_sram = bytearray(300_000)
    dummy_psram = bytearray(2_000_000)
    import time
    m = Mixer()                                              # one line, board defaults
    #drone = m.load("/Starcastle/drone_mono.wav")
    #fire  = m.load("/Starcastle/fire_mono.wav")

    #drone_tok = m.play(drone, vol=160, loop=True)            # start the bed
    
    SND_GHOST1 = m.load('/Pacman/ghost_1_.wav')
   
    a = 0
    try:
        BG_HANDLE = m.play(SND_GHOST1, vol=200, loop=True)
        for loop in range(10000):
            a = loop // 101
        time.sleep_ms(10_000)
        
#         for _ in range(4):                                   # four 3-shot bursts
#             for _ in range(3):
#                 m.play(fire, vol=220)
#                 time.sleep_ms(130)                           # < fire length -> overlap
#             time.sleep_ms(1200)
#         m.stop(drone_tok)                                    # turn the loop off
#         time.sleep_ms(500)
    finally:
        m.deinit()
        print("done")