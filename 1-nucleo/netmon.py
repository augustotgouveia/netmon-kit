# -*- coding: utf-8 -*-
# NetMon v5 - painel NOC leve (stdlib). Projeto aberto (MIT): github.com/<voce>/netmon-kit. Novidades do v5:
#   1) ALERTAS (motor com maquina de estado + cooldown; envio pronto p/ Telegram)
#   2) HISTORICO em SQLite (banda/cpu/mem/saturacao + sinal das antenas) com graficos
#   3) SAUDE DAS CAMERAS/DVRs (probe RTSP gentil em thread propria)
# Backend v3/v4 preservado; a pagina vem de index.html (lida a cada request -> editar design nao exige restart).
import json, re, socket, subprocess, threading, time, os, sqlite3, hashlib, ssl, base64, urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys, faulthandler
try:
    _elog = open(r"C:\netmon\netmon.err.log", "a", buffering=1, encoding="utf-8", errors="replace")
    sys.stdout = sys.stderr = _elog
    faulthandler.enable(_elog)
    _elog.write("\n===== start %s pid=%d =====\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid()))
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
    CFG = json.load(f)

C        = CFG["cisco"]
INTERVAL = CFG.get("interval", 5)
PORT     = CFG.get("port", 8787)
PLINK    = CFG["plink"]
ANT      = CFG.get("antenna_link")
NAMES    = CFG.get("names", {})
OUI      = CFG.get("oui", {})
LAN_CAP  = CFG.get("lan_cap_mbps", 100)
BIND     = CFG.get("bind", "127.0.0.1")
DOCAUTH  = CFG.get("doc") or {}
SITE     = CFG.get("site_name", "Rede")   # nome que aparece no painel e nos alertas
LAN_PREFIX = (CFG.get("rede") or {}).get("lan", "192.168.0.0/24").rsplit(".", 1)[0] + "."   # so redes /24
OMADA_CFG = CFG.get("omada") or {}
OMADA = {"enabled": bool(OMADA_CFG.get("user")), "site": OMADA_CFG.get("site_name", ""), "aps": [], "ts": 0, "err": ""}
_OM = {"op": None, "token": None}
DVRS     = CFG.get("dvrs", [])
ALERTS   = CFG.get("alerts", {})
DB_PATH       = os.path.join(BASE, "netmon.db")
HIST_DAYS     = CFG.get("history_days", 30)
HIST_WRITE    = CFG.get("history_interval", 30)
CAM_INTERVAL  = CFG.get("cam_interval", 30)
CHECK_INET    = CFG.get("check_inet", True)
INET_PROBE    = ALERTS.get("rules", {}).get("inet_probe", "8.8.8.8")

STATE = {"ok": False, "msg": "conectando...", "ts": 0, "hosts": [], "hist": []}
HIST  = []
LOCK  = threading.Lock()
PSEM  = threading.Semaphore(24)
PREV_DROPS = None
PREV_TS    = 0.0
LAST_DBW   = 0.0
CAMS = {"ts": 0, "dvrs": []}
AUTONAMES = {}   # ip -> nome descoberto via mDNS/NetBIOS (preenche o que responder)
try:
    with open(os.path.join(BASE, "camnames.json"), encoding="utf-8") as _f: CAMNAMES = json.load(_f)
except Exception:
    CAMNAMES = {}   # streamid -> nome dado pelo usuario (persistente no servidor)
try:
    with open(os.path.join(BASE, "camrot.json"), encoding="utf-8") as _f: CAMROT = json.load(_f)
except Exception:
    CAMROT = {}   # streamid -> rotacao em graus (90/180/270)
try:
    with open(os.path.join(BASE, "mappos.json"), encoding="utf-8") as _f: MAPPOS = json.load(_f)
except Exception:
    MAPPOS = {}   # id do no (ip) -> {x,y} fracoes 0..1 (posicao no mapa de enlaces)
try:
    with open(os.path.join(BASE, "maplinks.json"), encoding="utf-8") as _f: MAPEDGES = json.load(_f)
except Exception:
    MAPEDGES = None   # {"edges":[[from,to],...]} editado pelo usuario; None = topologia automatica
try:
    with open(os.path.join(BASE, "mapnodes.json"), encoding="utf-8") as _f: MAPNODES = json.load(_f)
    if not isinstance(MAPNODES, list): MAPNODES = []
except Exception:
    MAPNODES = []   # nos manuais [{id,name,ip,icon}] criados pelo usuario no mapa
ALERT_STATE  = {}     # key -> {"bad": bool, "last": ts}
RECENT       = []     # ultimos eventos p/ o painel
ALOCK = threading.Lock()

CISCO_CMDS = (
    "\n\nenable\n" + C["enable"] + "\nterminal length 0"
    "\nshow processes cpu | include utilization"
    "\nshow memory statistics"
    "\nshow interfaces " + C.get("wan_if", "GigabitEthernet0/1") + " | include rate|input errors|packets input"
    "\nshow interfaces " + C.get("lan_if", "GigabitEthernet0/0") + " | include rate|output drops"
    "\nshow ip dhcp pool " + C.get("dhcp_pool", "LAN") +
    "\nshow ip dhcp binding"
    "\nshow ip nat statistics | include active"
    "\nshow ip arp"
    "\nexit\n"
)

def run_cisco():
    p = subprocess.run([PLINK, "-1", "-batch", "-ssh", "%s@%s" % (C["user"], C["host"]), "-pw", C["password"]],
                       input=CISCO_CMDS, capture_output=True, text=True, timeout=30)
    return p.stdout

def parse_metrics(out):
    m = {}
    g = re.search(r"five seconds: (\d+)%/(\d+)%; one minute: (\d+)%; five minutes: (\d+)%", out)
    if g: m["cpu"] = int(g.group(1)); m["cpu5m"] = int(g.group(4))
    g = re.search(r"Processor\s+\w+\s+(\d+)\s+(\d+)\s+(\d+)", out)
    if g:
        tot, used = int(g.group(1)), int(g.group(2))
        m["mem_pct"] = round(used * 100.0 / tot, 1)
    ins  = re.findall(r"input rate (\d+) bits/sec", out)
    outs = re.findall(r"output rate (\d+) bits/sec", out)
    if len(ins) >= 2 and len(outs) >= 2:
        m["wan_down"] = round(int(ins[0]) / 1e6, 3); m["wan_up"] = round(int(outs[0]) / 1e6, 3)
        m["lan_up"]   = round(int(ins[1]) / 1e6, 3); m["lan_down"] = round(int(outs[1]) / 1e6, 3)
        m["lan_sat"]  = round(max(m["lan_down"], m["lan_up"]) / float(LAN_CAP) * 100.0, 1)
    g = re.search(r"Total output drops:\s*(\d+)", out)
    if g: m["drops_total"] = int(g.group(1))
    g = re.search(r"Leased addresses\s+:\s+(\d+)", out)
    if g: m["dhcp_leased"] = int(g.group(1))
    g = re.search(r"Total active translations:\s+(\d+)", out)
    if g: m["nat"] = int(g.group(1))
    # --- qualidade do enlace WAN (Gi0/1) ---
    # Contadores BRUTOS. Viram taxa em crc_quality(), mais abaixo.
    g = re.search(r"(\d+) input errors, (\d+) CRC, (\d+) frame", out)
    if g: m["crc_tot"] = int(g.group(2)); m["frame_tot"] = int(g.group(3))
    g = re.search(r"(\d+) packets input", out)
    if g: m["pkts_in"] = int(g.group(1))
    if not m: raise RuntimeError("sem dados do roteador (login/plink?)")
    return m

_CRC_REF = {"ts": 0.0, "crc": None, "frame": None, "pkts": None}
_CRC_JANELA = 1800.0   # segundos entre recalculos
_CRC_MIN_PKTS = 200000 # abaixo disto a amostra nao vale (1 erro vira ppm enorme)

def crc_quality(m):
    """Anota m['crc_ppm'] = erros por milhao de pacotes na WAN.

    Mantem o ultimo valor calculado entre janelas, para o painel nao piscar.
    Se o Cisco reiniciar, os contadores zeram: detectado por queda no total,
    e a referencia e refeita em vez de gerar numero negativo.
    """
    import time as _t
    crc, fr, pk = m.get("crc_tot"), m.get("frame_tot"), m.get("pkts_in")
    if crc is None or fr is None or pk is None:
        return
    r = _CRC_REF
    agora = _t.time()
    if r["crc"] is None or pk < (r["pkts"] or 0) or crc < (r["crc"] or 0):
        # primeira leitura, ou o Cisco reiniciou e zerou tudo
        r.update({"ts": agora, "crc": crc, "frame": fr, "pkts": pk})
        return
    if agora - r["ts"] < _CRC_JANELA:
        if _CRC_REF.get("ultimo") is not None:
            m["crc_ppm"] = _CRC_REF["ultimo"]
        return
    d_pk = pk - r["pkts"]
    d_er = (crc - r["crc"]) + (fr - r["frame"])
    if d_pk >= _CRC_MIN_PKTS:
        m["crc_ppm"] = round(d_er * 1e6 / d_pk, 2)
        _CRC_REF["ultimo"] = m["crc_ppm"]
    r.update({"ts": agora, "crc": crc, "frame": fr, "pkts": pk})

def parse_arp(out):
    return [{"ip": m.group(1), "age": m.group(2), "mac": m.group(3).lower()}
            for m in re.finditer(r"Internet\s+(192\.168\.2\.\d+)\s+(\S+)\s+([0-9a-fA-F.]{14})\s+ARPA", out)]

def mac_colon(m):
    h = m.replace(".", "")
    return ":".join(h[i:i + 2] for i in range(0, 12, 2)) if len(h) == 12 else m

def parse_bindings(out):
    b = {}
    for line in out.splitlines():
        m = re.match(r"\s*(192\.168\.2\.\d+)\s+([0-9a-fA-F.]+)\s+(.+?)\s+(Automatic|Manual|Static)\s*$", line)
        if m: b[m.group(1)] = {"type": m.group(4), "lease": m.group(3).strip(), "cid": m.group(2)}
    return b

def name_for(ip): return NAMES.get(ip) or AUTONAMES.get(ip) or ("host ." + ip.split(".")[3])
def vendor_for(mac):
    if mac[:7] in OUI: return OUI[mac[:7]]
    try:
        if int(mac[:2], 16) & 2: return "aleatorio"
    except Exception: pass
    return "?"

def ping(ip):
    try:
        r = subprocess.run(["ping", "-n", "1", "-w", "1000", ip], capture_output=True, text=True, timeout=4)
        if ("TTL=" in r.stdout) or ("ttl=" in r.stdout.lower()):
            g = re.search(r"[=<](\d+)ms", r.stdout); return True, (int(g.group(1)) if g else None)
        return False, None
    except Exception: return False, None

def tcp_probe(ip, port, timeout=1.5):
    t = time.time()
    try:
        s = socket.create_connection((ip, int(port)), timeout); s.close(); return True, round((time.time()-t)*1000)
    except Exception: return False, None

def poll_hosts(arp):
    out = [None]*len(arp)
    def work(i, a):
        with PSEM: ok, ms = ping(a["ip"])
        agen = int(a["age"]) if a["age"].isdigit() else None
        out[i] = {"ip": a["ip"], "name": name_for(a["ip"]), "vendor": vendor_for(a["mac"]),
                  "up": ok or (agen is not None and agen < 20), "ms": ms,
                  "mac": mac_colon(a["mac"]), "age": agen}
    ts = [threading.Thread(target=work, args=(i, a)) for i, a in enumerate(arp)]
    for t in ts: t.start()
    for t in ts: t.join()
    out.sort(key=lambda h: int(h["ip"].split(".")[3]))
    return out

# ---- SNMP v2c minimo ----
def _elen(n):
    if n < 0x80: return bytes([n])
    b = []
    while n: b.insert(0, n & 0xff); n >>= 8
    return bytes([0x80 | len(b)]) + bytes(b)
def _tlv(t, v): return bytes([t]) + _elen(len(v)) + v
def _eint(n):
    if n == 0: return _tlv(2, b"\x00")
    b = []; x = n
    while x: b.insert(0, x & 0xff); x >>= 8
    if b[0] & 0x80: b.insert(0, 0)
    return _tlv(2, bytes(b))
def _eoid(s):
    p = [int(x) for x in s.split(".")]; body = [40*p[0]+p[1]]
    for v in p[2:]:
        if v < 128: body.append(v)
        else:
            st = []
            while v: st.insert(0, v & 0x7f); v >>= 7
            for i in range(len(st)-1): st[i] |= 0x80
            body += st
    return _tlv(6, bytes(body))
def _m(comm, oid, pt):
    vb = _tlv(0x30, _eoid(oid) + _tlv(5, b""))
    return _tlv(0x30, _eint(1) + _tlv(4, comm) + _tlv(pt, _eint(1)+_eint(0)+_eint(0)+_tlv(0x30, vb)))
def _rt(d, i):
    t = d[i]; i += 1; l = d[i]; i += 1
    if l & 0x80:
        n = l & 0x7f; l = 0
        for _ in range(n): l = (l << 8) | d[i]; i += 1
    return t, d[i:i+l], i+l
def _do(b):
    p = [b[0]//40, b[0]%40]; v = 0
    for x in b[1:]:
        v = (v << 7) | (x & 0x7f)
        if not (x & 0x80): p.append(v); v = 0
    return ".".join(map(str, p))
def _vr(t, v):
    if t in (2, 0x41, 0x42, 0x43, 0x46):
        n = 0
        for x in v: n = (n << 8) | x
        if t == 2 and v and v[0] & 0x80: n -= 1 << (8*len(v))
        return n
    if t == 4:
        try: return v.decode("utf-8", "replace")
        except Exception: return v.hex()
    if t == 6: return _do(v)
    if t in (0x80, 0x81, 0x82): return None
    return v.hex()
def _pa(d):
    _, seq, _ = _rt(d, 0); i = 0
    _, _, i = _rt(seq, i); _, _, i = _rt(seq, i)
    _, pdu, _ = _rt(seq, i); j = 0
    for _ in range(3): _, _, j = _rt(pdu, j)
    _, vbl, _ = _rt(pdu, j); _, vb, _ = _rt(vbl, 0)
    _, ob, mm = _rt(vb, 0); vt, vv, _ = _rt(vb, mm)
    return _do(ob), vt, vv
def snmp_get(ip, comm, oid, timeout=2.5):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(timeout)
    try:
        s.sendto(_m(comm.encode(), oid, 0xA0), (ip, 161)); o, t, v = _pa(s.recvfrom(2048)[0]); return _vr(t, v)
    except Exception: return None
    finally: s.close()
def snmp_walk(ip, comm, root, maxn=40, timeout=2.5):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(timeout)
    oid, out = root, []
    try:
        for _ in range(maxn):
            s.sendto(_m(comm.encode(), oid, 0xA1), (ip, 161)); o, t, v = _pa(s.recvfrom(2048)[0])
            if t in (0x80, 0x81, 0x82) or not (o == root or o.startswith(root+".")): break
            out.append((o, _vr(t, v))); oid = o
    except Exception: pass
    finally: s.close()
    return out

def parse_dbm(s):
    if not s: return None
    nums = re.findall(r"-?\d+", str(s)); return min(int(n) for n in nums) if nums else None
def parse_pct(s):
    if not s: return None
    m = re.search(r"[\d.]+", str(s)); return float(m.group()) if m else None
def qual(dbm):
    if dbm is None: return "down"
    if dbm >= -65: return "good"
    if dbm >= -78: return "warn"
    return "bad"

def poll_antenna(arp_map):
    if not ANT: return None
    comm = ANT.get("snmp_community"); base = ANT.get("base")
    res = {"name": ANT.get("name", "Enlace"), "base": None, "remotes": [], "link_up": False}
    if comm and base:
        model = snmp_get(base["ip"], comm, "1.3.6.1.4.1.26138.1.1.0")
        if model is not None:
            res["base"] = {"name": base.get("name", "Base"), "ip": base["ip"], "up": True, "model": model,
                "fw": snmp_get(base["ip"], comm, "1.3.6.1.4.1.26138.1.2.0"),
                "channel": snmp_get(base["ip"], comm, "1.3.6.1.4.1.26138.3.6.0"),
                "ssid": snmp_get(base["ip"], comm, "1.3.6.1.4.1.26138.3.7.0"),
                "snr": snmp_get(base["ip"], comm, "1.3.6.1.4.1.26138.3.3.0")}
            rows = {}
            for oid, val in snmp_walk(base["ip"], comm, "1.3.6.1.4.1.26138.3.10"):
                rest = oid.split("26138.3.10.1.", 1)[-1]; pp = rest.split(".", 1)
                if len(pp) == 2: rows.setdefault(pp[1], {})[pp[0]] = val
            sta = {}
            for idx, cols in rows.items():
                mac = str(cols.get("1", "")).lower()
                if mac: sta[mac] = {"name": cols.get("2"), "rate": cols.get("3"), "signal": cols.get("4"), "ccq": cols.get("5")}
            for r in ANT.get("remotes", []):
                stt = sta.get(r["mac"].lower())
                if stt:
                    dbm = parse_dbm(stt.get("signal"))
                    res["remotes"].append({"name": r["name"], "ip": r["ip"], "up": True, "signal": stt.get("signal"),
                        "dbm": dbm, "rate": stt.get("rate"), "ccq": parse_pct(stt.get("ccq")), "quality": qual(dbm)})
                else:
                    res["remotes"].append({"name": r["name"], "ip": r["ip"], "up": False, "signal": None,
                        "dbm": None, "rate": None, "ccq": None, "quality": "down"})
            res["link_up"] = True
            return res
    for r in ANT.get("remotes", []):
        up, _ = tcp_probe(r["ip"], 80)
        if not up: up = ping(r["ip"])[0]
        if not up:
            ag = arp_map.get(r["ip"]); up = ag is not None and ag < 20
        res["remotes"].append({"name": r["name"], "ip": r["ip"], "up": up, "quality": "good" if up else "down",
            "signal": None, "dbm": None, "rate": None, "ccq": None})
    if base:
        bu, _ = tcp_probe(base["ip"], 80)
        res["base"] = {"name": base.get("name", "Base"), "ip": base["ip"], "up": bu, "model": "", "snr": ""}
    res["link_up"] = bool(res["base"] and res["base"].get("up"))
    return res

# ---- SAUDE DAS CAMERAS (probe RTSP Digest, gentil) ----
def rtsp_describe(ip, user, pw, ch=1, sub=1, port=554, timeout=6):
    t0 = time.time()
    try:
        s = socket.create_connection((ip, port), timeout)
    except Exception:
        return ("offline", "", None)
    s.settimeout(timeout)
    url = "rtsp://%s:%d/cam/realmonitor?channel=%d&subtype=%d" % (ip, port, ch, sub)
    def send(cseq, auth=""):
        msg = "DESCRIBE %s RTSP/1.0\r\nCSeq: %d\r\nAccept: application/sdp\r\n%s\r\n" % (url, cseq, auth)
        s.sendall(msg.encode("latin-1", "replace")); b = b""
        try:
            while b"\r\n\r\n" not in b:
                c = s.recv(2048)
                if not c: break
                b += c
        except Exception: pass
        return b.decode("latin1", "replace")
    r = send(1)
    if " 401" in r.split("\r\n")[0]:
        mm = re.search(r'realm="([^"]+)"', r); nn = re.search(r'nonce="([^"]+)"', r)
        if mm and nn:
            realm, nonce = mm.group(1), nn.group(1)
            ha1 = hashlib.md5(("%s:%s:%s" % (user, realm, pw)).encode()).hexdigest()
            ha2 = hashlib.md5(("DESCRIBE:%s" % url).encode()).hexdigest()
            resp = hashlib.md5(("%s:%s:%s" % (ha1, nonce, ha2)).encode()).hexdigest()
            auth = ('Authorization: Digest username="%s", realm="%s", nonce="%s", uri="%s", response="%s"\r\n'
                    % (user, realm, nonce, url, resp))
            r = send(2, auth)
    try: s.close()
    except Exception: pass
    line = r.split("\r\n")[0] if r else ""
    g = re.search(r"RTSP/1\.0\s+(\d{3})", line)
    code = g.group(1) if g else ("timeout" if not line else "?")
    cod = ",".join(re.findall(r"a=rtpmap:\d+\s+([A-Za-z0-9]+)/", r)) if code == "200" else ""
    return (code, cod, round((time.time() - t0) * 1000))

def dvr_storage(d, timeout=6):
    # saude do HD via API CGI do DVR (getDeviceAllInfo) - reaproveita as creds dos snapshots
    ip = d["ip"]; user = d.get("user", ""); pw = d.get("pass", "")
    url = "http://%s/cgi-bin/storageDevice.cgi?action=getDeviceAllInfo" % ip
    pm = urllib.request.HTTPPasswordMgrWithDefaultRealm(); pm.add_password(None, "http://%s" % ip, user, pw)
    op = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(pm))
    try:
        txt = op.open(url, timeout=timeout).read().decode("latin1", "replace")
    except Exception:
        return None
    disks = set(); err = False
    for line in txt.splitlines():
        line = line.strip()
        m = re.search(r"\.Path=/dev/(sd[a-z])", line)
        if m: disks.add(m.group(1))
        elif line.endswith(".IsError=true"):
            err = True
    if not disks:
        return None
    return {"disks": len(disks), "error": err}

def dvr_reboot(d, timeout=8):
    # manda reboot via API CGI do DVR (magicBox.cgi). Só funciona se a WEB do DVR responde.
    ip = d["ip"]; user = d.get("user", ""); pw = d.get("pass", "")
    url = "http://%s/cgi-bin/magicBox.cgi?action=reboot" % ip
    pm = urllib.request.HTTPPasswordMgrWithDefaultRealm(); pm.add_password(None, "http://%s" % ip, user, pw)
    op = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(pm))
    try:
        op.open(url, timeout=timeout)
        return True, "comando de reboot enviado — volta em ~1-2 min"
    except Exception as e:
        return False, "DVR nao respondeu (%s) — travou feio, reinicie na tomada" % type(e).__name__

