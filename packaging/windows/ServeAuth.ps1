#Requires -Version 5.1
# OpenCode serve basic auth for the start scripts.
# Process environment wins over .env, including an empty value.
# ASCII-only. Do not print the password.

$script:DotEnvBs = [char]92
$script:DotEnvDq = [char]34
$script:DotEnvSq = [char]39
$script:DotEnvDouble = ([string]$script:DotEnvBs) + ([string]$script:DotEnvSq) + ([string]$script:DotEnvDq) + "abfnrtv"
$script:DotEnvSingle = ([string]$script:DotEnvBs) + ([string]$script:DotEnvSq)

function ConvertFrom-DotEnvEscapes {
    param([string]$Text, [string]$Special)
    $sb = New-Object System.Text.StringBuilder
    $i = 0
    while ($i -lt $Text.Length) {
        $c = $Text[$i]
        if ($c -eq $script:DotEnvBs -and ($i + 1) -lt $Text.Length) {
            $n = $Text[$i + 1]
            if ($Special.IndexOf($n) -ge 0) {
                if ($n -eq $script:DotEnvBs) { [void]$sb.Append($script:DotEnvBs) }
                elseif ($n -eq $script:DotEnvSq) { [void]$sb.Append($script:DotEnvSq) }
                elseif ($n -eq $script:DotEnvDq) { [void]$sb.Append($script:DotEnvDq) }
                elseif ($n -eq "a") { [void]$sb.Append([char]7) }
                elseif ($n -eq "b") { [void]$sb.Append([char]8) }
                elseif ($n -eq "f") { [void]$sb.Append([char]12) }
                elseif ($n -eq "n") { [void]$sb.Append([char]10) }
                elseif ($n -eq "r") { [void]$sb.Append([char]13) }
                elseif ($n -eq "t") { [void]$sb.Append([char]9) }
                elseif ($n -eq "v") { [void]$sb.Append([char]11) }
                $i += 2
                continue
            }
        }
        [void]$sb.Append($c)
        $i++
    }
    return $sb.ToString()
}

function ConvertFrom-DotEnvQuoted {
    param([string]$Text, [ref]$Ok)
    $Ok.Value = $false
    if ($Text.Length -lt 2) { return "" }
    $q = $Text[0]
    if ($q -ne $script:DotEnvDq -and $q -ne $script:DotEnvSq) { return "" }
    $buf = New-Object System.Text.StringBuilder
    $esc = $false
    for ($i = 1; $i -lt $Text.Length; $i++) {
        $c = $Text[$i]
        if ($esc) {
            [void]$buf.Append($script:DotEnvBs)
            [void]$buf.Append($c)
            $esc = $false
            continue
        }
        if ($c -eq $script:DotEnvBs) {
            $esc = $true
            continue
        }
        if ($c -eq $q) {
            $special = $script:DotEnvSingle
            if ($q -eq $script:DotEnvDq) { $special = $script:DotEnvDouble }
            $Ok.Value = $true
            return (ConvertFrom-DotEnvEscapes -Text $buf.ToString() -Special $special)
        }
        [void]$buf.Append($c)
    }
    return ""
}

function ConvertFrom-DotEnvUnquoted {
    param([string]$Text)
    $cut = [regex]::Replace($Text, "\s+#.*$", "")
    return $cut.TrimEnd()
}

# ${NAME} and ${NAME:-default} only. Earlier file keys win over the process
# environment. Substituted text is not expanded again.
function Resolve-DotEnvReference {
    param(
        [AllowEmptyString()][string]$Name,
        [System.Collections.Hashtable]$Known,
        [bool]$HasDefault,
        [AllowEmptyString()][string]$DefaultValue
    )
    if ($null -ne $Known -and $Known.ContainsKey($Name)) {
        return [string]$Known[$Name]
    }
    if ($Name -cmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
        try {
            $item = Get-Item -LiteralPath ("Env:" + $Name) -ErrorAction SilentlyContinue
            if ($null -ne $item) {
                return [string]$item.Value
            }
        } catch {
            # The provider rejected the name. Treat it as unset.
        }
    }
    if ($HasDefault) { return [string]$DefaultValue }
    return ""
}

