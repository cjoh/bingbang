"""
Multiplayer arcade shooter server.

Single-file, stdlib-only Python asyncio server:
 - Serves index.html and static assets on HTTP
 - Accepts WebSocket connections on /ws (hand-rolled WS handshake + framing)
 - Per-room authoritative game simulation at 30Hz
 - 3 game modes: Co-op vs Bots, Free-for-all PvP, Team Deathmatch
 - 4-char join codes, max 4 players per room
"""

import asyncio
import base64
import hashlib
import json
import math
import os
import random
import secrets
import string
import struct
import time
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS / MAP
# ─────────────────────────────────────────────────────────────────────────────
PORT = 8000
TICK_HZ = 30
TICK_DT = 1.0 / TICK_HZ
W, H = 1200, 800
MAX_PLAYERS = 4

WALLS = [
    # outer border
    {"x":0,"y":0,"w":1200,"h":22},
    {"x":0,"y":778,"w":1200,"h":22},
    {"x":0,"y":22,"w":22,"h":756},
    {"x":1178,"y":22,"w":22,"h":756},
    # left vertical divider (x=342)
    {"x":342,"y":22,"w":18,"h":130},
    {"x":342,"y":280,"w":18,"h":68},
    {"x":342,"y":468,"w":18,"h":52},
    {"x":342,"y":538,"w":18,"h":82},
    {"x":342,"y":744,"w":18,"h":34},
    # right vertical divider (x=840)
    {"x":840,"y":22,"w":18,"h":130},
    {"x":840,"y":280,"w":18,"h":68},
    {"x":840,"y":468,"w":18,"h":52},
    {"x":840,"y":538,"w":18,"h":82},
    {"x":840,"y":744,"w":18,"h":34},
    # top horizontal divider (y=262)
    {"x":22,"y":262,"w":158,"h":18},
    {"x":360,"y":262,"w":120,"h":18},
    {"x":720,"y":262,"w":120,"h":18},
    {"x":1020,"y":262,"w":158,"h":18},
    # bottom horizontal divider (y=520)
    {"x":22,"y":520,"w":158,"h":18},
    {"x":360,"y":520,"w":120,"h":18},
    {"x":720,"y":520,"w":120,"h":18},
    {"x":1020,"y":520,"w":158,"h":18},
    # VIP lounge
    {"x":40,"y":42,"w":130,"h":18},
    {"x":40,"y":60,"w":18,"h":90},
    {"x":152,"y":60,"w":18,"h":90},
    {"x":218,"y":46,"w":86,"h":18},
    {"x":218,"y":64,"w":18,"h":68},
    {"x":286,"y":64,"w":18,"h":68},
    # entrance lobby
    {"x":440,"y":34,"w":80,"h":80},
    {"x":680,"y":34,"w":80,"h":80},
    # bar
    {"x":898,"y":44,"w":240,"h":20},
    {"x":898,"y":160,"w":90,"h":20},
    {"x":1060,"y":160,"w":78,"h":20},
    # dance floor
    {"x":510,"y":298,"w":180,"h":48},
    {"x":410,"y":358,"w":44,"h":44},
    {"x":746,"y":358,"w":44,"h":44},
    {"x":410,"y":464,"w":44,"h":44},
    {"x":746,"y":464,"w":44,"h":44},
    # left hallway
    {"x":52,"y":332,"w":70,"h":26},
    {"x":52,"y":448,"w":70,"h":26},
    {"x":232,"y":388,"w":70,"h":26},
    # right room
    {"x":876,"y":318,"w":170,"h":18},
    {"x":876,"y":452,"w":170,"h":18},
    {"x":1064,"y":370,"w":70,"h":70},
    # storage
    {"x":50,"y":568,"w":150,"h":14},
    {"x":50,"y":644,"w":150,"h":14},
    {"x":50,"y":720,"w":150,"h":14},
    {"x":228,"y":568,"w":14,"h":150},
    # back corridor
    {"x":460,"y":610,"w":80,"h":60},
    {"x":660,"y":610,"w":80,"h":60},
    # kitchen / lounge
    {"x":894,"y":558,"w":262,"h":16},
    {"x":894,"y":638,"w":200,"h":16},
    {"x":1060,"y":654,"w":16,"h":122},
]

SPAWN_ZONES = [
    (80,50),(280,160),(80,200),
    (590,50),
    (920,200),(1050,200),
    (80,380),(80,470),
    (1120,350),(1120,460),
    (80,610),(80,750),
    (560,760),(720,760),
    (1120,610),(1120,750),
]

# Top half / bottom half spawn pools for TDM
TOP_SPAWNS    = [(x,y) for (x,y) in SPAWN_ZONES if y < H/2]
BOTTOM_SPAWNS = [(x,y) for (x,y) in SPAWN_ZONES if y >= H/2]

PLAYER_COLORS = ["#00e5ff", "#ff8800", "#88ff88", "#ff44aa"]

STACKABLE = {"damage_up","speed_up","bullet_speed","max_hp","fire_rate_up","multishot_up"}

ALL_ABILITIES = [
    {"id":"bouncy","name":"Bouncy Bullets","desc":"Bullets ricochet off walls 4 times","color":"#aaff00","icon":"↗"},
    {"id":"flaming","name":"Flaming Rounds","desc":"Burns enemies dealing damage over 3s","color":"#ff6600","icon":"🔥"},
    {"id":"freezing","name":"Cryo Bullets","desc":"Freezes enemies solid for 2 seconds","color":"#44ccff","icon":"❄"},
    {"id":"explosive","name":"Explosive Shells","desc":"Bullets explode with splash damage","color":"#ff2244","icon":"💥"},
    {"id":"rapid","name":"Rapid Fire","desc":"Fire rate doubled immediately","color":"#ffff00","icon":"⚡"},
    {"id":"piercing","name":"Piercing Shots","desc":"Bullets pass through all enemies","color":"#cc44ff","icon":"▸"},
    {"id":"homing","name":"Homing Bullets","desc":"Bullets curve toward nearest enemy","color":"#ff88cc","icon":"⟳"},
    {"id":"multishot","name":"Multishot","desc":"Fires 3 bullets simultaneously","color":"#88ff88","icon":"⊞"},
    {"id":"vampiric","name":"Vampiric","desc":"Each kill restores 10 HP","color":"#cc0044","icon":"♥"},
    {"id":"ricochet_kill","name":"Ricochet Kill","desc":"Every kill fires a free bouncing bullet","color":"#ffffff","icon":"✦"},
    {"id":"chain_lightning","name":"Chain Lightning","desc":"Bullets arc to 2 nearby enemies on hit","color":"#66aaff","icon":"≈"},
    {"id":"toxic_rounds","name":"Toxic Rounds","desc":"Bullets poison: 5 dmg/s for 4s","color":"#44ff66","icon":"☠"},
    {"id":"splitting_bullets","name":"Splitting Shots","desc":"Bullets split into 2 on first enemy hit","color":"#ee44ff","icon":"⋙"},
    {"id":"shotgun","name":"Shotgun Blast","desc":"5 pellets per shot in a wide spread","color":"#ffaa44","icon":"∴"},
    {"id":"cryo_shatter","name":"Cryo Shatter","desc":"Frozen enemies burst into ice shards on kill","color":"#aaeeff","icon":"✼"},
    {"id":"death_explosion","name":"Death Explosion","desc":"Enemies explode on kill — 30 splash damage","color":"#ff5500","icon":"☢"},
    {"id":"bullet_time","name":"Bullet Time","desc":"On kill: slow all enemies for 0.6s","color":"#ccaaff","icon":"◉"},
    {"id":"rage","name":"Rage","desc":"Damage scales up to ×3 as HP drops","color":"#ff2200","icon":"⚔"},
    {"id":"shield","name":"Shield","desc":"Block one hit. Recharges each wave / 8s in PvP","color":"#ffd700","icon":"◈"},
    {"id":"lifesteal","name":"Lifesteal","desc":"Every bullet hit heals 1 HP","color":"#ff4488","icon":"✚"},
    {"id":"blood_money","name":"Blood Money","desc":"Combo window +2s and max combo raised to 20","color":"#aa0033","icon":"$"},
    {"id":"dash","name":"Dash","desc":"SPACE: burst forward in movement dir (1.5s cd)","color":"#00ffee","icon":"▶"},
    {"id":"damage_up","name":"Power Surge","desc":"Bullet damage ×1.6 per stack","color":"#ff8800","icon":"▲"},
    {"id":"speed_up","name":"Adrenaline","desc":"Move speed ×1.35 per stack","color":"#00ffcc","icon":"»"},
    {"id":"bullet_speed","name":"Bullet Speed","desc":"Bullets 40% faster per stack","color":"#ffff88","icon":"≫"},
    {"id":"max_hp","name":"Max HP +25","desc":"+25 max HP and current HP per stack","color":"#ff88aa","icon":"♦"},
    {"id":"fire_rate_up","name":"Fire Rate+","desc":"Fire rate ×0.75 per stack","color":"#ffee00","icon":"§"},
    {"id":"multishot_up","name":"Multishot +1","desc":"One extra bullet per shot per stack","color":"#88ffaa","icon":"#"},
]
ABILITY_BY_ID = {a["id"]: a for a in ALL_ABILITIES}

