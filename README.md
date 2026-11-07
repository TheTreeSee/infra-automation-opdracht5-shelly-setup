# Opdracht 5 — Shelly Smart Plug configureren na een factory reset

`shelly_setup.py` configureert een Shelly-stekker die net gereset is. Enkel de standaardbibliotheek van Python 3.10+ is nodig.

Na een reset is de stekker een open accesspoint (SSID `shelly…-<MAC-adres>`) en antwoordt hij op `192.168.33.1`.
Verbind je laptop met dat accesspoint (of laat het script dat doen met `--join-ap`) en start het script.

## Gebruik

```sh
python3 shelly_setup.py                                  # vraagt het Wi-Fi-wachtwoord van Howest-IoT
python3 shelly_setup.py --join-ap                        # verbindt eerst zelf met het shelly-AP (nmcli)
python3 shelly_setup.py --name Loccufier-Guus-Outlet2    # andere naam (en MQTT-topic)
python3 shelly_setup.py --cloud                          # cloudverbinding aan (standaard uit)
python3 shelly_setup.py --dry-run                        # tonen wat er zou gebeuren
python3 -m unittest discover -s tests                    # tests tegen een nagebootste Gen1- en Gen2-stekker
```

Het Wi-Fi-wachtwoord kan ook via `--wifi-password` of de omgevingsvariabele `SHELLY_WIFI_PASSWORD`; zonder één van beide vraagt het script erom (niet zichtbaar bij het typen).

| optie | betekenis | standaard |
|---|---|---|
| `--name` | naam van de stekker | `Loccufier-Guus-Outlet1` |
| `--topic` | MQTT-topic prefix | gelijk aan `--name` |
| `--cloud` / `--no-cloud` | Shelly-cloud aan/uit | uit |
| `--host` | adres van de stekker | `192.168.33.1` |
| `--mqtt` | MQTT-broker `host[:poort]` | `172.23.83.254:1883` |
| `--ssid` | Wi-Fi-netwerk | `Howest-IoT` |
| `--keep-ap` | eigen accesspoint van de stekker laten aanstaan | uit |
| `--join-ap` | eerst met het shelly-AP verbinden | |
| `--dry-run` | niets wijzigen | |

## Wat er ingesteld wordt

| eis uit de opgave | Gen2/Gen3/Gen4 (RPC-API) | Gen1 Plug S (HTTP-API) |
|---|---|---|
| naam `<familienaam>-<voornaam>-Outlet1` | `Sys.SetConfig` `device.name` | `/settings?name=` |
| uit na herstart | `Switch.SetConfig` `initial_state: off` | `/settings/relay/0?default_state=off` |
| alle LED's uit | `PLUGS_UI.SetConfig` `leds.mode: off` | `led_status_disable`, `led_power_disable` |
| MQTT naar 172.23.83.254, topic = naam | `MQTT.SetConfig` (`server`, `topic_prefix`, status-meldingen aan) | `mqtt_enable`, `mqtt_server`, `mqtt_id` |
| maximale belasting 2200 W | `Switch.SetConfig` `power_limit: 2200` | `max_power=2200` |
| niet-gebruikte diensten uit | Bluetooth, uitgaande websocket, Matter (als het model het heeft), cloud (tenzij `--cloud`), eigen AP | cloud, CoIoT (het AP gaat vanzelf uit in client-modus) |
| Wi-Fi naar `Howest-IoT` | `WiFi.SetConfig` `sta` | `/settings/sta` |

De generatie wordt herkend via `http://192.168.33.1/shelly`; het script vraagt met `Shelly.ListMethods` op welke
onderdelen het model heeft en slaat ontbrekende onderdelen over in plaats van te falen.

Het MAC-adres (dat ook in de SSID zit), het model en de firmware worden bij de start getoond.

## Volgorde

1. alle instellingen behalve Wi-Fi;
2. de configuratie terug uitlezen en tonen (`Shelly.GetConfig` / `/settings`) als controle;
3. Wi-Fi-client instellen en het eigen accesspoint uitschakelen: hierna valt de verbinding met de laptop weg;
4. herstarten. Daarna verbindt de stekker met `Howest-IoT` en publiceert hij op de MQTT-broker onder de gekozen naam.

Is het wachtwoord van `Howest-IoT` fout, dan is de stekker niet meer bereikbaar en moet je hem opnieuw resetten
(knop ±10 s indrukken). Gebruik `--keep-ap` als je dat risico niet wilt nemen.

## Getest op een echte stekker

Op 7 november 2026 getest met een **Shelly Plug S (Gen1, `SHPLG-S`, firmware v1.14.0)** na een factory reset.
Thuis bestaat `Howest-IoT` niet, dus de stekker werd met het thuisnetwerk verbonden:

```sh
python3 shelly_setup.py --join-ap --ssid LocNet
```

Na de herstart kreeg de stekker via DHCP een adres op het thuisnetwerk. Alle instellingen werden daar opnieuw
uitgelezen (`/settings`, `/status`):

| instelling | waarde op de stekker |
|---|---|
| naam | `Loccufier-Guus-Outlet1` |
| toestand na stroomonderbreking | `off` |
| LED's (status / stroom) | beide uit |
| maximale belasting | 2200 W |
| MQTT | aan, `172.23.83.254:1883`, id `Loccufier-Guus-Outlet1` |
| cloud / CoIoT | uit / uit |
| Wi-Fi | client op het opgegeven netwerk via DHCP, eigen accesspoint **uit** |

MQTT staat correct ingesteld maar is thuis niet verbonden: de broker `172.23.83.254` is enkel bereikbaar op het
netwerk van Howest. Op school: stekker resetten en `python3 shelly_setup.py --join-ap` (standaard `Howest-IoT`).
