/* ScarGuard — deterrent configuration page logic */

// ── Page data from server (CSP-safe JSON data blocks) ─────────────────────────
var _devices = JSON.parse(document.getElementById('devices-data').textContent);
var _groups = JSON.parse(document.getElementById('groups-data').textContent);
var _groupUsage = JSON.parse(document.getElementById('group-usage-data').textContent);
var _readOnly = JSON.parse(document.getElementById('deterrent-page-data').textContent).readOnly;

// ── Tab switching ────────────────────────────────────────────────────────
function _activateDetTab(name) {
  name = name || 'devices';
  document.querySelectorAll('.subtab-btn[data-det-tab]').forEach(function(b) {
    b.classList.toggle('active', b.dataset.detTab === name);
  });
  document.querySelectorAll('.det-tab-panel').forEach(function(p) {
    p.classList.toggle('active', p.dataset.detTab === name);
  });
  try { history.replaceState(null, '', '#' + name); } catch (_) {}
  if (name === 'latency') refreshLatency();
}
document.querySelectorAll('.subtab-btn[data-det-tab]').forEach(function(b) {
  b.addEventListener('click', function() { _activateDetTab(b.dataset.detTab); });
});
(function initDetTab() {
  var valid = ['devices', 'groups', 'defaults', 'battery', 'latency'];
  var hash = (location.hash || '').replace(/^#/, '');
  _activateDetTab(valid.indexOf(hash) >= 0 ? hash : 'devices');
})();

function renderDevices() {
  var tbody = document.getElementById('devices-body');
  tbody.innerHTML = '';
  _devices.forEach(function(dev, i) {
    var tr = document.createElement('tr');
    tr.style.borderBottom = '1px solid var(--border)';
    if (_readOnly) {
      tr.innerHTML =
        '<td style="padding:0.4rem 0.5rem;">' + esc(dev.name) + '</td>' +
        '<td style="padding:0.4rem 0.5rem;font-family:var(--mono);font-size:0.8rem;">' + esc(dev.device_id) + '</td>' +
        '<td style="padding:0.4rem 0.5rem;">' + esc(dev.type || 'sprinkler') + '</td>' +
        '<td style="padding:0.4rem 0.5rem;">' + (dev.enabled !== false ? 'Yes' : 'No') + '</td>';
    } else {
      tr.innerHTML =
        '<td style="padding:0.4rem 0.5rem;"><input type="text" data-idx="' + i + '" data-field="name" value="' + esc(dev.name) + '" style="width:100%;"></td>' +
        '<td style="padding:0.4rem 0.5rem;"><input type="text" data-idx="' + i + '" data-field="device_id" value="' + esc(dev.device_id) + '" style="width:100%;font-family:var(--mono);font-size:0.8rem;"></td>' +
        '<td style="padding:0.4rem 0.5rem;"><select data-idx="' + i + '" data-field="type">' +
          ['sprinkler','light','sound','plug'].map(function(t) {
            return '<option value="' + t + '"' + ((dev.type || 'sprinkler') === t ? ' selected' : '') + '>' + t + '</option>';
          }).join('') +
        '</select></td>' +
        '<td style="padding:0.4rem 0.5rem;text-align:center;"><input type="checkbox" data-idx="' + i + '" data-field="enabled"' + (dev.enabled !== false ? ' checked' : '') + '></td>' +
        '<td style="padding:0.4rem 0.5rem;"><button type="button" class="btn-secondary" style="font-size:0.75rem;padding:0.2em 0.6em;" onclick="testFire(\'' + esc(dev.device_id) + '\', this)" title="Test-fire for 3 seconds">Fire</button></td>' +
        '<td style="padding:0.4rem 0.5rem;"><button type="button" class="btn-danger-sm" onclick="removeDevice(' + i + ')" title="Remove">x</button></td>';
    }
    tbody.appendChild(tr);
  });
}

function addDevice() {
  _devices.push({name: '', device_id: '', type: 'sprinkler', enabled: true});
  renderDevices();
  // Focus the new name field
  var inputs = document.querySelectorAll('#devices-body input[data-field="name"]');
  if (inputs.length) inputs[inputs.length - 1].focus();
}

function removeDevice(idx) {
  _devices.splice(idx, 1);
  renderDevices();
}

function readDevicesFromForm() {
  var rows = document.querySelectorAll('#devices-body tr');
  var devices = [];
  rows.forEach(function(tr, i) {
    var nameEl = tr.querySelector('[data-field="name"]');
    var idEl = tr.querySelector('[data-field="device_id"]');
    var typeEl = tr.querySelector('[data-field="type"]');
    var enabledEl = tr.querySelector('[data-field="enabled"]');
    if (nameEl && idEl) {
      devices.push({
        name: nameEl.value.trim(),
        device_id: idEl.value.trim(),
        type: typeEl ? typeEl.value : 'sprinkler',
        enabled: enabledEl ? enabledEl.checked : true,
      });
    }
  });
  return devices;
}

function esc(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
                        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

async function saveDeterrent() {
  var btn = document.getElementById('save-deterrent-btn');
  var msg = document.getElementById('deterrent-msg');
  btn.disabled = true;
  msg.style.display = 'none';

  var devices = readDevicesFromForm();
  // Validate
  for (var i = 0; i < devices.length; i++) {
    if (!devices[i].name) {
      showMsg('Device ' + (i + 1) + ': name is required', true);
      btn.disabled = false;
      return;
    }
    if (!devices[i].device_id) {
      showMsg('Device "' + devices[i].name + '": Device ID is required', true);
      btn.disabled = false;
      return;
    }
  }

  var groups = readGroupsFromForm();

  var payload = {
    tuya: {
      api_key: document.getElementById('tuya-api-key').value.trim(),
      api_secret: document.getElementById('tuya-api-secret').value.trim(),
      api_region: document.getElementById('tuya-api-region').value,
    },
    devices: devices,
    groups: groups,
    defaults: {
      cooldown_seconds: parseInt(document.getElementById('def-cooldown').value) || 60,
      device_count_range: [
        parseInt(document.getElementById('def-device-min').value) || 1,
        parseInt(document.getElementById('def-device-max').value) || 4,
      ],
      spray_duration_range: [
        parseFloat(document.getElementById('def-spray-min').value) || 3.0,
        parseFloat(document.getElementById('def-spray-max').value) || 8.0,
      ],
      inter_device_delay_range: [
        parseFloat(document.getElementById('def-delay-min').value) || 1.0,
        parseFloat(document.getElementById('def-delay-max').value) || 5.0,
      ],
      pre_delay_range: [
        parseFloat(document.getElementById('def-pre-min').value) || 0.0,
        parseFloat(document.getElementById('def-pre-max').value) || 3.0,
      ],
    },
    battery_monitor: {
      enabled: document.getElementById('batt-enabled').checked,
      check_interval_hours: parseInt(document.getElementById('batt-interval').value) || 24,
      alert_threshold_percent: parseInt(document.getElementById('batt-threshold').value) || 20,
    },
  };

  try {
    var resp = await fetch('/admin/deterrent', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': getCsrfToken(),
      },
      body: JSON.stringify(payload),
    });
    var data = await resp.json();
    if (data.ok) {
      showMsg('Deterrent config saved. Changes apply within ~10 seconds.', false);
      _devices = devices;
      _groups = groups;
      renderGroups();  // re-render with updated device checkbox lists
    } else {
      showMsg('Error: ' + (data.error || 'Unknown error'), true);
    }
  } catch (e) {
    showMsg('Network error: ' + e.message, true);
  }
  btn.disabled = false;
}

