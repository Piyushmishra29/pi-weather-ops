#!/usr/bin/env python3
"""Pi Weather Server - DHT11 (temp/humidity) + PMS5003 (PM) + MQ-135 (VOC).

Single-file stdlib http.server. Sensor polling runs in background threads;
HTTP handlers just read from a shared snapshot dict. Designed to fail soft:
if a sensor is unplugged or errors, its fields go null but the service stays up.
"""
import json
import time
import struct
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone

BIND_HOST = "0.0.0.0"
BIND_PORT = 8000

DHT_PIN = 4
PMS_DEVICE = "/dev/serial0"
PMS_BAUD = 9600
MQ135_SPI_BUS = 0
MQ135_SPI_DEV = 0
MQ135_CHANNEL = 0
MQ135_DIVIDER_RATIO = 1.5  # 10k+20k divider: V_sensor = V_adc * 1.5
MQ135_VREF = 3.3
MQ135_R0_ENV = 10000.0  # clean-air baseline (calibrate later)

# Plus code WJH6+C7 Bengaluru decoded to decimal degrees
LOCATION_LAT = 12.9286
LOCATION_LON = 77.6107
LOCATION_PLUS = "WJH6+C7"
LOCATION_LABEL = "BENGALURU, KA, IND"
ASSET_CALLSIGN = "PIYUSHPI"

# ------------------------------- shared state -------------------------------

_state_lock = threading.Lock()
_state = {
    "temp_c": None,
    "humidity_pct": None,
    "pm1_0": None,
    "pm2_5": None,
    "pm10": None,
    "aqi": None,
    "aqi_label": None,
    "aqi_color": None,
    "voc_raw": None,
    "voc_mv": None,
    "voc_index": None,
    "dht_updated": None,
    "pms_updated": None,
    "mq_updated": None,
    "errors": {"dht": None, "pms": None, "mq": None},
    "lat": LOCATION_LAT,
    "lon": LOCATION_LON,
    "loc_plus": LOCATION_PLUS,
    "loc_label": LOCATION_LABEL,
    "callsign": ASSET_CALLSIGN,
}


def _update(**kw):
    with _state_lock:
        _state.update(kw)


def _set_err(key, msg):
    with _state_lock:
        _state["errors"][key] = msg


def _snapshot():
    with _state_lock:
        return json.loads(json.dumps(_state))


# ------------------------------- AQI from PM2.5 -----------------------------
# US EPA breakpoints: (C_low, C_high, AQI_low, AQI_high, label, color)
_AQI_BPS = [
    (0.0, 12.0, 0, 50, "Good", "#22c55e"),
    (12.1, 35.4, 51, 100, "Moderate", "#eab308"),
    (35.5, 55.4, 101, 150, "Unhealthy for Sensitive", "#f97316"),
    (55.5, 150.4, 151, 200, "Unhealthy", "#ef4444"),
    (150.5, 250.4, 201, 300, "Very Unhealthy", "#a855f7"),
    (250.5, 500.4, 301, 500, "Hazardous", "#7f1d1d"),
]


def pm25_to_aqi(c):
    if c is None:
        return None, None, None
    c = max(0.0, float(c))
    for clow, chigh, ilow, ihigh, label, color in _AQI_BPS:
        if c <= chigh:
            aqi = (ihigh - ilow) / (chigh - clow) * (c - clow) + ilow
            return int(round(aqi)), label, color
    return 500, "Hazardous", "#7f1d1d"


# ------------------------------- DHT11 -------------------------------------

class DHT11Poller(threading.Thread):
    """Polls DHT11 every ~3 s using pigpio edge callbacks."""

    def __init__(self, gpio=DHT_PIN, interval=3.0):
        super().__init__(daemon=True, name="dht11")
        self.gpio = gpio
        self.interval = interval

    def run(self):
        try:
            import pigpio
        except ImportError as e:
            _set_err("dht", f"pigpio not installed: {e}")
            return
        pi = pigpio.pi()
        if not pi.connected:
            _set_err("dht", "pigpiod not running")
            return

        edges = []

        def cbf(_gpio, level, tick):
            edges.append((level, tick))

        cb = pi.callback(self.gpio, pigpio.EITHER_EDGE, cbf)
        try:
            while True:
                t0 = time.monotonic()
                try:
                    edges.clear()
                    pi.set_mode(self.gpio, pigpio.OUTPUT)
                    pi.write(self.gpio, 0)
                    time.sleep(0.020)
                    pi.set_mode(self.gpio, pigpio.INPUT)
                    pi.set_pull_up_down(self.gpio, pigpio.PUD_UP)
                    time.sleep(0.2)

                    es = list(edges)
                    if len(es) < 6:
                        _set_err("dht", f"only {len(es)} edges (sensor unresponsive)")
                    else:
                        pairs = [
                            (es[i][0], pigpio.tickDiff(es[i - 1][1], es[i][1]))
                            for i in range(1, len(es))
                        ]
                        # HIGH durations = duration the line was HIGH right before going LOW
                        high_durs = [d for lvl, d in pairs if lvl == 0]
                        # [0]=brief release HIGH from MCU, [1]=sensor ACK HIGH (~80us)
                        # bits start at [2], 40 of them.
                        if len(high_durs) < 42:
                            _set_err(
                                "dht",
                                f"incomplete frame: {len(high_durs)} HIGH pulses",
                            )
                        else:
                            bits = [1 if d > 45 else 0 for d in high_durs[2:42]]
                            out = []
                            for i in range(5):
                                v = 0
                                for b in bits[i * 8:(i + 1) * 8]:
                                    v = (v << 1) | b
                                out.append(v)
                            ok = ((out[0] + out[1] + out[2] + out[3]) & 0xFF) == out[4]
                            if not ok:
                                _set_err("dht", f"checksum fail: {out}")
                            else:
                                humidity = out[0] + out[1] / 10.0
                                temp = out[2] + out[3] / 10.0
                                if 0 <= humidity <= 100 and -40 <= temp <= 80:
                                    _update(
                                        temp_c=round(temp, 1),
                                        humidity_pct=round(humidity, 1),
                                        dht_updated=time.time(),
                                    )
                                    _set_err("dht", None)
                                else:
                                    _set_err(
                                        "dht",
                                        f"out-of-range: t={temp} h={humidity}",
                                    )
                except Exception as e:
                    _set_err("dht", f"{type(e).__name__}: {e}")

                # sleep with floor of 2.2s for sensor recovery
                dt = self.interval - (time.monotonic() - t0)
                time.sleep(max(2.2, dt))
        finally:
            cb.cancel()
            pi.stop()


