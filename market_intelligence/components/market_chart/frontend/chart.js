/* Renders the series Streamlit passes in. No network calls. */
var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
var EXTRA_LINE_COLORS = ["#1e88e5", "#43a047", "#fb8c00", "#8e24aa", "#00acc1", "#e53935"];

function timeToIso(time) {
  if (typeof time === "string") {
    return time.slice(0, 10);
  }
  if (time && typeof time === "object" && time.year && time.month && time.day) {
    return (
      String(time.year) +
      "-" +
      String(time.month).padStart(2, "0") +
      "-" +
      String(time.day).padStart(2, "0")
    );
  }
  if (typeof time === "number" && Number.isFinite(time)) {
    var stamp = new Date(time * 1000);
    return (
      String(stamp.getUTCFullYear()) +
      "-" +
      String(stamp.getUTCMonth() + 1).padStart(2, "0") +
      "-" +
      String(stamp.getUTCDate()).padStart(2, "0")
    );
  }
  return "";
}

function formatDay(iso) {
  var parts = String(iso).slice(0, 10).split("-");
  var year = Number(parts[0]);
  var month = Number(parts[1]);
  var day = Number(parts[2]);
  if (!year || month < 1 || month > 12 || !day) {
    return iso || "—";
  }
  return MONTHS[month - 1] + " " + day + ", " + year;
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
  var grid = dark ? "rgba(250,250,250,0.10)" : "rgba(49,51,63,0.12)";
  return { background: background, text: text, line: line, font: font, grid: grid };
}

function seriesColor(palette, index) {
  if (index === 0) {
    return palette.line;
  }
  return EXTRA_LINE_COLORS[(index - 1) % EXTRA_LINE_COLORS.length];
}

function chartPoint(point) {
  if (!point || point.time == null) {
    return null;
  }
  var time = String(point.time).slice(0, 10);
  if (!time) {
    return null;
  }
  if (typeof point.value !== "number" || !Number.isFinite(point.value)) {
    return { time: time };
  }
  return { time: time, value: point.value };
}

function normalizePoints(points) {
  var byTime = {};
  var order = [];
  var i;
  for (i = 0; i < points.length; i++) {
    var next = chartPoint(points[i]);
    if (!next) {
      continue;
    }
    var prior = byTime[next.time];
    if (!prior) {
      order.push(next.time);
      byTime[next.time] = next;
    } else if (typeof next.value === "number") {
      byTime[next.time] = next;
    }
  }
  order.sort();
  var normalized = [];
  for (i = 0; i < order.length; i++) {
    normalized.push(byTime[order[i]]);
  }
  return normalized;
}

function indexByTime(points) {
  var byTime = {};
  var i;
  for (i = 0; i < points.length; i++) {
    var point = points[i];
    if (point && typeof point.value === "number" && Number.isFinite(point.value)) {
      byTime[point.time] = point.value;
    }
  }
  return byTime;
}

function unionTimes(items) {
  var seen = {};
  var times = [];
  var i;
  var j;
  for (i = 0; i < items.length; i++) {
    var points = items[i].points;
    for (j = 0; j < points.length; j++) {
      var time = points[j].time;
      if (!time || seen[time]) {
        continue;
      }
      seen[time] = true;
      times.push(time);
    }
  }
  times.sort();
  return times;
}

function seriesFromData(data) {
  if (Array.isArray(data.series)) {
    return data.series.map(function (item) {
      return {
        label: (item && item.label) || "Value",
        points: item && Array.isArray(item.points) ? item.points : [],
      };
    });
  }
  return [
    {
      label: data.series_label || "Value",
      points: Array.isArray(data.points) ? data.points : [],
    },
  ];
}

function formatReadoutValue(state, value) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "—";
  }
  var text = value.toFixed(2);
  if (state.valueFormat === "percent") {
    return text + "%";
  }
  return text;
}

function showAtTime(state, iso) {
  var multi = state.seriesList.length > 1;
  state.valueEl.classList.toggle("multi", multi);
  if (!iso) {
    state.activeTime = "";
    state.dateEl.textContent = "—";
    state.valueEl.textContent = "—";
    return;
  }
  state.activeTime = iso;
  state.dateEl.textContent = formatDay(iso);
  if (!state.seriesList.length) {
    state.valueEl.textContent = "—";
    return;
  }
  var lines = [];
  var i;
  for (i = 0; i < state.seriesList.length; i++) {
    var item = state.seriesList[i];
    var has = Object.prototype.hasOwnProperty.call(item.byTime, iso);
    var shown = has ? formatReadoutValue(state, item.byTime[iso]) : "—";
    if (multi) {
      lines.push(item.label + "  " + shown);
    } else {
      lines.push(item.label + ": " + shown);
    }
  }
  state.valueEl.textContent = lines.join("\n");
}

