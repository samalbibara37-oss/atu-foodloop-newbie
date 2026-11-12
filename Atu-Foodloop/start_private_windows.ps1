Set-Location $PSScriptRoot
if (Get-Command py -ErrorAction SilentlyContinue) { py -3 launch_private.py } else { python launch_private.py }
