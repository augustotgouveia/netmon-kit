# ============================================================================
#  mata_wpad.ps1 - desliga o WPAD nos perfis de UMA estacao, por WinRM
#
#  O QUE E O ZUMBI: WPAD ligado faz a maquina procurar um proxy por DNS a cada
#  conexao, gerando laco de consultas. O culpado recorrente aqui e o Warsaw,
#  que cria o perfil local WsiAccount JA COM WPAD LIGADO -- por isso volta
#  sozinho depois de atualizacao do modulo bancario.
#
#  MEDE E CONSERTA EXATAMENTE O QUE O NETMON OLHA: bit 0x08 do byte 8 do blob
#  DefaultConnectionSettings, em cada SID de HKU, PULANDO o perfil da propria
#  conta de gerencia (perfil novo nasce com WPAD ligado e acusaria pra sempre).
#
#  Guarda o blob original num arquivo na propria maquina antes de escrever.
#
#  USO: .\mata_wpad.ps1 -Maquina PC-EXEMPLO            -> simula
#       .\mata_wpad.ps1 -Maquina PC-EXEMPLO -Aplicar   -> aplica
# ============================================================================
param(
  [Parameter(Mandatory = $true)][string]$Maquina,
  [switch]$Aplicar,
  [string]$Cofre = 'C:\netmon\creds'
)
$ErrorActionPreference = 'Stop'

$arq = Join-Path $Cofre ($Maquina + '.xml')
if (-not (Test-Path $arq)) { "Nao achei $arq no cofre."; exit 1 }
$reg = Import-Clixml $arq
"alvo   : {0}  ip={1}" -f $reg.maquina, $reg.ip
"conta  : {0}" -f $reg.cred.UserName

$bloco = {
  # NAO chamar de $aplicar: o parametro do script la em cima e $Aplicar, e o
  # PowerShell nao diferencia maiuscula. Rodam em sessoes diferentes e nao
  # colidiriam de fato, mas essa ambiguidade ja custou caro neste projeto.
  param($fazer)
  $res = [ordered]@{ maquina = $env:COMPUTERNAME; perfis = @(); do_mode = $null; erro = $null }
  try {
    $doKey = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\DeliveryOptimization'
    if (Test-Path $doKey) {
      $res.do_mode = (Get-ItemProperty $doKey -Name DODownloadMode -ErrorAction SilentlyContinue).DODownloadMode
    }
  } catch { }

  try {
    if (-not (Get-PSDrive -Name HKU -ErrorAction SilentlyContinue)) {
      New-PSDrive -Name HKU -PSProvider Registry -Root HKEY_USERS -ErrorAction SilentlyContinue | Out-Null
    }
    # mesma exclusao que o coletor faz: o perfil da conta de gerencia nasce com
    # WPAD ligado e nao representa ninguem que usa a maquina
    $meuSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value

    $backup = @{}
    foreach ($p in @(Get-ChildItem 'HKU:\' -ErrorAction SilentlyContinue |
                     Where-Object { $_.PSChildName -match '^S-1-5-21-[\d-]+$' })) {
      $sid = $p.PSChildName
      if ($sid -eq $meuSid) { continue }
      $cam = "HKU:\$sid\Software\Microsoft\Windows\CurrentVersion\Internet Settings\Connections"
      $b = (Get-ItemProperty $cam -Name DefaultConnectionSettings -ErrorAction SilentlyContinue).DefaultConnectionSettings
      if (-not $b -or $b.Length -le 8) { continue }
      $ligado = [bool]($b[8] -band 0x08)
      $quem = try { ((New-Object Security.Principal.SecurityIdentifier($sid)).Translate([Security.Principal.NTAccount]).Value -split '\\')[-1] } catch { $sid }
      $item = [ordered]@{ sid = $sid; nome = $quem; antes = $b[8]; wpad = $ligado; depois = $null; mexi = $false }

      if ($ligado -and $fazer) {
        $backup[$sid] = ($b -join ',')
        $novo = $b.Clone()
        $novo[8] = [byte]($b[8] -band 0xF7)   # apaga so o bit 0x08, preserva o resto
        Set-ItemProperty -Path $cam -Name DefaultConnectionSettings -Value $novo -Type Binary
        $conf = (Get-ItemProperty $cam -Name DefaultConnectionSettings).DefaultConnectionSettings
        $item.depois = $conf[8]
        $item.mexi = $true
        $item.wpad = [bool]($conf[8] -band 0x08)
      }
      $res.perfis += [PSCustomObject]$item
    }

    if ($fazer -and $backup.Count) {
      $bk = "C:\Windows\Temp\wpad-backup-$(Get-Date -Format 'yyyyMMdd-HHmmss').json"
      ($backup | ConvertTo-Json -Depth 4) | Set-Content $bk -Encoding UTF8
      $res.backup = $bk
    }
  } catch { $res.erro = $_.Exception.Message }
  [PSCustomObject]$res
}

$r = Invoke-Command -ComputerName $reg.ip -Credential $reg.cred -ScriptBlock $bloco -ArgumentList ([bool]$Aplicar)

""
# A regra do NetMon (_est_zumbi) aceita 1 OU 99 -- modo 1 e P2P so dentro da
# LAN, decidido como aceitavel nas maquinas que nunca adoeceram; 99 e o corte
# total. Qualquer outro valor e que e regressao. (Nao e "100": esse numero era
# suposicao minha.)
"DO mode (DODownloadMode): {0}   {1}" -f $r.do_mode, $(if ($r.do_mode -in 1, 99) { '(aceitavel)' } else { '<- REGRESSAO, esperado 1 ou 99' })
if ($r.erro) { "ERRO: $($r.erro)" }
""
"perfis com blob de proxy:"
foreach ($p in $r.perfis) {
  $est = if ($p.wpad) { 'WPAD LIGADO' } else { 'wpad desligado' }
  $mex = if ($p.mexi) { "  (byte 8: $($p.antes) -> $($p.depois))" } else { '' }
  "  {0,-22} {1}{2}" -f $p.nome, $est, $mex
}
$maus = @($r.perfis | Where-Object { $_.wpad })
""
if (-not $Aplicar) {
  "[SIMULACAO] {0} perfil(is) com WPAD ligado. Rode com -Aplicar." -f $maus.Count
} else {
  if ($r.backup) { "backup do blob original: $($r.backup)" }
  if ($maus.Count -eq 0) { "RESULTADO: nenhum perfil com WPAD ligado. Zumbi morto." }
  else { "RESULTADO: ainda sobraram {0} perfil(is) com WPAD." -f $maus.Count }
}


