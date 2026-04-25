/* ScarGuard — chip-picker.js
 *
 * A small dependency-free type-ahead token input.  Replaces a plain
 * `<input type="text">` in-place with:
 *
 *   ┌───────────────────────────────────────────────────────┐
 *   │ [chip ✕] [chip ✕]   🔍 Type to add…                   │
 *   └───────────────────────────────────────────────────────┘
 *                        ┌────────────────────────┐
 *                        │ matching-entry-1       │
 *                        │ matching-entry-2       │
 *                        │ ─────────────────────  │
 *                        │ + Create "foo"         │  ← only when allowCreate
 *                        └────────────────────────┘
 *
 * Values are sourced from a `registry()` callback so the picker stays in
 * sync with a live shadow registry (e.g. window._availableChannels) that
 * updates as the user adds/renames/removes channels elsewhere on the page.
 *
 * Usage:
 *
 *   var picker = ChipPicker.create(inputEl, {
 *     registry: function() { return window._availableChannels; },
 *     values: ["pond-alerts"],
 *     onChange: function(newValues) { ... },
 *     allowCreate: true,
 *     onCreate: function(name) { ... },
 *     singleValue: false,              // true = at most one chip
 *     placeholder: "Type a channel name…",
 *     alwaysAvailable: ["*"],          // values not in registry but always valid
 *     readValues: function() { ... }   // optional — re-read values on render
 *   });
 *
 *   picker.refresh();                  // re-render (e.g. after registry change)
 *   picker.getValues();                // read current chips
 *   picker.destroy();
 */