# ------------------------------- PMS5003 -----------------------------------

class PMS5003Poller(threading.Thread):
    """Reads PMS5003 active-mode frames from UART. Sensor pushes ~1 frame/sec."""

    def __init__(self, device=PMS_DEVICE, baud=PMS_BAUD):
        super().__init__(daemon=True, name="pms5003")
        self.device = device
        self.baud = baud

    def run(self):
        try:
            import serial
        except ImportError as e:
            _set_err("pms", f"pyserial not installed: {e}")
            return

        while True:
            try:
                with serial.Serial(self.device, self.baud, timeout=2.0) as ser:
                    _set_err("pms", None)
                    while True:
                        # Sync to header 0x42 0x4D
                        b = ser.read(1)
                        if not b:
                            _set_err("pms", "uart timeout")
                            continue
                        if b != b"\x42":
                            continue
                        b = ser.read(1)
                        if b != b"\x4d":
                            continue
                        body = ser.read(30)
                        if len(body) != 30:
                            _set_err("pms", f"short frame: {len(body)} bytes")
                            continue
                        frame = b"\x42\x4d" + body
                        # Checksum: sum of bytes 0..29 == big-endian uint16 at 30..31
                        chk = struct.unpack(">H", frame[30:32])[0]
                        if sum(frame[:30]) != chk:
                            _set_err("pms", "checksum fail")
                            continue
                        # Atmospheric concentrations are at offsets 10/12/14 (big-endian u16)
                        pm1, pm25, pm10 = struct.unpack(">HHH", frame[10:16])
                        aqi, label, color = pm25_to_aqi(pm25)
                        _update(
                            pm1_0=pm1,
                            pm2_5=pm25,
                            pm10=pm10,
                            aqi=aqi,
                            aqi_label=label,
                            aqi_color=color,
                            pms_updated=time.time(),
                        )
                        _set_err("pms", None)
            except FileNotFoundError:
                _set_err("pms", f"device {self.device} not found")
                time.sleep(10)
            except Exception as e:
                _set_err("pms", f"{type(e).__name__}: {e}")
                time.sleep(5)


# ------------------------------- MQ-135 + MCP3008 --------------------------

class MQ135Poller(threading.Thread):
    """Reads MQ-135 analog via MCP3008 SPI ADC."""

    def __init__(
        self,
        bus=MQ135_SPI_BUS,
        dev=MQ135_SPI_DEV,
        channel=MQ135_CHANNEL,
        interval=5.0,
    ):
        super().__init__(daemon=True, name="mq135")
        self.bus = bus
        self.dev = dev
        self.channel = channel
        self.interval = interval

    def _read_adc(self, spi):
        # MCP3008 single-ended read: start bit, sgl/diff + channel high bits, dummy
        # cmd = [0x01, (8|channel)<<4, 0x00]
        cmd = [0x01, (0x08 | self.channel) << 4, 0x00]
        r = spi.xfer2(cmd)
        return ((r[1] & 0x03) << 8) | r[2]

    def run(self):
        try:
            import spidev
        except ImportError as e:
            _set_err("mq", f"spidev not installed: {e}")
            return
        try:
            spi = spidev.SpiDev()
            spi.open(self.bus, self.dev)
            spi.max_speed_hz = 1_350_000
        except Exception as e:
            _set_err("mq", f"SPI open: {e}")
            return

        try:
            while True:
                try:
                    raw = self._read_adc(spi)
                    v_adc = raw * MQ135_VREF / 1023.0
                    v_sensor_mv = v_adc * MQ135_DIVIDER_RATIO * 1000.0
                    # Rs = RL * (Vcc - Vs) / Vs, with RL=10k, Vcc=5V (sensor side)
                    vs = v_adc * MQ135_DIVIDER_RATIO
                    if vs <= 0.01:
                        rs = float("inf")
                    else:
                        rs = 10000.0 * (5.0 - vs) / vs
                    ratio = rs / MQ135_R0_ENV
                    # Lower ratio = more VOC. Map to a 0..100 index (rough).
                    # Clean air ratio ~1.0; heavy contamination ratio < 0.3.
                    index = max(0.0, min(100.0, (1.0 - ratio) * 100.0))
                    _update(
                        voc_raw=raw,
                        voc_mv=round(v_sensor_mv, 1),
                        voc_index=round(index, 1),
                        mq_updated=time.time(),
                    )
                    _set_err("mq", None)
                except Exception as e:
                    _set_err("mq", f"{type(e).__name__}: {e}")
                time.sleep(self.interval)
        finally:
            try:
                spi.close()
            except Exception:
                pass


# ------------------------------- HTTP --------------------------------------

FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
<rect width="32" height="32" rx="3" fill="#0a0d0a"/>
<path d="M3 8 L3 3 L8 3 M29 8 L29 3 L24 3 M3 24 L3 29 L8 29 M29 24 L29 29 L24 29" stroke="#7ba428" stroke-width="1.6" fill="none" stroke-linecap="square"/>
<circle cx="16" cy="16" r="11" fill="none" stroke="#7eea7a" stroke-width="0.8" stroke-dasharray="1.6 2" opacity="0.7"/>
<circle cx="16" cy="16" r="6.5" fill="none" stroke="#7eea7a" stroke-width="1" opacity="0.9"/>
<line x1="16" y1="5" x2="16" y2="8.5" stroke="#7eea7a" stroke-width="1.4"/>
<line x1="16" y1="23.5" x2="16" y2="27" stroke="#7eea7a" stroke-width="1.4"/>
<line x1="5" y1="16" x2="8.5" y2="16" stroke="#7eea7a" stroke-width="1.4"/>
<line x1="23.5" y1="16" x2="27" y2="16" stroke="#7eea7a" stroke-width="1.4"/>
<g style="transform-origin:16px 16px;animation:sweep 2.4s linear infinite">
  <defs><linearGradient id="sweepGrad" x1="16" y1="16" x2="16" y2="5" gradientUnits="userSpaceOnUse"><stop offset="0%" stop-color="#ff4040" stop-opacity="0"/><stop offset="60%" stop-color="#ff4040" stop-opacity="0.55"/><stop offset="100%" stop-color="#ff4040" stop-opacity="1"/></linearGradient></defs>
  <line x1="16" y1="16" x2="16" y2="5" stroke="url(#sweepGrad)" stroke-width="1.6" stroke-linecap="round"/>
</g>
<circle cx="16" cy="16" r="2.4" fill="#7eea7a">
  <animate attributeName="opacity" values="1;0.35;1" dur="1.4s" repeatCount="indefinite"/>
  <animate attributeName="r" values="2.2;3.2;2.2" dur="1.4s" repeatCount="indefinite"/>