# ─────────────────────────────────────────────────────────────────────────────
# GEOMETRY HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def rect_circle(rx, ry, rw, rh, cx, cy, cr):
    nx = max(rx, min(rx + rw, cx))
    ny = max(ry, min(ry + rh, cy))
    return math.hypot(cx - nx, cy - ny) < cr

def push_out_of_walls(e):
    for w in WALLS:
        if not rect_circle(w["x"], w["y"], w["w"], w["h"], e["x"], e["y"], e["r"]):
            continue
        oL = (e["x"] + e["r"]) - w["x"]
        oR = (w["x"] + w["w"]) - (e["x"] - e["r"])
        oT = (e["y"] + e["r"]) - w["y"]
        oB = (w["y"] + w["h"]) - (e["y"] - e["r"])
        m = min(oL, oR, oT, oB)
        if m == oL: e["x"] -= oL
        elif m == oR: e["x"] += oR
        elif m == oT: e["y"] -= oT
        else: e["y"] += oB

def line_hits_wall(x1, y1, x2, y2):
    # quick check used for sanity-clamping, not for collision (collision is per-step)
    dx, dy = x2 - x1, y2 - y1
    steps = max(1, int(math.hypot(dx, dy) / 6))
    for i in range(1, steps + 1):
        x = x1 + dx * i / steps
        y = y1 + dy * i / steps
        for w in WALLS:
            if w["x"] <= x <= w["x"] + w["w"] and w["y"] <= y <= w["y"] + w["h"]:
                return True
    return False

# ─────────────────────────────────────────────────────────────────────────────
# NAVIGATION: walkable grid + A* + line-of-sight
# ─────────────────────────────────────────────────────────────────────────────
GRID_SIZE = 20
GRID_W = W // GRID_SIZE + 2
GRID_H = H // GRID_SIZE + 2
BOT_NAV_R = 10  # smaller than the 16-radius bot; push_out_of_walls handles minor overlap

def _build_walk_grid():
    grid = [[True] * GRID_W for _ in range(GRID_H)]
    for gy in range(GRID_H):
        for gx in range(GRID_W):
            cx = gx * GRID_SIZE + GRID_SIZE / 2
            cy = gy * GRID_SIZE + GRID_SIZE / 2
            blocked = False
            for w in WALLS:
                if rect_circle(w["x"], w["y"], w["w"], w["h"], cx, cy, BOT_NAV_R):
                    blocked = True
                    break
            grid[gy][gx] = not blocked
    return grid

WALK_GRID = _build_walk_grid()

def cell_of(x, y):
    return (int(x // GRID_SIZE), int(y // GRID_SIZE))

def cell_walkable(gx, gy):
    if gx < 0 or gy < 0 or gx >= GRID_W or gy >= GRID_H:
        return False
    return WALK_GRID[gy][gx]

def nearest_walkable(gx, gy, radius=4):
    """Snap an unwalkable cell to the nearest walkable one (spiral search)."""
    if cell_walkable(gx, gy):
        return (gx, gy)
    for d in range(1, radius + 1):
        for dx in range(-d, d + 1):
            for dy in (-d, d):
                if cell_walkable(gx + dx, gy + dy): return (gx + dx, gy + dy)
            for dy in range(-d + 1, d):
                for dx2 in (-d, d):
                    if cell_walkable(gx + dx2, gy + dy): return (gx + dx2, gy + dy)
    return None

import heapq as _heapq

def astar(start, goal, max_nodes=1500):
    if start == goal: return [start]
    if not cell_walkable(*start) or not cell_walkable(*goal): return None
    SQRT2 = math.sqrt(2)
    open_set = []
    _heapq.heappush(open_set, (0.0, start))
    came = {}
    g = {start: 0.0}
    visited = 0
    while open_set and visited < max_nodes:
        _, cur = _heapq.heappop(open_set)
        if cur == goal:
            path = [cur]
            while cur in came:
                cur = came[cur]
                path.append(cur)
            path.reverse()
            return path
        visited += 1
        cx, cy = cur
        for dx, dy in ((1,0),(-1,0),(0,1),(0,-1),(1,1),(1,-1),(-1,1),(-1,-1)):
            nx, ny = cx + dx, cy + dy
            if not cell_walkable(nx, ny): continue
            if dx and dy:
                # diagonals require both orthogonals open (no corner-cutting)
                if not cell_walkable(cx + dx, cy) or not cell_walkable(cx, cy + dy):
                    continue
            step = SQRT2 if dx and dy else 1.0
            ng = g[cur] + step
            if ng < g.get((nx, ny), 1e18):
                came[(nx, ny)] = cur
                g[(nx, ny)] = ng
                gxd = abs(nx - goal[0]); gyd = abs(ny - goal[1])
                h = max(gxd, gyd) + (SQRT2 - 1) * min(gxd, gyd)
                _heapq.heappush(open_set, (ng + h, (nx, ny)))
    return None

def has_line_of_sight(x1, y1, x2, y2, pad=14):
    """True if a circle of radius `pad` can travel from p1 to p2 without hitting a wall."""
    dx, dy = x2 - x1, y2 - y1
    dist = math.hypot(dx, dy)
    if dist < 1: return True
    steps = max(2, int(dist / 10))
    for i in range(1, steps):
        x = x1 + dx * i / steps
        y = y1 + dy * i / steps
        for w in WALLS:
            if rect_circle(w["x"], w["y"], w["w"], w["h"], x, y, pad):
                return False
    return True

# ─────────────────────────────────────────────────────────────────────────────
# WEBSOCKET (stdlib, RFC 6455 minimum needed)
# ─────────────────────────────────────────────────────────────────────────────
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

class WSClient:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.alive = True
        self._send_lock = asyncio.Lock()

    async def recv(self):
        try:
            hdr = await self.reader.readexactly(2)
        except (asyncio.IncompleteReadError, ConnectionError):
            self.alive = False
            return None
        b1, b2 = hdr[0], hdr[1]
        opcode = b1 & 0x0f
        masked = b2 & 0x80
        plen = b2 & 0x7f
        try:
            if plen == 126:
                ext = await self.reader.readexactly(2)
                plen = struct.unpack("!H", ext)[0]
            elif plen == 127:
                ext = await self.reader.readexactly(8)
                plen = struct.unpack("!Q", ext)[0]
            if masked:
                mask = await self.reader.readexactly(4)
            else:
                mask = b"\0\0\0\0"
            payload = await self.reader.readexactly(plen) if plen else b""
        except (asyncio.IncompleteReadError, ConnectionError):
            self.alive = False
            return None
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode == 0x8:  # close
            self.alive = False
            return None
        if opcode == 0x9:  # ping
            await self._send_raw(payload, opcode=0xA)
            return await self.recv()
        if opcode == 0xA:  # pong
            return await self.recv()
        if opcode == 0x1:  # text
            try:
                return json.loads(payload.decode("utf-8"))
            except Exception:
                return None
        return None

    async def _send_raw(self, payload, opcode=0x1):
        if not self.alive: return
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([n])
        elif n < 65536:
            header += bytes([126]) + struct.pack("!H", n)
        else:
            header += bytes([127]) + struct.pack("!Q", n)
        async with self._send_lock:
            try:
                self.writer.write(header + payload)
                await self.writer.drain()
            except (ConnectionError, OSError):
                self.alive = False

    async def send(self, obj):
        await self._send_raw(json.dumps(obj, separators=(",",":")))

    async def close(self):
        if not self.alive: return
        self.alive = False
        try:
            await self._send_raw(b"", opcode=0x8)
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass


async def ws_handshake(reader, writer, headers):
    key = headers.get("sec-websocket-key", "")
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    resp = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    )
    writer.write(resp.encode())
    await writer.drain()
    return WSClient(reader, writer)

# ─────────────────────────────────────────────────────────────────────────────
# HTTP STATIC FILE SERVING (minimal)
# ─────────────────────────────────────────────────────────────────────────────
MIME = {
    ".html":"text/html; charset=utf-8",
    ".js":"application/javascript; charset=utf-8",
    ".css":"text/css; charset=utf-8",
    ".png":"image/png", ".jpg":"image/jpeg", ".svg":"image/svg+xml",
    ".ico":"image/x-icon",
}
SERVE_ROOT = os.path.dirname(os.path.abspath(__file__))

async def serve_static(writer, path):
    if path == "/" or path == "":
        path = "/index.html"
    # prevent path traversal
    safe = os.path.normpath(path).lstrip("/")
    full = os.path.join(SERVE_ROOT, safe)
    if not full.startswith(SERVE_ROOT) or not os.path.isfile(full):
        body = b"404 Not Found"
        writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: " +
                     str(len(body)).encode() + b"\r\n\r\n" + body)
        await writer.drain(); return
    ext = os.path.splitext(full)[1].lower()
    mime = MIME.get(ext, "application/octet-stream")
    with open(full, "rb") as f:
        body = f.read()
    headers = (
        f"HTTP/1.1 200 OK\r\n"
        f"Content-Type: {mime}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Cache-Control: no-store\r\n\r\n"
    ).encode()
    writer.write(headers + body)
    await writer.drain()

