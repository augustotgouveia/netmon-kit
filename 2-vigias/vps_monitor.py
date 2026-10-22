r"""vps_monitor.py - vigia PERMANENTE da VPS de vendas, no servidor da empresa.

ORIGEM: no piloto, uma pane geral do provedor (~1h20) deixou a VPS ilhada e
PAROU A VENDA. Um monitor de emergencia rodou num notebook; este e a versao
definitiva, para morar no servidor.

DIFERENCAS em relacao ao de emergencia:
  - PYTHON, nao PowerShell: o PS do Server 2016 nao fecha TLS com sites modernos
    (Invoke-WebRequest da "nao foi possivel criar um canal seguro"). O Telegram
    sai por urllib + C:\netmon\cacert.pem, igual ao NetMon.
  - PERMANENTE e nas duas direcoes: avisa quando CAI e quando VOLTA.
  - Mede o SSH (porta 22) -- e por ali que passam os tuneis do sistema de vendas. Ping
    e so dado auxiliar (ICMP pode ser filtrado algum dia sem nada quebrar).
  - Nao culpa a VPS pela internet da empresa: se nem o Google responde, a
    amostra e DESCARTADA (quem vigia a WAN e o TI-SondaWAN2).
  - No aviso de volta conta os TUNEIS de pe pelo NAT do Cisco, e conta de novo
    10 min depois. Licao do piloto: depois que a VPS volta, o sistema de vendas aberto
    NAO reconecta sozinho -- tem que fechar e abrir.

DECISAO DE ESTADO, por janela deslizante de 20 amostras x 15 s = 5 min:
  ok   -> fora : menos de 60% de SSH ok na janela
  fora -> ok   : 95% ou mais de SSH ok na janela
Enquanto a janela nao enche (logo apos iniciar) NAO decide nada: reiniciar o
servidor nao gera alarme falso. Estado persistido em vps-monitor-estado.json.

USO:
  python vps_monitor.py                        -> roda para sempre (modo servico)
  python vps_monitor.py --teste                -> uma medicao de cada coisa, imprime, sai
  python vps_monitor.py --teste --telegram     -> idem + manda mensagem de teste
  --base <pasta>                               -> pasta do config.json (padrao C:\netmon)
"""
import io
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.request

VPS = None   # vem do config.json (vps.host), lido mais abaixo
INTERVALO = 15
JANELA = 20
LIMITE_CAIU = 0.60
LIMITE_VOLTOU = 0.95
RECONTAR_TUNEIS_APOS = 600      # 10 min depois de voltar


def _arg(nome, padrao=None):
    if nome in sys.argv:
        i = sys.argv.index(nome)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return padrao


BASE = _arg("--base", r"C:\netmon")
CSV = os.path.join(BASE, "vps-monitor.csv")
ESTADO_ARQ = os.path.join(BASE, "vps-monitor-estado.json")
ERRLOG = os.path.join(BASE, "vps-monitor.err.log")

CFG = json.load(io.open(os.path.join(BASE, "config.json"), encoding="utf-8"))
TG = (CFG.get("alerts") or {}).get("telegram") or {}
CISCO = CFG.get("cisco") or {}
PLINK = CFG.get("plink") or os.path.join(BASE, "plink.exe")
VPS = (CFG.get("vps") or {}).get("host")
SISTEMA = (CFG.get("vps") or {}).get("sistema", "sistema de vendas")
SERVIDOR = (CFG.get("rede") or {}).get("servidor", "")
if not VPS:
    raise SystemExit("config.json: falta vps.host")

VERDE, VERMELHO, AMARELO = "\u2705", "\U0001F534", "\u26A0"


def log_erro(texto):
    try:
        with io.open(ERRLOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), texto))
    except Exception:
        pass


def _ctx():
    ca = os.path.join(BASE, "cacert.pem")
    try:
        return ssl.create_default_context(cafile=ca) if os.path.exists(ca) else ssl.create_default_context()
    except Exception:
        return ssl.create_default_context()