function showMsg(text, isError) {
  var msg = document.getElementById('deterrent-msg');
  msg.textContent = text;
  msg.className = 'alert ' + (isError ? 'alert-err' : 'alert-ok');
  msg.style.display = '';
}

renderDevices();

// ── Device-rename propagation ────────────────────────────────────────────
// When the operator edits a device name in the Devices table, keep the
// Groups tab's device references in sync.  Without this, a group's
// ``devices: [name]`` list can refer to stale pre-rename values after a
// save, leaving the deterrent worker unable to resolve those devices.
// Event delegation on the persistent tbody survives renderDevices()
// re-renders.
(function wireDeviceRenamePropagation() {
  var tbody = document.getElementById('devices-body');
  if (!tbody) return;
  tbody.addEventListener('change', function(e) {
    var t = e.target;
    if (!t || !t.dataset || t.dataset.field !== 'name') return;
    var idx = parseInt(t.dataset.idx, 10);
    if (!Number.isFinite(idx) || idx < 0 || idx >= _devices.length) return;
    var oldName = _devices[idx].name;
    var newName = t.value.trim();
    if (oldName === newName) return;
    _devices[idx].name = newName;
    if (oldName) {
      _groups.forEach(function(g) {
        var gi = g.devices.indexOf(oldName);
        if (gi < 0) return;
        if (newName) {
          g.devices[gi] = newName;
        } else {
          g.devices.splice(gi, 1);
        }
      });
    }
    renderGroups();
  });
})();

