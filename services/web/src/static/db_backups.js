function getCsrfToken() {
  var m = document.cookie.match(/(?:^|; )csrf_token=([^;]*)/);
  return m ? decodeURIComponent(m[1]) : "";
}

async function triggerBackup() {
  var btn = document.getElementById('trigger-btn');
  var status = document.getElementById('trigger-status');
  btn.disabled = true;
  status.style.display = '';
  status.textContent = 'Triggering...';
  status.style.color = '';
  try {
    var resp = await fetch('/admin/db-backups/trigger', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken()},
    });
    var data = await resp.json();
    if (data.ok) {
      status.textContent = 'Backup started — watching for completion...';
      status.style.color = 'var(--ok)';
    } else {
      status.textContent = 'Failed: ' + (data.error || 'unknown');
      status.style.color = 'var(--danger)';
      btn.disabled = false;
    }
  } catch (e) {
    status.textContent = 'Network error: ' + e.message;
    status.style.color = 'var(--danger)';
    btn.disabled = false;
  }
}

/* ── Live backup status (SSE) ────────────────────────────────────────────── */
(function() {
  var liveDiv = document.getElementById('backup-live-status');
  if (!liveDiv) return;

  function escHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  var es = new EventSource('/admin/db-backups/stream');

  es.addEventListener('backup-status', function(e) {
    try {
      var data = JSON.parse(e.data);
    } catch (_e) { return; }

    var phase = data.phase || 'unknown';
    var triggeredBy = data.triggered_by || '';
    var msg = '';

    if (phase === 'started') {
      msg = 'Backup in progress' + (triggeredBy ? ' (triggered by ' + escHtml(triggeredBy) + ')' : '') + '...';
      liveDiv.style.color = 'var(--accent)';
    } else if (phase === 'completed') {
      var results = data.results;
      var detail = '';
      if (results && typeof results === 'object') {
        var parts = [];
        for (var db in results) {
          if (Object.prototype.hasOwnProperty.call(results, db)) {
            parts.push(escHtml(db) + ': ' + (results[db].ok ? 'ok' : 'failed'));
          }
        }
        if (parts.length) detail = ' (' + parts.join(', ') + ')';
      }
      msg = 'Backup completed' + detail + ' — reloading...';
      liveDiv.style.color = 'var(--ok)';
      setTimeout(function() { location.reload(); }, 2000);
    } else if (phase === 'failed') {
      msg = 'Backup failed: ' + escHtml(data.error || 'unknown error');
      liveDiv.style.color = 'var(--danger)';
    } else {
      msg = 'Backup status: ' + escHtml(phase);
      liveDiv.style.color = '';
    }

    liveDiv.textContent = msg;
  });
})();
