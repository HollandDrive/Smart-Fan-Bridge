"""
Office Fan MQTT Bridge v3
Controls the office ceiling fan via TinyTuya (local LAN) and manages
the Zigbee wall switch (L2) power via raw ZCL commands through Z2M.

Key breakthrough: Using zigbee2mqtt/bridge/request/action with raw ZCL
commands and explicit network_address bypasses all state caching and
ALWAYS toggles the relay, even after physical button presses.

Fan offline detection: TinyTuya TCP connection monitoring detects when
the fan loses power (L2 physically turned off).
"""

import json
import time
import threading
import urllib.request
import os
import logging
import socket

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

HOMEY_URL = os.environ.get("HOMEY_URL", "http://192.168.30.10:4859")
HOMEY_TOKEN = os.environ.get("HOMEY_TOKEN", "")
VIRTUAL_FAN_DEVICE_ID = os.environ.get("VIRTUAL_FAN_DEVICE_ID", "")

# TinyTuya config
TUYA_DEVICE_ID = os.environ.get("TUYA_DEVICE_ID", "bff3dbe507cd4046b8k2mx")
TUYA_IP = os.environ.get("TUYA_IP", "192.168.30.162")
TUYA_KEY = os.environ.get("TUYA_KEY", "")
TUYA_VERSION = float(os.environ.get("TUYA_VERSION", "3.3"))

# Zigbee switch config
Z2M_SWITCH_IEEE = os.environ.get("Z2M_SWITCH_IEEE", "0xa4c13886289e5594")
Z2M_SWITCH_NWK = int(os.environ.get("Z2M_SWITCH_NWK", "52415"))
Z2M_L2_ENDPOINT = int(os.environ.get("Z2M_L2_ENDPOINT", "2"))

