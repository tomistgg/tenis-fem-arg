param(
  [Parameter(Mandatory = $true, Position = 0)]
  [string]$Id
)

Set-Location $PSScriptRoot
. "$PSScriptRoot\watch_wta_config.ps1"

$TaskName = "WTA Draw Watcher M"

$argsList = @(
  "$PSScriptRoot\check_draw.py",
  "--id", $Id,
  "--draw", "M",
  "--email-to", $EmailTo,
  "--smtp-host", $SmtpHost,
  "--smtp-port", $SmtpPort,
  "--smtp-user", $SmtpUser,
  "--smtp-pass-file", $SmtpPassFile,
  "--email-from-name", $EmailFromName,
  "--no-pdf-ok",
  "--stop-task-on-email",
  "--task-name", $TaskName
)
if ($AttachPages) { $argsList += @("--email-attach-pages") }

& $PythonExe @argsList
exit $LASTEXITCODE