function Expand-DotEnvReferences {
    param(
        [AllowEmptyString()][string]$Text,
        [System.Collections.Hashtable]$Known
    )
    if ([string]::IsNullOrEmpty($Text)) { return "" }
    $sb = New-Object System.Text.StringBuilder
    $i = 0
    $n = $Text.Length
    while ($i -lt $n) {
        $matched = $false
        if ($Text[$i] -eq '$' -and ($i + 1) -lt $n -and $Text[$i + 1] -eq '{') {
            $j = $i + 2
            $nameSb = New-Object System.Text.StringBuilder
            while ($j -lt $n) {
                $c = $Text[$j]
                if ($c -eq '}') {
                    $piece = Resolve-DotEnvReference -Name $nameSb.ToString() -Known $Known -HasDefault $false -DefaultValue ""
                    [void]$sb.Append([string]$piece)
                    $i = $j + 1
                    $matched = $true
                    break
                }
                if ($c -eq ':') {
                    $closed = $false
                    if (($j + 1) -lt $n -and $Text[$j + 1] -eq '-') {
                        $k = $j + 2
                        $defSb = New-Object System.Text.StringBuilder
                        while ($k -lt $n) {
                            $d = $Text[$k]
                            if ($d -eq '}') {
                                $closed = $true
                                break
                            }
                            [void]$defSb.Append($d)
                            $k++
                        }
                        if ($closed) {
                            $piece = Resolve-DotEnvReference -Name $nameSb.ToString() -Known $Known -HasDefault $true -DefaultValue $defSb.ToString()
                            [void]$sb.Append([string]$piece)
                            $i = $k + 1
                            $matched = $true
                        }
                    }
                    break
                }
                [void]$nameSb.Append($c)
                $j++
            }
        }
        if ($matched) { continue }
        [void]$sb.Append($Text[$i])
        $i++
    }
    return $sb.ToString()
}

function Read-DotEnvKey {
    param([string]$Path, [string]$Key)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    $text = [System.Text.Encoding]::UTF8.GetString($bytes)
    if ($text.Length -gt 0 -and [int]$text[0] -eq 0xFEFF) {
        $text = $text.Substring(1)
    }
    $known = New-Object System.Collections.Hashtable -ArgumentList ([StringComparer]::Ordinal)
    $found = $false
    foreach ($raw in ($text -split "`n", -1)) {
        $line = $raw.TrimEnd([char]13).TrimStart()
        if ($line.Length -eq 0 -or $line.StartsWith("#")) { continue }
        if ($line.Length -gt 6 -and $line.Substring(0, 6) -eq "export") {
            if ([char]::IsWhiteSpace($line[6])) {
                $line = $line.Substring(6).TrimStart()
            }
        }
        $eq = $line.IndexOf([char]61)
        if ($eq -lt 1) { continue }
        $name = $line.Substring(0, $eq).Trim()
        $rest = $line.Substring($eq + 1).TrimStart()
        $decoded = ""
        if ($rest.Length -gt 0 -and ($rest[0] -eq $script:DotEnvDq -or $rest[0] -eq $script:DotEnvSq)) {
            $ok = $false
            $parsed = ConvertFrom-DotEnvQuoted -Text $rest -Ok ([ref]$ok)
            if (-not $ok) { continue }
            $decoded = [string]$parsed
        } else {
            $decoded = [string](ConvertFrom-DotEnvUnquoted $rest)
        }
        $expanded = [string](Expand-DotEnvReferences -Text $decoded -Known $known)
        $known[$name] = $expanded
        if ([string]::Equals($name, $Key, [System.StringComparison]::Ordinal)) {
            $found = $true
        }
    }
    if (-not $found) {
        return [pscustomobject]@{ Found = $false; Value = "" }
    }
    return [pscustomobject]@{ Found = $true; Value = [string]$known[$Key] }
}

function Import-OpencodeServeAuth {
    param([Parameter(Mandatory = $true)][string]$ProjectDir)
    $path = Join-Path $ProjectDir ".env"
    foreach ($key in @("OPENCODE_SERVER_PASSWORD", "OPENCODE_SERVER_USERNAME")) {
        if (Test-Path -LiteralPath ("Env:" + $key)) { continue }
        $got = Read-DotEnvKey -Path $path -Key $key
        if ($got.Found) {
            Set-Item -LiteralPath ("Env:" + $key) -Value ([string]$got.Value)
        }
    }
}

function Get-OpencodeServeAuthorization {
    $password = [string]$env:OPENCODE_SERVER_PASSWORD
    if ($password -eq "") { return "" }
    $username = [string]$env:OPENCODE_SERVER_USERNAME
    if ($username -eq "") { $username = "opencode" }
    $pair = $username + ":" + $password
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($pair)
    $token = [Convert]::ToBase64String($bytes)
    return ("Basic " + $token)
}
