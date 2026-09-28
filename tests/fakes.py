"""Fake HID devices that speak just enough of each protocol for the tests."""
from __future__ import annotations

import time
from collections import deque
from typing import Dict, List, Optional


class FakeApi:
    """Stands in for ``hidio.HidApi``: a list of enumerate() dicts, and a
    factory per path that returns the handle to use."""

    def __init__(self):
        self.infos: List[dict] = []
        self.handles: Dict[bytes, object] = {}
        self.opened: List[bytes] = []

    def add(self, vid, pid, path, handle, usage_page=0, usage=0, interface=0, product=""):
        self.infos.append({"vendor_id": vid, "product_id": pid, "path": path,
                           "usage_page": usage_page, "usage": usage,
                           "interface_number": interface, "product_string": product})
        self.handles[path] = handle

    def enumerate(self, vid):
        return [d for d in self.infos if vid in (0, d["vendor_id"])]      # 0 = everything, like hidapi

    def open(self, path):
        h = self.handles[path]
        if isinstance(h, Exception):
            raise h
        self.opened.append(path)
        return h


class QueueHandle:
    """Reads come from a queue; writes are recorded (and may queue replies)."""

    def __init__(self):
        self.inbox: deque = deque()
        self.written: List[bytes] = []
        self.closed = False

    def write(self, data):
        self.written.append(bytes(data))
        self.on_write(bytes(data))
        return len(data)

    def on_write(self, data: bytes):
        pass

    def read(self, n, timeout_ms=0):
        if self.inbox:
            return list(self.inbox.popleft())
        if timeout_ms and not self.closed:
            time.sleep(min(timeout_ms, 10) / 1000)     # behave like a blocking read, briefly
        return []

    def close(self):
        self.closed = True


class FakeClock:
    def __init__(self, step=0.05):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


# --- Logitech HID++ ---------------------------------------------------------

class FakeLogiDevice:
    def __init__(self, name="PRO Wireless", dev_type=3, unit=b"\x1a\x2b\x3c\x4d",
                 battery_feature=0x1001, battery=(0x0F, 0xA0, 0x00), online=True):
        self.name = name.encode()
        self.dev_type = dev_type
        self.unit = unit
        self.battery_feature = battery_feature
        self.battery = list(battery)
        self.online = online
        # feature id -> index as a real device would assign them
        self.features = {0x0000: 0, 0x0003: 2, 0x0005: 3, battery_feature: 6}
        self.calls = 0


class FakeHidppChannel(QueueHandle):
    """The long HID++ collection of a receiver (slots) or a wired device (0xFF)."""

    def __init__(self, devices: Dict[int, FakeLogiDevice], short: Optional[QueueHandle] = None):
        super().__init__()
        self.devices = devices
        self.short = short or self

    def on_write(self, req: bytes):
        _, idx, fi, fnsw = req[:4]
        params = req[4:]
        dev = self.devices.get(idx)
        if dev is None:                          # empty slot: HID++ 1.0 unknown device
            self.short.inbox.append(bytes([0x10, idx, 0x8F, fi, fnsw, 0x08, 0x00]))
            return
        if not dev.online:                       # paired but switched off
            self.short.inbox.append(bytes([0x10, idx, 0x8F, fi, fnsw, 0x09, 0x00]))
            return
        dev.calls += 1
        fn = fnsw >> 4
        index_to_feature = {v: k for k, v in dev.features.items()}
        feature = index_to_feature.get(fi)
        out: List[int] = []
        if feature == 0x0000 and fn == 1:                # ping: protocol 4.2, echo
            out = [4, 2, params[2]]
        elif feature == 0x0000 and fn == 0:
            fid = (params[0] << 8) | params[1]
            out = [dev.features.get(fid, 0)]
        elif feature == 0x0005 and fn == 0:
            out = [len(dev.name)]
        elif feature == 0x0005 and fn == 1:
            out = list(dev.name[params[0]:params[0] + 16])
        elif feature == 0x0005 and fn == 2:
            out = [dev.dev_type]
        elif feature == 0x0003 and fn == 0:
            out = [1, *dev.unit]
        elif feature == dev.battery_feature:
            out = dev.battery
        else:                                    # HID++ 2.0 error: invalid function
            self.inbox.append(bytes([0x11, idx, 0xFF, fi, fnsw, 0x07] + [0] * 14))
            return
        # unrelated notification first, to prove replies are matched properly
        self.inbox.append(bytes([0x11, idx, 0x04, 0x00] + [0] * 16))
        self.inbox.append(bytes([0x11, idx, fi, fnsw, *out] + [0] * (16 - len(out))))


# --- Razer ------------------------------------------------------------------

class FakeRazer:
    def __init__(self, tid=0x1F, raw=153, charging=0, asleep=False, supported=True):
        self.tid = tid
        self.raw = raw
        self.charging = charging
        self.asleep = asleep
        self.supported = supported
        self.reply = bytes(91)
        self.sent: List[bytes] = []
        self.closed = False

    def send_feature_report(self, data):
        req = bytes(data)[1:]
        self.sent.append(req)
        msg = bytearray(req)
        if not self.supported:
            msg[0] = 0x05
        elif req[1] != self.tid:
            msg[0] = 0x03
        elif self.asleep:
            msg[0] = 0x04
        else:
            msg[0] = 0x02
            msg[9] = self.raw if req[7] == 0x80 else self.charging
        self.reply = b"\x00" + bytes(msg)
        return len(data)

    def get_feature_report(self, report_id, n):
        return list(self.reply)

    def close(self):
        self.closed = True
