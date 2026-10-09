function getCsrfToken(){var m=document.cookie.match(/(?:^|; )csrf_token=([^;]*)/);return m?decodeURIComponent(m[1]):"";}
document.addEventListener("htmx:configRequest",function(e){e.detail.headers["X-CSRF-Token"]=getCsrfToken();});

(function(){
  var dd = document.querySelector('.nav-dropdown');
  if (!dd) return;
  var btn = dd.querySelector('.nav-dropdown__toggle');
  btn.addEventListener('click', function(e) {
    e.stopPropagation();
    var open = dd.classList.toggle('open');
    btn.setAttribute('aria-expanded', open);
  });
  document.addEventListener('click', function() {
    dd.classList.remove('open');
    btn.setAttribute('aria-expanded', 'false');
  });
})();

/* ── Deterrent stuck-device banner (SSE) ─────────────────────────────────── */
(function() {
  var DISMISS_KEY = 'sg_stuck_dismissed';
  var banner = document.getElementById('stuck-banner');
  if (!banner) return;

  function getDismissed() {
    try {
      return JSON.parse(localStorage.getItem(DISMISS_KEY) || '[]');
    } catch (_e) { return []; }
  }

  function addDismissed(requestId) {
    var list = getDismissed();
    if (list.indexOf(requestId) === -1) list.push(requestId);
    // Keep last 50 to avoid unbounded growth.
    if (list.length > 50) list = list.slice(-50);
    localStorage.setItem(DISMISS_KEY, JSON.stringify(list));
  }

  function escHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function showBanner(data) {
    var requestId = data.request_id || '';
    if (requestId && getDismissed().indexOf(requestId) !== -1) return;
    var name = data.device_name || data.device_id || 'Unknown device';
    var error = data.error || 'Device may be stuck';
    banner.innerHTML =
      '<span>' + escHtml(name) + ': ' + escHtml(error) + '</span>' +
      '<button class="stuck-banner__dismiss" title="Dismiss">&times;</button>';
    banner.style.display = '';
    var dismissBtn = banner.querySelector('.stuck-banner__dismiss');
    if (dismissBtn) {
      dismissBtn.addEventListener('click', function() {
        banner.style.display = 'none';
        if (requestId) addDismissed(requestId);
      });
    }
  }

  var es = new EventSource('/deterrent-stuck/stream');
  es.addEventListener('stuck', function(e) {
    try {
      var data = JSON.parse(e.data);
      showBanner(data);
    } catch (_e) { /* ignore malformed */ }
  });
})();

// Native multipart forms cannot set headers. Submit via fetch so CSRF is
// checked before the server reads any file bytes; keep redirects and errors.
document.addEventListener("submit", async function(event) {
  var form = event.target;
  if (!(form instanceof HTMLFormElement) || form.enctype !== "multipart/form-data") return;
  event.preventDefault();
  var button = form.querySelector('[type="submit"]');
  if (button) button.disabled = true;
  var error = form.querySelector('[data-upload-error]');
  if (!error) {
    error = document.createElement("p");
    error.setAttribute("data-upload-error", "");
    error.setAttribute("role", "alert");
    form.appendChild(error);
  }
  error.textContent = "";
  try {
    var response = await fetch(form.action, {
      method: "POST", body: new FormData(form), credentials: "same-origin",
      headers: {"X-CSRF-Token": getCsrfToken()}
    });
    if (response.redirected) {
      window.location.assign(response.url);
    } else {
      // Validation pages contain the original form and error. Extract only
      // text, without executing returned scripts or replacing the document.
      var text = await response.text();
      var page = new DOMParser().parseFromString(text, "text/html");
      var message = page.querySelector('.alert-err');
      error.textContent = message ? message.textContent :
        (response.status === 413 ? "Upload exceeds the configured size limit." : "Upload failed. Check the file and try again.");
    }
  } catch (_error) {
    error.textContent = "Upload failed. Check your connection and try again.";
  } finally {
    if (button) button.disabled = false;
  }
});
