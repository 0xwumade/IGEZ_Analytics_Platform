@echo off
powershell -ExecutionPolicy Bypass -Command ^
  "$TOOL = 'C:\CBC_001\mongodb-database-tools-windows-x86_64-100.15.0\bin\bsondump.exe';" ^
  "$SOURCE = 'C:\backup\Paperless_app_prod';" ^
  "$DEST = 'C:\backup\json';" ^
  "New-Item -ItemType Directory -Force -Path $DEST | Out-Null;" ^
  "Get-ChildItem \"$SOURCE\*.bson\" | ForEach-Object { $out = Join-Path $DEST ($_.BaseName + '.json'); & $TOOL $_.FullName | Set-Content -Path $out -Encoding UTF8; Write-Host \"Converted: $($_.Name)\" };" ^
  "Write-Host 'All done!'"
pause