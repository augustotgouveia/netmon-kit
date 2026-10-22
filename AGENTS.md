# AGENTS.md — instalar o NetMon Kit com um agente de IA

Este arquivo é para **agentes de código** (Claude Code, Codex, Cursor, Copilot e similares) que forem
instalar ou manter o NetMon Kit a pedido de alguém. Leia inteiro antes de executar qualquer coisa.

Quem está do outro lado normalmente é o **dono de uma pequena empresa** ou quem cuida da TI dela, não
um especialista. Fale em português simples, explique o que cada passo faz e por quê, e trabalhe
**com** a pessoa: ela conhece a rede, você conhece o kit.

---

## Regras inegociáveis

1. **Segredo nunca passa pelo chat.** Senha, token do Telegram, community SNMP, credencial de DVR ou
   roteador: a pessoa digita **direto no arquivo** (`notepad C:\netmon\config.json`). Não peça, não
   aceite colado na conversa e não imprima esses valores. Para conferir se foram preenchidos, verifique
   só se ainda existe `<PREENCHER>`.
2. **Não mexa na rede da máquina em uso.** Nada de trocar IP, adicionar IP extra, mudar DNS, rota,
   adaptador ou perfil de rede, nem desativar serviço do Windows. Um IP "temporário" já derrubou a
   conexão de quem estava trabalhando, e desativar o `WinHttpAutoProxySvc` mata o Wi-Fi no próximo
   boot.
3. **Leitura é livre; mudança, só com OK.** Antes de qualquer alteração, diga o que vai fazer, o
   impacto e como desfazer, e espere a confirmação. Rode sempre `INSTALAR.ps1 -Simular` antes do
   `INSTALAR.ps1` de verdade.
4. **Equipamento de terceiros** (roteador, APs, DVRs, modem) é **só leitura**, salvo pedido explícito.
5. **Nada sai da rede da pessoa** além das mensagens para o próprio Telegram dela. Não publique,
   não envie logs ou configurações para lugar nenhum.
6. **Download só com permissão**, dizendo o quê, de onde e o tamanho. Fontes oficiais apenas
   (python.org, curl.se, chiark.greenend.org.uk/~sgtatham/putty, github.com/caddyserver,
   github.com/AlexxIT/go2rtc).

---

## Fluxo de instalação

### Fase 1 — Descoberta (só leitura)
Rode e resuma para a pessoa, sem jargão:

```powershell
Get-ComputerInfo -Property OsName, OsVersion, CsName
Get-NetIPConfiguration | Where-Object IPv4DefaultGateway
Get-NetNeighbor -AddressFamily IPv4 | Where-Object State -ne 'Unreachable'
Get-ScheduledTask | Where-Object TaskName -match 'NetMon|TI-'      # instalação anterior?
Test-Path C:\netmon\config.json
```

Daí saem: IP do servidor (`rede.servidor`), faixa (`rede.lan`, **só /24 é suportado**), gateway
(provável roteador) e quem mais está na rede. Para identificar aparelhos, use os 6 primeiros dígitos do
MAC (fabricante) e pergunte.

### Fase 2 — Entrevista
Pergunte, um bloco por vez:

| Pergunta | Vai para |
|---|---|
| Como a empresa quer ser chamada no painel? | `site_name` |
| O roteador é Cisco com SSH? Outro modelo? | `cisco.*` (sem Cisco: deixe `<PREENCHER>`, o módulo fica de fora) |
| Qual o IP do modem da operadora? | `rede.modem` |
| Existe VPS/servidor na nuvem que, se cair, para a venda? IP e nome do sistema? | `vps.host`, `vps.sistema` |
| Quais são os APs (nome e IP)? | `aps` |
| Tem rádio entre prédios? DVRs? | `antenna_link`, `dvrs` |
| Quem recebe os alertas? | `alerts.telegram` (a pessoa cria o bot no @BotFather e preenche) |
| O que é crítico e precisa de vigia especial? | `alerts.rules.watch_hosts` |

