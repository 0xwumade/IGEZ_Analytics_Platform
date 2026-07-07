$TOOL = "C:\Igez\mongodb-database-tools-windows-x86_64-100.15.0\bin"
$URI  = "mongodb+srv://Damilola:H868xEu5jrewT2Da@cluster0.zpxsdyo.mongodb.net/Paperless_app_prod"
$DATE = Get-Date -Format "yyyy-MM-dd"
$BKUP = "C:\backup\$DATE"
$JSON = "$BKUP\json"
$LOG  = "C:\backup\backup_log.txt"

New-Item -ItemType Directory -Force -Path $BKUP | Out-Null
New-Item -ItemType Directory -Force -Path $JSON | Out-Null

# Step 1: Dump from MongoDB
Write-Host "Running mongodump..."
& "$TOOL\mongodump.exe" --uri=$URI --out=$BKUP

# Step 2: Convert BSON to JSON using Python
Write-Host ""
Write-Host "Converting BSON files to JSON..."
python "C:\CBC_001\bson_to_json.py" "$BKUP\Paperless_app_prod" "$JSON"

Add-Content -Path $LOG -Value "Backup completed: $(Get-Date)"
Write-Host ""
Write-Host "Backup complete! Files saved to $BKUP"
