/* ScarGuard — structured config form logic */

// ── Chip-picker shadow registries (v0.13.4) ──────────────────────────────────
// Each ChipPicker reads its registry via a callback, so the pickers stay in
// sync as these lists change (channels renamed/added/removed, model swapped).
window._availableChannels = window._availableChannels || [];
window._classesByModel = window._classesByModel || {};
window._chipPickers = window._chipPickers || [];

function _channelsRegistry() { return window._availableChannels || []; }
function _groupsRegistry() { return window._availableGroups || []; }
function _classesFor(modelPath) {
  const key = modelPath || (document.getElementById("det-model-path") || {}).value || "";
  return window._classesByModel[key] || [];
}

function _refreshChipPickers() {
  (window._chipPickers || []).forEach(p => { try { p.refresh(); } catch (_) {} });
}

// Rebuild _availableChannels from the Notification Channels subtab UI.
// Called on mutation of the channels list.
function _rebuildChannelRegistry() {
  const names = Array.from(document.querySelectorAll("#channels-list .ch-name"))
    .map(el => el.value.trim())
    .filter(Boolean);
  window._availableChannels = Array.from(new Set(names));
  _refreshChipPickers();
}

// Fetch class list for a given model path via /models/{file}/classes; cache
// in window._classesByModel.  Returns the cached array when available.
async function _loadClassesForModel(modelPath) {
  if (!modelPath) return [];
  if (window._classesByModel[modelPath]) return window._classesByModel[modelPath];
  // Endpoint is keyed on the file's basename regardless of whether MODELS_DIR
  // is `/models` or something custom like `/mnt/models`.  Derive the last
  // path segment (works for absolute paths, bare filenames, or Windows-style
  // separators that might sneak in through manual YAML edits).
  const filename = modelPath.split(/[\\/]/).filter(Boolean).pop() || modelPath;
  try {
    const resp = await fetch("/models/" + encodeURIComponent(filename) + "/classes");
    if (!resp.ok) { window._classesByModel[modelPath] = []; return []; }
    const data = await resp.json();
    const classes = (data && data.ok && Array.isArray(data.classes)) ? data.classes : [];
    window._classesByModel[modelPath] = classes;
    _refreshChipPickers();
    return classes;
  } catch (_) {
    window._classesByModel[modelPath] = [];
    return [];
  }
}

function _trackChipPicker(picker) {
  window._chipPickers.push(picker);
  return picker;
}

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

// ── Settings sub-tabs ────────────────────────────────────────────────────────
// Split the long Settings form into focused sub-tabs (System / Detection /
// Cameras / Notifications / Advanced).  Each config-section carries a
// data-subtab attribute; we toggle visibility based on the active button.
// URL hash (e.g. #cameras) deep-links into a sub-tab.

function _activateSubtab(name) {
  name = name || "system";
  document.querySelectorAll(".subtab-btn").forEach(b => {
    b.classList.toggle("active", b.dataset.subtab === name);
  });
  document.querySelectorAll(".config-section[data-subtab]").forEach(sec => {
    sec.classList.toggle("subtab-hidden", sec.dataset.subtab !== name);
  });
  try {
    history.replaceState(null, "", "#" + name);
  } catch (_) { /* older browsers */ }
}

document.querySelectorAll(".subtab-btn").forEach(btn => {
  btn.addEventListener("click", () => _activateSubtab(btn.dataset.subtab));
});

