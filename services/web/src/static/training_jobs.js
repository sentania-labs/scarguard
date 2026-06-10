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

  /* Toggle visible fields based on job type. Process Videos shows the
     structured upload picker; other job types keep the JSON textarea. */
  function _setupJobTypeHint() {
    var sel = document.getElementById("job-type");
    if (!sel) return;
    var processFields = document.getElementById("process-video-fields");
    var jsonFields = document.getElementById("json-params-fields");
    var classesField = document.getElementById("classes-field");

    function update() {
      var isProcess = sel.value === "process_video";
      var hasClasses = sel.value === "prepare_dataset" || sel.value === "prepare_and_train";
      if (processFields) processFields.style.display = isProcess ? "" : "none";
      if (jsonFields) jsonFields.style.display = isProcess ? "none" : "";
      if (classesField) classesField.style.display = hasClasses ? "" : "none";
    }
    sel.addEventListener("change", update);
    update();
  }

  /* Delegated click for Watch buttons. CSP forbids inline onclick. */
  document.addEventListener("click", function (e) {
    var t = e.target;
    if (t && t.classList && t.classList.contains("js-watch-job")) {
      var id = t.getAttribute("data-job-id");
      if (id) window.startJobStream(id);
    }
  });

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
    var watchBtns = document.querySelectorAll(".js-watch-job[data-job-id]");
    if (watchBtns.length > 0) {
      var firstId = watchBtns[0].getAttribute("data-job-id");
      if (firstId) window.startJobStream(firstId);
    }
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
