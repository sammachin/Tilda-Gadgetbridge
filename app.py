import app
import asyncio
import json
import sys
from app_components import clear_background
from tildagonos import tildagonos
from system.eventbus import eventbus
from system.patterndisplay.events import PatternDisable, PatternEnable
from system.scheduler.events import RequestForegroundPushEvent
from events.input import Buttons, BUTTON_TYPES

_dir = __file__.rsplit("/", 1)[0] if "/" in __file__ else "."

GBClient = None
try:
    if _dir not in sys.path:
        sys.path.insert(0, _dir)
    from gb import GBClient
except Exception:
    pass

import power
import imu
import ota

# qr is a local module bundled with the app; degrade gracefully if absent.
try:
    from qr import QR
except Exception:
    QR = None

try:
    from events import Event
except Exception:
    Event = object

# Access the hexpansion manager so we can masquerade as the GPS hexpansion
# (VID 0x7CAB / PID 0xBEAC), letting existing GPS consumer apps discover us
# via get_app_by_vid_pid and receive phone GPS forwarded by Gadgetbridge.
try:
    from system.hexpansion import app as _hexapp
except Exception:
    _hexapp = None

GPS_VID = 0x7CAB
GPS_PID = 0xBEAC
_GPS_SLOT = 7  # synthetic slot; physical ports are 1-6, slot 0 is the top board

NUM_LEDS = 12
MAX_INBOX = 100
STATUS_INTERVAL_MS = 60_000
NUM_TOGGLES = 3  # Steps + GPS + Find Phone rows at the top of the Apps screen


class GPSEvent(Event):
    """Matches the GPS hexpansion's event: position=(lat,lon) deg,
    speed in knots, bearing in degrees true."""

    def __init__(self, position, speed, bearing):
        self.position = position
        self.speed = speed
        self.bearing = bearing

    def __str__(self):
        return "GPS fix %s, speed %s knots, bearing %s" % (
            self.position, self.speed, self.bearing)


class _GPSHeader:
    """Minimal duck-typed hexpansion header so get_slots_by_vid_pid matches."""
    vid = GPS_VID
    pid = GPS_PID
    eeprom_total_size = 0
    eeprom_page_size = 0
    fs_offset = 0
    friendly_name = "GPS (BLE)"

SETTINGS_PATH = _dir + "/app_filters.json"
SECRETS_PATH = _dir + "/gb_secrets.json"

COLORS = [
    ("Red", (255, 0, 0)),
    ("Green", (0, 200, 0)),
    ("Blue", (0, 100, 255)),
    ("Yellow", (255, 200, 0)),
    ("Cyan", (0, 200, 200)),
    ("Purple", (200, 0, 255)),
    ("Orange", (255, 100, 0)),
    ("White", (200, 200, 200)),
]

# A=UP  B=RIGHT  C=CONFIRM  D=DOWN  E=LEFT  F=CANCEL

VIEW_FW_ERROR = -1
VIEW_INBOX = 0
VIEW_ALERT = 1
VIEW_APPS = 2
VIEW_DETAIL = 3
VIEW_FORGET = 4


def _color_for(src):
    """Stable default color index for a never-before-seen app."""
    h = 0
    for c in src:
        h = (h * 31 + ord(c)) & 0xFFFF
    return h % len(COLORS)


def _truncate(text, max_len):
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[:max_len - 2] + ".."


def _wrap(text, width):
    lines = []
    for paragraph in text.split("\n"):
        while paragraph:
            if len(paragraph) <= width:
                lines.append(paragraph)
                break
            idx = paragraph.rfind(" ", 0, width + 1)
            if idx <= 0:
                idx = width
            lines.append(paragraph[:idx].rstrip())
            paragraph = paragraph[idx:].lstrip()
            if len(lines) >= 8:
                return lines
    return lines


# Minimum firmware with the BLE bonding fix: 2.0.0-alpha.5
_FW_MIN = (2, 0, 0, ("alpha", "5"))


