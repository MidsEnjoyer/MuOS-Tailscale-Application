#!/bin/sh
# HELP: Tailscale VPN - Connect and transfer files over your tailnet
# ICON: tailscale
. /opt/muos/script/var/func.sh
echo app >/tmp/act_go
export HOME=$(GET_VAR "device" "board/home")
SET_VAR "system" "foreground_process" "tailscale"
SDL_HQ_SCALER="$(GET_VAR "device" "sdl/scaler")"
export SDL_HQ_SCALER
export SDL_VIDEODRIVER=x11
export DISPLAY=:0
python3 /mnt/mmc/MUOS/application/Tailscale/tailscale_gui.py
unset SDL_HQ_SCALER
