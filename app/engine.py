"""Moteur : possède le port série, exécute les programmes multi-étapes,
enregistre les mesures et publie l'état en direct."""
from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from . import protocol as p
from .db import DB

# Limites EBC-A20 (fiche ZKETECH ; à confirmer sur ton appareil)
LIMITS = {
    "charge_current": (0.1, 5.0),
    "charge_voltage": (0.5, 18.0),
    "charge_cutoff": (0.01, 5.0),
    "discharge_current": (0.1, 20.0),
    "discharge_power": (1, 85),
    "cutoff_v": (0.0, 30.0),
}

STEP_TYPES = {
    "charge": "Charge CC-CV",
    "discharge_cc": "Décharge CC",
    "discharge_cp": "Décharge CP",
    "discharge_cr": "Décharge CR (logiciel)",
    "rest": "Pause",
    "loop": "Boucle",
}


class StepError(ValueError):
    pass


def _rng(name, v, key):
    lo, hi = LIMITS[key]
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise StepError(f"{name} : valeur manquante")
    if not lo <= v <= hi:
        raise StepError(f"{name} = {v} hors limites ({lo}–{hi})")
    return v


def validate_steps(steps: list[dict]) -> list[dict]:
    if not steps:
        raise StepError("programme vide")
    out = []
    for n, s in enumerate(steps, 1):
        t = s.get("type")
        if t not in STEP_TYPES:
            raise StepError(f"étape {n} : type inconnu {t!r}")
        mx = float(s.get("max_min") or 0)
        if t == "charge":
            out.append({"type": t,
                        "current": _rng(f"étape {n} courant", s.get("current"), "charge_current"),
                        "voltage": _rng(f"étape {n} tension", s.get("voltage"), "charge_voltage"),
                        "cutoff": _rng(f"étape {n} courant de fin", s.get("cutoff"), "charge_cutoff"),
                        "max_min": mx, "max_ah": float(s.get("max_ah") or 0)})
        elif t == "discharge_cc":
            out.append({"type": t,
                        "current": _rng(f"étape {n} courant", s.get("current"), "discharge_current"),
                        "cutoff_v": _rng(f"étape {n} tension de coupure", s.get("cutoff_v"), "cutoff_v"),
                        "max_min": mx})
        elif t == "discharge_cp":
            out.append({"type": t,
                        "power": _rng(f"étape {n} puissance", s.get("power"), "discharge_power"),
                        "cutoff_v": _rng(f"étape {n} tension de coupure", s.get("cutoff_v"), "cutoff_v"),
                        "max_min": mx})
        elif t == "discharge_cr":
            r = float(s.get("resistance") or 0)
            if r <= 0:
                raise StepError(f"étape {n} : résistance > 0 requise")
            out.append({"type": t, "resistance": r,
                        "cutoff_v": _rng(f"étape {n} tension de coupure", s.get("cutoff_v"), "cutoff_v"),
                        "max_min": mx})
        elif t == "rest":
            m = float(s.get("minutes") or 0)
            if m <= 0:
                raise StepError(f"étape {n} : durée de pause > 0 requise")
            out.append({"type": t, "minutes": m})
        elif t == "loop":
            g, k = int(s.get("goto") or 0), int(s.get("times") or 0)
            if not 1 <= g < n:
                raise StepError(f"étape {n} : la boucle doit revenir à une étape précédente")
            if k < 1:
                raise StepError(f"étape {n} : nombre de répétitions >= 1")
            out.append({"type": t, "goto": g, "times": k})
    return out


PRESETS = [
    ("Capacité Li-ion 18650 (1 A)", [
        {"type": "charge", "current": 1.0, "voltage": 4.2, "cutoff": 0.1},
        {"type": "rest", "minutes": 30},
        {"type": "discharge_cc", "current": 1.0, "cutoff_v": 2.8},
        {"type": "rest", "minutes": 30},
        {"type": "charge", "current": 1.0, "voltage": 3.8, "cutoff": 0.2},
    ]),
    ("Capacité LiFePO4 (1 A)", [
        {"type": "charge", "current": 1.0, "voltage": 3.65, "cutoff": 0.1},
        {"type": "rest", "minutes": 30},
        {"type": "discharge_cc", "current": 1.0, "cutoff_v": 2.5},
        {"type": "rest", "minutes": 30},
        {"type": "charge", "current": 1.0, "voltage": 3.3, "cutoff": 0.2},
    ]),
    ("3 cycles Li-ion", [
        {"type": "charge", "current": 1.0, "voltage": 4.2, "cutoff": 0.1},
        {"type": "rest", "minutes": 20},
        {"type": "discharge_cc", "current": 1.0, "cutoff_v": 2.8},
        {"type": "rest", "minutes": 20},
        {"type": "loop", "goto": 1, "times": 2},
        {"type": "charge", "current": 1.0, "voltage": 3.8, "cutoff": 0.2},
    ]),
    ("Décharge seule 1 A → 2,8 V", [
        {"type": "discharge_cc", "current": 1.0, "cutoff_v": 2.8},
    ]),
]