def probe_cameras():
    res = {"ts": time.time(), "dvrs": []}
    out = [None] * len(DVRS)
    def work(i, d):
        code, cod, ms = rtsp_describe(d["ip"], d.get("user", ""), d.get("pass", ""))
        if code == "200": status = "up"
        elif code in ("401", "403"): status = "warn"
        else: status = "down"
        hdd = dvr_storage(d) if status != "down" else None
        out[i] = {"name": d.get("name", "DVR"), "ip": d["ip"], "channels": d.get("channels"),
                  "code": code, "codec": cod, "ms": ms, "status": status, "hdd": hdd}
    ts = [threading.Thread(target=work, args=(i, d)) for i, d in enumerate(DVRS)]
    for t in ts: t.start()
    for t in ts: t.join()
    res["dvrs"] = [o for o in out if o]
    return res

def cam_poller():
    global CAMS
    while True:
        try:
            c = probe_cameras()
            with LOCK: CAMS = c
            alert_eval_cameras(c)
        except Exception: pass
        time.sleep(CAM_INTERVAL)

# ---- HISTORICO (SQLite) ----
def db_init():
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE IF NOT EXISTS metrics(ts INTEGER PRIMARY KEY, cpu REAL, mem REAL, "
                    "wan_down REAL, wan_up REAL, lan_down REAL, lan_up REAL, lan_sat REAL, hosts_up INTEGER, hosts_total INTEGER)")
        con.execute("CREATE TABLE IF NOT EXISTS ant_hist(ts INTEGER, name TEXT, dbm INTEGER, ccq REAL, up INTEGER)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_ant_ts ON ant_hist(ts)")
        con.execute("CREATE TABLE IF NOT EXISTS events(ts INTEGER, level TEXT, key TEXT, msg TEXT)")
        con.commit()
    finally: con.close()

def db_write(st):
    try:
        con = sqlite3.connect(DB_PATH); ts = int(st["ts"])
        con.execute("INSERT OR REPLACE INTO metrics VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ts, st.get("cpu5m", st.get("cpu")), st.get("mem_pct"), st.get("wan_down"), st.get("wan_up"),
             st.get("lan_down"), st.get("lan_up"), st.get("lan_sat"), st.get("hosts_up"), st.get("hosts_total")))
        ant = st.get("ant") or {}
        for r in (ant.get("remotes") or []):
            con.execute("INSERT INTO ant_hist VALUES(?,?,?,?,?)",
                        (ts, r.get("name"), r.get("dbm"), r.get("ccq"), 1 if r.get("up") else 0))
        if ts % 3600 < HIST_WRITE:   # poda ~1x por hora
            cut = ts - HIST_DAYS * 86400
            con.execute("DELETE FROM metrics WHERE ts<?", (cut,))
            con.execute("DELETE FROM ant_hist WHERE ts<?", (cut,))
            con.execute("DELETE FROM events WHERE ts<?", (cut,))
        con.commit(); con.close()
    except Exception: pass

def _rnd(x): return None if x is None else round(x, 3)