# ─────────────────────────────────────────────────────────────────────────────
# GAME ENTITIES
# ─────────────────────────────────────────────────────────────────────────────
_bot_id_seq = 0
def next_bot_id():
    global _bot_id_seq
    _bot_id_seq += 1
    return _bot_id_seq

def make_player(pid, name, color, team=0):
    return {
        "id": pid, "name": name[:14] or "Player", "color": color, "team": team,
        "x": 600.0, "y": 400.0, "r": 18.0,
        "speed": 220.0, "hp": 100.0, "maxHp": 100.0,
        "angle": 0.0,
        "fireRate": 0.19, "fireCooldown": 0.0, "damage": 20.0,
        "abilities": [], "invuln": 0.0,
        "dashCooldown": 0.0, "dashMaxCd": 1.5, "dashVx": 0.0, "dashVy": 0.0,
        "shield": False, "shieldCdTotal": 8.0, "shieldCd": 0.0,
        "score": 0, "kills": 0, "deaths": 0,
        "alive": True, "respawnAt": 0.0, "lastDamagerId": None,
        # input
        "keys": set(), "mx": 600.0, "my": 400.0, "firing": False, "dashQueued": False,
        # card pick state
        "pendingCards": None,  # list of ability ids when cards offered
    }

def make_bot(x, y, wave):
    hp = 42 + wave * 20
    return {
        "id": next_bot_id(), "x": float(x), "y": float(y), "r": 16.0,
        "hp": float(hp), "maxHp": float(hp),
        "speed": 78 + wave * 10 + random.random() * 40,
        "angle": 0.0,
        "fireRate": max(0.45, 1.5 - wave * 0.06) + random.random() * 0.5,
        "fireCooldown": random.random() * 1.6,
        "frozen": 0.0, "burning": 0.0, "burnDmg": 0.0,
        "toxicTimer": 0.0, "toxicDmg": 0.0,
        "color": f"hsl({335 + random.random() * 50:.0f},90%,58%)",
        "flash": 0.0,
        # navigation
        "path": [],              # list of (x,y) waypoints
        "targetId": None,        # which player they're chasing
        "lastTargetX": 0.0, "lastTargetY": 0.0,
        "replanCd": random.random() * 0.4,  # staggered initial replans
        "stuckTimer": 0.0,
        "lastX": float(x), "lastY": float(y),
        "strafeBias": random.choice((-1, 1)),  # for kiting variety
    }

def spawn_pos_for(room, team=None):
    pool = SPAWN_ZONES
    if room.mode == "tdm" and team is not None:
        pool = TOP_SPAWNS if team == 0 else BOTTOM_SPAWNS
    # avoid spawning right on top of any player
    candidates = [p for p in pool if all(
        math.hypot(p[0]-pl["x"], p[1]-pl["y"]) > 180
        for pl in room.players.values() if pl["alive"]
    )] or list(pool)
    x, y = random.choice(candidates)
    return (x + (random.random()-0.5)*16, y + (random.random()-0.5)*16)

# ─────────────────────────────────────────────────────────────────────────────
# ROOM / GAME
# ─────────────────────────────────────────────────────────────────────────────
def gen_code(taken):
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no confusables
    for _ in range(40):
        code = "".join(secrets.choice(alphabet) for _ in range(4))
        if code not in taken:
            return code
    return "".join(secrets.choice(alphabet) for _ in range(6))


