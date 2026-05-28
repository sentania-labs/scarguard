/* Labeling queue: bbox overlay, keyboard shortcuts, multi-box re-label canvas. */
(function () {
  "use strict";

  /* CSP-safe page data: read from <script type="application/json"> block. */
  var _labelData = JSON.parse(document.getElementById("label-page-data").textContent);

  /* Validators for values that flow into URL construction.
     CodeQL flags dataset-derived strings as DOM-tainted; gating with a
     strict pattern test breaks the taint flow. */
  function _isHexId(s) { return typeof s === "string" && /^[a-f0-9]+$/.test(s); }
  function _isUInt(s)  { return /^\d+$/.test(String(s || "")); }

  /* Per-card relabel state. Reset on every card swap. */
  var _relabelBoxes = [];   /* [{cls, bbox: [xc,yc,w,h]}] */
  var _drawing = null;      /* {startX, startY, curX, curY} during a drag */
  var _relabelActive = false;

  function _clamp01(v) { return Math.max(0, Math.min(1, v)); }

  function _detail() { return document.getElementById("label-detail"); }

  /* ── Original-detector bbox overlay ────────────────────────────────── */

  function renderBbox() {
    var detail = _detail();
    var box = document.getElementById("label-bbox");
    if (!detail || !box) return;
    box.classList.remove("state-approved", "state-rejected", "state-corrected");
    var state = detail.dataset.reviewState || "";
    if (state && state !== "pending") box.classList.add("state-" + state);
    var raw = detail.dataset.bbox;
    if (!raw || raw === "[]") { box.style.display = "none"; return; }
    try {
      var bbox = JSON.parse(raw);
      if (bbox.length < 4) { box.style.display = "none"; return; }
      var xc = bbox[0], yc = bbox[1], w = bbox[2], h = bbox[3];
      box.style.left = ((xc - w / 2) * 100) + "%";
      box.style.top = ((yc - h / 2) * 100) + "%";
      box.style.width = (w * 100) + "%";
      box.style.height = (h * 100) + "%";
      box.style.display = "block";
    } catch (e) {
      box.style.display = "none";
    }
  }

  function updateFrame() {
    var detail = _detail();
    var img = document.getElementById("label-frame");
    if (!detail || !img) return;
    var frameIdx = detail.dataset.frameIdx;
    if (!_isHexId(_labelData.uploadId) || !_isUInt(frameIdx)) return;
    img.src = "/admin/training/uploads/" + _labelData.uploadId + "/frames/" + frameIdx;
  }

  function navigateEvent(direction) {
    var detail = _detail();
    if (!detail) return;
    var targetId = direction === "next" ? detail.dataset.nextId : detail.dataset.prevId;
    if (!_isHexId(_labelData.uploadId) || !_isUInt(targetId)) return;
    var qs = "?event_id=" + targetId;
    if (_labelData.filterReviewState) qs += "&review_state=" + encodeURIComponent(_labelData.filterReviewState);
    if (_labelData.filterDetectionPass) qs += "&detection_pass=" + encodeURIComponent(_labelData.filterDetectionPass);
    window.location.href = "/admin/training/uploads/" + _labelData.uploadId + qs;
  }

  /* ── Re-label canvas ─────────────────────────────────────────────── */

  function _resetRelabelState() {
    _relabelBoxes = [];
    _drawing = null;
    _relabelActive = false;
    var canvas = document.getElementById("label-canvas");
    if (canvas) canvas.hidden = true;
    var panel = document.getElementById("relabel-panel");
    if (panel) panel.hidden = true;
    var list = document.getElementById("relabel-box-list");
    if (list) list.innerHTML = '<p class="muted relabel-empty">No boxes drawn yet.</p>';
    var submit = document.getElementById("btn-relabel-submit");
    if (submit) submit.disabled = true;
    _renderRelabelOverlay();
  }

  function _sizeCanvasToImage() {
    var img = document.getElementById("label-frame");
    var canvas = document.getElementById("label-canvas");
    if (!img || !canvas) return;
    canvas.width = img.clientWidth;
    canvas.height = img.clientHeight;
  }

  function _activateRelabel() {
    if (_relabelActive) return;
    _relabelActive = true;
    var panel = document.getElementById("relabel-panel");
    if (panel) panel.hidden = false;
    var canvas = document.getElementById("label-canvas");
    if (canvas) {
      canvas.hidden = false;
      _sizeCanvasToImage();
      _drawCanvas();
    }
    var input = document.getElementById("relabel-class-input");
    if (input) input.focus();
  }

  function _drawCanvas() {
    var canvas = document.getElementById("label-canvas");
    if (!canvas) return;
    var ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    /* Existing drawn boxes */
    ctx.lineWidth = 2;
    ctx.font = "14px monospace";
    for (var i = 0; i < _relabelBoxes.length; i++) {
      var b = _relabelBoxes[i].bbox;
      var x = (b[0] - b[2] / 2) * canvas.width;
      var y = (b[1] - b[3] / 2) * canvas.height;
      var w = b[2] * canvas.width;
      var h = b[3] * canvas.height;
      ctx.strokeStyle = "#34d399";
      ctx.strokeRect(x, y, w, h);
      ctx.fillStyle = "rgba(52, 211, 153, 0.85)";
      var label = _relabelBoxes[i].cls + " #" + (i + 1);
      var tw = ctx.measureText(label).width + 8;
      ctx.fillRect(x, Math.max(0, y - 18), tw, 18);
      ctx.fillStyle = "#0a1a12";
      ctx.fillText(label, x + 4, Math.max(12, y - 4));
    }

    /* Active drag */
    if (_drawing) {
      var dx = Math.min(_drawing.startX, _drawing.curX);
      var dy = Math.min(_drawing.startY, _drawing.curY);
      var dw = Math.abs(_drawing.curX - _drawing.startX);
      var dh = Math.abs(_drawing.curY - _drawing.startY);
      ctx.strokeStyle = "#fbbf24";
      ctx.setLineDash([4, 3]);
      ctx.strokeRect(dx, dy, dw, dh);
      ctx.setLineDash([]);
    }
  }

  function _renderRelabelOverlay() {
    if (_relabelActive) {
      _drawCanvas();
    }
    var list = document.getElementById("relabel-box-list");
    if (!list) return;
    if (_relabelBoxes.length === 0) {
      list.innerHTML = '<p class="muted relabel-empty">No boxes drawn yet.</p>';
    } else {
      var html = "";
      for (var i = 0; i < _relabelBoxes.length; i++) {
        var b = _relabelBoxes[i];
        html += '<div class="relabel-box-row">';
        html += '<span class="relabel-box-idx">#' + (i + 1) + '</span> ';
        html += '<span class="relabel-box-cls">' + _escape(b.cls) + '</span> ';
        html += '<button type="button" class="btn btn-small relabel-box-remove" data-idx="' + i + '">Remove</button>';
        html += '</div>';
      }
      list.innerHTML = html;
    }
    var submit = document.getElementById("btn-relabel-submit");
    if (submit) submit.disabled = _relabelBoxes.length === 0;
  }

  function _escape(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /* Canvas mouse handlers (delegated; canvas may re-mount on swap). */

  function _onCanvasDown(e) {
    if (!_relabelActive) return;
    var canvas = document.getElementById("label-canvas");
    if (!canvas || e.target !== canvas) return;
    var rect = canvas.getBoundingClientRect();
    _drawing = {
      startX: e.clientX - rect.left,
      startY: e.clientY - rect.top,
      curX: e.clientX - rect.left,
      curY: e.clientY - rect.top,
    };
    e.preventDefault();
  }

  function _onCanvasMove(e) {
    if (!_drawing) return;
    var canvas = document.getElementById("label-canvas");
    if (!canvas) return;
    var rect = canvas.getBoundingClientRect();
    _drawing.curX = e.clientX - rect.left;
    _drawing.curY = e.clientY - rect.top;
    _drawCanvas();
  }

  function _onCanvasUp(e) {
    if (!_drawing) return;
    var canvas = document.getElementById("label-canvas");
    if (!canvas) { _drawing = null; return; }
    var rect = canvas.getBoundingClientRect();
    var endX = e.clientX - rect.left;
    var endY = e.clientY - rect.top;
    var x1 = Math.min(_drawing.startX, endX);
    var y1 = Math.min(_drawing.startY, endY);
    var x2 = Math.max(_drawing.startX, endX);
    var y2 = Math.max(_drawing.startY, endY);
    _drawing = null;

    var w = x2 - x1;
    var h = y2 - y1;
    if (w < 6 || h < 6) { _drawCanvas(); return; }

    var cw = canvas.width || 1;
    var ch = canvas.height || 1;
    var input = document.getElementById("relabel-class-input");
    var cls = (input && input.value || "").trim().toLowerCase();
    if (!cls) {
      alert("Pick a class before drawing a box.");
      _drawCanvas();
      return;
    }

    _relabelBoxes.push({
      cls: cls,
      bbox: [
        _clamp01((x1 + w / 2) / cw),
        _clamp01((y1 + h / 2) / ch),
        _clamp01(w / cw),
        _clamp01(h / ch),
      ],
    });
    _renderRelabelOverlay();
  }

  /* ── Submit re-label ─────────────────────────────────────────────── */

  function _submitRelabel() {
    var detail = _detail();
    if (!detail) return;
    var eventId = detail.dataset.eventId;
    var uploadId = detail.dataset.uploadId;
    if (!_isHexId(uploadId) || !_isUInt(eventId)) return;
    if (_relabelBoxes.length === 0) return;

    var csrf = detail.dataset.csrfToken || "";
    var fd = new FormData();
    fd.append("_csrf_token", csrf);
    fd.append("action", "corrected");
    fd.append("corrected_bboxes", JSON.stringify(_relabelBoxes));

    var qs = "?review_state=" + encodeURIComponent(detail.dataset.filterReviewState || "")
           + "&detection_pass=" + encodeURIComponent(detail.dataset.filterDetectionPass || "");
    var url = "/admin/training/uploads/" + uploadId + "/events/" + eventId + "/review" + qs;

    fetch(url, { method: "POST", body: fd, credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.text();
      })
      .then(function (html) {
        var card = document.getElementById("label-card");
        if (card) card.innerHTML = html;
        /* Manual init: fetch() doesn't fire htmx:afterSettle. */
        _afterCardSwap();
      })
      .catch(function (err) {
        alert("Submit failed: " + err.message);
      });
  }

  function _afterCardSwap() {
    _resetRelabelState();
    renderBbox();
    updateFrame();
  }

  /* ── Event delegation (survives partial swaps) ──────────────────── */

  document.addEventListener("click", function (e) {
    var t = e.target;
    if (!t) return;
    if (t.id === "btn-relabel") {
      _activateRelabel();
    } else if (t.id === "btn-relabel-cancel") {
      _resetRelabelState();
    } else if (t.id === "btn-relabel-submit") {
      _submitRelabel();
    } else if (t.classList && t.classList.contains("relabel-box-remove")) {
      var idx = parseInt(t.getAttribute("data-idx") || "-1", 10);
      if (idx >= 0 && idx < _relabelBoxes.length) {
        _relabelBoxes.splice(idx, 1);
        _renderRelabelOverlay();
      }
    }
  });

  document.addEventListener("mousedown", _onCanvasDown);
  document.addEventListener("mousemove", _onCanvasMove);
  document.addEventListener("mouseup", _onCanvasUp);
  window.addEventListener("resize", function () {
    if (_relabelActive) { _sizeCanvasToImage(); _drawCanvas(); }
  });

  document.addEventListener("keydown", function (e) {
    var tag = (e.target.tagName || "").toUpperCase();
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;

    switch (e.key) {
      case "a":
        var btn = document.getElementById("btn-approve");
        if (btn) btn.click();
        break;
      case "x":
        var btnR = document.getElementById("btn-reject");
        if (btnR) btnR.click();
        break;
      case "r":
        var btnRl = document.getElementById("btn-relabel");
        if (btnRl) btnRl.click();
        break;
      case "j":
      case "ArrowRight":
        e.preventDefault();
        navigateEvent("next");
        break;
      case "k":
      case "ArrowLeft":
        e.preventDefault();
        navigateEvent("prev");
        break;
      case "Escape":
        if (_relabelActive) { _resetRelabelState(); }
        break;
    }
  });

  /* After HTMX swaps the label card (approve/reject), re-init. */
  document.addEventListener("htmx:afterSettle", function (e) {
    if (e.detail.target && e.detail.target.id === "label-card") {
      _afterCardSwap();
    }
  });

  /* Initial render */
  renderBbox();
})();