def history(rng):
    spans = {"1h": 3600, "6h": 6*3600, "24h": 86400, "7d": 7*86400, "30d": 30*86400}
    span = spans.get(rng, 86400); now = int(time.time()); start = now - span
    bw = max(1, span // 160)
    out = {"range": rng, "metrics": {k: [] for k in ("ts", "wan_down", "wan_up", "lan_sat", "cpu", "mem")},
           "ant": {}, "events": []}
    try:
        con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
        for r in con.execute(
                "SELECT (ts/?)*? b, AVG(wan_down) wd, AVG(wan_up) wu, AVG(lan_sat) ls, AVG(cpu) cpu, AVG(mem) mem "
                "FROM metrics WHERE ts>=? GROUP BY b ORDER BY b", (bw, bw, start)):
            mt = out["metrics"]; mt["ts"].append(r["b"]); mt["wan_down"].append(_rnd(r["wd"]))
            mt["wan_up"].append(_rnd(r["wu"])); mt["lan_sat"].append(_rnd(r["ls"]))
            mt["cpu"].append(_rnd(r["cpu"])); mt["mem"].append(_rnd(r["mem"]))
        for r in con.execute(
                "SELECT (ts/?)*? b, name, AVG(dbm) dbm, AVG(ccq) ccq FROM ant_hist WHERE ts>=? "
                "GROUP BY b,name ORDER BY b", (bw, bw, start)):
            a = out["ant"].setdefault(r["name"], {"ts": [], "dbm": [], "ccq": []})
            a["ts"].append(r["b"]); a["dbm"].append(_rnd(r["dbm"])); a["ccq"].append(_rnd(r["ccq"]))
        for r in con.execute("SELECT ts,level,key,msg FROM events ORDER BY ts DESC LIMIT 50"):
            out["events"].append({"ts": r["ts"], "level": r["level"], "key": r["key"], "msg": r["msg"]})
        con.close()
    except Exception as e:
        out["error"] = str(e)
    return out

# ---- ALERTAS ----
_TGCTX = None
def _tg_ctx():
    # store de raiz do Server 2016 esta desatualizado -> usa CA bundle proprio (cacert.pem) se existir
    global _TGCTX
    if _TGCTX is None:
        ca = os.path.join(BASE, "cacert.pem")
        try: _TGCTX = ssl.create_default_context(cafile=ca) if os.path.exists(ca) else ssl.create_default_context()
        except Exception: _TGCTX = ssl.create_default_context()
    return _TGCTX

def _log(m):
    try:
        with open(os.path.join(BASE, "tg.log"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), m))
    except Exception: pass

_TG_ULT_ERRO = [None, 0.0]   # ultima falha ja registrada (texto, quando):
                             # evita encher o tg.log quando o link fica fora horas

def tg_api(method, params, timeout=30, tentativas=1):
    tg = ALERTS.get("telegram", {}); tok = tg.get("token")
    if not tok: return None
    data = urllib.parse.urlencode(params).encode()
    # ANTES AQUI ERA "except Exception: return None" -- toda falha sumia calada.
    # O trajeto ate o Telegram e internacional e cai sozinho: em 01/10/2026 as 4
    # faixas do Telegram estavam fora enquanto google e 8.8.8.8 respondiam normal
    # (esses dois sao servidos de dentro do Brasil). Falhar as vezes e esperado;
    # falhar EM SILENCIO nao.
    erro = None
    for n in range(1, tentativas + 1):
        try:
            r = urllib.request.urlopen("https://api.telegram.org/bot%s/%s" % (tok, method),
                                       data=data, timeout=timeout, context=_tg_ctx())
            resp = json.loads(r.read().decode("utf-8", "replace"))
            if isinstance(resp, dict) and not resp.get("ok", True):
                _log("TG RECUSOU %s: %s" % (method, str(resp.get("description"))[:200]))
            return resp
        except Exception as e:
            erro = "%s: %s" % (type(e).__name__, str(e)[:150])
            if n < tentativas:
                time.sleep(2 * n)
    agora = time.time()
    if erro != _TG_ULT_ERRO[0] or (agora - _TG_ULT_ERRO[1]) > 300:
        _log("TG FALHOU %s apos %d tentativa(s): %s" % (method, tentativas, erro))
        _TG_ULT_ERRO[0] = erro; _TG_ULT_ERRO[1] = agora
    return None

LOG_FILES = {"err": "netmon.err.log", "watchdog": "watchdog.log", "tg": "tg.log"}

def logs_query(qs):
    def _1(name, d=""):
        v = qs.get(name, [d]); return v[0] if v else d
    def _int(name, d):
        try: return int(_1(name, str(d)))
        except Exception: return d
    src = _1("src", "events")
    if src in LOG_FILES:
        try:
            with open(os.path.join(BASE, LOG_FILES[src]), encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except Exception:
            lines = []
        n = min(max(_int("limit", 400), 1), 5000)
        return {"src": src, "text": "\n".join(lines[-n:]), "lines": min(len(lines), n), "total": len(lines)}
    spans = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
    where = []; args = []
    rng = _1("range", "24h")
    if rng in spans:
        where.append("ts >= ?"); args.append(int(time.time()) - spans[rng])
    levels = [x for x in _1("level", "").split(",") if x in ("alert", "warn", "ok")]
    if levels:
        where.append("level IN (%s)" % ",".join(["?"] * len(levels))); args += levels
    cat = _1("cat", "").strip()
    if cat:
        where.append("key LIKE ?"); args.append(cat + "%")
    q = _1("q", "").strip()
    if q:
        where.append("(msg LIKE ? OR key LIKE ?)"); args += ["%" + q + "%", "%" + q + "%"]
    lim = min(max(_int("limit", 300), 1), 2000)
    sql = "SELECT ts, level, key, msg FROM events"
    if where: sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC LIMIT ?"; args.append(lim)
    rows = []; cats = set()
    try:
        con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
        for r in con.execute(sql, args):
            k = r["key"] or ""
            rows.append({"ts": r["ts"], "level": r["level"], "key": k, "msg": r["msg"], "muted": _muted(k)})
        for r in con.execute("SELECT DISTINCT key FROM events"):
            cats.add((r[0] or "").split(":")[0])
        con.close()
    except Exception:
        pass
    return {"src": "events", "rows": rows, "count": len(rows), "cats": sorted(c for c in cats if c)}

def _omada_login():
    import http.cookiejar
    cfg = OMADA_CFG; host = cfg.get("host", "https://127.0.0.1:8043"); cid = cfg.get("cid")
    c = ssl.create_default_context(); c.check_hostname = False; c.verify_mode = ssl.CERT_NONE
    op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=c), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    b = json.dumps({"username": cfg.get("user"), "password": cfg.get("pass")}).encode()
    r = op.open(urllib.request.Request(host + "/%s/api/v2/login" % cid, data=b, headers={"Content-Type": "application/json"}, method="POST"), timeout=10)
    lg = json.loads(r.read().decode("utf-8", "replace"))
    token = (lg.get("result") or {}).get("token")
    if token: _OM["op"] = op; _OM["token"] = token
    return token, lg.get("msg", lg.get("errorCode"))

def omada_poll_once():
    cfg = OMADA_CFG; host = cfg.get("host", "https://127.0.0.1:8043"); cid = cfg.get("cid"); site = cfg.get("site")
    if not (cid and site and cfg.get("user") and cfg.get("pass")): return None
    def get_devices():
        req = urllib.request.Request(host + "/%s/api/v2/sites/%s/devices" % (cid, site), headers={"Content-Type": "application/json", "Csrf-Token": _OM["token"]})
        r = _OM["op"].open(req, timeout=10); return json.loads(r.read().decode("utf-8", "replace"))
    if not _OM["op"] or not _OM["token"]:
        tok, msg = _omada_login()
        if not tok: return {"err": "login: %s" % msg}
    try:
        dv = get_devices()
        if dv.get("errorCode") not in (0, None):
            tok, msg = _omada_login()
            if not tok: return {"err": "login: %s" % msg}
            dv = get_devices()
    except Exception:
        tok, msg = _omada_login()
        if not tok: return {"err": "login: %s" % msg}
        dv = get_devices()
    devs = dv.get("result") or []
    aps = []
    for d in devs:
        aps.append({"name": d.get("name") or d.get("mac"), "type": d.get("type"), "ip": d.get("ip"),
                    "mac": d.get("mac"), "model": d.get("model"), "status": d.get("status"),
                    "up": d.get("status", 0) != 0, "clients": d.get("clientNum", 0),
                    "cpu": d.get("cpuUtil"), "mem": d.get("memUtil")})
    return {"aps": aps, "err": ""}

def omada_poller():
    iv = int(OMADA_CFG.get("interval", 30))
    while True:
        try:
            res = omada_poll_once()
            if res is not None:
                with LOCK:
                    if not res.get("err"): OMADA["aps"] = res.get("aps", [])
                    OMADA["err"] = res.get("err", ""); OMADA["ts"] = int(time.time())
        except Exception as e:
            with LOCK:
                OMADA["err"] = type(e).__name__; OMADA["ts"] = int(time.time())
        time.sleep(iv)

def dnslog_query(qs):
    def _1(n, d=""):
        v = qs.get(n, [d]); return v[0] if v else d
    def _int(n, d):
        try: return int(_1(n, str(d)))
        except Exception: return d
    spans = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "3d": 3 * 86400, "7d": 7 * 86400}
    since = int(time.time()) - spans.get(_1("range", "24h"), 86400)
    client = _1("client", "").strip()
    q = _1("q", "").strip()
    lim = min(max(_int("limit", 300), 1), 2000)
    dbp = os.path.join(BASE, "dns_log.db")
    out = {"names": [], "hosts": [], "total": 0}
    if not os.path.exists(dbp): return out
    try:
        con = sqlite3.connect(dbp); con.row_factory = sqlite3.Row
        for r in con.execute("SELECT client, COUNT(*) n FROM queries WHERE ts>=? GROUP BY client ORDER BY n DESC LIMIT 80", (since,)):
            nm = ""
            try: nm = name_for(r["client"])
            except Exception: nm = ""
            out["hosts"].append({"ip": r["client"], "name": nm, "n": r["n"]})
        where = ["ts>=?"]; args = [since]
        if client: where.append("client=?"); args.append(client)
        if q: where.append("name LIKE ?"); args.append("%" + q + "%")
        sql = "SELECT name, COUNT(*) n, MAX(ts) last FROM queries WHERE " + " AND ".join(where) + " GROUP BY name ORDER BY n DESC LIMIT ?"
        args.append(lim)
        for r in con.execute(sql, args):
            out["names"].append({"name": r["name"], "n": r["n"], "last": r["last"]})
        row = con.execute("SELECT COUNT(*) FROM queries WHERE ts>=?", (since,)).fetchone()
        out["total"] = row[0] if row else 0
        con.close()
    except Exception:
        pass
    return out

def tg_send(text, chat=None):
    chat = chat or ALERTS.get("telegram", {}).get("chat_id")
    if not chat: return False
    # alerta nao pode se perder por um solucao de 30 s no link internacional ->
    # 3 tentativas aqui. O getUpdates continua com 1 pra nao travar o laco.
    return bool(tg_api("sendMessage", {"chat_id": chat, "text": text,
                                       "parse_mode": "HTML", "disable_web_page_preview": "true"},
                       tentativas=3))

# emojis em escape p/ manter a fonte ASCII-safe
EMO = {"g": "\U0001F7E2", "y": "\U0001F7E1", "r": "\U0001F534", "b": "⚫", "warn": "⚠",
       "net": "\U0001F310", "pc": "\U0001F5A5", "plug": "\U0001F50C", "sat": "\U0001F4E1",
       "cam": "\U0001F3A5", "bell": "\U0001F514", "wifi": "\U0001F4F6"}
def _qemo(q): return {"good": EMO["g"], "warn": EMO["y"], "bad": EMO["r"], "down": EMO["b"]}.get(q, "")
def _semo(s): return {"up": EMO["g"], "warn": EMO["y"], "down": EMO["r"]}.get(s, "")

def omada_text():
    with LOCK: om = dict(OMADA)
    if not om.get("enabled"): return "Omada nao configurado."
    if om.get("err"): return "%s Omada: erro (%s)" % (EMO["warn"], om.get("err"))
    aps = om.get("aps") or []
    if not aps: return "Omada ligado, mas nenhum AP no site ainda."
    L = ["%s <b>APs Omada</b> - site %s" % (EMO["wifi"], om.get("site", ""))]
    tot = 0
    for a in aps:
        tot += int(a.get("clients") or 0)
        L.append("%s <b>%s</b>  %s  %s  %s cli" % (
            EMO["g"] if a.get("up") else EMO["r"], a.get("name"),
            a.get("ip") or "-", a.get("model") or "", a.get("clients", 0)))
    L.append("%s Total: %d AP / %d clientes" % (EMO["plug"], len(aps), tot))
    return "\n".join(L)

