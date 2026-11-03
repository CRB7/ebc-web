import time

from app.db import DB
from app.device import SimDevice
from app.engine import Engine, validate_steps, StepError
import pytest


def run_program(tmp_path, steps, cap=2.6):
    db = DB(tmp_path / "t.db")
    events = []
    eng = Engine(lambda: SimDevice(speed=2000, tick=0.005, capacity_ah=cap), db, events.append)
    eng.start()
    for _ in range(200):
        if eng.connected:
            break
        time.sleep(0.02)
    assert eng.connected and eng.model == "EBC-A20"
    tid = eng.submit("start", steps=steps, name="t", battery="cellule A", notes="", program_name="p")
    for _ in range(3000):
        if eng.run_ is None:
            break
        time.sleep(0.01)
    assert eng.run_ is None, list(eng.log)[-10:]
    return db.get_test(tid), db, tid, eng


def test_capacity_program(tmp_path):
    steps = [
        {"type": "charge", "current": 2.0, "voltage": 4.2, "cutoff": 0.1},
        {"type": "rest", "minutes": 10},
        {"type": "discharge_cc", "current": 2.0, "cutoff_v": 2.8},
        {"type": "rest", "minutes": 5},
        {"type": "loop", "goto": 1, "times": 1},
    ]
    t, db, tid, eng = run_program(tmp_path, steps)
    assert t["status"] == "done", list(eng.log)
    types = [s["type"] for s in t["steps"]]
    assert types == ["charge", "rest", "discharge_cc", "rest"] * 2
    s = t["summary"]
    assert 2.0 < s["capacity_ah"] < 2.8, s
    assert s["ir_mohm"] and 20 < s["ir_mohm"] < 150, s
    assert s["n_discharges"] == 2
    assert len(db.samples(tid)) > 50


def test_cp_and_cr(tmp_path):
    steps = [
        {"type": "discharge_cp", "power": 5, "cutoff_v": 3.0, "max_min": 30},
        {"type": "discharge_cr", "resistance": 3.0, "cutoff_v": 3.0, "max_min": 20},
    ]
    t, *_ = run_program(tmp_path, steps)
    reasons = [s["end_reason"] for s in t["steps"]]
    assert reasons[0] in ("fin signalée par l'EBC", "durée max atteinte")
    assert len(t["steps"]) == 2


def test_validation():
    with pytest.raises(StepError):
        validate_steps([{"type": "charge", "current": 9, "voltage": 4.2, "cutoff": 0.1}])
    with pytest.raises(StepError):
        validate_steps([{"type": "loop", "goto": 1, "times": 1}])
