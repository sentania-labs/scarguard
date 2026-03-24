/* ScarGuard — structured config form logic */

// ── Tabs ─────────────────────────────────────────────────────────────────────

document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    const target = btn.dataset.tab;
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(target).classList.add("active");
  });
});

// ── Collapsible sections ──────────────────────────────────────────────────────

function toggleSection(id) {
  document.getElementById(id).classList.toggle("collapsed");
}

// ── Confidence slider ─────────────────────────────────────────────────────────

const slider = document.getElementById("conf-slider");
const sliderVal = document.getElementById("conf-value");
if (slider && sliderVal) {
  slider.addEventListener("input", () => {
    sliderVal.textContent = parseFloat(slider.value).toFixed(2);
  });
}

// ── Camera management ─────────────────────────────────────────────────────────

let cameraIndex = 0;

function buildCameraCard(cam) {
  const idx = cameraIndex++;
  const enabled = cam.enabled !== false;
  const div = document.createElement("div");
  div.className = "camera-card";
  div.dataset.idx = idx;
  div.innerHTML = `
    <div class="camera-card-header">
      <span class="camera-card-title">Camera</span>
      <button type="button" class="btn-remove" onclick="removeCamera(this)">Remove</button>
    </div>
    <div class="field-row">
      <div class="field-group">
        <label>Name</label>
        <input type="text" class="cam-name" value="${_esc(cam.name || "")}" placeholder="pond-north" required>
      </div>
      <div class="field-group">
        <label>Resolution</label>
        <input type="number" class="cam-resolution" value="${cam.resolution || 720}" min="240" max="4096">
      </div>
    </div>
    <div class="field-group">
      <label>RTSP URL</label>
      <input type="text" class="cam-rtsp" value="${_esc(cam.rtsp_url || "")}" placeholder="rtsp:// or rtsps://192.168.1.1:7447/TOKEN">
    </div>
    <label class="toggle-label">
      <input type="checkbox" class="cam-enabled" ${enabled ? "checked" : ""}>
      <span class="toggle-track"></span>
      Enabled
    </label>
  `;
  return div;
}

function addCamera() {
  document.getElementById("cameras-list").appendChild(
    buildCameraCard({ name: "", rtsp_url: "", enabled: true, resolution: 720 })
  );
}

function removeCamera(btn) {
  btn.closest(".camera-card").remove();
}

function readCameras() {
  return Array.from(document.querySelectorAll(".camera-card")).map(card => ({
    name: card.querySelector(".cam-name").value.trim(),
    rtsp_url: card.querySelector(".cam-rtsp").value.trim(),
    enabled: card.querySelector(".cam-enabled").checked,
    resolution: parseInt(card.querySelector(".cam-resolution").value, 10) || 720,
  }));
}

// ── Form serialization ────────────────────────────────────────────────────────

function readForm() {
  const toAddresses = document.getElementById("email-to").value
    .split("\n").map(s => s.trim()).filter(Boolean);

  const targetClasses = document.getElementById("target-classes").value
    .split(",").map(s => s.trim()).filter(Boolean);

  return {
    system: {
      armed: document.getElementById("sys-armed").checked,
      log_level: document.getElementById("sys-log-level").value,
      timezone: document.getElementById("sys-timezone").value.trim(),
      snapshot_retention_days: (v => isNaN(v) ? 30 : v)(parseInt(document.getElementById("sys-retention").value, 10)),
    },
    cameras: readCameras(),
    detection: {
      model_path: document.getElementById("det-model-path").value.trim(),
      confidence_threshold: parseFloat(document.getElementById("conf-slider").value),
      target_classes: targetClasses,
      cooldown_seconds: parseInt(document.getElementById("det-cooldown").value, 10),
      frame_skip: parseInt(document.getElementById("det-frame-skip").value, 10),
    },
    notifications: {
      discord: {
        enabled: document.getElementById("discord-enabled").checked,
        webhook_url: document.getElementById("discord-url").value.trim(),
        mention_role: document.getElementById("discord-role").value.trim(),
        include_snapshot: document.getElementById("discord-snapshot").checked,
      },
      email: {
        enabled: document.getElementById("email-enabled").checked,
        smtp_host: document.getElementById("email-host").value.trim(),
        smtp_port: parseInt(document.getElementById("email-port").value, 10) || 587,
        smtp_user: document.getElementById("email-user").value.trim(),
        smtp_pass: document.getElementById("email-pass").value,
        to_addresses: toAddresses,
        include_snapshot: document.getElementById("email-snapshot").checked,
      },
    },
    ssl: {
      enabled: document.getElementById("ssl-enabled").checked,
      cert_path: document.getElementById("ssl-cert-path").value.trim(),
      key_path: document.getElementById("ssl-key-path").value.trim(),
      https_only: document.getElementById("ssl-https-only").checked,
      keyfile_password: document.getElementById("ssl-key-password").value,
    },
  };
}

