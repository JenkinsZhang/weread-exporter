/*
 * 微信读书阅读页正文提取钩子
 *
 * 正文有两种渲染方式，必须同时采集才不会缺内容：
 *   1. 章节开头若干屏画在 <canvas> 上，逐字调用 fillText；
 *   2. 其余正文是 .passage-content 里逐字绝对定位的 <span data-wr-role="text">，
 *      DOM 顺序被打乱，只有 style 上的 transform 坐标可用，并且随视口虚拟化，
 *      滚出视野就会被移除，所以必须一边滚动一边采集。
 *
 * 两者的坐标都归一到内容容器（.wr_canvasContainer）的坐标系：canvas 通过自身
 * 的变换矩阵与元素位置换算，span 直接用 transform 的位移。归一之后按 (y, x)
 * 排序即可还原阅读顺序，图片、代码块、分隔线按同一套坐标插入到正文之间。
 *
 * 对外接口挂在 window.wrExtractor 上，由 Python 侧驱动：
 *   harvest()   采集当前 DOM 中的正文元素，返回新增条目数
 *   stats()     渲染与采集状态，用于判断是否已经画完
 *   findGaps()  返回正文纵向覆盖上的空洞，用于校验有没有漏采
 *   build()     合并全部条目并生成 markdown
 *   reset()     清空已采集内容
 */
