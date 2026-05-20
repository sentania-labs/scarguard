function getCsrfToken(){var m=document.cookie.match(/(?:^|; )csrf_token=([^;]*)/);return m?decodeURIComponent(m[1]):"";}

async function createBackup() {
  const btn = document.getElementById('create-btn');
  btn.disabled = true;
  btn.textContent = 'Creating...';
  try {
    const resp = await fetch('/admin/backups/create', {
      method: 'POST',
      headers: { 'X-CSRF-Token': getCsrfToken() },
    });
    const data = await resp.json();
    if (data.ok) {
      showBanner('Backup created: ' + data.filename, 'ok');
      location.reload();
    } else {
      showBanner('Error: ' + data.error, 'err');
    }
  } catch (e) {
    showBanner('Network error: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Create Backup Now';
  }
}

async function showDiff(name) {
  document.getElementById('diff-name').textContent = name;
  document.getElementById('diff-content').textContent = 'Loading...';
  document.getElementById('diff-modal').style.display = 'block';
  try {
    const resp = await fetch('/admin/backups/' + encodeURIComponent(name) + '/diff');
    const data = await resp.json();
    document.getElementById('diff-content').textContent = data.diff || data.error || 'No data';
  } catch (e) {
    document.getElementById('diff-content').textContent = 'Error: ' + e.message;
  }
}

async function restoreBackup(name) {
  if (!confirm('Restore config from ' + name + '? A pre-restore backup will be created first.')) return;
  try {
    const resp = await fetch('/admin/backups/' + encodeURIComponent(name) + '/restore', {
      method: 'POST',
      headers: { 'X-CSRF-Token': getCsrfToken() },
    });
    const data = await resp.json();
    if (data.ok) {
      showBanner('Config restored from ' + name + '. Changes take effect within ~10 seconds.', 'ok');
      location.reload();
    } else {
      showBanner('Error: ' + data.error, 'err');
    }
  } catch (e) {
    showBanner('Network error: ' + e.message, 'err');
  }
}

function showBanner(msg, type) {
  const banner = document.getElementById('backup-banner');
  banner.style.display = 'block';
  banner.className = 'alert alert-' + type;
  banner.textContent = msg;
}
