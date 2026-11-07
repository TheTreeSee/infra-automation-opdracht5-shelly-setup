#!/usr/bin/env python3
"""
Configure a Shelly smart plug right after a factory reset.

After a reset the plug is an open Wi-Fi access point (SSID like
"shellyplusplugs-<mac>") and answers on http://192.168.33.1. Connect this PC to
that AP (or use --join-ap) and run the script.

What gets configured
  * device name                <familienaam>-<voornaam>-Outlet1 (or --name)
  * relay state after reboot   off
  * LEDs                       all off
  * MQTT                       broker 172.23.83.254, topic prefix = the name
  * max allowed load           2200 W
  * unused services off        BLE, outbound websocket, Matter (if present),
                               CoIoT (Gen1), cloud unless --cloud
  * Wi-Fi                      join "Howest-IoT", then the plug's own AP is
                               switched off and the plug reboots

Supports Gen2/Gen3/Gen4 plugs (RPC API) and Gen1 Plug/Plug S (HTTP settings API);
the generation is detected from /shelly.

Usage
    python3 shelly_setup.py --wifi-password-prompt
    python3 shelly_setup.py --name Loccufier-Guus-Outlet2 --cloud
    python3 shelly_setup.py --join-ap --dry-run
"""

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

FAMILY_NAME = "Loccufier"
FIRST_NAME = "Guus"

DEFAULT_HOST = "192.168.33.1"
DEFAULT_NAME = f"{FAMILY_NAME}-{FIRST_NAME}-Outlet1"
DEFAULT_MQTT = "172.23.83.254:1883"
DEFAULT_SSID = "Howest-IoT"
MAX_POWER_W = 2200
TIMEOUT = 8


class ShellyError(Exception):
    pass