function applyTheme(state) {
  var palette = paletteFor(state.root);
  if (state.root.host) {
    state.root.host.style.background = palette.background;
    state.root.host.style.color = palette.text;
  }
  state.chart.applyOptions({
    layout: {
      background: { type: "solid", color: palette.background },
      textColor: palette.text,
      fontFamily: palette.font,
    },
    grid: {
      vertLines: { color: palette.grid },
      horzLines: { color: palette.grid },
    },
    rightPriceScale: { borderColor: palette.grid },
    timeScale: { borderColor: palette.grid },
    crosshair: {
      mode: state.magnet,
      vertLine: { color: palette.text, width: 1, style: 2, labelBackgroundColor: palette.line },
      horzLine: { color: palette.text, width: 1, style: 2, labelBackgroundColor: palette.line },
    },
  });
  state.seriesList.forEach(function (entry, index) {
    entry.api.applyOptions({ color: seriesColor(palette, index), lineWidth: 2 });
  });
}

function pointAt(state, iso) {
  var i;
  for (i = 0; i < state.seriesList.length; i++) {
    var item = state.seriesList[i];
    if (Object.prototype.hasOwnProperty.call(item.byTime, iso)) {
      return { time: iso, value: item.byTime[iso], api: item.api };
    }
  }
  return {
    time: iso,
    value: null,
    api: state.seriesList.length ? state.seriesList[0].api : null,
  };
}

function nearestPoint(state, x) {
  if (!state.times.length) {
    return null;
  }
  var logical = state.chart.timeScale().coordinateToLogical(x);
  var index;
  if (logical == null || Number.isNaN(logical)) {
    index = x < (state.container.clientWidth || 1) / 2 ? 0 : state.times.length - 1;
  } else {
    index = Math.round(logical);
  }
  if (index < 0) {
    index = 0;
  }
  if (index > state.times.length - 1) {
    index = state.times.length - 1;
  }
  return pointAt(state, state.times[index]);
}

function inspect(state, point) {
  if (!point) {
    return;
  }
  if (point.api && typeof point.value === "number" && Number.isFinite(point.value)) {
    state.chart.setCrosshairPosition(point.value, point.time, point.api);
  }
  showAtTime(state, point.time);
}

function touchDistance(first, second) {
  var dx = first.clientX - second.clientX;
  var dy = first.clientY - second.clientY;
  return Math.sqrt(dx * dx + dy * dy) || 1;
}

function pinchAnchor(container, chart, first, second) {
  var rect = container.getBoundingClientRect();
  var x = (first.clientX + second.clientX) / 2 - rect.left;
  return chart.timeScale().coordinateToLogical(x);
}

function applyPinch(chart, gesture, first, second) {
  if (!gesture.range || !gesture.dist) {
    return;
  }
  var scale = touchDistance(first, second) / gesture.dist;
  if (!Number.isFinite(scale) || scale <= 0) {
    return;
  }
  var width = gesture.range.to - gesture.range.from;
  var nextWidth = width / scale;
  var anchor = gesture.anchor;
  if (anchor == null || Number.isNaN(anchor)) {
    anchor = (gesture.range.from + gesture.range.to) / 2;
  }
  var ratio = width ? (anchor - gesture.range.from) / width : 0.5;
  var from = anchor - ratio * nextWidth;
  chart.timeScale().setVisibleLogicalRange({ from: from, to: from + nextWidth });
}

function panBy(chart, container, range, dx) {
  if (!range) {
    return;
  }
  var bars = range.to - range.from;
  var shift = (dx / (container.clientWidth || 1)) * bars;
  chart.timeScale().setVisibleLogicalRange({ from: range.from - shift, to: range.to - shift });
}

function onTimeAxis(container, clientY) {
  var rect = container.getBoundingClientRect();
  return clientY >= rect.bottom - 36;
}