class Room:
    def __init__(self, code, mode, host_id):
        self.code = code
        self.mode = mode  # 'coop' | 'ffa' | 'tdm'
        self.host_id = host_id
        self.players = {}       # pid -> player dict
        self.clients = {}       # pid -> WSClient
        self.bots = []
        self.bullets = []
        self.events = []        # FX events for next snapshot
        self.phase = "lobby"    # lobby | playing | card_select | game_over
        self.wave = 0
        self.bots_to_spawn = 0
        self.spawn_timer = 0.0
        self.bullet_time = 0.0  # global slow-mo timer (server-side)
        self.tick_task = None
        self.t = 0.0
        self.match_end_at = 0.0  # for PvP modes
        self.match_duration = 180.0
        self.target_score = 30   # ffa/tdm: first to N
        self.team_scores = [0, 0]
        self.winner = None
        self.next_card_at = 0.0  # for PvP card cadence

    def is_full(self): return len(self.players) >= MAX_PLAYERS
    def is_empty(self): return len(self.players) == 0

    def assign_team(self):
        """For TDM: balance teams; otherwise return 0."""
        if self.mode != "tdm":
            return 0
        c0 = sum(1 for p in self.players.values() if p["team"] == 0)
        c1 = sum(1 for p in self.players.values() if p["team"] == 1)
        return 0 if c0 <= c1 else 1

    def add_player(self, pid, name):
        idx = len(self.players)
        color = PLAYER_COLORS[idx % len(PLAYER_COLORS)]
        team = self.assign_team()
        p = make_player(pid, name, color, team)
        x, y = spawn_pos_for(self, team)
        p["x"], p["y"] = x, y
        self.players[pid] = p
        return p

    def remove_player(self, pid):
        self.players.pop(pid, None)
        self.clients.pop(pid, None)
        if self.host_id == pid and self.players:
            self.host_id = next(iter(self.players.keys()))

    def lobby_payload(self):
        return {
            "type": "lobby",
            "code": self.code,
            "mode": self.mode,
            "host": self.host_id,
            "players": [
                {"id": p["id"], "name": p["name"], "color": p["color"], "team": p["team"]}
                for p in self.players.values()
            ],
        }

    async def broadcast(self, obj):
        msg = json.dumps(obj, separators=(",",":"))
        for c in list(self.clients.values()):
            try:
                await c._send_raw(msg)
            except Exception:
                pass

    async def send_to(self, pid, obj):
        c = self.clients.get(pid)
        if c: await c.send(obj)

    # ───────────────────────────────────────────────────────────────────────
    # CARD HANDLING
    # ───────────────────────────────────────────────────────────────────────
    def offer_cards(self, pid):
        p = self.players.get(pid)
        if not p: return
        owned = set(p["abilities"])
        pool = [a for a in ALL_ABILITIES if a["id"] in STACKABLE or a["id"] not in owned]
        choices = random.sample(pool, min(3, len(pool)))
        p["pendingCards"] = [a["id"] for a in choices]
        asyncio.create_task(self.send_to(pid, {
            "type": "cards",
            "choices": [
                {**a, "stacks": p["abilities"].count(a["id"])} for a in choices
            ],
        }))

    def apply_card(self, p, ability_id):
        p["abilities"].append(ability_id)
        if ability_id == "rapid":
            p["fireRate"] = max(0.055, p["fireRate"] * 0.5)
        elif ability_id == "damage_up":
            p["damage"] *= 1.6
        elif ability_id == "speed_up":
            p["speed"] *= 1.35
        elif ability_id == "fire_rate_up":
            p["fireRate"] = max(0.055, p["fireRate"] * 0.75)
        elif ability_id == "max_hp":
            p["maxHp"] += 25
            p["hp"] = min(p["hp"] + 25, p["maxHp"])
        elif ability_id == "shield":
            p["shield"] = True

    def pick_card(self, pid, idx):
        p = self.players.get(pid)
        if not p or not p["pendingCards"]: return
        if idx < 0 or idx >= len(p["pendingCards"]): return
        ab_id = p["pendingCards"][idx]
        self.apply_card(p, ab_id)
        p["pendingCards"] = None

        if self.mode == "coop":
            # If everyone has picked, start next wave
            if not any(pp["pendingCards"] for pp in self.players.values()):
                self.start_wave(self.wave + 1)
        # In PvP/TDM modes: nothing else needed (game keeps running while cards display)

    # ───────────────────────────────────────────────────────────────────────
    # MATCH / WAVE LIFECYCLE
    # ───────────────────────────────────────────────────────────────────────
    def start_match(self):
        # reset
        self.bots.clear()
        self.bullets.clear()
        self.events.clear()
        self.team_scores = [0, 0]
        self.winner = None
        self.t = 0.0
        for p in self.players.values():
            p["abilities"] = []
            p["maxHp"] = 100
            p["hp"] = 100
            p["speed"] = 220
            p["damage"] = 20
            p["fireRate"] = 0.19
            p["fireCooldown"] = 0
            p["score"] = 0; p["kills"] = 0; p["deaths"] = 0
            p["alive"] = True
            p["shield"] = False
            p["shieldCd"] = 0
            p["dashCooldown"] = 0
            p["pendingCards"] = None
            x, y = spawn_pos_for(self, p["team"])
            p["x"], p["y"] = x, y

        if self.mode == "coop":
            self.phase = "playing"
            self.start_wave(1)
        else:
            self.phase = "playing"
            self.match_end_at = self.t + self.match_duration
            self.next_card_at = self.t + 25.0  # first cards 25s in
            # offer everyone a starter card immediately in PvP modes
            if self.mode == "ffa":
                for pid in list(self.players.keys()):
                    self.offer_cards(pid)
            elif self.mode == "tdm":
                # initial: everyone picks
                for pid in list(self.players.keys()):
                    self.offer_cards(pid)

    def start_wave(self, n):
        self.wave = n
        self.bots_to_spawn = 3 + n * 2
        self.spawn_timer = 0
        self.phase = "playing"
        for p in self.players.values():
            if p["alive"]:
                p["hp"] = min(p["maxHp"], p["hp"] + 18)
            if "shield" in p["abilities"]:
                p["shield"] = True
            p["pendingCards"] = None

    # ───────────────────────────────────────────────────────────────────────
    # FIRING
    # ───────────────────────────────────────────────────────────────────────
    def bullet_color_for(self, p):
        a = p["abilities"]
        if "toxic_rounds" in a:      return "#44ff66"
        if "splitting_bullets" in a: return "#ee44ff"
        if "flaming" in a:           return "#ff6600"
        if "freezing" in a:          return "#44ccff"
        if "explosive" in a:         return "#ff2244"
        if "homing" in a:            return "#ff88cc"
        if "bouncy" in a:            return "#aaff00"
        if "piercing" in a:          return "#cc44ff"
        if "chain_lightning" in a:   return "#66aaff"
        return "#ffee33"

    def get_rage_mult(self, p):
        if "rage" not in p["abilities"]: return 1.0
        return 1 + 2 * (1 - max(0, p["hp"]) / p["maxHp"])

    def get_bullet_speed(self, p):
        stacks = p["abilities"].count("bullet_speed")
        return 660 * (1.4 ** stacks)

    def spawn_bullet(self, x, y, angle, opts):
        spd = opts.get("speed", 660.0)
        self.bullets.append({
            "x": x, "y": y,
            "vx": math.cos(angle) * spd, "vy": math.sin(angle) * spd,
            "r": opts.get("r", 6.0),
            "damage": opts.get("damage", 20.0),
            "color": opts.get("color", "#ffee33"),
            "owner": opts.get("owner", "player"),  # 'player' | 'bot' | pid (for PvP)
            "ownerId": opts.get("ownerId"),
            "ownerTeam": opts.get("ownerTeam", -1),
            "bounces": opts.get("bounces", 0),
            "piercing": opts.get("piercing", False),
            "homing": opts.get("homing", False),
            "flaming": opts.get("flaming", False),
            "freezing": opts.get("freezing", False),
            "explosive": opts.get("explosive", False),
            "toxic": opts.get("toxic", False),
            "splitting": opts.get("splitting", False),
            "hasSplit": False,
            "life": opts.get("life", 3.8),
            "hitIds": set(),
        })

    def fire_player(self, p):
        a = p["abilities"]
        col = self.bullet_color_for(p)
        dmg = p["damage"] * self.get_rage_mult(p)
        spd = self.get_bullet_speed(p)
        base_opts = {
            "damage": dmg, "color": col, "speed": spd,
            "bounces": 4 if "bouncy" in a else 0,
            "piercing": "piercing" in a,
            "homing": "homing" in a,
            "flaming": "flaming" in a,
            "freezing": "freezing" in a,
            "explosive": "explosive" in a,
            "toxic": "toxic_rounds" in a,
            "splitting": "splitting_bullets" in a,
            "owner": "player",
            "ownerId": p["id"],
            "ownerTeam": p["team"],
        }
        self.events.append({"k": "shoot", "x": p["x"], "y": p["y"]})
        muzzleX = p["x"] + math.cos(p["angle"]) * 32
        muzzleY = p["y"] + math.sin(p["angle"]) * 32
        if "shotgun" in a:
            for i in range(5):
                off = (i - 2) * 0.19
                so = dict(base_opts)
                so["damage"] = dmg * 0.7
                so["r"] = 5
                so["speed"] = spd * 0.85
                self.spawn_bullet(muzzleX, muzzleY, p["angle"] + off, so)
            return
        base = 3 if "multishot" in a else 1
        extra = a.count("multishot_up")
        n = base + extra
        for i in range(n):
            off = 0 if n == 1 else (i - (n - 1) / 2) * 0.22
            self.spawn_bullet(muzzleX, muzzleY, p["angle"] + off, base_opts)

    # ───────────────────────────────────────────────────────────────────────
    # KILL HANDLING
    # ───────────────────────────────────────────────────────────────────────
    def explode_at(self, x, y, rad, dmg, source_pid=None, source_team=-1):
        self.events.append({"k": "explode", "x": x, "y": y, "r": rad})
        # damage bots
        for b in self.bots:
            d = math.hypot(b["x"] - x, b["y"] - y)
            if d < rad:
                b["hp"] -= dmg * (1 - d / rad)
        # damage players (excluding teammates / self in coop)
        for p in self.players.values():
            if not p["alive"] or p["invuln"] > 0: continue
            if source_pid is not None and p["id"] == source_pid: continue
            if self.mode == "coop": continue  # no friendly fire in coop
            if self.mode == "tdm" and p["team"] == source_team: continue
            d = math.hypot(p["x"] - x, p["y"] - y)
            if d < rad:
                self.damage_player(p, dmg * (1 - d / rad), source_pid)

    def damage_player(self, p, dmg, source_pid=None):
        if p["invuln"] > 0 or not p["alive"]: return
        if p["shield"]:
            p["shield"] = False
            p["shieldCd"] = p["shieldCdTotal"]
            p["invuln"] = 0.5
            self.events.append({"k":"shield","x":p["x"],"y":p["y"]})
            return
        p["hp"] -= dmg
        p["invuln"] = 0.14
        p["lastDamagerId"] = source_pid
        self.events.append({"k":"hurt","x":p["x"],"y":p["y"],"d":int(dmg)})
        if p["hp"] <= 0:
            self.kill_player(p, source_pid)

    def kill_player(self, p, killer_pid=None):
        p["alive"] = False
        p["deaths"] += 1
        self.events.append({"k":"playerDie","x":p["x"],"y":p["y"],"color":p["color"]})
        if self.mode == "coop":
            # In coop: respawn after wave clear (or 5s grace)
            p["respawnAt"] = self.t + 8.0
        else:
            # PvP / TDM
            p["respawnAt"] = self.t + 3.0
            killer = self.players.get(killer_pid) if killer_pid else None
            if killer and killer["id"] != p["id"]:
                if self.mode == "tdm" and killer["team"] == p["team"]:
                    # team kill — no points
                    pass
                else:
                    killer["kills"] += 1
                    killer["score"] += 100
                    if self.mode == "tdm":
                        self.team_scores[killer["team"]] += 1

    def kill_bot(self, bot, killer_pid=None):
        self.events.append({
            "k":"botDie","x":bot["x"],"y":bot["y"],"color":bot["color"],
            "frozen": 1 if bot["frozen"] > 0 else 0,
        })
        killer = self.players.get(killer_pid) if killer_pid else None
        if killer:
            if "ricochet_kill" in killer["abilities"]:
                self.spawn_bullet(bot["x"], bot["y"], random.random()*math.pi*2, {
                    "damage": killer["damage"] * 0.75, "color":"#fff",
                    "bounces": 4, "r": 7, "speed": 500,
                    "owner": "player", "ownerId": killer["id"], "ownerTeam": killer["team"],
                })
            if "vampiric" in killer["abilities"]:
                killer["hp"] = min(killer["maxHp"], killer["hp"] + 10)
            if "death_explosion" in killer["abilities"]:
                self.explode_at(bot["x"], bot["y"], 88, 30, killer["id"], killer["team"])
            if "cryo_shatter" in killer["abilities"] and bot["frozen"] > 0:
                for i in range(8):
                    a = i / 8 * math.pi * 2
                    self.spawn_bullet(bot["x"], bot["y"], a, {
                        "damage": killer["damage"] * 0.45, "color":"#44ccff",
                        "speed": 320, "r": 4, "life": 0.9, "bounces": 1, "freezing": True,
                        "owner": "player", "ownerId": killer["id"], "ownerTeam": killer["team"],
                    })
            if "bullet_time" in killer["abilities"]:
                self.bullet_time = max(self.bullet_time, 0.6)
            killer["kills"] += 1
            killer["score"] += 100

    # ───────────────────────────────────────────────────────────────────────
    # MAIN TICK
    # ───────────────────────────────────────────────────────────────────────
    def update_players(self, dt):
        for p in self.players.values():
            p["invuln"] = max(0, p["invuln"] - dt)
            p["shieldCd"] = max(0, p["shieldCd"] - dt)
            if "shield" in p["abilities"] and not p["shield"] and p["shieldCd"] <= 0 and self.mode in ("ffa","tdm"):
                p["shield"] = True
            # respawn
            if not p["alive"]:
                if self.t >= p["respawnAt"]:
                    p["alive"] = True
                    p["hp"] = p["maxHp"]
                    p["invuln"] = 1.2
                    x, y = spawn_pos_for(self, p["team"])
                    p["x"], p["y"] = x, y
                continue
            # movement
            dx = (1 if "ArrowRight" in p["keys"] or "KeyD" in p["keys"] else 0) - \
                 (1 if "ArrowLeft"  in p["keys"] or "KeyA" in p["keys"] else 0)
            dy = (1 if "ArrowDown"  in p["keys"] or "KeyS" in p["keys"] else 0) - \
                 (1 if "ArrowUp"    in p["keys"] or "KeyW" in p["keys"] else 0)
            ln = math.hypot(dx, dy) or 1
            if dx or dy:
                p["x"] += dx / ln * p["speed"] * dt
                p["y"] += dy / ln * p["speed"] * dt
                push_out_of_walls(p)
            # dash
            p["dashCooldown"] = max(0, p["dashCooldown"] - dt)
            if p["dashQueued"] and "dash" in p["abilities"] and p["dashCooldown"] <= 0:
                p["dashQueued"] = False
                ddx, ddy = dx, dy
                if not ddx and not ddy:
                    ddx, ddy = math.cos(p["angle"]), math.sin(p["angle"])
                dl = math.hypot(ddx, ddy) or 1
                p["dashVx"] = ddx / dl * 650
                p["dashVy"] = ddy / dl * 650
                p["dashCooldown"] = p["dashMaxCd"]
                p["invuln"] = max(p["invuln"], 0.18)
                self.events.append({"k":"dash","x":p["x"],"y":p["y"]})
            else:
                p["dashQueued"] = False
            if p["dashVx"] or p["dashVy"]:
                p["x"] += p["dashVx"] * dt
                p["y"] += p["dashVy"] * dt
                push_out_of_walls(p)
                decay = math.exp(-dt * 9)
                p["dashVx"] *= decay; p["dashVy"] *= decay
                if math.hypot(p["dashVx"], p["dashVy"]) < 4:
                    p["dashVx"] = 0; p["dashVy"] = 0
            # bounds
            p["x"] = max(p["r"] + 22, min(W - p["r"] - 22, p["x"]))
            p["y"] = max(p["r"] + 22, min(H - p["r"] - 22, p["y"]))
            # aim
            p["angle"] = math.atan2(p["my"] - p["y"], p["mx"] - p["x"])
            # firing
            p["fireCooldown"] = max(0, p["fireCooldown"] - dt)
            if p["firing"] and p["fireCooldown"] == 0:
                # block firing while picking a card
                if not p["pendingCards"]:
                    p["fireCooldown"] = p["fireRate"]
                    self.fire_player(p)

    def update_bullets(self, dt):
        keep = []
        for b in self.bullets:
            b["life"] -= dt
            if b["life"] <= 0: continue
            if b["homing"] and b["owner"] == "player":
                # find nearest enemy target
                target = None; bd = 1e18
                for bt in self.bots:
                    d = math.hypot(bt["x"]-b["x"], bt["y"]-b["y"])
                    if d < bd: bd = d; target = bt
                if self.mode != "coop":
                    for pl in self.players.values():
                        if not pl["alive"] or pl["id"] == b["ownerId"]: continue
                        if self.mode == "tdm" and pl["team"] == b["ownerTeam"]: continue
                        d = math.hypot(pl["x"]-b["x"], pl["y"]-b["y"])
                        if d < bd: bd = d; target = pl
                if target:
                    ta = math.atan2(target["y"]-b["y"], target["x"]-b["x"])
                    ca = math.atan2(b["vy"], b["vx"])
                    diff = ((ta - ca + math.pi*3) % (math.pi*2)) - math.pi
                    turn = max(-5*dt, min(5*dt, diff))
                    spd = math.hypot(b["vx"], b["vy"])
                    na = ca + turn
                    b["vx"] = math.cos(na) * spd; b["vy"] = math.sin(na) * spd
            b["x"] += b["vx"] * dt
            b["y"] += b["vy"] * dt
            # walls
            hit_wall = False
            for w in WALLS:
                if not rect_circle(w["x"], w["y"], w["w"], w["h"], b["x"], b["y"], b["r"]): continue
                if b["bounces"] > 0:
                    b["bounces"] -= 1
                    nx = b["x"] - max(w["x"], min(w["x"]+w["w"], b["x"]))
                    ny = b["y"] - max(w["y"], min(w["y"]+w["h"], b["y"]))
                    if abs(nx) >= abs(ny): b["vx"] *= -1
                    else: b["vy"] *= -1
                    b["x"] += b["vx"] * dt * 2
                    b["y"] += b["vy"] * dt * 2
                else:
                    hit_wall = True
                break
            if hit_wall:
                if b["explosive"]:
                    self.explode_at(b["x"], b["y"], 100, b["damage"] * 0.85,
                                    b.get("ownerId"), b.get("ownerTeam", -1))
                continue
            if b["x"] < -50 or b["x"] > W+50 or b["y"] < -50 or b["y"] > H+50:
                continue
            keep.append(b)
        self.bullets = keep

    def update_bots(self, dt):
        if self.mode != "coop":
            self.bots.clear()
            return
        slow = 0.22 if self.bullet_time > 0 else 1.0
        survivors = []
        for bot in self.bots:
            if bot["hp"] <= 0:
                # find a recent damager? naive: closest alive player
                killer = None
                bd = 1e18
                for pl in self.players.values():
                    if not pl["alive"]: continue
                    d = math.hypot(pl["x"]-bot["x"], pl["y"]-bot["y"])
                    if d < bd: bd = d; killer = pl
                self.kill_bot(bot, killer["id"] if killer else None)
                continue
            bot["flash"] = max(0, bot["flash"] - dt * 6)
            if bot["frozen"] > 0:
                bot["frozen"] -= dt * slow
                survivors.append(bot); continue
            if bot["burning"] > 0:
                bot["burning"] -= dt * slow
                bot["hp"] -= bot["burnDmg"] * dt * slow
            if bot["toxicTimer"] > 0:
                bot["toxicTimer"] -= dt * slow
                bot["hp"] -= bot["toxicDmg"] * dt * slow
            # target nearest alive player
            target = None; bd = 1e18
            for pl in self.players.values():
                if not pl["alive"]: continue
                d = math.hypot(pl["x"]-bot["x"], pl["y"]-bot["y"])
                if d < bd: bd = d; target = pl
            if target:
                dx = target["x"] - bot["x"]; dy = target["y"] - bot["y"]
                dist = math.hypot(dx, dy) or 1
                bot["angle"] = math.atan2(dy, dx)
                has_los = has_line_of_sight(bot["x"], bot["y"], target["x"], target["y"], 14)

                # ── PATH PLANNING ───────────────────────────────────────────
                bot["replanCd"] -= dt
                target_moved = math.hypot(target["x"] - bot["lastTargetX"],
                                          target["y"] - bot["lastTargetY"]) > 60
                target_switched = bot["targetId"] != target["id"]
                need_path = (not has_los) and (
                    not bot["path"] or bot["replanCd"] <= 0
                    or target_moved or target_switched
                )
                if need_path:
                    bot["replanCd"] = 0.35 + random.random() * 0.15
                    bot["targetId"] = target["id"]
                    bot["lastTargetX"] = target["x"]; bot["lastTargetY"] = target["y"]
                    sc = nearest_walkable(*cell_of(bot["x"], bot["y"]))
                    gc = nearest_walkable(*cell_of(target["x"], target["y"]))
                    if sc and gc:
                        path = astar(sc, gc)
                        if path and len(path) > 1:
                            # smooth: drop waypoints we already have LoS to next-next
                            wps = []
                            i = 1
                            while i < len(path):
                                wx = path[i][0] * GRID_SIZE + GRID_SIZE / 2
                                wy = path[i][1] * GRID_SIZE + GRID_SIZE / 2
                                wps.append((wx, wy))
                                # skip ahead while we still have LoS from current pos
                                j = i + 1
                                while j < len(path):
                                    wx2 = path[j][0] * GRID_SIZE + GRID_SIZE / 2
                                    wy2 = path[j][1] * GRID_SIZE + GRID_SIZE / 2
                                    if has_line_of_sight(bot["x"], bot["y"], wx2, wy2, 16):
                                        wps[-1] = (wx2, wy2)
                                        j += 1
                                    else:
                                        break
                                i = j
                            bot["path"] = wps
                        else:
                            bot["path"] = []

                # ── MOVEMENT ────────────────────────────────────────────────
                desired_range = 240  # bots prefer to stay in this kiting band
                close_range = 130
                if has_los:
                    bot["path"] = []  # clear path; we can see the player
                    if dist > desired_range:
                        # approach
                        bot["x"] += dx/dist * bot["speed"] * dt * slow
                        bot["y"] += dy/dist * bot["speed"] * dt * slow
                    elif dist < close_range:
                        # back away a touch
                        bot["x"] -= dx/dist * bot["speed"] * 0.6 * dt * slow
                        bot["y"] -= dy/dist * bot["speed"] * 0.6 * dt * slow
                    else:
                        # strafe in band
                        sx = -dy/dist * bot["strafeBias"]
                        sy =  dx/dist * bot["strafeBias"]
                        bot["x"] += sx * bot["speed"] * 0.55 * dt * slow
                        bot["y"] += sy * bot["speed"] * 0.55 * dt * slow
                    push_out_of_walls(bot)
                elif bot["path"]:
                    wp = bot["path"][0]
                    wdx = wp[0] - bot["x"]; wdy = wp[1] - bot["y"]
                    wd = math.hypot(wdx, wdy) or 1
                    if wd < GRID_SIZE * 0.55:
                        bot["path"].pop(0)
                    else:
                        bot["x"] += wdx/wd * bot["speed"] * dt * slow
                        bot["y"] += wdy/wd * bot["speed"] * dt * slow
                        push_out_of_walls(bot)
                else:
                    # No LoS, no path — drift toward player as fallback
                    bot["x"] += dx/dist * bot["speed"] * 0.5 * dt * slow
                    bot["y"] += dy/dist * bot["speed"] * 0.5 * dt * slow
                    push_out_of_walls(bot)

                # ── STUCK DETECTION ─────────────────────────────────────────
                moved = math.hypot(bot["x"] - bot["lastX"], bot["y"] - bot["lastY"])
                if moved < 1.2 * dt * bot["speed"] * 0.3:
                    bot["stuckTimer"] += dt
                else:
                    bot["stuckTimer"] = 0
                if bot["stuckTimer"] > 0.6:
                    # force replan + flip strafe direction
                    bot["replanCd"] = 0
                    bot["path"] = []
                    bot["strafeBias"] *= -1
                    bot["stuckTimer"] = 0
                bot["lastX"] = bot["x"]; bot["lastY"] = bot["y"]

                # ── FIRING (only when LoS) ──────────────────────────────────
                bot["fireCooldown"] -= dt * slow
                if bot["fireCooldown"] <= 0 and dist < 540 and has_los:
                    bot["fireCooldown"] = bot["fireRate"]
                    spread = (random.random() - 0.5) * 0.3
                    self.spawn_bullet(
                        bot["x"] + math.cos(bot["angle"]) * 24,
                        bot["y"] + math.sin(bot["angle"]) * 24,
                        bot["angle"] + spread,
                        {"speed": 330 + random.random()*90, "damage": 8 + self.wave*1.8,
                         "color":"#ff3366", "owner":"bot", "r":5, "life":2.2}
                    )
            survivors.append(bot)
        self.bots = survivors

    def check_collisions(self):
        # player bullets vs bots (coop only generates bots)
        for b in list(self.bullets):
            if b["owner"] != "player": continue
            removed = False
            # bots
            if self.bots:
                for bot in list(self.bots):
                    if bot["id"] in b["hitIds"]: continue
                    if math.hypot(b["x"]-bot["x"], b["y"]-bot["y"]) >= b["r"] + bot["r"]: continue
                    self._apply_bullet_to_bot(b, bot)
                    if not b["piercing"]:
                        if b in self.bullets:
                            self.bullets.remove(b)
                        removed = True; break
                if removed: continue
            # players (PvP modes)
            if self.mode in ("ffa", "tdm"):
                for pl in list(self.players.values()):
                    if not pl["alive"]: continue
                    if pl["id"] == b.get("ownerId"): continue
                    if self.mode == "tdm" and pl["team"] == b.get("ownerTeam", -1): continue
                    if pl["invuln"] > 0: continue
                    if math.hypot(b["x"]-pl["x"], b["y"]-pl["y"]) >= b["r"] + pl["r"]: continue
                    # apply
                    src = self.players.get(b.get("ownerId"))
                    dmg = b["damage"]
                    if src and "lifesteal" in src["abilities"]:
                        src["hp"] = min(src["maxHp"], src["hp"] + 1)
                    if b["explosive"]:
                        self.explode_at(b["x"], b["y"], 105, b["damage"]*0.92,
                                        b.get("ownerId"), b.get("ownerTeam", -1))
                    self.damage_player(pl, dmg, b.get("ownerId"))
                    if not b["piercing"]:
                        if b in self.bullets: self.bullets.remove(b)
                        removed = True; break
                if removed: continue
        # bot bullets vs players
        for b in list(self.bullets):
            if b["owner"] != "bot": continue
            removed = False
            for pl in list(self.players.values()):
                if not pl["alive"] or pl["invuln"] > 0: continue
                if math.hypot(b["x"]-pl["x"], b["y"]-pl["y"]) < b["r"] + pl["r"]:
                    self.damage_player(pl, b["damage"], None)
                    if b in self.bullets: self.bullets.remove(b)
                    removed = True; break
            if removed: continue

    def _apply_bullet_to_bot(self, b, bot):
        bot["hp"] -= b["damage"]
        bot["flash"] = 1
        if b["flaming"]:
            bot["burning"] = 3.6
            bot["burnDmg"] = b["damage"] * 0.28
        if b["freezing"]:
            bot["frozen"] = 2.4
        if b["explosive"]:
            self.explode_at(b["x"], b["y"], 105, b["damage"]*0.92,
                            b.get("ownerId"), b.get("ownerTeam", -1))
        if b["toxic"]:
            bot["toxicTimer"] = 4
            bot["toxicDmg"] = 5
        if b["splitting"] and not b["hasSplit"]:
            b["hasSplit"] = True
            b_ang = math.atan2(b["vy"], b["vx"])
            spd = math.hypot(b["vx"], b["vy"]) * 0.75
            so = {"damage": b["damage"]*0.6, "color": b["color"], "speed": spd,
                  "r": b["r"]*0.8, "life":1.4, "owner":"player",
                  "ownerId": b.get("ownerId"), "ownerTeam": b.get("ownerTeam",-1),
                  "bounces": b["bounces"], "flaming": b["flaming"],
                  "freezing": b["freezing"], "toxic": b["toxic"], "explosive": b["explosive"]}
            self.spawn_bullet(b["x"], b["y"], b_ang + 0.42, so)
            self.spawn_bullet(b["x"], b["y"], b_ang - 0.42, so)
        # Chain lightning
        src = self.players.get(b.get("ownerId"))
        if src and "chain_lightning" in src["abilities"]:
            others = [o for o in self.bots if o["id"] != bot["id"]]
            others.sort(key=lambda o: math.hypot(o["x"]-bot["x"], o["y"]-bot["y"]))
            for t in others[:2]:
                if math.hypot(t["x"]-bot["x"], t["y"]-bot["y"]) > 200: continue
                t["hp"] -= b["damage"]*0.45
                t["flash"] = 1
                self.events.append({"k":"chain","x1":bot["x"],"y1":bot["y"],"x2":t["x"],"y2":t["y"]})
        if src and "lifesteal" in src["abilities"]:
            src["hp"] = min(src["maxHp"], src["hp"] + 1)
        if b["piercing"]:
            b["hitIds"].add(bot["id"])
        self.events.append({"k":"hit","x":bot["x"],"y":bot["y"],"d":int(b["damage"]),"c":b["color"]})

    def update_wave(self, dt):
        if self.mode != "coop" or self.phase != "playing":
            return
        if self.bots_to_spawn > 0:
            self.spawn_timer -= dt
            if self.spawn_timer <= 0:
                self.spawn_timer = 0.52
                self.bots_to_spawn -= 1
                # spawn away from any player
                candidates = [(x,y) for (x,y) in SPAWN_ZONES if all(
                    math.hypot(x-p["x"], y-p["y"]) > 220 for p in self.players.values() if p["alive"]
                )]
                if not candidates: candidates = SPAWN_ZONES
                x, y = random.choice(candidates)
                self.bots.append(make_bot(x + (random.random()-0.5)*16,
                                          y + (random.random()-0.5)*16, self.wave))
        if self.bots_to_spawn == 0 and not self.bots:
            # wave clear → cards for everyone alive
            self.phase = "card_select"
            # revive any dead players for next round
            for p in self.players.values():
                if not p["alive"]:
                    p["alive"] = True
                    p["hp"] = max(50, p["maxHp"] * 0.6)
                    x, y = spawn_pos_for(self, p["team"])
                    p["x"], p["y"] = x, y
                self.offer_cards(p["id"])

    def update_pvp_match(self, dt):
        if self.mode == "coop" or self.phase != "playing":
            return
        # win conditions
        if self.mode == "ffa":
            # First to target_score kills (or 100 score per kill = 30 kills)
            winners = [p for p in self.players.values() if p["kills"] >= self.target_score]
            if winners or self.t >= self.match_end_at:
                top = max(self.players.values(), key=lambda p: (p["kills"], -p["deaths"]))
                self.winner = {"type": "player", "id": top["id"], "name": top["name"]}
                self.phase = "game_over"
                self.events.append({"k":"matchEnd"})
        elif self.mode == "tdm":
            if max(self.team_scores) >= self.target_score or self.t >= self.match_end_at:
                t = 0 if self.team_scores[0] > self.team_scores[1] else 1
                if self.team_scores[0] == self.team_scores[1]: t = -1
                self.winner = {"type":"team", "team": t, "scores": self.team_scores}
                self.phase = "game_over"
                self.events.append({"k":"matchEnd"})
        # PvP card cadence
        if self.phase == "playing" and self.t >= self.next_card_at:
            self.next_card_at = self.t + 30.0
            if self.mode == "ffa":
                for pid in list(self.players.keys()):
                    p = self.players[pid]
                    if not p["pendingCards"]:
                        self.offer_cards(pid)
            elif self.mode == "tdm":
                # cards for the losing team only
                if self.team_scores[0] == self.team_scores[1]:
                    target_team = -1  # tie: offer to everyone
                else:
                    target_team = 0 if self.team_scores[0] < self.team_scores[1] else 1
                for pid, p in self.players.items():
                    if (target_team == -1 or p["team"] == target_team) and not p["pendingCards"]:
                        self.offer_cards(pid)

    def state_payload(self):
        return {
            "type": "state",
            "t": round(self.t, 3),
            "phase": self.phase,
            "mode": self.mode,
            "wave": self.wave,
            "remaining": len(self.bots) + self.bots_to_spawn,
            "code": self.code,
            "host": self.host_id,
            "teamScores": self.team_scores if self.mode == "tdm" else None,
            "matchTimeLeft": max(0, self.match_end_at - self.t) if self.mode in ("ffa","tdm") else None,
            "targetScore": self.target_score if self.mode in ("ffa","tdm") else None,
            "winner": self.winner,
            "players": [
                {
                    "id": p["id"], "name": p["name"], "color": p["color"], "team": p["team"],
                    "x": round(p["x"],1), "y": round(p["y"],1), "r": p["r"],
                    "angle": round(p["angle"],3),
                    "hp": round(p["hp"],1), "maxHp": p["maxHp"],
                    "alive": p["alive"], "invuln": round(p["invuln"],2),
                    "shield": p["shield"],
                    "dashCd": round(p["dashCooldown"],2),
                    "dashMaxCd": p["dashMaxCd"],
                    "kills": p["kills"], "deaths": p["deaths"], "score": p["score"],
                    "abilities": p["abilities"],
                    "picking": bool(p["pendingCards"]),
                }
                for p in self.players.values()
            ],
            "bullets": [
                {"x": round(b["x"],1), "y": round(b["y"],1), "r": b["r"], "c": b["color"]}
                for b in self.bullets
            ],
            "bots": [
                {"id": bt["id"], "x": round(bt["x"],1), "y": round(bt["y"],1),
                 "r": bt["r"], "hp": round(bt["hp"],1), "maxHp": bt["maxHp"],
                 "angle": round(bt["angle"],2), "frozen": 1 if bt["frozen"] > 0 else 0,
                 "color": bt["color"]}
                for bt in self.bots
            ],
            "events": self.events,
        }

    def tick(self, dt):
        self.t += dt
        self.bullet_time = max(0, self.bullet_time - dt)
        if self.phase == "playing":
            self.update_players(dt)
            self.update_bullets(dt)
            self.update_bots(dt)
            self.check_collisions()
            self.update_wave(dt)
            self.update_pvp_match(dt)
        elif self.phase == "card_select":
            # still let bullets/particles finish, but no new wave action
            self.update_players(dt)
            self.update_bullets(dt)
            self.check_collisions()

    async def run_loop(self):
        last = time.monotonic()
        while True:
            await asyncio.sleep(TICK_DT)
            now = time.monotonic()
            dt = min(0.05, now - last)
            last = now
            try:
                self.tick(dt)
                payload = self.state_payload()
                self.events = []
                await self.broadcast(payload)
            except Exception as e:
                print("tick error:", e)
            if not self.players:
                # room empty — exit
                rooms.pop(self.code, None)
                return

