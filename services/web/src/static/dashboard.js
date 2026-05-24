function _updateRearmTimers() {
  document.querySelectorAll('.rearm-countdown').forEach(function(el) {
    var ms = new Date(el.dataset.rearmAt) - Date.now();
    var t = el.querySelector('.rearm-timer');
    if (!t) return;
    if (ms <= 0) { t.textContent = 'now'; return; }
    var m = Math.floor(ms / 60000);
    var s = Math.floor((ms % 60000) / 1000);
    t.textContent = m + ':' + String(s).padStart(2, '0');
  });
}
setInterval(_updateRearmTimers, 1000);
_updateRearmTimers();

var _snapshotFilename = '';
var _snapshotCamera = '';

async function grabSnapshot(cameraName) {
  var modal = document.getElementById('snapshot-modal');
  modal.style.display = 'flex';
  _snapshotCamera = cameraName;
  _snapshotFilename = '';
  document.getElementById('snapshot-title').textContent = 'Snapshot: ' + cameraName;
  document.getElementById('snapshot-status').textContent = 'Requesting snapshot...';
  document.getElementById('snapshot-img').style.display = 'none';
  var sendDiv = document.getElementById('snapshot-send');
  if (sendDiv) sendDiv.style.display = 'none';

  try {
    var resp = await fetch('/snapshot/' + encodeURIComponent(cameraName));
    var data = await resp.json();
    if (data.ok) {
      document.getElementById('snapshot-status').textContent = '';
      var img = document.getElementById('snapshot-img');
      img.src = data.snapshot_url + '?t=' + Date.now();
      img.style.display = 'block';
      _snapshotFilename = data.filename || '';
      if (sendDiv && _snapshotFilename) sendDiv.style.display = '';
    } else {
      document.getElementById('snapshot-status').textContent = 'Error: ' + (data.error || 'Unknown error');
    }
  } catch (e) {
    document.getElementById('snapshot-status').textContent = 'Network error: ' + e.message;
  }
}

async function sendSnapshotToChannel() {
  var btn = document.getElementById('snapshot-send-btn');
  var status = document.getElementById('snapshot-send-status');
  var channel = document.getElementById('snapshot-channel').value;
  btn.disabled = true;
  status.textContent = 'Sending...';
  status.style.color = '';

  var fd = new FormData();
  fd.append('filename', _snapshotFilename);
  fd.append('channel', channel);
  fd.append('camera_name', _snapshotCamera);

  try {
    var resp = await fetch('/snapshot/send', { method: 'POST', body: fd, headers: {'X-CSRF-Token': getCsrfToken()} });
    var data = await resp.json();
    if (data.ok) {
      status.textContent = 'Sent to ' + channel;
      status.style.color = 'var(--ok)';
    } else {
      status.textContent = data.error || 'Send failed';
      status.style.color = 'var(--danger)';
    }
  } catch (e) {
    status.textContent = 'Network error: ' + e.message;
    status.style.color = 'var(--danger)';
  }
  btn.disabled = false;
}

function closeSnapshotModal() {
  document.getElementById('snapshot-modal').style.display = 'none';
}

(function wireDashboardButtons() {
  var dismissBtn = document.getElementById('dismiss-training-nudge-btn');
  if (dismissBtn) {
    dismissBtn.addEventListener('click', function() {
      var nudge = document.getElementById('training-nudge');
      if (nudge) nudge.style.display = 'none';
    });
  }
  var closeBtn = document.getElementById('close-snapshot-modal-btn');
  if (closeBtn) closeBtn.addEventListener('click', closeSnapshotModal);
  var sendBtn = document.getElementById('snapshot-send-btn');
  if (sendBtn) sendBtn.addEventListener('click', sendSnapshotToChannel);
  // Per-camera Snapshot buttons (delegation on the table)
  document.querySelectorAll('button[data-action="grab-snapshot"]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      grabSnapshot(btn.dataset.cameraName);
    });
  });
})();
