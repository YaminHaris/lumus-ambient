#!/usr/bin/env python3
"""Watches logind PrepareForSleep over system D-Bus to trigger dreaming on lid-close
and start a fresh agy session on wake."""

import os
import urllib.request
import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

BASE_URL = "http://127.0.0.1:47891"


def post(path: str, timeout: int = 30):
    req = urllib.request.Request(f"{BASE_URL}{path}", data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "ignore")
    except Exception as e:
        print(f"POST {path} failed: {e}", flush=True)
        return None


class SleepWatcher:
    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self.inhibit_fd = None
        self.acquire_inhibit()
        self.bus.signal_subscribe(
            "org.freedesktop.login1",
            "org.freedesktop.login1.Manager",
            "PrepareForSleep",
            "/org/freedesktop/login1",
            None,
            Gio.DBusSignalFlags.NONE,
            self.on_prepare_for_sleep,
        )

    def acquire_inhibit(self):
        if self.inhibit_fd is not None:
            return
        try:
            res, fd_list = self.bus.call_with_unix_fd_list_sync(
                "org.freedesktop.login1",
                "/org/freedesktop/login1",
                "org.freedesktop.login1.Manager",
                "Inhibit",
                GLib.Variant("(ssss)", ("sleep", "agy-ambient", "Dreaming on session end", "delay")),
                GLib.VariantType("(h)"),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
                None,
            )
            idx = res.unpack()[0]
            fds = fd_list.steal_fds()
            self.inhibit_fd = fds[idx]
            print(f"Acquired sleep delay inhibitor fd={self.inhibit_fd}", flush=True)
        except Exception as e:
            print(f"Failed to acquire sleep inhibitor: {e}", flush=True)

    def release_inhibit(self):
        if self.inhibit_fd is not None:
            try:
                os.close(self.inhibit_fd)
                print(f"Released sleep inhibitor fd={self.inhibit_fd}", flush=True)
            except OSError:
                pass
            self.inhibit_fd = None

    def on_prepare_for_sleep(self, conn, sender, path, iface, signal, params):
        sleeping = bool(params.unpack()[0])
        if sleeping:
            print("PrepareForSleep(True): triggering dream...", flush=True)
            post("/sleep", timeout=28)
            self.release_inhibit()
        else:
            print("PrepareForSleep(False): laptop woke up, starting fresh session...", flush=True)
            self.acquire_inhibit()
            post("/wake", timeout=10)


def main():
    watcher = SleepWatcher()
    print("agy-ambient sleep_watcher running", flush=True)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