function signatureOf(items) {
  var parts = [];
  var i;
  var j;
  for (i = 0; i < items.length; i++) {
    parts.push(items[i].label);
    var points = items[i].points;
    for (j = 0; j < points.length; j++) {
      var point = points[j];
      parts.push(point.time + "=" + (typeof point.value === "number" ? point.value : ""));
    }
  }
  return parts.join("|");
}

function daysInMonth(year, month) {
  if (month === 2) {
    if ((year % 4 === 0 && year % 100 !== 0) || year % 400 === 0) {
      return 29;
    }
    return 28;
  }
  if (month === 4 || month === 6 || month === 9 || month === 11) {
    return 30;
  }
  return 31;
}

function shiftIso(iso, deltaMonths) {
  var parts = String(iso).slice(0, 10).split("-");
  var year = Number(parts[0]);
  var month = Number(parts[1]);
  var day = Number(parts[2]);
  if (!year || month < 1 || month > 12 || !day) {
    return String(iso).slice(0, 10);
  }
  var index = year * 12 + (month - 1) + deltaMonths;
  var nextYear = Math.floor(index / 12);
  var nextMonth = index - nextYear * 12 + 1;
  var dim = daysInMonth(nextYear, nextMonth);
  if (day > dim) {
    day = dim;
  }
  return (
    String(nextYear) +
    "-" +
    String(nextMonth).padStart(2, "0") +
    "-" +
    String(day).padStart(2, "0")
  );
}

function rangeStart(last, kind) {
  if (kind === "1M") {
    return shiftIso(last, -1);
  }
  if (kind === "3M") {
    return shiftIso(last, -3);
  }
  if (kind === "6M") {
    return shiftIso(last, -6);
  }
  if (kind === "YTD") {
    return String(last).slice(0, 4) + "-01-01";
  }
  if (kind === "1Y") {
    return shiftIso(last, -12);
  }
  if (kind === "3Y") {
    return shiftIso(last, -36);
  }
  return "";
}

function applyRange(state, kind) {
  var times = state.times;
  if (!times.length) {
    return;
  }
  var start = rangeStart(times[times.length - 1], kind);
  if (!start) {
    return;
  }
  var fromIndex = times.length - 1;
  var i;
  for (i = 0; i < times.length; i++) {
    if (times[i] >= start) {
      fromIndex = i;
      break;
    }
  }
  state.chart.timeScale().setVisibleLogicalRange({
    from: fromIndex - 0.5,
    to: times.length - 1 + 0.5,
  });
}

function applyChartHeight(state, data) {
  var compact = !!(state.media && state.media.matches);
  var next;
  if (compact) {
    next = "320px";
  } else {
    var total = Number(data && data.height) || 420;
    var toolbar = state.root.querySelector("#toolbar");
    var used = toolbar && toolbar.offsetHeight ? toolbar.offsetHeight : 68;
    next = Math.max(220, Math.round(total - used)) + "px";
  }
  if (state.container.style.height !== next) {
    state.container.style.height = next;
  }
}

function lineOptions() {
  return {
    lineWidth: 2,
    priceFormat: { type: "price", precision: 2, minMove: 0.01 },
    lastValueVisible: false,
    priceLineVisible: false,
    crosshairMarkerVisible: true,
    crosshairMarkerRadius: 5,
  };
}

function ensureSeries(state, count) {
  var charts = window.LightweightCharts;
  while (state.seriesList.length > count) {
    var removed = state.seriesList.pop();
    state.chart.removeSeries(removed.api);
  }
  while (state.seriesList.length < count) {
    state.seriesList.push({
      api: state.chart.addSeries(charts.LineSeries, lineOptions()),
      label: "Value",
      byTime: {},
    });
  }
}

