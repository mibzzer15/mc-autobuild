// Live preset-application feed on a building's page. Polls /buildings/<id>/preset-status so a
// single build's preset (expand, service, hire, vehicles, crew) fills in without a refresh.
(function () {
  var box = document.getElementById("preset-live");
  if (!box) return;
  var buildingId = box.getAttribute("data-building-id");
  var statusEl = document.getElementById("preset-live-status");
  var form = document.getElementById("apply-preset-form");
  var table = document.getElementById("preset-log-table");
  var body = document.getElementById("preset-log-body");
  var wasRunning = box.getAttribute("data-in-progress") === "true";

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function render(data) {
    if (form) form.style.display = data.in_progress ? "none" : "";
    statusEl.innerHTML = data.in_progress ? "<strong>Applying now — updates live below.</strong>" : "";
    if (data.actions && data.actions.length) {
      table.style.display = "";
      var rows = "";
      for (var i = 0; i < data.actions.length; i++) {
        var a = data.actions[i];
        rows += "<tr><td>" + escapeHtml(a.created_at) + "</td><td>" + escapeHtml(a.action_type) +
          '</td><td class="' + (a.success ? "run-ok" : "run-err") + '">' +
          (a.success ? "OK" : "Failed") + " — " + escapeHtml(a.message) + "</td></tr>";
      }
      body.innerHTML = rows;
    }
  }

  function poll() {
    fetch("/buildings/" + buildingId + "/preset-status", { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        render(data);
        if (data.in_progress) { wasRunning = true; setTimeout(poll, 1500); }
      })
      .catch(function () { setTimeout(poll, 3000); });
  }

  if (wasRunning) poll();
})();
