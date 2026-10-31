#!/usr/bin/env python3
"""Writes the demo tunes as standard MIDI files (format 1) into tunes/, and with --upload copies them to /tunes on
the board for fluid_fm.py. Public-domain pieces arranged here, plus one original groove.
usage: make_tunes.py [--upload]"""
import os, sys, struct

DIV = 480                                   # ticks per quarter note
NOTE = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}

def m(name):                                # 'C#4' / 'Bb3' -> MIDI note number
    n = NOTE[name[0]]; i = 1
    while name[i] in '#b':
        n += 1 if name[i] == '#' else -1; i += 1
    return n + 12 * (int(name[i:]) + 1)

class Song:
    def __init__(self):
        self.ev = {}                        # track -> [(tick, order, bytes)]
    def add(self, trk, tick, data, order=1):
        self.ev.setdefault(trk, []).append((int(tick), order, bytes(data)))
    def tempo(self, tick, bpm):
        us = int(60e6 / bpm)
        self.add(0, tick, b'\xff\x51\x03' + us.to_bytes(3, 'big'), 0)
    def program(self, trk, ch, prog):
        self.add(trk, 0, [0xC0 | ch, prog], 0)
    def note(self, trk, ch, tick, note, dur, vel=90):
        if isinstance(note, str): note = m(note)
        self.add(trk, tick, [0x90 | ch, note, vel], 2)
        self.add(trk, tick + dur, [0x80 | ch, note, 0], 0)   # offs before ons at the same tick
    def write(self, path):
        def vlq(x):
            b = [x & 0x7f]; x >>= 7
            while x: b.append(0x80 | (x & 0x7f)); x >>= 7
            return bytes(reversed(b))
        trks = []
        for t in sorted(self.ev):
            out = bytearray(); last = 0
            for tick, _, data in sorted(self.ev[t], key=lambda e: (e[0], e[1])):
                out += vlq(tick - last) + data; last = tick
            out += b'\x00\xff\x2f\x00'
            trks.append(b'MTrk' + struct.pack('>I', len(out)) + out)
        with open(path, 'wb') as f:
            f.write(b'MThd' + struct.pack('>IHHH', 6, 1, len(trks), DIV) + b''.join(trks))

S16, S8, Q = DIV // 4, DIV // 2, DIV

# ---------------------------------------------------------------- J.S. Bach: Prelude in C, BWV 846
PRELUDE = ('C4 E4 G4 C5 E5', 'C4 D4 A4 D5 F5', 'B3 D4 G4 D5 F5', 'C4 E4 G4 C5 E5', 'C4 E4 A4 E5 A5', 'C4 D4 F#4 A4 D5',
           'B3 D4 G4 D5 G5', 'B3 C4 E4 G4 C5', 'A3 C4 E4 G4 C5', 'D3 A3 D4 F#4 C5', 'G3 B3 D4 G4 B4', 'G3 Bb3 E4 G4 C#5',
           'F3 A3 D4 A4 D5', 'F3 Ab3 D4 F4 B4', 'E3 G3 C4 G4 C5', 'E3 F3 A3 C4 F4', 'D3 F3 A3 C4 F4', 'G2 D3 G3 B3 F4',
           'C3 E3 G3 C4 E4', 'C3 G3 Bb3 C4 E4', 'F2 F3 A3 C4 E4', 'F#2 C3 A3 C4 Eb4', 'Ab2 F3 B3 C4 D4', 'G2 F3 G3 B3 D4',
           'G2 E3 G3 C4 E4', 'G2 D3 G3 C4 F4', 'G2 D3 G3 B3 F4', 'G2 Eb3 A3 C4 F#4', 'G2 E3 G3 C4 G4', 'G2 D3 G3 C4 F4',
           'G2 D3 G3 B3 F4', 'C2 C3 G3 Bb3 E4')
PRELUDE_END = (('C2 C3', 'F3 A3 C4 F4 C4 A3 C4 A3 F3 A3 F3 D3 F3 D3'),
               ('C2 B2', 'G4 B4 D5 F5 D5 B4 D5 B4 G4 B4 D4 F4 E4 D4'))

