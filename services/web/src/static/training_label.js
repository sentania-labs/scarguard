/* Labeling queue: bbox overlay, keyboard shortcuts, auto-advance. */
(function () {
  "use strict";

  function renderBbox() {
    var detail = document.getElementById("label-detail");
    var box = document.getElementById("label-bbox");
    if (!detail || !box) return;
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
    var detail = document.getElementById("label-detail");
    var img = document.getElementById("label-frame");
    if (!detail || !img) return;
    var frameIdx = detail.dataset.frameIdx;
    if (frameIdx !== undefined && frameIdx !== "") {
      img.src = "/admin/training/uploads/" + _labelData.uploadId + "/frames/" + frameIdx;
    }
  }

  function navigateEvent(direction) {
    var detail = document.getElementById("label-detail");
    if (!detail) return;
    var targetId = direction === "next" ? detail.dataset.nextId : detail.dataset.prevId;
    if (!targetId) return;
    var qs = "?event_id=" + targetId;
    if (_labelData.filterReviewState) qs += "&review_state=" + encodeURIComponent(_labelData.filterReviewState);
    if (_labelData.filterDetectionPass) qs += "&detection_pass=" + encodeURIComponent(_labelData.filterDetectionPass);
    window.location.href = "/admin/training/uploads/" + _labelData.uploadId + qs;
  }

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
      case "c":
        var btnC = document.getElementById("btn-show-correct");
        if (btnC) btnC.click();
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
    }
  });

  /* After HTMX swaps the label card, re-render bbox and update frame. */
  document.addEventListener("htmx:afterSettle", function (e) {
    if (e.detail.target && e.detail.target.id === "label-card") {
      renderBbox();
      updateFrame();
      var detail = document.getElementById("label-detail");
      if (detail && detail.dataset.autoAdvance === "1" && detail.dataset.eventId) {
        /* Auto-advanced: already showing the next event */
      }
    }
  });

  /* Initial render */
  renderBbox();
})();
