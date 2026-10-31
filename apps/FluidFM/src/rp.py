#!/usr/bin/env python3
"""Minimal raw-REPL runner: rp.py file.py | rp.py -c 'code'"""
import sys, time, serial

import glob
PORT = (sorted(glob.glob('/dev/ttyACM*')) or ['/dev/ttyACM0'])[0]   # re-enumerates as ACM1 if a handle was open during a reset

class Board:
    def __init__(self, port=PORT):
        self.s = serial.Serial(port, 115200, timeout=1)
        self.s.write(b'\r\x03\x03')          # interrupt anything running
        time.sleep(0.1)
        self.s.reset_input_buffer()
        self.s.write(b'\r\x01')              # enter raw REPL
        self._until(b'raw REPL; CTRL-B to exit\r\n>')

    def _until(self, tok, timeout=10):
        buf = b''; t = time.time()
        while not buf.endswith(tok):
            c = self.s.read(1)
            if c: buf += c
            elif time.time() - t > timeout:
                raise TimeoutError(buf[-200:])
        return buf[:-len(tok)]

    def exec(self, code, timeout=60):
        if isinstance(code, str): code = code.encode()
        for i in range(0, len(code), 256):
            self.s.write(code[i:i+256]); time.sleep(0.005)
        self.s.write(b'\x04')
        if self.s.read(2) != b'OK':
            raise RuntimeError('raw REPL did not accept code')
        out = self._until(b'\x04', timeout)
        err = self._until(b'\x04', timeout)
        self._until(b'>')
        return out.decode(errors='replace'), err.decode(errors='replace')

    def soft_reset(self):
        """Ctrl-D at the raw prompt: soft reboot without running main.py."""
        self.s.write(b'\x04')
        self._until(b'raw REPL; CTRL-B to exit\r\n>', 15)

    def put(self, local, remote, chunk=2048):
        """Copy a file to the board's filesystem through the raw REPL."""
        data = open(local, 'rb').read()
        self.exec(f"_f = open({remote!r}, 'wb')")
        for i in range(0, len(data), chunk):
            out, err = self.exec(f"_f.write(bytes.fromhex({data[i:i + chunk].hex()!r}))")
            if err: raise RuntimeError(err)
        self.exec("_f.close(); del _f")
        return len(data)

    def close(self):
        self.s.write(b'\x02'); self.s.close()

if __name__ == '__main__':
    args = sys.argv[1:]
    reset = args[0] == '--reset'
    if reset: args = args[1:]
    code = args[1] if args[0] == '-c' else open(args[0]).read()
    b = Board()
    if reset: b.soft_reset()
    out, err = b.exec(code); b.close()
    sys.stdout.write(out)
    if err: sys.stderr.write(err); sys.exit(1)
