# ESP32 Home Air Quality Monitoring System

Little DIY ESP32 "rover style" meter with a Flask app and web dashboard to monitor the air quality inside my house.

The ESP32 reads its sensors every few seconds and sends them over the local network to a Flask server running in Docker on my home server. The server stores everything in SQLite, serves the dashboard, and sends the LED configuration back to the ESP32 in the response to every upload.

## Architecture

```
┌──────────────┐  POST /api/data (JSON)   ┌──────────────────────────┐
│    ESP32     │ ───────────────────────► │ Docker: air-quality-     │
│  + sensors   │ ◄─────────────────────── │ dashboard (Flask +       │
│              │   response: LED config   │ gunicorn) :8081 (LAN)    │
└──────────────┘                          └────────────┬─────────────┘
                                                       │
                                          ┌────────────▼─────────────┐
   Tailscale / public internet  ◄──────── │ Docker: caddy :8082      │
          (GET only)                      │ read-only reverse proxy  │
                                          └──────────────────────────┘
```

| Access | Address | Permissions |
|---|---|---|
| ESP32 + browser from home | `http://192.168.1.101:8081` | everything (GET, POST, LED control) |
| Tailscale / public (Funnel) | `http://100.102.62.49:8082` / Funnel URL | GET only |

The full app is only published on the LAN IP. The public side goes through Caddy, which blocks every non-GET request and the private endpoints.

## Hardware

| Component | Measures | Bus |
|---|---|---|
| ESP32 | | |
| AHT20 | temperature, humidity | I2C |
| BMP280 | temperature, pressure | I2C (address `0x77` or `0x76`) |
| SPS30 | PM1.0, PM2.5, PM10 | I2C |
| LED | status / threshold alarm | GPIO 5 |

I2C pins: **SDA = GPIO 10**, **SCL = GPIO 9**.

## Repository layout

```
.
├── Firmware/main.cpp          # ESP32 firmware (PlatformIO)
├── platformio.ini
└── Server/
    ├── server.py         # Flask app (API + SQLite)
    ├── dashboard.html    # web dashboard
    ├── Dockerfile
    └── deploy.sh         # rebuild + restart the container
```

## Firmware deployment

1. Open the project in VSCode with the PlatformIO extension.
2. Edit the configuration at the top of `src/main.cpp`:
   ```cpp
   const char* WIFI_SSID  = "...";
   const char* WIFI_PASS  = "...";
   const char* SERVER_URL = "http://192.168.1.XXX:8081/api/data";  // server LAN IP, port 8081
   const char* DEVICE_ID  = "Airometer_ESP32";
   ```
3. PlatformIO > **Upload**.
4. PlatformIO > **Serial Monitor** (115200 baud) to check the output.

Serial output to look for:

- `[AHT20] OK`, `[BMP280] OK`, `[SPS30] OK`: sensors found.
- `[WiFi] Connesso, IP: ...`: connected.
- `[HTTP] Errore: -1`: server unreachable (wrong URL/port, container down, firewall).

## Server deployment

The server runs as a Docker container and the SQLite database lives on a bind-mounted volume (`/mnt/hdd/sensori`), so data survives rebuilds.

### First setup

```bash
git clone "https://github.com/AleCava01/ESP32-Home-Air-Quality-Monitoring-System.git"
cd ESP32-Home-Air-Quality-Monitoring-System/Server
mkdir -p /mnt/hdd/sensori
chmod +x deploy.sh
sudo ./deploy.sh
```

### After every change to `server.py` or `dashboard.html`

The code is baked into the Docker image, so every change needs a rebuild and a new container:

```bash
cd ~/Projects/ESP32-Home-Air-Quality-Monitoring-System/Server
sudo ./deploy.sh
```

