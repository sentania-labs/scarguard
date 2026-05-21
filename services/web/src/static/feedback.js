(function() {
  // Toggle wrong-class form visibility
  var btn = document.getElementById('btn-show-wrong');
  var form = document.getElementById('wrong-class-form');
  if (btn && form) {
    btn.addEventListener('click', function() {
      form.classList.add('open');
      btn.style.display = 'none';
      var input = form.querySelector('input[type="text"]');
      if (input) input.focus();
    });
    if (form.classList.contains('open')) {
      btn.style.display = 'none';
    }
  }

  // Draw original bbox on snapshot image
  var wrap = document.getElementById('snapshot-wrap');
  var img = document.getElementById('snapshot-img');
  if (wrap && img) {
    var bboxRaw = wrap.dataset.bbox;
    var frameSizeRaw = wrap.dataset.frameSize;

    img.addEventListener('load', function() {
      if (!bboxRaw || !frameSizeRaw) return;
      try {
        var bbox = JSON.parse(bboxRaw);
        var frameSize = JSON.parse(frameSizeRaw);
        if (!bbox || bbox.length !== 4 || !frameSize || frameSize[0] <= 0 || frameSize[1] <= 0) return;
        var scaleX = img.naturalWidth / frameSize[0];
        var scaleY = img.naturalHeight / frameSize[1];
        var x1 = bbox[0] * scaleX, y1 = bbox[1] * scaleY;
        var x2 = bbox[2] * scaleX, y2 = bbox[3] * scaleY;
        var box = document.createElement('div');
        box.className = 'fb-bbox-overlay';
        box.id = 'fb-original-bbox';
        box.style.left = (x1 / img.naturalWidth * 100) + '%';
        box.style.top = (y1 / img.naturalHeight * 100) + '%';
        box.style.width = ((x2 - x1) / img.naturalWidth * 100) + '%';
        box.style.height = ((y2 - y1) / img.naturalHeight * 100) + '%';
        wrap.appendChild(box);
      } catch (e) { /* ignore parse errors */ }
    });
    // If image already loaded (cached)
    if (img.complete && img.naturalWidth > 0) {
      img.dispatchEvent(new Event('load'));
    }
  }

  // Redraw bbox button
  var redrawBtn = document.getElementById('btn-redraw-bbox');
  if (redrawBtn && wrap && img) {
    redrawBtn.addEventListener('click', function() {
      var frameSizeRaw2 = wrap.dataset.frameSize;
      if (!frameSizeRaw2) return;
      var frameSize;
      try { frameSize = JSON.parse(frameSizeRaw2); } catch (e) { return; }
      if (!frameSize || frameSize[0] <= 0 || frameSize[1] <= 0) return;

      redrawBtn.textContent = 'Draw on image…';
      redrawBtn.disabled = true;

      // Remove prior corrected bbox
      var prev = wrap.querySelector('.fb-corrected-bbox');
      if (prev) prev.remove();

      var drawBox = document.createElement('div');
      drawBox.className = 'fb-corrected-bbox';
      drawBox.style.display = 'none';
      wrap.appendChild(drawBox);

      wrap.style.cursor = 'crosshair';
      var drawing = false;
      var startX = 0, startY = 0;

      function getPos(e) {
        var rect = img.getBoundingClientRect();
        return {
          x: Math.max(0, Math.min(e.clientX - rect.left, rect.width)),
          y: Math.max(0, Math.min(e.clientY - rect.top, rect.height))
        };
      }

      function onDown(e) {
        e.preventDefault();
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
        var pos = getPos(e);
        // Map display pixels back to original frame coordinates
        var dispToNatX = img.naturalWidth / img.clientWidth;
        var dispToNatY = img.naturalHeight / img.clientHeight;
        var natToFrameX = frameSize[0] / img.naturalWidth;
        var natToFrameY = frameSize[1] / img.naturalHeight;
        var x1 = Math.min(startX, pos.x) * dispToNatX * natToFrameX;
        var y1 = Math.min(startY, pos.y) * dispToNatY * natToFrameY;
        var x2 = Math.max(startX, pos.x) * dispToNatX * natToFrameX;
        var y2 = Math.max(startY, pos.y) * dispToNatY * natToFrameY;

        var field = document.getElementById('corrected-bbox-field');
        if (Math.abs(x2 - x1) > 5 && Math.abs(y2 - y1) > 5) {
          var coords = [Math.round(x1), Math.round(y1), Math.round(x2), Math.round(y2)];
          if (field) field.value = JSON.stringify(coords);
          redrawBtn.textContent = 'Redraw bbox ✓';
          // Hide original bbox
          var orig = document.getElementById('fb-original-bbox');
          if (orig) orig.style.display = 'none';
        } else {
          drawBox.style.display = 'none';
          if (field) field.value = '';
          redrawBtn.textContent = 'Redraw bbox';
        }
        // Clean up
        wrap.style.cursor = '';
        redrawBtn.disabled = false;
        wrap.removeEventListener('mousedown', onDown);
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      }

      wrap.addEventListener('mousedown', onDown);
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  }
})();
