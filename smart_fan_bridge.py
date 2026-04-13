"""
Smart Fan Bridge v5 — Multi-fan support
Controls multiple Tuya WiFi ceiling fans through Zigbee wall switches.

Each fan is configured in fans_config.json with its own:
- Virtual fan device (Google Home voice trigger)
- Virtual light device (optional, Google Home voice trigger)
- Tuya fan motor device (Homey, for dim/colortemp)
- TinyTuya local connection (direct LAN control)
- Zigbee switch endpoint (raw ZCL for L2 power)
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
CONFIG_FILE = os.environ.get("CONFIG_FILE", "/root/fans_config.json")

# Timing
TUYA_BOOT_WAIT = float(os.environ.get("TUYA_BOOT_WAIT", "3.0"))
TUYA_RETRY_INTERVAL = float(os.environ.get("TUYA_RETRY_INTERVAL", "3.0"))
TUYA_MAX_RETRIES = int(os.environ.get("TUYA_MAX_RETRIES", "8"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))
FAN_MONITOR_INTERVAL = float(os.environ.get("FAN_MONITOR_INTERVAL", "3.0"))
SEQUENCE_GRACE_PERIOD = float(os.environ.get("SEQUENCE_GRACE_PERIOD", "10.0"))
FAN_OFFLINE_THRESHOLD = int(os.environ.get("FAN_OFFLINE_THRESHOLD", "3"))

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
# Dependencies
# ---------------------------------------------------------------------------
try:
    import paho.mqtt.client as mqtt
except ImportError:
    import subprocess; subprocess.check_call(["pip", "install", "paho-mqtt"])
    import paho.mqtt.client as mqtt

try:
    import tinytuya
except ImportError:
    import subprocess; subprocess.check_call(["pip", "install", "tinytuya"])
    import tinytuya


# ---------------------------------------------------------------------------
# Fan instance — one per physical fan
# ---------------------------------------------------------------------------
class FanController:
    def __init__(self, config, mqtt_client):
        self.name = config["name"]
        self.mqtt_client = mqtt_client

        # Homey device IDs
        self.virtual_fan_id = config["virtual_fan_id"]
        self.virtual_light_id = config.get("virtual_light_id")  # None if no light
        self.fan_motor_id = config.get("fan_motor_id")

        # TinyTuya
        self.tuya_id = config["tuya_device_id"]
        self.tuya_ip = config["tuya_ip"]
        self.tuya_key = config["tuya_key"]
        self.tuya_version = config.get("tuya_version", 3.3)

        # Zigbee switch
        self.z2m_ieee = config["z2m_ieee"]
        self.z2m_nwk = config["z2m_nwk"]
        self.z2m_endpoint = config["z2m_endpoint"]

        self.default_speed = config.get("default_speed", 3)

        # State tracking
        self.fan_state = None
        self.light_state = None
        self.dim_state = None
        self.colortemp_state = None
        self.sequence_running = False
        self.sequence_end_time = 0
        self.fan_was_online = None
        self.fan_offline_count = 0
        self.lock = threading.Lock()

        log.info(f"[{self.name}] Tuya: {self.tuya_ip}, Z2M: {self.z2m_ieee} ep:{self.z2m_endpoint}")

    # --- Raw ZCL ---
    def send_raw_zcl(self, command):
        payload = json.dumps({
            "action": "raw",
            "params": {
                "ieee_address": self.z2m_ieee,
                "network_address": self.z2m_nwk,
                "dst_endpoint": self.z2m_endpoint,
                "cluster_key": "genOnOff",
                "zcl": {"frame_type": 1, "direction": 0, "command_key": command, "payload": {}}
            }
        })
        self.mqtt_client.publish("zigbee2mqtt/bridge/request/action", payload)
        log.info(f"[{self.name}] Raw ZCL: ep {self.z2m_endpoint} → {command}")

    # --- TinyTuya ---
    def _get_device(self):
        d = tinytuya.Device(self.tuya_id, self.tuya_ip, self.tuya_key, version=self.tuya_version)
        d.set_socketTimeout(5)
        return d

    def tuya_fan_on(self, speed=None):
        if speed is None:
            speed = self.default_speed
        try:
            d = self._get_device()
            result = d.set_multiple_values({"1": True, "3": speed})
            d.close()
            if result and "Error" not in str(result):
                log.info(f"[{self.name}] TinyTuya: fan ON speed {speed}")
                return True
            return False
        except Exception as e:
            log.error(f"[{self.name}] TinyTuya fan ON error: {e}")
            return False

    def tuya_fan_off(self):
        try:
            d = self._get_device()
            result = d.set_value(1, False)
            d.close()
            if result and "Error" not in str(result):
                log.info(f"[{self.name}] TinyTuya: fan OFF")
                return True
            return False
        except Exception as e:
            log.error(f"[{self.name}] TinyTuya fan OFF error: {e}")
            return False

    def tuya_light_on(self):
        try:
            d = self._get_device()
            result = d.set_value(15, True)
            d.close()
            if result and "Error" not in str(result):
                log.info(f"[{self.name}] TinyTuya: light ON")
                return True
            return False
        except Exception as e:
            log.error(f"[{self.name}] TinyTuya light ON error: {e}")
            return False

    def tuya_light_off(self):
        try:
            d = self._get_device()
            result = d.set_value(15, False)
            d.close()
            if result and "Error" not in str(result):
                log.info(f"[{self.name}] TinyTuya: light OFF")
                return True
            return False
        except Exception as e:
            log.error(f"[{self.name}] TinyTuya light OFF error: {e}")
            return False

    def tuya_set_brightness(self, homey_dim):
        raw = max(0, min(100, int(round(homey_dim * 100))))
        if raw % 2 != 0: raw -= 1
        try:
            d = self._get_device()
            result = d.set_value(16, raw)
            d.close()
            if result and "Error" not in str(result):
                log.info(f"[{self.name}] TinyTuya: brightness {raw}%")
                return True
            return False
        except Exception as e:
            log.error(f"[{self.name}] TinyTuya brightness error: {e}")
            return False

    def tuya_set_colortemp(self, homey_temp):
        raw = max(0, min(100, int(round(homey_temp * 100))))
        if raw % 2 != 0: raw -= 1
        try:
            d = self._get_device()
            result = d.set_value(17, raw)
            d.close()
            if result and "Error" not in str(result):
                log.info(f"[{self.name}] TinyTuya: colortemp {raw}")
                return True
            return False
        except Exception as e:
            log.error(f"[{self.name}] TinyTuya colortemp error: {e}")
            return False

    def is_online(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect((self.tuya_ip, 6668))
            s.close()
            return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False

    # --- Homey API ---
    def _homey_get(self, device_id):
        return homey_api(f"manager/devices/device/{device_id}")

    def _homey_set_cap(self, device_id, capability, value):
        homey_api(f"manager/devices/device/{device_id}/capability/{capability}",
                  method="PUT", data={"value": value})

    def get_virtual_fan_state(self):
        result = self._homey_get(self.virtual_fan_id)
        if result and "capabilitiesObj" in result:
            return result["capabilitiesObj"].get("onoff", {}).get("value")
        return None

    def set_virtual_fan(self, on):
        self._homey_set_cap(self.virtual_fan_id, "onoff", on)
        log.info(f"[{self.name}] Virtual fan → {'ON' if on else 'OFF'}")

    def get_virtual_light_state(self):
        if not self.virtual_light_id:
            return None
        result = self._homey_get(self.virtual_light_id)
        if result and "capabilitiesObj" in result:
            return result["capabilitiesObj"].get("onoff", {}).get("value")
        return None

    def set_virtual_light(self, on):
        if not self.virtual_light_id:
            return
        self._homey_set_cap(self.virtual_light_id, "onoff", on)
        log.info(f"[{self.name}] Virtual light → {'ON' if on else 'OFF'}")

    def get_light_details(self):
        if not self.fan_motor_id:
            return None
        result = self._homey_get(self.fan_motor_id)
        if result and "capabilitiesObj" in result:
            caps = result["capabilitiesObj"]
            return {
                "dim": caps.get("dim", {}).get("value"),
                "colortemp": caps.get("light_temperature", {}).get("value"),
            }
        return None

    # --- Ensure L2 power ---
    def ensure_l2_on(self):
        if self.is_online():
            return True
        log.info(f"[{self.name}] Fan offline — powering L2")
        self.send_raw_zcl("on")
        time.sleep(TUYA_BOOT_WAIT)
        for attempt in range(1, TUYA_MAX_RETRIES + 1):
            if self.is_online():
                log.info(f"[{self.name}] Fan online after {attempt} checks")
                return True
            log.info(f"[{self.name}] Waiting for boot... {attempt}/{TUYA_MAX_RETRIES}")
            time.sleep(TUYA_RETRY_INTERVAL)
        log.warning(f"[{self.name}] Fan did not come online")
        return False

    # --- Sequences ---
    def sequence_fan_on(self):
        with self.lock:
            if self.sequence_running: return
            self.sequence_running = True
        try:
            log.info(f"[{self.name}] === FAN ON ===")
            if self.ensure_l2_on():
                for _ in range(3):
                    if self.tuya_fan_on():
                        log.info(f"[{self.name}] === FAN ON complete ===")
                        return
                    time.sleep(2)
        finally:
            with self.lock:
                self.sequence_running = False
                self.sequence_end_time = time.time()

    def sequence_fan_off(self):
        with self.lock:
            if self.sequence_running: return
            self.sequence_running = True
        try:
            log.info(f"[{self.name}] === FAN OFF ===")
            self.tuya_fan_off()
            time.sleep(2)
            if not self.light_state:
                log.info(f"[{self.name}] Light off — cutting L2")
                self.send_raw_zcl("off")
            else:
                log.info(f"[{self.name}] Light on — keeping L2")
            log.info(f"[{self.name}] === FAN OFF complete ===")
        finally:
            with self.lock:
                self.sequence_running = False
                self.sequence_end_time = time.time()

    def sequence_light_on(self):
        with self.lock:
            if self.sequence_running: return
            self.sequence_running = True
        try:
            log.info(f"[{self.name}] === LIGHT ON ===")
            if self.ensure_l2_on():
                for _ in range(3):
                    if self.tuya_light_on():
                        log.info(f"[{self.name}] === LIGHT ON complete ===")
                        return
                    time.sleep(2)
        finally:
            with self.lock:
                self.sequence_running = False
                self.sequence_end_time = time.time()

    def sequence_light_off(self):
        with self.lock:
            if self.sequence_running: return
            self.sequence_running = True
        try:
            log.info(f"[{self.name}] === LIGHT OFF ===")
            self.tuya_light_off()
            time.sleep(2)
            if not self.fan_state:
                log.info(f"[{self.name}] Fan off — cutting L2")
                self.send_raw_zcl("off")
            else:
                log.info(f"[{self.name}] Fan on — keeping L2")
            log.info(f"[{self.name}] === LIGHT OFF complete ===")
        finally:
            with self.lock:
                self.sequence_running = False
                self.sequence_end_time = time.time()

    # --- Pollers ---
    def poll_fan(self):
        while True:
            try:
                current = self.get_virtual_fan_state()
                if current is not None and current != self.fan_state:
                    old = self.fan_state
                    self.fan_state = current
                    if old is None:
                        log.info(f"[{self.name}] Fan initial: {'ON' if current else 'OFF'}")
                    elif not self.sequence_running:
                        if current:
                            threading.Thread(target=self.sequence_fan_on, daemon=True).start()
                        else:
                            threading.Thread(target=self.sequence_fan_off, daemon=True).start()
            except Exception as e:
                log.error(f"[{self.name}] Fan poll error: {e}")
            time.sleep(POLL_INTERVAL)

    def poll_light(self):
        if not self.virtual_light_id:
            return  # No light for this fan
        while True:
            try:
                current = self.get_virtual_light_state()
                if current is not None and current != self.light_state:
                    old = self.light_state
                    self.light_state = current
                    if old is None:
                        log.info(f"[{self.name}] Light initial: {'ON' if current else 'OFF'}")
                    elif not self.sequence_running:
                        if current:
                            threading.Thread(target=self.sequence_light_on, daemon=True).start()
                        else:
                            threading.Thread(target=self.sequence_light_off, daemon=True).start()

                # Brightness/colortemp sync
                details = self.get_light_details()
                if details:
                    dim = details["dim"]
                    colortemp = details["colortemp"]
                    if dim is not None and dim != self.dim_state:
                        self.dim_state = dim
                        if self.light_state and self.is_online():
                            log.info(f"[{self.name}] Brightness → {dim}")
                            self.tuya_set_brightness(dim)
                    if colortemp is not None and colortemp != self.colortemp_state:
                        self.colortemp_state = colortemp
                        if self.light_state and self.is_online():
                            log.info(f"[{self.name}] Color temp → {colortemp}")
                            self.tuya_set_colortemp(colortemp)
            except Exception as e:
                log.error(f"[{self.name}] Light poll error: {e}")
            time.sleep(POLL_INTERVAL)

    def get_tuya_status(self):
        """Read current DPS from the Tuya fan to sync virtual devices."""
        try:
            d = self._get_device()
            result = d.status()
            d.close()
            if result and "dps" in result:
                return result["dps"]
        except Exception:
            pass
        return None

    def sync_virtual_to_tuya(self):
        """Sync virtual fan/light state to match the actual Tuya fan state."""
        dps = self.get_tuya_status()
        if dps is None:
            return

        # DP 1 = fan power, DP 15 = light power
        actual_fan = bool(dps.get("1", False))
        actual_light = bool(dps.get("15", False))

        if self.fan_state != actual_fan:
            log.info(f"[{self.name}] Sync: fan {self.fan_state} → {actual_fan} (Tuya)")
            self.set_virtual_fan(actual_fan)
            self.fan_state = actual_fan

        if self.virtual_light_id and self.light_state != actual_light:
            log.info(f"[{self.name}] Sync: light {self.light_state} → {actual_light} (Tuya)")
            self.set_virtual_light(actual_light)
            self.light_state = actual_light

    def monitor_online(self):
        while True:
            try:
                online = self.is_online()
                with self.lock:
                    is_running = self.sequence_running
                    grace = time.time() - self.sequence_end_time
                if is_running or grace < SEQUENCE_GRACE_PERIOD:
                    time.sleep(FAN_MONITOR_INTERVAL)
                    continue

                if self.fan_was_online is None:
                    self.fan_was_online = online
                    self.fan_offline_count = 0
                    log.info(f"[{self.name}] Online initial: {online}")
                elif not online:
                    self.fan_offline_count += 1
                    if self.fan_offline_count >= FAN_OFFLINE_THRESHOLD:
                        if self.fan_state:
                            log.info(f"[{self.name}] OFFLINE — resetting fan")
                            self.set_virtual_fan(False)
                            self.fan_state = False
                        if self.light_state:
                            log.info(f"[{self.name}] OFFLINE — resetting light")
                            self.set_virtual_light(False)
                            self.light_state = False
                        self.fan_was_online = False
                elif online:
                    if not self.fan_was_online:
                        log.info(f"[{self.name}] ONLINE — L2 restored")
                    self.fan_was_online = True
                    self.fan_offline_count = 0

                    # Sync virtual devices to actual Tuya state when online
                    self.sync_virtual_to_tuya()
            except Exception as e:
                log.error(f"[{self.name}] Monitor error: {e}")
            time.sleep(FAN_MONITOR_INTERVAL)

    def start(self):
        threading.Thread(target=self.poll_fan, daemon=True).start()
        threading.Thread(target=self.poll_light, daemon=True).start()
        threading.Thread(target=self.monitor_online, daemon=True).start()
        log.info(f"[{self.name}] All pollers started")


# ---------------------------------------------------------------------------
# Homey API helper (shared)
# ---------------------------------------------------------------------------
def homey_api(path, method="GET", data=None):
    url = f"{HOMEY_URL}/api/{path}"
    headers = {"Authorization": f"Bearer {HOMEY_TOKEN}", "Content-Type": "application/json"}
    req = urllib.request.Request(url, headers=headers, method=method)
    if data is not None:
        req.data = json.dumps(data).encode()
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.error(f"Homey API error ({method} {path}): {e}")
        return None


# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------
def on_connect(client, userdata, flags, rc, properties=None):
    log.info(f"MQTT connected (rc={rc})")
    client.publish("smart-fan-bridge/status", "online", retain=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log.info("Starting Smart Fan Bridge v5 (Multi-fan)")

    if not HOMEY_TOKEN:
        log.error("HOMEY_TOKEN not set!")
        return

    # Load fan configs
    try:
        with open(CONFIG_FILE) as f:
            configs = json.load(f)
        log.info(f"Loaded {len(configs)} fan configs from {CONFIG_FILE}")
    except Exception as e:
        log.error(f"Failed to load config: {e}")
        return

    # MQTT client
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="smart-fan-bridge")
    client.on_connect = on_connect
    client.will_set("smart-fan-bridge/status", payload="offline", retain=True)
    client.connect(MQTT_HOST, MQTT_PORT)

    # Create and start fan controllers
    for config in configs:
        fan = FanController(config, client)
        fan.start()

    log.info("All fans initialized. Entering MQTT loop.")
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        client.publish("smart-fan-bridge/status", "offline", retain=True)
        client.disconnect()


if __name__ == "__main__":
    main()
