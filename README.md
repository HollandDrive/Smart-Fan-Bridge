# Smart Fan Bridge

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

MQTT bridge that seamlessly controls Tuya WiFi ceiling fans through Zigbee wall switches. Uses raw ZCL commands to bypass Tuya TS0004 firmware limitations, TinyTuya for local LAN fan control, and TCP monitoring for physical switch detection.

## The Problem

Tuya WiFi ceiling fans powered through Zigbee wall switches (like the TS0004) create a chicken-and-egg problem:

- The fan's WiFi module needs **power** (wall switch ON) to receive commands
- The wall switch needs **commands** to turn on, but physical button presses desync the Zigbee state
- Standard Zigbee2MQTT commands get ignored when the cached state doesn't match the physical relay
- The TS0004 firmware on endpoints 2-4 has broken state reporting (UNSUPPORTED_ATTRIBUTE)

## The Solution

This bridge solves all three problems:

1. **Raw ZCL commands** via `zigbee2mqtt/bridge/request/action` — bypasses all Z2M state caching and always toggles the physical relay, even after manual button presses
2. **TinyTuya local LAN control** — sub-second fan commands directly over your network, no Tuya cloud dependency
3. **TCP port monitoring** — detects when the fan loses power (physical switch off) by watching for the WiFi module going offline, without any Zigbee polling that could crash the switch

## Architecture (v6 — Combined Arjan)

```
Google Home → "Set the office fan to 50%"
    ↓
Arjan virtual fan device (class=fan, capabilities: onoff + fan_speed)
    ↓
Smart Fan Bridge (polls Homey API every 1s)
    ↓
    ├─ Fan offline? Raw ZCL ON → Zigbee switch endpoint (L2 relay clicks on)
    ├─ Wait for Tuya WiFi module to boot
    └─ TinyTuya → DP1=true, DP3=3 (fan ON, speed 3) — or just DP3 if already on

Google Home → "Turn on the office fan light"
    ↓
Arjan virtual light device (class=light, capabilities: onoff + dim + light_temperature)
    ↓
Smart Fan Bridge → Ensure L2 → TinyTuya → DP15=true (light ON)

Physical wall switch off → L2 cuts → Tuya loses power
    ↓
Smart Fan Bridge (TCP monitor every 2s)
    ↓
Fan IP:6668 unreachable (3 consecutive failures, ~6s) → grace period after sequences
    ↓
Reset Arjan fan + light to OFF (Google Home reflects it)
```

### Key behaviors

- **Auto-turn-on** — setting fan_speed while fan is off automatically turns the fan on at the new speed
- **Two-way state sync** — Tuya state changes (physical remote, app) reflect back to Arjan within ~3s
- **Sync-then-act** — before sending Tuya commands, bridge checks if Tuya already matches the desired state (skip no-ops, fix stale Arjan state)
- **Post-sequence sync** — immediately after any sequence, the bridge re-reads Tuya to update Arjan state
- **DP3 write debounce** — 5s window after writing fan speed, reverse-sync skips to avoid race conditions
- **Color temperature inversion** — Tuya FP9805 uses opposite polarity to Homey (DP17=0 is warm, 100 is cool); bridge inverts automatically
- **Independent L2 management** — L2 stays powered if either fan or light is on; only cuts when both are off

## Why Arjan virtual_switch (and not Homey's built-in virtual)?

Homey's built-in `virtualsocket` driver is locked to `onoff` only — no way to add `fan_speed` or sub-capabilities. The community **Virtual Devices** app by Arjan Kranenburg lets you pair virtual devices with arbitrary class + capability combinations, which is required for:

- `class=fan` + `fan_speed` capability → Google's FanSpeed trait (voice says "fan speed" not "brightness")
- `class=light` + `dim` + `light_temperature` → Google's Brightness + ColorTemperature traits
- Auto on-off semantics for `dim` (kept for compatibility)

## Requirements

- **Zigbee2MQTT** with an MQTT broker (Mosquitto)
- **Tuya WiFi ceiling fan** (tested with Point One / TAIDE FP9805)
- **Tuya TS0004 Zigbee switch** (or similar multi-gang switch)
- **Homey Self-Hosted** with the Virtual Devices (Arjan Kranenburg) community app installed
- **Python 3.9+** with `paho-mqtt` and `tinytuya`

