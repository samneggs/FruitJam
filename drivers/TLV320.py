"""
TLV320DAC3100 - Low-Power Stereo Audio DAC Driver for MicroPython
Texas Instruments TLV320DAC3100 with stereo headphone output and mono Class-D speaker

Features:
- Stereo DAC with 95-dB SNR
- 8 kHz to 192 kHz sample rates
- Stereo headphone/lineout and mono Class-D speaker outputs
- I2S, Left-Justified, Right-Justified, DSP modes
- Programmable PLL for flexible clock generation
- Digital volume control with soft-stepping
- DRC (Dynamic Range Compression)
- Beep generator for key clicks
"""

from micropython import const
from machine import I2C
import time

# I2C Address (fixed at 0x18)
_I2C_ADDR = const(0x18)

# Page Definitions
_PAGE_0 = const(0)                              # Default page - clocks, serial interface, flags
_PAGE_1 = const(1)                              # DAC, power controls, output drivers

# Page 0 Registers - Clock Configuration
_REG_PAGE_SELECT = const(0x00)                  # Page select register (exists on all pages)
_REG_SW_RESET = const(0x01)                     # Software reset
_REG_OT_FLAG = const(0x03)                      # Over-temperature flag
_REG_CLK_GEN_MUX = const(0x04)                  # Clock-gen muxing
_REG_PLL_P_R = const(0x05)                      # PLL P and R values
_REG_PLL_J = const(0x06)                        # PLL J value
_REG_PLL_D_MSB = const(0x07)                    # PLL D value MSB
_REG_PLL_D_LSB = const(0x08)                    # PLL D value LSB
_REG_DAC_NDAC = const(0x0B)                     # DAC NDAC divider
_REG_DAC_MDAC = const(0x0C)                     # DAC MDAC divider
_REG_DAC_DOSR_MSB = const(0x0D)                 # DAC DOSR MSB
_REG_DAC_DOSR_LSB = const(0x0E)                 # DAC DOSR LSB
_REG_CLKOUT_MUX = const(0x19)                   # CLKOUT mux
_REG_CLKOUT_M = const(0x1A)                     # CLKOUT M divider
_REG_CODEC_IF_CTRL1 = const(0x1B)               # Codec interface control 1
_REG_DATA_SLOT_OFFSET = const(0x1C)             # Data slot offset
_REG_CODEC_IF_CTRL2 = const(0x1D)               # Codec interface control 2
_REG_BCLK_N = const(0x1E)                       # BCLK N divider
_REG_DAC_FLAG = const(0x25)                     # DAC flag register
_REG_DAC_FLAG2 = const(0x26)                    # DAC flag register 2
_REG_OVERFLOW_FLAGS = const(0x27)               # Overflow flags
_REG_DAC_INT_FLAGS = const(0x2C)                # DAC interrupt flags (sticky)
_REG_INT_FLAGS_DAC = const(0x2E)                # Interrupt flags - DAC
_REG_INT1_CTRL = const(0x30)                    # INT1 control
_REG_INT2_CTRL = const(0x31)                    # INT2 control
_REG_GPIO1_CTRL = const(0x33)                   # GPIO1 control
_REG_DIN_CTRL = const(0x36)                     # DIN pin control
_REG_DAC_PRB = const(0x3C)                      # DAC processing block selection
_REG_DAC_DATAPATH = const(0x3F)                 # DAC data-path setup
_REG_DAC_VOL_CTRL = const(0x40)                 # DAC volume control
_REG_DAC_VOL_L = const(0x41)                    # DAC left volume
_REG_DAC_VOL_R = const(0x42)                    # DAC right volume
_REG_HEADSET_DETECT = const(0x43)               # Headset detection
_REG_DRC_CTRL1 = const(0x44)                    # DRC control 1
_REG_DRC_CTRL2 = const(0x45)                    # DRC control 2
_REG_DRC_CTRL3 = const(0x46)                    # DRC control 3
_REG_BEEP_L = const(0x47)                       # Left beep generator
_REG_BEEP_R = const(0x48)                       # Right beep generator
_REG_BEEP_LEN_MSB = const(0x49)                 # Beep length MSB
_REG_BEEP_LEN_MID = const(0x4A)                 # Beep length middle bits
_REG_BEEP_LEN_LSB = const(0x4B)                 # Beep length LSB
_REG_BEEP_SIN_MSB = const(0x4C)                 # Beep sin(x) MSB
_REG_BEEP_SIN_LSB = const(0x4D)                 # Beep sin(x) LSB
_REG_BEEP_COS_MSB = const(0x4E)                 # Beep cos(x) MSB
_REG_BEEP_COS_LSB = const(0x4F)                 # Beep cos(x) LSB
_REG_VOL_MICDET_SAR = const(0x74)               # VOL/MICDET pin SAR ADC
_REG_VOL_MICDET_GAIN = const(0x75)              # VOL/MICDET pin gain

