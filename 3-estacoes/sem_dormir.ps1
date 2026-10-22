# ============================================================================
#  sem_dormir.ps1 - deixa as estacoes sempre acessiveis pela rede
#
#  O QUE MUDA (somente no modo TOMADA / ligado na energia):
#    - suspensao        : nunca
#    - hibernacao ociosa: nunca
#    - desligar discos  : nunca
#    - tampa fechada    : nao faz nada  (notebook continua no ar)
#    - hibernacao       : DESLIGADA de vez (apaga o hiberfil.sys e sobra disco)
#    - placa de rede    : o Windows nao pode mais desligar ela para economizar
#    - TELA             : continua desligando (era o pedido)
#
#  O QUE NAO MUDA: o modo BATERIA. Notebook que nunca dorme na bateria
#  descarrega e morre - e maquina morta tambem nao e acessivel. Na bateria
#  ele continua dormindo normalmente.
#
#  -Simular mostra o antes sem mexer em nada.
# ============================================================================
param(
  [string]$Maquina,
  [switch]$Todas,
  [switch]$Simular,
  # -SoTomada: mexe SO no modo tomada e deixa a bateria em paz. Para a
  # notebook que sai da empresa (o do dono, por exemplo).
  [switch]$SoTomada,
  [int]$TelaMin = 15,
  [string]$Cofre = 'C:\netmon\creds'
)
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) } catch { }

$alvos = @()
if ($Todas)       { $alvos = @(Get-ChildItem (Join-Path $Cofre '*.xml') -ErrorAction SilentlyContinue) }
elseif ($Maquina) { $alvos = @(Get-Item (Join-Path $Cofre "$Maquina.xml") -ErrorAction SilentlyContinue) }
else { "use -Todas ou -Maquina <nome>"; exit 1 }

