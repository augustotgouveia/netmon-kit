# ============================================================================
#  gerar_rdp.ps1 - cria os perfis de acesso RDP no servidor .1
#
#  Roda NO SERVIDOR. Para cada estacao Pro do cofre gera:
#     C:\netmon\rdp\<MAQUINA>.rdp   perfil de conexao (SEM senha dentro)
#     C:\netmon\rdp\<MAQUINA>.bat   atalho que conecta ja autenticado
#
#  A SENHA NAO VAI PARA ARQUIVO NENHUM. O .bat chama o CONECTAR.ps1, que le a
#  credencial do cofre cifrado (DPAPI), injeta no Gerenciador de Credenciais
#  so durante a conexao e APAGA quando a janela fecha.
# ============================================================================
param([string]$Cofre = 'C:\netmon\creds', [string]$Destino = 'C:\netmon\rdp')
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) } catch { }

New-Item -ItemType Directory -Force $Destino | Out-Null
# so Administradores e SYSTEM, por SID (o nome do grupo muda com o idioma)
try {
  $acl = Get-Acl $Destino
  $acl.SetAccessRuleProtection($true, $false)
  $acl.Access | ForEach-Object { [void]$acl.RemoveAccessRule($_) }
  foreach ($sid in 'S-1-5-32-544','S-1-5-18') {
    $id = New-Object Security.Principal.SecurityIdentifier($sid)
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($id,'FullControl','ContainerInherit,ObjectInherit','None','Allow')))
  }
  Set-Acl $Destino $acl
  "permissoes de $Destino : so Administradores e SYSTEM"
} catch { "aviso: nao endureci as permissoes - $($_.Exception.Message.Split([char]10)[0])" }

# ----------------------------------------------------------- o conectador
$conectar = @'
# Conecta por RDP usando a credencial do cofre, sem guardar senha em arquivo.
param([Parameter(Mandatory = $true)][string]$Maquina)
$ErrorActionPreference = 'Stop'
$arq = "C:\netmon\creds\$Maquina.xml"
if (-not (Test-Path $arq)) { Write-Host "sem credencial de $Maquina no cofre" -ForegroundColor Red; pause; exit 1 }
$r = Import-Clixml $arq
$alvo = "TERMSRV/" + $r.ip
$u = $r.cred.UserName
$p = $r.cred.GetNetworkCredential().Password
Write-Host "conectando em $($r.maquina) ($($r.ip)) como $u ..." -ForegroundColor Cyan
# a credencial fica no Gerenciador so enquanto a janela estiver aberta
& cmdkey.exe /generic:$alvo /user:$u /pass:$p | Out-Null
try {
  $perfil = "C:\netmon\rdp\$Maquina.rdp"
  if (Test-Path $perfil) { Start-Process mstsc.exe -ArgumentList "`"$perfil`"" -Wait }
  else { Start-Process mstsc.exe -ArgumentList "/v:$($r.ip)" -Wait }
} finally {
  & cmdkey.exe /delete:$alvo | Out-Null
  Write-Host "credencial removida do Gerenciador." -ForegroundColor DarkGray
}
'@
[IO.File]::WriteAllText((Join-Path $Destino 'CONECTAR.ps1'), $conectar, (New-Object Text.UTF8Encoding $false))

# --------------------------------------------------------------- perfis
$feitos = 0; $pulados = @()
foreach ($f in Get-ChildItem (Join-Path $Cofre '*.xml') | Sort-Object Name) {
  $r = Import-Clixml $f.FullName
  $nome = $r.maquina

  # so gera para quem realmente aceita RDP
  $pode = $false
  try {
    $pode = Invoke-Command -ComputerName $r.ip -Credential $r.cred -ErrorAction Stop -ScriptBlock {
      $os = Get-CimInstance Win32_OperatingSystem
      -not ($os.Caption -match '(?i)home|single language|core')
    }
  } catch { $pulados += "$nome (fora do ar)"; continue }
  if (-not $pode) { $pulados += "$nome (Windows Home)"; continue }

  $rdp = @"
full address:s:$($r.ip)
username:s:$nome\$($r.cred.UserName.Split('\')[-1])
prompt for credentials:i:0
promptcredentialonce:i:1
authentication level:i:2
enablecredsspsupport:i:1
screen mode id:i:2
use multimon:i:0
dynamic resolution:i:1
smart sizing:i:1
session bpp:i:32
audiomode:i:2
redirectclipboard:i:1
redirectprinters:i:0
redirectsmartcards:i:0
drivestoredirect:s:
keyboardhook:i:2
autoreconnection enabled:i:1
bandwidthautodetect:i:1
networkautodetect:i:1
compression:i:1
"@
  [IO.File]::WriteAllText((Join-Path $Destino "$nome.rdp"), $rdp, (New-Object Text.UTF8Encoding $false))

  $bat = "@echo off`r`ntitle RDP - $nome`r`npowershell -NoProfile -ExecutionPolicy Bypass -File `"%~dp0CONECTAR.ps1`" -Maquina `"$nome`"`r`n"
  [IO.File]::WriteAllText((Join-Path $Destino "$nome.bat"), $bat, (New-Object Text.ASCIIEncoding))
  $feitos++
  "  perfil criado: $nome  ->  $($r.ip)"
}

"`n$feitos perfil(is) criado(s) em $Destino"
if ($pulados) { "sem perfil: " + ($pulados -join ', ') }
"`nconteudo:"
Get-ChildItem $Destino | Sort-Object Name | ForEach-Object { "  {0,-30} {1,6} b" -f $_.Name, $_.Length }
