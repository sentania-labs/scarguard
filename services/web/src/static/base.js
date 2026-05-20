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
