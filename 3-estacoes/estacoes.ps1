# ============================================================================
#  estacoes.ps1 - coletor de saude das estacoes, para o NetMon
#
#  Le as credenciais cifradas de C:\netmon\creds\*.xml, conversa com cada
#  estacao por WinRM e cospe UM JSON no stdout. Quem chama e o netmon.py.
#
#  Por que WinRM e nao SNMP: o SNMP do Windows e v1/v2c (community em texto
#  claro) e virou Feature on Demand no Win10/11. O WinRM ja esta ligado nas
#  estacoes, e autenticado e entrega muito mais.
#
#  O servidor tem pouca RAM livre (~0,7 GB). Por isso: UM processo por ciclo,
#  sessoes abertas em paralelo e fechadas no fim. Nada residente.
#
#  CADA BLOCO TEM SEU PROPRIO try. Uma entrada podre de registro derrubou o
#  MATA-ZUMBI inteiro uma vez; aqui um bloco que falha perde so o proprio
#  campo.
#
#  Sozinho, para depurar:  powershell -File estacoes.ps1 -Bonito
# ============================================================================
param(
  [string]$Creds   = 'C:\netmon\creds',
  [int]$TimeoutMs  = 6000,
  [switch]$Bonito
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'
# a saida deste script E o JSON: aviso solto quebra o json.loads do NetMon
$WarningPreference    = 'SilentlyContinue'
# o PS 5.1 escreve o stdout redirecionado na codepage OEM; sem isto o
# json.loads do Python recebe acento quebrado nas mensagens de erro
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) } catch { }

# ------------------------------------------------ AUTOCONSERTO DE IP
# As estacoes sao clientes DHCP: o IP gravado no cofre envelhece e a maquina
# vira "fora do ar" sem estar. Aconteceu no piloto com um notebook (.157 -> .118)
# e com outra estacao (.152 -> .200). O MAC nao muda, entao antes de coletar
# eu reconcilio o cofre com a vizinhanca ARP. So consulta local, sem tocar no
# Cisco - o ARP do servidor ja basta para quem esta ativo na rede.
$arquivos = @(Get-ChildItem (Join-Path $Creds '*.xml') -ErrorAction SilentlyContinue)
try {
  $porMac = @{}
  foreach ($n in @(Get-NetNeighbor -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                   Where-Object { $_.State -in 'Reachable','Stale','Delay','Probe' -and
                                  $_.LinkLayerAddress -notmatch '^(00-00-00|FF-FF-FF)' })) {
    $porMac[$n.LinkLayerAddress.ToUpper()] = $n.IPAddress
  }
  foreach ($fx in $arquivos) {
    $rx = Import-Clixml $fx.FullName
    $mx = ([string]$rx.mac).ToUpper()
    if (-not $mx) { continue }
    $ipNovo = $porMac[$mx]
    if ($ipNovo -and $ipNovo -ne $rx.ip) {
      [PSCustomObject]@{
        maquina = $rx.maquina; ip = $ipNovo; mac = $rx.mac; cred = $rx.cred
        quando = $rx.quando; ip_anterior = $rx.ip; ip_mudou_em = (Get-Date).ToString('o')
      } | Export-Clixml $fx.FullName
    }
  }
} catch { }
if ($arquivos.Count -eq 0) {
  '{"ok":true,"quando":"' + (Get-Date).ToString('o') + '","total":0,"no_ar":0,"estacoes":[]}'
  exit 0
}

# ---------------------------------------------------------------- 1. sessoes
# Cada estacao tem SENHA PROPRIA, entao nao da para um Invoke-Command unico
# com -ComputerName lista. Abro uma sessao por maquina e depois disparo o
# mesmo bloco em TODAS de uma vez, o que o PowerShell paraleliza sozinho.
$opt = New-PSSessionOption -OpenTimeout $TimeoutMs -OperationTimeout ($TimeoutMs * 10) -CancelTimeout 2000
$sessoes = @()
$mortas  = @()

