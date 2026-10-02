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
