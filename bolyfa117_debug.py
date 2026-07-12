#!/usr/bin/env python3
"""
BOLYFA 117 Serial Diagnostic Tool
=================================
Use this to see exactly what bytes the meter is sending.
Run this first, then we can fix the main logger.

Usage:
    python bolyfa117_debug.py COM4
"""

import serial
import serial.tools.list_ports
import sys
import time


def list_ports():
    print("Available serial ports:")
    for p in serial.tools.list_ports.comports():
        marker = "  <-- LIKELY BOLYFA 117" if "CH340" in p.description or "CH341" in p.description else ""
        print(f"  {p.device:12} - {p.description}{marker}")
    print()


def sniff_raw(port, duration=10):
    """Read raw bytes and print them in hex + ASCII."""
    try:
        ser = serial.Serial(port, 2400, timeout=1.0)
    except serial.SerialException as e:
        print(f"[ERROR] Could not open {port}: {e}")
        return

    print(f"[OK] Opened {port} at 2400 baud")
    print(f"[INFO] Sniffing for {duration} seconds...")
    print("[INFO] Set your meter to any reading (e.g., 25C) and wait.\n")

    # Try different DTR/RTS settings to see if meter needs them
    configs = [
        ("Default (DTR/RTS unchanged)", lambda: None),
        ("DTR=True, RTS=True", lambda: (ser.setDTR(True), ser.setRTS(True))),
        ("DTR=False, RTS=False", lambda: (ser.setDTR(False), ser.setRTS(False))),
    ]

    all_bytes = bytearray()

    for config_name, config_fn in configs:
        config_fn()
        print(f"\n--- Config: {config_name} ---")
        ser.reset_input_buffer()
        time.sleep(0.5)

        chunk_start = time.time()
        while time.time() - chunk_start < 3.0:
            data = ser.read(64)  # Read up to 64 bytes at once
            if data:
                all_bytes.extend(data)
                # Print hex dump
                for i in range(0, len(data), 16):
                    chunk = data[i:i+16]
                    hex_part = ' '.join(f'{b:02X}' for b in chunk)
                    ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
                    print(f"  {hex_part:<48} |{ascii_part}|")
            else:
                print("  (no data in last 1s)")

    ser.close()

    # Analysis
    print(f"\n{'='*60}")
    print(f"TOTAL BYTES READ: {len(all_bytes)}")

    if len(all_bytes) == 0:
        print("[WARNING] No bytes received at all.")
        print("  - Check the USB cable is firmly connected")
        print("  - Check the meter is ON and showing a reading")
        print("  - Try unplugging and re-plugging the USB cable")
        return

    # Look for the known header pattern
    header = bytes([0xAA, 0x55, 0x52, 0x24, 0x01, 0x10])
    found = 0
    for i in range(len(all_bytes) - 6):
        if all_bytes[i:i+6] == header:
            found += 1
            if found <= 3:
                print(f"\n[FOUND] Header at offset {i}")
                packet = all_bytes[i:i+22]
                print(f"  Full packet (22 bytes): {' '.join(f'{b:02X}' for b in packet)}")
                # Try to decode digits
                seg_map = {
                    0x5F: '0', 0x06: '1', 0x6B: '2', 0x2F: '3',
                    0x36: '4', 0x3D: '5', 0x7D: '6', 0x07: '7',
                    0x7F: '8', 0x3F: '9', 0x79: 'E', 0x58: 'L'
                }
                digits = []
                for idx in [9, 8, 7, 6]:
                    if idx < len(packet):
                        d = seg_map.get(packet[idx] & 0x7F, '?')
                        dp = '.' if packet[idx] & 0x80 else ''
                        digits.append(d + dp)
                print(f"  Decoded digits: {digits}")
                # Units
                u = []
                b21 = packet[21] if len(packet) > 21 else 0
                b20 = packet[20] if len(packet) > 20 else 0
                b19 = packet[19] if len(packet) > 19 else 0
                if b21 & 0x20: u.append('k')
                if b21 & 0x10: u.append('M')
                if b21 & 0x02: u.append('m')
                if b21 & 0x01: u.append('u')
                if b21 & 0x80: u.append('Hz')
                if b21 & 0x40: u.append('Ohm')
                if b21 & 0x08: u.append('V')
                if b21 & 0x04: u.append('A')
                if b20 & 0x20: u.append('u')
                if b20 & 0x40: u.append('n')
                if b20 & 0x80: u.append('F')
                if b20 & 0x02: u.append('degF')
                if b20 & 0x01: u.append('degC')
                if b19 & 0x20: u.append('%')
                if b19 & 0x40: u.append('hFE')
                print(f"  Units: {''.join(u) if u else 'none'}")
                # Sign
                b10 = packet[10] if len(packet) > 10 else 0
                sign = '-' if b10 & 0x08 else ''
                print(f"  Sign: {sign}")
                # Mode
                modes = []
                if b10 & 0x04: modes.append('DC')
                if b10 & 0x02: modes.append('AC')
                if b10 & 0x01: modes.append('DIODE')
                if b10 & 0x40: modes.append('CONT')
                print(f"  Modes: {modes if modes else 'none'}")

    print(f"\nTotal headers found: {found}")
    if found == 0:
        print("[WARNING] No expected header found.")
        print("  The meter might use a different protocol, or the data is garbled.")
        print("  Look at the hex dump above for repeating patterns.")

    # Byte frequency analysis
    print(f"\n--- Byte Frequency (top 10) ---")
    from collections import Counter
    freq = Counter(all_bytes)
    for byte, count in freq.most_common(10):
        char = chr(byte) if 32 <= byte < 127 else '.'
        print(f"  0x{byte:02X} ({char}): {count} times")


def main():
    if len(sys.argv) < 2:
        print("Usage: python bolyfa117_debug.py <COM_PORT>")
        print("Example: python bolyfa117_debug.py COM4")
        print()
        list_ports()
        sys.exit(1)

    port = sys.argv[1]
    # Auto-fix bare numbers
    if port.isdigit():
        port = f"COM{port}"
        print(f"[INFO] Auto-corrected to {port}")

    sniff_raw(port)


if __name__ == '__main__':
    main()
