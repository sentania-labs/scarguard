let _es = null;
let _paused = false;
let _lineCount = 0;
let _currentService = document.getElementById('log-output').dataset.initialService || 'detector';
const MAX_LINES = 3000;

function selectService(svc, btn) {
  document.querySelectorAll(".log-svc-btn").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  _currentService = svc;
  reconnect();
}

function reconnect() {
  if (_es) { _es.close(); _es = null; }
  _paused = false;
  document.getElementById("pause-btn").textContent = "Pause";
  clearLog();
  startStream();
}

function startStream() {
  const tail = document.getElementById("tail-lines").value;
  const url = `/admin/logs/stream?service=${_currentService}&tail=${tail}`;
  setStatus("Connecting…", "muted");

  _es = new EventSource(url);

  _es.onopen = () => setStatus(`Streaming ${_currentService}`, "ok");

  _es.onmessage = (e) => {
    if (_paused) return;
    appendLine(e.data);
  };

  _es.onerror = () => {
    setStatus("Connection lost — retrying…", "warn");
  };
}

function formatCaddyJson(line) {
  // Convert Caddy JSON log lines to a human-readable format
  if (!line.trimStart().startsWith("{")) return null;
  try {
    var obj = JSON.parse(line);
    if (!obj.level && !obj.msg) return null;
    var ts = obj.ts ? new Date(obj.ts * 1000).toISOString().replace("T", " ").replace("Z", "") : "";
    var level = (obj.level || "info").toUpperCase().padEnd(5);
    var msg = obj.msg || "";
    var parts = [ts, level, msg].filter(Boolean);
    // Append useful fields (skip ts, level, msg, logger which are already shown)
    var skip = new Set(["ts", "level", "msg", "logger"]);
    Object.keys(obj).forEach(function(k) {
      if (!skip.has(k)) {
        var v = typeof obj[k] === "object" ? JSON.stringify(obj[k]) : String(obj[k]);
        parts.push(k + "=" + v);
      }
    });
    return parts.join("  ");
  } catch (_) { return null; }
}

function appendLine(text) {
  const box = document.getElementById("log-output");
  const level = detectLevel(text);

  // Format Caddy JSON lines into readable text
  var display = (_currentService === "caddy") ? formatCaddyJson(text) || text : text;

  const div = document.createElement("div");
  div.className = `log-line log-${level}`;
  div.textContent = display;
  box.appendChild(div);
  _lineCount++;

  // Trim oldest lines to stay under MAX_LINES
  while (box.children.length > MAX_LINES) {
    box.removeChild(box.firstChild);
  }

  document.getElementById("log-line-count").textContent = `${box.children.length} lines`;

  if (document.getElementById("autoscroll-toggle").checked) {
    box.scrollTop = box.scrollHeight;
  }
}

function detectLevel(line) {
  // Caddy outputs JSON logs with "level":"error" — try parsing first
  if (line.trimStart().startsWith("{")) {
    try {
      var obj = JSON.parse(line);
      if (obj.level) {
        var l = obj.level.toLowerCase();
        if (l === "error" || l === "fatal" || l === "panic") return "error";
        if (l === "warn") return "warning";
        if (l === "debug") return "debug";
        return "info";
      }
    } catch (_) { /* not JSON, fall through */ }
  }
  const u = line.toUpperCase();
  if (u.includes("CRITICAL") || u.includes("ERROR") || u.includes("EXCEPTION") || u.includes("TRACEBACK")) return "error";
  if (u.includes("WARNING") || u.includes("WARN")) return "warning";
  if (u.includes("DEBUG")) return "debug";
  return "info";
}

// CSS-class-based filter — hides lines by level without removing them from DOM
function applyFilter() {
  const filter = document.getElementById("level-filter").value;
  document.getElementById("log-output").dataset.filter = filter;
  // Re-scroll to bottom after filter change
  if (document.getElementById("autoscroll-toggle").checked) {
    const box = document.getElementById("log-output");
    box.scrollTop = box.scrollHeight;
  }
}

function togglePause() {
  _paused = !_paused;
  const btn = document.getElementById("pause-btn");
  btn.textContent = _paused ? "Resume" : "Pause";
  if (!_paused && document.getElementById("autoscroll-toggle").checked) {
    document.getElementById("log-output").scrollTop = document.getElementById("log-output").scrollHeight;
  }
}

function onAutoScrollChange() {
  if (document.getElementById("autoscroll-toggle").checked) {
    const box = document.getElementById("log-output");
    box.scrollTop = box.scrollHeight;
  }
}

function clearLog() {
  document.getElementById("log-output").innerHTML = "";
  _lineCount = 0;
  document.getElementById("log-line-count").textContent = "";
}

function setStatus(msg, style) {
  const el = document.getElementById("log-status");
  el.textContent = msg;
  el.className = "log-status-text " + (style || "");
}

// Start streaming the first service on page load
startStream();