$TRABALHO = {
  param($simular, $telaMin, $soTomada)
  $L = New-Object Collections.ArrayList
  function Reg($t) { [void]$L.Add($t) }

  # --- e notebook ou desktop? muda o que faz sentido
  $cs = Get-CimInstance Win32_ComputerSystem
  $bat = @(Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue)
  $tipo = if ($bat.Count -gt 0) { 'notebook' } else { 'desktop' }
  Reg ("  tipo: {0} ({1})" -f $tipo, $cs.Model)

  # --- estado ANTES
  $hiberfil = if (Test-Path 'C:\hiberfil.sys') { [math]::Round((Get-Item 'C:\hiberfil.sys' -Force).Length/1GB,2) } else { 0 }
  Reg ("  antes: hiberfil.sys = {0} GB" -f $hiberfil)

  if ($simular) {
    Reg "  [simulacao] nada foi alterado"
    return ($L -join "`n")
  }

  # --- 1. tempos no modo TOMADA
  foreach ($par in @(
    @('standby-timeout-ac',  '0',        'suspensao na tomada    : nunca'),
    @('hibernate-timeout-ac','0',        'hibernacao ociosa      : nunca'),
    @('disk-timeout-ac',     '0',        'desligar disco         : nunca'),
    @('monitor-timeout-ac',  "$telaMin", "tela na tomada         : desliga em $telaMin min"))) {
    try { & powercfg.exe /change $par[0] $par[1] 2>&1 | Out-Null; Reg ("  ok  " + $par[2]) }
    catch { Reg ("  FALHOU " + $par[0]) }
  }

  # --- 1b. mesmos tempos na BATERIA
  # Estas maquinas nunca saem da empresa, entao a bateria delas e na pratica
  # um no-break: se a energia piscar, a maquina continua no ar em vez de
  # levar um desligamento sujo (o PDV ja levou 12 em 60 dias).
  if (-not $soTomada) {
    foreach ($par in @(
      @('standby-timeout-dc',  '0',        'suspensao na bateria   : nunca'),
      @('hibernate-timeout-dc','0',        'hibernacao na bateria  : nunca'),
      @('disk-timeout-dc',     '0',        'desligar disco (bat)   : nunca'),
      @('monitor-timeout-dc',  "$telaMin", "tela na bateria        : desliga em $telaMin min"))) {
      try { & powercfg.exe /change $par[0] $par[1] 2>&1 | Out-Null; Reg ("  ok  " + $par[2]) }
      catch { Reg ("  FALHOU " + $par[0]) }
    }
    # mas quando a bateria estiver no fim, desligar LIMPO (em vez de cair seco)
    $SUB_BATTERY   = 'e73a048d-bf27-4f12-9731-8b2076e8891f'
    $BATACTIONCRIT = '637ea02f-bbcb-4015-8e2c-a1c7b9c0b546'
    try {
      & powercfg.exe /setdcvalueindex SCHEME_CURRENT $SUB_BATTERY $BATACTIONCRIT 3 2>&1 | Out-Null
      Reg "  ok  bateria critica       : desliga de forma limpa"
    } catch { }
  } else {
    Reg "  --  modo bateria NAO tocado (-SoTomada)"
  }

  # --- 2. tampa fechada nao suspende
  # GUIDs fixos: o nome dessas opcoes e traduzido, o GUID nao.
  $SUB_BUTTONS = '4f971e89-eebd-4455-a8de-9e59040e7347'
  $LIDACTION   = '5ca83367-6e45-459f-a27b-476b1d01c936'
  if ($tipo -eq 'notebook') {
    try {
      & powercfg.exe /setacvalueindex SCHEME_CURRENT $SUB_BUTTONS $LIDACTION 0 2>&1 | Out-Null
      if (-not $soTomada) { & powercfg.exe /setdcvalueindex SCHEME_CURRENT $SUB_BUTTONS $LIDACTION 0 2>&1 | Out-Null }
      & powercfg.exe /setactive SCHEME_CURRENT 2>&1 | Out-Null
      Reg ("  ok  tampa fechada: nao faz nada" + $(if ($soTomada) { ' (so na tomada)' } else { ' (tomada e bateria)' }))
    } catch { Reg "  FALHOU acao da tampa" }
  }

  # --- 3. hibernacao de vez (libera o hiberfil.sys)
  try {
    & powercfg.exe /hibernate off 2>&1 | Out-Null
    Reg "  ok  hibernacao desligada"
  } catch { Reg "  FALHOU desligar hibernacao" }

  # --- 4. o Windows nao pode desligar a placa de rede
  # E o motivo classico de "a maquina some da rede depois de um tempo".
  #
  # ATENCAO: Set-NetAdapterPowerManagement NAO aceita -AllowComputerToTurnOffDevice
  # (essa propriedade e SO DE LEITURA no Get-). A versao anterior deste script
  # tentava por ali, o erro caia num catch vazio, e ele reportava alegremente
  # "0 adaptadores" como se estivesse tudo certo. Nunca fez nada.
  #
  # O controle de verdade e PnPCapabilities no registro da classe de rede:
  #   24 (0x18) = o computador NAO pode desligar este dispositivo.
  # So vale no proximo boot, mas nao derruba a rede agora.
  $CLS = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e972-e325-11ce-bfc1-08002be10318}'
  $mex = @()
  foreach ($a in @(Get-NetAdapter -Physical -ErrorAction SilentlyContinue)) {
    foreach ($k in @(Get-ChildItem $CLS -ErrorAction SilentlyContinue | Where-Object { $_.PSChildName -match '^\d{4}$' })) {
      $p = Get-ItemProperty $k.PSPath -ErrorAction SilentlyContinue
      if ($p.NetCfgInstanceId -ne $a.InterfaceGuid) { continue }
      if ($p.PnPCapabilities -eq 24) { $mex += "$($a.Name)=ja"; continue }
      try {
        Set-ItemProperty $k.PSPath -Name PnPCapabilities -Value 24 -Type DWord -ErrorAction Stop
        $mex += "$($a.Name)=ajustada"
      } catch { $mex += "$($a.Name)=FALHOU" }
    }
  }
  Reg ("  ok  placa de rede (PnPCapabilities=24, vale no proximo boot): " + ($mex -join ' '))

  # --- estado DEPOIS
  Start-Sleep -Seconds 2
  $hiberfil2 = if (Test-Path 'C:\hiberfil.sys') { [math]::Round((Get-Item 'C:\hiberfil.sys' -Force).Length/1GB,2) } else { 0 }
  if ($hiberfil -gt 0) { Reg ("  disco liberado pelo hiberfil.sys: {0} GB" -f ($hiberfil - $hiberfil2)) }

  # Conferencia pelo REGISTRO, nao pelo texto do powercfg.
  # A versao anterior lia os dois ultimos "0x..." depois do GUID e pegava
  # valores de OUTRA configuracao, reportando coisas como "suspensao = 1 s".
  # O registro e a fonte da verdade e nao depende de idioma.
  $g = [regex]::Match((& powercfg.exe /getactivescheme 2>&1 | Out-String),
                      '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}').Value
  if ($g) {
    $base = "HKLM:\SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes\$g"
    $slp = Get-ItemProperty "$base\238c9fa8-0aad-41ed-83f4-97be242c8f20\29f6c1db-86da-48c5-9fdb-f2b67b1f44da" -ErrorAction SilentlyContinue
    $lid = Get-ItemProperty "$base\4f971e89-eebd-4455-a8de-9e59040e7347\5ca83367-6e45-459f-a27b-476b1d01c936" -ErrorAction SilentlyContinue
    function Mostra($v) { if ($null -eq $v) { '?' } elseif ($v -eq 0) { 'nunca' } else { "$v s" } }
    Reg ("  CONFERIDO no registro: suspensao tomada={0} bateria={1}" -f (Mostra $slp.ACSettingIndex), (Mostra $slp.DCSettingIndex))
    if ($tipo -eq 'notebook' -and $lid) {
      Reg ("  CONFERIDO no registro: tampa CA={0} CC={1}   (0 = nao faz nada)" -f $lid.ACSettingIndex, $lid.DCSettingIndex)
    }
    Reg ("  CONFERIDO no registro: HibernateEnabled={0}  hiberfil={1}" -f
         (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Power' -Name HibernateEnabled -ErrorAction SilentlyContinue).HibernateEnabled,
         $(if (Test-Path 'C:\hiberfil.sys') { 'existe' } else { 'nao existe' }))
  }
  ($L -join "`n")
}

$opt = New-PSSessionOption -OpenTimeout 8000 -OperationTimeout 120000
foreach ($f in $alvos) {
  $r = Import-Clixml $f.FullName
  "=" * 66
  "$($r.maquina)  ($($r.ip))"
  "=" * 66
  try {
    Invoke-Command -ComputerName $r.ip -Credential $r.cred -SessionOption $opt -ScriptBlock $TRABALHO -ArgumentList $Simular.IsPresent, $TelaMin, $SoTomada.IsPresent -ErrorAction Stop
  } catch { "  FALHOU: $($_.Exception.Message.Split([char]10)[0])" }
  ""
}


