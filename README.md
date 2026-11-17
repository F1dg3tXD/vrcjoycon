# VRCJoyCon — OSC Joy-Con Haptics for VRChat

VRCJoyCon connects Nintendo Switch Joy-Cons (and other rumble-capable gamepads) to VRChat/ChilloutVR through OSC.  
Avatar contact receivers and custom avatar parameters can activate individual rumble outputs, allowing inexpensive localized haptic feedback on the body.

## Supported platforms

- Linux (primary development target)
- Windows
- macOS (Apple Silicon and Intel, built separately)

## Supported controller backends

- Nintendo Switch Joy-Cons through direct HID access (`hid`)
- Paired Joy-Cons or other gamepads exposed by SDL/pygame
- Other rumble-capable controllers supported by the pygame backend

Joy-Cons can be used individually through HID, or as a paired/logical controller through SDL. The application discovers all available backends and shows their available rumble outputs.

## Installation

### Linux

Install system dependencies:

```bash
sudo apt install libhidapi-dev
```

Optional but recommended for pygame/SDL:

```bash
sudo apt install python3-pygame
```

Create a Python 3.11+ environment and install dependencies:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt  # or: pip install pyside6 python-osc pygame hid
```

If dependencies are managed with Poetry:

```bash
poetry install
```

Run the application:

```bash
cd src
python main.py
```

### HID permissions on Linux

Do not run the whole application as root. Instead, install udev rules for Joy-Cons:

```bash
cat << 'EOF' | sudo tee /etc/udev/rules.d/50-nintendo-switch.rules
KERNEL=="hidraw*", SUBSYSTEM=="hidraw", ATTRS{idVendor}=="057e", ATTRS{idProduct}=="2006", MODE="0666"
KERNEL=="hidraw*", SUBSYSTEM=="hidraw", ATTRS{idVendor}=="057e", ATTRS{idProduct}=="2007", MODE="0666"
EOF
sudo udevadm control --reload-rules
```

If direct HID access does not work, the `hid_nintendo` kernel module may have claimed the device. Disable it:

```bash
echo "blacklist hid_nintendo" | sudo tee /etc/modprobe.d/blacklist-hid-nintendo.conf
sudo rmmod hid_nintendo
```

Then reconnect the Joy-Cons.

### Windows

Install Python 3.11+, then:

```bash
pip install pyside6 python-osc pygame hid
cd src
python main.py
```

The bundled `hidapi.dll` may be required for direct Joy-Con access on Windows.

### macOS

Use the provided build script on a native macOS machine:

```bash
./scripts/build_macos.sh
```

This produces an architecture-specific `.app` bundle and ZIP.

## Multiple Joy-Cons

The application supports multiple Joy-Cons connected to one computer.

Important: two Joy-Cons paired together may appear as one logical SDL controller instead of two independent devices. In that case the SDL backend may expose one or more rumble outputs. Use the **Haptics Test** tab to identify which physical controller vibrates for each output.

For independent control of each Joy-Con, connect them individually and access them through the HID backend where possible.

## Configuration

Configuration is stored in `src/config.json`. The older `src/config.ini` may be migrated automatically on first run; a backup is preserved.

Each mapping associates:

- an OSC parameter path
- a controller identifier
- a rumble output index on that controller

Example mappings:

```json
{
  "version": 1,
  "listen": {
    "ip": "0.0.0.0",
    "port": 9001
  },
  "mappings": [
    {
      "osc_path": "/avatar/parameters/joyconrumble1",
      "controller": "joycon:2006:SERIAL",
      "motor": 0
    },
    {
      "osc_path": "/avatar/parameters/joyconrumble2",
      "controller": "gamepad:GUID",
      "motor": 1
    }
  ]
}
```

## OSC server

The application listens on UDP port 9001 by default. Set the listen IP to `0.0.0.0` to receive remote OSC traffic.

Supported value types:

- float in the range 0.0 to 1.0
- integer
- boolean

Values are clamped to the supported range. Zero stops the corresponding output.

## Haptics Test tab

The **Haptics Test** tab lets you identify and configure rumble outputs without VRChat.

Features:

- lists discovered controllers and outputs
- tests each output independently
- adjustable strength
- stop controls
- live OSC parameter monitor
- manual OSC parameter simulation

Short test pulses make it easy to identify which physical controller vibrates.

## VRChat avatar setup

Use VRChat Contact Receivers:

1. Add one or more Contact Receiver components to your avatar.
2. Set Receiver Type to **Proximity**.
3. Configure collision tags (for example `Hand`).
4. Enable **Allow Self** while testing.
5. Assign the desired avatar parameter, for example `joyconrumble1`.
6. Add the corresponding float parameter to the avatar's animator parameters with a default value of `0.0`.

Keep `joyconrumble1` and `joyconrumble2` as the default examples, but you can define additional custom parameters and mappings in VRCJoyCon.

Joy-Cons can be attached to body straps or clothing to provide inexpensive localized vibration feedback.

## Building

Build locally:

```bash
python scripts/build.py
```

Platform-specific scripts:

```bash
scripts/build_linux.sh
scripts/build_macos.sh
scripts/build_windows.bat
```

GitHub Actions builds and uploads separate artifacts for:

- Windows x64
- Linux x64
- Linux ARM64
- macOS ARM64

## Known limitations

- Direct Joy-Con rumble currently uses the existing simple rumble command; strength is effectively on/off for HID Joy-Cons.
- Some Joy-Con pairs may be exposed as one logical controller by SDL.
- Physical hardware behavior varies between genuine and third-party Joy-Cons.
- macOS builds must be produced on native runners for their target architecture.