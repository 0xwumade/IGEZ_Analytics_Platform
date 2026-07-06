from bson import ObjectId

cursors = {
    "Subsidiary":           "68ac761e1ae9cc8e949692b7",
    "Leave_Request":        "6a477a68ce62ada590ea051c",
    "ExpenseClaim":         "6a47a084ce62ada590ea0529",
    "CashAdvance":          "6a467c52ce62ada590ea0501",
    "RequestToPaySupplier": "6a47b243ce62ada590ea0576",
}
for col, oid in cursors.items():
    ts = ObjectId(oid).generation_time
    print(f"{col}: last synced up to {ts.strftime('%Y-%m-%d %H:%M UTC')}")