# ─────────────────────────────────────────────────────────────────────────────
# ROOMS DICT / CONNECTION HANDLER
# ─────────────────────────────────────────────────────────────────────────────
rooms: dict[str, Room] = {}
_pid_seq = 0
def next_pid():
    global _pid_seq
    _pid_seq += 1
    return _pid_seq

async def handle_ws(ws: WSClient):
    pid = next_pid()
    room: Optional[Room] = None
    name = "Player"
    try:
        await ws.send({"type":"hello","pid":pid})
        while ws.alive:
            msg = await ws.recv()
            if msg is None: break
            t = msg.get("type")
            if t == "create":
                if room:
                    await ws.send({"type":"error","msg":"already in a room"}); continue
                mode = msg.get("mode","coop")
                if mode not in ("coop","ffa","tdm"):
                    await ws.send({"type":"error","msg":"unknown mode"}); continue
                code = gen_code(rooms.keys())
                room = Room(code, mode, pid)
                rooms[code] = room
                name = msg.get("name","Player")
                room.add_player(pid, name)
                room.clients[pid] = ws
                room.tick_task = asyncio.create_task(room.run_loop())
                await ws.send({"type":"joined","code":code,"pid":pid,"mode":mode,"host":pid})
                await room.broadcast(room.lobby_payload())
            elif t == "join":
                if room:
                    await ws.send({"type":"error","msg":"already in a room"}); continue
                code = (msg.get("code") or "").upper().strip()
                r = rooms.get(code)
                if not r:
                    await ws.send({"type":"error","msg":"no such room"}); continue
                if r.is_full():
                    await ws.send({"type":"error","msg":"room full"}); continue
                if r.phase != "lobby":
                    await ws.send({"type":"error","msg":"game already in progress"}); continue
                name = msg.get("name","Player")
                r.add_player(pid, name)
                r.clients[pid] = ws
                room = r
                await ws.send({"type":"joined","code":r.code,"pid":pid,"mode":r.mode,"host":r.host_id})
                await room.broadcast(room.lobby_payload())
            elif t == "start":
                if not room: continue
                if pid != room.host_id: continue
                if room.phase != "lobby": continue
                room.start_match()
            elif t == "input":
                if not room: continue
                p = room.players.get(pid)
                if not p: continue
                p["keys"] = set(msg.get("keys") or [])
                if "mx" in msg: p["mx"] = float(msg["mx"])
                if "my" in msg: p["my"] = float(msg["my"])
                p["firing"] = bool(msg.get("fire"))
                if msg.get("dash"): p["dashQueued"] = True
            elif t == "pickCard":
                if not room: continue
                room.pick_card(pid, int(msg.get("idx", -1)))
            elif t == "leave":
                break
            elif t == "switchTeam":
                if not room or room.mode != "tdm" or room.phase != "lobby": continue
                p = room.players.get(pid)
                if p:
                    p["team"] = 1 - p["team"]
                    await room.broadcast(room.lobby_payload())
            elif t == "rematch":
                if not room: continue
                if pid != room.host_id: continue
                if room.phase != "game_over": continue
                room.start_match()
    except Exception as e:
        print("ws handle error:", e)
    finally:
        if room:
            room.remove_player(pid)
            await room.broadcast(room.lobby_payload())
        await ws.close()

