# ScarGuard Actuation MVP — Specification

**Version:** 0.1 (draft)
**Target ScarGuard Release:** v0.2.x
**Author:** Scott / Claude (design collaboration)
**Date:** 2026-04-06

---

## Overview

This document specifies the MVP actuation subsystem for ScarGuard — the ability to trigger sprinkler valves in response to wildlife detection events. The design prioritizes minimal physical integration (no custom wiring, soldering, or relay boards) by using off-the-shelf WiFi smart hose timer valves controlled via the Tuya local API.

### Design Principles

- **No custom hardware.** All actuation hardware is commercially available, hose-thread, battery-powered, and WiFi-connected.
- **Software-defined actuation.** Randomization logic, zone selection, timing, and cooldowns are handled entirely in a new Docker Compose service on the Orin.
- **LAN-only control.** Valve commands are sent via `tinytuya` over the local network. No cloud API dependency at runtime.
- **Seasonal deployment.** System is deployed spring through fall (Wisconsin climate). Batteries are swapped and hoses connected at season start; hoses are disconnected and valves stored for winter.

---

## Bill of Materials

### Required Hardware

| Qty | Item | Purpose | Est. Cost | Notes |
|-----|------|---------|-----------|-------|
| 4 | Tuya/Smart Life WiFi hose timer valve | Sprinkler zone control | $25–40 ea | Must use **Smart Life** or **Tuya Smart** app (not proprietary app). See Valve Selection below. |
| 4 | Garden hose (appropriate length) | Water delivery to sprinkler heads | Varies | Length depends on hose bib to pond distance. Seasonal surface deployment — no burial needed. |
| 4 | Sprinkler heads | Water dispersion around pond | $5–15 ea | Impact, oscillating, or fixed-pattern — pick based on coverage area. Aim toward pond perimeter. |
| 16+ | AA batteries | Valve power | $10–15 (bulk) | 4x AA per valve. Replace at season start. Budget one extra set mid-season. |
| 1 | Hose splitter (4-way) | Single hose bib to 4 lines | $15–25 | Brass preferred. Individual shutoffs per port are nice but not required. |

**Estimated total: $175–275** depending on hose lengths and sprinkler choice.

### Valve Selection Criteria

When purchasing valves, confirm ALL of the following:

1. **Smart Life / Tuya Smart app compatible.** The product listing or packaging must reference the "Smart Life" or "Tuya Smart" app. This is what makes `tinytuya` local control possible.
2. **2.4GHz WiFi.** All Tuya devices are 2.4GHz only — confirm your WiFi network broadcasts a 2.4GHz SSID the valves can reach at the pond.
3. **Standard 3/4" garden hose thread (GHT).** Fits any standard US garden hose bib and hose.
4. **No hub required (preferred).** Hub-free models simplify the stack. If a hub is required, one hub typically supports 4 valves.
5. **Battery powered (4x AA).** Confirms DC solenoid operation and seasonal battery swap.

**Known compatible product lines (Tuya-based):**

