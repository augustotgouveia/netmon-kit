# NetMon Kit

🇧🇷 [Português](README.md) · 🇺🇸 **English**

**Network monitoring for small businesses, built for the owner, not for an IT team.**

The Windows server the business already has becomes the watchtower of the network: router,
internet, Wi-Fi, cameras, workstations and the VPS that runs the point-of-sale system. Everything
shows up on a web dashboard, and NetMon **pings you on Telegram, in plain language, only when it
matters**.

> Status: **v0.3, alpha.** Born in, and running for months at, a real business (~30 devices, one
> Windows server, a Cisco router, 3 access points, DVRs, 10 workstations, a sales system on a VPS). It
> works, but it was shaped by that environment. Adapt it to yours and tell us what broke.
>
> The dashboard and the alert messages are in **Brazilian Portuguese** for now. Translating them is on
> the roadmap.

**MIT** license: use it, adapt it, sell services on top of it. Just keep the copyright notice.

## What it looks like

![Bandwidth, LAN saturation, router CPU/memory and radio signal history](imagens/historico.png)

![Link map: internet, router, access points, DVRs and radios, with live bandwidth](imagens/mapa.png)

<p>
  <img src="imagens/fluxo-internet.png" alt="Internet flow down to the local network, with live bandwidth" width="49%">
  <img src="imagens/roteador-e-enlace.png" alt="Router CPU, memory, saturation, DHCP and NAT, plus the radio link" width="49%">
</p>

<sub>Screenshots from the pilot site, with names, addresses and location removed.</sub>

---

## What it watches

