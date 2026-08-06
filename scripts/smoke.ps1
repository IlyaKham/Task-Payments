# Локальная проверка контракта из PowerShell.
#
# curl.exe в PowerShell непригоден для JSON: оболочка портит экранированные
# кавычки ещё до запуска процесса. Здесь используется Invoke-WebRequest,
# которому тело передаётся уже готовой строкой.
#
# Запуск:  .\scripts\smoke.ps1
#          .\scripts\smoke.ps1 -BaseUrl http://localhost:8080 -OperationId op-42

param(
    [string]$BaseUrl = "http://localhost:8080",
    [string]$OperationId = "op-$(Get-Random -Maximum 99999)",
    [int]$TimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"

function Invoke-Api {
    param(
        [string]$Method,
        [string]$Path,
        [string]$Body,
        [string]$Title,
        [switch]$Quiet
    )

    $uri = "$BaseUrl$Path"
    $params = @{
        Method  = $Method
        Uri     = $uri
        Headers = @{ "Content-Type" = "application/json; charset=utf-8" }
    }

    if ($Body) {
        $params["Body"] = [System.Text.Encoding]::UTF8.GetBytes($Body)
    }

    if (-not $Quiet) {
        Write-Host ""
        Write-Host "=== $Title" -ForegroundColor Cyan
        Write-Host "$Method $Path" -ForegroundColor DarkGray
    }

    try {
        $response = Invoke-WebRequest @params -UseBasicParsing
        $code = [int]$response.StatusCode
        $content = $response.Content
    }
    catch [System.Net.WebException] {
        $webResponse = $_.Exception.Response
        if ($null -eq $webResponse) { throw }
        $code = [int]$webResponse.StatusCode
        $reader = New-Object System.IO.StreamReader($webResponse.GetResponseStream())
        $content = $reader.ReadToEnd()
        $reader.Close()
    }
    catch {
        # PowerShell 7 бросает HttpResponseException вместо WebException.
        if ($_.Exception.Response) {
            $code = [int]$_.Exception.Response.StatusCode
            $content = $_.ErrorDetails.Message
        }
        else { throw }
    }

    if (-not $Quiet) {
        Write-Host "HTTP $code" -ForegroundColor Yellow
        if ($content) { Write-Host $content }
    }

    return [pscustomobject]@{ Code = $code; Content = $content }
}

$createBody = @{
    operationId = $OperationId
    amount      = "1000.00"
    currency    = "RUB"
    description = "Оплата заказа"
} | ConvertTo-Json -Compress

Write-Host "Операция: $OperationId" -ForegroundColor Green

Invoke-Api -Method GET  -Path "/health" -Title "Готовность (ожидаем 200)" | Out-Null
Invoke-Api -Method POST -Path "/operations" -Body $createBody -Title "Создание (ожидаем 201)" | Out-Null
Invoke-Api -Method POST -Path "/operations" -Body $createBody -Title "Повторное создание (ожидаем 409)" | Out-Null
Invoke-Api -Method POST -Path "/operations/$OperationId/submit" -Title "Первый submit (ожидаем 202)" | Out-Null
Invoke-Api -Method POST -Path "/operations/$OperationId/submit" -Title "Повторный submit (ожидаем 200)" | Out-Null

# Диспетчер вызывает провайдера в фоне, симулятор присылает квитанцию своим
# темпом. Поэтому не «ожидаем PROCESSING», а ждём финального статуса.
Write-Host ""
Write-Host "=== Ожидание квитанции провайдера" -ForegroundColor Cyan
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$status = $null
while ((Get-Date) -lt $deadline) {
    $state = Invoke-Api -Method GET -Path "/operations/$OperationId" -Quiet
    $status = ($state.Content | ConvertFrom-Json).status
    Write-Host "  статус: $status" -ForegroundColor DarkGray
    if ($status -eq "COMPLETED" -or $status -eq "REJECTED") { break }
    Start-Sleep -Milliseconds 700
}

if ($status -eq "COMPLETED" -or $status -eq "REJECTED") {
    Write-Host "Финальный статус: $status" -ForegroundColor Green
}
else {
    Write-Host "Квитанция не пришла за $TimeoutSeconds с, статус: $status" -ForegroundColor Red
}

Invoke-Api -Method GET -Path "/operations/$OperationId" -Title "Состояние операции" | Out-Null
Invoke-Api -Method GET -Path "/operations/$OperationId/events" -Title "История переходов" | Out-Null

Write-Host ""
Write-Host "Готово." -ForegroundColor Green
