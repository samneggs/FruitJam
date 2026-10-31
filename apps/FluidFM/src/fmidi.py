# Standard MIDI file -> event rows for the FM synth sequencer in fluid_fm.py (runs on the board and on CPython).
# Everything slow happens here, at load time: tempo map -> sample times, GM program -> FM instrument,
# and voice allocation, so the audio IRQ only copies rows into the voice state.
# Row (8 x int32): time (samples), voice, carrier inc (0 = release), modulator inc, FM index, amplitude
# (-1 = release), decay shift (0 = sustain; per IRQ call t -= t >> shift), info = note | kind << 8 | vel << 16 | ch << 24
import array

# name: (modulator ratio, modulation index, decay shift, volume)
INSTR = {'piano': (1.0, 2.2, 7, 1.0), 'bell': (3.5, 2.5, 8, 0.8), 'organ': (2.0, 1.2, 0, 0.7),
         'pluck': (1.0, 2.8, 6, 1.0), 'bass': (1.0, 1.8, 7, 1.2), 'strings': (1.0, 1.3, 0, 0.75),
         'brass': (1.0, 3.0, 0, 0.8), 'lead': (2.0, 2.0, 0, 0.8), 'pad': (1.0, 1.0, 0, 0.6)}
GM = ((0, 'piano'), (8, 'bell'), (16, 'organ'), (24, 'pluck'), (32, 'bass'), (40, 'strings'), (56, 'brass'),
      (80, 'lead'), (88, 'pad'), (96, 'bell'), (104, 'pluck'), (112, 'bell'), (120, 'piano'))
# channel 10 notes -> kind, frequency, ratio, index, decay shift, volume   (kind: 1 kick 2 snare 3 hat 4 crash 5 tom)
DRUMS = {35: (1, 50, 1.0, 3.0, 4, 1.4), 36: (1, 55, 1.0, 3.0, 4, 1.4), 38: (2, 190, 3.53, 6.0, 4, 0.9),
         40: (2, 200, 3.53, 6.0, 4, 0.9), 42: (3, 4800, 1.43, 8.0, 2, 0.3), 44: (3, 4800, 1.43, 8.0, 2, 0.3),
         46: (3, 4800, 1.43, 8.0, 4, 0.3), 49: (4, 3200, 1.43, 10.0, 7, 0.4), 57: (4, 3000, 1.43, 10.0, 7, 0.4),
         51: (3, 3600, 1.43, 6.0, 5, 0.3)}
TOMS = (41, 43, 45, 47, 48, 50)
RELEASE = 3                                 # decay shift after note-off (~33 ms)
LOW_F, LOW_BOOST = 250.0, 2.5               # notes below LOW_F get up to +LOW_BOOST FM index: small speakers can't
                                            # play the fundamental, but its harmonics carry the pitch
VOL = 5200                                  # amplitude of one voice at velocity 127
BETA = 2 ** 17 / 6.283185307                # fm_render index units per radian

def _instr(prog):
    name = 'piano'
    for p, n in GM:
        if prog >= p: name = n
    return INSTR[name]

def _vlq(d, i):
    v = 0
    while True:
        c = d[i]; i += 1
        v = (v << 7) | (c & 0x7f)
        if not c & 0x80: return v, i

def events(path):
    """-> division, [(tick, order, status, a, b)] merged over all tracks (order: offs before ons at one tick)."""
    with open(path, 'rb') as f:
        d = f.read()
    assert d[:4] == b'MThd', 'not a MIDI file'
    ntrk = int.from_bytes(d[10:12], 'big'); div = int.from_bytes(d[12:14], 'big')
    assert not div & 0x8000, 'SMPTE timing not supported'
    i = 8 + int.from_bytes(d[4:8], 'big'); ev = []
    for _ in range(ntrk):
        assert d[i:i + 4] == b'MTrk'
        end = i + 8 + int.from_bytes(d[i + 4:i + 8], 'big'); i += 8; t = 0; st = 0
        while i < end:
            dt, i = _vlq(d, i); t += dt
            c = d[i]
            if c == 0xff:                   # meta: only tempo matters
                typ = d[i + 1]; n, i = _vlq(d, i + 2)
                if typ == 0x51: ev.append((t, 0, 0x51, int.from_bytes(d[i:i + 3], 'big'), 0))
                i += n; continue
            if c in (0xf0, 0xf7):
                n, i = _vlq(d, i + 1); i += n; continue
            if c & 0x80: st = c; i += 1     # else running status
            k = st & 0xf0
            if k in (0xc0, 0xd0):
                a = d[i]; i += 1; b = 0
            else:
                a, b = d[i], d[i + 1]; i += 2
            if k == 0x90 and b == 0: k = 0x80
            if k in (0x80, 0x90, 0xc0):
                ev.append((t, 1 if k == 0x90 else 0, k | (st & 15), a, b))
        i = end
    ev.sort(key=lambda e: (e[0], e[1]))
    return div, ev

def load(path, rate, nv):
    """-> (rows array('i'), row count, end time in samples)"""
    div, ev = events(path)
    inc = 2 ** 32 / rate
    tempo = 500000; last = 0; now = 0.0
    prog = [0] * 16
    held = [None] * nv                      # (ch, note) per voice, or None
    since = [0] * nv                        # when the voice was last started / released
    out = []
    def row(*r): out.extend(r)
    for t, _, st, a, b in ev:
        now += (t - last) * tempo * rate / (div * 1e6); last = t
        T = int(now); k = st & 0xf0; ch = st & 15
        if st == 0x51: tempo = a; continue
        if k == 0xc0: prog[ch] = a; continue
        if k == 0x80:
            for v in range(nv):
                if held[v] == (ch, a):
                    held[v] = None; since[v] = T
                    row(T, v, 0, 0, 0, -1, RELEASE, 0)
                    break
            continue
        # note on
        if ch == 9:
            if a in DRUMS: kind, f, ratio, beta, dec, vol = DRUMS[a]
            elif a in TOMS: kind, f, ratio, beta, dec, vol = 5, 70 * 2 ** ((a - 41) / 12), 1.0, 2.0, 5, 1.0
            else: continue
        else:
            kind = 0; ratio, beta, dec, vol = _instr(prog[ch]); f = 440 * 2 ** ((a - 69) / 12)
            if f < LOW_F: beta += LOW_BOOST * (1 - f / LOW_F)
        for v in range(nv):                 # same note still sounding on this channel: retrigger that voice
            if held[v] == (ch, a): break
        else:
            free = [v for v in range(nv) if held[v] is None]
            pool = free if free else range(nv)
            v = min(pool, key=lambda j: since[j])  # longest released, else oldest sounding (steal)
        held[v] = None if ch == 9 else (ch, a); since[v] = T
        g = b / 127
        mi = min(int(f * ratio * inc), 0x7fffffff)
        row(T, v, int(f * inc), mi, int(beta * BETA * (0.55 + 0.45 * g)), int(VOL * vol * g), dec,
            a | kind << 8 | b << 16 | ch << 24)
    n = len(out) // 8
    end = out[-8] if n else 0
    return array.array('i', out), n, end