| Module | What it does | Files |
|---|---|---|
| **NetMon dashboard** | Web dashboard (desktop and phone, installable as an app): router (CPU, memory, saturation, WAN CRC errors), internet, radio links (SNMP), network map, DVRs, workstations, history. Alert engine and a Telegram bot with commands. | `1-nucleo/netmon.py` + `*.html` |
| **Internal DNS** | Lightweight DNS forwarder on the server plus a query log that exposes machines stuck in DNS/WPAD loops. | `1-nucleo/dns.py`, `dnslog.html` |
| **HTTPS by name** | Caddy in front of the dashboard (`https://noc`). | `1-nucleo/Caddyfile` |
| **Cameras** | DVR video wall via [go2rtc](https://github.com/AlexxIT/go2rtc), substream and a read-only user. | `cameras.html`, `0-modelos/go2rtc.example.yaml` |
| **VPS watcher** | Tests SSH every 15 s over a 5-minute window; alerts when it goes down and when it comes back; does not blame the VPS when it was the office internet that dropped; counts the sales-system tunnels through NAT. | `2-vigias/vps_monitor.py` |
| **Internet probes** | Continuous domestic and international reachability plus WAN CRC errors. Tells "the ISP is down" apart from "our cable or modem". | `sonda_wan2.ps1`, `medir_wan.ps1` |
| **Wi-Fi** | Pings the access points, keeps their remote log (syslog) for 90 days and alerts on **reboots** using SNMP uptime. | `vigia_aps.ps1`, `syslog_rx.py`, `snmp_aps.py` |
| **Windows workstations** | Disk, temperature, firewall, antivirus, power settings and DNS loops on each PC, over WinRM, with credentials in a DPAPI vault. | `3-estacoes/*` |
| **USB-stick kit** | For a PC that is not managed yet: `ABRE-GERENCIA` (local admin + WinRM) and `MATA-ZUMBI` (DNS/WPAD loop fix). | `4-pendrive/*` |
| **Tools** | Wake-on-LAN, Cisco commands, LAN sweep, topology. | `5-ferramentas/*` |

---

## Requirements

- **A Windows server** that stays on 24/7 (Server 2016+ or Windows 10/11 Pro), with ~2 GB of free RAM.
- **Python 3** (the embeddable package in a `python\` folder next to the scripts is enough) and
  `cacert.pem` (Windows Server 2016 cannot do modern TLS on its own).
- **A Telegram bot** (@BotFather) and the `chat_id` of whoever gets the alerts.
- **plink.exe** (PuTTY) if the router is Cisco/SSH.
- Admin access to the workstations (WinRM) or the `ABRE-GERENCIA` USB stick.
- **Read-only SNMP** on radios and access points, with its own community, restricted to the server's IP.

---

## Quick install

**With an AI agent (recommended).** Download the kit to the server, open Claude Code, Codex or another
coding agent in the folder and ask: *"install NetMon on this network"*. [`AGENTS.md`](AGENTS.md) teaches
the agent to discover the network, interview you, build `config.json` and run the installer, with
safety rules: passwords never go through the chat, and nothing on the network changes without your OK.

**With the installer.** Fill in `C:\netmon\config.json` from `0-modelos\config.example.json` and, in a
PowerShell window **run as administrator**, from the kit folder:

```powershell
.\INSTALAR.ps1 -Simular   # dry run: shows what it would do, changes nothing
.\INSTALAR.ps1            # tests Telegram, creates tasks and firewall rules, starts everything
.\INSTALAR.ps1 -Remover   # uninstall (tasks and firewall rules; files stay)
```

Modules switch on by themselves based on what exists: no Cisco router, no DVR or no VPS, and whatever
depends on them is left out.

## Manual install (what the installer does under the hood)

1. **Survey** the network (checklist below).
2. Copy the contents of folders `1-` to `5-` to `C:\netmon\` on the server, together with `python\`,
   `cacert.pem` and `plink.exe`.
3. Copy `0-modelos/config.example.json` to `C:\netmon\config.json` and fill it in. **Everything that is
   specific to the network lives in the config**: business name (`site_name`), IPs (`rede`), router
   (`cisco`), VPS (`vps`), access points (`aps`), Telegram, alert rules.
   Do the same with `dns.example.json`, `go2rtc.example.yaml` and `aps-snmp.example.json`.
4. **USB stick:** in `ABRE-GERENCIA.ps1`, set `-Rede`, `-Relay` and `$SEGREDO_RELAY` (the same value as
   `alerts.entrega_segredo`). It is the only script that runs off the server, so it does not read the config.
5. Create the scheduled tasks as **SYSTEM**, **at startup**, with **no time limit (`PT0S`)** and automatic
   restart: `NetMon`, `NetMon-DNS`, `NetMon-Caddy`, `NetMon-Cameras`, `NetMon-Watchdog`,
   `TI-VigiaVPS`, `TI-SondaWAN2`, `TI-MedirWAN`, `TI-VigiaAPs`, `TI-Syslog`.
6. **Test Telegram before turning on the watchers** (`INSTALAR-VIGIA-VPS.ps1` is the pattern: if the
   test message does not arrive, it does not install).
7. Workstations: USB stick where there is no WinRM, then `importar-credenciais.ps1` (vault) and `estacoes.ps1`.
8. Server firewall: allow only the dashboard, DNS and syslog (UDP 514) from the local network.
9. During the first week, tune the thresholds (CRC, CPU, latency) to what is normal **on that** network.

> **Never** send a password over chat or e-mail to whoever is installing: the owner types it on the server.

---

## Alert policy (anti-noise)

- **A new problem is sent right away**, always. So is the recovery.
- **A problem that persists** is repeated only at fixed times (`repeat_hours`, 12:00 and 18:00 by default).
- **Confirmation:** it only fires after N bad readings in a row (`confirm`, 3 by default).
- **Mute** (`mute`) keeps it out of Telegram but still in the history.
- **Daily routines** only speak up when something goes wrong.

At the pilot site this took us from **137 to 23 messages a day**.

---

## Site survey checklist

- [ ] IP range, gateway, who serves DHCP, reservations, DNS
- [ ] Router/firewall: model, SSH/SNMP access, logging and NTP enabled
- [ ] Internet: ISP, modem and the modem's IP
- [ ] Wi-Fi: access points, controller (or not), SSIDs and who uses which
- [ ] Radio links between buildings
- [ ] Servers, VPS and the system that stops sales if it goes down
- [ ] Workstations: name, user, Windows Home or Pro, laptop or desktop
- [ ] DVRs, printers, POS terminals, scales
- [ ] Power: what is on a UPS and what shares an outlet
- [ ] Who gets the alerts, and when

---

## Lessons from the pilot (each one cost us)

1. **Windows `ping` lies in its exit code:** "Destination host unreachable" exits as success. Only `TTL=` in the reply counts.
2. **PowerShell is case-insensitive:** `$INTL` and `$intl` are the same variable; `-replace` ignores case too.
3. **A scheduled task dies after 72 h** unless its execution time limit is `PT0S`.
4. **A JSON file with a BOM breaks Python.** Write UTF-8 without a BOM; read with `utf-8-sig`.
5. **Server 2016 cannot speak modern TLS from PowerShell:** Telegram and friends go through Python + `cacert.pem`.
6. **Never disable the `WinHttpAutoProxySvc` service:** Windows Wi-Fi depends on it. To kill WPAD, the `DisableWpad=1` policy is enough.
7. **A swallowed error hides the problem:** every watcher logs its failures and exits with an error.
8. **Ping and ARP do not prove a PC is off** (firewalls block ICMP; ARP goes stale). The honest test is opening a WinRM session.
9. **A task running as SYSTEM cannot see the user's PuTTY host key:** copy it to the SYSTEM profile.
10. **An API that answers "success" has not proven it saved anything** (Omada controller). Read it back after writing.
11. **EAP225 access points that froze when power came back** became stable after a factory reset, out of the controller and with a static IP. In standalone mode: SSIDs are per band (create them twice), there is no "minimum RSSI", and the AP's name goes in the SNMP SysName.
12. **The access points' syslog sends every client's browsing** every 5 s (~150 MB/day, and it is personal data): the receiver drops it and only counts it.
13. **An automatic printer queue (WSD/IPP) on a laptop** gets tied to the network adapter it was created on (`fe80::…%N`): switch from cable to Wi-Fi and it stops printing.
14. **A status report that hides repeats hides open problems:** always list what is "open right now".
15. **Do not touch the network settings of the machine the owner is using** (extra IP, network service) without asking.
16. **Devices that share an outlet go down together** (AP + DVR): one alert helps diagnose the other.
17. **After the VPS reboots, the sales system may not reconnect by itself:** the recovery alert already says "close it and open it again".

---

## Security

- **No secrets in the repository.** `config.json`, `dns.json`, `aps-snmp.json`, `go2rtc.yaml`, vaults,
  databases and logs are in `.gitignore`. Only the `*.example.*` files are committed.
- Turn on **Secret scanning** and **Push protection** in your fork's settings.
- Workstation credentials live in a DPAPI vault (`importar-credenciais.ps1`): only the account and the
  machine that wrote them can read them.
- The dashboard listens on the LAN. Do not expose port 8787 to the internet.

---

## Roadmap

- [x] Everything site-specific lives in `config.json` (v0.2)
- [x] Single `INSTALAR.ps1`: folders, tasks, firewall and Telegram test (v0.3)
- [x] AI-guided install: `AGENTS.md` (v0.3)
- [x] Documentation in English
- [ ] Dashboard and alert messages in English (i18n)
- [ ] First-run wizard: sweeps the LAN and suggests names by vendor
- [ ] On/off modules (no Cisco, no DVR, no controller...)
- [ ] Other routers: MikroTik, pfSense, ISP router via SNMP
- [ ] A dashboard that gathers several sites, and remote updates

## Contributing

Issues and pull requests are welcome, especially: support for other routers, other AP/DVR brands and
install reports ("I installed it in this setup and it broke here"). Before opening a PR, make sure no
IP, name or password from your network goes with it.

## License

[MIT](LICENSE).
