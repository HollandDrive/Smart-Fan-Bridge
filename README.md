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

## Architecture

```
Google Home → "Turn on the office fan"
    ↓
Homey (Virtual Fan Device) → state changes to ON
    ↓
Smart Fan Bridge (polls Homey API every 1s)
    ↓
    ├─ Raw ZCL ON → Zigbee switch endpoint 2 (L2 relay clicks on)
    ├─ Wait 5s for Tuya WiFi module to boot
    └─ TinyTuya → Fan ON + Speed 3 (direct LAN, <100ms)

Physical button press → L2 off → Fan loses power
    ↓
Smart Fan Bridge (TCP monitor every 3s)
    ↓
Fan IP:6668 unreachable (3 consecutive failures = ~9s)
    ↓
Reset Virtual Fan to OFF → Next voice command works again
```

## Requirements

- **Zigbee2MQTT** with an MQTT broker (Mosquitto)
- **Tuya WiFi ceiling fan** (tested with Point One / TAIDE FP9805)
- **Tuya TS0004 Zigbee switch** (or similar multi-gang switch)
- **Homey Self-Hosted** with a virtual fan device
- **Python 3.9+** with `paho-mqtt` and `tinytuya`

## Setup

### 1. Get your Tuya local key

```bash
pip install tinytuya
python -m tinytuya wizard
```

You'll need a [Tuya IoT developer account](https://iot.tuya.com) (free) to pull device keys. This is a one-time setup.

### 2. Find your Zigbee switch network address

In the Zigbee2MQTT frontend, go to your switch device → About → look for the network address (nwkAddr). You'll also need the IEEE address.

### 3. Create a Virtual Fan device in Homey

Enable Virtual Devices under Homey → Settings → Experiments, then create a Virtual Socket with `virtualClass: fan`. Note its device ID from the Homey API.

### 4. Configure environment variables

Copy `start_bridge.sh.example` to `start_bridge.sh` and fill in your values:

```bash
export MQTT_HOST=localhost
export HOMEY_URL=http://YOUR_HOMEY_IP:4859
export HOMEY_TOKEN=your-homey-api-token
export VIRTUAL_FAN_DEVICE_ID=your-virtual-fan-device-id
export TUYA_DEVICE_ID=your-tuya-device-id
export TUYA_IP=your-fan-ip
export TUYA_KEY=your-tuya-local-key
export TUYA_VERSION=3.3
export Z2M_SWITCH_IEEE=0xYOUR_SWITCH_IEEE
export Z2M_SWITCH_NWK=YOUR_NETWORK_ADDRESS
export DEFAULT_FAN_SPEED=3
```

### 5. Install as a systemd service

```bash
sudo cp office-fan-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable office-fan-bridge
sudo systemctl start office-fan-bridge
```

### 6. Add the Virtual Fan to Google Home

Sync devices in Google Home. The virtual fan will appear as a controllable device. Add it to the appropriate room.

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `MQTT_HOST` | MQTT broker host | `localhost` |
| `MQTT_PORT` | MQTT broker port | `1883` |
| `HOMEY_URL` | Homey API URL | `http://192.168.30.10:4859` |
| `HOMEY_TOKEN` | Homey API bearer token | — |
| `VIRTUAL_FAN_DEVICE_ID` | Homey virtual fan device ID | — |
| `TUYA_DEVICE_ID` | Tuya device ID | — |
| `TUYA_IP` | Fan's LAN IP address | — |
| `TUYA_KEY` | Tuya local encryption key | — |
| `TUYA_VERSION` | Tuya protocol version | `3.3` |
| `Z2M_SWITCH_IEEE` | Zigbee switch IEEE address | — |
| `Z2M_SWITCH_NWK` | Zigbee switch network address | — |
| `Z2M_L2_ENDPOINT` | Switch endpoint for fan circuit | `2` |
| `DEFAULT_FAN_SPEED` | Fan speed on turn-on (1-6) | `3` |
| `TUYA_BOOT_WAIT` | Seconds to wait for WiFi boot | `5.0` |
| `TUYA_RETRY_INTERVAL` | Seconds between TinyTuya retries | `5.0` |
| `TUYA_MAX_RETRIES` | Max TinyTuya connection attempts | `8` |
| `FAN_MONITOR_INTERVAL` | Seconds between TCP checks | `3.0` |
| `SEQUENCE_GRACE_PERIOD` | Ignore offline during sequence | `10.0` |

## Technical Details

### Why raw ZCL?

The Tuya TS0004 (`_TZ3000_ewgtuk8o`) has a firmware deficiency:

- **Endpoints 2-4** accept genOnOff commands but don't support attribute reads or reporting
- Physical button presses send Tuya profile 4098 (manufacturer-specific) instead of standard profile 260
- Zigbee2MQTT ignores profile 4098 messages, causing permanent state desync
- Flooding the device with failed read/configReport requests crashes its Zigbee stack

Raw ZCL via `bridge/request/action` with explicit `network_address` bypasses all converter logic and reliably toggles the relay every time.

### Why TinyTuya instead of Tuya Cloud?

- **5-20x faster** — direct LAN TCP vs cloud round-trip
- **No cloud dependency** — works even if Tuya servers are down
- **Instant offline detection** — TCP connection drop = power loss
- **Local key never expires** — works indefinitely after one-time setup

### Why TCP monitoring instead of Zigbee polling?

- Zigbee polling (`/get`) on TS0004 endpoints 2-4 returns `UNSUPPORTED_ATTRIBUTE`
- These error responses overload the device's Zigbee stack, causing relay resets
- TCP monitoring is completely independent of Zigbee — no side effects

## DPS Mapping (Point One / TAIDE FP9805 fans)

| DP | Type | Description | Values |
|----|------|-------------|--------|
| 1 | bool | Fan power | `True`/`False` |
| 2 | enum | Fan mode | `"normal"`, `"sleep"`, `"nature"` |
| 3 | int | Fan speed | 1-6 |
| 8 | enum | Direction | `"forward"`, `"reverse"` |
| 15 | bool | Light power | `True`/`False` |
| 16 | int | Light brightness | 10-1000 |
| 17 | int | Color temperature | 0-1000 |
| 22 | enum | Timer | `"off"`, ... |

## Tested Hardware

- **Fan**: Point One PO-ECO (Threecubes, Singapore) — model FP9805_TAIDE
- **Switch**: Tuya TS0004 (`_TZ3000_ewgtuk8o`) 4-gang with neutral
- **Coordinator**: Sonoff Dongle Plus V2 (Ember/EZSP)
- **Platform**: Homey Self-Hosted on Proxmox LXC (Debian)

## License

MIT
