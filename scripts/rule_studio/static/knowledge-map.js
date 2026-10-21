/* Read-only business navigation. The parent owns requests, full content and scope. */
(function () {
  "use strict";
  var SVG_NS = "http://www.w3.org/2000/svg";
  var KIND_NAMES = {global_document:"通用要求",domain_principle:"大类原则",contract_type:"合同类型",
    type_principle:"类型原则",clause_module:"条款模块",module_group:"模块组合",
    knowledge_source:"依据来源",experience_card:"经验卡",trigger:"风险触发器",template:"配套模板",client_rule:"客户规则"};
  var ORBIT = [[18,13],[42,8],[67,12],[88,28],[89,58],[80,85],[55,92],[30,88],[10,67],[9,36]];
  function el(tag, text, className) {
    var node = document.createElement(tag);
    if (text !== undefined && text !== null) { node.textContent = text; }
    if (className) { node.className = className; }
    return node;
  }
  function empty(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }
  function button(text, handler, className) {
    var node = el("button", text, className || "ra-button");
    node.type = "button"; node.addEventListener("click", handler); return node;
  }
  function normalizeContext(value) {
    value = value || {};
    return {domainCode:value.domainCode || null,typeId:value.typeId || null,
      query:value.query || "",layer:value.layer || null,offset:Number(value.offset) || 0};
  }
  function key(value) { return JSON.stringify(normalizeContext(value)); }
  function countLabel(count, noun) { return String(count) + " " + noun; }
  function create(options) {
    options = options || {};
    var host = options.container;
    if (typeof host === "string") { host = document.querySelector(host); }
    if (!host) { throw new Error("RuleAtlas needs a container"); }
    var framework = {domains:[],global_rule_ids:[]}, domainIndex = {}, typeIndex = {};
    var context = normalizeContext(), mode = "overview", rules = null, detail = null;
    var loading = true, error = "", typeQuery = "", typeOffset = 0, typeLimit = 12;
    var destroyed = false, frame = null, observer = null, lines = [];
    var root = el("section", null, "ra-atlas");
    root.setAttribute("aria-label", "规则体系可视导航"); host.appendChild(root);

    var toolbar = el("div", null, "ra-toolbar"), heading = el("div");
    heading.appendChild(el("p", "规则体系 · 全局导航", "ra-eyebrow"));
    heading.appendChild(el("h3", "先看全局，再深入一条规则"));
    var summary = el("p", "正在读取业务目录……", "ra-muted"); summary.id = "ra-summary";
    heading.appendChild(summary); toolbar.appendChild(heading);
    var modes = el("div", null, "ra-modes"); modes.setAttribute("role", "group"); modes.setAttribute("aria-label", "导航视图");
    var overviewButton = button("星云总览", function () { changeMode("overview"); }, "ra-mode"); overviewButton.id = "ra-overview";
    var treeButton = button("树状深入", function () { changeMode("tree"); }, "ra-mode"); treeButton.id = "ra-tree";
    modes.appendChild(overviewButton); modes.appendChild(treeButton); toolbar.appendChild(modes); root.appendChild(toolbar);
    var contextBar = el("div", null, "ra-context-bar");
    var contextLabel = el("p", null, "ra-context"); contextLabel.id = "ra-context"; contextBar.appendChild(contextLabel);
    var browse = button("阅读完整规则列表 ↓", function () { invoke("onBrowse"); }, "ra-text-button"); browse.id = "ra-browse";
    contextBar.appendChild(browse); root.appendChild(contextBar);
    var stage = el("div", null, "ra-stage"); stage.id = "ra-stage"; root.appendChild(stage);
    var locationBox = el("section", null, "ra-locations"); locationBox.id = "ra-locations"; root.appendChild(locationBox);
    var legend = el("div", null, "ra-legend"); legend.setAttribute("aria-label", "图例");
    var directoryLegend = el("span", "目录位置", "ra-legend-directory");
    var sharedLegend = el("span", "同一规则的多处归属", "ra-legend-shared");
    legend.appendChild(directoryLegend); legend.appendChild(sharedLegend);
    legend.appendChild(el("span", "连线不代表依赖关系或本次合同已采用。", "ra-muted")); root.appendChild(legend);
    var status = el("p", null, "ra-status ra-muted"); status.id = "ra-status"; status.setAttribute("role", "status"); root.appendChild(status);

    function invoke(name) {
      if (typeof options[name] === "function") { options[name].apply(null, Array.prototype.slice.call(arguments, 1)); }
    }
    function domains() { return framework.domains || []; }
    function locations() { return detail && Array.isArray(detail.locations) ? detail.locations : []; }
    function relatedDomains() {
      var result = {};
      locations().forEach(function (loc) { if (loc.domain_code) { result[loc.domain_code] = true; } });
      return result;
    }
    function selectedDomain() { return domainIndex[context.domainCode] || null; }
    function currentLabel() {
      var parts = [], domain = selectedDomain(), type = typeIndex[context.typeId];
      if (domain) { parts.push(domain.title); }
      if (type) { parts.push(type.title); }
      if (context.query) { parts.push("搜索：“" + context.query + "”"); }
      if (!parts.length) { parts.push(context.layer === "global" ? "通用审查要求" : "全部公共规则"); }
      return "当前语境：" + parts.join(" → ");
    }
    function showContext() {
      contextLabel.textContent = currentLabel();
      contextLabel.dataset.domainCode = context.domainCode || "";
      contextLabel.dataset.typeId = context.typeId || "";
      contextLabel.dataset.layer = context.layer || "";
      contextLabel.dataset.query = context.query;
      contextLabel.dataset.offset = String(context.offset);
      overviewButton.setAttribute("aria-pressed", mode === "overview" ? "true" : "false");
      treeButton.setAttribute("aria-pressed", mode === "tree" ? "true" : "false");
      root.dataset.mode = mode;
      status.textContent = error ? "规则读取失败：" + error : loading ? "正在读取当前语境的规则。目录仍可浏览。" :
        "图中规则与下方列表为同一页；完整正文、来源与依赖在选中后的详情中保留。";
    }
    function chooseDomain(code) {
      if (!domainIndex[code]) { return; }
      context = normalizeContext({domainCode:code,layer:"domain"});
      rules = null; loading = true; error = ""; typeQuery = ""; typeOffset = 0; mode = "tree";
      render(); invoke("onDomain", code);
    }
    function chooseType(id) {
      var type = typeIndex[id]; if (!type) { return; }
      if (type.domainCode !== context.domainCode) { typeQuery = ""; typeOffset = 0; }
      context = normalizeContext({domainCode:type.domainCode,typeId:id});
      rules = null; loading = true; error = ""; mode = "tree";
      render(); invoke("onType", id);
    }
    function chooseGlobal() {
      context = normalizeContext({layer:"global"}); rules = null; loading = true; error = "";
      mode = "tree"; render(); invoke("onGlobal");
    }
    function chooseLocation(location) {
      if (location.type_id && typeIndex[location.type_id]) {
        if (typeof options.onLocation === "function" && detail) {
          if (typeIndex[location.type_id].domainCode !== context.domainCode) { typeQuery = ""; typeOffset = 0; }
          context = normalizeContext({domainCode:typeIndex[location.type_id].domainCode,typeId:location.type_id});
          rules = null; loading = true; error = ""; mode = "tree";
          render(); invoke("onLocation", location.type_id, detail.node_id);
        } else { chooseType(location.type_id); }
      } else if (location.domain_code && domainIndex[location.domain_code]) { chooseDomain(location.domain_code); }
    }
    function changeMode(next) {
      mode = next; render();
      (mode === "overview" ? overviewButton : treeButton).focus({preventScroll:true});
    }
    function makeSvg(container) {
      var svg = document.createElementNS(SVG_NS, "svg");
      svg.setAttribute("class", "ra-connections"); svg.setAttribute("aria-hidden", "true");
      svg.setAttribute("focusable", "false"); container.appendChild(svg); return svg;
    }
    function addLine(svg, from, to, kind) { lines.push({svg:svg,from:from,to:to,kind:kind || "directory"}); }
    function domainButton(domain, className) {
      var b = button(null, function () { chooseDomain(domain.domain_code); }, className || "ra-domain");
      b.dataset.domainCode = domain.domain_code;
      b.appendChild(el("small", domain.domain_code, "ra-code"));
      b.appendChild(el("strong", domain.title));
      b.appendChild(el("span", countLabel((domain.types || []).length, "个合同类型"), "ra-node-meta"));
      b.setAttribute("aria-current", context.domainCode === domain.domain_code ? "true" : "false");
      return b;
    }
    function renderOverview() {
      var nebula = el("div", null, "ra-nebula"); stage.appendChild(nebula);
      nebula.setAttribute("aria-label", "全部业务大类星云图");
      var svg = makeSvg(nebula), halo = el("div", null, "ra-orbit"); halo.setAttribute("aria-hidden", "true"); nebula.appendChild(halo);
      var hub = el("div", null, "ra-hub");
      var global = button(null, chooseGlobal, "ra-global"); global.id = "ra-global";
      global.appendChild(el("span", "共用起点", "ra-node-meta")); global.appendChild(el("strong", "通用审查要求"));
      global.appendChild(el("span", countLabel((framework.global_rule_ids || []).length, "份通用内容") + " · 点击阅读", "ra-node-meta"));
      hub.appendChild(global); var shared = null;
      if (detail) {
        shared = button(null, function () { invoke("onNode", detail.node_id); }, "ra-shared-node");
        shared.id = "ra-shared-node"; shared.dataset.nodeId = detail.node_id;
        shared.appendChild(el("span", locations().length > 1 ? "同一规则 · " + locations().length + " 处归属" : "当前选中内容", "ra-node-meta"));
        shared.appendChild(el("strong", detail.title || detail.node_id));
        shared.setAttribute("aria-label", "阅读完整内容：" + (detail.title || detail.node_id));
        hub.appendChild(shared);
      }
      nebula.appendChild(hub);
      var related = relatedDomains();
      domains().forEach(function (domain, index) {
        var b = domainButton(domain); b.id = "ra-domain-" + domain.domain_code;
        // The current taxonomy has ten domains. Additional domains remain visible
        // in a compact grid rather than being silently left outside the canvas.
        var position = ORBIT[index] || [50,50];
        b.style.setProperty("--ra-x", position[0] + "%"); b.style.setProperty("--ra-y", position[1] + "%");
        if (related[domain.domain_code]) {
          b.classList.add("ra-related"); b.appendChild(el("span", "所选内容在此有归属", "ra-related-label"));
        }
        nebula.appendChild(b); addLine(svg, global, b);
        if (shared && related[domain.domain_code]) { addLine(svg, shared, b, "shared"); }
      });
      if (!domains().length) { nebula.appendChild(el("p", "业务目录尚未载入。", "ra-empty")); }
      nebula.dataset.forceCompact = domains().length > ORBIT.length ? "true" : "false";
    }
    function column(title, subtitle) {
      var box = el("section", null, "ra-tree-column");
      box.appendChild(el("h4", title)); if (subtitle) { box.appendChild(el("p", subtitle, "ra-muted")); }
      return box;
    }
    function typeNote(type) {
      var coverage = type.coverage || {};
      return coverage.status === "gap" ? "专项内容待建设" : type.runtime_enabled ? "类型已启用 · 各规则状态另核对" : "候选类型 · 不代表已采用";
    }
    function typeButton(type, selected) {
      var b = button(null, function () { chooseType(type.type_id); }, selected ? "ra-selected-type" : "ra-type");
      b.dataset.typeId = type.type_id; b.dataset.domainCode = type.domainCode || context.domainCode || "";
      b.appendChild(el("strong", type.title)); b.appendChild(el("span", typeNote(type), "ra-node-meta"));
      b.setAttribute("aria-current", context.typeId === type.type_id ? "true" : "false"); return b;
    }
    function renderTypeList(box, domain, svg, domainAnchor) {
      var label = el("label", "查找本大类的全部类型", "ra-label"); label.htmlFor = "ra-type-search"; box.appendChild(label);
      var input = el("input"); input.type = "search"; input.id = "ra-type-search"; input.value = typeQuery;
      input.placeholder = "输入类型名称或编号";
      box.appendChild(input);
      var listArea = el("div", null, "ra-types-area"); box.appendChild(listArea);
      var selected = typeIndex[context.typeId] && typeIndex[context.typeId].domainCode === domain.domain_code ? typeIndex[context.typeId] : null;
      var selectedAnchor = null;
      if (selected) {
        var selectedBox = el("div", null, "ra-current-type"); selectedBox.appendChild(el("p", "当前类型", "ra-node-meta"));
        selectedAnchor = typeButton(selected, true); selectedAnchor.id = "ra-current-type"; selectedBox.appendChild(selectedAnchor);
        box.insertBefore(selectedBox, label); addLine(svg, domainAnchor, selectedAnchor);
      }
      function drawTypes() {
        // Only the local directory page changes; network rule results stay under
        // the parent's captured request context.
        empty(listArea);
        lines = lines.filter(function (line) { return line.kind !== "type"; });
        var term = typeQuery.trim().toLocaleLowerCase();
        var filtered = (domain.types || []).filter(function (t) {
          return !term || [t.title,t.type_code,t.type_id].join(" ").toLocaleLowerCase().indexOf(term) >= 0;
        });
        if (typeOffset >= filtered.length) { typeOffset = 0; }
        var count = el("p", "本大类共 " + (domain.types || []).length + " 类" + (term ? "，匹配 " + filtered.length + " 类" : "") +
          "；当前显示 " + (filtered.length ? typeOffset + 1 : 0) + "–" + Math.min(typeOffset + typeLimit, filtered.length) + " 类。", "ra-muted");
        count.id = "ra-type-count"; listArea.appendChild(count);
        var list = el("ul", null, "ra-type-list");
        filtered.slice(typeOffset, typeOffset + typeLimit).forEach(function (type) {
          var item = el("li"), b = typeButton(type, false); item.appendChild(b); list.appendChild(item); addLine(svg, domainAnchor, b, "type");
        });
        listArea.appendChild(list);
        if (!filtered.length) { listArea.appendChild(el("p", "未找到匹配类型。可清除搜索继续查看全部目录。", "ra-empty")); }
        var paging = el("nav", null, "ra-paging"); paging.setAttribute("aria-label", "合同类型分页");
        var previous = button("上一组类型", function () { typeOffset = Math.max(0,typeOffset - typeLimit); drawTypes(); }, "ra-button"); previous.id = "ra-type-previous"; previous.disabled = typeOffset === 0;
        var next = button("下一组类型", function () { typeOffset += typeLimit; drawTypes(); }, "ra-button"); next.id = "ra-type-next"; next.disabled = typeOffset + typeLimit >= filtered.length;
        paging.appendChild(previous); paging.appendChild(next); listArea.appendChild(paging); schedule();
      }
      input.addEventListener("input", function () { typeQuery = input.value; typeOffset = 0; drawTypes(); });
      drawTypes(); return selectedAnchor;
    }
    function renderRules(box, svg, anchor) {
      var count = el("p", null, "ra-rule-count"); count.id = "ra-rule-count"; box.appendChild(count);
      if (loading) { count.textContent = "正在读取当前语境的规则……"; return; }
      if (error) { count.textContent = "读取失败：" + error; return; }
      if (!rules) { count.textContent = "请选择目录位置，或在下方搜索规则。"; return; }
      var nodes = Array.isArray(rules.nodes) ? rules.nodes : [];
      var total = Number.isFinite(Number(rules.total)) ? Number(rules.total) : nodes.length;
      var offset = Number(rules.offset) || 0;
      count.textContent = "第 " + (nodes.length ? offset + 1 : 0) + "–" + (offset + nodes.length) + " 项，共 " + total + " 项";
      var list = el("ul", null, "ra-rule-list"); box.appendChild(list);
      nodes.forEach(function (node) {
        var item = el("li"), b = button(null, function () {
          if (node.node_kind === "contract_type" && typeIndex[node.node_id]) { chooseType(node.node_id); }
          else { invoke("onNode", node.node_id); }
        }, "ra-rule");
        b.dataset.nodeId = node.node_id; b.setAttribute("aria-current", detail && detail.node_id === node.node_id ? "true" : "false");
        b.appendChild(el("strong", node.title || node.node_id));
        var statusText = node.node_kind === "knowledge_source" ? "来源核验见详情" :
          node.node_kind === "global_document" ? "通用审查内容" :
          node.approval_status === "active" && node.activation_status === "active" ? "已启用 · 采用条件见详情" : "维护可读 · 尚未启用";
        b.appendChild(el("span", (KIND_NAMES[node.node_kind] || "配套知识") + " · " + statusText, "ra-node-meta"));
        item.appendChild(b); list.appendChild(item); if (anchor) { addLine(svg, anchor, b); }
      });
      if (!nodes.length) { box.appendChild(el("p", "此位置尚无专项规则。目录保留，通用要求仍可独立阅读。", "ra-empty")); }
      var note = rules.truncated || offset ? "本图同步当前页；在完整列表翻页可继续阅读全部 " + total + " 项。" : "点击规则打开完整正文、来源和关系。";
      box.appendChild(el("p", note, "ra-muted"));
      box.appendChild(button("前往完整列表与分页 ↓", function () { invoke("onBrowse"); }, "ra-text-button"));
    }
    function renderTree() {
      var top = el("div", null, "ra-tree-top");
      var back = button("← 返回星云总览", function () { changeMode("overview"); }, "ra-text-button"); back.id = "ra-return"; top.appendChild(back);
      top.appendChild(el("span", "保留当前目录、规则和搜索语境", "ra-muted")); stage.appendChild(top);
      var tree = el("div", null, "ra-tree-canvas"); stage.appendChild(tree); var svg = makeSvg(tree);
      var domain = selectedDomain();
      var first = column("01 / 业务位置", "目录关系不等于本次合同的适用结论。"), domainAnchor = null;
      first.appendChild(button("通用审查要求", chooseGlobal, "ra-root-link"));
      if (domain) {
        domainAnchor = domainButton(domain, "ra-tree-domain"); domainAnchor.id = "ra-tree-domain-selected"; first.appendChild(domainAnchor);
        first.appendChild(el("p", "可在右侧深入具体类型，也可阅读本大类原则。", "ra-muted"));
      } else {
        first.appendChild(el("p", "选择业务大类进入真实类型目录。", "ra-muted"));
        var domainList = el("div", null, "ra-domain-list");
        domains().forEach(function (item) { var b = button(item.title, function () { chooseDomain(item.domain_code); }, "ra-domain-choice"); b.dataset.domainCode = item.domain_code; domainList.appendChild(b); });
        first.appendChild(domainList);
      }
      tree.appendChild(first);
      var second = column("02 / 合同类型", domain ? "包含已启用、候选与待建设的类型。" : "每个类型保留自己的原则、模块与配套知识。"); tree.appendChild(second);
      var selectedType = domain ? renderTypeList(second, domain, svg, domainAnchor) : null;
      if (!domain) { second.appendChild(el("p", context.layer === "global" ? "通用要求不限定为某一个合同类型。" : "选择大类后显示该大类的全部合同类型。", "ra-empty")); }
      var label = context.typeId ? "03 / 当前类型的规则" : context.layer === "domain" ? "03 / 本大类原则" : context.query ? "03 / 搜索结果" : context.layer === "global" ? "03 / 通用要求" : "03 / 当前规则页";
      var third = column(label, context.layer === "domain" ? "大类原则与类型目录并列，不是任一类型的专属规则。" : "共享规则只有一个身份；完整内容在下方详情。"); tree.appendChild(third);
      renderRules(third, svg, selectedType || (context.layer === "domain" ? domainAnchor : null));
    }
    function renderLocations() {
      empty(locationBox); locationBox.hidden = !detail;
      if (!detail) { return; }
      var title = el("div", null, "ra-location-heading");
      if (mode === "tree") {
        var selected = button("当前定位：" + (detail.title || detail.node_id), function () { invoke("onNode", detail.node_id); }, "ra-selected-rule");
        selected.id = "ra-shared-node"; selected.dataset.nodeId = detail.node_id; title.appendChild(selected);
      } else { title.appendChild(el("strong", "当前定位：" + (detail.title || detail.node_id))); }
      title.appendChild(button("阅读完整内容 ↗", function () { invoke("onNode", detail.node_id); }, "ra-text-button")); locationBox.appendChild(title);
      var all = locations();
      if (!all.length) {
        locationBox.appendChild(el("p", detail.asset_kind === "global_document" || detail.node_kind === "global_document" ?
          "通用审查要求，可供各类合同阅读；不强行归入某个大类。" : "该内容未声明独立目录归属；请在完整详情中核对引用与使用关系。", "ra-muted"));
        return;
      }
      locationBox.appendChild(el("p", "共 " + all.length + " 处已声明归属。跳转位置仍阅读同一份内容；按当前类型重新核对依赖。", "ra-muted"));
      var list = el("div", null, "ra-location-list");
      all.forEach(function (loc) {
        var domain = domainIndex[loc.domain_code], type = typeIndex[loc.type_id];
        var text = loc.domain_title || (domain && domain.title) || loc.domain_code || "未声明大类";
        if (loc.type_id) { text += " → " + (loc.type_title || (type && type.title) || loc.type_id); }
        var b = button(text, function () { chooseLocation(loc); }, "ra-location");
        b.dataset.domainCode = loc.domain_code || ""; if (loc.type_id) { b.dataset.typeId = loc.type_id; }
        b.dataset.nodeId = detail.node_id;
        b.disabled = loc.type_id ? !type : !domain;
        if (b.disabled) { b.title = "归属不在当前目录中，保留记录供核对"; }
        b.setAttribute("aria-current", loc.type_id ? context.typeId === loc.type_id ? "true" : "false" : context.domainCode === loc.domain_code ? "true" : "false");
        list.appendChild(b);
      });
      locationBox.appendChild(list);
    }
    function render() {
      if (destroyed) { return; }
      lines = []; empty(stage); showContext();
      if (mode === "overview") { renderOverview(); } else { renderTree(); }
      renderLocations(); schedule();
    }
    function schedule() {
      if (destroyed || frame !== null) { return; }
      frame = window.requestAnimationFrame(function () { frame = null; drawConnections(); });
    }
    function drawConnections() {
      if (destroyed || !root.getClientRects().length || stage.clientWidth <= 0) { return; }
      root.classList.toggle("ra-narrow", stage.clientWidth < 650);
      root.classList.toggle("ra-stack", stage.clientWidth < 950);
      var nebula = stage.querySelector(".ra-nebula");
      if (nebula) {
        var compact = stage.clientWidth < 650 || nebula.dataset.forceCompact === "true";
        nebula.classList.toggle("ra-compact", compact);
        Array.prototype.forEach.call(nebula.querySelectorAll(".ra-domain"), function (node, index) {
          if (compact) { node.style.removeProperty("left"); node.style.removeProperty("top"); return; }
          var position = ORBIT[index], halfWidth = node.offsetWidth / 2, halfHeight = node.offsetHeight / 2;
          // Labels wrap at tablet widths. Keep the whole measured card inside
          // the orbit, including the extra shared-location label.
          node.style.left = Math.max(halfWidth + 12, Math.min(nebula.clientWidth - halfWidth - 12, nebula.clientWidth * position[0] / 100)) + "px";
          node.style.top = Math.max(halfHeight + 12, Math.min(nebula.clientHeight - halfHeight - 12, nebula.clientHeight * position[1] / 100)) + "px";
        });
      }
      var svgs = stage.querySelectorAll(".ra-connections");
      Array.prototype.forEach.call(svgs, function (svg) {
        empty(svg); var canvas = svg.parentNode;
        svg.setAttribute("viewBox", "0 0 " + canvas.clientWidth + " " + canvas.clientHeight);
        svg.setAttribute("width", String(canvas.clientWidth)); svg.setAttribute("height", String(canvas.clientHeight));
      });
      lines.forEach(function (line) {
        if (!line.from || !line.to || !line.from.isConnected || !line.to.isConnected) { return; }
        var canvas = line.svg.parentNode, base = canvas.getBoundingClientRect();
        var a = line.from.getBoundingClientRect(), b = line.to.getBoundingClientRect();
        if (!a.width || !b.width) { return; }
        // Scrolling a list must not leave connections painted across unrelated rows.
        var scrollList = line.to.closest(".ra-type-list,.ra-rule-list");
        if (scrollList) { var clip = scrollList.getBoundingClientRect(); if (b.top < clip.top || b.bottom > clip.bottom) { return; } }
        var x1 = a.left + a.width / 2 - base.left, y1 = a.top + a.height / 2 - base.top;
        var x2 = b.left + b.width / 2 - base.left, y2 = b.top + b.height / 2 - base.top;
        var path = document.createElementNS(SVG_NS, "path");
        var d = "M " + x1 + " " + y1 + " L " + x2 + " " + y2;
        if (canvas.classList.contains("ra-tree-canvas")) {
          if (root.classList.contains("ra-stack")) { return; }
          x1 = a.right - base.left; x2 = b.left - base.left;
          var middle = x1 + (x2 - x1) / 2;
          d = "M " + x1 + " " + y1 + " C " + middle + " " + y1 + ", " + middle + " " + y2 + ", " + x2 + " " + y2;
        }
        path.setAttribute("d", d); path.setAttribute("class", line.kind === "shared" ? "ra-edge ra-edge-shared" : "ra-edge");
        line.svg.appendChild(path);
      });
    }
    function setFramework(value) {
      framework = value && Array.isArray(value.domains) ? value : {domains:[],global_rule_ids:[]};
      domainIndex = {}; typeIndex = {};
      var typeCount = 0;
      domains().forEach(function (domain) {
        domainIndex[domain.domain_code] = domain;
        (domain.types || []).forEach(function (type) {
          typeIndex[type.type_id] = Object.assign({}, type, {domainCode:domain.domain_code}); typeCount += 1;
        });
      });
      summary.textContent = domains().length + " 个业务大类 · " + typeCount + " 个合同类型 · 共享内容按真实归属连接";
      render();
    }
    function setContext(value) {
      var next = normalizeContext(value);
      if (next.domainCode !== context.domainCode) { typeQuery = ""; typeOffset = 0; }
      context = next; rules = null; loading = true; error = ""; render();
    }
    function setRules(value, capturedContext) {
      if (destroyed || (capturedContext && key(capturedContext) !== key(context))) { return false; }
      rules = value || {nodes:[],total:0,offset:context.offset,truncated:false}; loading = false; error = ""; render(); return true;
    }
    function setError(message, capturedContext) {
      if (destroyed || (capturedContext && key(capturedContext) !== key(context))) { return false; }
      rules = null; loading = false; error = String(message || "未能读取规则，请重试。"); render(); return true;
    }
    function setDetail(value) {
      // Customer overlays have their own explicitly scoped view. They must not
      // become public atlas nodes when the parent opens a private rule detail.
      detail = value && value.asset_kind !== "client_rule" && value.node_kind !== "client_rule" &&
        String(value.node_id || "").indexOf("client:") !== 0 ? value : null;
      render();
    }
    function destroy() {
      destroyed = true;
      if (observer) { observer.disconnect(); }
      if (frame !== null) { window.cancelAnimationFrame(frame); }
      window.removeEventListener("resize", schedule); document.removeEventListener("visibilitychange", schedule);
      root.removeEventListener("scroll", schedule, true);
      if (root.parentNode) { root.parentNode.removeChild(root); }
    }
    if (typeof ResizeObserver !== "undefined") { observer = new ResizeObserver(schedule); observer.observe(root); observer.observe(stage); }
    window.addEventListener("resize", schedule); document.addEventListener("visibilitychange", schedule); root.addEventListener("scroll", schedule, true);
    setFramework(options.framework);
    return {setContext:setContext,setRules:setRules,setDetail:setDetail,setFramework:setFramework,setError:setError,destroy:destroy};
  }
  window.RuleAtlas = {create:create};
}());