foreach ($f in $arquivos) {
  $alvo = $null
  try { $alvo = Import-Clixml $f.FullName } catch {
    $mortas += [PSCustomObject]@{ maquina = $f.BaseName; ok = $false; erro = 'credencial ilegivel' }
    continue
  }
  $ip = [string]$alvo.ip; $nome = [string]$alvo.maquina; $cred = $alvo.cred
  if (-not $ip -or -not $cred) {
    $mortas += [PSCustomObject]@{ maquina = $f.BaseName; ok = $false; erro = 'registro incompleto' }
    continue
  }
  try {
    $s = New-PSSession -ComputerName $ip -Credential $cred -SessionOption $opt -ErrorAction Stop
    $s | Add-Member -NotePropertyName _maquina -NotePropertyValue $nome -Force
    $sessoes += $s
  } catch {
    $mortas += [PSCustomObject]@{
      maquina = $nome; ip = $ip; ok = $false
      erro = $_.Exception.Message.Split([char]10)[0]
    }
  }
}

# ---------------------------------------------------------------- 2. coleta
$COLETA = {
  $o = [ordered]@{ ok = $true }

  # --- identidade e nome -----------------------------------------------
  try {
    $os = Get-CimInstance Win32_OperatingSystem
    $cs = Get-CimInstance Win32_ComputerSystem
    # nome ativo x nome apos o boot: se diferem, foi renomeada e AINDA NAO
    # REINICIOU. E como eu sei quem falta reiniciar.
    $o.nome_ativo   = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\ComputerName\ActiveComputerName' -Name ComputerName).ComputerName
    $o.nome_proximo = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\ComputerName\ComputerName'       -Name ComputerName).ComputerName
    $o.renomeada    = ($o.nome_ativo -ne $o.nome_proximo)
    $o.modelo   = "$($cs.Manufacturer) $($cs.Model)".Trim()
    $o.windows  = $os.Caption
    $o.build    = $os.BuildNumber
    # NAO chamar de 'ps': o merge la embaixo descarta tudo que casa com 'PS*'
    # (e o -like do PowerShell ignora maiuscula), entao o campo sumia calado.
    $o.psver    = "$($PSVersionTable.PSVersion)"
    $o.instalado = $os.InstallDate.ToString('dd/MM/yyyy')
    $o.uptime_h = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalHours, 1)
    $o.ram_livre_gb = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
    $o.ram_total_gb = [math]::Round($cs.TotalPhysicalMemory / 1GB, 1)
    $o.usuario  = $cs.UserName
  } catch { $o.erro_identidade = $_.Exception.Message.Split([char]10)[0] }

  try { $o.serial = (Get-CimInstance Win32_BIOS).SerialNumber } catch { }

  # --- disco: espaco ----------------------------------------------------
  try {
    $c = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"
    $o.disco_livre_gb = [math]::Round($c.FreeSpace / 1GB, 1)
    $o.disco_total_gb = [math]::Round($c.Size / 1GB, 1)
    $o.disco_livre_pct = if ($c.Size) { [math]::Round(100 * $c.FreeSpace / $c.Size, 1) } else { $null }
  } catch { }

  # --- disco: SMART -----------------------------------------------------
  # Avisa ANTES do disco levar a estacao junto. PowerOnHours so aparece em
  # alguns SATA; a maioria dos NVMe nao expoe - por isso pode vir nulo.
  try {
    $pd = @(Get-PhysicalDisk -ErrorAction Stop | Where-Object { $_.BusType -ne 'USB' })
    $o.disco_modelo = ($pd | ForEach-Object { $_.FriendlyName }) -join ' | '
    $o.disco_saude  = ($pd | ForEach-Object { $_.HealthStatus }) -join ','
    $rc = @($pd | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue)
    if ($rc) {
      $t = @($rc | ForEach-Object { $_.Temperature } | Where-Object { $_ -gt 0 })
      $o.disco_temp  = if ($t) { [int]($t | Measure-Object -Maximum).Maximum } else { $null }
      $w = @($rc | ForEach-Object { $_.Wear } | Where-Object { $null -ne $_ })
      $o.disco_wear  = if ($w) { [int]($w | Measure-Object -Maximum).Maximum } else { $null }
      $h = @($rc | ForEach-Object { $_.PowerOnHours } | Where-Object { $_ -gt 0 })
      $o.disco_horas = if ($h) { [int]($h | Measure-Object -Maximum).Maximum } else { $null }
    }
  } catch { $o.disco_saude = 'sem dados' }

  # --- desligamento sujo ------------------------------------------------
  # 6008 = "o desligamento anterior foi inesperado"; 41 = Kernel-Power.
  # Foi assim que apareceu que o PDV levava 12 quedas em 60 dias enquanto as
  # vizinhas tinham zero - ou seja, problema DAQUELA tomada, nao da rede.
  try {
    $ev = @(Get-WinEvent -FilterHashtable @{LogName='System'; Id=6008,41; StartTime=(Get-Date).AddDays(-60)} -ErrorAction SilentlyContinue)
    $o.quedas_60d = $ev.Count
    $o.queda_ultima = if ($ev) { $ev[0].TimeCreated.ToString('dd/MM HH:mm') } else { $null }
    $o.queda_ultima_ts = if ($ev) { $ev[0].TimeCreated.ToString('o') } else { $null }
  } catch { $o.quedas_60d = $null }

  # --- antivirus e firewall ---------------------------------------------
  try {
    $av = @(Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct -ErrorAction Stop)
    # O Security Center guarda o registro de versoes JA DESINSTALADAS: a cada
    # atualizacao do antivirus entra uma entrada nova e a velha fica. Na
    # um notebook havia 4 do Kaspersky (21.20, 21.22, 21.24 e 21.26) e so
    # a ultima existia em disco. Contar tudo dava "5 antivirus brigando", o que
    # era falso. So conta quem tem executavel de verdade.
    # FANTASMA e so quem aponta para um ARQUIVO que nao existe mais.
    # O Defender registra "windowsdefender://" (URI, nao arquivo) e e legitimo -
    # a primeira versao deste filtro exigia arquivo em disco e zerou o antivirus
    # de 8 maquinas, o que teria virado alarme falso pior que o problema.
    $vivo = {
      param($x)
      $p = ([string]$x.pathToSignedProductExe -replace '"','').Trim()
      (-not $p) -or ($p -match '^[a-z][a-z0-9+.\-]*://') -or (Test-Path $p -ErrorAction SilentlyContinue)
    }
    # bit 0x1000 do productState = produto LIGADO. O Defender recuado (porque
    # ha antivirus de terceiros) vem sem esse bit - e nao deve contar como
    # antivirus ativo nem gerar alarme.
    $reais = @($av | Where-Object { (& $vivo $_) -and (($_.productState -band 0x1000) -ne 0) })
    $o.av_qtd       = $reais.Count
    $o.av_qtd_bruto = $av.Count
    $o.av_fantasmas = @($av | Where-Object { -not (& $vivo $_) }).Count
    $o.av_nome = (($reais | ForEach-Object { $_.displayName } | Select-Object -Unique) -join ' + ')
    if (-not $o.av_nome) { $o.av_nome = 'nenhum ativo' }
  } catch { $o.av_qtd = $null; $o.av_nome = 'nao consegui ler' }
  try {
    $fp = @(Get-NetFirewallProfile -ErrorAction Stop)
    $o.fw_off = (($fp | Where-Object { -not $_.Enabled } | ForEach-Object { $_.Name }) -join ',')
    $o.fw_ok  = -not [bool]$o.fw_off
  } catch { }

  # --- administradores locais -------------------------------------------
  # O nome do grupo muda com o idioma; o SID nao.
  try {
    $gn = ((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')).Translate([Security.Principal.NTAccount]).Value -split '\\')[-1]
    $g = [ADSI]"WinNT://$env:COMPUTERNAME/$gn,group"
    $o.admins = ((@($g.psbase.Invoke('Members')) | ForEach-Object {
      try { $_.GetType().InvokeMember('Name','GetProperty',$null,$_,$null) } catch { } } | Sort-Object) -join ',')
  } catch { $o.admins = 'nao consegui ler' }

  # --- atualizacao ------------------------------------------------------
  try {
    $h = Get-HotFix -ErrorAction Stop | Where-Object { $_.InstalledOn } | Sort-Object InstalledOn -Descending | Select-Object -First 1
    if ($h) {
      $o.patch_ultimo = $h.InstalledOn.ToString('dd/MM/yyyy')
      $o.patch_dias   = [int]((Get-Date) - $h.InstalledOn).TotalDays
    }
  } catch { }
  try {
    $o.reiniciar = (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') -or
                   (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired')
  } catch { }

  # --- o zumbi do DNS ---------------------------------------------------
  try {
    $doKey = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\DeliveryOptimization'
    $o.do_mode = if (Test-Path $doKey) { (Get-ItemProperty $doKey -Name DODownloadMode -ErrorAction SilentlyContinue).DODownloadMode } else { $null }
  } catch { }
  # WPAD: bit 0x08 do byte 8 do blob. NAO usar HKCU: dentro do WinRM o HKCU e
  # o perfil do gtiadmin, nao o do usuario que trabalha na maquina.
  try {
    if (-not (Get-PSDrive -Name HKU -ErrorAction SilentlyContinue)) {
      New-PSDrive -Name HKU -PSProvider Registry -Root HKEY_USERS -ErrorAction SilentlyContinue | Out-Null
    }
    # IGNORAR O PROPRIO PERFIL DA CONTA DE GERENCIA. Conectar por WinRM cria
    # o perfil do gtiadmin, e perfil novo nasce com o WPAD LIGADO - o monitor
    # acusaria eternamente uma conta de servico que nunca abre navegador.
    $meuSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $w = $false; $perfis = @()
    foreach ($p in @(Get-ChildItem 'HKU:\' -ErrorAction SilentlyContinue | Where-Object { $_.PSChildName -match '^S-1-5-21-[\d-]+$' })) {
      if ($p.PSChildName -eq $meuSid) { continue }
      $b = (Get-ItemProperty "HKU:\$($p.PSChildName)\Software\Microsoft\Windows\CurrentVersion\Internet Settings\Connections" -Name DefaultConnectionSettings -ErrorAction SilentlyContinue).DefaultConnectionSettings
      if ($b -and $b.Length -gt 8 -and ($b[8] -band 0x08)) {
        $w = $true
        # traduzir o SID para nome: saber que e o 'WsiAccount' (a conta de
        # servico do Warsaw) vale muito mais do que ver um SID cru no alerta
        $quem = try { ((New-Object Security.Principal.SecurityIdentifier($p.PSChildName)).Translate([Security.Principal.NTAccount]).Value -split '\\')[-1] } catch { $p.PSChildName }
        $perfis += $quem
      }
    }
    $o.wpad_ligado = $w
    $o.wpad_perfis = $perfis
  } catch { }

  [PSCustomObject]$o
}

$vivas = @()
if ($sessoes.Count -gt 0) {
  $res = Invoke-Command -Session $sessoes -ScriptBlock $COLETA -ErrorAction SilentlyContinue
  foreach ($r in @($res)) {
    $s = $sessoes | Where-Object { $_.ComputerName -eq $r.PSComputerName } | Select-Object -First 1
    $o = [ordered]@{
      maquina = $(if ($s) { $s._maquina } else { $r.PSComputerName })
      ip      = $r.PSComputerName
    }
    foreach ($p in $r.PSObject.Properties) {
      if ($p.Name -like 'PS*' -or $p.Name -eq 'RunspaceId') { continue }
      $o[$p.Name] = $p.Value
    }
    $vivas += [PSCustomObject]$o
  }
  $sessoes | Remove-PSSession -ErrorAction SilentlyContinue
}

$saida = [PSCustomObject]@{
  ok       = $true
  quando   = (Get-Date).ToString('o')
  total    = ($vivas.Count + $mortas.Count)
  no_ar    = $vivas.Count
  estacoes = @($vivas + $mortas)
}

if ($Bonito) { $saida | ConvertTo-Json -Depth 5 }
else         { $saida | ConvertTo-Json -Depth 5 -Compress }
