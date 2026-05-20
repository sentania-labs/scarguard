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
    var hideSelectors = [
      '#save-btn',
      '.btn-add',
      '.btn-remove',
      '.btn-test',
      '.btn-upload',
      'button[onclick*="Save"]',
      'button[onclick*="save"]',
      'button[onclick*="test"]',
      'button[onclick*="upload"]',
      'button[onclick*="remove"]',
      'button[onclick*="add"]',
    ];
    for (var j = 0; j < hideSelectors.length; j++) {
      var matches = panel.querySelectorAll(hideSelectors[j]);
      for (var k = 0; k < matches.length; k++) matches[k].style.display = 'none';
    }
  }
  document.addEventListener('DOMContentLoaded', apply);
  var target = document.getElementById('tab-settings');
  if (target && window.MutationObserver) {
    new MutationObserver(apply).observe(target, { childList: true, subtree: true });
  }
})();
