# -*- coding: utf-8 -*-
# NetMon-DNS - forwarder DNS leve (somente stdlib).
# Responde nomes LOCAIS (dns.json -> hosts) e REENCAMINHA o resto pros upstreams.
# Motivo: o "ip dns server" do IOS no Cisco 2911 pegava ~60% de CPU servindo a LAN.
# Aqui a carga sai do Cisco. Se este processo cair, o DNS secundario (DHCP) assume -> ninguem fica sem net.
import socket, struct, threading, json, os, sys, time, traceback, sqlite3
from concurrent.futures import ThreadPoolExecutor
from collections import deque

BASE = os.path.dirname(os.path.abspath(__file__))

# roda sob pythonw (sem console) -> redireciona stdout/stderr p/ arquivo (senao tracebacks somem)
try:
    _elog = open(os.path.join(BASE, "dns.err.log"), "a", buffering=1, encoding="utf-8", errors="replace")
    sys.stdout = sys.stderr = _elog
    _elog.write("\n===== dns start %s pid=%d =====\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid()))
except Exception:
    pass

with open(os.path.join(BASE, "dns.json"), encoding="utf-8") as f:
    CFG = json.load(f)

DOMAIN    = (CFG.get("domain") or "").lower().strip(".")
TTL       = int(CFG.get("ttl", 300))
UPSTREAM  = CFG.get("upstream") or ["8.8.8.8", "1.1.1.1"]
BIND      = CFG.get("bind", "0.0.0.0")
PORT      = int(CFG.get("port", 53))
UPTIMEOUT = float(CFG.get("upstream_timeout", 3))
HOSTS = {}
for k, v in (CFG.get("hosts") or {}).items():
    HOSTS[k.lower().strip(".")] = v

STATS = {"local": 0, "fwd": 0, "err": 0}
LOCK = threading.Lock()
CLIENTS = {}
START = time.time()

def bump(kind, ip):
    with LOCK:
        STATS[kind] = STATS.get(kind, 0) + 1
        if ip: CLIENTS[ip] = CLIENTS.get(ip, 0) + 1

# --- log de consultas por host (SQLite, retencao 7 dias, escrita em lote) ---
LOGDB = os.path.join(BASE, "dns_log.db")
LOGQ = deque()
LOGQ_MAX = 60000
RETAIN_DAYS = 7

def log_writer():
    try:
        con = sqlite3.connect(LOGDB)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE IF NOT EXISTS queries(ts INTEGER, client TEXT, name TEXT)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_q_ts ON queries(ts)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_q_client ON queries(client)")
        con.commit(); con.close()
    except Exception:
        traceback.print_exc()
    last_prune = 0
    while True:
        time.sleep(3)
        batch = []
        while LOGQ and len(batch) < 8000:
            try: batch.append(LOGQ.popleft())
            except IndexError: break
        if batch:
            try:
                con = sqlite3.connect(LOGDB)
                con.executemany("INSERT INTO queries VALUES(?,?,?)", batch)
                con.commit(); con.close()
            except Exception:
                traceback.print_exc()
        now = time.time()
        if now - last_prune > 3600:
            last_prune = now
            try:
                con = sqlite3.connect(LOGDB)
                con.execute("DELETE FROM queries WHERE ts < ?", (int(now - RETAIN_DAYS * 86400),))
                con.commit(); con.execute("PRAGMA wal_checkpoint(TRUNCATE)"); con.close()
            except Exception:
                pass

def qname_of(data):
    # le o QNAME da pergunta (offset 12; perguntas nao usam compressao) + qtype; devolve fim da pergunta
    off = 12; labels = []
    while True:
        if off >= len(data): raise ValueError("truncated")
        l = data[off]
        if l == 0: off += 1; break
        if l & 0xC0: raise ValueError("ptr in question")
        off += 1; labels.append(data[off:off + l].decode("latin1")); off += l
    qtype = struct.unpack(">H", data[off:off + 2])[0]
    return ".".join(labels).lower(), qtype, off + 4

def local_ip(qname):
    n = qname
    if DOMAIN and n.endswith("." + DOMAIN):
        n = n[: -(len(DOMAIN) + 1)]
    return HOSTS.get(n) or HOSTS.get(qname)

def build_local(data, qend, qtype, ip):
    tid = data[0:2]; rd = data[2] & 0x01
    b1 = 0x84 | (0x01 if rd else 0)   # QR=1, AA=1, RD copiado
    b2 = 0x80                          # RA=1, RCODE=0
    q = data[12:qend]
    if qtype == 1:                     # A -> responde com o IP
        ans = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, TTL, 4) + socket.inet_aton(ip)
        return tid + bytes([b1, b2]) + struct.pack(">HHHH", 1, 1, 0, 0) + q + ans
    # nome local mas nao-A (ex.: AAAA) -> NOERROR sem resposta (NODATA), nao reencaminha
    return tid + bytes([b1, b2]) + struct.pack(">HHHH", 1, 0, 0, 0) + q