(function initSubtab() {
  const valid = ["system", "detection", "cameras", "notifications", "advanced"];
  const hash = (location.hash || "").replace(/^#/, "");
  _activateSubtab(valid.includes(hash) ? hash : "system");
})();

// ── Collapsible sections ──────────────────────────────────────────────────────

function toggleSection(id) {
  document.getElementById(id).classList.toggle("collapsed");
}

// ── Expert mode toggle ───────────────────────────────────────────────────────

function toggleExpertMode(enabled) {
  document.body.classList.toggle("expert-mode", enabled);
  localStorage.setItem("scarguard-expert-mode", enabled ? "1" : "0");
}

(function restoreExpertMode() {
  if (localStorage.getItem("scarguard-expert-mode") === "1") {
    document.body.classList.add("expert-mode");
    const toggle = document.getElementById("expert-mode-toggle");
    if (toggle) toggle.checked = true;
  }
})();

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

function _buildRuleRow(rule) {
  const row = document.createElement("div");
  row.className = "rule-row";
  row.innerHTML = `
    <div class="field-row" style="gap:0.5rem;align-items:flex-end;">
      <div class="field-group" style="flex:1;">
        <label>Class (or *)</label>
        <input type="text" class="rule-class" value="${_esc(rule.class_name || "*")}" placeholder="* or great_blue_heron">
      </div>
      <div class="field-group" style="flex:2;">
        <label>Channels (comma-separated names)</label>
        <input type="text" class="rule-channels" value="${_esc((rule.channels || []).join(", "))}" placeholder="pond-alerts, email-digest">
      </div>
      <button type="button" class="btn-remove" onclick="this.closest('.rule-row').remove()">✕</button>
    </div>
  `;
  return row;
}

function _attachNotifRulePickers(row, classRegistry) {
  const classInput = row.querySelector(".rule-class");
  const chanInput = row.querySelector(".rule-channels");
  if (classInput && !classInput.dataset.chipAttached) {
    classInput.dataset.chipAttached = "1";
    _trackChipPicker(ChipPicker.create(classInput, {
      registry: classRegistry || (() => _classesFor()),
      values: classInput.value.trim() ? [classInput.value.trim()] : [],
      onChange: (vals) => { classInput.value = (vals[0] || ""); },
      singleValue: true,
      alwaysAvailable: ["*"],
      placeholder: "* or class name",
      allowCreate: true,  // free-text allowed for class names not yet in model
    }));
  }
  if (chanInput && !chanInput.dataset.chipAttached) {
    chanInput.dataset.chipAttached = "1";
    const initial = (chanInput.value || "").split(",").map(s => s.trim()).filter(Boolean);
    _trackChipPicker(ChipPicker.create(chanInput, {
      registry: _channelsRegistry,
      values: initial,
      onChange: (vals) => { chanInput.value = vals.join(","); },
      allowCreate: false,
      placeholder: "Type a channel name…",
    }));
  }
}

function addNotificationRule(card) {
  const row = _buildRuleRow({ class_name: "*", channels: [] });
  card.querySelector(".notif-rules-list").appendChild(row);
  // Registry callback re-reads the camera's model every render so swapping
  // the model dropdown updates this picker's class suggestions in place.
  _loadClassesForModel(_cameraModelPath(card));
  _attachNotifRulePickers(row, () => _classesFor(_cameraModelPath(card)));
}

function readNotificationRules(card) {
  return Array.from(card.querySelectorAll(".notif-rules-list .rule-row")).map(row => ({
    class_name: row.querySelector(".rule-class").value.trim() || "*",
    channels: row.querySelector(".rule-channels").value
      .split(",").map(s => s.trim()).filter(Boolean),
  }));
}

// ── Deterrent rules (per-camera) ────────────────────────────────────────────

function _buildDeterrentRuleRow(rule) {
  const row = document.createElement("div");
  row.className = "rule-row det-rule-row";
  row.innerHTML = `
    <div class="field-row" style="gap:0.5rem;align-items:flex-end;">
      <div class="field-group" style="flex:1;">
        <label>Class (or *)</label>
        <input type="text" class="drule-class" value="${_esc(rule.class_name || "*")}" placeholder="* or great_blue_heron">
      </div>
      <div class="field-group" style="flex:2;">
        <label>Groups (comma-separated names from /admin/deterrent)</label>
        <input type="text" class="drule-groups" value="${_esc((rule.groups || []).join(", "))}" placeholder="minor, thermonuclear">
      </div>
      <button type="button" class="btn-remove" onclick="this.closest('.rule-row').remove()">✕</button>
    </div>
  `;
  return row;
}

function _attachDeterrentRulePickers(row, classRegistry) {
  const classInput = row.querySelector(".drule-class");
  const groupInput = row.querySelector(".drule-groups");
  if (classInput && !classInput.dataset.chipAttached) {
    classInput.dataset.chipAttached = "1";
    _trackChipPicker(ChipPicker.create(classInput, {
      registry: classRegistry || (() => _classesFor()),
      values: classInput.value.trim() ? [classInput.value.trim()] : [],
      onChange: (vals) => { classInput.value = (vals[0] || ""); },
      singleValue: true,
      alwaysAvailable: ["*"],
      placeholder: "* or class name",
      allowCreate: true,
    }));
  }
  if (groupInput && !groupInput.dataset.chipAttached) {
    groupInput.dataset.chipAttached = "1";
    const initial = (groupInput.value || "").split(",").map(s => s.trim()).filter(Boolean);
    _trackChipPicker(ChipPicker.create(groupInput, {
      registry: _groupsRegistry,
      values: initial,
      onChange: (vals) => { groupInput.value = vals.join(","); },
      allowCreate: false,
      placeholder: "Type a group name…",
    }));
  }
}

function addDeterrentRule(card) {
  const row = _buildDeterrentRuleRow({ class_name: "*", groups: [] });
  card.querySelector(".det-rules-list").appendChild(row);
  _loadClassesForModel(_cameraModelPath(card));
  _attachDeterrentRulePickers(row, () => _classesFor(_cameraModelPath(card)));
}

function _cameraModelPath(card) {
  const cam = card && card.querySelector ? card.querySelector(".cam-model-path") : null;
  const val = cam ? (cam.value || "").trim() : "";
  return val || (document.getElementById("det-model-path") || {}).value || "";
}

function readDeterrentRules(card) {
  return Array.from(card.querySelectorAll(".det-rules-list .rule-row")).map(row => ({
    class_name: row.querySelector(".drule-class").value.trim() || "*",
    groups: row.querySelector(".drule-groups").value
      .split(",").map(s => s.trim()).filter(Boolean),
  }));
}

function _buildModelSelect(currentPath) {
  const models = window._availableModels || [];
  if (!models.length) {
    return `<input type="text" class="cam-model-path" value="${_esc(currentPath || "")}" placeholder="(Use global model)">`;
  }
  const opts = ['<option value="">(Use global model)</option>']
    .concat(models.map(m =>
      `<option value="${_esc(m)}" ${currentPath === m ? "selected" : ""}>${_esc(m)}</option>`
    ));
  // If the current value is set but not in the model list, add it as an option
  if (currentPath && !models.includes(currentPath)) {
    opts.push(`<option value="${_esc(currentPath)}" selected>${_esc(currentPath)} (missing)</option>`);
  }
  return `<select class="cam-model-path">${opts.join("")}</select>`;
}

function buildCameraCard(cam) {
  const idx = cameraIndex++;
  const enabled = cam.enabled !== false;
  const zones = cam.exclusion_zones || [];
  // notification_rules was renamed from action_rules in v0.13.3; accept
  // either key on incoming data so the form survives pre-migration configs.
  const rules = cam.notification_rules || cam.action_rules || [];
  const detRules = cam.deterrent_rules || [];
  const camConf = (cam.confidence_threshold != null) ? cam.confidence_threshold : "";
  const snapUrl = cam.snapshot_url || null;

  const div = document.createElement("div");
  div.className = "camera-card";
  div.dataset.idx = idx;
  div.innerHTML = `
    <div class="camera-card-header">
      <span class="camera-card-title">Camera</span>
      <button type="button" class="btn-remove" onclick="removeCamera(this)">Remove</button>
    </div>
    <label class="toggle-label">
      <input type="checkbox" class="cam-enabled" ${enabled ? "checked" : ""}>
      <span class="toggle-track"></span>
      Enabled
    </label>
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
    <details class="expert-only" style="margin-top:0.75rem;">
      <summary style="cursor:pointer;font-weight:500;">Per-Camera Model, Classes & Confidence</summary>
      <div style="margin-top:0.5rem;">
        <p class="hint">
          Override the global detection model, target classes, and/or confidence
          threshold for this camera.  Leave blank to inherit the global values
          from the Detection section.
        </p>
        <div class="field-row">
          <div class="field-group">
            <label>Model (blank = global)</label>
            ${_buildModelSelect(cam.model_path)}
          </div>
          <div class="field-group">
            <label>Detect classes (comma-separated, blank = global)</label>
            <input type="text" class="cam-detect-classes" value="${_esc((cam.detect_classes || []).join(", "))}" placeholder="e.g. great_blue_heron, green_heron">
          </div>
        </div>
        <div class="field-row" style="margin-top:0.5rem;">
          <div class="field-group" style="flex:1;">
            <label>Confidence threshold (blank = inherit global)</label>
            <div style="display:flex;gap:0.5rem;align-items:center;">
              <input type="number" class="cam-confidence" min="0" max="1" step="0.01"
                     value="${camConf}" placeholder="inherit" style="max-width:8rem;">
              <span class="hint">Higher = fewer false positives. Useful for fine-tuned species models.</span>
            </div>
          </div>
        </div>
      </div>
    </details>
    <details class="expert-only" style="margin-top:0.75rem;">
      <summary style="cursor:pointer;font-weight:500;">Exclusion Zones (${zones.length})</summary>
      <div style="margin-top:0.5rem;">
        <p class="hint">
          Draw rectangles to suppress false positives. Detections whose center falls inside a zone are silently ignored.
          ${snapUrl ? "Drag on the image to draw a zone. Click a zone to delete it." : "No snapshot available yet — add a zone below or wait for the first detection."}
        </p>
        <div class="zone-canvas-wrap" style="position:relative;display:inline-block;max-width:100%;">
          ${snapUrl ? `<img class="zone-bg-img" src="${_esc(snapUrl)}" style="display:block;max-width:100%;border-radius:4px;" draggable="false">` : '<div class="zone-bg-img" style="width:640px;height:360px;background:#1a1a2e;border-radius:4px;"></div>'}
          <canvas class="zone-canvas" style="position:absolute;top:0;left:0;width:100%;height:100%;cursor:crosshair;"></canvas>
        </div>
        <div class="zone-list" style="margin-top:0.4rem;"></div>
      </div>
    </details>
    <details class="expert-only" style="margin-top:0.75rem;">
      <summary style="cursor:pointer;font-weight:500;">Notification Rules (${rules.length})</summary>
      <div style="margin-top:0.5rem;">
        <p class="hint">
          Route detections to specific named channels. Rules are evaluated in order — first match wins.
          Use <code>*</code> as a wildcard class to match any detection. Leave empty to notify all channels.
        </p>
        <div class="notif-rules-list"></div>
        <button type="button" class="btn-add" style="margin-top:0.4rem;" onclick="addNotificationRule(this.closest('.camera-card'))">+ Add Rule</button>
      </div>
    </details>
    <details class="expert-only" style="margin-top:0.75rem;">
      <summary style="cursor:pointer;font-weight:500;">Deterrent Rules (${detRules.length})</summary>
      <div style="margin-top:0.5rem;">
        <p class="hint">
          Fire deterrent groups in response to detections from this camera.
          Rules are evaluated in order — first match wins.  Use <code>*</code>
          as a wildcard class.  <strong>Empty = no deterrent action</strong> —
          deterrents are explicit-opt-in.  Create groups on the
          <a href="/admin/deterrent#groups">Deterrent page</a> first.
        </p>
        <div class="det-rules-list"></div>
        <button type="button" class="btn-add" style="margin-top:0.4rem;" onclick="addDeterrentRule(this.closest('.camera-card'))">+ Add Rule</button>
      </div>
    </details>
  `;

  // Populate initial rules (synchronous — no layout dependency)
  const notifList = div.querySelector(".notif-rules-list");
  rules.forEach(r => notifList.appendChild(_buildRuleRow(r)));
  const detList = div.querySelector(".det-rules-list");
  detRules.forEach(r => detList.appendChild(_buildDeterrentRuleRow(r)));

  // Chip pickers need the DOM to be in place first, so defer one tick.
  requestAnimationFrame(() => {
    // Registry callback re-reads the camera's current model path every time
    // a picker calls it.  Swapping the model in the dropdown below will make
    // every chip picker on this card pick up the new class list on the next
    // render, so "unknown" chips re-resolve and autocomplete targets the
    // right registry — no card-rebuild needed.
    const classReg = () => _classesFor(_cameraModelPath(div));
    _loadClassesForModel(_cameraModelPath(div));

    notifList.querySelectorAll(".rule-row").forEach(row => _attachNotifRulePickers(row, classReg));
    detList.querySelectorAll(".rule-row").forEach(row => _attachDeterrentRulePickers(row, classReg));

    // Per-camera detect_classes chip picker — registry is the camera's model.
    const detectInput = div.querySelector(".cam-detect-classes");
    if (detectInput && !detectInput.dataset.chipAttached) {
      detectInput.dataset.chipAttached = "1";
      const initial = (detectInput.value || "")
        .split(",").map(s => s.trim()).filter(Boolean);
      _trackChipPicker(ChipPicker.create(detectInput, {
        registry: classReg,
        values: initial,
        onChange: (vals) => { detectInput.value = vals.join(","); },
        allowCreate: true,       // allow future classes not yet in model
        placeholder: "Type a class name (blank = inherit global)…",
      }));
    }

    // When the camera's model changes, fetch the new class list and refresh
    // all pickers — registry callbacks re-read the model path, so this pass
    // resolves unknown chips and retargets autocomplete in place.
    const modelEl = div.querySelector(".cam-model-path");
    if (modelEl && !modelEl.dataset.chipWired) {
      modelEl.dataset.chipWired = "1";
      modelEl.addEventListener("change", () => {
        _loadClassesForModel(_cameraModelPath(div)).then(() => _refreshChipPickers());
      });
    }

    initZoneEditor(div, zones);
  });
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
  return Array.from(document.querySelectorAll("#cameras-list .camera-card")).map(card => {
    const modelPath = card.querySelector(".cam-model-path").value.trim() || null;
    const detectClassesRaw = card.querySelector(".cam-detect-classes").value.trim();
    const detectClasses = detectClassesRaw
      ? detectClassesRaw.split(",").map(s => s.trim()).filter(Boolean)
      : null;
    const confRaw = card.querySelector(".cam-confidence").value.trim();
    // Always emit confidence_threshold (possibly null) so clearing the UI
    // field actually removes the override server-side.  Pydantic accepts
    // null → field becomes None; the merge in routes/config.py preserves
    // it correctly via model_dump(exclude_unset=True).
    const confidence = confRaw === "" ? null : parseFloat(confRaw);
    const cam = {
      name: card.querySelector(".cam-name").value.trim(),
      rtsp_url: card.querySelector(".cam-rtsp").value.trim(),
      enabled: card.querySelector(".cam-enabled").checked,
      resolution: parseInt(card.querySelector(".cam-resolution").value, 10) || 720,
      exclusion_zones: readZones(card),
      notification_rules: readNotificationRules(card),
      deterrent_rules: readDeterrentRules(card),
      confidence_threshold: (confidence !== null && !Number.isNaN(confidence)) ? confidence : null,
    };
    if (modelPath) cam.model_path = modelPath;
    if (detectClasses) cam.detect_classes = detectClasses;
    return cam;
  });
}

function readZones(card) {
  return (card._zones || []).map(z => ({
    x: z.x, y: z.y, w: z.w, h: z.h, label: z.label || "",
  }));
}

// ── Form serialization ────────────────────────────────────────────────────────

function readForm() {
  const targetClasses = document.getElementById("target-classes").value
    .split(",").map(s => s.trim()).filter(Boolean);

  const schedEnabled = document.getElementById("sched-enabled").checked;
  const useSolar = document.getElementById("sched-use-solar").checked;
  const schedLat = parseFloat(document.getElementById("sched-lat").value);
  const schedLon = parseFloat(document.getElementById("sched-lon").value);
  const schedule = {
    enabled: schedEnabled,
    arm_time: document.getElementById("sched-arm-time").value.trim(),
    disarm_time: document.getElementById("sched-disarm-time").value.trim(),
    use_solar: useSolar,
    latitude: useSolar && !isNaN(schedLat) ? schedLat : null,
    longitude: useSolar && !isNaN(schedLon) ? schedLon : null,
  };

  return {
    system: {
      armed: document.getElementById("sys-armed").checked,
      log_level: document.getElementById("sys-log-level").value,
      timezone: document.getElementById("sys-timezone").value.trim(),
      // base_url removed in v0.12.4 — derived from tls.domain at runtime
      retention_days: (v => isNaN(v) ? 90 : v)(parseInt(document.getElementById("sys-retention").value, 10)),
      stats_interval: (v => isNaN(v) || v < 1 ? 5 : Math.min(v, 60))(parseInt(document.getElementById("sys-stats-interval").value, 10)),
      visit_timeout_seconds: parseInt(document.getElementById('visit_timeout_seconds')?.value || '300'),
      training_nudge_threshold: parseInt(document.getElementById('training_nudge_threshold')?.value || '100'),
      camera_health: {
        alert_threshold_minutes: parseInt(document.getElementById('camera_health_alert_threshold_minutes')?.value || '10'),
        debounce_seconds: parseInt(document.getElementById('camera_health_debounce_seconds')?.value || '30'),
      },
      backup: {
        max_backups: parseInt(document.getElementById('backup_max_backups')?.value || '50'),
        debounce_seconds: parseInt(document.getElementById('backup_debounce_seconds')?.value || '180'),
      },
      schedule,
      auth: {
        enabled: document.getElementById("auth-enabled").checked,
        session_timeout_hours: parseInt(document.getElementById("auth-session-timeout").value, 10) || 24,
        max_login_attempts: parseInt(document.getElementById("auth-max-attempts").value, 10) || 5,
        lockout_duration_minutes: parseInt(document.getElementById("auth-lockout-minutes").value, 10) || 15,
        require_api_auth: document.getElementById("auth-require-api").checked,
        nonadmin_rearm_minutes: parseInt(document.getElementById("auth-rearm-minutes").value, 10) || 0,
      },
      summary_report: {
        enabled: document.getElementById("summary-report-enabled").checked,
        frequency: document.getElementById("summary-report-frequency").value,
        time: document.getElementById("summary-report-time").value.trim() || "07:00",
        channels: (document.getElementById("summary-report-channels").value || "")
          .split(",").map(s => s.trim()).filter(Boolean),
      },
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
      channels: readChannels(),
    },
    tls: {
      mode: document.getElementById("tls-mode").value,
      domain: document.getElementById("tls-domain").value.trim(),
      cert_path: document.getElementById("tls-cert-path").value.trim(),
      key_path: document.getElementById("tls-key-path").value.trim(),
    },
    deterrent: {
      enabled: document.getElementById("deterrent-enabled").checked,
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

  const tls = data.tls;
  if (tls.mode === "auto" && !tls.domain)
    errors.push("TLS: Domain name is required for automatic (Let's Encrypt) mode");
  if (tls.mode === "manual" && !tls.cert_path)
    errors.push("TLS: Certificate path is required for manual mode");
  if (tls.mode === "manual" && !tls.key_path)
    errors.push("TLS: Key path is required for manual mode");

  const sched = data.system.schedule;
  const timeRe = /^\d{2}:\d{2}$/;
  if (sched.arm_time && !timeRe.test(sched.arm_time))
    errors.push("Schedule: arm_time must be in HH:MM format");
  if (sched.disarm_time && !timeRe.test(sched.disarm_time))
    errors.push("Schedule: disarm_time must be in HH:MM format");
  if (sched.enabled && sched.use_solar && (sched.latitude === null || sched.longitude === null))
    errors.push("Schedule: latitude and longitude are required for solar mode");

  return errors;
}

// ── Save ──────────────────────────────────────────────────────────────────────

function _showBanner(kind, text, warnings) {
  // kind: "ok" | "err" | "warn".  Scrolls into view so the user sees it
  // regardless of which sub-tab / scroll position they were in (banner lives
  // at the top of the Settings tab).
  const banner = document.getElementById("form-banner");
  if (!banner) return;
  banner.className = (kind === "err")
    ? "alert alert-err"
    : (kind === "warn") ? "alert alert-warn"
    : (kind === "ok")   ? "alert alert-ok"
    : "";
  banner.innerHTML = "";
  if (text) {
    const p = document.createElement("div");
    p.textContent = text;
    banner.appendChild(p);
  }
  if (Array.isArray(warnings) && warnings.length) {
    const ul = document.createElement("ul");
    ul.style.margin = "0.4em 0 0";
    ul.style.paddingLeft = "1.2em";
    warnings.forEach(w => {
      const li = document.createElement("li");
      li.textContent = w;
      ul.appendChild(li);
    });
    banner.appendChild(ul);
  }
  try { banner.scrollIntoView({ behavior: "smooth", block: "center" }); } catch (_) {}
}

async function saveConfig() {
  // Belt-and-braces read-only guard — the DOM hardening in config.html
  // already hides the Save button, and the server rejects the POST with
  // 403, but this stops a stray onclick or test script from firing a
  // spurious request.
  if (window.SCARGUARD_READ_ONLY) return;
  _showBanner(null, "");

  const data = readForm();
  const errors = validate(data);
  if (errors.length) {
    _showBanner("err", errors.join(" · "));
    return;
  }

  const btn = document.getElementById("save-btn");
  btn.disabled = true;
  btn.textContent = "Saving…";

  try {
    const resp = await fetch("/config/structured", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() },
      body: JSON.stringify(data),
    });
    const result = await resp.json();
    if (result.ok) {
      const base = result.tls_changed
        ? "Config saved. TLS settings changed — Caddy will reload within a few seconds."
        : "Config saved. Changes take effect within ~10 seconds.";
      const warnings = Array.isArray(result.warnings) ? result.warnings : [];
      _showBanner(warnings.length ? "warn" : "ok", base, warnings);
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
      // Server returns a generic message + request_id (issue #95). Surface
      // the request_id so the operator can correlate with web container logs.
      let msg = "Error: " + (result.error || "unknown");
      if (result.request_id) msg += " (request_id=" + result.request_id + ")";
      _showBanner("err", msg);
    }
  } catch (e) {
    _showBanner("err", "Network error: " + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Save";
  }
}

// ── Notification channel management ──────────────────────────────────────────

let channelIndex = 0;

const _CHANNEL_FIELDS = {
  discord: [
    { id: "webhook_url", label: "Webhook URL", type: "text", placeholder: "https://discord.com/api/webhooks/…" },
    { id: "mention_role", label: "Mention role ID (optional)", type: "text", placeholder: "" },
    { id: "include_snapshot", label: "Attach snapshot image", type: "checkbox", default: true },
  ],
  email: [
    { id: "smtp_host", label: "SMTP host", type: "text", placeholder: "smtp.gmail.com" },
    { id: "smtp_port", label: "SMTP port", type: "number", placeholder: "587" },
    { id: "smtp_user", label: "SMTP username", type: "text", placeholder: "you@example.com" },
    { id: "smtp_pass", label: "SMTP password", type: "password", placeholder: "" },
    { id: "to_addresses", label: "Recipients (one per line)", type: "textarea", placeholder: "you@example.com" },
    { id: "include_snapshot", label: "Attach snapshot image", type: "checkbox", default: true },
  ],
  webhook: [
    { id: "url", label: "URL", type: "text", placeholder: "https://example.com/webhook" },
    { id: "method", label: "HTTP method", type: "select", options: ["POST", "PUT"], default: "POST" },
    { id: "auth_token", label: "Bearer token (optional)", type: "password", placeholder: "" },
  ],
  ntfy: [
    { id: "server", label: "Server URL", type: "text", placeholder: "https://ntfy.sh", default: "https://ntfy.sh" },
    { id: "topic", label: "Topic", type: "text", placeholder: "scarguard-alerts" },
    { id: "token", label: "Access token (optional)", type: "password", placeholder: "" },
    { id: "username", label: "Username (optional, alternative to token)", type: "text", placeholder: "" },
    { id: "password", label: "Password (optional)", type: "password", placeholder: "" },
    { id: "priority", label: "Priority", type: "select", options: ["1", "2", "3", "4", "5"], default: "3" },
    { id: "include_snapshot", label: "Attach snapshot image", type: "checkbox", default: true },
  ],
};

function buildChannelCard(ch) {
  const idx = channelIndex++;
  const type = (ch.type || "discord").toLowerCase();
  const enabled = ch.enabled !== false;

  const div = document.createElement("div");
  div.className = "camera-card"; // reuse camera-card styles
  div.dataset.idx = idx;
  div.dataset.chtype = type;

  const fields = _CHANNEL_FIELDS[type] || [];
  const fieldsHtml = fields.map(f => {
    const val = ch[f.id] !== undefined ? ch[f.id] : (f.default !== undefined ? f.default : "");
    if (f.type === "checkbox") {
      return `<label class="toggle-label"><input type="checkbox" class="ch-field" data-field="${f.id}" ${val ? "checked" : ""}><span class="toggle-track"></span> ${f.label}</label>`;
    }
    if (f.type === "textarea") {
      const lines = Array.isArray(val) ? val.join("\n") : (val || "");
      return `<div class="field-group"><label>${f.label}</label><textarea class="ch-field" data-field="${f.id}" rows="3" style="width:100%;">${_esc(lines)}</textarea></div>`;
    }
    if (f.type === "select") {
      const opts = (f.options || []).map(o => `<option value="${o}" ${val === o ? "selected" : ""}>${o}</option>`).join("");
      return `<div class="field-group"><label>${f.label}</label><select class="ch-field" data-field="${f.id}">${opts}</select></div>`;
    }
    return `<div class="field-group"><label>${f.label}</label><input type="${f.type}" class="ch-field" data-field="${f.id}" value="${_esc(String(val))}" placeholder="${_esc(f.placeholder || "")}"></div>`;
  }).join("");

  div.innerHTML = `
    <div class="camera-card-header">
      <span class="camera-card-title">${type.charAt(0).toUpperCase() + type.slice(1)} Channel</span>
      <span>
        <button type="button" class="btn-add" onclick="sendTestNotification(this)" style="margin-right:0.5rem;">Send Test</button>
        <button type="button" class="btn-remove" onclick="removeChannel(this)">Remove</button>
      </span>
    </div>
    <label class="toggle-label">
      <input type="checkbox" class="ch-enabled" ${enabled ? "checked" : ""}>
      <span class="toggle-track"></span>
      Enabled
    </label>
    <div class="field-row">
      <div class="field-group">
        <label>Channel name (unique)</label>
        <input type="text" class="ch-name" value="${_esc(ch.name || "")}" placeholder="e.g. pond-alerts">
      </div>
    </div>
    ${fieldsHtml}
  `;
  return div;
}

function addChannel(type) {
  const card = buildChannelCard({ type, name: "", enabled: true });
  document.getElementById("channels-list").appendChild(card);
}

function removeChannel(btn) {
  btn.closest(".camera-card").remove();
}

function readChannels() {
  return Array.from(document.querySelectorAll("#channels-list .camera-card")).map(card => {
    const type = card.dataset.chtype;
    const ch = {
      name: card.querySelector(".ch-name").value.trim(),
      type,
      enabled: card.querySelector(".ch-enabled").checked,
    };
    card.querySelectorAll(".ch-field").forEach(el => {
      const field = el.dataset.field;
      if (el.type === "checkbox") {
        ch[field] = el.checked;
      } else if (el.tagName === "TEXTAREA") {
        ch[field] = el.value.split("\n").map(s => s.trim()).filter(Boolean);
      } else if (el.type === "number") {
        const n = parseInt(el.value, 10);
        if (!isNaN(n)) ch[field] = n;
      } else {
        ch[field] = el.value.trim();
      }
    });
    return ch;
  });
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
  // Populate notification channels
  const channelsEl = document.getElementById("channels-data");
  if (channelsEl) {
    const channels = JSON.parse(channelsEl.textContent);
    channels.forEach(ch => {
      document.getElementById("channels-list").appendChild(buildChannelCard(ch));
    });
  }
  // Chip-picker wiring (v0.13.4) — run after DOM is populated.
  requestAnimationFrame(() => _wireGlobalChipPickers());
})();

function _wireGlobalChipPickers() {
  // Shadow-channel registry — seeded from the channels list, refreshed via
  // MutationObserver + change events on channel-name inputs.
  _rebuildChannelRegistry();
  const chanList = document.getElementById("channels-list");
  if (chanList && window.MutationObserver) {
    new MutationObserver(_rebuildChannelRegistry).observe(chanList, {
      childList: true, subtree: true,
    });
    chanList.addEventListener("input", (e) => {
      if (e.target && e.target.classList.contains("ch-name")) {
        _rebuildChannelRegistry();
      }
    });
  }

  // Detection > Target classes
  const targetEl = document.getElementById("target-classes");
  if (targetEl && !targetEl.dataset.chipAttached) {
    targetEl.dataset.chipAttached = "1";
    const initial = (targetEl.value || "").split(",").map(s => s.trim()).filter(Boolean);
    _loadClassesForModel((document.getElementById("det-model-path") || {}).value || "");
    _trackChipPicker(ChipPicker.create(targetEl, {
      registry: () => _classesFor(),
      values: initial,
      onChange: (vals) => { targetEl.value = vals.join(","); },
      allowCreate: true,
      placeholder: "Type a class name…",
    }));
  }

  // Global model-path change → refresh class registry for pickers that fall
  // back to the global model.
  const globalModelEl = document.getElementById("det-model-path");
  if (globalModelEl && !globalModelEl.dataset.chipWired) {
    globalModelEl.dataset.chipWired = "1";
    globalModelEl.addEventListener("change", () => {
      _loadClassesForModel(globalModelEl.value || "");
    });
  }

  // Summary-report channels
  const sumEl = document.getElementById("summary-report-channels");
  if (sumEl && !sumEl.dataset.chipAttached) {
    sumEl.dataset.chipAttached = "1";
    const initial = (sumEl.value || "").split(",").map(s => s.trim()).filter(Boolean);
    _trackChipPicker(ChipPicker.create(sumEl, {
      registry: _channelsRegistry,
      values: initial,
      onChange: (vals) => { sumEl.value = vals.join(","); },
      allowCreate: false,
      placeholder: "Type a channel name…",
    }));
  }
}

// ── Exclusion zone canvas editor ──────────────────────────────────────────────

function initZoneEditor(card, initialZones) {
  const canvas = card.querySelector(".zone-canvas");
  const bg = card.querySelector(".zone-bg-img");
  if (!canvas) return;

  card._zones = initialZones.map(z => Object.assign({}, z));

  // Size the canvas to match the rendered background
  function syncCanvasSize() {
    const rect = bg.getBoundingClientRect();
    if (rect.width > 0) {
      canvas.width = rect.width;
      canvas.height = rect.height;
    }
  }

  if (bg.tagName === "IMG" && !bg.complete) {
    bg.onload = () => { syncCanvasSize(); drawZones(card); };
  } else {
    syncCanvasSize();
  }

  drawZones(card);

  // Draw on mouse drag
  let dragging = false;
  let startX = 0, startY = 0;

  canvas.addEventListener("mousedown", e => {
    if (e.button !== 0) return;
    // Check if click hits an existing zone (to delete it)
    const pos = _canvasPos(canvas, e);
    const hitIdx = _hitZone(card, pos.x, pos.y);
    if (hitIdx >= 0) {
      card._zones.splice(hitIdx, 1);
      drawZones(card);
      updateZoneList(card);
      return;
    }
    dragging = true;
    startX = pos.x;
    startY = pos.y;
  });

  canvas.addEventListener("mousemove", e => {
    if (!dragging) return;
    const pos = _canvasPos(canvas, e);
    drawZones(card, { x: startX, y: startY, ex: pos.x, ey: pos.y });
  });

  canvas.addEventListener("mouseup", e => {
    if (!dragging) return;
    dragging = false;
    const pos = _canvasPos(canvas, e);
    const nx = Math.min(startX, pos.x) / canvas.width;
    const ny = Math.min(startY, pos.y) / canvas.height;
    const nw = Math.abs(pos.x - startX) / canvas.width;
    const nh = Math.abs(pos.y - startY) / canvas.height;
    if (nw > 0.01 && nh > 0.01) {
      card._zones.push({ x: nx, y: ny, w: nw, h: nh, label: "" });
      drawZones(card);
      updateZoneList(card);
    }
  });

  canvas.addEventListener("mouseleave", () => {
    if (dragging) { dragging = false; drawZones(card); }
  });

  updateZoneList(card);
}

function drawZones(card, drag) {
  const canvas = card.querySelector(".zone-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  const zones = card._zones || [];
  zones.forEach((z, i) => {
    const px = z.x * canvas.width;
    const py = z.y * canvas.height;
    const pw = z.w * canvas.width;
    const ph = z.h * canvas.height;
    ctx.fillStyle = "rgba(220,38,38,0.25)";
    ctx.fillRect(px, py, pw, ph);
    ctx.strokeStyle = "rgba(220,38,38,0.9)";
    ctx.lineWidth = 2;
    ctx.strokeRect(px, py, pw, ph);
    ctx.fillStyle = "rgba(220,38,38,0.9)";
    ctx.font = "11px sans-serif";
    ctx.fillText(z.label || `Zone ${i + 1}`, px + 4, py + 13);
  });

  if (drag) {
    const rx = Math.min(drag.x, drag.ex);
    const ry = Math.min(drag.y, drag.ey);
    const rw = Math.abs(drag.ex - drag.x);
    const rh = Math.abs(drag.ey - drag.y);
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = "rgba(255,255,255,0.8)";
    ctx.lineWidth = 1.5;
    ctx.strokeRect(rx, ry, rw, rh);
    ctx.setLineDash([]);
  }
}

function updateZoneList(card) {
  const list = card.querySelector(".zone-list");
  if (!list) return;
  list.innerHTML = "";
  const zones = card._zones || [];
  if (zones.length === 0) {
    list.innerHTML = '<p class="hint" style="margin:0;">No zones. Drag on the image above to add one.</p>';
    return;
  }
  zones.forEach((z, i) => {
    const row = document.createElement("div");
    row.style.cssText = "display:flex;gap:0.5rem;align-items:center;margin-bottom:0.3rem;";
    row.innerHTML = `
      <span style="flex:0 0 auto;color:var(--muted);">Zone ${i + 1}</span>
      <input type="text" placeholder="Label (optional)" value="${_esc(z.label || "")}"
             style="flex:1;min-width:0;">
      <button type="button" class="btn-remove" style="flex:0 0 auto;"
              onclick="deleteZone(this,${i})">✕</button>
    `;
    const input = row.querySelector("input");
    const zoneIdx = i;
    input.addEventListener("input", function() {
      if (card._zones[zoneIdx]) card._zones[zoneIdx].label = this.value;
      drawZones(card);
    });
    list.appendChild(row);
  });
}

function deleteZone(btn, idx) {
  const card = btn.closest(".camera-card");
  card._zones.splice(idx, 1);
  drawZones(card);
  updateZoneList(card);
}

function _canvasPos(canvas, e) {
  const r = canvas.getBoundingClientRect();
  return { x: e.clientX - r.left, y: e.clientY - r.top };
}

function _hitZone(card, px, py) {
  const canvas = card.querySelector(".zone-canvas");
  const zones = card._zones || [];
  for (let i = zones.length - 1; i >= 0; i--) {
    const z = zones[i];
    const zx = z.x * canvas.width, zy = z.y * canvas.height;
    const zw = z.w * canvas.width, zh = z.h * canvas.height;
    if (px >= zx && px <= zx + zw && py >= zy && py <= zy + zh) return i;
  }
  return -1;
}

// ── Schedule helpers ──────────────────────────────────────────────────────────

function toggleSolar(enabled) {
  document.getElementById("sched-solar-fields").style.display = enabled ? "block" : "none";
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function _esc(s) {
  return s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

// ── Test notification ────────────────────────────────────────────────────────

async function sendTestNotification(btn) {
  const card = btn.closest(".camera-card");
  const name = card.querySelector(".ch-name").value.trim();
  if (!name) {
    alert("Give this channel a name first.");
    return;
  }

  btn.disabled = true;
  const origText = btn.textContent;
  btn.textContent = "Sending\u2026";

  try {
    const resp = await fetch("/config/test-notification", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() },
      body: JSON.stringify({ channel: name }),
    });
    const result = await resp.json();
    if (result.ok) {
      btn.textContent = "Sent!";
      setTimeout(() => { btn.textContent = origText; }, 2000);
    } else {
      alert("Test failed: " + result.error);
      btn.textContent = origText;
    }
  } catch (e) {
    alert("Network error: " + e.message);
    btn.textContent = origText;
  } finally {
    btn.disabled = false;
  }
}