</circle>
<style>@keyframes sweep{to{transform:rotate(360deg)}}</style>
</svg>"""

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0a0d0a">
<meta name="color-scheme" content="dark">
<title>OPS PANEL // ASSET PIYUSHPI</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="alternate icon" href="/favicon.svg">
<link rel="mask-icon" href="/favicon.svg" color="#7eea7a">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Black+Ops+One&family=Share+Tech+Mono&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
<style>
:root{
  --bg:#0a0d0a; --bg-2:#0d1209; --surface:#10140d; --surface-2:#161c12;
  --grid:rgba(123,164,40,0.07);
  --border:#2c3a20; --border-bright:#4a5d33;
  --accent:#7ba428; --accent-glow:rgba(123,164,40,0.45);
  --ok:#7eea7a; --warn:#ffb000; --crit:#ff4040; --pending:#5a6840;
  --text:#cfd9b0; --text-dim:#94a06d; --mute:#6a774a;
  --display:'Black Ops One',Impact,sans-serif;
  --mono:'Share Tech Mono','Courier New',monospace;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{
  font-family:var(--mono); color:var(--text); background:var(--bg);
  min-height:100vh; padding:18px clamp(10px,3vw,24px);
  background-image:
    linear-gradient(var(--grid) 1px, transparent 1px),
    linear-gradient(90deg, var(--grid) 1px, transparent 1px),
    radial-gradient(ellipse at 50% 0%, rgba(123,164,40,0.06) 0%, transparent 60%),
    radial-gradient(ellipse at 50% 100%, rgba(0,0,0,0.6) 0%, transparent 70%);
  background-size: 36px 36px, 36px 36px, 100% 100%, 100% 100%;
  overflow-x:hidden;
}
body::before{
  content:''; position:fixed; inset:0; pointer-events:none; z-index:0;
  background:repeating-linear-gradient(180deg, transparent 0 2px, rgba(123,164,40,0.025) 2px 3px);
  mix-blend-mode:screen;
}
body::after{
  content:''; position:fixed; inset:0; pointer-events:none; z-index:1;
  background:radial-gradient(ellipse at center, transparent 55%, rgba(0,0,0,0.55) 100%);
}
@media (prefers-reduced-motion: reduce){
  body::before{display:none}
  *{animation:none !important; transition:none !important}
}

.shell{max-width:1200px; margin:0 auto; position:relative; z-index:2}

/* HEADER STRIP */
.banner{
  display:grid; grid-template-columns:auto 1fr auto; align-items:end; gap:18px;
  padding:14px 18px; margin-bottom:14px; background:var(--surface);
  position:relative; border:1px solid var(--border);
}
.banner::before, .banner::after,
.cell::before, .cell::after{
  content:''; position:absolute; width:14px; height:14px; pointer-events:none;
}
.banner::before{top:-1px; left:-1px; border-top:2px solid var(--accent); border-left:2px solid var(--accent)}
.banner::after{bottom:-1px; right:-1px; border-bottom:2px solid var(--accent); border-right:2px solid var(--accent)}
.bannerL{display:flex; align-items:center; gap:14px}
.brand{
  font-family:var(--display); font-size:clamp(1.6rem, 4.2vw, 2.4rem); line-height:1;
  color:var(--accent); letter-spacing:0.06em;
  text-shadow:0 0 12px var(--accent-glow);
}
.brand-sub{font-family:var(--mono); font-size:0.78rem; color:var(--mute); letter-spacing:0.18em; margin-top:6px}
.bannerC{text-align:center; color:var(--text-dim); font-size:0.74rem; letter-spacing:0.15em}
.bannerC b{color:var(--accent); font-weight:normal}
.bannerR{text-align:right; font-variant-numeric:tabular-nums; line-height:1.4}
.tstamp{font-size:0.95rem; color:var(--text); letter-spacing:0.05em}
.tstamp-line{font-size:0.7rem; color:var(--mute); letter-spacing:0.1em}
.live{
  display:inline-flex; align-items:center; gap:6px; font-size:0.7rem; color:var(--ok);
  letter-spacing:0.2em; margin-left:8px;
}
.dot{display:inline-block; width:8px; height:8px; border-radius:50%; background:currentColor; box-shadow:0 0 6px currentColor; animation:pulse 1.4s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:0.45;transform:scale(0.7)}}

/* GRID */
.grid{display:grid; grid-template-columns:1fr; gap:14px}
@media (min-width:880px){ .grid{grid-template-columns:minmax(0,1fr) minmax(0,1.15fr)} }

.stack{display:flex; flex-direction:column; gap:14px; min-width:0}

/* CELL (card) */
.cell{
  position:relative; background:var(--surface); border:1px solid var(--border);
  padding:18px 18px 16px; min-width:0;
}
.cell::before{top:-1px; left:-1px; border-top:2px solid var(--accent); border-left:2px solid var(--accent)}
.cell::after{bottom:-1px; right:-1px; border-bottom:2px solid var(--accent); border-right:2px solid var(--accent)}
.cell[data-state="warn"]::before,.cell[data-state="warn"]::after{border-color:var(--warn)}
.cell[data-state="crit"]::before,.cell[data-state="crit"]::after{border-color:var(--crit)}
.cell[data-state="pending"]::before,.cell[data-state="pending"]::after{border-color:var(--pending)}

.cellHead{
  display:flex; justify-content:space-between; align-items:flex-start; gap:12px;
  margin-bottom:12px; padding-bottom:10px; border-bottom:1px dashed var(--border);
}
.cellId{font-family:var(--display); font-size:0.82rem; color:var(--accent); letter-spacing:0.12em}
.cellSub{font-size:0.7rem; color:var(--mute); letter-spacing:0.18em; margin-top:3px}
.cellMeta{font-size:0.66rem; color:var(--mute); letter-spacing:0.18em; text-align:right; line-height:1.5}
.status{
  display:inline-flex; align-items:center; gap:6px;
  padding:2px 8px; border:1px solid currentColor; font-size:0.66rem; letter-spacing:0.18em;
}
.status[data-s="ok"]{color:var(--ok)}
.status[data-s="warn"]{color:var(--warn)}
.status[data-s="crit"]{color:var(--crit)}
.status[data-s="pending"]{color:var(--pending)}

/* THERMAL CELL */
.thermal{display:grid; grid-template-columns:1fr 1fr; gap:14px}
.metric{display:flex; flex-direction:column; gap:6px; min-width:0}
.metric-label{font-size:0.7rem; color:var(--text-dim); letter-spacing:0.18em}
.metric-val{
  font-family:var(--display); font-size:clamp(2.4rem, 7vw, 3.4rem); line-height:1;
  color:var(--text); font-variant-numeric:tabular-nums;
  text-shadow:0 0 12px rgba(207,217,176,0.18);
}
.metric-unit{font-family:var(--mono); font-size:0.85rem; color:var(--text-dim); margin-left:4px; letter-spacing:0.05em}
.metric-band{
  height:6px; background:var(--surface-2); border:1px solid var(--border); position:relative; margin-top:4px;
}
.metric-band::before{
  content:''; position:absolute; top:0; bottom:0; background:var(--accent);
  width:var(--w,0%); transition:width 0.5s ease; box-shadow:0 0 8px var(--accent-glow);
}
.metric-band[data-s="warn"]::before{background:var(--warn); box-shadow:0 0 8px rgba(255,176,0,0.5)}
.metric-band[data-s="crit"]::before{background:var(--crit); box-shadow:0 0 8px rgba(255,64,64,0.5)}
.metric-band[data-s="pending"]::before{background:var(--pending); box-shadow:none}
.band-scale{display:flex; justify-content:space-between; font-size:0.62rem; color:var(--mute); letter-spacing:0.12em; margin-top:3px}

/* AQI CELL */
.aqiTop{display:grid; grid-template-columns:auto 1fr; gap:18px; align-items:end; margin-bottom:14px}
.aqiNum{font-family:var(--display); font-size:clamp(3rem, 9vw, 4.2rem); line-height:1; color:var(--text); font-variant-numeric:tabular-nums; text-shadow:0 0 14px var(--accent-glow)}
.aqiLab{
  padding:8px 12px; border:1px solid var(--accent); color:var(--text);
  font-size:0.78rem; letter-spacing:0.18em; text-align:center;
  background:rgba(123,164,40,0.06);
}
.pmRow{display:grid; grid-template-columns:repeat(3,1fr); gap:8px}
.pm{padding:10px 12px; background:var(--surface-2); border:1px solid var(--border); position:relative}
.pm-name{font-size:0.62rem; color:var(--mute); letter-spacing:0.18em}
.pm-val{font-family:var(--display); font-size:1.4rem; color:var(--text); margin-top:3px; font-variant-numeric:tabular-nums}
.pm-unit{font-size:0.62rem; color:var(--mute); margin-left:3px; letter-spacing:0.05em}

/* VOC CELL */
.vocRow{display:flex; align-items:flex-end; gap:18px; margin-bottom:10px; flex-wrap:wrap}
.vocNum{font-family:var(--display); font-size:clamp(2.6rem, 8vw, 3.6rem); line-height:1; color:var(--text); font-variant-numeric:tabular-nums; text-shadow:0 0 12px rgba(207,217,176,0.2)}
.vocMv{font-size:0.78rem; color:var(--text-dim); letter-spacing:0.1em; padding-bottom:8px}
.vocBar{height:10px; background:var(--surface-2); border:1px solid var(--border); position:relative; overflow:hidden}
.vocBar::before{
  content:''; position:absolute; left:0; top:0; bottom:0; width:var(--w,0%);
  background:linear-gradient(90deg, var(--ok) 0%, var(--warn) 60%, var(--crit) 100%);
  transition:width 0.6s ease; box-shadow:0 0 8px rgba(255,176,0,0.3);
}
.vocScale{display:flex; justify-content:space-between; font-size:0.62rem; color:var(--mute); letter-spacing:0.18em; margin-top:4px}

/* LOCATION CELL */
.locCell{padding:0; display:flex; flex-direction:column; min-height:100%}
.locCell .cellHead{padding:14px 18px 10px; margin-bottom:0; border-bottom:1px solid var(--border)}
.mapWrap{position:relative; flex:1; min-height:340px}
#map{position:absolute; inset:0; background:var(--bg-2); filter:saturate(0.7) contrast(1.08) brightness(0.92) hue-rotate(-8deg)}
.mapOverlay{
  position:absolute; inset:0; pointer-events:none; z-index:600;
  background:
    linear-gradient(transparent 79%, rgba(123,164,40,0.18) 80%, transparent 81%),
    linear-gradient(90deg, transparent 79%, rgba(123,164,40,0.18) 80%, transparent 81%);
}
.mapCorners::before, .mapCorners::after,
.mapCorners > i::before, .mapCorners > i::after{
  content:''; position:absolute; width:18px; height:18px; border:0 solid var(--accent); z-index:601; pointer-events:none;
}
.mapCorners::before{top:8px; left:8px; border-top-width:2px; border-left-width:2px}
.mapCorners::after{top:8px; right:8px; border-top-width:2px; border-right-width:2px}
.mapCorners > i::before{bottom:8px; left:8px; border-bottom-width:2px; border-left-width:2px}
.mapCorners > i::after{bottom:8px; right:8px; border-bottom-width:2px; border-right-width:2px}
.mapStamp{
  position:absolute; top:14px; right:14px; z-index:602; pointer-events:none;
  font-size:0.62rem; color:var(--accent); letter-spacing:0.2em; background:rgba(10,13,10,0.7);
  border:1px solid var(--border); padding:3px 8px;
}
.reconBtn{
  position:absolute; bottom:14px; left:14px; z-index:603;
  font-family:var(--mono); font-size:0.7rem; color:var(--text); letter-spacing:0.18em;
  padding:6px 10px; background:rgba(10,13,10,0.75); border:1px solid var(--border);
  cursor:pointer; transition:all 0.15s;
}
.reconBtn:hover{border-color:var(--accent); color:var(--accent)}
.reconBtn[data-on="1"]{color:var(--accent); border-color:var(--accent); background:rgba(123,164,40,0.1)}
.locked{cursor:not-allowed !important}
.locked .leaflet-grab{cursor:not-allowed !important}

.reticle{position:relative; width:64px; height:64px; pointer-events:none; filter:drop-shadow(0 0 6px rgba(126,234,122,0.7))}
.reticle svg{position:absolute; inset:0; width:100%; height:100%; overflow:visible}
.reticle .ping-out{transform-origin:center; animation:pingOut 2s ease-out infinite}
@keyframes pingOut{0%{r:5;opacity:1;stroke-width:1.8}80%{r:22;opacity:0;stroke-width:0.4}100%{r:22;opacity:0}}

.coords{
  padding:14px 18px; border-top:1px solid var(--border); background:var(--surface-2);
  display:grid; grid-template-columns:auto 1fr; gap:6px 14px;
  font-size:0.78rem; letter-spacing:0.05em;
}
.coords dt{color:var(--mute); letter-spacing:0.18em; font-size:0.7rem; padding-top:2px}
.coords dd{color:var(--text); font-variant-numeric:tabular-nums}

/* FOOTER */
.foot{
  display:flex; flex-wrap:wrap; gap:12px 18px; padding:10px 16px; margin-top:14px;
  background:var(--surface); border:1px solid var(--border); font-size:0.7rem; color:var(--mute); letter-spacing:0.14em;
  position:relative;
}
.foot::before, .foot::after{
  content:''; position:absolute; width:12px; height:12px;
}
.foot::before{top:-1px; left:-1px; border-top:2px solid var(--accent); border-left:2px solid var(--accent)}
.foot::after{bottom:-1px; right:-1px; border-bottom:2px solid var(--accent); border-right:2px solid var(--accent)}
.foot b{color:var(--text); font-weight:normal}
.foot .sep{color:var(--border-bright)}

/* Leaflet overrides */
.leaflet-container{background:#0d1209 !important; font-family:var(--mono) !important}
.leaflet-control-attribution{
  background:rgba(10,13,10,0.7) !important; color:var(--mute) !important;
  font-size:0.55rem !important; letter-spacing:0.08em !important; padding:2px 6px !important;
}
.leaflet-control-attribution a{color:var(--text-dim) !important}

/* Error chip */
.err{margin-top:10px; padding:8px 10px; border:1px dashed var(--crit); color:#ffb0b0; font-size:0.7rem; letter-spacing:0.05em; word-break:break-word}
.err::before{content:'⚠ '; color:var(--crit)}

</style>
</head>
<body>
<div class="shell">

  <header class="banner">
    <div class="bannerL">
      <div>
        <div class="brand">OPS&nbsp;PANEL</div>
        <div class="brand-sub">// ATMOSPHERIC SURVEILLANCE NODE</div>
      </div>
    </div>
    <div class="bannerC">
      ASSET <b id="callsign">PIYUSHPI</b> &nbsp;/&nbsp; SECTOR <b id="loc-short">BLR-IND</b> &nbsp;/&nbsp; COMMS <b>TLS</b>
      <span class="live"><span class="dot"></span><span id="live-state">LIVE</span></span>
    </div>
    <div class="bannerR">
      <div class="tstamp" id="zulu">--:--:-- Z</div>
      <div class="tstamp-line" id="local">--:--:-- LOCAL</div>
    </div>
  </header>

  <main class="grid">

    <div class="stack">
      <!-- SITREP // THERMAL -->
      <section class="cell" id="cell-th" data-state="pending">
        <header class="cellHead">
          <div>
            <div class="cellId">SITREP&nbsp;//&nbsp;THERMAL</div>
            <div class="cellSub">DHT11 :: GPIO-04 :: SENS-01</div>
          </div>
          <div class="cellMeta">
            <div class="status" data-s="pending" id="st-th"><span class="dot"></span>STANDBY</div>
            <div style="margin-top:6px" id="age-th">ACQ&hellip;</div>
          </div>
        </header>
        <div class="thermal">
          <div class="metric">
            <div class="metric-label">TEMPERATURE</div>
            <div><span class="metric-val" id="temp">--</span><span class="metric-unit">°C</span></div>
            <div class="metric-band" id="band-temp"></div>
            <div class="band-scale"><span>0</span><span>20</span><span>40°C</span></div>
          </div>
          <div class="metric">
            <div class="metric-label">HUMIDITY</div>
            <div><span class="metric-val" id="hum">--</span><span class="metric-unit">% RH</span></div>
            <div class="metric-band" id="band-hum"></div>
            <div class="band-scale"><span>0</span><span>50</span><span>100%</span></div>
          </div>
        </div>
        <div class="err" id="err-th" hidden></div>
      </section>

      <!-- ATMOSPHERIC // PARTICULATE -->
      <section class="cell" id="cell-aq" data-state="pending">
        <header class="cellHead">
          <div>
            <div class="cellId">ATMOSPHERIC&nbsp;//&nbsp;PARTICULATE</div>
            <div class="cellSub">PMS5003 :: UART0 :: SENS-02</div>
          </div>
          <div class="cellMeta">
            <div class="status" data-s="pending" id="st-aq"><span class="dot"></span>STANDBY</div>
            <div style="margin-top:6px" id="age-aq">ACQ&hellip;</div>
          </div>
        </header>
        <div class="aqiTop">
          <div>
            <div class="metric-label">AQI&nbsp;//&nbsp;US-EPA</div>
            <div class="aqiNum" id="aqi">--</div>
          </div>
          <div class="aqiLab" id="aqi-label">— AWAITING SENSOR —</div>
        </div>
        <div class="pmRow">
          <div class="pm"><div class="pm-name">PM 1.0</div><div class="pm-val"><span id="pm1">--</span><span class="pm-unit">µg/m³</span></div></div>
          <div class="pm"><div class="pm-name">PM 2.5</div><div class="pm-val"><span id="pm25">--</span><span class="pm-unit">µg/m³</span></div></div>
          <div class="pm"><div class="pm-name">PM 10</div><div class="pm-val"><span id="pm10">--</span><span class="pm-unit">µg/m³</span></div></div>
        </div>
        <div class="err" id="err-aq" hidden></div>
      </section>

      <!-- CONTAMINANT // VOC -->
      <section class="cell" id="cell-voc" data-state="pending">
        <header class="cellHead">
          <div>
            <div class="cellId">CONTAMINANT&nbsp;//&nbsp;VOC</div>
            <div class="cellSub">MQ-135 :: SPI0.0 :: SENS-03</div>
          </div>
          <div class="cellMeta">
            <div class="status" data-s="pending" id="st-voc"><span class="dot"></span>STANDBY</div>
            <div style="margin-top:6px" id="age-voc">ACQ&hellip;</div>
          </div>
        </header>
        <div class="vocRow">
          <div><div class="metric-label">VOC INDEX</div><div class="vocNum" id="voc">--</div></div>
          <div class="vocMv" id="voc-mv">-- mV</div>
        </div>
        <div class="vocBar" id="voc-bar"></div>
        <div class="vocScale"><span>CLEAR</span><span>50</span><span>SATURATED</span></div>
        <div class="err" id="err-voc" hidden></div>
      </section>
    </div>

    <!-- LOCATION -->
    <section class="cell locCell" id="cell-loc" data-state="ok">
      <header class="cellHead">
        <div>
          <div class="cellId">LOCATION&nbsp;//&nbsp;SATCOM</div>
          <div class="cellSub" id="loc-sub">ESRI WORLD IMAGERY :: ZOOM 16</div>
        </div>
        <div class="cellMeta">
          <div class="status" data-s="ok"><span class="dot"></span>FIX&nbsp;LOCK</div>
          <div style="margin-top:6px">RECON LINK</div>
        </div>
      </header>
      <div class="mapWrap locked" id="mapWrap">
        <div id="map"></div>
        <div class="mapOverlay"></div>
        <div class="mapCorners"><i></i></div>
        <div class="mapStamp">▲ TGT-01 // PRIMARY</div>
        <button class="reconBtn" id="reconBtn" type="button" data-on="0">▶ TAP TO RECON</button>
      </div>
      <dl class="coords">
        <dt>LAT</dt><dd id="c-lat">--</dd>
        <dt>LON</dt><dd id="c-lon">--</dd>
        <dt>PLUS</dt><dd id="c-plus">--</dd>
        <dt>LOCALITY</dt><dd id="c-label">--</dd>
      </dl>
    </section>
  </main>

  <footer class="foot">
    <span>COMMS <b id="f-comms">●LINK</b></span><span class="sep">|</span>
    <span>UPLINK <b>TAILSCALE FUNNEL</b></span><span class="sep">|</span>
    <span>TLS <b>LE</b></span><span class="sep">|</span>
    <span>POLL <b>3000ms</b></span><span class="sep">|</span>
    <span>LATENCY <b id="f-lat">-- ms</b></span><span class="sep">|</span>
    <span>UPTIME <b id="f-up">--</b></span>
  </footer>

</div>

<!-- Off-DOM SVG marker template: friendly green asset dot -->
<template id="reticle-tpl">
  <div class="reticle">
    <svg viewBox="-32 -32 64 64">
      <!-- outer expanding ping -->
      <circle class="ping-out" cx="0" cy="0" r="6" fill="none" stroke="#7eea7a" stroke-width="1.4"/>
      <!-- inner glow halo -->
      <circle cx="0" cy="0" r="9" fill="#7eea7a" opacity="0.18"/>
      <!-- solid green dot -->
      <circle cx="0" cy="0" r="5" fill="#7eea7a" stroke="#0a0d0a" stroke-width="1.2"/>
    </svg>
  </div>
</template>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<script>
const $ = (id) => document.getElementById(id);
const PAGE_LOADED_AT = performance.now();

// ----- TIME (Zulu + local), ticking once per second -----
function pad2(n){return n<10?'0'+n:''+n}
function tickClock(){
  const d = new Date();
  const z = `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}:${pad2(d.getUTCSeconds())} Z`;
  const l = `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())} ${
    new Intl.DateTimeFormat('en', {timeZoneName:'short'}).formatToParts(d).find(p=>p.type==='timeZoneName')?.value || 'LOCAL'
  }`;
  $('zulu').textContent = z;
  $('local').textContent = l;
}
tickClock(); setInterval(tickClock, 1000);

// ----- FORMATTERS -----
function fmt(v, d=1){
  if(v == null || isNaN(v)) return '--';
  if(Number.isInteger(v)) return ''+v;
  return v.toFixed(d);
}
function toDMS(deg, posChar, negChar){
  if(deg == null) return '--';
  const sign = deg < 0 ? negChar : posChar;
  deg = Math.abs(deg);
  const d = Math.floor(deg);
  const mFloat = (deg - d) * 60;
  const m = Math.floor(mFloat);
  const s = ((mFloat - m) * 60).toFixed(2);
  return `${d}° ${pad2(m)}' ${s.padStart(5,'0')}" ${sign}`;
}

// ----- STATUS LOGIC -----
function classify(metric, v){
  if(v == null || isNaN(v)) return 'pending';
  switch(metric){
    case 'temp': return (v>=10 && v<=35) ? 'ok' : (v>=0 && v<=42) ? 'warn' : 'crit';
    case 'hum':  return (v>=20 && v<=75) ? 'ok' : (v>=10 && v<=90) ? 'warn' : 'crit';
    case 'aqi':  return (v<=100) ? 'ok' : (v<=150) ? 'warn' : 'crit';
    case 'voc':  return (v<=50) ? 'ok' : (v<=75) ? 'warn' : 'crit';
  }
  return 'pending';
}
const STATUS_TEXT = {ok:'NOMINAL', warn:'CAUTION', crit:'CRITICAL', pending:'STANDBY'};
function applyCardStatus(prefix, state){
  $('cell-'+prefix).dataset.state = state;
  const s = $('st-'+prefix);
  s.dataset.s = state;
  s.innerHTML = `<span class="dot"></span>${STATUS_TEXT[state]}`;
}
function ageStr(updated){
  if(!updated) return 'ACQ…';
  const sec = Math.max(0, Math.round(Date.now()/1000 - updated));
  if(sec < 5) return 'LIVE T-'+sec+'s';
  if(sec < 60) return 'AGE '+sec+'s';
  return 'AGE '+Math.floor(sec/60)+'m'+(sec%60)+'s';
}
function fmtUptime(ms){
  const s = Math.floor(ms/1000);
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), ss = s%60;
  return `${pad2(h)}:${pad2(m)}:${pad2(ss)}`;
}

// ----- MAP -----
let map, marker;
function makeReticle(){
  const tpl = document.getElementById('reticle-tpl');
  return tpl.content.firstElementChild.cloneNode(true);
}
function initMap(lat, lon){
  if(map) return;
  map = L.map('map', {
    center:[lat, lon], zoom:16, zoomControl:false, attributionControl:false,
    dragging:false, scrollWheelZoom:false, doubleClickZoom:false, boxZoom:false, keyboard:false, touchZoom:false
  });
  // Esri World Imagery base + Reference boundaries/places overlay (no API key)
  L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
    maxZoom: 19, attribution: 'Tiles © Esri'
  }).addTo(map);
  L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}', {
    maxZoom: 19, opacity:0.85
  }).addTo(map);
  const icon = L.divIcon({
    className:'reticle-icon', html: makeReticle().outerHTML,
    iconSize:[64,64], iconAnchor:[32,32]
  });
  marker = L.marker([lat, lon], {icon, interactive:false}).addTo(map);

  // RECON toggle
  $('reconBtn').addEventListener('click', () => {
    const on = $('reconBtn').dataset.on === '1';
    const next = !on;
    $('reconBtn').dataset.on = next ? '1' : '0';
    $('reconBtn').textContent = next ? '■ RECON ENGAGED' : '▶ TAP TO RECON';
    const wrap = $('mapWrap');
    wrap.classList.toggle('locked', !next);
    const fn = next ? 'enable' : 'disable';
    map.dragging[fn](); map.scrollWheelZoom[fn](); map.doubleClickZoom[fn](); map.boxZoom[fn](); map.keyboard[fn](); map.touchZoom[fn]();
  });
}
function updateCoords(lat, lon, plus, label){
  $('c-lat').textContent = toDMS(lat, 'N', 'S');
  $('c-lon').textContent = toDMS(lon, 'E', 'W');
  $('c-plus').textContent = plus || '--';
  $('c-label').textContent = label || '--';
}

// ----- TICK -----
let lastLat = null, lastLon = null;
async function tick(){
  const t0 = performance.now();
  try{
    const r = await fetch('/api/now', {cache:'no-store'});
    if(!r.ok) throw new Error('HTTP '+r.status);
    const d = await r.json();
    const latency = Math.round(performance.now() - t0);
    $('f-lat').textContent = latency + ' ms';
    $('f-comms').innerHTML = '●LINK';
    $('f-comms').style.color = 'var(--ok)';
    $('live-state').textContent = 'LIVE';

    if(d.callsign) $('callsign').textContent = d.callsign;

    // --- thermal ---
    $('temp').textContent = fmt(d.temp_c);
    $('hum').textContent  = fmt(d.humidity_pct);
    const tBand = $('band-temp'); tBand.style.setProperty('--w', d.temp_c==null? '0%' : Math.min(100, Math.max(0, d.temp_c/40*100))+'%');
    const hBand = $('band-hum');  hBand.style.setProperty('--w', d.humidity_pct==null? '0%' : Math.min(100, Math.max(0, d.humidity_pct))+'%');
    const tState = classify('temp', d.temp_c);
    const hState = classify('hum', d.humidity_pct);
    tBand.dataset.s = tState; hBand.dataset.s = hState;
    let thState = (d.errors && d.errors.dht) ? 'crit'
                  : (d.temp_c == null) ? 'pending'
                  : ['crit','warn','ok'].find(s => [tState,hState].includes(s));
    applyCardStatus('th', thState);
    $('age-th').textContent = ageStr(d.dht_updated);
    if(d.errors && d.errors.dht){ $('err-th').textContent = d.errors.dht; $('err-th').hidden = false; }
    else { $('err-th').hidden = true; }

    // --- AQI ---
    $('aqi').textContent = fmt(d.aqi, 0);
    const lab = $('aqi-label');
    if(d.aqi_label){
      lab.textContent = d.aqi_label.toUpperCase();
      lab.style.borderColor = d.aqi_color || 'var(--accent)';
      lab.style.color = d.aqi_color || 'var(--text)';
      lab.style.background = 'rgba(0,0,0,0.4)';
    } else {
      lab.textContent = '— AWAITING SENSOR —';
    }
    $('pm1').textContent  = fmt(d.pm1_0, 0);
    $('pm25').textContent = fmt(d.pm2_5, 0);
    $('pm10').textContent = fmt(d.pm10, 0);
    let aqState = (d.errors && d.errors.pms && d.pm2_5 == null) ? 'pending'
                : (d.errors && d.errors.pms) ? 'crit'
                : classify('aqi', d.aqi);
    applyCardStatus('aq', aqState);
    $('age-aq').textContent = ageStr(d.pms_updated);
    if(d.errors && d.errors.pms && d.pm2_5 == null){ /* still pending, don't shout */ $('err-aq').hidden = true; }
    else if(d.errors && d.errors.pms){ $('err-aq').textContent = d.errors.pms; $('err-aq').hidden = false; }
    else { $('err-aq').hidden = true; }

    // --- VOC ---
    $('voc').textContent = fmt(d.voc_index, 0);
    $('voc-mv').textContent = (d.voc_mv == null ? '--' : d.voc_mv) + ' mV';
    $('voc-bar').style.setProperty('--w', d.voc_index==null? '0%' : Math.min(100, Math.max(0, d.voc_index))+'%');
    let vocState = (d.errors && d.errors.mq) ? 'crit'
                  : (d.voc_index == null) ? 'pending'
                  : classify('voc', d.voc_index);
    applyCardStatus('voc', vocState);
    $('age-voc').textContent = ageStr(d.mq_updated);
    if(d.errors && d.errors.mq){ $('err-voc').textContent = d.errors.mq; $('err-voc').hidden = false; }
    else { $('err-voc').hidden = true; }

    // --- LOCATION ---
    if(d.lat != null && d.lon != null){
      if(!map){ initMap(d.lat, d.lon); }
      else if(d.lat !== lastLat || d.lon !== lastLon){
        marker.setLatLng([d.lat, d.lon]); map.setView([d.lat, d.lon], map.getZoom());
      }
      lastLat = d.lat; lastLon = d.lon;
      updateCoords(d.lat, d.lon, d.loc_plus, d.loc_label);
    }

    // uptime
    $('f-up').textContent = fmtUptime(performance.now() - PAGE_LOADED_AT);
  } catch(e){
    $('f-comms').innerHTML = '●LOST';
    $('f-comms').style.color = 'var(--crit)';
    $('live-state').textContent = 'NO LINK';
  }
}
tick();
setInterval(tick, 3000);
setInterval(() => {
  // tick uptime + age strings each second so they don't only refresh on poll
  $('f-up').textContent = fmtUptime(performance.now() - PAGE_LOADED_AT);
}, 1000);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return  # silence default access log

    def _send(self, code, ctype, body, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/index.html":
            self._send(200, "text/html; charset=utf-8", INDEX_HTML)
        elif path == "/api/now":
            snap = _snapshot()
            snap["server_ts"] = datetime.now(timezone.utc).isoformat()
            self._send(200, "application/json", json.dumps(snap))
        elif path == "/healthz":
            self._send(200, "text/plain", "ok\n")
        elif path == "/favicon.svg" or path == "/favicon.ico":
            self._send(
                200,
                "image/svg+xml" if path.endswith(".svg") else "image/svg+xml",
                FAVICON_SVG,
                extra={"Cache-Control": "public, max-age=86400"},
            )
        else:
            self._send(404, "text/plain", "not found\n")


def main():
    DHT11Poller().start()
    PMS5003Poller().start()
    MQ135Poller().start()
    print(f"[weather-web] listening on http://{BIND_HOST}:{BIND_PORT}", flush=True)
    srv = ThreadingHTTPServer((BIND_HOST, BIND_PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
