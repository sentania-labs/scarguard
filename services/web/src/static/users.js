/* ScarGuard - users page logic */

document.addEventListener('DOMContentLoaded', function() {
  // Auto-submit role <select> on change.
  document.querySelectorAll('select[data-action="submit-on-change"]').forEach(function(el) {
    el.addEventListener('change', function() {
      if (el.form) el.form.submit();
    });
  });
  // form[data-confirm] is handled by static/confirm-submit.js, which is
  // loaded for every page. Keeping a second listener here made a button
  // inside a confirming form prompt twice.
});
