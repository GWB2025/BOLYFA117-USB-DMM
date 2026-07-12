
# BOLYFA 117 USB Serial Protocol Analysis
## Based on PaulZC's DMM_Data_Logger and reverse-engineering

---

## 1. Physical Layer
- **USB Bridge**: CH340/CH341 (QinHeng Electronics HL-340)
- **Vendor:Product ID**: 1a86:7523
- **Serial Parameters**: 2400 baud, 8 data bits, No parity, 1 stop bit (8N1)
- **Windows Driver**: CH340 driver (built into Windows 10/11, or download from WCH)
- **Port**: Appears as COM port (e.g., COM3, COM4)

---

## 2. Packet Structure (22 bytes)

The meter continuously streams 22-byte packets. The packet header is:

| Byte Index | Value | Meaning |
|------------|-------|---------|
| 0 | 0xAA | Header byte 1 |
| 1 | 0x55 | Header byte 2 |
| 2 | 0x52 | Header byte 3 ('R') |
| 3 | 0x24 | Header byte 4 ('$') |
| 4 | 0x01 | Header byte 5 |
| 5 | 0x10 | Header byte 6 |
| 6-9 | varies | Digit 4, 3, 2, 1 (7-segment + DP) |
| 10 | varies | Sign, AC/DC, Bar legend, Diode, Continuity |
| 11-18 | varies | Bar graph segments (8 bytes) |
| 19 | varies | MAX, MIN, %, hFE, USB flags |
| 20 | varies | Units: n, F, oF, oC, u |
| 21 | varies | Units: k, M, m, u, Hz, R, V, A |

---

## 3. Seven-Segment Display Encoding

Each digit is encoded as 7-segment bits. Bit order (msb to lsb): **DP E G F D C B A**

| Digit | 7-segment value (0x7F mask) |
|-------|----------------------------|
| 0 | 0x5F |
| 1 | 0x06 |
| 2 | 0x6B |
| 3 | 0x2F |
| 4 | 0x36 |
| 5 | 0x3D |
| 6 | 0x7D |
| 7 | 0x07 |
| 8 | 0x7F |
| 9 | 0x3F |
| E | 0x79 |
| L | 0x58 |

**Decimal Point**: Bit 7 (0x80) set = DP active for that digit.

Digit positions (from left to right on display):
- Byte 9 = Digit 1 (leftmost)
- Byte 8 = Digit 2
- Byte 7 = Digit 3
- Byte 6 = Digit 4 (rightmost)

---

## 4. Units Encoding

### Byte 21 (primary units):
| Bit | Mask | Unit |
|-----|------|------|
| 7 | 0x80 | Hz |
| 6 | 0x40 | R (Ohms Ω) |
| 5 | 0x20 | k (kilo) |
| 4 | 0x10 | M (Mega) |
| 3 | 0x08 | V (Volts) |
| 2 | 0x04 | A (Amps) |
| 1 | 0x02 | m (milli) |
| 0 | 0x01 | u (micro) |

### Byte 20 (secondary units):
| Bit | Mask | Unit |
|-----|------|------|
| 7 | 0x80 | F (Farads) |
| 6 | 0x40 | n (nano) |
| 5 | 0x20 | u (micro) |
| 2 | 0x02 | oF (°F) |
| 1 | 0x01 | oC (°C) |

### Byte 19 (tertiary):
| Bit | Mask | Unit |
|-----|------|------|
| 6 | 0x40 | hFE |
| 5 | 0x20 | % |

---

## 5. Mode & Status Flags

### Byte 10:
| Bit | Mask | Meaning |
|-----|------|---------|
| 7 | 0x80 | (unused in script) |
| 6 | 0x40 | CONT (Continuity beep) |
| 5 | 0x20 | Bar graph legend |
| 4 | 0x10 | (unused in script) |
| 3 | 0x08 | Negative sign (-) |
| 2 | 0x04 | DC mode |
| 1 | 0x02 | AC mode |
| 0 | 0x01 | DIODE mode |

### Byte 19:
| Bit | Mask | Meaning |
|-----|------|---------|
| 4 | 0x10 | (unused) |
| 3 | 0x08 | MIN |
| 2 | 0x04 | MIN indicator (dash?) |
| 1 | 0x02 | MAX |
| 0 | 0x01 | USB flag |

### Byte 18:
| Bit | Mask | Meaning |
|-----|------|---------|
| 7 | 0x80 | REL (Relative mode) |
| 6 | 0x40 | (unused) |
| 5 | 0x20 | AUTO (Auto-ranging) |
| 4-0 | 0x1F | Lower 5 bits = part of bar graph |

---

## 6. Bar Graph

The bar graph has a maximum of 60 segments.
- Bytes 11-17: each bit set = one bar segment
- Byte 18 lower 4 bits (0x0F): remaining bar segments
- Total bars = count of set bits in bytes 11-18 (with byte 18 masked to 0x0F)

---

## 7. Example Decoding

From the original repo:
- **15.2 Ω**: The display shows "15.2" with Ω symbol
  - Digits: '1', '5', '.', '2'
  - Units: R (Ohms)

- **25°C**: The display shows "25" with °C symbol
  - Digits: '2', '5'
  - Units: oC (°C)

The raw serial output `UR$=k` etc. corresponds to the 22-byte packet starting with the header.

---

## 8. Windows 11 Notes

1. **Driver**: Windows 11 includes the CH340 driver automatically. If not, download from WCH official site.
2. **Port**: Check Device Manager → Ports (COM & LPT) to find the COM port number.
3. **Python**: Use `pyserial` library for serial communication.
4. **Permissions**: No special permissions needed on Windows (unlike Linux dialout group).
