import time
from PySide6.QtWidgets import QApplication

import main


class FakeController:
    def __init__(self, cid):
        self.cid = cid
        self.outputs = {}

    def set_output(self, index, strength):
        self.outputs[index] = strength

    def is_connected(self):
        return True


class FakeEmitter:
    def __init__(self):
        self.messages = []

    @property
    def message(self):
        return self

    def emit(self, msg):
        self.messages.append(msg)


class FakeMonitor:
    def __init__(self):
        self.updates = []

    @property
    def update(self):
        return self

    def emit(self, entry):
        self.updates.append(entry)


def test_osc_dispatch_multiple_and_zero(qapp):
    ctrl = FakeController("c1")
    cfg = {"listen": {"ip": "0.0.0.0", "port": 9001}, "mappings": [
        {"osc_path": "/avatar/parameters/joyconrumble1", "controller": "c1", "motor": 0},
        {"osc_path": "/avatar/parameters/joyconrumble2", "controller": "c1", "motor": 1},
    ]}
    bridge = main.OscBridge(cfg, [ctrl], FakeEmitter(), FakeMonitor())
    bridge.dispatch("/avatar/parameters/joyconrumble1", 1.0)
    bridge.dispatch("/avatar/parameters/joyconrumble2", 0.5)
    assert ctrl.outputs[0] == 1.0
    assert ctrl.outputs[1] == 0.5
    # zero stops correct output only
    bridge.dispatch("/avatar/parameters/joyconrumble1", 0.0)
    assert ctrl.outputs[0] == 0.0
    assert ctrl.outputs[1] == 0.5


def test_osc_invalid_and_clamp(qapp):
    ctrl = FakeController("c1")
    cfg = {"listen": {"ip": "0.0.0.0", "port": 9001}, "mappings": [
        {"osc_path": "/p", "controller": "c1", "motor": 0},
    ]}
    bridge = main.OscBridge(cfg, [ctrl], FakeEmitter(), FakeMonitor())
    bridge.dispatch("/p", 2.0)
    assert ctrl.outputs[0] == 1.0
    bridge.dispatch("/p", "bad")
    assert ctrl.outputs[0] == 0.0
    bridge.dispatch("/p", True)
    assert ctrl.outputs[0] == 1.0


def test_osc_callback_boolean(qapp):
    ctrl = FakeController("c1")
    cfg = {"listen": {"ip": "0.0.0.0", "port": 9001}, "mappings": [
        {"osc_path": "/p", "controller": "c1", "motor": 0},
    ]}
    bridge = main.OscBridge(cfg, [ctrl], FakeEmitter(), FakeMonitor())
    bridge._osc_callback("/p", True)
    assert ctrl.outputs[0] == 1.0


def test_osc_unmapped_does_not_crash(qapp):
    bridge = main.OscBridge({"listen": {"ip": "0.0.0.0", "port": 9001}, "mappings": []},
                            [], FakeEmitter(), FakeMonitor())
    bridge.dispatch("/nope", 1.0)  # no crash


def test_monitor_update_emitted(qapp):
    mon = FakeMonitor()
    bridge = main.OscBridge({"listen": {"ip": "0.0.0.0", "port": 9001}, "mappings": []},
                            [], FakeEmitter(), mon)
    bridge.dispatch("/x", 0.3, simulated=True)
    assert mon.updates
    assert mon.updates[0]["path"] == "/x"
    assert mon.updates[0]["source"] == "simulated"
