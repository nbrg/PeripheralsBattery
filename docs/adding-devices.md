# Adding a device

## 1. Is it already covered?

Run `PeripheralsBattery.exe --probe` (or `python -m peribatt --probe`). It lists
the HID collections of known gaming brands, what every provider tried, and what
it read. Bluetooth devices appear whenever Windows shows their battery under
**Settings > Bluetooth & devices**.

## 2. Request/reply devices: write a recipe

Most dongle headsets answer a fixed request with a report that holds the level at
a fixed byte. Put a `recipes.json` in `%APPDATA%\PeripheralsBattery` (it is merged
with the bundled one, and wins on a clash):

```json
[
  {
    "name": "My Headset",
    "kind": "headset",
    "vendor_id": "0x1234",
    "product_ids": ["0xabcd"],
    "match": {"usage_page": "0xff00"},
    "steps": [
      {"write": "06 ff bb 02", "pad_to": 52, "expect": "06 ff bb 02",
       "level": {"byte": 7}},
      {"write": "06 ff bb 03", "pad_to": 52, "expect": "06 ff bb 03",
       "charging": {"byte": 4, "in": [1]}}
    ]
  }
]
```

| Key | Meaning |
|---|---|
| `match` | pick the HID collection: any of `usage_page`, `usage`, `interface` |
| `write` | bytes to send (first byte = report id, `00` if the device has none) |
| `pad_to` | pad the request with zeros to this length |
| `expect` / `expect_at` | only accept replies with these bytes at this offset |
| `feature` | `true` to use feature reports instead of output/input reports |
| `level` | `{"byte": n}`, optional `"mask"`, `"shift"`, `"min"`, `"max"` (scaled to 0–100) |
| `charging` / `offline` / `muted` | `{"byte": n, "in": [values that mean yes]}` |

Restart the app (or use **Refresh now**) and check `--once`.

## 3. Everything else: write a provider

A provider is any object with `poll() -> list[Reading]` (see `peribatt/model.py`).
Add it in `peribatt/sources.py`, and add a fake device in `tests/fakes.py`.
Event-driven devices can also implement `poll_fast()` for cheap status checks
every few seconds and call `on_change(reading)` when a report arrives
(see `peribatt/hyperx.py`).
