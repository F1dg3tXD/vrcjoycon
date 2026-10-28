#!/usr/bin/env python3
"""VRCJoyCon - cross-platform OSC haptics controller.

Maps VRChat/ChilloutVR OSC parameters to rumble motors on Nintendo Switch
Joy-Cons and standard gamepads. Provides a PySide6 UI for assigning
parameters, controllers and motors, plus a standard OSC UDP server.
"""

import json
import logging
import os
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("VRCJOYCON")

# Optional heavy deps
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

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFormLayout, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QPushButton,
    QScrollArea, QSpinBox, QSplitter, QTabWidget, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

CONFIG_PATH = HERE / "config.json"


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


# ---------------- Controller abstraction ----------------

class Controller:
    def __init__(self, cid, kind, name, motors):
        self.cid = cid
        self.kind = kind
        self.name = name
        self.display_name = name
        self.color = None
        self.icon = None
        self.motors = motors
        self._instance = None

    def connect(self):
        return True

    def rumble(self, motor_index, strength):
        pass

    def stop_all(self):
        for i in range(len(self.motors)):
            self.rumble(i, 0.0)

    def is_connected(self):
        return True


class JoyConController(Controller):
    def __init__(self, serial, product_id):
        super().__init__(f"joycon:{serial}", "joycon",
                         f"Joy-Con ({'L' if product_id == 0x2006 else 'R'})",
                         ["rumble"])
        self.serial = serial
        self.product_id = product_id
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

    def rumble(self, motor_index, strength):
        if not self._jc:
            return
        try:
            if strength and strength > 0:
                self._jc.rumble_simple()
            else:
                self._jc.rumble_stop()
        except Exception as e:
            log.error("JoyCon rumble error: %s", e)

    def is_connected(self):
        try:
            return bool(self._jc) and self._jc.connected()
        except Exception:
            return False


class GamepadController(Controller):
    def __init__(self, index):
        super().__init__(f"gamepad:{index}", "gamepad", f"Gamepad #{index}", ["low", "high"])
        self.index = index
        self._js = None

    def connect(self):
        if pygame is None:
            return False
        try:
            pygame.joystick.init()
            self._js = pygame.joystick.Joystick(self.index)
            self._js.init()
            name = self._js.get_name() or f"Gamepad #{self.index}"
            self.name = name
            self.display_name = f"🎮 {name}"
            self.icon = "🎮"
            return True
        except Exception as e:
            log.error("Gamepad connect failed: %s", e)
            return False

    def rumble(self, motor_index, strength):
        if not self._js:
            return
        try:
            low = strength if motor_index == 0 else 0.0
            high = strength if motor_index == 1 else 0.0
            if strength and strength > 0:
                self._js.rumble(low, high, 250)
            else:
                self._js.rumble(0, 0, 0)
        except Exception as e:
            log.error("Gamepad rumble error: %s", e)

    def is_connected(self):
        try:
            return bool(self._js) and self._js.get_init()
        except Exception:
            return False


def discover_controllers():
    controllers = []
    if get_device_ids is not None:
        try:
            for vid, pid, serial in get_device_ids():
                c = JoyConController(serial, pid)
                c.connect()
                controllers.append(c)
        except Exception as e:
            log.error("JoyCon discovery error: %s", e)
    if pygame is not None:
        try:
            pygame.joystick.init()
            for i in range(pygame.joystick.get_count()):
                c = GamepadController(i)
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

DEFAULT_CONFIG = {
    "listen": {"ip": "0.0.0.0", "port": 9001},
    "mappings": [],
}


