# TildaGB

Android push notifications on your [Tildagon](https://tildagon.badge.emfcamp.org/)
badge, via [Gadgetbridge](https://gadgetbridge.org/).

The badge pretends to be a [Bangle.js](https://www.espruino.com/Bangle.js)
watch, so Gadgetbridge talks to it over its standard Bluetooth LE protocol —
no extra phone app or root required. Notifications pop up on the badge screen,
flash the LEDs, and collect in an inbox. The badge can also report battery,
step count, ring your phone ("find my phone"), and feed your phone's GPS fix to
other badge apps.

## Features

- **Notifications** — title + body shown on screen, app jumps to foreground,
  LEDs flash a per-app colour until you dismiss. Handles new / updated /
  dismissed notifications and incoming calls.
- **Inbox** — last 100 alerts, scroll and re-read, clear all. Notifications
  dismissed on the phone are removed from the badge too.
- **Per-app settings** — each app that sends an alert is learned automatically;
  pick its LED colour or mute it.
- **Battery** — reports charge %, voltage and charging state to Gadgetbridge.
- **Step counter** — feeds the badge's BMI270 step count into Gadgetbridge's
  activity tracking (toggle, off by default).
- **Find my phone** — make the phone ring from the badge (toggle).
- **GPS bridge** — receives the phone's GPS fix and re-publishes it as a
  `GPSEvent`, masquerading as the GPS hexpansion (VID `0x7CAB` / PID `0xBEAC`)
  so apps like [emf-speedometer](https://github.com/mbooth101/emf-speedometer)
  work unmodified (toggle, off by default).
- **Persistent pairing** — bonds are saved to flash and the badge advertises a
  stable address, so it reconnects automatically after a reboot.

## Requirements

- A Tildagon badge running **firmware 2.0.0-alpha.5 or newer**. Earlier
  firmware has a BLE bonding bug that breaks pairing — the app detects this on
  launch, shows an error, and exits.
- [Gadgetbridge](https://gadgetbridge.org/) on F-Droid 
- [Bangle.js Gadgetbridge](https://play.google.com/store/apps/details?id=com.espruino.gadgetbridge.banglejs) on Play Store.


## Pairing

1. Open the **Gadgetbridge** app on the badge — while not connected it shows
   the badge's id (`Tildagon ABCD`) and a QR code linking to the phone app.
2. In Gadgetbridge on your phone, scan for a new device and add
   **"Bangle.js Tildagon"** (match the last 4 hex digits shown on the badge to
   the device's MAC address).
3. Accept the pairing prompt. The badge status line shows `Encrypted` once
   bonded.

After this, the badge reconnects on its own whenever it and the phone are in
range.

> Re-pairing trouble? Clear **both** sides: on the badge use **Forget phone**,
> and in Android **Settings → Bluetooth** un-pair the device (resetting the
> Gadgetbridge app alone does *not* remove the OS-level bond). Then add it again.

## Using it

Buttons: **A** up · **D** down · **C** confirm/select · **F** back/minimise ·
**B** right · **E** left.

### Main screen
- Not connected → shows the pairing id + QR.
- Connected → the notification inbox.
- **Up/Down** scroll, **C** open an alert, **B** Apps screen, **E** Forget
  phone, **F** minimise. Select **[ Clear All ]** to empty the inbox.

### Apps screen (B)
Toggle rows at the top, then one row per learned app:
- **Steps** — report step count to Gadgetbridge.
- **GPS** — bridge the phone's GPS to badge apps.
- **Find Phone** — ring the phone (toggle on to ring, off to stop).
- **App rows** — **C** mutes/unmutes, **E/B** cycle the LED colour. A filled
  dot means enabled, a hollow circle means muted.

## Settings & data files

Written next to the app on the badge:
- `app_filters.json` — per-app colour/enable, plus the Steps and GPS toggles.
- `gb_secrets.json` — saved Bluetooth bond keys (cleared by **Forget phone**).

## How it works

TildaGB implements the Bangle.js side of the Gadgetbridge protocol over the
Nordic UART Service:

- Service `6e400001-…`, RX (phone→badge) `6e400002-…`, TX (badge→phone)
  `6e400003-…`.
- It advertises the NUS UUID and a name starting with `Bangle.js`, which is how
  Gadgetbridge recognises it.
- Messages are newline-framed JSON. Phone→badge commands arrive wrapped as
  `\x10GB({...})`; badge→phone messages are sent as raw JSON.
- Badge→phone lines are terminated with **`\r\n`**, not `\n`, to work around an
  off-by-one in Gadgetbridge's line splitter that would otherwise drop the
  final character and corrupt the JSON.
- A stable **public** BLE address is used so Gadgetbridge (which tracks the
  device by MAC) reconnects after a badge reboot.

## Credits

- Built on the [Tildagon badge software](https://github.com/emfcamp/badge-2024-software).
- Gadgetbridge / Bangle.js protocol reference:
  [espruino.com/Gadgetbridge](https://www.espruino.com/Gadgetbridge).
- GPS event interface matches the
  [GPS hexpansion](https://github.com/TechCabin/EMFBadge-Hexpansions-GPS).

## License

MIT — see the project for details.
