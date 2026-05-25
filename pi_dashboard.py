#!/usr/bin/env python3
"""Pi OLED OPS Dashboard v5 — adds a message-overlay mode triggered by /tmp/pi_oled_msg."""
import os, time, threading, logging
from collections import deque

from pigpio_dht import DHT11 as _PigDHT
_dht_sensor = _PigDHT(4, timeout_secs=0.5)
def read_dht11():
    try:
        r = _dht_sensor.read()
    except Exception:
        return None, None
    if not r.get('valid'):
        return None, None
    t = r.get('temp_c'); h = r.get('humidity')
    if t is None or h is None or not (0 <= h <= 100):
        return None, None
    return t, h

import psutil
from luma.core.interface.serial import i2c
from luma.core.error import DeviceNotFoundError
from luma.oled.device import sh1106
from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('dashboard')

W, H = 128, 64
FP = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf'
F_HERO = ImageFont.truetype(FP, 20)
F_LBL  = ImageFont.truetype(FP, 8)
F_PCT  = ImageFont.truetype(FP, 10)
F_BAR  = ImageFont.truetype(FP, 8)
F_BOT  = ImageFont.truetype(FP, 8)
F_MSG_LABEL = ImageFont.truetype(FP, 7)

MSG_PATH = '/tmp/pi_oled_msg'
MSG_TTL = 20.0  # seconds

def make_device():
    delay = 1
    for attempt in range(30):
        try:
            d = sh1106(i2c(port=1, address=0x3C), width=W, height=H)
            d.persist = True
            d.contrast(255)
            log.info('OLED ready (attempt %d)', attempt + 1)
            return d
        except (OSError, DeviceNotFoundError) as e:
            log.warning('init attempt %d failed: %s', attempt + 1, e)
            time.sleep(delay)
            delay = min(delay + 1, 5)
    raise RuntimeError('OLED never came up')

device = make_device()

dht_state = {'t': None, 'h': None, 'last_ok': 0}
_lock = threading.Lock()

def dht_worker():
    while True:
        ok = False
        for _ in range(5):
            t, h = read_dht11()
            if t is not None and 0 <= t <= 60 and 0 <= h <= 100:
                with _lock:
                    dht_state.update({'t': t, 'h': h, 'last_ok': time.time()})
                log.info('DHT ok: t=%s°C h=%s%%', t, h)
                ok = True
                break
            time.sleep(2.5)
        time.sleep(5 if ok else 3)

threading.Thread(target=dht_worker, daemon=True).start()

def pi_cpu_temp():
    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            return int(f.read().strip()) / 1000
    except Exception:
        return None

