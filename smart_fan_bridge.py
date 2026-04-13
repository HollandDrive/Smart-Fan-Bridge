"""
Smart Fan Bridge v4
Controls ceiling fan motor + light via TinyTuya (local LAN) and manages
the Zigbee wall switch (L2) power via raw ZCL commands through Z2M.

Features:
- Fan on/off with speed control via Google Home voice
- Fan light on/off with brightness via Google Home voice
- Raw ZCL commands bypass Tuya TS0004 firmware limitations
- TinyTuya local LAN control (sub-second, no cloud)
- TCP monitoring detects physical switch off
- Light and fan are independent — light stays on when fan motor is off
- L2 stays on if either fan or light is active
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

# Virtual fan device (Google Home sees this for fan on/off)
VIRTUAL_FAN_DEVICE_ID = os.environ.get("VIRTUAL_FAN_DEVICE_ID", "")

# Tuya fan motor device in Homey (has onoff.light, dim, light_temperature)
FAN_MOTOR_DEVICE_ID = os.environ.get("FAN_MOTOR_DEVICE_ID", "")

# Virtual light device (Google Home sees this for light on/off)
VIRTUAL_LIGHT_DEVICE_ID = os.environ.get("VIRTUAL_LIGHT_DEVICE_ID", "")

# TinyTuya config
TUYA_DEVICE_ID = os.environ.get("TUYA_DEVICE_ID", "")
TUYA_IP = os.environ.get("TUYA_IP", "")
TUYA_KEY = os.environ.get("TUYA_KEY", "")
TUYA_VERSION = float(os.environ.get("TUYA_VERSION", "3.3"))

# Zigbee switch config
Z2M_SWITCH_IEEE = os.environ.get("Z2M_SWITCH_IEEE", "")
Z2M_SWITCH_NWK = int(os.environ.get("Z2M_SWITCH_NWK", "0"))
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
virtual_light_last_state = None
virtual_dim_last = None
virtual_colortemp_last = None
sequence_running = False
sequence_end_time = 0
SEQUENCE_GRACE_PERIOD = 10.0
fan_was_online = None
fan_offline_count = 0
FAN_OFFLINE_THRESHOLD = 3
lock = threading.Lock()


# ---------------------------------------------------------------------------
# Raw ZCL command — bypasses Z2M state cache
# ---------------------------------------------------------------------------
def send_raw_zcl(mqtt_client, endpoint, command):
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
# TinyTuya fan + light control
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
            log.info("TinyTuya: fan OFF")
            return True
        log.warning(f"TinyTuya: fan OFF failed — {result}")
        return False
    except Exception as e:
        log.error(f"TinyTuya: fan OFF error — {e}")
        return False


def tuya_light_on():
    try:
        d = get_tuya_device()
        result = d.set_value(15, True)
        d.close()
        if result and "Error" not in str(result):
            log.info("TinyTuya: light ON")
            return True
        log.warning(f"TinyTuya: light ON failed — {result}")
        return False
    except Exception as e:
        log.error(f"TinyTuya: light ON error — {e}")
        return False


def tuya_light_off():
    try:
        d = get_tuya_device()
        result = d.set_value(15, False)
        d.close()
        if result and "Error" not in str(result):
            log.info("TinyTuya: light OFF")
            return True
        log.warning(f"TinyTuya: light OFF failed — {result}")
        return False
    except Exception as e:
        log.error(f"TinyTuya: light OFF error — {e}")
        return False


def tuya_set_brightness(homey_dim):
    """Set brightness. homey_dim: 0.0-1.0 → DP16: 0-100 (even int)."""
    raw = int(round(homey_dim * 100))
    raw = max(0, min(100, raw))
    if raw % 2 != 0:
        raw -= 1
    try:
        d = get_tuya_device()
        result = d.set_value(16, raw)
        d.close()
        if result and "Error" not in str(result):
            log.info(f"TinyTuya: brightness {raw}%")
            return True
        log.warning(f"TinyTuya: brightness failed — {result}")
        return False
    except Exception as e:
        log.error(f"TinyTuya: brightness error — {e}")
        return False


def tuya_set_colortemp(homey_temp):
    """Set color temp. homey_temp: 0.0 (warm) - 1.0 (cool) → DP17: 0-100."""
    raw = int(round(homey_temp * 100))
    raw = max(0, min(100, raw))
    if raw % 2 != 0:
        raw -= 1
    try:
        d = get_tuya_device()
        result = d.set_value(17, raw)
        d.close()
        if result and "Error" not in str(result):
            log.info(f"TinyTuya: color temp {raw}")
            return True
        log.warning(f"TinyTuya: color temp failed — {result}")
        return False
    except Exception as e:
        log.error(f"TinyTuya: color temp error — {e}")
        return False


def is_fan_online():
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
        method="PUT", data={"value": on},
    )
    log.info(f"Virtual fan → {'ON' if on else 'OFF'}")


def get_virtual_light_state():
    """Get light on/off from the virtual light device."""
    result = homey_api(f"manager/devices/device/{VIRTUAL_LIGHT_DEVICE_ID}")
    if result and "capabilitiesObj" in result:
        return result["capabilitiesObj"].get("onoff", {}).get("value", None)
    return None


def get_fan_motor_light_details():
    """Get dim and colortemp from the Tuya fan device in Homey."""
    result = homey_api(f"manager/devices/device/{FAN_MOTOR_DEVICE_ID}")
    if result and "capabilitiesObj" in result:
        caps = result["capabilitiesObj"]
        return {
            "dim": caps.get("dim", {}).get("value"),
            "colortemp": caps.get("light_temperature", {}).get("value"),
        }
    return None


def set_virtual_light(on: bool):
    """Set the virtual light device state."""
    homey_api(
        f"manager/devices/device/{VIRTUAL_LIGHT_DEVICE_ID}/capability/onoff",
        method="PUT", data={"value": on},
    )
    log.info(f"Virtual light → {'ON' if on else 'OFF'}")


# ---------------------------------------------------------------------------
# Ensure L2 is on (for both fan and light commands)
# ---------------------------------------------------------------------------
def ensure_l2_on(mqtt_client):
    """Send raw ZCL ON to L2 and wait for Tuya boot if needed."""
    online = is_fan_online()
    if online:
        return True  # Already powered

    log.info("Fan offline — powering L2 on")
    send_raw_zcl(mqtt_client, Z2M_L2_ENDPOINT, "on")
    time.sleep(TUYA_BOOT_WAIT)

    for attempt in range(1, TUYA_MAX_RETRIES + 1):
        if is_fan_online():
            log.info(f"Fan online after {attempt} checks")
            return True
        log.info(f"Waiting for fan boot... attempt {attempt}/{TUYA_MAX_RETRIES}")
        time.sleep(TUYA_RETRY_INTERVAL)

    log.warning("Fan did not come online after L2 on")
    return False


# ---------------------------------------------------------------------------
# Fan control sequences
# ---------------------------------------------------------------------------
def sequence_turn_on(mqtt_client):
    """Power on fan: ensure L2, TinyTuya fan on."""
    global sequence_running, sequence_end_time
    with lock:
        if sequence_running:
            log.info("Sequence already running, skipping")
            return
        sequence_running = True

    try:
        log.info("=== FAN ON ===")
        if ensure_l2_on(mqtt_client):
            for attempt in range(1, 4):
                if tuya_fan_on(DEFAULT_FAN_SPEED):
                    log.info("=== FAN ON complete ===")
                    return
                time.sleep(2)
            log.warning("Fan ON failed after retries")
        else:
            log.warning("Could not power L2")
    finally:
        with lock:
            sequence_running = False
            sequence_end_time = time.time()


def sequence_turn_off(mqtt_client):
    """Turn off fan. Only cut L2 if light is also off."""
    global sequence_running, sequence_end_time
    with lock:
        if sequence_running:
            log.info("Sequence already running, skipping")
            return
        sequence_running = True

    try:
        log.info("=== FAN OFF ===")
        tuya_fan_off()
        time.sleep(2)

        # Only cut L2 if light is also off
        if not virtual_light_last_state:
            log.info("Light is off — cutting L2")
            send_raw_zcl(mqtt_client, Z2M_L2_ENDPOINT, "off")
        else:
            log.info("Light is on — keeping L2 powered")

        log.info("=== FAN OFF complete ===")
    finally:
        with lock:
            sequence_running = False
            sequence_end_time = time.time()


# ---------------------------------------------------------------------------
# Light control sequences
# ---------------------------------------------------------------------------
def sequence_light_on(mqtt_client):
    """Turn on fan light: ensure L2, TinyTuya light on."""
    global sequence_running, sequence_end_time
    with lock:
        if sequence_running:
            log.info("Sequence already running, skipping")
            return
        sequence_running = True

    try:
        log.info("=== LIGHT ON ===")
        if ensure_l2_on(mqtt_client):
            for attempt in range(1, 4):
                if tuya_light_on():
                    log.info("=== LIGHT ON complete ===")
                    return
                time.sleep(2)
            log.warning("Light ON failed after retries")
        else:
            log.warning("Could not power L2")
    finally:
        with lock:
            sequence_running = False
            sequence_end_time = time.time()


def sequence_light_off(mqtt_client):
    """Turn off fan light. Only cut L2 if fan motor is also off."""
    global sequence_running, sequence_end_time
    with lock:
        if sequence_running:
            log.info("Sequence already running, skipping")
            return
        sequence_running = True

    try:
        log.info("=== LIGHT OFF ===")
        tuya_light_off()
        time.sleep(2)

        # Only cut L2 if fan is also off
        if not virtual_fan_last_state:
            log.info("Fan is off — cutting L2")
            send_raw_zcl(mqtt_client, Z2M_L2_ENDPOINT, "off")
        else:
            log.info("Fan is on — keeping L2 powered")

        log.info("=== LIGHT OFF complete ===")
    finally:
        with lock:
            sequence_running = False
            sequence_end_time = time.time()


# ---------------------------------------------------------------------------
# Fan online/offline monitor — detects physical L2 off
# ---------------------------------------------------------------------------
def monitor_fan_online(mqtt_client):
    global fan_was_online, virtual_fan_last_state, virtual_light_last_state, fan_offline_count

    while True:
        try:
            online = is_fan_online()

            with lock:
                is_running = sequence_running
                grace_elapsed = time.time() - sequence_end_time

            if is_running or grace_elapsed < SEQUENCE_GRACE_PERIOD:
                time.sleep(FAN_MONITOR_INTERVAL)
                continue

            if fan_was_online is None:
                fan_was_online = online
                fan_offline_count = 0
                log.info(f"Fan initial online state: {online}")
            elif not online:
                fan_offline_count += 1
                if fan_offline_count >= FAN_OFFLINE_THRESHOLD:
                    if virtual_fan_last_state:
                        log.info(f"Fan OFFLINE ({fan_offline_count} checks) — resetting virtual fan")
                        set_virtual_fan(False)
                        virtual_fan_last_state = False
                    if virtual_light_last_state:
                        log.info(f"Fan OFFLINE ({fan_offline_count} checks) — resetting light")
                        set_virtual_light(False)
                        virtual_light_last_state = False
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
                    log.info("Virtual fan ON — starting fan-on sequence")
                    threading.Thread(target=sequence_turn_on, args=(mqtt_client,), daemon=True).start()
                else:
                    log.info("Virtual fan OFF — starting fan-off sequence")
                    threading.Thread(target=sequence_turn_off, args=(mqtt_client,), daemon=True).start()

        except Exception as e:
            log.error(f"Fan poll error: {e}")

        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Fan light state poller
# ---------------------------------------------------------------------------
def poll_fan_light(mqtt_client):
    global virtual_light_last_state, virtual_dim_last, virtual_colortemp_last

    while True:
        try:
            # Poll virtual light device for on/off (Google Home triggers this)
            light_on = get_virtual_light_state()

            if light_on is not None and light_on != virtual_light_last_state:
                old = virtual_light_last_state
                virtual_light_last_state = light_on

                if old is None:
                    log.info(f"Light initial state: {'ON' if light_on else 'OFF'}")
                else:
                    with lock:
                        is_running = sequence_running
                    if not is_running:
                        if light_on:
                            log.info("Light ON — starting light-on sequence")
                            threading.Thread(target=sequence_light_on, args=(mqtt_client,), daemon=True).start()
                        else:
                            log.info("Light OFF — starting light-off sequence")
                            threading.Thread(target=sequence_light_off, args=(mqtt_client,), daemon=True).start()

            # Poll fan motor device for brightness/colortemp changes
            details = get_fan_motor_light_details()
            if details:
                dim = details["dim"]
                colortemp = details["colortemp"]

                if dim is not None and dim != virtual_dim_last:
                    virtual_dim_last = dim
                    if virtual_light_last_state and is_fan_online():
                        log.info(f"Brightness changed to {dim}")
                        tuya_set_brightness(dim)

                if colortemp is not None and colortemp != virtual_colortemp_last:
                    virtual_colortemp_last = colortemp
                    if virtual_light_last_state and is_fan_online():
                        log.info(f"Color temp changed to {colortemp}")
                        tuya_set_colortemp(colortemp)

        except Exception as e:
            log.error(f"Light poll error: {e}")

        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log.info("Starting Smart Fan Bridge v4 (Fan + Light)")
    log.info(f"MQTT: {MQTT_HOST}:{MQTT_PORT}")
    log.info(f"Tuya: {TUYA_IP} (ID: {TUYA_DEVICE_ID})")
    log.info(f"Zigbee: {Z2M_SWITCH_IEEE} nwk:{Z2M_SWITCH_NWK} ep:{Z2M_L2_ENDPOINT}")
    log.info(f"Virtual fan: {VIRTUAL_FAN_DEVICE_ID}")
    log.info(f"Fan motor: {FAN_MOTOR_DEVICE_ID}")
    log.info(f"Default speed: {DEFAULT_FAN_SPEED}")

    if not HOMEY_TOKEN:
        log.error("HOMEY_TOKEN not set!")
        return
    if not TUYA_KEY:
        log.error("TUYA_KEY not set!")
        return
    if not FAN_MOTOR_DEVICE_ID:
        log.error("FAN_MOTOR_DEVICE_ID not set!")
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
    threading.Thread(target=poll_fan_light, args=(client,), daemon=True).start()
    threading.Thread(target=monitor_fan_online, args=(client,), daemon=True).start()
    log.info("All pollers started (fan + light + monitor)")

    try:
        client.loop_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        client.publish("office-fan-bridge/status", "offline", retain=True)
        client.disconnect()


if __name__ == "__main__":
    main()
