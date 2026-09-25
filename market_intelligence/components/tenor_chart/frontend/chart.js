/* Renders the tenor curve Streamlit passes in. No network calls. */
var MOBILE_QUERY = "(max-width: 699px)";
var MOBILE_HEIGHT = 320;
var DESKTOP_HEIGHT = 390;

function frameHeight() {
  return window.matchMedia(MOBILE_QUERY).matches ? MOBILE_HEIGHT : DESKTOP_HEIGHT;
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
  var height = frameHeight() + "px";
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
  var chart = root.querySelector("#chart");
  if (chart) {
    sizeBox(chart, height);
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
  if (!point || typeof point !== "object") {
    return null;
  }
  var number = Number(point.value);
  return Number.isFinite(number) ? number : null;
}

function formatLevel(params) {
  var number = finiteValue(params && params.data);
  return number == null ? "" : number.toFixed(2);
}

function formatTooltip(params) {
  var row = Array.isArray(params) ? params[0] : params;
  if (!row) {
    return "";
  }
  var point = row.data && typeof row.data === "object" ? row.data : null;
  var tenor = point && point.tenor ? point.tenor : row.axisValue || "";
  var number = finiteValue(point);
  if (number == null) {
    return escapeHtml(tenor) + "<br/>No observation";
  }
  var lines = [
    escapeHtml(tenor),
    escapeHtml(point.name || point.ticker || ""),
    number.toFixed(2),
  ];
  if (point.curve_date_label) {
    lines.push(escapeHtml(point.curve_date_label));
  }
  return lines.join("<br/>");
}

function applyTheme(option, palette, compact) {
  option.backgroundColor = "transparent";
  option.textStyle = { color: palette.text, fontFamily: palette.font };
  option.xAxis.axisLabel = option.xAxis.axisLabel || {};
  option.xAxis.axisLabel.color = palette.text;
  option.xAxis.axisLabel.fontSize = compact ? 11 : 12;
  option.xAxis.axisLine = { lineStyle: { color: palette.grid } };
  option.yAxis.axisLabel = { color: palette.text, fontSize: compact ? 11 : 12 };
  option.yAxis.nameTextStyle = { color: palette.text, fontSize: 11, padding: [0, 0, 0, 4] };
  option.yAxis.splitLine = { show: true, lineStyle: { color: palette.grid } };
  option.yAxis.axisLine = { show: false };
  var series = option.series[0];
  series.symbolSize = compact ? 8 : 10;
  series.lineStyle = { width: 2, color: palette.line };
  series.itemStyle = { color: palette.line };
  series.label = series.label || {};
  series.label.show = true;
  series.label.position = "top";
  series.label.distance = 6;
  series.label.color = palette.text;
  series.label.fontSize = compact ? 11 : 12;
  series.label.formatter = formatLevel;
  series.labelLayout = { hideOverlap: false, moveOverlap: "shiftY" };
  option.tooltip.backgroundColor = palette.dark ? "rgba(28,28,32,0.96)" : "rgba(255,255,255,0.96)";
  option.tooltip.borderColor = palette.grid;
  option.tooltip.textStyle = { color: palette.text, fontSize: 13 };
  option.tooltip.extraCssText = "box-shadow:none;border-radius:8px;";
  option.tooltip.formatter = formatTooltip;
  option.tooltip.axisPointer = {
    type: "line",
    snap: true,
    lineStyle: { color: palette.grid, type: "dashed" },
  };
  option.tooltip.position = function (point, _params, _dom, _rect, size) {
    var boxWidth = size.contentSize[0];
    var viewWidth = size.viewSize[0];
    var left = point[0] - boxWidth / 2;
    if (left < 8) {
      left = 8;
    }
    if (left + boxWidth > viewWidth - 8) {
      left = Math.max(8, viewWidth - boxWidth - 8);
    }
    return [left, size.viewSize[1] - size.contentSize[1] - 28];
  };
  return option;
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
  var state = { root: root, container: container, chart: chart, compact: null };
  state.observer = new ResizeObserver(function () {
    applyFrameHeight(root);
    var compact = window.matchMedia(MOBILE_QUERY).matches;
    if (state.option && compact !== state.compact) {
      state.compact = compact;
      chart.setOption(applyTheme(state.option, paletteFor(root), compact), true);
    }
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
  keepPageScroll(chart);
  return state;
}

function updateState(state, data) {
  var source = data && data.option ? data.option : null;
  if (!source) {
    return;
  }
  var option = JSON.parse(JSON.stringify(source));
  option.dataZoom = [];
  option.toolbox = { show: false };
  option.legend = { show: false };
  state.option = applyTheme(option, paletteFor(state.root), window.matchMedia(MOBILE_QUERY).matches);
  state.compact = window.matchMedia(MOBILE_QUERY).matches;
  applyFrameHeight(state.root);
  state.chart.setOption(state.option, true);
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
