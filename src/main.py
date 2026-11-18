#!/usr/bin/env python3
"""VRCJoyCon - cross-platform OSC haptics controller.

Maps VRChat/ChilloutVR OSC parameters to rumble motors on Nintendo Switch
Joy-Cons and standard gamepads. Provides a PySide6 UI for assigning
parameters, controllers and motors, plus a standard OSC UDP server.
"""

import asyncio
import configparser
import json
import logging
import math
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("VRCJOYCON")

try:
    import hid
except Exception as e:
    hid = None
    log.warning("hid unavailable: %s", e)

try:
    from pyjoycon import get_device_ids
    from pyjoycon import JoyCon as _JoyConBase
except Exception as e:
    get_device_ids = None
    _JoyConBase = None
    log.warning("pyjoycon unavailable: %s", e)

try:
    import pygame
except Exception as e:
    pygame = None
    log.warning("pygame unavailable: %s", e)

from pythonosc import dispatcher, osc_server

from PySide6.QtCore import QObject, Qt, Signal, Slot, QTimer
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QPushButton,
    QScrollArea, QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget,
)

CONFIG_PATH = HERE / "config.json"
CONFIG_VERSION = 1


def find_image(name):
    candidates = []
    if getattr(sys, "_MEIPASS", None):
        candidates.append(Path(sys._MEIPASS) / "images" / name)
    candidates.append(HERE / "images" / name)
    candidates.append(HERE.parent / "images" / name)
    for c in candidates:
        if c.exists():
            return c
    return candidates[-1]


JOYCON_COLOR_NAMES = {
    (237, 28, 36): "RED",
    (0, 120, 215): "BLUE",
    (255, 204, 0): "YELLOW",
    (57, 255, 20): "GREEN",
    (128, 128, 128): "GRAY",
    (255, 255, 255): "WHITE",
    (0, 0, 0): "BLACK",
    (255, 105, 180): "PINK",
    (128, 0, 128): "PURPLE",
    (255, 140, 0): "ORANGE",
}


def color_name(rgb):
    if not rgb:
        return "UNKNOWN"
    best = min(JOYCON_COLOR_NAMES, key=lambda c: sum((c[i] - rgb[i]) ** 2 for i in range(3)))
    return JOYCON_COLOR_NAMES[best]


