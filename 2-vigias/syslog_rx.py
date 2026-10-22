r"""syslog_rx.py - receptor de syslog (UDP 514) no servidor do NetMon. Criado em 22/out/2026.

POR QUE: o log dos APs EAP225 fica so na memoria e SOME quando eles reiniciam -
justo numa travada perderiamos o que aconteceu antes. Com o log remoto apontado
pra ca, cada linha que o AP solta fica gravada em disco.

  - escuta UDP 514, aceita SO da rede local (config.json -> rede.lan)
  - grava um arquivo por dia: C:\netmon\logs\syslog\AAAA-MM-DD.log
    formato: "AAAA-MM-DD HH:MM:SS  <nome/ip>  <mensagem>"
  - apaga arquivos com mais de 90 dias
  - nao interpreta nada, nao alerta nada: e caixa-preta, pra consulta

USO: python syslog_rx.py            (servico - tarefa TI-Syslog)
     python syslog_rx.py --teste    (manda uma linha pra si mesmo e confere o arquivo)
"""
import datetime
import io
import json
import os
import re
import socket
import sys
import time

PASTA = r"C:\netmon\logs\syslog"
PORTA = 514
_CFG = json.load(io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"), encoding="utf-8"))
REDE = (_CFG.get("rede") or {}).get("lan", "192.168.0.0/24").rsplit(".", 1)[0] + "."
DIAS = 90
NOMES = {a["ip"]: a["nome"] for a in (_CFG.get("aps") or [])}
NOMES[(_CFG.get("cisco") or {}).get("host", "")] = "Roteador"
NOMES["127.0.0.1"] = "teste-local"
PRI = re.compile(r"^<\d{1,3}>")
# Os EAP225 mandam, a cada ~5 s, um lote com CADA conexao dos clientes
# ("AP MAC=... MAC SRC=... IP SRC=... IP DST=... proto=..."). Seriam ~150 MB/dia
# e um registro da navegacao de todo mundo - nao e o que queremos guardar.
# Lote de trafego e DESCARTADO; so contamos quantos chegaram (prova de vida).
TRAFEGO = re.compile(r"MAC SRC=\S+ IP SRC=\S+ IP DST=")
CONT_TRAFEGO = {}
ERR = os.path.join(r"C:\netmon", "syslog_rx.err.log")


def arquivo(dia):
    return os.path.join(PASTA, "%s.log" % dia)


def limpa_antigos():
    corte = (datetime.date.today() - datetime.timedelta(days=DIAS)).isoformat()
    for n in os.listdir(PASTA):
        if n.endswith(".log") and n[:10] < corte:
            try:
                os.remove(os.path.join(PASTA, n))
            except OSError:
                pass


def grava(origem, texto):
    agora = datetime.datetime.now()
    texto = PRI.sub("", texto).replace("\r", " ").replace("\n", " ").strip()
    texto = "".join(ch if ch.isprintable() else "?" for ch in texto)[:2000]
    with io.open(arquivo(agora.date().isoformat()), "a", encoding="utf-8") as f:
        f.write("%s  %-14s %s\n" % (agora.strftime("%Y-%m-%d %H:%M:%S"), NOMES.get(origem, origem), texto))


os.makedirs(PASTA, exist_ok=True)

if "--teste" in sys.argv:
    marca = "teste-syslog-%d" % int(time.time())
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(("<14>%s" % marca).encode(), ("127.0.0.1", PORTA))
    time.sleep(2)
    hoje = arquivo(datetime.date.today().isoformat())
    ok = os.path.exists(hoje) and marca in io.open(hoje, encoding="utf-8").read()
    print("teste: %s (%s)" % ("GRAVOU" if ok else "NAO GRAVOU - o servico esta rodando?", hoje))
    raise SystemExit(0 if ok else 1)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", PORTA))
ultimo_dia = None
while True:
    try:
        dados, (ip, _) = sock.recvfrom(8192)
        if not (ip.startswith(REDE) or ip == "127.0.0.1"):
            continue
        hoje = datetime.date.today()
        if hoje != ultimo_dia:
            if ultimo_dia and CONT_TRAFEGO:
                # uma linha por dia com o total descartado por AP (prova de que estavam vivos)
                for origem, n in sorted(CONT_TRAFEGO.items()):
                    grava(origem, "[resumo] %d lotes de log de trafego descartados em %s" % (n, ultimo_dia))
                CONT_TRAFEGO.clear()
            limpa_antigos()
            ultimo_dia = hoje
        texto = dados.decode("utf-8", "replace")
        if TRAFEGO.search(texto):
            CONT_TRAFEGO[ip] = CONT_TRAFEGO.get(ip, 0) + 1
            continue
        grava(ip, texto)
    except Exception as e:
        try:
            with io.open(ERR, "a", encoding="utf-8") as f:
                f.write("%s %s: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), type(e).__name__, e))
        except Exception:
            pass
        time.sleep(1)
