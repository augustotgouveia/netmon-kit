# ============================================================================
#  ABRE-GERENCIA.ps1
#
#  Deixa a maquina gerenciavel REMOTAMENTE, sem precisar voltar nela:
#    1. cria um usuario administrador local, com senha UNICA por maquina
#    2. liga o WinRM (PowerShell remoto)
#    3. libera WMI/CIM, log de eventos, servicos e tarefas agendadas remotos
#    4. libera SMB (C$) e ping
#    5. ajusta o LocalAccountTokenFilterPolicy - sem ele nada disso funciona
#       em rede de GRUPO DE TRABALHO (explicado la embaixo)
#    6. opcionalmente liga a Area de Trabalho Remota (-ComRDP)
#
#  TUDO e liberado SOMENTE para a rede local (-Rede; AJUSTE o padrao abaixo). Nada fica exposto
#  para fora e nenhuma regra fica com origem "qualquer".
#
#  A SENHA e gerada aqui e e DIFERENTE em cada maquina. Senha igual em todas
#  seria uma chave mestra: quem comprometer UMA entraria em TODAS.
#
#  A senha e guardada em DOIS lugares, de proposito:
#    - no seu Telegram, mandada pelo servidor do NetMon (o token do bot fica la, nao
#      viaja no pendrive - o pendrive so leva um segredo que pede "manda essa
#      mensagem", e nada alem disso)
#    - no SENHAS-GERENCIA.csv, junto com nome, modelo, IP e MAC
#
#  Duas copias porque senha de admin local nao se recupera: mensagem apagada
#  sem querer, ou o fio perdido no meio de uma ronda de 11 maquinas, custaria
#  refazer a ronda toda.
#
#  >>> O PRECO: com o CSV preenchido, este pendrive abre TODAS as maquinas.
#  >>> Guarde como voce guardaria o molho de chaves do galpao.
#
#  ESTE ARQUIVO E SO ASCII de proposito: se for gravado em UTF-8 sem BOM o
#  PowerShell 5.1 leria acento como lixo e os textos sairiam quebrados.
#
#  USO (como ADMINISTRADOR):
#    .\ABRE-GERENCIA.ps1              faz tudo
#    .\ABRE-GERENCIA.ps1 -Simular     so mostra, nao muda nada
#    .\ABRE-GERENCIA.ps1 -ComRDP      inclui Area de Trabalho Remota
#    .\ABRE-GERENCIA.ps1 -Desfazer    remove o usuario e volta as politicas
# ============================================================================
param(
  [string]$Usuario = 'gtiadmin',
  # NAO chamar de $Nome: o PowerShell nao diferencia maiuscula em nome de
  # variavel, e $nome (o nome da maquina, la embaixo) sobrescreveria este
  # parametro em silencio. Foi exatamente isso que quebrou na primeira
  # execucao de campo: o script achou que tinha recebido -Nome PC-EXEMPLO,
  # pulou a pergunta e tentou renomear a maquina para o proprio nome dela.
  [string]$NovoNome = '',
  [string]$Rede    = '192.168.0.0/24',              # AJUSTE para a rede do cliente
  [string]$Relay   = 'http://192.168.0.1:8787/api/entrega',   # AJUSTE: IP do servidor NetMon
  [switch]$ComRDP,
  [switch]$Simular,
  [switch]$Desfazer,
  [switch]$SemTelegram
)

# Segredo do relay. NAO e o token do bot: so serve para pedir ao servidor do NetMon
# que mande UMA mensagem. Se este pendrive se perder, quem achar consegue no
# maximo mandar mensagem no Telegram do dono - nao consegue ler nada, nem
# usar o bot do NOC. Por isso a senha vai por aqui em vez de ficar no CSV.
$SEGREDO_RELAY = '<PREENCHER - o mesmo valor de alerts.entrega_segredo no config.json>'

$ErrorActionPreference = 'Stop'
$AQUI = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $AQUI) { $AQUI = (Get-Location).Path }
$nome = $env:COMPUTERNAME

function Titulo($t){ Write-Host ""; Write-Host ("="*68) -ForegroundColor DarkCyan; Write-Host $t -ForegroundColor Cyan; Write-Host ("="*68) -ForegroundColor DarkCyan }
function Ok($t)   { Write-Host "  [OK]   $t" -ForegroundColor Green }
function Info($t) { Write-Host "  .      $t" -ForegroundColor Gray }
function Aviso($t){ Write-Host "  [!]    $t" -ForegroundColor Yellow }
function Erro($t) { Write-Host "  [X]    $t" -ForegroundColor Red }
function Pausa($t){ try { Read-Host $t | Out-Null } catch { Write-Host "  ($t)" -ForegroundColor DarkGray } }

