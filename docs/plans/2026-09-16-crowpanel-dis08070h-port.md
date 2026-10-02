# CrowPanel DIS08070H Port — Implementation Plan

> **For the implementing agent:** Work task-by-task, in order. Each task ends with a
> hardware verification step and a commit. Do not start task N+1 until task N's
> verification has actually been observed in serial output. Steps use checkbox
> (`- [ ]`) syntax for tracking.

**Goal:** Port this firmware from the Elecrow **CrowPanel Advance 7.0** (which it was
written for) to the Elecrow **CrowPanel 7.0" HMI, SKU DIS08070H**, which is the board
actually on the desk.

**Architecture:** This is a pin-map and board-bring-up port, not a logic change. The
application layer (radar, OpenSky client, webserver, LVGL UI) is board-independent and
must not be touched. All changes are confined to `main/waveshare_rgb_lcd_port.h`,
`main/waveshare_rgb_lcd_port.c`, and the hardware-init prologue of `main/main.c`.

**Tech Stack:** ESP-IDF v5.5.2, LVGL 8.4.0, esp_lcd RGB panel driver, GT911 touch
controller, ESP32-S3-WROOM-1-N4R8.

## Background: why this port is needed

The board this code was written for and the board it is running on are different
products with conflicting pin maps. Verified on hardware 2026-09-16:

- An I2C bus scan of the full address range `0x08`–`0x77` on the configured pins
  (SDA=15, SCL=16) returned **zero devices**, both before and after the existing
  expander writes.
- GPIO 15 and 16 are **LCD data lines** on DIS08070H (blue bit 0, green bit 4). There
  is no I2C peripheral on them.
- GPIO 19, which the current code drives as a backlight output, **is the I2C SDA line**
  on DIS08070H.
- The firmware aborts at `waveshare_rgb_lcd_port.c:165` when
  `esp_lcd_touch_new_i2c_gt911()` fails, producing a reboot loop.

## Global Constraints

- Board: Elecrow CrowPanel 7.0" HMI, **SKU DIS08070H**, module ESP32-S3-WROOM-1-**N4R8**.
- Flash is **4 MB**. `sdkconfig`, `sdkconfig.defaults` and `partitions.csv` are already
  correct for 4 MB and **must not be changed**. Any partition layout must total
  `<= 0x400000`.
- ESP-IDF **v5.5.2**, installed at `$HOME/esp/esp-idf`. Activate in every new shell with
  `. $HOME/esp/esp-idf/export.sh` before any `idf.py` command.
- Project directory is `src/`, not the repo root. Run `idf.py` from `src/`, or use
  `idf.py -C src`.
- Serial port: `/dev/cu.usbserial-2130`. Console UART is on GPIO 43/44 at 115200 baud.
- **Do not** hold the serial port open while flashing. `idf.py monitor` locks it and
  `flash` will fail with `Could not exclusively lock port`.
- There is no host unit-test framework and none should be added. Verification for every
  task is: build, flash, capture serial output, assert on specific log lines.
- Do not modify `main/ui/`, `main/radar.c`, `main/opensky_client.c`, or
  `main/webserver.c`. They are board-independent.
- LVGL stays at 8.4.0. Do not upgrade.

## Reference: DIS08070H hardware map

