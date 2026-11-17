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
        self.motors = list(motors)
        self.backend = backend
        self.output_states = [0.0 for _ in self.motors]
        self._lock = threading.Lock()

    def connect(self):
        return True

    def _apply(self):
        pass

    def set_output(self, index, strength):
        strength = clamp_strength(strength)
        with self._lock:
            if index < 0 or index >= len(self.motors):
                return False
            self.output_states[index] = strength
        try:
            self._apply()
        except Exception as e:
            log.error("Controller apply error %s: %s", self.cid, e)
        return True

    def get_output(self, index):
        with self._lock:
            if index < 0 or index >= len(self.motors):
                return 0.0
            return self.output_states[index]

    def tick(self):
        try:
            self._apply()
        except Exception as e:
            log.error("Controller tick error %s: %s", self.cid, e)

    def stop_all(self):
        with self._lock:
            self.output_states = [0.0 for _ in self.motors]
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
        super().__init__(f"joycon:{product_id:04x}:{serial or path}", "joycon",
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
                for vid, pid, serial in get_device_ids():
                    if serial == self.serial:
                        ids = (vid, pid, serial)
                        break
            if not ids:
                return False
            self._jc = _JoyConBase(*ids)
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

    def _apply(self):
        if not self._jc:
            return
        active = any(s > 0.0 for s in self.output_states)
        if active:
            self._jc.rumble_simple()
        else:
            self._jc.rumble_stop()

    def rumble(self, motor_index, strength):
        self.set_output(motor_index, strength)

    def is_connected(self):
        try:
            return bool(self._jc) and self._jc.connected()
        except Exception:
            return False


class GamepadController(Controller):
    def __init__(self, index, guid=""):
        cid = f"gamepad:{guid}" if guid else f"gamepad:index:{index}"
        super().__init__(cid, "gamepad", f"Gamepad #{index}", ["Low-frequency", "High-frequency"],
                         backend="sdl")
        self.index = index
        self.guid = guid
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
                if self.guid and candidate.get_guid() == self.guid:
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
            if not self.guid:
                self.guid = self._js.get_guid()
                self.cid = f"gamepad:{self.guid}" if self.guid else self.cid
            self.display_name = f"🎮 {name}"
            self.icon = "🎮"
            return True
        except Exception as e:
            log.error("Gamepad connect failed: %s", e)
            return False

    def _apply(self):
        if not self._js:
            return
        states = list(self.output_states)
        low = states[0] if len(states) > 0 else 0.0
        high = states[1] if len(states) > 1 else 0.0
        if any(s > 0.0 for s in states):
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

    # Joy-Cons via HID
    if get_device_ids is not None:
        try:
            for vid, pid, serial in get_device_ids():
                cid = f"joycon:{pid:04x}:{serial}"
                if cid in existing_by_cid and existing_by_cid[cid].is_connected():
                    controllers.append(existing_by_cid[cid])
                    continue
                c = JoyConController(serial, pid)
                c.connect()
                controllers.append(c)
        except Exception as e:
            log.error("JoyCon discovery error: %s", e)

    # Gamepads via SDL/pygame
    if pygame is not None:
        try:
            pygame.joystick.init()
            for i in range(pygame.joystick.get_count()):
                js = pygame.joystick.Joystick(i)
                js.init()
                guid = js.get_guid()
                cid = f"gamepad:{guid}" if guid else f"gamepad:index:{i}"
                if cid in existing_by_cid and existing_by_cid[cid].is_connected():
                    controllers.append(existing_by_cid[cid])
                    continue
                c = GamepadController(i, guid)
                c.connect()
                controllers.append(c)
        except Exception as e:
            log.error("Gamepad discovery error: %s", e)

    # unique display names
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
    return {"version": CONFIG_VERSION, "listen": {"ip": "0.0.0.0", "port": 9001}, "mappings": []}


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
            ctrl.set_output(int(motor), value)
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
        if controller.color:
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
        root_layout = QVBoxLayout(central)

        self.tabs = QTabWidget()
        root_layout.addWidget(self.tabs, 1)

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

        # Log tab
        log_tab = QWidget()
        log_layout = QVBoxLayout(log_tab)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        log_layout.addWidget(self.log_text)
        self.tabs.addTab(log_tab, "Log")

        # Bottom controls
        bottom = QHBoxLayout()
        start_btn = QPushButton("Start OSC Server")
        start_btn.clicked.connect(self._on_start)
        stop_btn = QPushButton("Stop OSC Server")
        stop_btn.clicked.connect(self._on_stop)
        bottom.addWidget(start_btn)
        bottom.addWidget(stop_btn)
        bottom.addStretch()
        root_layout.addLayout(bottom)

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
        self.bridge.update_controllers(self.controllers)
        self._refresh_controller_list()
        self._refresh_haptics()
        self._append_log("Controllers refreshed")

    def _on_connect_all(self):
        for c in self.controllers:
            c.connect()
        self._refresh_controller_list()
        self._refresh_haptics()

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