def prelude():
    s = Song(); s.program(1, 0, 0)
    s.tempo(0, 70)
    for b, ch in enumerate(PRELUDE):
        n = ch.split()
        for h in (0, 1):
            t = (2 * b + h) * 8 * S16
            v = 72 + (8 if h == 0 else 0)
            s.note(1, 0, t, n[0], 8 * S16, v + 8)
            s.note(1, 0, t + S16, n[1], 7 * S16, v)
            for k, x in enumerate(n[2:] + n[2:]):
                s.note(1, 0, t + (2 + k) * S16, x, (3 if k < 3 else 6 - k) * S16, v - 4 + (6 if k in (0, 3) else 0))
    for i, (bass, run) in enumerate(PRELUDE_END):
        t = (len(PRELUDE) + i) * 16 * S16
        b0, b1 = bass.split()
        s.note(1, 0, t, b0, 16 * S16, 84); s.note(1, 0, t + S16, b1, 15 * S16, 74)
        for k, x in enumerate(run.split()):
            s.note(1, 0, t + (2 + k) * S16, x, S16 * 2, 70)
    t = (len(PRELUDE) + 2) * 16 * S16
    s.tempo(t - 8 * S16, 60); s.tempo(t - 4 * S16, 50)                          # ritardando into the last chord
    for x in 'C2 C3 E4 G4 C5'.split():
        s.note(1, 0, t, x, 16 * S16, 80)
    return s

# ---------------------------------------------------------------- J. Pachelbel: Canon in D (canon over the ground bass)
GROUND = 'D3 A2 B2 F#2 G2 D2 G2 A2'.split()
CHORDS = ('D F# A', 'A C# E', 'B D F#', 'F# A C#', 'G B D', 'D F# A', 'G B D', 'A C# E')

def canon_melody():
    """Pachelbel's own violin variations, one 2-bar cycle (8 beats over the ground bass) each; every violin of the
    canon plays this list, entering 2 bars after the previous one."""
    def seq(notes, dur): return [(i * dur, x, dur) for i, x in enumerate(notes.split())]
    return [seq('F#5 E5 D5 C#5 B4 A4 B4 C#5', Q),
            seq('D5 C#5 B4 A4 G4 F#4 G4 E4', Q),
            seq('D4 F#4 A4 G4 F#4 D4 F#4 E4 D4 B3 D4 A4 G4 B4 A4 G4', S8),
            seq('F#4 D4 E4 C#5 D5 F#5 A5 A4 B4 G4 A4 F#4 D4 D5 D5 C#5', S8),
            seq('D5 C#5 D5 D4 C#4 A4 E4 F#4 D4 D5 C#5 B4 C#5 F#5 A5 B5 G5 F#5 E5 G5 F#5 E5 D5 C#5 B4 A4 G4 F#4 E4 G4 F#4 E4', S16),
            seq('D4 E4 F#4 G4 A4 E4 A4 G4 F#4 B4 A4 G4 A4 G4 F#4 E4 D4 B3 B4 C#5 D5 C#5 B4 A4 G4 F#4 E4 B4 A4 B4 A4 G4', S16),
            seq('F#5 E5 D5 C#5 B4 A4 B4 C#5', Q),
            seq('D5 C#5 B4 A4 G4 F#4 G4 E4', Q)]

def canon():
    s = Song(); s.tempo(0, 66)
    s.program(1, 0, 42); s.program(2, 1, 40); s.program(3, 2, 40); s.program(4, 3, 40)    # cello, 3 violins
    var = canon_melody()
    cycles = len(var) + 3
    cyc = 8 * Q
    for c in range(cycles):
        for i, x in enumerate(GROUND):
            s.note(1, 0, c * cyc + i * Q, x, Q - 10, 92)
    for vi, (trk, ch, vel) in enumerate(((2, 1, 82), (3, 2, 76), (4, 3, 72))):
        for k, v in enumerate(var):
            base = (k + 1 + vi) * cyc
            for t, x, d in v:
                if isinstance(x, str): x = m(x)
                s.note(trk, ch, base + t, x, d - 8, vel)
    end = cycles * cyc
    s.tempo(end - 4 * Q, 54)
    for x in ('D2', 'D3', 'A3', 'F#4', 'D5'):
        s.note(1, 0, end, x, 8 * Q, 86)
    return s

