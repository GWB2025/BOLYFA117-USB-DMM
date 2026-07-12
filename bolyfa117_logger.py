#!/usr/bin/env python3
"""
BOLYFA 117 USB Digital Multimeter Data Logger v3
==================================================
All-features edition: auto-detect, auto-reconnect, alerts, config,
smoothing, mobile dashboard, InfluxDB/MQTT export.

Usage:
    python bolyfa117_logger.py --mode dashboard
    python bolyfa117_logger.py --mode csv --alert-max 30.0
    python bolyfa117_logger.py --config bolyfa117.json

Requirements:
    pip install pyserial
    pip install paho-mqtt        # (optional, for MQTT)
"""

import serial
import serial.tools.list_ports
import time
import sys
import argparse
import csv
import json
import threading
import socket
import os
from datetime import datetime
from pathlib import Path
from collections import deque

DEFAULT_CONFIG = {
    "port": None,
    "mode": "live",
    "baudrate": 2400,
    "output_dir": ".",
    "web_port": 8080,
    "smoothing_window": 5,
    "alert_min": None,
    "alert_max": None,
    "alert_beep": False,
    "influxdb_url": None,
    "influxdb_token": None,
    "influxdb_bucket": "dmm",
    "influxdb_org": "-",
    "mqtt_broker": None,
    "mqtt_port": 1883,
    "mqtt_topic": "bolyfa117/data",
    "mqtt_user": None,
    "mqtt_pass": None,
    "auto_reconnect": True,
    "reconnect_delay": 3
}


def load_config(path):
    """Load JSON config, merging with defaults."""
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(path):
        with open(path, "r") as f:
            user_cfg = json.load(f)
        config.update(user_cfg)
        print(f"[OK] Loaded config from {path}")
    else:
        print(f"[INFO] No config file at {path}, using defaults")
    return config


def save_config_template(path):
    """Write a template config file."""
    with open(path, "w") as f:
        json.dump(DEFAULT_CONFIG, f, indent=2)
    print(f"[OK] Wrote config template to {path}")

class BOLYFA117Decoder:
    HEADER = [0xAA, 0x55, 0x52, 0x24, 0x01, 0x10]
    PACKET_SIZE = 22

    SEGMENTS = {
        0x5F: "0", 0x06: "1", 0x6B: "2", 0x2F: "3",
        0x36: "4", 0x3D: "5", 0x7D: "6", 0x07: "7",
        0x7F: "8", 0x3F: "9", 0x79: "E", 0x58: "L",
    }

    def __init__(self):
        self.packet = [0] * self.PACKET_SIZE
        self.value_str = ""
        self.units_str = ""
        self.mode_str = ""
        self.bar_count = 0
        self.flags = []
        self.timestamp = ""

    def decode_digit(self, byte_val):
        return self.SEGMENTS.get(byte_val & 0x7F, "")

    def has_dp(self, byte_val):
        return bool(byte_val & 0x80)

    def count_bits(self, byte_val):
        return bin(byte_val).count("1")

    def process_packet(self, packet_bytes):
        if len(packet_bytes) != self.PACKET_SIZE:
            return None
        p = packet_bytes
        self.packet = p

        digits = []
        d = self.decode_digit(p[9])
        if d:
            digits.append(d)
        for idx in [8, 7, 6]:
            if self.has_dp(p[idx]):
                digits.append(".")
            d = self.decode_digit(p[idx])
            if d:
                digits.append(d)

        sign = "-" if (p[10] & 0x08) else ""
        self.value_str = sign + "".join(digits)

        units = []
        b21, b20, b19 = p[21], p[20], p[19]
        if b21 & 0x20: units.append("k")
        if b21 & 0x10: units.append("M")
        if b21 & 0x02: units.append("m")
        if b21 & 0x01: units.append("μ")
        if b21 & 0x80: units.append("Hz")
        if b21 & 0x40: units.append("Ω")
        if b21 & 0x08: units.append("V")
        if b21 & 0x04: units.append("A")
        if b20 & 0x20: units.append("μ")
        if b20 & 0x40: units.append("n")
        if b20 & 0x80: units.append("F")
        if b20 & 0x02: units.append("°F")
        if b20 & 0x01: units.append("°C")
        if b19 & 0x20: units.append("%")
        if b19 & 0x40: units.append("hFE")
        self.units_str = "".join(units)

        modes = []
        if p[10] & 0x04: modes.append("DC")
        if p[10] & 0x02: modes.append("AC")
        if p[10] & 0x01: modes.append("DIODE")
        if p[10] & 0x40: modes.append("CONT")
        self.mode_str = "+".join(modes) if modes else ""

        flags = []
        if p[19] & 0x01: flags.append("USB")
        if p[18] & 0x20: flags.append("AUTO")
        if p[18] & 0x80: flags.append("REL")
        if p[19] & 0x02: flags.append("MAX")
        if p[19] & 0x08: flags.append("MIN")
        if p[10] & 0x20: flags.append("BAR")
        self.flags = flags

        bars = 0
        for i in range(11, 18):
            bars += self.count_bits(p[i])
        bars += self.count_bits(p[18] & 0x0F)
        self.bar_count = bars

        self.timestamp = datetime.now().isoformat()

        return {
            "timestamp": self.timestamp,
            "value": self.value_str,
            "units": self.units_str,
            "mode": self.mode_str,
            "flags": ",".join(self.flags),
            "bar": self.bar_count,
            "raw": " ".join(f"{b:02X}" for b in p)
        }