(function () {
  "use strict";
  if (window.wrExtractor && window.wrExtractor.version === 2) {
    return;
  }

  var DEBUG = !!window.__wereadDebug;
  // 页面用这串哑元文本测量字体宽度，不是正文
  var MEASURE_PREFIX = "abcdefghijklmn";
  // 字号阈值沿用旧版钩子的取值，并按实际正文字号等比缩放，
  // 这样用户在阅读器里调整字号后依然成立
  var BASE_PX = 21;
  var HEADING1_PX = 27;
  var HEADING2_PX = 23;
  var SUP_PX = 18;

  function debug() {
    if (DEBUG) {
      console.log.apply(console, arguments);
    }
  }

  function normColor(value) {
    if (!value) {
      return null;
    }
    var s = String(value).trim();
    var m = /^#([0-9a-fA-F]{6})$/.exec(s);
    if (m) {
      return [
        parseInt(m[1].slice(0, 2), 16),
        parseInt(m[1].slice(2, 4), 16),
        parseInt(m[1].slice(4, 6), 16),
      ];
    }
    m = /^#([0-9a-fA-F]{3})$/.exec(s);
    if (m) {
      return [
        parseInt(m[1][0] + m[1][0], 16),
        parseInt(m[1][1] + m[1][1], 16),
        parseInt(m[1][2] + m[1][2], 16),
      ];
    }
    m = /rgba?\(\s*([0-9.]+)[\s,]+([0-9.]+)[\s,]+([0-9.]+)/.exec(s);
    if (m) {
      return [Math.round(+m[1]), Math.round(+m[2]), Math.round(+m[3])];
    }
    return null;
  }

  function colorKey(color) {
    return color ? color.join(",") : "?";
  }

  function parseFontSize(font) {
    var m = /([0-9.]+)px/.exec(font || "");
    return m ? parseFloat(m[1]) : 0;
  }

  function parseTranslate(el) {
    var style = el.getAttribute("style") || "";
    var m = /translate\(\s*(-?[0-9.]+)px[\s,]+(-?[0-9.]+)px/.exec(style);
    if (m) {
      return [parseFloat(m[1]), parseFloat(m[2])];
    }
    return null;
  }

  function modal(values) {
    var count = Object.create(null);
    var best = null;
    var bestN = 0;
    for (var i = 0; i < values.length; i++) {
      var key = String(values[i]);
      count[key] = (count[key] || 0) + 1;
      if (count[key] > bestN) {
        bestN = count[key];
        best = values[i];
      }
    }
    return best;
  }

  function scrollX() {
    return window.scrollX || window.pageXOffset || 0;
  }

  function scrollY() {
    return window.scrollY || window.pageYOffset || 0;
  }

  // ---------------------------------------------------------------- 容器坐标

  function contentRoot() {
    return (
      document.querySelector(".wr_canvasContainer") ||
      document.querySelector(".passage-wrapper") ||
      document.querySelector(".readerContent")
    );
  }

  function origin() {
    var root = contentRoot();
    if (!root) {
      return { top: 0, left: 0, height: 0 };
    }
    var rect = root.getBoundingClientRect();
    return {
      top: rect.top + scrollY(),
      left: rect.left + scrollX(),
      height: Math.max(rect.height, root.scrollHeight || 0),
    };
  }

  // ---------------------------------------------------------------- 采集结果

  var store = {
    text: Object.create(null),
    elem: Object.create(null),
    seenId: Object.create(null),
    textCount: 0,
    elemCount: 0,
    canvasChars: 0,
    spanChars: 0,
    lastDraw: 0,
    height: 0,
  };

  function addText(x, y, text, fontSize, color, source) {
    var key = Math.round(y) + "|" + Math.round(x) + "|" + text;
    if (key in store.text) {
      return false;
    }
    store.text[key] = {
      x: x,
      y: y,
      t: text,
      fs: fontSize,
      c: color,
      src: source,
    };
    store.textCount += 1;
    if (source === "canvas") {
      store.canvasChars += text.length;
    } else {
      store.spanChars += text.length;
    }
    return true;
  }

  function addElem(kind, x, y, height, text) {
    var key = kind + "|" + Math.round(y) + "|" + Math.round(x) + "|" + text;
    if (key in store.elem) {
      var known = store.elem[key];
      if (height > known.h) {
        known.h = height;
      }
      return false;
    }
    store.elem[key] = { kind: kind, x: x, y: y, h: height, t: text };
    store.elemCount += 1;
    return true;
  }

  // ------------------------------------------------------------ canvas 绘制

  var geomCache = new WeakMap();

  function canvasGeom(canvas) {
    var stamp =
      (canvas.getAttribute("style") || "") + "#" + canvas.width + "x" + canvas.height;
    var cached = geomCache.get(canvas);
    if (cached && cached.stamp === stamp) {
      return cached.geom;
    }
    var rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) {
      return null;
    }
    var o = origin();
    var geom = {
      // 后备像素与 CSS 像素的比例
      sx: canvas.width / rect.width,
      sy: canvas.height / rect.height,
      // canvas 元素相对内容容器的偏移
      top: rect.top + scrollY() - o.top,
      left: rect.left + scrollX() - o.left,
    };
    geomCache.set(canvas, { stamp: stamp, geom: geom });
    return geom;
  }

  function recordCanvasText(ctx, text, x, y) {
    var s = text === null || text === undefined ? "" : String(text);
    if (!s || s.slice(0, MEASURE_PREFIX.length).toLowerCase() === MEASURE_PREFIX) {
      return;
    }
    var canvas = ctx.canvas;
    if (!canvas || canvas.isConnected === false) {
      return;
    }
    var geom = canvasGeom(canvas);
    if (!geom) {
      return;
    }
    var bx = x;
    var by = y;
    var scale = 1;
    if (ctx.getTransform) {
      var m = ctx.getTransform();
      bx = m.a * x + m.c * y + m.e;
      by = m.b * x + m.d * y + m.f;
      scale = m.d || 1;
    }
    var px = bx / geom.sx + geom.left;
    var py = by / geom.sy + geom.top;
    // 字号设置在用户坐标系里，换算成 CSS 像素才能和 span 的字号比较
    var fs = (parseFontSize(ctx.font) * scale) / geom.sy;
    // canvas 的 y 是文字基线，DOM 元素的 y 是盒顶，统一成盒顶便于一起排序
    addText(px, py - fs, s, fs, normColor(ctx.fillStyle), "canvas");
    store.lastDraw = Date.now();
  }

  var proto = window.CanvasRenderingContext2D && window.CanvasRenderingContext2D.prototype;
  if (proto && !proto.__wrPatched) {
    var origFillText = proto.fillText;
    proto.fillText = function (text, x, y) {
      try {
        recordCanvasText(this, text, x, y);
      } catch (ex) {
        debug("record canvas text failed", ex);
      }
      return origFillText.apply(this, arguments);
    };
    proto.__wrPatched = true;
  }

  // ------------------------------------------------------------- DOM 采集

  function imageSource(el) {
    var src = el.getAttribute("data-src") || el.getAttribute("src") || "";
    if (!src || src.indexOf("data:") === 0) {
      return "";
    }
    return src;
  }

  function elemPos(el, o) {
    var pos = parseTranslate(el);
    var rect = el.getBoundingClientRect();
    if (!pos) {
      if (!rect.width && !rect.height) {
        return null;
      }
      pos = [rect.left + scrollX() - o.left, rect.top + scrollY() - o.top];
    }
    return { x: pos[0], y: pos[1], h: rect.height || 0 };
  }

  function harvest() {
    var added = 0;
    var o = origin();
    if (o.height > store.height) {
      store.height = o.height;
    }
    var root = document.querySelector(".readerContent") || document.body;
    if (!root) {
      return 0;
    }

    var spans = root.querySelectorAll('[data-wr-role="text"]');
    for (var i = 0; i < spans.length; i++) {
      var span = spans[i];
      var id = span.getAttribute("data-wr-id");
      if (id) {
        if (store.seenId[id]) {
          continue;
        }
        store.seenId[id] = 1;
      }
      var text = span.textContent;
      if (!text) {
        continue;
      }
      var pos = elemPos(span, o);
      if (!pos) {
        continue;
      }
      var style = window.getComputedStyle(span);
      if (addText(pos.x, pos.y, text, parseFloat(style.fontSize) || 0, normColor(style.color), "span")) {
        added += 1;
      }
    }

    var groups = [
      ["img", ".passage-content img"],
      ["pre", ".passage-content pre"],
      ["hr", ".passage-content hr"],
    ];
    for (var g = 0; g < groups.length; g++) {
      var kind = groups[g][0];
      var nodes = root.querySelectorAll(groups[g][1]);
      for (var j = 0; j < nodes.length; j++) {
        var el = nodes[j];
        var p = elemPos(el, o);
        if (!p) {
          continue;
        }
        var value = "";
        if (kind === "img") {
          value = imageSource(el);
          if (!value) {
            continue;
          }
        } else if (kind === "pre") {
          value = el.innerText || "";
          if (!value) {
            continue;
          }
        }
        if (addElem(kind, p.x, p.y, p.h, value)) {
          added += 1;
        }
      }
    }
    return added;
  }

  // ---------------------------------------------------------------- 版面还原

  function textItems() {
    var items = [];
    for (var key in store.text) {
      items.push(store.text[key]);
    }
    items.sort(function (a, b) {
      return a.y - b.y || a.x - b.x;
    });
    return items;
  }

  function elemItems() {
    var items = [];
    for (var key in store.elem) {
      items.push(store.elem[key]);
    }
    items.sort(function (a, b) {
      return a.y - b.y;
    });
    return items;
  }

  function layout() {
    var items = textItems();
    if (!items.length) {
      return null;
    }
    var sizes = [];
    var colors = [];
    for (var i = 0; i < items.length; i++) {
      sizes.push(Math.round(items[i].fs * 10) / 10);
      colors.push(colorKey(items[i].c));
    }
    var bodySize = modal(sizes) || BASE_PX;
    var bodyColor = modal(colors);
    var tolerance = Math.max(4, bodySize * 0.7);

    var lines = [];
    for (var j = 0; j < items.length; j++) {
      var item = items[j];
      var last = lines.length ? lines[lines.length - 1] : null;
      if (last && item.y - last.y <= tolerance) {
        last.items.push(item);
      } else {
        lines.push({ y: item.y, items: [item] });
      }
    }
    for (var k = 0; k < lines.length; k++) {
      lines[k].items.sort(function (a, b) {
        return a.x - b.x;
      });
    }

    var gaps = [];
    for (var n = 1; n < lines.length; n++) {
      var gap = Math.round(lines[n].y - lines[n - 1].y);
      if (gap > 0) {
        gaps.push(gap);
      }
    }
    var lineHeight = modal(gaps) || Math.round(bodySize * 1.95);

    return {
      lines: lines,
      bodySize: bodySize,
      bodyColor: bodyColor,
      lineHeight: lineHeight,
      scale: bodySize / BASE_PX,
    };
  }

  function headingPrefix(fontSize, scale) {
    if (fontSize >= HEADING1_PX * scale) {
      return "## ";
    }
    if (fontSize >= HEADING2_PX * scale) {
      return "### ";
    }
    return "";
  }

  function renderLine(line, info, isHeading) {
    var out = "";
    var highlight = false;
    var sup = false;
    for (var i = 0; i < line.items.length; i++) {
      var item = line.items[i];
      if (!isHeading) {
        var wantSup = item.fs > 0 && item.fs <= SUP_PX * info.scale;
        if (wantSup !== sup) {
          if (highlight) {
            out += "`";
            highlight = false;
          }
          out += wantSup ? "<sup>" : "</sup>";
          sup = wantSup;
        }
        var wantHighlight = colorKey(item.c) !== info.bodyColor;
        if (wantHighlight !== highlight) {
          out += "`";
          highlight = wantHighlight;
        }
      }
      out += item.t;
    }
    if (highlight) {
      out += "`";
    }
    if (sup) {
      out += "</sup>";
    }
    return out.replace(/^\s+|\s+$/g, "");
  }

  function build() {
    var info = layout();
    var blocks = [];
    if (info) {
      for (var i = 0; i < info.lines.length; i++) {
        blocks.push({ y: info.lines[i].y, kind: "line", line: info.lines[i] });
      }
    } else {
      // 整章没有文字（例如只有插图的章节），仍然输出图片等元素
      info = { lines: [], bodySize: BASE_PX, bodyColor: null, lineHeight: 41, scale: 1 };
    }
    var elems = elemItems();
    for (var j = 0; j < elems.length; j++) {
      blocks.push({ y: elems[j].y, kind: elems[j].kind, t: elems[j].t });
    }
    blocks.sort(function (a, b) {
      if (a.y !== b.y) {
        return a.y - b.y;
      }
      return (a.kind === "line" ? 0 : 1) - (b.kind === "line" ? 0 : 1);
    });

    var out = "";
    var prev = null;
    var paraGap = info.lineHeight * 1.35;
    for (var n = 0; n < blocks.length; n++) {
      var block = blocks[n];
      if (block.kind === "line") {
        var size = 0;
        for (var m = 0; m < block.line.items.length; m++) {
          if (block.line.items[m].fs > size) {
            size = block.line.items[m].fs;
          }
        }
        var prefix = headingPrefix(size, info.scale);
        var text = renderLine(block.line, info, !!prefix);
        if (!text) {
          continue;
        }
        var separator;
        if (!out) {
          separator = "";
        } else if (prefix || !prev || prev.kind !== "line" || prev.heading) {
          separator = "\n\n";
        } else {
          separator = block.y - prev.y >= paraGap ? "\n\n" : "\n";
        }
        out += separator + prefix + text;
        prev = { kind: "line", y: block.y, heading: !!prefix };
      } else if (block.kind === "img") {
        out += (out ? "\n\n" : "") + "![](" + block.t + ")\n";
        prev = { kind: "img", y: block.y };
      } else if (block.kind === "hr") {
        out += (out ? "\n\n" : "") + "------\n";
        prev = { kind: "hr", y: block.y };
      } else if (block.kind === "pre") {
        out += (out ? "\n\n" : "") + "```\n" + block.t + "\n```";
        prev = { kind: "pre", y: block.y };
      }
    }
    out = out.replace(/^\s+/, "");
    return out ? out + "\n" : "";
  }

  function lineFontSize(line) {
    var size = 0;
    for (var i = 0; i < line.items.length; i++) {
      if (line.items[i].fs > size) {
        size = line.items[i].fs;
      }
    }
    return size;
  }

  /*
   * 找出正文纵向覆盖上的空洞，用来判断有没有漏采：
   *   1. 相邻两行的距离明显大于行距，且其间没有图片、代码块、分隔线能解释这段留白；
   *   2. 最后一行之后到内容容器底部之间还剩下一大片空白。
   * 标题前后本来就有额外留白，因此阈值放宽。返回容器坐标下的 [y0, y1] 区间，
   * Python 侧据此回滚补采。
   */
  function findGaps() {
    var info = layout();
    if (!info) {
      // 没有任何文字：有图片就算正常（纯插图章节），否则整章都是空洞
      if (store.elemCount > 0) {
        return [];
      }
      return store.height > 0 ? [[0, Math.round(store.height)]] : [];
    }
    var elems = elemItems();
    var limit = info.lineHeight * 2.5;
    var headingLimit = limit * 2.5;

    function coveredBy(y0, y1) {
      var total = 0;
      for (var j = 0; j < elems.length; j++) {
        var e = elems[j];
        if (e.y >= y0 - info.lineHeight && e.y <= y1) {
          total += Math.max(e.h, info.lineHeight);
        }
      }
      return total;
    }

    function isHeading(line) {
      return lineFontSize(line) >= HEADING2_PX * info.scale;
    }

    var gaps = [];
    for (var i = 1; i < info.lines.length; i++) {
      var prev = info.lines[i - 1];
      var next = info.lines[i];
      var span = next.y - prev.y;
      var slack = isHeading(prev) || isHeading(next) ? headingLimit : limit;
      if (span < slack) {
        continue;
      }
      if (span - coveredBy(prev.y, next.y) >= slack) {
        gaps.push([Math.round(prev.y), Math.round(next.y)]);
      }
    }

    var last = info.lines[info.lines.length - 1];
    if (store.height > 0) {
      var tail = store.height - (last.y + info.lineHeight);
      if (tail - coveredBy(last.y, store.height) >= limit) {
        gaps.push([Math.round(last.y), Math.round(store.height)]);
      }
    }
    return gaps;
  }

  /* 轮询用的轻量状态，不做版面还原 */
  function pulse() {
    return {
      textItems: store.textCount,
      elemItems: store.elemCount,
      lastDrawAgo: store.lastDraw ? Date.now() - store.lastDraw : -1,
    };
  }

  function stats() {
    var info = layout();
    return {
      textItems: store.textCount,
      elemItems: store.elemCount,
      canvasChars: store.canvasChars,
      spanChars: store.spanChars,
      lines: info ? info.lines.length : 0,
      bodySize: info ? info.bodySize : 0,
      lineHeight: info ? info.lineHeight : 0,
      lastDrawAgo: store.lastDraw ? Date.now() - store.lastDraw : -1,
      contentHeight: Math.round(store.height),
      firstY: info ? Math.round(info.lines[0].y) : -1,
      lastY: info ? Math.round(info.lines[info.lines.length - 1].y) : -1,
    };
  }

  function reset() {
    store.text = Object.create(null);
    store.elem = Object.create(null);
    store.seenId = Object.create(null);
    store.textCount = 0;
    store.elemCount = 0;
    store.canvasChars = 0;
    store.spanChars = 0;
    store.lastDraw = 0;
    store.height = 0;
  }

  window.wrExtractor = {
    version: 2,
    harvest: harvest,
    build: build,
    stats: stats,
    pulse: pulse,
    findGaps: findGaps,
    reset: reset,
    origin: origin,
  };
})();