# ------------------------------------------------------------ contas locais
# Get-LocalUser, New-LocalUser e Add-LocalGroupMember vem do modulo
# Microsoft.PowerShell.LocalAccounts, que so existe do WINDOWS 10 em diante.
# No Windows 8.1 (PowerShell 4.0) eles simplesmente nao existem - foi assim
# que este script morreu numa estacao do piloto.
# Tudo abaixo usa ADSI/WinNT, que funciona do 8.1 ao 11 e no Server 2016.
# Um caminho so de proposito: dois caminhos significa que um deles vive sem
# teste ate o dia em que quebra na frente do cliente.
$PC = [ADSI]"WinNT://$env:COMPUTERNAME,computer"

function Conta-Existe($n) {
  try { [ADSI]::Exists("WinNT://$env:COMPUTERNAME/$n,user") } catch { $false }
}

function Nome-Grupo-Admin {
  # o nome muda com o idioma (Administrators / Administradores); o SID nunca
  $sid = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')
  ($sid.Translate([Security.Principal.NTAccount]).Value -split '\\')[-1]
}

function Membros-Do-Grupo($grupo) {
  $g = [ADSI]"WinNT://$env:COMPUTERNAME/$grupo,group"
  @($g.psbase.Invoke('Members')) | ForEach-Object {
    try { $_.GetType().InvokeMember('Name', 'GetProperty', $null, $_, $null) } catch { }
  }
}

function Criar-Ou-Atualizar-Conta($n, $senhaTexto, $desc) {
  if (Conta-Existe $n) {
    $u = [ADSI]"WinNT://$env:COMPUTERNAME/$n,user"
    $u.SetPassword($senhaTexto)
  } else {
    $u = $PC.Create('User', $n)
    $u.SetPassword($senhaTexto)
    $u.SetInfo()
    $u = [ADSI]"WinNT://$env:COMPUTERNAME/$n,user"
  }
  $u.Put('Description', $desc)
  $u.Put('FullName', 'Gerencia TI')
  $u.SetInfo()
  # UserFlags: 0x10000 = senha nunca expira | 0x0002 = conta desabilitada.
  # Separado e protegido: se isto falhar a conta JA existe e ja funciona -
  # so perderia o "senha nunca expira". Nao vale derrubar o script por isso.
  try {
    $f = [int]$u.UserFlags.Value
    $u.Put('UserFlags', (($f -bor 0x10000) -band (-bnot 0x0002)))
    $u.SetInfo()
  } catch {
    Aviso "nao consegui marcar 'senha nunca expira': $($_.Exception.Message.Split([char]10)[0])"
  }
}

function Conta-Habilitada($n) {
  if (-not (Conta-Existe $n)) { return $null }
  $u = [ADSI]"WinNT://$env:COMPUTERNAME/$n,user"
  -not ([int]$u.UserFlags.Value -band 0x0002)
}

function Remover-Conta($n) { if (Conta-Existe $n) { $PC.Delete('User', $n) } }

function Por-No-Grupo($n, $grupo) {
  ([ADSI]"WinNT://$env:COMPUTERNAME/$grupo,group").Add("WinNT://$env:COMPUTERNAME/$n,user")
}

$ehAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $ehAdmin) {
  Erro "Precisa rodar como ADMINISTRADOR."
  Pausa "  Enter para sair"
  exit 1
}

$undo     = Join-Path $AQUI "DESFAZER-GERENCIA-$nome.json"
$csv      = Join-Path $AQUI "SENHAS-GERENCIA.csv"
$latfpKey = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
$tsKey    = 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server'

