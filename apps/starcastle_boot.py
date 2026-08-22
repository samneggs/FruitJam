from dvi_rp2_hstx_frame_sync_v2 import DVI_RP2_HSTX
import colors as rv_colors
import framebuf, gc
from uctypes import addressof
import sys
sys.path.append('/Starcastle')

SCREEN_W    = const(640)
SCREEN_H    = const(480)
fb = bytearray(SCREEN_W * SCREEN_H)
display = DVI_RP2_HSTX()
print('mem free:', gc.mem_free())
display.begin(fb, rv_colors.COLOR_MODE_BGR233,
              height=SCREEN_H, width=SCREEN_W, bytes_per_pixel=1)
FLAG_ADDR = addressof(display._frame_flag)

SCREEN = framebuf.FrameBuffer(fb, SCREEN_W, SCREEN_H, framebuf.GS8)

import shared_state
shared_state.fb = fb
shared_state.display = display
shared_state.SCREEN = SCREEN
shared_state.FLAG_ADDR = FLAG_ADDR

from starcastle import *