def status_text():
    with LOCK: m = dict(STATE); cams = dict(CAMS); om = dict(OMADA)
    with ALOCK: last = RECENT[0] if RECENT else None
    if not m.get("ts"): return "NetMon iniciando, tenta de novo em instantes."
    ant = m.get("ant") or {}; rem = ant.get("remotes") or []
    rdown = any(r.get("quality") == "down" for r in rem) or bool(ant.get("base") and not ant["base"].get("up"))
    camsdown = any(d.get("status") == "down" for d in cams.get("dvrs", []))
    apsdown = bool(om.get("enabled")) and bool(om.get("aps")) and any(not a.get("up") for a in om.get("aps", []))
    mn = [n for n in (MAPNODES or []) if n.get("ip")]
    _hm = {h["ip"]: h for h in m.get("hosts", [])}
    mndown = any(not (_hm.get(n["ip"]) and _hm[n["ip"]].get("up")) for n in mn)
    cpu = m.get("cpu") or 0; mem = m.get("mem_pct") or 0; sat = m.get("lan_sat") or 0
    if (not m.get("ok")) or rdown or camsdown or (m.get("inet_up") is False) or cpu > 90 or mem > 92: head = EMO["r"]
    elif any(r.get("quality") in ("warn", "bad") for r in rem) or sat > 75 or mndown or apsdown: head = EMO["y"]
    else: head = EMO["g"]
    L = ["%s <b>NOC %s</b>" % (head, SITE)]
    L.append("%s Internet: <b>%s</b>  ↓%.2f ↑%.2f Mb/s" % (
        EMO["net"], ("OK" if m.get("inet_up") else "CAIU"), (m.get("wan_down") or 0), (m.get("wan_up") or 0)))
    L.append("%s Cisco: CPU %s%% / Mem %s%% / sat %s%%" % (EMO["pc"], m.get("cpu", "-"), m.get("mem_pct", "-"), m.get("lan_sat", "-")))
    L.append("%s Hosts %s/%s on / DHCP %s / NAT %s" % (EMO["plug"], m.get("hosts_up", "-"), m.get("hosts_total", "-"), m.get("dhcp_leased", "-"), m.get("nat", "-")))
    if rem:
        L.append("%s Enlace: %s" % (EMO["sat"], " / ".join(
            "%s %s%s" % (_qemo(r.get("quality")), r["name"], (" %sdBm" % r["dbm"] if r.get("dbm") is not None else "")) for r in rem)))
    if cams.get("dvrs"):
        L.append("%s DVRs: %s" % (EMO["cam"], " / ".join("%s %s" % (_semo(d["status"]), d["name"]) for d in cams["dvrs"])))
    _aps = om.get("aps") or []
    if om.get("enabled") and _aps:
        L.append("%s APs: %s" % (EMO["wifi"], " / ".join(
            "%s %s(%s)" % (EMO["g"] if a.get("up") else EMO["r"], a.get("name"), a.get("clients", 0)) for a in _aps)))
    elif om.get("enabled") and om.get("err"):
        L.append("%s APs: erro" % EMO["wifi"])
    if mn:
        L.append("\U0001F4CD Mapa: %s" % " / ".join(
            "%s %s" % (EMO["g"] if (_hm.get(n["ip"]) and _hm[n["ip"]].get("up")) else EMO["r"], n.get("name") or n["ip"]) for n in mn))
    if not m.get("ok"): L.append("%s %s" % (EMO["warn"], m.get("msg", "")))
    if last: L.append("%s ult.: %s" % (EMO["bell"], last["msg"]))
    return "\n".join(L)

def cameras_text():
    with LOCK: cams = dict(CAMS)
    d = cams.get("dvrs", [])
    if not d: return "Sem DVRs configurados."
    L = ["%s <b>Cameras / DVRs</b>" % EMO["cam"]]
    for v in d:
        extra = (v.get("codec") or "") + (" %sms" % v["ms"] if v.get("ms") is not None else "")
        h = v.get("hdd")
        hs = "" if not h else (" | HD %dx %s" % (h["disks"], "ERRO" if h.get("error") else "ok"))
        L.append("%s %s (%s) - %s canais - %s%s" % (_semo(v["status"]), v["name"], v["ip"], v.get("channels", "?"), (extra.strip() or v.get("code", "")), hs))
    return "\n".join(L)

def snapshot(d, ch=1, timeout=10):
    # JPEG direto do DVR via /cgi-bin/snapshot.cgi (Digest) - nao precisa de ffmpeg
    ip = d["ip"]; user = d.get("user", ""); pw = d.get("pass", "")
    url = "http://%s/cgi-bin/snapshot.cgi?channel=%d" % (ip, ch)
    pm = urllib.request.HTTPPasswordMgrWithDefaultRealm(); pm.add_password(None, "http://%s" % ip, user, pw)
    op = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(pm))
    try:
        data = op.open(url, timeout=timeout).read()
        return data if (len(data) > 1000 and data[:2] == b"\xff\xd8") else None
    except Exception:
        return None

def tg_send_photo(chat, jpeg, caption=""):
    tg = ALERTS.get("telegram", {}); tok = tg.get("token")
    if not tok or not chat: return False
    bd = "----nm" + hashlib.md5(jpeg[:128]).hexdigest()[:18]
    def fld(n, v): return ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (bd, n, v)).encode()
    body = fld("chat_id", str(chat))
    if caption: body += fld("caption", caption)
    body += ("--%s\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"cam.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n" % bd).encode()
    body += jpeg + b"\r\n" + ("--%s--\r\n" % bd).encode()
    try:
        req = urllib.request.Request("https://api.telegram.org/bot%s/sendPhoto" % tok, data=body,
                                     headers={"Content-Type": "multipart/form-data; boundary=%s" % bd})
        urllib.request.urlopen(req, timeout=25, context=_tg_ctx()); return True
    except Exception:
        return False

def tg_send_album(chat, items):
    # items = [(jpeg, caption), ...] -> sendMediaGroup (2..10). 1 item cai no sendPhoto.
    tg = ALERTS.get("telegram", {}); tok = tg.get("token")
    if not tok or not chat or not items: return False
    if len(items) == 1: return tg_send_photo(chat, items[0][0], items[0][1])
    bd = "----nm" + hashlib.md5(items[0][0][:64]).hexdigest()[:18]
    def fld(n, v): return ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (bd, n, v)).encode()
    media = [{"type": "photo", "media": "attach://photo%d" % i, "caption": cap} for i, (jp, cap) in enumerate(items)]
    body = fld("chat_id", str(chat)) + fld("media", json.dumps(media))
    for i, (jp, cap) in enumerate(items):
        body += ("--%s\r\nContent-Disposition: form-data; name=\"photo%d\"; filename=\"c%d.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n" % (bd, i, i)).encode()
        body += jp + b"\r\n"
    body += ("--%s--\r\n" % bd).encode()
    try:
        req = urllib.request.Request("https://api.telegram.org/bot%s/sendMediaGroup" % tok, data=body,
                                     headers={"Content-Type": "multipart/form-data; boundary=%s" % bd})
        urllib.request.urlopen(req, timeout=45, context=_tg_ctx()); return True
    except Exception: return False

def _avail_dvrs():
    with LOCK: st = {d["ip"]: d.get("status") for d in CAMS.get("dvrs", [])}
    return [d for d in DVRS if st.get(d["ip"]) != "down"]   # up/warn/desconhecido (pula so o down)

def _dvr_by_ip(ip):
    return next((d for d in DVRS if d["ip"] == ip), None)

def _dvr_by_num(n):
    for d in DVRS:
        m = re.search(r"(\d+)\s*$", d.get("name", ""))   # casa "DVR 2" -> 2
        if m and int(m.group(1)) == n: return d
    return DVRS[n - 1] if 1 <= n <= len(DVRS) else None

def send_dvr_album(chat, d):
    n = min(d.get("channels") or 1, 16)
    res = [None] * n; sem = threading.Semaphore(4)        # snapshots em paralelo, mas gentil
    def grab(i):
        with sem: res[i] = snapshot(d, i + 1)
    ts = [threading.Thread(target=grab, args=(i,)) for i in range(n)]
    for t in ts: t.start()
    for t in ts: t.join()
    items = [(res[i], "%s - Cam %d" % (d.get("name", "DVR"), i + 1)) for i in range(n) if res[i]]
    if not items:
        tg_send("Sem imagem do %s agora (offline/travado ou sem permissao)." % d.get("name", "DVR"), chat=chat); return
    for i in range(0, len(items), 10):                    # album = no max 10 por vez
        tg_send_album(chat, items[i:i + 10])

def tg_send_menu(chat):
    av = _avail_dvrs()
    if not av: tg_send("Nenhum DVR acessivel agora.", chat=chat); return
    btns = [[{"text": "\U0001F3A5 %s - %s cam" % (d.get("name", "DVR"), d.get("channels", "?")),
              "callback_data": "dvr:" + d["ip"]}] for d in av]
    r = tg_api("sendMessage", {"chat_id": chat, "text": "\U0001F4F9 Toque no DVR pra ver as cameras:",
                               "reply_markup": json.dumps({"inline_keyboard": btns})})
    if not (r and r.get("ok")):           # se o menu falhar, manda o album do 1o disponivel
        _log("menu sendMessage falhou -> fallback album"); send_dvr_album(chat, av[0])

# ---- reiniciar AP pela controladora (rota validada em 05/10/2026) ----------
def _omada_req(caminho, dados=None, metodo=None, timeout=20):
    """Chama a API da Omada reusando a sessao do poller; re-loga se ela caiu."""
    cfg = OMADA_CFG; host = cfg.get("host", "https://127.0.0.1:8043"); cid = cfg.get("cid")
    for _tentativa in (1, 2):
        op, tok = _OM.get("op"), _OM.get("token")
        if not (op and tok):
            tok, _ = _omada_login(); op = _OM.get("op")
            if not (op and tok):
                return None
        try:
            req = urllib.request.Request(
                host + "/%s%s" % (cid, caminho),
                data=json.dumps(dados).encode() if dados is not None else None,
                headers={"Content-Type": "application/json", "Csrf-Token": tok},
                method=metodo)
            return json.loads(op.open(req, timeout=timeout).read().decode("utf-8", "replace"))
        except Exception:
            _OM["token"] = None          # forca re-login na segunda volta
    return None

def omada_aps():
    """[(nome, mac, online)] direto da controladora, nao do cache do poller."""
    r = _omada_req("/api/v2/sites/%s/devices" % OMADA_CFG.get("site"))
    if not r or r.get("errorCode") != 0:
        return []
    return [(x.get("name"), x.get("mac"), x.get("statusCategory") == 1)
            for x in (r.get("result") or [])]

def _nome_do_ap(mac):
    for n, m, _o in omada_aps():
        if m == mac:
            return n
    return mac

def omada_reboot(mac):
    """So funciona com o AP ONLINE: offline a controladora devolve -39006
    'This device does not exist' (testei os 5 formatos de MAC, nao e formato)."""
    r = _omada_req("/api/v2/sites/%s/cmd/devices/%s/reboot" % (OMADA_CFG.get("site"), mac),
                   {}, "POST")
    if not r:
        return False, "controladora nao respondeu"
    return (r.get("errorCode") == 0), str(r.get("msg") or r.get("errorCode"))

def _acompanha_reboot(chat, nome, mac):
    """A controladora MENTE por ~110 s depois do reboot: segue devolvendo o
    uptime velho. Por isso a confirmacao e o uptime zerar, nao o errorCode."""
    for _ in range(30):                      # ate ~5 min
        time.sleep(10)
        r = _omada_req("/api/v2/sites/%s/eaps/%s" % (OMADA_CFG.get("site"), mac))
        up = ((r or {}).get("result") or {}).get("uptimeLong")
        if up is not None and up < 300:
            tg_send("%s <b>%s</b> reiniciou e voltou (uptime %ds)." % (EMO["g"], nome, up), chat=chat)
            return
    tg_send("%s Nao consegui confirmar o reboot de <b>%s</b> em 5 min. "
            "Veja o uptime com <b>wifi</b>." % (EMO["warn"], nome), chat=chat)

def tg_menu_reboot(chat):
    aps = omada_aps()
    if not aps:
        tg_send("Nao consegui falar com a controladora agora.", chat=chat); return
    btns = []
    for nome, mac, online in aps:
        if online:
            btns.append([{"text": "\U0001F501 %s" % nome, "callback_data": "aprb:" + mac}])
        else:
            btns.append([{"text": "%s (fora do ar)" % nome, "callback_data": "aprbno:fora"}])
    tg_api("sendMessage", {"chat_id": chat,
                           "text": "Qual AP reiniciar?\n\nAP fora do ar a controladora NAO reinicia "
                                   "- esse precisa de ciclo de energia na mao.",
                           "reply_markup": json.dumps({"inline_keyboard": btns})})

def tg_confirma_reboot(chat, mac):
    nome = _nome_do_ap(mac)
    btns = [[{"text": "SIM, reiniciar", "callback_data": "aprbok:" + mac},
             {"text": "cancelar", "callback_data": "aprbno:cancel"}]]
    tg_api("sendMessage", {"chat_id": chat,
                           "text": "%s Reiniciar <b>%s</b>?\nEle sai do ar por 1 a 2 min e "
                                   "os clientes dele caem." % (EMO["warn"], nome),
                           "parse_mode": "HTML",
                           "reply_markup": json.dumps({"inline_keyboard": btns})})