def _parse_version(s):
    """(major, minor, patch, prerelease_tuple) or None for a dev/custom build.
    Empty prerelease tuple = a final release (ranks above any prerelease)."""
    if not s:
        return None
    s = s.strip().lstrip("v")
    if "+" in s:                       # drop build metadata
        s = s.split("+", 1)[0]
    pre = ()
    if "-" in s:
        s, pre_s = s.split("-", 1)
        pre = tuple(pre_s.split("."))
    parts = s.split(".")
    if not parts or not parts[0].isdigit():
        return None                    # e.g. "HEAD-HASH-NOTFOUND"
    nums = [int(p) if p.isdigit() else 0 for p in parts[:3]]
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2], pre)


def _pre_ge(a, b):
    """Semver prerelease precedence: True if a >= b. Empty tuple = release."""
    if not a:
        return True            # release (or equal-empty) >= anything
    if not b:
        return False           # b is release, a is a prerelease
    for x, y in zip(a, b):
        xd, yd = x.isdigit(), y.isdigit()
        if xd and yd:
            if int(x) != int(y):
                return int(x) > int(y)
        elif xd != yd:
            return not xd       # numeric identifiers rank below alphanumeric
        elif x != y:
            return x > y
    if len(a) != len(b):
        return len(a) > len(b)  # longer prerelease ranks higher
    return True                 # equal


def _firmware_ok(version_str):
    v = _parse_version(version_str)
    if v is None:
        return True             # unknown/dev build: don't nag
    if v[:3] != _FW_MIN[:3]:
        return v[:3] > _FW_MIN[:3]
    return _pre_ge(v[3], _FW_MIN[3])


