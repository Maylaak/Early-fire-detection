"""Automatic credentials for a single-user Windows demonstration.

DPAPI protects the local file. The secret never enters source/config files,
command-line arguments, browser configuration or terminal output.
"""
import os
from pathlib import Path
import re
import subprocess


def local_operator_token():
    if os.name != 'nt':
        raise RuntimeError('Automatic local-demo authorization currently requires Windows.')
    legacy = Path.home() / 'Documents/Codex/wildfire-local-runtime/operator-token.xml'
    path = legacy if legacy.exists() else Path(os.environ['LOCALAPPDATA']) / 'EarlyFireDetection/operator-token.xml'
    env = dict(os.environ, WILDFIRE_CREDENTIAL_FILE=str(path))
    script = r"""
$ErrorActionPreference='Stop'
$mutex=[Threading.Mutex]::new($false, 'Local\WildfireLocalOperatorCredentials')
$locked=$false
try {
  $locked=$mutex.WaitOne(15000)
  if (!$locked) { throw 'Credential lock unavailable' }
  $file=$env:WILDFIRE_CREDENTIAL_FILE
  if (!(Test-Path -LiteralPath $file)) {
    New-Item -ItemType Directory -Force -Path (Split-Path $file) | Out-Null
    $bytes=New-Object byte[] 32
    $rng=[Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes); $rng.Dispose()
    $value=[Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
    ConvertTo-SecureString $value -AsPlainText -Force | Export-Clixml -LiteralPath $file
  }
  $secure=Import-Clixml -LiteralPath $file
  [Console]::Out.Write([Net.NetworkCredential]::new('', $secure).Password)
} finally { if ($locked) {$mutex.ReleaseMutex()}; $mutex.Dispose() }
"""
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                env=env, capture_output=True, text=True, timeout=25,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('Local demo authorization could not be loaded.') from None
    token = result.stdout.strip()
    if result.returncode or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', token):
        raise RuntimeError('Local demo authorization could not be loaded.')
    return token