window.ChipPicker = (function() {
  "use strict";

  function _esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function create(targetEl, opts) {
    opts = opts || {};
    var registry = opts.registry || function() { return []; };
    var alwaysAvail = opts.alwaysAvailable || [];
    var allowCreate = !!opts.allowCreate;
    var onCreate = opts.onCreate || null;
    var singleValue = !!opts.singleValue;
    var placeholder = opts.placeholder || "Type to add…";
    var readValues = opts.readValues || null;

    var values = Array.isArray(opts.values) ? opts.values.slice() : [];

    // Build wrapper DOM, insert after the target, hide the original input.
    var wrap = document.createElement("div");
    wrap.className = "chip-picker";
    wrap.innerHTML = ''
      + '<div class="chip-picker-chips"></div>'
      + '<input type="text" class="chip-picker-input" placeholder="' + _esc(placeholder) + '">'
      + '<ul class="chip-picker-dropdown" style="display:none;"></ul>';
    targetEl.style.display = "none";
    targetEl.parentNode.insertBefore(wrap, targetEl.nextSibling);

    var chipsEl = wrap.querySelector(".chip-picker-chips");
    var inputEl = wrap.querySelector(".chip-picker-input");
    var dropEl = wrap.querySelector(".chip-picker-dropdown");

    function _emit() {
      // Update the original input so readForm/serialization still works if
      // something else reads it, and call the onChange callback.
      targetEl.value = values.join(",");
      if (typeof opts.onChange === "function") opts.onChange(values.slice());
    }

    function _renderChips() {
      chipsEl.innerHTML = "";
      values.forEach(function(v, i) {
        var known = _isKnown(v);
        var chip = document.createElement("span");
        chip.className = "chip-picker-chip" + (known ? "" : " chip-unknown");
        chip.setAttribute("data-idx", String(i));
        chip.innerHTML = _esc(v)
          + ' <button type="button" class="chip-picker-x" aria-label="Remove">✕</button>';
        if (!known) {
          chip.title = "Not in registry — will not match anything at runtime";
        }
        chipsEl.appendChild(chip);
      });
      // Show/hide input when singleValue is full
      inputEl.style.display = (singleValue && values.length >= 1) ? "none" : "";
    }

    function _isKnown(v) {
      if (alwaysAvail.indexOf(v) >= 0) return true;
      var reg = registry() || [];
      return reg.indexOf(v) >= 0;
    }

    function _filterRegistry(query) {
      var reg = (registry() || []).slice();
      // Combine with alwaysAvail, dedupe
      alwaysAvail.forEach(function(v) { if (reg.indexOf(v) < 0) reg.push(v); });
      var q = (query || "").toLowerCase().trim();
      var out = reg.filter(function(r) {
        if (values.indexOf(r) >= 0) return false;   // already selected
        if (!q) return true;
        return r.toLowerCase().indexOf(q) >= 0;
      });
      return out;
    }

    function _renderDropdown() {
      var q = inputEl.value;
      var matches = _filterRegistry(q);
      dropEl.innerHTML = "";

      matches.forEach(function(m) {
        var li = document.createElement("li");
        li.className = "chip-picker-option";
        li.textContent = m;
        li.addEventListener("mousedown", function(e) {
          e.preventDefault();  // don't blur input
          _addValue(m);
        });
        dropEl.appendChild(li);
      });

      // "+ Create" escape hatch — only when nothing matched exactly and
      // allowCreate is enabled.
      var trimmed = (q || "").trim();
      var exact = matches.indexOf(trimmed) >= 0;
      if (allowCreate && trimmed && !exact) {
        if (matches.length > 0) {
          var sep = document.createElement("li");
          sep.className = "chip-picker-sep";
          dropEl.appendChild(sep);
        }
        var createLi = document.createElement("li");
        createLi.className = "chip-picker-option chip-picker-create";
        createLi.textContent = '+ Create "' + trimmed + '"';
        createLi.addEventListener("mousedown", function(e) {
          e.preventDefault();
          if (typeof onCreate === "function") onCreate(trimmed);
          _addValue(trimmed);
        });
        dropEl.appendChild(createLi);
      }

      dropEl.style.display = dropEl.children.length ? "" : "none";
    }

    function _addValue(v) {
      if (!v) return;
      if (values.indexOf(v) >= 0) {
        inputEl.value = "";
        _renderDropdown();
        return;
      }
      if (singleValue) {
        values = [v];
      } else {
        values.push(v);
      }
      inputEl.value = "";
      _renderChips();
      _renderDropdown();
      _emit();
    }

    function _removeIdx(idx) {
      if (idx < 0 || idx >= values.length) return;
      var dropdownWasOpen = dropEl.style.display !== "none";
      values.splice(idx, 1);
      _renderChips();
      // Only re-render the dropdown if the user already had it open —
      // clicking ✕ on a chip shouldn't pop a closed dropdown back open.
      if (dropdownWasOpen) _renderDropdown();
      _emit();
    }

    // Events ----------------------------------------------------------------
    chipsEl.addEventListener("click", function(e) {
      if (!e.target.classList.contains("chip-picker-x")) return;
      var chip = e.target.closest(".chip-picker-chip");
      if (!chip) return;
      _removeIdx(parseInt(chip.getAttribute("data-idx"), 10));
    });

    inputEl.addEventListener("focus", _renderDropdown);
    inputEl.addEventListener("input", _renderDropdown);

    inputEl.addEventListener("keydown", function(e) {
      if (e.key === "Enter") {
        e.preventDefault();
        var q = inputEl.value.trim();
        if (!q) return;
        // Prefer an exact match in the registry; otherwise treat as Create
        var reg = (registry() || []).concat(alwaysAvail);
        if (reg.indexOf(q) >= 0) {
          _addValue(q);
        } else if (allowCreate) {
          if (typeof onCreate === "function") onCreate(q);
          _addValue(q);
        }
      } else if (e.key === "Backspace" && !inputEl.value && values.length) {
        _removeIdx(values.length - 1);
      } else if (e.key === "Escape") {
        dropEl.style.display = "none";
        inputEl.blur();
      }
    });

    inputEl.addEventListener("blur", function() {
      // Delay so click-to-select on dropdown still fires
      setTimeout(function() { dropEl.style.display = "none"; }, 150);
    });

    // Public API ------------------------------------------------------------
    var api = {
      refresh: function() {
        if (readValues) {
          var next = readValues();
          if (Array.isArray(next)) values = next.slice();
        }
        var dropdownWasOpen = dropEl.style.display !== "none";
        _renderChips();
        // Background registry refreshes (async class-list fetch, channel
        // mutation observer) shouldn't pop the dropdown open — only
        // re-render if the user already had it open.
        if (dropdownWasOpen) _renderDropdown();
      },
      getValues: function() { return values.slice(); },
      setValues: function(next) {
        values = Array.isArray(next) ? next.slice() : [];
        _renderChips();
        _emit();
      },
      destroy: function() {
        wrap.parentNode.removeChild(wrap);
        targetEl.style.display = "";
      },
      element: wrap,
    };

    _renderChips();
    _emit();
    return api;
  }

  return { create: create };
})();
