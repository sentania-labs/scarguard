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
      status.textContent = 'Backup started — refreshing in 5s...';
      status.style.color = 'var(--ok)';
      setTimeout(function() { location.reload(); }, 5000);
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