- RAINPOINT T-series (TTV103WRF — requires TWG004 hub)
- VEVOR WiFi Sprinkler Timer (Smart Life app, hub-free models available)
- Generic "Tuya WiFi irrigation timer" listings on Amazon (verify Smart Life app compatibility in listing/reviews)
- [Valve purchased for PoC] (https://www.amazon.com/Sprinkler-GreenVation-Automatic-Watering-Irrigation/dp/B0G41G99YR/ref=sr_1_6?crid=H7IHM37EBJCT&dib=eyJ2IjoiMSJ9.qcdu0TSfg0GoazuKs5eQBSKTn9aTqGdQAYhIdujsFwtK4zStLneBAqXtRYsxOedVG_-V9i8Klzz0URDu4IJKAh4aRyZm4i8xd3KYPjhJvH6_79OLwRwpb87a997HjNIXFmuGhXjDJAm3iHIHH4wyWX875og9wmYPeh7O3SDzMpfq3sun6PNOfFiPBAmMdVN8nUK5EwtUAxpGlfnnNBRuCe6Q0dExVf318tuy_HK8HnRB3ytL6eGCRzUqRxCmdIexHXmy8qyuJAGplFC_PvBRCJ7_eOYPn9Nb49lLj99Pzto.UQSlSJscIwJSCC5Eeszh6_eQDdrj2Vr0026ciE1V6v0&dib_tag=se&keywords=Tuya%2BWiFi%2Bwater%2Btimer%2Bhose%2BSmart%2BLife%2Bno%2Bhub&nsdOptOutParam=true&qid=1775494442&sprefix=tuya%2Bwifi%2Bwater%2Btimer%2Bhose%2Bsmart%2Blife%2Bno%2Bhub%2Caps%2C230&sr=8-6&th=1)

**Avoid:** RainPoint H-series (HTV*) — these use RainPoint's proprietary "Home" app and are NOT Tuya-compatible. The hardware protocols are incompatible.

---

## Architecture

### Integration Point

The actuation subsystem integrates into the existing ScarGuard stack as a new Docker Compose service (`actuator`). It subscribes to the same Redis pub/sub channel (`scarguard:detections`) that the notifier already consumes.

```
┌──────────┐    RTSP     ┌──────────┐   Redis pub/sub   ┌───────────┐
│ Cameras  │────────────▶│ Detector │──────────────────▶│ Notifier  │
└──────────┘             └──────────┘        │          └───────────┘
                                             │
                                             ▼
                                       ┌───────────┐   tinytuya    ┌─────────────┐
                                       │ Actuator  │──────────────▶│ WiFi Valves │
                                       └───────────┘   (LAN TCP)   └─────────────┘
```

### Actuator Service

| Property | Value |
|----------|-------|
| Base image | `python:3.11-slim` |
| Dependencies | `tinytuya`, `pyyaml`, `redis`, `pydantic` |
| Config source | `config/scarguard.yml` (mounted read-only) |
| Redis dependency | Same Redis instance as detector/notifier |
| Network requirements | LAN access to valve IP addresses (UDP 6666-6667, 7000; TCP 6668) |
| Resource usage | Negligible CPU/RAM — event-driven, sleeps between detections |

### docker-compose.yml Addition

```yaml
  # ── Actuator ──────────────────────────────────────────────────────────────
  actuator:
    build:
      context: .
      dockerfile: services/actuator/Dockerfile
    image: ghcr.io/${GHCR_OWNER:-sentania-labs}/scarguard-actuator:${IMAGE_TAG:-latest}
    environment:
      CONFIG_PATH: /config/scarguard.yml
    volumes:
      - ./config:/config:ro
    depends_on:
      redis:
        condition: service_healthy
    restart: unless-stopped
```

### Service Directory Structure

```
services/actuator/
├── Dockerfile
├── requirements.txt
└── src/
    ├── main.py           # Entry point: Redis subscriber, event dispatcher
    ├── valve_controller.py  # tinytuya device management, on/off commands
    ├── randomizer.py     # Zone selection, duration, delay randomization
    └── battery_monitor.py   # Periodic battery status polling
```

---

## Configuration

### scarguard.yml — Actuation Section

```yaml
actuation:
  enabled: true

  # Randomization parameters
  min_valves: 1              # Minimum valves to fire per event
  max_valves: 4              # Maximum valves to fire per event
  spray_duration_min_sec: 3  # Minimum spray time per valve
  spray_duration_max_sec: 8  # Maximum spray time per valve
  inter_spray_delay_min_sec: 1  # Min pause between valve activations
  inter_spray_delay_max_sec: 5  # Max pause between valve activations
  pre_delay_min_sec: 0       # Min delay before first spray (post-detection)
  pre_delay_max_sec: 3       # Max delay before first spray

  # Cooldown — prevents continuous firing if animal persists
  cooldown_seconds: 60

  # Battery monitoring
  battery_check_enabled: true
  battery_check_interval_hours: 24
  battery_alert_threshold_percent: 20  # Alert via notifications when below this

  # Valve definitions
  valves:
    - name: pond-north
      device_id: "bf1234567890abcdef"
      local_key: "abcdef1234567890"
      ip: "172.16.1.100"
      protocol_version: "3.3"
      enabled: true

    - name: pond-east
      device_id: "bf0987654321fedcba"
      local_key: "fedcba0987654321"
      ip: "172.16.1.101"
      protocol_version: "3.3"
      enabled: true

    - name: pond-south
      device_id: "bf1122334455667788"
      local_key: "8877665544332211"
      ip: "172.16.1.102"
      protocol_version: "3.3"
      enabled: true

    - name: pond-west
      device_id: "bfaabbccddeeff0011"
      local_key: "1100ffeeddccbbaa"
      ip: "172.16.1.103"
      protocol_version: "3.3"
      enabled: true
```

### Config Notes

- `device_id`, `local_key`, and `ip` are obtained via the `tinytuya` wizard (one-time setup).
- `protocol_version` is typically `"3.3"` or `"3.4"` for current Tuya devices. The wizard reports this.
- Individual valves can be disabled without removing them from config.
- Assign static IPs or DHCP reservations on the UDM for each valve to prevent IP drift.

---

## Actuation Logic

### Event Flow

1. Detector publishes detection event to Redis channel `scarguard:detections`.
2. Actuator receives event, checks: is system armed? Is actuation enabled? Is cooldown active?
3. If clear, randomizer selects parameters: which valves (random subset of enabled valves), spray duration per valve (random within range), inter-spray delay, and pre-delay.
4. After pre-delay, actuator fires selected valves sequentially with randomized timing.
5. Each valve command is: connect via tinytuya → set switch DP to `True` → sleep for duration → set switch DP to `False` → disconnect.
6. Cooldown timer starts after sequence completes.
7. Actuation events are published back to Redis (channel: `scarguard:actuations`) for the web UI and notifier to consume.

### Randomization Parameters

All randomization uses `random.uniform()` (continuous) or `random.sample()` (valve selection) per event. No two events should produce identical patterns.

| Parameter | Range (default) | Purpose |
|-----------|----------------|---------|
| Valve count | 1–4 | How many valves fire |
| Valve selection | Random subset of enabled valves | Which valves fire |
| Spray duration | 3–8 seconds per valve | How long each valve stays open |
| Inter-spray delay | 1–5 seconds | Pause between sequential valve activations |
| Pre-delay | 0–3 seconds | Delay before first spray (breaks association between camera/box and spray) |

### Cooldown

After an actuation sequence completes, the actuator ignores further detection events for `cooldown_seconds`. This prevents continuous firing when an animal lingers. Detection events that arrive during cooldown are logged but not acted on.

### Failure Handling

- **Valve unreachable:** Log error, skip that valve, continue with remaining valves in sequence. Do not fail the entire actuation because one valve is offline.
- **All valves unreachable:** Log error, publish a notification event (so Discord/email alerts fire).
- **Redis disconnection:** Reconnect with backoff (same pattern as notifier service).

---

## Battery Monitoring

### Polling

A background task in the actuator service queries each valve's status DPs once per `battery_check_interval_hours` (default: 24 hours). This is a single short-lived TCP connection per valve — connect, query `device.status()`, parse battery DP, disconnect.

### Alerting

When any valve's battery percentage drops below `battery_alert_threshold_percent`, the actuator publishes a notification event to Redis channel `scarguard:notifications` with type `battery_low` and the valve name/level. The notifier service dispatches this via the same Discord/email channels used for detection alerts.

### Data Storage

Battery readings are written to SQLite (`data/scarguard.db`) in an `actuator_status` table for historical tracking and web UI display.

```sql
CREATE TABLE IF NOT EXISTS actuator_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    valve_name TEXT NOT NULL,
    battery_percent INTEGER,
    reachable BOOLEAN NOT NULL DEFAULT 1,
    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## Valve Setup Procedure (One-Time)

### 1. Pair valves in Smart Life app

Install the Smart Life app on your phone. Create an account (or use existing). Pair each valve following the manufacturer's instructions. Verify you can manually open/close each valve from the app.

### 2. Create Tuya IoT developer project

Go to `iot.tuya.com`, create a free developer account. Create a Cloud Development project. Subscribe to the "Smart Home Device Management" API. Link your Smart Life app account by scanning the QR code in the Tuya IoT console.

### 3. Run tinytuya wizard

On your dev machine (or any machine on the same LAN):

```bash
pip install tinytuya
python -m tinytuya wizard
```

Enter your Tuya IoT API ID, Secret, and Region when prompted. The wizard outputs a `devices.json` with each device's `id`, `key` (local_key), and `ip`. Copy these values into `scarguard.yml`.

### 4. Assign static IPs

In the UDM console, create DHCP reservations for each valve's MAC address so the IPs don't change.

### 5. Test connectivity

```bash
python -c "
import tinytuya
d = tinytuya.OutletDevice('DEVICE_ID', 'DEVICE_IP', 'LOCAL_KEY', version=3.3)
print(d.status())
"
```

You should get a JSON response with the device's data points. If you see a switch DP (usually DP `1`), you can test toggling it.

### 6. Firewall rules

Ensure your UDM firewall allows the Orin to reach the valve IPs on UDP 6666-6667, 7000 and TCP 6668. On a flat home LAN this is typically open by default.

---

## Web UI Integration (Future)

The following are out of scope for the actuation MVP but should be planned for:

- **Actuation log tab:** Show actuation events with timestamp, valves fired, durations, and triggering detection event.
- **Valve status panel:** Show each valve's name, enabled/disabled state, last battery reading, and reachability.
- **Manual trigger button:** Fire a test actuation sequence from the web UI (useful for aiming sprinklers during setup).
- **Actuation config in GUI:** Expose randomization parameters and valve definitions in the config editor.

---

## CI/CD

The actuator service uses `python:3.11-slim` (same as web and notifier), so it builds on both x86 and ARM64 via the existing multi-arch buildx workflow. No GPU or Jetson-specific dependencies.

Add to `ci.yml`:

- Lint and type check `services/actuator/`
- Build and push `scarguard-actuator` image alongside web and notifier
- Same `runs-on` as web/notifier builds (x86 runners, multi-arch via QEMU)

---

## Open Questions

1. **DP mapping varies by manufacturer.** The switch DP is usually `1`, but battery percentage DP ID varies. Need to discover DPs for the specific valve model purchased using `device.detect_available_dps()` and document the mapping.

2. **Actuation event schema.** Define the Pydantic model for actuation events published to Redis. Should include: event_id, triggering_detection_id, valves_fired (list with name/duration), total_sequence_time, timestamp.

3. **Config hot-reload.** Should the actuator watch `scarguard.yml` for changes (matching detector/notifier behavior), or is a container restart acceptable for config changes? Recommend: match existing hot-reload pattern for consistency.

4. **Web UI actuation log.** Should actuation events write to the existing `events` table with a new event type, or a dedicated `actuations` table? Recommend: dedicated table to keep schemas clean.

---

## Acceptance Criteria

- [ ] Actuator service starts, connects to Redis, and subscribes to `scarguard:detections`
- [ ] On detection event (system armed, actuation enabled, cooldown clear): randomized valve sequence fires
- [ ] Valves physically open and close via tinytuya LAN commands
- [ ] Cooldown prevents re-firing within configured window
- [ ] Unreachable valves are skipped without failing the sequence
- [ ] Battery monitor runs on configured interval and publishes alerts below threshold
- [ ] Actuation events are logged to SQLite
- [ ] Service follows existing patterns: config from `scarguard.yml`, Python 3.11, type hints, Pydantic models, structured logging
- [ ] Docker image builds in CI alongside web/notifier
