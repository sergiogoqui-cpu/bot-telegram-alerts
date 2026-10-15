"""Dry run with a fake transport (no network, no credentials):  python examples/basic.py"""

from bot_telegram_alerts import TelegramAlerter


def fake_transport(url, payload):
    print("->", payload["text"])
    return 200, {"ok": True}


alerts = TelegramAlerter("FAKE:token", 1, transport=fake_transport, dedup_window=60)
for _ in range(3):
    print(alerts.send("Disk almost full", key="disk"))
alerts.send("Another alert")
print(alerts.stats)