# ---------------------------------------------------------------- "Fluid Drive" (original): synth groove with drums
def groove():
    s = Song(); s.tempo(0, 118)
    s.program(1, 0, 38); s.program(2, 1, 89); s.program(3, 2, 81)   # synth bass, pad, saw lead
    prog = ('A2', 'F2', 'C3', 'G2')
    chords = ('A3 C4 E4', 'F3 A3 C4', 'G3 C4 E4', 'G3 B3 D4')
    bar = 4 * Q
    sections = (('intro', 4), ('a', 8), ('b', 8), ('break', 4), ('b', 8), ('outro', 4))
    t0 = 0
    for name, nb in sections:
        for b in range(nb):
            t = t0 + b * bar; i = b % 4; root = m(prog[i])
            drums = name not in ('break',)
            if drums:
                for k in range(8):                                   # hats: 8ths, open on the off-beat of 4
                    s.note(9, 9, t + k * S8, 46 if k == 7 else 42, S8, 60 + 20 * (k % 2 == 0))
                if name != 'intro':
                    for k in (0, 6, 8, 11):                          # kick (16ths)
                        s.note(9, 9, t + k * S16, 36, S16, 110)
                    for k in (4, 12):                                # snare
                        s.note(9, 9, t + k * S16, 38, S16, 100)
                    if b % 8 == 7:
                        for k in (12, 13, 14, 15):
                            s.note(9, 9, t + k * S16, 38, S16, 70 + 8 * (k - 12))
            if name in ('a', 'b', 'outro'):                          # bass: root, octave, fifth pattern
                pat = (0, 0, 12, 0, 7, 0, 12, 10)
                for k, d in enumerate(pat):
                    s.note(1, 0, t + k * S8, root + d, S8 - 20, 96 if k % 2 == 0 else 80)
            for x in chords[i].split():                              # pad
                s.note(2, 1, t, x, bar - 10, 58 if name != 'break' else 70)
            if name in ('b', 'break'):                               # lead: 16th arpeggio over two octaves
                tones = [m(x) + 12 for x in chords[i].split()]
                tones = tones + [x + 12 for x in tones]
                order = (0, 1, 2, 3, 4, 5, 4, 3, 2, 1, 2, 3, 4, 5, 3, 1) if b % 2 == 0 else (5, 4, 3, 2, 1, 0, 1, 2, 3, 4, 5, 4, 3, 5, 4, 2)
                for k, j in enumerate(order):
                    s.note(3, 2, t + k * S16, tones[j], S16 - 10, 78 + 14 * (k % 4 == 0))
        t0 += nb * bar
    for x in ('A1', 'A2', 'E3', 'A3', 'C4', 'E4'):
        s.note(2, 1, t0, x, 2 * bar, 70)
    s.note(9, 9, t0, 49, Q, 110); s.note(9, 9, t0, 36, Q, 110)
    return s

# ---------------------------------------------------------------- G. Holst: The Planets (mashup)
# Mars (5/4 ostinato on G, theme G-D-Db) -> Jupiter's big tune ("Thaxted", melody as Holst wrote it; harmony here is my own).
THAXTED = ('G4:.5 Bb4:.5 C5:1.5 Eb5:.5 D5:.75 Bb4:.25 Eb5:.5 F5:.5 Eb5:1 D5:1 C5:.5 D5:.5 C5:1 Bb4:1 G4:2 '
           'G4:.5 Bb4:.5 C5:1.5 Eb5:.5 D5:.75 Bb4:.25 Eb5:.5 F5:.5 G5:1 G5:1 G5:.5 F5:.5 Eb5:1 F5:1 Eb5:2 '
           'Bb4:.5 G4:.5 F4:1.5 F4:.5 Eb4:.5 G4:.5 F4:1 Bb4:1 Bb4:.5 G4:.5 F4:1.5 F4:.5 G4:.5 Bb4:.5 C5:2 '
           'C5:.5 D5:.5 Eb5:.75 Eb5:.25 D5:1 C5:1 Bb4:1 Eb5:1 G4:1 F4:.5 Eb4:.5 F4:1 G4:1 Bb4:2 '
           'G4:.5 Bb4:.5 C5:1.5 Eb5:.5 D5:.75 Bb4:.25 Eb5:.5 F5:.5 Eb5:1 D5:1 C5:.5 D5:.5 C5:1 Bb4:1 G4:2 '
           'G4:.5 Bb4:.5 C5:1.5 Eb5:.5 D5:.75 Bb4:.25 Eb5:.5 F5:.5 G5:1 G5:1 G5:.5 F5:.5 Eb5:1 F5:1 Eb5:3')
# one chord per beat for the 24 bars after the pickup (3/4): root + triad
THAX_CH = ('Ab Ab Bb  Eb Eb Bb  Ab Ab Bb  Eb Eb Eb  Ab Ab Bb  Eb Cm Cm  Eb Eb Bb  Eb Eb Eb '
           'Bb Bb Eb  Bb Bb Eb  Bb Bb Eb  Ab Ab Fm  Cm Bb Ab  Eb Eb Eb  Bb Bb Eb  Bb Bb Eb '
           'Ab Ab Bb  Eb Eb Bb  Ab Ab Bb  Eb Eb Eb  Ab Ab Bb  Eb Cm Cm  Eb Eb Bb  Eb Eb Eb').split()
