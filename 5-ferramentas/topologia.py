r"""A topologia de switches esta causando problema? SO LE.

PERGUNTA DO DONO: "pode ter algo relacionado ao caos que esta a rede,
switch pra la, switch pra ca".

O que da pra medir sem switch gerenciavel:
  1. Gi0/0 do Cisco -- TODO o trafego da LAN passa por essa porta unica.
     Erro, colisao, descarte e duplex aparecem ai. E o melhor termometro
     que existe nesta rede.
  2. LLDP dos APs -- cada AP enxerga seus vizinhos e revela encadeamento.
  3. Tamanho da tabela ARP e quantidade de MACs -- dimensiona o dominio de
     broadcast (loop e tempestade moram aqui).
  4. Erros nas portas LAN dos proprios APs.

O que NAO da pra ver, e e justamente o buraco: switch nao gerenciavel nao
reporta nada. Nao ha como saber em que porta esta o problema, nem detectar
loop antes dele derrubar tudo.
"""
import io
import json
import re
import ssl
import subprocess
import urllib.request

CFG = json.load(io.open(r"C:\netmon\config.json", encoding="utf-8"))
CISCO = CFG.get("cisco") or {}
PLINK = CFG.get("plink") or r"C:\netmon\plink.exe"
CHOST = CISCO.get("host")
CUSER = CISCO.get("user") or ""
CPASS = CISCO.get("password") or CISCO.get("pass") or ""


def cisco(cmds):
    """As duas linhas em branco sao aquecimento: a 1a linha do stdin chega
    corrompida no SSHv1 do 2911."""
    entrada = ("\n\n" + "\n".join(cmds) + "\n").encode()
    p = subprocess.Popen([PLINK, "-1", "-batch", "-ssh", "%s@%s" % (CUSER, CHOST), "-pw", CPASS],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    saida, _ = p.communicate(entrada, timeout=90)
    return saida.decode("latin-1", "replace")


print("=" * 72)
print("1) Gi0/0 (LAN) DO CISCO -- o funil por onde TODA a rede passa")
print("=" * 72)
txt = cisco(["terminal length 0", "show interfaces GigabitEthernet0/0"])
interessa = ("line protocol", "Full-duplex", "Half-duplex", "Auto-duplex", "BW ",
             "packets input", "input errors", "packets output", "output errors",
             "collisions", "Total output drops", "runts", "giants", "throttles",
             "interface resets", "babbles", "late collision", "deferred",
             "lost carrier", "no carrier", "output buffer")
for l in txt.splitlines():
    if any(x in l for x in interessa):
        print("  " + l.strip())

print("\n" + "=" * 72)
print("2) TAMANHO DO DOMINIO DE BROADCAST (onde loop vira tempestade)")
print("=" * 72)
arp = cisco(["terminal length 0", "show ip arp"])
macs = re.findall(r"([0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4})", arp)
ips = re.findall(r"Internet\s+(\d+\.\d+\.\d+\.\d+)", arp)
print("  entradas ARP: %d   MACs distintos: %d" % (len(ips), len(set(macs))))
print("  TODOS no mesmo /24 e no mesmo dominio de broadcast (sem VLAN).")

print("\n" + "=" * 72)
print("3) O CISCO ESTA VENDO ALGO ESTRANHO? (log recente)")
print("=" * 72)
log = cisco(["terminal length 0", "show logging | include %LINK|%LINEPROTO|duplex|DUPLEX|STORM|LOOP|flap"])
linhas = [l.strip() for l in log.splitlines()
          if any(x in l for x in ("%LINK", "%LINEPROTO", "duplex", "DUPLEX", "STORM", "LOOP", "flap"))]
if linhas:
    for l in linhas[-20:]:
        print("  " + l)
else:
    print("  nada de link caindo/subindo nem aviso de duplex no buffer de log")

print("\n" + "=" * 72)
print("4) O QUE CADA AP ENXERGA COMO VIZINHO (LLDP) -- revela encadeamento")
print("=" * 72)
O = CFG["omada"]
BASE = O["host"].rstrip("/") + "/" + O["cid"]
SITE = O["site"]
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                 urllib.request.HTTPCookieProcessor())


def q(p, t=None, d=None):
    r = urllib.request.Request(BASE + p, data=json.dumps(d).encode() if d is not None else None)
    r.add_header("Content-Type", "application/json")
    if t:
        r.add_header("Csrf-Token", t)
    return json.load(op.open(r, timeout=30))


tok = q("/api/v2/login", d={"username": O["user"], "password": O["pass"]})["result"]["token"]
for x in q("/api/v2/sites/%s/devices" % SITE, tok).get("result", []):
    det = q("/api/v2/sites/%s/eaps/%s" % (SITE, x.get("mac")), tok)["result"]
    lan = det.get("lanTraffic") or {}
    print("\n  %s (%s)" % (x.get("name"), x.get("ip")))
    print("      porta LAN: rx_err=%s tx_err=%s rx_drop=%s tx_drop=%s"
          % (lan.get("rxErrPkts"), lan.get("txErrPkts"),
             lan.get("rxDropPkts"), lan.get("txDropPkts")))
    dl = det.get("downlinkList") or []
    if dl:
        for v in dl:
            print("      vizinho LLDP: %s (%s) modelo %s porta %s"
                  % (v.get("deviceName"), v.get("ip"), v.get("model"), v.get("port")))
    else:
        print("      nenhum vizinho LLDP (switch nao gerenciavel nao se anuncia)")

