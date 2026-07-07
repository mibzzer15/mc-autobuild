// Makes every <table class="sortable"> clickable-header-sortable. No dependencies, no build
// step - matches this project's plain server-rendered-HTML approach.
//
// Sorts as numbers if every cell in a column has a `data-sort` attribute (or, failing that,
// looks like a plain number/comma-grouped number itself); otherwise sorts as case-insensitive text.
(function () {
  function cellValue(cell) {
    if (cell.dataset.sort !== undefined) return cell.dataset.sort;
    return cell.textContent.trim();
  }

  function isNumeric(value) {
    return value !== "" && !isNaN(Number(value.replace(/,/g, "")));
  }

  function sortTable(table, columnIndex, ascending) {
    var tbody = table.tBodies[0];
    var rows = Array.prototype.slice.call(tbody.rows);

    var allNumeric = rows.every(function (row) {
      var cell = row.cells[columnIndex];
      return cell && isNumeric(cellValue(cell));
    });

    rows.sort(function (a, b) {
      var av = cellValue(a.cells[columnIndex]);
      var bv = cellValue(b.cells[columnIndex]);
      var result;
      if (allNumeric) {
        result = Number(av.replace(/,/g, "")) - Number(bv.replace(/,/g, ""));
      } else {
        result = av.toLowerCase().localeCompare(bv.toLowerCase());
      }
      return ascending ? result : -result;
    });

    rows.forEach(function (row) {
      tbody.appendChild(row);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("table.sortable").forEach(function (table) {
      var headerRow = table.tHead && table.tHead.rows[0];
      if (!headerRow) return;

      Array.prototype.forEach.call(headerRow.cells, function (th, columnIndex) {
        if (!th.textContent.trim()) return; // skip empty action-column headers
        th.classList.add("sortable-header");
        var ascending = true;
        th.addEventListener("click", function () {
          Array.prototype.forEach.call(headerRow.cells, function (other) {
            other.removeAttribute("data-sort-dir");
          });
          th.setAttribute("data-sort-dir", ascending ? "asc" : "desc");
          sortTable(table, columnIndex, ascending);
          ascending = !ascending;
        });
      });
    });
  });
})();
