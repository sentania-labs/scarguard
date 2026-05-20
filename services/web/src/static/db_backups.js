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

/* ── Auth DB re-authentication download ─────────────────────────────────── */
var _pendingAuthDownload = {db: '', filename: ''};

function downloadAuthBackup(db, filename) {
  _pendingAuthDownload = {db: db, filename: filename};
  var modal = document.getElementById('reauth-modal');
  var errEl = document.getElementById('reauth-error');
  var pwEl = document.getElementById('reauth-password');
  errEl.style.display = 'none';
  errEl.textContent = '';
  pwEl.value = '';
  modal.style.display = 'flex';
  pwEl.focus();
}

function closeReauthModal() {
  document.getElementById('reauth-modal').style.display = 'none';
  _pendingAuthDownload = {db: '', filename: ''};
}

async function submitReauth() {
  var pw = document.getElementById('reauth-password').value;
  var errEl = document.getElementById('reauth-error');
  var btn = document.getElementById('reauth-submit');
  if (!pw) {
    errEl.textContent = 'Password is required.';
    errEl.style.display = '';
    return;
  }
  btn.disabled = true;
  errEl.style.display = 'none';
  try {
    var url = '/admin/db-backups/download/' + encodeURIComponent(_pendingAuthDownload.db) + '/' + encodeURIComponent(_pendingAuthDownload.filename);
    var resp = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': getCsrfToken()
      },
      body: JSON.stringify({password: pw})
    });
    if (!resp.ok) {
      var data;
      try { data = await resp.json(); } catch (_e) { data = {}; }
      errEl.textContent = data.detail || 'Download failed (HTTP ' + resp.status + ')';
      errEl.style.display = '';
      btn.disabled = false;
      return;
    }
    // Download succeeded — trigger the browser file-save via a blob.
    var blob = await resp.blob();
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = _pendingAuthDownload.filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(a.href);
    closeReauthModal();
  } catch (e) {
    errEl.textContent = 'Network error: ' + e.message;
    errEl.style.display = '';
  } finally {
    btn.disabled = false;
  }
}

// Allow Enter key in the password field to submit.
document.addEventListener('DOMContentLoaded', function() {
  var pwEl = document.getElementById('reauth-password');
  if (pwEl) {
    pwEl.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') { e.preventDefault(); submitReauth(); }
    });
  }
});

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
