var evtSource = null;

document.getElementById('eval-form').addEventListener('htmx:afterRequest', function() {
  if (evtSource) { evtSource.close(); }
  document.getElementById('eval-progress').style.display = 'block';
  document.getElementById('eval-results').style.display = 'none';
  document.getElementById('eval-submit').disabled = true;

  evtSource = new EventSource('/admin/training/evaluate/stream');

  evtSource.addEventListener('progress', function(e) {
    var data = JSON.parse(e.data);
    var bar = document.getElementById('progress-bar');
    var label = document.getElementById('progress-label');
    var detail = document.getElementById('progress-detail');
    bar.style.width = data.pct + '%';
    label.textContent = data.status.charAt(0).toUpperCase() + data.status.slice(1);
    detail.textContent = data.current + ' / ' + data.total;
  });

  evtSource.addEventListener('result', function(e) {
    evtSource.close();
    evtSource = null;
    document.getElementById('eval-submit').disabled = false;
    var data = JSON.parse(e.data);

    if (data.status === 'error') {
      document.getElementById('eval-progress').style.display = 'none';
      document.getElementById('eval-status').innerHTML =
        '<div class="alert alert-err">' + escHtml(data.error) + '</div>';
      return;
    }

    document.getElementById('eval-results').style.display = 'block';
    renderResults(data);
  });

  evtSource.onerror = function() {
    evtSource.close();
    evtSource = null;
    document.getElementById('eval-submit').disabled = false;
  };
});

function renderResults(data) {
  var a = data.model_a;
  var b = data.model_b;
  document.getElementById('results-a-title').textContent = 'Model A: ' + a.path.split('/').pop();
  document.getElementById('results-b-title').textContent = 'Model B: ' + b.path.split('/').pop();
  document.getElementById('results-a-table').innerHTML = metricsTable(a.metrics);
  document.getElementById('results-b-table').innerHTML = metricsTable(b.metrics);

  var bName = b.path.split('/').pop();
  document.getElementById('promote-model-path').value = bName;
  document.getElementById('promote-btn').textContent = 'Promote ' + bName;
  document.getElementById('promote-section').style.display = 'block';

  var grid = document.getElementById('eval-samples');
  grid.innerHTML = '';
  (data.samples || []).forEach(function(s) {
    var div = document.createElement('div');
    div.className = 'eval-sample-card card';
    var img = document.createElement('img');
    img.src = '/snapshots/' + s.snapshot_filename;
    img.style.cssText = 'max-width:100%;border-radius:4px;';
    img.loading = 'lazy';
    div.appendChild(img);
    var info = document.createElement('div');
    info.className = 'eval-sample-info';
    info.innerHTML =
      '<strong>GT:</strong> ' + escHtml(s.ground_truth.class_name) +
      '<br><strong>A:</strong> ' + (s.predictions_a.length ? s.predictions_a.map(function(p){return escHtml(p.class_name)+' '+Math.round(p.confidence*100)+'%';}).join(', ') : 'none') +
      '<br><strong>B:</strong> ' + (s.predictions_b.length ? s.predictions_b.map(function(p){return escHtml(p.class_name)+' '+Math.round(p.confidence*100)+'%';}).join(', ') : 'none');
    div.appendChild(info);
    grid.appendChild(div);
  });
}

function metricsTable(metrics) {
  var html = '<thead><tr><th>Class</th><th>Precision</th><th>Recall</th><th>F1</th><th>TP</th><th>FP</th><th>FN</th></tr></thead><tbody>';
  var pc = metrics.per_class || {};
  var classes = Object.keys(pc).sort();
  classes.forEach(function(cls) {
    var m = pc[cls];
    html += '<tr><td>' + escHtml(cls.replace(/_/g,' ')) + '</td>';
    html += '<td>' + (m.precision * 100).toFixed(1) + '%</td>';
    html += '<td>' + (m.recall * 100).toFixed(1) + '%</td>';
    html += '<td>' + (m.f1 * 100).toFixed(1) + '%</td>';
    html += '<td>' + m.tp + '</td><td>' + m.fp + '</td><td>' + m.fn + '</td></tr>';
  });
  html += '</tbody>';
  html += '<tfoot><tr><td><strong>Mean</strong></td>';
  html += '<td><strong>' + (metrics.mean_precision * 100).toFixed(1) + '%</strong></td>';
  html += '<td><strong>' + (metrics.mean_recall * 100).toFixed(1) + '%</strong></td>';
  html += '<td><strong>' + (metrics.mean_f1 * 100).toFixed(1) + '%</strong></td>';
  html += '<td colspan="3"></td>';
  html += '</tr></tfoot>';
  return html;
}

function escHtml(s) {
  var d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

document.getElementById('promote-form').addEventListener('submit', function(e) {
  e.preventDefault();
  var form = this;
  fetch(form.action, {
    method: 'POST',
    body: new FormData(form),
    headers: {'X-CSRF-Token': getCsrfToken()},
  }).then(function(r) { return r.text(); })
    .then(function(html) { document.getElementById('promote-result').innerHTML = html; });
});