# --- o que liberar no firewall ----------------------------------------------
# Cada item aponta para uma regra ANCORA pelo -Name. O -Name das regras
# embutidas do Windows NAO muda com o idioma (o DisplayName muda: em PT-BR
# "Windows Remote Management" vira "Gerenciamento Remoto do Windows").
# Da ancora eu leio a propriedade Group e ligo o grupo inteiro - assim pego
# tambem as portas dinamicas do RPC, que mudam a cada boot e nao daria para
# fixar numa regra escrita a mao.
#
# modo 'grupo'  = liga o grupo inteiro. Necessario onde o servico usa portas
#                 dinamicas de RPC (WMI, servicos, log de eventos, tarefas).
# modo 'regras' = liga SO as regras listadas. Usado no Compartilhamento de
#                 Arquivo e Impressora: o grupo dele tem 29 regras de entrada
#                 e acordaria NetBIOS, datagrama e spooler junto. Eu preciso
#                 de duas: porta 445 e o ping. Porta fixa, nao precisa do RPC.
$ALVOS = @(
  @{ ancora = 'WINRM-HTTP-In-TCP';        oque = 'WinRM (PowerShell remoto)';      modo = 'grupo' },
  @{ ancora = 'WMI-WINMGMT-In-TCP';       oque = 'WMI/CIM remoto';                 modo = 'grupo' },
  @{ ancora = 'RemoteEventLogSvc-In-TCP'; oque = 'ler log de eventos de longe';    modo = 'grupo' },
  @{ ancora = 'RemoteSvcAdmin-In-TCP';    oque = 'parar/iniciar servicos';         modo = 'grupo' },
  @{ ancora = 'RemoteTask-In-TCP';        oque = 'agendar tarefa remota';          modo = 'grupo' },
  @{ ancora = 'FPS-SMB-In-TCP';           oque = 'SMB / compartilhamento C$';      modo = 'regras'; regras = @('FPS-SMB-In-TCP') },
  @{ ancora = 'FPS-ICMP4-ERQ-In';         oque = 'ping (ICMPv4 e ICMPv6)';         modo = 'regras'; regras = @('FPS-ICMP4-ERQ-In','FPS-ICMP6-ERQ-In') }
)

# ---------------------------------------------------------------- DESFAZER
if ($Desfazer) {
  Titulo "DESFAZENDO EM $nome"

  # Se a maquina foi renomeada e ainda NAO reiniciou (ou foi renomeada e
  # reiniciou, e o arquivo esta no nome antigo), procurar pelo nome nao acha.
  # O MAC nao muda nunca - e por ele que eu acho o arquivo certo.
  if (-not (Test-Path $undo)) {
    $meusMacs = @(Get-NetAdapter -Physical -ErrorAction SilentlyContinue |
                  Where-Object Status -eq 'Up' | ForEach-Object { $_.MacAddress })
    foreach ($f in @(Get-ChildItem (Join-Path $AQUI 'DESFAZER-GERENCIA-*.json') -ErrorAction SilentlyContinue)) {
      try { $t = Get-Content $f.FullName -Raw | ConvertFrom-Json } catch { continue }
      if ($t.mac -and ($meusMacs -contains $t.mac)) {
        $undo = $f.FullName
        Info "achei pelo MAC $($t.mac): $($f.Name)"
        if ($t.nomeAntes -and $t.nomeAntes -ne $nome) { Info "(esta maquina se chamava $($t.nomeAntes))" }
        break
      }
    }
  }

  if (Test-Path $undo) {
    $u = Get-Content $undo -Raw | ConvertFrom-Json

    if ($u.renomeou -and $u.nomeAntes) {
      try {
        Rename-Computer -NewName $u.nomeAntes -Force -ErrorAction Stop
        Ok "nome devolvido para '$($u.nomeAntes)' (vale no proximo boot)"
      } catch { Aviso ("nao consegui devolver o nome: " + $_.Exception.Message.Split([char]10)[0]) }
    }

    if ($u.criouUsuario -and (Conta-Existe $u.usuario)) {
      Remover-Conta $u.usuario
      Ok "usuario '$($u.usuario)' removido"
    } elseif (Conta-Existe $u.usuario) {
      Aviso "usuario '$($u.usuario)' ja existia antes deste script - NAO removido"
    }

    if ($null -ne $u.latfpAntes) {
      Set-ItemProperty $latfpKey -Name LocalAccountTokenFilterPolicy -Value ([int]$u.latfpAntes)
      Ok "LocalAccountTokenFilterPolicy voltou para $($u.latfpAntes)"
    } else {
      Remove-ItemProperty $latfpKey -Name LocalAccountTokenFilterPolicy -ErrorAction SilentlyContinue
      Ok "LocalAccountTokenFilterPolicy removido (volta ao padrao do Windows)"
    }

    if ($null -ne $u.rdpAntes) {
      Set-ItemProperty $tsKey -Name fDenyTSConnections -Value ([int]$u.rdpAntes)
      Ok "RDP voltou ao estado anterior (fDenyTSConnections=$($u.rdpAntes))"
    }

    # So desliga regra que ESTE script ligou, e so se ela estava DESLIGADA
    # antes. Regra que ja vinha ligada de fabrica fica como estava - desligar
    # agora quebraria algo que nao era meu.
    $desligadas = 0; $mantidas = 0
    foreach ($rn in @($u.regrasLigadas)) {
      if (-not $rn) { continue }
      if (-not ($u.regrasAntes -and $u.regrasAntes.PSObject.Properties[$rn])) { continue }
      $antes = $u.regrasAntes.$rn

      # devolve a origem exatamente como estava (LocalSubnet, Any, o que fosse)
      if ($antes.origem) {
        Set-NetFirewallRule -Name $rn -RemoteAddress @($antes.origem) -ErrorAction SilentlyContinue
      }
      if ($antes.ligada -eq 'False') {
        Disable-NetFirewallRule -Name $rn -ErrorAction SilentlyContinue
        $desligadas++
      } else {
        $mantidas++
      }
    }
    Ok "firewall: $desligadas regra(s) desligadas, $mantidas mantidas (ja vinham ligadas de fabrica)"

    Aviso "O servico WinRM continua ligado. Para fechar de vez: Disable-PSRemoting -Force"
  } else {
    Erro "nao achei $undo"
  }
  Pausa "  Enter"
  exit 0
}