## Setup

### 1. Get your Tuya local key

```bash
pip install tinytuya
python -m tinytuya wizard
```

You'll need a [Tuya IoT developer account](https://iot.tuya.com) (free) to pull device keys. One-time setup; keys don't expire unless devices are re-paired in Smart Life.

### 2. Find your Zigbee switch network address

In the Zigbee2MQTT frontend, go to your switch device → About → look for the network address (`nwkAddr`). You'll also need the IEEE address.

### 3. Pair Arjan virtual devices in Homey

Install the **Virtual Devices** community app by Arjan Kranenburg, then pair one device per fan:

**Per fan:**
- Class: **Fan**
- Capabilities: **On/Off**, **Fan Speed**

**Per light** (skip if no light installed):
- Class: **Light**
- Capabilities: **On/Off**, **Dimmable**, **Light Temperature**

Note the device IDs from Homey API — you'll need them for `fans_config.json`.

### 4. Configure `fans_config.json`

Copy the example below and customize. **Important:** keep this file out of version control if possible (contains Tuya local keys).

```json
[
  {
    "name": "Office Fan",
    "mode": "combined",
    "arjan_id": "YOUR-ARJAN-FAN-DEVICE-UUID",
    "arjan_light_id": "YOUR-ARJAN-LIGHT-DEVICE-UUID",
    "fan_motor_id": null,
    "tuya_device_id": "your-tuya-device-id",
    "tuya_ip": "192.168.x.x",
    "tuya_key": "your-local-key",
    "tuya_version": 3.3,
    "z2m_ieee": "0xYOUR_SWITCH_IEEE",
    "z2m_nwk": 12345,
    "z2m_endpoint": 2,
    "default_speed": 3
  }
]
```

Per-fan fields:
- `mode`: `"combined"` for the v6 architecture (Arjan virtual_switch with fan_speed); `"split"` for legacy (Homey virtualsocket)
- `arjan_id`: Arjan virtual fan device UUID (combined mode only)
- `arjan_light_id`: Arjan virtual light device UUID (optional — omit for fans without lights)
- `fan_motor_id`: Tuya driver device UUID (legacy split mode only; optional in combined mode)
- `tuya_device_id`, `tuya_ip`, `tuya_key`, `tuya_version`: TinyTuya local connection
- `z2m_ieee`, `z2m_nwk`, `z2m_endpoint`: Zigbee switch identification + relay endpoint
- `default_speed`: fallback fan speed (1-6) when bridge can't read current Arjan fan_speed

### 5. Configure environment variables

Copy `start_bridge.sh.example` to `start_bridge.sh` and fill in:

```bash
export MQTT_HOST=localhost
export HOMEY_URL=http://YOUR_HOMEY_IP:4859
export HOMEY_TOKEN=your-homey-api-token
export CONFIG_FILE=/root/fans_config.json
```

### 6. Install as a systemd service

```bash
sudo cp smart-fan-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable smart-fan-bridge
sudo systemctl start smart-fan-bridge
```

### 7. Sync devices in Google Home

Voice phrases that work after sync:
- `"Set the office fan to 30 percent"` → fan_speed=0.3 → DP3=2
- `"Increase the office fan"` → relative bump, FanSpeed trait
- `"Turn on the office fan light"` → DP15=true (with L2 on if needed)
- `"Set the office fan light to warm"` → DP17 (inverted)

## Configuration

### Per-fan config (`fans_config.json`)

See section 4 above for full schema.

### Bridge environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `MQTT_HOST` | MQTT broker host | `localhost` |
| `MQTT_PORT` | MQTT broker port | `1883` |
| `HOMEY_URL` | Homey local API URL | `http://192.168.30.10:4859` |
| `HOMEY_TOKEN` | Homey API bearer token (`<userId>:<sessionId>:<tokenSalt>`) | — |
| `CONFIG_FILE` | Path to fans_config.json | `/root/fans_config.json` |
| `POLL_INTERVAL` | Homey API poll interval (s) | `1.0` |
| `FAN_MONITOR_INTERVAL` | Tuya TCP probe interval (s) | `2.0` |
| `FAN_OFFLINE_THRESHOLD` | Consecutive failures before marking offline | `3` |
| `SEQUENCE_GRACE_PERIOD` | Skip offline detection for N seconds after a sequence (s) | `10.0` |
| `TUYA_BOOT_WAIT` | Initial wait after L2 power-on before retrying TinyTuya (s) | `3.0` |
| `TUYA_RETRY_INTERVAL` | Seconds between TinyTuya boot-wait retries | `3.0` |
| `TUYA_MAX_RETRIES` | Max TinyTuya boot-wait retries | `8` |

