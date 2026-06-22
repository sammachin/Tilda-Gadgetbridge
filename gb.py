"""
Gadgetbridge BLE server for Tildagon badge.

Receives Android push notifications via the BangleJS protocol over
Nordic UART Service (NUS).  Advertises as a Bangle.js device so
Gadgetbridge recognizes it automatically.
"""

import bluetooth
import json
import binascii
from micropython import const

_DEVICE_NAME = "Bangle.js Tildagon"
_ADV_INTERVAL_US = const(100_000)
_MAX_BUFFER = const(2048)
_ENABLE_BONDING = True
_USE_LE_SECURE = True

_status_cb = None


def _log(event_type, message):
    if _status_cb:
        _status_cb(event_type, message)


# Use the controller's public address (from the chip's factory MAC) so the
# advertised BLE address stays stable across reboots.  Gadgetbridge tracks the
# device by MAC, so a changing (random) address breaks auto-reconnection.
_ADDR_MODE_PUBLIC = const(0x00)

_IRQ_CENTRAL_CONNECT = const(1)
_IRQ_CENTRAL_DISCONNECT = const(2)
_IRQ_GATTS_WRITE = const(3)
_IRQ_MTU_EXCHANGED = const(21)
_IRQ_ENCRYPTION_UPDATE = const(28)
_IRQ_GET_SECRET = const(29)
_IRQ_SET_SECRET = const(30)
_IRQ_PASSKEY_ACTION = const(31)

_IO_CAPABILITY_NO_INPUT_OUTPUT = const(3)
_PASSKEY_ACTION_NUMCMP = const(4)

_NUS_UUID = bluetooth.UUID("6e400001-b5a3-f393-e0a9-e50e24dcca9e")
_NUS_TX = (
    bluetooth.UUID("6e400003-b5a3-f393-e0a9-e50e24dcca9e"),
    bluetooth.FLAG_NOTIFY,
)
_NUS_RX = (
    bluetooth.UUID("6e400002-b5a3-f393-e0a9-e50e24dcca9e"),
    bluetooth.FLAG_WRITE | bluetooth.FLAG_WRITE_NO_RESPONSE,
)
_NUS_SERVICE = (_NUS_UUID, (_NUS_TX, _NUS_RX))

# Flags (LE General Discoverable | BR/EDR Not Supported) +
# Complete 128-bit NUS service UUID (little-endian)
_ADV_DATA = bytes((
    0x02, 0x01, 0x06,
    0x11, 0x07,
    0x9e, 0xca, 0xdc, 0x24, 0x0e, 0xe5, 0xa9, 0xe0,
    0x93, 0xf3, 0xa3, 0xb5, 0x01, 0x00, 0x40, 0x6e,
))


def _build_scan_response(name):
    name_bytes = name.encode()
    payload = bytearray()
    payload.append(len(name_bytes) + 1)
    payload.append(0x09)
    payload.extend(name_bytes)
    return bytes(payload)


class SecretStore:
    def __init__(self, path):
        self._path = path
        self._store = {}
        self._load()

    def _load(self):
        try:
            with open(self._path) as f:
                entries = json.load(f)
            for sec_type, key_hex, val_hex in entries:
                key = binascii.unhexlify(key_hex)
                val = binascii.unhexlify(val_hex)
                self._store[(sec_type, key)] = val
            _log("status", "Loaded %d bond(s)" % len(self._store))
        except (OSError, ValueError):
            pass

    def _save(self):
        entries = [
            [sec_type, binascii.hexlify(key).decode(), binascii.hexlify(val).decode()]
            for (sec_type, key), val in self._store.items()
        ]
        try:
            with open(self._path, "w") as f:
                json.dump(entries, f)
        except OSError:
            _log("error", "Failed to save bonds")

    def get(self, sec_type, index, key):
        if key is None:
            i = 0
            for (st, _k), val in self._store.items():
                if st == sec_type:
                    if i == index:
                        return val
                    i += 1
            return None
        key_bytes = bytes(key)
        result = self._store.get((sec_type, key_bytes), None)
        if result is not None:
            return result
        # NimBLE stores bonds keyed by the peer's identity address but
        # looks them up by the random address on reconnection.  The two
        # differ because the phone uses a resolvable random address.  Fall
        # back to returning any stored bond of the matching sec_type.
        for (st, _k), val in self._store.items():
            if st == sec_type:
                return val
        return None

    def set(self, sec_type, key, value):
        item_key = (sec_type, bytes(key))
        if value is None:
            if item_key in self._store:
                del self._store[item_key]
                self._save()
        else:
            self._store[item_key] = bytes(value)
            self._save()
        return True

    def has_bonds(self):
        return len(self._store) > 0

    def clear(self):
        self._store = {}
        self._save()
        _log("status", "Bonds cleared")


