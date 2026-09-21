(function readOnlyHardening() {
  function apply() {
    var panel = document.getElementById('tab-settings');
    if (!panel) return;
    var controls = panel.querySelectorAll('input, select, textarea');
    for (var i = 0; i < controls.length; i++) {
      var el = controls[i];
      if (el.id === 'expert-mode-toggle') continue;
      el.disabled = true;
    }
    panel.querySelectorAll('[data-requires-admin]').forEach(function(el) {
      el.hidden = true;
      el.disabled = true;
    });
  }
  document.addEventListener('DOMContentLoaded', apply);
  var target = document.getElementById('tab-settings');
  if (target && window.MutationObserver) {
    new MutationObserver(apply).observe(target, { childList: true, subtree: true });
  }
})();