class BOLYFA117Reader:
    def __init__(self, port, baudrate=2400, auto_reconnect=True,
                 reconnect_delay=3, smoothing_window=1,
                 alert_min=None, alert_max=None, alert_beep=False):
        self.port = port
        self.baudrate = baudrate
        self.auto_reconnect = auto_reconnect
        self.reconnect_delay = reconnect_delay
        self.smoothing_window = smoothing_window
        self.alert_min = alert_min
        self.alert_max = alert_max
        self.alert_beep = alert_beep
        self.ser = None
        self.decoder = BOLYFA117Decoder()
        self.buffer = bytearray()
        self.packets_found = 0
        self.bytes_read = 0
        self.value_history = deque(maxlen=smoothing_window)
        self.alert_state = False
        self._stop = False

    def open(self):
        try:
            self.ser = serial.Serial(
                port=self.port, baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE, timeout=0.5
            )
            self.ser.setDTR(True)
            self.ser.setRTS(True)
            time.sleep(0.2)
            self.ser.reset_input_buffer()
            print(f"[OK] Opened {self.port} at {self.baudrate} baud")
            return True
        except serial.SerialException as e:
            print(f"[ERROR] Could not open {self.port}: {e}")
            return False

    def close(self):
        self._stop = True
        if self.ser and self.ser.is_open:
            self.ser.close()
            print(f"[OK] Closed {self.port}")

    def _read_raw(self):
        if not self.ser or not self.ser.is_open:
            return b""
        try:
            available = self.ser.in_waiting
            if available > 0:
                return self.ser.read(min(available, 64))
        except (serial.SerialException, OSError):
            if self.auto_reconnect:
                print(f"[WARN] Port {self.port} disconnected. Reconnecting in {self.reconnect_delay}s...")
                self._reconnect()
            else:
                raise
        return b""

    def _reconnect(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        time.sleep(self.reconnect_delay)
        for attempt in range(10):
            if self._stop:
                return
            try:
                if self.open():
                    print(f"[OK] Reconnected to {self.port}")
                    return
            except Exception as e:
                print(f"[RETRY {attempt+1}/10] {e}")
            time.sleep(self.reconnect_delay)
        print(f"[ERROR] Failed to reconnect after 10 attempts.")

    def read_packet(self):
        data = self._read_raw()
        if data:
            self.buffer.extend(data)
            self.bytes_read += len(data)
        if len(self.buffer) > 200:
            self.buffer = self.buffer[-100:]
        header = bytes(BOLYFA117Decoder.HEADER)
        ps = BOLYFA117Decoder.PACKET_SIZE
        for i in range(len(self.buffer) - ps + 1):
            if self.buffer[i:i+6] == header:
                packet = bytes(self.buffer[i:i+ps])
                self.buffer = self.buffer[i+ps:]
                self.packets_found += 1
                result = self.decoder.process_packet(packet)
                if result:
                    result = self._apply_smoothing(result)
                    self._check_alerts(result)
                    return result
        return None

    def _apply_smoothing(self, data):
        try:
            val = float(data["value"])
            self.value_history.append(val)
            smoothed = sum(self.value_history) / len(self.value_history)
            data["value_raw"] = data["value"]
            data["value"] = str(round(smoothed, 3)).rstrip("0").rstrip(".")
        except ValueError:
            pass
        return data

    def _check_alerts(self, data):
        try:
            val = float(data["value"])
            triggered = False
            msg = None
            if self.alert_min is not None and val < self.alert_min:
                triggered = True
                msg = f"Value {val} below minimum {self.alert_min}"
            elif self.alert_max is not None and val > self.alert_max:
                triggered = True
                msg = f"Value {val} above maximum {self.alert_max}"
            if triggered and not self.alert_state:
                print(f"\n[ALERT] {msg}")
                if self.alert_beep:
                    print("\a", end="")
                self.alert_state = True
            elif not triggered and self.alert_state:
                print(f"[OK] Value back in range: {val}")
                self.alert_state = False
            data["alert"] = triggered
            data["alert_msg"] = msg
        except ValueError:
            data["alert"] = False
            data["alert_msg"] = None

class DataExporter:
    def __init__(self, config):
        self.config = config
        self.influx_queue = deque(maxlen=1000)
        self.mqtt_queue = deque(maxlen=1000)
        self.mqtt_client = None
        self._stop = False
        self._start_threads()

    def _start_threads(self):
        if self.config.get("influxdb_url"):
            t = threading.Thread(target=self._influx_worker, daemon=True)
            t.start()
        if self.config.get("mqtt_broker"):
            self._init_mqtt()
            t = threading.Thread(target=self._mqtt_worker, daemon=True)
            t.start()

    def push(self, data):
        if self.config.get("influxdb_url"):
            self.influx_queue.append(data)
        if self.config.get("mqtt_broker"):
            self.mqtt_queue.append(data)

    def stop(self):
        self._stop = True

    def _influx_worker(self):
        import urllib.request
        url = self.config["influxdb_url"] + "/api/v2/write"
        token = self.config.get("influxdb_token", "")
        org = self.config.get("influxdb_org", "-")
        bucket = self.config.get("influxdb_bucket", "dmm")
        headers = {"Authorization": f"Token {token}", "Content-Type": "text/plain; charset=utf-8"}
        while not self._stop:
            if not self.influx_queue:
                time.sleep(1)
                continue
            batch = []
            while self.influx_queue and len(batch) < 10:
                d = self.influx_queue.popleft()
                try:
                    val = float(d["value"])
                    ts = datetime.fromisoformat(d["timestamp"]).timestamp()
                    line = f"dmm,units={d['units']} value={val} {int(ts*1e9)}"
                    batch.append(line)
                except (ValueError, KeyError):
                    continue
            if batch:
                payload = "\n".join(batch)
                full_url = f"{url}?org={org}&bucket={bucket}&precision=ns"
                try:
                    req = urllib.request.Request(full_url, data=payload.encode("utf-8"), headers=headers, method="POST")
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        if resp.status != 204:
                            print(f"[INFLUX WARN] HTTP {resp.status}")
                except Exception as e:
                    print(f"[INFLUX ERROR] {e}")

    def _init_mqtt(self):
        try:
            import paho.mqtt.client as mqtt
            self._mqtt_lib = mqtt
            self.mqtt_client = mqtt.Client()
            user = self.config.get("mqtt_user")
            pw = self.config.get("mqtt_pass")
            if user and pw:
                self.mqtt_client.username_pw_set(user, pw)
            self.mqtt_client.connect(self.config["mqtt_broker"], self.config.get("mqtt_port", 1883))
            self.mqtt_client.loop_start()
            print(f"[OK] MQTT connected to {self.config['mqtt_broker']}")
        except ImportError:
            print("[ERROR] paho-mqtt not installed. Run: pip install paho-mqtt")
            self.mqtt_client = None
        except Exception as e:
            print(f"[MQTT ERROR] {e}")
            self.mqtt_client = None

    def _mqtt_worker(self):
        while not self._stop:
            if not self.mqtt_queue or not self.mqtt_client:
                time.sleep(0.5)
                continue
            d = self.mqtt_queue.popleft()
            try:
                payload = json.dumps({"timestamp": d["timestamp"], "value": d["value"], "units": d["units"], "mode": d["mode"], "flags": d["flags"], "bar": d["bar"]})
                self.mqtt_client.publish(self.config.get("mqtt_topic", "bolyfa117/data"), payload, qos=0)
            except Exception as e:
                print(f"[MQTT PUB ERROR] {e}")

def run_csv_logger(port, config, exporter):
    reader = BOLYFA117Reader(
        port,
        baudrate=config.get("baudrate", 2400),
        auto_reconnect=config.get("auto_reconnect", True),
        reconnect_delay=config.get("reconnect_delay", 3),
        smoothing_window=config.get("smoothing_window", 1),
        alert_min=config.get("alert_min"),
        alert_max=config.get("alert_max"),
        alert_beep=config.get("alert_beep", False)
    )
    if not reader.open():
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = Path(config.get("output_dir", ".")) / f"BOLYFA117_Log_{ts}.csv"
    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Timestamp", "Value", "Units", "Mode", "Flags", "Bar", "Alert", "Raw"])
        print(f"\n[LOG] Writing to: {filename}")
        print("[INFO] Press CTRL+C to stop logging\n")
        print(f"{"Timestamp":<26} {"Value":>8} {"Units":<8} {"Mode":<8} {"Flags":<12} {"Bar":<4} {"Alert":<5}")
        print("-" * 75)
        last_status = time.time()
        try:
            while True:
                data = reader.read_packet()
                if data:
                    writer.writerow([data["timestamp"], data["value"], data["units"], data["mode"], data["flags"], data["bar"], data.get("alert", False), data["raw"]])
                    alert_mark = "***" if data.get("alert") else ""
                    print(f"{data["timestamp"]:<26} {data["value"]:>8} {data["units"]:<8} {data["mode"]:<8} {data["flags"]:<12} {data["bar"]:<4} {alert_mark:<5}")
                    if exporter:
                        exporter.push(data)
                else:
                    if time.time() - last_status > 5:
                        print(f"[INFO] Waiting... bytes={reader.bytes_read} packets={reader.packets_found}")
                        last_status = time.time()
        except KeyboardInterrupt:
            print("\n[INFO] Stopping logger...")
        finally:
            reader.close()
            if exporter:
                exporter.stop()
            print(f"[OK] Saved to {filename}")

def run_live(port, config, exporter):
    reader = BOLYFA117Reader(
        port,
        baudrate=config.get("baudrate", 2400),
        auto_reconnect=config.get("auto_reconnect", True),
        reconnect_delay=config.get("reconnect_delay", 3),
        smoothing_window=config.get("smoothing_window", 1),
        alert_min=config.get("alert_min"),
        alert_max=config.get("alert_max"),
        alert_beep=config.get("alert_beep", False)
    )
    if not reader.open():
        return
    print("\n[INFO] Press CTRL+C to stop")
    print("[INFO] Auto-reconnect enabled\n")
    print(f"{"Value":>10} {"Units":<8} {"Mode":<8} {"Flags":<15} {"Bar":<4} {"Alert":<5}")
    print("-" * 55)
    last_update = time.time()
    try:
        while True:
            data = reader.read_packet()
            if data:
                alert_mark = "***" if data.get("alert") else ""
                print(f"\r{data["value"]:>10} {data["units"]:<8} {data["mode"]:<8} {data["flags"]:<15} {data["bar"]:<4} {alert_mark:<5}", end="", flush=True)
                last_update = time.time()
                if exporter:
                    exporter.push(data)
            elif time.time() - last_update > 5:
                print(f"\r{"---":>10} {"waiting":<8} {"":<8} {"":<15} {"":<4} {"":<5}", end="", flush=True)
                last_update = time.time()
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n[INFO] Stopped.")
    finally:
        reader.close()
        if exporter:
            exporter.stop()

def run_dashboard(port, config, exporter):
    host = "127.0.0.1"
    web_port = config.get("web_port", 8080)
    latest_data = {"timestamp": "", "value": "---", "units": "", "mode": "", "flags": "", "bar": 0, "connected": False, "alert": False, "alert_msg": ""}
    data_lock = threading.Lock()
    history = []

    def serial_thread():
        reader = BOLYFA117Reader(
            port,
            baudrate=config.get("baudrate", 2400),
            auto_reconnect=config.get("auto_reconnect", True),
            reconnect_delay=config.get("reconnect_delay", 3),
            smoothing_window=config.get("smoothing_window", 1),
            alert_min=config.get("alert_min"),
            alert_max=config.get("alert_max"),
            alert_beep=config.get("alert_beep", False)
        )
        if not reader.open():
            with data_lock:
                latest_data["connected"] = False
            return
        with data_lock:
            latest_data["connected"] = True
        try:
            while not reader._stop:
                data = reader.read_packet()
                if data:
                    with data_lock:
                        latest_data.update(data)
                        history.append({"t": time.time(), "val": data["value"], "u": data["units"]})
                        if len(history) > 120:
                            history.pop(0)
                    if exporter:
                        exporter.push(data)
        except Exception as e:
            print(f"[SERIAL ERROR] {e}")
        finally:
            reader.close()
            with data_lock:
                latest_data["connected"] = False

    t = threading.Thread(target=serial_thread, daemon=True)
    t.start()

    # Build HTML page
    html_lines = []
    html_lines.append("<!DOCTYPE html>")
    html_lines.append("<html><head><meta charset=\"UTF-8\">")
    html_lines.append("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">")
    html_lines.append("<title>BOLYFA 117 Dashboard</title>")
    html_lines.append("<style>")
    html_lines.append("body{font-family:'Segoe UI',sans-serif;background:#1a1a2e;color:#eee;margin:0;padding:10px}")
    html_lines.append("h1{text-align:center;color:#00d4ff;margin:8px 0;font-size:1.5rem}")
    html_lines.append(".container{max-width:900px;margin:0 auto;padding:0 5px}")
    html_lines.append(".card{background:#16213e;border-radius:12px;padding:15px;margin:10px 0;box-shadow:0 4px 15px rgba(0,0,0,0.3)}")
    html_lines.append(".big-value{font-size:clamp(48px,12vw,72px);text-align:center;color:#00ff88;font-weight:bold;margin:8px 0;line-height:1}")
    html_lines.append(".units{font-size:clamp(18px,4vw,28px);color:#aaa;text-align:center;margin-top:4px}")
    html_lines.append(".mode{font-size:clamp(14px,3vw,18px);color:#ffaa00;text-align:center;margin-top:4px;min-height:20px}")
    html_lines.append(".flags{text-align:center;margin:8px 0;min-height:24px;display:flex;flex-wrap:wrap;justify-content:center;gap:4px}")
    html_lines.append(".flag{background:#0f3460;padding:3px 10px;border-radius:10px;font-size:11px;color:#00d4ff;border:1px solid #0f3460}")
    html_lines.append(".bar-container{background:#0f3460;border-radius:6px;height:28px;margin:12px 0;overflow:hidden;position:relative}")
    html_lines.append(".bar-fill{background:linear-gradient(90deg,#00ff88,#00d4ff);height:100%;width:0%;transition:width 0.4s ease}")
    html_lines.append(".bar-label{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-size:11px;color:#fff;text-shadow:0 1px 2px rgba(0,0,0,0.5);pointer-events:none}")
    html_lines.append(".status{text-align:center;font-size:13px;margin-top:8px}")
    html_lines.append(".connected{color:#00ff88}.disconnected{color:#ff4444}")
    html_lines.append(".timestamp{text-align:center;color:#666;font-size:11px;margin-top:6px}")
    html_lines.append(".chart-wrap{position:relative;margin-top:12px;overflow-x:auto}")
    html_lines.append("canvas{width:100%;height:200px;background:#0f1a2e;border-radius:6px;border:1px solid #1a3a5c;display:block}")
    html_lines.append(".stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(80px,1fr));gap:8px;margin-top:10px}")
    html_lines.append(".stat{text-align:center;background:#0f1a2e;border-radius:6px;padding:6px}")
    html_lines.append(".stat-label{color:#888;font-size:10px;text-transform:uppercase;letter-spacing:0.5px}")
    html_lines.append(".stat-value{color:#00d4ff;font-size:clamp(14px,3vw,16px);font-weight:bold}")
    html_lines.append(".alert-banner{background:#ff4444;color:#fff;text-align:center;padding:8px;border-radius:6px;margin:8px 0;font-weight:bold;display:none}")
    html_lines.append(".alert-banner.show{display:block}")
    html_lines.append("@media(min-width:600px){.stats{grid-template-columns:repeat(5,1fr)}.big-value{font-size:72px}.units{font-size:28px}}")
    html_lines.append("</style></head>")
    html_lines.append("<body><div class=\"container\">")
    html_lines.append("<h1>&#128268; BOLYFA 117 Live Dashboard</h1>")
    html_lines.append("<div class=\"alert-banner\" id=\"alert-banner\">ALERT</div>")
    html_lines.append("<div class=\"card\">")
    html_lines.append("<div class=\"big-value\" id=\"value\">---</div>")
    html_lines.append("<div class=\"units\" id=\"units\">Waiting for data...</div>")
    html_lines.append("<div class=\"mode\" id=\"mode\"></div>")
    html_lines.append("<div class=\"flags\" id=\"flags\"></div>")
    html_lines.append("<div class=\"bar-container\"><div class=\"bar-fill\" id=\"bar\"></div><div class=\"bar-label\" id=\"bar-label\">0 / 60</div></div>")
    html_lines.append("<div class=\"status\" id=\"status\"><span class=\"disconnected\">&#9679; Disconnected</span></div>")
    html_lines.append("<div class=\"timestamp\" id=\"timestamp\"></div>")
    html_lines.append("</div>")
    html_lines.append("<div class=\"card\">")
    html_lines.append("<h3>&#128202; Live Trend</h3>")
    html_lines.append("<div class=\"chart-wrap\"><canvas id=\"chart\" width=\"860\" height=\"200\"></canvas></div>")
    html_lines.append("<div class=\"stats\">")
    html_lines.append("<div class=\"stat\"><div class=\"stat-label\">Current</div><div class=\"stat-value\" id=\"stat-cur\">---</div></div>")
    html_lines.append("<div class=\"stat\"><div class=\"stat-label\">Min</div><div class=\"stat-value\" id=\"stat-min\">---</div></div>")
    html_lines.append("<div class=\"stat\"><div class=\"stat-label\">Max</div><div class=\"stat-value\" id=\"stat-max\">---</div></div>")
    html_lines.append("<div class=\"stat\"><div class=\"stat-label\">Range</div><div class=\"stat-value\" id=\"stat-rng\">---</div></div>")
    html_lines.append("<div class=\"stat\"><div class=\"stat-label\">Points</div><div class=\"stat-value\" id=\"stat-pts\">0</div></div>")
    html_lines.append("</div></div></div>")
    html_lines.append("<script>")
    # JS lines
    js = [
        "const canvas=document.getElementById('chart');",
        "const ctx=canvas.getContext('2d');",
        "let historyData=[];let lastNumericValue=NaN;",
        "function formatNum(n){return isNaN(n)?'---':n.toFixed(3).replace(/\\.0+$/,'').replace(/(\\.\\d*?)0+$/,'$1');}",
        "function drawChart(){const w=canvas.width,h=canvas.height;const padLeft=50,padRight=10,padTop=15,padBottom=25;const chartW=w-padLeft-padRight,chartH=h-padTop-padBottom;ctx.clearRect(0,0,w,h);if(historyData.length<2){ctx.fillStyle='#666';ctx.font='14px Segoe UI';ctx.textAlign='center';ctx.fillText('Collecting data...',w/2,h/2);return;}const vals=historyData.map(d=>d.val).filter(v=>!isNaN(v));if(vals.length===0)return;let minV=Math.min(...vals),maxV=Math.max(...vals);const rangeRaw=maxV-minV;const pad=rangeRaw===0?Math.max(Math.abs(minV)*0.1,1):rangeRaw*0.1;minV-=pad;maxV+=pad;const range=maxV-minV||1;ctx.strokeStyle='#1a3a5c';ctx.lineWidth=1;for(let i=0;i<=5;i++){const y=padTop+(i/5)*chartH;ctx.beginPath();ctx.moveTo(padLeft,y);ctx.lineTo(w-padRight,y);ctx.stroke();const labelVal=maxV-(i/5)*range;ctx.fillStyle='#888';ctx.font='11px Segoe UI';ctx.textAlign='right';ctx.textBaseline='middle';ctx.fillText(formatNum(labelVal),padLeft-6,y);}ctx.strokeStyle='#2a4a6c';ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(padLeft,padTop);ctx.lineTo(padLeft,h-padBottom);ctx.stroke();ctx.beginPath();historyData.forEach((pt,i)=>{const x=padLeft+(i/(historyData.length-1))*chartW;const y=padTop+((maxV-pt.val)/range)*chartH;if(i===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);});ctx.strokeStyle='#00ff88';ctx.lineWidth=2.5;ctx.stroke();ctx.lineTo(padLeft+chartW,h-padBottom);ctx.lineTo(padLeft,h-padBottom);ctx.closePath();const grad=ctx.createLinearGradient(0,padTop,0,h-padBottom);grad.addColorStop(0,'rgba(0,255,136,0.25)');grad.addColorStop(1,'rgba(0,255,136,0.02)');ctx.fillStyle=grad;ctx.fill();historyData.forEach((pt,i)=>{const x=padLeft+(i/(historyData.length-1))*chartW;const y=padTop+((maxV-pt.val)/range)*chartH;ctx.beginPath();ctx.arc(x,y,4,0,Math.PI*2);ctx.fillStyle='#00ff88';ctx.fill();ctx.strokeStyle='#1a1a2e';ctx.lineWidth=2;ctx.stroke();});ctx.fillStyle='#888';ctx.font='11px Segoe UI';ctx.textAlign='center';ctx.textBaseline='top';const times=[0,Math.floor(historyData.length/2),historyData.length-1];times.forEach(idx=>{const x=padLeft+(idx/(historyData.length-1))*chartW;const t=new Date(historyData[idx].t);const label=t.getHours().toString().padStart(2,'0')+':'+t.getMinutes().toString().padStart(2,'0')+':'+t.getSeconds().toString().padStart(2,'0');ctx.fillText(label,x,h-padBottom+4);});}",
        "async function fetchData(){try{const r=await fetch('/data');const d=await r.json();document.getElementById('value').textContent=d.value||'---';document.getElementById('units').textContent=d.units||'';document.getElementById('mode').textContent=d.mode||'';document.getElementById('timestamp').textContent=d.timestamp||'';const barPct=Math.min((d.bar/60)*100,100);document.getElementById('bar').style.width=barPct+'%';document.getElementById('bar-label').textContent=(d.bar||0)+' / 60';const status=document.getElementById('status');status.innerHTML=d.connected?'<span class=\"connected\">&#9679; Connected</span>':'<span class=\"disconnected\">&#9679; Disconnected</span>';const banner=document.getElementById('alert-banner');if(d.alert){banner.textContent=d.alert_msg||'ALERT';banner.classList.add('show');}else{banner.classList.remove('show');}const flagsDiv=document.getElementById('flags');flagsDiv.innerHTML='';if(d.flags){d.flags.split(',').forEach(f=>{if(f.trim()){const s=document.createElement('span');s.className='flag';s.textContent=f.trim();flagsDiv.appendChild(s);}});}let nv=parseFloat(d.value);if(!isNaN(nv)){lastNumericValue=nv;historyData.push({t:Date.now(),val:nv});if(historyData.length>120)historyData.shift();}const vals=historyData.map(d=>d.val).filter(v=>!isNaN(v));document.getElementById('stat-cur').textContent=formatNum(lastNumericValue);document.getElementById('stat-min').textContent=vals.length?formatNum(Math.min(...vals)):'---';document.getElementById('stat-max').textContent=vals.length?formatNum(Math.max(...vals)):'---';const rng=vals.length?Math.max(...vals)-Math.min(...vals):NaN;document.getElementById('stat-rng').textContent=formatNum(rng);document.getElementById('stat-pts').textContent=historyData.length;drawChart();}catch(e){console.error(e);}}",
        "setInterval(fetchData,500);",
    ]
    for j in js:
        html_lines.append(j)
    html_lines.append("</script></body></html>")
    HTML_PAGE = "\n".join(html_lines)

    def handle_request(conn):
        try:
            request = conn.recv(4096).decode("utf-8", errors="ignore")
            if not request: return
            lines = request.split("\r\n")
            if not lines: return
            req_line = lines[0]
            if "GET /data" in req_line:
                with data_lock:
                    body = json.dumps(latest_data)
                response = ("HTTP/1.1 200 OK\r\n" + "Content-Type: application/json\r\n" + "Access-Control-Allow-Origin: *\r\n" + f"Content-Length: {len(body)}\r\n" + "\r\n" + body)
            else:
                body = HTML_PAGE
                response = ("HTTP/1.1 200 OK\r\n" + "Content-Type: text/html; charset=utf-8\r\n" + f"Content-Length: {len(body)}\r\n" + "\r\n" + body)
            conn.sendall(response.encode("utf-8"))
        except Exception as e:
            print(f"[HTTP ERROR] {e}")
        finally:
            conn.close()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, web_port))
    sock.listen(5)
    print(f"\n[OK] Dashboard running at http://{host}:{web_port}")
    print("[INFO] Open the URL in your browser to see live data")
    print("[INFO] Press CTRL+C to stop\n")
    try:
        while True:
            conn, addr = sock.accept()
            handle_request(conn)
    except KeyboardInterrupt:
        print("\n[INFO] Stopping dashboard...")
    finally:
        sock.close()
        if exporter:
            exporter.stop()



