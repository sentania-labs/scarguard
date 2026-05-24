var SCARGUARD_TARGET_CLASSES = JSON.parse(
  document.getElementById('events-page-data').textContent
);

// Delegated handlers for in-row feedback controls (event_rows.html partial,
// which is swapped in by HTMX so per-element listeners would be lost).
document.addEventListener('click', function(e) {
  var btn = e.target.closest('button[data-action]');
  if (!btn) return;
  if (btn.dataset.action === 'show-feedback-form') {
    var td = btn.closest('td');
    if (!td) return;
    var form = td.querySelector('.feedback-form');
    if (form) form.style.display = 'block';
    btn.style.display = 'none';
    if (btn.previousElementSibling) btn.previousElementSibling.style.display = 'none';
  } else if (btn.dataset.action === 'show-wrong-class-picker') {
    var fb = btn.closest('.feedback-form');
    if (!fb) return;
    var picker = fb.querySelector('.wrong-class-picker');
    if (picker) picker.style.display = 'block';
  }
});

document.addEventListener('click', function(e) {
  var link = e.target.closest('.snapshot-link');
  if (!link) return;
  e.preventDefault();

  var bbox = link.dataset.bbox ? JSON.parse(link.dataset.bbox) : null;
  var frameSize = link.dataset.frameSize ? JSON.parse(link.dataset.frameSize) : null;
  var eventId = link.dataset.eventId ? parseInt(link.dataset.eventId) : null;
  var initFeedback = link.dataset.feedback || '';
  var initCorrected = link.dataset.correctedClass || '';
  var className = link.dataset.className || '';
  var confidence = link.dataset.confidence ? parseFloat(link.dataset.confidence) : null;
  var cameraName = link.dataset.cameraName || '';
  var src = link.querySelector('img').src;

  var overlay = document.createElement('div');
  overlay.className = 'snapshot-overlay';

  var inner = document.createElement('div');
  inner.className = 'snapshot-overlay__inner';

  var header = document.createElement('div');
  header.className = 'snapshot-overlay__header';
  header.addEventListener('click', function(ev) { ev.stopPropagation(); });
  var headerHTML = '';
  if (className) {
    headerHTML += '<span class="snapshot-overlay__class">' +
                  _escHtml(className.replace(/_/g, ' ')) + '</span>';
  }
  if (confidence !== null && !isNaN(confidence)) {
    headerHTML += '<span class="snapshot-overlay__conf">' +
                  Math.round(confidence * 100) + '%</span>';
  }
  if (cameraName) {
    headerHTML += '<span class="snapshot-overlay__cam">' +
                  _escHtml(cameraName) + '</span>';
  }
  if (headerHTML) {
    header.innerHTML = headerHTML;
    inner.appendChild(header);
  }

  var imgWrap = document.createElement('div');
  imgWrap.className = 'snapshot-overlay__img-wrap';
  var img = document.createElement('img');
  img.src = src;
  imgWrap.appendChild(img);
  inner.appendChild(imgWrap);

  img.onload = function() {
    if (bbox && frameSize && frameSize[0] > 0 && frameSize[1] > 0) {
      var scaleX = img.naturalWidth / frameSize[0];
      var scaleY = img.naturalHeight / frameSize[1];
      var x1 = bbox[0] * scaleX, y1 = bbox[1] * scaleY;
      var x2 = bbox[2] * scaleX, y2 = bbox[3] * scaleY;
      var pctL = (x1 / img.naturalWidth) * 100;
      var pctT = (y1 / img.naturalHeight) * 100;
      var pctW = ((x2 - x1) / img.naturalWidth) * 100;
      var pctH = ((y2 - y1) / img.naturalHeight) * 100;
      var box = document.createElement('div');
      box.className = 'snapshot-overlay__bbox';
      box.style.left = pctL + '%';
      box.style.top = pctT + '%';
      box.style.width = pctW + '%';
      box.style.height = pctH + '%';
      imgWrap.appendChild(box);
    }
  };

  if (eventId) {
    var fbPanel = document.createElement('div');
    fbPanel.className = 'overlay-feedback';
    fbPanel.innerHTML = buildOverlayFeedbackHTML(initFeedback, initCorrected);
    fbPanel.addEventListener('click', function(ev) { ev.stopPropagation(); });
    wireOverlayFeedback(fbPanel, eventId, link);
    inner.appendChild(fbPanel);
  }

  overlay.appendChild(inner);
  overlay.addEventListener('click', function() { overlay.remove(); });
  document.body.appendChild(overlay);
});

