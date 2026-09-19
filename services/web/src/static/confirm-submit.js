/* Declarative confirmation for destructive actions.
 *
 * Replaces onclick="return confirm('...')", which the browser ignores under
 * this app's Content-Security-Policy (script-src 'self', no 'unsafe-inline').
 * Those attributes were silently dropped, so the guarded buttons submitted on
 * the first click with no prompt at all.
 *
 * Two shapes are supported, because both already existed in the templates:
 *
 *   <form data-confirm="Delete user bob?">            ... on submit
 *   <button type="submit" data-confirm="Cancel job?"> ... on click
 *
 * A form-level attribute is the more robust of the two: it also catches
 * implicit submission (Enter in a text field), which never fires a click on
 * the button. The button form is kept for cases where one form has several
 * buttons that need different prompts.
 *
 * Delegated from the document so there is exactly one place that owns this
 * behaviour and so markup added after load is covered without re-binding.
 * Before this file, users.js carried its own form-level copy; having two
 * listeners meant a button inside a form that both carried the attribute
 * prompted twice, because closest() walks from the button up to the form.
 */
(function () {
  'use strict';

  function ask(message) {
    return !message || window.confirm(message);
  }

  // Click on a button that carries its own prompt. Scoped to the element
  // itself, NOT closest(), so a button inside a confirming form does not
  // inherit the form's prompt and ask twice.
  document.addEventListener(
    'click',
    function (e) {
      var btn = e.target.closest('button[data-confirm], input[data-confirm]');
      if (!btn) return;
      if (ask(btn.getAttribute('data-confirm'))) return;
      // preventDefault alone still lets a submit button activate its form on
      // some paths, so stop the event reaching other handlers as well.
      e.preventDefault();
      e.stopPropagation();
    },
    true
  );

  // Submit of a form that carries the prompt. Also covers Enter-in-a-field,
  // which produces no click.
  document.addEventListener(
    'submit',
    function (e) {
      var form = e.target;
      if (!form || !form.hasAttribute || !form.hasAttribute('data-confirm')) return;
      if (ask(form.getAttribute('data-confirm'))) return;
      e.preventDefault();
      e.stopPropagation();
    },
    true
  );
})();
