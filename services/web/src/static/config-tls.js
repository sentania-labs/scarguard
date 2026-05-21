function toggleTlsFields() {
  var mode = document.getElementById('tls-mode').value;
  document.getElementById('tls-domain-field').style.display = (mode === 'auto' || mode === 'manual') ? '' : 'none';
  document.getElementById('tls-domain-hint-auto').style.display = mode === 'auto' ? '' : 'none';
  document.getElementById('tls-domain-hint-manual').style.display = mode === 'manual' ? '' : 'none';
  document.getElementById('tls-manual-fields').style.display = mode === 'manual' ? '' : 'none';
}
toggleTlsFields();

function switchCertTab(tab, btn) {
  document.getElementById('cert-tab-upload').style.display = tab === 'upload' ? '' : 'none';
  document.getElementById('cert-tab-paste').style.display = tab === 'paste' ? '' : 'none';
  document.querySelectorAll('.cert-tab-btn').forEach(function(b) { b.classList.remove('active'); });
  btn.classList.add('active');
}

async function uploadCerts() {
  var status = document.getElementById('cert-upload-status');
  var btn = document.getElementById('cert-upload-btn');
  btn.disabled = true;
  status.textContent = 'Uploading...';
  status.style.color = '';

  var fd = new FormData();
  var certFile = document.getElementById('tls-cert-file').files[0];
  var keyFile = document.getElementById('tls-key-file').files[0];
  var certPem = document.getElementById('tls-cert-pem').value;
  var keyPem = document.getElementById('tls-key-pem').value;

  if (certFile) fd.append('cert_file', certFile);
  if (keyFile) fd.append('key_file', keyFile);
  if (certPem) fd.append('cert_pem', certPem);
  if (keyPem) fd.append('key_pem', keyPem);

  try {
    var resp = await fetch('/config/tls/upload-cert', {
      method: 'POST',
      body: fd,
      headers: {'X-CSRF-Token': getCsrfToken()}
    });
    var data = await resp.json();
    if (data.ok) {
      status.textContent = 'Uploaded: ' + data.written.join(', ');
      status.style.color = 'var(--ok)';
    } else {
      status.textContent = data.error || 'Upload failed';
      status.style.color = 'var(--danger)';
    }
  } catch (e) {
    status.textContent = 'Network error: ' + e.message;
    status.style.color = 'var(--danger)';
  }
  btn.disabled = false;
}
