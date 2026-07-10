# ScarGuard Deterrent Service — Specification

**Version:** 0.2
**Target ScarGuard Release:** v0.13.x
**Author:** Scott / Claude (design collaboration)
**Date:** 2026-04-12

---

## Overview

This document specifies the deterrent subsystem for ScarGuard — the ability to trigger physical deterrence devices (sprinklers, lights, sirens, smart plugs) in response to wildlife detection events. The design uses off-the-shelf Tuya/Smart Life ecosystem devices controlled via the **Tuya Cloud API** (`tinytuya.Cloud`).

### Design Principles

- **No custom hardware.** All deterrent hardware is commercially available, WiFi-connected, and battery or mains powered.
- **Software-defined deterrence.** Randomization logic, device selection, timing, and cooldowns are handled entirely in a new Docker Compose service (`deterrent`) on the host.
- **Cloud API control.** Device commands are sent via `tinytuya.Cloud` (HTTPS to Tuya's OpenAPI). Local LAN control is not viable for battery-powered devices — they deep-sleep with WiFi radio off between cloud check-ins.
- **Multi-device-type support.** Not limited to sprinkler valves — any Tuya-compatible smart device (lights, sirens, smart plugs) can be used as a deterrent.
- **Seasonal deployment.** Sprinkler hardware is deployed spring through fall (Wisconsin climate). Batteries are swapped and hoses connected at season start; hoses are disconnected and valves stored for winter. Lights and sirens may remain year-round.

### Changes from v0.1

| Area | v0.1 (superseded) | v0.2 (current) |
|------|-------------------|----------------|
| Control method | `tinytuya` LAN TCP | `tinytuya.Cloud` HTTPS |
| Service name | `actuator` | `deterrent` |
| Device types | Sprinkler valves only | Sprinklers, lights, sirens, smart plugs |
| Config fields | `local_key`, `ip`, `protocol_version` | `api_key`, `api_secret`, `api_region` |
| Network requirements | LAN access (UDP 6666-6667, TCP 6668) | Outbound HTTPS only |
| Response profiles | None (all devices fire) | Planned for 0.13.x (species-based routing) |

**Why Cloud API?** Battery-powered Tuya devices enter deep sleep between cloud check-ins to conserve battery. The WiFi radio is powered down during sleep. Local LAN discovery (`tinytuya` scan on UDP 6666-6667) finds nothing, and TCP connections to port 6668 fail with "No route to host" because the device doesn't respond to ARP. Cloud commands work because the device polls the Tuya cloud on wake (~1-2s intervals) and picks up queued commands. This was validated on 2026-04-12 with a GreenVation WiFi sprinkler valve (device ID `eb074b33939a9f6effqm7l`).

---

## Bill of Materials

### Required Hardware

| Qty | Item | Purpose | Est. Cost | Notes |
|-----|------|---------|-----------|-------|
| 1-4 | Tuya/Smart Life WiFi hose timer valve | Sprinkler zone control | $25-40 ea | Must use Smart Life or Tuya Smart app |
| 1-4 | Garden hose + sprinkler heads | Water delivery | Varies | Length depends on layout |
| 16+ | AA batteries | Valve power | $10-15 (bulk) | 4x AA per valve, replace seasonally |
| 1 | Hose splitter (4-way) | Single hose bib to multiple lines | $15-25 | Brass preferred |
| 0-2 | Tuya smart lights/plugs | Light deterrence | $10-20 ea | Optional — for night-time raccoon deterrence |
| 0-1 | Tuya smart siren/alarm | Sound deterrence | $15-30 | Optional |

**Estimated total: $100-350** depending on configuration.

### Device Selection Criteria

1. **Smart Life / Tuya Smart app compatible.** Must reference the "Smart Life" or "Tuya Smart" app.
2. **2.4GHz WiFi.** All Tuya devices are 2.4GHz only.
3. **Battery-powered devices work via Cloud API** but NOT via LAN control. Mains-powered devices may support both (future LAN fallback, unprioritized).
4. **Standard garden hose fittings** for sprinkler valves (3/4" GHT).

**Known compatible:**
- [GreenVation WiFi Sprinkler Timer](https://www.amazon.com/dp/B0G41G99YR) — PoC validated
- VEVOR WiFi Sprinkler Timer (hub-free models)
- Generic Tuya WiFi irrigation timers (verify Smart Life app compatibility)

**Avoid:** RainPoint H-series (proprietary app, NOT Tuya-compatible).

---

## Architecture

### Integration Point

```
┌──────────┐    RTSP     ┌──────────┐   Redis pub/sub   ┌───────────┐
│ Cameras  │────────────>│ Detector │──────────────────>│ Notifier  │
└──────────┘             └──────────┘        │          └───────────┘
                                             │
                                             v
                                       ┌───────────┐  tinytuya.Cloud  ┌─────────────────┐
                                       │ Deterrent │─────────────────>│ Tuya Cloud API  │
                                       └───────────┘     (HTTPS)      └────────┬────────┘
                                                                               │
                                                                               v
                                                                     ┌─────────────────┐
                                                                     │ Smart Devices   │
                                                                     │ (valves, lights,│
                                                                     │  sirens, plugs) │
                                                                     └─────────────────┘
```

### Deterrent Service

| Property | Value |
|----------|-------|
| Base image | `python:3.11-slim` |
| Dependencies | `tinytuya`, `pyyaml`, `redis`, `pydantic` |
| Config source | `scarguard.yml` (mounted read-only) |
| Redis dependency | Same Redis instance as detector/notifier |
| Network requirements | **Outbound HTTPS** to `openapi.tuyaus.com` (or regional equivalent). No inbound ports needed. |
| Resource usage | Negligible CPU/RAM — event-driven, sleeps between detections |

### Service Directory Structure

```
services/deterrent/
├── Dockerfile
├── entrypoint.sh
├── requirements.txt
├── src/
│   ├── main.py              # Redis subscriber, event dispatcher, config reload
│   ├── cloud_controller.py  # tinytuya.Cloud wrapper — on/off commands, status queries
│   ├── randomizer.py        # Device selection, duration/delay randomization
│   ├── cooldown.py          # Global cooldown tracker
│   ├── models.py            # Pydantic models for config and actuation events
│   └── battery_monitor.py   # Periodic battery polling via Cloud API
└── tests/
    ├── test_randomizer.py
    └── test_cooldown.py
```

---

## Configuration

### scarguard.yml — Actuation Section

```yaml
deterrent:
  enabled: true

  # Tuya Cloud API credentials — see TUYA_SETUP.md for how to obtain these
  tuya:
    api_key: "your-access-id"
    api_secret: "your-access-secret"
    api_region: "us"              # us | eu | cn | in

  # Device definitions — one entry per physical device
  devices:
    - name: pond-north-valve
      device_id: "bf1234567890abcdef"
      type: sprinkler              # sprinkler | light | sound | plug
      enabled: true

    - name: pond-light
      device_id: "bf0987654321fedcba"
      type: light
      enabled: true

  # Randomization ranges
  defaults:
    device_count_range: [1, 4]
    spray_duration_range: [3, 8]
    inter_device_delay_range: [1, 5]
    pre_delay_range: [0, 3]
    cooldown_seconds: 60

  # Battery monitoring
  battery_monitor:
    enabled: true
    check_interval_hours: 24
    alert_threshold_percent: 20
```

### Config Notes

- `api_key` and `api_secret` are obtained from the Tuya IoT Platform — see [TUYA_SETUP.md](TUYA_SETUP.md) for the full walkthrough.
- `api_region` must match the Data Center selected during project creation.
- No `local_key`, `ip`, or `protocol_version` fields — Cloud API doesn't need them.
- Individual devices can be disabled without removing them from config.
- Config hot-reloads without service restart (via `ConfigWatcher`).

---

## Actuation Logic

### Event Flow (MVP — v0.13.0)

1. Detector publishes detection event to Redis channel `scarguard:detections`.
2. Deterrent receives event, checks: is system armed? Is actuation enabled? Is cooldown clear?
3. If clear, randomizer selects parameters: which devices (random subset of enabled), activation duration per device (random within range), inter-device delay, and pre-delay.
4. After pre-delay, deterrent fires selected devices sequentially with randomized timing.
5. Each device command: `tinytuya.Cloud.sendcommand()` → set DP to `True` → sleep for duration → set DP to `False`.
6. Cooldown timer starts after sequence completes.
7. Actuation events are published to Redis (channel: `scarguard:actuations`).

### Response Profiles (v0.13.x — future)

Species-based routing with time-of-day conditions:

```yaml
  response_profiles:
    - name: heron-thermonuclear
      match:
        species: [great_blue_heron, green_heron]
      device_types: [sprinkler, light, sound, plug]
      devices: all

    - name: raccoon-night
      match:
        species: [raccoon]
        time_of_day: [sunset, sunrise]
      device_types: [light, sound]

    - name: duck-gentle
      match:
        species: [duck]
      device_types: [sprinkler]
      device_count_range: [1, 2]
```

### Randomization Parameters

| Parameter | Range (default) | Purpose |
|-----------|----------------|---------|
| Device count | 1-4 | How many devices fire |
| Device selection | Random subset of enabled devices | Which devices fire |
| Spray duration | 3-8 seconds per device | How long each device stays on |
| Inter-device delay | 1-5 seconds | Pause between sequential activations |
| Pre-delay | 0-3 seconds | Delay before first activation |

### Cooldown

After an actuation sequence completes, the deterrent ignores further detection events for `cooldown_seconds`. Events arriving during cooldown are logged but not acted on.

### Failure Handling

- **Device unreachable:** Log error, skip that device, continue with remaining devices. Do not fail the entire sequence because one device is offline.
- **All devices unreachable:** Log error, publish a notification event.
- **Redis disconnection:** Reconnect with exponential backoff (same pattern as notifier).

### Cloud API Rate Limits

Tuya's free tier allows approximately 500 API calls/day. Each actuation uses ~2 calls per device (ON + OFF). With 4 devices and a 60-second cooldown, you'd need ~60 events/day to approach the limit. Monitor usage if you have high detection volumes.

---

## Battery Monitoring

### Polling

A background thread queries each device's status DPs once per `check_interval_hours` (default: 24). Uses `tinytuya.Cloud.getstatus()` — a single HTTPS call per device.

### Alerting

When any device's battery drops below `alert_threshold_percent`, the deterrent publishes a notification event to Redis. The notifier dispatches alerts via the same channels used for detection alerts.

### Data Storage

Battery readings are written to SQLite for historical tracking and future web UI display.

---

## Device Setup

See [TUYA_SETUP.md](TUYA_SETUP.md) for the complete step-by-step guide covering:

1. Creating a Tuya IoT Platform account
2. Creating a Cloud Development project
3. Subscribing to required API services
4. Linking your Smart Life app account
5. Obtaining API credentials and device IDs
6. Configuring ScarGuard

---

## Web UI Integration (v0.13.x)

Out of scope for the MVP but planned:

- **Deterrent config page:** Dedicated page (not inline in main config) for Tuya credentials, device list, randomization params, and battery monitoring settings. Main config page shows just an enable/disable toggle.
- **Actuation log:** Show actuation events with timestamp, devices fired, durations, and triggering detection.
- **Device status panel:** Each device's name, enabled state, last battery reading, and reachability.
- **Manual trigger button:** Fire a test sequence from the web UI.

---

## CI/CD

The deterrent service uses `python:3.11-slim` (same as web and notifier), so it builds on both x86 and ARM64. No GPU dependencies.

- `ci.yml`: Lint (`ruff`), type check (`mypy`), and `pytest` for `services/deterrent/`
- `build.yml`: Build and push `scarguard-deterrent` image
- `release.yml`: Include deterrent in the release image matrix

---

## Resolved Questions (from v0.1)

1. **DP mapping:** Default DP codes per device type (`switch_1` for sprinklers/plugs, `switch_led` for lights, `switch` for sirens). Per-device `dp_code` override available in config.
2. **Actuation event schema:** Pydantic `ActuationEvent` model with trigger info, device actions (name, type, duration, success/error), total duration.
3. **Config hot-reload:** Yes — uses `ConfigWatcher` + `AtomicRef`, matching existing service pattern.
4. **Web UI actuation log:** Deferred to 0.13.x. Events published to `scarguard:actuations` Redis channel for future consumption.

---

## Acceptance Criteria

- [ ] Deterrent service starts, connects to Redis, and subscribes to `scarguard:detections`
- [ ] On detection event (system armed, actuation enabled, cooldown clear): randomized device sequence fires
- [ ] Devices activate and deactivate via Tuya Cloud API commands
- [ ] Cooldown prevents re-firing within configured window
- [ ] Unreachable devices are skipped without failing the sequence
- [ ] Battery monitor runs on configured interval and publishes alerts below threshold
- [ ] Actuation events are published to `scarguard:actuations` Redis channel
- [ ] Service follows existing patterns: config from `scarguard.yml`, Python 3.11, type hints, Pydantic models, structured logging
- [ ] Docker image builds in CI alongside web/notifier
- [ ] `TUYA_SETUP.md` provides L100-level instructions for L200/L300 Tuya Cloud setup
