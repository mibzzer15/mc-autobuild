// Live progress bar for "Run entire plan". Polls /plan/run/status and renders a progress bar +
// per-station log without a full page refresh. No dependencies.
(function () {
  var container = document.getElementById("plan-run-progress");
  if (!container) return;
  var runBtn = document.getElementById("plan-run-btn");

  function statusClass(status) {
    if (status === "done" || status === "skipped") return "ok";
    if (status === "failed" || status === "budget_stopped" || status === "preset_failed") return "err";
    return "warn"; // preset_incomplete, etc.
  }

  function render(data) {
    var html = "";
    if (data.in_progress) {
      var pct = data.total ? Math.round((data.processed / data.total) * 100) : 0;
      html += '<p><strong>Running…</strong> ' + data.processed + " / " + data.total + " station(s)";
      if (data.current) html += " · currently: " + escapeHtml(data.current);
      html += "</p>";
      html += '<progress max="' + data.total + '" value="' + data.processed + '" style="width:100%;height:1.2rem"></progress>';
    } else if (data.result) {
      html += '<p class="flash flash-' + (data.result.status === "success" ? "success" : "error") + '">' +
        escapeHtml(data.result.message) + ' <span class="muted">(' + escapeHtml(data.result.finished_at || "") + ")</span></p>";
    }

    var log = (data.result && data.result.log) || data.log || [];
    if (log.length) {
      html += '<table><thead><tr><th>Station</th><th>Result</th><th>Detail</th></tr></thead><tbody>';
      for (var i = 0; i < log.length; i++) {
        var row = log[i];
        html += "<tr><td>" + escapeHtml(row.name) + '</td><td class="run-' + statusClass(row.status) +
          '">' + escapeHtml(row.status) + "</td><td>" + escapeHtml(row.detail || "") + "</td></tr>";
      }
      html += "</tbody></table>";
    }
    container.innerHTML = html;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  var wasRunning = container.getAttribute("data-in-progress") === "true";

  function poll() {
    fetch("/plan/run/status", { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        render(data);
        if (runBtn) runBtn.style.display = data.in_progress ? "none" : "";
        if (data.in_progress) {
          wasRunning = true;
          setTimeout(poll, 1500);
        } else if (wasRunning) {
          // Just finished: reload once so the plan counts/table above reflect the new state.
          wasRunning = false;
          setTimeout(function () { window.location.reload(); }, 800);
        }
      })
      .catch(function () { setTimeout(poll, 3000); });
  }

  // A run is active on load (we land here right after POSTing /plan/run) — start polling.
  if (wasRunning) poll();
})();
