"""
WOL - Wake-on-LAN para a rede local. Roda no servidor do NetMon.
Somente biblioteca padrao, no mesmo espirito do netmon.py.

O truque: uma maquina DESLIGADA some da tabela ARP, entao o MAC dela
nao esta mais disponivel na hora que voce precisa acorda-la. Por isso
este script mantem um CACHE de MACs, alimentado pelo proprio NetMon
(que le a ARP do Cisco a cada 5s). Rode com --atualizar de vez em
quando e o cache aprende sozinho.

Uso:
    python wol.py --listar
    python wol.py 192.168.0.50
    python wol.py .166
    python wol.py aa:bb:cc:dd:ee:ff
    python wol.py "App Vendas"
    python wol.py .166 --esperar 120
    python wol.py --atualizar

Modos de envio (--modo):
    broadcast  (padrao)  broadcast da LAN (config) e 255.255.255.255
    unicast              direto no IP do alvo; cria ARP estatico temporario,
                         porque maquina desligada nao esta mais na tabela ARP
    todos                dispara nos tres destinos

    python wol.py .166 --modo unicast
    python wol.py .166 --modo todos
    python wol.py aa:bb:cc:dd:ee:ff --para 192.168.1.255   # outra sub-rede
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = r"C:\netmon"
CACHE = os.path.join(BASE, "wol-macs.json")
API = "http://127.0.0.1:8787/api/metrics"
_R = (json.load(open(os.path.join(BASE, "config.json"), encoding="utf-8")).get("rede") or {})
REDE = _R.get("lan", "192.168.0.0/24").rsplit(".", 1)[0] + "."
ORIGEM = _R.get("servidor", "")                     # IP de saida do servidor
IFACE  = _R.get("interface_servidor", "Ethernet")   # nome da interface (para ARP estatico)
BROADCASTS = [_R.get("broadcast", "255.255.255.255"), "255.255.255.255"]
PORTAS = [9, 7]


def norm_mac(txt):
    """aceita 0a:e0:af:.., 0ae0.aff9.30d3, 0A-E0-.. -> 12 hex minusculos"""
    m = re.sub(r"[^0-9a-fA-F]", "", str(txt))
    return m.lower() if len(m) == 12 else None


def limpa(txt):
    """tira caractere nao imprimivel de nome vindo da auto-descoberta"""
    return "".join(c if 32 <= ord(c) < 127 or c.isalnum() else "?" for c in str(txt or ""))


def bonito(mac):
    return ":".join(mac[i:i + 2] for i in range(0, 12, 2))


def carrega_cache():
    if os.path.exists(CACHE):
        try:
            with open(CACHE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def salva_cache(c):
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=1, sort_keys=True)


def atualiza_cache(silencioso=False):
    """le os hosts do NetMon (que ja vem da ARP do Cisco) e memoriza ip/nome -> mac"""
    cache = carrega_cache()
    novos = 0
    try:
        with urllib.request.urlopen(API, timeout=10) as r:
            dados = json.load(r)
    except Exception as e:
        if not silencioso:
            print("  nao consegui falar com o NetMon (%s) - uso so o cache" % e)
        return cache, 0

    for h in dados.get("hosts", []):
        ip = h.get("ip")
        mac = norm_mac(h.get("mac", ""))
        if not ip or not mac:
            continue
        reg = cache.get(ip, {})
        if reg.get("mac") != mac:
            novos += 1
        reg["mac"] = mac
        nome = (h.get("name") or "").strip()
        if nome and not nome.startswith("host ."):
            reg["nome"] = nome
        reg["visto"] = time.strftime("%Y-%m-%d %H:%M")
        cache[ip] = reg

    salva_cache(cache)
    if not silencioso:
        print("  cache atualizado: %d hosts conhecidos (%d novos/alterados)" % (len(cache), novos))
    return cache, novos


def resolve(alvo, cache):
    """devolve (mac, ip, nome) a partir de MAC, IP, '.166' ou nome"""
    m = norm_mac(alvo)
    if m:
        for ip, reg in cache.items():
            if reg.get("mac") == m:
                return m, ip, reg.get("nome", "")
        return m, None, ""

    ip = alvo.strip()
    if re.fullmatch(r"\.?\d{1,3}", ip):
        ip = REDE + ip.lstrip(".")
    if ip in cache:
        return cache[ip].get("mac"), ip, cache[ip].get("nome", "")

    alvo_l = alvo.strip().lower()
    for i, reg in cache.items():
        if reg.get("nome", "").lower() == alvo_l:
            return reg.get("mac"), i, reg.get("nome", "")
    for i, reg in cache.items():
        if alvo_l in reg.get("nome", "").lower():
            return reg.get("mac"), i, reg.get("nome", "")
    return None, None, ""


def ja_no_arp(ip):
    """o IP ja tem entrada ARP valida? se sim, unicast dispensa ARP estatico"""
    try:
        r = subprocess.run(["netsh", "interface", "ipv4", "show", "neighbors"],
                           capture_output=True, text=True, timeout=10)
        for l in r.stdout.splitlines():
            campos = l.split()
            if len(campos) >= 2 and campos[0] == ip:
                # NAO da para ler o estado por texto: em pt-BR o netsh escreve
                # "Inalcancavel" e a codificacao do console quebra a palavra em
                # dois campos. O criterio confiavel e: o campo 1 e um MAC valido?
                mac_txt = campos[1]
                so_hex = re.sub(r"[^0-9a-fA-F]", "", mac_txt)
                if len(so_hex) != 12:
                    return False            # nao ha MAC -> entrada invalida
                if so_hex == "000000000000":
                    return False            # MAC nulo -> resolucao falhou
                return True
    except Exception:
        pass
    return False


def arp_estatico(ip, mac, remover=False):
    """
    Maquina desligada some da tabela ARP, entao o Windows nao sabe para qual
    MAC mandar um pacote unicast. Esta funcao insere (ou tira) uma entrada
    ARP temporaria para permitir o envio dirigido. Precisa de admin.
    """
    mac_win = "-".join(mac[i:i + 2] for i in range(0, 12, 2))
    if remover:
        cmd = ["netsh", "interface", "ipv4", "delete", "neighbors", IFACE, ip]
    else:
        cmd = ["netsh", "interface", "ipv4", "add", "neighbors", IFACE, ip, mac_win, "store=active"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def acorda(mac, ip=None, modo="broadcast", para=None):
    """
    monta e dispara o pacote magico: 6x FF + 16x o MAC.
    modo: broadcast | unicast | todos
    para: destino manual (ex. broadcast de outra sub-rede, 192.168.1.255)
    """
    pacote = b"\xff" * 6 + bytes.fromhex(mac) * 16
    enviados = []
    arp_criado = False

    destinos = []
    if para:
        destinos = [para]
    else:
        if modo in ("broadcast", "todos"):
            destinos += BROADCASTS
        if modo in ("unicast", "todos"):
            if ip:
                destinos.append(ip)
            else:
                enviados.append("unicast IGNORADO (alvo sem IP conhecido)")

    # So mexe no ARP quando o host NAO esta na tabela (ou seja, desligado).
    # Se ja estiver la, o Windows resolve sozinho - e apagar a entrada
    # aprendida deixaria o host momentaneamente 'Unreachable'.
    if ip and (ip in destinos):
        if ja_no_arp(ip):
            enviados.append("(ARP ja existia - nao mexi)")
        else:
            arp_criado = arp_estatico(ip, mac)
            enviados.append("ARP estatico criado" if arp_criado
                            else "aviso: nao consegui criar ARP estatico (precisa de admin?)")

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        try:
            s.bind((ORIGEM, 0))     # garante a interface certa
        except Exception:
            pass
        for dest in destinos:
            for porta in PORTAS:
                try:
                    s.sendto(pacote, (dest, porta))
                    enviados.append("%s:%d" % (dest, porta))
                except Exception as e:
                    enviados.append("%s:%d FALHOU(%s)" % (dest, porta, e))
    finally:
        s.close()
        if arp_criado:
            time.sleep(0.5)          # deixa o pacote sair antes de limpar
            arp_estatico(ip, mac, remover=True)

    return len(pacote), enviados


def responde(ip):
    try:
        r = subprocess.run(["ping", "-n", "1", "-w", "1000", ip],
                           capture_output=True, text=True, timeout=6)
        return "TTL=" in r.stdout or "TTL=" in r.stdout.upper()
    except Exception:
        return False


def main():
    args = [a for a in sys.argv[1:]]
    if not args or "--ajuda" in args or "-h" in args:
        print(__doc__)
        return

    if "--atualizar" in args:
        atualiza_cache()
        if len(args) == 1:
            return
        args = [a for a in args if a != "--atualizar"]

    cache = carrega_cache()

    if "--listar" in args:
        cache, _ = atualiza_cache(silencioso=True)
        print("ALVOS CONHECIDOS (%d)" % len(cache))
        print("%-16s %-19s %-24s %s" % ("IP", "MAC", "NOME", "VISTO"))
        for ip in sorted(cache, key=lambda x: int(x.split(".")[-1]) if x.count(".") == 3 else 0):
            reg = cache[ip]
            print("%-16s %-19s %-24s %s" % (ip, bonito(reg.get("mac", "")),
                                            limpa(reg.get("nome"))[:24], reg.get("visto", "")))
        return

    espera = 0
    if "--esperar" in args:
        i = args.index("--esperar")
        try:
            espera = int(args[i + 1])
            del args[i:i + 2]
        except Exception:
            print("!! --esperar precisa de um numero de segundos")
            return

    modo = "broadcast"
    if "--modo" in args:
        i = args.index("--modo")
        try:
            modo = args[i + 1].lower()
            del args[i:i + 2]
        except Exception:
            modo = "broadcast"
        if modo not in ("broadcast", "unicast", "todos"):
            print("!! --modo aceita: broadcast | unicast | todos")
            return

    para = None
    if "--para" in args:
        i = args.index("--para")
        try:
            para = args[i + 1]
            del args[i:i + 2]
        except Exception:
            print("!! --para precisa de um IP de destino")
            return

    if not args:
        print("!! informe o alvo (IP, MAC ou nome). Use --listar para ver os conhecidos.")
        return

    alvo = args[0]
    # tenta atualizar antes, pra pegar MAC de quem apareceu agora
    cache, _ = atualiza_cache(silencioso=True)
    mac, ip, nome = resolve(alvo, cache)

    if not mac:
        print("!! nao encontrei o MAC de '%s'." % alvo)
        print("   veja os conhecidos com:  python wol.py --listar")
        print("   ou informe o MAC direto: python wol.py aa:bb:cc:dd:ee:ff")
        return

    etiqueta = nome or ip or "?"
    print("ACORDANDO  %s   [%s]  %s" % (etiqueta, bonito(mac), ip or ""))

    if ip and responde(ip):
        print("  ja esta no ar (respondeu ao ping) - envio o pacote mesmo assim")

    tam, destinos = acorda(mac, ip, modo, para)
    print("  modo: %s%s" % (modo, ("  destino manual: " + para) if para else ""))
    print("  pacote magico de %d bytes -> %s" % (tam, ", ".join(destinos)))

    if espera and ip:
        print("  aguardando ate %ds pela resposta..." % espera)
        t0 = time.time()
        while time.time() - t0 < espera:
            if responde(ip):
                seg = int(time.time() - t0)
                print("  RESPONDEU em %ds" % seg)
                print("  ATENCAO: isto so diz que o IP voltou a responder.")
                print("  NAO prova que foi o pacote - se alguem apertou o botao")
                print("  nesta janela, o resultado seria o mesmo. Para valer como")
                print("  teste, ninguem pode encostar na maquina.")
                return
            time.sleep(5)
        print("  nao respondeu em %ds." % espera)
        print("  causas comuns: WOL desligado na BIOS/placa, cabo fora, ou")
        print("  'Fast Startup' do Windows (que desliga a placa de vez).")
    elif espera:
        print("  (sem IP conhecido para confirmar - so enviei o pacote)")


if __name__ == "__main__":
    main()