# ---------------------------------------------------------------- estado antes
Titulo "MAQUINA"
$cs = Get-CimInstance Win32_ComputerSystem
$os = Get-CimInstance Win32_OperatingSystem
Info "nome     : $nome"
Info "modelo   : $($cs.Manufacturer) / $($cs.Model)"
Info "windows  : $($os.Caption) build $($os.BuildNumber)"
Info "powershell: $($PSVersionTable.PSVersion)"
# PowerShell 2 (padrao do Windows 7) nao tem metade do que este script usa.
# Melhor avisar de cara do que morrer no meio com erro obscuro.
if ($PSVersionTable.PSVersion.Major -lt 3) {
  Erro "PowerShell $($PSVersionTable.PSVersion) e antigo demais para este script (precisa 3 ou mais)."
  Aviso "instale o Windows Management Framework 4.0 nesta maquina, ou faca o passo 1 na mao."
  Pausa "  Enter para sair"
  exit 1
}
if ($cs.PartOfDomain) { Info "dominio  : $($cs.Domain)" }
else                  { Info "dominio  : GRUPO DE TRABALHO ($($cs.Workgroup))" }

$nics = Get-NetIPConfiguration | Where-Object { $_.IPv4Address -and $_.NetAdapter.Status -eq 'Up' }
foreach ($r in $nics) {
  $perfil = (Get-NetConnectionProfile -InterfaceIndex $r.InterfaceIndex -ErrorAction SilentlyContinue).NetworkCategory
  Info ("rede     : {0} = {1}  MAC {2}  perfil={3}" -f $r.InterfaceAlias, $r.IPv4Address.IPAddress, $r.NetAdapter.MacAddress, $perfil)
}

# Escolher a placa CERTA, nao a primeira. Maquina com Hyper-V, VPN ou com o
# modulo de banco (Warsaw/Topaz) tem adaptador virtual que aparece ANTES da
# placa de verdade - e ai o inventario sairia com IP e MAC de mentira.
# (Num notebook do piloto eram 5 adaptadores e o verdadeiro era o ultimo.)
$prefixo = ($Rede -split '/')[0] -replace '\d+$', ''      # 192.168.0.0/24 -> 192.168.0.
$nic1 = $nics | Where-Object { $_.IPv4Address.IPAddress -like "$prefixo*" } | Select-Object -First 1
if (-not $nic1) {
  $nic1 = $nics | Select-Object -First 1
  Aviso "nenhuma placa em $prefixo* - usando '$($nic1.InterfaceAlias)', confira o inventario"
}
$ip1  = [string]$nic1.IPv4Address.IPAddress
$mac1 = [string]$nic1.NetAdapter.MacAddress
Info "placa    : $($nic1.InterfaceAlias) = $ip1  MAC $mac1   <- usada no inventario"

$latfpAntes = (Get-ItemProperty $latfpKey -Name LocalAccountTokenFilterPolicy -ErrorAction SilentlyContinue).LocalAccountTokenFilterPolicy
$rdpAntes   = (Get-ItemProperty $tsKey    -Name fDenyTSConnections           -ErrorAction SilentlyContinue).fDenyTSConnections
$winrmAntes = (Get-Service WinRM -ErrorAction SilentlyContinue).Status
$jaExiste   = [bool](Conta-Existe $Usuario)