# ─────────────────────────────────────────────────────────────────────────────
# TCP CONNECTION DISPATCH (HTTP / WS upgrade)
# ─────────────────────────────────────────────────────────────────────────────
async def handle_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        request_line_b = await reader.readline()
        if not request_line_b: return
        request_line = request_line_b.decode("latin-1").rstrip("\r\n")
        parts = request_line.split(" ")
        if len(parts) < 2:
            return
        method, path = parts[0], parts[1]
        headers = {}
        while True:
            line = await reader.readline()
            if not line or line in (b"\r\n", b"\n"): break
            try:
                k, _, v = line.decode("latin-1").rstrip("\r\n").partition(":")
                headers[k.strip().lower()] = v.strip()
            except Exception:
                pass
        if headers.get("upgrade","").lower() == "websocket" and (path.startswith("/ws")):
            ws = await ws_handshake(reader, writer, headers)
            await handle_ws(ws)
            return
        if method == "GET":
            await serve_static(writer, path)
        else:
            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
    except Exception as e:
        # connection errors are fine; print for visibility
        if not isinstance(e, (ConnectionError, asyncio.IncompleteReadError)):
            print("conn error:", e)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

async def main():
    server = await asyncio.start_server(handle_conn, host="0.0.0.0", port=PORT)
    print(f"Game running at http://localhost:{PORT}")
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