function createState(root) {
  var container = root.querySelector("#chart");
  var dateEl = root.querySelector("#readout-date");
  var valueEl = root.querySelector("#readout-value");
  var reset = root.querySelector("#reset");
  var ranges = root.querySelector("#ranges");
  if (!container || !dateEl || !valueEl || !reset || !ranges || !window.LightweightCharts) {
    throw new Error("Market chart markup or Lightweight Charts bundle is missing.");
  }
  var charts = window.LightweightCharts;
  var magnet = charts.CrosshairMode ? charts.CrosshairMode.Magnet : 1;
  var chart = charts.createChart(container, {
    autoSize: true,
    layout: {
      background: { type: "solid", color: "#ffffff" },
      textColor: "#31333f",
    },
    crosshair: { mode: magnet },
    rightPriceScale: { borderVisible: true },
    timeScale: {
      borderVisible: true,
      timeVisible: false,
      secondsVisible: false,
      tickMarkFormatter: function (time, tickType) {
        var iso = timeToIso(time);
        var parts = iso.split("-");
        var month = MONTHS[Number(parts[1]) - 1] || "";
        if (tickType === 0) {
          return parts[0] || "";
        }
        if (tickType === 1) {
          return month;
        }
        return month + " " + Number(parts[2]);
      },
    },
    localization: {
      timeFormatter: function (time) {
        return formatDay(timeToIso(time));
      },
    },
    handleScroll: {
      mouseWheel: false,
      pressedMouseMove: true,
      horzTouchDrag: false,
      vertTouchDrag: false,
    },
    handleScale: {
      mouseWheel: false,
      pinch: true,
      axisPressedMouseMove: { time: true, price: false },
      axisDoubleClickReset: { time: true, price: false },
    },
    kineticScroll: { touch: false, mouse: false },
  });
  var state = {
    root: root,
    container: container,
    dateEl: dateEl,
    valueEl: valueEl,
    reset: reset,
    rangesEl: ranges,
    chart: chart,
    magnet: magnet,
    seriesList: [],
    times: [],
    valueFormat: "number",
    activeTime: "",
    signature: null,
    data: null,
    gesture: null,
  };
  chart.subscribeCrosshairMove(function (param) {
    if (state.holdReadout) {
      return;
    }
    if (state.gesture && state.gesture.mode !== "inspect") {
      return;
    }
    if (!param || param.time == null) {
      return;
    }
    var iso = timeToIso(param.time);
    if (!iso) {
      return;
    }
    showAtTime(state, iso);
  });
  state.observer = new ResizeObserver(function () {
    if (state.data) {
      applyChartHeight(state, state.data);
    }
    if (chart.autoSizeActive && chart.autoSizeActive()) {
      return;
    }
    chart.applyOptions({
      width: container.clientWidth || 320,
      height: container.clientHeight || 320,
    });
  });
  state.observer.observe(container);
  state.media = window.matchMedia ? window.matchMedia("(max-width: 699px)") : null;
  state.onMedia = function () {
    if (state.data) {
      applyChartHeight(state, state.data);
    }
  };
  if (state.media && state.media.addEventListener) {
    state.media.addEventListener("change", state.onMedia);
  }
  state.onTouchStart = function (event) {
    if (event.touches.length >= 2) {
      var startRange = chart.timeScale().getVisibleLogicalRange();
      state.gesture = {
        mode: "pinch",
        dist: touchDistance(event.touches[0], event.touches[1]),
        range: startRange,
        anchor: pinchAnchor(container, chart, event.touches[0], event.touches[1]),
      };
      event.stopPropagation();
      return;
    }
    var touch = event.touches[0];
    state.gesture = {
      x: touch.clientX,
      y: touch.clientY,
      mode: "undecided",
      axis: onTimeAxis(container, touch.clientY),
      range: chart.timeScale().getVisibleLogicalRange(),
      pinned: { time: state.dateEl.textContent, valueText: state.valueEl.textContent },
    };
    event.stopPropagation();
  };
  state.onTouchMove = function (event) {
    if (!state.gesture) {
      return;
    }
    if (event.touches.length >= 2) {
      if (state.gesture.mode !== "pinch") {
        state.gesture = {
          mode: "pinch",
          dist: touchDistance(event.touches[0], event.touches[1]),
          range: chart.timeScale().getVisibleLogicalRange(),
          anchor: pinchAnchor(container, chart, event.touches[0], event.touches[1]),
        };
      }
      if (event.cancelable) {
        event.preventDefault();
      }
      event.stopPropagation();
      applyPinch(chart, state.gesture, event.touches[0], event.touches[1]);
      return;
    }
    if (event.touches.length !== 1 || state.gesture.mode === "pinch") {
      return;
    }
    var touch = event.touches[0];
    var dx = touch.clientX - state.gesture.x;
    var dy = touch.clientY - state.gesture.y;
    if (state.gesture.mode === "undecided") {
      if (Math.abs(dx) < 8 && Math.abs(dy) < 8) {
        return;
      }
      if (Math.abs(dy) >= Math.abs(dx)) {
        state.gesture.mode = "scroll";
      } else if (state.gesture.axis) {
        state.gesture.mode = "pan";
      } else {
        state.gesture.mode = "inspect";
      }
    }
    if (state.gesture.mode === "scroll") {
      event.stopPropagation();
      return;
    }
    if (event.cancelable) {
      event.preventDefault();
    }
    event.stopPropagation();
    if (state.gesture.mode === "pan") {
      panBy(chart, container, state.gesture.range, dx);
      return;
    }
    var rect = container.getBoundingClientRect();
    inspect(state, nearestPoint(state, touch.clientX - rect.left));
  };
  state.onTouchEnd = function () {
    if (state.gesture && state.gesture.mode === "scroll" && state.gesture.pinned) {
      state.holdReadout = true;
      state.dateEl.textContent = state.gesture.pinned.time;
      state.valueEl.textContent = state.gesture.pinned.valueText;
    }
    state.gesture = null;
  };
  state.onPointerMove = function (event) {
    if (event.pointerType === "mouse") {
      state.holdReadout = false;
    }
  };
  container.addEventListener("pointermove", state.onPointerMove, true);
  container.addEventListener("touchstart", state.onTouchStart, { capture: true, passive: true });
  container.addEventListener("touchmove", state.onTouchMove, { capture: true, passive: false });
  container.addEventListener("touchend", state.onTouchEnd, { passive: true });
  container.addEventListener("touchcancel", state.onTouchEnd, { passive: true });
  state.onReset = function () {
    chart.timeScale().fitContent();
    showAtTime(state, state.times.length ? state.times[state.times.length - 1] : null);
  };
  reset.addEventListener("click", state.onReset);
  state.onRangeClick = function (event) {
    var button = event.target && event.target.closest ? event.target.closest("button[data-range]") : null;
    if (!button) {
      return;
    }
    applyRange(state, button.getAttribute("data-range"));
  };
  ranges.addEventListener("click", state.onRangeClick);
  return state;
}