Authoritative source: the ESPHome device PR for this exact SKU
(<https://github.com/esphome/esphome-devices/pull/1494>).

| Signal | GPIO |
|---|---|
| LCD DE | 41 |
| LCD HSYNC | 39 |
| LCD VSYNC | 40 |
| LCD PCLK | 0 |
| Backlight | 2 |
| Touch I2C SDA | 19 |
| Touch I2C SCL | 20 |
| Touch INT | 38 |
| Red bits (5) | 14, 21, 47, 48, 45 |
| Green bits (6) | 9, 46, 3, 8, 16, 1 |
| Blue bits (5) | 15, 7, 6, 5, 4 |

Other facts about this board:

- I/O expander is a **PCA9557 at I2C address 0x18**. It drives GT911 reset (expander
  pin 0) and INT (pin 1). **Leave it alone.** A hardware pull-up already releases the
  GT911 from reset before firmware runs; writing to the expander during boot breaks
  touch. The current code writes to `0x30`, which is not this board's expander at all.
- GT911 address is **0x5D** when GPIO 38 is low at power-up, **0x14** when high. It
  varies between individual units.
- **GPIO 19 and 20 are the USB_SERIAL_JTAG pins.** The USB PHY pads are enabled at
  reset and will hold the lines, causing I2C timeouts, unless the PHY pad is explicitly
  disabled before I2C init.
- Panel timings differ from the Advance: 15 MHz pixel clock; hsync front porch 40,
  pulse 48, back porch 13; vsync front porch 1, pulse 31, back porch 13.

---

### Task 0: Baseline — commit the working flash fix and add a serial capture tool

The 4 MB flash fix is already made and verified working in the tree, but uncommitted.
Commit it before starting the port so the port can be reverted independently.

**Files:**
- Create: `.gitignore`
- Create: `tools/capture_serial.py`
- Commit (already modified): `src/partitions.csv`, `src/sdkconfig`,
  `src/sdkconfig.defaults`, `src/main/main.c`

- [ ] **Step 1: Add a `.gitignore` so the build directory stays untracked**

```bash
cat > .gitignore <<'EOF'
build/
sdkconfig.old
EOF
```

- [ ] **Step 2: Create the serial capture tool**

This is the verification instrument for every remaining task. It captures serial output
for a fixed duration without holding the port open indefinitely, so it can run
unattended.

Create `tools/capture_serial.py`:

```python
#!/usr/bin/env python3
"""Capture ESP32 serial output for a fixed duration.

Usage: python tools/capture_serial.py [seconds] [port]
Requires pyserial, which is present in the ESP-IDF python environment.
"""
import sys
import time

import serial

DEFAULT_PORT = "/dev/cu.usbserial-2130"
BAUD = 115200


def main() -> int:
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
    port = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_PORT

    with serial.Serial(port, BAUD, timeout=0.2) as ser:
        buf = b""
        deadline = time.time() + duration
        while time.time() < deadline:
            buf += ser.read(4096)

    sys.stdout.write(buf.decode("utf-8", errors="replace"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Verify the tool runs**

```bash
. $HOME/esp/esp-idf/export.sh
python tools/capture_serial.py 5 | head -20
```

Expected: serial text from the board. The board is currently in a reboot loop, so
expect repeated boot banners ending in the GT911 abort. A `SerialException` about the
port being busy means something else (usually `idf.py monitor`) is holding it — close
that first.

- [ ] **Step 4: Commit**

```bash
git add .gitignore tools/capture_serial.py src/partitions.csv src/sdkconfig \
        src/sdkconfig.defaults src/main/main.c src/dependencies.lock
git commit -m "fix: configure for 4MB flash and add serial capture tool

The ESP32-S3-WROOM-1-N4R8 module has 4MB of flash, but the build was
configured for 16MB, so the bootloader rejected the image header with
'Detected size(4096k) smaller than the size in the binary image header
(16384k)' and rebooted in a loop.

Sets flash size to 4MB and rewrites the partition table to fit within
0x400000. The app uses 1.58MB of the 3.19MB factory partition.

Also adds a temporary I2C bus scan diagnostic used to investigate the
GT911 touch failure, removed in a later commit."
```

---

### Task 1: Fix the I2C pins and release the USB_SERIAL_JTAG pads

This is the decisive task. Until the bus scan finds devices, nothing else can be
verified.

**Files:**
- Modify: `src/main/waveshare_rgb_lcd_port.h:18-19`
- Modify: `src/main/main.c` (include block, and top of `app_main`)

**Interfaces:**
- Consumes: `i2c_bus_scan(const char *when)` — the diagnostic added in Task 0, defined
  in `src/main/main.c`.
- Produces: a working I2C bus on port 0 at SDA=19, SCL=20, used by every later task.

- [ ] **Step 1: Change the I2C pins**

In `src/main/waveshare_rgb_lcd_port.h`, replace lines 18-19:

```c
#define I2C_MASTER_SCL_IO           16       /*!< GPIO number used for I2C master clock */
#define I2C_MASTER_SDA_IO           15       /*!< GPIO number used for I2C master data  */
```

with:

```c
#define I2C_MASTER_SCL_IO           20       /*!< GPIO number used for I2C master clock */
#define I2C_MASTER_SDA_IO           19       /*!< GPIO number used for I2C master data  */
```

- [ ] **Step 2: Release the USB PHY pads**

GPIO 19 and 20 are the USB_SERIAL_JTAG D- and D+ pads. The PHY is enabled at reset and
will hold them, producing I2C timeouts. Disable it before `i2c_master_init()`.

In `src/main/main.c`, add to the include block (it already contains `#include
"nvs_flash.h"` — put this directly after it):

```c
#include "hal/usb_serial_jtag_ll.h"
```

Then in `app_main`, make the PHY disable the very first statement. The function
currently begins:

```c
void app_main()
{

    vTaskDelay(pdMS_TO_TICKS(50));

    i2c_master_init();
```

Change it to:

```c
void app_main()
{
    /* GPIO19/20 are the USB_SERIAL_JTAG pads and are enabled at reset. They must be
     * released before those pins can be used as the touch I2C bus. */
    usb_serial_jtag_ll_phy_enable_pad(false);

    vTaskDelay(pdMS_TO_TICKS(50));

    i2c_master_init();
```

This include and call have been compile-verified against ESP-IDF v5.5.2.

- [ ] **Step 3: Build**

```bash
. $HOME/esp/esp-idf/export.sh
cd src && idf.py build
```

Expected: `Project build complete`, and a size line reporting roughly 50% of the
factory partition free.

- [ ] **Step 4: Flash and capture**

```bash
idf.py -p /dev/cu.usbserial-2130 flash
python ../tools/capture_serial.py 12 | grep I2CSCAN
```

- [ ] **Step 5: Verify the bus scan finds hardware**

Expected output, with the pin numbers now reading 19/20:

```
W (977) I2CSCAN: --- bus scan (before expander writes) sda=19 scl=20 port=0 ---
W (98x) I2CSCAN:     ACK at 0x18
W (98x) I2CSCAN:     ACK at 0x5D
W (98x) I2CSCAN: --- 2 device(s) responded ---
```

`0x18` is the PCA9557 expander. The touch controller will be at `0x5D` or `0x14` —
**record which one your unit reports**, it is needed in Task 5.

If **0 devices** still respond, stop and do not continue to Task 2. Check in order:
the USB PHY disable is genuinely the first statement in `app_main`; the header edit
took effect (`grep I2C_MASTER src/main/waveshare_rgb_lcd_port.h`); the board is a
DIS08070H and not another SKU.

The GT911 abort at the end of boot is still expected at this point — Task 5 addresses
the address handling. Reaching the abort with a successful scan is a pass.

- [ ] **Step 6: Commit**

```bash
git add src/main/waveshare_rgb_lcd_port.h src/main/main.c
git commit -m "fix: move touch I2C to GPIO19/20 for DIS08070H

The pin map targeted the CrowPanel Advance 7.0, where touch I2C is on
GPIO15/16. On the DIS08070H those pins are LCD data lines, so the bus
scan found no devices at all.

GPIO19/20 are also the USB_SERIAL_JTAG pads, which are enabled at reset
and hold the lines, so the PHY pad is now released before I2C init."
```

---

### Task 2: Move the backlight to GPIO 2 and stop writing to the wrong expander

With I2C on GPIO 19, the existing backlight code actively breaks the bus: it
reconfigures GPIO 19 as a general-purpose output, clobbering SDA.

**Files:**
- Modify: `src/main/main.c:48` (the `LCD_BL_PIN` definition)
- Modify: `src/main/main.c` (`app_main`, the expander writes and backlight setup)

- [ ] **Step 1: Change the backlight pin**

In `src/main/main.c`, line 48 currently reads:

```c
#define LCD_BL_PIN 19
```

Change it to:

```c
#define LCD_BL_PIN 2
```

- [ ] **Step 2: Remove the wrong-expander writes and drive the backlight on**

`0x30` is not this board's expander — the PCA9557 is at `0x18`, and it must be left
alone because a hardware pull-up already releases the GT911 from reset. The current
code also never actually sets the backlight level, only its direction.

In `app_main`, replace:

```c
    i2c_write_byte(0x30, 0x18);
    i2c_write_byte(0x30, 0x10);

    gpio_reset_pin(LCD_BL_PIN);
    gpio_set_direction(LCD_BL_PIN, GPIO_MODE_OUTPUT);
```

with:

```c
    /* The DIS08070H expander is a PCA9557 at 0x18 and must be left alone: a hardware
     * pull-up releases the GT911 from reset before firmware runs, and writing to the
     * expander during boot breaks touch. */

    gpio_reset_pin(LCD_BL_PIN);
    gpio_set_direction(LCD_BL_PIN, GPIO_MODE_OUTPUT);
    gpio_set_level(LCD_BL_PIN, 1);
```

Leave the two `i2c_bus_scan` diagnostic calls in place. Their `"before expander
writes"` / `"after expander writes"` labels are now inaccurate but harmless; both are
removed in Task 6.

- [ ] **Step 3: Build, flash, capture**

```bash
. $HOME/esp/esp-idf/export.sh
cd src && idf.py build && idf.py -p /dev/cu.usbserial-2130 flash
python ../tools/capture_serial.py 12 | grep I2CSCAN
```

- [ ] **Step 4: Verify**

Both scans must still report the same devices found in Task 1. If the second scan
now reports fewer devices than the first, the backlight code is still touching an I2C
pin — re-check that `LCD_BL_PIN` is 2.

The screen backlight should visibly come on, even though nothing is drawn yet. **This
requires looking at the panel.** A dark panel with a passing scan means the backlight
pin is wrong; report it rather than guessing at another pin.

- [ ] **Step 5: Commit**

```bash
git add src/main/main.c
git commit -m "fix: move backlight to GPIO2 and stop writing to absent expander

GPIO19 is the I2C SDA line on DIS08070H, so driving it as the backlight
output clobbered the touch bus. The board's expander is a PCA9557 at
0x18, not 0x30, and must be left alone because a pull-up already
releases the GT911 from reset."
```

---

### Task 3: Replace the RGB LCD pin map

**Files:**
- Modify: `src/main/waveshare_rgb_lcd_port.h:38-58`

**Interfaces:**
- Produces: the `EXAMPLE_LCD_IO_RGB_*` macros consumed by
  `waveshare_esp32_s3_rgb_lcd_init()` in `src/main/waveshare_rgb_lcd_port.c:73-126`.

The esp_lcd RGB driver takes `data_gpio_nums[]` least-significant-bit first. For
RGB565 the conventional grouping is: indices 0-4 blue, 5-10 green, 11-15 red.

- [ ] **Step 1: Replace the pin block**

In `src/main/waveshare_rgb_lcd_port.h`, replace lines 38-58 (from
`EXAMPLE_LCD_IO_RGB_DISP` through `EXAMPLE_LCD_IO_RGB_DATA15`) with:

```c
#define EXAMPLE_LCD_IO_RGB_DISP         (-1)             // -1 if not used
#define EXAMPLE_LCD_IO_RGB_VSYNC        (GPIO_NUM_40)
#define EXAMPLE_LCD_IO_RGB_HSYNC        (GPIO_NUM_39)
#define EXAMPLE_LCD_IO_RGB_DE           (GPIO_NUM_41)
#define EXAMPLE_LCD_IO_RGB_PCLK         (GPIO_NUM_0)
/* Blue, bits 0-4 */
#define EXAMPLE_LCD_IO_RGB_DATA0        (GPIO_NUM_15)
#define EXAMPLE_LCD_IO_RGB_DATA1        (GPIO_NUM_7)
#define EXAMPLE_LCD_IO_RGB_DATA2        (GPIO_NUM_6)
#define EXAMPLE_LCD_IO_RGB_DATA3        (GPIO_NUM_5)
#define EXAMPLE_LCD_IO_RGB_DATA4        (GPIO_NUM_4)
/* Green, bits 0-5 */
#define EXAMPLE_LCD_IO_RGB_DATA5        (GPIO_NUM_9)
#define EXAMPLE_LCD_IO_RGB_DATA6        (GPIO_NUM_46)
#define EXAMPLE_LCD_IO_RGB_DATA7        (GPIO_NUM_3)
#define EXAMPLE_LCD_IO_RGB_DATA8        (GPIO_NUM_8)
#define EXAMPLE_LCD_IO_RGB_DATA9        (GPIO_NUM_16)
#define EXAMPLE_LCD_IO_RGB_DATA10       (GPIO_NUM_1)
/* Red, bits 0-4 */
#define EXAMPLE_LCD_IO_RGB_DATA11       (GPIO_NUM_14)
#define EXAMPLE_LCD_IO_RGB_DATA12       (GPIO_NUM_21)
#define EXAMPLE_LCD_IO_RGB_DATA13       (GPIO_NUM_47)
#define EXAMPLE_LCD_IO_RGB_DATA14       (GPIO_NUM_48)
#define EXAMPLE_LCD_IO_RGB_DATA15       (GPIO_NUM_45)
```

Note that GPIO 38, previously used as an LCD data line, is now free — it is the touch
INT pin on this board.

- [ ] **Step 2: Build, flash, capture**

```bash
. $HOME/esp/esp-idf/export.sh
cd src && idf.py build && idf.py -p /dev/cu.usbserial-2130 flash
python ../tools/capture_serial.py 12 | tail -40
```

- [ ] **Step 3: Verify**

The RGB driver cannot detect a wrong pin map — it just drives pins — so serial output
alone cannot confirm this task. **Verification requires looking at the panel.**

The firmware still aborts on GT911 before LVGL draws anything, so at this point the
correct result is a lit backlight with a blank or noise-filled panel, not a rendered
UI. Full visual verification happens at the end of Task 5.

Confirm only that the build succeeded and no new `esp_lcd` errors appear in the log,
such as `invalid GPIO number` or `install RGB LCD panel driver failed`.

- [ ] **Step 4: Commit**

```bash
git add src/main/waveshare_rgb_lcd_port.h
git commit -m "fix: replace RGB LCD pin map for DIS08070H

Sync, DE, PCLK and all 16 data lines differ from the CrowPanel Advance
the map was written for. Frees GPIO38, which is the touch INT pin on
this board."
```

---

### Task 4: Correct the RGB panel timings

**Files:**
- Modify: `src/main/waveshare_rgb_lcd_port.h:33`
- Modify: `src/main/waveshare_rgb_lcd_port.c:78-92`

- [ ] **Step 1: Change the pixel clock**

In `src/main/waveshare_rgb_lcd_port.h`, line 33 currently reads:

```c
#define EXAMPLE_LCD_PIXEL_CLOCK_HZ      (16 * 1000 * 1000)
```

Change it to:

```c
#define EXAMPLE_LCD_PIXEL_CLOCK_HZ      (15 * 1000 * 1000)
```

- [ ] **Step 2: Change the porch and pulse timings**

In `src/main/waveshare_rgb_lcd_port.c`, the `.timings` block currently reads:

```c
            .hsync_pulse_width = 4, // Horizontal sync pulse width
            .hsync_back_porch = 8, // Horizontal back porch
            .hsync_front_porch = 8, // Horizontal front porch
            .vsync_pulse_width = 4, // Vertical sync pulse width
            .vsync_back_porch = 8, // Vertical back porch
            .vsync_front_porch = 8, // Vertical front porch
```

Change it to:

```c
            .hsync_pulse_width = 48, // Horizontal sync pulse width
            .hsync_back_porch = 13, // Horizontal back porch
            .hsync_front_porch = 40, // Horizontal front porch
            .vsync_pulse_width = 31, // Vertical sync pulse width
            .vsync_back_porch = 13, // Vertical back porch
            .vsync_front_porch = 1, // Vertical front porch
```

Leave `.pclk_active_neg = 1` as it is. That already matches this panel's requirement
for an inverted pixel clock.

- [ ] **Step 3: Build, flash, capture**

```bash
. $HOME/esp/esp-idf/export.sh
cd src && idf.py build && idf.py -p /dev/cu.usbserial-2130 flash
python ../tools/capture_serial.py 12 | tail -40
```

- [ ] **Step 4: Verify**

As with Task 3, serial output cannot confirm timings. Confirm the build succeeded and
no new `esp_lcd` errors appear. Visual verification happens at the end of Task 5.

- [ ] **Step 5: Commit**

```bash
git add src/main/waveshare_rgb_lcd_port.h src/main/waveshare_rgb_lcd_port.c
git commit -m "fix: set RGB panel timings for the DIS08070H panel

15MHz pixel clock, hsync 40/48/13, vsync 1/31/13."
```

---

### Task 5: Handle the GT911 address and stop aborting on touch failure

Two separate problems. First, the GT911 sits at `0x5D` or `0x14` depending on the
GPIO 38 level at power-up, and this varies between individual units, but the driver
only ever tries the address it is handed. Second, `ESP_ERROR_CHECK` turns any touch
failure into a reboot loop, which makes every other problem on this board
undiagnosable.

**Files:**
- Modify: `src/main/waveshare_rgb_lcd_port.c:143-165`

**Interfaces:**
- Consumes: `ESP_LCD_TOUCH_IO_I2C_GT911_ADDRESS` (0x5D) and
  `ESP_LCD_TOUCH_IO_I2C_GT911_ADDRESS_BACKUP` (0x14) from
  `components/espressif__esp_lcd_touch_gt911/include/esp_lcd_touch_gt911.h`.
- Produces: `tp_handle`, which is passed to `lvgl_port_init()`. It may now be `NULL`.
  This is safe and has been verified: `src/main/lvgl_port.c:514` guards the input-device
  setup with `if (tp_handle)`, so a `NULL` handle simply registers no input device. Do
  not remove that guard — `indev_init()` at `lvgl_port.c:455` asserts on a `NULL` handle.

- [ ] **Step 1: Try both addresses and degrade gracefully**

In `src/main/waveshare_rgb_lcd_port.c`, replace this block:

```c
    esp_lcd_panel_io_handle_t tp_io_handle = NULL; // Declare a handle for touch panel I/O
    const esp_lcd_panel_io_i2c_config_t tp_io_config = ESP_LCD_TOUCH_IO_I2C_GT911_CONFIG(); // Configure I2C for GT911 touch controller

    ESP_LOGI(TAG, "Initialize I2C panel IO"); // Log I2C panel I/O initialization
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_i2c((esp_lcd_i2c_bus_handle_t)I2C_MASTER_NUM, &tp_io_config, &tp_io_handle)); // Create new I2C panel I/O

    ESP_LOGI(TAG, "Initialize touch controller GT911"); // Log touch controller initialization
    const esp_lcd_touch_config_t tp_cfg = {
        .x_max = EXAMPLE_LCD_H_RES, // Set maximum X coordinate
        .y_max = EXAMPLE_LCD_V_RES, // Set maximum Y coordinate
        .rst_gpio_num = EXAMPLE_PIN_NUM_TOUCH_RST, // GPIO number for reset
        .int_gpio_num = EXAMPLE_PIN_NUM_TOUCH_INT, // GPIO number for interrupt
        .levels = {
            .reset = 0, // Reset level
            .interrupt = 0, // Interrupt level
        },
        .flags = {
            .swap_xy = 0, // No swap of X and Y
            .mirror_x = 0, // No mirroring of X
            .mirror_y = 0, // No mirroring of Y
        },
    };
    ESP_ERROR_CHECK(esp_lcd_touch_new_i2c_gt911(tp_io_handle, &tp_cfg, &tp_handle)); // Create new I2C GT911 touch controller
```

with:

```c
    const esp_lcd_touch_config_t tp_cfg = {
        .x_max = EXAMPLE_LCD_H_RES, // Set maximum X coordinate
        .y_max = EXAMPLE_LCD_V_RES, // Set maximum Y coordinate
        .rst_gpio_num = EXAMPLE_PIN_NUM_TOUCH_RST, // GPIO number for reset
        .int_gpio_num = EXAMPLE_PIN_NUM_TOUCH_INT, // GPIO number for interrupt
        .levels = {
            .reset = 0, // Reset level
            .interrupt = 0, // Interrupt level
        },
        .flags = {
            .swap_xy = 0, // No swap of X and Y
            .mirror_x = 0, // No mirroring of X
            .mirror_y = 0, // No mirroring of Y
        },
    };

    /* The GT911 latches its address from the INT pin at power-up, and which one it
     * lands on varies between individual DIS08070H units. Try both. */
    const uint8_t gt911_addrs[] = {
        ESP_LCD_TOUCH_IO_I2C_GT911_ADDRESS,
        ESP_LCD_TOUCH_IO_I2C_GT911_ADDRESS_BACKUP,
    };

    for (size_t i = 0; i < sizeof(gt911_addrs) / sizeof(gt911_addrs[0]); i++) {
        esp_lcd_panel_io_handle_t tp_io_handle = NULL;
        esp_lcd_panel_io_i2c_config_t tp_io_config = ESP_LCD_TOUCH_IO_I2C_GT911_CONFIG();
        tp_io_config.dev_addr = gt911_addrs[i];

        ESP_LOGI(TAG, "Trying GT911 at address 0x%02X", gt911_addrs[i]);
        esp_err_t err = esp_lcd_new_panel_io_i2c((esp_lcd_i2c_bus_handle_t)I2C_MASTER_NUM,
                                                 &tp_io_config, &tp_io_handle);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "Panel IO for 0x%02X failed: %s", gt911_addrs[i], esp_err_to_name(err));
            continue;
        }

        err = esp_lcd_touch_new_i2c_gt911(tp_io_handle, &tp_cfg, &tp_handle);
        if (err == ESP_OK) {
            ESP_LOGI(TAG, "GT911 initialized at address 0x%02X", gt911_addrs[i]);
            break;
        }

        ESP_LOGW(TAG, "GT911 not at 0x%02X: %s", gt911_addrs[i], esp_err_to_name(err));
        esp_lcd_panel_io_del(tp_io_handle);
        tp_handle = NULL;
    }

    if (tp_handle == NULL) {
        /* Deliberately not fatal. Touch failure must not cost us the display, the
         * radar, or the serial log needed to diagnose it. */
        ESP_LOGE(TAG, "GT911 not found at 0x%02X or 0x%02X - continuing without touch",
                 ESP_LCD_TOUCH_IO_I2C_GT911_ADDRESS,
                 ESP_LCD_TOUCH_IO_I2C_GT911_ADDRESS_BACKUP);
    }
```

Note the removal of the outer `tp_io_handle` declaration — it is now scoped to the
loop. Check that no later code in the function references `tp_io_handle`; as of this
plan, none does.

- [ ] **Step 2: Build, flash, capture**

```bash
. $HOME/esp/esp-idf/export.sh
cd src && idf.py build && idf.py -p /dev/cu.usbserial-2130 flash
python ../tools/capture_serial.py 15 | grep -E "GT911|I2CSCAN|Rebooting"
```

- [ ] **Step 3: Verify the reboot loop is gone**

Expected:

```
I (xxxx) Home Clock: Trying GT911 at address 0x5D
I (xxxx) Home Clock: GT911 initialized at address 0x5D
```

(or `0x14`, matching whichever address the Task 1 scan reported).

The critical assertion is that **`Rebooting...` no longer appears** and the log
continues past LCD init into WiFi setup. The boot loop is fixed when the log reaches
`main_task: Calling app_main()` and then proceeds to application logging without
resetting.

- [ ] **Step 4: Verify the display visually**

**This step requires looking at the panel.** With touch initialized and LVGL running,
the UI should now render. Check and report:

1. Is anything drawn at all? If the panel is lit but blank, the RGB pin map or timings
   from Tasks 3-4 are wrong.
2. Is the image stable, or does it tear, shear, or roll? Rolling or shearing means the
   timings in Task 4 need adjustment.
3. Are the colors correct? Specific failure modes and their causes:
   - **Red and blue swapped** — the blue and red groups are reversed. Swap
     `DATA0`-`DATA4` with `DATA11`-`DATA15` in `waveshare_rgb_lcd_port.h`.
   - **Colors roughly right but banded or posterized** — bit order within a color
     group is reversed. Reverse the pin order within that group.
   - **A photographic negative** — this corresponds to the `invert_colors: true`
     setting in the ESPHome config for this board. The ESP-IDF RGB panel driver has no
     equivalent flag. **Stop and report this rather than guessing**; it needs a
     separate decision about where to invert.
4. Does touch respond, and are the coordinates correctly oriented? If touch registers
   in a mirrored or rotated position, adjust `swap_xy`, `mirror_x`, and `mirror_y` in
   `tp_cfg`.

- [ ] **Step 5: Commit**

```bash
git add src/main/waveshare_rgb_lcd_port.c
git commit -m "fix: probe both GT911 addresses and survive touch failure

The GT911 latches 0x5D or 0x14 from the INT pin at power-up and this
varies between individual units, so try both. Touch failure no longer
aborts: losing the display and the serial log to a reboot loop made
every other problem on this board undiagnosable."
```

---

### Task 6: Remove the diagnostic scan and document the board

**Files:**
- Modify: `src/main/main.c` (remove `i2c_bus_scan` and both call sites)
- Modify: `README.md`

- [ ] **Step 1: Remove the diagnostic**

Delete the `i2c_bus_scan` function from `src/main/main.c` — the whole block marked
`/* TEMP DIAGNOSTIC - remove after debugging GT911 init failure */` — and both of its
call sites in `app_main`, each also marked with that comment.

Leave `i2c_scan_address()` in place. It predates this work and may be referenced
elsewhere; confirm with `grep -rn i2c_scan_address src/main/` before removing anything
else.

- [ ] **Step 2: Correct the hardware section of the README**

`README.md` currently claims the project targets:

```markdown
- Elecrow CrowPanel Advance 7" ESP32-S3 HMI Display **V1.2**
```

That is a different product from the board this firmware now targets. Replace that
line with:

```markdown
- Elecrow CrowPanel 7.0" ESP32-S3 HMI Display, SKU **DIS08070H** (ESP32-S3-WROOM-1-N4R8, 4MB flash, 8MB PSRAM)
```

Also add this note directly beneath the Hardware list:

```markdown
> **Board compatibility:** This firmware targets the CrowPanel 7.0" HMI (DIS08070H).
> It is *not* compatible with the CrowPanel Advance 7.0, which is a different product
> with 16MB of flash and a different pin map. Flashing this build to an Advance, or an
> Advance build to this board, results in a boot loop.
```

- [ ] **Step 3: Final build, flash, and full verification**

```bash
. $HOME/esp/esp-idf/export.sh
cd src && idf.py fullclean && idf.py build && idf.py -p /dev/cu.usbserial-2130 flash
python ../tools/capture_serial.py 20 | tail -60
```

Confirm all of the following:

- The log contains no `I2CSCAN` lines.
- The log contains `GT911 initialized at address 0x..`.
- The log contains no `Rebooting...` and no `abort() was called`.
- The log proceeds into WiFi setup.
- The UI renders on the panel and touch responds.

- [ ] **Step 4: Commit**

```bash
git add src/main/main.c README.md
git commit -m "chore: remove I2C debug scan and document DIS08070H target

The README described the CrowPanel Advance 7.0, a different product with
16MB flash and a different pin map, which is what sent this firmware into
a boot loop on DIS08070H hardware."
```

---

### Task 7 (optional): Rename the board support files

Purely cosmetic and carries merge-conflict risk against upstream. Skip unless
specifically wanted.

`main/waveshare_rgb_lcd_port.c` and `.h` are named for Waveshare, but this is Elecrow
hardware and the code no longer resembles the Waveshare original.

- [ ] **Step 1: Rename and update references**

```bash
cd src/main
git mv waveshare_rgb_lcd_port.c crowpanel_rgb_lcd_port.c
git mv waveshare_rgb_lcd_port.h crowpanel_rgb_lcd_port.h
cd ../..
grep -rln "waveshare_rgb_lcd_port" src/main | xargs sed -i '' 's/waveshare_rgb_lcd_port/crowpanel_rgb_lcd_port/g'
sed -i '' 's/"waveshare_rgb_lcd_port.c"/"crowpanel_rgb_lcd_port.c"/' src/main/CMakeLists.txt
```

Note that the function names `waveshare_esp32_s3_rgb_lcd_init()`,
`waveshare_esp32_s3_touch_reset()` and `wavesahre_rgb_lcd_bl_on()` (the last is
misspelled in the original) are left alone here. Renaming them touches `main.c` call
sites too; do it only if asked.

- [ ] **Step 2: Build and verify**

```bash
. $HOME/esp/esp-idf/export.sh
cd src && idf.py fullclean && idf.py build
```

Expected: `Project build complete`.

- [ ] **Step 3: Commit**

```bash
git add -A src/main
git commit -m "chore: rename board support files from waveshare to crowpanel"
```

---

## Known uncertainties

Flagged deliberately. Do not paper over these — report them.

1. **RGB data line bit order within each color group.** The source lists red as
   `14, 21, 47, 48, 45`, green as `9, 46, 3, 8, 16, 1`, blue as `15, 7, 6, 5, 4`, but
   does not state whether each list is LSB-first or MSB-first. This plan assumes
   LSB-first. A wrong assumption produces banded or posterized color, not a blank
   screen. Remedy is in Task 5 Step 4.
2. **`invert_colors`.** The ESPHome config for this board sets `invert_colors: true`.
   The ESP-IDF RGB panel driver has no equivalent, and this plan does not attempt to
   translate it. If the panel shows a photographic negative, stop and report.
3. **Whether the display worked before this port.** Unknown. The firmware has never
   booted far enough on this hardware to draw anything, so Tasks 3 and 4 are corrections
   made from documentation, not from an observed-broken display. They are unverified
   until Task 5 Step 4.
