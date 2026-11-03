"""Serveur web EBC-A20 (FastAPI)."""
from __future__ import annotations

import asyncio
import csv
import io
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .db import DB
from .device import SerialDevice, SimDevice
from .engine import LIMITS, PRESETS, STEP_TYPES, Engine, StepError, validate_steps

PORT = os.environ.get("EBC_PORT", "/dev/ttyUSB0")
DB_PATH = os.environ.get("EBC_DB", "/data/ebc.db")
SIM_SPEED = float(os.environ.get("EBC_SIM_SPEED", "20"))
STATIC = Path(__file__).parent / "static"

db = DB(DB_PATH)
db.mark_interrupted()
db.seed_programs(PRESETS)

clients: set[asyncio.Queue] = set()
loop: asyncio.AbstractEventLoop | None = None


def publish(ev: dict) -> None:
    if loop is None:
        return
    def push():
        for q in list(clients):
            if q.qsize() < 500:
                q.put_nowait(ev)
    loop.call_soon_threadsafe(push)


def device_factory():
    if PORT == "sim":
        return SimDevice(speed=SIM_SPEED)
    return SerialDevice(PORT)


engine = Engine(device_factory, db, publish)


@asynccontextmanager
async def lifespan(_app):
    global loop
    loop = asyncio.get_running_loop()
    engine.start()
    yield


app = FastAPI(title="EBC-A20 Web", lifespan=lifespan)


# ------------------------------------------------------------------ modèles
class ProgramIn(BaseModel):
    name: str
    steps: list[dict]


class RunIn(BaseModel):
    name: str = ""
    battery: str = ""
    notes: str = ""
    program_id: int | None = None
    steps: list[dict] | None = None


class TestPatch(BaseModel):
    name: str
    battery: str
    notes: str = ""


def _err(e: Exception):
    raise HTTPException(400, str(e))


# ------------------------------------------------------------------ état
@app.get("/api/status")
def status():
    return engine.status()


@app.get("/api/meta")
def meta():
    return {"step_types": STEP_TYPES, "limits": LIMITS, "port": PORT,
            "simulator": PORT == "sim"}


@app.get("/api/log")
def log():
    return list(engine.log)


@app.post("/api/run")
def run(body: RunIn):
    steps, pname = body.steps, "Personnalisé"
    if body.program_id:
        prog = db.get_program(body.program_id)
        if not prog:
            raise HTTPException(404, "programme introuvable")
        steps, pname = prog["steps"], prog["name"]
    if not steps:
        raise HTTPException(400, "aucune étape")
    name = body.name or pname
    try:
        tid = engine.submit("start", steps=steps, name=name, battery=body.battery.strip(),
                            notes=body.notes, program_name=pname)
    except StepError as e:
        _err(e)
    return {"test_id": tid}


@app.post("/api/stop")
def stop():
    engine.submit("stop")
    return {"ok": True}


@app.post("/api/skip")
def skip():
    engine.submit("skip")
    return {"ok": True}


@app.post("/api/reconnect")
def reconnect():
    engine.submit("reconnect")
    return {"ok": True}


# ------------------------------------------------------------------ programmes
@app.get("/api/programs")
def programs():
    return db.list_programs()


@app.post("/api/programs")
def create_program(body: ProgramIn):
    try:
        steps = validate_steps(body.steps)
    except StepError as e:
        _err(e)
    return {"id": db.save_program(body.name.strip() or "Sans nom", steps)}


@app.put("/api/programs/{pid}")
def update_program(pid: int, body: ProgramIn):
    try:
        steps = validate_steps(body.steps)
    except StepError as e:
        _err(e)
    db.save_program(body.name.strip() or "Sans nom", steps, pid)
    return {"id": pid}


@app.delete("/api/programs/{pid}")
def delete_program(pid: int):
    db.delete_program(pid)
    return {"ok": True}


# ------------------------------------------------------------------ historique
@app.get("/api/tests")
def tests():
    return db.list_tests()


@app.get("/api/tests/{tid}")
def test(tid: int):
    t = db.get_test(tid)
    if not t:
        raise HTTPException(404)
    return t


@app.patch("/api/tests/{tid}")
def patch_test(tid: int, body: TestPatch):
    db.update_test(tid, body.name, body.battery.strip(), body.notes)
    return {"ok": True}


@app.delete("/api/tests/{tid}")
def delete_test(tid: int):
    if engine.run_ and engine.run_.test_id == tid:
        raise HTTPException(400, "test en cours")
    db.delete_test(tid)
    return {"ok": True}


@app.get("/api/tests/{tid}/samples")
def test_samples(tid: int, max: int = 3000):
    return db.samples(tid, max)


@app.get("/api/tests/{tid}/csv")
def test_csv(tid: int):
    t = db.get_test(tid)
    if not t:
        raise HTTPException(404)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["temps_s", "etape", "type", "tension_V", "courant_A", "capacite_etape_Ah",
                "energie_etape_Wh", "mode"])
    types = {s["step"]: s["type"] for s in t["steps"]}
    steps = t["program"].get("steps", [])
    for r in db.samples(tid):
        typ = types.get(r["step"]) or (steps[r["step"]]["type"] if r["step"] < len(steps) else "")
        w.writerow([r["t"], r["step"] + 1, typ, f"{r['v']:.3f}", f"{r['i']:.2f}",
                    f"{r['cap']:.4f}", f"{r['wh']:.4f}", r["mode"]])
    safe = "".join(c if c.isalnum() else "_" for c in (t["battery"] or t["name"] or "test"))
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="test{tid}_{safe}.csv"'})


# ------------------------------------------------------------------ comparaison
@app.get("/api/batteries")
def batteries():
    """Une ligne par étiquette de batterie : dernier test complet."""
    out: dict[str, dict] = {}
    for t in db.list_tests():
        label = t["battery"] or "(sans nom)"
        s = t["summary"] or {}
        if not s.get("capacity_ah"):
            continue
        b = out.setdefault(label, {"battery": label, "tests": 0, "best_ah": 0})
        b["tests"] += 1
        b["best_ah"] = max(b["best_ah"], s["capacity_ah"])
        if "last" not in b:  # list_tests est trié du plus récent au plus ancien
            b["last"] = {"test_id": t["id"], "date": t["started"], **s}
    return sorted(out.values(), key=lambda b: -(b["last"]["capacity_ah"]))


@app.get("/api/compare")
def compare(ids: str):
    res = []
    for tid in [int(x) for x in ids.split(",") if x.strip().isdigit()][:8]:
        t = db.get_test(tid)
        if not t:
            continue
        dis = [s for s in t["steps"] if s["type"].startswith("discharge") and s["capacity_ah"] > 0]
        curve = []
        if dis:
            ref = dis[-1]["step"]
            pts = [r for r in db.samples(tid) if r["step"] == ref and r["i"] > 0]
            k = max(1, len(pts) // 400)
            curve = [[round(r["cap"], 4), r["v"]] for r in pts[::k]]
        res.append({"id": tid, "name": t["name"], "battery": t["battery"],
                    "started": t["started"], "summary": t["summary"], "curve": curve})
    return res


# ------------------------------------------------------------------ temps réel
@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    q: asyncio.Queue = asyncio.Queue()
    clients.add(q)
    try:
        await sock.send_json({"type": "status", "status": engine.status()})
        while True:
            ev = await q.get()
            await sock.send_json(ev)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        clients.discard(q)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
