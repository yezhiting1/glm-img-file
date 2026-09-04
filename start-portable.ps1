$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
$python = Join-Path $root "runtime\python\python.exe"
$envFile = Join-Path $root ".env"
$url = "http://127.0.0.1:8000"
$healthUrl = "$url/api/health"
$expectedApp = "昆虫标本图片识别与Excel录入工作台"
$openBrowser = $env:INSECT_PORTABLE_NO_BROWSER -ne "1"

function Test-AppHealth {
    try {
        $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        return $response.status -eq "ok" -and $response.app -eq $expectedApp
    }
    catch {
        return $false
    }
}

function ConvertTo-DotenvValue([string]$value) {
    if ($value.Contains("`r") -or $value.Contains("`n")) {
        throw "账号和密码不能包含换行符。"
    }
    return '"' + $value.Replace("\", "\\").Replace('"', '\"') + '"'
}

function New-InitialEnvironment {
    Write-Host "首次启动需要创建管理员账号。" -ForegroundColor Cyan

    do {
        $username = (Read-Host "管理员用户名").Trim()
    } while (-not $username)

    do {
        $securePassword = Read-Host "管理员密码（至少 12 位）" -AsSecureString
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
            $securePassword
        )
        try {
            $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
                $pointer
            )
        }
        finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        }
        if ($password.Length -lt 12) {
            Write-Host "密码至少需要 12 位，请重新输入。" -ForegroundColor Yellow
        }
    } while ($password.Length -lt 12)

    $lines = @(
        "BACKEND_HOST=127.0.0.1"
        "BACKEND_PORT=8000"
        "INSECT_BOOTSTRAP_ADMIN_USERNAME=$(ConvertTo-DotenvValue $username)"
        "INSECT_BOOTSTRAP_ADMIN_PASSWORD=$(ConvertTo-DotenvValue $password)"
        "AUTH_COOKIE_SECURE=false"
        "AUTH_SESSION_HOURS=24"
        "DEFAULT_USER_QUOTA=100"
        "MODEL_TIMEOUT_SECONDS=120"
        "MODEL_MAX_RETRIES=2"
        "IMAGE_MAX_LONG_EDGE=3000"
        "IMAGE_JPEG_QUALITY=90"
        'CORS_ORIGINS=["http://localhost:5173","http://127.0.0.1:5173"]'
    )
    $temporary = "$envFile.tmp-$PID"
    $encoding = New-Object System.Text.UTF8Encoding($false)
    try {
        [IO.File]::WriteAllLines($temporary, $lines, $encoding)
        Move-Item -LiteralPath $temporary -Destination $envFile
    }
    finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        $password = $null
    }
}

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "内置 Python 运行环境缺失，请重新下载并完整解压便携版。"
}

$writeProbe = Join-Path $root ".portable-write-test-$PID"
try {
    [IO.File]::WriteAllText($writeProbe, "ok")
}
catch {
    throw "当前目录不可写，请把便携版解压到桌面或其他可写目录。"
}
finally {
    Remove-Item -LiteralPath $writeProbe -Force -ErrorAction SilentlyContinue
}

if (Test-AppHealth) {
    Write-Host "昆虫标本工作台已经在运行。"
    if ($openBrowser) {
        Start-Process $url
    }
    exit 0
}

$listener = Get-NetTCPConnection -LocalPort 8000 -State Listen `
    -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    $processName = "未知"
    try {
        $processName = (Get-Process -Id $listener.OwningProcess).ProcessName
    }
    catch {
    }
    throw "端口 8000 已被进程 $processName（PID $($listener.OwningProcess)）占用。"
}

if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
    New-InitialEnvironment
}

Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONNOUSERSITE = "1"

$browserJob = $null
if ($openBrowser) {
    $browserJob = Start-Job -ArgumentList $healthUrl, $url, $expectedApp `
        -ScriptBlock {
            param($health, $page, $appName)
            for ($attempt = 0; $attempt -lt 60; $attempt++) {
                try {
                    $response = Invoke-RestMethod -Uri $health -TimeoutSec 1
                    if (
                        $response.status -eq "ok" -and
                        $response.app -eq $appName
                    ) {
                        Start-Process $page
                        return
                    }
                }
                catch {
                }
                Start-Sleep -Milliseconds 500
            }
        }
}

Write-Host "正在启动昆虫标本工作台，请保持此窗口开启。" -ForegroundColor Cyan
Push-Location $root
try {
    & $python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 `
        --app-dir backend
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
    if ($browserJob) {
        Stop-Job $browserJob -ErrorAction SilentlyContinue
        Remove-Job $browserJob -Force -ErrorAction SilentlyContinue
    }
}

exit $exitCode
