r"""snmp_aps.py - le SNMP v2c dos APs stand-alone (community em C:\netmon\aps-snmp.json).
Nunca imprime a community. USO: python snmp_aps.py [ip ...]"""
import io
import json
import socket
import sys

CFG = json.load(io.open(r"C:\netmon\aps-snmp.json", encoding="utf-8-sig"))
COMM = CFG["community"].encode()
OIDS = [("sysName", "1.3.6.1.2.1.1.5.0"), ("sysLocation", "1.3.6.1.2.1.1.6.0"),
        ("sysUpTime", "1.3.6.1.2.1.1.3.0"), ("sysDescr", "1.3.6.1.2.1.1.1.0")]


def tlv(t, v):
    n = len(v)
    if n < 128:
        ln = bytes([n])
    else:
        b = n.to_bytes((n.bit_length() + 7) // 8, "big")
        ln = bytes([0x80 | len(b)]) + b
    return bytes([t]) + ln + v


def oid_enc(o):
    p = [int(x) for x in o.split(".")]
    out = bytes([40 * p[0] + p[1]])
    for n in p[2:]:
        s = [n & 0x7F]
        n >>= 7
        while n:
            s.insert(0, 0x80 | (n & 0x7F))
            n >>= 7
        out += bytes(s)
    return out


def ler(buf, i):
    t = buf[i]
    n = buf[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(buf[i:i + k], "big")
        i += k
    return t, buf[i:i + n], i + n


def get(ip, oid, timeout=2.5):
    vb = tlv(0x30, tlv(0x06, oid_enc(oid)) + b"\x05\x00")
    pdu = tlv(0xA0, tlv(0x02, b"\x01") + tlv(0x02, b"\x00") + tlv(0x02, b"\x00") + tlv(0x30, vb))
    msg = tlv(0x30, tlv(0x02, b"\x01") + tlv(0x04, COMM) + pdu)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(msg, (ip, 161))
        d, _ = s.recvfrom(4096)
    except socket.timeout:
        return None
    finally:
        s.close()
    _, corpo, _ = ler(d, 0)
    i = 0
    for _ in range(2):
        _, _, i = ler(corpo, i)
    _, pdu, _ = ler(corpo, i)
    j = 0
    for _ in range(3):
        _, _, j = ler(pdu, j)
    _, vbl, _ = ler(pdu, j)
    _, vb1, _ = ler(vbl, 0)
    _, _, k = ler(vb1, 0)
    t, v, _ = ler(vb1, k)
    if t == 0x43:
        cs = int.from_bytes(v, "big")
        return "%dd %02dh%02dm" % (cs // 8640000, cs // 360000 % 24, cs // 6000 % 60)
    if t in (0x80, 0x81, 0x82):
        return "<sem valor>"
    return v.decode("utf-8", "replace") if t == 0x04 else v.hex()


for ip in (sys.argv[1:] or CFG.get("hosts") or []):
    r = {n: get(ip, o) for n, o in OIDS}
    if all(v is None for v in r.values()):
        print("%-14s sem resposta SNMP (community/Get Source/agente desligado?)" % ip)
    else:
        print("%-14s %s" % (ip, "  ".join("%s=%s" % (n, (v or "-")[:60]) for n, v in r.items())))