TRIAD = {'Eb': 'Eb G Bb', 'Ab': 'Ab C Eb', 'Bb': 'Bb D F', 'Cm': 'C Eb G', 'Fm': 'F Ab C'}

def planets():
    s = Song()
    s.program(1, 0, 10); s.program(2, 1, 24); s.program(3, 2, 61); s.program(4, 3, 40); s.program(5, 4, 48)
    s.program(6, 5, 33)                         # 0 celesta/bells, 1 col legno (pluck), 2 brass, 3 strings, 4 pad, 5 bass
    t = 0
    # Mars: 5/4, ostinato on G (triplet 8ths, q, q, 8 8, q), theme G-D-Db in the brass, building up
    s.tempo(t, 168)
    trip = Q // 3
    ost = [(0, trip), (trip, trip), (2 * trip, trip), (Q, Q), (2 * Q, Q), (3 * Q, S8), (3 * Q + S8, S8), (4 * Q, Q)]
    bars = 20
    for b in range(bars):
        tb = t + b * 5 * Q; v = 70 + 2 * b
        for o, d in ost:
            s.note(2, 1, tb + o, 'G3', d - 10, v); s.note(2, 1, tb + o, 'G2', d - 10, v)
            if b >= 8: s.note(9, 9, tb + o, 41, d, v - 10)          # low tom doubles the rhythm
        if b >= 12 and b % 2: s.note(9, 9, tb, 36, Q, 110)
    for k, (x, d) in enumerate((('G3', 10), ('D4', 10), ('Db4', 20), ('G3', 10), ('D4', 10), ('Db4', 20))):
        st = t + 4 * 5 * Q + sum(dd for _, dd in (('G3', 10), ('D4', 10), ('Db4', 20), ('G3', 10), ('D4', 10), ('Db4', 20))[:k]) * Q // 2
        s.note(3, 2, st, x, d * Q // 2 - 20, 90 + 6 * (k >= 3)); s.note(3, 2, st, m(x) - 12, d * Q // 2 - 20, 90)
    t += bars * 5 * Q
    for k in range(4):                           # Mars-like hammered ending on G
        for x in ('G2', 'G3', 'D4', 'G4'): s.note(3, 2, t + k * Q, x, Q - 40, 115)
        s.note(9, 9, t + k * Q, 36, Q, 120)
    s.note(9, 9, t + 3 * Q, 49, 2 * Q, 120)
    t += 6 * Q
    # Jupiter: Thaxted, strings + octave-lower brass, own chords on the pad, bass on the roots
    s.tempo(t, 76)
    tt = t
    for tok in THAXTED.split():
        x, d = tok.split(':'); d = int(float(d) * Q)
        s.note(4, 3, tt, x, d - 15, 96); s.note(3, 2, tt, m(x) - 12, d - 15, 70)
        tt += d
    tb = t + Q                                   # after the pickup
    for k, c in enumerate(THAX_CH):
        for x in TRIAD[c].split(): s.note(5, 4, tb + k * Q, x + '4', Q - 10, 54)
        root = TRIAD[c].split()[0]
        if k % 3 == 0: s.note(6, 5, tb + k * Q, root + '2', 3 * Q - 20, 92)
    t = tb + len(THAX_CH) * Q
    for x in ('Eb2', 'Eb3', 'G4', 'Bb4', 'Eb5', 'G5'): s.note(5, 4, t, x, 4 * Q, 90)
    s.note(9, 9, t, 49, 2 * Q, 115); s.note(9, 9, t, 36, Q, 115)
    return s

TUNES = (('1_bach_prelude_c.mid', prelude), ('2_pachelbel_canon.mid', canon), ('3_fluid_drive.mid', groove), ('4_holst_planets.mid', planets))

def main():
    os.makedirs('tunes', exist_ok=True)
    for name, fn in TUNES:
        fn().write(f'tunes/{name}')
        print('wrote', name, os.path.getsize(f'tunes/{name}'), 'bytes')
    if '--upload' in sys.argv:
        from rp import Board
        b = Board()
        b.exec("import os\nfor d in ('/FluidFM', '/FluidFM/tunes'):\n    try: os.mkdir(d)\n    except OSError: pass")
        for name, _ in TUNES:
            print('uploaded', b.put(f'tunes/{name}', f'/FluidFM/tunes/{name}'))
        b.close()

if __name__ == '__main__':
    main()
