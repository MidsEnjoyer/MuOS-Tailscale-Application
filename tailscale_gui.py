#!/usr/bin/env python3
"""
Tailscale GUI for MustardOS - RG40XXV (640x480)
Uses SDL2 via ctypes - no pip required
"""

import ctypes
import ctypes.util
import subprocess
import os
import sys
import json
import threading
import time

# --- QR Code Generator ------------------------------------------------------

# Pure-Python QR encoder, byte mode, EC level M, versions 1-10
# Built from authoritative spec values (Thonky / ISO 18004)
# Returns a 2D list of bools (True=dark module)

# ── GF(256) ──────────────────────────────────────────────────────────────────
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x; _LOG[_x] = _i
    _x <<= 1
    if _x & 256: _x ^= 0x11D
for _i in range(255, 512): _EXP[_i] = _EXP[_i - 255]

def _mul(a, b):
    return 0 if not a or not b else _EXP[_LOG[a] + _LOG[b]]

def _rs_enc(data, n_ec):
    # Build generator polynomial
    g = [1]
    for i in range(n_ec):
        ng = [0] * (len(g) + 1)
        for j, gj in enumerate(g):
            ng[j]   ^= gj
            ng[j+1] ^= _mul(gj, _EXP[i])
        g = ng
    # Divide
    msg = list(data) + [0] * n_ec
    for i in range(len(data)):
        c = msg[i]
        if c:
            for j in range(len(g)):
                msg[i + j] ^= _mul(g[j], c)
    return msg[len(data):]

# ── Capacity table (EC level M) ───────────────────────────────────────────────
# (max_bytes, ec_per_block, nb1, dc1, nb2, dc2)
# Values from Thonky error correction table, EC level M
_CAP = {
    1:  (14,  10, 1, 16, 0,  0),
    2:  (26,  16, 1, 28, 0,  0),
    3:  (42,  26, 1, 44, 0,  0),
    4:  (62,  18, 2, 32, 0,  0),
    5:  (84,  24, 2, 43, 0,  0),
    6:  (106, 16, 4, 27, 0,  0),
    7:  (122, 18, 4, 31, 0,  0),
    8:  (152, 22, 2, 38, 2, 39),
    9:  (180, 22, 3, 36, 2, 37),
    10: (214, 26, 4, 43, 1, 44),
}

def _pick_version(n):
    for v, cap in _CAP.items():
        if cap[0] >= n:
            return v
    raise ValueError("data too long")

def _make_codewords(data_bytes, version):
    max_dc, ec, nb1, dc1, nb2, dc2 = _CAP[version]
    total_dc = nb1 * dc1 + nb2 * dc2

    # Build data bit stream
    bits = []
    def ab(v, l):
        for i in range(l - 1, -1, -1): bits.append((v >> i) & 1)

    ab(4, 4)                    # byte mode indicator
    ab(len(data_bytes), 8)      # character count
    for b in data_bytes: ab(b, 8)
    cap_bits = total_dc * 8
    for _ in range(min(4, cap_bits - len(bits))): bits.append(0)  # terminator
    while len(bits) % 8: bits.append(0)                           # byte align
    pi = 0
    while len(bits) < cap_bits:                                    # pad codewords
        ab([0xEC, 0x11][pi & 1], 8); pi += 1

    # Split into codewords
    cw = [int(''.join(str(b) for b in bits[i*8:(i+1)*8]), 2)
          for i in range(total_dc)]

    # Split into blocks and compute EC
    pos = 0
    dblks, eblks = [], []
    for _ in range(nb1):
        blk = cw[pos:pos+dc1]; dblks.append(blk)
        eblks.append(_rs_enc(blk, ec)); pos += dc1
    for _ in range(nb2):
        blk = cw[pos:pos+dc2]; dblks.append(blk)
        eblks.append(_rs_enc(blk, ec)); pos += dc2

    # Interleave data then EC
    final = []
    for i in range(max(len(b) for b in dblks)):
        for b in dblks:
            if i < len(b): final.append(b[i])
    for i in range(ec):
        for b in eblks:
            if i < len(b): final.append(b[i])
    return final

# ── Format information (EC level M, masks 0-7) ───────────────────────────────
# Pre-computed from spec: BCH(EC_M | mask_id) XOR 101010000010010
_FMT = {
    0: 0b101010000010010,
    1: 0b101000100100101,
    2: 0b101111001111100,
    3: 0b101101101001011,
    4: 0b100010111111001,
    5: 0b100000011001110,
    6: 0b100111110010111,
    7: 0b100101010100000,
}

# ── Alignment pattern positions (versions 1-10) ───────────────────────────────
_ALIGN = {
    2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
    7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}

# ── Mask functions ─────────────────────────────────────────────────────────────
_MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]

# ── Matrix placement helpers ──────────────────────────────────────────────────
def _place_finder(mat, r, c):
    """Place 7x7 finder + 1-module separator. r,c = top-left of finder."""
    pat = [
        [1,1,1,1,1,1,1],
        [1,0,0,0,0,0,1],
        [1,0,1,1,1,0,1],
        [1,0,1,1,1,0,1],
        [1,0,1,1,1,0,1],
        [1,0,0,0,0,1,1],  # NOTE: intentional typo check below
        [1,1,1,1,1,1,1],
    ]
    # Actually use the correct pattern:
    pat = [
        [1,1,1,1,1,1,1],
        [1,0,0,0,0,0,1],
        [1,0,1,1,1,0,1],
        [1,0,1,1,1,0,1],
        [1,0,1,1,1,0,1],
        [1,0,0,0,0,0,1],
        [1,1,1,1,1,1,1],
    ]
    sz = len(mat)
    # Place finder
    for dr in range(7):
        for dc in range(7):
            nr, nc = r + dr, c + dc
            if 0 <= nr < sz and 0 <= nc < sz:
                mat[nr][nc] = pat[dr][dc]
    # Place separator (border of 0s around finder)
    for dr in range(-1, 8):
        for dc in range(-1, 8):
            if 0 <= dr <= 6 and 0 <= dc <= 6:
                continue  # already placed above
            nr, nc = r + dr, c + dc
            if 0 <= nr < sz and 0 <= nc < sz and mat[nr][nc] is None:
                mat[nr][nc] = 0

def _place_align(mat, cr, cc):
    """Place 5x5 alignment pattern centered at (cr,cc)."""
    pat = [
        [1,1,1,1,1],
        [1,0,0,0,1],
        [1,0,1,0,1],
        [1,0,0,0,1],
        [1,1,1,1,1],
    ]
    for dr in range(-2, 3):
        for dc in range(-2, 3):
            mat[cr + dr][cc + dc] = pat[dr + 2][dc + 2]

def _place_format(mat, sz, mask_id):
    """Place format information in both copies."""
    f = _FMT[mask_id]
    bits = [(f >> (14 - i)) & 1 for i in range(15)]  # bit14=MSB first

    # Copy 1: around top-left finder
    # Row 8, cols 0-5 (bits 0-5), skip col 6 (timing), col 7 (bit 6), col 8 (bit 7)
    fmt_row = [0,1,2,3,4,5,7,8]
    fmt_col = [8,8,8,8,8,8,8,8]
    fmt_row2 = [8,8,8,8,8,8,8,8]
    fmt_col2 = [0,1,2,3,4,5,7,8]

    for i, c in enumerate([0, 1, 2, 3, 4, 5, 7, 8]):
        mat[8][c] = bits[i]
    for i, r in enumerate([7, 5, 4, 3, 2, 1, 0]):
        mat[r][8] = bits[8 + i]

    # Dark module (always 1)
    mat[sz - 8][8] = 1

    # Copy 2: top-right finder (row 8, cols sz-8..sz-1 = bit7 down to bit0)
    for i in range(8):
        mat[8][sz - 8 + i] = bits[7 - i]
    # Copy 2: bottom-left finder (col 8, rows sz-7..sz-1 = bit8 up to bit14)
    for i in range(7):
        mat[sz - 7 + i][8] = bits[8 + i]

def _place_data(mat, codewords, sz, mask_fn):
    """Place data bits in the matrix using the two-column zigzag pattern."""
    bits = []
    for cw in codewords:
        for i in range(7, -1, -1): bits.append((cw >> i) & 1)

    bi = 0
    col = sz - 1
    going_up = True
    while col > 0:
        if col == 6:  # skip vertical timing strip
            col -= 1
        rows = range(sz - 1, -1, -1) if going_up else range(sz)
        for row in rows:
            for dc in (0, 1):
                c = col - dc
                if mat[row][c] is None:
                    bit = bits[bi] if bi < len(bits) else 0
                    bi += 1
                    mat[row][c] = bit ^ (1 if mask_fn(row, c) else 0)
        col -= 2
        going_up = not going_up

