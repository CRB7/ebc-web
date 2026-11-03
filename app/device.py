"""Accès matériel : port série réel, ou simulateur pour tester sans l'EBC."""
from __future__ import annotations

import math
import threading
import time

from . import protocol as p


class SerialDevice:
    """EBC-A20 réel sur un port USB-série."""

    def __init__(self, port: str) -> None:
        self.port = port
        self.ser = None
        self.reader = p.FrameReader()

    def open(self) -> None:
        import serial  # pyserial

        self.ser = serial.Serial(
            self.port, 9600, bytesize=8, parity=serial.PARITY_ODD,
            stopbits=1, timeout=0.3,
        )
        self.reader = p.FrameReader()

    def close(self) -> None:
        if self.ser:
            try:
                self.ser.close()
            finally:
                self.ser = None

    @property
    def is_open(self) -> bool:
        return self.ser is not None and self.ser.is_open

    def send(self, data: bytes) -> None:
        self.ser.write(data)
        self.ser.flush()

    def read_frames(self) -> list[p.Frame]:
        data = self.ser.read(64)
        return self.reader.feed(data) if data else []

    def clock(self) -> float:
        return time.monotonic()


class _Cell:
    """Cellule Li-ion très simplifiée (OCV + résistance interne)."""

    def __init__(self, capacity_ah: float = 2.6, r_int: float = 0.065, soc: float = 0.35):
        self.cap = capacity_ah
        self.r = r_int
        self.soc = soc

    def ocv(self) -> float:
        s = min(max(self.soc, 0.0), 1.05)
        # courbe plausible 2.70 V (vide) -> 4.20 V (plein)
        return 2.95 + 1.25 * s - 0.25 * math.exp(-12 * s) + 0.08 * math.sin(math.pi * min(s, 1.0))


class SimDevice:
    """Émule un EBC-A20 relié à une cellule 18650.

    `speed` accélère le temps simulé (60 = une minute simulée par seconde).
    """

    def __init__(self, speed: float = 1.0, capacity_ah: float = 2.6, tick: float = 1.0) -> None:
        self.speed = speed
        self.tick = tick
        self.cell = _Cell(capacity_ah)
        self.lock = threading.Lock()
        self.connected = False
        self.is_open = False
        self.mode = 0x00          # D-CC arrêt
        self.params = (0.0, 0.0, 0.0)
        self.count_ah = 0.0
        self.t_run = 0.0
        self._t0 = time.monotonic()
        self._last = None
        self.cv_phase = False

    # interface commune ------------------------------------------------
    def open(self) -> None:
        self.is_open = True
        self._last = time.monotonic()

    def close(self) -> None:
        self.is_open = False
        self.connected = False

    def clock(self) -> float:
        return self._t0 + (time.monotonic() - self._t0) * self.speed

    def send(self, data: bytes) -> None:
        if len(data) != p.CMD_LEN or data[0] != p.START:
            return
        cmd = data[1]
        a = p.b240_decode(data[2], data[3])
        b = p.b240_decode(data[4], data[5])
        c = p.b240_decode(data[6], data[7])
        with self.lock:
            if cmd == p.Cmd.CONNECT:
                self.connected = True
            elif cmd == p.Cmd.DISCONNECT:
                self.connected = False
            elif cmd == p.Cmd.STOP:
                self.mode = self.mode % 10
            elif cmd == p.Cmd.CHARGE_CCCV:
                self._start(p.Kind.C_CV, (a / 100, b / 100, c / 100))
                self.cv_phase = False
            elif cmd == p.Cmd.DISCHARGE_CC:
                self._start(p.Kind.D_CC, (a / 100, b / 100, c))
            elif cmd == p.Cmd.ADJUST_CC and self.mode == 10:
                self.params = (a / 100, b / 100, c)
            elif cmd == p.Cmd.DISCHARGE_CP:
                self._start(p.Kind.D_CP, (float(a), b / 100, c))

    def _start(self, kind: p.Kind, params) -> None:
        self.mode = 10 + kind
        self.params = params
        self.count_ah = 0.0
        self.t_run = 0.0

    def read_frames(self) -> list[p.Frame]:
        time.sleep(self.tick)
        if not self.connected:
            return []
        now = time.monotonic()
        dt = (now - self._last) * self.speed
        self._last = now
        with self.lock:
            v, i = self._step(dt)
            s1, s2, s3 = self.params
            raw = p.build_response(self.mode, i, v, self.count_ah, s1, s2, s3)
        f = p.parse(raw)
        return [f] if f else []

    def _step(self, dt: float) -> tuple[float, float]:
        c = self.cell
        kind, active = self.mode % 10, self.mode // 10 == 1
        if not active:
            return c.ocv() + 0.0005 * math.sin(time.time()), 0.0
        self.t_run += dt
        s1, s2, s3 = self.params
        if kind == p.Kind.C_CV:
            target_v, i_set, i_cut = s2, s1, s3
            i = i_set
            v = c.ocv() + i * c.r
            if v >= target_v:
                self.cv_phase = True
                i = max((target_v - c.ocv()) / c.r, 0.0)
                v = target_v
            c.soc += i * dt / 3600 / c.cap
            self.count_ah += i * dt / 3600
            if self.cv_phase and i <= i_cut:
                self.mode = 20 + kind
            return v, i
        # décharge
        ocv = c.ocv()
        if kind == p.Kind.D_CP:
            pw = s1
            i = (ocv - math.sqrt(max(ocv * ocv - 4 * c.r * pw, 0))) / (2 * c.r)
        else:
            i = s1
        v = ocv - i * c.r
        c.soc -= i * dt / 3600 / c.cap
        self.count_ah += i * dt / 3600
        if v <= s2 or c.soc <= 0 or (s3 and self.t_run >= s3 * 60):
            self.mode = 20 + kind
            return c.ocv(), 0.0
        return v, i