def tg_faz_reboot(chat, mac):
    nome = _nome_do_ap(mac)
    ok, msg = omada_reboot(mac)
    if not ok:
        tg_send("%s Nao deu pra reiniciar <b>%s</b>: %s" % (EMO["r"], nome, msg), chat=chat)
        return
    tg_send("%s Reboot enviado para <b>%s</b>. A controladora demora ~2 min pra "
            "admitir; eu confirmo aqui quando o uptime zerar." % (EMO["y"], nome), chat=chat)
    threading.Thread(target=_acompanha_reboot, args=(chat, nome, mac), daemon=True).start()

def help_text():
    return ("Comandos:\n"
            "• <b>status</b> — panorama da rede\n"
            "• <b>fotos</b> — menu de DVRs (botões)\n"
            "• <b>fotos 2</b> — álbum de todas as câmeras do DVR 2\n"
            "• <b>foto 2 3</b> — câmera 3 do DVR 2\n"
            "• <b>dvrs</b> — status dos DVRs\n"
            "• <b>wifi</b> — APs Omada (clientes por AP)\n"
            "• <b>estacoes</b> — saude dos PCs (disco, uptime, quem esta logado)\n"
            "• <b>zumbi</b> — a correcao do laco de DNS ainda esta no lugar?\n"
            "• <b>frota</b> — inventario: modelo, disco, RAM, serial de cada PC\n"
            "\u2022 <b>reiniciar</b> \u2014 reinicia um AP pela controladora (pede confirmacao)")

def handle_cmd(chat, cmd, nums):
    try:
        if cmd in ("status", "rede", "panorama"): tg_send(status_text(), chat=chat)
        elif cmd in ("dvrs", "dvr"): tg_send(cameras_text(), chat=chat)
        elif cmd == "fotos":
            if nums:
                d = _dvr_by_num(nums[0])
                if d: send_dvr_album(chat, d)
                else: tg_send("DVR %d nao encontrado." % nums[0], chat=chat)
            else: tg_send_menu(chat)
        elif cmd in ("foto", "cam", "cameras", "câmeras", "print", "snap"):
            if len(nums) >= 2:
                d = _dvr_by_num(nums[0])
                if not d: tg_send("DVR %d nao encontrado." % nums[0], chat=chat); return
                jp = snapshot(d, nums[1])
                if jp: tg_send_photo(chat, jp, "%s - Cam %d" % (d.get("name", "DVR"), nums[1]))
                else: tg_send("Sem imagem do %s cam %d." % (d.get("name", "DVR"), nums[1]), chat=chat)
            elif len(nums) == 1:
                av = _avail_dvrs()
                if len(av) == 1:
                    jp = snapshot(av[0], nums[0])
                    if jp: tg_send_photo(chat, jp, "%s - Cam %d" % (av[0].get("name", "DVR"), nums[0]))
                    else: tg_send("Sem imagem.", chat=chat)
                else:
                    tg_send("Tem mais de um DVR — use <b>foto &lt;dvr&gt; &lt;cam&gt;</b> (ex: foto 2 3) ou escolha:", chat=chat)
                    tg_send_menu(chat)
            else: tg_send_menu(chat)
        elif cmd in ("wifi", "aps", "ap"): tg_send(omada_text(), chat=chat)
        elif cmd in ("estacoes", "estacao", "pcs", "pc", "maquinas"): tg_send(estacoes_text(), chat=chat)
        elif cmd in ("zumbi", "dns", "zumbis"): tg_send(zumbi_text(), chat=chat)
        elif cmd in ("frota", "inventario", "hardware"): tg_send(frota_text(), chat=chat)
        elif cmd in ("reiniciar", "reboot", "restart"): tg_menu_reboot(chat)
        elif cmd in ("start", "help", "ajuda", "menu"): tg_send(help_text(), chat=chat)
    except Exception: pass

def tg_poller():
    tg = ALERTS.get("telegram", {})
    if not tg.get("token"): return
    allowed = str(tg.get("chat_id") or "")
    try:
        tg_api("setMyCommands", {"commands": json.dumps([
            {"command": "status", "description": "Panorama da rede"},
            {"command": "fotos", "description": "Menu/album de cameras (ex: fotos 2)"},
            {"command": "foto", "description": "Uma camera (ex: foto 2 3 = DVR2 cam3)"},
            {"command": "dvrs", "description": "Status dos DVRs"},
            {"command": "wifi", "description": "APs Omada (clientes por AP)"},
            {"command": "estacoes", "description": "Saude dos PCs (disco, uptime, usuario)"},
            {"command": "zumbi", "description": "A correcao do laco de DNS esta no lugar?"},
            {"command": "frota", "description": "Inventario: modelo, disco, RAM, serial"},
            {"command": "reiniciar", "description": "Reiniciar um AP (pede confirmacao)"}])}, timeout=10)
    except Exception: pass
    offset = 0
    init = tg_api("getUpdates", {"timeout": 0, "allowed_updates": json.dumps(["message", "callback_query"])}, timeout=15)   # pula o backlog no startup
    if init and init.get("ok") and init.get("result"):
        offset = init["result"][-1]["update_id"] + 1
    while True:
        r = tg_api("getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": json.dumps(["message", "callback_query"])}, timeout=40)
        if not r or not r.get("ok"):
            time.sleep(5); continue
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            cb = u.get("callback_query")
            if cb:                                              # toque em botao do menu
                data = cb.get("data", ""); cchat = str(((cb.get("message") or {}).get("chat") or {}).get("id") or "")
                _log("CALLBACK data=%r chat=%s" % (data, cchat))
                try: tg_api("answerCallbackQuery", {"callback_query_id": cb.get("id")}, timeout=8)
                except Exception as e: _log("answerCallback erro: %s" % e)
                if allowed and cchat != allowed: _log("callback ignorado chat!=%s" % allowed); continue
                if data.startswith("dvr:"):
                    d = _dvr_by_ip(data[4:])
                    if d: threading.Thread(target=send_dvr_album, args=(cchat, d), daemon=True).start()
                    else: _log("dvr nao achado: %r" % data)
                    continue
                if data.startswith("aprb:"):        # 1o toque: pede confirmacao
                    tg_confirma_reboot(cchat, data[5:])
                    continue
                if data.startswith("aprbok:"):      # 2o toque: ai sim reinicia
                    threading.Thread(target=tg_faz_reboot, args=(cchat, data[7:]), daemon=True).start()
                    continue
                if data.startswith("aprbno:"):
                    tg_send("Ok, nao reiniciei nada.", chat=cchat)
                    continue
                continue
            msg = u.get("message") or u.get("edited_message") or {}
            chat = str((msg.get("chat") or {}).get("id") or "")
            raw = (msg.get("text") or "").strip().lstrip("/")
            cmd = (raw.lower().split() or [""])[0]
            nums = [int(x) for x in re.findall(r"\d+", raw)]
            _log("MSG text=%r cmd=%s nums=%s chat=%s" % (raw, cmd, nums, chat))
            if allowed and chat != allowed: continue           # responde so ao chat configurado
            threading.Thread(target=handle_cmd, args=(chat, cmd, nums), daemon=True).start()

def _muted(key):
    for mk in (ALERTS.get("mute") or []):
        if mk and mk in key: return True
    return False


# ============================ ESTACOES (WinRM) ==============================
# Coleta a saude das estacoes Windows. WinRM e nao SNMP: o SNMP do Windows e
# v1/v2c (community em texto claro) e virou Feature on Demand no Win10/11.
# O WinRM ja esta ligado nas estacoes, e autenticado e entrega muito mais.
#
# Quem fala com as maquinas e o estacoes.ps1, num processo POR CICLO - o
# servidor tem pouca RAM livre e nao comporta mais um residente.
EST_INTERVAL = int(CFG.get("estacoes_interval", 900))   # a coleta cheia leva ~65s
ESTACOES = {"quando": None, "total": 0, "no_ar": 0, "lista": [], "erro": None}
_EST_NOMES = {}          # ip -> ultimo nome ativo visto, para notar o reboot
_EST_ANT   = {}          # maquina -> coleta anterior inteira, para ver o que MUDOU

def _est_zumbi(e):
    """(esta_ruim, motivos) - o estado da correcao do laco de DNS.

    Modo 99 = sem P2P e sem falar com a nuvem do Delivery Optimization (o
    unico que PARA as consultas). Modo 1 = P2P so na LAN, aceitavel nas
    maquinas que nunca adoeceram. Outro valor, ou WPAD de volta, e regressao."""
    do, motivos = e.get("do_mode"), []
    if do not in (1, 99):
        motivos.append("Delivery Optimization %s" % (
            "no padrao (P2P ligado)" if do is None else "no modo %s" % do))
    if e.get("wpad_ligado"):
        motivos.append("WPAD religou")
    return (bool(motivos), motivos)

# limites - cada um existe porque custaria dinheiro descobrir tarde
LIM_DISCO_PCT  = 10     # abaixo disso o Windows comeca a engasgar
LIM_DISCO_GB   = 5
LIM_TEMP_C     = 55     # SSD acima disso reduz a propria velocidade
LIM_PATCH_DIAS = 45
LIM_UPTIME_H   = 720    # 30 dias sem reiniciar

def _est_problemas(e):
    """Lista de (chave, ruim, texto_ruim, texto_ok) de UMA estacao."""
    nome = e.get("maquina") or e.get("ip") or "?"
    P = []

    ruim, motivos = _est_zumbi(e)
    P.append(("zumbi", ruim,
              "o zumbi voltou - %s" % "; ".join(motivos),
              "correcao de DNS de volta no lugar"))

    pct, gb = e.get("disco_livre_pct"), e.get("disco_livre_gb")
    if pct is not None:
        P.append(("disco", pct < LIM_DISCO_PCT or (gb or 99) < LIM_DISCO_GB,
                  "disco quase cheio: %s%% livres (%s GB)" % (pct, gb),
                  "espaco em disco normalizou"))

    saude = e.get("disco_saude")
    if saude and saude not in ("sem dados",):
        P.append(("smart", saude.replace("Healthy", "").strip(",") != "",
                  "o disco reporta saude '%s' - faca backup e planeje a troca" % saude,
                  "disco voltou a reportar saude boa"))

    t = e.get("disco_temp")
    if t:
        P.append(("temp", t >= LIM_TEMP_C,
                  "disco a %s C - acima de %s C ele reduz a propria velocidade" % (t, LIM_TEMP_C),
                  "temperatura do disco normalizou"))

    av = e.get("av_qtd")
    if av is not None:
        # av_qtd ja conta SO os que tem executavel em disco. Registro de versao
        # antiga (o instalador do antivirus nao limpa o anterior) nao e problema
        # e nao entra na conta - so aparece no texto, como informacao.
        P.append(("av", av != 1,
                  ("NENHUM antivirus ativo nesta maquina" if av == 0 else
                   "%d antivirus DE VERDADE instalados (%s) - eles se atrapalham" % (av, e.get("av_nome"))),
                  "antivirus voltou ao normal"))

    if e.get("fw_ok") is not None:
        P.append(("fw", not e.get("fw_ok"),
                  "firewall desligado no perfil: %s" % e.get("fw_off"),
                  "firewall ligado de novo"))

    pd = e.get("patch_dias")
    if pd is not None:
        P.append(("patch", pd > LIM_PATCH_DIAS,
                  "sem atualizacao ha %d dias (ultima em %s)" % (pd, e.get("patch_ultimo")),
                  "voltou a receber atualizacao"))

    up = e.get("uptime_h")
    if up is not None:
        P.append(("uptime", up > LIM_UPTIME_H,
                  "ligada ha %d dias sem reiniciar" % (up / 24),
                  "reiniciou"))
    return nome, P

def estacoes_poller():
    ps = os.path.join(BASE, "estacoes.ps1")
    while True:
        try:
            if os.path.exists(ps):
                p = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                txt = p.stdout.decode("utf-8", "replace").strip()
                d = json.loads(txt) if txt.startswith("{") else None
                if d:
                    with LOCK:
                        ESTACOES["quando"] = d.get("quando")
                        ESTACOES["total"]  = d.get("total", 0)
                        ESTACOES["no_ar"]  = d.get("no_ar", 0)
                        ESTACOES["lista"]  = d.get("estacoes", [])
                        ESTACOES["erro"]   = None
                    estacoes_alertas(d)
                else:
                    with LOCK: ESTACOES["erro"] = "saida nao era JSON"
        except Exception as e:
            with LOCK: ESTACOES["erro"] = "%s: %s" % (type(e).__name__, e)
        time.sleep(EST_INTERVAL)