class TildaGBApp(app.App):
    # Exposed so GPS consumer apps can do `provider.GPSEvent`.
    GPSEvent = GPSEvent

    def __init__(self):
        super().__init__()
        self.buttons = Buttons(self)
        self.view = VIEW_INBOX
        self.ble_status = "Starting..."
        self.ble_error = False

        self._load_settings()
        self._app_list = sorted(self.filters.keys())

        self.inbox = []
        self.current_alert = None
        self.flash_active = False
        self._flash_color = (0, 0, 0)
        self._flash_reassert = 0

        self._notif_queue = []
        self.inbox_idx = 0
        self.apps_idx = 0
        self.detail_item = None

        self.gb = None
        self._status_timer = STATUS_INTERVAL_MS  # send once shortly after start
        self._last_steps = None
        self.device_id = "????"

        self._gps_injected = False
        self._gps_position = None
        self._gps_speed = 0
        self._gps_bearing = 0

        self._finding_phone = False  # momentary action, not persisted

        # Firmware version check (BLE bonding fix landed in 2.0.0-alpha.5).
        self.fw_version = None
        try:
            self.fw_version = ota.get_version()
        except Exception:
            self.fw_version = None
        self.fw_ok = _firmware_ok(self.fw_version)

    # ---- GPS provider accessors (match the GPS hexpansion API) ----

    @property
    def position(self):
        return self._gps_position

    @property
    def speed(self):
        return self._gps_speed

    @property
    def bearing(self):
        return self._gps_bearing

    # ---- Battery ----

    def _battery_level(self):
        try:
            level = int(round(power.BatteryLevel()))
        except Exception:
            return None
        return max(0, min(100, level))

    def _send_status(self):
        if not self.gb or not self.gb.is_connected():
            return
        bat = self._battery_level()
        volt = None
        chg = None
        try:
            volt = power.Vbat()
        except Exception:
            pass
        try:
            chg = power.Vin() > 4.5
        except Exception:
            pass
        if bat is not None or volt is not None or chg is not None:
            self.gb.send_status(bat=bat, volt=volt, chg=chg)

    def _read_steps(self):
        try:
            return int(imu.step_counter_read())
        except Exception:
            return None

    def _send_steps(self):
        if not self.steps_enabled:
            self._last_steps = None
            return
        if not self.gb or not self.gb.is_connected():
            return
        total = self._read_steps()
        if total is None:
            return
        if self._last_steps is None:
            # First reading after enabling: set a baseline so we don't report
            # the whole boot-time count as one huge sample.
            self._last_steps = total
            return
        delta = total - self._last_steps
        if delta < 0:
            delta = total  # counter was reset
        self._last_steps = total
        if delta > 0:
            self.gb.send_activity(delta)

    # ---- GPS bridge ----

    def _on_gps(self, payload):
        # Called from gb.poll() (background_update context), not an IRQ.
        if not self.gps_enabled:
            return
        lat = payload.get("lat")
        lon = payload.get("lon")
        if lat is None or lon is None:
            return
        speed_kph = payload.get("speed") or 0
        speed_kn = speed_kph / 1.852  # Gadgetbridge sends kph; event uses knots
        if speed_kn < 1:
            speed_kn = 0
        bearing = payload.get("course") or 0
        self._gps_position = (round(lat, 5), round(lon, 5))
        self._gps_speed = speed_kn
        self._gps_bearing = bearing
        try:
            eventbus.emit(self.GPSEvent(
                self._gps_position, self._gps_speed, self._gps_bearing))
        except Exception:
            pass

    def _gps_set(self, enable):
        self.gps_enabled = enable
        self._save_settings()
        if enable:
            self._gps_inject()
        else:
            self._gps_remove()
            self._gps_position = None
            self._gps_speed = 0
            self._gps_bearing = 0
        if self.gb:
            self.gb.request_gps(enable)

    def _gps_manager(self):
        if _hexapp is None:
            return None
        return getattr(_hexapp, "_hexpansion_manager", None)

    def _gps_inject(self):
        if self._gps_injected:
            return
        mgr = self._gps_manager()
        if mgr is None:
            return
        try:
            # Defer to a real GPS hexpansion on a physical slot if present
            # (ignore our own synthetic slot / a stale entry from a prior run).
            for slot, hdr in mgr.hexpansion_headers.items():
                if slot == _GPS_SLOT:
                    continue
                if (hdr is not None and getattr(hdr, "vid", None) == GPS_VID
                        and getattr(hdr, "pid", None) == GPS_PID):
                    return
            # Overwrite our slot, replacing any stale entry from a prior run.
            mgr.hexpansion_headers[_GPS_SLOT] = _GPSHeader()
            mgr.hexpansion_apps[_GPS_SLOT] = self
            self._gps_injected = True
        except Exception:
            pass

    def _gps_remove(self):
        mgr = self._gps_manager()
        if mgr is not None:
            try:
                if mgr.hexpansion_headers.get(_GPS_SLOT) is not None:
                    del mgr.hexpansion_headers[_GPS_SLOT]
                if mgr.hexpansion_apps.get(_GPS_SLOT) is not None:
                    del mgr.hexpansion_apps[_GPS_SLOT]
            except Exception:
                pass
        self._gps_injected = False

    # ---- Persistence ----

    def _load_settings(self):
        try:
            with open(SETTINGS_PATH, "r") as f:
                data = json.load(f)
        except Exception:
            data = {}
        # New format: {"steps": bool, "apps": {...}}.  Old format was a flat
        # dict of app filters.
        if isinstance(data, dict) and "apps" in data:
            self.filters = data.get("apps", {})
            self.steps_enabled = bool(data.get("steps", False))
            self.gps_enabled = bool(data.get("gps", False))
        else:
            self.filters = data
            self.steps_enabled = False
            self.gps_enabled = False

    def _save_settings(self):
        try:
            with open(SETTINGS_PATH, "w") as f:
                json.dump({"steps": self.steps_enabled,
                           "gps": self.gps_enabled,
                           "apps": self.filters}, f)
        except Exception:
            pass

    def _filter_for(self, src):
        filt = self.filters.get(src)
        if filt is None:
            filt = {"enabled": True, "color": _color_for(src)}
            self.filters[src] = filt
            self._app_list = sorted(self.filters.keys())
            self._save_settings()
        return filt

    def _color_rgb(self, src):
        ci = self._filter_for(src).get("color", 0)
        return COLORS[ci % len(COLORS)][1]

    # ---- GB callbacks ----

    def _on_notification(self, notif):
        self._notif_queue.append(notif)

    def _on_status(self, event_type, message):
        self.ble_status = message
        self.ble_error = event_type == "error"

    # ---- Notification processing ----

    def _process_notification(self, notif):
        event = notif.get("event")

        if event == "removed":
            nid = notif.get("id")
            self.inbox = [n for n in self.inbox if n.get("id") != nid]
            if self.current_alert and self.current_alert.get("id") == nid:
                self._stop_flash()
                self.view = VIEW_INBOX
            return

        if event == "modified":
            nid = notif.get("id")
            for item in self.inbox:
                if item.get("id") == nid:
                    body = notif.get("message")
                    if body:
                        item["message"] = body
                    break
            return

        if event == "find":
            self._start_flash((255, 255, 255))
            self.current_alert = {
                "app": "Gadgetbridge",
                "title": "Find Device",
                "message": "",
            }
            self.view = VIEW_ALERT
            eventbus.emit(RequestForegroundPushEvent(self))
            return

        if event != "added":
            return

        src = notif.get("app", "")
        filt = self._filter_for(src)

        self.inbox.insert(0, notif)
        if len(self.inbox) > MAX_INBOX:
            self.inbox = self.inbox[:MAX_INBOX]

        if not filt.get("enabled", True):
            return

        ci = filt.get("color", 0)
        self._start_flash(COLORS[ci % len(COLORS)][1])
        self.current_alert = notif
        self.view = VIEW_ALERT
        eventbus.emit(RequestForegroundPushEvent(self))

    # ---- LED control ----

    def _write_leds(self, color):
        for i in range(1, NUM_LEDS + 1):
            tildagonos.leds[i] = color
        tildagonos.leds.write()

    def _start_flash(self, color):
        self._flash_color = color
        # Re-assert the disable for several frames so the pattern display
        # can't reclaim the LEDs while the app is being pushed to foreground.
        self._flash_reassert = 12
        eventbus.emit(PatternDisable())
        tildagonos.set_led_power(True)
        self.flash_active = True
        self._write_leds(color)

    def _stop_flash(self):
        if not self.flash_active:
            return
        self.flash_active = False
        self._flash_reassert = 0
        self._write_leds((0, 0, 0))
        eventbus.emit(PatternEnable())

    # ---- Background ----

    def background_update(self, delta):
        if self.gb:
            self.gb.poll()
            if self._finding_phone and not self.gb.is_connected():
                self._finding_phone = False
            self._status_timer += delta
            if self._status_timer >= STATUS_INTERVAL_MS:
                self._status_timer = 0
                self._send_status()
                self._send_steps()
        while self._notif_queue:
            self._process_notification(self._notif_queue.pop(0))

        # Hold the LEDs against the pattern display while a flash is active.
        # Rewriting every tick is cheap; the disable is only re-emitted for a
        # short window to survive the foreground transition without spamming
        # the eventbus.
        if self.flash_active:
            self._write_leds(self._flash_color)
            if self._flash_reassert > 0:
                self._flash_reassert -= 1
                eventbus.emit(PatternDisable())

    # ---- Lifecycle ----

    def update(self, delta):
        pass

    async def run(self, render_update):
        if not self.fw_ok:
            # The BLE bonding fix only exists in 2.0.0-alpha.5+.  The app can't
            # work without it, so show the error and exit on any button.
            self.view = VIEW_FW_ERROR
            await render_update()
            while True:
                for btn in ("CONFIRM", "CANCEL", "UP", "DOWN", "LEFT", "RIGHT"):
                    if self.buttons.get(BUTTON_TYPES[btn]):
                        self.buttons.clear()
                        self.minimise()
                        return
                await asyncio.sleep(0.05)
                await render_update()

        if GBClient is not None:
            try:
                self.gb = GBClient(
                    on_notification=self._on_notification,
                    on_status=self._on_status,
                    on_gps=self._on_gps,
                    secrets_path=SECRETS_PATH,
                )
                self.gb.start()
                self.device_id = self.gb.mac_suffix()
                if self.gps_enabled:
                    # Restore provider + ask the phone for GPS.
                    self._gps_inject()
                    self.gb.request_gps(True)
            except Exception as e:
                self.ble_status = "BLE: %s" % str(e)[:20]
                self.ble_error = True
        else:
            self.ble_status = "BLE unavailable"
            self.ble_error = True

        await render_update()

        while True:
            if self.view == VIEW_INBOX:
                self._handle_inbox()
            elif self.view == VIEW_ALERT:
                self._handle_alert()
            elif self.view == VIEW_APPS:
                self._handle_apps()
            elif self.view == VIEW_DETAIL:
                self._handle_detail()
            elif self.view == VIEW_FORGET:
                self._handle_forget()

            await asyncio.sleep(0.05)
            await render_update()

    # ---- Button handlers ----

    def _handle_inbox(self):
        n = len(self.inbox)
        total = n + (1 if n > 0 else 0)
        if self.buttons.get(BUTTON_TYPES["UP"]):
            self.buttons.clear()
            self.inbox_idx = max(0, self.inbox_idx - 1)
        elif self.buttons.get(BUTTON_TYPES["DOWN"]):
            self.buttons.clear()
            if total > 0:
                self.inbox_idx = min(total - 1, self.inbox_idx + 1)
        elif self.buttons.get(BUTTON_TYPES["CONFIRM"]):
            self.buttons.clear()
            if n > 0 and self.inbox_idx == n:
                self.inbox = []
                self.inbox_idx = 0
            elif self.inbox_idx < n:
                self.detail_item = self.inbox[self.inbox_idx]
                self.view = VIEW_DETAIL
        elif self.buttons.get(BUTTON_TYPES["RIGHT"]):
            self.buttons.clear()
            self.view = VIEW_APPS
            self.apps_idx = 0
        elif self.buttons.get(BUTTON_TYPES["LEFT"]):
            self.buttons.clear()
            self.view = VIEW_FORGET
        elif self.buttons.get(BUTTON_TYPES["CANCEL"]):
            self.buttons.clear()
            self.minimise()

    def _handle_alert(self):
        for btn in ("CONFIRM", "CANCEL", "UP", "DOWN", "LEFT", "RIGHT"):
            if self.buttons.get(BUTTON_TYPES[btn]):
                self.buttons.clear()
                self._stop_flash()
                self.view = VIEW_INBOX
                return

    def _handle_apps(self):
        n = len(self._app_list)
        total = NUM_TOGGLES + n  # toggle rows + app rows
        if self.buttons.get(BUTTON_TYPES["UP"]):
            self.buttons.clear()
            self.apps_idx = max(0, self.apps_idx - 1)
        elif self.buttons.get(BUTTON_TYPES["DOWN"]):
            self.buttons.clear()
            self.apps_idx = min(total - 1, self.apps_idx + 1)
        elif self.buttons.get(BUTTON_TYPES["CONFIRM"]):
            self.buttons.clear()
            if self.apps_idx == 0:
                self.steps_enabled = not self.steps_enabled
                if not self.steps_enabled:
                    self._last_steps = None
                self._save_settings()
            elif self.apps_idx == 1:
                self._gps_set(not self.gps_enabled)
            elif self.apps_idx == 2:
                self._finding_phone = not self._finding_phone
                if self.gb:
                    self.gb.find_phone(self._finding_phone)
            else:
                ai = self.apps_idx - NUM_TOGGLES
                if 0 <= ai < n:
                    src = self._app_list[ai]
                    f = self.filters[src]
                    f["enabled"] = not f.get("enabled", True)
                    self._save_settings()
        elif self.buttons.get(BUTTON_TYPES["LEFT"]):
            self.buttons.clear()
            ai = self.apps_idx - NUM_TOGGLES
            if 0 <= ai < n:
                src = self._app_list[ai]
                f = self.filters[src]
                f["color"] = (f.get("color", 0) - 1) % len(COLORS)
                self._save_settings()
        elif self.buttons.get(BUTTON_TYPES["RIGHT"]):
            self.buttons.clear()
            ai = self.apps_idx - NUM_TOGGLES
            if 0 <= ai < n:
                src = self._app_list[ai]
                f = self.filters[src]
                f["color"] = (f.get("color", 0) + 1) % len(COLORS)
                self._save_settings()
        elif self.buttons.get(BUTTON_TYPES["CANCEL"]):
            self.buttons.clear()
            self.view = VIEW_INBOX

    def _handle_detail(self):
        for btn in ("CONFIRM", "CANCEL"):
            if self.buttons.get(BUTTON_TYPES[btn]):
                self.buttons.clear()
                self.view = VIEW_INBOX
                return

    def _handle_forget(self):
        if self.buttons.get(BUTTON_TYPES["CONFIRM"]):
            self.buttons.clear()
            if self.gb:
                self.gb.clear_bonds()
                self.ble_status = "Cleared, forget on phone"
                self.ble_error = False
            self.view = VIEW_INBOX
        elif self.buttons.get(BUTTON_TYPES["CANCEL"]):
            self.buttons.clear()
            self.view = VIEW_INBOX

    # ---- Drawing ----

    def draw(self, ctx):
        clear_background(ctx)
        if self.view == VIEW_FW_ERROR:
            self._draw_fw_error(ctx)
        elif self.view == VIEW_INBOX:
            self._draw_inbox(ctx)
        elif self.view == VIEW_ALERT:
            self._draw_alert(ctx)
        elif self.view == VIEW_APPS:
            self._draw_apps(ctx)
        elif self.view == VIEW_DETAIL:
            self._draw_detail(ctx)
        elif self.view == VIEW_FORGET:
            self._draw_forget(ctx)

    def _draw_qr(self, ctx, cy, module):
        if QR is None:
            return
        n = len(QR)
        size = n * module
        x0 = -size // 2
        y0 = cy - size // 2
        border = module * 4
        # White quiet zone (4 modules, the spec minimum) so it scans reliably.
        ctx.rgb(1, 1, 1)
        ctx.rectangle(x0 - border, y0 - border,
                      size + 2 * border, size + 2 * border).fill()
        ctx.rgb(0, 0, 0)
        for r in range(n):
            row = QR[r]
            ry = y0 + r * module
            c = 0
            # Draw horizontal runs of dark modules as single rects.
            while c < n:
                if row[c] == "1":
                    c2 = c
                    while c2 < n and row[c2] == "1":
                        c2 += 1
                    ctx.rectangle(x0 + c * module, ry,
                                  (c2 - c) * module, module).fill()
                    c = c2
                else:
                    c += 1

    def _draw_inbox(self, ctx):
        ctx.save()
        ctx.text_align = ctx.CENTER

        # Header: device id (for pairing) + BLE status
        ctx.font_size = 12
        ctx.rgb(0.2, 0.7, 1.0)
        ctx.move_to(0, -90).text("Tildagon " + self.device_id)

        ctx.font_size = 10
        if self.ble_error:
            ctx.rgb(1.0, 0.3, 0.3)
        else:
            ctx.rgb(0.4, 0.4, 0.4)
        ctx.move_to(0, -76).text(_truncate(self.ble_status, 28))

        connected = bool(self.gb and self.gb.is_connected())
        n = len(self.inbox)
        if not connected:
            # Advertising / not paired: show the QR to install the phone app.
            self._draw_qr(ctx, 14, 3)
            ctx.font_size = 10
            ctx.rgb(0.5, 0.5, 0.5)
            ctx.move_to(0, 88).text("Scan to get the app")
        elif n == 0:
            ctx.font_size = 14
            ctx.rgb(0.5, 0.5, 0.5)
            ctx.move_to(0, 4).text("No notifications")
            ctx.font_size = 9
            ctx.rgb(0.3, 0.3, 0.3)
            ctx.move_to(0, 68).text("B:apps  E:forget")
            ctx.move_to(0, 80).text("F:back")
        else:
            total = n + 1
            visible = 6
            scroll = max(0, min(self.inbox_idx - visible // 2, total - visible))
            y = -58
            for i in range(scroll, min(scroll + visible, total)):
                sel = i == self.inbox_idx
                ctx.font_size = 14 if sel else 12

                if i < n:
                    alert = self.inbox[i]
                    src = alert.get("app", "")
                    name = src[:8]
                    title = _truncate(alert.get("title", ""), 14)
                    label = "%s: %s" % (name, title) if name else title

                    cr, cg, cb = self._color_rgb(src)
                    if sel:
                        ctx.rgb(1, 1, 0)
                    else:
                        ctx.rgb(cr / 255, cg / 255, cb / 255)
                else:
                    label = "[ Clear All ]"
                    if sel:
                        ctx.rgb(1, 1, 0)
                    else:
                        ctx.rgb(0.8, 0.3, 0.3)

                prefix = "> " if sel else "  "
                ctx.move_to(0, y).text(prefix + _truncate(label, 24))
                y += 20

            ctx.font_size = 9
            ctx.rgb(0.3, 0.3, 0.3)
            ctx.move_to(0, 68).text("C:view B:apps E:forget")
            ctx.move_to(0, 80).text("F:back")

        ctx.restore()

    def _draw_alert(self, ctx):
        if not self.current_alert:
            return
        a = self.current_alert
        cr, cg, cb = self._color_rgb(a.get("app", ""))

        ctx.save()
        ctx.text_align = ctx.CENTER

        ctx.font_size = 14
        ctx.rgb(cr / 255, cg / 255, cb / 255)
        ctx.move_to(0, -90).text(_truncate(a.get("app", ""), 20))

        ctx.font_size = 18
        ctx.rgb(1, 1, 1)
        ctx.move_to(0, -68).text(_truncate(a.get("title", ""), 20))

        msg = a.get("message", "")
        lines = _wrap(msg, 30)
        ctx.font_size = 18
        ctx.rgb(0.8, 0.8, 0.8)
        y = -38
        for line in lines[:5]:
            ctx.move_to(0, y).text(line)
            y += 22

        ctx.font_size = 9
        ctx.rgb(0.3, 0.3, 0.3)
        ctx.move_to(0, 85).text("press any button")

        ctx.restore()

    def _draw_apps(self, ctx):
        ctx.save()
        ctx.text_align = ctx.CENTER

        ctx.font_size = 18
        ctx.rgb(0.2, 0.7, 1.0)
        ctx.move_to(0, -92).text("Apps")

        n = len(self._app_list)
        total = NUM_TOGGLES + n  # toggle rows + apps
        visible = 5
        scroll = max(0, min(self.apps_idx - visible // 2, total - visible))
        if scroll < 0:
            scroll = 0
        y = -58
        for i in range(scroll, min(scroll + visible, total)):
            sel = i == self.apps_idx
            prefix = "> " if sel else "  "

            if i < NUM_TOGGLES:
                if i == 0:
                    on, label = self.steps_enabled, "Steps"
                elif i == 1:
                    on, label = self.gps_enabled, "GPS"
                else:
                    on, label = self._finding_phone, "Find Phone"
                ctx.font_size = 18
                tag = "ON" if on else "--"
                if sel:
                    ctx.rgb(1, 1, 0)
                elif on:
                    ctx.rgb(0.3, 0.8, 0.3)
                else:
                    ctx.rgb(0.5, 0.5, 0.5)
                ctx.text_align = ctx.CENTER
                ctx.move_to(0, y).text(prefix + "[%s] %s" % (tag, label))
            else:
                src = self._app_list[i - NUM_TOGGLES]
                filt = self.filters[src]
                enabled = filt.get("enabled", True)
                ci = filt.get("color", 0)
                cr, cg, cb = COLORS[ci % len(COLORS)][1]

                ctx.font_size = 18
                if sel:
                    ctx.rgb(1, 1, 0)
                elif enabled:
                    ctx.rgb(0.7, 0.7, 0.7)
                else:
                    ctx.rgb(0.4, 0.4, 0.4)

                ctx.text_align = ctx.LEFT
                ctx.move_to(-55, y).text(prefix + _truncate(src, 13))
                ctx.text_align = ctx.CENTER

                # Coloured dot = enabled, hollow circle = disabled
                if enabled:
                    ctx.rgb(cr / 255, cg / 255, cb / 255)
                    ctx.arc(72, y - 5, 6, 0, 6.283, 1).fill()
                else:
                    ctx.rgb(0.4, 0.4, 0.4)
                    ctx.line_width = 1
                    ctx.arc(72, y - 5, 6, 0, 6.283, 1).stroke()

            y += 24

        ctx.font_size = 12
        ctx.rgb(0.3, 0.3, 0.3)
        ctx.move_to(0, 68).text("C:toggle  E/B:color")
        ctx.move_to(0, 82).text("F:back")

        ctx.restore()

    def _draw_detail(self, ctx):
        if not self.detail_item:
            return
        a = self.detail_item
        cr, cg, cb = self._color_rgb(a.get("app", ""))

        ctx.save()
        ctx.text_align = ctx.CENTER

        ctx.font_size = 14
        ctx.rgb(cr / 255, cg / 255, cb / 255)
        ctx.move_to(0, -90).text(_truncate(a.get("app", ""), 20))

        ctx.font_size = 18
        ctx.rgb(1, 1, 1)
        ctx.move_to(0, -68).text(_truncate(a.get("title", ""), 20))

        msg = a.get("message", "")
        lines = _wrap(msg, 30)
        ctx.font_size = 18
        ctx.rgb(0.8, 0.8, 0.8)
        y = -38
        for line in lines[:5]:
            ctx.move_to(0, y).text(line)
            y += 22

        ctx.font_size = 9
        ctx.rgb(0.3, 0.3, 0.3)
        ctx.move_to(0, 85).text("C/F:back")

        ctx.restore()

    def _draw_forget(self, ctx):
        ctx.save()
        ctx.text_align = ctx.CENTER

        ctx.font_size = 20
        ctx.rgb(1, 0.3, 0.3)
        ctx.move_to(0, -25).text("Forget phone?")

        ctx.font_size = 14
        ctx.rgb(0.6, 0.6, 0.6)
        ctx.move_to(0, 5).text("Clears saved pairing")

        ctx.font_size = 14
        ctx.rgb(0.3, 0.3, 0.3)
        ctx.move_to(0, 40).text("C:yes  F:no")

        ctx.restore()

    def _draw_fw_error(self, ctx):
        ctx.save()
        ctx.text_align = ctx.CENTER

        ctx.font_size = 22
        ctx.rgb(1, 0.3, 0.3)
        ctx.move_to(0, -60).text("Firmware too old")

        ctx.font_size = 13
        ctx.rgb(0.8, 0.8, 0.8)
        ctx.move_to(0, -30).text("BLE bonding needs")
        ctx.move_to(0, -13).text("firmware 2.0.0-alpha.5+")

        ctx.font_size = 12
        ctx.rgb(0.6, 0.6, 0.6)
        ctx.move_to(0, 12).text("Installed: " + _truncate(self.fw_version or "?", 18))

        ctx.font_size = 12
        ctx.rgb(0.6, 0.6, 0.6)
        ctx.move_to(0, 40).text("Update the badge, then")
        ctx.move_to(0, 56).text("reopen this app")

        ctx.font_size = 10
        ctx.rgb(0.3, 0.3, 0.3)
        ctx.move_to(0, 82).text("press any button to exit")

        ctx.restore()


__app_export__ = TildaGBApp
