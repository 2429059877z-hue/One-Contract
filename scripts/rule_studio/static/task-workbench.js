/* Contract tasks use the stored task state and verified artifacts. No local
 * completion flag can replace server validation or an explicit visual review. */
(function () {
  "use strict";
  var api, root, workspace, framework, vocabulary, current = null, handoff = null;
  var busy = false, selectedId = null, detailEpoch = 0, workspaceEpoch = 0, clients = {}, types = {};
  var MAX_UPLOAD = 30 * 1024 * 1024;
  var states = {awaiting_review:"等待实质审查",awaiting_confirmation:"有处理事项待补全",plan_ready:"修改安排已核对，待执行",executing:"正在生成与检查文档",blocked:"检查未通过，暂不可交付",awaiting_visual_review:"等待逐页核验",ready:"已通过交付验收"};
  var views = {reviewed:"保留修订的合同",accepted:"接受全部修订后的合同",rejected:"拒绝全部修订后的合同",report:"审查报告"};
  var checks = [
    ["no_clipping","各页内容完整，没有被裁切"], ["no_overlap","文字、表格及批注没有重叠"],
    ["no_missing_text","没有丢失正文或应保留的内容"], ["fonts_checked","字体、字号和中文显示正常"],
    ["tables_headers_numbering_checked","表格、页眉页脚和编号已核对"],
    ["non_target_format_preserved","未修改部分的格式保持完整"],
    ["accepted_rejected_checked","接受版与拒绝版均已核对，修订行为符合预期"]
  ];
  var labels = {confirmed_valid:"已核验有效",confirmed_absent:"已确认未取得",unverified:"尚未核验",buyer:"买方／采购方",supplier:"卖方／供应方",any:"不限角色"};
  // Accepted review-runtime roles, distinct from display labels and the broader
  // knowledge scope vocabulary. Unknown business roles require an explicit choice.
  var reviewerRoles = [{value:"party_a",label:"甲方"},{value:"party_b",label:"乙方"},{value:"neutral",label:"中立"},
    {value:"other",label:"其他"},{value:"buyer",label:"采购方"},{value:"supplier",label:"供应方"},
    {value:"client",label:"委托方"},{value:"service_provider",label:"服务方"},{value:"lessor",label:"出租方"},
    {value:"lessee",label:"承租方"},{value:"transferor",label:"转让方"},{value:"transferee",label:"受让方"},
    {value:"employer",label:"用人单位"},{value:"employee",label:"劳动者"},{value:"target_company",label:"标的公司"},{value:"guarantor",label:"保证人"}];
  function el(tag,text,cls) { return api.el(tag,text,cls); }
  function $(id) { return document.getElementById(id); }
  function request(method,path,body) {
    return api.call(method,path,body).then(function(r) {
      if (!r.body.success) { throw new Error(api.errorText(r.body,"操作未完成：")); }
      return r.body.result;
    });
  }
  function button(text,run,id,write) {
    var b=el("button",text,"secondary");b.type="button";if(id){b.id=id;}
    if(write){b.dataset.taskWrite="true";b.disabled=api.isReadOnly();}
    b.addEventListener("click",run);return b;
  }
  function option(select,value,text) { var o=el("option",text);o.value=value;select.appendChild(o); }
  function field(parent,title,id,kind) {
    var label=el("label",null,"tw-field"),name=el("span",title),input=el(kind||"input");
    input.id=id;input.setAttribute("aria-label",title);label.appendChild(name);label.appendChild(input);parent.appendChild(label);return input;
  }
  function panel(parent,title) { var p=el("section",null,"tw-panel");p.appendChild(el("h3",title));parent.appendChild(p);return p; }
  function note(text,warning) { return el("p",text,warning?"tw-notice tw-warning":"tw-notice"); }
  function status(target,text,warning) { target.textContent=text;target.className=warning?"tw-status tw-warning":"tw-status"; }
  function setBusy(value,message) {
    busy=value;root.setAttribute("aria-busy",String(value));
    // A finished or failed request must restore each control's own prerequisites;
    // it must not turn an incomplete visual-acceptance button into an enabled one.
    root.querySelectorAll("[data-task-write],#tw-new-form input,#tw-new-form select,#tw-new-form button").forEach(function(b){
      if(value){if(b.dataset.taskBeforeBusy===undefined){b.dataset.taskBeforeBusy=String(b.disabled);}b.disabled=true;}
      else if(b.dataset.taskBeforeBusy!==undefined){b.disabled=api.isReadOnly()||b.dataset.taskBeforeBusy==="true";delete b.dataset.taskBeforeBusy;}
      else if(api.isReadOnly()){b.disabled=true;}
    });
    if(message){status($("tw-status"),message);}
  }
  function fileBase64(file) {
    if(!file){return Promise.reject(new Error("请先选择本地文件。"));}
    if(file.size>MAX_UPLOAD){return Promise.reject(new Error("页面支持不超过 30 MB 的文件。更大的文件请通过随包的本地任务或恢复工具处理。"));}
    return file.arrayBuffer().then(function(buffer){var bytes=new Uint8Array(buffer),parts=[];for(var i=0;i<bytes.length;i+=32768){parts.push(String.fromCharCode.apply(null,bytes.subarray(i,i+32768)));}return btoa(parts.join(""));});
  }
  function download(blob,name) { var url=URL.createObjectURL(blob),a=el("a");a.href=url;a.download=name;a.click();setTimeout(function(){URL.revokeObjectURL(url);},1000); }
  function decodeArtifact(item) {
    var raw=atob(item.content_base64),bytes=new Uint8Array(raw.length);for(var i=0;i<raw.length;i++){bytes[i]=raw.charCodeAt(i);}return bytes;
  }
  function artifact(taskId,path,expected) {
    return request("GET","/api/v1/tasks/"+encodeURIComponent(taskId)+"/artifact?path="+encodeURIComponent(path)).then(function(item){
      if(expected&&item.sha256!==expected){throw new Error("文件版本已经改变，请重新载入任务并核验。");}return item;
    });
  }
  function downloadArtifact(task,path,sha) { return artifact(task.task_id,path,sha).then(function(item){download(new Blob([decodeArtifact(item)],{type:item.mime_type||item.media_type}),item.filename);}).catch(showError); }
  function showError(error) { status($("tw-status"),error.message||String(error),true); }
  function populateSelect(select,values,placeholder) { option(select,"",placeholder);values.forEach(function(v){option(select,v.value,v.label);}); }
  function makeTaskLayout() {
    root.className="task-workbench";
    root.appendChild(el("h2","把合同审查落实到交付文件"));
    root.appendChild(note("先固定本次使用的合同与规则，再由审查 Agent 完成实质判断。上传修改安排后生成文件；全部检查和逐页核验通过，才能进入交付状态。"));
    if(api.isReadOnly()){root.appendChild(note("当前为只读模式，可查看任务和下载已有文件，不能创建、执行或确认交付。",true));}
    var rendering=panel(root,"文档生成环境");rendering.id="tw-rendering";rendering.appendChild(note("正在检查文档转换与逐页预览工具……"));
    var newBox=panel(root,"1 · 新建合同任务"),form=el("form");form.id="tw-new-form";
    var title=field(form,"任务名称","tw-title");title.placeholder="例如：设备采购合同审查";title.maxLength=200;
    var file=field(form,"合同原件（DOCX，最多 30 MB）","tw-input");file.type="file";file.accept=".docx";file.required=true;
    var client=field(form,"本次采用的客户规则","tw-client","select");client.required=true;option(client,"","请明确选择");option(client,"__none__","仅使用公共规则，不叠加客户规则");
    var type=field(form,"合同类型","tw-type","select");type.required=true;option(type,"","请选择已确认的合同类型");
    var role=field(form,"我方角色","tw-role","select");role.required=true;
    populateSelect(role,vocabulary.role_options,"请选择我方角色");
    var stage=field(form,"合同所处阶段","tw-stage","select");stage.required=true;
    populateSelect(stage,vocabulary.stage_options,"请选择合同阶段");
    var reviewScope=field(form,"本次审查范围","tw-review-scope","select");option(reviewScope,"whole_contract","整合同审查（包含应补充的条款）");option(reviewScope,"clause_groups","仅审查明确指定的条款议题");
    var groups=field(form,"本次指定条款议题（可多选）","tw-clause-groups","select");groups.multiple=true;groups.size=6;groups.disabled=true;groups.parentElement.hidden=true;
    (vocabulary.clause_group_options||[]).forEach(function(v){option(groups,v.value,v.label);});
    reviewScope.addEventListener("change",function(){var scoped=reviewScope.value==="clause_groups";groups.parentElement.hidden=!scoped;groups.disabled=!scoped||api.isReadOnly();groups.required=scoped;if(!scoped){Array.from(groups.options).forEach(function(o){o.selected=false;});}});
    form.appendChild(note("整合同审查会核对所有适用客户要求，包括原合同缺失、需要补写的条款。只有明确选择定向审查时，才按条款议题收窄本次范围。"));
    var more=el("details",null,"tw-extra");more.appendChild(el("summary","补充业务场景和已核对的事实"));
    var scenes=field(more,"业务场景（可多选）","tw-scenes","select");scenes.multiple=true;scenes.size=5;
    (vocabulary.scene_options||[]).forEach(function(v){option(scenes,v.value,v.label);});
    more.appendChild(note("只填写实际掌握的事实。留空表示尚未提供，审查 Agent 应结合合同全文判断；仍不确定的前提或商业选择须在对应合同批注中说明，不替您补造事实。"));
    (vocabulary.fact_keys||[]).forEach(function(f,index){var input=field(more,f.description,"tw-fact-"+index,f.enum_values?"select":"input");input.dataset.factKey=f.fact_key;input.dataset.valueType=f.value_type;if(f.enum_values){option(input,"","尚未提供");f.enum_values.forEach(function(v){option(input,v,labels[v]||v);});}else if(f.value_type==="number"){input.type="number";input.step="any";}});
    form.appendChild(more);form.appendChild(note("这里固定已选择的规则版本，不会自动读取合同并替您作出审查判断。"));
    var submit=el("button","上传原件并固定本次规则","primary");submit.id="tw-create";submit.type="submit";submit.dataset.taskWrite="true";form.appendChild(submit);newBox.appendChild(form);
    form.addEventListener("submit",function(e){e.preventDefault();createTask();});
    var live=el("p",null,"tw-status");live.id="tw-status";live.setAttribute("role","status");root.appendChild(live);
    var listBox=panel(root,"任务记录");listBox.appendChild(button("刷新任务",function(){refreshTasks();if(selectedId){loadTask(selectedId);}},"tw-refresh"));
    var list=el("ul",null,"tw-task-list");list.id="tw-task-list";listBox.appendChild(list);
    var detail=el("article",null,"tw-detail");detail.id="tw-detail";detail.appendChild(note("选择一项任务，继续实质审查、生成文件或逐页验收。"));root.appendChild(detail);
    framework.domains.forEach(function(d){var group=el("optgroup");group.label=d.title;d.types.forEach(function(t){types[t.type_id]={title:t.title,domain:d.domain_code};option(group,t.type_id,t.title+(t.runtime_enabled?"":"（专项内容待确认）"));});type.appendChild(group);});
    setBusy(false);
  }
  function refreshClients() {
    return request("GET","/api/v1/clients").then(function(result){var select=$("tw-client"),old=select.value;while(select.options.length>2){select.remove(2);}clients={};(result.clients||[]).forEach(function(c){clients[c.client_profile_id]=c.display_name;if(c.status==="active"){option(select,c.client_profile_id,c.display_name);}});select.value=old;});
  }
  function createTask() {
    if(busy||api.isReadOnly()){return;}
    var form=$("tw-new-form");if(!form.reportValidity()){return;}
    var source=$("tw-input").files[0],typeId=$("tw-type").value,domain=types[typeId]&&types[typeId].domain;
    if(!domain){showError(new Error("请从目录选择合同类型。"));return;}
    if(!source||!source.name.toLowerCase().endsWith(".docx")){showError(new Error("请选择真实的 DOCX 合同原件。"));return;}
    var context={primary_type_id:typeId,secondary_type_ids:[],primary_domain_code:domain,confirmed_domain_code:domain,our_role:$("tw-role").value,contract_stage:$("tw-stage").value,review_scope:$("tw-review-scope").value,clause_groups:$("tw-review-scope").value==="clause_groups"?Array.from($("tw-clause-groups").selectedOptions).map(function(o){return o.value;}):[],scene_tags:Array.from($("tw-scenes").selectedOptions).map(function(o){return o.value;}),classification_status:"high",facts:{},case_authorizations:[],client_profile_id:$("tw-client").value==="__none__"?null:$("tw-client").value,client_policy_snapshot_id:null};
    form.querySelectorAll("[data-fact-key]").forEach(function(i){if(i.value.trim()){context.facts[i.dataset.factKey]=i.dataset.valueType==="number"?Number(i.value):i.value;}});
    setBusy(true,"正在保存原件并固定本次规则……");
    fileBase64(source).then(function(encoded){return request("POST","/api/v1/tasks",{input_name:source.name,input_base64:encoded,title:$("tw-title").value.trim()||source.name,context:context});}).then(function(task){setBusy(false);status($("tw-status"),"原件与规则已固定。"+(task.resolution_blocked?"当前版本检查未通过，请核对具体原因。":"下一步请交由审查 Agent 完成实质审查。"));refreshTasks();return loadTask(task.task_id);}).catch(function(e){setBusy(false);showError(e);});
  }
  function refreshTasks() {
    return request("GET","/api/v1/tasks").then(function(result){var list=$("tw-task-list");api.clear(list);(result.tasks||[]).forEach(function(t){var li=el("li"),open=button(t.title,function(){loadTask(t.task_id);});open.dataset.taskId=t.task_id;li.appendChild(open);li.appendChild(el("span",states[t.state]||"状态待核对","tw-state"));list.appendChild(li);});if(!(result.tasks||[]).length){list.appendChild(el("li","尚无合同任务。"));}}).catch(showError);
  }
  function loadTask(id) {
    var epoch=++detailEpoch;selectedId=id;api.clear($("tw-detail"));$("tw-detail").appendChild(note("正在读取任务与固定规则……"));
    return Promise.all([request("GET","/api/v1/tasks/"+encodeURIComponent(id)),request("GET","/api/v1/tasks/"+encodeURIComponent(id)+"/handoff")]).then(function(results){if(epoch!==detailEpoch){return;}current=results[0];handoff=results[1];renderTask(current,handoff);}).catch(function(e){if(epoch===detailEpoch){current=null;showError(e);$("tw-detail").appendChild(note("本任务未能完整读取，请核对上方提示后刷新。",true));}});
  }
  function namedOption(options,value) { var item=(options||[]).find(function(o){return o.value===value;});return item?item.label:value||"未提供"; }
  function renderTask(task,bundle) {
    var box=$("tw-detail");api.clear(box);box.dataset.taskId=task.task_id;box.dataset.taskState=task.state;
    box.appendChild(el("h2",task.title));box.appendChild(el("p",states[task.state]||"状态待核对","tw-current-state"));
    if(task.state!=="ready"){box.appendChild(note("本任务尚未完成交付验收。即使已生成文件，也应先核对待确认事项、修订结果和全部页面。",true));}
    if(task.errors&&task.errors.length){var errors=panel(box,"尚未解决的事项"),ul=el("ul");task.errors.forEach(function(e){ul.appendChild(el("li",String(e)));});errors.appendChild(ul);}
    var fixed=panel(box,"已固定的原件与规则"),ctx=bundle.context||{},pin=task.pin||{};
    fixed.appendChild(el("p","原件："+task.input.name));
    fixed.appendChild(el("p","合同类型："+((types[ctx.primary_type_id]||{}).title||ctx.primary_type_id)+"；我方："+namedOption(vocabulary.role_options,ctx.our_role)+"；阶段："+namedOption(vocabulary.stage_options,ctx.contract_stage)));
    fixed.appendChild(el("p","本次范围："+(ctx.review_scope==="clause_groups"?"定向审查 · "+(ctx.clause_groups||[]).map(function(g){return namedOption(vocabulary.clause_group_options,g);}).join("、"):"整合同审查，包含应补充的条款")));
    fixed.appendChild(el("p","客户规则："+(pin.client_profile_id?(clients[pin.client_profile_id]||"已明确选择的客户")+(pin.client_policy_snapshot_id?"，已固定已启用版本":"，暂无可用的已启用版本"):"仅使用公共规则")));
    var effective=bundle.effective_rule_set||{};
    fixed.appendChild(el("p","本次解析采用 "+(effective.effective_rules||[]).length+" 项规则；任务期间保持本次版本。"));
    if(effective.coverage_notice){fixed.appendChild(note(effective.coverage_notice));}
    if(task.resolution_blocked){fixed.appendChild(note("固定规则或任务绑定检查未通过。本任务暂不能生成交付文件；请按下列具体原因处理。仅有适用性疑问时，由审查 Agent 结合全文判断并安排合同批注，不要求逐条点击人工确认。",true));}
    var issues=(effective.rule_conflicts||[]).concat((effective.excluded_rules||[]).filter(function(e){return task.resolution_blocked;}));
    if(issues.length){var issueList=el("ul");issues.forEach(function(i){issueList.appendChild(el("li",i.reason||i.message||i.explanation||"有关规则的适用条件需要核对，具体记录可见下方固定信息。"));});fixed.appendChild(issueList);}
    var rules=el("details",null,"tw-extra");rules.appendChild(el("summary","查看本次采用的规则"));var list=el("ul");(effective.effective_rules||[]).forEach(function(r){var body=r.rule_payload||{},li=el("li");li.appendChild(el("strong",body.title||body.name||body.clause_group||body.domain_name||"规则条目"));if(r.inclusion_reason){li.appendChild(el("p",r.inclusion_reason));}list.appendChild(li);});rules.appendChild(list);fixed.appendChild(rules);
    if((bundle.client_rule_requirements||[]).length){
      var clientPanel=panel(box,"客户要求在本合同中的处理"),records=(task.client_rule_dispositions||{}).records||[];
      var dispositionLabels={applied:"安排正文落实",already_satisfied:"原合同已满足",commented:"安排合同批注，保留待判断事项",requires_confirmation:"安排合同批注，保留待判断事项（兼容记录）",not_applicable:"本合同不适用，须在合同批注中说明"};
      (bundle.client_rule_requirements||[]).forEach(function(required){var row=el("div",null,"tw-client-disposition");row.dataset.ruleId=required.rule_id;var record=records.find(function(r){return r.rule_id===required.rule_id;});row.appendChild(el("h4",(required.rule_payload||{}).title||"客户规则"));row.appendChild(el("p",record?dispositionLabels[record.status]||"处理状态待核对":"等待审查 Agent 逐条说明处理方式"));if(record){row.appendChild(el("p",record.explanation));var proof=el("details");proof.appendChild(el("summary","查看处理依据与关联事项"));proof.appendChild(el("pre",JSON.stringify({finding_ids:record.finding_ids,evidence:record.evidence},null,2)));row.appendChild(proof);}if(!required.included&&required.exclusion_reason){row.appendChild(note(required.exclusion_reason));}renderRuleConfirmation(row,task,bundle,required);clientPanel.appendChild(row);});
      if(((task.client_rule_dispositions||{}).pending_rule_ids||[]).length){clientPanel.appendChild(note("部分客户要求尚缺处理依据或真实关联批注。请由审查 Agent 补齐原文依据、批注及关联事项后重新上传；无需为适用性逐条点击人工确认。确定适用的要求仍须落实正文。",true));}
    }
    var technical=el("details",null,"tw-extra");technical.appendChild(el("summary","查看固定版本、依据与原始记录"));technical.appendChild(el("pre",JSON.stringify({pin:pin,context:ctx,effective_rule_set:effective},null,2)));fixed.appendChild(technical);
    var transfer=panel(box,"2 · 交给审查 Agent 完成实质审查");
    transfer.appendChild(note("下载交接资料，并把合同原件一同交给您正在使用的审查 Agent。请其依据固定规则阅读全文，落实适用要求；不适用、事实不足或需要商业选择的事项须在合同真实批注中说明，再返回修改安排。此页面不会自行审合同。"));
    transfer.appendChild(button("下载给审查 Agent 的交接资料",function(){download(new Blob([JSON.stringify(bundle,null,2)],{type:"application/json"}),task.task_id+"-handoff.json");},"tw-handoff"));
    transfer.appendChild(button("下载合同原件",function(){downloadArtifact(task,task.input.path,task.input.sha256);},"tw-original"));
    var instructions=el("details",null,"tw-extra");instructions.appendChild(el("summary","给审查 Agent 的工作说明"));instructions.appendChild(el("p","请读取固定规则与合同全文，逐条判断客户要求的适用性并保留原文证据。确定适用的要求应落实正文；明确不适用、事实不足或需要商业选择的事项，应在对应条款安排真实批注，说明理由、待核实前提或选择，不以报告替代批注，也不因尚无人工决定停下其他确定修改。按 One-Contract 契约交回 JSON 修改安排，保留任务与原件绑定及逐规则处理记录。"));transfer.appendChild(instructions);
    var execution=panel(box,"3 · 上传修改安排并生成文件");
    execution.appendChild(note("此步骤接收审查 Agent 已完成的结构化修改安排（JSON），检查其与原件及固定规则的一致性，再执行真实修订、批注和报告生成。"));
    var upload=field(execution,"审查 Agent 返回的修改安排（JSON）","tw-plan-file");upload.type="file";upload.accept=".json,application/json";upload.dataset.taskWrite="true";upload.disabled=api.isReadOnly()||task.resolution_blocked||busy||task.state==="executing";
    var attach=button("上传并检查修改安排",function(){attachPlan(task);},"tw-plan-upload",true);attach.disabled=upload.disabled;execution.appendChild(attach);
    if(task.plan){execution.appendChild(el("p","已保存通过结构检查的修改安排。执行后仍须核验实际修改结果。"));execution.appendChild(button("下载当前修改安排",function(){downloadArtifact(task,task.plan.path,task.plan.sha256);},"tw-plan-download"));}
    var reviewer=field(execution,"文档修订署名（审查人）","tw-reviewer");reviewer.placeholder="请输入实际审查人姓名";reviewer.dataset.taskWrite="true";
    var reviewRole=field(execution,"文档审查立场","tw-review-role","select");reviewRole.dataset.taskWrite="true";populateSelect(reviewRole,reviewerRoles,"请明确选择审查立场");reviewRole.value=reviewerRoles.some(function(r){return r.value===ctx.our_role;})?ctx.our_role:"";
    var org=field(execution,"机构（可选）","tw-organization");org.dataset.taskWrite="true";
    var run=button("生成修订合同与审查报告",function(){executePlan(task);},"tw-execute",true);
    run.disabled=api.isReadOnly()||busy||!task.plan||task.resolution_blocked||task.state==="awaiting_confirmation"||task.state==="executing";execution.appendChild(run);
    if(api.isReadOnly()){reviewer.disabled=true;org.disabled=true;reviewRole.disabled=true;}
    renderDelivery(box,task);
    if(task.attempts&&task.attempts.length){var records=el("details",null,"tw-extra");records.appendChild(el("summary","查看执行记录和诊断资料"));task.attempts.forEach(function(a,index){var p=el("p","第 "+(index+1)+" 次处理 · "+(states[a.status]||a.status));records.appendChild(p);records.appendChild(button("下载第 "+(index+1)+" 次处理记录",function(){downloadArtifact(task,a.path+"/engine.log");}));});box.appendChild(records);}
  }
  function renderRuleConfirmation(parent,task,bundle,required) {
    var pending=((task.client_rule_dispositions||{}).pending_reasons||{})[required.rule_id]||[];
    pending.forEach(function(reason){parent.appendChild(note(reason,true));});
    var records=(bundle.rule_confirmations||[]).filter(function(r){return r.rule_id===required.rule_id;});
    if(records.length){var history=el("section",null,"tw-confirmation-history");history.appendChild(el("h5","已保存的人工确认"));records.forEach(function(record,index){var item=el("div");item.dataset.evidenceId=record.evidence_id;item.appendChild(el("strong",index===records.length-1?"当前最新决定":"历史决定（不得替代最新决定）"));item.appendChild(el("p",(record.purpose==="not_applicable"?"本合同不适用决定":"升级审核决定")+" · "+record.confirmed_by+" · "+record.confirmed_at));item.appendChild(el("p",record.decision));var code=el("details");code.appendChild(el("summary","给审查 Agent 的确认依据编号"));code.appendChild(el("code",record.evidence_id));item.appendChild(code);history.appendChild(item);});parent.appendChild(history);}
    var details=el("details",null,"tw-confirmation-form");details.appendChild(el("summary","可选：记录已经取得的人工决定"));
    details.appendChild(note("此入口不是适用性判断或生成合同的必经步骤。仅在实际取得人工决定、且希望留存时填写。保存后须由审查 Agent 更新修改安排引用本记录；记录本身不能替代正文修改或真实合同批注。"));
    var suffix=required.rule_id,purpose=field(details,"确认事项","tw-confirm-purpose-"+suffix,"select");option(purpose,"","请选择真实决定的用途");option(purpose,"not_applicable","经核对，决定本规则不适用于本合同");
    if(((required.rule_payload||{}).effect||{}).action==="escalate"){option(purpose,"escalation","已经完成本规则要求的升级审核");}
    var decision=field(details,"具体决定与理由","tw-confirm-decision-"+suffix,"textarea");decision.rows=4;
    var person=field(details,"实际作出确认的人","tw-confirm-person-"+suffix);person.placeholder="填写真实确认人姓名";
    var checkLabel=el("label",null,"tw-check"),check=el("input");check.type="checkbox";check.id="tw-confirm-explicit-"+suffix;checkLabel.appendChild(check);checkLabel.appendChild(document.createTextNode("我已核对本合同与本规则，以上记录反映实际取得的决定"));details.appendChild(checkLabel);
    var save=button("保存人工决定，继续更新修改安排",function(){
      if(save.disabled||busy||api.isReadOnly()){return;}setBusy(true,"正在保存与本合同、规则版本绑定的人工决定……");
      request("POST","/api/v1/tasks/"+encodeURIComponent(task.task_id)+"/rule-confirmations",{rule_id:required.rule_id,purpose:purpose.value,decision:decision.value.trim(),confirmed_by:person.value.trim(),confirmation:check.checked,expected_revision:task.revision}).then(function(result){
        setBusy(false);refreshTasks();return loadTask(result.task.task_id).then(function(){status($("tw-status"),"人工决定已保存。请重新下载交接资料，交由审查 Agent 更新修改安排并引用最新记录；原文依据和应有的合同批注仍须完整，再上传检查。");});
      }).catch(function(error){setBusy(false);showError(error);});
    },"tw-confirm-save-"+suffix,true);
    [purpose,decision,person,check].forEach(function(input){input.dataset.taskWrite="true";input.disabled=api.isReadOnly()||busy||task.state==="executing";input.addEventListener("input",update);input.addEventListener("change",update);});
    function update(){save.disabled=api.isReadOnly()||busy||task.state==="executing"||!purpose.value||!decision.value.trim()||!person.value.trim()||!check.checked;}
    details.appendChild(save);parent.appendChild(details);update();
  }
  function writeTask(task,route,body,message) {
    if(busy||api.isReadOnly()){return Promise.resolve();}setBusy(true,message);
    return request("POST","/api/v1/tasks/"+encodeURIComponent(task.task_id)+"/"+route,Object.assign({expected_revision:task.revision},body)).then(function(updated){setBusy(false);status($("tw-status"),states[updated.state]||"任务已更新",updated.state==="blocked");refreshTasks();return loadTask(updated.task_id);}).catch(function(e){setBusy(false);showError(e);return loadTask(task.task_id);});
  }
  function attachPlan(task) {
    var file=$("tw-plan-file").files[0];if(!file){showError(new Error("请先选择审查 Agent 返回的 JSON 修改安排。"));return;}
    if(file.size>MAX_UPLOAD){showError(new Error("修改安排超过页面允许的大小。"));return;}
    file.text().then(function(text){var plan;try{plan=JSON.parse(text);}catch(e){throw new Error("修改安排不是可读取的 JSON，请审查 Agent 核对后重新提供。");}return writeTask(task,"plan",{plan:plan},"正在检查修改安排与原件、固定规则的对应关系……");}).catch(showError);
  }
  function executePlan(task) {
    var author=$("tw-reviewer").value.trim();if(!author){showError(new Error("请填写实际审查人姓名，作为文档修订署名。"));return;}
    var role=$("tw-review-role").value;if(!role){showError(new Error("请明确选择文档审查立场。"));return;}
    writeTask(task,"execute",{reviewer:{author:author,organization:$("tw-organization").value.trim(),party_role:role}},"正在执行正文修订、生成报告并检查各版本页面；可能需要几分钟，请稍候……");
  }
  function renderDelivery(parent,task) {
    var box=panel(parent,"4 · 核对文件与逐页验收");box.id="tw-delivery";
    if(!task.delivery){box.appendChild(note("文件尚未生成并通过检查。完成实质审查和文档执行后，这里将展示修订合同、报告及逐页图片。"));return;}
    box.appendChild(note(task.state==="ready"?"该版本已完成逐页确认。以下文件与已验收的内容绑定。":"以下是供核对的中间文件，尚不可作为完成件交付。",task.state!=="ready"));
    var downloads=el("div",null,"tw-downloads");(task.delivery.artifacts||[]).forEach(function(a){downloads.appendChild(button("下载"+a.label+(task.state==="ready"?"":"（待验收）"),function(){downloadArtifact(task,a.path,a.sha256);}));});box.appendChild(downloads);
    if(task.state==="ready"){box.appendChild(el("p","检查人："+task.delivery.accepted_by+"；确认时间："+task.delivery.accepted_at));}
    var canAccept=task.state==="awaiting_visual_review"&&!api.isReadOnly(),pageChecks=[],checkInputs=[];
    box.appendChild(note("逐页打开保留修订版、接受版、拒绝版及审查报告。每页实际显示后才能勾选；如果发现问题，请保留未通过状态并交回审查 Agent 修正。"));
    var pages=el("div",null,"tw-pages");box.appendChild(pages);
    Object.keys(views).forEach(function(view){var items=(task.delivery.pages||[]).filter(function(p){return p.view===view;});if(!items.length){return;}var group=el("section",null,"tw-page-group");group.appendChild(el("h4",views[view]+" · "+items.length+" 页"));items.forEach(function(p){
      var card=el("details",null,"tw-page-card");card.dataset.pagePath=p.path;card.appendChild(el("summary","查看第 "+p.page+" 页"));var imageBox=el("div",null,"tw-image");card.appendChild(imageBox);var row=el("label",null,"tw-check"),check=el("input");check.type="checkbox";check.disabled=true;check.dataset.pagePath=p.path;row.appendChild(check);row.appendChild(document.createTextNode("已核对本页，没有影响交付的问题"));if(task.state!=="ready"){card.appendChild(row);}pageChecks.push({input:check,page:p});check.addEventListener("change",updateAcceptance);
      var loaded=false,loading=false;function loadImage(){if(loaded||loading||!card.open){return;}loading=true;api.clear(imageBox);imageBox.appendChild(note("正在读取本版本页面……"));artifact(task.task_id,p.path,p.sha256).then(function(item){if((item.mime_type||item.media_type)!=="image/png"){throw new Error("页面证据不是预期的 PNG 图片。");}api.clear(imageBox);var img=el("img");img.alt=views[view]+" 第 "+p.page+" 页";img.addEventListener("load",function(){loaded=true;loading=false;check.disabled=!canAccept;card.dataset.pageLoaded="true";});img.addEventListener("error",function(){loading=false;check.disabled=true;api.clear(imageBox);imageBox.appendChild(note("页面显示失败，请关闭后重新展开。",true));});img.src="data:image/png;base64,"+item.content_base64;imageBox.appendChild(img);}).catch(function(e){loading=false;api.clear(imageBox);imageBox.appendChild(note(e.message,true));});}
      card.addEventListener("toggle",loadImage);group.appendChild(card);
    });pages.appendChild(group);});
    if(task.state!=="awaiting_visual_review"){box.appendChild(note(task.state==="ready"?"本次验收记录已保存。重新执行或上传新的修改安排后须再次核验。":"当前还有检查未通过，不能确认交付。",task.state!=="ready"));return;}
    var confirm=el("div",null,"tw-acceptance");confirm.id="tw-acceptance";confirm.appendChild(el("h4","对本版本填写最终核对记录"));checks.forEach(function(pair){var row=el("label",null,"tw-check"),input=el("input");input.type="checkbox";input.dataset.visualCheck=pair[0];input.disabled=!canAccept;row.appendChild(input);row.appendChild(document.createTextNode(pair[1]));confirm.appendChild(row);checkInputs.push({key:pair[0],input:input});input.addEventListener("change",updateAcceptance);});
    var inspector=field(confirm,"实际逐页检查人","tw-inspector");inspector.disabled=!canAccept;inspector.addEventListener("input",updateAcceptance);var progress=el("p",null,"tw-notice");progress.id="tw-accept-progress";confirm.appendChild(progress);
    var accept=button("确认本版本通过交付验收",function(){if(accept.disabled){return;}var checklist={};checkInputs.forEach(function(c){checklist[c.key]=c.input.checked;});writeTask(task,"accept",{visual_acceptance:{status:"PASS",inspected_by:inspector.value.trim(),render_manifest_sha256:task.delivery.render_manifest_sha256,checklist:checklist,pages:pageChecks.map(function(c){return {path:c.page.path,sha256:c.page.sha256,status:"PASS"};})}},"正在核对文件版本并保存逐页验收记录……");},"tw-accept",true);accept.disabled=true;confirm.appendChild(accept);box.appendChild(confirm);updateAcceptance();
    function updateAcceptance(){if(!accept){return;}var count=pageChecks.filter(function(c){return c.input.checked&&!c.input.disabled;}).length;progress.textContent="已确认 "+count+" / "+pageChecks.length+" 页。还需完成全部检查项并填写检查人。";accept.disabled=!canAccept||busy||count!==pageChecks.length||!pageChecks.length||checkInputs.some(function(c){return !c.input.checked;})||!inspector.value.trim();}
  }
  function makeWorkspace() {
    workspace.className="task-workbench";workspace.appendChild(el("h2","工作区与备份"));workspace.appendChild(note("客户规则、任务原件、审查记录和交付文件保存在本机工作区。备份包含这些内容，请妥善保管。"));
    var detail=panel(workspace,"当前工作区");detail.id="ws-info";
    var rendering=panel(workspace,"文档生成环境");rendering.id="ws-rendering";
    var backup=panel(workspace,"保存工作区备份");backup.appendChild(note("创建完整备份文件，完成后显示本机保存位置。"));backup.appendChild(button("生成完整备份",function(){workspaceWrite("backup",{},"正在校验并生成工作区备份……");},"ws-backup",true));
    var restore=panel(workspace,"从备份恢复到新工作区");restore.appendChild(note("恢复前会校验备份。恢复结果写入新的目录，当前工作区继续保留；重新启动工作台时选择新目录即可切换。"));
    var file=field(restore,"选择备份文件（ZIP，最多 30 MB）","ws-restore-file");file.type="file";file.accept=".zip,application/zip";file.disabled=api.isReadOnly();
    restore.appendChild(button("校验并恢复到新目录",function(){var file=$("ws-restore-file").files[0];fileBase64(file).then(function(encoded){return workspaceWrite("restore",{archive_base64:encoded},"正在校验并恢复备份……");}).catch(function(e){status($("ws-status"),e.message,true);});},"ws-restore",true));
    restore.appendChild(note("页面恢复支持 30 MB 以内的备份；更大的备份请使用随包 studio_data 恢复工具。"));
    var live=el("div",null,"tw-status");live.id="ws-status";live.setAttribute("role","status");workspace.appendChild(live);
    workspace.appendChild(button("刷新工作区信息",refreshWorkspace,"ws-refresh"));refreshWorkspace();
  }
  function renderEnvironment(target,rendering,error) {
    if(!target){return;}api.clear(target);target.appendChild(el("h3","文档生成环境"));
    var toolNames={soffice:"合同转为 PDF（LibreOffice）",pdfinfo:"读取页面信息（Poppler）",pdftoppm:"生成逐页预览（Poppler）"};
    var keys=Object.keys(toolNames),tools=rendering&&rendering.tools||{},missing=rendering&&rendering.missing_tools;
    var complete=rendering&&rendering.ready===true&&rendering.status==="tools_found"&&Array.isArray(missing)&&!missing.length&&keys.every(function(k){return tools[k]&&typeof tools[k].path==="string"&&tools[k].path.trim();});
    var incomplete=rendering&&rendering.ready===false&&rendering.status==="missing_tools"&&Array.isArray(missing)&&missing.length;
    target.dataset.renderingState=complete?"tools_found":incomplete?"missing_tools":"unknown";
    if(complete){target.appendChild(note("已找到文档转换与逐页预览工具。工具已找到不等于合同已通过验收；仍须实际生成文件，核对字体、修订和每一页。"));}
    else if(incomplete){target.appendChild(note("暂不能生成并完成逐页检查，缺少："+missing.map(function(k){return toolNames[k]||k;}).join("、")+"。可以先查看规则、保存任务和准备修改安排。",true));}
    else{target.appendChild(note("尚未取得可靠的文档生成环境检查结果，请刷新后核对。"+(error?" "+error:""),true));}
    if(rendering&&!error){
      (rendering.remediation||[]).forEach(function(message){target.appendChild(note(String(message),!complete));});
      var details=el("details",null,"tw-extra");details.appendChild(el("summary","查看检测到的工具位置"));
      var list=el("ul");keys.forEach(function(key){var row=el("li");row.appendChild(el("strong",toolNames[key]));row.appendChild(el("p",tools[key]&&tools[key].path||"尚未找到","tw-path"));list.appendChild(row);});details.appendChild(list);target.appendChild(details);
    }
    target.appendChild(button("重新检查文档生成环境",refreshWorkspace,target.id+"-refresh"));
  }
  function refreshWorkspace() {
    var epoch=++workspaceEpoch;
    return request("GET","/api/v1/workspace/status").then(function(result){
      if(epoch!==workspaceEpoch){return;}if($("ws-status").dataset.workspaceReadError){status($("ws-status"),"");delete $("ws-status").dataset.workspaceReadError;}var box=$("ws-info");api.clear(box);box.appendChild(el("h3","当前工作区"));box.appendChild(el("p",result.data_root,"tw-path"));box.appendChild(note(result.read_only?"当前以只读模式打开。":"当前可保存本机维护数据。",result.read_only));(result.recovery_warnings||[]).forEach(function(w){box.appendChild(note(typeof w==="string"?w:w.message||w.reason||JSON.stringify(w),true));});var more=el("details",null,"tw-extra");more.appendChild(el("summary","诊断信息"));more.appendChild(el("pre",JSON.stringify(result,null,2)));box.appendChild(more);
      [$("tw-rendering"),$("ws-rendering")].forEach(function(target){renderEnvironment(target,result.rendering);});
    }).catch(function(e){if(epoch!==workspaceEpoch){return;}status($("ws-status"),e.message,true);$("ws-status").dataset.workspaceReadError="true";[$("tw-rendering"),$("ws-rendering")].forEach(function(target){renderEnvironment(target,null,e.message);});});
  }
  function workspaceWrite(route,body,message) {
    if(api.isReadOnly()){return Promise.resolve();}delete $("ws-status").dataset.workspaceReadError;$("ws-backup").disabled=true;$("ws-restore").disabled=true;status($("ws-status"),message);
    return request("POST","/api/v1/workspace/"+route,body).then(function(result){var box=$("ws-status");api.clear(box);box.appendChild(el("p",result.notice));box.appendChild(el("p",result.path,"tw-path"));}).catch(function(e){status($("ws-status"),e.message,true);}).finally(function(){$("ws-backup").disabled=api.isReadOnly();$("ws-restore").disabled=api.isReadOnly();});
  }
  document.addEventListener("studio-ready",function(){api=window.Studio;root=$("task-workbench");workspace=$("workspace-workbench");if(!root||!workspace){return;}
    Promise.all([request("GET","/api/v1/knowledge/framework"),request("GET","/api/v1/authoring/schema?kind=client_rule")]).then(function(results){framework=results[0];vocabulary=results[1].vocabulary;makeTaskLayout();makeWorkspace();
      var taskTab=document.querySelector('button.tab[data-view="tasks"]'),workspaceTab=document.querySelector('button.tab[data-view="workspace"]');
      if(taskTab){taskTab.addEventListener("click",function(){refreshClients().catch(showError);refreshTasks();refreshWorkspace();});}
      if(workspaceTab){workspaceTab.addEventListener("click",refreshWorkspace);}
      return Promise.all([refreshClients(),refreshTasks()]);}).catch(function(e){root.appendChild(note("任务页面未能完整载入："+e.message,true));});
  });
  window.TaskWorkbench={refresh:function(){return Promise.all([refreshTasks(),refreshClients(),refreshWorkspace()]);},openTask:loadTask};
})();