def estacoes_alertas(d):
    if not ALERTS.get("enabled"): return
    for e in d.get("estacoes", []):
        if not e.get("ok"):
            continue      # maquina fora do ar ja e coberta pelo ping do poller
        nome, problemas = _est_problemas(e)
        for chave, ruim, msg_ruim, msg_ok in problemas:
            trigger("%s:%s" % (chave, nome), ruim,
                    "%s <b>%s</b>: %s" % (EMO["warn"], nome, msg_ruim),
                    "%s <b>%s</b>: %s." % (EMO["g"], nome, msg_ok), level="warn")

        # --- coisas que so fazem sentido comparando com a coleta anterior ---
        ant = _EST_ANT.get(nome) or {}

        # queda suja NOVA: o contador de 60 dias subiu desde a ultima coleta
        q, qa = e.get("quedas_60d"), ant.get("quedas_60d")
        if q is not None and qa is not None and q > qa:
            emit("warn", "queda:%s:%s" % (nome, e.get("queda_ultima")),
                 "%s <b>%s</b> desligou de forma suja (%s). Sao %d em 60 dias." % (
                     EMO["plug"], nome, e.get("queda_ultima"), q))

        # admin local novo ou removido: conta de admin aparecendo e sinal
        a, aa = e.get("admins"), ant.get("admins")
        if a and aa and a != aa:
            novos = sorted(set(a.split(",")) - set(aa.split(",")))
            sumiu = sorted(set(aa.split(",")) - set(a.split(",")))
            partes = []
            if novos: partes.append("entrou: " + ", ".join(novos))
            if sumiu: partes.append("saiu: " + ", ".join(sumiu))
            emit("alert", "admins:%s:%s" % (nome, a),
                 "%s <b>%s</b>: mudou quem e administrador local (%s)" % (
                     EMO["warn"], nome, " | ".join(partes)))

        # reiniciou e assumiu o nome novo (util durante a ronda)
        if ant.get("nome_ativo") and e.get("nome_ativo") and ant["nome_ativo"] != e["nome_ativo"]:
            emit("ok", "renome:%s" % e["nome_ativo"],
                 "%s <b>%s</b> reiniciou e assumiu o nome novo (era %s)." % (
                     EMO["pc"], e["nome_ativo"], ant["nome_ativo"]))

        _EST_ANT[nome] = e

def _est_icone(e):
    if not e.get("ok"): return EMO["r"]
    _, problemas = _est_problemas(e)
    return EMO["y"] if any(p[1] for p in problemas) else EMO["g"]

def estacoes_text():
    with LOCK: est = dict(ESTACOES); lista = list(ESTACOES["lista"])
    if not lista:
        return ("Nenhuma estacao cadastrada ainda.\n\n"
                "Rode o <b>ABRE-GERENCIA.ps1</b> nas maquinas e depois o "
                "<b>importar-credenciais.ps1</b> aqui no servidor.")
    L = ["%s <b>Estacoes</b>  -  %d de %d no ar" % (EMO["pc"], est["no_ar"], est["total"]), ""]
    for e in sorted(lista, key=lambda x: str(x.get("maquina") or "")):
        nome = e.get("maquina") or e.get("ip")
        if not e.get("ok"):
            L.append("%s <b>%s</b>  %s" % (EMO["r"], nome, (e.get("erro") or "")[:50]))
            continue
        rot = nome
        if e.get("renomeada"):
            rot = "%s \u2192 %s" % (e.get("nome_ativo"), e.get("nome_proximo"))
        L.append("%s <b>%s</b>  <code>%s</code>" % (_est_icone(e), rot, e.get("ip")))
        L.append("    %s GB livres (%s%%)  |  no ar ha %sh  |  %s" % (
            e.get("disco_livre_gb"), e.get("disco_livre_pct"), e.get("uptime_h"),
            (e.get("usuario") or "ninguem logado").split(chr(92))[-1]))
        _, problemas = _est_problemas(e)
        for chave, ruim, msg_ruim, _ok in problemas:
            if ruim: L.append("    %s %s" % (EMO["warn"], msg_ruim))
        if e.get("renomeada"):
            L.append("    %s falta reiniciar para o nome novo valer" % EMO["warn"])
    if est.get("erro"): L.append("\n%s coleta: %s" % (EMO["warn"], est["erro"]))
    return "\n".join(L)

def frota_text():
    """Inventario seco: o que tem em cada maquina."""
    with LOCK: lista = list(ESTACOES["lista"])
    if not lista: return "Nenhuma estacao cadastrada ainda."
    L = ["%s <b>Inventario da frota</b>" % EMO["pc"], ""]
    for e in sorted(lista, key=lambda x: str(x.get("maquina") or "")):
        if not e.get("ok"): continue
        L.append("<b>%s</b>  <code>%s</code>" % (e.get("maquina"), e.get("ip")))
        L.append("    %s" % (e.get("modelo") or "?"))
        L.append("    %s (build %s)  PS %s" % (e.get("windows"), e.get("build"), e.get("psver")))
        L.append("    disco: %s  %s GB  %s%s" % (
            e.get("disco_modelo") or "?", e.get("disco_total_gb"),
            e.get("disco_saude") or "?",
            ("  %s C" % e.get("disco_temp")) if e.get("disco_temp") else ""))
        if e.get("disco_horas"): L.append("    ligado ha %s horas de uso" % e.get("disco_horas"))
        L.append("    RAM %s GB  |  serial %s  |  windows de %s" % (
            e.get("ram_total_gb"), e.get("serial"), e.get("instalado")))
        av = "%s" % (e.get("av_nome") or "?")
        if e.get("av_fantasmas"):
            av += "  (+%d registro(s) de versao antiga, sem executavel)" % e["av_fantasmas"]
        L.append("    antivirus: %s" % av)
        L.append("    admins: %s" % (e.get("admins") or "?"))
        L.append("")
    return "\n".join(L)

def zumbi_text():
    """Estado da correcao do laco de DNS, maquina por maquina."""
    with LOCK: lista = list(ESTACOES["lista"])
    if not lista: return "Nenhuma estacao cadastrada ainda."
    nomes = {99: "99 - sem P2P e sem nuvem (ideal)", 1: "1 - P2P so na LAN (ok)",
             0: "0 - sem P2P mas AINDA fala com a nuvem", 2: "2 - grupo", 3: "3 - internet (o problema)"}
    bons = [e for e in lista if e.get("ok") and not _est_zumbi(e)[0]]
    maus = [e for e in lista if e.get("ok") and _est_zumbi(e)[0]]
    L = ["%s <b>Cacada ao zumbi do DNS</b>" % EMO["net"],
         "%d limpa(s), %d para olhar" % (len(bons), len(maus)), ""]
    for e in maus:
        do = e.get("do_mode")
        L.append("%s <b>%s</b>" % (EMO["y"], e.get("maquina")))
        L.append("    DO: %s" % nomes.get(do, "padrao (P2P ligado)" if do is None else str(do)))
        if e.get("wpad_ligado"):
            L.append("    WPAD ligado em %d perfil(is)" % len(e.get("wpad_perfis") or []))
    for e in bons:
        L.append("%s <b>%s</b>  -  DO %s, WPAD desligado" % (EMO["g"], e.get("maquina"), e.get("do_mode")))
    if not maus: L.append("\n%s Nenhuma regressao. O zumbi continua morto." % EMO["g"])
    return "\n".join(L)

def estacoes_html():
    """Pagina /estacoes: tabela no computador, cartao no celular."""
    with LOCK: est = dict(ESTACOES); lista = list(ESTACOES["lista"])
    def esc(s):
        return (str(s) if s is not None else "-").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    linhas = []
    for e in sorted(lista, key=lambda x: str(x.get("maquina") or "")):
        nome = esc(e.get("maquina"))
        if not e.get("ok"):
            linhas.append('<tr class="off"><td class="nome"><b>%s</b></td>'
                          '<td class="av" data-l="">%s</td></tr>'
                          % (nome, esc((e.get("erro") or "")[:90])))
            continue
        _, problemas = _est_problemas(e)
        avisos = [m for k, r, m, o in problemas if r]
        cls = "warn" if avisos else "ok"
        rot = nome
        if e.get("renomeada"):
            rot = "%s &rarr; %s" % (esc(e.get("nome_ativo")), esc(e.get("nome_proximo")))
        linhas.append(
            '<tr class="%s">'
            '<td class="nome"><b>%s</b><div class="sub">%s</div></td>'
            '<td data-l="ip"><code>%s</code></td>'
            '<td data-l="disco livre">%s GB <span class="sub">(%s%%)</span></td>'
            '<td data-l="temp">%s</td>'
            '<td data-l="quedas 60d">%s</td>'
            '<td data-l="no ar">%sh</td>'
            '<td data-l="patch">%sd</td>'
            '<td data-l="antivirus">%s</td>'
            '<td data-l="usuario">%s</td>'
            '<td class="%s" data-l="">%s</td></tr>' % (
                cls, rot, esc(e.get("modelo"))[:40], esc(e.get("ip")),
                esc(e.get("disco_livre_gb")), esc(e.get("disco_livre_pct")),
                ("%s C" % e.get("disco_temp")) if e.get("disco_temp") else "-",
                esc(e.get("quedas_60d")), esc(e.get("uptime_h")), esc(e.get("patch_dias")),
                esc(e.get("av_qtd")), esc((e.get("usuario") or "-").split(chr(92))[-1]),
                "av" if avisos else "av vazio",
                "<br>".join("\u26a0 " + esc(a) for a in avisos) or "-"))
    return """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Estacoes - NetMon</title><style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{background:#0f1216;color:#dfe6ee;font:14px/1.5 system-ui,Segoe UI,sans-serif;margin:0;padding:16px}
h1{font-size:19px;margin:0 0 4px}
.meta{color:#8b97a6;font-size:12px;margin-bottom:14px}
table{border-collapse:collapse;width:100%%;font-size:13px}
th{text-align:left;color:#8b97a6;font-weight:600;padding:7px 9px;border-bottom:1px solid #263041;white-space:nowrap}
td{padding:7px 9px;border-bottom:1px solid #1a2028;vertical-align:top}
tr.warn td{background:#2a2410}
tr.off td{background:#2a1416;color:#e08a94}
.sub{color:#788392;font-size:11px}
.av{color:#e8c46a;font-size:12px}
code{color:#9fd0ff}
a{color:#6ab0f3}
/* no celular a tabela nao cabe: cada maquina vira um cartao com rotulo */
@media (max-width:760px){
  body{padding:12px}
  thead{display:none}
  table,tbody,tr,td{display:block;width:100%%}
  tr{border:1px solid #263041;border-radius:10px;margin:0 0 10px;padding:8px 0;background:#141922}
  tr.warn{background:#241f11;border-color:#4a3d16}
  tr.off{background:#241416;border-color:#54262c}
  tr.warn td,tr.off td{background:transparent}
  td{border:0;padding:3px 12px;display:flex;gap:10px;align-items:baseline}
  td::before{content:attr(data-l);color:#8b97a6;font-size:12px;flex:0 0 96px}
  td.nome{display:block;padding-bottom:6px;font-size:15px}
  td.nome::before{content:none}
  td.av{display:block;margin-top:4px;padding-top:6px;border-top:1px solid #2b3444;line-height:1.45}
  td.av::before{content:none}
  td.av.vazio{display:none}
}
</style></head><body>
<h1>Estacoes</h1>
<div class="meta">%d de %d no ar &middot; %s &middot; <a href="/">voltar ao painel</a></div>
<table><thead><tr><th>maquina</th><th>ip</th><th>disco livre</th><th>temp</th><th>quedas 60d</th>
<th>no ar</th><th>patch</th><th>AV</th><th>usuario</th><th>o que olhar</th></tr></thead>
<tbody>
%s</tbody></table></body></html>""" % (est.get("no_ar", 0), est.get("total", 0),
                                       esc(est.get("quando")), "\n".join(linhas))
# ========================== fim ESTACOES (WinRM) ============================

def emit(level, key, msg):
    ts = int(time.time())
    try:
        con = sqlite3.connect(DB_PATH); con.execute("INSERT INTO events VALUES(?,?,?,?)",
            (ts, level, key, msg)); con.commit(); con.close()
    except Exception: pass
    if _muted(key): return   # congelado: fica no historico (DB) mas nao vai pro painel nem Telegram
    ev = {"ts": ts, "level": level, "key": key, "msg": msg}
    with ALOCK:
        RECENT.insert(0, ev); del RECENT[60:]
    icon = {"alert": "\U0001F534", "warn": "\U0001F7E1", "ok": "\U0001F7E2"}.get(level, "ℹ️")
    threading.Thread(target=tg_send, args=("%s <b>NetMon %s</b>\n%s" % (icon, SITE, msg),), daemon=True).start()

