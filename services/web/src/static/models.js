(function() {
  var cache = {};

  async function fetchClasses(name) {
    if (cache[name]) return cache[name];
    var resp = await fetch('/models/' + encodeURIComponent(name) + '/classes');
    var data = await resp.json();
    cache[name] = data;
    return data;
  }

  function renderPanel(panel, data) {
    panel.innerHTML = '';
    if (!data.ok) {
      var err = document.createElement('span');
      err.className = 'alert alert-err';
      err.textContent = 'Error: ' + (data.error || 'unknown');
      panel.appendChild(err);
      return;
    }
    var classes = data.classes || [];
    if (classes.length === 0) {
      var msg = document.createElement('p');
      msg.className = 'muted';
      msg.style.margin = '0';
      msg.textContent = data.warning || 'No classes embedded in this model.';
      panel.appendChild(msg);
      return;
    }

    var header = document.createElement('div');
    header.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:0.4rem;gap:0.5rem;flex-wrap:wrap;';
    var count = document.createElement('span');
    count.className = 'muted';
    count.textContent = classes.length + ' class' + (classes.length === 1 ? '' : 'es');
    if (data.cached) count.textContent += ' (cached)';
    header.appendChild(count);

    var copyBtn = document.createElement('button');
    copyBtn.type = 'button';
    copyBtn.className = 'btn-secondary';
    copyBtn.style.fontSize = '0.8rem';
    copyBtn.textContent = 'Copy comma-separated list';
    copyBtn.addEventListener('click', function() {
      var text = classes.join(', ');
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function() {
          copyBtn.textContent = 'Copied!';
          setTimeout(function() { copyBtn.textContent = 'Copy comma-separated list'; }, 1500);
        });
      }
    });
    header.appendChild(copyBtn);
    panel.appendChild(header);

    var cloud = document.createElement('div');
    cloud.className = 'model-class-cloud';
    classes.forEach(function(c) {
      var chip = document.createElement('span');
      chip.className = 'tag tag-ok';
      chip.textContent = c;
      cloud.appendChild(chip);
    });
    panel.appendChild(cloud);

    if (data.warning) {
      var warn = document.createElement('p');
      warn.className = 'muted';
      warn.style.margin = '0.5rem 0 0';
      warn.textContent = 'Note: ' + data.warning;
      panel.appendChild(warn);
    }
  }

  document.querySelectorAll('.btn-show-classes').forEach(function(btn) {
    btn.addEventListener('click', async function() {
      var name = btn.dataset.name;
      var row = document.querySelector('.model-classes-row[data-name="' + name + '"]');
      var panel = row.querySelector('.model-classes-panel');
      if (row.style.display === 'none') {
        row.style.display = '';
        btn.textContent = 'Hide classes';
        panel.innerHTML = '<span class="muted">Loading…</span>';
        try {
          var data = await fetchClasses(name);
          renderPanel(panel, data);
        } catch (e) {
          panel.innerHTML = '<span class="alert alert-err">Network error: ' + e.message + '</span>';
        }
      } else {
        row.style.display = 'none';
        btn.textContent = 'Show classes';
      }
    });
  });
})();
