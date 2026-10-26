#!/usr/bin/env python3
"""VRCJoyCon - cross-platform OSC haptics controller.

Maps VRChat/ChilloutVR OSC parameters to rumble motors on Nintendo Switch
Joy-Cons and standard gamepads. Provides a Tkinter UI for assigning
parameters, controllers and motors, plus a standard OSC UDP server.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

# Ensure local imports work when run as script
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("VRCJOYCON")

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

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
from pythonosc.udp_client import SimpleUDPClient

CONFIG_PATH = HERE / "config.json"


# ---------------- Controller abstraction ----------------

class Controller:
    def __init__(self, cid, kind, name, motors):
        self.cid = cid
        self.kind = kind
        self.name = name
        self.display_name = name
        self.color = None  # (r,g,b) or None
        self.icon = None
        self.motors = motors  # list of motor labels
        self._instance = None

    def connect(self):
        return True

    def rumble(self, motor_index, strength):
        """strength in [0,1]"""
        pass

    def stop_all(self):
        for i in range(len(self.motors)):
            self.rumble(i, 0.0)

    def is_connected(self):
        return True


class JoyConController(Controller):
    def __init__(self, serial, product_id):
        super().__init__(f"joycon:{serial}", "joycon",
                         f"Joy-Con ({'L' if product_id == 0x2006 else 'R'}) {serial}",
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
                btn = self._jc.color_btn
                self.color = tuple(int(x) for x in body) if body else None
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
            # pygame rumble takes low/high frequencies
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
    # Joy-Cons
    if get_device_ids is not None:
        try:
            for vid, pid, serial in get_device_ids():
                c = JoyConController(serial, pid)
                c.connect()
                controllers.append(c)
        except Exception as e:
            log.error("JoyCon discovery error: %s", e)
    # Gamepads
    if pygame is not None:
        try:
            pygame.joystick.init()
            for i in range(pygame.joystick.get_count()):
                c = GamepadController(i)
                c.connect()
                controllers.append(c)
        except Exception as e:
            log.error("Gamepad discovery error: %s", e)
    # ensure unique display names
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
    "mappings": [],  # list of {osc_path, controller, motor}
}


def load_config():
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open() as f:
                data = json.load(f)
            # merge defaults
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

class OscBridge:
    def __init__(self, cfg, controllers, log_cb):
        self.cfg = cfg
        self.controllers = {c.cid: c for c in controllers}
        self.log_cb = log_cb
        self.server = None
        self.transport = None
        self.protocol = None
        self.loop = None
        self.thread = None
        self.running = False
        self.mappings = {}  # osc_path -> (controller_id, motor_index)
        self._rebuild_mappings()

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
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._serve())
        except Exception as e:
            self.log_cb(f"OSC server error: {e}")
        finally:
            self.running = False

    async def _serve(self):
        ip = self.cfg["listen"]["ip"]
        port = int(self.cfg["listen"]["port"])

        def handler(address, *args):
            self._handle(address, *args)

        d = dispatcher.Dispatcher()
        # map each configured path
        for path in list(self.mappings.keys()):
            d.map(path, lambda address, *args, p=path: self._handle(p, *args))
        d.set_default_handler(lambda address, *args: self._default(address, *args))

        self.server = osc_server.AsyncIOOSCUDPServer((ip, port), d, self.loop)
        self.log_cb(f"OSC server listening on {ip}:{port}")
        transport, protocol = await self.server.create_serve_endpoint()
        self.transport = transport
        # keep running
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
            self.log_cb(f"No controller {cid} for {path}")
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
            self.log_cb(f"{path} -> {cid} motor {motor}: {val}")
        except Exception as e:
            self.log_cb(f"Rumble error: {e}")

    def _default(self, address, *args):
        # unhandled; could relay
        pass


# ---------------- UI ----------------

class App:
    def __init__(self, root):
        self.root = root
        self.root.title("VRCJoyCon")
        self.cfg = load_config()
        self.controllers = discover_controllers()
        # connect controllers
        for c in self.controllers:
            c.connect()
        self.bridge = OscBridge(self.cfg, self.controllers, self._log)
        self._build_ui()
        self._refresh_controller_list()
        self._refresh_mapping_list()

    def _build_ui(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True)

        # Controllers tab
        tab_ctrl = ttk.Frame(nb)
        nb.add(tab_ctrl, text="Controllers")
        ttk.Label(tab_ctrl, text="Discovered controllers").pack(anchor="w", padx=5, pady=5)
        self.ctrl_container = ttk.Frame(tab_ctrl)
        self.ctrl_container.pack(fill="both", expand=True, padx=5)
        btnf = ttk.Frame(tab_ctrl)
        btnf.pack(fill="x", padx=5, pady=5)
        ttk.Button(btnf, text="Refresh", command=self._on_refresh_controllers).pack(side="left")
        ttk.Button(btnf, text="Connect All", command=self._on_connect_all).pack(side="left", padx=5)

        # Mappings tab
        tab_map = ttk.Frame(nb)
        nb.add(tab_map, text="Mappings")
        cols = ("osc", "controller", "motor")
        self.map_tree = ttk.Treeview(tab_map, columns=cols, show="headings", height=12)
        for c, t in zip(cols, ("OSC Parameter", "Controller", "Motor")):
            self.map_tree.heading(c, text=t)
        self.map_tree.pack(fill="both", expand=True, padx=5, pady=5)

        form = ttk.Frame(tab_map)
        form.pack(fill="x", padx=5, pady=5)
        ttk.Label(form, text="OSC path").grid(row=0, column=0)
        self.osc_entry = ttk.Entry(form, width=30)
        self.osc_entry.grid(row=0, column=1, padx=5)
        ttk.Label(form, text="Controller").grid(row=0, column=2)
        self.ctrl_combo = ttk.Combobox(form, values=[], width=25)
        self.ctrl_combo.grid(row=0, column=3, padx=5)
        self.ctrl_combo.bind("<<ComboboxSelected>>", lambda e: self._update_motor_combo())
        ttk.Label(form, text="Motor").grid(row=0, column=4)
        self.motor_combo = ttk.Combobox(form, values=[], width=10)
        self.motor_combo.grid(row=0, column=5, padx=5)

        bf = ttk.Frame(tab_map)
        bf.pack(fill="x", padx=5, pady=5)
        ttk.Button(bf, text="Add/Update", command=self._on_add_mapping).pack(side="left")
        ttk.Button(bf, text="Delete", command=self._on_delete_mapping).pack(side="left", padx=5)
        ttk.Button(bf, text="Save", command=self._on_save_mappings).pack(side="left", padx=5)

        # Settings tab
        tab_set = ttk.Frame(nb)
        nb.add(tab_set, text="Settings")
        ttk.Label(tab_set, text="Listen IP").grid(row=0, column=0, padx=5, pady=5)
        self.listen_ip = ttk.Entry(tab_set, width=20)
        self.listen_ip.insert(0, self.cfg["listen"]["ip"])
        self.listen_ip.grid(row=0, column=1)
        ttk.Label(tab_set, text="Listen Port").grid(row=0, column=2, padx=5)
        self.listen_port = ttk.Entry(tab_set, width=8)
        self.listen_port.insert(0, str(self.cfg["listen"]["port"]))
        self.listen_port.grid(row=0, column=3)

        # Log tab
        tab_log = ttk.Frame(nb)
        nb.add(tab_log, text="Log")
        self.log_text = scrolledtext.ScrolledText(tab_log, height=15)
        self.log_text.pack(fill="both", expand=True, padx=5, pady=5)

        # Bottom controls
        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", padx=5, pady=5)
        ttk.Button(bottom, text="Start OSC Server", command=self._on_start).pack(side="left")
        ttk.Button(bottom, text="Stop OSC Server", command=self._on_stop).pack(side="left", padx=5)

    def _log(self, msg):
        def _do():
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
        try:
            self.root.after(0, _do)
        except Exception:
            print(msg)

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
        for w in self.ctrl_container.winfo_children():
            w.destroy()
        for c in self.controllers:
            status = "connected" if c.is_connected() else "disconnected"
            row = ttk.Frame(self.ctrl_container)
            row.pack(fill="x", pady=2)
            # color swatch / icon
            if c.color:
                sw = tk.Canvas(row, width=24, height=24, highlightthickness=1)
                sw.pack(side="left", padx=4)
                hexcolor = "#{:02x}{:02x}{:02x}".format(*c.color)
                sw.create_rectangle(0, 0, 24, 24, fill=hexcolor, outline="#333")
            elif c.icon:
                ttk.Label(row, text=c.icon, font=("TkDefaultFont", 14)).pack(side="left", padx=4)
            else:
                ttk.Label(row, text="🎮", font=("TkDefaultFont", 14)).pack(side="left", padx=4)
            ttk.Label(row, text=f"{c.display_name}  [{status}]  motors={c.motors}").pack(side="left", padx=4)
        # update combo with display names
        self.ctrl_combo["values"] = [c.display_name for c in self.controllers]
        if self.controllers and not self.ctrl_combo.get():
            self.ctrl_combo.set(self.controllers[0].display_name)
        self._update_motor_combo()

    def _update_motor_combo(self):
        name = self.ctrl_combo.get()
        ctrl = next((c for c in self.controllers if c.display_name == name), None)
        if ctrl:
            self.motor_combo["values"] = list(range(len(ctrl.motors)))
            if not self.motor_combo.get():
                self.motor_combo.set("0")

    def _refresh_mapping_list(self):
        for item in self.map_tree.get_children():
            self.map_tree.delete(item)
        for m in self.cfg.get("mappings", []):
            self.map_tree.insert("", "end", values=(m["osc_path"], self._display_name_for(m["controller"]), m["motor"]))

    def _on_refresh_controllers(self):
        self.controllers = discover_controllers()
        for c in self.controllers:
            c.connect()
        self.bridge.update_controllers(self.controllers)
        self._refresh_controller_list()
        self._log("Controllers refreshed")

    def _on_connect_all(self):
        for c in self.controllers:
            c.connect()
        self._refresh_controller_list()

    def _on_add_mapping(self):
        path = self.osc_entry.get().strip()
        name = self.ctrl_combo.get().strip()
        cid = self._cid_for_display(name)
        motor = self.motor_combo.get().strip()
        if not path or not cid:
            messagebox.showwarning("Missing", "OSC path and controller required")
            return
        try:
            motor_i = int(motor)
        except ValueError:
            motor_i = 0
        # replace existing
        mappings = [m for m in self.cfg["mappings"] if m["osc_path"] != path]
        mappings.append({"osc_path": path, "controller": cid, "motor": motor_i})
        self.cfg["mappings"] = mappings
        self.bridge.update_config(self.cfg)
        self._refresh_mapping_list()
        self._log(f"Mapping added: {path} -> {cid} motor {motor_i}")

    def _on_delete_mapping(self):
        sel = self.map_tree.selection()
        if not sel:
            return
        vals = self.map_tree.item(sel[0], "values")
        path = vals[0]
        self.cfg["mappings"] = [m for m in self.cfg["mappings"] if m["osc_path"] != path]
        self.bridge.update_config(self.cfg)
        self._refresh_mapping_list()

    def _on_save_mappings(self):
        # update listen settings from entries
        self.cfg["listen"]["ip"] = self.listen_ip.get().strip() or "0.0.0.0"
        try:
            self.cfg["listen"]["port"] = int(self.listen_port.get().strip())
        except ValueError:
            pass
        save_config(self.cfg)
        self.bridge.update_config(self.cfg)
        self._log("Config saved")

    def _on_start(self):
        self._on_save_mappings()
        self.bridge.start()
        self._log("Starting OSC server")

    def _on_stop(self):
        self.bridge.stop()
        self._log("Stopping OSC server")


def main():
    root = tk.Tk()
    app = App(root)
    root.geometry("720x520")
    root.mainloop()


if __name__ == "__main__":
    main()