// ── Groups editor ────────────────────────────────────────────────────────

function _groupUsageChips(name) {
  var cams = _groupUsage[name] || [];
  if (!cams.length) return '<span class="muted" style="font-size:0.8rem;">unassigned</span>';
  return cams.map(function(c) {
    return '<span class="tag" style="background:rgba(var(--accent-rgb,100,181,246),0.15);font-size:0.75rem;">' + esc(c) + '</span>';
  }).join(' ');
}

function _rangeInputs(name, idx, vals, attrs) {
  vals = vals || [null, null];
  attrs = attrs || '';
  return (
    '<input type="number" data-idx="' + idx + '" data-field="' + name + '_min" ' + attrs +
    ' value="' + (vals[0] != null ? vals[0] : '') + '" style="width:5rem;" placeholder="inherit">' +
    ' <span>to</span> ' +
    '<input type="number" data-idx="' + idx + '" data-field="' + name + '_max" ' + attrs +
    ' value="' + (vals[1] != null ? vals[1] : '') + '" style="width:5rem;" placeholder="inherit">'
  );
}

function renderGroups() {
  var list = document.getElementById('groups-list');
  list.innerHTML = '';
  if (!_groups.length) {
    list.innerHTML = '<p class="muted" style="font-style:italic;">No groups yet. Create one to arm deterrents.</p>';
    return;
  }
  _groups.forEach(function(g, i) {
    var card = document.createElement('div');
    card.className = 'camera-card';
    card.style.marginBottom = '0.75rem';
    var devCheckboxes = _devices.map(function(d) {
      var checked = (g.devices || []).indexOf(d.name) >= 0 ? ' checked' : '';
      var disabled = _readOnly ? ' disabled' : '';
      return (
        '<label style="display:inline-flex;align-items:center;gap:0.25rem;margin-right:0.75rem;font-size:0.85rem;">' +
        '<input type="checkbox" data-idx="' + i + '" data-field="device" value="' + esc(d.name) + '"' + checked + disabled + '>' +
        esc(d.name) +
        '</label>'
      );
    }).join('');
    if (!devCheckboxes) {
      devCheckboxes = '<span class="muted" style="font-size:0.85rem;">No devices registered yet — add some on the Devices tab.</span>';
    }
    card.innerHTML =
      '<div class="camera-card-header">' +
        '<span class="camera-card-title">Group: ' + esc(g.name || '(unnamed)') + '</span>' +
        (_readOnly ? '' : '<button type="button" class="btn-remove" onclick="removeGroup(' + i + ')">Remove</button>') +
      '</div>' +
      '<div class="field-row">' +
        '<div class="field-group" style="flex:1;">' +
          '<label>Name</label>' +
          '<input type="text" data-idx="' + i + '" data-field="name" value="' + esc(g.name) + '" placeholder="e.g. minor or thermonuclear"' + (_readOnly ? ' disabled' : '') + '>' +
        '</div>' +
        '<div class="field-group" style="max-width:10rem;">' +
          '<label>Cooldown (sec)</label>' +
          '<input type="number" min="5" max="3600" step="1" data-idx="' + i + '" data-field="cooldown_seconds" value="' + (g.cooldown_seconds || 60) + '"' + (_readOnly ? ' disabled' : '') + '>' +
        '</div>' +
      '</div>' +
      '<div class="field-group" style="margin-top:0.5rem;">' +
        '<label>Devices in group</label>' +
        '<div>' + devCheckboxes + '</div>' +
      '</div>' +
      '<details style="margin-top:0.5rem;"><summary style="cursor:pointer;font-size:0.85rem;">Randomization overrides (blank = inherit)</summary>' +
        '<div class="field-row" style="margin-top:0.5rem;">' +
          '<div class="field-group">' +
            '<label>Device count</label><div style="display:flex;gap:0.5rem;align-items:center;">' +
              _rangeInputs('device_count_range', i, g.device_count_range, 'min="1" max="20" step="1"' + (_readOnly ? ' disabled' : '')) +
            '</div>' +
          '</div>' +
          '<div class="field-group">' +
            '<label>Spray duration (s)</label><div style="display:flex;gap:0.5rem;align-items:center;">' +
              _rangeInputs('spray_duration_range', i, g.spray_duration_range, 'min="0.5" max="60" step="0.5"' + (_readOnly ? ' disabled' : '')) +
            '</div>' +
          '</div>' +
        '</div>' +
        '<div class="field-row" style="margin-top:0.5rem;">' +
          '<div class="field-group">' +
            '<label>Inter-device delay (s)</label><div style="display:flex;gap:0.5rem;align-items:center;">' +
              _rangeInputs('inter_device_delay_range', i, g.inter_device_delay_range, 'min="0" max="30" step="0.5"' + (_readOnly ? ' disabled' : '')) +
            '</div>' +
          '</div>' +
          '<div class="field-group">' +
            '<label>Pre-delay (s)</label><div style="display:flex;gap:0.5rem;align-items:center;">' +
              _rangeInputs('pre_delay_range', i, g.pre_delay_range, 'min="0" max="30" step="0.5"' + (_readOnly ? ' disabled' : '')) +
            '</div>' +
          '</div>' +
        '</div>' +
      '</details>' +
      '<div style="margin-top:0.5rem;font-size:0.85rem;"><span class="muted">Used by:</span> ' + _groupUsageChips(g.name) + '</div>';
    list.appendChild(card);
  });
}