def auto_detect_port():
    """Find the most likely BOLYFA 117 COM port."""
    for p in serial.tools.list_ports.comports():
        desc = p.description.lower()
        if any(x in desc for x in ["ch340", "ch341", "hl-340", "qinheng"]):
            return p.device
    return None


def list_ports():
    print("\nAvailable serial ports:")
    ports = serial.tools.list_ports.comports()
    if not ports:
        print("  No serial ports found.")
        return
    for p in ports:
        marker = ""
        if any(x in p.description.lower() for x in ["ch340", "ch341", "hl-340"]):
            marker = "  <-- LIKELY YOUR BOLYFA 117"
        print(f"  {p.device:12} - {p.description}{marker}")


def main():
    parser = argparse.ArgumentParser(
        description='BOLYFA 117 USB Digital Multimeter Data Logger v3',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --mode dashboard                    # auto-detect port
  %(prog)s --mode csv --alert-max 30.0
  %(prog)s --config bolyfa117.json
  %(prog)s --list
  %(prog)s --write-config                      # create template config
        """
    )
    parser.add_argument('--mode', choices=['live', 'csv', 'dashboard'], default='live')
    parser.add_argument('--port', default=None, help='COM port (auto-detect if omitted)')
    parser.add_argument('--output-dir', default='.', help='CSV output directory')
    parser.add_argument('--web-port', type=int, default=8080, help='Dashboard port')
    parser.add_argument('--list', action='store_true', help='List ports and exit')
    parser.add_argument('--config', default='bolyfa117.json', help='Config file path')
    parser.add_argument('--write-config', action='store_true', help='Write template config and exit')
    parser.add_argument('--smoothing', type=int, default=None, help='Smoothing window size')
    parser.add_argument('--alert-min', type=float, default=None, help='Alert if value below this')
    parser.add_argument('--alert-max', type=float, default=None, help='Alert if value above this')
    parser.add_argument('--alert-beep', action='store_true', help='Terminal beep on alert')
    parser.add_argument('--mqtt-broker', default=None, help='MQTT broker host')
    parser.add_argument('--mqtt-topic', default=None, help='MQTT topic')
    parser.add_argument('--influxdb-url', default=None, help='InfluxDB URL (e.g., http://localhost:8086)')

    args = parser.parse_args()

    if args.list:
        list_ports()
        return

    if args.write_config:
        save_config_template(args.config)
        return

    # Load config file
    config = load_config(args.config)

    # CLI overrides config
    if args.mode:
        config['mode'] = args.mode
    if args.port:
        config['port'] = args.port
    if args.output_dir != '.':
        config['output_dir'] = args.output_dir
    if args.web_port != 8080:
        config['web_port'] = args.web_port
    if args.smoothing is not None:
        config['smoothing_window'] = args.smoothing
    if args.alert_min is not None:
        config['alert_min'] = args.alert_min
    if args.alert_max is not None:
        config['alert_max'] = args.alert_max
    if args.alert_beep:
        config['alert_beep'] = True
    if args.mqtt_broker:
        config['mqtt_broker'] = args.mqtt_broker
    if args.mqtt_topic:
        config['mqtt_topic'] = args.mqtt_topic
    if args.influxdb_url:
        config['influxdb_url'] = args.influxdb_url

    # Auto-detect port if not specified
    port = config.get('port')
    if not port:
        port = auto_detect_port()
        if port:
            print(f"[OK] Auto-detected port: {port}")
        else:
            print("[ERROR] Could not auto-detect port. Use --port or --list.")
            sys.exit(1)

    # Validate port
    if isinstance(port, str) and port.isdigit():
        port = f"COM{port}"
        config['port'] = port

    # Initialize exporter
    exporter = DataExporter(config) if (config.get('influxdb_url') or config.get('mqtt_broker')) else None

    # Run selected mode
    if config['mode'] == 'csv':
        run_csv_logger(port, config, exporter)
    elif config['mode'] == 'dashboard':
        run_dashboard(port, config, exporter)
    else:
        run_live(port, config, exporter)


if __name__ == '__main__':
    main()
