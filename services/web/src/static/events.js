/* Event selection is a set of persisted IDs, never a moving live-feed range. */
(() => {
  'use strict';
  const classes = JSON.parse(document.getElementById('events-page-data').textContent);
  const $ = id => document.getElementById(id);
  const selected = new Set();
  let action = '', saving = false, editor = null, pending = false, refreshing = null;
  let refreshTimer = null;
  const editingRows = new Set();
  const names = {correct: 'Positive / correct', false_positive: 'Negative / false positive', wrong_class: 'Wrong class'};
  const rows = () => [...document.querySelectorAll('#event-table-body tr[data-event-id]')];
  const status = message => { $('event-review-status').textContent = message; };
  function held() {
    return selected.size > 0 || saving || !!editor || editingRows.size > 0 ||
      !!document.activeElement.closest('input, select, textarea');
  }
  function updateSelection() {
    const count = selected.size;
    const boxes = [...document.querySelectorAll('.event-select')];
    boxes.forEach(box => {
      box.checked = selected.has(Number(box.value));
      box.disabled = saving;
      box.closest('tr').classList.toggle('event-selected', box.checked);
    });
    if ($('selection-count')) {
      $('selection-count').textContent = count + ' selected';
      $('select-page-events').checked = count > 0 && count === boxes.length;
      $('select-page-events').indeterminate = count > 0 && count < boxes.length;
      $('select-page-events').disabled = saving;
      $('select-all-events').disabled = saving;
      $('select-no-events').disabled = saving;
      $('bulk-class-wrap').hidden = action !== 'wrong_class';
      $('bulk-corrected-class').disabled = saving;
      document.querySelectorAll('[data-bulk-feedback]').forEach(button => {
        button.setAttribute('aria-pressed', String(action === button.dataset.bulkFeedback));
        button.disabled = saving;
      });
      $('apply-feedback').disabled = saving || editingRows.size > 0 || !count || !action ||
        (action === 'wrong_class' && !$('bulk-corrected-class').value.trim());
      $('apply-feedback').textContent = saving ? 'Saving…' : 'Apply to ' + count + ' events';
      const reviewed = rows().filter(row => selected.has(Number(row.dataset.eventId)) && row.dataset.feedback).length;
      $('selection-summary').textContent = editingRows.size ? 'Finish or cancel row edits before applying bulk feedback.' : count ?
        (names[action] || 'Choose feedback') + (action === 'wrong_class' ? ': ' + ($('bulk-corrected-class').value.trim() || 'choose class') : '') +
        ' · ' + count + ' selected · ' + reviewed + ' previously reviewed will change.' : 'Select events, then choose feedback.';
    }
    $('event-live-status').textContent = held() ? 'Live updates paused while reviewing.' :
      pending ? 'New events available.' : 'Live updates active.';
    $('refresh-events').hidden = !pending;
    $('refresh-events').disabled = saving || !!editor || selected.size > 0 || editingRows.size > 0;
  }
  async function checkedResponse(response) {
    if (!response.ok || response.redirected) {
      let message = 'Unable to save. Your changes are still here. Please retry.';
      if (response.status === 401 || response.redirected) message = 'Your session has expired. Sign in again before saving.';
      else {
        try {
          const body = await response.json();
          if (typeof body.detail === 'string') message = body.detail;
        } catch (_) { /* Non-JSON errors keep the readable fallback. */ }
      }
      throw new Error(message);
    }
    return response;
  }
  async function refreshRows(force = false) {
    if (refreshing) {
      if (force) { await refreshing; return refreshRows(true); }
      return;
    }
    if (held() && !force) return;
    refreshing = (async () => {
    try {
      const response = await checkedResponse(await fetch(location.href, {headers: {Accept: 'text/html'}}));
      const html = new DOMParser().parseFromString(await response.text(), 'text/html');
      if (!html.getElementById('event-table-body')) throw new Error('Unable to refresh events.');
      // A user may have started selecting or editing while the read was in flight.
      if (held() && !force) return;
      ['event-table-body', 'events-heading', 'event-pagination'].forEach(id => {
        $(id).innerHTML = html.getElementById(id).innerHTML;
      });
      htmx.process($('event-table-body'));
      pending = false;
    } catch (error) { status(error.message); }
    finally { refreshing = null; updateSelection(); }
    })();
    return refreshing;
  }
  $('select-all-events')?.addEventListener('click', () => {
    rows().forEach(row => selected.add(Number(row.dataset.eventId))); updateSelection();
  });
  $('select-no-events')?.addEventListener('click', () => { selected.clear(); updateSelection(); });
  $('select-page-events')?.addEventListener('change', event => {
    if (event.target.checked) $('select-all-events').click(); else $('select-no-events').click();
  });
  document.addEventListener('change', event => {
    if (!event.target.matches('.event-select')) return;
    const id = Number(event.target.value);
    if (event.target.checked) selected.add(id); else selected.delete(id);
    updateSelection();
  });
  document.querySelectorAll('[data-bulk-feedback]').forEach(button => button.addEventListener('click', () => {
    action = button.dataset.bulkFeedback; updateSelection();
  }));
  $('bulk-corrected-class')?.addEventListener('input', updateSelection);
  $('apply-feedback')?.addEventListener('click', async () => {
    if (saving || $('apply-feedback').disabled) return;
    const payload = {event_ids: [...selected], feedback: action, corrected_class: $('bulk-corrected-class').value.trim()};
    saving = true; updateSelection(); status('Saving feedback…');
    try {
      const response = await checkedResponse(await fetch('/events/feedback/batch', {
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken()}, body: JSON.stringify(payload)
      }));
      const result = await response.json();
      selected.clear(); editingRows.clear();
      status(result.updated + ' events updated.');
      await refreshRows(true);
    } catch (error) { status(error.message); }
    finally { saving = false; updateSelection(); }
  });
  $('refresh-events').addEventListener('click', () => refreshRows());
  // Fetch persisted rows with the current filters; pub/sub fragments lack DB IDs.
  const stream = new EventSource('/events/stream');
  stream.addEventListener('detection', () => {
    pending = true; updateSelection();
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => refreshRows(), 600);
  });
  window.addEventListener('pagehide', () => { stream.close(); clearTimeout(refreshTimer); });
  document.addEventListener('htmx:beforeRequest', event => {
    if (event.target.closest('#event-table-body')) {
      if (saving) { event.preventDefault(); return; }
      saving = true; updateSelection();
    }
  });
  document.addEventListener('htmx:afterRequest', event => {
    if (!event.detail.requestConfig?.path?.match(/^\/events\/\d+\/feedback$/)) return;
    saving = false;
    if (event.detail.successful) { editingRows.delete(event.detail.requestConfig.path.split('/')[2]); status('Feedback saved.'); }
    else status('Feedback was not saved. Check your class and session, then retry.');
    updateSelection();
  });
  document.addEventListener('htmx:afterSwap', () => updateSelection());
  document.addEventListener('click', event => {
    const button = event.target.closest('button[data-action]');
    if (button && !saving) {
      if (button.dataset.action === 'cancel-inline-feedback') {
        const row = button.closest('tr');
        const td = button.closest('td');
        td.querySelector('.feedback-form').style.display = row.dataset.feedback ? 'none' : 'block';
        td.querySelector('.wrong-class-picker').style.display = 'none';
        td.querySelector('input[name="corrected_class"]').value = '';
        const badge = td.querySelector('.badge');
        const edit = td.querySelector('.btn-edit-feedback');
        if (badge) badge.style.display = '';
        if (edit) edit.style.display = '';
        button.hidden = true;
        editingRows.delete(row.dataset.eventId);
      } else if (button.dataset.action === 'show-feedback-form') {
        const td = button.closest('td');
        td.querySelector('.feedback-form').style.display = 'block';
        button.style.display = 'none';
        if (button.previousElementSibling) button.previousElementSibling.style.display = 'none';
        editingRows.add(button.closest('tr').dataset.eventId);
        button.closest('td').querySelector('.inline-feedback-cancel').hidden = false;
      } else if (button.dataset.action === 'show-wrong-class-picker') {
        button.closest('.feedback-form').querySelector('.wrong-class-picker').style.display = 'block';
        editingRows.add(button.closest('tr').dataset.eventId);
        button.closest('td').querySelector('.inline-feedback-cancel').hidden = false;
      }
      updateSelection();
    }
    const link = event.target.closest('.snapshot-link');
    if (!link) return;
    event.preventDefault();
    if (!saving) openSnapshot(link);
  });
  function element(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text) el.textContent = text;
    return el;
  }
  function openSnapshot(link) {
    if (editor) return;
    const dialog = element('dialog', 'event-snapshot-dialog');
    const head = element('div', 'snapshot-overlay__header');
    const title = element('h2', 'snapshot-overlay__class', link.dataset.className.replace(/_/g, ' ') + ' · ' + link.dataset.cameraName);
    title.id = 'snapshot-title'; dialog.setAttribute('aria-labelledby', title.id);
    const close = element('button', '', 'Close'); close.type = 'button';
    head.append(title, close);
    const wrap = element('div', 'snapshot-overlay__img-wrap');
    const img = element('img'); img.src = link.querySelector('img').src; img.draggable = false;
    img.alt = 'Event snapshot, ' + link.dataset.className + ', ' + link.dataset.cameraName;
    const originalBox = element('div', 'snapshot-overlay__bbox');
    const correctedBox = element('div', 'snapshot-overlay__corrected-bbox');
    wrap.append(img, originalBox, correctedBox);
    const panel = element('div', 'overlay-feedback event-correction-panel');
    const message = element('p', 'event-correction-status'); message.setAttribute('role', 'status');
    let bbox = JSON.parse(link.dataset.correctedBbox || 'null');
    let dirty = false, drawing = false, start = null, dragPointer = null, previousBbox = bbox, inFlight = false;
    const frame = JSON.parse(link.dataset.frameSize || 'null');
    const original = JSON.parse(link.dataset.bbox || 'null');
    function paint(box, coords) {
      box.hidden = !coords || !frame;
      if (box.hidden) return;
      box.style.left = (coords[0] / frame[0] * 100) + '%';
      box.style.top = (coords[1] / frame[1] * 100) + '%';
      box.style.width = ((coords[2] - coords[0]) / frame[0] * 100) + '%';
      box.style.height = ((coords[3] - coords[1]) / frame[1] * 100) + '%';
    }
    function paintAll() { paint(originalBox, original); paint(correctedBox, bbox); originalBox.style.opacity = bbox ? '.25' : '1'; }
    paintAll();
    const controls = element('div', 'event-review-controls');
    const correct = element('button', 'btn-fb-correct', '✓ Positive / correct');
    const negative = element('button', 'btn-fb-fp', '✕ Negative / false positive');
    const wrong = element('button', 'btn-fb-wrong', '? Wrong class');
    controls.append(correct, negative, wrong);
    const correction = element('div', 'event-review-controls'); correction.hidden = link.dataset.feedback !== 'wrong_class';
    const label = element('label', '', 'Correct class');
    const cls = element('input'); cls.type = 'text'; cls.maxLength = 100;
    cls.value = link.dataset.correctedClass || ''; cls.placeholder = 'Select or type class';
    const options = element('datalist'); options.id = 'snapshot-classes'; cls.setAttribute('list', options.id);
    classes.forEach(value => { const option = element('option'); option.value = value; options.append(option); });
    label.append(cls);
    const redraw = element('button', '', 'Redraw box');
    redraw.disabled = !frame || frame[0] <= 0 || frame[1] <= 0;
    const save = element('button', '', 'Save correction');
    correction.append(redraw, label, options, save);
    panel.append(controls, correction, message);
    if (link.dataset.canReview === 'false') { controls.hidden = true; correction.hidden = true; }
    message.textContent = bbox ? 'Saved corrected box.' :
      link.dataset.feedback ? (names[link.dataset.feedback] || '') + (cls.value ? ': ' + cls.value : '') : '';
    dialog.append(head, wrap, panel); document.body.append(dialog); editor = dialog; dialog.showModal(); updateSelection();
    function dismiss() {
      if (inFlight) return;
      if (dirty && !confirm('Discard the unsaved class and box correction?')) return;
      dialog.close(); dialog.remove(); editor = null; link.focus(); updateSelection();
    }
    close.addEventListener('click', dismiss);
    dialog.addEventListener('cancel', event => { event.preventDefault(); dismiss(); });
    // Only explicit Close/Escape dismiss. Image clicks and drag release never do.
    cls.addEventListener('input', () => { dirty = true; });
    wrong.addEventListener('click', () => { correction.hidden = false; cls.focus(); });
    redraw.addEventListener('click', () => {
      drawing = true; redraw.disabled = true; wrap.classList.add('event-drawing');
      message.textContent = 'Draw around the desired subject, then choose its class and save.';
    });
    function pos(event) {
      const rect = img.getBoundingClientRect();
      return [Math.round(Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)) * frame[0]),
        Math.round(Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)) * frame[1])];
    }
    wrap.addEventListener('pointerdown', event => {
      if (!drawing || event.button !== 0 || start) return;
      event.preventDefault(); previousBbox = bbox; start = pos(event); dragPointer = event.pointerId;
      wrap.setPointerCapture(event.pointerId);
    });
    function move(event) {
      if (!start || event.pointerId !== dragPointer) return;
      const end = pos(event);
      bbox = [Math.min(start[0], end[0]), Math.min(start[1], end[1]), Math.max(start[0], end[0]), Math.max(start[1], end[1])];
      paintAll();
    }
    wrap.addEventListener('pointermove', move);
    function endDraw(event) {
      if (!start || event.pointerId !== dragPointer) return;
      if (event.type !== 'pointercancel') move(event);
      if (event.type === 'pointercancel' || !bbox || bbox[2] - bbox[0] <= 5 || bbox[3] - bbox[1] <= 5) {
        bbox = previousBbox; message.textContent = 'No valid replacement drawn. Previous box retained; try again.';
      } else { dirty = true; message.textContent = 'Box drawn, not saved. Choose a class, then Save correction.'; }
      start = null; dragPointer = null; drawing = false; redraw.disabled = false;
      wrap.classList.remove('event-drawing'); paintAll();
    }
    wrap.addEventListener('pointerup', endDraw); wrap.addEventListener('pointercancel', endDraw);
    async function submit(feedback) {
      if (inFlight) return;
      if (feedback === 'wrong_class' && !cls.value.trim()) { message.textContent = 'Choose a correct class before saving.'; cls.focus(); return; }
      if (start) return;
      drawing = false; wrap.classList.remove('event-drawing');
      const fd = new FormData(); fd.append('feedback', feedback);
      if (feedback === 'wrong_class') {
        fd.append('corrected_class', cls.value.trim());
        if (bbox) fd.append('corrected_bbox', JSON.stringify(bbox));
      }
      inFlight = true;
      dialog.querySelectorAll('button, input').forEach(el => { el.disabled = true; });
      message.textContent = 'Saving…';
      try {
        const response = await checkedResponse(await fetch('/events/' + link.dataset.eventId + '/feedback', {
          method: 'POST', body: fd, headers: {'X-CSRF-Token': getCsrfToken()}
        }));
        const table = document.createElement('tbody'); table.innerHTML = await response.text();
        const newRow = table.querySelector('tr[data-event-id="' + link.dataset.eventId + '"]');
        if (!newRow) throw new Error('Unexpected response. Check your session before retrying.');
        const oldRow = $('event-row-' + link.dataset.eventId);
        if (oldRow) { oldRow.replaceWith(newRow); htmx.process(newRow); }
        link = newRow.querySelector('.snapshot-link'); dirty = false;
        if (feedback !== 'wrong_class') { bbox = null; cls.value = ''; correction.hidden = true; }
        paintAll(); message.textContent = 'Saved. You can close this image or continue reviewing.';
        status('Event feedback saved.'); updateSelection();
      } catch (error) { message.textContent = error.message; }
      finally {
        inFlight = false;
        dialog.querySelectorAll('button, input').forEach(el => { el.disabled = false; });
        redraw.disabled = !frame || frame[0] <= 0 || frame[1] <= 0;
      }
    }
    correct.addEventListener('click', () => submit('correct'));
    negative.addEventListener('click', () => submit('false_positive'));
    save.addEventListener('click', () => submit('wrong_class'));
  }
  updateSelection();
})();