function _escHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function buildOverlayFeedbackHTML(feedback, correctedClass) {
  var html = '<div class="overlay-feedback-label">Feedback</div>';
  if (feedback) {
    var bCls = feedback === 'correct' ? 'badge-correct' :
               feedback === 'false_positive' ? 'badge-fp' : 'badge-wrong';
    var bTxt = feedback === 'correct' ? 'Correct' :
               feedback === 'false_positive' ? 'False Positive' :
               'Wrong: ' + _escHtml((correctedClass || '?').replace(/_/g, ' '));
    html += '<span class="badge ' + bCls + '">' + bTxt + '</span>';
    html += '<button class="btn-edit-feedback js-overlay-edit">edit</button>';
  }
  html += '<div class="overlay-fb-btns"' + (feedback ? ' style="display:none"' : '') + '>';
  html += '<button class="btn-fb btn-fb-correct js-fb" data-fb="correct" title="Correct detection">&#10003;</button>';
  html += '<button class="btn-fb btn-fb-fp js-fb" data-fb="false_positive" title="False positive">&#10007;</button>';
  html += '<button class="btn-fb btn-fb-wrong js-fb-wrong" title="Wrong class">?</button>';
  html += '</div>';
  html += '<div class="wrong-class-picker" style="display:none">';
  var dlId = 'overlay-class-options-' + Math.random().toString(36).slice(2, 8);
  html += '<input type="text" class="js-wrong-cls" list="' + dlId + '" placeholder="Select or type class…">';
  html += '<datalist id="' + dlId + '">';
  (SCARGUARD_TARGET_CLASSES || []).forEach(function(cls) {
    var escaped = _escHtml(cls);
    html += '<option value="' + escaped + '">' + escaped.replace(/_/g, ' ') + '</option>';
  });
  html += '</datalist>';
  html += '<button class="btn-fb btn-fb-wrong js-wrong-set">Set</button>';
  html += '<button class="btn-fb js-redraw-bbox" title="Redraw bounding box" style="margin-left:4px">Redraw bbox</button>';
  html += '</div>';
  return html;
}

function wireOverlayFeedback(panel, eventId, sourceLink) {
  var editBtn = panel.querySelector('.js-overlay-edit');
  if (editBtn) {
    editBtn.addEventListener('click', function() {
      panel.querySelector('.overlay-fb-btns').style.display = 'flex';
      this.style.display = 'none';
      var badge = panel.querySelector('.badge');
      if (badge) badge.style.display = 'none';
    });
  }

  panel.querySelectorAll('.js-fb').forEach(function(btn) {
    btn.addEventListener('click', function() {
      doOverlayFeedback(panel, eventId, btn.dataset.fb, '', sourceLink);
    });
  });

  var wrongBtn = panel.querySelector('.js-fb-wrong');
  if (wrongBtn) {
    wrongBtn.addEventListener('click', function() {
      panel.querySelector('.wrong-class-picker').style.display = 'flex';
    });
  }

  // State for corrected bbox drawn by user
  panel._correctedBbox = null;

  var redrawBtn = panel.querySelector('.js-redraw-bbox');
  if (redrawBtn) {
    redrawBtn.addEventListener('click', function() {
      var overlay = panel.closest('.snapshot-overlay');
      if (!overlay) return;
      var imgWrap = overlay.querySelector('.snapshot-overlay__img-wrap');
      var img = imgWrap ? imgWrap.querySelector('img') : null;
      if (!img) return;
      _initBboxDraw(imgWrap, img, sourceLink, panel, redrawBtn);
    });
  }

  var setBtn = panel.querySelector('.js-wrong-set');
  if (setBtn) {
    setBtn.addEventListener('click', function() {
      var cls = panel.querySelector('.js-wrong-cls').value.trim();
      if (cls) doOverlayFeedback(panel, eventId, 'wrong_class', cls, sourceLink);
    });
  }
}

/**
 * Enable drag-to-draw on the image wrap.  The user drags a green
 * rectangle; on mouseup the corrected bbox is stored on panel._correctedBbox
 * as [x1, y1, x2, y2] in original frame-pixel coordinates.
 */