// ── Validation ────────────────────────────────────────────────────────────────

function validate(data) {
  const errors = [];

  data.cameras.forEach((cam, i) => {
    if (!cam.name) errors.push(`Camera ${i + 1}: name is required`);
    if (cam.rtsp_url && !cam.rtsp_url.startsWith("rtsp://") && !cam.rtsp_url.startsWith("rtsps://"))
      errors.push(`Camera ${i + 1} (${cam.name || i + 1}): RTSP URL must start with rtsp:// or rtsps://`);
  });

  const conf = data.detection.confidence_threshold;
  if (isNaN(conf) || conf < 0 || conf > 1)
    errors.push("Confidence threshold must be between 0.0 and 1.0");

  if (!data.detection.model_path)
    errors.push("Model path is required");

  const ep = data.notifications.email;
  if (ep.enabled && !ep.smtp_host)
    errors.push("Email: SMTP host is required when email notifications are enabled");
  if (ep.smtp_port < 1 || ep.smtp_port > 65535)
    errors.push("Email: SMTP port must be between 1 and 65535");

  const ssl = data.ssl;
  if (ssl.enabled && !ssl.cert_path)
    errors.push("SSL: Certificate path is required when SSL is enabled");
  if (ssl.enabled && !ssl.key_path)
    errors.push("SSL: Key path is required when SSL is enabled");

  return errors;
}

// ── Save ──────────────────────────────────────────────────────────────────────

async function saveConfig() {
  const banner = document.getElementById("form-banner");
  banner.className = "";
  banner.textContent = "";

  const data = readForm();
  const errors = validate(data);
  if (errors.length) {
    banner.className = "alert alert-err";
    banner.textContent = errors.join(" · ");
    return;
  }

  const btn = document.getElementById("save-btn");
  btn.disabled = true;
  btn.textContent = "Saving…";

  try {
    const resp = await fetch("/config/structured", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    const result = await resp.json();
    if (result.ok) {
      banner.className = "alert alert-ok";
      if (result.ssl_changed) {
        banner.textContent = "Config saved. SSL settings changed — the web service will restart automatically. This page may briefly disconnect.";
      } else {
        banner.textContent = "Config saved. Changes take effect within ~10 seconds.";
      }
      // Refresh the Advanced/Raw YAML textarea so it reflects the saved config
      try {
        const rawRes = await fetch("/config/raw");
        if (rawRes.ok) {
          const rawData = await rawRes.json();
          const yamlArea = document.querySelector("#tab-advanced textarea[name='raw_yaml']");
          if (yamlArea) yamlArea.value = rawData.yaml;
        }
      } catch (_) { /* non-critical — textarea will update on next page load */ }
    } else {
      banner.className = "alert alert-err";
      banner.textContent = "Error: " + result.error;
    }
  } catch (e) {
    banner.className = "alert alert-err";
    banner.textContent = "Network error: " + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Save";
  }
}

// ── Bootstrap ─────────────────────────────────────────────────────────────────

(function init() {
  // Populate camera cards from server-injected data
  const camerasEl = document.getElementById("cameras-data");
  if (camerasEl) {
    const cameras = JSON.parse(camerasEl.textContent);
    cameras.forEach(cam => {
      document.getElementById("cameras-list").appendChild(buildCameraCard(cam));
    });
  }
})();

// ── Helpers ───────────────────────────────────────────────────────────────────

function _esc(s) {
  return s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}