function addGroup() {
  _groups.push({name: '', devices: [], cooldown_seconds: 60});
  renderGroups();
}

function removeGroup(idx) {
  _groups.splice(idx, 1);
  renderGroups();
}

function _readGroupRange(card, field) {
  var minEl = card.querySelector('[data-field="' + field + '_min"]');
  var maxEl = card.querySelector('[data-field="' + field + '_max"]');
  if (!minEl || !maxEl) return null;
  var minV = minEl.value.trim();
  var maxV = maxEl.value.trim();
  if (minV === '' && maxV === '') return null;  // inherit
  return [parseFloat(minV) || 0, parseFloat(maxV) || 0];
}

function readGroupsFromForm() {
  var out = [];
  document.querySelectorAll('#groups-list .camera-card').forEach(function(card) {
    var nameEl = card.querySelector('[data-field="name"]');
    if (!nameEl) return;
    var name = nameEl.value.trim();
    if (!name) return;
    var cooldown = parseInt(card.querySelector('[data-field="cooldown_seconds"]').value) || 60;
    var devices = [];
    card.querySelectorAll('[data-field="device"]:checked').forEach(function(cb) {
      devices.push(cb.value);
    });
    var entry = {name: name, devices: devices, cooldown_seconds: cooldown};
    ['device_count_range', 'spray_duration_range',
     'inter_device_delay_range', 'pre_delay_range'].forEach(function(f) {
      var r = _readGroupRange(card, f);
      if (r) entry[f] = r;
    });
    out.push(entry);
  });
  return out;
}