def fmt_up(s):
    d, r = divmod(int(s), 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    return f'{d}d{h:02d}h' if d else f'{h}h{m:02d}m'

def tw(s, font):
    return font.getbbox(s)[2]

def get_pending_message():
    """Return (text, ttl_remaining) if /tmp/pi_oled_msg exists and is fresh; else (None, 0)."""
    try:
        st = os.stat(MSG_PATH)
        age = time.time() - st.st_mtime
        if age >= MSG_TTL:
            return None, 0
        with open(MSG_PATH) as f:
            txt = f.read().strip()
        if not txt:
            return None, 0
        return txt, MSG_TTL - age
    except (OSError, FileNotFoundError):
        return None, 0

def wrap_text(text, font, max_w):
    out = []
    for paragraph in text.split('\n'):
        words = paragraph.split()
        cur = ''
        for w in words:
            candidate = (cur + ' ' + w).strip() if cur else w
            if tw(candidate, font) <= max_w:
                cur = candidate
            else:
                if cur:
                    out.append(cur)
                cur = w
        out.append(cur)
    return out

def line_height(font):
    return font.getbbox('Hg')[3] + 1

def render_message(d, text, ttl):
    """Take over the screen with the message; show a TTL bar at top."""
    # outline border (1 px)
    d.rectangle((0, 0, W - 1, H - 1), outline=1, fill=0)
    # MSG label top-left
    d.text((3, 1), 'MSG', fill=1, font=F_MSG_LABEL)
    # TTL countdown bar — shrinks left-to-right
    bar_w = int((W - 36) * max(0, min(1, ttl / MSG_TTL)))
    d.line((30, 4, 30 + bar_w, 4), fill=1)
    # seconds remaining text top-right
    sec_str = f'{int(ttl)}s'
    sw = tw(sec_str, F_MSG_LABEL)
    d.text((W - sw - 3, 1), sec_str, fill=1, font=F_MSG_LABEL)
    # auto-size font to fit
    avail_w = W - 8
    avail_h = H - 14
    chosen_font = None
    chosen_lines = None
    chosen_lh = 0
    for size in (24, 22, 20, 18, 16, 14, 12, 10, 9, 8):
        font = ImageFont.truetype(FP, size)
        lines = wrap_text(text, font, avail_w)
        lh = line_height(font)
        if lh * len(lines) <= avail_h and len(lines) <= 6:
            chosen_font = font; chosen_lines = lines; chosen_lh = lh
            break
    if chosen_font is None:
        # text doesn't fit even at 8pt; truncate
        chosen_font = ImageFont.truetype(FP, 8)
        chosen_lh = line_height(chosen_font)
        chosen_lines = wrap_text(text, chosen_font, avail_w)[: avail_h // chosen_lh]
    total_h = chosen_lh * len(chosen_lines)
    start_y = 10 + (avail_h - total_h) // 2
    for i, ln in enumerate(chosen_lines):
        lw = tw(ln, chosen_font)
        d.text(((W - lw) // 2, start_y + i * chosen_lh), ln, fill=1, font=chosen_font)

def segmented_bar(d, x, y, w, h, pct, seg_w=4, gap=1):
    pct = max(0.0, min(100.0, pct))
    n_seg = (w + gap) // (seg_w + gap)
    filled = int(round(n_seg * pct / 100))
    for i in range(n_seg):
        sx = x + i * (seg_w + gap)
        if i < filled:
            d.rectangle((sx, y, sx + seg_w - 1, y + h - 1), outline=0, fill=1)
        else:
            d.rectangle((sx, y, sx + seg_w - 1, y + h - 1), outline=1, fill=0)

def draw_sparkline(d, x, y, w, h, hist):
    if not hist:
        return
    mx = max(max(hist), 1)
    n = min(w, len(hist))
    start = len(hist) - n
    for i in range(n):
        v = hist[start + i]
        bh = max(1, int(round((h - 1) * v / mx)))
        d.line((x + i, y + h - bh, x + i, y + h - 1), fill=1)

def render_dashboard(d, t, h, cpu_s, mem_s, pi_t, load, up, cpu_hist):
    if t is not None:
        tval = f'{int(t):2d}°'
        hval = f'{int(h):2d}%'
    else:
        tval = '--°'
        hval = '--%'
    tvw = tw(tval, F_HERO)
    hvw = tw(hval, F_HERO)
    col_w = W // 2
    d.text(((col_w - tvw) // 2, 0), tval, fill=1, font=F_HERO)
    d.text((col_w + (col_w - hvw) // 2, 0), hval, fill=1, font=F_HERO)
    d.line((col_w, 0, col_w, 28), fill=1)
    d.text((col_w // 2 - tw('TEMP', F_LBL) // 2, 22), 'TEMP', fill=1, font=F_LBL)
    d.text((col_w + col_w // 2 - tw('HUM', F_LBL) // 2, 22), 'HUM',  fill=1, font=F_LBL)
    d.line((0, 31, W - 1, 31), fill=1)
    d.text((0, 32), 'CPU', fill=1, font=F_BAR)
    segmented_bar(d, 20, 33, 78, 8, cpu_s)
    pcs = f'{int(cpu_s):3d}%'
    d.text((W - tw(pcs, F_PCT) - 1, 31), pcs, fill=1, font=F_PCT)
    d.text((0, 42), 'MEM', fill=1, font=F_BAR)
    segmented_bar(d, 20, 43, 78, 8, mem_s)
    pms = f'{int(mem_s):3d}%'
    d.text((W - tw(pms, F_PCT) - 1, 41), pms, fill=1, font=F_PCT)
    d.line((0, 53, W - 1, 53), fill=1)
    draw_sparkline(d, 0, 55, 56, 9, list(cpu_hist))
    pi_str = f'{int(pi_t)}°' if pi_t is not None else '--'
    bot = f'{pi_str} {fmt_up(up)} L{load:.1f}'
    bw = tw(bot, F_BOT)
    d.text((W - bw - 1, 55), bot, fill=1, font=F_BOT)

def safe_display(img):
    global device
    for attempt in range(3):
        try:
            device.display(img)
            return True
        except (OSError, DeviceNotFoundError) as e:
            log.warning('display fail %d: %s', attempt + 1, e)
            time.sleep(0.05 * (attempt + 1))
    log.error('display failed 3x, re-init')
    try:
        device = make_device()
    except Exception as e:
        log.error('re-init failed: %s', e)
    return False

def _splash_text_lines(d, lines, cursor_xy=None):
    """Render multiple lines starting at top-left + optional blinking cursor."""
    for i, line in enumerate(lines):
        d.text((2, 2 + i * 9), line, fill=1, font=F_LBL)
    if cursor_xy is not None:
        cx, cy = cursor_xy
        d.rectangle((cx, cy, cx + 4, cy + 7), fill=1)


def _splash_plant_frame(stage):
    """Return a PIL image of a pothos plant at growth stage 0..5 (0 = pot only)."""
    img = Image.new('1', (W, H), 0)
    d = ImageDraw.Draw(img)
    # Pot trapezoid bottom-center
    POT_X1, POT_X2, POT_TOP, POT_BOT = 52, 76, 52, 62
    d.line((POT_X1 - 3, POT_TOP, POT_X1, POT_BOT), fill=1)
    d.line((POT_X2 + 3, POT_TOP, POT_X2, POT_BOT), fill=1)
    d.line((POT_X1 - 3, POT_TOP, POT_X2 + 3, POT_TOP), fill=1)
    d.line((POT_X1, POT_BOT, POT_X2, POT_BOT), fill=1)
    # Rim hatch dots
    for x in range(POT_X1 - 2, POT_X2 + 3, 3):
        d.point((x, POT_TOP - 1), fill=1)
    if stage < 1:
        return img
    # stem
    stem_top = POT_TOP - 4 - (stage * 3)
    d.line((64, POT_TOP, 64, stem_top), fill=1)
    if stage >= 2:
        # left leaf
        d.ellipse((46, POT_TOP - 16, 60, POT_TOP - 6), outline=1)
        d.line((60, POT_TOP - 10, 64, POT_TOP - 8), fill=1)
    if stage >= 3:
        # right leaf
        d.ellipse((68, POT_TOP - 18, 82, POT_TOP - 8), outline=1)
        d.line((68, POT_TOP - 12, 64, POT_TOP - 10), fill=1)
    if stage >= 4:
        # top leaf (heart-shaped pothos)
        d.ellipse((57, stem_top - 8, 71, stem_top + 4), outline=1)
        d.line((64, stem_top + 4, 64, stem_top - 2), fill=1)
        # variegation dot
        d.point((61, stem_top - 1), fill=1)
        d.point((67, stem_top + 1), fill=1)
    if stage >= 5:
        # trailing vine
        d.line((POT_X1, POT_BOT - 2, POT_X1 - 4, POT_BOT + 1), fill=1)
        d.line((POT_X1 - 4, POT_BOT + 1, POT_X1 - 6, POT_BOT - 1), fill=1)
        d.line((POT_X2, POT_BOT - 2, POT_X2 + 4, POT_BOT + 1), fill=1)
    return img


def intro_animation():
    """6s boot splash: terminal log -> plant grows -> hero logo + progress."""
    import socket, subprocess
    log.info('intro animation start')
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = 'piyushpi'
    try:
        ip = subprocess.check_output(['hostname', '-I'], timeout=1).decode().strip().split()[0]
    except Exception:
        ip = '---'

    # --- Phase 1: blinking cursor (~0.6s) ---
    for blink in range(4):
        img = Image.new('1', (W, H), 0)
        d = ImageDraw.Draw(img)
        if blink % 2 == 0:
            d.rectangle((4, 4, 9, 11), fill=1)
        safe_display(img)
        time.sleep(0.15)

    # --- Phase 2: typewriter terminal (~2.2s) ---
    lines = [
        '> biome.init',
        f'> host..........{hostname}',
        '> i2c@0x3C......online',
        '> dht11.........ready',
        '> READY.',
    ]
    accum = []
    for line in lines:
        for i in range(1, len(line) + 1):
            cur_line = line[:i]
            img = Image.new('1', (W, H), 0)
            d = ImageDraw.Draw(img)
            for j, l in enumerate(accum):
                d.text((2, 2 + j * 9), l, fill=1, font=F_LBL)
            d.text((2, 2 + len(accum) * 9), cur_line, fill=1, font=F_LBL)
            # cursor block at end
            cx = 2 + len(cur_line) * 5
            d.rectangle((cx, 2 + len(accum) * 9, cx + 4, 2 + len(accum) * 9 + 7), fill=1)
            safe_display(img)
            time.sleep(0.025)
        accum.append(line)
        time.sleep(0.06)
    # Hold final terminal frame
    img = Image.new('1', (W, H), 0)
    d = ImageDraw.Draw(img)
    _splash_text_lines(d, accum)
    safe_display(img)
    time.sleep(0.35)

    # --- Phase 2.5: scanline wipe down (~0.4s) ---
    for y in range(0, H + 4, 4):
        img = Image.new('1', (W, H), 0)
        d = ImageDraw.Draw(img)
        if y < H:
            # remaining terminal text above wipe line
            for j, l in enumerate(accum):
                ly = 2 + j * 9
                if ly + 7 < y:
                    d.text((2, ly), l, fill=1, font=F_LBL)
            # the sweep line
            d.line((0, y, W - 1, y), fill=1)
        safe_display(img)
        time.sleep(0.018)

    # --- Phase 3: pothos grows (~1.5s) ---
    for stage in range(6):
        safe_display(_splash_plant_frame(stage))
        time.sleep(0.22)
    time.sleep(0.25)

    # --- Phase 4: hero logo + progress bar (~1.6s) ---
    logo = 'PIYUSHPI'
    sub = f'{ip}'
    sub2 = 'arm64 · 4GB · rev1.1'
    for pct in range(0, 101, 4):
        img = Image.new('1', (W, H), 0)
        d = ImageDraw.Draw(img)
        # marching ants top border
        for x in range(0, W, 3):
            d.point((x, 0), fill=1)
            d.point((x, H - 1), fill=1)
        # hero logo centered
        lw = tw(logo, F_HERO)
        d.text(((W - lw) // 2, 8), logo, fill=1, font=F_HERO)
        # ip line
        sw = tw(sub, F_LBL)
        d.text(((W - sw) // 2, 34), sub, fill=1, font=F_LBL)
        sw2 = tw(sub2, F_LBL)
        d.text(((W - sw2) // 2, 44), sub2, fill=1, font=F_LBL)
        # progress bar
        bx, by, bw, bh = 14, 55, W - 28, 5
        d.rectangle((bx, by, bx + bw, by + bh), outline=1)
        fill_w = max(0, (bw - 2) * pct // 100)
        d.rectangle((bx + 1, by + 1, bx + 1 + fill_w, by + bh - 1), fill=1)
        safe_display(img)
        time.sleep(0.04)

    # --- Phase 5: full-screen flash transition (~0.2s) ---
    img = Image.new('1', (W, H), 1)
    safe_display(img)
    time.sleep(0.08)
    img = Image.new('1', (W, H), 0)
    safe_display(img)
    time.sleep(0.08)
    log.info('intro animation done')

cpu_s = mem_s = 0.0
cpu_hist = deque([0]*64, maxlen=64)
psutil.cpu_percent(interval=None)
boot = psutil.boot_time()
last_msg = None

intro_animation()

while True:
    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory().percent
    cpu_s += (cpu - cpu_s) * 0.30
    mem_s += (mem - mem_s) * 0.30
    cpu_hist.append(cpu)
    with _lock:
        t = dht_state['t']; h_val = dht_state['h']
    pi_t = pi_cpu_temp()
    load = psutil.getloadavg()[0]
    up = time.time() - boot

    img = Image.new('1', (W, H), 0)
    d = ImageDraw.Draw(img)

    msg, ttl = get_pending_message()
    if msg is not None:
        if msg != last_msg:
            log.info('showing message: %r', msg[:80])
            last_msg = msg
        render_message(d, msg, ttl)
    else:
        if last_msg is not None:
            log.info('message cleared')
            last_msg = None
        render_dashboard(d, t, h_val, cpu_s, mem_s, pi_t, load, up, cpu_hist)

    safe_display(img)
    time.sleep(0.2)