def http_get(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raise ShellyError(f"GET {url} -> HTTP {exc.code}: {exc.read().decode(errors='replace')}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ShellyError(f"cannot reach {url}: {exc}") from exc


def http_post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raise ShellyError(f"POST {url} -> HTTP {exc.code}: {exc.read().decode(errors='replace')}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ShellyError(f"cannot reach {url}: {exc}") from exc


class Step:
    """One configuration call, kept so --dry-run can show the whole plan."""

    def __init__(self, title: str, call, may_disconnect: bool = False):
        self.title = title
        self.call = call
        self.may_disconnect = may_disconnect  # Wi-Fi changes / reboot can drop our connection


class Gen2Plug:
    """Gen2+ devices: JSON-RPC over HTTP POST /rpc."""

    def __init__(self, host: str, info: dict):
        self.base = f"http://{host}"
        self.info = info
        self._id = 0
        self._methods: set[str] | None = None

    def rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        reply = http_post_json(f"{self.base}/rpc", {"id": self._id, "method": method, "params": params or {}})
        if "error" in reply:
            raise ShellyError(f"{method}: {reply['error'].get('message', reply['error'])}")
        return reply.get("result", {})

    def has(self, method: str) -> bool:
        if self._methods is None:
            self._methods = set(self.rpc("Shelly.ListMethods").get("methods", []))
        return method in self._methods

    def set_config(self, component: str, config: dict, **extra) -> dict:
        result = self.rpc(f"{component}.SetConfig", {**extra, "config": config})
        if result.get("restart_required"):
            print(f"    ({component} needs a restart - done at the end)")
        return result

    def steps(self, args) -> list[Step]:
        return [
            Step(f"device name = {args.name}", lambda: self.set_config("Sys", {"device": {"name": args.name}})),
            Step(
                f"relay: off after power-up, max {MAX_POWER_W} W",
                lambda: self.set_config("Switch", {"name": args.name, "initial_state": "off", "power_limit": MAX_POWER_W}, id=0),
            ),
            Step("LEDs off", self.leds_off),
            Step(
                f"MQTT -> {args.mqtt} topic prefix '{args.topic}'",
                lambda: self.set_config("MQTT", {
                    "enable": True,
                    "server": args.mqtt,
                    "client_id": args.topic,
                    "topic_prefix": args.topic,
                    "rpc_ntf": True,
                    "status_ntf": True,
                }),
            ),
            Step(f"cloud {'on' if args.cloud else 'off'}", lambda: self.set_config("Cloud", {"enable": args.cloud})),
            Step("Bluetooth off", lambda: self.optional_set("BLE", {"enable": False})),
            Step("outbound websocket off", lambda: self.optional_set("Ws", {"enable": False})),
            Step("Matter off (if supported)", lambda: self.optional_set("Matter", {"enable": False})),
            Step("read back the configuration", lambda: print(json.dumps(self.read_back(), indent=2))),
            Step(
                f"Wi-Fi client -> {args.ssid}" + ("" if args.keep_ap else ", own access point off"),
                lambda: self.set_config("WiFi", {
                    "sta": {"enable": True, "ssid": args.ssid, "pass": args.wifi_password, "ipv4mode": "dhcp"},
                    "ap": {"enable": args.keep_ap},
                }),
                may_disconnect=True,
            ),
            Step("reboot", lambda: self.rpc("Shelly.Reboot"), may_disconnect=True),
        ]

    def leds_off(self):
        # Plus/Gen3 plugs expose their ring/LEDs through PLUGS_UI
        if self.has("PLUGS_UI.SetConfig"):
            return self.set_config("PLUGS_UI", {"leds": {"mode": "off"}})
        if self.has("Ui.SetConfig"):
            return self.set_config("Ui", {"leds": {"mode": "off"}})
        print("    no LED configuration on this model - skipped")

    def optional_set(self, component: str, config: dict):
        if self.has(f"{component}.SetConfig"):
            return self.set_config(component, config)
        print(f"    {component} not present on this model - skipped")

    def read_back(self) -> dict:
        cfg = self.rpc("Shelly.GetConfig")
        return {
            "name": cfg.get("sys", {}).get("device", {}).get("name"),
            "initial_state": cfg.get("switch:0", {}).get("initial_state"),
            "power_limit": cfg.get("switch:0", {}).get("power_limit"),
            "leds": cfg.get("plugs_ui", {}).get("leds", {}).get("mode"),
            "mqtt": {k: cfg.get("mqtt", {}).get(k) for k in ("enable", "server", "topic_prefix")},
            "cloud": cfg.get("cloud", {}).get("enable"),
            "ble": cfg.get("ble", {}).get("enable"),
            "ws": cfg.get("ws", {}).get("enable"),
            "wifi_sta": {k: cfg.get("wifi", {}).get("sta", {}).get(k) for k in ("enable", "ssid")},
            "wifi_ap": cfg.get("wifi", {}).get("ap", {}).get("enable"),
        }


class Gen1Plug:
    """Gen1 Shelly Plug / Plug S: HTTP GET /settings... with query parameters."""

    def __init__(self, host: str, info: dict):
        self.base = f"http://{host}"
        self.info = info

    def get(self, path: str, **params) -> dict:
        query = urllib.parse.urlencode({k: str(v).lower() if isinstance(v, bool) else v for k, v in params.items()})
        return http_get(f"{self.base}{path}" + (f"?{query}" if query else ""))

    def steps(self, args) -> list[Step]:
        return [
            Step(f"device name = {args.name}", lambda: self.get("/settings", name=args.name)),
            Step("relay off after power-up", lambda: self.get("/settings/relay/0", default_state="off", name=args.name)),
            Step("LEDs off", lambda: self.get("/settings", led_status_disable=True, led_power_disable=True)),
            Step(f"max {MAX_POWER_W} W", lambda: self.get("/settings", max_power=MAX_POWER_W)),
            Step(
                f"MQTT -> {args.mqtt} id '{args.topic}'",
                lambda: self.get("/settings", mqtt_enable=True, mqtt_server=args.mqtt, mqtt_id=args.topic, mqtt_retain=False),
            ),
            Step(f"cloud {'on' if args.cloud else 'off'}", lambda: self.get("/settings/cloud", enabled=args.cloud)),
            Step("CoIoT off", lambda: self.get("/settings", coiot_enable=False)),
            Step("read back the configuration", lambda: print(json.dumps(self.read_back(), indent=2))),
            # Gen1 switches its own AP off by itself once station mode is enabled
            Step(
                f"Wi-Fi client -> {args.ssid}",
                lambda: self.get("/settings/sta", enabled=True, ssid=args.ssid, key=args.wifi_password),
                may_disconnect=True,
            ),
            Step("reboot", lambda: self.get("/reboot"), may_disconnect=True),
        ]

    def read_back(self) -> dict:
        s = self.get("/settings")
        return {
            "name": s.get("name"),
            "default_state": s.get("relays", [{}])[0].get("default_state"),
            "max_power": s.get("max_power"),
            "leds_off": (s.get("led_status_disable"), s.get("led_power_disable")),
            "mqtt": {k: s.get("mqtt", {}).get(k) for k in ("enable", "server", "id")},
            "cloud": s.get("cloud", {}).get("enabled"),
            "coiot": s.get("coiot", {}).get("enabled"),
            "wifi_sta": {k: s.get("wifi_sta", {}).get(k) for k in ("enabled", "ssid")},
        }


def detect(host: str):
    info = http_get(f"http://{host}/shelly")
    gen = info.get("gen")
    if gen and int(gen) >= 2:
        return Gen2Plug(host, info)
    if "type" in info:
        return Gen1Plug(host, info)
    raise ShellyError(f"{host} does not look like a Shelly device: {info}")


def join_shelly_ap() -> None:
    """Connect this PC to the first open 'shelly...' access point with nmcli."""
    if not shutil.which("nmcli"):
        raise ShellyError("--join-ap needs NetworkManager (nmcli)")
    subprocess.run(["nmcli", "dev", "wifi", "rescan"], capture_output=True)
    time.sleep(3)
    listing = subprocess.run(["nmcli", "-t", "-f", "SSID", "dev", "wifi", "list"], capture_output=True, text=True).stdout
    candidates = sorted({ssid for ssid in listing.splitlines() if re.match(r"(?i)^shelly", ssid)})
    if not candidates:
        raise ShellyError("no 'shelly...' Wi-Fi network found - is the plug factory reset and powered?")
    if len(candidates) > 1:
        print(f"Several Shelly APs found: {', '.join(candidates)} - using {candidates[0]}")
    print(f"Connecting to {candidates[0]} ...")
    subprocess.run(["nmcli", "dev", "wifi", "connect", candidates[0]], check=True)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Configure a factory-reset Shelly smart plug.")
    p.add_argument("--name", default=DEFAULT_NAME, help=f"device name (default {DEFAULT_NAME})")
    p.add_argument("--topic", help="MQTT topic prefix (default: same as --name)")
    p.add_argument("--cloud", action=argparse.BooleanOptionalAction, default=False, help="Shelly cloud on/off (default off)")
    p.add_argument("--host", default=DEFAULT_HOST, help=f"plug address (default {DEFAULT_HOST})")
    p.add_argument("--mqtt", default=DEFAULT_MQTT, help=f"MQTT broker host:port (default {DEFAULT_MQTT})")
    p.add_argument("--ssid", default=DEFAULT_SSID, help=f"Wi-Fi network to join (default {DEFAULT_SSID})")
    p.add_argument("--wifi-password", default=os.environ.get("SHELLY_WIFI_PASSWORD"),
                   help="Wi-Fi password (or env SHELLY_WIFI_PASSWORD; asked when missing)")
    p.add_argument("--keep-ap", action="store_true", help="leave the plug's own access point on")
    p.add_argument("--join-ap", action="store_true", help="connect this PC to the plug's AP first (nmcli)")
    p.add_argument("--dry-run", action="store_true", help="show what would be configured, change nothing")
    args = p.parse_args(argv)

    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", args.name):
        p.error("--name: use letters, digits, '-', '_' or '.' (max 64)")
    args.topic = args.topic or args.name
    if ":" not in args.mqtt:
        args.mqtt += ":1883"
    return args


def main(argv=None) -> int:
    args = parse_args(argv)

    try:
        if args.join_ap:
            join_shelly_ap()
        plug = detect(args.host)
    except (ShellyError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"Is this PC connected to the plug's own Wi-Fi (shelly...-<mac>) and does it answer on {args.host}?", file=sys.stderr)
        return 1

    model = plug.info.get("app") or plug.info.get("model") or plug.info.get("type")
    gen = plug.info.get("gen", 1)
    print(f"Found Shelly {model} (Gen{gen}), MAC {plug.info.get('mac')}, firmware {plug.info.get('ver') or plug.info.get('fw')}")

    if not args.dry_run and not args.wifi_password:
        args.wifi_password = getpass.getpass(f"Wi-Fi password for {args.ssid}: ")

    steps = plug.steps(args)
    for number, step in enumerate(steps, start=1):
        print(f"[{number}/{len(steps)}] {step.title}")
        if args.dry_run:
            continue
        try:
            step.call()
        except ShellyError as exc:
            if step.may_disconnect:
                print(f"    connection dropped ({exc}) - expected while the Wi-Fi changes")
                continue
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    if args.dry_run:
        print("Dry run - nothing was changed.")
    else:
        print(f"Done. The plug reboots and joins '{args.ssid}'; look for it as '{args.name}' on the network / MQTT broker.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