# Guardo o estado ANTES de cada regra que eu mexer, para o -Desfazer devolver
# exatamente como estava. Duas armadilhas que isso evita:
#  - regra que ja vinha LIGADA de fabrica nao pode ser desligada no rollback
#  - as regras de Compartilhamento ja vem com origem LocalSubnet, nao "Any".
#    Se o rollback jogasse "Any" nelas, eu devolveria a maquina MAIS ABERTA
#    do que encontrei - o oposto do que um rollback deve fazer.
$regrasLigadas = @()
$regrasAntes   = @{}

function Guardar-Estado($r) {
  if ($regrasAntes.ContainsKey($r.Name)) { return }
  $end = ($r | Get-NetFirewallAddressFilter -ErrorAction SilentlyContinue).RemoteAddress
  $regrasAntes[$r.Name] = @{ ligada = "$($r.Enabled)"; origem = @($end) }
}

# ---------------------------------------------------------------- 0. nome
# Padronizar o nome agora, com a maquina na frente, e o momento mais barato.
# Hoje os nomes nao dizem nada (DESKTOP-POT6KN1) e a gente casa maquina por
# MAC. Nome bom resolve isso de vez.
#
# A troca NAO reinicia na hora: vale no proximo boot. Assim da para fazer a
# ronda inteira e reiniciar tudo no fim do expediente.
function Nome-Valido($n) {
  if ([string]::IsNullOrWhiteSpace($n))      { return "vazio" }
  if ($n.Length -gt 15)                      { return "tem $($n.Length) caracteres; o limite do NetBIOS e 15" }
  if ($n -notmatch '^[A-Za-z0-9-]+$')        { return "so pode letra, numero e hifen (sem espaco e sem acento)" }
  if ($n -match '^-|-$')                     { return "nao pode comecar nem terminar com hifen" }
  if ($n -notmatch '[A-Za-z]')               { return "nao pode ser so numero" }
  return $null
}

$nomeNovo = ''
if (-not $Desfazer) {
  Titulo "NOME DA MAQUINA"
  Info "agora : $nome"
  Info "padrao: PDV-01 (caixa)   ADM-01 (escritorio)   DEP-01 (deposito)"
  Write-Host ""

  if ($NovoNome) {
    $erro = Nome-Valido $NovoNome
    if ($erro) { Erro "nome '$NovoNome' invalido: $erro"; Pausa "  Enter para sair"; exit 1 }
    if ($NovoNome -ieq $nome) { Info "'$NovoNome' ja e o nome atual, nao ha o que renomear" }
    else { $nomeNovo = $NovoNome }
  } else {
    while ($true) {
      $r = ''
      try { $r = (Read-Host "  Novo nome (Enter para NAO renomear)").Trim() }
      catch { Info "sem console - nao vou renomear"; break }
      if ($r -eq '') { Info "sem renomear, continua como $nome"; break }
      $erro = Nome-Valido $r
      if ($erro) { Erro "$erro"; continue }
      if ($r -ieq $nome) { Info "esse ja e o nome atual, nada a fazer"; break }
      # nome repetido na rede da um conflito chato de achar depois
      $conflito = $null
      try { $conflito = [Net.Dns]::GetHostEntry($r) } catch { }
      if ($conflito) {
        Aviso "ja existe algo na rede respondendo por '$r' ($($conflito.AddressList[0]))"
        if ((Read-Host "  Usar assim mesmo? (s/N)") -notmatch '^[sS]') { continue }
      }
      $nomeNovo = $r
      break
    }
  }
}

if ($Simular) { Titulo "SIMULACAO - nada sera alterado" } else { Titulo "APLICANDO" }

if ($nomeNovo) {
  if ($Simular) {
    Info "faria: renomear '$nome' para '$nomeNovo' (vale no proximo boot)"
  } else {
    try {
      Rename-Computer -NewName $nomeNovo -Force -ErrorAction Stop
      Ok "renomeada: $nome  ->  $nomeNovo"
      Aviso "o nome novo so vale DEPOIS de reiniciar"
    } catch {
      Erro ("nao consegui renomear: " + $_.Exception.Message.Split([char]10)[0])
      $nomeNovo = ''
    }
  }
}
# a partir daqui, este e o nome com que a maquina vai ser conhecida
$nomeFinal = $(if ($nomeNovo) { $nomeNovo } else { $nome })

