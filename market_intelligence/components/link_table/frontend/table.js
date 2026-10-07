/* Draws the pre-formatted rows Streamlit passes in. No network calls, no math. */

function clearChildren(node) {
  while (node && node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

function render(root, component) {
  var data = component.data || {};
  var columns = Array.isArray(data.columns) ? data.columns : [];
  var rows = Array.isArray(data.rows) ? data.rows : [];
  var clickable = Boolean(data.clickable);
  var head = root.querySelector("#link-table thead");
  var body = root.querySelector("#link-table tbody");
  if (!head || !body) {
    return;
  }
  clearChildren(head);
  clearChildren(body);
  var headRow = document.createElement("tr");
  for (var c = 0; c < columns.length; c++) {
    var th = document.createElement("th");
    th.scope = "col";
    th.textContent = String(columns[c]);
    headRow.appendChild(th);
  }
  head.appendChild(headRow);
  var nonce = 0;
  for (var r = 0; r < rows.length; r++) {
    var row = rows[r];
    var tr = document.createElement("tr");
    tr.dataset.rowId = row.id;
    if (clickable) {
      tr.className = "clickable";
    }
    var cells = Array.isArray(row.cells) ? row.cells : [];
    for (var i = 0; i < cells.length; i++) {
      var cell = cells[i] || {};
      var td = document.createElement("td");
      td.className = "tone-" + String(cell.tone || "plain");
      if (i === 0 && clickable) {
        var link = document.createElement("a");
        link.className = "row-link";
        link.href = "#";
        link.setAttribute("role", "button");
        link.dataset.rowId = row.id;
        link.textContent = String(cell.text || "");
        if (row.help || data.link_help) {
          link.title = String(row.help || data.link_help);
        }
        td.appendChild(link);
      } else {
        td.textContent = String(cell.text || "");
      }
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
  if (!clickable || root.__linkTableBound) {
    return;
  }
  root.__linkTableBound = true;
  body.addEventListener("click", function (event) {
    var target = event.target;
    while (target && target !== body && !(target.dataset && target.dataset.rowId && target.classList.contains("row-link"))) {
      target = target.parentElement;
    }
    if (!target || target === body) {
      return;
    }
    event.preventDefault();
    nonce += 1;
    var live = root.__linkTableComponent || component;
    if (live && typeof live.setTriggerValue === "function") {
      live.setTriggerValue("row_click", { rowId: target.dataset.rowId, nonce: String(Date.now()) + "-" + nonce });
    }
  });
}

export default function (component) {
  var root = component.parentElement;
  root.__linkTableComponent = component;
  render(root, component);
  return function cleanup() {
    if (root.__linkTableComponent === component) {
      root.__linkTableComponent = null;
    }
  };
}
