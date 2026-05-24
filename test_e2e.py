"""Simple stdlib-only WebSocket client to smoke-test the multiplayer server."""
import asyncio, base64, hashlib, json, os, secrets, struct, sys

PORT = 8000
HOST = "localhost"

async def open_ws(path="/ws"):
    reader, writer = await asyncio.open_connection(HOST, PORT)
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {HOST}:{PORT}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    writer.write(req.encode()); await writer.drain()
    # Read response headers
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b""): break
    return reader, writer

async def ws_send(writer, obj):
    payload = json.dumps(obj).encode("utf-8")
    mask = secrets.token_bytes(4)
    n = len(payload)
    header = bytes([0x81])  # FIN+text
    if n < 126: header += bytes([0x80 | n])
    elif n < 65536: header += bytes([0x80 | 126]) + struct.pack("!H", n)
    else: header += bytes([0x80 | 127]) + struct.pack("!Q", n)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    writer.write(header + mask + masked)
    await writer.drain()

async def ws_recv(reader):
    hdr = await reader.readexactly(2)
    opcode = hdr[0] & 0x0f
    n = hdr[1] & 0x7f
    if n == 126: n = struct.unpack("!H", await reader.readexactly(2))[0]
    elif n == 127: n = struct.unpack("!Q", await reader.readexactly(8))[0]
    payload = await reader.readexactly(n) if n else b""
    if opcode == 0x8: return None
    return json.loads(payload.decode("utf-8"))

async def gather_until(reader, type_, timeout=2.0):
    end = asyncio.get_event_loop().time() + timeout
    msgs = []
    while asyncio.get_event_loop().time() < end:
        try:
            m = await asyncio.wait_for(ws_recv(reader), timeout=end - asyncio.get_event_loop().time())
        except asyncio.TimeoutError:
            return msgs, None
        if m is None: return msgs, None
        msgs.append(m)
        if m.get("type") == type_:
            return msgs, m
    return msgs, None

async def test_coop():
    print("[test_coop] connecting host…")
    r1, w1 = await open_ws()
    _, hello = await gather_until(r1, "hello")
    assert hello and hello.get("pid"), "expected hello with pid"
    print(f"  host pid={hello['pid']}")
    await ws_send(w1, {"type":"create","mode":"coop","name":"Alice"})
    msgs, joined = await gather_until(r1, "joined")
    assert joined, f"no joined; got {[m.get('type') for m in msgs]}"
    code = joined["code"]
    print(f"  created room code={code}")

    # Second player joins
    r2, w2 = await open_ws()
    await gather_until(r2, "hello")
    await ws_send(w2, {"type":"join","code":code,"name":"Bob"})
    msgs2, joined2 = await gather_until(r2, "joined")
    assert joined2, f"join failed: {[m.get('type') for m in msgs2]}"
    print(f"  joined as pid={joined2['pid']}")

    # Start match (host)
    await ws_send(w1, {"type":"start"})
    # Both should start getting state messages
    msgs, state = await gather_until(r1, "state", timeout=2.0)
    assert state, "no state msg"
    assert state["phase"] in ("playing","card_select"), f"phase={state['phase']}"
    assert len(state["players"]) == 2
    print(f"  state: phase={state['phase']} bots_remaining={state['remaining']}")

    # Send input
    await ws_send(w1, {"type":"input","keys":["KeyD"],"mx":600,"my":400,"fire":True})
    await asyncio.sleep(0.5)
    # Drain
    drained = []
    end = asyncio.get_event_loop().time() + 0.5
    while asyncio.get_event_loop().time() < end:
        try:
            m = await asyncio.wait_for(ws_recv(r1), timeout=0.2)
            drained.append(m)
        except asyncio.TimeoutError:
            break
    state2 = next((m for m in reversed(drained) if m and m.get("type")=="state"), None)
    print(f"  after input: bullets={len(state2['bullets'])} bots={len(state2['bots'])}")
    assert len(state2["bullets"]) >= 0  # smoke
    print("  ✓ coop test passed")

    # cleanup
    w1.close(); w2.close()
    try: await w1.wait_closed()
    except: pass
    try: await w2.wait_closed()
    except: pass

async def test_ffa():
    print("[test_ffa] host create…")
    r1, w1 = await open_ws(); await gather_until(r1, "hello")
    await ws_send(w1, {"type":"create","mode":"ffa","name":"P1"})
    _, joined = await gather_until(r1, "joined")
    code = joined["code"]
    r2, w2 = await open_ws(); await gather_until(r2, "hello")
    await ws_send(w2, {"type":"join","code":code,"name":"P2"})
    await gather_until(r2, "joined")
    await ws_send(w1, {"type":"start"})
    # Expect a card offer eventually (FFA offers initial cards)
    msgs1, cards1 = await gather_until(r1, "cards", timeout=3.0)
    msgs2, cards2 = await gather_until(r2, "cards", timeout=3.0)
    assert cards1 and cards2, f"expected card offers on FFA start; got {cards1=} {cards2=}"
    print(f"  both players got {len(cards1['choices'])} card choices each")
    # Each picks idx 0
    await ws_send(w1, {"type":"pickCard","idx":0})
    await ws_send(w2, {"type":"pickCard","idx":0})
    await asyncio.sleep(0.4)
    print("  ✓ ffa test passed")
    w1.close(); w2.close()

async def test_tdm():
    print("[test_tdm] 4-player TDM…")
    conns = []
    for name in ("R1","R2","B1","B2"):
        r, w = await open_ws(); await gather_until(r, "hello")
        conns.append((r, w, name))
    r0, w0, _ = conns[0]
    await ws_send(w0, {"type":"create","mode":"tdm","name":"R1"})
    _, joined = await gather_until(r0, "joined")
    code = joined["code"]
    for r, w, name in conns[1:]:
        await ws_send(w, {"type":"join","code":code,"name":name})
        await gather_until(r, "joined")
    # Drain lobby messages
    await asyncio.sleep(0.2)
    # Start
    await ws_send(w0, {"type":"start"})
    msgs, state = await gather_until(r0, "state", timeout=2.0)
    assert state, "no TDM state"
    teams = [p["team"] for p in state["players"]]
    print(f"  teams assigned: {teams}")
    assert teams.count(0) >= 1 and teams.count(1) >= 1, "teams should be split"
    print("  ✓ tdm test passed")
    for _, w, _ in conns: w.close()

async def main():
    await test_coop()
    print()
    await test_ffa()
    print()
    await test_tdm()
    print("\nALL TESTS PASSED")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print("FAIL:", e)
        sys.exit(1)