def clamp_strength(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v != v:  # NaN
        return 0.0
    return max(0.0, min(1.0, v))


# ---------------- Controller abstraction ----------------

class Controller:
    def __init__(self, cid, kind, name, motors, backend=""):
        self.cid = cid
        self.kind = kind
        self.name = name
        self.display_name = name
        self.color = None
        self.icon = None
        self.pair_colors = None
        self.motors = list(motors)
        self.backend = backend
        self.base_states = [0.0 for _ in self.motors]
        self.pulse_states = [0.0 for _ in self.motors]
        self.output_states = self.base_states
        self._lock = threading.Lock()

    def connect(self):
        return True

    def _apply(self):
        pass

    def set_output(self, index, strength):
        return self.set_base_output(index, strength)

    def set_base_output(self, index, strength):
        strength = clamp_strength(strength)
        with self._lock:
            if index < 0 or index >= len(self.motors):
                return False
            self.base_states[index] = strength
        try:
            self._apply()
        except Exception as e:
            log.error("Controller apply error %s: %s", self.cid, e)
        return True

    def add_pulse(self, index, amount):
        amount = clamp_strength(amount)
        with self._lock:
            if index < 0 or index >= len(self.motors):
                return False
            self.pulse_states[index] = min(2.0, self.pulse_states[index] + amount)
        try:
            self._apply()
        except Exception as e:
            log.error("Controller pulse error %s: %s", self.cid, e)
        return True

    def get_output(self, index):
        with self._lock:
            if index < 0 or index >= len(self.motors):
                return 0.0
            return self.base_states[index]

    def tick(self):
        with self._lock:
            for i in range(len(self.pulse_states)):
                self.pulse_states[i] *= 0.82
                if self.pulse_states[i] < 0.01:
                    self.pulse_states[i] = 0.0
        try:
            self._apply()
        except Exception as e:
            log.error("Controller tick error %s: %s", self.cid, e)

    def stop_all(self):
        with self._lock:
            self.base_states = [0.0 for _ in self.motors]
            self.pulse_states = [0.0 for _ in self.motors]
        try:
            self._apply()
        except Exception as e:
            log.error("Controller stop error %s: %s", self.cid, e)

    def is_connected(self):
        return True

    def output_labels(self):
        return [f"{i}: {label}" for i, label in enumerate(self.motors)]


class JoyConController(Controller):
    def __init__(self, serial, product_id, path=""):
        side = "L" if product_id == 0x2006 else "R"
        stable = serial or path or "unknown"
        super().__init__(f"joycon:{product_id:04x}:{stable}", "joycon",
                         f"Joy-Con ({side})", ["Rumble"], backend="hid")
        self.serial = serial
        self.product_id = product_id
        self.path = path
        self._jc = None

    def connect(self):
        if _JoyConBase is None or hid is None:
            return False
        try:
            ids = None
            if get_device_ids:
                for vid, pid, serial, path in get_device_ids():
                    if (self.serial and serial == self.serial) or (not self.serial and path and path == self.path):
                        ids = (vid, pid, serial, path)
                        break
            if not ids:
                return False
            vid, pid, serial, path = ids
            self._jc = _JoyConBase(vid, pid, serial, path=path)
            try:
                body = self._jc.color_body
                if body:
                    self.color = tuple(int(x) for x in body)
                side = "L" if self.product_id == 0x2006 else "R"
                hexcolor = ""
                if self.color:
                    hexcolor = "#{:02x}{:02x}{:02x}".format(*self.color)
                self.display_name = f"Joy-Con ({side}) {hexcolor}".strip()
                self.name = self.display_name
            except Exception:
                pass
            return True
        except Exception as e:
            log.error("JoyCon connect failed: %s", e)
            return False

    def _encode_joycon_rumble(self, amplitude):
        amplitude = max(0.0, min(1.0, amplitude))
        if amplitude <= 0.0:
            return b"\\x00\\x01\\x40\\x40"
        if amplitude > 0.23:
            encoded = round(math.log2(amplitude * 8.7) * 32.0)
        elif amplitude > 0.12:
            encoded = round(math.log2(amplitude * 17.0) * 16.0)
        else:
            encoded = round(amplitude * 64.0)
        encoded = int(max(0, min(255, encoded)))
        hf = 0x0100
        hf_amp = encoded * 2
        lf = 0x40
        lf_amp = encoded // 2 + 64
        byte0 = hf & 0xFF
        byte1 = (hf_amp + ((hf >> 8) & 0xFF)) & 0xFF
        byte2 = (lf + ((lf_amp >> 8) & 0xFF)) & 0xFF
        byte3 = lf_amp & 0xFF
        return bytes([byte0, byte1, byte2, byte3])

    def _apply(self):
        if not self._jc:
            return
        with self._lock:
            effective = [min(1.0, max(0.0, self.base_states[i] + self.pulse_states[i])) for i in range(len(self.motors))]
        amplitude = max(effective) if effective else 0.0
        neutral = b"\\x00\\x01\\x40\\x40"
        side = self._encode_joycon_rumble(amplitude)
        if self.product_id == 0x2006:
            data = side + neutral
        else:
            data = neutral + side
        try:
            self._jc._send_rumble(data)
        except Exception as e:
            log.error("JoyCon rumble error: %s", e)

    def rumble(self, motor_index, strength):
        self.set_output(motor_index, strength)

    def is_connected(self):
        try:
            return bool(self._jc) and self._jc.connected()
        except Exception:
            return False


class GamepadController(Controller):
    def __init__(self, index, guid="", instance_id=None, pair_colors=None, display_name_override=None):
        if instance_id is not None:
            cid = f"gamepad:instance:{instance_id}"
        elif guid:
            cid = f"gamepad:{guid}"
        else:
            cid = f"gamepad:index:{index}"
        super().__init__(cid, "gamepad", f"Gamepad #{index}", ["Low-frequency", "High-frequency"],
                         backend="sdl")
        self.index = index
        self.guid = guid
        self.instance_id = instance_id
        self.pair_colors = pair_colors
        self.display_name_override = display_name_override
        self._js = None

    def connect(self):
        if pygame is None:
            return False
        try:
            pygame.joystick.init()
            # find by guid if possible
            js = None
            for i in range(pygame.joystick.get_count()):
                candidate = pygame.joystick.Joystick(i)
                candidate.init()
                iid = getattr(candidate, "get_instance_id", lambda: None)()
                if self.instance_id is not None and iid == self.instance_id:
                    js = candidate
                    self.index = i
                    break
                if self.instance_id is None and self.guid and candidate.get_guid() == self.guid:
                    js = candidate
                    self.index = i
                    break
            if js is None:
                if self.index >= pygame.joystick.get_count():
                    return False
                js = pygame.joystick.Joystick(self.index)
                js.init()
            self._js = js
            name = self._js.get_name() or f"Gamepad #{self.index}"
            self.name = name
            if self.instance_id is None:
                self.instance_id = getattr(self._js, "get_instance_id", lambda: None)()
                if self.instance_id is not None:
                    self.cid = f"gamepad:instance:{self.instance_id}"
            if not self.guid:
                self.guid = self._js.get_guid()
                if not self.instance_id:
                    self.cid = f"gamepad:{self.guid}" if self.guid else self.cid
            if self.display_name_override:
                self.display_name = self.display_name_override
                self.name = self.display_name
            else:
                self.display_name = f"🎮 {name}"
                self.name = self.display_name
            self.icon = "🎮"
            return True
        except Exception as e:
            log.error("Gamepad connect failed: %s", e)
            return False

    def _apply(self):
        if not self._js:
            return
        with self._lock:
            effective = [min(1.0, max(0.0, self.base_states[i] + self.pulse_states[i])) for i in range(len(self.motors))]
        low = effective[0] if len(effective) > 0 else 0.0
        high = effective[1] if len(effective) > 1 else 0.0
        if any(s > 0.0 for s in effective):
            self._js.rumble(low, high, 250)
        else:
            self._js.rumble(0, 0, 0)

    def rumble(self, motor_index, strength):
        self.set_output(motor_index, strength)

    def is_connected(self):
        try:
            return bool(self._js) and self._js.get_init()
        except Exception:
            return False


def discover_controllers(existing=None):
    existing_by_cid = {c.cid: c for c in (existing or [])}
    controllers = []

    sdl_gamepads = []
    paired_joycon_present = False
    if pygame is not None:
        try:
            pygame.joystick.init()
            for i in range(pygame.joystick.get_count()):
                js = pygame.joystick.Joystick(i)
                js.init()
                name = (js.get_name() or "").lower()
                guid = js.get_guid()
                iid = getattr(js, "get_instance_id", lambda: None)()
                sdl_gamepads.append((i, guid, name, js, iid))
                if "joy-con" in name or "joycon" in name:
                    if js.get_numbuttons() >= 10 or "pair" in name or "combined" in name:
                        paired_joycon_present = True
        except Exception as e:
            log.error("Gamepad discovery error: %s", e)

    hid_pair_colors = []
    if get_device_ids is not None:
        try:
            for vid, pid, serial, path in get_device_ids():
                c = JoyConController(serial, pid, path)
                if c.connect() and c.color is not None:
                    hid_pair_colors.append((pid, c.color))
        except Exception as e:
            log.error("JoyCon color discovery error: %s", e)
    left_pair_colors = [col for pid, col in hid_pair_colors if pid == 0x2006]
    right_pair_colors = [col for pid, col in hid_pair_colors if pid == 0x2007]

    if paired_joycon_present:
        left_idx = 0
        right_idx = 0
        if pygame is not None:
            try:
                for i, guid, name, js, iid in sdl_gamepads:
                    if not ("joy-con" in name or "joycon" in name):
                        continue
                    if not (js.get_numbuttons() >= 10 or "pair" in name or "combined" in name):
                        continue
                    lc = left_pair_colors[left_idx] if left_idx < len(left_pair_colors) else None
                    rc = right_pair_colors[right_idx] if right_idx < len(right_pair_colors) else None
                    left_idx += 1
                    right_idx += 1
                    lc_name = color_name(lc)
                    rc_name = color_name(rc)
                    display_override = f"PAIR:{lc_name}|{rc_name}"
                    pair_colors = [lc if lc else (128, 128, 128), rc if rc else (128, 128, 128)]
                    if iid is not None:
                        cid = f"gamepad:instance:{iid}"
                    elif guid:
                        cid = f"gamepad:{guid}"
                    else:
                        cid = f"gamepad:index:{i}"
                    if cid in existing_by_cid and existing_by_cid[cid].is_connected():
                        controllers.append(existing_by_cid[cid])
                        continue
                    c = GamepadController(i, guid, iid, pair_colors=pair_colors, display_name_override=display_override)
                    c.connect()
                    controllers.append(c)
            except Exception as e:
                log.error("Gamepad discovery error: %s", e)
    else:
        if get_device_ids is not None:
            try:
                for vid, pid, serial, path in get_device_ids():
                    cid = f"joycon:{pid:04x}:{serial or path}"
                    if cid in existing_by_cid and existing_by_cid[cid].is_connected():
                        controllers.append(existing_by_cid[cid])
                        continue
                    c = JoyConController(serial, pid, path)
                    c.connect()
                    controllers.append(c)
            except Exception as e:
                log.error("JoyCon discovery error: %s", e)

    if pygame is not None:
        try:
            for i, guid, name, js, iid in sdl_gamepads:
                if "joy-con" in name or "joycon" in name:
                    continue
                if iid is not None:
                    cid = f"gamepad:instance:{iid}"
                elif guid:
                    cid = f"gamepad:{guid}"
                else:
                    cid = f"gamepad:index:{i}"
                if cid in existing_by_cid and existing_by_cid[cid].is_connected():
                    controllers.append(existing_by_cid[cid])
                    continue
                c = GamepadController(i, guid, iid)
                c.connect()
                controllers.append(c)
        except Exception as e:
            log.error("Gamepad discovery error: %s", e)

    seen = {}
    for c in controllers:
        base = c.display_name
        if base in seen:
            seen[base] += 1
            c.display_name = f"{base} ({seen[base]})"
            c.name = c.display_name
        else:
            seen[base] = 1
    return controllers

# ---------------- Config ----------------

def _validate_mapping(mapping):
    if not isinstance(mapping, dict):
        return None
    path = mapping.get("osc_path", mapping.get("path"))
    if not isinstance(path, str) or not path.strip():
        return None
    controller = mapping.get("controller", mapping.get("controller_id"))
    if not isinstance(controller, str) or not controller.strip():
        return None
    try:
        motor = int(mapping.get("motor", mapping.get("output", 0)))
    except (TypeError, ValueError):
        motor = 0
    if motor < 0:
        motor = 0
    return {"osc_path": path.strip(), "controller": controller.strip(), "motor": motor}


def _default_config():
    return {"version": CONFIG_VERSION, "listen": {"ip": "0.0.0.0", "port": 9001}, "mappings": [], "controllers": {}}


def _migrate_ini_config():
    candidates = [HERE / "config.ini", HERE.parent / "config.ini"]
    ini_path = None
    for c in candidates:
        if c.exists():
            ini_path = c
            break
    if ini_path is None:
        return None
    try:
        cp = configparser.ConfigParser()
        cp.read(ini_path)
        cfg = _default_config()
        if cp.has_section("listen"):
            if cp.has_option("listen", "ip"):
                cfg["listen"]["ip"] = cp.get("listen", "ip")
            if cp.has_option("listen", "port"):
                try:
                    cfg["listen"]["port"] = cp.getint("listen", "port")
                except ValueError:
                    pass
        # old osc.rumble sections: controller id -> paths
        if cp.has_section("osc.rumble"):
            for controller_id, paths in cp.items("osc.rumble"):
                for line in paths.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    cfg["mappings"].append({"osc_path": line, "controller": str(controller_id), "motor": 0})
        # backup
        backup = ini_path.with_suffix(".ini.bak")
        if not backup.exists():
            backup.write_text(ini_path.read_text())
        return cfg
    except Exception as e:
        log.error("INI migration failed: %s", e)
        return None


def load_config():
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open() as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("config root is not an object")
            # version handling
            version = data.get("version", 0)
            if version != CONFIG_VERSION:
                # best-effort: keep data, bump version
                data["version"] = CONFIG_VERSION
            data.setdefault("listen", {})
            data["listen"].setdefault("ip", "0.0.0.0")
            try:
                data["listen"]["port"] = int(data["listen"].get("port", 9001))
            except (TypeError, ValueError):
                data["listen"]["port"] = 9001
            raw_mappings = data.get("mappings", [])
            if not isinstance(raw_mappings, list):
                raw_mappings = []
            validated = []
            for m in raw_mappings:
                vm = _validate_mapping(m)
                if vm:
                    validated.append(vm)
            data["mappings"] = validated
            if not isinstance(data.get("controllers"), dict):
                data["controllers"] = {}
            if "version" not in data:
                data["version"] = CONFIG_VERSION
            return data
        except Exception as e:
            log.error("Config load error: %s", e)
            try:
                bad = CONFIG_PATH.with_suffix(".json.bak")
                if not bad.exists():
                    CONFIG_PATH.replace(bad)
            except Exception:
                pass
            return _default_config()
    # try migration only if config file was absent
    migrated = _migrate_ini_config()
    if migrated is not None:
        try:
            save_config(migrated)
        except Exception:
            pass
        return migrated
    return _default_config()


def save_config(cfg):
    if not isinstance(cfg, dict):
        cfg = _default_config()
    cfg.setdefault("version", CONFIG_VERSION)
    cfg.setdefault("listen", {"ip": "0.0.0.0", "port": 9001})
    try:
        cfg["listen"]["port"] = int(cfg["listen"].get("port", 9001))
    except (TypeError, ValueError):
        cfg["listen"]["port"] = 9001
    if not isinstance(cfg.get("mappings"), list):
        cfg["mappings"] = []
    if not isinstance(cfg.get("controllers"), dict):
        cfg["controllers"] = {}
    validated = []
    for m in cfg["mappings"]:
        vm = _validate_mapping(m)
        if vm:
            validated.append(vm)
    cfg["mappings"] = validated
    try:
        with CONFIG_PATH.open("w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        log.error("Config save error: %s", e)


# ---------------- OSC Bridge ----------------

class LogEmitter(QObject):
    message = Signal(str)


class MonitorEmitter(QObject):
    update = Signal(dict)


class OscBridge:
    def __init__(self, cfg, controllers, log_emitter, monitor_emitter):
        self.cfg = cfg
        self.controllers = {c.cid: c for c in controllers}
        self.log_emitter = log_emitter
        self.monitor_emitter = monitor_emitter
        self.running = False
        self.thread = None
        self.loop = None
        self.server = None
        self.transport = None
        self.mappings = {}
        self.monitor = {}  # path -> dict
        self._previous_values = {}
        self._lock = threading.Lock()
        self._rebuild_mappings()

    def _log(self, msg):
        try:
            self.log_emitter.message.emit(msg)
        except Exception:
            print(msg)

    def _rebuild_mappings(self):
        with self._lock:
            self.mappings = {}
            for m in self.cfg.get("mappings", []):
                vm = _validate_mapping(m)
                if vm:
                    self.mappings[vm["osc_path"]] = (vm["controller"], vm["motor"])

    def update_controllers(self, controllers):
        with self._lock:
            self.controllers = {c.cid: c for c in controllers}

    def update_config(self, cfg):
        self.cfg = cfg
        self._rebuild_mappings()

    def dispatch(self, path, value, simulated=False):
        value = clamp_strength(value)
        with self._lock:
            mapping = self.mappings.get(path)
            monitor_entry = {
                "path": path,
                "value": value,
                "active": value > 0.0,
                "simulated": simulated,
                "source": "simulated" if simulated else "osc",
                "time": datetime.now().strftime("%H:%M:%S"),
                "controller": mapping[0] if mapping else None,
                "motor": mapping[1] if mapping else None,
            }
            self.monitor[path] = monitor_entry
        try:
            self.monitor_emitter.update.emit(monitor_entry)
        except Exception:
            pass
        if not mapping:
            self._log(f"Unmapped OSC parameter: {path} = {value}")
            return
        cid, motor = mapping
        ctrl = self.controllers.get(cid)
        if not ctrl:
            self._log(f"No controller {cid} for {path}")
            return
        try:
            prev = self._previous_values.get(path, 0.0)
            delta = value - prev
            self._previous_values[path] = value
            setter = getattr(ctrl, "set_base_output", None) or ctrl.set_output
            setter(int(motor), value)
            if delta > 0.05 and hasattr(ctrl, "add_pulse"):
                pulse = min(1.0, delta * 2.0 + value * 0.5)
                ctrl.add_pulse(int(motor), pulse)
            self._log(f"{path} -> {cid} output {motor}: {value}{' (simulated)' if simulated else ''}")
        except Exception as e:
            self._log(f"Rumble dispatch error: {e}")

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.loop:
            try:
                self.loop.call_soon_threadsafe(self.loop.stop)
            except Exception:
                pass

    def _run(self):
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._serve())
        except Exception as e:
            self._log(f"OSC server error: {e}")
        finally:
            self.running = False

    async def _serve(self):
        raw_ip = str(self.cfg["listen"]["ip"]).strip()
        if raw_ip.lower() in ("any", "*", "", "all"):
            raw_ip = "0.0.0.0"
        port = int(self.cfg["listen"]["port"])

        d = dispatcher.Dispatcher()
        for path in list(self.mappings.keys()):
            d.map(path, lambda address, *args, p=path: self._osc_callback(p, *args))
        d.set_default_handler(lambda address, *args: None)

        ip = raw_ip
        try:
            self.server = osc_server.AsyncIOOSCUDPServer((ip, port), d, self.loop)
            self._log(f"OSC server listening on {ip}:{port}")
            transport, protocol = await self.server.create_serve_endpoint()
        except OSError as e:
            self._log(f"Cannot bind to {ip}:{port} ({e}); falling back to 0.0.0.0")
            ip = "0.0.0.0"
            self.server = osc_server.AsyncIOOSCUDPServer((ip, port), d, self.loop)
            self._log(f"OSC server listening on {ip}:{port}")
            transport, protocol = await self.server.create_serve_endpoint()
        self.transport = transport
        while self.running:
            await asyncio.sleep(0.5)
        try:
            transport.close()
        except Exception:
            pass

    def _osc_callback(self, path, *args):
        val = 0.0
        if args:
            v = args[0]
            if isinstance(v, (list, tuple)) and v:
                v = v[0]
            if isinstance(v, bool):
                val = 1.0 if v else 0.0
            else:
                try:
                    val = float(v)
                except (TypeError, ValueError):
                    val = 0.0
        self.dispatch(path, val, simulated=False)


# ---------------- UI ----------------

class ControllerRow(QWidget):
    def __init__(self, controller):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        if controller.pair_colors:
            for col in controller.pair_colors:
                sw = QFrame()
                sw.setFixedSize(20, 20)
                r, g, b = col
                sw.setStyleSheet(f"background-color: rgb({r},{g},{b}); border: 1px solid #555; border-radius: 3px;")
                layout.addWidget(sw)
        elif controller.color:
            sw = QFrame()
            sw.setFixedSize(24, 24)
            r, g, b = controller.color
            sw.setStyleSheet(f"background-color: rgb({r},{g},{b}); border: 1px solid #555; border-radius: 4px;")
            layout.addWidget(sw)
        else:
            icon = QLabel(controller.icon or "🎮")
            icon.setStyleSheet("font-size: 16px;")
            layout.addWidget(icon)
        layout.addWidget(QLabel(controller.display_name))
        status = "connected" if controller.is_connected() else "disconnected"
        st = QLabel(f"[{status}]")
        st.setStyleSheet("color: gray;")
        layout.addWidget(st)
        layout.addWidget(QLabel(f"outputs={controller.motors}"))
        layout.addStretch()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("VRCJoyCon")
        icon_path = find_image("app.ico" if sys.platform.startswith("win") else "app.png")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self.cfg = load_config()
        self.controllers = discover_controllers()
        self._apply_saved_appearance()
        self.emitter = LogEmitter()
        self.monitor_emitter = MonitorEmitter()
        self.bridge = OscBridge(self.cfg, self.controllers, self.emitter, self.monitor_emitter)
        self.emitter.message.connect(self._append_log)
        self.monitor_emitter.update.connect(self._on_monitor_update)

        # rumble tick timer
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(200)
        self._tick_timer.timeout.connect(self._tick_controllers)
        self._tick_timer.start()

        self._build_ui()
        self._refresh_controller_list()
        self._refresh_mapping_list()
        self._refresh_haptics()

        hints = QApplication.instance().styleHints()
        hints.colorSchemeChanged.connect(self._on_scheme_changed)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)

        self.tabs = QTabWidget()
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.addWidget(self.tabs, 1)

        # bottom controls attached to left column
        bottom = QHBoxLayout()
        start_btn = QPushButton("Start OSC Server")
        start_btn.clicked.connect(self._on_start)
        stop_btn = QPushButton("Stop OSC Server")
        stop_btn.clicked.connect(self._on_stop)
        bottom.addWidget(start_btn)
        bottom.addWidget(stop_btn)
        bottom.addStretch()
        left_layout.addLayout(bottom)

        root_layout.addWidget(left_widget, 3)

        # persistent console panel on the right
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.addWidget(QLabel("Console"))
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        right_layout.addWidget(self.log_text, 1)
        root_layout.addWidget(right_widget, 1)

        # Controllers tab
        ctrl_tab = QWidget()
        ctrl_layout = QVBoxLayout(ctrl_tab)
        ctrl_layout.addWidget(QLabel("Discovered controllers"))
        self.ctrl_list = QListWidget()
        ctrl_layout.addWidget(self.ctrl_list, 1)
        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._on_refresh_controllers)
        connect_btn = QPushButton("Connect All")
        connect_btn.clicked.connect(self._on_connect_all)
        btn_row.addWidget(refresh_btn)
        btn_row.addWidget(connect_btn)
        btn_row.addStretch()
        ctrl_layout.addLayout(btn_row)
        self.tabs.addTab(ctrl_tab, "Controllers")

        # Mappings tab
        map_tab = QWidget()
        map_layout = QVBoxLayout(map_tab)
        self.map_table = QTableWidget(0, 3)
        self.map_table.setHorizontalHeaderLabels(["OSC Parameter", "Controller", "Output"])
        self.map_table.horizontalHeader().setStretchLastSection(True)
        map_layout.addWidget(self.map_table, 1)

        form = QHBoxLayout()
        form.addWidget(QLabel("OSC path"))
        self.osc_entry = QLineEdit()
        form.addWidget(self.osc_entry, 2)
        form.addWidget(QLabel("Controller"))
        self.ctrl_combo = QComboBox()
        form.addWidget(self.ctrl_combo, 2)
        form.addWidget(QLabel("Output"))
        self.motor_spin = QSpinBox()
        self.motor_spin.setMinimum(0)
        form.addWidget(self.motor_spin)
        map_layout.addLayout(form)

        preset_box = QHBoxLayout()
        preset_box.addWidget(QLabel("Parameter name"))
        self.param_entry = QLineEdit()
        preset_box.addWidget(self.param_entry, 2)
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(["joyconrumble1", "joyconrumble2"])
        preset_box.addWidget(self.preset_combo)
        fill_btn = QPushButton("Use /avatar/parameters/")
        fill_btn.clicked.connect(self._fill_preset)
        preset_box.addWidget(fill_btn)
        use_selected_btn = QPushButton("Use selected")
        use_selected_btn.clicked.connect(self._use_selected_preset)
        preset_box.addWidget(use_selected_btn)
        map_layout.addLayout(preset_box)

        btn_row2 = QHBoxLayout()
        add_btn = QPushButton("Add/Update")
        add_btn.clicked.connect(self._on_add_mapping)
        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(self._on_delete_mapping)
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._on_save_mappings)
        btn_row2.addWidget(add_btn)
        btn_row2.addWidget(del_btn)
        btn_row2.addWidget(save_btn)
        btn_row2.addStretch()
        map_layout.addLayout(btn_row2)
        self.tabs.addTab(map_tab, "Mappings")

        self.ctrl_combo.currentIndexChanged.connect(self._update_motor_range)

        # Haptics Test tab
        self._build_haptics_tab()

        # Settings tab
        set_tab = QWidget()
        set_layout = QFormLayout(set_tab)
        self.listen_ip = QLineEdit(self.cfg["listen"]["ip"])
        self.listen_port = QLineEdit(str(self.cfg["listen"]["port"]))
        set_layout.addRow("Listen IP", self.listen_ip)
        set_layout.addRow("Listen Port", self.listen_port)
        self.tabs.addTab(set_tab, "Settings")

        # Bottom controls already added to left column

    def _build_haptics_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # Device/output testing
        layout.addWidget(QLabel("Discovered rumble outputs"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.haptics_container = QWidget()
        self.haptics_layout = QVBoxLayout(self.haptics_container)
        self.haptics_layout.addStretch()
        scroll.setWidget(self.haptics_container)
        layout.addWidget(scroll, 2)

        # Live monitor
        layout.addWidget(QLabel("Live OSC parameter monitor"))
        self.monitor_table = QTableWidget(0, 7)
        self.monitor_table.setHorizontalHeaderLabels(["Parameter", "Controller", "Output", "Value", "Active", "Source", "Last Update"])
        self.monitor_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.monitor_table, 2)

        # Controller appearance / color assignment
        layout.addWidget(QLabel("Controller appearance"))
        app_scroll = QScrollArea()
        app_scroll.setWidgetResizable(True)
        self.appearance_container = QWidget()
        self.appearance_layout = QVBoxLayout(self.appearance_container)
        self.appearance_layout.addStretch()
        app_scroll.setWidget(self.appearance_container)
        layout.addWidget(app_scroll, 1)
        self._refresh_appearance()

        # Manual simulation
        sim_box = QHBoxLayout()
        sim_box.addWidget(QLabel("Simulate mapping"))
        self.sim_combo = QComboBox()
        sim_box.addWidget(self.sim_combo, 2)
        sim_box.addWidget(QLabel("Strength"))
        self.sim_slider = QSpinBox()
        self.sim_slider.setRange(0, 100)
        self.sim_slider.setValue(100)
        sim_box.addWidget(self.sim_slider)
        sim_test = QPushButton("Test")
        sim_test.clicked.connect(self._on_sim_test)
        sim_stop = QPushButton("Stop")
        sim_stop.clicked.connect(self._on_sim_stop)
        sim_box.addWidget(sim_test)
        sim_box.addWidget(sim_stop)
        layout.addLayout(sim_box)

        self.tabs.addTab(tab, "Haptics Test")

    def _append_log(self, msg):
        self.log_text.append(msg)
        self.log_text.ensureCursorVisible()

    def _cid_for_display(self, name):
        for c in self.controllers:
            if c.display_name == name:
                return c.cid
        return name

    def _display_name_for(self, cid):
        for c in self.controllers:
            if c.cid == cid:
                return c.display_name
        return cid

    def _apply_saved_appearance(self):
        saved = self.cfg.get("controllers", {})
        for c in self.controllers:
            info = saved.get(c.cid)
            if not isinstance(info, dict):
                continue
            if isinstance(info.get("display_name"), str):
                c.display_name = info["display_name"]
                c.name = c.display_name
            if isinstance(info.get("color"), list) and len(info["color"]) == 3:
                c.color = tuple(int(x) for x in info["color"])
            if isinstance(info.get("pair_colors"), list):
                parsed = []
                for col in info["pair_colors"]:
                    if isinstance(col, list) and len(col) == 3:
                        parsed.append(tuple(int(x) for x in col))
                if parsed:
                    c.pair_colors = parsed

    def _refresh_controller_list(self):
        self.ctrl_list.clear()
        for c in self.controllers:
            item = QListWidgetItem()
            row = ControllerRow(c)
            item.setSizeHint(row.sizeHint())
            self.ctrl_list.addItem(item)
            self.ctrl_list.setItemWidget(item, row)
        self.ctrl_combo.blockSignals(True)
        self.ctrl_combo.clear()
        for c in self.controllers:
            self.ctrl_combo.addItem(c.display_name)
        self.ctrl_combo.blockSignals(False)
        if self.controllers:
            self.ctrl_combo.setCurrentIndex(0)
        self._update_motor_range()

    def _update_motor_range(self):
        name = self.ctrl_combo.currentText()
        ctrl = next((c for c in self.controllers if c.display_name == name), None)
        if ctrl:
            self.motor_spin.setMaximum(max(0, len(ctrl.motors) - 1))
            self.motor_spin.setValue(0)

    def _refresh_mapping_list(self):
        self.map_table.setRowCount(0)
        for m in self.cfg.get("mappings", []):
            row = self.map_table.rowCount()
            self.map_table.insertRow(row)
            self.map_table.setItem(row, 0, QTableWidgetItem(m["osc_path"]))
            self.map_table.setItem(row, 1, QTableWidgetItem(self._display_name_for(m["controller"])))
            self.map_table.setItem(row, 2, QTableWidgetItem(str(m["motor"])))

    def _refresh_sim_combo(self):
        self.sim_combo.clear()
        for m in self.cfg.get("mappings", []):
            self.sim_combo.addItem(m["osc_path"])

    def _refresh_haptics(self):
        # clear
        while self.haptics_layout.count():
            item = self.haptics_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for c in self.controllers:
            for i, label in enumerate(c.motors):
                row = QFrame()
                rl = QHBoxLayout(row)
                if c.pair_colors:
                    for col in c.pair_colors:
                        sw = QFrame()
                        sw.setFixedSize(16, 16)
                        r, g, b = col
                        sw.setStyleSheet(f"background-color: rgb({r},{g},{b}); border: 1px solid #555; border-radius: 2px;")
                        rl.addWidget(sw)
                rl.addWidget(QLabel(f"{c.display_name}"))
                rl.addWidget(QLabel(f"({c.cid})"))
                rl.addWidget(QLabel(f"Output {i}: {label}"))
                status = QLabel("idle")
                status.setStyleSheet("color: gray;")
                rl.addWidget(status)
                strength = QSpinBox()
                strength.setRange(0, 100)
                strength.setValue(50)
                rl.addWidget(strength)
                test_btn = QPushButton("Test")
                def make_test(ctrl=c, idx=i, spin=strength, st=status):
                    def _test():
                        val = spin.value() / 100.0
                        ctrl.set_output(idx, val)
                        st.setText("active")
                        st.setStyleSheet("color: green;")
                        QTimer.singleShot(1000, lambda: self._stop_output(ctrl, idx, st))
                    return _test
                test_btn.clicked.connect(make_test())
                stop_btn = QPushButton("Stop")
                def make_stop(ctrl=c, idx=i, st=status):
                    def _stop():
                        ctrl.set_output(idx, 0.0)
                        st.setText("idle")
                        st.setStyleSheet("color: gray;")
                    return _stop
                stop_btn.clicked.connect(make_stop())
                rl.addWidget(test_btn)
                rl.addWidget(stop_btn)
                self.haptics_layout.addWidget(row)
        self.haptics_layout.addStretch()
        self._refresh_sim_combo()

    def _parse_color(self, text):
        text = text.strip()
        if text in JOYCON_COLOR_NAMES.values():
            # reverse lookup
            for rgb, name in JOYCON_COLOR_NAMES.items():
                if name == text.upper():
                    return rgb
        if text.startswith("#") and len(text) == 7:
            try:
                return (int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16))
            except ValueError:
                return None
        return None

    def _refresh_appearance(self):
        while self.appearance_layout.count():
            item = self.appearance_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for c in self.controllers:
            row = QFrame()
            rl = QHBoxLayout(row)
            rl.addWidget(QLabel(c.cid))
            name_edit = QLineEdit(c.display_name)
            rl.addWidget(name_edit, 2)
            if c.pair_colors:
                left_edit = QLineEdit("#%02x%02x%02x" % c.pair_colors[0])
                right_edit = QLineEdit("#%02x%02x%02x" % c.pair_colors[1])
                rl.addWidget(QLabel("L"))
                rl.addWidget(left_edit)
                rl.addWidget(QLabel("R"))
                rl.addWidget(right_edit)
            else:
                color_edit = QLineEdit("#%02x%02x%02x" % c.color if c.color else "#808080")
                rl.addWidget(color_edit)
            apply_btn = QPushButton("Apply")
            def make_apply(ctrl=c, ne=name_edit, le=locals().get("left_edit"), re=locals().get("right_edit"), ce=locals().get("color_edit")):
                def _apply():
                    ctrl.display_name = ne.text().strip() or ctrl.display_name
                    ctrl.name = ctrl.display_name
                    if ctrl.pair_colors is not None:
                        lc = self._parse_color(le.text()) if le else None
                        rc = self._parse_color(re.text()) if re else None
                        if lc:
                            ctrl.pair_colors[0] = lc
                        if rc:
                            ctrl.pair_colors[1] = rc
                    else:
                        col = self._parse_color(ce.text()) if ce else None
                        if col:
                            ctrl.color = col
                    self.cfg.setdefault("controllers", {})
                    self.cfg["controllers"][ctrl.cid] = {
                        "display_name": ctrl.display_name,
                        "color": list(ctrl.color) if ctrl.color else None,
                        "pair_colors": [list(x) for x in ctrl.pair_colors] if ctrl.pair_colors else None,
                    }
                    save_config(self.cfg)
                    self._refresh_controller_list()
                    self._refresh_haptics()
                    self._refresh_appearance()
                    self._append_log(f"Updated appearance for {ctrl.cid}")
                return _apply
            apply_btn.clicked.connect(make_apply())
            rl.addWidget(apply_btn)
            self.appearance_layout.addWidget(row)
        self.appearance_layout.addStretch()

    def _stop_output(self, ctrl, idx, status_label):
        ctrl.set_output(idx, 0.0)
        status_label.setText("idle")
        status_label.setStyleSheet("color: gray;")

    def _tick_controllers(self):
        for c in self.controllers:
            if c.is_connected():
                c.tick()

    def _on_monitor_update(self, entry):
        # update or insert row
        path = entry["path"]
        target_row = None
        for r in range(self.monitor_table.rowCount()):
            if self.monitor_table.item(r, 0) and self.monitor_table.item(r, 0).text() == path:
                target_row = r
                break
        if target_row is None:
            target_row = self.monitor_table.rowCount()
            self.monitor_table.insertRow(target_row)
        self.monitor_table.setItem(target_row, 0, QTableWidgetItem(path))
        ctrl_name = self._display_name_for(entry.get("controller")) if entry.get("controller") else "-"
        self.monitor_table.setItem(target_row, 1, QTableWidgetItem(ctrl_name))
        motor = entry.get("motor")
        self.monitor_table.setItem(target_row, 2, QTableWidgetItem(str(motor) if motor is not None else "-"))
        self.monitor_table.setItem(target_row, 3, QTableWidgetItem(f"{entry['value']:.2f}"))
        active_item = QTableWidgetItem("YES" if entry["active"] else "no")
        if entry["active"]:
            active_item.setForeground(QColor("green"))
        self.monitor_table.setItem(target_row, 4, active_item)
        self.monitor_table.setItem(target_row, 5, QTableWidgetItem(entry.get("source", "osc")))
        self.monitor_table.setItem(target_row, 6, QTableWidgetItem(entry.get("time", "")))

    def _on_refresh_controllers(self):
        self.controllers = discover_controllers(self.controllers)
        for c in self.controllers:
            c.connect()
        self._apply_saved_appearance()
        self.bridge.update_controllers(self.controllers)
        self._refresh_controller_list()
        self._refresh_haptics()
        self._refresh_appearance()
        self._append_log("Controllers refreshed")

    def _on_connect_all(self):
        for c in self.controllers:
            c.connect()
        self._refresh_controller_list()
        self._refresh_haptics()

    def _fill_preset(self):
        name = self.param_entry.text().strip()
        if name:
            self.osc_entry.setText(f"/avatar/parameters/{name}")

    def _use_selected_preset(self):
        name = self.preset_combo.currentText().strip()
        if name:
            self.osc_entry.setText(f"/avatar/parameters/{name}")

    def _on_add_mapping(self):
        path = self.osc_entry.text().strip()
        name = self.ctrl_combo.currentText().strip()
        cid = self._cid_for_display(name)
        motor = self.motor_spin.value()
        if not path or not cid:
            return
        mappings = [m for m in self.cfg["mappings"] if m["osc_path"] != path]
        mappings.append({"osc_path": path, "controller": cid, "motor": int(motor)})
        self.cfg["mappings"] = mappings
        self.bridge.update_config(self.cfg)
        self._refresh_mapping_list()
        self._refresh_sim_combo()
        self._append_log(f"Mapping added: {path} -> {cid} output {motor}")

    def _on_delete_mapping(self):
        rows = sorted(set(i.row() for i in self.map_table.selectedIndexes()), reverse=True)
        for r in rows:
            path_item = self.map_table.item(r, 0)
            if not path_item:
                continue
            path = path_item.text()
            self.cfg["mappings"] = [m for m in self.cfg["mappings"] if m["osc_path"] != path]
        self.bridge.update_config(self.cfg)
        self._refresh_mapping_list()
        self._refresh_sim_combo()

    def _on_save_mappings(self):
        self.cfg["listen"]["ip"] = self.listen_ip.text().strip() or "0.0.0.0"
        try:
            self.cfg["listen"]["port"] = int(self.listen_port.text().strip())
        except ValueError:
            pass
        save_config(self.cfg)
        self.bridge.update_config(self.cfg)
        self._append_log("Config saved")

    def _on_start(self):
        self._on_save_mappings()
        self.bridge.start()
        self._append_log("Starting OSC server")

    def _on_stop(self):
        self.bridge.stop()
        self._append_log("Stopping OSC server")

    def _on_sim_test(self):
        path = self.sim_combo.currentText().strip()
        if not path:
            return
        val = self.sim_slider.value() / 100.0
        self.bridge.dispatch(path, val, simulated=True)

    def _on_sim_stop(self):
        path = self.sim_combo.currentText().strip()
        if not path:
            return
        self.bridge.dispatch(path, 0.0, simulated=True)

    def _on_scheme_changed(self, scheme):
        app = QApplication.instance()
        app.setPalette(app.style().standardPalette())

    def closeEvent(self, event):
        try:
            self._tick_timer.stop()
            self.bridge.stop()
            for c in self.controllers:
                c.stop_all()
        except Exception:
            pass
        event.accept()


def main():
    app = QApplication(sys.argv)
    icon_path = find_image("app.ico" if sys.platform.startswith("win") else "app.png")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    window = MainWindow()
    window.resize(900, 620)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
