/* Renders the tenor curve Streamlit passes in. No network calls. */
var MOBILE_QUERY = "(max-width: 699px)";
var MOBILE_HEIGHT = 320;
var DESKTOP_HEIGHT = 390;

function frameHeight(root) {
  var mobile = root && root.__tenorMobile ? root.__tenorMobile : MOBILE_HEIGHT;
  var desktop = root && root.__tenorDesktop ? root.__tenorDesktop : DESKTOP_HEIGHT;
  return window.matchMedia(MOBILE_QUERY).matches ? mobile : desktop;
}

function sizeBox(el, height) {
  if (!el || !el.style) {
    return;
  }
  el.style.setProperty("height", height, "important");
  el.style.setProperty("min-height", "0px", "important");
  el.style.setProperty("flex-basis", height, "important");
  el.style.setProperty("flex-grow", "0", "important");
  el.style.setProperty("flex-shrink", "0", "important");
}

function applyFrameHeight(root) {
  var full = frameHeight(root);
  var height = full + "px";
  var node = root && root.host ? root.host : null;
  var container = null;
  var current = node;
  while (current) {
    if (current.classList && current.classList.contains("stElementContainer")) {
      container = current;
      break;
    }
    current = current.parentElement;
  }
  sizeBox(container, height);
  if (container) {
    var boxes = container.querySelectorAll(".stBidiComponent, .stBidiComponent > div");
    for (var i = 0; i < boxes.length; i++) {
      sizeBox(boxes[i], height);
    }
  }
  sizeBox(node, height);
  var readout = root ? root.querySelector("#policy-readout") : null;
  var used = readout && !readout.hidden ? readout.offsetHeight || 96 : 0;
  var chartHeight = Math.max(160, full - used) + "px";
  var chart = root.querySelector("#chart");
  if (chart) {
    sizeBox(chart, chartHeight);
    if (chart.parentElement && chart.parentElement !== root) {
      sizeBox(chart.parentElement, height);
    }
  }
}

function cssVar(node, name) {
  var current = node && node.host ? node.host : node;
  while (current) {
    var value = getComputedStyle(current).getPropertyValue(name).trim();
    if (value && value !== "unset") {
      return value;
    }
    current = current.parentElement;
  }
  return "";
}

function luminance(color) {
  var match = String(color).match(/rgba?\(([^)]+)\)/);
  if (!match) {
    return null;
  }
  var parts = match[1].split(",").map(function (part) {
    return Number(part.trim());
  });
  if (parts.length < 3 || parts.some(function (part) { return Number.isNaN(part); })) {
    return null;
  }
  return (0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]) / 255;
}

function paletteFor(root) {
  var background = cssVar(root, "--st-background-color") || "#ffffff";
  var text = cssVar(root, "--st-text-color") || "#31333f";
  var line = cssVar(root, "--st-primary-color") || "#ff4b4b";
  var font = cssVar(root, "--st-font") || "Source Sans Pro, sans-serif";
  var level = luminance(background);
  var dark = level == null ? false : level < 0.45;
  var grid = dark ? "rgba(250,250,250,0.12)" : "rgba(49,51,63,0.12)";
  return { background: background, text: text, line: line, font: font, grid: grid, dark: dark };
}

function escapeHtml(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
  });
}

function finiteValue(point) {
  if (typeof point === "number") {
    return Number.isFinite(point) ? point : null;
  }
  if (!point || typeof point !== "object") {
    return null;
  }
  if (point.value == null || point.value === "") {
    return null;
  }
  var number = Number(point.value);
  return Number.isFinite(number) ? number : null;
}

function formatNumber(number, point) {
  if (point && point.unit === "percent") {
    return (number * 100).toFixed(1) + "%";
  }
  if (point && point.unit === "bps") {
    return Number(number).toFixed(0);
  }
  return Number(number).toFixed(2);
}

function formatLevel(params) {
  var number = finiteValue(params && params.data);
  return number == null ? "" : number.toFixed(2);
}

function tooltipBox(html) {
  return '<div style="color:#f4f6f8;background:transparent;font-size:13px;line-height:1.35;padding:0;margin:0">' + html + "</div>";
}