def _penalty(mat, sz):
    """Calculate mask penalty score."""
    p = 0
    # Rule 1: runs of 5+ same color in rows/cols
    for line in [mat[r] for r in range(sz)] + [[mat[r][c] for r in range(sz)] for c in range(sz)]:
        run = 1
        for i in range(1, sz):
            if line[i] == line[i-1]:
                run += 1
            else:
                if run >= 5: p += 3 + (run - 5)
                run = 1
        if run >= 5: p += 3 + (run - 5)
    # Rule 2: 2x2 blocks
    for r in range(sz - 1):
        for c in range(sz - 1):
            v = mat[r][c]
            if v == mat[r][c+1] == mat[r+1][c] == mat[r+1][c+1]:
                p += 3
    # Rule 3: finder-like patterns
    pat1 = [1,0,1,1,1,0,1,0,0,0,0]
    pat2 = [0,0,0,0,1,0,1,1,1,0,1]
    for line in [mat[r] for r in range(sz)] + [[mat[r][c] for r in range(sz)] for c in range(sz)]:
        for i in range(sz - 10):
            if list(line[i:i+11]) == pat1 or list(line[i:i+11]) == pat2:
                p += 40
    # Rule 4: dark module ratio
    dark = sum(mat[r][c] for r in range(sz) for c in range(sz))
    total = sz * sz
    pct = dark * 100 // total
    prev5 = (pct // 5) * 5
    next5 = prev5 + 5
    p += min(abs(prev5 - 50) // 5, abs(next5 - 50) // 5) * 10
    return p

def build_qr(text):
    """Encode text as QR code (byte mode, EC level M).
    Returns 2D list of bools: True=dark, False=light."""
    data = text.encode('iso-8859-1')
    version = _pick_version(len(data))
    sz = version * 4 + 17
    codewords = _make_codewords(data, version)

    best_mat = None
    best_score = 10**9

    for mask_id in range(8):
        mat = [[None] * sz for _ in range(sz)]

        # Place finder patterns (top-left, top-right, bottom-left)
        _place_finder(mat, 0, 0)
        _place_finder(mat, 0, sz - 7)
        _place_finder(mat, sz - 7, 0)

        # Place alignment patterns (skip positions overlapping finders)
        ap = _ALIGN.get(version, [])
        last = ap[-1] if ap else None
        for ar in ap:
            for ac in ap:
                # Skip if overlaps any finder pattern area
                if (ar <= 8 and ac <= 8): continue          # TL
                if (ar <= 8 and ac >= sz - 9): continue     # TR
                if (ar >= sz - 9 and ac <= 8): continue     # BL
                _place_align(mat, ar, ac)

        # Place timing patterns (row 6 and col 6)
        for i in range(8, sz - 8):
            if mat[6][i] is None: mat[6][i] = i % 2 == 0
            if mat[i][6] is None: mat[i][6] = i % 2 == 0

        # Dark module
        mat[sz - 8][8] = 1

        # Place format information
        _place_format(mat, sz, mask_id)

        # Place data
        _place_data(mat, codewords, sz, _MASKS[mask_id])

        # Fill any remaining None (shouldn't happen)
        for r in range(sz):
            for c in range(sz):
                if mat[r][c] is None: mat[r][c] = 0

        score = _penalty(mat, sz)
        if score < best_score:
            best_score = score
            best_mat = [row[:] for row in mat]

    return best_mat


# --- SDL2 Setup --------------------------------------------------------------

SDL2 = ctypes.CDLL("libSDL2-2.0.so.0")
SDL2_TTF = None
try:
    SDL2_TTF = ctypes.CDLL("libSDL2_ttf-2.0.so.0")
except:
    pass

# SDL2 constants
SDL_INIT_VIDEO          = 0x00000020
SDL_INIT_JOYSTICK       = 0x00000200
SDL_INIT_GAMECONTROLLER = 0x00002000
SDL_WINDOW_SHOWN        = 0x00000004
SDL_WINDOW_FULLSCREEN   = 0x00000001
SDL_RENDERER_ACCELERATED = 0x00000002
SDL_RENDERER_SOFTWARE    = 0x00000001

SDL_QUIT             = 0x100
SDL_KEYDOWN          = 0x300
SDL_JOYBUTTONDOWN    = 0x603
SDL_JOYAXISMOTION    = 0x600
SDL_JOYHATMOTION     = 0x602
SDL_CONTROLLERAXISMOTION   = 0x650
SDL_CONTROLLERBUTTONDOWN   = 0x652

SDLK_UP    = 1073741906
SDLK_DOWN  = 1073741905
SDLK_LEFT  = 1073741904
SDLK_RIGHT = 1073741903
SDLK_RETURN = 13
SDLK_ESCAPE = 27
SDLK_SPACE  = 32
SDLK_a      = 97
SDLK_b      = 98
SDLK_x      = 120
SDLK_y      = 121

# Joystick button mappings (RG40XXV)
BTN_A = 3   # confirm
BTN_B = 4   # back
BTN_X = 2
BTN_Y     = 5   # new folder / QR
BTN_START = 11  # start/menu button

# D-pad hat values
HAT_UP    = 1
HAT_DOWN  = 4
HAT_LEFT  = 8
HAT_RIGHT = 2

W, H = 640, 480

# Color palette
C_BG        = (20,  20,  32,  255)
C_BG2       = (28,  28,  44,  255)
C_ACCENT    = (255, 185, 0,   255)
C_TEXT      = (220, 220, 230, 255)
C_TEXT_DIM  = (130, 130, 150, 255)
C_BORDER    = (60,  60,  80,  255)
C_GREEN     = (80,  200, 120, 255)
C_RED       = (220, 80,  80,  255)
C_PANEL     = (30,  30,  42,  255)
C_SEL_BG    = (255, 185, 0,   255)
C_SEL_TEXT  = (20,  20,  32,  255)
C_SELECTED  = (255, 185, 0,   60)   # semi-transparent selection highlight
C_ORANGE    = (255, 140, 0,   255)
C_BLACK     = (0,   0,   0,   255)

RECEIVE_DIR = "/mnt/mmc/ROMS/Taildrop"

class SDL_Rect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int),
                ("w", ctypes.c_int), ("h", ctypes.c_int)]

class SDL_Color(ctypes.Structure):
    _fields_ = [("r", ctypes.c_uint8), ("g", ctypes.c_uint8),
                ("b", ctypes.c_uint8), ("a", ctypes.c_uint8)]

class SDL_Event(ctypes.Union):
    class _key(ctypes.Structure):
        class _keysym(ctypes.Structure):
            _fields_ = [("scancode", ctypes.c_int), ("sym", ctypes.c_int),
                        ("mod", ctypes.c_uint16), ("unused", ctypes.c_uint32)]
        _fields_ = [("type", ctypes.c_uint32), ("timestamp", ctypes.c_uint32),
                    ("windowID", ctypes.c_uint32), ("state", ctypes.c_uint8),
                    ("repeat", ctypes.c_uint8), ("padding2", ctypes.c_uint8),
                    ("padding3", ctypes.c_uint8), ("keysym", _keysym)]
    class _jbutton(ctypes.Structure):
        _fields_ = [("type", ctypes.c_uint32), ("timestamp", ctypes.c_uint32),
                    ("which", ctypes.c_int32), ("button", ctypes.c_uint8),
                    ("state", ctypes.c_uint8), ("padding1", ctypes.c_uint8),
                    ("padding2", ctypes.c_uint8)]
    class _jaxis(ctypes.Structure):
        _fields_ = [("type", ctypes.c_uint32), ("timestamp", ctypes.c_uint32),
                    ("which", ctypes.c_int32), ("axis", ctypes.c_uint8),
                    ("padding1", ctypes.c_uint8), ("padding2", ctypes.c_uint8),
                    ("padding3", ctypes.c_uint8), ("value", ctypes.c_int16),
                    ("padding4", ctypes.c_uint16)]
    class _jhat(ctypes.Structure):
        _fields_ = [("type", ctypes.c_uint32), ("timestamp", ctypes.c_uint32),
                    ("which", ctypes.c_int32), ("hat", ctypes.c_uint8),
                    ("value", ctypes.c_uint8), ("padding1", ctypes.c_uint8),
                    ("padding2", ctypes.c_uint8)]
    class _caxis(ctypes.Structure):
        _fields_ = [("type", ctypes.c_uint32), ("timestamp", ctypes.c_uint32),
                    ("which", ctypes.c_int32), ("axis", ctypes.c_uint8),
                    ("padding1", ctypes.c_uint8), ("padding2", ctypes.c_uint8),
                    ("padding3", ctypes.c_uint8), ("value", ctypes.c_int16),
                    ("padding4", ctypes.c_uint16)]
    class _cbutton(ctypes.Structure):
        _fields_ = [("type", ctypes.c_uint32), ("timestamp", ctypes.c_uint32),
                    ("which", ctypes.c_int32), ("button", ctypes.c_uint8),
                    ("state", ctypes.c_uint8), ("padding1", ctypes.c_uint8),
                    ("padding2", ctypes.c_uint8)]
    _fields_ = [("type", ctypes.c_uint32),
                ("key", _key), ("jbutton", _jbutton), ("jaxis", _jaxis),
                ("jhat", _jhat), ("caxis", _caxis), ("cbutton", _cbutton),
                ("padding", ctypes.c_uint8 * 56)]