## Technical Details

### Why raw ZCL?

The Tuya TS0004 (`_TZ3000_ewgtuk8o`) has a firmware deficiency:
- Endpoints 2-4 accept genOnOff commands but don't support attribute reads or reporting
- Physical button presses send Tuya profile 4098 (manufacturer-specific) instead of standard profile 260
- Zigbee2MQTT ignores profile 4098 messages, causing permanent state desync
- Flooding the device with failed read/configReport requests crashes its Zigbee stack

Raw ZCL via `bridge/request/action` with explicit `network_address` bypasses all converter logic and reliably toggles the relay every time.

### Why TinyTuya instead of Tuya Cloud?

- **5-20× faster** — direct LAN TCP vs cloud round-trip
- **No cloud dependency** — works even if Tuya servers are down
- **Instant offline detection** — TCP connection drop = power loss
- **Local key never expires** — works indefinitely after one-time setup

### Why TCP monitoring instead of Zigbee polling?

- Zigbee polling (`/get`) on TS0004 endpoints 2-4 returns `UNSUPPORTED_ATTRIBUTE`
- These error responses overload the device's Zigbee stack, causing relay resets
- TCP monitoring is completely independent of Zigbee — no side effects

### Why fan_speed and not dim?

When the Arjan virtual fan device exposes `dim` (0..1), Google Home's cloud integration maps it to the **Brightness** trait — voice says "setting brightness to 30%". Switching to `fan_speed` triggers Google's **FanSpeed** trait — voice says "setting fan speed to 30%" and unlocks "increase / decrease" commands.

### Sync race fix (v6)

The bridge writes Tuya DP3 in one thread (poll_fan handler) while monitor_online runs sync_virtual_to_tuya in another. They can race: bridge writes DP3=2, sync immediately reads Tuya status which still shows DP3=5 (write hadn't propagated), sync "corrects" Arjan dim back to 0.83 — undoing the user's command.

Fix: 5-second debounce on reverse-sync after any DP3 write. Sync skips during that window; the user's value is authoritative.

## Tuya DPS Mapping (Point One / TAIDE FP9805)

| DP | Type | Description | Values |
|----|------|-------------|--------|
| 1 | bool | Fan power | `True`/`False` |
| 2 | enum | Fan mode | `"normal"`, `"sleep"`, `"nature"` |
| 3 | int | Fan speed | 1-6 |
| 8 | enum | Direction | `"forward"`, `"reverse"` |
| 15 | bool | Light power | `True`/`False` |
| 16 | int | Light brightness | 0-100 (even integers) |
| 17 | int | Color temperature | 0-100 (**0=warm, 100=cool** — inverted vs Homey convention) |
| 22 | enum | Timer | `"off"`, ... |

## Tested Hardware

- **Fan**: Point One PO-ECO (Threecubes, Singapore) — model FP9805_TAIDE
- **Switch**: Tuya TS0004 (`_TZ3000_ewgtuk8o`) 4-gang with neutral
- **Coordinator**: Sonoff Dongle Plus V2 (Ember/EZSP)
- **Platform**: Homey Self-Hosted on Proxmox LXC (Debian)
- **Virtual Devices app**: `com.arjankranenburg.virtual` (Homey community)

## Upgrade from v5

If migrating from v5 (split mode):

1. Pair new Arjan virtual fan + light devices per [step 3](#3-pair-arjan-virtual-devices-in-homey)
2. In `fans_config.json`, add `mode: "combined"`, `arjan_id`, `arjan_light_id` per fan; remove `virtual_fan_id`/`virtual_light_id`
3. Restart the bridge — the new code keeps backwards compatibility (split mode still works for unmigrated entries)
4. Once tested, delete the old virtualsocket fan triggers and Tuya driver devices in Homey
5. Update any flows that reference the old device IDs

## License

MIT