class GBClient:
    def __init__(self, on_notification=None, on_status=None, on_gps=None,
                 secrets_path="gb_secrets.json"):
        global _status_cb
        _status_cb = on_status
        self._on_notification = on_notification
        self._on_gps = on_gps
        self._gps_wanted = False
        self._ble = bluetooth.BLE()
        self._secrets = SecretStore(secrets_path)
        self._scan_response = _build_scan_response(_DEVICE_NAME)

        self._conn_handle = None
        self._tx_handle = None
        self._rx_handle = None
        self._rx_buffer = bytearray()
        self._encrypted = False
        self._mtu = 23  # default ATT MTU until negotiated

    def clear_bonds(self):
        self.disconnect()
        self._secrets.clear()

    def mac_suffix(self):
        """Last 4 hex digits of the BLE address, to identify this badge when
        pairing.  Must be called after start() (radio active)."""
        try:
            _addr_type, addr = self._ble.config("mac")
            return binascii.hexlify(bytes(addr)[-2:]).decode().upper()
        except Exception:
            return "????"

    def start(self):
        self._ble.active(True)
        try:
            self._ble.config(addr_mode=_ADDR_MODE_PUBLIC)
        except (OSError, ValueError):
            pass
        self._ble.config(gap_name=_DEVICE_NAME)
        self._ble.config(
            io=_IO_CAPABILITY_NO_INPUT_OUTPUT,
            mitm=False,
            le_secure=_USE_LE_SECURE,
            bond=_ENABLE_BONDING,
        )
        try:
            self._ble.config(mtu=256)
        except (OSError, ValueError):
            pass
        self._ble.irq(self._irq)

        ((self._tx_handle, self._rx_handle),) = (
            self._ble.gatts_register_services((_NUS_SERVICE,))
        )
        self._ble.gatts_set_buffer(self._rx_handle, 256)

        _log("status", "Starting")
        self._advertise()

    def disconnect(self):
        if self._conn_handle is not None:
            try:
                self._ble.gap_disconnect(self._conn_handle)
            except OSError:
                pass
            self._conn_handle = None

    def is_connected(self):
        return self._conn_handle is not None

    def poll(self):
        self._process_buffer()

    def _advertise(self):
        try:
            self._ble.gap_advertise(
                _ADV_INTERVAL_US,
                adv_data=_ADV_DATA,
                resp_data=self._scan_response,
                connectable=True,
            )
        except OSError:
            _log("error", "Advertise failed")
            return
        _log("status", "Advertising")

    def _irq(self, event, data):
        try:
            return self._irq_dispatch(event, data)
        except Exception as exc:
            import sys
            sys.print_exception(exc)
            return None

    def _irq_dispatch(self, event, data):
        if event == _IRQ_CENTRAL_CONNECT:
            conn_handle, _, _ = data
            self._conn_handle = conn_handle
            self._rx_buffer = bytearray()
            self._encrypted = False
            _log("status", "Connected")

        elif event == _IRQ_CENTRAL_DISCONNECT:
            self._conn_handle = None
            self._rx_buffer = bytearray()
            _log("status", "Disconnected")
            self._advertise()

        elif event == _IRQ_GATTS_WRITE:
            conn_handle, attr_handle = data
            if attr_handle == self._rx_handle:
                chunk = self._ble.gatts_read(self._rx_handle)
                self._rx_buffer.extend(chunk)

        elif event == _IRQ_ENCRYPTION_UPDATE:
            conn_handle, encrypted, _authenticated, bonded, _key_size = data
            if encrypted:
                self._encrypted = True
                _log("status", "Encrypted (bonded)" if bonded else "Encrypted")

        elif event == _IRQ_PASSKEY_ACTION:
            conn_handle, action, _passkey = data
            # No input/output: auto-confirm just-works numeric comparison.
            if action == _PASSKEY_ACTION_NUMCMP:
                self._ble.gap_passkey(conn_handle, action, 1)

        elif event == _IRQ_GET_SECRET:
            sec_type, index, key = data
            return self._secrets.get(sec_type, index, key)

        elif event == _IRQ_SET_SECRET:
            sec_type, key, value = data
            return self._secrets.set(sec_type, key, value)

        elif event == _IRQ_MTU_EXCHANGED:
            _conn_handle, mtu = data
            self._mtu = mtu

    def _process_buffer(self):
        while True:
            nl = self._rx_buffer.find(b"\n")
            if nl < 0:
                if len(self._rx_buffer) > _MAX_BUFFER:
                    self._rx_buffer = bytearray()
                break
            line = bytes(self._rx_buffer[:nl])
            self._rx_buffer = self._rx_buffer[nl + 1:]
            self._parse_message(line)

    def _parse_message(self, raw):
        try:
            if raw and raw[0] == 0x10:
                raw = raw[1:]
            text = raw.decode("latin-1")
            if text.startswith("GB(") and text.endswith(")"):
                text = text[3:-1]
            payload = json.loads(text)
            self._handle_payload(payload)
        except (ValueError, KeyError):
            pass

    def _handle_payload(self, payload):
        t = payload.get("t")
        if t == "notify":
            if self._on_notification:
                self._on_notification({
                    "event": "added",
                    "app": payload.get("src", ""),
                    "title": payload.get("title", ""),
                    "message": payload.get("body", ""),
                    "id": payload.get("id", 0),
                })
        elif t == "notify-":
            if self._on_notification:
                self._on_notification({
                    "event": "removed",
                    "id": payload.get("id", 0),
                })
        elif t == "notify~":
            if self._on_notification:
                self._on_notification({
                    "event": "modified",
                    "id": payload.get("id", 0),
                    "message": payload.get("body", ""),
                })
        elif t == "call":
            if payload.get("cmd") == "incoming" and self._on_notification:
                name = payload.get("name") or payload.get("number", "Unknown")
                self._on_notification({
                    "event": "added",
                    "app": "Phone",
                    "title": "Incoming Call",
                    "message": name,
                    "id": payload.get("id", 0),
                })
        elif t == "find":
            if payload.get("n") and self._on_notification:
                self._on_notification({"event": "find"})
        elif t == "gps":
            if self._on_gps:
                self._on_gps(payload)
        elif t == "is_gps_active":
            # Phone is asking whether we want GPS; answer with our state.
            self._send_gps_power()

    def _send(self, data):
        if self._conn_handle is None or self._tx_handle is None:
            return
        payload = data.encode()
        # Must go in a single notification.  Splitting across notifies risks a
        # dropped trailing packet, which leaves an unterminated line on the
        # phone that then swallows the next message.  If it won't fit the
        # negotiated MTU, skip it rather than send a partial line.
        if len(payload) > self._mtu - 3:
            return
        try:
            self._ble.gatts_notify(self._conn_handle, self._tx_handle, payload)
        except OSError:
            pass

    def send_status(self, bat=None, volt=None, chg=None):
        # Lines MUST end with "\r\n", not "\n".  Gadgetbridge's line splitter
        # has an off-by-one (substring(0, p-1)) that drops the character before
        # the newline; the "\r" absorbs it so the closing "}" survives.
        parts = ['"t":"status"']
        if bat is not None:
            parts.append('"bat":%d' % bat)
        if volt is not None:
            parts.append('"volt":%.2f' % volt)
        if chg is not None:
            parts.append('"chg":%d' % (1 if chg else 0))
        self._send("{" + ",".join(parts) + "}\r\n")

    def send_activity(self, steps):
        # Steps for this sample period.  No "rt" flag, so Gadgetbridge stores
        # the sample; no "ts", so it timestamps with the phone clock.
        self._send('{"t":"act","stp":%d}\r\n' % steps)

    def find_phone(self, on):
        # Make the phone ring (on=True) or stop (on=False).
        self._send('{"t":"findPhone","n":%s}\r\n' % ("true" if on else "false"))

    def request_gps(self, enable):
        # Ask the phone to start/stop streaming its GPS fix to us.
        self._gps_wanted = bool(enable)
        self._send_gps_power()

    def _send_gps_power(self):
        self._send('{"t":"gps_power","status":%s}\r\n'
                   % ("true" if self._gps_wanted else "false"))