function updateState(state, data) {
  state.data = data || {};
  var nextFormat = String(state.data.value_format || "").toLowerCase() === "percent" ? "percent" : "number";
  var formatChanged = nextFormat !== state.valueFormat;
  state.valueFormat = nextFormat;
  state.rangesEl.hidden = state.data.ranges !== true;
  applyChartHeight(state, state.data);
  var prepared = seriesFromData(state.data).map(function (item) {
    return {
      label: item.label || "Value",
      points: normalizePoints(item.points),
    };
  });
  var signature = signatureOf(prepared);
  var changed = signature !== state.signature;
  ensureSeries(state, prepared.length);
  if (changed) {
    var times = unionTimes(prepared);
    var i;
    for (i = 0; i < prepared.length; i++) {
      var entry = state.seriesList[i];
      entry.label = prepared[i].label;
      entry.byTime = indexByTime(prepared[i].points);
      entry.api.setData(prepared[i].points);
    }
    state.times = times;
    state.signature = signature;
    state.chart.timeScale().fitContent();
    showAtTime(state, times.length ? times[times.length - 1] : null);
  } else {
    var j;
    for (j = 0; j < prepared.length && j < state.seriesList.length; j++) {
      state.seriesList[j].label = prepared[j].label;
    }
    if (formatChanged) {
      showAtTime(state, state.activeTime || null);
    }
  }
  applyTheme(state);
}

export default function (component) {
  var root = component.parentElement;
  var data = component.data || {};
  var state = root.__marketChart;
  if (!state) {
    state = createState(root);
    root.__marketChart = state;
  }
  updateState(state, data);
  return function cleanup() {
    if (root.__marketChart !== state) {
      return;
    }
    state.observer.disconnect();
    if (state.media && state.media.removeEventListener && state.onMedia) {
      state.media.removeEventListener("change", state.onMedia);
    }
    state.container.removeEventListener("pointermove", state.onPointerMove, true);
    state.container.removeEventListener("touchstart", state.onTouchStart, { capture: true });
    state.container.removeEventListener("touchmove", state.onTouchMove, { capture: true });
    state.container.removeEventListener("touchend", state.onTouchEnd);
    state.container.removeEventListener("touchcancel", state.onTouchEnd);
    state.rangesEl.removeEventListener("click", state.onRangeClick);
    state.reset.removeEventListener("click", state.onReset);
    state.chart.remove();
    root.__marketChart = null;
  };
}
