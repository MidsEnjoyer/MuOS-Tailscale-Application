# Tailscale for muOS (RG40XXV)

A native GUI app for connecting your Anbernic RG40XXV (and other muOS devices) to your [Tailscale](https://tailscale.com) tailnet. Browse your peers, send/receive files via Taildrop, and authenticate via QR code — all from your handheld.


## Features

- **Connect / Disconnect** — bring Tailscale up or down, with full status display
- **QR Code login** — scan with your phone to authenticate, no keyboard needed
- **Taildrop file transfer** — receive incoming files or send files to any peer on your tailnet
- **Live status** — shows connection state, your Tailscale IP, and online peer count
- **No dependencies** — pure Python 3, no pip, no internet required at install time

## Requirements

- muOS 2410.3 AW BANANA or later (tested on RG40XXV)
- Tailscale daemon already installed at `/opt/muos/bin/tailscale` (comes pre-installed on supported muOS builds)
- Python 3 (pre-installed on muOS)

## Installation

1. Copy `Tailscale.sh` to `/mnt/mmc/MUOS/application/`
2. Copy the `Tailscale/` folder to `/mnt/mmc/MUOS/application/`

Your SD card should look like this:
```
/mnt/mmc/MUOS/application/
├── Tailscale.sh
└── Tailscale/
    └── tailscale_gui.py
```

3. Reboot or refresh the Applications list
4. Launch **Tailscale** from the Applications menu

## First-Time Setup

1. Open the app — it will show **Needs Login** if not yet authenticated
2. Press **Connect**
3. When the auth URL appears, press **Y** to show the QR code
4. Scan the QR code with your phone camera
5. Complete login on your phone's browser
6. Press **Connect** again — the device will connect automatically

## Controls

| Button | Action |
|--------|--------|
| D-pad | Navigate |
| A | Confirm / Select |
| B | Back / Cancel |
| Y | Show QR code (on auth screen) |

## Screens

### Main Menu
Shows your current connection state, Tailscale IP, and number of online devices. From here you can connect, disconnect, transfer files, or exit.

### Connect
Initiates `tailscale up`. If authentication is required, displays the login URL and a scannable QR code.

### Disconnect
Choose between:
- **Tailscale Down** — disconnect but keep your login credentials saved
- **Logout** — disconnect and remove all authentication

### File Transfer
- **Receive Files** — pull any pending Taildrop files into your download folder
- **Send a File** — browse your SD card and send a file to a peer
- **Download Location** — change where received files are saved (default: `SD:/MUOS/downloads`)

## Troubleshooting

**App doesn't appear in the menu**
Make sure `Tailscale.sh` is in `/mnt/mmc/MUOS/application/` (not inside the `Tailscale/` subfolder).

**App launches but freezes**
The app requires an active X11 display. Always launch through the muOS Applications menu or via:
```sh
DISPLAY=:0 python3 /mnt/mmc/MUOS/application/Tailscale/tailscale_gui.py
```

**Nothing happens when I click the app**
If the app crashed previously, muOS may be holding the foreground lock. Fix with:
```sh
echo "" > /run/muos/system/foreground_process
```

**QR code doesn't scan**
Make sure the screen brightness is high and your phone camera can focus. Try moving slightly further away. The QR code is generated on-device with no internet connection required.

## File Structure

```
Tailscale.sh              # muOS launcher (goes in /mnt/mmc/MUOS/application/)
Tailscale/
└── tailscale_gui.py      # Main application
```

## Credits

Built with Python 3 and SDL2 via ctypes. QR code generation is pure Python with no external dependencies.

Tested on:
- Anbernic RG40XXV running muOS 2410.3 AW BANANA

## License

MIT License — do whatever you want with it.