# Page 1 Registers - DAC and Output Driver Configuration
_REG_HP_SPK_ERR_CTRL = const(0x1E)              # Headphone and speaker amplifier error control
_REG_HP_DRIVERS = const(0x1F)                   # Headphone drivers
_REG_SPK_AMP = const(0x20)                      # Class-D speaker amplifier
_REG_HP_POP_REMOVAL = const(0x21)               # HP output drivers POP removal
_REG_PGA_RAMP = const(0x22)                     # Output driver PGA ramp-down period
_REG_MIXER_ROUTING = const(0x23)                # DAC_L and DAC_R output mixer routing
_REG_ANALOG_VOL_HPL = const(0x24)               # Left analog volume to HPL
_REG_ANALOG_VOL_HPR = const(0x25)               # Right analog volume to HPR
_REG_ANALOG_VOL_SPK = const(0x26)               # Left analog volume to SPK
_REG_HPL_DRIVER = const(0x28)                   # HPL driver
_REG_HPR_DRIVER = const(0x29)                   # HPR driver
_REG_SPK_DRIVER = const(0x2A)                   # Class-D speaker driver
_REG_HP_DRIVER_CTRL = const(0x2C)               # HP driver control
_REG_MICBIAS = const(0x2E)                      # MICBIAS configuration

# Audio Interface Modes
INTERFACE_I2S = const(0)                        # I2S mode (default)
INTERFACE_DSP = const(1)                        # DSP/PCM mode
INTERFACE_RJF = const(2)                        # Right-justified mode
INTERFACE_LJF = const(3)                        # Left-justified mode

# Word Lengths
WORD_LENGTH_16 = const(0)                       # 16-bit word length
WORD_LENGTH_20 = const(1)                       # 20-bit word length
WORD_LENGTH_24 = const(2)                       # 24-bit word length
WORD_LENGTH_32 = const(3)                       # 32-bit word length

# DAC Data Path Sources
DAC_PATH_OFF = const(0)                         # DAC data path off
DAC_PATH_LEFT = const(1)                        # Left data
DAC_PATH_RIGHT = const(2)                       # Right data
DAC_PATH_MONO = const(3)                        # (L+R)/2 mono mix

# Output Common-Mode Voltage Settings
CM_1V35 = const(0)                              # 1.35V common mode
CM_1V50 = const(1)                              # 1.50V common mode
CM_1V65 = const(2)                              # 1.65V common mode
CM_1V80 = const(3)                              # 1.80V common mode

# Class-D Speaker Gain Settings
SPK_GAIN_6DB = const(0)                         # 6 dB gain
SPK_GAIN_12DB = const(1)                        # 12 dB gain
SPK_GAIN_18DB = const(2)                        # 18 dB gain
SPK_GAIN_24DB = const(3)                        # 24 dB gain

# Processing Block Selection (PRB_P1 to PRB_P25)
PRB_P1 = const(1)                               # Filter A, DRC, beep disabled
PRB_P2 = const(2)                               # Filter A, DRC, no beep
PRB_P7 = const(7)                               # Filter B, default (lowest power)
PRB_P25 = const(25)                             # Filter A, DRC, beep enabled


