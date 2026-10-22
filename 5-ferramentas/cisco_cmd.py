r"""cisco_cmd.py - roda comandos no Cisco 2911 (SSHv1 via plink) e imprime a saida sem senha.
USO: python cisco_cmd.py "cmd1" "cmd2" ...   (ja entra em enable e terminal length 0)"""
import io
import json
import re
import subprocess
import sys

CFG = json.load(io.open(r"C:\netmon\config.json", encoding="utf-8"))
CI = CFG.get("cisco") or {}
PLINK = CFG.get("plink") or r"C:\netmon\plink.exe"
entrada = ("\n\nenable\n%s\nterminal length 0\nterminal width 200\n%s\nexit\n"
           % (CI.get("enable") or "", "\n".join(sys.argv[1:]))).encode()
p = subprocess.Popen([PLINK, "-1", "-batch", "-ssh", "%s@%s" % (CI.get("user", ""), CI.get("host", "")),
                      "-pw", CI.get("password") or ""], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
out, err = p.communicate(entrada, timeout=90)
txt = out.decode("latin-1", "replace")
if CI.get("enable"):
    txt = txt.replace(CI["enable"], "****")
print("\n".join(l for l in txt.splitlines() if l.strip() and not re.match(r"^\s*$", l)))
