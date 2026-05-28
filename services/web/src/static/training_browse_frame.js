/* Frame browser focus view: draw multi-box annotations on a frame that
   had no detector output, then POST to /annotate. */
(function () {
  "use strict";

  var _boxes = [];
  var _drawing = null;

  function _clamp01(v) { return Math.max(0, Math.min(1, v)); }

  function _detail() { return document.getElementById("label-detail"); }

  function _escape(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function _sizeCanvasToImage() {
    var img = document.getElementById("label-frame");
    var canvas = document.getElementById("label-canvas");
    if (!img || !canvas) return;
    canvas.width = img.clientWidth;
    canvas.height = img.clientHeight;
  }

  function _drawCanvas() {
    var canvas = document.getElementById("label-canvas");
    if (!canvas) return;
    var ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    ctx.lineWidth = 2;
    ctx.font = "10px monospace";
    for (var i = 0; i < _boxes.length; i++) {
      var b = _boxes[i].bbox;
      var x = (b[0] - b[2] / 2) * canvas.width;
      var y = (b[1] - b[3] / 2) * canvas.height;
      var w = b[2] * canvas.width;
      var h = b[3] * canvas.height;
      ctx.strokeStyle = "#34d399";
      ctx.strokeRect(x, y, w, h);
      ctx.fillStyle = "rgba(52, 211, 153, 0.9)";
      var label = _boxes[i].cls + " #" + (i + 1);
      var tw = ctx.measureText(label).width + 4;
      var ly = y >= 12 ? y - 12 : y;
      ctx.fillRect(x, ly, tw, 12);
      ctx.fillStyle = "#0a1a12";
      ctx.fillText(label, x + 2, ly + 9);
    }

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

  function _renderList() {
    var list = document.getElementById("relabel-box-list");
    if (!list) return;
    if (_boxes.length === 0) {
      list.innerHTML = '<p class="muted relabel-empty">No boxes drawn yet.</p>';
    } else {
      var html = "";
      for (var i = 0; i < _boxes.length; i++) {
        var b = _boxes[i];
        html += '<div class="relabel-box-row">';
        html += '<span class="relabel-box-idx">#' + (i + 1) + '</span> ';
        html += '<span class="relabel-box-cls">' + _escape(b.cls) + '</span> ';
        html += '<button type="button" class="btn btn-small relabel-box-remove" data-idx="' + i + '">Remove</button>';
        html += '</div>';
      }
      list.innerHTML = html;
    }
    var save = document.getElementById("btn-browse-save");
    if (save) save.disabled = _boxes.length === 0;
    _drawCanvas();
  }

  function _loadExisting() {
    var detail = _detail();
    if (!detail) return;
    var raw = detail.dataset.existingBboxes || "";
    if (!raw) return;
    try {
      var entries = JSON.parse(raw);
      if (!Array.isArray(entries)) return;
      for (var i = 0; i < entries.length; i++) {
        var b = entries[i] && entries[i].bbox;
        var cls = entries[i] && entries[i].cls;
        if (!Array.isArray(b) || b.length < 4 || typeof cls !== "string") continue;
        _boxes.push({ cls: cls, bbox: [+b[0], +b[1], +b[2], +b[3]] });
      }
      _renderList();
    } catch (e) {
      /* ignore malformed payload */
    }
  }

  function _onCanvasDown(e) {
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
    var cw = canvas.width || 1;
    var ch = canvas.height || 1;
    var endX = Math.max(0, Math.min(cw, e.clientX - rect.left));
    var endY = Math.max(0, Math.min(ch, e.clientY - rect.top));
    var startX = Math.max(0, Math.min(cw, _drawing.startX));
    var startY = Math.max(0, Math.min(ch, _drawing.startY));
    var x1 = Math.min(startX, endX);
    var y1 = Math.min(startY, endY);
    var x2 = Math.max(startX, endX);
    var y2 = Math.max(startY, endY);
    _drawing = null;

    var w = x2 - x1;
    var h = y2 - y1;
    if (w < 6 || h < 6) { _drawCanvas(); return; }

    var input = document.getElementById("relabel-class-input");
    var cls = (input && input.value || "").trim().toLowerCase();
    if (!cls) {
      alert("Pick a class before drawing a box.");
      _drawCanvas();
      return;
    }

    _boxes.push({
      cls: cls,
      bbox: [
        _clamp01((x1 + w / 2) / cw),
        _clamp01((y1 + h / 2) / ch),
        _clamp01(w / cw),
        _clamp01(h / ch),
      ],
    });
    _renderList();
  }

  function _save(thenNext) {
    var detail = _detail();
    if (!detail || _boxes.length === 0) return;
    var uploadId = detail.dataset.uploadId;
    var frameIdx = detail.dataset.frameIdx;
    if (!/^[a-f0-9]+$/.test(uploadId) || !/^\d+$/.test(frameIdx)) return;

    var fd = new FormData();
    fd.append("_csrf_token", detail.dataset.csrfToken || "");
    fd.append("corrected_bboxes", JSON.stringify(_boxes));

    var url = "/admin/training/uploads/" + uploadId + "/browse/" + frameIdx + "/annotate";
    fetch(url, { method: "POST", body: fd, credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function () {
        if (thenNext) {
          var nextLink = document.getElementById("browse-next");
          if (nextLink) { window.location.href = nextLink.href; return; }
        }
        window.location.reload();
      })
      .catch(function (err) { alert("Save failed: " + err.message); });
  }

  /* ── Events ─────────────────────────────────────────────────────────── */

  document.addEventListener("click", function (e) {
    var t = e.target;
    if (!t) return;
    if (t.id === "btn-browse-save") {
      _save(true);
    } else if (t.classList && t.classList.contains("relabel-box-remove")) {
      var idx = parseInt(t.getAttribute("data-idx") || "-1", 10);
      if (idx >= 0 && idx < _boxes.length) {
        _boxes.splice(idx, 1);
        _renderList();
      }
    }
  });

  document.addEventListener("mousedown", _onCanvasDown);
  document.addEventListener("mousemove", _onCanvasMove);
  document.addEventListener("mouseup", _onCanvasUp);
  window.addEventListener("resize", function () { _sizeCanvasToImage(); _drawCanvas(); });

  document.addEventListener("keydown", function (e) {
    var tag = (e.target.tagName || "").toUpperCase();
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    switch (e.key) {
      case "j":
      case "ArrowRight":
        e.preventDefault();
        var nxt = document.getElementById("browse-next");
        if (nxt) window.location.href = nxt.href;
        break;
      case "k":
      case "ArrowLeft":
        e.preventDefault();
        var prv = document.getElementById("browse-prev");
        if (prv) window.location.href = prv.href;
        break;
      case "s":
        e.preventDefault();
        var save = document.getElementById("btn-browse-save");
        if (save && !save.disabled) save.click();
        break;
    }
  });

  /* Resize canvas once the image lays out. */
  var img = document.getElementById("label-frame");
  if (img) {
    if (img.complete) { _sizeCanvasToImage(); _drawCanvas(); }
    img.addEventListener("load", function () { _sizeCanvasToImage(); _drawCanvas(); });
  }

  _loadExisting();
})();