def load_config():
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open() as f:
                data = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                data.setdefault(k, v)
            return data
        except Exception as e:
            log.error("Config load error: %s", e)
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg):
    try:
        with CONFIG_PATH.open("w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        log.error("Config save error: %s", e)


# ---------------- OSC Bridge ----------------

class LogEmitter(QObject):
    message = Signal(str)


class OscBridge:
    def __init__(self, cfg, controllers, emitter):
        self.cfg = cfg
        self.controllers = {c.cid: c for c in controllers}
        self.emitter = emitter
        self.running = False
        self.thread = None
        self.loop = None
        self.server = None
        self.transport = None
        self.mappings = {}
        self._rebuild_mappings()

    def _log(self, msg):
        try:
            self.emitter.message.emit(msg)
        except Exception:
            print(msg)

    def _rebuild_mappings(self):
        self.mappings = {}
        for m in self.cfg.get("mappings", []):
            self.mappings[m["osc_path"]] = (m["controller"], m["motor"])

    def update_controllers(self, controllers):
        self.controllers = {c.cid: c for c in controllers}

    def update_config(self, cfg):
        self.cfg = cfg
        self._rebuild_mappings()

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
        self.loop = None
        try:
            import asyncio
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._serve())
        except Exception as e:
            self._log(f"OSC server error: {e}")
        finally:
            self.running = False

    async def _serve(self):
        import asyncio
        raw_ip = str(self.cfg["listen"]["ip"]).strip()
        if raw_ip.lower() in ("any", "*", "", "all"):
            raw_ip = "0.0.0.0"
        port = int(self.cfg["listen"]["port"])

        d = dispatcher.Dispatcher()
        for path in list(self.mappings.keys()):
            d.map(path, lambda address, *args, p=path: self._handle(p, *args))
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

    def _handle(self, path, *args):
        mapping = self.mappings.get(path)
        if not mapping:
            return
        cid, motor = mapping
        ctrl = self.controllers.get(cid)
        if not ctrl:
            self._log(f"No controller {cid} for {path}")
            return
        val = 0.0
        if args:
            v = args[0]
            if isinstance(v, (list, tuple)) and v:
                v = v[0]
            try:
                if isinstance(v, bool):
                    val = 1.0 if v else 0.0
                else:
                    val = float(v)
            except Exception:
                val = 1.0 if v else 0.0
        try:
            ctrl.rumble(int(motor), val)
            self._log(f"{path} -> {cid} motor {motor}: {val}")
        except Exception as e:
            self._log(f"Rumble error: {e}")


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
        layout.addWidget(QLabel(f"motors={controller.motors}"))
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
        self.bridge = OscBridge(self.cfg, self.controllers, self.emitter)
        self.emitter.message.connect(self._append_log)

        self._build_ui()
        self._refresh_controller_list()
        self._refresh_mapping_list()

        # theme responsiveness
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
        self.map_table.setHorizontalHeaderLabels(["OSC Parameter", "Controller", "Motor"])
        self.map_table.horizontalHeader().setStretchLastSection(True)
        map_layout.addWidget(self.map_table, 1)

        form = QHBoxLayout()
        form.addWidget(QLabel("OSC path"))
        self.osc_entry = QLineEdit()
        form.addWidget(self.osc_entry, 2)
        form.addWidget(QLabel("Controller"))
        self.ctrl_combo = QComboBox()
        form.addWidget(self.ctrl_combo, 2)
        form.addWidget(QLabel("Motor"))
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

    def _append_log(self, msg):
        self.log_text.append(msg)
        self.log_text.ensureCursorVisible()

    def _display_name_for(self, cid):
        for c in self.controllers:
            if c.cid == cid:
                return c.display_name
        return cid

    def _cid_for_display(self, name):
        for c in self.controllers:
            if c.display_name == name:
                return c.cid
        return name

    def _refresh_controller_list(self):
        self.ctrl_list.clear()
        for c in self.controllers:
            item = QListWidgetItem()
            row = ControllerRow(c)
            item.setSizeHint(row.sizeHint())
            self.ctrl_list.addItem(item)
            self.ctrl_list.setItemWidget(item, row)
        # combo
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

    def _on_refresh_controllers(self):
        self.controllers = discover_controllers()
        for c in self.controllers:
            c.connect()
        self.bridge.update_controllers(self.controllers)
        self._refresh_controller_list()
        self._append_log("Controllers refreshed")

    def _on_connect_all(self):
        for c in self.controllers:
            c.connect()
        self._refresh_controller_list()

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
        self._append_log(f"Mapping added: {path} -> {cid} motor {motor}")

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

    def _on_scheme_changed(self, scheme):
        app = QApplication.instance()
        app.setPalette(app.style().standardPalette())

    def closeEvent(self, event):
        try:
            self.bridge.stop()
        except Exception:
            pass
        event.accept()


def main():
    app = QApplication(sys.argv)
    # native style; keep responsive to scheme changes
    icon_path = find_image("app.ico" if sys.platform.startswith("win") else "app.png")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    window = MainWindow()
    window.resize(820, 560)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