function formatHeatmapTooltip(params) {
  var row = Array.isArray(params) ? params[0] : params;
  var point = row && row.data && typeof row.data === "object" ? row.data : null;
  if (!point) {
    return "";
  }
  var headline = escapeHtml(point.row || "") + " · " + escapeHtml(point.column || "");
  var shown = point.display ? escapeHtml(point.display) : "unavailable";
  var note = point.note
    ? '<div style="color:#f4f6f8;opacity:0.86;font-size:12px;margin-top:3px">' + escapeHtml(point.note) + "</div>"
    : "";
  return tooltipBox(
    '<div style="font-weight:500">' + headline + "</div>" +
    '<div style="color:#f4f6f8;font-size:18px;font-weight:650;margin:2px 0 1px">' + shown + "</div>" +
    note
  );
}

function detailTooltip(params) {
  var rows = Array.isArray(params) ? params : [params];
  var index;
  for (index = 0; index < rows.length; index++) {
    var candidate = rows[index] && rows[index].data;
    if (candidate && Array.isArray(candidate.detail_lines) && candidate.detail_lines.length) {
      var html = "";
      var lineIndex;
      for (lineIndex = 0; lineIndex < candidate.detail_lines.length; lineIndex++) {
        var weight = lineIndex === 0 ? "font-weight:650" : "font-weight:500";
        html += '<div style="' + weight + '">' + escapeHtml(candidate.detail_lines[lineIndex]) + "</div>";
      }
      return tooltipBox(html);
    }
  }
  return "";
}

function formatTooltip(params) {
  var detailed = detailTooltip(params);
  if (detailed) {
    return detailed;
  }
  var row = Array.isArray(params) ? params[0] : params;
  if (!row) {
    return "";
  }
  var point = row.data && typeof row.data === "object" ? row.data : null;
  var tenor = point && point.tenor ? point.tenor : row.name || row.axisValue || "";
  var missingSlot = row.data == null || (point && (point.value == null || point.value === ""));
  var number = missingSlot ? null : finiteValue(point || row.data);
  if (!missingSlot && number == null && typeof row.data === "number" && Number.isFinite(row.data)) {
    number = row.data;
  }
  if (!missingSlot && number == null && typeof row.value === "number" && Number.isFinite(row.value)) {
    number = row.value;
  }
  var ticker = point ? point.ticker || "" : "";
  var headline = ticker ? escapeHtml(tenor) + " · " + escapeHtml(ticker) : escapeHtml(tenor);
  if (number == null) {
    return tooltipBox(headline + "<br/>unavailable on this date");
  }
  var dateLabel = "";
  if (point && point.curve_date_label) {
    dateLabel = point.curve_date_label;
  } else if (point && point.curve_date_long) {
    dateLabel = point.curve_date_long;
  }
  var body =
    '<div style="font-weight:500">' + headline + "</div>" +
    '<div style="color:#f4f6f8;font-size:18px;font-weight:650;margin:2px 0 1px">' +
    formatNumber(number, point) +
    "</div>";
  if (dateLabel) {
    body += '<div style="color:#f4f6f8;opacity:0.82;font-size:12px">' + escapeHtml(dateLabel) + "</div>";
  }
  if (point && Array.isArray(point.notes)) {
    var noteIndex;
    for (noteIndex = 0; noteIndex < point.notes.length; noteIndex++) {
      body += '<div style="color:#f4f6f8;opacity:0.9;font-size:12px;margin-top:3px">' + escapeHtml(point.notes[noteIndex]) + "</div>";
    }
  }
  return tooltipBox(body);
}

function formatMDY(value) {
  var text = String(value == null ? "" : value);
  var iso = "";
  if (text.length >= 10 && text.charAt(4) === "-" && text.charAt(7) === "-") {
    iso = text.slice(0, 10);
  } else if (value && typeof value === "object" && value.year && value.month && value.day) {
    iso =
      String(value.year) +
      "-" +
      String(value.month).padStart(2, "0") +
      "-" +
      String(value.day).padStart(2, "0");
  } else {
    var stamp = new Date(value);
    if (Number.isNaN(stamp.getTime())) {
      return text || "—";
    }
    iso =
      String(stamp.getUTCFullYear()) +
      "-" +
      String(stamp.getUTCMonth() + 1).padStart(2, "0") +
      "-" +
      String(stamp.getUTCDate()).padStart(2, "0");
  }
  var parts = iso.split("-");
  if (parts.length < 3 || parts[0].length !== 4) {
    return text || "—";
  }
  return parts[1] + "/" + parts[2] + "/" + parts[0];
}