class TLV320DAC3100:
    """Driver for TLV320DAC3100 stereo audio DAC with headphone and speaker outputs."""
    
    def __init__(self, i2c, addr=_I2C_ADDR):
        """Initialize TLV320DAC3100 driver.
        Args:
            i2c: MicroPython I2C object
            addr: I2C address (default 0x18)
        """
        self._i2c = i2c                         # I2C bus object
        self._addr = addr                       # I2C device address
        self._page = 0                          # Current register page
        self._sample_rate = 44100               # Current sample rate
    
    def _set_page(self, page):
        """Switch to specified register page.
        Args:
            page: Page number (0, 1, 3, 8, 9, 12, 13)
        """
        if self._page != page:                  # Only write if page changed
            self._i2c.writeto_mem(self._addr, _REG_PAGE_SELECT, bytes([page]))
            self._page = page                   # Track current page
    
    def _write_reg(self, page, reg, value):
        """Write a single register.
        Args:
            page: Register page
            reg: Register address
            value: Byte value to write
        """
        self._set_page(page)                    # Switch to correct page
        self._i2c.writeto_mem(self._addr, reg, bytes([value]))
    
    def _read_reg(self, page, reg):
        """Read a single register.
        Args:
            page: Register page
            reg: Register address
        Returns:
            Register value as integer
        """
        self._set_page(page)                    # Switch to correct page
        return self._i2c.readfrom_mem(self._addr, reg, 1)[0]
    
    def _modify_reg(self, page, reg, mask, value):
        """Modify specific bits in a register.
        Args:
            page: Register page
            reg: Register address
            mask: Bit mask for bits to modify
            value: New value for masked bits
        """
        current = self._read_reg(page, reg)     # Read current value
        new_val = (current & ~mask) | (value & mask)  # Apply mask and new value
        self._write_reg(page, reg, new_val)     # Write back
    
    def reset(self):
        """Perform software reset of the device."""
        self._write_reg(_PAGE_0, _REG_SW_RESET, 0x01)  # Set reset bit (self-clearing)
        self._page = 0                          # Reset tracks page 0
        time.sleep_ms(2)                        # Wait for reset to complete
    
    def init(self, sample_rate=44100, bits=16, mode=INTERFACE_I2S, master=False):
        """Initialize DAC with specified parameters.
        Args:
            sample_rate: Audio sample rate in Hz (8000-192000)
            bits: Bit depth (16, 20, 24, or 32)
            mode: Interface mode (I2S, DSP, RJF, LJF)
            master: True for master mode, False for slave
        """
        self._sample_rate = sample_rate         # Store sample rate
        self.reset()                            # Start with clean slate
        time.sleep_ms(10)                       # Wait after reset
        self._configure_clocks(sample_rate)     # Set up PLL and dividers
        word_len = {16: WORD_LENGTH_16, 20: WORD_LENGTH_20, 24: WORD_LENGTH_24, 32: WORD_LENGTH_32}.get(bits, WORD_LENGTH_16)
        iface_val = (mode << 6) | (word_len << 4)  # Interface mode and word length
        if master:                              # Master mode: BCLK and WCLK as outputs
            iface_val |= 0x0C                   # D3=1 (BCLK out), D2=1 (WCLK out)
        self._write_reg(_PAGE_0, _REG_CODEC_IF_CTRL1, iface_val)
        self._write_reg(_PAGE_0, _REG_DAC_PRB, PRB_P7)  # Use PRB_P7 (Filter B, lowest power)
    
    def _configure_clocks(self, sample_rate, mclk_freq=12288000):
        """Configure PLL and clock dividers for specified sample rate.
        Args:
            sample_rate: Target audio sample rate in Hz
            mclk_freq: MCLK frequency in Hz (default 12.288 MHz)
        Clock equations:
            PLL_CLK = MCLK * R * J.D / P
            DAC_CLK = PLL_CLK / NDAC
            DAC_MOD_CLK = DAC_CLK / MDAC
            DAC_FS = DAC_MOD_CLK / DOSR
        """
        self._write_reg(_PAGE_0, _REG_CLK_GEN_MUX, 0x03)  # CODEC_CLKIN = PLL_CLK, PLL_CLKIN = MCLK
        if sample_rate == 44100:                # 44.1 kHz family
            p, r, j, d = 1, 1, 7, 5264          # PLL: 12.288MHz * 7.5264 / 1 = 92.44MHz
            ndac, mdac, dosr = 2, 7, 128        # 92.44M / 2 / 7 / 128 = 51.5kHz (approx)
        elif sample_rate == 48000:              # 48 kHz family
            p, r, j, d = 1, 1, 8, 0             # PLL: 12.288MHz * 8 / 1 = 98.304MHz
            ndac, mdac, dosr = 2, 8, 128        # 98.304M / 2 / 8 / 128 = 48kHz
        elif sample_rate == 96000:              # 96 kHz
            p, r, j, d = 1, 1, 8, 0             # PLL: 98.304MHz
            ndac, mdac, dosr = 2, 8, 64         # 98.304M / 2 / 8 / 64 = 96kHz
        elif sample_rate == 192000:             # 192 kHz
            p, r, j, d = 1, 1, 8, 0             # PLL: 98.304MHz
            ndac, mdac, dosr = 2, 8, 32         # 98.304M / 2 / 8 / 32 = 192kHz
        elif sample_rate == 32000:              # 32 kHz
            p, r, j, d = 1, 1, 8, 0             # PLL: 98.304MHz
            ndac, mdac, dosr = 3, 8, 128        # 98.304M / 3 / 8 / 128 = 32kHz
        elif sample_rate == 22050:              # 22.05 kHz
            p, r, j, d = 1, 1, 7, 5264          # PLL: 92.44MHz
            ndac, mdac, dosr = 4, 7, 128        # 92.44M / 4 / 7 / 128 = 25.8kHz (approx)
        elif sample_rate == 16000:              # 16 kHz
            p, r, j, d = 1, 1, 8, 0             # PLL: 98.304MHz
            ndac, mdac, dosr = 6, 8, 128        # 98.304M / 6 / 8 / 128 = 16kHz
        elif sample_rate == 8000:               # 8 kHz
            p, r, j, d = 1, 1, 8, 0             # PLL: 98.304MHz
            ndac, mdac, dosr = 12, 8, 128       # 98.304M / 12 / 8 / 128 = 8kHz
        else:                                   # Default to 48 kHz
            p, r, j, d = 1, 1, 8, 0
            ndac, mdac, dosr = 2, 8, 128
        self._write_reg(_PAGE_0, _REG_PLL_P_R, (1 << 7) | (p << 4) | r)  # Power up PLL, set P and R
        self._write_reg(_PAGE_0, _REG_PLL_J, j)  # Set J value
        self._write_reg(_PAGE_0, _REG_PLL_D_MSB, (d >> 8) & 0x3F)  # D value MSB
        self._write_reg(_PAGE_0, _REG_PLL_D_LSB, d & 0xFF)  # D value LSB (must follow MSB immediately)
        self._write_reg(_PAGE_0, _REG_DAC_NDAC, (1 << 7) | ndac)  # Power up NDAC divider
        self._write_reg(_PAGE_0, _REG_DAC_MDAC, (1 << 7) | mdac)  # Power up MDAC divider
        self._write_reg(_PAGE_0, _REG_DAC_DOSR_MSB, (dosr >> 8) & 0x03)  # DOSR MSB
        self._write_reg(_PAGE_0, _REG_DAC_DOSR_LSB, dosr & 0xFF)  # DOSR LSB (must follow MSB)
        time.sleep_ms(15)                       # Wait for PLL to lock
    
    def power_on_dac(self, left=True, right=True):
        """Power on the DAC channels.
        Args:
            left: Enable left channel DAC
            right: Enable right channel DAC
        """
        val = 0x00                              # Start with both off
        if left:
            val |= 0x80                         # D7: Power up left DAC
        if right:
            val |= 0x40                         # D6: Power up right DAC
        val |= 0x14                             # D5-D4=01 (left=left), D3-D2=01 (right=right)
        self._write_reg(_PAGE_0, _REG_DAC_DATAPATH, val)
        self.unmute_dac(left, right)            # Unmute after power on
    
    def power_off_dac(self):
        """Power off both DAC channels."""
        self._write_reg(_PAGE_0, _REG_DAC_DATAPATH, 0x14)  # DACs off, paths configured
    
    def mute_dac(self, left=True, right=True):
        """Mute DAC channels.
        Args:
            left: Mute left channel
            right: Mute right channel
        """
        val = self._read_reg(_PAGE_0, _REG_DAC_VOL_CTRL) | 0x0C  # Read and set both mutes
        if not left:
            val &= ~0x08                        # Clear D3 to unmute left
        if not right:
            val &= ~0x04                        # Clear D2 to unmute right
        self._write_reg(_PAGE_0, _REG_DAC_VOL_CTRL, val)
    
    def unmute_dac(self, left=True, right=True):
        """Unmute DAC channels.
        Args:
            left: Unmute left channel
            right: Unmute right channel
        """
        val = self._read_reg(_PAGE_0, _REG_DAC_VOL_CTRL)  # Read current value
        if left:
            val &= ~0x08                        # Clear D3 to unmute left
        if right:
            val &= ~0x04                        # Clear D2 to unmute right
        self._write_reg(_PAGE_0, _REG_DAC_VOL_CTRL, val)
    
    def set_dac_volume(self, volume_db, channel='both'):
        """Set DAC digital volume.
        Args:
            volume_db: Volume in dB (-63.5 to +24.0)
            channel: 'left', 'right', or 'both'
        """
        volume_db = max(-63.5, min(24.0, volume_db))  # Clamp to valid range
        if volume_db >= 0:                      # Positive gain
            reg_val = int(volume_db * 2)        # 0.5 dB steps, positive values
        else:                                   # Negative gain (attenuation)
            reg_val = int(256 + (volume_db * 2))  # Two's complement
        reg_val = reg_val & 0xFF                # Ensure 8-bit value
        if channel in ('left', 'both'):
            self._write_reg(_PAGE_0, _REG_DAC_VOL_L, reg_val)
        if channel in ('right', 'both'):
            self._write_reg(_PAGE_0, _REG_DAC_VOL_R, reg_val)
    
    def enable_headphone(self, left=True, right=True, cm_voltage=CM_1V65):
        """Enable headphone output drivers.
        Args:
            left: Enable left headphone output
            right: Enable right headphone output
            cm_voltage: Output common-mode voltage (CM_1V35/50/65/80)
        """
        hp_val = 0x04                           # D2=1 (reserved, must be 1)
        if left:
            hp_val |= 0x80                      # D7: Power up HPL
        if right:
            hp_val |= 0x40                      # D6: Power up HPR
        hp_val |= (cm_voltage & 0x03) << 3      # D4-D3: Common-mode voltage
        self._write_reg(_PAGE_1, _REG_HP_DRIVERS, hp_val)
        self._write_reg(_PAGE_1, _REG_MIXER_ROUTING, 0x44)  # Route DAC_L to HPL mixer, DAC_R to HPR mixer
        self._write_reg(_PAGE_1, _REG_ANALOG_VOL_HPL, 0x80)  # Route left analog vol to HPL, 0dB
        self._write_reg(_PAGE_1, _REG_ANALOG_VOL_HPR, 0x80)  # Route right analog vol to HPR, 0dB
        time.sleep_ms(50)                       # Wait for power-on sequence
        if left:
            self._modify_reg(_PAGE_1, _REG_HPL_DRIVER, 0x04, 0x04)  # Unmute HPL (D2=1)
        if right:
            self._modify_reg(_PAGE_1, _REG_HPR_DRIVER, 0x04, 0x04)  # Unmute HPR (D2=1)
    
    def disable_headphone(self):
        """Disable headphone output drivers."""
        self._modify_reg(_PAGE_1, _REG_HPL_DRIVER, 0x04, 0x00)  # Mute HPL
        self._modify_reg(_PAGE_1, _REG_HPR_DRIVER, 0x04, 0x00)  # Mute HPR
        self._write_reg(_PAGE_1, _REG_HP_DRIVERS, 0x04)  # Power down both HP drivers
    
    def set_headphone_gain(self, gain_db, channel='both'):
        """Set headphone driver gain.
        Args:
            gain_db: Gain in dB (0 to 9)
            channel: 'left', 'right', or 'both'
        """
        gain_db = max(0, min(9, int(gain_db)))  # Clamp to 0-9 dB
        if channel in ('left', 'both'):
            val = self._read_reg(_PAGE_1, _REG_HPL_DRIVER)
            val = (val & 0x87) | ((gain_db & 0x0F) << 3)  # Set D6-D3
            self._write_reg(_PAGE_1, _REG_HPL_DRIVER, val)
        if channel in ('right', 'both'):
            val = self._read_reg(_PAGE_1, _REG_HPR_DRIVER)
            val = (val & 0x87) | ((gain_db & 0x0F) << 3)  # Set D6-D3
            self._write_reg(_PAGE_1, _REG_HPR_DRIVER, val)
    
    def set_analog_volume(self, volume_db, output='headphone'):
        """Set analog volume attenuation.
        Args:
            volume_db: Volume in dB (0 to -78, 0 = no attenuation)
            output: 'headphone' or 'speaker'
        Non-linear mapping as per datasheet Table 6-24.
        """
        volume_db = max(-78, min(0, volume_db))  # Clamp to valid range
        if volume_db >= -0.5:                   # 0 dB to -0.5 dB
            reg_val = 0x00 if volume_db == 0 else 0x01
        elif volume_db >= -6:                   # -1 dB to -6 dB (1 dB steps)
            reg_val = int(-volume_db) + 1
        else:                                   # -6.5 dB to -78 dB (0.5 dB steps)
            reg_val = int((-volume_db - 6) * 2) + 7
        reg_val = 0x80 | (reg_val & 0x7F)       # D7=1 to enable routing
        if output == 'headphone':
            self._write_reg(_PAGE_1, _REG_ANALOG_VOL_HPL, reg_val)
            self._write_reg(_PAGE_1, _REG_ANALOG_VOL_HPR, reg_val)
        elif output == 'speaker':
            self._write_reg(_PAGE_1, _REG_ANALOG_VOL_SPK, reg_val)
    
    def enable_speaker(self, gain=SPK_GAIN_6DB):
        """Enable Class-D speaker output.
        Args:
            gain: Speaker gain (SPK_GAIN_6DB/12DB/18DB/24DB)
        """
        self._write_reg(_PAGE_1, _REG_SPK_AMP, 0x86)  # Power up Class-D driver
        self._write_reg(_PAGE_1, _REG_MIXER_ROUTING, 0x40)  # Route DAC_L to mixer
        self._write_reg(_PAGE_1, _REG_ANALOG_VOL_SPK, 0x80)  # Route to SPK, 0dB attenuation
        time.sleep_ms(50)                       # Wait for power-on
        spk_val = 0x04 | ((gain & 0x03) << 3)   # D2=1 (unmute), D4-D3 = gain
        self._write_reg(_PAGE_1, _REG_SPK_DRIVER, spk_val)
    
    def disable_speaker(self):
        """Disable Class-D speaker output."""
        self._write_reg(_PAGE_1, _REG_SPK_DRIVER, 0x00)  # Mute speaker
        self._write_reg(_PAGE_1, _REG_SPK_AMP, 0x06)  # Power down Class-D
    
    def set_speaker_gain(self, gain):
        """Set Class-D speaker output stage gain.
        Args:
            gain: SPK_GAIN_6DB, SPK_GAIN_12DB, SPK_GAIN_18DB, or SPK_GAIN_24DB
        """
        val = self._read_reg(_PAGE_1, _REG_SPK_DRIVER)
        val = (val & 0xE7) | ((gain & 0x03) << 3)  # Set D4-D3
        self._write_reg(_PAGE_1, _REG_SPK_DRIVER, val)
    
    def enable_drc(self, threshold_db=-12, left=True, right=True):
        """Enable Dynamic Range Compression.
        Args:
            threshold_db: Compression threshold (-3 to -24 dB, multiples of 3)
            left: Enable DRC for left channel
            right: Enable DRC for right channel
        """
        thresh_map = {-3: 0, -6: 1, -9: 2, -12: 3, -15: 4, -18: 5, -21: 6, -24: 7}
        thresh = thresh_map.get(threshold_db, 3)  # Default -12 dB
        val = (thresh << 2) | 0x03              # Threshold + 3dB hysteresis
        if left:
            val |= 0x40                         # D6: Enable left DRC
        if right:
            val |= 0x20                         # D5: Enable right DRC
        self._write_reg(_PAGE_0, _REG_DRC_CTRL1, val)
    
    def disable_drc(self):
        """Disable Dynamic Range Compression."""
        self._write_reg(_PAGE_0, _REG_DRC_CTRL1, 0x0F)  # DRC disabled, default threshold
    
    def enable_beep(self, frequency_hz=1000, duration_ms=100, volume_db=0):
        """Generate a beep tone (requires PRB_P25 processing block).
        Args:
            frequency_hz: Beep frequency in Hz
            duration_ms: Beep duration in milliseconds
            volume_db: Beep volume (-61 to +2 dB)
        """
        self._write_reg(_PAGE_0, _REG_DAC_PRB, PRB_P25)  # Switch to beep-capable PRB
        sin_val = int(32767 * _sin_approx(2 * 3.14159 * frequency_hz / self._sample_rate))
        cos_val = int(32767 * _cos_approx(2 * 3.14159 * frequency_hz / self._sample_rate))
        self._write_reg(_PAGE_0, _REG_BEEP_SIN_MSB, (sin_val >> 8) & 0xFF)
        self._write_reg(_PAGE_0, _REG_BEEP_SIN_LSB, sin_val & 0xFF)
        self._write_reg(_PAGE_0, _REG_BEEP_COS_MSB, (cos_val >> 8) & 0xFF)
        self._write_reg(_PAGE_0, _REG_BEEP_COS_LSB, cos_val & 0xFF)
        samples = int((duration_ms / 1000) * self._sample_rate)  # Duration in samples
        self._write_reg(_PAGE_0, _REG_BEEP_LEN_MSB, (samples >> 16) & 0xFF)
        self._write_reg(_PAGE_0, _REG_BEEP_LEN_MID, (samples >> 8) & 0xFF)
        self._write_reg(_PAGE_0, _REG_BEEP_LEN_LSB, samples & 0xFF)
        volume_db = max(-61, min(2, volume_db))  # Clamp volume
        vol_reg = (2 - volume_db) & 0x3F        # Convert to register value
        self._write_reg(_PAGE_0, _REG_BEEP_L, 0x80 | vol_reg)  # Enable beep + volume
        self._write_reg(_PAGE_0, _REG_BEEP_R, vol_reg)  # Right channel volume
    
    def enable_headset_detection(self, debounce_ms=64):
        """Enable headset insertion detection.
        Args:
            debounce_ms: Debounce time (16, 32, 64, 128, 256, or 512 ms)
        """
        debounce_map = {16: 0, 32: 1, 64: 2, 128: 3, 256: 4, 512: 5}
        debounce = debounce_map.get(debounce_ms, 2)  # Default 64ms
        self._write_reg(_PAGE_0, _REG_HEADSET_DETECT, 0x80 | (debounce << 2))  # Enable + debounce
    
    def get_headset_status(self):
        """Get headset detection status.
        Returns:
            tuple: (detected: bool, has_mic: bool)
        """
        val = self._read_reg(_PAGE_0, _REG_HEADSET_DETECT)
        status = (val >> 5) & 0x03              # D6-D5: status bits
        if status == 0:
            return (False, False)               # No headset
        elif status == 1:
            return (True, False)                # Headset without mic
        elif status == 3:
            return (True, True)                 # Headset with mic
        return (False, False)                   # Unknown state
    
    def set_micbias(self, voltage_mv=2500):
        """Set microphone bias voltage.
        Args:
            voltage_mv: Bias voltage in mV (2000, 2500, or 'avdd')
        """
        if voltage_mv == 'avdd' or voltage_mv > 2500:
            self._write_reg(_PAGE_1, _REG_MICBIAS, 0x08)  # MICBIAS = AVDD
        elif voltage_mv >= 2250:
            self._write_reg(_PAGE_1, _REG_MICBIAS, 0x0A)  # MICBIAS = 2.5V
        else:
            self._write_reg(_PAGE_1, _REG_MICBIAS, 0x09)  # MICBIAS = 2.0V
    
    def get_dac_flags(self):
        """Get DAC status flags.
        Returns:
            dict: Status flags for DAC and output drivers
        """
        flag1 = self._read_reg(_PAGE_0, _REG_DAC_FLAG)
        flag2 = self._read_reg(_PAGE_0, _REG_DAC_FLAG2)
        return {'left_dac_powered': bool(flag1 & 0x80), 'right_dac_powered': bool(flag1 & 0x08), 'hpl_powered': bool(flag1 & 0x20), 'hpr_powered': bool(flag1 & 0x02), 'spk_powered': bool(flag1 & 0x10), 'left_gain_applied': bool(flag2 & 0x10), 'right_gain_applied': bool(flag2 & 0x01)}
    
    def configure_gpio1(self, function='disabled'):
        """Configure GPIO1 pin function.
        Args:
            function: 'disabled', 'input', 'gpi', 'gpo', 'clkout', 'int1', 'int2'
        """
        func_map = {'disabled': 0x00, 'input': 0x04, 'gpi': 0x08, 'gpo': 0x0C, 'clkout': 0x10, 'int1': 0x14, 'int2': 0x18}
        self._write_reg(_PAGE_0, _REG_GPIO1_CTRL, func_map.get(function, 0x00))
    
    def set_processing_block(self, prb):
        """Set DAC signal processing block.
        Args:
            prb: Processing block number (PRB_P1 to PRB_P25)
        """
        self._write_reg(_PAGE_0, _REG_DAC_PRB, prb & 0x1F)