function _initBboxDraw(imgWrap, img, sourceLink, panel, redrawBtn) {
  // Parse frame_size from the source link to map back to frame coords
  var frameSize = sourceLink.dataset.frameSize ? JSON.parse(sourceLink.dataset.frameSize) : null;
  if (!frameSize || frameSize[0] <= 0 || frameSize[1] <= 0) return;

  redrawBtn.textContent = 'Draw on image…';
  redrawBtn.disabled = true;

  // Remove any prior corrected-bbox overlay
  var prev = imgWrap.querySelector('.snapshot-overlay__corrected-bbox');
  if (prev) prev.remove();

  var drawing = false;
  var startX = 0, startY = 0;
  var drawBox = document.createElement('div');
  drawBox.className = 'snapshot-overlay__corrected-bbox';
  drawBox.style.cssText = 'position:absolute;border:2px solid #3ecf8e;pointer-events:none;display:none;';
  imgWrap.appendChild(drawBox);

  imgWrap.style.cursor = 'crosshair';

  function getPos(e) {
    var rect = img.getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(e.clientX - rect.left, rect.width)),
      y: Math.max(0, Math.min(e.clientY - rect.top, rect.height))
    };
  }

  function onDown(e) {
    e.preventDefault();
    e.stopPropagation();
    drawing = true;
    var pos = getPos(e);
    startX = pos.x;
    startY = pos.y;
    drawBox.style.left = (startX / img.clientWidth * 100) + '%';
    drawBox.style.top = (startY / img.clientHeight * 100) + '%';
    drawBox.style.width = '0';
    drawBox.style.height = '0';
    drawBox.style.display = 'block';
  }

  function onMove(e) {
    if (!drawing) return;
    e.preventDefault();
    e.stopPropagation();
    var pos = getPos(e);
    var x = Math.min(startX, pos.x);
    var y = Math.min(startY, pos.y);
    var w = Math.abs(pos.x - startX);
    var h = Math.abs(pos.y - startY);
    drawBox.style.left = (x / img.clientWidth * 100) + '%';
    drawBox.style.top = (y / img.clientHeight * 100) + '%';
    drawBox.style.width = (w / img.clientWidth * 100) + '%';
    drawBox.style.height = (h / img.clientHeight * 100) + '%';
  }

  function onUp(e) {
    if (!drawing) return;
    drawing = false;
    e.preventDefault();
    e.stopPropagation();
    var pos = getPos(e);
    // Convert pixel coords on the displayed image back to frame coordinates
    var scaleX = frameSize[0] / img.naturalWidth;
    var scaleY = frameSize[1] / img.naturalHeight;
    var dispToNatX = img.naturalWidth / img.clientWidth;
    var dispToNatY = img.naturalHeight / img.clientHeight;
    var x1 = Math.min(startX, pos.x) * dispToNatX * scaleX;
    var y1 = Math.min(startY, pos.y) * dispToNatY * scaleY;
    var x2 = Math.max(startX, pos.x) * dispToNatX * scaleX;
    var y2 = Math.max(startY, pos.y) * dispToNatY * scaleY;
    // Require a minimum size to avoid accidental clicks
    if (Math.abs(x2 - x1) > 5 && Math.abs(y2 - y1) > 5) {
      panel._correctedBbox = [
        Math.round(x1), Math.round(y1),
        Math.round(x2), Math.round(y2)
      ];
      redrawBtn.textContent = 'Redraw bbox ✓';
      // Remove the original red bbox if present
      var origBox = imgWrap.querySelector('.snapshot-overlay__bbox');
      if (origBox) origBox.style.display = 'none';
    } else {
      drawBox.style.display = 'none';
      redrawBtn.textContent = 'Redraw bbox';
      panel._correctedBbox = null;
    }
    // Clean up listeners
    imgWrap.style.cursor = '';
    redrawBtn.disabled = false;
    imgWrap.removeEventListener('mousedown', onDown);
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
  }

  imgWrap.addEventListener('mousedown', onDown);
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
}

function doOverlayFeedback(panel, eventId, feedback, correctedClass, sourceLink) {
  var fd = new FormData();
  fd.append('feedback', feedback);
  if (correctedClass) fd.append('corrected_class', correctedClass);
  if (feedback === 'wrong_class' && panel._correctedBbox) {
    fd.append('corrected_bbox', JSON.stringify(panel._correctedBbox));
  }
  fetch('/events/' + eventId + '/feedback', { method: 'POST', body: fd, headers: {'X-CSRF-Token': getCsrfToken()} })
    .then(function(r) { return r.text(); })
    .then(function(html) {
      var row = document.getElementById('event-row-' + eventId);
      if (row) {
        var tmp = document.createElement('tbody');
        tmp.innerHTML = html;
        var newRow = tmp.firstElementChild;
        if (newRow) {
          row.replaceWith(newRow);
          htmx.process(newRow);
        }
      }
      sourceLink.dataset.feedback = feedback;
      sourceLink.dataset.correctedClass = correctedClass;
      var overlay = panel.closest('.snapshot-overlay');
      if (overlay) overlay.remove();
    })
    .catch(function(err) { console.error('Feedback submit failed:', err); });
}