# ---------------------------------------------------------------- 1. usuario
# Alfabeto sem caracteres ambiguos (zero/O, um/l/I), porque uma hora voce vai
# ter que digitar isso no teclado de alguem.
$abc   = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789#%@+=?'
$rng   = [Security.Cryptography.RandomNumberGenerator]::Create()
$bytes = New-Object byte[] 24
$rng.GetBytes($bytes)
$senha = -join ($bytes | ForEach-Object { $abc[$_ % $abc.Length] })

# o NOME do grupo Administradores muda com o idioma do Windows; o SID nao muda
$grpAdmin = Nome-Grupo-Admin

if ($Simular) {
  Info "faria: criar/atualizar o usuario local '$Usuario' e por no grupo '$grpAdmin'"
} else {
  $desc = "Gerencia TI - criada $(Get-Date -Format 'dd/MM/yy')"
  Criar-Ou-Atualizar-Conta $Usuario $senha $desc
  if ($jaExiste) { Ok "usuario '$Usuario' ja existia - senha redefinida" }
  else           { Ok "usuario '$Usuario' criado" }

  if (@(Membros-Do-Grupo $grpAdmin) -notcontains $Usuario) { Por-No-Grupo $Usuario $grpAdmin }
  Ok "membro do grupo '$grpAdmin'"
}

# ---------------------------------------------------------------- 2. LATFP
# ESTE E O PULO DO GATO em rede de grupo de trabalho.
# Sem ele, o UAC remoto TIRA os poderes de administrador de qualquer conta
# LOCAL que entra pela rede. O login funciona, mas tudo da "acesso negado" -
# e o sintoma engana: parece senha errada ou firewall fechado.
if ($Simular) {
  if ($null -eq $latfpAntes) { Info "faria: LocalAccountTokenFilterPolicy = 1 (hoje: nao definido)" }
  else                       { Info "faria: LocalAccountTokenFilterPolicy = 1 (hoje: $latfpAntes)" }
} else {
  if (-not (Test-Path $latfpKey)) { New-Item -Path $latfpKey -Force | Out-Null }
  New-ItemProperty -Path $latfpKey -Name LocalAccountTokenFilterPolicy -Value 1 -PropertyType DWord -Force | Out-Null
  Ok "LocalAccountTokenFilterPolicy = 1 (conta local mantem poder de admin pela rede)"
  Aviso "isso reduz uma protecao: credencial local roubada passa a servir remotamente"
}

# ---------------------------------------------------------------- 3. WinRM
if ($Simular) {
  Info "faria: Enable-PSRemoting (servico WinRM hoje: $winrmAntes)"
} else {
  try {
    # -SkipNetworkProfileCheck: sem isso o comando se recusa a rodar quando o
    # perfil da rede esta como Publica. As regras ficam restritas a LAN logo
    # abaixo, entao ligar no perfil publico nao abre nada para fora.
    Enable-PSRemoting -Force -SkipNetworkProfileCheck -ErrorAction Stop | Out-Null
    Ok "WinRM habilitado"
  } catch {
    Aviso ("Enable-PSRemoting reclamou: " + $_.Exception.Message.Split([char]10)[0])
  }
  Set-Service WinRM -StartupType Automatic
  Start-Service WinRM -ErrorAction SilentlyContinue
  $w = Get-Service WinRM
  Ok "servico WinRM: $($w.Status) / inicio $($w.StartType)"
}

# ---------------------------------------------------------------- 4. firewall
Titulo "FIREWALL (tudo restrito a $Rede)"
foreach ($a in $ALVOS) {
  $regra = Get-NetFirewallRule -Name $a.ancora -ErrorAction SilentlyContinue
  if (-not $regra) {
    Aviso "regra ancora nao existe neste Windows: $($a.ancora)  [$($a.oque)]"
    continue
  }
  $grupo = $regra.Group
  if (-not $grupo) { $grupo = $a.ancora }

  # monta a lista exata de regras que vou mexer, conforme o modo
  if ($a.modo -eq 'regras') {
    $alvo = @()
    foreach ($rn in $a.regras) {
      $x = Get-NetFirewallRule -Name $rn -ErrorAction SilentlyContinue
      if ($x) { $alvo += $x } else { Info "  (regra opcional ausente: $rn)" }
    }
  } else {
    $alvo = @(Get-NetFirewallRule -Group $grupo -Direction Inbound -ErrorAction SilentlyContinue)
  }
  if (-not $alvo -or $alvo.Count -eq 0) { Aviso "$($a.oque): nenhuma regra encontrada"; continue }

  if ($Simular) {
    Info "faria: ligar $($alvo.Count) regra(s) restritas a $Rede  -> $($a.oque)  [$($a.modo)]"
    continue
  }
  try {
    foreach ($x in $alvo) {
      Guardar-Estado $x
      Enable-NetFirewallRule -Name $x.Name -ErrorAction Stop
      # SEM a linha abaixo a regra fica valendo para QUALQUER origem
      Set-NetFirewallRule -Name $x.Name -RemoteAddress $Rede -ErrorAction Stop
      $regrasLigadas += $x.Name
    }
    Ok ("{0}: {1} regra(s) ligadas, origem limitada a {2}" -f $a.oque, $alvo.Count, $Rede)
  } catch {
    Erro ("$($a.oque): " + $_.Exception.Message.Split([char]10)[0])
  }
}

