/* ScarGuard — users page logic */

document.addEventListener('DOMContentLoaded', function() {
  // Auto-submit role <select> on change.
  document.querySelectorAll('select[data-action="submit-on-change"]').forEach(function(el) {
    el.addEventListener('change', function() {
      if (el.form) el.form.submit();
    });
  });
  // Forms that require a confirm() before submitting.
  document.querySelectorAll('form[data-confirm]').forEach(function(form) {
    form.addEventListener('submit', function(e) {
      if (!window.confirm(form.dataset.confirm)) {
        e.preventDefault();
      }
    });
  });
});
