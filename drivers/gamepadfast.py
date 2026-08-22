from machine import I2C, Pin
from time import sleep_us

_STATUS_BASE = const(0x00)
_GPIO_BASE = const(0x01)
_ADC_BASE = const(0x09)

_GPIO_BULK = const(0x04)
_GPIO_PULLENSET = const(0x0B)

_ADC_CHANNEL_OFFSET = const(0x07)

class Gamepad:
    def __init__(self):
        self.i2c = I2C(0, scl=Pin(21), sda=Pin(20), freq=400_000)
        self.addr = 0x53

        self.read_adc0 = bytearray([_ADC_BASE, _ADC_CHANNEL_OFFSET + 14])
        self.read_adc1 = bytearray([_ADC_BASE, _ADC_CHANNEL_OFFSET + 15])
        self.read_gpio = bytearray([_GPIO_BASE, _GPIO_BULK])

        self.write_gpio = bytearray([
            _GPIO_BASE,
            _GPIO_PULLENSET,
            0xff, 0xff, 0xff, 0xff
        ])

        self.adc0 = bytearray(2)
        self.adc1 = bytearray(2)
        self.gpio = bytearray(4)

        self.x = 0
        self.y = 0

        self.left = 0
        self.right = 0
        self.up = 0
        self.down = 0
        self.buttons = 0

        # Tune these.
        self.gpio_delay_us = 0
        self.adc_delay_us = 500

        self.init_IO()

    def _read_adc_raw(self, cmd, buf):
        i2c = self.i2c
        i2c.writeto(self.addr, cmd)

        if self.adc_delay_us:
            sleep_us(self.adc_delay_us)

        i2c.readfrom_into(self.addr, buf)
        return (buf[0] << 8) | buf[1]

    def init_IO(self):
        self.x_offset = self._read_adc_raw(self.read_adc0, self.adc0)
        self.y_offset = self._read_adc_raw(self.read_adc1, self.adc1)

        self.i2c.writeto(self.addr, self.write_gpio)
        sleep_us(500)

    def read(self):
        i2c = self.i2c
        addr = self.addr
        try:
            # Read buttons.
            i2c.writeto(addr, self.read_gpio)

            if self.gpio_delay_us:
                sleep_us(self.gpio_delay_us)

            i2c.readfrom_into(addr, self.gpio)

            buttons = self.gpio[3]
            self.buttons = buttons

            self.right = 1 if (buttons & 0b0100000) == 0 else 0
            self.left  = 1 if (buttons & 0b0000100) == 0 else 0
            self.up    = 1 if (buttons & 0b1000000) == 0 else 0
            self.down  = 1 if (buttons & 0b0000010) == 0 else 0

            # Read X axis.
            i2c.writeto(addr, self.read_adc0)
            sleep_us(self.adc_delay_us)
            i2c.readfrom_into(addr, self.adc0)

            x = ((self.adc0[0] << 8) | self.adc0[1]) - self.x_offset
            x = -x

            if -20 < x < 20:
                x = 0

            self.x = x

            # Read Y axis.
            i2c.writeto(addr, self.read_adc1)
            sleep_us(self.adc_delay_us)
            i2c.readfrom_into(addr, self.adc1)

            y = ((self.adc1[0] << 8) | self.adc1[1]) - self.y_offset

            if -20 < y < 20:
                y = 0

            self.y = y
        except OSError:
            print('OS Error')
            pass
        
if __name__ == "__main__":
    from time import ticks_us, ticks_diff
    gamepad = Gamepad() # init class
  
    for i in range(1000):
        ticks = ticks_us()
        gamepad.read() # read all I/O
        x = gamepad.x # -500 to 500 with 0 = center
        y = gamepad.y # -500 to 500 with 0 = center
        left  = gamepad.left   # bool 
        right = gamepad.right  # bool       
        up    = gamepad.up     # bool 
        down  = gamepad.down   # bool
        diff = ticks_diff(ticks_us(),ticks)
        print(diff)
        
        #print(left,right,up,down,x,y)
        #print(bin(gamepad.buttons))
        #print(gamepad.left)