### Fase 3 — Montar o config
1. Copie `0-modelos\config.example.json` para `C:\netmon\config.json` (e os outros `*.example.*`
   que forem usados: `dns.json`, `go2rtc.yaml`, `aps-snmp.json`).
2. Preencha **você** só o que não é segredo: nomes, IPs, faixas.
3. Abra o arquivo para a pessoa (`notepad C:\netmon\config.json`) e peça que ela preencha os
   `<PREENCHER>`. Depois confirme sem ler os valores:
   `Select-String C:\netmon\config.json -Pattern '<PREENCHER' | Measure-Object`.
4. Valide: `Get-Content C:\netmon\config.json -Raw | ConvertFrom-Json | Out-Null`.

### Fase 4 — Dependências
| Item | Onde fica | Obrigatório? |
|---|---|---|
| Python 3 ("Windows embeddable package") | `C:\netmon\python\` | sim |
| `cacert.pem` (curl.se/ca/cacert.pem) | `C:\netmon\` | sim no Server 2016 |
| `plink.exe` (PuTTY) | `C:\netmon\` | se tiver roteador com SSH |
| `caddy.exe`, `go2rtc.exe` | `C:\netmon\` | opcionais (HTTPS, câmeras) |

Com roteador SSH: peça para a pessoa rodar o `plink` **uma vez** e aceitar a chave do host, antes do
instalador (ele copia essa chave para o SYSTEM).

### Fase 5 — Instalar
A pessoa abre o PowerShell **como administrador** na pasta do kit:

```powershell
.\INSTALAR.ps1 -Simular      # revise a saída junto com ela
.\INSTALAR.ps1               # testa o Telegram, cria tarefas e firewall, sobe tudo
```

Se o teste do Telegram falhar, o instalador para. Não use `-PularTelegram` sem entender o motivo:
um monitor que não consegue avisar é pior que nenhum.

### Fase 6 — Conferir
- Painel: `http://<rede.servidor>:8787` abre de outra máquina da rede.
- Tarefas: `Get-ScheduledTask -TaskName NetMon*,TI-* | Get-ScheduledTaskInfo` mostra `LastTaskResult` 0 ou 267009 (rodando).
- Logs: `C:\netmon\*.err.log` vazios ou sem erro novo.
- Mande a pessoa olhar o Telegram: chegou a mensagem de teste?

### Fase 7 — Entregar
Explique, em poucas linhas: o que está sendo vigiado, como os alertas funcionam (novo na hora, repete
às 12 h e 18 h, avisa quando volta), onde fica o painel e como desfazer (`.\INSTALAR.ps1 -Remover`).
Combine revisar os limites depois de uma semana.

---

## Armadilhas conhecidas (diagnóstico rápido)

| Sintoma | Causa provável |
|---|---|
| Telegram não chega no Server 2016 | falta `cacert.pem` (TLS); ou `chat_id` errado |
| Leitura do roteador vazia só quando roda como tarefa | chave de host do PuTTY não está no perfil do SYSTEM |
| Vigia parou depois de ~3 dias | limite da tarefa não é `PT0S` |
| Python não lê um JSON | arquivo gravado com BOM; leia com `utf-8-sig` |
| "Host está fora" mas ele responde | `ping.exe` dá exit 0 com "host inacessível"; use `Test-Connection` ou procure `TTL=` |
| Variável muda de valor sozinha num `.ps1` | PowerShell não diferencia maiúscula (`$INTL` = `$intl`) |
| Notebook "parou de imprimir" | fila WSD presa à placa de rede (`fe80::…%N`) em que foi criada |
| Status diz "tudo ok" mas tem problema | conferir alertas em aberto (os que repetem com "(continua)") |

O README tem as 17 lições completas.

## Manutenção
- Atualizar: `git pull` na pasta do kit e `.\INSTALAR.ps1` de novo (ele guarda cópia do que trocar).
- Remover: `.\INSTALAR.ps1 -Remover` (tarefas e firewall saem; arquivos e config ficam).
- Antes de qualquer commit ou PR: nenhum IP real, nome de pessoa ou segredo. `config.json` está no `.gitignore` por isso.