def _ultimo_marco_repeticao(agora, horas):
    """Timestamp do horario de repeticao mais recente que ja passou.

    Olha hoje E ontem para cobrir a virada do dia (as 02h, o marco mais recente
    e o das 18h de ontem).
    """
    lt = time.localtime(agora)
    meia_noite = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    marcos = [meia_noite + d * 86400 + int(h) * 3600 for d in (0, -1) for h in horas]
    passados = [m for m in marcos if m <= agora]
    return max(passados) if passados else 0


def _deve_repetir(agora, ultimo):
    """Repete so quando cruzou um horario configurado desde o ultimo envio.

    Sem `repeat_hours` no config, cai no comportamento antigo (cooldown_min).
    """
    horas = ALERTS.get("repeat_hours")
    if horas:
        return _ultimo_marco_repeticao(agora, horas) > ultimo
    return agora - ultimo >= ALERTS.get("cooldown_min", 15) * 60


def trigger(key, bad, msg_bad, msg_ok, level="alert"):
    now = time.time(); need = max(1, ALERTS.get("confirm", 3)); action = None
    with ALOCK:
        s = ALERT_STATE.get(key) or {"bad": False, "last": 0, "n": 0}
        s["n"] = (s.get("n", 0) + 1) if bad else 0
        if bad and not s["bad"] and s["n"] >= need:
            # alerta NOVO: sai na hora, sempre. Nao e isso que faz barulho.
            s["bad"] = True; s["last"] = now; action = (level, msg_bad)
        elif bad and s["bad"] and _deve_repetir(now, s["last"]):
            s["last"] = now; action = (level, msg_bad + " (continua)")
        elif (not bad) and s["bad"]:
            s = {"bad": False, "last": 0, "n": 0}; action = ("ok", msg_ok)
        ALERT_STATE[key] = s
    if action: emit(action[0], key, action[1])

def alert_eval(st):
    if not ALERTS.get("enabled"): return
    R = ALERTS.get("rules", {})
    try:
        trigger("cisco", not st.get("ok"),
                "Roteador/NetMon sem dados: %s" % st.get("msg", ""), "Roteador respondendo de novo.")
        if st.get("ok"):
            cpu = st.get("cpu5m") if st.get("cpu5m") is not None else st.get("cpu")
            if R.get("cpu_pct") and cpu is not None:
                trigger("cpu", cpu >= R["cpu_pct"], "CPU do Cisco alta: %s%%" % cpu, "CPU normalizou.", "warn")
            if R.get("mem_pct") and st.get("mem_pct") is not None:
                trigger("mem", st["mem_pct"] >= R["mem_pct"], "Memoria do Cisco alta: %s%%" % st["mem_pct"], "Memoria normalizou.", "warn")
            if R.get("lan_sat_pct") and st.get("lan_sat") is not None:
                trigger("lan", st["lan_sat"] >= R["lan_sat_pct"], "Saturacao da LAN: %s%%" % st["lan_sat"], "Saturacao da LAN normalizou.", "warn")
            if R.get("crc_ppm") and st.get("crc_ppm") is not None:
                trigger("crc", st["crc_ppm"] >= R["crc_ppm"],
                        "Enlace WAN degradado: %s erros por milhao de pacotes (normal 9 a 16)." % st["crc_ppm"],
                        "Enlace WAN normalizou.", "warn")
            if "inet_up" in st:
                trigger("inet", not st["inet_up"], "Internet caiu (sem resposta de %s)." % INET_PROBE, "Internet voltou.")
            warn_dbm = R.get("antenna_warn_dbm", -78)
            for r in ((st.get("ant") or {}).get("remotes") or []):
                k = "ant:" + r["name"]
                if not r.get("up"):
                    trigger(k, True, "Enlace %s CAIU." % r["name"], "Enlace %s voltou." % r["name"])
                elif r.get("dbm") is not None:
                    trigger(k, r["dbm"] <= warn_dbm, "Enlace %s fraco: %s dBm." % (r["name"], r["dbm"]), "Enlace %s normalizou." % r["name"], "warn")
                else:
                    trigger(k, False, "", "Enlace %s normalizou." % r["name"])
            hmap = {h["ip"]: h for h in st.get("hosts", [])}
            wh = R.get("watch_hosts") or []
            for ip in wh:
                h = hmap.get(ip)
                if h is not None:
                    trigger("host:" + ip, not h["up"], "Host %s (%s) caiu." % (h["name"], ip), "Host %s (%s) voltou." % (h["name"], ip))
            whset = set(wh)
            for n in (MAPNODES or []):
                ip = n.get("ip")
                if not ip or ip in whset: continue
                h = hmap.get(ip); up = bool(h and h.get("up"))
                nm = n.get("name") or ip
                trigger("mapnode:" + ip, not up, "%s (%s) caiu." % (nm, ip), "%s (%s) voltou." % (nm, ip), "warn")
    except Exception: pass

def alert_eval_cameras(cams):
    if not ALERTS.get("enabled"): return
    try:
        for d in cams.get("dvrs", []):
            trigger("dvr:" + d["ip"], d["status"] == "down",
                    "%s (%s) sem resposta RTSP." % (d["name"], d["ip"]),
                    "%s (%s) voltou." % (d["name"], d["ip"]))
            h = d.get("hdd")
            if h:
                trigger("hdd:" + d["ip"], bool(h.get("error")),
                        "%s: HD com ERRO/falha! Verificar o disco." % d["name"],
                        "%s: HD voltou ao normal." % d["name"])
    except Exception: pass

def _skipname(d, off):
    while off < len(d):
        l = d[off]
        if l == 0: return off + 1
        if l & 0xC0 == 0xC0: return off + 2
        off += 1 + l
    return off

def _rdname(d, off):
    out = []; safety = 0
    while safety < 30 and off < len(d):
        safety += 1; l = d[off]
        if l == 0: break
        if l & 0xC0 == 0xC0: off = ((l & 0x3f) << 8) | d[off + 1]; continue
        out.append(d[off + 1:off + 1 + l].decode("latin1", "replace")); off += 1 + l
    return ".".join(out)

def mdns_name(ip, timeout=1.0):
    rev = ".".join(reversed(ip.split("."))) + ".in-addr.arpa"
    q = b""
    for p in rev.split("."): q += bytes([len(p)]) + p.encode()
    q += b"\x00"
    pkt = b"\x4d\x4e\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00" + q + b"\x00\x0c\x80\x01"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(timeout)
    try:
        s.sendto(pkt, (ip, 5353)); d = s.recvfrom(2048)[0]
    except Exception:
        return None
    finally:
        s.close()
    try:
        off = _skipname(d, 12) + 4
        off = _skipname(d, off) + 8 + 2
        nm = _rdname(d, off).split(".local")[0].strip()
        return nm or None
    except Exception:
        return None

def nbns_name(ip, timeout=1.0):
    enc = bytearray([0x20])
    for b in (b"\x2a" + b"\x00" * 15):
        enc.append(0x41 + (b >> 4)); enc.append(0x41 + (b & 0x0f))
    enc.append(0x00)
    pkt = b"\xab\xcd\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00" + bytes(enc) + b"\x00\x21\x00\x01"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(timeout)
    try:
        s.sendto(pkt, (ip, 137)); d = s.recvfrom(2048)[0]
    except Exception:
        return None
    finally:
        s.close()
    try:
        off = _skipname(d, 12) + 4
        off = _skipname(d, off) + 8 + 2
        n = d[off]; off += 1
        for _ in range(n):
            nm = d[off:off + 15].decode("latin1", "replace").rstrip()
            suf = d[off + 15]; fl = (d[off + 16] << 8) | d[off + 17]
            if suf == 0x00 and not (fl & 0x8000) and nm and nm != "__MSBROWSE__":
                return nm.strip()
            off += 18
    except Exception:
        pass
    return None

def name_poller():
    global AUTONAMES
    time.sleep(20)   # espera o 1o poll popular os hosts
    while True:
        try:
            with LOCK: ips = [h["ip"] for h in STATE.get("hosts", [])]
            todo = [ip for ip in ips if ip not in NAMES]
            res = {}; sem = threading.Semaphore(16)
            def work(ip):
                with sem:
                    nm = mdns_name(ip) or nbns_name(ip)
                    if nm: res[ip] = nm
            ts = [threading.Thread(target=work, args=(ip,)) for ip in todo]
            for t in ts: t.start()
            for t in ts: t.join()
            if res:
                nd = dict(AUTONAMES); nd.update(res); AUTONAMES = nd
        except Exception:
            pass
        time.sleep(600)

def poller():
    global STATE, HIST, PREV_DROPS, PREV_TS, LAST_DBW
    while True:
        st = {"ts": time.time()}
        try:
            out = run_cisco(); st.update(parse_metrics(out))
            crc_quality(st)   # contador bruto -> erros por milhao de pacotes
            d, now = st.get("drops_total"), st["ts"]
            if d is not None and PREV_DROPS is not None and now > PREV_TS:
                dps = (d - PREV_DROPS) / (now - PREV_TS); st["drops_ps"] = round(dps, 1) if dps >= 0 else 0.0
            if d is not None: PREV_DROPS, PREV_TS = d, now
            arp = parse_arp(out)
            arp_map = {a["ip"]: (int(a["age"]) if a["age"].isdigit() else 9999) for a in arp}
            hosts = poll_hosts(arp)
            binds = parse_bindings(out)
            for h in hosts:
                bi = binds.get(h["ip"])
                if bi: h["dhcp_type"] = bi["type"]; h["lease"] = bi["lease"]; h["cid"] = bi["cid"]
            st["hosts"] = hosts; st["hosts_up"] = sum(1 for h in hosts if h["up"]); st["hosts_total"] = len(hosts)
            st["ant"] = poll_antenna(arp_map); st["ok"] = True; st["msg"] = "online"
            if CHECK_INET: st["inet_up"] = ping(INET_PROBE)[0]
        except Exception as e:
            st["ok"] = False; st["msg"] = ("erro: " + str(e))[:140]
            with LOCK:
                st["hosts"] = STATE.get("hosts", []); st["ant"] = STATE.get("ant")
        alert_eval(st)
        with LOCK:
            if st.get("ok") and "wan_down" in st:
                HIST.append({"down": st["wan_down"], "up": st["wan_up"]}); del HIST[:-90]
            st["hist"] = list(HIST); st["cams"] = CAMS
            with ALOCK: st["alerts"] = list(RECENT[:20])
            STATE = st
        if st.get("ok") and (st["ts"] - LAST_DBW >= HIST_WRITE):
            db_write(st); LAST_DBW = st["ts"]
        time.sleep(INTERVAL)

def load_page(name="index.html"):
    try:
        with open(os.path.join(BASE, name), encoding="utf-8") as f: return f.read().replace("{{SITE}}", SITE)
    except Exception as e:
        return "<!doctype html><meta charset=utf-8><body style='font:14px sans-serif;color:#fff;background:#111'>%s nao encontrado: %s</body>" % (name, e)