def servfail(data):
    tid = data[0:2]; rd = data[2] & 0x01
    b1 = 0x80 | (0x01 if rd else 0); b2 = 0x82   # RA=1, RCODE=2 SERVFAIL
    return tid + bytes([b1, b2]) + data[4:6] + struct.pack(">HHH", 0, 0, 0) + data[12:]

def forward_udp(data):
    for up in UPSTREAM:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(UPTIMEOUT)
        try:
            s.sendto(data, (up, 53)); return s.recvfrom(4096)[0]
        except Exception:
            continue
        finally:
            s.close()
    return None

def handle_udp(sock, data, addr):
    try:
        if len(data) < 12 or (data[2] & 0x80):   # ignora respostas / lixo
            return
        try:
            qname, qtype, qend = qname_of(data)
        except Exception:
            r = forward_udp(data)
            if r: sock.sendto(r, addr)
            return
        if qtype == 1 and len(LOGQ) < LOGQ_MAX:
            LOGQ.append((int(time.time()), addr[0], qname))
        ip = local_ip(qname) if qtype in (1, 28) else None
        if ip:
            bump("local", addr[0])
            sock.sendto(build_local(data, qend, qtype, ip), addr); return
        bump("fwd", addr[0])
        r = forward_udp(data)
        sock.sendto(r if r else servfail(data), addr)
    except Exception:
        bump("err", addr[0] if addr else None)
        try: sock.sendto(servfail(data), addr)
        except Exception: pass

def udp_server():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((BIND, PORT))
    pool = ThreadPoolExecutor(max_workers=64)
    print("NetMon-DNS UDP %s:%d | %d hosts locais | upstream %s" % (BIND, PORT, len(HOSTS), UPSTREAM))
    while True:
        try:
            data, addr = s.recvfrom(2048)
            pool.submit(handle_udp, s, data, addr)
        except Exception:
            traceback.print_exc(); time.sleep(0.05)

def handle_tcp(conn):
    try:
        conn.settimeout(5)
        h = conn.recv(2)
        if len(h) < 2: return
        ln = struct.unpack(">H", h)[0]; data = b""
        while len(data) < ln:
            c = conn.recv(ln - len(data))
            if not c: return
            data += c
        try:
            qname, qtype, qend = qname_of(data); ip = local_ip(qname) if qtype in (1, 28) else None
        except Exception:
            ip = None; qtype = 0; qend = 0
        if ip:
            resp = build_local(data, qend, qtype, ip)
        else:
            resp = None
            for up in UPSTREAM:
                us = socket.socket(); us.settimeout(UPTIMEOUT)
                try:
                    us.connect((up, 53)); us.sendall(h + data)
                    rh = us.recv(2); rln = struct.unpack(">H", rh)[0]; rb = b""
                    while len(rb) < rln:
                        c = us.recv(rln - len(rb))
                        if not c: break
                        rb += c
                    resp = rb; break
                except Exception:
                    continue
                finally:
                    us.close()
            if resp is None: resp = servfail(data)
        conn.sendall(struct.pack(">H", len(resp)) + resp)
    except Exception:
        pass
    finally:
        try: conn.close()
        except Exception: pass

def tcp_server():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((BIND, PORT)); s.listen(32)
    while True:
        try:
            conn, _ = s.accept()
            threading.Thread(target=handle_tcp, args=(conn,), daemon=True).start()
        except Exception:
            time.sleep(0.05)

def stats_writer():
    while True:
        try:
            with LOCK:
                snap = dict(STATS); top = sorted(CLIENTS.items(), key=lambda kv: kv[1], reverse=True)[:12]
            data = {"ts": int(time.time()), "uptime": int(time.time() - START), "hosts": len(HOSTS),
                    "upstream": UPSTREAM, "stats": snap,
                    "top_clients": [{"ip": ip, "n": n} for ip, n in top]}
            with open(os.path.join(BASE, "dns_stats.json"), "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:
            pass
        time.sleep(10)

if __name__ == "__main__":
    threading.Thread(target=stats_writer, daemon=True).start()
    threading.Thread(target=log_writer, daemon=True).start()
    threading.Thread(target=tcp_server, daemon=True).start()
    udp_server()