@dataclass
class StepRun:
    index: int
    spec: dict
    t_start: float
    confirmed: bool = False
    sent_at: float = 0.0
    retries: int = 0
    cap: float = 0.0
    wh: float = 0.0
    n: int = 0
    v_start: float | None = None
    v_sum: float = 0.0
    v_last: float = 0.0
    v_rest: float | None = None
    ir_mohm: float | None = None
    low_count: int = 0
    zero_count: int = 0
    cr_count: int = 0


@dataclass
class Run:
    test_id: int
    name: str
    battery: str
    steps: list[dict]
    t0: float
    step: StepRun | None = None
    loops: dict = field(default_factory=dict)
    results: list[dict] = field(default_factory=list)
    last_t: float | None = None


class Engine(threading.Thread):
    def __init__(self, device_factory: Callable, db: DB, publish: Callable[[dict], None]):
        super().__init__(daemon=True, name="ebc-engine")
        self.device_factory = device_factory
        self.db = db
        self.publish = publish
        self.cmds: queue.Queue = queue.Queue()
        self.dev = None
        self.connected = False
        self.model = None
        self.last_frame: p.Frame | None = None
        self.last_frame_t = 0.0
        self.run_: Run | None = None
        self.log: deque = deque(maxlen=200)
        self.error: str | None = None
        self.bad_frames = 0

    # ---------------------------------------------------------- API (threads web)
    def submit(self, kind: str, **kw):
        ev = threading.Event()
        box = {"ev": ev}
        self.cmds.put((kind, kw, box))
        ev.wait(5)
        if "error" in box:
            raise StepError(box["error"])
        return box.get("result")

    def status(self) -> dict:
        f = self.last_frame
        r = self.run_
        st = {
            "connected": self.connected,
            "model": self.model,
            "error": self.error,
            "age": round(time.monotonic() - self.last_frame_t, 1) if f else None,
            "frame": None,
            "run": None,
        }
        if f:
            st["frame"] = {"v": f.voltage, "i": f.current, "p": round(f.voltage * f.current, 3),
                           "cap_dev": f.capacity, "mode": f.mode, "mode_label": f.mode_label,
                           "active": f.state == p.State.ACTIVE}
        if r:
            s = r.step
            now = self.dev.clock() if self.dev else time.monotonic()
            st["run"] = {
                "test_id": r.test_id, "name": r.name, "battery": r.battery,
                "steps": r.steps, "elapsed": round(now - r.t0),
                "step": None if not s else {
                    "index": s.index, "type": s.spec["type"],
                    "label": STEP_TYPES[s.spec["type"]],
                    "elapsed": round(now - s.t_start), "cap": round(s.cap, 4),
                    "wh": round(s.wh, 4), "ir_mohm": s.ir_mohm,
                },
                "results": r.results,
            }
        return st

    def _log(self, msg: str) -> None:
        line = time.strftime("%H:%M:%S ") + msg
        self.log.append(line)
        self.publish({"type": "log", "line": line})

    # ---------------------------------------------------------- boucle
    def run(self) -> None:
        while True:
            try:
                self._loop_once()
            except Exception as e:  # ne jamais laisser mourir le thread
                self.error = f"{type(e).__name__}: {e}"
                self._log("Erreur : " + self.error)
                self._drop_device()
                time.sleep(3)

    def _drop_device(self) -> None:
        if self.dev:
            try:
                self.dev.close()
            except Exception:
                pass
        self.dev = None
        self.connected = False

    def _loop_once(self) -> None:
        self._process_cmds()
        if self.dev is None:
            try:
                self.dev = self.device_factory()
                self.dev.open()
                self.dev.send(p.cmd_connect())
                self.last_frame_t = time.monotonic()
                self.error = None
                self._log(f"Port ouvert ({getattr(self.dev, 'port', 'simulateur')}), connexion…")
            except Exception as e:
                self.error = f"Port série indisponible : {e}"
                self.dev = None
                self.publish({"type": "status", "status": self.status()})
                time.sleep(3)
                return
        frames = self.dev.read_frames()
        now_m = time.monotonic()
        for f in frames:
            self.last_frame = f
            self.last_frame_t = now_m
            if not self.connected:
                self.connected = True
                self.model = f.model
                self._log(f"Connecté : {f.model}")
            self._on_frame(f, self.dev.clock())
        if hasattr(self.dev, "reader"):
            self.bad_frames = self.dev.reader.bad
        silent = now_m - self.last_frame_t
        if silent > 6 and self.connected:
            self.connected = False
            self._log("Plus de données de l'EBC, nouvelle tentative de connexion")
        if not self.connected and silent > 6 and int(silent) % 5 == 0:
            self.dev.send(p.cmd_connect())
        if silent > 60 and self.run_:
            self._finish("interrupted", "connexion perdue depuis 60 s")
        if not frames:
            self._tick_rest()
            self.publish({"type": "status", "status": self.status()})

    def _process_cmds(self) -> None:
        while True:
            try:
                kind, kw, box = self.cmds.get_nowait()
            except queue.Empty:
                return
            try:
                box["result"] = getattr(self, "_cmd_" + kind)(**kw)
            except Exception as e:
                box["error"] = str(e)
            box["ev"].set()

    # ---------------------------------------------------------- commandes
    def _cmd_start(self, steps, name, battery, notes, program_name):
        if self.run_:
            raise StepError("un test est déjà en cours")
        if not self.connected:
            raise StepError("EBC non connecté")
        steps = validate_steps(steps)
        tid = self.db.create_test(name, battery, notes, {"name": program_name, "steps": steps})
        self.run_ = Run(tid, name, battery, steps, self.dev.clock())
        self._log(f"Test #{tid} démarré : {name} ({battery})")
        self._begin_step(0)
        return tid

    def _cmd_stop(self):
        if self.run_:
            self._finish("aborted", "arrêt manuel")
        elif self.dev:
            self.dev.send(p.cmd_stop())

    def _cmd_skip(self):
        if self.run_ and self.run_.step:
            self._end_step("passée manuellement")

    def _cmd_reconnect(self):
        self._drop_device()

    # ---------------------------------------------------------- étapes
    def _send_step(self, s: StepRun) -> None:
        sp = s.spec
        t = sp["type"]
        mx = int(sp.get("max_min") or 0)
        if t == "charge":
            pkt = p.cmd_charge_cccv(sp["current"], sp["voltage"], sp["cutoff"])
        elif t == "discharge_cc":
            pkt = p.cmd_discharge_cc(sp["current"], sp["cutoff_v"], mx)
        elif t == "discharge_cp":
            pkt = p.cmd_discharge_cp(sp["power"], sp["cutoff_v"], mx)
        elif t == "discharge_cr":
            v = self.last_frame.voltage if self.last_frame else 3.7
            i = min(max(v / sp["resistance"], 0.1), LIMITS["discharge_current"][1])
            pkt = p.cmd_discharge_cc(i, sp["cutoff_v"], mx)
        else:
            return
        s.sent_at = self.dev.clock()
        self.dev.send(pkt)

    def _begin_step(self, idx: int) -> None:
        r = self.run_
        if idx >= len(r.steps):
            self._finish("done", "programme terminé")
            return
        spec = r.steps[idx]
        if spec["type"] == "loop":
            done = r.loops.get(idx, 0)
            if done < spec["times"]:
                r.loops[idx] = done + 1
                # remet à zéro les boucles internes
                for k in list(r.loops):
                    if spec["goto"] - 1 <= k < idx:
                        r.loops.pop(k)
                self._log(f"Boucle {done + 1}/{spec['times']} → étape {spec['goto']}")
                self._begin_step(spec["goto"] - 1)
            else:
                r.loops.pop(idx, None)
                self._begin_step(idx + 1)
            return
        now = self.dev.clock()
        s = StepRun(idx, spec, now)
        if self.last_frame and self.last_frame.state != p.State.ACTIVE:
            s.v_rest = self.last_frame.voltage
        r.step = s
        self._log(f"Étape {idx + 1} : {STEP_TYPES[spec['type']]} {self._describe(spec)}")
        if spec["type"] == "rest":
            if self.last_frame and self.last_frame.state == p.State.ACTIVE:
                self.dev.send(p.cmd_stop())
            s.confirmed = True
        else:
            self._send_step(s)

    @staticmethod
    def _describe(sp: dict) -> str:
        t = sp["type"]
        if t == "charge":
            return f"{sp['current']} A / {sp['voltage']} V, fin {sp['cutoff']} A"
        if t == "discharge_cc":
            return f"{sp['current']} A → {sp['cutoff_v']} V"
        if t == "discharge_cp":
            return f"{sp['power']} W → {sp['cutoff_v']} V"
        if t == "discharge_cr":
            return f"{sp['resistance']} Ω → {sp['cutoff_v']} V"
        if t == "rest":
            return f"{sp['minutes']} min"
        return ""

    def _expected_kind(self, t: str):
        return {"charge": p.Kind.C_CV, "discharge_cc": p.Kind.D_CC,
                "discharge_cr": p.Kind.D_CC, "discharge_cp": p.Kind.D_CP}.get(t)

    def _on_frame(self, f: p.Frame, now: float) -> None:
        r = self.run_
        if not r:
            self.publish({"type": "status", "status": self.status()})
            return
        max_dt = 30.0 * getattr(self.dev, "speed", 1.0)  # garde-fou si trou de données
        dt = 0.0 if r.last_t is None else min(now - r.last_t, max_dt)
        r.last_t = now
        s = r.step
        if s:
            self._update_step(s, f, now, dt)
        if self.run_ and self.run_.step is s and s:
            self.db.add_sample(r.test_id, round(now - r.t0, 2), s.index, f.voltage, f.current,
                               round(s.cap, 5), round(s.wh, 5), f.mode)
            self.publish({"type": "sample", "test_id": r.test_id,
                          "s": {"t": round(now - r.t0, 2), "step": s.index, "v": f.voltage,
                                "i": f.current, "cap": round(s.cap, 5), "wh": round(s.wh, 5)}})
        self.publish({"type": "status", "status": self.status()})

    def _update_step(self, s: StepRun, f: p.Frame, now: float, dt: float) -> None:
        sp = s.spec
        t = sp["type"]
        s.v_last = f.voltage
        if t == "rest":
            s.n += 1
            s.v_sum += f.voltage
            if s.v_start is None:
                s.v_start = f.voltage
            if f.state == p.State.ACTIVE and now - s.t_start > 3:
                self.dev.send(p.cmd_stop())
            self._tick_rest(now)
            return

        want = self._expected_kind(t)
        if not s.confirmed:
            if f.kind == want and f.state == p.State.ACTIVE:
                s.confirmed = True
            elif now - s.sent_at > 6:
                if s.retries >= 2:
                    self._log("L'EBC ne démarre pas l'étape (batterie branchée ? tension hors plage ?)")
                    self._finish("error", "l'EBC n'a pas démarré l'étape")
                    return
                s.retries += 1
                self._log(f"Pas de réponse, renvoi de la commande ({s.retries})")
                self._send_step(s)
            return

        # mesures
        if f.state == p.State.ACTIVE:
            s.cap += f.current * dt / 3600
            s.wh += f.current * f.voltage * dt / 3600
            s.n += 1
            s.v_sum += f.voltage
            if s.v_start is None:
                s.v_start = f.voltage
            if t.startswith("discharge") and s.n == 3 and s.v_rest and f.current > 0.05:
                ir = (s.v_rest - f.voltage) / f.current * 1000
                if 0 < ir < 5000:
                    s.ir_mohm = round(ir, 1)

        elapsed_min = (now - s.t_start) / 60
        # fins d'étape
        if f.state in (p.State.FINISHED, p.State.STOPPED) or f.kind != want:
            self._end_step("fin signalée par l'EBC")
            return
        if sp.get("max_min") and elapsed_min >= sp["max_min"]:
            self.dev.send(p.cmd_stop())
            self._end_step("durée max atteinte")
            return
        if t == "charge" and sp.get("max_ah") and s.cap >= sp["max_ah"]:
            self.dev.send(p.cmd_stop())
            self._end_step("capacité max atteinte")
            return
        if t.startswith("discharge"):
            s.low_count = s.low_count + 1 if f.voltage <= sp["cutoff_v"] else 0
            if s.low_count >= 3:  # sécurité si l'EBC ne coupe pas
                self.dev.send(p.cmd_stop())
                self._end_step("tension de coupure (logiciel)")
                return
        s.zero_count = s.zero_count + 1 if f.current < 0.005 else 0
        if s.zero_count >= 5:
            self.dev.send(p.cmd_stop())
            self._end_step("courant nul (batterie débranchée ?)")
            return
        if t == "discharge_cr":
            s.cr_count += 1
            if s.cr_count % 3 == 0:
                i = min(max(f.voltage / sp["resistance"], 0.1), LIMITS["discharge_current"][1])
                self.dev.send(p.cmd_adjust_cc(i, sp["cutoff_v"], int(sp.get("max_min") or 0)))

    def _tick_rest(self, now: float | None = None) -> None:
        r = self.run_
        if not r or not r.step or r.step.spec["type"] != "rest":
            return
        now = now if now is not None else self.dev.clock()
        if (now - r.step.t_start) / 60 >= r.step.spec["minutes"]:
            self._end_step("durée écoulée")

    def _end_step(self, reason: str) -> None:
        r = self.run_
        s = r.step
        now = self.dev.clock()
        res = {
            "step": s.index, "type": s.spec["type"], "label": STEP_TYPES[s.spec["type"]],
            "t_start": round(s.t_start - r.t0, 1), "t_end": round(now - r.t0, 1),
            "capacity_ah": round(s.cap, 4), "energy_wh": round(s.wh, 4),
            "v_start": s.v_start, "v_end": s.v_last,
            "v_avg": round(s.v_sum / s.n, 4) if s.n else None,
            "ir_mohm": s.ir_mohm, "end_reason": reason,
        }
        r.results.append(res)
        self.db.add_step_result(r.test_id, s.index, s.spec["type"], s.spec, res)
        self._log(f"Étape {s.index + 1} finie ({reason}) : {res['capacity_ah']:.3f} Ah, {res['energy_wh']:.2f} Wh")
        r.step = None
        self._begin_step(s.index + 1)

    def _finish(self, status: str, reason: str) -> None:
        r = self.run_
        if not r:
            return
        if r.step and r.step.spec["type"] != "rest" and self.dev:
            try:
                self.dev.send(p.cmd_stop())
            except Exception:
                pass
        if r.step:
            s = r.step
            r.step = None
            if s.n:
                now = self.dev.clock() if self.dev else time.monotonic()
                res = {"step": s.index, "type": s.spec["type"], "label": STEP_TYPES[s.spec["type"]],
                       "t_start": round(s.t_start - r.t0, 1), "t_end": round(now - r.t0, 1),
                       "capacity_ah": round(s.cap, 4), "energy_wh": round(s.wh, 4),
                       "v_start": s.v_start, "v_end": s.v_last,
                       "v_avg": round(s.v_sum / s.n, 4), "ir_mohm": s.ir_mohm,
                       "end_reason": "interrompue"}
                r.results.append(res)
                self.db.add_step_result(r.test_id, s.index, s.spec["type"], s.spec, res)
        summary = summarize(r.results)
        self.db.finish_test(r.test_id, status, summary)
        self._log(f"Test #{r.test_id} terminé ({status}) : {reason}")
        self.publish({"type": "test_finished", "test_id": r.test_id, "status": status})
        self.run_ = None


def summarize(results: list[dict]) -> dict:
    dis = [x for x in results if x["type"].startswith("discharge") and x["capacity_ah"] > 0]
    chg = [x for x in results if x["type"] == "charge" and x["capacity_ah"] > 0]
    out: dict = {"n_discharges": len(dis)}
    if dis:
        # la décharge de référence = la dernière décharge allée jusqu'à la coupure
        full = [x for x in dis if "coupure" in x["end_reason"] or "EBC" in x["end_reason"]]
        ref = (full or dis)[-1]
        out.update(capacity_ah=ref["capacity_ah"], energy_wh=ref["energy_wh"],
                   v_avg=ref["v_avg"], ir_mohm=ref["ir_mohm"],
                   capacities=[x["capacity_ah"] for x in dis])
        irs = [x["ir_mohm"] for x in dis if x["ir_mohm"]]
        if irs:
            out["ir_mohm"] = round(sum(irs) / len(irs), 1)
    if chg:
        out["charge_ah"] = chg[-1]["capacity_ah"]
    return out
