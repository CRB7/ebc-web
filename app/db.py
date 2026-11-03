"""Stockage SQLite : programmes, tests, mesures, résultats d'étapes."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS programs (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    steps TEXT NOT NULL,
    builtin INTEGER DEFAULT 0,
    created REAL
);
CREATE TABLE IF NOT EXISTS tests (
    id INTEGER PRIMARY KEY,
    name TEXT,
    battery TEXT,
    notes TEXT,
    program TEXT,
    started REAL,
    ended REAL,
    status TEXT,
    summary TEXT
);
CREATE TABLE IF NOT EXISTS samples (
    test_id INTEGER,
    t REAL,          -- secondes depuis le début du test
    step INTEGER,
    v REAL, i REAL,
    cap REAL,        -- Ah intégrés sur l'étape (signe : + charge / + décharge)
    wh REAL,
    mode INTEGER
);
CREATE INDEX IF NOT EXISTS samples_test ON samples(test_id, t);
CREATE TABLE IF NOT EXISTS step_results (
    test_id INTEGER,
    step INTEGER,
    type TEXT,
    params TEXT,
    t_start REAL, t_end REAL,
    capacity_ah REAL, energy_wh REAL,
    v_start REAL, v_end REAL, v_avg REAL,
    ir_mohm REAL,
    end_reason TEXT
);
"""


class DB:
    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.c = sqlite3.connect(str(path), check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.c.execute("PRAGMA journal_mode=WAL")
            self.c.executescript(SCHEMA)
            self.c.commit()

    def _q(self, sql: str, args=(), commit=False):
        with self.lock:
            cur = self.c.execute(sql, args)
            if commit:
                self.c.commit()
            return cur

    # programmes -------------------------------------------------------
    def list_programs(self):
        rows = self._q("SELECT * FROM programs ORDER BY builtin DESC, name").fetchall()
        return [dict(r, steps=json.loads(r["steps"])) for r in rows]

    def get_program(self, pid: int):
        r = self._q("SELECT * FROM programs WHERE id=?", (pid,)).fetchone()
        return dict(r, steps=json.loads(r["steps"])) if r else None

    def save_program(self, name: str, steps: list, pid: int | None = None, builtin=False) -> int:
        if pid:
            self._q("UPDATE programs SET name=?, steps=? WHERE id=? AND builtin=0",
                    (name, json.dumps(steps), pid), commit=True)
            return pid
        cur = self._q("INSERT INTO programs(name, steps, builtin, created) VALUES (?,?,?,?)",
                      (name, json.dumps(steps), int(builtin), time.time()), commit=True)
        return cur.lastrowid

    def delete_program(self, pid: int) -> None:
        self._q("DELETE FROM programs WHERE id=? AND builtin=0", (pid,), commit=True)

    def seed_programs(self, presets: list[tuple[str, list]]) -> None:
        if self._q("SELECT COUNT(*) FROM programs WHERE builtin=1").fetchone()[0]:
            return
        for name, steps in presets:
            self.save_program(name, steps, builtin=True)

    # tests ------------------------------------------------------------
    def create_test(self, name, battery, notes, program) -> int:
        cur = self._q(
            "INSERT INTO tests(name, battery, notes, program, started, status) VALUES (?,?,?,?,?,?)",
            (name, battery, notes, json.dumps(program), time.time(), "running"), commit=True)
        return cur.lastrowid

    def finish_test(self, tid: int, status: str, summary: dict) -> None:
        self._q("UPDATE tests SET ended=?, status=?, summary=? WHERE id=?",
                (time.time(), status, json.dumps(summary), tid), commit=True)

    def update_test(self, tid: int, name: str, battery: str, notes: str) -> None:
        self._q("UPDATE tests SET name=?, battery=?, notes=? WHERE id=?",
                (name, battery, notes, tid), commit=True)

    def delete_test(self, tid: int) -> None:
        with self.lock:
            for t in ("samples", "step_results", "tests"):
                col = "id" if t == "tests" else "test_id"
                self.c.execute(f"DELETE FROM {t} WHERE {col}=?", (tid,))
            self.c.commit()

    def _test_row(self, r):
        d = dict(r)
        d["program"] = json.loads(d["program"] or "{}")
        d["summary"] = json.loads(d["summary"] or "{}")
        return d

    def list_tests(self):
        rows = self._q("SELECT * FROM tests ORDER BY started DESC").fetchall()
        return [self._test_row(r) for r in rows]

    def get_test(self, tid: int):
        r = self._q("SELECT * FROM tests WHERE id=?", (tid,)).fetchone()
        if not r:
            return None
        d = self._test_row(r)
        d["steps"] = [dict(x, params=json.loads(x["params"])) for x in self._q(
            "SELECT * FROM step_results WHERE test_id=? ORDER BY t_start", (tid,)).fetchall()]
        return d

    def mark_interrupted(self) -> None:
        """Au démarrage : un test resté 'running' a été coupé (reboot, crash)."""
        self._q("UPDATE tests SET status='interrupted', ended=? WHERE status='running'",
                (time.time(),), commit=True)

    # mesures ----------------------------------------------------------
    def add_sample(self, tid, t, step, v, i, cap, wh, mode) -> None:
        self._q("INSERT INTO samples VALUES (?,?,?,?,?,?,?,?)",
                (tid, t, step, v, i, cap, wh, mode), commit=True)

    def samples(self, tid: int, max_points: int | None = None):
        rows = self._q("SELECT t, step, v, i, cap, wh, mode FROM samples WHERE test_id=? ORDER BY t",
                       (tid,)).fetchall()
        if max_points and len(rows) > max_points:
            k = len(rows) / max_points
            rows = [rows[int(n * k)] for n in range(max_points)] + [rows[-1]]
        return [dict(r) for r in rows]

    def add_step_result(self, tid, step, typ, params, res: dict) -> None:
        self._q("INSERT INTO step_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            tid, step, typ, json.dumps(params), res["t_start"], res["t_end"],
            res["capacity_ah"], res["energy_wh"], res["v_start"], res["v_end"],
            res["v_avg"], res.get("ir_mohm"), res["end_reason"]), commit=True)