# ---------------------------------------------------------------- 5. RDP
if ($ComRDP) {
  if ($Simular) {
    Info "faria: ligar Area de Trabalho Remota com NLA (hoje fDenyTSConnections=$rdpAntes)"
  } else {
    Set-ItemProperty $tsKey -Name fDenyTSConnections -Value 0
    # NLA = autentica ANTES de desenhar a tela. Nao deixar desligado.
    Set-ItemProperty "$tsKey\WinStations\RDP-Tcp" -Name UserAuthentication -Value 1 -ErrorAction SilentlyContinue
    $rdpRegra = Get-NetFirewallRule -Name 'RemoteDesktop-UserMode-In-TCP' -ErrorAction SilentlyContinue
    if ($rdpRegra) {
      foreach ($x in @(Get-NetFirewallRule -Group $rdpRegra.Group -Direction Inbound -ErrorAction SilentlyContinue)) {
        Guardar-Estado $x
        Enable-NetFirewallRule -Name $x.Name -ErrorAction SilentlyContinue
        Set-NetFirewallRule    -Name $x.Name -RemoteAddress $Rede -ErrorAction SilentlyContinue
        $regrasLigadas += $x.Name
      }
      Ok "Area de Trabalho Remota ligada (com NLA), restrita a $Rede"
    } else {
      Aviso "regra de RDP nao encontrada neste Windows"
    }
  }
} else {
  Info "RDP: nao mexido (use -ComRDP se quiser)"
}

