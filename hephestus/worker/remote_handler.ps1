param(
    [string]$RelayUrl = "",
    [string]$SecretPath = "",
    [ValidateRange(1, 3600)][int]$CommandTimeoutSeconds = 120,
    [ValidateRange(1000, 500000)][int]$MaxOutputChars = 32000
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

if (-not ("Hephestus.ProcessJob" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;

namespace Hephestus {
    public sealed class ProcessJob : IDisposable {
        private IntPtr handle;
        private const uint KillOnJobClose = 0x00002000;

        [StructLayout(LayoutKind.Sequential)]
        private struct BasicLimits {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct IoCounters {
            public ulong ReadOperationCount;
            public ulong WriteOperationCount;
            public ulong OtherOperationCount;
            public ulong ReadTransferCount;
            public ulong WriteTransferCount;
            public ulong OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct ExtendedLimits {
            public BasicLimits BasicLimitInformation;
            public IoCounters IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        private static extern IntPtr CreateJobObject(IntPtr attributes, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(
            IntPtr job, int informationClass, IntPtr information, uint length);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        [DllImport("kernel32.dll")]
        private static extern bool CloseHandle(IntPtr handle);

        public ProcessJob() {
            handle = CreateJobObject(IntPtr.Zero, null);
            if (handle == IntPtr.Zero) throw new Win32Exception();
            ExtendedLimits limits = new ExtendedLimits();
            limits.BasicLimitInformation.LimitFlags = KillOnJobClose;
            int size = Marshal.SizeOf(typeof(ExtendedLimits));
            IntPtr value = Marshal.AllocHGlobal(size);
            try {
                Marshal.StructureToPtr(limits, value, false);
                if (!SetInformationJobObject(handle, 9, value, (uint)size))
                    throw new Win32Exception();
            }
            catch {
                CloseHandle(handle);
                handle = IntPtr.Zero;
                throw;
            }
            finally { Marshal.FreeHGlobal(value); }
        }

        public void AddProcess(Process process) {
            if (handle == IntPtr.Zero || !AssignProcessToJobObject(handle, process.Handle))
                throw new Win32Exception();
        }

        public void Dispose() {
            if (handle != IntPtr.Zero) {
                CloseHandle(handle);
                handle = IntPtr.Zero;
            }
        }
    }
}
'@
}

function Get-Utf8Bytes([string]$Value) {
    return ,([Text.Encoding]::UTF8.GetBytes($Value))
}

function Get-UnixTime {
    return [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
}

function Get-Hmac([byte[]]$Key, [byte[]]$Data) {
    $hmac = New-Object Security.Cryptography.HMACSHA256 -ArgumentList (, $Key)
    try { return $hmac.ComputeHash($Data) }
    finally { $hmac.Dispose() }
}

function Get-DerivedKey([byte[]]$MasterKey, [string]$Label) {
    if ($MasterKey.Length -ne 32) { throw "Master key must contain exactly 32 bytes." }
    $prefix = Get-Utf8Bytes "hephestus-v1"
    $suffix = Get-Utf8Bytes $Label
    $data = New-Object byte[] ($prefix.Length + 1 + $suffix.Length)
    [Array]::Copy($prefix, 0, $data, 0, $prefix.Length)
    $data[$prefix.Length] = 0
    [Array]::Copy($suffix, 0, $data, $prefix.Length + 1, $suffix.Length)
    return Get-Hmac $MasterKey $data
}

function Compare-Bytes([byte[]]$Left, [byte[]]$Right) {
    if ($null -eq $Left -or $null -eq $Right -or $Left.Length -ne $Right.Length) { return $false }
    $difference = 0
    for ($index = 0; $index -lt $Left.Length; $index++) {
        $difference = $difference -bor ($Left[$index] -bxor $Right[$index])
    }
    return $difference -eq 0
}

function Get-EnvelopeMacBytes($Envelope) {
    $canonical = [ordered]@{
        ciphertext = [string]$Envelope.ciphertext
        expires_at = [int64]$Envelope.expires_at
        iv = [string]$Envelope.iv
        job_id = [string]$Envelope.job_id
        v = [int]$Envelope.v
    }
    return Get-Utf8Bytes ($canonical | ConvertTo-Json -Compress)
}

function Assert-EnvelopeShape($Envelope, [int64]$Now, [string]$ExpectedJobId = "") {
    if ($null -eq $Envelope) { throw "Invalid encrypted envelope." }
    $names = @($Envelope.PSObject.Properties.Name | Sort-Object)
    $expected = @("ciphertext", "expires_at", "iv", "job_id", "tag", "v")
    if (($names -join ",") -ne ($expected -join ",")) { throw "Invalid encrypted envelope." }
    if ([int]$Envelope.v -ne 1) { throw "Invalid encrypted envelope." }
    if ([string]$Envelope.job_id -notmatch "^[a-f0-9]{32}$") { throw "Invalid encrypted envelope." }
    if ($ExpectedJobId -and [string]$Envelope.job_id -ne $ExpectedJobId) { throw "Invalid encrypted envelope." }
    if ([int64]$Envelope.expires_at -le $Now) { throw "Encrypted envelope expired." }
}

function Protect-Payload(
    [byte[]]$MasterKey,
    $Payload,
    [string]$JobId,
    [int64]$ExpiresAt,
    [int64]$Now,
    [byte[]]$Iv = $null
) {
    if ($JobId -notmatch "^[a-f0-9]{32}$" -or $ExpiresAt -le $Now) {
        throw "Invalid job identifier or expiry."
    }
    if ($null -eq $Iv) {
        $Iv = New-Object byte[] 16
        $random = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $random.GetBytes($Iv) }
        finally { $random.Dispose() }
    }
    if ($Iv.Length -ne 16) { throw "IV must contain 16 bytes." }
    $plaintext = Get-Utf8Bytes ($Payload | ConvertTo-Json -Compress -Depth 8)
    $encryptionKey = Get-DerivedKey $MasterKey "payload-encryption"
    $macKey = Get-DerivedKey $MasterKey "payload-authentication"
    $aes = [Security.Cryptography.Aes]::Create()
    try {
        $aes.KeySize = 256
        $aes.BlockSize = 128
        $aes.Mode = [Security.Cryptography.CipherMode]::CBC
        $aes.Padding = [Security.Cryptography.PaddingMode]::PKCS7
        $aes.Key = $encryptionKey
        $aes.IV = $Iv
        $encryptor = $aes.CreateEncryptor()
        try { $ciphertext = $encryptor.TransformFinalBlock($plaintext, 0, $plaintext.Length) }
        finally { $encryptor.Dispose() }
    }
    finally { $aes.Dispose() }
    $envelope = [ordered]@{
        v = 1
        job_id = $JobId
        expires_at = $ExpiresAt
        iv = [Convert]::ToBase64String($Iv)
        ciphertext = [Convert]::ToBase64String($ciphertext)
    }
    $envelope.tag = [Convert]::ToBase64String((Get-Hmac $macKey (Get-EnvelopeMacBytes $envelope)))
    return [PSCustomObject]$envelope
}

function Unprotect-Envelope([byte[]]$MasterKey, $Envelope, [int64]$Now, [string]$ExpectedJobId = "") {
    Assert-EnvelopeShape $Envelope $Now $ExpectedJobId
    try {
        $iv = [Convert]::FromBase64String([string]$Envelope.iv)
        $ciphertext = [Convert]::FromBase64String([string]$Envelope.ciphertext)
        $tag = [Convert]::FromBase64String([string]$Envelope.tag)
    }
    catch { throw "Invalid encrypted envelope." }
    if ($iv.Length -ne 16 -or $ciphertext.Length -eq 0 -or ($ciphertext.Length % 16) -ne 0 -or $tag.Length -ne 32) {
        throw "Invalid encrypted envelope."
    }
    $macKey = Get-DerivedKey $MasterKey "payload-authentication"
    $expectedTag = Get-Hmac $macKey (Get-EnvelopeMacBytes $Envelope)
    if (-not (Compare-Bytes $tag $expectedTag)) { throw "Invalid encrypted envelope." }
    $encryptionKey = Get-DerivedKey $MasterKey "payload-encryption"
    $aes = [Security.Cryptography.Aes]::Create()
    try {
        $aes.KeySize = 256
        $aes.BlockSize = 128
        $aes.Mode = [Security.Cryptography.CipherMode]::CBC
        $aes.Padding = [Security.Cryptography.PaddingMode]::PKCS7
        $aes.Key = $encryptionKey
        $aes.IV = $iv
        $decryptor = $aes.CreateDecryptor()
        try { $plaintext = $decryptor.TransformFinalBlock($ciphertext, 0, $ciphertext.Length) }
        finally { $decryptor.Dispose() }
    }
    catch { throw "Invalid encrypted envelope." }
    finally { $aes.Dispose() }
    try { return (Get-Utf8String $plaintext | ConvertFrom-Json) }
    catch { throw "Invalid encrypted envelope." }
}

function Get-Utf8String([byte[]]$Value) {
    return [Text.Encoding]::UTF8.GetString($Value)
}

function New-RequestHeaders(
    [byte[]]$AuthKey,
    [string]$Method,
    [string]$Path,
    [byte[]]$Body,
    [int64]$Timestamp,
    [string]$Nonce
) {
    if ($AuthKey.Length -ne 32 -or $Path[0] -ne "/" -or $Nonce -notmatch "^[A-Za-z0-9_-]{16,64}$") {
        throw "Invalid request authentication input."
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $bodyHash = ([BitConverter]::ToString($sha.ComputeHash($Body))).Replace("-", "").ToLowerInvariant() }
    finally { $sha.Dispose() }
    $canonical = "{0}`n{1}`n{2}`n{3}`n{4}" -f $Method.ToUpperInvariant(), $Path, $Timestamp, $Nonce, $bodyHash
    $signature = Get-Hmac $AuthKey (Get-Utf8Bytes $canonical)
    return [ordered]@{
        "X-Hephestus-Time" = [string]$Timestamp
        "X-Hephestus-Nonce" = $Nonce
        "X-Hephestus-Signature" = [Convert]::ToBase64String($signature)
    }
}

function Assert-RelayUrl([string]$Url) {
    $uri = $null
    if (-not [Uri]::TryCreate($Url, [UriKind]::Absolute, [ref]$uri)) { throw "Relay URL is invalid." }
    if ($uri.UserInfo -or $uri.Query -or $uri.Fragment -or ($uri.AbsolutePath -ne "/" -and $uri.AbsolutePath -ne "")) {
        throw "Relay URL must not contain credentials, a path, query, or fragment."
    }
    $loopback = $uri.Host -in @("127.0.0.1", "localhost", "::1")
    if ($uri.Scheme -eq "http" -and -not $loopback) { throw "Remote relay URL must use HTTPS." }
    if ($uri.Scheme -notin @("http", "https")) { throw "Relay URL must use HTTP or HTTPS." }
    if (-not $loopback -and $uri.Port -notin @(443, 8000)) { throw "Relay URL must use HTTPS/443 or port 8000." }
}

function Read-KeyFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Secret key file was not found." }
    try { $key = [Convert]::FromBase64String(([IO.File]::ReadAllText($Path)).Trim()) }
    catch { throw "Secret key file is invalid." }
    if ($key.Length -ne 32) { throw "Secret key file must decode to 32 bytes." }
    return $key
}

function Resolve-WorkerKeyPath([string]$Path) {
    $requested = if ($null -ne $Path) { $Path.Trim() } else { "" }
    if ($requested) {
        $candidate = $requested
    }
    elseif ($PSScriptRoot) {
        # Normal .ps1 execution: keep the key beside the worker script.
        $candidate = Join-Path -Path $PSScriptRoot -ChildPath "worker.key"
    }
    else {
        # ISE paste/unsaved-buffer execution has no script root. In that mode
        # the operator is expected to cd to the tool directory first.
        $candidate = Join-Path -Path (Get-Location).Path -ChildPath "worker.key"
    }
    $resolved = [IO.Path]::GetFullPath($candidate)
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "Worker key file was not found at '$resolved'. Put worker.key in the current tool directory or provide -SecretPath."
    }
    return $resolved
}

function Protect-KeyFileAcl([string]$Path) {
    $whoami = Join-Path $env:SystemRoot "System32\whoami.exe"
    $icacls = Join-Path $env:SystemRoot "System32\icacls.exe"
    $identity = (& $whoami /user /fo csv /nh | ConvertFrom-Csv -Header Account, Sid)
    if ($LASTEXITCODE -ne 0 -or $null -eq $identity -or [string]$identity.Sid -notmatch "^S-1-") {
        throw "Could not resolve the current Windows account SID."
    }
    & $icacls $Path /inheritance:r /grant:r "*$($identity.Sid):F" "*S-1-5-18:F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not apply a private ACL to the worker key." }
}

function Invoke-RelayRequest([string]$Method, [string]$Path, $Payload = $null, [int]$Timeout = 35) {
    $body = if ($null -eq $Payload) { "" } else { $Payload | ConvertTo-Json -Compress -Depth 8 }
    $bodyBytes = Get-Utf8Bytes $body
    $headers = New-RequestHeaders $script:RelayAuthKey $Method $Path $bodyBytes (Get-UnixTime) ([Guid]::NewGuid().ToString("N"))
    $arguments = @{
        Uri = $script:RelayBaseUrl + $Path
        Method = $Method
        Headers = $headers
        UseBasicParsing = $true
        TimeoutSec = $Timeout
        ErrorAction = "Stop"
    }
    if ($body.Length -gt 0) {
        $arguments.Body = $bodyBytes
        $arguments.ContentType = "application/json; charset=utf-8"
    }
    try { return Invoke-WebRequest @arguments }
    catch {
        $status = 0
        if ($null -ne $_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
        throw "Relay request failed (HTTP $status)."
    }
}

function Test-CommandPolicy([string]$Command) {
    if (-not $Command -or $Command.Length -gt 4000 -or $Command -match "[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F\u202A-\u202E\u2066-\u2069]") { return $false }
    if ($Command.Contains("...") -or $Command.Contains([string][char]0x2026)) { return $false }
    $tokens = $null
    $parseErrors = $null
    [void][System.Management.Automation.Language.Parser]::ParseInput($Command, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count -gt 0) { return $false }
    # Match each primitive only as a standalone command token: not preceded by
    # '-', '.' or a word char (so '-Format', '.Format', 'Reformat' are safe) and
    # not followed by '-' or a word char (so 'Format-Table', 'formatting' are
    # safe). The real destructive 'format C:' / 'diskpart' / etc. still match.
    # Keep in sync with hephestus/src/policy_engine.py.
    $blocked = @(
        "(?i)(?<![-.\w])(format|diskpart|bcdedit)(?![-\w])",
        "(?i)(?<![-.\w])(reg\s+delete|cipher\s+/w|shutdown|restart-computer)(?![-\w])",
        "(?i)(?<![-.\w])(del|rd|rmdir)\s+/s(?![-\w])"
    )
    foreach ($pattern in $blocked) { if ($Command -match $pattern) { return $false } }
    return $true
}

function Test-ExecutionFresh([int64]$ExpiresAt, [int64]$Now) {
    return $ExpiresAt -gt $Now
}

function Test-RelayJobActive([string]$JobId) {
    try {
        $response = Invoke-RelayRequest "GET" "/v1/jobs/$JobId/active" $null 3
        return [bool](($response.Content | ConvertFrom-Json).active)
    }
    catch { return $false }
}

function Show-CommandApproval([string]$Command, [string]$JobId) {
    Write-Host ""
    Write-Host "LOCAL APPROVAL REQUIRED" -ForegroundColor Black -BackgroundColor Yellow
    Write-Host "[Y] Approve  [N] Reject  [U] Update (same action, different syntax)  [Q] Quit worker" -ForegroundColor Cyan
    while ($true) {
        $answer = (Read-Host "Choose Y, N, U, or Q").Trim().ToLowerInvariant()
        switch ($answer) {
            "y" { return "y" }
            "yes" { return "y" }
            "n" { return "n" }
            "no" { return "n" }
            "u" { return "u" }
            "update" { return "u" }
            "q" { return "q" }
            "quit" { return "q" }
            default { Write-Host "Please enter Y, N, U, or Q." -ForegroundColor Yellow }
        }
    }
}

function Convert-WorkerErrorText([string]$Text) {
    if (-not $Text.StartsWith("#< CLIXML")) { return $Text }
    try {
        $settings = New-Object Xml.XmlReaderSettings
        $settings.DtdProcessing = [Xml.DtdProcessing]::Prohibit
        $settings.XmlResolver = $null
        $reader = [Xml.XmlReader]::Create((New-Object IO.StringReader($Text.Substring(9).Trim())), $settings)
        $document = New-Object Xml.XmlDocument
        $document.XmlResolver = $null
        try { $document.Load($reader) } finally { $reader.Dispose() }
        $lines = @($document.SelectNodes("//*[local-name()='S' and @S='Error']") | ForEach-Object { $_.InnerText })
        $plain = $lines -join ""
        return [regex]::Replace($plain, '_x([0-9A-Fa-f]{4})_', { param($m) [string][char][Convert]::ToInt32($m.Groups[1].Value, 16) })
    }
    catch { return $Text }
}

function Invoke-BoundedCommand([string]$Command, [int]$TimeoutSeconds, [int]$OutputLimit, [string]$JobId = "") {
    $powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $powershell
    $statusTrailer = '$commandSucceeded=$?; if (-not $commandSucceeded) { exit 1 }; exit [int]$LASTEXITCODE'
    # Encode the complete command and status trailer as one PowerShell unit.
    # Unlike line-by-line standard input, this makes parse errors set a
    # non-zero process exit code instead of letting the trailer mask them.
    $encodedCommand = [Convert]::ToBase64String(
        [Text.Encoding]::Unicode.GetBytes('$ErrorActionPreference="Stop"; $LASTEXITCODE=0;' + $Command + "`n" + $statusTrailer)
    )
    $start.Arguments = "-NoLogo -NoProfile -NonInteractive -EncodedCommand $encodedCommand"
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $start
    $job = New-Object Hephestus.ProcessJob
    $started = $false
    try {
    if (-not $process.Start()) { throw "Could not start child PowerShell." }
    $started = $true
    try { $job.AddProcess($process) }
    catch { throw "Could not contain the child process tree." }
    $stdout = New-Object Text.StringBuilder
    $stderr = New-Object Text.StringBuilder
    $stdoutBuffer = New-Object char[] 1024
    $stderrBuffer = New-Object char[] 1024
    $stdoutRead = $process.StandardOutput.ReadAsync($stdoutBuffer, 0, $stdoutBuffer.Length)
    $stderrRead = $process.StandardError.ReadAsync($stderrBuffer, 0, $stderrBuffer.Length)
    $stdoutEof = $false
    $stderrEof = $false
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $reason = ""
    $killed = $false
    $nextCheck = [DateTime]::UtcNow
    while ($true) {
        if ($JobId -and [DateTime]::UtcNow -ge $nextCheck) {
            if (-not (Test-RelayJobActive $JobId)) { $reason = "Session stopped or relay disconnected" }
            $nextCheck = [DateTime]::UtcNow.AddSeconds(1)
        }
        if (-not $stdoutEof -and $stdoutRead.IsCompleted) {
            $count = $stdoutRead.Result
            if ($count -eq 0) { $stdoutEof = $true }
            else {
                $remaining = $OutputLimit - $stdout.Length - $stderr.Length
                if ($remaining -le 0) { $reason = "Output limit reached" }
                else {
                    $keep = [Math]::Min($remaining, $count)
                    [void]$stdout.Append($stdoutBuffer, 0, $keep)
                    if ($keep -lt $count) { $reason = "Output limit reached" }
                    else { $stdoutRead = $process.StandardOutput.ReadAsync($stdoutBuffer, 0, $stdoutBuffer.Length) }
                }
            }
        }
        if (-not $stderrEof -and $stderrRead.IsCompleted) {
            $count = $stderrRead.Result
            if ($count -eq 0) { $stderrEof = $true }
            else {
                $remaining = $OutputLimit - $stdout.Length - $stderr.Length
                if ($remaining -le 0) { $reason = "Output limit reached" }
                else {
                    $keep = [Math]::Min($remaining, $count)
                    [void]$stderr.Append($stderrBuffer, 0, $keep)
                    if ($keep -lt $count) { $reason = "Output limit reached" }
                    else { $stderrRead = $process.StandardError.ReadAsync($stderrBuffer, 0, $stderrBuffer.Length) }
                }
            }
        }
        if (-not $reason -and [DateTime]::UtcNow -ge $deadline) { $reason = "Execution timed out" }
        if ($reason -and -not $killed) {
            $taskkill = Join-Path $env:SystemRoot "System32\taskkill.exe"
            # The child can exit while its final output is being drained.
            # Closing the Job object in finally still contains surviving children.
            if (-not $process.HasExited) {
                try { & $taskkill /PID $process.Id /T /F 2>$null | Out-Null } catch {}
            }
            $killed = $true
        }
        if ($reason) { break }
        if ($process.HasExited -and $stdoutEof -and $stderrEof) { break }
        Start-Sleep -Milliseconds 10
    }
    if (-not $process.HasExited) { [void]$process.WaitForExit(5000) }
    $exitCode = if ($reason) { 124 } else { $process.ExitCode }
    $stdoutText = $stdout.ToString()
    $stderrText = Convert-WorkerErrorText $stderr.ToString()
    if ($reason) {
        # Keep the termination notice inside the combined capture budget.
        $notice = "`r`n" + $reason + "."
        $remaining = [Math]::Max(0, $OutputLimit - $notice.Length)
        $stdoutText = $stdoutText.Substring(0, [Math]::Min($stdoutText.Length, $remaining))
        $remaining -= $stdoutText.Length
        $stderrText = $stderrText.Substring(0, [Math]::Min($stderrText.Length, $remaining)) + $notice
    }
    return [ordered]@{
        status = "approved"
        executed = $true
        exit_code = $exitCode
        stdout = $stdoutText
        stderr = $stderrText
    }
    }
    finally {
        try { $job.Dispose() } catch {}
        if ($started) {
            try {
                if (-not $process.HasExited) { $process.Kill() }
                [void]$process.WaitForExit(2000)
            }
            catch {}
        }
        try { $process.Dispose() } catch {}
    }
}

function Connect-Relay {
    # Tunnel registration can precede public DNS propagation. Retry only the
    # idempotent enrollment heartbeat, never a command or execution result.
    for ($attempt = 1; $attempt -le 10; $attempt++) {
        try {
            Invoke-RelayRequest "POST" "/v1/worker/heartbeat" | Out-Null
            return
        }
        catch {
            if ($attempt -eq 10) {
                throw "Could not connect to the relay. Check DNS/HTTPS access or obtain a fresh connection bundle."
            }
            Write-Host "Waiting for the relay connection ($attempt/10)..."
            Start-Sleep -Seconds 2
        }
    }
}

try {
    if (-not $RelayUrl) {
        $RelayUrl = (Read-Host "Relay URL (https://...trycloudflare.com)").Trim()
    }
    Assert-RelayUrl $RelayUrl
    $SecretPath = Resolve-WorkerKeyPath $SecretPath
    Protect-KeyFileAcl $SecretPath
    $script:MasterKey = Read-KeyFile $SecretPath
    $script:RelayAuthKey = Get-DerivedKey $script:MasterKey "relay-request-authentication"
    $script:RelayBaseUrl = $RelayUrl.TrimEnd("/")

    Connect-Relay
    Write-Host "Remote worker connected to the disposable relay. Press Ctrl+C to stop."
    while ($true) {
        $response = Invoke-RelayRequest "GET" "/v1/jobs/next?wait=5"
        if ([int]$response.StatusCode -eq 204) { continue }
        if ([int]$response.StatusCode -ne 200) { throw "Unexpected relay response." }
        $envelope = $response.Content | ConvertFrom-Json
        try {
            $answer = ""
            $executionStarted = $false
            $payload = Unprotect-Envelope $script:MasterKey $envelope (Get-UnixTime)
            $jobId = [string]$envelope.job_id
            if ([string]$payload.kind -ne "command" -or -not (Test-CommandPolicy ([string]$payload.command))) {
                $result = [ordered]@{ status = "error"; executed = $false; exit_code = 126; stdout = ""; stderr = "Command failed remote-worker validation" }
            }
            else {
                $command = [string]$payload.command
                Write-Host ""
                Write-Host "Proposed command for this REMOTE PC:" -ForegroundColor Cyan
                Write-Host $command -ForegroundColor Yellow
                $answer = Show-CommandApproval $command $jobId
                if ($answer -eq "cancelled") { break }
                if ($answer -eq "y" -or $answer -eq "yes") {
                    if (-not (Test-RelayJobActive $jobId)) { break }
                    if (-not (Test-ExecutionFresh ([int64]$envelope.expires_at) (Get-UnixTime))) {
                        $result = [ordered]@{ status = "error"; executed = $false; exit_code = 126; stdout = ""; stderr = "Command expired before approval" }
                    }
                    else {
                        $executionStarted = $true
                        $result = Invoke-BoundedCommand $command $CommandTimeoutSeconds $MaxOutputChars $jobId
                    }
                }
                elseif ($answer -eq "u") {
                    # Update: the operator describes a surgical edit; the AI applies
                    # exactly that to this command and re-sends it this iteration.
                    Write-Host "Describe the edit to make to this command." -ForegroundColor Cyan
                    Write-Host "Examples: 'change the message to hello'  |  'keep it the same but obfuscate the content'" -ForegroundColor DarkGray
                    $instruction = (Read-Host "Edit (blank = just use a different syntax)").Trim()
                    if ($instruction.Length -gt 1000) { $instruction = $instruction.Substring(0, 1000) }
                    $result = [ordered]@{ status = "update"; executed = $false; exit_code = 125; stdout = ""; stderr = "Remote operator requested an edit to the command"; instruction = $instruction }
                }
                elseif ($answer -eq "q") {
                    # Quitting the worker is a different decision from rejecting one command.
                    $result = [ordered]@{ status = "quit"; executed = $false; exit_code = 130; stdout = ""; stderr = "Remote operator quit the worker; the command was not executed" }
                }
                else {
                    $result = [ordered]@{ status = "rejected"; executed = $false; exit_code = 125; stdout = ""; stderr = "Remote operator rejected the command" }
                }
            }
            $resultEnvelope = Protect-Payload $script:MasterKey $result $jobId ((Get-UnixTime) + 60) (Get-UnixTime)
            Invoke-RelayRequest "POST" "/v1/jobs/$jobId/result" $resultEnvelope | Out-Null
            Write-Host "Result returned to the main PC."
            if ($answer -eq "q") { break }
        }
        catch {
            if ($executionStarted) { Write-Warning "Command execution started, but its result could not be returned." }
            else { Write-Warning "Rejected an invalid encrypted job; no command was executed." }
        }
    }
}
catch [System.Management.Automation.PipelineStoppedException] {
    Write-Host "Worker stopped."
}
catch {
    $message = "Worker stopped: " + $_.Exception.Message
    Write-Host $message -ForegroundColor Red
    if ($null -ne (Get-Variable -Name psISE -ValueOnly -ErrorAction SilentlyContinue)) {
        return
    }
    throw $message
}