function formatPolicyTooltip(params) {
  var rows = Array.isArray(params) ? params : [params];
  var dateLabel = "";
  var lines = [];
  var index;
  for (index = 0; index < rows.length; index++) {
    var row = rows[index];
    if (!row || row.seriesName === "Target span") {
      continue;
    }
    var data = row.data;
    var day = "";
    var number = null;
    if (Array.isArray(data)) {
      day = formatMDY(data[0]);
      if (typeof data[1] === "number" && Number.isFinite(data[1])) {
        number = data[1];
      }
    }
    if (!dateLabel && day) {
      dateLabel = day;
    }
    var shown = number == null ? "—" : number.toFixed(2) + "%";
    lines.push(escapeHtml(row.seriesName || "Value") + "  " + shown);
  }
  if (!dateLabel && !lines.length) {
    return;
  }
  return tooltipBox(escapeHtml(dateLabel) + (lines.length ? "<br/>" + lines.join("<br/>") : ""));
}

function applyTheme(option, palette, compact) {
  option.backgroundColor = "transparent";
  option.textStyle = { color: palette.text, fontFamily: palette.font };
  option.xAxis.axisLabel = option.xAxis.axisLabel || {};
  option.xAxis.axisLabel.color = palette.text;
  option.xAxis.axisLabel.fontSize = compact ? 11 : 12;
  option.xAxis.axisLine = { lineStyle: { color: palette.grid } };
  option.yAxis.axisLabel = { color: palette.text, fontSize: compact ? 11 : 12 };
  if (option.chartKind === "heatmap") {
    option.yAxis.axisLabel.interval = 0;
    option.xAxis.axisLabel.interval = 0;
    if (option.visualMap) {
      option.visualMap.textStyle = { color: palette.text };
    }
  }
  option.yAxis.nameTextStyle = { color: palette.text, fontSize: 11, padding: [0, 0, 0, 4] };
  option.yAxis.splitLine = { show: true, lineStyle: { color: palette.grid } };
  option.yAxis.axisLine = { show: false };
  var seriesList = Array.isArray(option.series) ? option.series : [];
  var paletteColors = [palette.line, "#4c78a8", "#f2c14e", "#59a14f", "#e15759", "#76b7b2"];
  for (var seriesIndex = 0; seriesIndex < seriesList.length; seriesIndex++) {
    var series = seriesList[seriesIndex];
    if (series.policyRole === "span") {
      series.showSymbol = false;
      series.lineStyle = { width: 0, color: "transparent" };
      series.areaStyle = series.areaStyle || { color: "rgba(76, 120, 168, 0.22)" };
      continue;
    }
    if (option.chartKind === "policy_rates") {
      series.showSymbol = false;
    }
    if (series.type === "heatmap") {
      if (series.label && series.label.show) {
        series.label.color = "#f4f6f8";
        series.label.fontSize = compact ? 10 : 11;
        series.label.formatter = function (params) {
          var point = params && params.data;
          if (point && point.display) {
            return point.display;
          }
          return "";
        };
      }
      continue;
    }
    var color = (series.itemStyle && series.itemStyle.color) || paletteColors[seriesIndex % paletteColors.length];
    if (!series.type || series.type === "line") {
      series.symbolSize = compact ? 8 : 10;
      series.lineStyle = series.lineStyle || {};
      if (!series.lineStyle.color) {
        series.lineStyle.color = color;
      }
      if (!series.lineStyle.width) {
        series.lineStyle.width = 2;
      }
      series.itemStyle = series.itemStyle || {};
      if (!series.itemStyle.color) {
        series.itemStyle.color = color;
      }
      if (series.label && series.label.show) {
        series.label.color = palette.text;
        series.label.fontSize = compact ? 11 : 12;
        series.label.formatter = formatLevel;
        series.labelLayout = { hideOverlap: false, moveOverlap: "shiftY" };
      }
    } else {
      series.itemStyle = series.itemStyle || {};
      if (!series.itemStyle.color) {
        series.itemStyle.color = color;
      }
      if (series.label) {
        series.label.color = palette.text;
      }
    }
  }
  if (option.legend && option.legend.show) {
    option.legend.textStyle = { color: palette.text, fontSize: 12 };
  }
  option.tooltip.backgroundColor = "rgba(22, 24, 28, 0.96)";
  option.tooltip.borderColor = "rgba(255, 255, 255, 0.14)";
  option.tooltip.borderWidth = 1;
  option.tooltip.padding = [8, 10];
  option.tooltip.textStyle = { color: "#f4f6f8", fontSize: 13 };
  // appendToBody defaults to true in this ECharts build. That mounts the
  // tooltip on document.body, outside the component shadow root, so the box
  // is positioned against the page, picks up Streamlit's light background,
  // and steals the pointer. Keeping it in the chart and ignoring pointer
  // events stops the flicker, the blank white box, and the jump.
  option.tooltip.appendToBody = false;
  option.tooltip.enterable = false;
  option.tooltip.transitionDuration = 0;
  option.tooltip.className = "mi-echart-tooltip";
  option.tooltip.renderMode = "html";
  option.tooltip.extraCssText =
    "background:rgba(22,24,28,0.96)!important;color:#f4f6f8!important;" +
    "border:1px solid rgba(255,255,255,0.14)!important;border-radius:8px;" +
    "box-shadow:none;padding:8px 10px;pointer-events:none;";
  option.tooltip.formatter =
    option.chartKind === "policy_rates"
      ? formatPolicyTooltip
      : option.chartKind === "heatmap"
        ? formatHeatmapTooltip
        : formatTooltip;
  option.tooltip.axisPointer = {
    type: "line",
    snap: true,
    lineStyle: { color: palette.grid, type: "dashed" },
  };
  if (option.xAxis && option.xAxis.type === "time") {
    option.xAxis.axisLabel = option.xAxis.axisLabel || {};
    option.xAxis.axisLabel.hideOverlap = true;
    option.xAxis.axisLabel.formatter = function (value) {
      return formatMDY(value);
    };
    option.tooltip.axisPointer.label = {
      formatter: function (params) {
        return formatMDY(params && params.value);
      },
    };
  }
  option.tooltip.position = function (point, _params, _dom, _rect, size) {
    var gap = 14;
    var boxWidth = size.contentSize[0];
    var boxHeight = size.contentSize[1];
    var viewWidth = size.viewSize[0];
    var viewHeight = size.viewSize[1];
    var left = point[0] + gap;
    var top = point[1] + gap;
    if (left + boxWidth > viewWidth - 8) {
      left = point[0] - boxWidth - gap;
    }
    if (left < 8) {
      left = 8;
    }
    if (top + boxHeight > viewHeight - 8) {
      top = point[1] - boxHeight - gap;
    }
    if (top < 8) {
      top = 8;
    }
    return [left, top];
  };
  return option;
}

