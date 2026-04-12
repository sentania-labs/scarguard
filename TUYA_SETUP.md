# Tuya IoT Platform Setup Guide

This guide walks you through creating the Tuya Cloud API credentials that
ScarGuard's deterrent service needs to control your smart devices (sprinkler
valves, lights, sirens, smart plugs).

**Time required:** ~15 minutes

**Difficulty:** Moderate (you'll be creating a developer account and linking
your smart home app — no coding required)

---

## Prerequisites

Before you begin, make sure you have:

1. **Smart Life or Tuya Smart app** installed on your phone (iOS or Android)
2. **All deterrent devices paired** and controllable in the app — test each
   device (turn on/off) before proceeding
3. **An email address** for your Tuya IoT Platform developer account (use
   the **same email** as your Smart Life app account to simplify linking)

---

## Step 1: Create a Tuya IoT Platform Account

1. Open your browser and go to **https://iot.tuya.com**
2. Click **Register** (top right)
3. Enter your email, create a password, and complete the verification
4. Log in to the Tuya IoT Platform

> **Tip:** Use the same email as your Smart Life / Tuya Smart app. This makes
> Step 4 (account linking) easier.

---

## Step 2: Create a Cloud Development Project

1. In the left sidebar, click **Cloud** then **Development**
2. Click **Create Cloud Project** (blue button, top right)
3. Fill in the project details:
   - **Project Name:** `ScarGuard` (or any name you prefer)
   - **Description:** `ScarGuard deterrent control` (optional)
   - **Industry:** Select **Smart Home**
   - **Development Method:** Select **Smart Home**
   - **Data Center:** Choose the region closest to you:
     - **Western America** → `us` (for US users)
     - **Central Europe** → `eu`
     - **China** → `cn`
     - **India** → `in`
     
     > **Important:** Remember which Data Center you choose — you'll need
     > this as the `api_region` value in your ScarGuard config.

4. Click **Create**

---

## Step 3: Subscribe to Required API Services

After creating the project, you'll be prompted to select API services.
Subscribe to these (they're free):

- **IoT Core** (required)
- **Smart Home Device Management** (required)
- **Authorization Token Management** (required)

If you're not prompted automatically:

1. In your project, go to the **Service API** tab
2. Click **Go to Authorize**
3. Search for and subscribe to the services listed above

---

## Step 4: Link Your Smart Life App Account

This step connects your physical devices to the Cloud API.

1. In your project, go to the **Devices** tab
2. Click **Link Tuya App Account**
3. Choose **Automatic Link**
4. A QR code will appear on screen
5. Open the **Smart Life** app on your phone:
   - Go to **Profile** (bottom right tab)
   - Tap the **scan icon** (top right, looks like a viewfinder)
   - Scan the QR code displayed on your computer screen
6. Confirm the link in the app when prompted
7. Your devices should now appear in the Devices tab

> **Troubleshooting:** If no devices appear after linking:
> - Make sure you used the same email for iot.tuya.com and Smart Life
> - Check that your Data Center region matches your Smart Life region
> - Try the **Manual Link** option using your Smart Life account credentials

---

## Step 5: Get Your API Credentials

1. Navigate to your project's **Overview** page (click the project name)
2. Find these two values:
   - **Access ID / Client ID** — this is your `api_key`
   - **Access Secret / Client Secret** — click "Show" to reveal, then copy

> **Keep these secret.** Anyone with these credentials can control your devices.

---

## Step 6: Get Device IDs

1. In your project, go to the **Devices** tab
2. Each linked device shows a **Device ID** column
3. Copy the Device ID for each device you want ScarGuard to control

> **Tip:** The Device ID is a long string like `eb074b33939a9f6effqm7l`.
> Device names in the Tuya portal match what you see in the Smart Life app.

---

## Step 7: Configure ScarGuard

Add the `deterrent` section to your `scarguard.yml`:

```yaml
deterrent:
  enabled: true

  tuya:
    api_key: "your-access-id-here"
    api_secret: "your-access-secret-here"
    api_region: "us"    # must match the Data Center from Step 2

  devices:
    - name: pond-north-valve       # any descriptive name
      device_id: "your-device-id"  # from Step 6
      type: sprinkler              # sprinkler | light | sound | plug
      enabled: true

    # Add more devices as needed:
    # - name: pond-light
    #   device_id: "another-device-id"
    #   type: light
    #   enabled: true

  defaults:
    device_count_range: [1, 4]
    spray_duration_range: [3, 8]
    inter_device_delay_range: [1, 5]
    pre_delay_range: [0, 3]
    cooldown_seconds: 60

  battery_monitor:
    enabled: true
    check_interval_hours: 24
    alert_threshold_percent: 20
```

---

## Step 8: Test

1. Restart your ScarGuard stack:
   ```bash
   docker compose down && docker compose up -d
   ```
2. Check the deterrent service logs:
   ```bash
   docker compose logs deterrent -f
   ```
3. You should see:
   ```
   ScarGuard deterrent service starting
   Tuya Cloud controller initialised (region=us)
   Actuation enabled — N device(s) registered, cooldown 60s
   Subscribed to Redis channel: scarguard:detections
   ```
4. When a detection event fires, the deterrent service will activate your
   devices with randomized timing.

---

## Troubleshooting

### "sign invalid" error
- Double-check `api_key` and `api_secret` — copy-paste directly from iot.tuya.com
- Verify `api_region` matches the Data Center you selected in Step 2
  (Western America = `us`, Central Europe = `eu`)

### "permission deny" error
- Go to **Service API** in your project and ensure you've subscribed to
  all three required API services (Step 3)

### "device offline" error
- Open the Smart Life app and verify you can still control the device
- Battery-powered devices sleep between cloud check-ins — this is normal.
  The Cloud API queues commands for pickup on the next wake cycle (~1-2s)

### No devices appear after linking
- The Smart Life app account email must match the iot.tuya.com account email
- The Data Center region must match. US users with a "Western America"
  Smart Life account must select "Western America" in iot.tuya.com

### Rate limits
- Tuya's free tier allows approximately 500 API calls/day
- Each actuation sequence uses ~2 API calls per device (ON + OFF)
- With 4 devices and a 60-second cooldown, you'd need ~60 events/day
  to approach the limit — unlikely in normal operation

---

## Security Notes

- API credentials in `scarguard.yml` are stored in plaintext. The config
  file is mounted read-only in the deterrent container.
- **Do not commit `scarguard.yml` to git** if it contains credentials.
  The config file lives in an external data volume, not in the repo.
- Credentials are redacted in the web UI for the `viewer` role.
- Encrypted config secrets are planned for a future release (v0.14.x or
  v0.15.x).

---

## What's Next

Once the deterrent service is running, detections will automatically trigger
your devices. The MVP fires all enabled devices on any detection. Future
releases (v0.13.x) will add response profiles for species-based routing:

- Heron detected → all deterrents fire (sprinklers + lights + sirens)
- Raccoon at night → lights and sound only
- Duck → gentle single-sprinkler deterrence

See [ROADMAP.md](ROADMAP.md) for the full plan.