# --- Config ------------------------------------------------------------------

CONFIG_PATH = "/mnt/mmc/MUOS/application/Tailscale/config.ini"

def load_config():
    cfg = {"receive_dir": RECEIVE_DIR}
    try:
        with open(CONFIG_PATH) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
    except:
        pass
    return cfg

def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            for k, v in cfg.items():
                f.write(f"{k} = {v}\n")
    except:
        pass

# --- Tailscale helpers -------------------------------------------------------

TS     = "/opt/muos/bin/tailscale"
SOCKET = "/run/tailscale/tailscaled.sock"

def ts_run(*args, timeout=5):
    try:
        r = subprocess.run(
            [TS, "--socket=" + SOCKET] + list(args),
            capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return "", str(e)

def _fetch_status_now_worker(app):
    """Blocking status fetch. Call from background thread only."""
    try:
        out, _ = ts_run("status", "--json", timeout=3)
        data = json.loads(out)
        state = data.get("BackendState", "unknown")
        self_node = data.get("Self", {})
        ips = self_node.get("TailscaleIPs", [])
        ip = ips[0] if ips else ""
        peers = []
        for p in data.get("Peer", {}).values():
            peers.append({
                "name": p.get("HostName", p.get("DNSName", "?")).split(".")[0],
                "ip":   (p.get("TailscaleIPs") or [""])[0],
                "online": p.get("Online", False),
                "os":   p.get("OS", ""),
            })
        app.state_cache = state
        app.ip_cache    = ip
        app.peers_cache = peers
    except Exception as e:
        app.state_cache = "unknown"
        app.ip_cache    = ""
        app.peers_cache = []

def ts_get_state():
    out, _ = ts_run("status", "--json", timeout=3)
    try:
        return json.loads(out).get("BackendState", "unknown")
    except:
        return "unknown"

def ts_get_peers():
    out, _ = ts_run("status", "--json", timeout=3)
    try:
        data = json.loads(out)
        peers = []
        for p in data.get("Peer", {}).values():
            peers.append({
                "name":   p.get("HostName", p.get("DNSName", "?")).split(".")[0],
                "ip":     (p.get("TailscaleIPs") or [""])[0],
                "online": p.get("Online", False),
                "os":     p.get("OS", ""),
            })
        return peers
    except:
        return []

def ts_status_json():
    out, _ = ts_run("status", "--json", timeout=3)
    try:
        return json.loads(out)
    except:
        return {}

def ts_get_ip():
    out, _ = ts_run("ip", "--4", timeout=3)
    return out.strip()

def ts_receive_files_to(directory):
    out, err = ts_run("file", "get", directory + "/", timeout=30)
    return out or err

def ts_send_file(target, filepath):
    out, err = ts_run("file", "cp", filepath, target + ":", timeout=30)
    return out or err

def ts_connect():
    import re
    url_re = re.compile(r'https://login\.tailscale\.com/\S+')
    try:
        proc = subprocess.Popen(
            [TS, "--socket=" + SOCKET, "up",
             "--accept-dns=true", "--accept-routes=true"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True
        )
        url     = None
        msg     = ""
        done_ev = threading.Event()

        def _reader():
            nonlocal url, msg
            try:
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    m = url_re.search(line)
                    if m:
                        url = m.group(0)
                        done_ev.set()
                        return
                    msg = line
            finally:
                done_ev.set()

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        done_ev.wait(timeout=20)

        if url:
            return url, msg
        proc.wait(timeout=3)
        state = ts_get_state()
        if state == "Running":
            return None, "Connected successfully."
        return None, msg or "No output from tailscale up."
    except Exception as e:
        return None, str(e)

def ts_down():
    ts_run("down", timeout=5)

def ts_logout():
    try:
        proc = subprocess.Popen(
            [TS, "--socket=" + SOCKET, "logout"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        try:
            proc.communicate(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
            return "logout sent"
        return "logged out"
    except Exception as e:
        return str(e)

def ts_receive(directory=None):
    if directory:
        out, err = ts_run("file", "get", directory + "/", timeout=30)
    else:
        out, err = ts_run("file", "get", RECEIVE_DIR + "/", timeout=30)
    return out, err

def ts_send(filepath, target):
    out, err = ts_run("file", "cp", filepath, target + ":", timeout=30)
    return out, err

# --- Renderer ----------------------------------------------------------------

def find_font():
    candidates = [
        "/mnt/mmc/MUOS/application/.terminal/res/SourceCodePro-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None

class Renderer:
    def __init__(self, renderer, font_small, font_med, font_large):
        self.r  = renderer
        self.fs = font_small
        self.fm = font_med
        self.fl = font_large

    def clear(self, color=None):
        c = color or C_BG
        SDL2.SDL_SetRenderDrawColor(self.r, c[0], c[1], c[2], c[3] if len(c) > 3 else 255)
        SDL2.SDL_RenderClear(self.r)

    def rect(self, x, y, w, h, color, fill=True):
        SDL2.SDL_SetRenderDrawColor(self.r, color[0], color[1], color[2], color[3] if len(color) > 3 else 255)
        rct = SDL_Rect(int(x), int(y), int(w), int(h))
        if fill:
            SDL2.SDL_RenderFillRect(self.r, ctypes.byref(rct))
        else:
            SDL2.SDL_RenderDrawRect(self.r, ctypes.byref(rct))

    def line(self, x1, y1, x2, y2, color):
        SDL2.SDL_SetRenderDrawColor(self.r, color[0], color[1], color[2], color[3] if len(color) > 3 else 255)
        SDL2.SDL_RenderDrawLine(self.r, int(x1), int(y1), int(x2), int(y2))

    def text(self, font, txt, x, y, color, center=False, right=False):
        if not font or not SDL2_TTF:
            return
        if not txt:
            return
        try:
            txt_bytes = str(txt).encode("ascii", errors="replace")
        except:
            txt_bytes = b"?"
        col = SDL_Color(color[0], color[1], color[2], 255)
        surf = SDL2_TTF.TTF_RenderUTF8_Blended(font, txt_bytes, col)
        if not surf:
            return
        tex = SDL2.SDL_CreateTextureFromSurface(self.r, surf)
        SDL2.SDL_FreeSurface(surf)
        if not tex:
            return
        w_out = ctypes.c_int(0)
        h_out = ctypes.c_int(0)
        SDL2.SDL_QueryTexture(tex, None, None, ctypes.byref(w_out), ctypes.byref(h_out))
        tw, th = w_out.value, h_out.value
        if center:
            rx = x - tw // 2
        elif right:
            rx = x - tw
        else:
            rx = x
        dst = SDL_Rect(int(rx), int(y - th // 2) if center else int(y), tw, th)
        SDL2.SDL_RenderCopy(self.r, tex, None, ctypes.byref(dst))
        SDL2.SDL_DestroyTexture(tex)

    def present(self):
        SDL2.SDL_RenderPresent(self.r)

    def header(self, title, state=None):
        self.rect(0, 0, W, 48, C_BG2)
        self.text(self.fl, "TAILSCALE", 16, 8, C_ACCENT)
        if title:
            self.line(160, 8, 160, 38, C_BORDER)
            self.text(self.fm, title, 172, 14, C_TEXT)
        self.line(0, 46, W, 46, C_ACCENT)
        if state is not None:
            col = C_GREEN if state == "Running" else C_RED if state in ("Stopped","NeedsLogin") else C_TEXT_DIM
            label = state.upper() if state else "UNKNOWN"
            self.rect(W - 120, 14, 8, 8, col)
            self.text(self.fs, label, W - 108, 14, col)

    def footer(self, hints):
        self.rect(0, H - 34, W, 34, C_BG2)
        self.line(0, H - 34, W, H - 34, C_BORDER)
        x = 8
        for btn, lbl in hints:
            # Badge width based on text length (min 18px)
            badge_w = max(18, len(btn) * 7 + 6)
            self.rect(x, H - 26, badge_w, 18, C_ACCENT)
            self.text(self.fs, btn, x + badge_w // 2, H - 17, C_SEL_TEXT, center=True)
            x += badge_w + 4
            self.text(self.fs, lbl, x, H - 17, C_TEXT_DIM)
            x += len(lbl) * 7 + 12

    def panel(self, x, y, w, h, border_col, bg_col):
        self.rect(x, y, w, h, bg_col)
        self.rect(x, y, w, h, border_col, fill=False)

    def set_color(self, color):
        SDL2.SDL_SetRenderDrawColor(self.r, color[0], color[1], color[2],
                                    color[3] if len(color) > 3 else 255)

    def button(self, x, y, w, h, label, selected, font=None):
        f = font or self.fm
        if selected:
            self.rect(x, y, w, h, C_ACCENT)
            # Draw text twice for contrast: shadow then main
            self.text(f, label, x + w // 2 + 1, y + h // 2 + 1, (0, 0, 0, 180), center=True)
            self.text(f, label, x + w // 2, y + h // 2, C_SEL_TEXT, center=True)
        else:
            self.rect(x, y, w, h, C_PANEL)
            self.rect(x, y, w, h, C_BORDER, fill=False)
            self.text(f, label, x + w // 2, y + h // 2, C_TEXT, center=True)


# --- Screens -----------------------------------------------------------------

class App:
    def __init__(self):
        self.screen = "main"
        self.sel = 0
        self.running = True
        self.state_cache = "unknown"
        self.ip_cache = ""
        self.peers_cache = []
        self.status_msg = ""
        self.last_refresh = 0
        self.send_sel = 0
        self.browse_path = "/mnt/mmc/ROMS"
        self.browse_entries = []
        self.browse_sel = 0
        self.send_target = ""
        self.result_msg = ""
        self.axis_timer = {}
        _cfg = load_config()
        self.receive_dir = _cfg.get("receive_dir", RECEIVE_DIR)
        self.browse_mode = "send"  # "send" or "pick_dir"
        self.loading_msg = ""
        self.loading_start = 0
        self.exit_pending = False
        self.osk_text = ""
        self.osk_col = 0
        self.osk_row = 0
        self.osk_callback = None  # function(text) called on confirm
        self.osk_title = "Enter name"
        self.login_url = None      # set when auth URL is available
        self.qr_matrix = None      # pre-computed QR matrix
        self.qr_error = None

        # Fetch initial status in background
        self._refresh_status()

    def _fetch_status_now(self):
        """Blocking status fetch - call from background threads only.
        Uses a single JSON call with short timeout to avoid hanging."""
        try:
            j = ts_status_json()  # single call, timeout=3
            if j:
                backend = j.get("BackendState", "unknown")
                self.state_cache = {
                    "Running": "Running", "NeedsLogin": "NeedsLogin",
                    "Stopped": "Stopped", "NoState": "Stopped",
                    "NeedsMachineAuth": "NeedsLogin",
                }.get(backend, backend)
                self.ip_cache = ""
                me = j.get("Self", {})
                ips = me.get("TailscaleIPs", [])
                if ips: self.ip_cache = ips[0]
                self.peers_cache = []
                for v in j.get("Peer", {}).values():
                    dns = v.get("DNSName", "")
                    name = dns.split(".")[0] if dns else v.get("HostName", "?")
                    ips = v.get("TailscaleIPs", [])
                    self.peers_cache.append({
                        "name": name,
                        "online": bool(v.get("Online", False)),
                        "ip": ips[0] if ips else "",
                        "os": v.get("OS", ""),
                    })
            else:
                self.state_cache = "unknown"
                self.ip_cache = ""
                self.peers_cache = []
        except Exception:
            self.state_cache = "unknown"
            self.ip_cache = ""
            self.peers_cache = []
        self.last_refresh = time.time()

    def _refresh_status(self):
        """Non-blocking status refresh - spawns background thread."""
        t = threading.Thread(target=self._fetch_status_now, daemon=True)
        t.start()

    def handle_input(self, action):
        if self.screen == "main":
            self._handle_main(action)
        elif self.screen == "status":
            self._handle_status(action)
        elif self.screen == "connect":
            self._handle_connect(action)
        elif self.screen == "disconnect":
            self._handle_disconnect(action)
        elif self.screen == "filetransfer":
            self._handle_filetransfer(action)
        elif self.screen == "send_pick_device":
            self._handle_send_device(action)
        elif self.screen == "browse":
            self._handle_browse(action)
        elif self.screen == "osk":
            self._handle_osk(action)
        elif self.screen == "result":
            self._handle_result(action)
        elif self.screen == "qr":
            self._handle_qr(action)
        elif self.screen == "loading":
            pass  # no input during loading

    def _handle_main(self, action):
        items = 5
        if action == "UP":
            self.sel = (self.sel - 1) % items
        elif action == "DOWN":
            self.sel = (self.sel + 1) % items
        elif action == "CONFIRM":
            if self.sel == 0:
                self._refresh_status()
                self.screen = "status"
                self.sel = 0
            elif self.sel == 1:
                self.screen = "connect"
                self.sel = 0
                self.status_msg = ""
            elif self.sel == 2:
                self.screen = "disconnect"
                self.sel = 0
            elif self.sel == 3:
                self.screen = "filetransfer"
                self.sel = 0
            elif self.sel == 4:
                self._start_exit()
        elif action == "BACK":
            self._start_exit()

    def _start_exit(self):
        self.loading_msg = "Exiting"
        self.loading_start = time.time()
        self.screen = "loading"
        self.exit_pending = True

    def _handle_status(self, action):
        if action in ("BACK", "CONFIRM", "B"):
            self.screen = "main"
            self.sel = 0
        elif action == "UP":
            self.loading_msg = "Refreshing status"
            self.loading_start = time.time()
            self.screen = "loading"
            def _refresh_then_return():
                self.state_cache = ts_get_state()
                self.ip_cache = ts_get_ip()
                self.peers_cache = ts_get_peers()
                self.screen = "status"
            threading.Thread(target=_refresh_then_return, daemon=True).start()

    def _handle_connect(self, action):
        items = 2
        if action == "UP":
            self.sel = (self.sel - 1) % items
        elif action == "DOWN":
            self.sel = (self.sel + 1) % items
        elif action == "CONFIRM":
            if self.sel == 0:
                # Connect
                self.loading_msg = "Connecting to Tailscale"
                self.loading_start = time.time()
                self.screen = "loading"
                def _connect():
                    url, msg = ts_connect()
                    if url:
                        self.login_url = url
                        short = url.replace("https://login.tailscale.com/", "")
                        self.result_msg = (
                            "Authentication required\n\n"
                            "Visit on another device:\n"
                            "https://login.tailscale.com/" + short +
                            "\n\nPress Y for QR code\n"
                            "or log in manually, then\npress Connect again."
                        )
                    else:
                        self._refresh_status()
                        state = ts_get_state()
                        if state == "Running":
                            self.result_msg = "Connected!\nTailscale IP: " + ts_get_ip()
                        else:
                            self.result_msg = msg or "Done."
                    self.screen = "result"
                    self.sel = 0
                threading.Thread(target=_connect, daemon=True).start()
            elif self.sel == 1:
                self.screen = "main"
                self.sel = 1
        elif action == "BACK":
            self.screen = "main"
            self.sel = 1

    def _handle_disconnect(self, action):
        items = 3
        if action == "UP":
            self.sel = (self.sel - 1) % items
        elif action == "DOWN":
            self.sel = (self.sel + 1) % items
        elif action == "CONFIRM":
            if self.sel == 0:
                self.loading_msg = "Disconnecting..."
                self.screen = "loading"
                def _do_down():
                    msg = ts_down() or ""
                    # Wait for state to actually change
                    import time
                    for _ in range(10):
                        time.sleep(0.5)
                        state = ts_get_state()
                        if state != "Running":
                            break
                    state = ts_get_state()
                    if state == "Running":
                        self.result_msg = "Disconnect may have failed.\nState: " + state + "\n\n" + msg
                    else:
                        self.result_msg = "Tailscale disconnected.\nAuthentication preserved."
                    self._fetch_status_now()  # blocking - we're already off main thread
                    self.screen = "result"
                    self.sel = 0
                threading.Thread(target=_do_down, daemon=True).start()
            elif self.sel == 1:
                self.loading_msg = "Logging out..."
                self.screen = "loading"
                def _do_logout():
                    ts_logout()
                    # After logout, state goes to NeedsLogin or similar.
                    # Don't poll - just fetch once and move on.
                    import time
                    time.sleep(1)  # brief pause for daemon to update state
                    self._fetch_status_now()
                    self.result_msg = "Logged out.\nYou will need to authenticate again."
                    self.screen = "result"
                    self.sel = 0
                threading.Thread(target=_do_logout, daemon=True).start()
            elif self.sel == 2:
                self.screen = "main"
                self.sel = 2
        elif action == "BACK":
            self.screen = "main"
            self.sel = 2

    def _handle_filetransfer(self, action):
        items = 4
        if action == "UP":
            self.sel = (self.sel - 1) % items
        elif action == "DOWN":
            self.sel = (self.sel + 1) % items
        elif action == "CONFIRM":
            if self.sel == 0:
                # Receive
                self.loading_msg = "Checking for incoming files..."
                self.loading_start = time.time()
                self.screen = "loading"
                def _recv():
                    msg = ts_receive_files_to(self.receive_dir)
                    self.result_msg = "Receive complete:\n\n" + msg + "\n\nFiles saved to:\n" + self.receive_dir
                    self.screen = "result"
                threading.Thread(target=_recv, daemon=True).start()
            elif self.sel == 1:
                # Send - pick device
                online = [p for p in self.peers_cache if p["online"]]
                if not online:
                    self.result_msg = "No devices online on tailnet.\nMake sure other devices are connected."
                    self.screen = "result"
                else:
                    self.screen = "send_pick_device"
                    self.send_sel = 0
            elif self.sel == 2:
                # Change download location
                self.browse_mode = "pick_dir"
                self._load_browse("/mnt/mmc", dirs_only=True)
                self.browse_sel = 0
                self.screen = "browse"
            elif self.sel == 3:
                self.screen = "main"
                self.sel = 3
        elif action == "BACK":
            self.screen = "main"
            self.sel = 3

    def _handle_send_device(self, action):
        online = [p for p in self.peers_cache if p["online"]]
        items = len(online) + 1
        if action == "UP":
            self.send_sel = (self.send_sel - 1) % items
        elif action == "DOWN":
            self.send_sel = (self.send_sel + 1) % items
        elif action == "CONFIRM":
            if self.send_sel < len(online):
                self.send_target = online[self.send_sel]["name"]
                self._load_browse(self.browse_path)
                self.screen = "browse"
                self.browse_sel = 0
            else:
                self.screen = "filetransfer"
                self.sel = 1
        elif action == "BACK":
            self.screen = "filetransfer"
            self.sel = 1

    def _load_browse(self, path, dirs_only=False):
        self.browse_path = path
        self.browse_dirs_only = dirs_only
        entries = []
        try:
            if path != "/mnt/mmc":
                entries.append((".. (back)", "dir", ".."))
            items = sorted(os.listdir(path))
            for item in items:
                full = os.path.join(path, item)
                if os.path.isdir(full):
                    entries.append((item + "/", "dir", full))
                elif not dirs_only:
                    try:
                        size = os.path.getsize(full)
                        size_str = self._fmt_size(size)
                    except:
                        size_str = "?"
                    entries.append((item + "  " + size_str, "file", full))
        except Exception as e:
            entries.append(("Error: " + str(e), "err", ""))
        self.browse_entries = entries

    def _fmt_size(self, b):
        if b < 1024: return str(b) + "B"
        if b < 1024*1024: return str(b//1024) + "KB"
        return str(b//(1024*1024)) + "MB"

    def _handle_browse(self, action):
        items = len(self.browse_entries)
        if items == 0:
            if action == "BACK":
                if self.browse_mode == "pick_dir":
                    self.screen = "filetransfer"
                else:
                    self.screen = "send_pick_device"
            return
        if action == "UP":
            self.browse_sel = (self.browse_sel - 1) % items
        elif action == "DOWN":
            self.browse_sel = (self.browse_sel + 1) % items
        elif action == "CONFIRM":
            name, kind, full_path = self.browse_entries[self.browse_sel]
            if kind == "dir":
                if full_path == "..":
                    parent = os.path.dirname(self.browse_path)
                    self._load_browse(parent, dirs_only=self.browse_dirs_only)
                else:
                    self._load_browse(full_path, dirs_only=self.browse_dirs_only)
                self.browse_sel = 0
            elif kind == "file" and self.browse_mode == "send":
                target = self.send_target
                fp = full_path
                self.loading_msg = "Sending " + os.path.basename(fp) + " to " + target + "..."
                self.loading_start = time.time()
                self.screen = "loading"
                def _send():
                    msg = ts_send_file(target, fp)
                    self.result_msg = "Sent to " + target + ":\n" + os.path.basename(fp) + "\n\n" + msg
                    self.screen = "result"
                    self.sel = 0
                threading.Thread(target=_send, daemon=True).start()
        elif action == "LEFT" and self.browse_mode == "pick_dir":
            # Select current directory as download location
            os.makedirs(self.browse_path, exist_ok=True)
            self.receive_dir = self.browse_path
            save_config({"receive_dir": self.browse_path})
            self.result_msg = "Download location saved:\n" + self.browse_path
            self.screen = "result"
            self.sel = 0
        elif action == "NEW_FOLDER" and self.browse_mode == "pick_dir":
            self.osk_row = 0
            self.osk_col = 0
            self.osk_title = "New Folder Name"
            current_path = self.browse_path
            def _mkdir(name):
                if name:
                    full = os.path.join(current_path, name)
                    try:
                        os.makedirs(full, exist_ok=True)
                        self._load_browse(current_path, dirs_only=True)
                        # auto-select the new folder
                        for i, (n, k, p) in enumerate(self.browse_entries):
                            if p == full:
                                self.browse_sel = i
                                break
                    except Exception as e:
                        self.result_msg = "Error: " + str(e)
                        self.screen = "result"
                        return
                self.screen = "browse"
            self.osk_callback = _mkdir
            self.screen = "osk"
        elif action == "BACK":
            if self.browse_mode == "pick_dir":
                self.screen = "filetransfer"
                self.sel = 2
            else:
                self.screen = "send_pick_device"
                self.send_sel = 0


    # OSK layout
    OSK_ROWS = [
        list("abcdefghij"),
        list("klmnopqrst"),
        list("uvwxyz0123"),
        list("456789-_ ."),
        ["BACK", "SPACE", "OK"],
    ]

    def _handle_osk(self, action):
        rows = self.OSK_ROWS
        row = self.osk_row
        col = self.osk_col
        row_len = len(rows[row])

        if action == "UP":
            self.osk_row = (row - 1) % len(rows)
            self.osk_col = min(self.osk_col, len(rows[self.osk_row]) - 1)
        elif action == "DOWN":
            self.osk_row = (row + 1) % len(rows)
            self.osk_col = min(self.osk_col, len(rows[self.osk_row]) - 1)
        elif action == "LEFT":
            self.osk_col = (col - 1) % row_len
        elif action == "RIGHT":
            self.osk_col = (col + 1) % row_len
        elif action == "CONFIRM":
            key = rows[row][col]
            if key == "OK":
                cb = self.osk_callback
                text = self.osk_text
                self.osk_callback = None
                if cb:
                    cb(text)
            elif key == "BACK":
                self.osk_text = self.osk_text[:-1]
            elif key == "SPACE":
                self.osk_text += " "
            else:
                if len(self.osk_text) < 40:
                    self.osk_text += key
        elif action == "BACK":
            # Cancel - go back to browse
            self.screen = "browse"

    def _draw_osk(self, rnd):
        rnd.header(self.osk_title, self.state_cache)

        # Text input display
        rnd.panel(16, 54, W - 32, 44, C_ACCENT, C_BG2)
        display = self.osk_text + "_"
        rnd.text(rnd.fm, display, 28, 66, C_ACCENT)

        # Keyboard grid
        rows = self.OSK_ROWS
        start_y = 114
        for r, row in enumerate(rows):
            is_action_row = r == len(rows) - 1
            if is_action_row:
                # Action row: BACK, SPACE, OK - wider buttons
                widths = [80, 200, 80]
                labels = row
                x = (W - sum(widths) - 2 * (len(widths)-1) * 4) // 2
                for c, (label, w) in enumerate(zip(labels, widths)):
                    sel = (r == self.osk_row and c == self.osk_col)
                    rnd.button(x, start_y + r * 52, w, 40, label, sel, rnd.fs)
                    x += w + 8
            else:
                # Character row
                cell_w = (W - 32) // 10
                for c, ch in enumerate(row):
                    sel = (r == self.osk_row and c == self.osk_col)
                    cx = 16 + c * cell_w
                    cy = start_y + r * 52
                    if sel:
                        rnd.rect(cx, cy, cell_w - 2, 40, C_ACCENT)
                        rnd.text(rnd.fm, ch.upper(), cx + (cell_w-2)//2, cy + 10, C_BLACK, center=True)
                    else:
                        rnd.rect(cx, cy, cell_w - 2, 40, C_PANEL)
                        rnd.rect(cx, cy, cell_w - 2, 40, C_BORDER, filled=False)
                        rnd.text(rnd.fm, ch.upper(), cx + (cell_w-2)//2, cy + 10, C_TEXT, center=True)

        rnd.footer([("D-pad", "Navigate"), ("A", "Type"), ("B", "Cancel")])


    def _handle_qr(self, action):
        if action in ("CONFIRM", "BACK", "NEW_FOLDER"):
            self.screen = "result"

    def _draw_qr(self, rnd):
        rnd.header("SCAN TO LOGIN", self.state_cache)

        if not self.qr_matrix:
            rnd.text(rnd.fm, "QR generation failed", W//2, H//2 - 10, C_RED, center=True)
            if self.qr_error:
                rnd.text(rnd.fs, str(self.qr_error)[:50], W//2, H//2 + 20, C_TEXT_DIM, center=True)
            rnd.footer([("B", "Back")])
            return

        mat = self.qr_matrix
        qr_size = len(mat)
        quiet = 2  # minimum quiet zone to maximize module size on small screen

        # Use as much of the screen as possible - leave only header and footer
        max_w = W - 20       # 10px margin each side
        max_h = H - 90       # header ~40px + footer ~50px
        available = min(max_w, max_h)
        module_px = available // (qr_size + quiet * 2)
        module_px = max(4, module_px)  # minimum 4px per module for scannability
        total_px = (qr_size + quiet * 2) * module_px

        ox = (W - total_px) // 2
        oy = 40 + (max_h - total_px) // 2

        # Solid white background (includes quiet zone)
        rnd.rect(ox, oy, total_px, total_px, (255, 255, 255))

        # Draw dark modules
        for r in range(qr_size):
            for c in range(qr_size):
                if mat[r][c]:
                    px = ox + (quiet + c) * module_px
                    py = oy + (quiet + r) * module_px
                    rnd.rect(px, py, module_px, module_px, (0, 0, 0))

        rnd.footer([("B", "Back")])

    def _handle_result(self, action):
        if action == "NEW_FOLDER" and self.login_url:  # Y button
            try:
                self.qr_matrix = build_qr(self.login_url)
                self.qr_error = None
            except Exception as e:
                self.qr_matrix = None
                self.qr_error = str(e)
            self.screen = "qr"
        elif action in ("CONFIRM", "BACK"):
            self.login_url = None
            self.screen = "main"
            self.sel = 0

    # --- Drawing -------------------------------------------------------------

    def draw(self, rnd):
        rnd.clear()
        if self.screen == "main":
            self._draw_main(rnd)
        elif self.screen == "status":
            self._draw_status(rnd)
        elif self.screen == "connect":
            self._draw_connect(rnd)
        elif self.screen == "disconnect":
            self._draw_disconnect(rnd)
        elif self.screen == "filetransfer":
            self._draw_filetransfer(rnd)
        elif self.screen == "send_pick_device":
            self._draw_send_device(rnd)
        elif self.screen == "browse":
            self._draw_browse(rnd)
        elif self.screen == "osk":
            self._draw_osk(rnd)
        elif self.screen == "result":
            self._draw_result(rnd)
        elif self.screen == "qr":
            self._draw_qr(rnd)
        elif self.screen == "loading":
            self._draw_loading(rnd)
        rnd.present()

    def _draw_main(self, rnd):
        state = self.state_cache
        rnd.header("", state)

        # Big status area
        sy = 60
        rnd.panel(16, sy, W - 32, 90, C_BORDER, C_BG2)

        # Status indicator
        if state == "Running":
            rnd.text(rnd.fl, "* CONNECTED", 36, sy + 12, C_GREEN)
            rnd.text(rnd.fm, self.ip_cache, 36, sy + 48, C_TEXT_DIM)
        elif state == "NeedsLogin":
            rnd.text(rnd.fl, "! NEEDS LOGIN", 36, sy + 12, C_ORANGE)
            rnd.text(rnd.fm, "Select Connect to authenticate", 36, sy + 48, C_TEXT_DIM)
        elif state == "Stopped":
            rnd.text(rnd.fl, "o DISCONNECTED", 36, sy + 12, C_RED)
            rnd.text(rnd.fm, "Select Connect to start", 36, sy + 48, C_TEXT_DIM)
        else:
            rnd.text(rnd.fl, "o " + state.upper(), 36, sy + 12, C_TEXT_DIM)

        # Peer count
        online = sum(1 for p in self.peers_cache if p["online"])
        total = len(self.peers_cache)
        rnd.text(rnd.fs, str(online) + "/" + str(total) + " devices online", W - 32, sy + 16, C_TEXT_DIM, right=True)

        # Menu items
        menu = [
            ("STATUS & PEERS",        "View connection details"),
            ("CONNECT",               "Bring Tailscale up"),
            ("DISCONNECT",            "Bring Tailscale down"),
            ("FILE TRANSFER",         "Send / receive via Taildrop"),
            ("EXIT",                  ""),
        ]
        my = 168
        mh = 46
        mx = 16
        mw = W - 32

        for i, (label, sub) in enumerate(menu):
            selected = (i == self.sel)
            if selected:
                rnd.rect(mx, my + i * mh, mw, mh - 4, C_ACCENT)
                rnd.rect(mx, my + i * mh, 4, mh - 4, C_BG)
                rnd.text(rnd.fm, label, mx + 20, my + i * mh + 6, C_BG)
                if sub:
                    rnd.text(rnd.fs, sub, mx + 20, my + i * mh + 26, (60, 50, 10, 255))
            else:
                rnd.rect(mx, my + i * mh, mw, mh - 4, C_BG2)
                rnd.text(rnd.fm, label, mx + 20, my + i * mh + 6, C_TEXT)
                if sub:
                    rnd.text(rnd.fs, sub, mx + 20, my + i * mh + 26, C_TEXT_DIM)
            rnd.line(mx, my + i * mh + mh - 4, mx + mw, my + i * mh + mh - 4, C_BORDER)

        rnd.footer([("D-pad", "Navigate"), ("A", "Select")])

    def _draw_status(self, rnd):
        state = self.state_cache
        rnd.header("STATUS", state)

        y = 60
        rnd.panel(16, y, W - 32, 100, C_BORDER, C_BG2)
        rnd.text(rnd.fm, "State:", 32, y + 10, C_TEXT_DIM)
        color = {"Running": C_GREEN, "NeedsLogin": C_ORANGE}.get(state, C_RED)
        rnd.text(rnd.fm, state, 110, y + 10, color)
        rnd.text(rnd.fm, "IP:", 32, y + 36, C_TEXT_DIM)
        rnd.text(rnd.fm, self.ip_cache or "N/A", 110, y + 36, C_TEXT)
        rnd.text(rnd.fm, "Peers:", 32, y + 62, C_TEXT_DIM)
        online = sum(1 for p in self.peers_cache if p["online"])
        rnd.text(rnd.fm, str(online) + " online / " + str(len(self.peers_cache)) + " total", 110, y + 62, C_TEXT)

        # Peer list
        py = y + 116
        rnd.text(rnd.fm, "DEVICES ON TAILNET", 16, py - 24, C_ACCENT)
        rnd.line(16, py - 4, W - 16, py - 4, C_BORDER)

        for i, peer in enumerate(self.peers_cache[:8]):
            row_y = py + i * 30
            dot_color = C_GREEN if peer["online"] else C_TEXT_DIM
            for dx in range(-4, 5):
                for dy in range(-4, 5):
                    if dx*dx + dy*dy <= 16:
                        rnd.set_color(dot_color)
                        r = SDL_Rect(28 + dx, row_y + 10 + dy, 1, 1)
                        SDL2.SDL_RenderFillRect(rnd.r, ctypes.byref(r))
            rnd.text(rnd.fm, peer["name"], 44, row_y, C_TEXT if peer["online"] else C_TEXT_DIM)
            rnd.text(rnd.fs, peer["ip"], 44, row_y + 16, C_TEXT_DIM)
            rnd.text(rnd.fs, peer["os"], W - 20, row_y + 6, C_TEXT_DIM, right=True)

        rnd.footer([("B", "Back"), ("^", "Refresh")])

    def _draw_connect(self, rnd):
        state = self.state_cache
        rnd.header("CONNECT", state)

        rnd.panel(16, 70, W - 32, 80, C_BORDER, C_BG2)
        if state == "Running":
            rnd.text(rnd.fm, "Already connected", 32, 86, C_GREEN)
            rnd.text(rnd.fs, "IP: " + self.ip_cache, 32, 112, C_TEXT_DIM)
        elif state == "NeedsLogin":
            rnd.text(rnd.fm, "Authentication required", 32, 86, C_ORANGE)
            rnd.text(rnd.fs, "A login URL will be shown", 32, 112, C_TEXT_DIM)
        else:
            rnd.text(rnd.fm, "Ready to connect", 32, 86, C_TEXT)
            rnd.text(rnd.fs, "Tailscale will start and connect", 32, 112, C_TEXT_DIM)

        if self.status_msg:
            rnd.text(rnd.fm, self.status_msg, W // 2, 180, C_ACCENT, center=True)

        btns = [("CONNECT TO TAILNET", "Start connection"), ("CANCEL", "")]
        by = 220
        for i, (label, sub) in enumerate(btns):
            sel = (i == self.sel)
            rnd.button(40, by + i * 60, W - 80, 46, label, sel, rnd.fm)
            if sub and not sel:
                rnd.text(rnd.fs, sub, W // 2, by + i * 60 + 48, C_TEXT_DIM, center=True)

        rnd.footer([("A", "Confirm"), ("B", "Back")])

    def _draw_disconnect(self, rnd):
        state = self.state_cache
        rnd.header("DISCONNECT", state)

        rnd.panel(16, 70, W - 32, 60, C_BORDER, C_BG2)
        rnd.text(rnd.fm, "Current state: " + state, 32, 86, C_TEXT)
        rnd.text(rnd.fs, "Choose disconnect method below", 32, 110, C_TEXT_DIM)

        opts = [
            ("TAILSCALE DOWN", "Disconnect but keep login"),
            ("LOGOUT", "Disconnect and remove auth"),
            ("CANCEL", ""),
        ]
        by = 150
        bh = 66
        for i, (label, sub) in enumerate(opts):
            sel = (i == self.sel)
            bx = 40; by2 = by + i * (bh + 6)
            bw = W - 80
            bg = C_ACCENT if sel else C_PANEL
            fg = C_BG   if sel else C_TEXT
            fg2 = (60, 50, 10, 255) if sel else C_TEXT_DIM
            rnd.rect(bx, by2, bw, bh, bg)
            rnd.rect(bx, by2, bw, bh, C_BORDER, fill=False)
            if sub:
                block_h = 42
                ty = by2 + (bh - block_h) // 2
                rnd.text(rnd.fm, label, bx + bw // 2, ty,      fg,  center=True)
                rnd.text(rnd.fs, sub,   bx + bw // 2, ty + 26, fg2, center=True)
            else:
                rnd.text(rnd.fm, label, bx + bw // 2, by2 + bh // 2 - 9, fg, center=True)

        rnd.footer([("A", "Confirm"), ("B", "Back")])

    def _draw_filetransfer(self, rnd):
        state = self.state_cache
        rnd.header("FILE TRANSFER", state)

        # Show current receive dir (truncated if long)
        recv_short = self.receive_dir.replace("/mnt/mmc/", "SD:/")
        rnd.panel(16, 70, W - 32, 46, C_BORDER, C_BG2)
        rnd.text(rnd.fm, "Taildrop", 32, 78, C_ACCENT)
        rnd.text(rnd.fs, "Save to: " + recv_short, 32, 100, C_TEXT_DIM)

        opts = [
            ("RECEIVE FILES", "Get pending incoming files"),
            ("SEND A FILE", "Browse and send to a device"),
            ("DOWNLOAD LOCATION", "Change where files are saved"),
            ("CANCEL", ""),
        ]
        by = 128
        bh = 62
        for i, (label, sub) in enumerate(opts):
            sel = (i == self.sel)
            bx = 40; by2 = by + i * (bh + 4)
            bw = W - 80
            bg = C_ACCENT if sel else C_PANEL
            fg = C_BG   if sel else C_TEXT
            fg2 = (60, 50, 10, 255) if sel else C_TEXT_DIM
            rnd.rect(bx, by2, bw, bh, bg)
            rnd.rect(bx, by2, bw, bh, C_BORDER, fill=False)
            if sub:
                block_h = 42
                ty = by2 + (bh - block_h) // 2
                rnd.text(rnd.fm, label, bx + bw // 2, ty,      fg,  center=True)
                rnd.text(rnd.fs, sub,   bx + bw // 2, ty + 26, fg2, center=True)
            else:
                rnd.text(rnd.fm, label, bx + bw // 2, by2 + bh // 2 - 9, fg, center=True)

        rnd.footer([("A", "Select"), ("B", "Back")])

    def _draw_send_device(self, rnd):
        state = self.state_cache
        rnd.header("SEND FILE", state)

        online = [p for p in self.peers_cache if p["online"]]
        rnd.text(rnd.fm, "SELECT TARGET DEVICE", 16, 58, C_ACCENT)
        rnd.line(16, 78, W - 16, 78, C_BORDER)

        if not online:
            rnd.text(rnd.fm, "No devices online", W // 2, 200, C_TEXT_DIM, center=True)
        else:
            for i, peer in enumerate(online[:7]):
                sel = (i == self.send_sel)
                row_y = 90 + i * 44
                if sel:
                    rnd.rect(16, row_y - 4, W - 32, 40, C_SELECTED)
                    rnd.rect(16, row_y - 4, 4, 40, C_ACCENT)
                rnd.text(rnd.fm, peer["name"], 36, row_y, C_ACCENT if sel else C_TEXT)
                rnd.text(rnd.fs, peer["ip"] + "  " + peer["os"], 36, row_y + 20, C_TEXT_DIM)
                rnd.line(16, row_y + 38, W - 16, row_y + 38, C_BORDER)

        # Cancel option
        cancel_y = 90 + len(online) * 44
        sel = (self.send_sel == len(online))
        rnd.button(40, cancel_y + 10, W - 80, 40, "CANCEL", sel, rnd.fm)

        rnd.footer([("A", "Select"), ("B", "Back")])

    def _draw_browse(self, rnd):
        state = self.state_cache
        rnd.header("BROWSE FILES", state)
        rnd.text(rnd.fs, "> " + self.send_target, W - 16, 52, C_ACCENT, right=True)

        path_short = self.browse_path.replace("/mnt/mmc/ROMS", "SD:/ROMS")
        rnd.text(rnd.fs, path_short, 16, 52, C_TEXT_DIM)
        rnd.line(16, 68, W - 16, 68, C_BORDER)

        visible = 10
        start = max(0, self.browse_sel - visible + 3)
        for i, (name, kind, full_path) in enumerate(self.browse_entries[start:start+visible]):
            real_i = start + i
            sel = (real_i == self.browse_sel)
            row_y = 76 + i * 36
            if sel:
                rnd.rect(16, row_y, W - 32, 32, C_SELECTED)
                rnd.rect(16, row_y, 4, 32, C_ACCENT)
            icon = "[D] " if kind == "dir" else "[F] "
            color = C_ACCENT if (sel and kind == "dir") else (C_GREEN if (sel and kind == "file") else (C_TEXT_DIM if kind == "dir" else C_TEXT))
            # No emoji support in SDL2_ttf without special font, use text prefix
            prefix = "[D] " if kind == "dir" else "[F] "
            rnd.text(rnd.fm, prefix + name, 28, row_y + 6, color)

        if self.browse_mode == "pick_dir":
            rnd.footer([("A", "Open"), ("<", "Select here"), ("Y", "New folder"), ("B", "Back")])
        else:
            rnd.footer([("A", "Open/Send"), ("B", "Back")])

    def _draw_loading(self, rnd):
        rnd.header("PLEASE WAIT", self.state_cache)

        # Spinner using elapsed time
        elapsed = time.time() - self.loading_start
        dots = "." * (int(elapsed * 2) % 4)

        rnd.panel(60, 140, W - 120, 160, C_ACCENT, C_BG2)

        # Spinner arc segments
        cx, cy = W // 2, 195
        seg = int(elapsed * 8) % 8
        colors = [C_ACCENT if i == seg or i == (seg-1)%8 else C_BORDER for i in range(8)]
        positions = [(0,-18),(12,-12),(18,0),(12,12),(0,18),(-12,12),(-18,0),(-12,-12)]
        for i, (dx, dy) in enumerate(positions):
            c = colors[i]
            rnd.set_color(c)
            r = SDL_Rect(cx+dx-4, cy+dy-4, 8, 8)
            SDL2.SDL_RenderFillRect(rnd.r, ctypes.byref(r))

        rnd.text(rnd.fm, self.loading_msg + dots, W // 2, 230, C_TEXT, center=True)
        rnd.text(rnd.fs, "This may take a moment", W // 2, 260, C_TEXT_DIM, center=True)

    def _draw_result(self, rnd):
        state = self.state_cache
        rnd.header("RESULT", state)

        rnd.panel(16, 70, W - 32, H - 120, C_BORDER, C_BG2)
        lines = self.result_msg.split("\n")
        for i, line in enumerate(lines[:14]):
            color = C_ACCENT if i == 0 else C_TEXT
            if "error" in line.lower() or "fail" in line.lower():
                color = C_RED
            elif "success" in line.lower() or "connected" in line.lower() or "sent" in line.lower():
                color = C_GREEN
            rnd.text(rnd.fm if i == 0 else rnd.fs, line, 32, 88 + i * 24, color)

        rnd.text(rnd.fm, "Press A to continue", W // 2, H - 70, C_TEXT_DIM, center=True)
        rnd.footer([("A", "OK")])


# --- Main Loop ---------------------------------------------------------------

def find_font():
    candidates = [
        "/mnt/mmc/MUOS/application/.terminal/res/SourceCodePro-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    # Search for any ttf
    try:
        r = subprocess.run(["find", "/usr/share/fonts", "-name", "*.ttf"],
                          capture_output=True, text=True, timeout=3)
        fonts = r.stdout.strip().split("\n")
        if fonts and fonts[0]:
            return fonts[0]
    except:
        pass
    return None

def main():
    os.environ["SDL_VIDEODRIVER"] = "x11"  # will try, fallback handled
    os.environ["DISPLAY"] = ":0"

    # Try different video drivers
    for driver in ["x11", "directfb", "fbcon", ""]:
        if driver:
            os.environ["SDL_VIDEODRIVER"] = driver
        else:
            os.environ.pop("SDL_VIDEODRIVER", None)

        ret = SDL2.SDL_Init(SDL_INIT_VIDEO | SDL_INIT_JOYSTICK | SDL_INIT_GAMECONTROLLER)
        if ret == 0:
            break
    else:
        print("SDL_Init failed: " + ctypes.string_at(SDL2.SDL_GetError()).decode())
        sys.exit(1)

    SDL2.SDL_ShowCursor(0)

    window = SDL2.SDL_CreateWindow(
        b"Tailscale",
        0x1FFF0000, 0x1FFF0000,  # SDL_WINDOWPOS_UNDEFINED
        W, H,
        SDL_WINDOW_SHOWN
    )
    if not window:
        print("Window creation failed")
        sys.exit(1)

    renderer = SDL2.SDL_CreateRenderer(window, -1, SDL_RENDERER_ACCELERATED | SDL_RENDERER_SOFTWARE)
    if not renderer:
        renderer = SDL2.SDL_CreateRenderer(window, -1, SDL_RENDERER_SOFTWARE)

    # Open joystick
    SDL2.SDL_JoystickOpen(0)

    # Load fonts
    font_small = font_med = font_large = None
    if SDL2_TTF:
        SDL2_TTF.TTF_Init()
        font_path = find_font()
        if font_path:
            fp = font_path.encode()
            font_small = SDL2_TTF.TTF_OpenFont(fp, 14)
            font_med   = SDL2_TTF.TTF_OpenFont(fp, 18)
            font_large = SDL2_TTF.TTF_OpenFont(fp, 24)

    rnd = Renderer(renderer, font_small, font_med, font_large)
    app = App()

    event = SDL_Event()
    clock_last = time.time()

    # Disable screensaver at the SDL level (works without xset)
    SDL2.SDL_DisableScreenSaver()

    # Also kill X11 DPMS via xset as belt-and-suspenders
    import subprocess as _sp
    try:
        _sp.Popen(["xset", "s", "off", "-dpms", "s", "noblank"],
                  stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
    except Exception:
        pass

    _last_keepalive = time.time()

    while app.running:
        # Process events
        while SDL2.SDL_PollEvent(ctypes.byref(event)):
            t = event.type
            if t == SDL_QUIT:
                app.running = False
            elif t == SDL_KEYDOWN:
                sym = event.key.sym
                if sym == SDLK_UP:      app.handle_input("UP")
                elif sym == SDLK_DOWN:  app.handle_input("DOWN")
                elif sym == SDLK_LEFT:  app.handle_input("LEFT")
                elif sym == SDLK_RIGHT: app.handle_input("RIGHT")
                elif sym in (SDLK_RETURN, SDLK_a, SDLK_SPACE): app.handle_input("CONFIRM")
                elif sym in (SDLK_ESCAPE, SDLK_b): app.handle_input("BACK")
            elif t == SDL_JOYBUTTONDOWN:
                btn = event.jbutton.button
                if btn == BTN_A:        app.handle_input("CONFIRM")
                elif btn == BTN_B:      app.handle_input("BACK")
                elif btn == BTN_Y:      app.handle_input("NEW_FOLDER")
                elif btn == BTN_START:  app.handle_input("BACK")
            elif t == SDL_JOYHATMOTION:
                val = event.jhat.value
                if val == HAT_UP:       app.handle_input("UP")
                elif val == HAT_DOWN:   app.handle_input("DOWN")
                elif val == HAT_LEFT:   app.handle_input("LEFT")
                elif val == HAT_RIGHT:  app.handle_input("RIGHT")
            elif t == SDL_JOYAXISMOTION:
                pass  # analog sticks, not used

        # Auto-refresh status every 5 seconds on main screen
        now = time.time()
        if app.screen == "main" and now - app.last_refresh > 10:
            app.last_refresh = now
            app._refresh_status()

        # Handle exit animation
        if app.exit_pending and time.time() - app.loading_start > 1.2:
            app.running = False

        app.draw(rnd)

        # Keepalive: re-assert screensaver disable every 10s (cheap SDL call, no subprocess)
        _now_ka = time.time()
        if _now_ka - _last_keepalive > 10:
            _last_keepalive = _now_ka
            SDL2.SDL_DisableScreenSaver()

        SDL2.SDL_Delay(16)  # ~60fps

    # Cleanup
    if SDL2_TTF:
        if font_small: SDL2_TTF.TTF_CloseFont(font_small)
        if font_med:   SDL2_TTF.TTF_CloseFont(font_med)
        if font_large: SDL2_TTF.TTF_CloseFont(font_large)
        SDL2_TTF.TTF_Quit()
    SDL2.SDL_DestroyRenderer(renderer)
    SDL2.SDL_DestroyWindow(window)
    SDL2.SDL_Quit()

if __name__ == "__main__":
    main()