STATIC = {
    "/manifest.json": ("manifest.json", "application/manifest+json"),
    "/noc-ca.crt": ("noc-ca.crt", "application/x-x509-ca-cert"),
    "/sw.js": ("sw.js", "application/javascript"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path.startswith("/api/metrics"):
            with LOCK: body = json.dumps(STATE).encode("utf-8")
            ct = "application/json"
        elif self.path.startswith("/api/history"):
            q = urllib.parse.urlparse(self.path).query
            rng = (urllib.parse.parse_qs(q).get("range", ["24h"])[0])
            body = json.dumps(history(rng)).encode("utf-8"); ct = "application/json"
        elif self.path.startswith("/api/logs"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            body = json.dumps(logs_query(qs)).encode("utf-8"); ct = "application/json"
        elif self.path.startswith("/api/dnslog"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            body = json.dumps(dnslog_query(qs)).encode("utf-8"); ct = "application/json"
        elif self.path.startswith("/api/estacoes"):
            with LOCK: body = json.dumps(ESTACOES).encode("utf-8")
            ct = "application/json"
        elif self.path.startswith("/api/omada"):
            with LOCK: body = json.dumps(OMADA).encode("utf-8")
            ct = "application/json"
        elif self.path.startswith("/api/dns"):
            try:
                with open(os.path.join(BASE, "dns_stats.json"), encoding="utf-8") as fh: body = fh.read().encode("utf-8")
            except Exception:
                body = b"{}"
            ct = "application/json"
        elif self.path.startswith("/api/status"):
            body = status_text().encode("utf-8"); ct = "text/plain; charset=utf-8"
        elif self.path.startswith("/api/camnames"):
            with LOCK: body = json.dumps(CAMNAMES).encode("utf-8")
            ct = "application/json"
        elif self.path.startswith("/api/camrot"):
            with LOCK: body = json.dumps(CAMROT).encode("utf-8")
            ct = "application/json"
        elif self.path.startswith("/api/mappos"):
            with LOCK: body = json.dumps(MAPPOS).encode("utf-8")
            ct = "application/json"
        elif self.path.startswith("/api/maplinks"):
            with LOCK: body = json.dumps(MAPEDGES).encode("utf-8")
            ct = "application/json"
        elif self.path.startswith("/api/mapnodes"):
            with LOCK: body = json.dumps(MAPNODES).encode("utf-8")
            ct = "application/json"
        elif self.path.split("?")[0] in STATIC:
            fn, ct = STATIC[self.path.split("?")[0]]
            try:
                with open(os.path.join(BASE, fn), "rb") as fh: body = fh.read()
            except Exception:
                self.send_response(404); self.end_headers(); return
        elif self.path.split("?")[0] == "/estacoes":
            body = estacoes_html().encode("utf-8"); ct = "text/html; charset=utf-8"
        elif self.path.startswith("/cameras"):
            body = load_page("cameras.html").encode("utf-8"); ct = "text/html; charset=utf-8"
        elif self.path.startswith("/logs"):
            body = load_page("logs.html").encode("utf-8"); ct = "text/html; charset=utf-8"
        elif self.path.startswith("/dnslog"):
            body = load_page("dnslog.html").encode("utf-8"); ct = "text/html; charset=utf-8"
        elif self.path.split("?")[0] == "/doc":
            au = self.headers.get("Authorization", "")
            okauth = False
            if au.startswith("Basic ") and DOCAUTH.get("pass"):
                try:
                    dec = base64.b64decode(au[6:]).decode("utf-8", "replace"); usr, _, pw = dec.partition(":")
                    okauth = (usr == DOCAUTH.get("user", "noc") and pw == DOCAUTH.get("pass"))
                except Exception: okauth = False
            if not okauth:
                self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="NOC - Documentacao"')
                self.send_header("Content-Type", "text/plain; charset=utf-8"); self.end_headers(); self.wfile.write("Acesso restrito.".encode("utf-8")); return
            body = load_page("doc.html").encode("utf-8"); ct = "text/html; charset=utf-8"
        else:
            body = load_page("index.html").encode("utf-8"); ct = "text/html; charset=utf-8"
        self.send_response(200); self.send_header("Content-Type", ct)
        self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        if self.path.startswith("/api/camnames"):
            try:
                n = int(self.headers.get("Content-Length", 0)); data = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
                cid = str(data.get("id", "")); nm = (data.get("name") or "").strip()
                if cid:
                    with LOCK:
                        if nm: CAMNAMES[cid] = nm[:40]
                        else: CAMNAMES.pop(cid, None)
                        try:
                            with open(os.path.join(BASE, "camnames.json"), "w", encoding="utf-8") as f: json.dump(CAMNAMES, f, ensure_ascii=False)
                        except Exception: pass
                body = b'{"ok":true}'
            except Exception as e:
                body = ('{"ok":false,"err":"%s"}' % type(e).__name__).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
        elif self.path.startswith("/api/camrot"):
            try:
                n = int(self.headers.get("Content-Length", 0)); data = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
                cid = str(data.get("id", "")); rot = int(data.get("rot", 0)) % 360
                if cid:
                    with LOCK:
                        if rot in (90, 180, 270): CAMROT[cid] = rot
                        else: CAMROT.pop(cid, None)
                        try:
                            with open(os.path.join(BASE, "camrot.json"), "w", encoding="utf-8") as f: json.dump(CAMROT, f)
                        except Exception: pass
                body = b'{"ok":true}'
            except Exception as e:
                body = ('{"ok":false,"err":"%s"}' % type(e).__name__).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
        elif self.path.startswith("/api/mappos"):
            try:
                n = int(self.headers.get("Content-Length", 0)); data = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
                def _ll(v):
                    return {"lat": max(-90.0, min(90.0, float(v["lat"]))), "lng": max(-180.0, min(180.0, float(v["lng"])))}
                with LOCK:
                    if isinstance(data.get("all"), dict):
                        for k, v in data["all"].items():
                            try: MAPPOS[str(k)] = _ll(v)
                            except Exception: pass
                    elif data.get("id"):
                        try: MAPPOS[str(data["id"])] = _ll(data)
                        except Exception: pass
                    if isinstance(data.get("view"), dict):
                        try:
                            vv = data["view"]; MAPPOS["_view"] = {"lat": float(vv["lat"]), "lng": float(vv["lng"]), "zoom": float(vv.get("zoom", 13))}
                        except Exception: pass
                    try:
                        with open(os.path.join(BASE, "mappos.json"), "w", encoding="utf-8") as f: json.dump(MAPPOS, f)
                    except Exception: pass
                body = b'{"ok":true}'
            except Exception as e:
                body = ('{"ok":false,"err":"%s"}' % type(e).__name__).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
        elif self.path.startswith("/api/maplinks"):
            try:
                global MAPEDGES
                n = int(self.headers.get("Content-Length", 0)); data = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
                with LOCK:
                    if data.get("reset"):
                        MAPEDGES = None
                    elif isinstance(data.get("edges"), list):
                        ed = []
                        for pr in data["edges"]:
                            if isinstance(pr, (list, tuple)) and len(pr) >= 2: ed.append([str(pr[0]), str(pr[1])])
                        MAPEDGES = {"edges": ed}
                    try:
                        with open(os.path.join(BASE, "maplinks.json"), "w", encoding="utf-8") as f: json.dump(MAPEDGES, f)
                    except Exception: pass
                body = b'{"ok":true}'
            except Exception as e:
                body = ('{"ok":false,"err":"%s"}' % type(e).__name__).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
        elif self.path.startswith("/api/mapnodes"):
            try:
                global MAPNODES
                n = int(self.headers.get("Content-Length", 0)); data = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
                with LOCK:
                    if isinstance(data.get("nodes"), list):
                        out = []
                        for nd in data["nodes"]:
                            if isinstance(nd, dict) and nd.get("id"):
                                out.append({"id": str(nd["id"]), "name": str(nd.get("name", "")), "ip": str(nd.get("ip", "")), "icon": str(nd.get("icon", "ic-net"))})
                        MAPNODES = out
                        try:
                            with open(os.path.join(BASE, "mapnodes.json"), "w", encoding="utf-8") as f: json.dump(MAPNODES, f)
                        except Exception: pass
                body = b'{"ok":true}'
            except Exception as e:
                body = ('{"ok":false,"err":"%s"}' % type(e).__name__).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
        elif self.path.startswith("/api/dvr_reboot"):
            try:
                n = int(self.headers.get("Content-Length", 0)); data = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
                ip = str(data.get("ip", "")); d = next((x for x in DVRS if x["ip"] == ip), None)
                if d:
                    ok, msg = dvr_reboot(d)
                    if ok: emit("warn", "reboot:" + ip, "%s (%s): reboot enviado pelo painel." % (d.get("name", "DVR"), ip))
                    body = json.dumps({"ok": ok, "msg": msg}).encode("utf-8")
                else:
                    body = b'{"ok":false,"msg":"DVR nao encontrado"}'
            except Exception as e:
                body = ('{"ok":false,"msg":"%s"}' % type(e).__name__).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
        elif self.path.startswith("/api/entrega"):
            # Recebe a credencial recem-criada numa estacao e repassa pelo
            # Telegram. Existe para o token do bot NAO precisar viajar no
            # pendrive: a estacao manda para ca e o token fica so aqui.
            import hmac
            try:
                if not self.client_address[0].startswith(LAN_PREFIX):
                    raise ValueError("fora da LAN")
                n = int(self.headers.get("Content-Length", 0))
                if n <= 0 or n > 4096:
                    raise ValueError("tamanho invalido")
                data = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
                esperado = str(ALERTS.get("entrega_segredo", ""))
                if not esperado:
                    raise ValueError("segredo nao configurado")
                if not hmac.compare_digest(str(data.get("segredo", "")), esperado):
                    raise ValueError("segredo invalido")

                def _c(k, lim=60):
                    # escapa HTML: o parse_mode do tg_send e HTML e a senha
                    # pode conter < > &
                    s = str(data.get(k, ""))[:lim]
                    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

                txt = ("%s <b>Credencial de gerencia</b>\n\n"
                       "Maquina: <b>%s</b>\n"
                       "<i>%s</i>\n"
                       "IP: <code>%s</code>   MAC: <code>%s</code>\n\n"
                       "Usuario: <code>%s</code>\n"
                       "Senha: <code>%s</code>\n\n"
                       "<i>Guarde no gerenciador de senhas e apague esta mensagem.</i>"
                       % (EMO["plug"], _c("maquina"), _c("modelo", 80), _c("ip", 40),
                          _c("mac", 40), _c("usuario", 40), _c("senha", 80)))
                ok = tg_send(txt)
                body = json.dumps({"ok": bool(ok),
                                   "err": "" if ok else "telegram recusou"}).encode("utf-8")
            except Exception as e:
                body = json.dumps({"ok": False,
                                   "err": "%s: %s" % (type(e).__name__, e)}).encode("utf-8")
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()


# ---------------------------------------------------------------------------
# APs STAND-ALONE: alerta de REINICIO via SNMP (22/out/2026)
# Os EAP225 sairam da controladora em 20/out. O vigia por ping so ve queda que
# dure mais de ~45 s; um AP que reinicia e volta sozinho passava em branco.
# Aqui lemos o sysUpTime (1.3.6.1.2.1.1.3.0) a cada 5 min: se ficou MENOR que
# na leitura anterior, o AP reiniciou. Ressalva: esse contador e do agente SNMP
# e tambem zera quando alguem SALVA a tela de SNMP do AP (o firmware nao
# informa o uptime do sistema - hrSystemUptime vem vazio).
# Communities em aps-snmp.json (gravado pelo dono, nunca por chat). Estado persistido em
# aps-uptime-estado.json, entao reiniciar o NetMon nao gera alarme falso.
# Entrada com "teste": true no estado dispara UMA mensagem marcada TESTE.
# ---------------------------------------------------------------------------
APS_SNMP_ARQ = os.path.join(BASE, "aps-snmp.json")
APS_UP_ESTADO = os.path.join(BASE, "aps-uptime-estado.json")


def aps_uptime_poller():
    try:
        with open(APS_UP_ESTADO, encoding="utf-8") as f:
            est = json.load(f)
    except Exception:
        est = {}
    while True:
        try:
            with open(APS_SNMP_ARQ, encoding="utf-8-sig") as f:
                cfg = json.load(f)
            comm = cfg.get("community")
            for ip in (cfg.get("hosts") or []):
                up = snmp_get(ip, comm, "1.3.6.1.2.1.1.3.0")
                if not isinstance(up, int):
                    continue          # sem resposta: queda quem avisa e o vigia por ping
                nome = snmp_get(ip, comm, "1.3.6.1.2.1.1.5.0") or ip
                ant = est.get(ip) or {}
                if ant and up < ant.get("up", 0):
                    minutos = up // 6000
                    teste = "[TESTE - pode ignorar] " if ant.get("teste") else ""
                    emit("warn", "apboot:" + ip,
                         "%s<b>%s</b> (%s) REINICIOU e voltou sozinho - ligado ha %d min.\n"
                         "Se ninguem salvou a tela de SNMP dele agora, foi reinicio de verdade "
                         "(queda de energia ou travamento)." % (teste, nome, ip, minutos))
                est[ip] = {"up": up, "ts": int(time.time()), "nome": str(nome)}
            with open(APS_UP_ESTADO, "w", encoding="utf-8") as f:
                f.write(json.dumps(est))
        except Exception as e:
            try:
                with open(os.path.join(BASE, "netmon.err.log"), "a", encoding="utf-8") as f:
                    f.write("%s aps_uptime_poller: %s: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), type(e).__name__, e))
            except Exception:
                pass
        time.sleep(300)

if __name__ == "__main__":
    db_init()
    threading.Thread(target=poller, daemon=True).start()
    if DVRS: threading.Thread(target=cam_poller, daemon=True).start()
    threading.Thread(target=name_poller, daemon=True).start()
    if ALERTS.get("telegram", {}).get("token"): threading.Thread(target=tg_poller, daemon=True).start()
    if OMADA.get("enabled"): threading.Thread(target=omada_poller, daemon=True).start()
    threading.Thread(target=estacoes_poller, daemon=True).start()
    if os.path.exists(APS_SNMP_ARQ): threading.Thread(target=aps_uptime_poller, daemon=True).start()
    srv = ThreadingHTTPServer((BIND, PORT), H)
    print("NetMon v5 em http://%s:%d  (Ctrl+C para parar)" % (BIND, PORT))
    try:
        srv.serve_forever()
    except BaseException:
        import traceback
        try:
            with open(r"C:\netmon\netmon.err.log", "a", encoding="utf-8", errors="replace") as f:
                f.write("\n##### MORREU %s #####\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
                traceback.print_exc(file=f)
        except Exception:
            pass
        raise
