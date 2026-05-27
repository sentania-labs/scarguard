/* Training jobs: SSE progress streaming + log tailing. */
(function () {
  "use strict";

  var evtSource = null;

  window.startJobStream = function (jobId) {
    if (evtSource) { evtSource.close(); }

    var progressEl = document.getElementById("job-progress");
    var barEl = document.getElementById("progress-bar");
    var labelEl = document.getElementById("progress-label");
    var detailEl = document.getElementById("progress-detail");
    var logEl = document.getElementById("job-log");

    if (progressEl) progressEl.style.display = "block";
    if (logEl) logEl.textContent = "";

    evtSource = new EventSource("/admin/training/jobs/" + jobId + "/stream");

    evtSource.addEventListener("progress", function (e) {
      var data = JSON.parse(e.data);
      if (barEl) barEl.style.width = data.pct + "%";
      if (labelEl) labelEl.textContent = data.phase || "Working...";
      if (detailEl) detailEl.textContent = data.detail || "";
    });

    evtSource.addEventListener("log", function (e) {
      var line = JSON.parse(e.data);
      if (logEl) {
        logEl.textContent += line + "\n";
        logEl.scrollTop = logEl.scrollHeight;
      }
    });

    evtSource.addEventListener("result", function (e) {
      evtSource.close();
      evtSource = null;
      var data = JSON.parse(e.data);
      if (labelEl) labelEl.textContent = data.status === "completed" ? "Complete" : "Failed";
      if (barEl) barEl.style.width = "100%";
      if (detailEl) {
        if (data.error) {
          detailEl.textContent = "Error: " + data.error;
        } else if (data.model_path) {
          detailEl.textContent = "Model saved to " + data.model_path;
        } else {
          detailEl.textContent = "Done";
        }
      }
      setTimeout(function () { window.location.reload(); }, 2000);
    });

    evtSource.onerror = function () {
      evtSource.close();
      evtSource = null;
      if (labelEl) labelEl.textContent = "Connection lost";
    };
  };

  /* Update hint text when job type changes. */
  function _setupJobTypeHint() {
    var sel = document.getElementById("job-type");
    var hint = document.getElementById("params-hint");
    var textarea = document.getElementById("params-json");
    if (!sel || !hint) return;

    var defaultHint = "Override defaults from config. Leave {} for defaults.";
    var processHint = "Processes all uploaded videos that haven't been processed yet. No parameters needed.";

    function update() {
      if (sel.value === "process_video") {
        hint.textContent = processHint;
        if (textarea && textarea.value === "{}") textarea.value = "{}";
      } else {
        hint.textContent = defaultHint;
      }
    }
    sel.addEventListener("change", update);
    update();
  }

  /* Auto-start stream on page load: either for a just-submitted job
     (via ?submitted= query param) or for any already-running job. */
  document.addEventListener("DOMContentLoaded", function () {
    _setupJobTypeHint();
    var params = new URLSearchParams(window.location.search);
    var submitted = params.get("submitted");
    if (submitted) {
      _pollUntilRunning(submitted);
      return;
    }
    var rows = document.querySelectorAll("tr");
    rows.forEach(function (tr) {
      var statusBadge = tr.querySelector(".badge");
      if (statusBadge && statusBadge.textContent.trim() === "running") {
        var watchBtn = tr.querySelector("button[onclick^='startJobStream']");
        if (watchBtn) watchBtn.click();
      }
    });
  });

  function _pollUntilRunning(jobId) {
    var progressEl = document.getElementById("job-progress");
    var labelEl = document.getElementById("progress-label");
    if (progressEl) progressEl.style.display = "block";
    if (labelEl) labelEl.textContent = "Waiting for trainer to pick up job...";

    var attempts = 0;
    var maxAttempts = 120;
    var poll = setInterval(function () {
      attempts++;
      fetch("/admin/training/jobs/" + jobId)
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.job && data.job.status === "running") {
            clearInterval(poll);
            startJobStream(jobId);
          } else if (data.job && (data.job.status === "completed" || data.job.status === "failed" || data.job.status === "cancelled")) {
            clearInterval(poll);
            if (labelEl) labelEl.textContent = "Job " + data.job.status;
            setTimeout(function () { window.location.reload(); }, 1000);
          } else if (attempts >= maxAttempts) {
            clearInterval(poll);
            if (labelEl) labelEl.textContent = "Timed out waiting for job to start";
          }
        })
        .catch(function () {});
    }, 2000);
  }
})();
