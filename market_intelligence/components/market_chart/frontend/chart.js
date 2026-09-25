/* Renders the points Streamlit passes in. No network calls. */
var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

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

function showPoint(state, point) {
  if (!point) {
    state.dateEl.textContent = "—";
    state.valueEl.textContent = "—";
    return;
  }
  state.dateEl.textContent = formatDay(point.time);
  state.valueEl.textContent = state.seriesLabel + ": " + Number(point.value).toFixed(2);
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
  state.series.applyOptions({ color: palette.line, lineWidth: 2 });
}

function nearestPoint(state, x) {
  if (!state.points.length) {
    return null;
  }
  var logical = state.chart.timeScale().coordinateToLogical(x);
  var index;
  if (logical == null || Number.isNaN(logical)) {
    index = x < (state.container.clientWidth || 1) / 2 ? 0 : state.points.length - 1;
  } else {
    index = Math.round(logical);
  }
  if (index < 0) {
    index = 0;
  }
  if (index > state.points.length - 1) {
    index = state.points.length - 1;
  }
  return state.points[index];
}

function inspect(state, point) {
  if (!point) {
    return;
  }
  state.chart.setCrosshairPosition(point.value, point.time, state.series);
  showPoint(state, point);
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

function signatureOf(points) {
  if (!points.length) {
    return "empty";
  }
  var first = points[0];
  var last = points[points.length - 1];
  return points.length + "|" + first.time + "|" + last.time + "|" + last.value;
}

function createState(root) {
  var container = root.querySelector("#chart");
  var dateEl = root.querySelector("#readout-date");
  var valueEl = root.querySelector("#readout-value");
  var reset = root.querySelector("#reset");
  if (!container || !dateEl || !valueEl || !reset || !window.LightweightCharts) {
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
  var series = chart.addSeries(charts.LineSeries, {
    lineWidth: 2,
    priceFormat: { type: "price", precision: 2, minMove: 0.01 },
    lastValueVisible: false,
    priceLineVisible: false,
    crosshairMarkerVisible: true,
    crosshairMarkerRadius: 5,
  });
  var state = {
    root: root,
    container: container,
    dateEl: dateEl,
    valueEl: valueEl,
    chart: chart,
    series: series,
    magnet: magnet,
    points: [],
    seriesLabel: "Value",
    signature: "",
    gesture: null,
  };
  chart.subscribeCrosshairMove(function (param) {
    if (state.holdReadout) {
      return;
    }
    if (state.gesture && state.gesture.mode !== "inspect") {
      return;
    }
    if (!param || param.time == null || !param.seriesData) {
      return;
    }
    var bar = param.seriesData.get(series);
    if (!bar || bar.value == null) {
      return;
    }
    showPoint(state, { time: bar.time || param.time, value: bar.value });
  });
  state.observer = new ResizeObserver(function () {
    if (chart.autoSizeActive && chart.autoSizeActive()) {
      return;
    }
    chart.applyOptions({
      width: container.clientWidth || 320,
      height: container.clientHeight || 320,
    });
  });
  state.observer.observe(container);
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
  container.addEventListener("pointermove", function (event) {
    if (event.pointerType === "mouse") {
      state.holdReadout = false;
    }
  }, true);
  container.addEventListener("touchstart", state.onTouchStart, { capture: true, passive: true });
  container.addEventListener("touchmove", state.onTouchMove, { capture: true, passive: false });
  container.addEventListener("touchend", state.onTouchEnd, { passive: true });
  container.addEventListener("touchcancel", state.onTouchEnd, { passive: true });
  state.onReset = function () {
    chart.timeScale().fitContent();
    showPoint(state, state.points.length ? state.points[state.points.length - 1] : null);
  };
  reset.addEventListener("click", state.onReset);
  return state;
}

function updateState(state, data) {
  state.seriesLabel = data.series_label || "Value";
  var height = Number(data.height) || 440;
  state.container.style.height = Math.max(220, height - 68) + "px";
  var nextPoints = Array.isArray(data.points) ? data.points : [];
  applyTheme(state);
  var signature = signatureOf(nextPoints);
  if (signature !== state.signature) {
    state.points = nextPoints;
    state.series.setData(nextPoints);
    state.chart.timeScale().fitContent();
    showPoint(state, nextPoints.length ? nextPoints[nextPoints.length - 1] : null);
    state.signature = signature;
  }
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
    state.container.removeEventListener("touchstart", state.onTouchStart, { capture: true });
    state.container.removeEventListener("touchmove", state.onTouchMove, { capture: true });
    state.container.removeEventListener("touchend", state.onTouchEnd);
    state.container.removeEventListener("touchcancel", state.onTouchEnd);
    state.chart.remove();
    root.__marketChart = null;
  };
}
