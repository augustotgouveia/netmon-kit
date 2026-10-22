r"""Varredura da LAN: o que existe, o que esta sob gerencia, o que esta exposto.

SO LE -- nenhuma conexao autenticada, nenhuma tentativa de login. SÃ³ pergunta
"essa porta atende?" e "qual o nome dessa maquina?".

PRIORIDADE: as maquinas .166 e .176 rodam o tunel SSH ate a VPS, ou seja, SAO
o sistema de venda -- e nenhuma das duas esta no cofre. Sao o maior ponto cego
da rede e justamente o que o dono disse que importa.

Cruza tres fontes: ARP do Cisco (quem existe), cofre DPAPI (quem esta sob
gerencia) e config.names (quem a gente ja batizou).
"""
import concurrent.futures as cf
import io
import json
import os
import re
import socket
import subprocess

CFG = json.load(io.open(r"C:\netmon\config.json", encoding="utf-8"))
NOMES = CFG.get("names") or {}
OUI = CFG.get("oui") or {}
CISCO = CFG.get("cisco") or {}
PLINK = CFG.get("plink") or r"C:\netmon\plink.exe"

PORTAS = [(3389, "RDP"), (445, "SMB"), (5985, "WinRM"), (22, "SSH"),
          (80, "HTTP"), (443, "HTTPS"), (3306, "MySQL"), (554, "RTSP"), (8080, "HTTP-alt")]

# quem esta sob gerencia (tem credencial no cofre)
geridas = {}
for a in os.listdir(r"C:\netmon\creds"):
    if a.endswith(".xml"):
        try:
            import subprocess as sp
            geridas[a[:-4]] = True
        except Exception:
            pass
# o IP de cada uma vem do proprio cofre, lido via PowerShell (Clixml e formato PS)
try:
    ps = sp.run(["powershell", "-NoProfile", "-Command",
                 "Get-ChildItem C:\\netmon\\creds\\*.xml | ForEach-Object { $r=Import-Clixml $_.FullName; \"$($r.ip)|$($r.maquina)\" }"],
                capture_output=True, timeout=90)
    cofre = {}
    for l in ps.stdout.decode("latin-1", "replace").splitlines():
        if "|" in l:
            ip, nome = l.strip().split("|", 1)
            cofre[ip] = nome
except Exception:
    cofre = {}


def arp_cisco():
    entrada = b"\n\nterminal length 0\nshow ip arp\n"
    p = subprocess.Popen([PLINK, "-1", "-batch", "-ssh",
                          "%s@%s" % (CISCO.get("user", ""), CISCO.get("host", "")),
                          "-pw", CISCO.get("password") or ""],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    saida, _ = p.communicate(entrada, timeout=90)
    txt = saida.decode("latin-1", "replace")
    out = {}
    for l in txt.splitlines():
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)\s+\S+\s+([0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4})", l)
        if m:
            cru = m.group(2).replace(".", "")
            mac = "-".join(cru[i * 2:i * 2 + 2] for i in range(6)).upper()
            out[m.group(1)] = mac
    return out


def porta(ip, p, timeout=1.5):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((ip, p))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def nome_de(ip):
    try:
        return socket.gethostbyaddr(ip)[0].split(".")[0]
    except Exception:
        return ""


def olha(ip, mac):
    abertas = [n for p, n in PORTAS if porta(ip, p)]
    return {"ip": ip, "mac": mac, "dns": nome_de(ip), "portas": abertas,
            "fab": OUI.get(mac[:8].replace("-", ":"), OUI.get(mac[:8], "")),
            "nome": NOMES.get(ip, ""), "cofre": cofre.get(ip, "")}


arp = arp_cisco()
print("hosts no ARP do Cisco: %d" % len(arp))
print("maquinas no cofre (sob gerencia): %d\n" % len(cofre))

res = []
with cf.ThreadPoolExecutor(max_workers=24) as ex:
    for r in ex.map(lambda kv: olha(kv[0], kv[1]), sorted(arp.items(),
                    key=lambda kv: int(kv[0].split(".")[-1]))):
        res.append(r)

print("=" * 96)
print("%-16s %-18s %-20s %-10s %s" % ("IP", "MAC", "NOME", "GERENCIA", "PORTAS ABERTAS"))
print("=" * 96)
for r in res:
    nome = r["nome"] or r["cofre"] or r["dns"] or r["fab"] or "?"
    ger = "cofre" if r["cofre"] else ("-" if r["portas"] else "")
    marca = ""
    if r["ip"].endswith(".166") or r["ip"].endswith(".176"):
        marca = "   <<<< RODA A VENDA"
    print("%-16s %-18s %-20s %-10s %s%s" % (
        r["ip"], r["mac"], nome[:20], ger, ", ".join(r["portas"]) or "nenhuma das testadas", marca))

print("\n" + "=" * 96)
print("WINDOWS SEM GERENCIA (tem SMB ou RDP e NAO esta no cofre)")
print("=" * 96)
alvo = [r for r in res if not r["cofre"] and ("SMB" in r["portas"] or "RDP" in r["portas"])]
if not alvo:
    print("  nenhuma")
for r in alvo:
    nome = r["nome"] or r["dns"] or r["fab"] or "?"
    tem_winrm = "WinRM" in r["portas"]
    print("  %-16s %-22s %s   %s" % (
        r["ip"], nome[:22], ", ".join(r["portas"]),
        "(da pra adotar: WinRM aberto)" if tem_winrm else "(sem WinRM - precisaria do pendrive)"))

print("\n" + "=" * 96)
print("RESUMO")
print("=" * 96)
print("  hosts vistos             : %d" % len(res))
print("  sob gerencia (cofre)     : %d" % len([r for r in res if r["cofre"]]))
print("  com RDP aberto na LAN    : %d" % len([r for r in res if "RDP" in r["portas"]]))
print("  com SMB aberto na LAN    : %d" % len([r for r in res if "SMB" in r["portas"]]))
print("  sem nenhuma porta conhecida: %d" % len([r for r in res if not r["portas"]]))

