# Usage (from this folder):  .\run-testnet.ps1 status
# Prompts for the agent key once per PowerShell window; never writes it to disk.
if (-not $env:HL_AGENT_PRIVATE_KEY) {
    $sec = Read-Host "Paste API wallet private key (input hidden)" -AsSecureString
    $env:HL_AGENT_PRIVATE_KEY = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
}
if (Test-Path .\.venv\Scripts\Activate.ps1) { . .\.venv\Scripts\Activate.ps1 }
tripwire --config tripwire.yaml @args
