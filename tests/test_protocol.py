"""Vérifie l'encodage contre des trames réelles capturées par les auteurs
d'esp-ebc-mqtt (commentaires de command.cpp / message.cpp)."""
from app import protocol as p


def h(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", ""))


def test_connect_stop():
    assert p.cmd_connect() == h("fa 05 0000 0000 0000 05 f8")
    assert p.cmd_stop() == h("fa 02 0000 0000 0000 02 f8")
    assert p.cmd_disconnect() == h("fa 06 0000 0000 0000 06 f8")


def test_charge_frames():
    # checksum brut 0xfa -> l'appareil attend 0x0a
    assert p.cmd_charge_cccv(1.0, 4.2, 0.1) == h("fa 21 0064 01b4 000a 0a f8")
    assert p.cmd_charge_cccv(1.0, 4.1, 0.1) == h("fa 21 0064 01aa 000a e4 f8")
    assert p.cmd_charge_cccv(0.5, 4.1, 0.2) == h("fa 21 0032 01aa 0014 ac f8")


def test_discharge_frames():
    assert p.cmd_discharge_cc(0.35, 2.5, 15) == h("fa 01 0023 010a 000f 26 f8")
    assert p.cmd_discharge_cc(0.36, 2.6, 16) == h("fa 01 0024 0114 0010 20 f8")
    assert p.cmd_discharge_cp(1, 2.5, 10) == h("fa 11 0001 010a 000a 11 f8")
    assert p.cmd_discharge_cp(11, 2.6, 11) == h("fa 11 000b 0114 000b 04 f8")


def test_rounding():
    # 4.22 * 100 = 421.999… ; doit donner 422
    f = p.cmd_charge_cccv(1, 4.22, 0.1)
    assert p.b240_decode(f[4], f[5]) == 422


def test_parse_real_response():
    raw = h("fa 0a 0023 101c 0000 0000 0023 018c 0000 09 82 f8")
    f = p.parse(raw)
    assert f is not None
    assert f.kind == p.Kind.D_CC and f.state == p.State.ACTIVE
    assert f.current == 0.35
    assert abs(f.voltage - 3.868) < 1e-9
    assert f.model == "EBC-A20"


def test_roundtrip_and_reader():
    frames = [
        p.build_response(0x0C, 1.5, 3.95, 1.234, 1.5, 4.2, 0.1),
        p.build_response(0x0A, 2.0, 3.61, 25.5, 2.0, 2.8, 0),
        p.build_response(0x0B, 3.1, 3.30, 230.4, 10, 2.8, 120),
    ]
    stream = b"\x00\x13garbage" + b"".join(frames)
    r = p.FrameReader()
    out = r.feed(stream[:20]) + r.feed(stream[20:])
    assert len(out) == 3
    assert out[0].kind == p.Kind.C_CV and abs(out[0].capacity - 1.234) < 1e-9
    assert abs(out[1].capacity - 25.5) < 1e-9
    assert abs(out[2].capacity - 230.4) < 1e-9 and out[2].set1 == 10


def test_bad_crc_rejected():
    raw = bytearray(h("fa 0a 0023 101c 0000 0000 0023 018c 0000 09 82 f8"))
    raw[17] ^= 0x01
    assert p.parse(bytes(raw)) is None