renderGroups();

// ── Latency summary ───────────────────────────────────────────────────────

async function refreshLatency() {
  var tbody = document.getElementById('latency-body');
  var countEl = document.getElementById('latency-count');
  tbody.innerHTML = '<tr><td colspan="3" class="muted" style="padding:0.5rem;">Loading...</td></tr>';
  try {
    var resp = await fetch('/admin/deterrent/latency-summary?last_n=100');
    var data = await resp.json();
    if (!data || !data.count) {
      tbody.innerHTML = '<tr><td colspan="3" class="muted" style="padding:0.5rem;">No actuation events recorded yet.</td></tr>';
      countEl.textContent = '';
      return;
    }
    function fmtMs(v) { return v == null ? '—' : v.toFixed(0) + ' ms'; }
    function fmtS(v) { return v == null ? '—' : v.toFixed(1) + ' s'; }
    tbody.innerHTML =
      '<tr><td style="padding:0.4rem 0.5rem;">Trigger delay <span class="muted" style="font-size:0.75rem;">(detection → dequeue)</span></td>' +
        '<td style="padding:0.4rem 0.5rem;">' + fmtMs(data.trigger_delay_ms.p50) + '</td>' +
        '<td style="padding:0.4rem 0.5rem;">' + fmtMs(data.trigger_delay_ms.p95) + '</td></tr>' +
      '<tr><td style="padding:0.4rem 0.5rem;">Cloud ack <span class="muted" style="font-size:0.75rem;">(ON command → Tuya success)</span></td>' +
        '<td style="padding:0.4rem 0.5rem;">' + fmtMs(data.cloud_ack_ms.p50) + '</td>' +
        '<td style="padding:0.4rem 0.5rem;">' + fmtMs(data.cloud_ack_ms.p95) + '</td></tr>' +
      '<tr><td style="padding:0.4rem 0.5rem;">Total duration <span class="muted" style="font-size:0.75rem;">(wall-clock sequence)</span></td>' +
        '<td style="padding:0.4rem 0.5rem;">' + fmtS(data.total_duration_sec.p50) + '</td>' +
        '<td style="padding:0.4rem 0.5rem;">' + fmtS(data.total_duration_sec.p95) + '</td></tr>';
    countEl.textContent = 'Based on ' + data.count + ' recent actuation(s).';
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="3" style="color:var(--danger);padding:0.5rem;">Failed to load: ' + esc(e.message) + '</td></tr>';
  }
}

async function testFire(deviceId, btn) {
  var origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Firing...';
  try {
    var resp = await fetch('/admin/deterrent/test-fire', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken()},
      body: JSON.stringify({device_id: deviceId, duration_sec: 3.0}),
    });
    var data = await resp.json();
    if (data.ok) {
      btn.textContent = 'OK';
      btn.style.color = 'var(--ok)';
    } else {
      btn.textContent = 'Fail';
      btn.style.color = 'var(--danger)';
      showMsg('Test-fire failed: ' + (data.error || 'Unknown error'), true);
    }
  } catch (e) {
    btn.textContent = 'Err';
    btn.style.color = 'var(--danger)';
    showMsg('Network error: ' + e.message, true);
  }
  setTimeout(function() {
    btn.textContent = origText;
    btn.style.color = '';
    btn.disabled = false;
  }, 3000);
}