function policyIso(value) {
  var text = String(value == null ? "" : value);
  if (text.length >= 10 && text.charAt(4) === "-" && text.charAt(7) === "-") {
    return text.slice(0, 10);
  }
  var stamp = new Date(value);
  if (Number.isNaN(stamp.getTime())) {
    return "";
  }
  return (
    String(stamp.getUTCFullYear()) +
    "-" +
    String(stamp.getUTCMonth() + 1).padStart(2, "0") +
    "-" +
    String(stamp.getUTCDate()).padStart(2, "0")
  );
}

function policyPercent(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "—";
  }
  return value.toFixed(2) + "%";
}

function policySeriesList(option) {
  var wanted = {
    "Target lower": true,
    "Target upper": true,
    "Effective Fed Funds": true,
    SOFR: true,
  };
  var series = option && Array.isArray(option.series) ? option.series : [];
  var out = [];
  var index;
  for (index = 0; index < series.length; index++) {
    if (wanted[series[index].name]) {
      out.push(series[index]);
    }
  }
  return out;
}

function policyValueOnDay(series, iso) {
  var data = series && Array.isArray(series.data) ? series.data : [];
  var index;
  for (index = 0; index < data.length; index++) {
    var row = data[index];
    if (!Array.isArray(row) || policyIso(row[0]) !== iso) {
      continue;
    }
    if (typeof row[1] === "number" && Number.isFinite(row[1])) {
      return row[1];
    }
    return null;
  }
  return null;
}

function writePolicyReadout(root, headerIso, rows) {
  var box = root.querySelector("#policy-readout");
  var dateEl = root.querySelector("#policy-readout-date");
  var valueEl = root.querySelector("#policy-readout-value");
  if (!box || !dateEl || !valueEl) {
    return;
  }
  box.hidden = false;
  dateEl.textContent = headerIso ? formatMDY(headerIso) : "—";
  var lines = [];
  var index;
  for (index = 0; index < rows.length; index++) {
    var row = rows[index];
    var line = row.label + "  " + policyPercent(row.value);
    if (row.date && headerIso && row.date !== headerIso) {
      line += "  " + formatMDY(row.date);
    }
    lines.push(line);
  }
  valueEl.textContent = lines.length ? lines.join("\n") : "—";
}

function renderPolicyDefault(state) {
  var option = state.option;
  var box = state.root.querySelector("#policy-readout");
  if (!option || option.chartKind !== "policy_rates") {
    if (box) {
      box.hidden = true;
    }
    return;
  }
  var payload = option.policyReadout || {};
  writePolicyReadout(state.root, payload.date || "", Array.isArray(payload.rows) ? payload.rows : []);
}