# ---------------------------------------------------------------- 6. conferir
if (-not $Simular) {
  Titulo "CONFERINDO"
  $hab = Conta-Habilitada $Usuario
  if ($null -ne $hab) { Info "usuario habilitado  : $hab" } else { Info "usuario habilitado  : NAO EXISTE" }
  Info ("e administrador     : " + [bool](@(Membros-Do-Grupo $grpAdmin) -contains $Usuario))
  Info ("WinRM escutando 5985: " + [bool](Get-NetTCPConnection -State Listen -LocalPort 5985 -ErrorAction SilentlyContinue))
  Info ("LATFP               : " + (Get-ItemProperty $latfpKey -Name LocalAccountTokenFilterPolicy -ErrorAction SilentlyContinue).LocalAccountTokenFilterPolicy)

  $estado = @{
    maquina       = $nomeFinal
    nomeAntes     = $nome
    renomeou      = [bool]$nomeNovo
    mac           = $mac1          # o que NAO muda quando a maquina e renomeada
    quando        = (Get-Date).ToString('o')
    usuario       = $Usuario
    criouUsuario  = (-not $jaExiste)
    latfpAntes    = $latfpAntes
    rdpAntes      = $rdpAntes
    winrmAntes    = "$winrmAntes"
    regrasLigadas = $regrasLigadas
    regrasAntes   = $regrasAntes
  }
  # grava pelo nome FINAL: depois do boot a maquina se chama assim, e e por
  # esse nome que o -Desfazer vai procurar
  $undo = Join-Path $AQUI "DESFAZER-GERENCIA-$nomeFinal.json"
  $estado | ConvertTo-Json -Depth 4 | Set-Content $undo -Encoding UTF8
  Ok "rollback gravado em DESFAZER-GERENCIA-$nomeFinal.json"

  # ------------------------------------------------------ entrega da senha
  # A senha vai pelo Telegram, atraves do servidor do NetMon - assim ela NAO fica
  # gravada no pendrive. O pendrive leva so o inventario (nome, modelo, IP,
  # MAC), que nao abre porta nenhuma.
  Titulo "ENTREGA DA SENHA"
  $entregue = $false
  $porque   = 'nao tentei (-SemTelegram)'

  if (-not $SemTelegram) {
    try {
      # os casts [string] sao de proposito: Get-Content e outras fontes
      # devolvem string com propriedades do provider penduradas, e o
      # ConvertTo-Json serializa um OBJETO no lugar do texto. Ja me pegou.
      $corpo = @{
        segredo = [string]$SEGREDO_RELAY
        maquina = [string]$nomeFinal
        modelo  = [string]$(if ($nomeNovo) { "$($cs.Manufacturer) $($cs.Model) - antes se chamava $nome" }
                            else           { "$($cs.Manufacturer) $($cs.Model)" })
        ip      = $ip1
        mac     = $mac1
        usuario = [string]$Usuario
        senha   = [string]$senha
      } | ConvertTo-Json -Compress
      $resp = Invoke-RestMethod -Uri $Relay -Method Post -Body $corpo -ContentType 'application/json' -TimeoutSec 25
      if ($resp.ok) { $entregue = $true; Ok "senha enviada para o seu Telegram (pelo servidor do NetMon)" }
      else          { $porque = "servidor recusou: $($resp.err)" }
    } catch {
      $porque = $_.Exception.Message.Split([char]10)[0]
    }
  }

  # O CSV grava a senha SEMPRE, mesmo quando o Telegram deu certo. Sao duas
  # copias de proposito: mensagem apagada sem querer, ou o fio perdido no meio
  # de uma ronda de 11 maquinas, significaria refazer a ronda inteira - a senha
  # de admin local nao tem como ser recuperada depois.
  # O preco e que o pendrive passa a valer o mesmo que o molho de chaves.
  $linha = [PSCustomObject]@{
    maquina  = $nomeFinal
    nome_antigo = $(if ($nomeNovo) { $nome } else { '' })
    modelo   = "$($cs.Manufacturer) $($cs.Model)"
    ip       = $ip1
    mac      = $mac1
    usuario  = $Usuario
    senha    = $senha
    entregue = $(if ($entregue) { 'sim' } else { 'NAO' })
    quando   = (Get-Date -Format 'dd/MM/yyyy HH:mm')
  }
  if (Test-Path $csv) { $linha | Export-Csv $csv -Append -NoTypeInformation -Encoding UTF8 }
  else                { $linha | Export-Csv $csv         -NoTypeInformation -Encoding UTF8 }
  Ok "senha gravada tambem em SENHAS-GERENCIA.csv"

  if (-not $entregue) {
    Aviso "NAO consegui entregar pelo Telegram: $porque"
    Aviso "A senha existe so na tela e no CSV. Confira antes de fechar."
  }
}

Titulo "PRONTO"
if (-not $Simular) {
  Write-Host ""
  if ($nomeNovo) {
    Write-Host "  Maquina : $nomeNovo" -ForegroundColor White -NoNewline
    Write-Host "   (era $nome - vale apos reiniciar)" -ForegroundColor DarkGray
  } else {
    Write-Host "  Maquina : $nome" -ForegroundColor White
  }
  Write-Host "  Usuario : $Usuario" -ForegroundColor White
  Write-Host "  Senha   : $senha"   -ForegroundColor Yellow
  Write-Host ""
  if ($entregue) {
    Ok "Guardada em dois lugares: seu Telegram e o CSV do pendrive."
  } else {
    Aviso "TELEGRAM FALHOU. Esta senha so existe nesta tela e no CSV."
  }
  Write-Host ""
  Info "Teste a partir do servidor do NetMon ou do seu PC:"
  if ($nomeNovo) {
    # O dominio da credencial e o NOME DA MAQUINA, e ele muda no reboot.
    # Antes de reiniciar a conta e $nome\$Usuario; depois, $nomeNovo\$Usuario.
    Write-Host "    ANTES de reiniciar:  Enter-PSSession -ComputerName $ip1 -Credential $nome\$Usuario" -ForegroundColor DarkGray
    Write-Host "    DEPOIS de reiniciar: Enter-PSSession -ComputerName $ip1 -Credential $nomeNovo\$Usuario" -ForegroundColor DarkGray
  } else {
    Write-Host "    Enter-PSSession -ComputerName $ip1 -Credential $nome\$Usuario" -ForegroundColor DarkGray
  }
  Info "Se der erro de TrustedHosts, rode NA MAQUINA DE ONDE VOCE CHAMA:"
  Write-Host "    Set-Item WSMan:\localhost\Client\TrustedHosts -Value '$($prefixo)*' -Concatenate -Force" -ForegroundColor DarkGray
}
Write-Host ""
Pausa "  Enter para fechar"