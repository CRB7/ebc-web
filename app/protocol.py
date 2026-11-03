"""Protocole série ZKETECH EBC-A20.

Reconstitué à partir de deux projets open source :
  - github.com/dev-strom/esp-ebc-mqtt (lib/commands/EbcA20.cpp)
  - github.com/ardiehl/EBC-controller (main.pas, ebcc.init)

Liaison : 9600 bauds, 8 bits, parité impaire, 1 stop (8O1).

Trame commande (PC -> EBC), 10 octets :
    FA  cmd  p1h p1l  p2h p2l  p3h p3l  crc  F8
Trame réponse (EBC -> PC), 19 octets, ~1 par seconde :
    FA  mode  v0..v6 (7 x 2 octets)  model_id  crc  F8

Les valeurs 16 bits sont en "base 240" : valeur = hi*240 + lo.
Cela garantit qu'aucun octet de données n'atteint 0xF0 (donc jamais FA/F8).
Le checksum est un XOR des octets entre FA et crc ; s'il vaut >= 0xF0,
l'appareil n'utilise que le quartet bas.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

START = 0xFA
END = 0xF8
CMD_LEN = 10
RESP_LEN = 19

MODEL_IDS = {0x05: "EBC-A05", 0x06: "EBC-A10H", 0x09: "EBC-A20", 0x0B: "EBC-A40"}


class Cmd(IntEnum):
    DISCHARGE_CC = 0x01
    STOP = 0x02
    CONNECT = 0x05
    DISCONNECT = 0x06
    ADJUST_CC = 0x07      # modifie le courant d'une décharge CC en cours
    CONTINUE_CC = 0x08
    DISCHARGE_CP = 0x11
    CONTINUE_CP = 0x18
    CHARGE_CCCV = 0x21
    CONTINUE_CCCV = 0x28


class Kind(IntEnum):
    D_CC = 0
    D_CP = 1
    C_CV = 2


class State(IntEnum):
    STOPPED = 0
    ACTIVE = 1
    FINISHED = 2
    UNKNOWN = 3


# --------------------------------------------------------------- encodage
def b240_encode(n: int) -> tuple[int, int]:
    n = max(0, min(int(n), 239 * 240 + 239))
    return n // 240, n % 240


def b240_decode(hi: int, lo: int) -> int:
    return hi * 240 + lo


def checksum(payload: bytes) -> int:
    cs = 0
    for b in payload:
        cs ^= b
    if cs & 0xF0 == 0xF0:
        cs &= 0x0F
    return cs


def _cs_ok(payload: bytes, received: int) -> bool:
    raw = 0
    for b in payload:
        raw ^= b
    return received == raw or (raw & 0xF0 == 0xF0 and received == raw & 0x0F)


def build(cmd: int, p1: int = 0, p2: int = 0, p3: int = 0) -> bytes:
    """p1..p3 : entiers déjà mis à l'échelle (ex. centièmes d'ampère)."""
    body = bytes([cmd, *b240_encode(p1), *b240_encode(p2), *b240_encode(p3)])
    return bytes([START]) + body + bytes([checksum(body), END])


def _c(x: float) -> int:  # A ou V -> centièmes, arrondi propre (4.22 -> 422)
    return int(round(x * 100))


def cmd_connect() -> bytes:
    return build(Cmd.CONNECT)


def cmd_disconnect() -> bytes:
    return build(Cmd.DISCONNECT)


def cmd_stop() -> bytes:
    return build(Cmd.STOP)


def cmd_charge_cccv(current_a: float, voltage_v: float, cutoff_a: float) -> bytes:
    return build(Cmd.CHARGE_CCCV, _c(current_a), _c(voltage_v), _c(cutoff_a))


def cmd_discharge_cc(current_a: float, cutoff_v: float, max_min: int = 0) -> bytes:
    return build(Cmd.DISCHARGE_CC, _c(current_a), _c(cutoff_v), int(max_min))


def cmd_adjust_cc(current_a: float, cutoff_v: float, max_min: int = 0) -> bytes:
    return build(Cmd.ADJUST_CC, _c(current_a), _c(cutoff_v), int(max_min))


def cmd_discharge_cp(power_w: float, cutoff_v: float, max_min: int = 0) -> bytes:
    return build(Cmd.DISCHARGE_CP, int(round(power_w)), _c(cutoff_v), int(max_min))


# --------------------------------------------------------------- réponses
def decode_capacity(hi: int, lo: int) -> float:
    if hi & 0x80:
        # >= 10 Ah (règle validée jusqu'à ~230 Ah par l'auteur d'EBC-controller)
        return ((hi & 0x7F) * 240 + lo - 0x0800) / 100.0
    return (hi * 240 + lo) / 1000.0


@dataclass
class Frame:
    mode: int
    current: float      # A
    voltage: float      # V
    capacity: float     # Ah (compteur interne de l'EBC)
    set1: float         # consigne courant (A) ou puissance (W)
    set2: float         # tension consigne / coupure (V)
    set3: float         # temps max (min) ou courant de coupure (A)
    model_id: int
    raw: bytes = field(repr=False, default=b"")

    # Modes : 0x00/0x01/0x02 = arrêt, +10 = en cours, +20 = terminé
    # (D-CC, D-CP, C-CV). Les valeurs >= 100 sont des variantes observées
    # sur l'A20 dont le rôle est inconnu ; on les ramène à la base.
    def _m(self) -> int | None:
        m = self.mode - 100 if self.mode >= 100 else self.mode
        return m if 0 <= m < 30 and m % 10 < 3 else None

    @property
    def kind(self) -> Kind | None:
        m = self._m()
        return None if m is None else Kind(m % 10)

    @property
    def state(self) -> State:
        m = self._m()
        return State.UNKNOWN if m is None else State(m // 10)

    @property
    def model(self) -> str:
        return MODEL_IDS.get(self.model_id, f"inconnu (0x{self.model_id:02x})")

    @property
    def mode_label(self) -> str:
        names = {Kind.D_CC: "Décharge CC", Kind.D_CP: "Décharge CP", Kind.C_CV: "Charge CC-CV"}
        states = {State.STOPPED: "arrêt", State.ACTIVE: "en cours", State.FINISHED: "terminé"}
        k = self.kind
        if k is None or self.state == State.UNKNOWN:
            return f"mode 0x{self.mode:02x}"
        return f"{names[k]} ({states[self.state]})"


def parse(frame: bytes) -> Frame | None:
    if len(frame) != RESP_LEN or frame[0] != START or frame[-1] != END:
        return None
    if not _cs_ok(frame[1:17], frame[17]):
        return None
    v = [b240_decode(frame[2 + 2 * i], frame[3 + 2 * i]) for i in range(7)]
    mode = frame[1]
    kind = (mode - 100 if mode >= 100 else mode) % 10
    set1 = v[4] if kind == Kind.D_CP else v[4] / 100.0
    set3 = v[6] / 100.0 if kind == Kind.C_CV else float(v[6])
    return Frame(
        mode=mode,
        current=v[0] / 100.0,
        voltage=v[1] / 1000.0,
        capacity=decode_capacity(frame[6], frame[7]),
        set1=set1,
        set2=v[5] / 100.0,
        set3=set3,
        model_id=frame[16],
        raw=bytes(frame),
    )


class FrameReader:
    """Reconstitue les trames de 19 octets dans un flux série."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.bad = 0

    def feed(self, data: bytes) -> list[Frame]:
        self.buf.extend(data)
        out: list[Frame] = []
        while True:
            try:
                i = self.buf.index(START)
            except ValueError:
                self.buf.clear()
                break
            del self.buf[:i]
            if len(self.buf) < RESP_LEN:
                break
            f = parse(bytes(self.buf[:RESP_LEN]))
            if f is None:
                self.bad += 1
                del self.buf[:1]   # resynchronisation
                continue
            out.append(f)
            del self.buf[:RESP_LEN]
        return out


def build_response(mode: int, current: float, voltage: float, capacity: float,
                   set1: float, set2: float, set3: float, model_id: int = 0x09) -> bytes:
    """Fabrique une trame réponse (utilisé par le simulateur et les tests)."""
    def cap_enc(ah: float) -> tuple[int, int]:
        if ah < 10:
            return b240_encode(int(round(ah * 1000)))
        n = int(round(ah * 100)) + 0x0800
        hi, lo = n // 240, n % 240
        return hi | 0x80, lo

    kind = mode % 10
    s1 = int(round(set1)) if kind == Kind.D_CP else _c(set1)
    s3 = _c(set3) if kind == Kind.C_CV else int(set3)
    vals = [b240_encode(_c(current)), b240_encode(int(round(voltage * 1000))), cap_enc(capacity),
            (0, 0), b240_encode(s1), b240_encode(_c(set2)), b240_encode(s3)]
    body = bytes([mode]) + bytes(b for pair in vals for b in pair) + bytes([model_id])
    return bytes([START]) + body + bytes([checksum(body), END])
