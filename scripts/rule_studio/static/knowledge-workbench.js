/* Complete maintenance views. Text is rendered with DOM text nodes only. */
(function () {
  "use strict";
  var bridge, root, framework, atlas;
  var frameworkEpoch = 0, listScope = null;
  var state = { typeId: null, domainCode: null, query: "", offset: 0, limit: 20 };
  var typeIndex = {}, domainIndex = {}, titleIndex = {}, fieldLabels = {};
  var roleLabels = {}, sceneLabels = {}, listEpoch = 0, detailEpoch = 0, currentDetail = null;
  var clientEpoch = 0, clientId = null, clientData = null, clientNodes = {}, clientSchema = null;
  var clientOffsets = {active:0,draft:0}, clientPageSize = 10;
  var maintenanceEpoch = 0, maintenanceRecords = [], maintenanceLoading = false, maintenanceError = "";
  var maintenanceOffset = 0, maintenancePageSize = 20;
  var kindNames = { global_document: "通用审查要求", domain_principle: "大类原则", contract_type: "合同类型",
    type_principle: "类型原则", clause_module: "条款模块", module_group: "模块组合", knowledge_source: "依据来源",
    experience_card: "经验卡", trigger: "风险触发器", template: "配套模板", client_rule: "客户规则" };
  var statusNames = { active: "已启用", candidate: "待审核", draft: "草稿", lawyer_approved: "已记录审核",
    regression_passed: "已记录测试通过", deprecated: "已停用", inactive: "未启用" };
  var extraLabels = { type_name: "合同类型名称", domain_name: "业务大类", build_scope: "建设范围",
    activation_basis: "启用依据", source_id: "来源编号", source_kind: "来源种类", title: "名称", author: "作者或发布机构",
    published_at: "发布日期", verified_at: "核验时间", content_verification: "内容核验说明", used_for: "支持的判断事项",
    jurisdiction: "适用地区", access_scope: "访问范围", knowledge_tags: "知识标签", url: "来源链接",
    domain_code: "业务大类", type_id: "合同类型", type_ids: "适用合同类型", approval_status: "审核状态",
    activation_status: "启用状态", strong_protection: "强保护口径", balanced: "平衡口径", concession: "可接受退让",
    source_refs: "依据来源", conditions: "适用条件", primary_type_id: "主要合同类型", scope: "适用范围" };

  function el(tag, text, cls) { return bridge.el(tag, text, cls); }
  function clear(node) { bridge.clear(node); }
  function byId(id) { return document.getElementById(id); }
  function button(text, callback, cls) {
    var b = el("button", text, cls || "kw-link"); b.type = "button"; b.addEventListener("click", callback); return b;
  }
  function label(key) { return fieldLabels[key] || extraLabels[key] || key; }
  function request(path) {
    return bridge.call("GET", path).then(function (response) {
      if (!response.body.success) { throw new Error(bridge.errorText(response.body, "读取失败：")); }
      return response.body.result;
    });
  }
  function message(target, text, warning) {
    clear(target); target.appendChild(el("p", text, warning ? "kw-message kw-warning" : "kw-message"));
  }
  function sourceStatus(node) {
    var body=node.body||node, approval=body.approval_status||node.approval_status;
    return (approval==="active"||approval==="lawyer_approved"?"已记录审核":statusNames[approval]||"审核记录待核对")+
      (body.verified_at?" · 核验时间："+body.verified_at:"");
  }
  function maintenanceLocations(body) {
    body=body||{};
    var types=Array.isArray(body.type_ids)?body.type_ids.slice():[];
    if(typeof body.type_id==="string"&&body.type_id){types.push(body.type_id);}
    if(types.length){return Array.from(new Set(types)).filter(function(id){return Object.prototype.hasOwnProperty.call(typeIndex,id)&&(!body.domain_code||body.domain_code===typeIndex[id].domainCode);}).map(function(id){return {typeId:id,domainCode:typeIndex[id].domainCode};});}
    return Object.prototype.hasOwnProperty.call(domainIndex,body.domain_code)?[{typeId:null,domainCode:body.domain_code}]:[];
  }
  function maintenanceMatches(entry) {
    var locations=maintenanceLocations(entry.locationBody);
    return locations.some(function(location){return state.typeId?location.typeId===state.typeId:state.domainCode?location.domainCode===state.domainCode:false;})
      &&(!state.query||JSON.stringify(entry.record).toLowerCase().indexOf(state.query.toLowerCase())>=0);
  }
  function renderMaintenanceDetail(entry) {
    ++detailEpoch;currentDetail=null;
    if(atlas){atlas.setDetail(null);}
    var box=byId("kw-detail");clear(box);delete box.dataset.nodeId;box.dataset.maintenanceId=entry.id;
    box.appendChild(el("p",entry.kind==="proposal"?"公共修改建议":"未完成的公共填写进度","kw-kicker"));
    box.appendChild(el("h3",entry.title));
    box.appendChild(el("p","尚未启用 · 仅供维护核对，不会参与正式公共规则解析或合同审查。","kw-status kw-warning"));
    var locations=section("已填写的业务位置");maintenanceLocations(entry.locationBody).forEach(function(location){
      var p=el("p");p.appendChild(nodeLink(location.domainCode));if(location.typeId){p.appendChild(el("span"," → "));p.appendChild(nodeLink(location.typeId));}locations.appendChild(p);
    });if(entry.record.operation==="deprecate"){locations.appendChild(el("p","位置来自明确指定的停用对象；这条建议尚未停用正式规则。","kw-muted"));}box.appendChild(locations);
    if(entry.kind==="proposal"){
      box.appendChild(el("p","修改理由："+(entry.record.rationale||"尚未填写")));
      box.appendChild(el("p","当前记录："+({accepted:"已同意导出，未启用",rejected:"已退回",deprecated:"已结束"}[entry.record.status]||statusNames[entry.record.status]||entry.record.status)));
    }
    var body=section("完整候选内容");body.appendChild(entry.body?valueView(entry.body,"body"):el("p","这是一项停用建议，没有替换正文；请核对指定对象和修改理由。"));box.appendChild(body);
    var open=button(entry.kind==="proposal"?"打开既有建议，继续核对或编辑":"继续填写这份进度",function(){
      var authoring=window.AuthoringWorkbench;
      if(!authoring||!authoring.ready){message(box,"编辑器尚未就绪，请稍后重试。",true);return;}
      if(entry.kind==="proposal"){authoring.openPublicProposal(entry.id);}else{authoring.openPublicProgress(entry.id);}
    },"secondary");if(entry.kind==="progress"&&bridge.isReadOnly()){open.disabled=true;}box.appendChild(open);
    var raw=el("details",null,"kw-raw");raw.appendChild(el("summary","完整保存记录与技术编号"));raw.appendChild(el("pre",JSON.stringify(entry.record,null,2)));box.appendChild(raw);revealSelection(box);
  }
  function renderMaintenance() {
    var box=byId("kw-public-maintenance");if(!box){return;}clear(box);
    box.appendChild(el("h3","尚未启用的公共修改建议与填写进度"));
    box.appendChild(el("p","按已保存正文中的业务大类和合同类型定位；这些记录与正式规则分开，不会自动启用。","kw-warning"));
    if(maintenanceLoading){box.appendChild(el("p","正在读取已保存的公共维护内容……","kw-muted"));return;}
    if(maintenanceError){box.appendChild(el("p",maintenanceError,"kw-warning"));return;}
    if(!state.typeId&&!state.domainCode){box.appendChild(el("p","请选择业务大类或合同类型，查看该位置已经保存的建议与填写进度。","kw-muted"));return;}
    var entries=maintenanceRecords.filter(maintenanceMatches),ul=el("ul",null,"kw-rule-items");
    var offset=Math.min(maintenanceOffset,Math.max(0,Math.floor((entries.length-1)/maintenancePageSize)*maintenancePageSize));maintenanceOffset=offset;
    entries.slice(offset,offset+maintenancePageSize).forEach(function(entry){var li=el("li");li.dataset.maintenanceKind=entry.kind;li.dataset.maintenanceId=entry.id;li.appendChild(button(entry.title,function(){renderMaintenanceDetail(entry);},"kw-rule-button"));li.appendChild(el("p","尚未启用 · "+(entry.kind==="proposal"?"公共修改建议":"未完成的填写进度"),"kw-meta"));ul.appendChild(li);});box.appendChild(ul);
    var pages=el("nav",null,"kw-pagination");pages.setAttribute("aria-label","公共建议与填写进度分页");pages.appendChild(el("p","第 "+(entries.length?offset+1:0)+"–"+Math.min(offset+maintenancePageSize,entries.length)+" 项，共 "+entries.length+" 项"));
    var previous=button("上一页",function(){maintenanceOffset=offset-maintenancePageSize;renderMaintenance();},"secondary");previous.disabled=offset===0;pages.appendChild(previous);
    var next=button("下一页",function(){maintenanceOffset=offset+maintenancePageSize;renderMaintenance();},"secondary");next.disabled=offset+maintenancePageSize>=entries.length;pages.appendChild(next);box.appendChild(pages);
    if(!entries.length){box.appendChild(el("p","该位置尚无已保存的公共建议或填写进度。","kw-muted"));}
    var unlocated=maintenanceRecords.filter(function(entry){return !maintenanceLocations(entry.locationBody).length;}).length;
    if(unlocated){box.appendChild(el("p","另有 "+unlocated+" 份公共维护内容尚无可定位的业务位置，请在公共维护页补充；未替其猜测归属。","kw-muted"));}
    box.appendChild(button("打开公共维护页",function(){bridge.showView("proposals");},"secondary"));
  }
  function loadMaintenance() {
    var epoch=++maintenanceEpoch;maintenanceOffset=0;maintenanceLoading=true;maintenanceError="";renderMaintenance();
    return Promise.all([request("/api/v1/public-proposals"),request("/api/v1/authoring/drafts")]).then(function(lists){
      var jobs=lists[0].proposals.map(function(summary){return function(){return request("/api/v1/public-proposals/"+encodeURIComponent(summary.proposal_id)).then(async function(p){
        var locationBody=p.proposed_rule;
        if(!locationBody&&p.operation==="deprecate"&&p.target_rule_id){locationBody=(await request("/api/v1/knowledge/nodes/"+encodeURIComponent(p.target_rule_id)+"?projection_mode=maintenance")).body;}
        return {kind:"proposal",id:p.proposal_id,title:p.title,body:p.proposed_rule,locationBody:locationBody,record:p};
      });};});
      lists[1].drafts.filter(function(summary){return summary.kind!=="client_rule";}).forEach(function(summary){jobs.push(function(){return request("/api/v1/authoring/drafts/"+encodeURIComponent(summary.draft_id)).then(function(p){
        if(p.client_profile_id||p.kind==="client_rule"){throw Error("公共填写进度返回了不匹配的客户归属，未展示。");}
        return {kind:"progress",id:p.draft_id,title:p.title,body:p.draft,locationBody:p.draft,record:p};
      });});});
      var cursor=0,records=new Array(jobs.length),failure=null;
      async function worker(){while(cursor<jobs.length&&epoch===maintenanceEpoch&&!failure){var index=cursor++;try{records[index]=await jobs[index]();}catch(error){failure=error;}}}
      return Promise.all(Array.from({length:Math.min(4,jobs.length)},worker)).then(function(){if(failure){throw failure;}return records;});
    }).then(function(records){if(epoch!==maintenanceEpoch){return;}maintenanceRecords=records;maintenanceLoading=false;renderMaintenance();})
      .catch(function(error){if(epoch!==maintenanceEpoch){return;}maintenanceRecords=[];maintenanceLoading=false;maintenanceError=error.message+" 公共维护列表尚未完整载入。";renderMaintenance();});
  }
  function loadClientChoices() {
    return request("/api/v1/clients").then(function(result){
      var select=byId("kw-client-select");clear(select);var option=el("option","仅查看公共规则");option.value="";select.appendChild(option);
      result.clients.forEach(function(profile){var o=el("option",profile.display_name+(profile.status!=="active"?"（档案已停用）":""));o.value=profile.client_profile_id;select.appendChild(o);});
      select.value=clientId||"";
      if(clientId&&select.value!==clientId){loadClientOverlay("");}
    }).catch(function(error){message(byId("kw-client-status"),error.message,true);});
  }
  function scopeMatches(scope,typeId,domainCode) {
    scope=scope||{};if(scope.applies_to_all){return true;}
    var types=scope.type_ids||[],domains=scope.domain_codes||[];
    if(typeId){return (!types.length||types.indexOf(typeId)>=0)&&(!domains.length||domains.indexOf(domainCode)>=0);}
    if(domainCode){return (!domains.length||domains.indexOf(domainCode)>=0)&&(!types.length||types.some(function(id){return typeIndex[id]&&typeIndex[id].domainCode===domainCode;}));}
    return true;
  }
  function clientSections(body) {
    var metadata={},groups={};
    ((clientSchema||{}).fields||[]).forEach(function(f){if(f.path.indexOf(".")<0&&f.path.indexOf("[")<0){metadata[f.path]=f;}});
    ((clientSchema||{}).groups||[]).forEach(function(g){groups[g.id]={id:g.id,title:g.title,fields:[]};});
    groups.original={id:"original",title:"其他完整记录",fields:[]};
    Object.keys(body).forEach(function(key){var m=metadata[key],group=m&&groups[m.group]||groups.original;group.fields.push({key:key,label:m?m.label:label(key),value:body[key]});});
    return Object.values(groups).filter(function(g){return g.fields.length;});
  }
  function registerClientRule(body,variant) {
    var id="client:"+clientId+":"+variant+":"+body.client_rule_id,scope=body.scope||{},locations=[];
    (scope.type_ids||[]).forEach(function(t){var type=typeIndex[t];if(type){locations.push({domain_code:type.domainCode,domain_title:domainIndex[type.domainCode].title,type_id:t,type_title:type.title});}});
    if(!locations.length){(scope.domain_codes||[]).forEach(function(d){if(domainIndex[d]){locations.push({domain_code:d,domain_title:domainIndex[d].title});}});}
    var node={node_id:id,node_kind:"client_rule",asset_kind:"client_rule",authoring_kind:"client_rule",title:body.title,
      body:body,version:body.version,client_variant:variant,client_profile_id:clientId,locations:locations,
      approval_status:variant==="active"?"active":body.approval_status,activation_status:variant==="active"?"active":"inactive",
      content_sections:clientSections(body),sources:[],relations:{outgoing:[],incoming:[]}};
    clientNodes[id]=node;titleIndex[id]=body.title+"（"+(variant==="active"?"启用快照":"维护草稿")+"）";
  }
  function loadClientOverlay(id) {
    var epoch=++clientEpoch;clientId=id||null;clientData=null;clientNodes={};clientOffsets={active:0,draft:0};
    if(currentDetail&&currentDetail.asset_kind==="client_rule"){++detailEpoch;currentDetail=null;if(atlas){atlas.setDetail(null);}delete byId("kw-detail").dataset.nodeId;message(byId("kw-detail"),"客户已切换，请重新选择对应客户的规则。");}
    renderClientLayer();if(currentDetail){renderDetail(currentDetail);}
    if(!id){message(byId("kw-client-status"),"当前仅查看公共规则。选择客户后可核对其启用快照和维护草稿。");return Promise.resolve();}
    message(byId("kw-client-status"),"正在读取客户快照和维护草稿……");
    return request("/api/v1/clients/"+encodeURIComponent(id)+"/knowledge").then(function(result){
      if(epoch!==clientEpoch){return;}
      clientData=result;(result.active_rules||[]).forEach(function(rule){registerClientRule(rule,"active");});
      (result.drafts||[]).forEach(function(rule){registerClientRule(rule,"draft");});
      message(byId("kw-client-status"),"已选择“"+result.profile.display_name+"”。启用快照与维护草稿分别列示；位置仅表示范围关联，实际采用须运行条件解析。"+(result.profile.status!=="active"?"该客户档案已停用。":""),result.profile.status!=="active");
      renderClientLayer();if(currentDetail){renderDetail(currentDetail);}
    }).catch(function(error){if(epoch===clientEpoch){message(byId("kw-client-status"),error.message+" 客户叠加未载入，未以草稿替代启用内容。",true);renderClientLayer();}});
  }
  function matchingClientNodes(variant) {
    return Object.values(clientNodes).filter(function(n){return n.client_variant===variant&&scopeMatches(n.body.scope,state.typeId,state.domainCode)&&(!state.query||JSON.stringify(n.body).toLowerCase().indexOf(state.query.toLowerCase())>=0);});
  }
  function renderClientLayer() {
    var box=byId("kw-client-layer");if(!box){return;}clear(box);if(!clientData){return;}
    box.appendChild(el("h3",clientData.profile.display_name+"的补充要求"));
    box.appendChild(el("p","这里只按所选合同类型或大类定位；角色、阶段、条款议题、场景、事实和人工确认均须另行解析。","kw-muted"));
    if(bridge.openRulePreview){box.appendChild(button("带入客户与类型，核对实际采用情况",function(){Promise.resolve(bridge.openRulePreview(clientId,state.typeId)).catch(function(e){message(byId("kw-client-status"),e.message,true);});},"secondary"));}
    [["active","当前启用快照"],["draft","维护草稿"]].forEach(function(pair){
      var variant=pair[0],part=section(pair[1]);part.dataset.clientVariant=variant;box.appendChild(part);
      if(variant==="active"){
        if(!clientData.active_snapshot){part.appendChild(el("p","尚无启用快照。以下维护草稿不会替代正式规则。","kw-warning"));return;}
        var meta=el("details",null,"kw-snapshot-info");meta.appendChild(el("summary","固定版本与来源"));meta.appendChild(valueView(clientData.active_snapshot,"snapshot"));part.appendChild(meta);
      }else{part.appendChild(el("p","可能包含已发布规则的工作副本；列表始终显示当前草稿原文，不代表正式启用。","kw-muted"));}
      var nodes=matchingClientNodes(variant),offset=Math.min(clientOffsets[variant],Math.max(0,Math.floor((nodes.length-1)/clientPageSize)*clientPageSize));clientOffsets[variant]=offset;
      var ul=el("ul",null,"kw-rule-items kw-client-items");
      nodes.slice(offset,offset+clientPageSize).forEach(function(n){var li=el("li"),b=button(n.title,function(){selectNode(n.node_id);},"kw-rule-button");b.dataset.clientNodeId=n.node_id;b.dataset.ruleId=n.body.client_rule_id;li.appendChild(b);li.appendChild(el("p",(variant==="active"?"启用快照固定版本":"维护草稿 · "+(statusNames[n.body.approval_status]||n.body.approval_status))+" · 版本 "+n.version,"kw-meta"));ul.appendChild(li);});
      part.appendChild(ul);if(!nodes.length){part.appendChild(el("p","该位置没有匹配的客户规则。","kw-muted"));}
      var pages=el("nav",null,"kw-pagination");pages.setAttribute("aria-label",pair[1]+"分页");pages.appendChild(el("p","第 "+(nodes.length?offset+1:0)+"–"+Math.min(offset+clientPageSize,nodes.length)+" 项，共 "+nodes.length+" 项"));
      var previous=button("上一页",function(){clientOffsets[variant]=offset-clientPageSize;renderClientLayer();},"secondary");previous.disabled=offset===0;pages.appendChild(previous);
      var next=button("下一页",function(){clientOffsets[variant]=offset+clientPageSize;renderClientLayer();},"secondary");next.disabled=offset+clientPageSize>=nodes.length;pages.appendChild(next);part.appendChild(pages);
    });
  }
  function clientEdges(node) {
    var edges=[],scope=node.body.scope||{};
    function add(id,kind,title,field){var local=clientNodes["client:"+node.client_profile_id+":"+node.client_variant+":"+id],to=local?local.node_id:id;edges.push({from_id:node.node_id,from_title:titleIndex[node.node_id],to_id:to,to_title:titleIndex[to]||id,label:title,kind:kind,source_field:field,status:titleIndex[to]?"valid":"unresolved"});}
    (scope.domain_codes||[]).forEach(function(id){add(id,"classified_under","限定业务大类","scope.domain_codes");});
    (scope.type_ids||[]).forEach(function(id){add(id,"belongs_to","限定合同类型","scope.type_ids");});
    (node.body.depends_on||[]).forEach(function(id){add(id,"depends_on","前置依赖","depends_on");});
    (node.body.conflicts_with||[]).forEach(function(id){add(id,"conflicts_with","声明冲突","conflicts_with");});
    (node.body.source_refs||[]).forEach(function(id){add(id,"sourced_from","依据来源","source_refs");});return edges;
  }
  function clientDetail(node) {
    var ids=(node.body.source_refs||[]).concat(node.body.depends_on||[],node.body.conflicts_with||[]),epoch=clientEpoch;
    return Promise.all(Array.from(new Set(ids)).map(function(id){
      if(clientNodes["client:"+node.client_profile_id+":"+node.client_variant+":"+id]){return null;}
      return bridge.call("GET","/api/v1/knowledge/nodes/"+encodeURIComponent(id)+"?projection_mode=maintenance").then(function(response){
        if(response.body.success){return response.body.result;}
        if(response.status===404){return {node_id:id,title:id,body:null};}
        throw new Error(bridge.errorText(response.body,"关联读取失败："));
      });
    })).then(function(records){
      if(epoch!==clientEpoch){throw Error("客户已切换，请重新选择规则。");}
      records.filter(Boolean).forEach(function(n){if(n.body){titleIndex[n.node_id]=n.title;}});
      node.sources=records.filter(function(n){return n&&(node.body.source_refs||[]).indexOf(n.node_id)>=0;});
      node.relations={outgoing:clientEdges(node),incoming:[]};
      Object.values(clientNodes).filter(function(n){return n.client_variant===node.client_variant&&n.node_id!==node.node_id;}).forEach(function(n){clientEdges(n).forEach(function(edge){if(edge.to_id===node.node_id){node.relations.incoming.push(edge);}});});return node;
    });
  }
  function appendClientRelations(node) {
    if(!clientData||node.asset_kind==="client_rule"){return;}
    var box=section("所选客户的补充与引用","kw-client-relations");
    box.appendChild(el("p","范围关联不表示前置依赖、优先级或本次合同已采用；明确依赖和来源引用另列。","kw-muted"));
    var any=false;
    [["active","启用快照"],["draft","维护草稿"]].forEach(function(pair){
      var matches=[];Object.values(clientNodes).filter(function(n){return n.client_variant===pair[0];}).forEach(function(n){
        var explicit=clientEdges(n).filter(function(edge){return edge.to_id===node.node_id;});
        var scoped=node.asset_kind!=="knowledge_source"&&(node.locations||[]).some(function(loc){return (!state.typeId||loc.type_id===state.typeId)&&scopeMatches(n.body.scope,loc.type_id,loc.domain_code);});
        if(node.asset_kind==="contract_type"){scoped=scopeMatches(n.body.scope,node.node_id,typeIndex[node.node_id]&&typeIndex[node.node_id].domainCode);}
        if(node.asset_kind==="domain_principle"){scoped=scopeMatches(n.body.scope,null,node.body.domain_code);}
        if(explicit.length||scoped){matches.push({node:n,explicit:explicit,scoped:scoped});}
      });
      if(!matches.length){return;}any=true;box.appendChild(el("h5",pair[1]));var ul=el("ul");
      matches.forEach(function(item){var li=el("li");li.appendChild(nodeLink(item.node.node_id,titleIndex[item.node.node_id]));li.appendChild(el("p",item.explicit.length?item.explicit.map(function(e){return e.label+"（"+e.source_field+"）";}).join("、"):"相同类型范围的客户补充；未声明依赖关系", "kw-meta"));ul.appendChild(li);});box.appendChild(ul);
    });
    if(!any){box.appendChild(el("p","该位置没有已声明的客户引用或匹配的范围关联。","kw-muted"));}byId("kw-detail").appendChild(box);
  }
  function nodeLink(id, title) {
    return button(title || titleIndex[id] || id, function () {
      if(typeIndex[id]) { selectType(id); }
      else if(domainIndex[id]) { selectDomain(id); }
      else { selectNode(id); }
    });
  }
  function author(kind, operation, copied) {
    if(kind === "client_rule") {
      if(!clientId) { message(byId("kw-client-status"),"请先明确选择本次维护的客户，再新增客户要求。",true);return; }
      // The chosen overlay owner is explicit; the editor must never inherit a
      // different client selected in another view.
      bridge.loadClientDetail(clientId);
    }
    var useExisting = currentDetail && currentDetail.authoring_kind === kind && (operation === "supersede" || copied);
    var body = useExisting ? JSON.parse(JSON.stringify(currentDetail.body || {})) : {};
    if (!useExisting) {
      if (state.typeId) { body.type_id = state.typeId; if (kind === "clause_module") { body.type_ids = [state.typeId]; } }
      if (state.domainCode && kind === "domain_principle") {
        body.domain_code = state.domainCode; body.domain_name = domainIndex[state.domainCode].title;
      }
    }
    document.dispatchEvent(new CustomEvent("studio-author-rule", { detail: {
      kind: kind, body: body, node: useExisting ? currentDetail : null, operation: operation || "create",
      copied: !!copied, source_node_id: copied && currentDetail ? currentDetail.node_id : null,
      context_type_id: state.typeId, context_domain_code: state.domainCode
    } }));
  }

  function makeLayout() {
    root.className = "knowledge-workbench";
    var heading = el("div", null, "kw-heading");
    var intro = el("div"); intro.appendChild(el("p", "公共知识 · 维护视图", "kw-kicker"));
    intro.appendChild(el("h2", "理解规则，也看清它的位置"));
    intro.appendChild(el("p", "从业务大类找到合同类型，再阅读原则、条款模块和依据。阅读或保存候选内容不会使它自动启用。"));
    heading.appendChild(intro);
    var refresh = button("刷新规则目录", loadFramework, "secondary"); refresh.id = "kw-refresh"; heading.appendChild(refresh);
    root.appendChild(heading);
    var map = el("div", null, "kw-framework-guide"); map.id = "kw-framework-guide";
    map.appendChild(el("strong", "通用要求 → 业务大类 → 合同类型 → 原则与条款模块"));
    map.appendChild(el("span", "客户规则在明确选择客户后叠加；共享模块可以出现在多个类型下，规则身份保持同一个。"));
    root.appendChild(map);
    var atlasContainer = el("section"); atlasContainer.id = "kw-atlas";
    atlasContainer.setAttribute("aria-label", "规则体系可视图"); root.appendChild(atlasContainer);
    var clientBar=el("div",null,"kw-client-controls"),clientLabel=el("label","客户补充规则");
    clientLabel.htmlFor="kw-client-select";clientBar.appendChild(clientLabel);
    var clientSelect=el("select");clientSelect.id="kw-client-select";clientSelect.setAttribute("aria-label","客户补充规则");
    var publicOnly=el("option","仅查看公共规则");publicOnly.value="";clientSelect.appendChild(publicOnly);clientBar.appendChild(clientSelect);
    clientSelect.addEventListener("change",function(){loadClientOverlay(clientSelect.value);});
    clientBar.appendChild(button("刷新客户与规则",function(){loadClientChoices();if(clientId){loadClientOverlay(clientId);} },"secondary"));
    var clientStatus=el("p","选择客户后分别查看当前启用快照与维护草稿；目录位置不代表本次合同一定采用。","kw-muted");clientStatus.id="kw-client-status";clientStatus.setAttribute("role","status");clientBar.appendChild(clientStatus);root.appendChild(clientBar);
    var search = el("form", null, "kw-search"); search.id = "kw-search-form";
    var searchLabel = el("label", "搜索规则名称或完整正文"); searchLabel.htmlFor = "kw-search-input";
    var input = el("input"); input.id = "kw-search-input"; input.type = "search"; input.placeholder = "例如：验收、交割、付款前提";
    var submit = el("button", "搜索全部规则", "primary"); submit.type = "submit";
    search.appendChild(searchLabel); search.appendChild(input); search.appendChild(submit);
    search.addEventListener("submit", function (event) {
      event.preventDefault(); state.query = input.value.trim(); state.typeId = null; state.domainCode = null; state.offset = 0;
      renderDirectory(byId("kw-type-search").value); loadRules();
    });
    root.appendChild(search);
    var layout = el("div", null, "kw-layout");
    var nav = el("aside", null, "kw-panel kw-navigation"); nav.setAttribute("aria-label", "业务目录");
    nav.appendChild(el("h3", "业务目录"));
    var count = el("p", "正在读取完整目录……", "kw-muted"); count.id = "kw-directory-count"; nav.appendChild(count);
    var typeLabel = el("label", "查找合同类型"); typeLabel.htmlFor = "kw-type-search"; nav.appendChild(typeLabel);
    var typeSearch = el("input"); typeSearch.id = "kw-type-search"; typeSearch.type = "search"; typeSearch.placeholder = "输入类型名称";
    typeSearch.addEventListener("input", function () { renderDirectory(typeSearch.value.trim()); }); nav.appendChild(typeSearch);
    var directory = el("div"); directory.id = "kw-directory"; nav.appendChild(directory); layout.appendChild(nav);
    var workspace = el("section", null, "kw-workspace");
    var crumb = el("nav", null, "kw-breadcrumb"); crumb.id = "kw-breadcrumb"; crumb.setAttribute("aria-label", "当前位置"); workspace.appendChild(crumb);
    var workGrid = el("div", null, "kw-work-grid");
    var listPanel = el("section", null, "kw-panel kw-list-panel");
    var listTitle = el("h3", "选择一个合同类型"); listTitle.id = "kw-list-title"; listPanel.appendChild(listTitle);
    var actions = el("div", null, "kw-actions"); actions.id = "kw-new-actions"; listPanel.appendChild(actions);
    var list = el("div"); list.id = "kw-rule-list"; list.setAttribute("aria-live", "polite"); listPanel.appendChild(list);
    var paging = el("nav", null, "kw-pagination"); paging.id = "kw-pagination"; paging.setAttribute("aria-label", "规则分页"); listPanel.appendChild(paging);
    var maintenance=el("section");maintenance.id="kw-public-maintenance";maintenance.setAttribute("aria-live","polite");listPanel.appendChild(maintenance);
    var clientList=el("section",null,"kw-client-layer");clientList.id="kw-client-layer";listPanel.appendChild(clientList);
    workGrid.appendChild(listPanel);
    var detail = el("article", null, "kw-panel kw-detail"); detail.id = "kw-detail"; detail.setAttribute("aria-live", "polite");
    message(detail, "选择一条规则，阅读完整要求、处理口径、依据及使用关系。"); workGrid.appendChild(detail);
    workspace.appendChild(workGrid); layout.appendChild(workspace); root.appendChild(layout);
  }

  function renderDirectory(query) {
    var box = byId("kw-directory"); clear(box); query = (query || "").toLowerCase();
    box.appendChild(button("通用审查要求", function () { selectGlobal("global"); }, "kw-directory-global"));
    var matched = 0;
    framework.domains.forEach(function (domain) {
      var types = domain.types.filter(function (t) { return !query || (t.title + " " + t.type_code).toLowerCase().indexOf(query) >= 0 || domain.title.indexOf(query) >= 0; });
      if (query && !types.length) { return; }
      matched += types.length;
      var group = el("details", null, "kw-domain"); group.dataset.domainCode = domain.domain_code;
      group.open = !!query || domain.domain_code === state.domainCode;
      var summary = el("summary", domain.title + "（" + domain.types.length + " 类）"); group.appendChild(summary);
      var principle = button("阅读本大类原则", function () {
        selectDomain(domain.domain_code);
      }, "kw-domain-principle"); group.appendChild(principle);
      var ul = el("ul", null, "kw-types");
      types.forEach(function (type) {
        var li = el("li"), b = button(type.title, function () { selectType(type.type_id); }, "kw-type");
        b.dataset.typeId = type.type_id; b.setAttribute("aria-current", state.typeId === type.type_id ? "true" : "false");
        var note = type.coverage.status === "gap" ? "专项内容待建设" : type.runtime_enabled ? "已启用类型" : "候选类型";
        b.appendChild(el("small", note)); li.appendChild(b); ul.appendChild(li);
      });
      group.appendChild(ul); box.appendChild(group);
    });
    if (!matched) { box.appendChild(el("p", "没有匹配的合同类型。", "kw-muted")); }
    byId("kw-directory-count").textContent = framework.domain_count + " 个业务大类 · " + framework.type_count + " 个合同类型，包含待建设类型";
  }

  function renderBreadcrumb() {
    var box = byId("kw-breadcrumb"); clear(box);
    box.appendChild(button("全部规则", function () { selectGlobal(); }));
    if (state.domainCode && domainIndex[state.domainCode]) {
      box.appendChild(el("span", " / ")); box.appendChild(nodeLink(state.domainCode, domainIndex[state.domainCode].title));
    }
    if (state.typeId && typeIndex[state.typeId]) {
      box.appendChild(el("span", " / ")); box.appendChild(nodeLink(state.typeId, typeIndex[state.typeId].title));
    }
    if (state.query) { box.appendChild(el("span", " / 搜索：“" + state.query + "”")); }
  }

  function selectGlobal(layer) {
    state.typeId = null; state.domainCode = null; state.query = ""; state.offset = 0;
    byId("kw-search-input").value = ""; renderDirectory(byId("kw-type-search").value); loadRules(layer);
  }

  function selectType(typeId, options) {
    options = options || {};
    if (!typeIndex[typeId]) { selectNode(typeId, options); return; }
    state.typeId = typeId; state.domainCode = typeIndex[typeId].domainCode; state.query = ""; state.offset = 0;
    byId("kw-search-input").value = ""; renderDirectory(byId("kw-type-search").value);
    var loading = loadRules(), selectedListEpoch = listEpoch;
    selectNode(options.nodeId || typeId, options);
    var selectedDetailEpoch = detailEpoch;
    loading.then(function () {
      if (state.typeId === typeId && selectedListEpoch === listEpoch && selectedDetailEpoch === detailEpoch && options.reveal !== false) {
        revealSelection(byId("kw-list-title"));
      }
    });
  }

  function selectDomain(domainCode, options) {
    if(!domainIndex[domainCode]) { selectNode(domainCode, options);return; }
    state.domainCode=domainCode;state.typeId=null;state.query="";state.offset=0;
    byId("kw-search-input").value="";renderDirectory(byId("kw-type-search").value);
    loadRules("domain");selectNode(domainIndex[domainCode].principle_node_ids[0]||domainCode, options);
  }

  function loadRules(layer) {
    var epoch = ++listEpoch, box = byId("kw-rule-list");
    state.layer = layer || null;
    var context = Object.assign({}, state), scope = JSON.stringify([state.domainCode,state.typeId,state.query,state.layer]);
    if(scope !== listScope){
      listScope = scope; ++detailEpoch; currentDetail = null;
      delete byId("kw-detail").dataset.nodeId; delete byId("kw-detail").dataset.maintenanceId;
      message(byId("kw-detail"), "选择一条规则，阅读完整要求、处理口径、依据及使用关系。");
      if(atlas){atlas.setDetail(null);}
    }
    if(atlas){atlas.setContext(context);}
    renderBreadcrumb();clientOffsets={active:0,draft:0};renderClientLayer();
    loadMaintenance();
    byId("kw-list-title").textContent = state.typeId ? typeIndex[state.typeId].title + "的知识" : state.query ? "搜索结果" : layer === "global" ? "通用审查要求" : layer === "domain" ? "本大类审查原则" : "全部公共规则";
    var actions = byId("kw-new-actions"); clear(actions);
    if (state.typeId) {
      actions.appendChild(button("新增类型原则", function () { author("principle", "create"); }, "secondary"));
      actions.appendChild(button("新增条款模块", function () { author("clause_module", "create"); }, "secondary"));
      actions.appendChild(button("新增客户要求", function () { author("client_rule", "create"); }, "secondary"));
    }
    message(box, "正在读取规则……"); clear(byId("kw-pagination"));
    var params = "limit=" + state.limit + "&offset=" + state.offset;
    var route;
    if (state.query) { route = "/api/v1/rules/search?q=" + encodeURIComponent(state.query) + "&" + params; }
    else {
      route = "/api/v1/rules/tree?" + params;
      if (state.typeId) { route += "&layer=type&type_id=" + encodeURIComponent(state.typeId); }
      else if (layer) { route += "&layer=" + layer; }
    }
    return request(route).then(function (result) {
      if (epoch !== listEpoch) { return; }
      if (layer === "domain" && state.domainCode) {
        result.nodes = result.nodes.filter(function (n) { return n.node_id === state.domainCode; });
        result.total = result.nodes.length; result.offset = 0; result.truncated = false;
      }
      if(atlas){atlas.setRules(result,context);}
      clear(box);
      if (!result.nodes.length) { message(box, "该位置尚无专项规则。可从当前类型新建草稿；通用要求仍可独立查阅。"); }
      var ul = el("ul", null, "kw-rule-items");
      result.nodes.forEach(function (node) {
        titleIndex[node.node_id] = node.title;
        var li = el("li"), b = button(node.title, function () {
          if (node.node_kind === "contract_type") { selectType(node.node_id); } else { selectNode(node.node_id); }
        }, "kw-rule-button"); b.dataset.nodeId = node.node_id;
        li.appendChild(b); li.appendChild(el("div", (kindNames[node.node_kind] || "配套知识") + " · " +
          (node.node_kind === "knowledge_source" ? sourceStatus(node) : node.activation_status === "active" && node.approval_status === "active" ? "已启用" : "未启用 · " + (statusNames[node.approval_status] || node.approval_status)), "kw-meta"));
        ul.appendChild(li);
      });
      box.appendChild(ul);
      var paging = byId("kw-pagination"); clear(paging);
      var first = result.total ? result.offset + 1 : 0, last = result.offset + result.nodes.length;
      paging.appendChild(el("p", "第 " + first + "–" + last + " 项，共 " + result.total + " 项"));
      var previous = button("上一页", function () { state.offset = Math.max(0, state.offset - state.limit); loadRules(state.layer); }, "secondary");
      previous.id = "kw-page-previous"; previous.disabled = result.offset === 0;
      var next = button("下一页", function () { state.offset += state.limit; loadRules(state.layer); }, "secondary");
      next.id = "kw-page-next"; next.disabled = !result.truncated; paging.appendChild(previous); paging.appendChild(next);
    }).catch(function (error) { if (epoch === listEpoch) { message(box, error.message, true); if(atlas){atlas.setError(error.message,context);} } });
  }

  function revealSelection(box) {
    if (!box || !box.getClientRects().length) { return; }
    var header = document.querySelector(".topbar");
    box.style.scrollMarginTop = ((header ? header.getBoundingClientRect().height : 0) + 16) + "px";
    box.tabIndex = -1;
    box.focus({preventScroll:true});
    box.scrollIntoView({block:"start",behavior:"instant"});
  }

  function renderSelection(node, options) {
    renderDetail(node);
    if (node.asset_kind !== "contract_type" && (!options || options.reveal !== false)) { revealSelection(byId("kw-detail")); }
  }

  function selectNode(id, options) {
    var epoch = ++detailEpoch; currentDetail = null;
    if(atlas){atlas.setDetail(null);}
    delete byId("kw-detail").dataset.nodeId; delete byId("kw-detail").dataset.maintenanceId;
    message(byId("kw-detail"), "正在读取完整内容……");
    if(clientNodes[id]) { return clientDetail(clientNodes[id]).then(function(node){if(epoch===detailEpoch){renderSelection(node,options);}}).catch(function(error){if(epoch===detailEpoch){message(byId("kw-detail"),error.message,true);}}); }
    var url = "/api/v1/knowledge/nodes/" + encodeURIComponent(id) + "?projection_mode=maintenance";
    if (state.typeId) { url += "&type_id=" + encodeURIComponent(state.typeId); }
    return request(url).then(function (node) { if (epoch === detailEpoch) { renderSelection(node,options); } })
      .catch(function (error) { if (epoch === detailEpoch) { message(byId("kw-detail"), error.message, true); } });
  }

  function valueView(value, key) {
    var box = el("div", null, "kw-value");
    if (value === null || value === undefined) { box.appendChild(el("span", "尚未记录", "kw-muted")); return box; }
    if (Array.isArray(value)) {
      if (!value.length) { box.appendChild(el("span", "未列出；不据此推定已确认或不适用", "kw-muted")); return box; }
      var ul = el("ul"); value.forEach(function (item) { var li = el("li"); li.appendChild(valueView(item, key)); ul.appendChild(li); }); box.appendChild(ul); return box;
    }
    if (typeof value === "object") {
      var dl = el("dl", null, "kw-nested"); Object.keys(value).forEach(function (name) {
        dl.appendChild(el("dt", titleIndex[name] || label(name))); var dd = el("dd"); dd.appendChild(valueView(value[name], name)); dl.appendChild(dd);
      }); box.appendChild(dl); return box;
    }
    if (typeof value === "boolean") { box.appendChild(el("span", value ? "是" : "否")); return box; }
    var text = String(value);
    if (key === "roles" && roleLabels[text]) { text = roleLabels[text] + "（" + text + "）"; }
    else if (key === "scene_tags" && sceneLabels[text]) { text = sceneLabels[text] + "（" + text + "）"; }
    else if ((key === "activation_status" || key === "approval_status") && statusNames[text]) { text = statusNames[text]; }
    if(key === "url" && /^https?:\/\//i.test(text)) { var link=el("a",text);link.href=text;link.target="_blank";link.rel="noopener noreferrer";box.appendChild(link);return box; }
    if (titleIndex[value] && (key.indexOf("id") >= 0 || key.indexOf("source") >= 0 || key.indexOf("depend") >= 0 || key.indexOf("module") >= 0 || key.indexOf("conflict") >= 0)) {
      box.appendChild(nodeLink(value));
    } else { box.appendChild(el("span", text)); }
    return box;
  }

  function section(title, id) { var s = el("section", null, "kw-content-section"); if (id) { s.id = id; } s.appendChild(el("h4", title)); return s; }
  function renderDetail(node) {
    if (!root) { return; }
    currentDetail = node; titleIndex[node.node_id] = node.title;
    if(atlas){atlas.setDetail(node);}
    (node.sources || []).forEach(function (s) { titleIndex[s.node_id] = s.title; });
    (node.relations && node.relations.outgoing || []).concat(node.relations && node.relations.incoming || []).forEach(function (r) {
      titleIndex[r.from_id] = r.from_title; titleIndex[r.to_id] = r.to_title;
    });
    var box = byId("kw-detail"); clear(box); box.dataset.nodeId = node.node_id;
    box.appendChild(el("p", kindNames[node.asset_kind || node.node_kind] || "配套知识", "kw-kicker"));
    box.appendChild(el("h3", node.title));
    var enabled = node.approval_status === "active" && node.activation_status === "active";
    var statusText=enabled ? "当前资产已启用；具体是否采用仍须结合本次合同情形解析。" : "维护可读 · 当前未启用。这里展示完整候选内容，不会因此进入正式合同审查。";
    if(node.asset_kind === "knowledge_source") { statusText="依据来源记录 · "+sourceStatus(node)+"。来源核验记录与规则启用是不同事项，请结合原始核验日期和本次适用情形核对。"; }
    if(node.asset_kind === "client_rule") { statusText=node.client_variant === "active" ? "当前启用快照中的版本，内容固定；具体采用仍须结合角色、阶段、条件及真实解析结果。" : "维护草稿 · 尚未以此内容启用。即使同一编号已有启用版本，也须分别核对。";if(clientData&&clientData.profile.status!=="active"){statusText="客户档案已停用。以下内容仅供维护核对，不表示仍可用于新任务。";} }
    box.appendChild(el("p",statusText,"kw-status"+(!enabled&&node.asset_kind!=="knowledge_source"?" kw-warning":"")));
    if (node.asset_kind === "contract_type") {
      box.appendChild(el("p", "这是合同分类位置。请选择左侧具体原则或模块，阅读该类型的完整审查要求。", "kw-message"));
    }
    var kind = node.authoring_kind;
    if(kind === "client_rule") {
      var edit=button(node.client_variant === "active"?"打开对应维护草稿":"编辑这份维护草稿",function(){
        if(bridge.editClientRule){Promise.resolve(bridge.editClientRule(node.client_profile_id,node.body.client_rule_id)).catch(function(e){message(byId("kw-client-status"),e.message,true);});}
        else{bridge.loadClientDetail(node.client_profile_id);bridge.loadRuleIntoForm(node.body.client_rule_id);bridge.showView("clients");}
      },"secondary");box.appendChild(edit);
      if(node.client_variant === "active"&&!Object.values(clientNodes).some(function(n){return n.client_variant==="draft"&&n.body.client_rule_id===node.body.client_rule_id;})){edit.disabled=true;box.appendChild(el("p","当前没有同编号维护草稿，请先在客户维护页新建下一版本。","kw-muted"));}
    } else if (["domain_principle", "principle", "clause_module"].indexOf(kind) >= 0) {
      var actions = el("div", null, "kw-actions");
      actions.appendChild(button("提出修改", function () { author(kind, "supersede"); }, "secondary"));
      actions.appendChild(button("复制为新规则", function () { author(kind, "create", true); }, "secondary")); box.appendChild(actions);
    }
    var locations = section("在规则体系中的位置", "kw-locations");
    if (!(node.locations || []).length) { locations.appendChild(el("p", node.asset_kind === "global_document" ? "通用审查要求，供各类合同使用。" : node.asset_kind === "client_rule" ? "未按合同类型收窄；请完整核对下方角色、阶段、场景与触发条件。" : "该知识没有单独声明合同类型归属；请结合反向引用核对。")); }
    (node.locations || []).forEach(function (location) {
      var p = el("p"); p.appendChild(nodeLink(location.domain_code, location.domain_title));
      if (location.type_id) { p.appendChild(el("span", " → ")); p.appendChild(button(location.type_title, function () { selectType(location.type_id); })); }
      locations.appendChild(p);
    }); box.appendChild(locations);
    if (state.typeId && node.asset_kind === "clause_module") {
      box.appendChild(el("p", "当前依赖按“" + typeIndex[state.typeId].title + "”展示；原始内容仍保留所有类型的依赖映射。", "kw-context-note"));
    }
    if (node.body_format === "markdown") {
      var textSection = section("完整内容"); var pre = el("pre", node.body || "", "kw-document"); textSection.appendChild(pre); box.appendChild(textSection);
    } else {
      (node.content_sections || []).forEach(function (group) {
        var s = section(group.title); var dl = el("dl", null, "kw-fields");
        group.fields.forEach(function (field) {
          dl.appendChild(el("dt", field.label)); var dd = el("dd"); dd.appendChild(valueView(field.value, field.key)); dl.appendChild(dd);
        }); s.appendChild(dl); box.appendChild(s);
      });
    }
    var sources = section("依据内容与核验记录", "kw-sources");
    if (!(node.sources || []).length) { sources.appendChild(el("p", "该资产没有列出直接来源；不据此推定已有完整或有效的法律依据。", "kw-muted")); }
    (node.sources || []).forEach(function (source) {
      var details = el("details", null, "kw-source"); details.appendChild(el("summary", source.title));
      details.appendChild(source.body ? valueView(source.body, "source") : el("p", "来源记录缺失，请补充核验。", "kw-warning"));
      details.appendChild(nodeLink(source.node_id, "查看来源及使用它的规则")); sources.appendChild(details);
    }); box.appendChild(sources);
    var relations = section("关联与反向引用", "kw-relations"); box.appendChild(relations); renderRelations(node.relations || {});
    appendClientRelations(node);
    var raw = el("details", null, "kw-raw"); raw.appendChild(el("summary", "对照原始结构化内容与技术编号"));
    raw.appendChild(el("p", "编号：" + node.node_id + " · 版本：" + (node.version || "未记录")));
    raw.appendChild(el("pre", typeof node.body === "string" ? node.body : JSON.stringify(node.body, null, 2))); box.appendChild(raw);
  }

  function renderRelations(relations) {
    var box = byId("kw-relations"); if (!box) { return; } clear(box); box.appendChild(el("h4", "关联与反向引用"));
    [["outgoing", "本项引用和适用位置"], ["incoming", "哪些规则或类型使用本项"]].forEach(function (pair) {
      box.appendChild(el("h5", pair[1])); var ul = el("ul", null, "kw-relations-list");
      (relations[pair[0]] || []).forEach(function (edge) {
        var li = el("li"); li.appendChild(el("span", edge.label + "："));
        var id = pair[0] === "outgoing" ? edge.to_id : edge.from_id;
        var title = pair[0] === "outgoing" ? edge.to_title : edge.from_title;
        if (edge.status === "valid") { li.appendChild(nodeLink(id, title)); }
        else { li.appendChild(el("span", title + "（引用未解析，请核对）", "kw-warning")); }
        if (edge.scope && edge.scope.type_ids) { li.appendChild(el("small", "适用于：" + edge.scope.type_ids.map(function (t) { return titleIndex[t] || t; }).join("、"))); }
        if(edge.scope && edge.scope.scene_tags && edge.scope.scene_tags.length){li.appendChild(el("small","场景："+edge.scope.scene_tags.map(function(t){return sceneLabels[t]||t;}).join("、")));}
        var technical = el("details", null, "kw-relation-origin"); technical.appendChild(el("summary", "关系依据"));
        technical.appendChild(el("span", "依据已有字段“" + label(edge.source_field) + "”推导；该关系不决定规则优先级。")); li.appendChild(technical); ul.appendChild(li);
      });
      if (!(relations[pair[0]] || []).length) { ul.appendChild(el("li", "没有已声明的关联。", "kw-muted")); }
      box.appendChild(ul);
    });
  }

  function loadMetadata() {
    return Promise.all(["domain_principle", "principle", "clause_module", "client_rule"].map(function (kind) {
      return request("/api/v1/knowledge/authoring-schema?kind=" + kind).then(function (result) {
        if(kind === "client_rule"){clientSchema=result;}
        result.fields.forEach(function (field) { fieldLabels[field.path.split(".").pop().replace("[]", "")] = field.label; });
        var v = result.vocabulary || {};
        (v.role_options || []).forEach(function (item) { roleLabels[item.value] = item.label; });
        (v.scene_options || []).forEach(function (item) { sceneLabels[item.value] = item.label; });
      });
    })).catch(function () { /* Details still contain complete backend-labelled fields. */ });
  }
  function loadFramework() {
    var epoch = ++frameworkEpoch;
    return request("/api/v1/knowledge/framework").then(function (result) {
      if(epoch !== frameworkEpoch){return;}
      framework = result; typeIndex = {}; domainIndex = {};
      result.domains.forEach(function (domain) {
        domainIndex[domain.domain_code] = domain; titleIndex[domain.domain_code] = domain.title;
        domain.types.forEach(function (type) { type.domainCode = domain.domain_code; typeIndex[type.type_id] = type; titleIndex[type.type_id] = type.title; });
      });
      if(window.RuleAtlas){
        if(atlas){atlas.setFramework(result);}else{
          atlas = window.RuleAtlas.create({container:byId("kw-atlas"),framework:result,
            onDomain:function(code){selectDomain(code,{reveal:false});},
            onType:function(id){selectType(id,{reveal:false});},
            onNode:function(id){selectNode(id);},
            onLocation:function(typeId,nodeId){selectType(typeId,{reveal:false,nodeId:nodeId});},
            onGlobal:function(){selectGlobal("global");},
            onBrowse:function(){revealSelection(byId("kw-list-title"));}
          });
        }
        byId("kw-framework-guide").hidden = true;
      }
      renderDirectory(byId("kw-type-search").value); loadRules(state.layer);
      byId("view-map").classList.add("kw-enhanced");
    }).catch(function (error) { if(epoch===frameworkEpoch){message(byId("kw-directory"), error.message, true);} });
  }
  function start() {
    if (root) { return; }
    bridge = window.Studio; root = byId("knowledge-workbench"); if (!bridge || !root) { root = null; return; }
    makeLayout(); loadMetadata().then(function(){return Promise.all([loadFramework(),loadClientChoices()]);});
  }
  window.KnowledgeWorkbench = { renderDetail: renderDetail, renderRelations: renderRelations, selectNode: selectNode,
    selectType: selectType, refresh: loadFramework, getContext: function () { return Object.assign({}, state); } };
  document.addEventListener("studio-ready", start);
  document.addEventListener("studio-authoring-saved",function(event){if(root&&!event.detail.client_profile_id){loadMaintenance();}});
})();
