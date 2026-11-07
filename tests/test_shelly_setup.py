"""Runs shelly_setup.py against fake Gen1 and Gen2 Shelly plugs on localhost."""

import io
import json
import sys
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import shelly_setup


def deep_merge(target: dict, update: dict) -> None:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            deep_merge(target[key], value)
        else:
            target[key] = value


class FakeGen2(BaseHTTPRequestHandler):
    """Minimal Shelly Plus Plug S: /shelly + JSON-RPC on /rpc."""

    config: dict = {}
    calls: list = []

    def log_message(self, *args):
        pass

    def reply(self, body: dict):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/shelly":
            self.reply({"id": "shellyplusplugs-aabbccddeeff", "mac": "AABBCCDDEEFF", "gen": 2, "app": "PlusPlugS", "ver": "1.4.4"})
        else:
            self.send_error(404)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method, params = req["method"], req.get("params", {})
        self.calls.append((method, params))
        if method == "Shelly.ListMethods":
            result = {"methods": ["Sys.SetConfig", "Switch.SetConfig", "PLUGS_UI.SetConfig", "MQTT.SetConfig",
                                  "Cloud.SetConfig", "BLE.SetConfig", "Ws.SetConfig", "WiFi.SetConfig"]}
        elif method == "Shelly.GetConfig":
            result = self.config
        elif method == "Shelly.Reboot":
            result = None
        elif method.endswith(".SetConfig"):
            component = method.split(".")[0].lower()
            if component == "switch":
                component = f"switch:{params.get('id', 0)}"
            deep_merge(self.config.setdefault(component, {}), params["config"])
            result = {"restart_required": component == "mqtt"}
        else:
            self.reply({"id": req["id"], "error": {"code": 404, "message": f"No handler for {method}"}})
            return
        self.reply({"id": req["id"], "result": result})


class FakeGen1(FakeGen2):
    """Minimal Gen1 Shelly Plug S: GET /settings... with query parameters."""

    settings: dict = {}

    def do_GET(self):
        url = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.calls.append((url.path, params))
        s = self.settings
        if url.path == "/shelly":
            self.reply({"type": "SHPLG-S", "mac": "AABBCCDDEEFF", "fw": "20230913-114008/v1.14.0"})
            return
        if url.path == "/settings":
            if "name" in params:
                s["name"] = params["name"]
            if "max_power" in params:
                s["max_power"] = int(params["max_power"])
            for key in ("led_status_disable", "led_power_disable"):
                if key in params:
                    s[key] = params[key] == "true"
            if "mqtt_enable" in params:
                s["mqtt"] = {"enable": params["mqtt_enable"] == "true", "server": params["mqtt_server"], "id": params["mqtt_id"]}
            if "coiot_enable" in params:
                s["coiot"] = {"enabled": params["coiot_enable"] == "true"}
        elif url.path == "/settings/relay/0":
            s["relays"] = [{"default_state": params["default_state"]}]
        elif url.path == "/settings/cloud":
            s["cloud"] = {"enabled": params["enabled"] == "true"}
        elif url.path == "/settings/sta":
            s["wifi_sta"] = {"enabled": params["enabled"] == "true", "ssid": params["ssid"]}
        elif url.path != "/reboot":
            self.send_error(404)
            return
        self.reply(s)


class ShellyTestCase(unittest.TestCase):
    handler = FakeGen2

    def setUp(self):
        self.handler.config = {}
        self.handler.settings = {}
        self.handler.calls = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.host = f"127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def run_script(self, *extra: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = shelly_setup.main(["--host", self.host, "--wifi-password", "secret", *extra])
        return code, out.getvalue()


class Gen2Tests(ShellyTestCase):
    def test_full_configuration(self):
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        cfg = self.handler.config
        self.assertEqual(cfg["sys"]["device"]["name"], "Loccufier-Guus-Outlet1")
        self.assertEqual(cfg["switch:0"]["initial_state"], "off")
        self.assertEqual(cfg["switch:0"]["power_limit"], 2200)
        self.assertEqual(cfg["plugs_ui"]["leds"]["mode"], "off")
        self.assertEqual(cfg["mqtt"]["server"], "172.23.83.254:1883")
        self.assertEqual(cfg["mqtt"]["topic_prefix"], "Loccufier-Guus-Outlet1")
        self.assertTrue(cfg["mqtt"]["enable"])
        self.assertFalse(cfg["cloud"]["enable"])
        self.assertFalse(cfg["ble"]["enable"])
        self.assertFalse(cfg["ws"]["enable"])
        self.assertEqual(cfg["wifi"]["sta"]["ssid"], "Howest-IoT")
        self.assertFalse(cfg["wifi"]["ap"]["enable"])
        methods = [m for m, _ in self.handler.calls]
        self.assertEqual(methods[-1], "Shelly.Reboot")
        # The configuration is verified before the Wi-Fi change can drop the connection
        self.assertLess(methods.index("Shelly.GetConfig"), methods.index("WiFi.SetConfig"))

    def test_name_and_cloud_arguments(self):
        code, out = self.run_script("--name", "Test-Plug", "--cloud", "--keep-ap")
        self.assertEqual(code, 0, out)
        cfg = self.handler.config
        self.assertEqual(cfg["sys"]["device"]["name"], "Test-Plug")
        self.assertEqual(cfg["mqtt"]["topic_prefix"], "Test-Plug")
        self.assertTrue(cfg["cloud"]["enable"])
        self.assertTrue(cfg["wifi"]["ap"]["enable"])

    def test_missing_component_is_skipped(self):
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertIn("Matter not present on this model - skipped", out)

    def test_dry_run_changes_nothing(self):
        code, out = self.run_script("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self.handler.config, {})
        self.assertIn("Dry run", out)


class Gen1Tests(ShellyTestCase):
    handler = FakeGen1

    def test_full_configuration(self):
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        s = self.handler.settings
        self.assertEqual(s["name"], "Loccufier-Guus-Outlet1")
        self.assertEqual(s["relays"][0]["default_state"], "off")
        self.assertEqual(s["max_power"], 2200)
        self.assertTrue(s["led_status_disable"] and s["led_power_disable"])
        self.assertEqual(s["mqtt"], {"enable": True, "server": "172.23.83.254:1883", "id": "Loccufier-Guus-Outlet1"})
        self.assertFalse(s["cloud"]["enabled"])
        self.assertFalse(s["coiot"]["enabled"])
        self.assertEqual(s["wifi_sta"]["ssid"], "Howest-IoT")
        self.assertEqual(self.handler.calls[-1][0], "/reboot")


class Unreachable(unittest.TestCase):
    def test_clear_error_when_not_on_the_plug_ap(self):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), unittest.mock.patch("sys.stderr", err):
            code = shelly_setup.main(["--host", "127.0.0.1:9", "--wifi-password", "x"])
        self.assertEqual(code, 1)
        self.assertIn("connected to the plug's own Wi-Fi", err.getvalue())


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
