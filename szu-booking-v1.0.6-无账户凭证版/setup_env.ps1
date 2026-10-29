# Prepare runtime for 场馆抢单助手.
# Called by 启动.bat when .venv is missing.
# Messages are ASCII-only on purpose: cmd runs with codepage 936 while this
# file is UTF-8, so any non-ASCII here would be garbled when piped back.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$root   = $PSScriptRoot
$venvPy = Join-Path $root '.venv\Scripts\python.exe'
$reqFile = Join-Path $root 'requirements.txt'
$ver = '3.12.8'

function Test-Python([string]$exe) {
    if (-not $exe) { return $false }
    if (-not (Test-Path $exe)) { return $false }
    try {
        $out = & $exe -c "import sys;print(sys.version_info[0]*100+sys.version_info[1])" 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
        return [int]$out -ge 310
    } catch {
        return $false
    }
}

function Find-Python {
    $cands = New-Object System.Collections.ArrayList

    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { [void]$cands.Add($cmd.Source) }

    foreach ($pat in @("$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
                       "C:\Python3*\python.exe",
                       "$env:ProgramFiles\Python3*\python.exe")) {
        Get-ChildItem -Path $pat -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            ForEach-Object { [void]$cands.Add($_.FullName) }
    }

    foreach ($c in $cands) {
        if (Test-Python $c) { return $c }
    }
    return $null
}

function Install-Python {
    Write-Host '[1/3] downloading Python ...'
    $urls = @(
        "https://mirrors.huaweicloud.com/python/$ver/python-$ver-amd64.exe",
        "https://www.python.org/ftp/python/$ver/python-$ver-amd64.exe"
    )
    $out = Join-Path $env:TEMP "szu-python-$ver.exe"
    $ok = $false
    foreach ($u in $urls) {
        try {
            Invoke-WebRequest -Uri $u -OutFile $out -UseBasicParsing -TimeoutSec 600
            $ok = $true
            break
        } catch {
            Write-Host '      mirror failed, trying next ...'
        }
    }
    if (-not $ok) { return $false }

    $size = [math]::Round((Get-Item $out).Length / 1MB, 1)
    Write-Host "      downloaded $size MB, installing ..."

    $argList = '/quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_pip=1 Include_launcher=1'
    $p = Start-Process -FilePath $out -ArgumentList $argList -Wait -PassThru
    if ($p.ExitCode -ne 0) {
        Write-Host "      installer exit code $($p.ExitCode)"
        return $false
    }
    return $true
}

Write-Host '[1/3] looking for Python ...'
$py = Find-Python

if (-not $py) {
    Write-Host '      not found, installing Python 3.12 ...'
    if (-not (Install-Python)) {
        Write-Host '[!] failed to install Python'
        exit 3
    }
    $py = Find-Python
    if (-not $py) {
        Write-Host '[!] Python still not found after install'
        exit 4
    }
}
Write-Host "      using $py"

Write-Host '[2/3] creating virtual environment ...'
if (Test-Path (Join-Path $root '.venv')) {
    Remove-Item (Join-Path $root '.venv') -Recurse -Force -ErrorAction SilentlyContinue
}
& $py -m venv (Join-Path $root '.venv')
if (-not (Test-Path $venvPy)) {
    Write-Host '[!] failed to create .venv'
    exit 5
}

Write-Host '[3/3] installing dependencies ...'
# 中文 Windows 上 pip 会用 GBK 读文件，强制 UTF-8 避免任何编码坑
$env:PYTHONUTF8 = '1'
& $venvPy -m pip install --disable-pip-version-check -q -r $reqFile
if ($LASTEXITCODE -ne 0) {
    Write-Host '      default source failed, retrying with Tsinghua mirror ...'
    & $venvPy -m pip install --disable-pip-version-check -q -r $reqFile `
        -i https://pypi.tuna.tsinghua.edu.cn/simple
}
if ($LASTEXITCODE -ne 0) {
    Write-Host '[!] pip install failed'
    exit 6
}

Write-Host '[ok] environment ready'
exit 0