async function forceOffAll() {
  if (!confirm(
    'Emergency Off will send an OFF command to EVERY configured device.\n\n' +
    'Use this when a sprinkler or siren is stuck on. It will attempt to switch ' +
    'off every device regardless of current state or armed/disarmed status.\n\n' +
    'Proceed?'
  )) return;
  var btn = document.getElementById('force-off-btn');
  var status = document.getElementById('force-off-status');
  btn.disabled = true;
  status.style.display = '';
  status.textContent = 'Forcing OFF...';
  status.style.color = '';
  try {
    var resp = await fetch('/admin/deterrent/force-off', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken()},
      body: JSON.stringify({}),
    });
    var data = await resp.json();
    if (data.ok) {
      var n = (data.devices || []).length;
      status.textContent = 'OFF sent to ' + n + ' device' + (n === 1 ? '' : 's') + '.';
      status.style.color = 'var(--ok)';
    } else {
      var failed = (data.devices || []).filter(function(d) { return !d.ok; });
      var msg = failed.length > 0
        ? failed.length + ' device(s) failed: ' + failed.map(function(d) { return d.name; }).join(', ')
        : (data.error || 'Unknown error');
      status.textContent = 'Partial: ' + msg;
      status.style.color = 'var(--danger)';
    }
  } catch (e) {
    status.textContent = 'Network error: ' + e.message;
    status.style.color = 'var(--danger)';
  }
  setTimeout(function() {
    btn.disabled = false;
    status.style.display = 'none';
  }, 8000);
}

async function checkDeviceStatus() {
  var btn = document.getElementById('status-btn');
  var spinner = document.getElementById('status-spinner');
  var panel = document.getElementById('status-panel');
  btn.disabled = true;
  spinner.style.display = '';
  panel.innerHTML = '';
  try {
    var resp = await fetch('/admin/deterrent/device-status');
    var data = await resp.json();
    if (data.ok && data.devices) {
      var html = '<table style="width:100%;border-collapse:collapse;font-size:0.85rem;">';
      html += '<thead><tr style="border-bottom:2px solid var(--border);text-align:left;">';
      html += '<th style="padding:0.3rem 0.5rem;">Device</th>';
      html += '<th style="padding:0.3rem 0.5rem;">Type</th>';
      html += '<th style="padding:0.3rem 0.5rem;">Online</th>';
      html += '<th style="padding:0.3rem 0.5rem;">Battery</th>';
      html += '<th style="padding:0.3rem 0.5rem;">Switch</th>';
      html += '</tr></thead><tbody>';
      data.devices.forEach(function(d) {
        html += '<tr style="border-bottom:1px solid var(--border);">';
        html += '<td style="padding:0.3rem 0.5rem;">' + esc(d.name) + '</td>';
        html += '<td style="padding:0.3rem 0.5rem;">' + esc(d.type) + '</td>';
        html += '<td style="padding:0.3rem 0.5rem;">';
        html += d.online
          ? '<span style="color:var(--ok);">Online</span>'
          : '<span style="color:var(--danger);">Offline</span>';
        html += '</td>';
        html += '<td style="padding:0.3rem 0.5rem;">';
        html += d.battery_pct != null ? d.battery_pct + '%' : '<span class="muted">N/A</span>';
        html += '</td>';
        html += '<td style="padding:0.3rem 0.5rem;">';
        if (d.switch_state === true) html += '<span style="color:var(--ok);">ON</span>';
        else if (d.switch_state === false) html += '<span class="muted">OFF</span>';
        else html += '<span class="muted">N/A</span>';
        html += '</td>';
        html += '</tr>';
      });
      html += '</tbody></table>';
      panel.innerHTML = html;
    } else {
      panel.innerHTML = '<span class="muted">' + esc(data.error || 'No data') + '</span>';
    }
  } catch (e) {
    panel.innerHTML = '<span style="color:var(--danger);">Network error: ' + esc(e.message) + '</span>';
  }
  btn.disabled = false;
  spinner.style.display = 'none';
}