# Timing
TUYA_BOOT_WAIT = float(os.environ.get("TUYA_BOOT_WAIT", "5.0"))
TUYA_RETRY_INTERVAL = float(os.environ.get("TUYA_RETRY_INTERVAL", "5.0"))
TUYA_MAX_RETRIES = int(os.environ.get("TUYA_MAX_RETRIES", "8"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))
FAN_MONITOR_INTERVAL = float(os.environ.get("FAN_MONITOR_INTERVAL", "3.0"))
DEFAULT_FAN_SPEED = int(os.environ.get("DEFAULT_FAN_SPEED", "3"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [fan-bridge] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fan-bridge")

# ---------------------------------------------------------------------------
# paho-mqtt & tinytuya
# ---------------------------------------------------------------------------
try:
    import paho.mqtt.client as mqtt
except ImportError:
    import subprocess
    subprocess.check_call(["pip", "install", "paho-mqtt"])
    import paho.mqtt.client as mqtt

try:
    import tinytuya
except ImportError:
    import subprocess
    subprocess.check_call(["pip", "install", "tinytuya"])
    import tinytuya

# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------
virtual_fan_last_state = None
sequence_running = False
sequence_end_time = 0
SEQUENCE_GRACE_PERIOD = 10.0
fan_was_online = None  # Track fan online/offline transitions
fan_offline_count = 0  # Consecutive offline detections
FAN_OFFLINE_THRESHOLD = 3  # Need 3 consecutive failures (~9s) to confirm offline
lock = threading.Lock()


# ---------------------------------------------------------------------------
# Raw ZCL command — bypasses Z2M state cache
# ---------------------------------------------------------------------------
def send_raw_zcl(mqtt_client, endpoint, command):
    """Send a raw genOnOff command to a specific endpoint via Z2M.
    command: 'on', 'off', or 'toggle'
    """
    payload = json.dumps({
        "action": "raw",
        "params": {
            "ieee_address": Z2M_SWITCH_IEEE,
            "network_address": Z2M_SWITCH_NWK,
            "dst_endpoint": endpoint,
            "cluster_key": "genOnOff",
            "zcl": {
                "frame_type": 1,
                "direction": 0,
                "command_key": command,
                "payload": {}
            }
        }
    })
    mqtt_client.publish("zigbee2mqtt/bridge/request/action", payload)
    log.info(f"Raw ZCL: endpoint {endpoint} → {command}")


# ---------------------------------------------------------------------------
# TinyTuya fan control
# ---------------------------------------------------------------------------
def get_tuya_device():
    d = tinytuya.Device(TUYA_DEVICE_ID, TUYA_IP, TUYA_KEY, version=TUYA_VERSION)
    d.set_socketTimeout(5)
    return d


def tuya_fan_on(speed=None):
    if speed is None:
        speed = DEFAULT_FAN_SPEED
    try:
        d = get_tuya_device()
        result = d.set_multiple_values({"1": True, "3": speed})
        d.close()
        if result and "Error" not in str(result):
            log.info(f"TinyTuya: fan ON speed {speed}")
            return True
        log.warning(f"TinyTuya: fan ON failed — {result}")
        return False
    except Exception as e:
        log.error(f"TinyTuya: fan ON error — {e}")
        return False


def tuya_fan_off():
    try:
        d = get_tuya_device()
        result = d.set_value(1, False)
        d.close()
        if result and "Error" not in str(result):
            log.info(f"TinyTuya: fan OFF")
            return True
        log.warning(f"TinyTuya: fan OFF failed — {result}")
        return False
    except Exception as e:
        log.error(f"TinyTuya: fan OFF error — {e}")
        return False


def is_fan_online():
    """Check if fan is reachable via TCP port 6668."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect((TUYA_IP, 6668))
        s.close()
        return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


# ---------------------------------------------------------------------------
# Homey API helpers
# ---------------------------------------------------------------------------
def homey_api(path, method="GET", data=None):
    url = f"{HOMEY_URL}/api/{path}"
    headers = {
        "Authorization": f"Bearer {HOMEY_TOKEN}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, headers=headers, method=method)
    if data is not None:
        req.data = json.dumps(data).encode()
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.error(f"Homey API error ({method} {path}): {e}")
        return None


def get_virtual_fan_state():
    result = homey_api(f"manager/devices/device/{VIRTUAL_FAN_DEVICE_ID}")
    if result and "capabilitiesObj" in result:
        return result["capabilitiesObj"].get("onoff", {}).get("value", None)
    return None


def set_virtual_fan(on: bool):
    homey_api(
        f"manager/devices/device/{VIRTUAL_FAN_DEVICE_ID}/capability/onoff",
        method="PUT",
        data={"value": on},
    )
    log.info(f"Virtual fan → {'ON' if on else 'OFF'}")


# ---------------------------------------------------------------------------
# Fan control sequences
# ---------------------------------------------------------------------------
def sequence_turn_on(mqtt_client):
    """Power on fan: raw ZCL L2 ON, wait for boot, TinyTuya fan on."""
    global sequence_running, sequence_end_time
    with lock:
        if sequence_running:
            log.info("Sequence already running, skipping")
            return
        sequence_running = True

    try:
        log.info("=== TURN ON ===")

        # Step 1: Send raw ZCL ON to L2 — always works regardless of state
        log.info("Step 1: Raw ZCL ON → endpoint 2")
        send_raw_zcl(mqtt_client, Z2M_L2_ENDPOINT, "on")

        # Step 2: Wait for Tuya WiFi module to boot
        log.info(f"Step 2: Waiting {TUYA_BOOT_WAIT}s for initial boot...")
        time.sleep(TUYA_BOOT_WAIT)

        # Step 3: Retry TinyTuya fan on until it works
        for attempt in range(1, TUYA_MAX_RETRIES + 1):
            log.info(f"Step 3: TinyTuya fan ON attempt {attempt}/{TUYA_MAX_RETRIES}")
            if tuya_fan_on(DEFAULT_FAN_SPEED):
                log.info("=== TURN ON complete ===")
                return
            log.info(f"Fan not ready, retry in {TUYA_RETRY_INTERVAL}s...")
            time.sleep(TUYA_RETRY_INTERVAL)

        log.warning("Could not turn on fan after all retries")
    finally:
        with lock:
            sequence_running = False
            sequence_end_time = time.time()


def sequence_turn_off(mqtt_client):
    """Turn off fan via TinyTuya, then cut L2 power."""
    global sequence_running, sequence_end_time
    with lock:
        if sequence_running:
            log.info("Sequence already running, skipping")
            return
        sequence_running = True

    try:
        log.info("=== TURN OFF ===")

        # Step 1: Turn off fan via TinyTuya
        log.info("Step 1: TinyTuya fan OFF")
        tuya_fan_off()
        time.sleep(2)

        # Step 2: Cut power via raw ZCL OFF
        log.info("Step 2: Raw ZCL OFF → endpoint 2")
        send_raw_zcl(mqtt_client, Z2M_L2_ENDPOINT, "off")

        log.info("=== TURN OFF complete ===")
    finally:
        with lock:
            sequence_running = False
            sequence_end_time = time.time()


# ---------------------------------------------------------------------------
# Fan online/offline monitor — detects physical L2 off
# ---------------------------------------------------------------------------
def monitor_fan_online(mqtt_client):
    """Monitor fan's network presence to detect L2 physical off."""
    global fan_was_online, virtual_fan_last_state, fan_offline_count

    while True:
        try:
            online = is_fan_online()

            with lock:
                is_running = sequence_running
                grace_elapsed = time.time() - sequence_end_time

            # Skip during sequences and grace period
            if is_running or grace_elapsed < SEQUENCE_GRACE_PERIOD:
                time.sleep(FAN_MONITOR_INTERVAL)
                continue

            if fan_was_online is None:
                fan_was_online = online
                fan_offline_count = 0
                log.info(f"Fan initial online state: {online}")
            elif not online:
                fan_offline_count += 1
                log.debug(f"Fan offline check {fan_offline_count}/{FAN_OFFLINE_THRESHOLD}")
                if fan_offline_count >= FAN_OFFLINE_THRESHOLD:
                    # Confirmed offline — reset virtual fan if it's ON
                    if virtual_fan_last_state:
                        log.info(f"Fan OFFLINE confirmed ({fan_offline_count} checks) — resetting virtual fan")
                        set_virtual_fan(False)
                        virtual_fan_last_state = False
                    fan_was_online = False
            elif online:
                if not fan_was_online:
                    log.info("Fan came ONLINE — L2 restored")
                fan_was_online = True
                fan_offline_count = 0

        except Exception as e:
            log.error(f"Fan monitor error: {e}")

        time.sleep(FAN_MONITOR_INTERVAL)


# ---------------------------------------------------------------------------
# MQTT callbacks
# ---------------------------------------------------------------------------
def on_connect(client, userdata, flags, rc, properties=None):
    log.info(f"MQTT connected (rc={rc})")
    client.publish("office-fan-bridge/status", "online", retain=True)


# ---------------------------------------------------------------------------
# Virtual fan state poller
# ---------------------------------------------------------------------------
def poll_virtual_fan(mqtt_client):
    """Poll Homey API for virtual fan state changes."""
    global virtual_fan_last_state

    while True:
        try:
            current = get_virtual_fan_state()

            if current is not None and current != virtual_fan_last_state:
                old = virtual_fan_last_state
                virtual_fan_last_state = current

                if old is None:
                    log.info(f"Virtual fan initial state: {'ON' if current else 'OFF'}")
                    continue

                with lock:
                    is_running = sequence_running
                if is_running:
                    continue

                if current:
                    log.info("Virtual fan ON — starting turn-on sequence")
                    threading.Thread(target=sequence_turn_on, args=(mqtt_client,), daemon=True).start()
                else:
                    log.info("Virtual fan OFF — starting turn-off sequence")
                    threading.Thread(target=sequence_turn_off, args=(mqtt_client,), daemon=True).start()

        except Exception as e:
            log.error(f"Poll error: {e}")

        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log.info("Starting Office Fan MQTT Bridge v3 (Raw ZCL + TinyTuya)")
    log.info(f"MQTT: {MQTT_HOST}:{MQTT_PORT}")
    log.info(f"Tuya: {TUYA_IP} (ID: {TUYA_DEVICE_ID})")
    log.info(f"Zigbee: {Z2M_SWITCH_IEEE} nwk:{Z2M_SWITCH_NWK} ep:{Z2M_L2_ENDPOINT}")
    log.info(f"Virtual fan: {VIRTUAL_FAN_DEVICE_ID}")
    log.info(f"Default speed: {DEFAULT_FAN_SPEED}")

    if not HOMEY_TOKEN:
        log.error("HOMEY_TOKEN not set!")
        return
    if not TUYA_KEY:
        log.error("TUYA_KEY not set!")
        return

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="office-fan-bridge",
    )
    client.on_connect = on_connect
    client.will_set("office-fan-bridge/status", payload="offline", retain=True)
    client.connect(MQTT_HOST, MQTT_PORT)

    # Start all background threads
    threading.Thread(target=poll_virtual_fan, args=(client,), daemon=True).start()
    threading.Thread(target=monitor_fan_online, args=(client,), daemon=True).start()
    log.info("Virtual fan poller + fan monitor started")

    try:
        client.loop_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        client.publish("office-fan-bridge/status", "offline", retain=True)
        client.disconnect()


if __name__ == "__main__":
    main()