def _sin_approx(x):
    """Approximate sine using Taylor series (for beep frequency calculation).
    Args:
        x: Angle in radians
    Returns:
        Approximate sine value
    """
    x = x % (2 * 3.14159)                       # Normalize to 0-2π
    if x > 3.14159:
        x = x - 2 * 3.14159                     # Map to -π to π
    x3 = x * x * x                              # x^3
    x5 = x3 * x * x                             # x^5
    return x - x3 / 6 + x5 / 120                # Taylor series approximation


def _cos_approx(x):
    """Approximate cosine using Taylor series.
    Args:
        x: Angle in radians
    Returns:
        Approximate cosine value
    """
    return _sin_approx(x + 1.5708)              # cos(x) = sin(x + π/2)

def complete_setup_example():
    """Complete example: Initialize DAC and play a sine wave test tone."""
    i2c = I2C(0, scl=Pin(21), sda=Pin(20), freq=400000)  # I2C for control
    dac = TLV320DAC3100(i2c)                   # Create DAC
    s_rate = 44100
    dac.init(sample_rate=s_rate, bits=16, mode=INTERFACE_I2S, master=False)  # Configure
    dac.power_on_dac()                         # Power on DACs
    dac.set_dac_volume(-6)                     # -6 dB volume
    dac.enable_headphone()                     # Enable headphones
    #dac.enable_speaker(gain=0)
    sck_pin = Pin(26)                          # I2S pins
    ws_pin = Pin(27)
    sd_pin = Pin(24)
    audio_out = I2S(0, sck=sck_pin, ws=ws_pin, sd=sd_pin, mode=I2S.TX, bits=16, format=I2S.STEREO, rate=s_rate, ibuf=20_000)
    import math                                # For sine wave generation
    import array
    samples = array.array('h', [0] * (s_rate//50))      # 882 samples = 20ms at 44.1 kHz
    dac.set_dac_volume(-6)
    for i in range(s_rate//100):                       # Generate 1 kHz sine wave
        val = int(16000 * math.sin(2 * math.pi * i / (s_rate/1000)))  # 1 kHz tone
        samples[i * 2] = val                   # Left channel
        samples[i * 2 + 1] = val               # Right channel
     
    for _ in range(100):                       # Play for ~2 seconds   
        audio_out.write(samples)               # Write samples
    dac.mute_dac()                             # Mute when done
    audio_out.deinit()                         # Clean up
    dac.disable_speaker()                      # Disable outputs
    print("Playback complete")


if __name__ == '__main__':
    from machine import Pin, I2S
    rst_pin = Pin(22, Pin.OUT)   # reset
    rst_pin.value(0)
    rst_pin.value(1)
    complete_setup_example()
    