# ============================================================================
#  importar-credenciais.ps1
#
#  Le o SENHAS-GERENCIA.csv do pendrive e monta o cofre do NetMon em
#  C:\netmon\creds\, um arquivo por estacao, CIFRADO por DPAPI.
#
#  DPAPI amarra a cifra a CONTA e a MAQUINA: so o SERVIDOR\Administrador
#  (que e a conta sob a qual o NetMon roda) consegue abrir, e so neste servidor.
#  Copiar o arquivo para outro lugar nao adianta nada para quem copiou.
#
#  Depois que isto rodar, o CSV do pendrive pode ser destruido - as senhas
#  passam a viver so aqui, cifradas.
#
#  RODAR NO SERVIDOR .1, como Administrador:
#     powershell -ExecutionPolicy Bypass -File importar-credenciais.ps1 -Csv <caminho>
#     ... -Testar        tenta conectar em cada uma depois de importar
#     ... -ApagarCsv     apaga o CSV de origem no fim (sobrescreve antes)
# ============================================================================
param(
  [Parameter(Mandatory = $true)][string]$Csv,
  [string]$Destino = 'C:\netmon\creds',
  [switch]$Testar,
  [switch]$ApagarCsv
)

$ErrorActionPreference = 'Stop'
function Ok($t)   { Write-Host "  [OK]   $t" -ForegroundColor Green }
function Info($t) { Write-Host "  .      $t" -ForegroundColor Gray }
function Aviso($t){ Write-Host "  [!]    $t" -ForegroundColor Yellow }
function Erro($t) { Write-Host "  [X]    $t" -ForegroundColor Red }

if (-not (Test-Path $Csv)) { Erro "nao achei $Csv"; exit 1 }
New-Item -ItemType Directory -Force $Destino | Out-Null

# so Administradores e SYSTEM enxergam a pasta do cofre
try {
  $acl = Get-Acl $Destino
  $acl.SetAccessRuleProtection($true, $false)   # para de herdar
  $acl.Access | ForEach-Object { [void]$acl.RemoveAccessRule($_) }
  # Por SID, nao por nome: num Windows em portugues 'BUILTIN\Administrators'
  # se chama 'BUILTIN\Administradores' e 'NT AUTHORITY\SYSTEM' e
  # 'NT AUTHORITY\SISTEMA'. O nome muda com o idioma, o SID nunca muda.
  # S-1-5-32-544 = Administradores   |   S-1-5-18 = SISTEMA
  foreach ($sid in 'S-1-5-32-544', 'S-1-5-18') {
    $id = New-Object Security.Principal.SecurityIdentifier($sid)
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
      $id, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
  }
  Set-Acl $Destino $acl
  Ok "permissoes de $Destino : so Administradores e SYSTEM"
} catch { Aviso "nao consegui endurecer as permissoes: $($_.Exception.Message.Split([char]10)[0])" }

$linhas = @(Import-Csv $Csv)
Info "$($linhas.Count) linha(s) no CSV"
$feitas = 0; $puladas = 0

foreach ($l in $linhas) {
  $maq = ([string]$l.maquina).Trim()
  $ip  = ([string]$l.ip).Trim()
  $usr = ([string]$l.usuario).Trim()
  $sen = [string]$l.senha

  if (-not $maq -or -not $ip -or -not $usr -or -not $sen) {
    Aviso "linha incompleta, pulei: maquina='$maq' ip='$ip'"; $puladas++; continue
  }
  if ($sen -like '(enviada*') {
    Aviso "$maq : CSV sem a senha (versao antiga do script). Pegue do Telegram."; $puladas++; continue
  }

  # O "dominio" da credencial local e o NOME DA MAQUINA. Depois do rename +
  # reboot, e o nome NOVO - que e justamente o que a coluna 'maquina' guarda.
  $cred = New-Object PSCredential("$maq\$usr", (ConvertTo-SecureString $sen -AsPlainText -Force))

  $registro = [PSCustomObject]@{
    maquina = $maq
    ip      = $ip
    mac     = ([string]$l.mac).Trim()
    cred    = $cred
    quando  = (Get-Date).ToString('o')
  }
  $arq = Join-Path $Destino ($maq + '.xml')
  $registro | Export-Clixml $arq
  Ok "$maq  ->  $(Split-Path $arq -Leaf)   ($ip)"
  $feitas++
}

Write-Host ""
Info "importadas: $feitas   puladas: $puladas"

# ---------------------------------------------------------------- conferir
Write-Host ""
Info "conferindo que o cofre abre e que nada ficou em texto claro..."
$falhouLeitura = 0
foreach ($f in @(Get-ChildItem (Join-Path $Destino '*.xml'))) {
  try {
    $r = Import-Clixml $f.FullName
    if (-not $r.cred.GetNetworkCredential().Password) { throw "senha vazia" }
  } catch { Erro "$($f.Name): nao abriu - $($_.Exception.Message.Split([char]10)[0])"; $falhouLeitura++ }
  # o .xml nao pode conter a senha legivel
  if ((Get-Content $f.FullName -Raw) -match [regex]::Escape(($linhas | Where-Object { $_.maquina -eq $f.BaseName } | Select-Object -First 1).senha)) {
    Erro "$($f.Name): A SENHA ESTA EM TEXTO CLARO NO ARQUIVO - nao use"
    $falhouLeitura++
  }
}
if ($falhouLeitura -eq 0) { Ok "todos os $feitas arquivos abrem, e nenhum tem senha legivel" }

# ---------------------------------------------------------------- testar
if ($Testar) {
  Write-Host ""
  Info "testando WinRM em cada estacao (quem nao reiniciou ainda pode falhar)..."
  $opt = New-PSSessionOption -OpenTimeout 5000 -OperationTimeout 10000
  foreach ($f in @(Get-ChildItem (Join-Path $Destino '*.xml'))) {
    $r = Import-Clixml $f.FullName
    try {
      $n = Invoke-Command -ComputerName $r.ip -Credential $r.cred -SessionOption $opt -ScriptBlock { $env:COMPUTERNAME } -ErrorAction Stop
      if ($n -eq $r.maquina) { Ok "$($r.maquina) ($($r.ip)) respondeu como $n" }
      else { Aviso "$($r.maquina) ($($r.ip)) respondeu como '$n' - ainda nao reiniciou?" }
    } catch {
      Erro "$($r.maquina) ($($r.ip)): $($_.Exception.Message.Split([char]10)[0])"
    }
  }
}

# ---------------------------------------------------------------- apagar CSV
if ($ApagarCsv) {
  if ($falhouLeitura -gt 0) {
    Erro "NAO vou apagar o CSV: houve falha na conferencia acima."
  } else {
    Write-Host ""
    # sobrescreve antes de apagar - deletar so tira o indice, os bytes ficam
    $tam = (Get-Item $Csv).Length
    $lixo = New-Object byte[] $tam
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($lixo)
    [IO.File]::WriteAllBytes($Csv, $lixo)
    Remove-Item $Csv -Force
    Ok "CSV sobrescrito com lixo e apagado: $Csv"
    Aviso "as senhas agora existem SO no cofre cifrado e no seu Telegram"
  }
}
Write-Host ""
