# Configure a narrowly scoped inbound port for the isolated WSL test stack.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$GitLabAddress,
    [Parameter(Mandatory = $true)]
    [string]$LocalAddress,
    [ValidateRange(1024, 65535)]
    [int]$Port = 18080,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$brainPrincipal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $brainPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from an administrator PowerShell."
}
foreach ($brainAddress in @($GitLabAddress, $LocalAddress)) {
    $brainParsedAddress = $null
    if (-not [System.Net.IPAddress]::TryParse($brainAddress, [ref]$brainParsedAddress)) {
        throw "Only explicit IP addresses are accepted."
    }
}

$brainRuleName = "OBC-Brain-Local-$Port"
$brainDisplayName = "OBC Brain local GitLab test TCP $Port"
$brainSources = @($GitLabAddress, $LocalAddress) | Select-Object -Unique
$brainHostRule = Get-NetFirewallRule -Name $brainRuleName -ErrorAction SilentlyContinue
$brainWslRule = Get-NetFirewallHyperVRule -Name $brainRuleName -ErrorAction SilentlyContinue
foreach ($brainExisting in @($brainHostRule, $brainWslRule)) {
    if ($brainExisting -and $brainExisting.DisplayName -ne $brainDisplayName) {
        throw "An unrelated rule already owns the requested name."
    }
}

if ($Remove) {
    if ($brainHostRule) { Remove-NetFirewallRule -Name $brainRuleName }
    if ($brainWslRule) { Remove-NetFirewallHyperVRule -Name $brainRuleName }
    Write-Output "Removed isolated Brain test rules."
    exit 0
}
if ($brainHostRule -or $brainWslRule) {
    throw "Test rules already exist; remove them explicitly before changing the scope."
}

$brainCreatedWslRule = $false
$brainCreatedHostRule = $false
try {
    New-NetFirewallHyperVRule -Name $brainRuleName -DisplayName $brainDisplayName `
        -Direction Inbound -VMCreatorId "{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}" `
        -Protocol TCP -LocalAddresses $LocalAddress -LocalPorts $Port `
        -RemoteAddresses $brainSources -Action Allow | Out-Null
    $brainCreatedWslRule = $true
    New-NetFirewallRule -Name $brainRuleName -DisplayName $brainDisplayName `
        -Direction Inbound -Action Allow -Protocol TCP -LocalAddress $LocalAddress `
        -LocalPort $Port -RemoteAddress $brainSources -Profile Any | Out-Null
    $brainCreatedHostRule = $true
} catch {
    # Roll back only rules created by this invocation if the second operation fails.
    if ($brainCreatedHostRule) { Remove-NetFirewallRule -Name $brainRuleName -ErrorAction SilentlyContinue }
    if ($brainCreatedWslRule) { Remove-NetFirewallHyperVRule -Name $brainRuleName -ErrorAction SilentlyContinue }
    throw
}
Write-Output "Enabled TCP $Port only for the configured GitLab and local IP addresses."
