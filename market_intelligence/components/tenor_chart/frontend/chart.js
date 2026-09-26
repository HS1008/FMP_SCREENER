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

function formatTooltip(params) {
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
  return tooltipBox(body);
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
  var seriesList = Array.isArray(option.series) ? option.series : [];
  var paletteColors = [palette.line, "#4c78a8", "#f2c14e", "#59a14f", "#e15759", "#76b7b2"];
  for (var seriesIndex = 0; seriesIndex < seriesList.length; seriesIndex++) {
    var series = seriesList[seriesIndex];
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
  option.tooltip.extraCssText =
    "background:rgba(22,24,28,0.96)!important;color:#f4f6f8!important;" +
    "border:1px solid rgba(255,255,255,0.14)!important;border-radius:8px;" +
    "box-shadow:none;padding:8px 10px;";
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
  state.root.__tenorMobile = Number(data.mobile_height) || MOBILE_HEIGHT;
  state.root.__tenorDesktop = Number(data.desktop_height) || DESKTOP_HEIGHT;
  var option = JSON.parse(JSON.stringify(source));
  option.dataZoom = [];
  option.toolbox = { show: false };
  if (!option.legend) {
    option.legend = { show: false };
  }
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