function renderPolicyHover(state, rawDay) {
  var iso = policyIso(rawDay);
  if (!iso) {
    renderPolicyDefault(state);
    return;
  }
  var series = policySeriesList(state.option);
  var rows = [];
  var index;
  for (index = 0; index < series.length; index++) {
    rows.push({
      label: series[index].name,
      date: iso,
      value: policyValueOnDay(series[index], iso),
    });
  }
  writePolicyReadout(state.root, iso, rows);
}

function bindPolicyReadout(state) {
  if (state.policyBound) {
    return;
  }
  state.policyBound = true;
  state.chart.on("updateAxisPointer", function (event) {
    if (!state.option || state.option.chartKind !== "policy_rates") {
      return;
    }
    var info = event && event.axesInfo && event.axesInfo[0];
    if (!info || info.value == null) {
      renderPolicyDefault(state);
      return;
    }
    renderPolicyHover(state, info.value);
  });
  state.chart.on("globalout", function () {
    renderPolicyDefault(state);
  });
}

function keepPageScroll(chart) {
  var dom = chart.getDom();
  dom.style.touchAction = "pan-y";
  var canvas = dom.querySelector("canvas");
  if (canvas) {
    canvas.style.touchAction = "pan-y";
  }
}

function createState(root) {
  var container = root.querySelector("#chart");
  var echarts = globalThis.echarts;
  if (!container || !echarts || typeof echarts.init !== "function") {
    throw new Error("Tenor chart markup or Apache ECharts bundle is missing.");
  }
  applyFrameHeight(root);
  var chart = echarts.init(container, null, { renderer: "canvas" });
  var state = { root: root, container: container, chart: chart, compact: null, signature: null, sizeW: -1, sizeH: -1 };
  state.observer = new ResizeObserver(function () {
    applyFrameHeight(root);
    var compact = window.matchMedia(MOBILE_QUERY).matches;
    if (state.option && compact !== state.compact) {
      state.compact = compact;
      chart.setOption(applyTheme(state.option, paletteFor(root), compact), true);
    }
    var width = container.clientWidth || 0;
    var height = container.clientHeight || 0;
    if (width === state.sizeW && height === state.sizeH) {
      return;
    }
    state.sizeW = width;
    state.sizeH = height;
    chart.resize();
    keepPageScroll(chart);
  });
  state.observer.observe(container);
  state.themeObserver = new MutationObserver(function () {
    if (state.option) {
      state.chart.setOption(applyTheme(state.option, paletteFor(root), window.matchMedia(MOBILE_QUERY).matches), true);
      keepPageScroll(chart);
    }
  });
  state.themeObserver.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["class", "data-theme", "style"],
  });
  bindPolicyReadout(state);
  keepPageScroll(chart);
  return state;
}

function updateState(state, data) {
  var source = data && data.option ? data.option : null;
  if (!source) {
    return;
  }
  state.root.__tenorMobile = Number(data.mobile_height) || MOBILE_HEIGHT;
  state.root.__tenorDesktop = Number(data.desktop_height) || DESKTOP_HEIGHT;
  var signature = JSON.stringify(source);
  var compact = window.matchMedia(MOBILE_QUERY).matches;
  if (state.signature === signature && state.compact === compact) {
    applyFrameHeight(state.root);
    return;
  }
  state.signature = signature;
  var option = JSON.parse(signature);
  option.dataZoom = [];
  option.toolbox = { show: false };
  if (!option.legend) {
    option.legend = { show: false };
  }
  state.option = applyTheme(option, paletteFor(state.root), window.matchMedia(MOBILE_QUERY).matches);
  state.compact = window.matchMedia(MOBILE_QUERY).matches;
  applyFrameHeight(state.root);
  state.chart.setOption(state.option, true);
  renderPolicyDefault(state);
  applyFrameHeight(state.root);
  state.chart.resize();
  keepPageScroll(state.chart);
}

export default function (component) {
  var root = component.parentElement;
  var data = component.data || {};
  var state = root.__tenorChart;
  if (!state) {
    state = createState(root);
    root.__tenorChart = state;
  }
  updateState(state, data);
  return function cleanup() {
    if (root.__tenorChart !== state) {
      return;
    }
    state.observer.disconnect();
    state.themeObserver.disconnect();
    state.chart.dispose();
    root.__tenorChart = null;
  };
}
