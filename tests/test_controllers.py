from unittest.mock import MagicMock

import main


class FakeJoyCon:
    def __init__(self):
        self.calls = []

    def rumble_simple(self):
        self.calls.append("simple")

    def rumble_stop(self):
        self.calls.append("stop")

    def connected(self):
        return True


def test_joycon_output_independent_and_state():
    c = main.JoyConController("serial", 0x2006)
    fake = FakeJoyCon()
    c._jc = fake
    assert c.set_output(0, 1.0) is True
    assert c.get_output(0) == 1.0
    assert "simple" in fake.calls
    assert c.set_output(0, 0.0) is True
    assert "stop" in fake.calls
    # invalid index does not crash and returns False
    assert c.set_output(5, 1.0) is False


class FakeJS:
    def __init__(self):
        self.calls = []

    def rumble(self, low, high, duration):
        self.calls.append((low, high, duration))

    def get_init(self):
        return True


def test_gamepad_outputs_independent_and_combined():
    c = main.GamepadController(0, "guid")
    fake = FakeJS()
    c._js = fake
    c.set_output(0, 0.5)
    c.set_output(1, 0.25)
    # second output must not reset first
    assert c.get_output(0) == 0.5
    assert c.get_output(1) == 0.25
    # combined apply happened
    assert any(call[0] == 0.5 and call[1] == 0.25 for call in fake.calls)
    # changing output 0 keeps output 1 state
    c.set_output(0, 0.1)
    assert c.get_output(1) == 0.25
    # stop all
    c.stop_all()
    assert c.get_output(0) == 0.0 and c.get_output(1) == 0.0
    assert fake.calls[-1] == (0, 0, 0)


def test_controller_ids_stable():
    c1 = main.JoyConController("abc", 0x2006)
    c2 = main.JoyConController("abc", 0x2006)
    assert c1.cid == c2.cid
    g1 = main.GamepadController(0, "guid-1")
    g2 = main.GamepadController(5, "guid-1")
    assert g1.cid == g2.cid
