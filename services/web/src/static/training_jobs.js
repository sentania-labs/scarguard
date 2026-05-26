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

  /* Auto-start stream for running jobs on page load */
  document.addEventListener("DOMContentLoaded", function () {
    var rows = document.querySelectorAll("tr");
    rows.forEach(function (tr) {
      var statusBadge = tr.querySelector(".badge");
      if (statusBadge && statusBadge.textContent.trim() === "running") {
        var watchBtn = tr.querySelector("button[onclick^='startJobStream']");
        if (watchBtn) watchBtn.click();
      }
    });
  });
})();