def avisa(texto):
    """3 tentativas: o caminho ate o Telegram e internacional e pisca sozinho."""
    tok, chat = TG.get("token"), TG.get("chat_id")
    if not (tok and chat):
        log_erro("sem token/chat_id no config")
        return False
    dados = json.dumps({"chat_id": chat, "text": texto, "parse_mode": "HTML"}).encode()
    erro = None
    for n in range(3):
        try:
            r = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % tok, data=dados)
            r.add_header("Content-Type", "application/json")
            urllib.request.urlopen(r, timeout=25, context=_ctx()).read()
            return True
        except Exception as e:
            erro = "%s: %s" % (type(e).__name__, str(e)[:150])
            time.sleep(3 * (n + 1))
    log_erro("telegram falhou apos 3 tentativas: %s" % erro)
    return False


def ssh_ok(timeout=4):
    """Le o banner do sshd. E o que o tunel do PDV precisa -- nao autentica."""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        t0 = time.time()
        s.connect((VPS, 22))
        ok = s.recv(64).startswith(b"SSH-")
        return ok, int((time.time() - t0) * 1000)
    except Exception:
        return False, -1
    finally:
        try:
            s.close()
        except Exception:
            pass


def ping_ok(alvo):
    try:
        return subprocess.call(["ping", "-n", "1", "-w", "1500", alvo],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
    except Exception:
        return False


def internet_ok():
    """Se nem o Google/Cloudflare respondem, o problema e a WAN, nao a VPS."""
    for host, porta in (("8.8.8.8", 443), ("1.1.1.1", 443)):
        s = socket.socket()
        s.settimeout(3)
        try:
            s.connect((host, porta))
            return True
        except Exception:
            pass
        finally:
            try:
                s.close()
            except Exception:
                pass
    return False


def tuneis():
    """IPs da LAN com tunel SSH de pe ate a VPS, lidos do NAT do Cisco.
    None se nao der para perguntar (plink ausente, Cisco fora)."""
    if not os.path.exists(PLINK):
        return None
    entrada = ("\n\nenable\n%s\nterminal length 0\nshow ip nat translations | include %s:22\n"
               % (CISCO.get("enable") or "", VPS)).encode()
    try:
        p = subprocess.Popen([PLINK, "-1", "-batch", "-ssh",
                              "%s@%s" % (CISCO.get("user", ""), CISCO.get("host", "")),
                              "-pw", CISCO.get("password") or ""],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        saida, _ = p.communicate(entrada, timeout=40)
    except Exception as e:
        log_erro("tuneis: %s" % str(e)[:120])
        return None
    txt = saida.decode("latin-1", "replace")
    # Sem o prompt privilegiado do roteador na saida, a sessao NAO aconteceu
    # (Cisco fora, senha errada, plink barrado). Devolver lista vazia aqui
    # viraria "Nenhum tunel de venda de pe" -- alarme falso. O teste no
    # notebook, fora da rede, pegou exatamente isso.
    if not re.search(r"^\S+#", txt, re.M):
        log_erro("tuneis: sessao com o Cisco nao confirmada (sem prompt '#')")
        return None
    padrao = r"\s(192\.168\.2\.\d+):\d+\s+%s:22" % re.escape(VPS)
    # O PROPRIO servidor aparece no NAT: e o teste de SSH deste vigia, a cada
    # 15 s. Nao e tunel de venda -- contava sempre um a mais. Fora da lista.
    try:
        meus = set(socket.gethostbyname_ex(socket.gethostname())[2])
    except Exception:
        meus = set()
    meus.add(SERVIDOR)
    achados = set(re.findall(padrao, txt)) - meus
    return sorted(achados, key=lambda ip: int(ip.split(".")[-1]))


def texto_tuneis(lista):
    if lista is None:
        return "Nao consegui contar os tuneis (Cisco nao respondeu)."
    if not lista:
        return "<b>Nenhum tunel de venda de pe.</b> Fechar e abrir o %s nas maquinas da venda." % SISTEMA
    return "Tuneis de venda de pe: <b>%d</b> (%s)." % (len(lista), ", ".join(lista))


def carrega_estado():
    try:
        return json.load(io.open(ESTADO_ARQ, encoding="utf-8"))
    except Exception:
        return {"estado": "ok", "desde": time.time()}


def salva_estado(st):
    try:
        with io.open(ESTADO_ARQ, "w", encoding="utf-8") as f:
            f.write(json.dumps(st))
    except Exception as e:
        log_erro("estado: %s" % e)


# ------------------------------------------------------------------ teste
if "--teste" in sys.argv:
    print("base         : %s" % BASE)
    print("internet     : %s" % internet_ok())
    ok, ms = ssh_ok()
    print("SSH da VPS   : %s (%s ms)" % (ok, ms))
    print("ping da VPS  : %s" % ping_ok(VPS))
    t = tuneis()
    print("tuneis       : %s" % ("nao deu para contar" if t is None else (", ".join(t) or "nenhum")))
    print("telegram cfg : token=%s chat_id=%s" % (bool(TG.get("token")), bool(TG.get("chat_id"))))
    if "--telegram" in sys.argv:
        enviado = avisa("%s <b>Teste do vigia da VPS</b>\nSSH %s (%s ms). %s\n"
                        "Se esta mensagem chegou, o servidor consegue te avisar."
                        % (VERDE, "ok" if ok else "FORA", ms, texto_tuneis(t)))
        print("telegram     : %s" % ("ENVIADO" if enviado else "FALHOU - ver vps-monitor.err.log"))
    sys.exit(0)

# ------------------------------------------------------------------ servico
if not os.path.exists(CSV):
    io.open(CSV, "w", encoding="utf-8").write("quando,internet,ssh,ssh_ms,ping,taxa_ok,estado\n")

st = carrega_estado()
janela = []
recontar_em = None

while True:
    try:
        net = internet_ok()
        ok, ms = ssh_ok()
        pg = ping_ok(VPS)
        if net:                                   # sem internet, a amostra nao vale
            janela.append(ok)
            janela = janela[-JANELA:]
        taxa = (sum(janela) / float(len(janela))) if janela else 0.0

        with io.open(CSV, "a", encoding="utf-8") as f:
            f.write("%s,%s,%s,%s,%s,%.2f,%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                                  net, ok, ms, pg, taxa, st["estado"]))

        if len(janela) >= JANELA:
            agora = time.time()
            if st["estado"] == "ok" and taxa < LIMITE_CAIU:
                st = {"estado": "fora", "desde": agora}
                salva_estado(st)
                avisa("%s <b>VPS de vendas com problema</b> as %s\n"
                      "So %d%% das conexoes SSH funcionaram nos ultimos 5 min - "
                      "<b>a venda pode estar parada</b>.\n"
                      "A internet da empresa esta ok, entao o problema e do lado da VPS/provedor."
                      % (VERMELHO, time.strftime("%H:%M"), int(taxa * 100)))
            elif st["estado"] == "fora" and taxa >= LIMITE_VOLTOU:
                minutos = int((agora - st.get("desde", agora)) / 60)
                st = {"estado": "ok", "desde": agora}
                salva_estado(st)
                avisa("%s <b>VPS de vendas VOLTOU</b> as %s (fora por ~%d min)\n%s\n"
                      "Sistema de vendas que der erro: fechar e abrir. Reconto os tuneis em 10 min."
                      % (VERDE, time.strftime("%H:%M"), minutos, texto_tuneis(tuneis())))
                recontar_em = agora + RECONTAR_TUNEIS_APOS

        if recontar_em and time.time() >= recontar_em:
            recontar_em = None
            avisa("%s Recontagem 10 min apos a volta da VPS: %s" % (VERDE, texto_tuneis(tuneis())))
    except Exception as e:
        log_erro("laco: %s: %s" % (type(e).__name__, str(e)[:150]))
    time.sleep(INTERVALO)
