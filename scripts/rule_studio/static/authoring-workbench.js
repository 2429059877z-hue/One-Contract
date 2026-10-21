/* Schema-led editing. Complete saved objects are retained, including fields the
 * renderer does not yet understand. No credentials or client data leave memory. */
(function () {
  "use strict";
  var api, schemas = {}, framework, clientEditor, publicEditor, proposalKind = "clause_module";
  var pendingAuthor = null, ready = false, cases = [], currentProposal = null;
  var clientProgress = null, publicProgress = null, conditionIntent = "";
  var batch = null, editingProposal = null, previewExtraState = {review_scope:"whole_contract",clause_groups:[],facts:{}};
  var copy = function (v) { return v === undefined ? undefined : JSON.parse(JSON.stringify(v)); };
  function semanticCondition(body){return Boolean(body&&body.condition&&body.condition.assessment_mode==="semantic");}
  var $ = function (id) { return document.getElementById(id); };
  var texts = { all:"全部满足",any:"任一满足",equals:"等于",not_equals:"不等于",in:"属于所列值",not_in:"不属于所列值",exists:"有该事实",gte:"大于或等于",lte:"小于或等于",require:"列为要求",prefer:"优先采用",forbid:"禁止",escalate:"提交人工处理",set_output:"设定输出",requirement:"明确要求",preference:"优先做法",fallback:"备用处理",escalation:"交由律师判断",output_style:"文书表达",mandatory:"必须遵守",preferred:"优先遵守",advisory:"参考建议",confirmed_valid:"已核验有效",confirmed_absent:"已确认未取得",unverified:"尚未核验",true:"是",false:"否",candidate:"待审核",inactive:"未启用",active:"已启用",not_checked:"尚未核验",checked:"已核验",required:"必需",conditional:"满足条件时需要",optional:"可选",recommended:"建议采用" };
  texts.semantic="由 AI 结合合同全文判断";texts.structured="使用结构化条件";
  function element(tag, text, cls) { return api.el(tag,text,cls); }
  function button(text, action, cls) { var b=element("button",text,cls||"quiet"); b.type="button"; b.addEventListener("click",action); return b; }
  function id(prefix) { return prefix + crypto.randomUUID().replace(/-/g,""); }
  function option(select,value,label) { var o=element("option",label);o.value=value;select.appendChild(o); }
  function status(target,text,warning) { target.textContent=text;target.className=warning?"warning":"inline-message"; }
  function resolved(schema, root) {
    if (!schema || !schema.$ref || schema.$ref.indexOf("#/")!==0) { return schema||{}; }
    var result=root;schema.$ref.slice(2).split("/").forEach(function(k){result=result[k];});
    return Object.assign({},result,schema,{$ref:undefined});
  }
  function initial(schema, root) {
    schema=resolved(schema,root);
    if (schema.const!==undefined) {return copy(schema.const);}
    if (schema.default!==undefined) {return copy(schema.default);}
    var type=Array.isArray(schema.type)?schema.type.filter(function(t){return t!=="null";})[0]:schema.type;
    if(type==="object" || schema.properties){var obj={};(schema.required||[]).forEach(function(k){obj[k]=initial(schema.properties[k],root);});return obj;}
    if(type==="array"){return [];}
    if(type==="boolean"){return false;}
    if(type==="number"||type==="integer"){return null;}
    return "";
  }
  function vocab(meta,path) {
    var key=path.replace(/\[\]$/,"").split(".").pop(),v=meta.vocabulary;
    if(key==="type_id"||key==="type_ids"||key==="supported_type_ids"||key==="unsupported_type_ids") {
      return framework.domains.flatMap(function(d){return d.types.map(function(t){return {value:t.type_id,label:d.title+" · "+t.title+(t.runtime_enabled?"":"（维护候选）")};});});
    }
    if(key==="domain_code"||key==="domain_codes"){return framework.domains.map(function(d){return {value:d.domain_code,label:d.title};});}
    if(key==="source_ids"||key==="source_refs"){return v.source_options||[];}
    if(key==="clause_groups"){return v.clause_group_options||[];}
    if(key==="roles"){return v.role_options;}
    if(key==="scene_tags"){return v.scene_options;}
    if(key==="contract_stages"){return v.stage_options;}
    if(key==="fact_key"){return v.fact_keys.map(function(f){return {value:f.fact_key,label:f.description};});}
    if(key==="target_key"){return v.effect_targets.filter(function(t){return t.client_writable;}).map(function(t){return {value:t.target_key,label:t.description};});}
    return null;
  }
  function Editor(container,meta,value) { this.container=container;this.meta=meta;this.value=copy(value)||initial(meta.schema,meta.schema);this.render();this.baseline=JSON.stringify(this.value); }
  function beginClientRequest(kind) {
    if(!clientEditor){return null;}
    if(clientEditor.pendingRequest){status($("rule-status"),"当前表单的操作尚未完成，请等待后再提交；期间可以继续填写。",true);return null;}
    var s=api.clientState(),token={kind:kind,editor:clientEditor,profileId:s.profileId,ruleId:s.ruleId,
      value:JSON.stringify(clientEditor.value),intent:conditionIntent};
    clientEditor.pendingRequest=token;return token;
  }
  function clientRequestCurrent(token) {
    var s=api.clientState();return token&&clientEditor===token.editor&&s.profileId===token.profileId&&s.ruleId===token.ruleId;
  }
  function endClientRequest(token){if(token&&token.editor.pendingRequest===token){delete token.editor.pendingRequest;}}
  function applyClientResult(token,draft) {
    if(!clientRequestCurrent(token)||draft.client_profile_id!==token.profileId||draft.client_rule_id!==token.editor.value.client_rule_id){return {applied:false};}
    var editor=token.editor,preserve=JSON.stringify(editor.value)!==token.value||conditionIntent!==token.intent
      ||(token.kind!=="save"&&token.value!==editor.baseline);
    if(!preserve){api.fillRuleForm(draft);return {applied:true,preserved:false};}
    // Advance only the saved base and server-owned records. Editable values and
    // the baseline of the submitted version remain distinct from later typing.
    if(!api.adoptRuleVersion(draft)){return {applied:false};}
    editor.meta.fields.filter(function(f){return !f.path.includes(".")&&!f.path.includes("[]")&&f.managed_by!=="user"&&f.path!=="version";}).forEach(function(f){
      if(draft[f.path]!==undefined){editor.value[f.path]=copy(draft[f.path]);}
      var node=editor.container.querySelector('div.aw-field[data-field-path="'+f.path+'"] .aw-managed');
      if(node){var value=editor.value[f.path];node.textContent=value===undefined?"由实际操作产生":(typeof value==="object"?JSON.stringify(value,null,2):String(value));}
    });
    editor.baseline=JSON.stringify(draft);
    return {applied:true,preserved:true};
  }
  function proposalFormIdentity(){return {operation:$("pp-op").value,target:$("pp-target").value.trim(),kind:proposalKind};}
  function proposalFormMetadata(){return JSON.stringify([$("pp-title").value,$("pp-rationale").value,$("pp-sources").value]);}
  function editorDirty(editor,isClient){return JSON.stringify(editor.value)!==editor.baseline||(!isClient&&editor.baselineMetadata!==undefined&&editor.baselineMetadata!==proposalFormMetadata());}
  function readContext(isClient){return isClient?conditionIntent:JSON.stringify([proposalFormIdentity(),proposalFormMetadata()]);}
  function currentView(){var tab=document.querySelector('button.tab[aria-current="page"]');return tab&&tab.dataset.view;}
  function beginEditorRead(isClient){
    var editor=isClient?clientEditor:publicEditor,s=api.clientState();
    var token={editor:editor,isClient:isClient,profileId:s.profileId,ruleId:s.ruleId,draft:s.draft,
      proposal:editingProposal,view:currentView(),value:JSON.stringify(editor.value),context:readContext(isClient)};
    editor.pendingRead=token;return token;
  }
  function editorReadCurrent(token,box){
    var s=api.clientState();
    if(token.editor!==(token.isClient?clientEditor:publicEditor)||token.editor.pendingRead!==token
      ||token.profileId!==s.profileId||token.ruleId!==s.ruleId||token.draft!==s.draft||token.view!==currentView()
      ||(!token.isClient&&token.proposal!==editingProposal)){return false;}
    if(token.value!==JSON.stringify(token.editor.value)||token.context!==readContext(token.isClient)){
      status(box,"读取期间有新的填写，未覆盖当前内容；请核对后重新载入。",true);return false;
    }
    return true;
  }
  function endEditorRead(token){if(token.editor.pendingRead===token){delete token.editor.pendingRead;}}
  Editor.prototype.metadata=function(path){return this.meta.fields.find(function(f){return f.path===path;})||this.meta.fields.find(function(f){return f.path===path.replace(/\[\]$/ , "");})||{label:path.split(".").pop(),description:"此扩展字段含义尚未确认，原值保留，需核对原始定义。",conditional_required:[]};};
  var referenceKindNames={clause_module:"条款模块",principle:"类型原则",domain_principle:"大类原则",client_rule:"本客户规则"};
  function typeOptions(){return framework.domains.flatMap(function(d){return d.types.map(function(t){return {value:t.type_id,label:d.title+" · "+t.title};});});}
  Editor.prototype.referenceCatalog=function(){
    var client=this.meta.kind==="client_rule";
    return (this.meta.vocabulary.rule_reference_options||[]).map(function(o){return Object.assign({},o,{value:client?(o.runtime_value||o.value):o.value});}).concat(this.clientReferences||[]);
  };
  Editor.prototype.referenceOptions=function(path,contextType){
    var value=this.value,own=value.module_id||value.doctrine_id||value.domain_doctrine_id||value.client_rule_id;
    var kinds=this.metadata(path).reference_kinds||[],types=value.type_ids||(value.type_id?[value.type_id]:[]);
    return this.referenceCatalog().filter(function(o){
      if(o.value===own||!kinds.includes(o.kind)){return false;}
      if(contextType){return (o.type_ids||[]).includes(contextType);}
      if(path==="linked_module_ids"){return (o.type_ids||[]).includes(value.type_id);}
      if(path==="derived_principle_ids"||path==="derived_module_ids"||path==="module_index[].module_ids"){return (o.domain_codes||[]).includes(value.domain_code);}
      if(path==="dependencies"&&types.length){return value.dependencies_by_type?types.some(function(t){return (o.type_ids||[]).includes(t);}):types.every(function(t){return (o.type_ids||[]).includes(t);});}
      return true;
    });
  };
  function referenceLabel(item){return item.label+" · "+(referenceKindNames[item.kind]||item.kind)+(item.runtime_enabled?"":"（维护内容，未启用）");}
  Editor.prototype.referenceList=function(parent,obj,key,path,contextType,onChange,readOnly){
    var self=this,value=obj[key],label=this.metadata(path).label,box=element("div",null,"aw-reference-list");box.dataset.referencePath=path;parent.appendChild(box);
    if(!Array.isArray(value)){box.appendChild(element("p","原有关联格式尚未识别，完整保留；请先核对，未自动改写。","warning"));box.appendChild(element("pre",JSON.stringify(value,null,2)));return;}
    var selected=element("div"),search=element("input"),choices=element("select"),add;
    search.type="search";search.placeholder="按规则名称搜索";search.setAttribute("aria-label","搜索"+label);
    choices.multiple=true;choices.size=5;choices.setAttribute("aria-label","选择"+label);
    box.appendChild(selected);
    function changed(){if(onChange){onChange();}self.render();}
    function draw(){
      var catalog=self.referenceCatalog(),allowed=self.referenceOptions(path,contextType),query=search.value.trim().toLowerCase();api.clear(selected);api.clear(choices);
      value.forEach(function(ref,index){
        var item=catalog.find(function(o){return o.value===ref;}),row=element("div",null,"aw-reference-item");
        row.appendChild(element("p",item?referenceLabel(item):"未登记的原有引用："+String(ref),item?"":"warning"));
        if(item&&!allowed.some(function(o){return o.value===ref;})){row.appendChild(element("p","原有引用不符合当前对象种类或适用位置，已保留；需核对后调整。","warning"));}
        var detail=element("details");detail.appendChild(element("summary","查看关联编号"));detail.appendChild(element("code",String(ref)));row.appendChild(detail);
        if(!readOnly){row.appendChild(button("移除此关联",function(){value.splice(index,1);changed();}));}selected.appendChild(row);
      });
      if(!value.length){selected.appendChild(element("p","当前未声明关联。","hint"));}
      allowed.filter(function(o){return !value.includes(o.value)&&(!query||referenceLabel(o).toLowerCase().includes(query));}).forEach(function(o){option(choices,o.value,referenceLabel(o));});
      if(self.clientReferenceError){selected.appendChild(element("p",self.clientReferenceError,"warning"));}
      if(api.isReadOnly()){box.querySelectorAll("input,textarea,select,button").forEach(function(e){e.disabled=true;});}
    }
    if(!readOnly){
      box.appendChild(search);box.appendChild(choices);
      add=button("添加所选关联",function(){var refs=Array.from(choices.selectedOptions).map(function(o){return o.value;});if(!refs.length){return;}refs.forEach(function(ref){if(!value.includes(ref)){value.push(ref);}});changed();});box.appendChild(add);
      search.addEventListener("input",draw);
    }else{box.appendChild(element("p","此处是逐类型依赖的汇总。请在下方逐类型编辑，并核对汇总是否一致。","hint"));}
    this.referenceWidgets.push(draw);draw();
  };
  function dependencyUnion(mapping){if(!mapping||typeof mapping!=="object"||Array.isArray(mapping)||Object.values(mapping).some(function(v){return !Array.isArray(v);})){return null;}return Array.from(new Set(Object.values(mapping).flat()));}
  function sameReferences(a,b){return Array.isArray(a)&&Array.isArray(b)&&a.length===b.length&&a.every(function(x){return b.includes(x);});}
  Editor.prototype.typeDependencies=function(parent,obj,key){
    var self=this,mapping=obj[key],box=element("div",null,"aw-type-dependencies");parent.appendChild(box);
    if(!mapping||typeof mapping!=="object"||Array.isArray(mapping)){box.appendChild(element("p","原有逐类型依赖格式尚未识别，完整保留；未自动改写。","warning"));box.appendChild(element("pre",JSON.stringify(mapping,null,2)));return;}
    var types=typeOptions(),ownTypes=this.value.type_ids||(this.value.type_id?[this.value.type_id]:[]),initialUnion=dependencyUnion(mapping);
    function changed(){var next=dependencyUnion(mapping);if(next&&sameReferences(self.value.dependencies,initialUnion)){self.value.dependencies=next;}}
    box.appendChild(element("p","每个适用合同类型分别选择前置模块，明确无依赖时保留空列表。既有未知类型或引用不会被自动删除。","hint"));
    Object.keys(mapping).forEach(function(typeId){
      var entry=element("section",null,"aw-type-dependency"),known=types.find(function(t){return t.value===typeId;});entry.dataset.dependencyType=typeId;
      entry.appendChild(element("h4",known?known.label:"未登记的原有合同类型："+typeId));
      if(!known||!ownTypes.includes(typeId)){entry.appendChild(element("p","该原有位置尚未登记或不在当前适用类型中，已保留；请核对，不会自动调整适用范围。","warning"));}
      self.referenceList(entry,mapping,typeId,"dependencies_by_type",typeId,changed,false);
      entry.appendChild(button("移除此类型的依赖配置",function(){if((!Array.isArray(mapping[typeId])||mapping[typeId].length)&&!window.confirm("移除此类型将同时移除其依赖配置，是否继续？")){return;}delete mapping[typeId];changed();self.render();}));box.appendChild(entry);
    });
    var choose=element("select");choose.setAttribute("aria-label","新增依赖配置的合同类型");option(choose,"","请选择适用合同类型");
    types.filter(function(t){return ownTypes.includes(t.value)&&!Object.prototype.hasOwnProperty.call(mapping,t.value);}).forEach(function(t){option(choose,t.value,t.label);});box.appendChild(choose);
    box.appendChild(button("添加此类型的依赖配置",function(){if(!choose.value){return;}mapping[choose.value]=[];changed();self.render();}));
    if(!ownTypes.length){box.appendChild(element("p","请先选择本模块的适用合同类型，再逐类型配置依赖。","hint"));}
    var union=dependencyUnion(mapping);
    if(!sameReferences(this.value.dependencies,union)){
      box.appendChild(element("p","逐类型依赖与汇总尚不一致。原有汇总已保留，核对后可明确同步；只修改标题不会改写依赖。","warning"));
      if(union){box.appendChild(button("按逐类型配置同步依赖汇总",function(){var removed=(self.value.dependencies||[]).filter(function(ref){return !union.includes(ref);});if(removed.length&&!window.confirm("同步会移除仅存在于原汇总中的引用："+removed.join("、")+"。确认已核对？")){return;}self.value.dependencies=union;self.render();}));}
    }
  };
  function loadClientReferences(editor,profileId){
    if(!profileId){return;}
    api.call("GET","/api/v1/clients/"+encodeURIComponent(profileId)+"/knowledge").then(function(r){
      if(clientEditor!==editor||api.clientState().profileId!==profileId){return;}
      if(!r.body.success){editor.clientReferenceError=api.errorText(r.body,"当前客户关联规则未能载入，原值已保留：");}
      else{var byId={};(r.body.result.active_rules||[]).forEach(function(rule){byId[rule.client_rule_id]={value:rule.client_rule_id,label:rule.title+"（启用快照）",kind:"client_rule",runtime_enabled:true};});
        (r.body.result.drafts||[]).forEach(function(rule){var old=byId[rule.client_rule_id];byId[rule.client_rule_id]={value:rule.client_rule_id,label:rule.title+(old?"（另有启用快照，请分别核对）":"（维护草稿）"),kind:"client_rule",runtime_enabled:false};});editor.clientReferences=Object.values(byId);}
      editor.referenceWidgets.forEach(function(draw){draw();});
    });
  }
  Editor.prototype.render=function(){
    var self=this;this.referenceWidgets=[];api.clear(this.container);this.container.classList.add("aw-editor");
    this.meta.groups.forEach(function(group){
      var fields=self.meta.fields.filter(function(f){return f.path.indexOf(".")<0 && f.path.indexOf("[]")<0 && f.group===group.id;});
      if(!fields.length){return;}var section=element("details",null,"aw-group");section.open=group.id!=="validation";section.appendChild(element("summary",group.title));section.appendChild(element("p",group.description,"hint"));
      fields.forEach(function(field){self.field(section,self.value,field.path,field.path,field.schema,field.required);});self.container.appendChild(section);
    });
    var unknown=Object.keys(this.value).filter(function(k){return !self.meta.schema.properties[k];});
    if(unknown.length){var saved=element("details",null,"aw-group");saved.appendChild(element("summary","原有扩展字段（保留原值）"));saved.appendChild(element("p","这些字段尚无已确认的业务说明，原样保留，需核对原始定义；不自动推定其含义。","hint"));unknown.forEach(function(k){saved.appendChild(element("strong",k));saved.appendChild(element("pre",JSON.stringify(self.value[k],null,2)));});this.container.appendChild(saved);}
    if(api.isReadOnly()){this.container.querySelectorAll("input,textarea,select,button").forEach(function(e){e.disabled=true;});}
  };
  Editor.prototype.field=function(parent,obj,key,path,schema,required){
    var self=this;schema=resolved(schema,this.meta.schema);var meta=this.metadata(path),value=obj[key],box=element("div",null,"aw-field");
    var semantic=this.meta.kind==="client_rule"&&semanticCondition(this.value);
    if(semantic&&path==="condition.natural_language_note"){required=true;}
    var top=path.split(".")[0],managed=this.metadata(top).managed_by,locked=managed==="reviewer"||managed==="system";
    var optionalScope=/^scope\.(domain_codes|type_ids|roles|scene_tags|contract_stages|clause_groups)$/.test(path);
    var label=element("div",meta.label,"aw-field-label");label.appendChild(element("span",optionalScope?"按需限定":locked?(managed==="system"?"系统生成":"审核操作记录"):(required?"必填":((meta.conditional_required||[]).length?"条件必填":"可选")),"aw-tag"));box.appendChild(label);
    box.dataset.fieldPath=path;
    if(meta.description){box.appendChild(element("p",meta.description,"hint"));}
    if(meta.input_format){box.appendChild(element("small","填写格式："+meta.input_format,"aw-example aw-format"));}
    (meta.conditional_required||[]).forEach(function(t){box.appendChild(element("p",t,"aw-condition-note"));});
    if(locked){box.appendChild(element("pre",value===undefined?"由实际操作产生":(typeof value==="object"?JSON.stringify(value,null,2):String(value)),"aw-managed"));parent.appendChild(box);return;}
    if(path==="condition.assessment_mode"){
      box.appendChild(element("p",texts[value||"structured"]||String(value)));
      box.appendChild(element("small","在上方“适用条件的填写状态”中选择；切换时保留原有条件文字和条目。","hint"));parent.appendChild(box);return;
    }
    if((semantic&&path==="condition.mode")||(this.meta.kind==="client_rule"&&path==="condition.human_confirmation_required")){
      box.appendChild(element("p",path==="condition.mode"?"自然语言条件按完整文字判断；原有组合方式已保留。":"历史提示："+(value?"原规则要求人工确认。":"原规则未单独要求人工确认。")));
      if(path==="condition.human_confirmation_required"){box.appendChild(element("p","本次适用性由 AI 结合合同全文判断并以真实批注留痕，不要求重复点击确认；正式审核和启用独立保留。","hint"));}parent.appendChild(box);return;
    }
    if(semantic&&path==="condition.clauses"){
      if(!Array.isArray(value)||value.length){box.appendChild(element("p","原有结构化条件已保留；请核对后移除条目，或切换回结构化方式，不能混用。","warning"));}
      else{box.appendChild(element("p","由 AI 阅读下方完整条件说明，无需填写事实编号。","hint"));parent.appendChild(box);return;}
    }
    if(!required){var toggle=element("label",null,"aw-enable"),check=document.createElement("input");check.type="checkbox";check.checked=value!==undefined;toggle.appendChild(check);toggle.appendChild(document.createTextNode("填写此项"));box.appendChild(toggle);check.addEventListener("change",function(){if(check.checked){obj[key]=initial(schema,self.meta.schema);}else{delete obj[key];}self.render();});if(value===undefined){parent.appendChild(box);return;}}
    if(path==="dependencies_by_type"){this.typeDependencies(box,obj,key);parent.appendChild(box);return;}
    if((meta.reference_kinds||[]).length){this.referenceList(box,obj,key,path,null,null,path==="dependencies"&&Boolean(this.value.dependencies_by_type));parent.appendChild(box);return;}
    var type=Array.isArray(schema.type)?schema.type.filter(function(t){return t!=="null";})[0]:schema.type;
    var declaration=null,operators=null;
    if(path==="effect.value"){declaration=this.meta.vocabulary.effect_targets.find(function(t){return t.target_key===obj.target_key;});}
    if(path==="condition.clauses[].value"){declaration=this.meta.vocabulary.fact_keys.find(function(t){return t.fact_key===obj.fact_key;});if(obj.operator==="in"||obj.operator==="not_in"){type="json_list";}else if(obj.operator==="exists"){delete obj[key];box.appendChild(element("p","“有该事实”无需填比较值。","hint"));parent.appendChild(box);return;}}
    if(declaration && type!=="json_list"){type=declaration.value_type;}
    if(path==="condition.clauses[].operator"){var fact=this.meta.vocabulary.fact_keys.find(function(t){return t.fact_key===obj.fact_key;});if(fact){operators=fact.allowed_operators;}}
    if(value===null && Array.isArray(schema.type) && schema.type.includes("null") && (type==="object"||schema.properties)){box.appendChild(element("p","原值为尚未确定（null），已保留。","hint"));box.appendChild(button("填写内容",function(){obj[key]=initial(Object.assign({},schema,{type:"object"}),self.meta.schema);self.render();}));parent.appendChild(box);return;}
    if(schema.properties||type==="object"){
      if(value===null||typeof value!=="object"||Array.isArray(value)){value={};obj[key]=value;}
      Object.keys(schema.properties||{}).forEach(function(k){self.field(box,value,k,path+"."+k,schema.properties[k],(schema.required||[]).includes(k));});
      if(!schema.properties){var extra=element("textarea");extra.value=JSON.stringify(value,null,2);extra.rows=4;extra.setAttribute("aria-label",meta.label);box.appendChild(extra);extra.addEventListener("change",function(){try{var v=JSON.parse(extra.value);if(!v||typeof v!=="object"||Array.isArray(v)){throw Error();}obj[key]=v;extra.setCustomValidity("");}catch(e){extra.setCustomValidity("请填写完整的对象格式；原值已保留。");extra.reportValidity();}});}
    }else if(type==="array"){
      if(!Array.isArray(value)){value=[];obj[key]=value;}
      value.forEach(function(v,i){var row=element("div",null,"aw-array-item");var itemSchema=resolved(schema.items||{},self.meta.schema);self.field(row,value,i,path+"[]",itemSchema,true);row.appendChild(button("移除此项",function(){value.splice(i,1);self.render();}));box.appendChild(row);});
      box.appendChild(button("＋ 添加一项",function(){value.push(initial(schema.items||{},self.meta.schema));self.render();}));
      if(!value.length){box.appendChild(element("small","当前没有条目。请根据实际业务确定是否应补充。"));}
    }else{
      var options=vocab(this.meta,path),input;
      if(type==="boolean"||schema.enum||options||(declaration&&declaration.enum_values)||operators){input=element("select");option(input,"","请选择");if(type==="boolean"){options=[{value:"true",label:"是"},{value:"false",label:"否"}];}else if(!options){options=(operators||(declaration&&declaration.enum_values)||schema.enum).map(function(v){return {value:v,label:texts[v]||v};});}
        options.forEach(function(o){option(input,String(o.value),o.label);});if(value!==undefined&&value!==null&&value!==""&&!options.some(function(o){return String(o.value)===String(value);})){option(input,String(value),String(value)+"（未登记的原值，保留待核对）");box.appendChild(element("p","原有值不在当前选项中，已完整保留；正式保存或启用仍须通过原有校验。","warning"));}input.value=value===undefined||value===null?"":String(value);
      }else{input=element(type==="number"||type==="integer"?"input":"textarea");if(input.tagName==="INPUT"){input.type="number";input.step=type==="integer"?"1":"any";}else{input.rows=typeof value==="string"&&value.length>100?4:2;}input.value=value===undefined||value===null?"":(typeof value==="object"?JSON.stringify(value):String(value));}
      input.setAttribute("aria-label",meta.label);input.dataset.fieldPath=path;box.appendChild(input);
      input.addEventListener("input",function(){var val=input.value;if(type==="boolean"){obj[key]=val===""?null:val==="true";}else if(type==="number"||type==="integer"){obj[key]=val===""?null:Number(val);}else if(type==="json_list"||(!schema.type&&!schema.enum&&!schema.const)){try{obj[key]=JSON.parse(val);}catch(e){obj[key]=val;}}else{obj[key]=val;}
        if(path==="effect.target_key"||path==="condition.clauses[].fact_key"||path==="condition.clauses[].operator"){self.render();}
        if(path==="type_id"||path==="type_ids[]"||path==="domain_code"){deriveIdentity(self.value,self.meta.kind);self.render();}
      });
      if(meta.example!==undefined){box.appendChild(element("small","示例："+(typeof meta.example==="object"?JSON.stringify(meta.example):String(meta.example))+"（仅供填写参考）","aw-example"));}
    }
    parent.appendChild(box);
  };
  function freshClient(profileId){return {schema_version:"1.0",client_rule_id:id("cr_"),client_profile_id:profileId||"",version:"1.0.0",title:"",rule_kind:"requirement",normative_level:"mandatory",scope:{applies_to_all:false,domain_codes:[],type_ids:[],roles:[],scene_tags:[],contract_stages:[],clause_groups:[]},condition:{mode:"all",clauses:[],human_confirmation_required:false},effect:{action:"require",target_key:"output.clause_instruction",value:""},source_refs:[],approval_status:"candidate",activation_status:"inactive",review:{lawyer_approval_evidence_id:null,lawyer_approved_by:null,lawyer_approved_at:null,legal_check_status:"not_checked",legal_checked_at:null,regression_evidence_ids:[]}};}
  function loadClientSources(editor,profileId){
    if(!profileId){return;}
    api.call("GET","/api/v1/authoring/schema?kind=client_rule&client_profile_id="+encodeURIComponent(profileId)).then(function(r){
      if(editor!==clientEditor||profileId!==api.clientState().profileId||!r.body.success){return;}
      editor.meta=r.body.result;
      // Preserve focus and current typing; the next normal editor render also
      // uses the newly registered sources. Never put private options in globals.
      if(!editor.container.contains(document.activeElement)){editor.render();}
    });
  }
  function loadClient(draft){if(!ready){return;}if($("client-manual-editor")){$("client-manual-editor").open=Boolean(draft);}clientProgress=null;conditionIntent=draft?(semanticCondition(draft)?"semantic":((draft.condition.clauses||[]).length?"conditional":"unconditional")):"";var state=api.clientState(),container=$("client-authoring");api.clear(container);var help=element("p","按各组填写；条件、依赖、有效期和来源都会原样保存。修改实质内容后须重新审核与测试。","hint");container.appendChild(help);
    progressControls(container,true);
    var intent=selectField(container,"适用条件的填写状态",[{value:"",label:"尚未确定（可保存填写进度）"},{value:"semantic",label:"由 AI 结合合同全文判断"},{value:"conditional",label:"有结构化事实条件，按下方条目填写"},{value:"unconditional",label:"已确认：在适用范围内不另设事实条件"}],conditionIntent);intent.id="aw-condition-intent";intent.disabled=api.isReadOnly();intent.addEventListener("change",function(){conditionIntent=intent.value;var condition=clientEditor.value.condition;if(conditionIntent==="semantic"){condition.assessment_mode="semantic";if(condition.natural_language_note===undefined){condition.natural_language_note="";}}else if(conditionIntent&&condition.assessment_mode==="semantic"){condition.assessment_mode="structured";}clientEditor.render();});
    if(draft){container.appendChild(button("创建下一版本草稿",function(){var parts=clientEditor.value.version.split(".").map(Number);parts[2]+=1;clientEditor.value.version=parts.join(".");clientEditor.render();status($("rule-status"),"版本号已提升，请核对内容并保存。");}));}
    var box=element("div");container.appendChild(box);clientEditor=new Editor(box,copy(schemas.client_rule),draft||freshClient(state.profileId));loadClientSources(clientEditor,state.profileId);loadClientReferences(clientEditor,state.profileId);cases=[];renderTests();if(batch&&batch.profileId!==state.profileId){batch=null;}renderBatch();document.dispatchEvent(new CustomEvent("studio-client-editor",{detail:{profileId:state.profileId}}));}
  function getClientRule(profileId){if(!clientEditor){throw Error("字段说明尚未加载，请稍候。");}var rule=copy(clientEditor.value);rule.client_profile_id=profileId;
    if(!conditionIntent){throw Error("请明确事实条件；尚未确定时可以先保存填写进度。");}
    if((conditionIntent==="semantic")!==semanticCondition(rule)){throw Error("适用条件的填写状态与判断方式不一致，请重新核对。");}
    if(conditionIntent==="semantic"&&(!rule.condition.natural_language_note||!rule.condition.natural_language_note.trim())){throw Error("请完整填写由 AI 结合合同全文判断的条件说明。");}
    if(conditionIntent==="semantic"&&rule.condition.clauses.length){throw Error("原有结构化条件已保留，请核对后移除条目或切换回结构化方式，不能混用。");}
    if(conditionIntent==="conditional"&&!rule.condition.clauses.length){throw Error("已选择有事实条件，请至少填写一条条件。");}
    if(conditionIntent==="unconditional"&&rule.condition.clauses.length){throw Error("已有事实条件，不能同时声明不另设条件。请选择有事实条件，或核对后移除条件。");}
    if(rule.scope.applies_to_all && ["domain_codes","type_ids","roles","scene_tags","contract_stages","clause_groups"].some(function(k){return rule.scope[k].length;})){throw Error("全部适用与限定范围同时存在，请清空限定项或取消全部适用。");}
    previewEditor(true);return rule;}
  function deriveIdentity(value,kind){
    var all=framework.domains.flatMap(function(d){return d.types;});
    if(kind==="principle"){var t=all.find(function(t){return t.type_id===value.type_id;});if(t){value.type_code=t.type_code;}}
    if(kind==="clause_module"){value.type_codes=(value.type_ids||[]).map(function(k){var t=all.find(function(t){return t.type_id===k;});return t?t.type_code:null;}).filter(Boolean);}
    if(kind==="domain_principle"){var d=framework.domains.find(function(d){return d.domain_code===value.domain_code;});if(d){value.domain_doctrine_id="domain-doctrine-"+d.domain_code.toLowerCase();value.domain_name=d.title;value.coverage={total_type_count:d.types.length,supported_type_count:d.types.filter(function(t){return t.runtime_enabled;}).length,coverage_status:d.types.every(function(t){return t.runtime_enabled;})?"strong":d.types.some(function(t){return t.runtime_enabled;})?"limited":"gap",supported_type_ids:d.types.filter(function(t){return t.runtime_enabled;}).map(function(t){return t.type_id;}),unsupported_type_ids:d.types.filter(function(t){return !t.runtime_enabled;}).map(function(t){return t.type_id;})};}}
  }
  function publicSeed(kind,source,creating){var meta=schemas[kind],value=Object.assign(initial(meta.schema,meta.schema),copy(source)||{});if(creating){["doctrine_id","module_id"].forEach(function(k){if(meta.schema.properties[k]){value[k]=id(k==="module_id"?"mod-":"doctrine-");}});value.version="0.1.0";}
    else if(source&&/^\d+\.\d+\.\d+$/.test(source.version||"")){var version=source.version.split(".").map(Number);version[2]+=1;value.version=version.join(".");}
    ["reviewer","reviewed_at","legal_checked_at"].forEach(function(k){if(meta.schema.properties[k]){value[k]=null;}});["activated_at","activation_basis"].forEach(function(k){delete value[k];});
    if(meta.schema.properties.approval_status){value.approval_status="candidate";}if(meta.schema.properties.activation_status){value.activation_status="inactive";}if(meta.schema.properties.content_status){value.content_status="studio_draft_unreviewed";}deriveIdentity(value,kind);return value;}
  function setPublic(kind,source,creating){editingProposal=null;publicProgress=null;proposalKind=kind;var container=$("public-editor");publicEditor=new Editor(container,schemas[kind],publicSeed(kind,source,creating));$("aw-kind").value=kind;}
  function initPublic(){var c=$("public-authoring");c.appendChild(element("h3","规则种类与完整内容"));var select=element("select");select.id="aw-kind";select.setAttribute("aria-label","新增规则种类");["domain_principle","principle","clause_module"].forEach(function(k){option(select,k,schemas[k].title);});select.value=proposalKind;c.appendChild(select);c.appendChild(element("p","从规则详情进入修改，会载入完整原文；新增时根据所选种类逐项填写。系统字段和真实审核记录分开显示。","hint"));progressControls(c,false);var ed=element("div");ed.id="public-editor";c.appendChild(ed);select.addEventListener("change",function(){if(publicEditor&&editorDirty(publicEditor,false)&&!window.confirm("切换种类会清空此处未保存的内容，是否继续？")){select.value=proposalKind;return;}setPublic(select.value,null,true);$("pp-op").value="create";$("pp-target").value="";});setPublic(proposalKind,null,true);$("pp-op").value="create";$("pp-target").value="";$("pp-sources").value="";
    c.appendChild(button("检查字段与业务内容缺口",function(){api.call("POST","/api/v1/authoring/validate",{kind:proposalKind,rule:publicEditor.value}).then(function(r){var box=$("authoring-validation");api.clear(box);if(!r.body.success){box.appendChild(element("p",api.errorText(r.body,"检查失败：")));return;}box.appendChild(element("p",r.body.result.content_ready?"填写内容已通过结构检查，仍须审核与测试。":"以下字段仍需补充或核对："));r.body.result.issues.forEach(function(i){box.appendChild(element("p",i.label+" · "+i.message+(i.kind==="governance"?"（由后续实际操作产生）":"")));});box.appendChild(element("p",r.body.result.notice,"hint"));});}));var validation=element("div");validation.id="authoring-validation";c.appendChild(validation);
    var load=button("载入现有规则完整内容",function(){var target=$("pp-target").value.trim();if(!target){status($("proposal-status"),"请先填写目标规则编号，或从规则总览进入修改。",true);return;}var token=beginEditorRead(false);api.call("GET","/api/v1/rules/"+encodeURIComponent(target)).then(function(r){if(!editorReadCurrent(token,$("proposal-status"))){return;}if(!r.body.success){status($("proposal-status"),api.errorText(r.body,"载入失败："),true);return;}author({kind:r.body.result.authoring_kind,body:r.body.result.body,node:r.body.result,operation:"supersede"});}).finally(function(){endEditorRead(token);});});$("pp-target").parentNode.appendChild(load);
  }
  function author(detail){if(!ready){pendingAuthor=detail;return;}if($("public-manual-editor")){$("public-manual-editor").open=true;}if(detail.kind==="client_rule"){api.showView("clients");loadClient(null);$("client-manual-editor").open=true;var scope=clientEditor.value.scope;["type_ids","domain_codes"].forEach(function(k){if(detail.body[k]){scope[k]=copy(detail.body[k]);}});if(detail.body.type_id){scope.type_ids=[detail.body.type_id];}if(detail.body.domain_code){scope.domain_codes=[detail.body.domain_code];}clientEditor.render();return;}
    if(!schemas[detail.kind]||typeof detail.body!=="object"){return;}api.showView("proposals");setPublic(detail.kind,detail.body,detail.operation==="create");$("pp-op").value=detail.operation;$("pp-target").value=detail.operation==="create"?"":detail.node.node_id;$("pp-title").value=detail.operation==="create"?"":("修改："+detail.node.title);$("pp-sources").value=(detail.body.source_ids||detail.body.source_refs||[]).join(",");status($("proposal-status"),"完整内容已载入。请填写修改理由并核对各字段。");}
  function saveProposal(){
    if(publicEditor.pendingRequest){status($("proposal-status"),"当前建议正在保存，请等待后再提交；期间可以继续填写。",true);return;}
    previewEditor(false);var op=$("pp-op").value,target=$("pp-target").value.trim();if(op!=="create"&&!target){status($("proposal-status"),"修改或停用须指定现有规则。",true);return;}
    var payload={operation:op,layer:proposalKind==="domain_principle"?"domain":"type",target_rule_id:op==="create"?null:target,title:$("pp-title").value.trim(),rationale:$("pp-rationale").value.trim(),source_refs:$("pp-sources").value.split(/[,，\n]/).map(function(s){return s.trim();}).filter(Boolean),proposed_rule:op==="deprecate"?null:copy(publicEditor.value)};
    if(editingProposal&&(editingProposal.operation!==op||editingProposal.target_rule_id!==payload.target_rule_id)){status($("proposal-status"),"现有建议的对象与操作不能在原记录内改变，请从目录另建建议。",true);return;}
    var endpoint="/api/v1/public-proposals";if(editingProposal){endpoint+="/"+encodeURIComponent(editingProposal.proposal_id)+"/content";payload.expected_content_sha256=editingProposal.content_sha256;}
    var token={editor:publicEditor,proposal:editingProposal,identity:JSON.stringify(proposalFormIdentity()),value:JSON.stringify(publicEditor.value),metadata:proposalFormMetadata()};
    publicEditor.pendingRequest=token;
    api.call("POST",endpoint,payload).then(function(r){
      if(r.body.success){document.dispatchEvent(new CustomEvent("studio-authoring-saved",{detail:{kind:"public_proposal"}}));}
      if(token.editor!==publicEditor||token.proposal!==editingProposal||token.identity!==JSON.stringify(proposalFormIdentity())){return;}
      if(!r.body.success){status($("proposal-status"),api.errorText(r.body,"保存失败："),true);return;}
      var changed=token.value!==JSON.stringify(publicEditor.value)||token.metadata!==proposalFormMetadata();
      editingProposal=r.body.result;publicEditor.baseline=token.value;publicEditor.baselineMetadata=token.metadata;
      status($("proposal-status"),"完整修改建议已保存，尚未参与正式审查。"+(changed?"等待期间的填写已保留，尚未保存；请核对后再次保存。":""));
      api.loadProposals();showProposal(r.body.result.proposal_id);
    }).finally(function(){if(token.editor.pendingRequest===token){delete token.editor.pendingRequest;}});
  }
  function selectField(parent,label,options,value){var wrap=element("label",null,"field");wrap.appendChild(element("span",label));var s=element("select");s.setAttribute("aria-label",label);options.forEach(function(o){option(s,o.value,o.label);});s.value=value===undefined?"":String(value);wrap.appendChild(s);parent.appendChild(wrap);return s;}
  function inputField(parent,label,value){var wrap=element("label",null,"field");wrap.appendChild(element("span",label));var i=element("input");i.setAttribute("aria-label",label);i.value=value||"";wrap.appendChild(i);parent.appendChild(wrap);return i;}
  function addCase(){var d=api.clientState().draft;if(!d){status($("lc-status"),"请先保存并选择规则，再添加测试场景。",true);return;}var s=d.scope||{};cases.push({case_id:"场景 "+(cases.length+1),input:{primary_type_id:(s.type_ids||[])[0]||"",primary_domain_code:(s.domain_codes||[])[0]||"",confirmed_domain_code:(s.domain_codes||[])[0]||"",our_role:(s.roles||[])[0]||"",contract_stage:(s.contract_stages||[])[0]||"",scene_tags:copy(s.scene_tags||[]),secondary_type_ids:[],facts:{},classification_status:"high"},expected:{},effect:copy(d.effect)});renderTests();}
  function renderTests(){var c=$("scenario-workbench");api.clear(c);c.appendChild(element("h4","场景测试"));c.appendChild(element("p","先写预期，再运行。此处验证哪些客户规则进入 AI 审查待办，不判断本合同最终是否适用；业务条件须由审查 AI 阅读全文、说明依据并在合同中批注。请覆盖不同预筛结果及明确的审查范围排除。","hint"));c.appendChild(button("＋ 添加测试场景",addCase));
    cases.forEach(function(test,index){var box=element("details",null,"aw-test");box.open=true;box.appendChild(element("summary",test.case_id));var name=inputField(box,"场景名称",test.case_id);name.addEventListener("input",function(){test.case_id=name.value;});var type=selectField(box,"合同类型",[{value:"",label:"请选择"}].concat(vocab(schemas.client_rule,"scope.type_ids")),test.input.primary_type_id);type.addEventListener("change",function(){test.input.primary_type_id=type.value;framework.domains.forEach(function(d){if(d.types.some(function(t){return t.type_id===type.value;})){test.input.primary_domain_code=d.domain_code;test.input.confirmed_domain_code=d.domain_code;}});});
      var role=selectField(box,"我方角色",[{value:"",label:"请选择"}].concat(schemas.client_rule.vocabulary.role_options),test.input.our_role);role.addEventListener("change",function(){test.input.our_role=role.value;});var stage=selectField(box,"合同阶段",[{value:"",label:"未提供"}].concat(schemas.client_rule.vocabulary.stage_options),test.input.contract_stage);stage.addEventListener("change",function(){test.input.contract_stage=stage.value;});var scene=inputField(box,"场景标签（逗号分隔，可用原规则的标签）",test.input.scene_tags.join(","));scene.addEventListener("input",function(){test.input.scene_tags=scene.value.split(/[,，]/).map(function(s){return s.trim();}).filter(Boolean);});
      schemas.client_rule.vocabulary.fact_keys.forEach(function(f){var val=inputField(box,f.description+"（留空表示未提供）",test.input.facts[f.fact_key]===undefined?"":String(test.input.facts[f.fact_key]));val.addEventListener("input",function(){if(!val.value.trim()){delete test.input.facts[f.fact_key];}else{test.input.facts[f.fact_key]=f.value_type==="number"?Number(val.value):val.value;}});});
      var yn=[{value:"",label:"请明确预期"},{value:"true",label:"是"},{value:"false",label:"否"}];["applies","human_confirmation_required"].forEach(function(k){var choice=selectField(box,k==="applies"?"预期交给 AI 审查这条规则":"预期需要人工确认",yn,test.expected[k]);choice.addEventListener("change",function(){if(!choice.value){delete test.expected[k];}else{test.expected[k]=choice.value==="true";}});});
      reviewScopeFields(box,test.input);var expected=element("p","交给 AI 审查时携带的处理要求："+(texts[test.effect.action]||test.effect.action)+"；作用目标："+test.effect.target_key,"hint");box.appendChild(expected);var value=inputField(box,"预期处理值（请核对后填写）",typeof test.effect.value==="object"?JSON.stringify(test.effect.value):String(test.effect.value));value.addEventListener("input",function(){try{test.effect.value=JSON.parse(value.value);}catch(e){test.effect.value=value.value;}});box.appendChild(button("移除此场景",function(){cases.splice(index,1);renderTests();}));c.appendChild(box);
    });var result=element("div");result.id="scenario-results";result.setAttribute("role","status");c.appendChild(result);}
  function runTests(){
    var s=api.clientState();
    if(!s.draft||!cases.length){status($("lc-status"),"请先选择已保存规则，并添加具有明确预期的测试场景。",true);return;}
    var request=[];
    for(var test of cases){
      if(typeof test.expected.applies!=="boolean"||typeof test.expected.human_confirmation_required!=="boolean"){status($("lc-status"),"每个场景都须明确两项预期，不能留空。",true);return;}
      var t=copy(test);if(t.expected.applies){t.expected.effect={action:t.effect.action,target_key:t.effect.target_key,value:t.effect.value};}delete t.effect;request.push(t);
    }
    var token=beginClientRequest("test");if(!token){return;}
    var submittedCases=JSON.stringify(cases);
    status($("lc-status"),"正在用已保存的规则执行测试……");
    api.call("POST","/api/v1/clients/"+encodeURIComponent(s.profileId)+"/client-rules/"+encodeURIComponent(s.ruleId)+"/test",{cases:request,expected_revision:s.draft.revision}).then(function(r){
      if(!clientRequestCurrent(token)){return;}
      if(!r.body.success){status($("lc-status"),api.errorText(r.body,"测试失败："),true);return;}
      var result=r.body.result,savedCases=cases,casesChanged=JSON.stringify(cases)!==submittedCases;
      var applied=applyClientResult(token,result.draft);if(!applied.applied){return;}
      cases=savedCases;renderTests();
      var message=result.passed?(result.release_check_recorded?"全部场景通过，已绑定规则交接与运行边界的测试证据；不代表已判断合同适用性。":"交接场景通过；不代表已判断合同适用性，还需完成规则审核后重新执行发布前测试。"):"部分场景未通过，请核对实际结果并修改规则或预期。";
      if(applied.preserved||casesChanged){message+=" 当前未保存的规则或新改的场景已保留，不在本次测试结论内。";}
      status($("lc-status"),message,!result.passed||applied.preserved||casesChanged);
      var box=$("scenario-results");result.report.cases.forEach(function(c){box.appendChild(element("h4",c.case_id+" · "+(c.passed?"通过":"未通过")));box.appendChild(element("pre","预期："+JSON.stringify(c.expected,null,2)+"\n实际："+JSON.stringify(c.actual,null,2)));(c.excluded||[]).forEach(function(e){box.appendChild(element("p",e.reason));});});box.appendChild(element("small","证据编号："+result.evidence_id));
    }).finally(function(){endClientRequest(token);});
  }
  function proposalKindFor(body){return body&&body.module_id?"clause_module":body&&body.domain_doctrine_id?"domain_principle":"principle";}
  async function showProposal(proposalId){
    currentProposal=proposalId;var base="/api/v1/public-proposals/"+encodeURIComponent(proposalId),rs=await Promise.all([api.call("GET",base),api.call("GET",base+"/diff"),api.call("GET",base+"/governance")]);
    if(currentProposal!==proposalId){return;}var box=$("proposal-diff");api.clear(box);api.clear($("proposal-audit"));if(rs.some(function(r){return !r.body.success;})){rs.filter(function(r){return !r.body.success;}).forEach(function(r){box.appendChild(element("li",api.errorText(r.body,"读取失败：")));});return;}
    var p=rs[0].body.result,d=rs[1].body.result,g=rs[2].body.result,kind=proposalKindFor(p.proposed_rule||d.current_body),meta=schemas[kind];
    $("proposal-detail-hint").textContent=p.title+" · "+g.status_label;
    (d.changed_fields||[]).forEach(function(k){var row=element("li");row.appendChild(element("strong",(meta.fields.find(function(f){return f.path===k;})||{}).label||k));row.appendChild(element("pre","修改前："+JSON.stringify((d.current_body||d.current||{})[k],null,2)+"\n修改后："+JSON.stringify((d.proposed||{})[k],null,2)));box.appendChild(row);});
    if(!(d.changed_fields||[]).length){box.appendChild(element("li",d.removes_current?"建议停用该规则。":"没有内容差异。"));}
    var review=element("li",null,"aw-governance");review.appendChild(element("h4","内容检查与人工确认"));
    review.appendChild(element("p","检查正文、字段结构、引用和依赖；此处未执行公共规则全量回归或合同交付验证，批准和导出均不会更新正式规则。","hint"));
    (g.gaps||[]).forEach(function(t){review.appendChild(element("p",t,"warning"));});
    var report=g.validation_evidence&&g.validation_evidence.report;if(report){(report.checks||[]).forEach(function(c){var labels={public_baseline:"公共基线",complete_content:"完整正文",schema:"字段结构",references:"引用来源",dependencies:"依赖关系"};review.appendChild(element("strong",(labels[c.check_id]||c.check_id)+" · "+(c.passed?"通过":"未通过")));(c.issues||[]).forEach(function(i){review.appendChild(element("p",typeof i==="string"?i:JSON.stringify(i)));});});}
    var statusBox=element("p");statusBox.id="proposal-governance-status";statusBox.setAttribute("role","status");
    async function act(suffix,payload){status(statusBox,"正在记录当前内容的操作……");var r=await api.call("POST",base+suffix,Object.assign({expected_content_sha256:p.content_sha256},payload||{}));if(currentProposal!==proposalId){return;}if(!r.body.success){status(statusBox,api.errorText(r.body,"操作未完成："),true);return;}api.loadProposals();await showProposal(proposalId);status($("proposal-governance-status"),suffix==="/validate"?(r.body.result.validation.passed?"正文、结构、引用和依赖检查通过；等待人工核对。":"检查发现问题，请修改建议内容后重试。"):suffix==="/approval"?"已记录操作者对本版内容及检查结果的确认；公共规则未启用。":"已同意导出本版建议；公共规则未启用。");}
    var check=button("运行正文、引用与依赖检查",function(){act("/validate");});check.disabled=api.isReadOnly();review.appendChild(check);
    if(!["accepted","rejected","deprecated"].includes(p.status)&&p.proposed_rule&&schemas[kind]){var edit=button("继续编辑本条建议",function(){setPublic(kind,p.proposed_rule,false);publicEditor.value=copy(p.proposed_rule);publicEditor.render();publicEditor.baseline=JSON.stringify(publicEditor.value);$("public-manual-editor").open=true;editingProposal=p;$("pp-op").value=p.operation;$("pp-target").value=p.target_rule_id||"";$("pp-title").value=p.title;$("pp-rationale").value=p.rationale;$("pp-sources").value=p.source_refs.join(",");status($("proposal-status"),"已载入本条建议。保存实质修改会撤下原确认资格。 ");});edit.disabled=api.isReadOnly();review.appendChild(edit);}
    var operator=inputField(review,"本次核对人姓名","");operator.disabled=api.isReadOnly();var confirmLabel=element("label",null,"aw-enable"),confirm=element("input");confirm.type="checkbox";confirm.disabled=api.isReadOnly();confirmLabel.appendChild(confirm);confirmLabel.appendChild(document.createTextNode("我已阅读完整改稿、依据和本版检查结果，并确认本次操作"));review.appendChild(confirmLabel);
    var approve=button("确认本版公共建议",function(){if(!confirm.checked||!operator.value.trim()){status(statusBox,"请填写真实核对人姓名，并明确勾选已核对当前内容。",true);return;}act("/approval",{confirmation:true,actor:operator.value.trim()});});approve.disabled=api.isReadOnly()||!g.validation_valid||["accepted","rejected","deprecated"].includes(p.status);review.appendChild(approve);
    var accept=button("同意导出已核对建议",function(){if(!confirm.checked||!operator.value.trim()){status(statusBox,"请填写真实核对人姓名，并明确勾选已核对当前内容。",true);return;}act("/transition",{to_status:"accepted",actor:operator.value.trim()});});accept.disabled=api.isReadOnly()||!g.ready_for_governance_export||p.status==="accepted";review.appendChild(accept);
    review.appendChild(element("p","姓名和确认属于本机操作者自述记录，不构成身份认证。","hint"));review.appendChild(statusBox);box.appendChild(review);
    var exportButton=button(g.ready_for_governance_export?"导出已核对治理包":"导出候选治理包（附待办）",async function(){var r=await api.call("POST",base+"/export",{});if(!r.body.success){status($("proposal-status"),api.errorText(r.body,"导出失败："),true);return;}var blob=new Blob([JSON.stringify(r.body.result,null,2)],{type:"application/json"}),url=URL.createObjectURL(blob),a=document.createElement("a");a.href=url;a.download=proposalId+"-governance.json";a.click();setTimeout(function(){URL.revokeObjectURL(url);},1000);});exportButton.disabled=api.isReadOnly();box.appendChild(exportButton);
  }
  function reviewScopeFields(parent,context){
    var mode=selectField(parent,"本次审查范围",[{value:"whole_contract",label:"整份合同（包括需要补充的条款）"},{value:"clause_groups",label:"仅审查明确选定的条款类别"}],context.review_scope||"whole_contract");
    var wrap=element("label",null,"field");wrap.appendChild(element("span","定向审查的条款类别（可多选）"));var groups=element("select");groups.setAttribute("aria-label","定向审查的条款类别（可多选）");groups.multiple=true;groups.size=5;(schemas.client_rule.vocabulary.clause_group_options||[]).forEach(function(o){option(groups,o.value,o.label);});Array.from(groups.options).forEach(function(o){o.selected=(context.clause_groups||[]).includes(o.value);});groups.disabled=mode.value!=="clause_groups";wrap.appendChild(groups);parent.appendChild(wrap);
    mode.addEventListener("change",function(){context.review_scope=mode.value;groups.disabled=mode.value!=="clause_groups";context.clause_groups=mode.value==="clause_groups"?Array.from(groups.selectedOptions).map(function(o){return o.value;}):[];});groups.addEventListener("change",function(){context.clause_groups=Array.from(groups.selectedOptions).map(function(o){return o.value;});});
  }
  function initPreviewExtras(){var box=$("preview-context-extra");if(!box){return;}reviewScopeFields(box,previewExtraState);var facts=element("details",null,"aw-group");facts.appendChild(element("summary","补充规则判断需要的事实"));facts.appendChild(element("p","填写真实已知事实，留空表示未提供；客户业务条件的预筛结果供全文审查 AI 参考，公共硬边界仍按实际材料核验。","hint"));schemas.client_rule.vocabulary.fact_keys.filter(function(f){return f.fact_key!=="presale.permit.validity";}).forEach(function(f){var field=f.enum_values?selectField(facts,f.description,[{value:"",label:"尚未提供"}].concat(f.enum_values.map(function(v){return {value:v,label:texts[v]||v};})),""):inputField(facts,f.description,"");field.addEventListener("input",function(){if(!field.value.trim()){delete previewExtraState.facts[f.fact_key];}else{previewExtraState.facts[f.fact_key]=f.value_type==="number"?Number(field.value):field.value;}});});box.appendChild(facts);}
  async function prepareBatch(){
    var profileId=api.clientState().profileId;if(!profileId){status($("batch-status"),"请先选择客户。",true);return;}
    var base="/api/v1/clients/"+encodeURIComponent(profileId),responses=await Promise.all([api.call("GET",base+"/drafts"),api.call("GET",base+"/snapshots")]);
    if(profileId!==api.clientState().profileId){return;}
    if(responses.some(function(r){return !r.body.success;})){status($("batch-status"),"未能载入完整草稿集合及现行版本，请刷新后重试。",true);return;}
    var all=responses[0].body.result.drafts,eligible=all.filter(function(d){return ["lawyer_approved","regression_passed","active"].includes(d.approval_status);});
    if(!eligible.length){batch=null;renderBatch();status($("batch-status"),"该客户尚无已完成律师确认的规则，请先逐条审核。",true);return;}
    var drafts=await Promise.all(eligible.map(function(d){return api.call("GET",base+"/drafts/"+encodeURIComponent(d.client_rule_id));}));if(profileId!==api.clientState().profileId){return;}
    if(drafts.some(function(r){return !r.body.success;})){status($("batch-status"),"有规则未能完整载入，未创建整组测试。",true);return;}
    batch={profileId:profileId,rules:drafts.map(function(r){return r.body.result.draft;}),snapshot:responses[1].body.result.active_snapshot_id,skipped:all.filter(function(d){return !eligible.includes(d);}),cases:[]};renderBatch();
  }
  function renderBatch(){
    var box=$("batch-scenario-workbench");if(!box){return;}api.clear(box);box.appendChild(element("h4","整组规则的发布前测试"));box.appendChild(element("p","同时启用多条规则前，须验证完整集合。逐条通过不能证明组合后没有冲突。载入后逐项写明每个情形应交给 AI 审查哪些规则，预期不会由实际结果自动填写；通过不等于已经完成合同适用性判断。","hint"));var load=button("载入已确认规则集合",prepareBatch);load.disabled=api.isReadOnly();box.appendChild(load);
    var message=element("p");message.id="batch-status";message.setAttribute("role","status");box.appendChild(message);if(!batch||batch.profileId!==api.clientState().profileId){return;}
    box.appendChild(element("p","本组共 "+batch.rules.length+" 条："+batch.rules.map(function(r){return r.title+"（第 "+r.revision+" 次修改）";}).join("；")));
    if(batch.skipped.length){box.appendChild(element("p","本次不包含："+batch.skipped.map(function(r){return r.title+"（未确认或已停用）";}).join("；"),"warning"));}
    var add=button("＋ 添加整组测试情形",function(){batch.cases.push({case_id:"整组情形 "+(batch.cases.length+1),input:{primary_type_id:"",our_role:"",contract_stage:"",primary_domain_code:"",confirmed_domain_code:"",scene_tags:[],secondary_type_ids:[],facts:{},classification_status:"high"},expected:{},choices:{},values:Object.fromEntries(batch.rules.map(function(r){return [r.client_rule_id,copy(r.effect.value)];}))});renderBatch();});add.disabled=api.isReadOnly();box.appendChild(add);
    batch.cases.forEach(function(test,index){
      var item=element("details",null,"aw-batch-case aw-test");item.open=true;item.appendChild(element("summary",test.case_id));
      var name=inputField(item,"整组情形名称",test.case_id);name.addEventListener("input",function(){test.case_id=name.value;});
      var type=selectField(item,"合同类型",[{value:"",label:"请选择"}].concat(vocab(schemas.client_rule,"type_ids")),test.input.primary_type_id);type.addEventListener("change",function(){test.input.primary_type_id=type.value;framework.domains.forEach(function(d){if(d.types.some(function(t){return t.type_id===type.value;})){test.input.primary_domain_code=d.domain_code;test.input.confirmed_domain_code=d.domain_code;}});});
      [["our_role","我方角色",schemas.client_rule.vocabulary.role_options],["contract_stage","合同阶段",schemas.client_rule.vocabulary.stage_options]].forEach(function(f){var c=selectField(item,f[1],[{value:"",label:"请选择"}].concat(f[2]),test.input[f[0]]);c.addEventListener("change",function(){test.input[f[0]]=c.value;});});
      var scenes=inputField(item,"场景标签（逗号分隔）",test.input.scene_tags.join(","));scenes.addEventListener("input",function(){test.input.scene_tags=scenes.value.split(/[,，]/).map(function(s){return s.trim();}).filter(Boolean);});
      schemas.client_rule.vocabulary.fact_keys.forEach(function(f){var value=inputField(item,f.description+"（留空表示未提供）",test.input.facts[f.fact_key]===undefined?"":String(test.input.facts[f.fact_key]));value.addEventListener("input",function(){if(!value.value.trim()){delete test.input.facts[f.fact_key];}else{test.input.facts[f.fact_key]=f.value_type==="number"?Number(value.value):value.value;}});});
      reviewScopeFields(item,test.input);batch.rules.forEach(function(rule){var rid=rule.client_rule_id,row=element("section",null,"aw-field");row.appendChild(element("strong",rule.title));var choice=selectField(row,"预期交给 AI 审查："+rule.title,[{value:"",label:"请明确预期"},{value:"true",label:"应交给 AI 审查"},{value:"false",label:"不进入本次审查待办"}],test.choices[rid]);choice.addEventListener("change",function(){test.choices[rid]=choice.value;});row.appendChild(element("p","动作："+(texts[rule.effect.action]||rule.effect.action)+"；目标："+((schemas.client_rule.vocabulary.effect_targets.find(function(t){return t.target_key===rule.effect.target_key;})||{}).description||rule.effect.target_key)));var value=inputField(row,"预期处理值："+rule.title,String(test.values[rid]));value.addEventListener("input",function(){test.values[rid]=typeof rule.effect.value==="number"?Number(value.value):typeof rule.effect.value==="boolean"?value.value==="true":value.value;});item.appendChild(row);});
      var human=selectField(item,"整组预期需要人工确认",[{value:"",label:"请明确预期"},{value:"true",label:"是"},{value:"false",label:"否"}],test.expected.human_confirmation_required);human.addEventListener("change",function(){test.expected.human_confirmation_required=human.value===""?undefined:human.value==="true";});item.appendChild(button("移除此整组情形",function(){batch.cases.splice(index,1);renderBatch();}));box.appendChild(item);
    });var run=button("运行整组场景测试",runBatch,"secondary");run.disabled=api.isReadOnly();box.appendChild(run);var result=element("div");result.id="batch-results";box.appendChild(result);
  }
  async function runBatch(){
    var selected=batch,box=$("batch-status");if(!selected||!selected.cases.length){status(box,"请先添加具有明确预期的整组情形。",true);return;}
    if(selected.pendingRequest){status(box,"本组测试仍在执行，请等待后再提交；期间可以继续填写。",true);return;}
    var valid=selected.cases.every(function(c){return typeof c.expected.human_confirmation_required==="boolean"&&selected.rules.every(function(r){return ["true","false"].includes(c.choices[r.client_rule_id]);});});if(!valid){status(box,"请明确每条规则是否交给 AI 审查，以及整组是否存在需处理的阻断。",true);return;}
    var token=beginClientRequest("batch");if(!token){return;}
    selected.pendingRequest=token;var submittedCases=JSON.stringify(selected.cases);
    var request={expected_revisions:Object.fromEntries(selected.rules.map(function(r){return [r.client_rule_id,r.revision];})),expected_active_snapshot:selected.snapshot,cases:selected.cases.map(function(c){var rules={};selected.rules.forEach(function(r){if(c.choices[r.client_rule_id]==="true"){rules[r.client_rule_id]={action:r.effect.action,target_key:r.effect.target_key,value:c.values[r.client_rule_id]};}});return {case_id:c.case_id,input:copy(c.input),expected:{rules:rules,human_confirmation_required:c.expected.human_confirmation_required}};})};
    status(box,"正在执行整组规则测试……");
    try {
      var response=await api.call("POST","/api/v1/clients/"+encodeURIComponent(selected.profileId)+"/rule-set/test",request);
      if(selected!==batch||selected.profileId!==api.clientState().profileId){return;}
      if(!response.body.success){status($("batch-status"),api.errorText(response.body,"整组测试未完成："),true);return;}
      var result=response.body.result;if(result.drafts){selected.rules=result.drafts;}
      var updated=(result.drafts||[]).find(function(r){return r.client_rule_id===token.ruleId;}),applied={applied:false,preserved:false};
      if(updated&&clientRequestCurrent(token)){applied=applyClientResult(token,updated);}
      renderBatch();api.loadClientDetail(selected.profileId);
      var casesChanged=JSON.stringify(selected.cases)!==submittedCases;
      var message=result.recovery_required?"测试已记录，但写入恢复尚待完成。请重新载入并核对后再启用。":result.passed?"整组情形全部通过，已记录与完整集合绑定的交接测试证据；未判断具体合同适用性，仍须显式启用。":"整组存在未通过情形，已撤下相关草稿的发布资格；现行版本不变。";
      if(applied.preserved||casesChanged){message+=" 当前未保存的规则或新改的场景已保留，不在本次测试结论内。";}
      status($("batch-status"),message,!result.passed||result.recovery_required||applied.preserved||casesChanged);
      var output=$("batch-results");result.report.cases.forEach(function(c){output.appendChild(element("h4",c.case_id+" · "+(c.passed?"通过":"未通过")));output.appendChild(element("pre","预期："+JSON.stringify(c.expected,null,2)+"\n实际："+JSON.stringify(c.actual,null,2)));(c.excluded||[]).forEach(function(e){output.appendChild(element("p",e.reason));});(c.conflicts||[]).forEach(function(e){output.appendChild(element("pre",JSON.stringify(e,null,2)));});});
    } finally {if(selected.pendingRequest===token){delete selected.pendingRequest;}endClientRequest(token);}
  }
  function progressControls(container,isClient){
    var prefix=isClient?"client":"public",box=element("section",null,"aw-progress");
    box.appendChild(element("h4","填写进度与保存前预览"));
    box.appendChild(element("p","未写完也可以保存填写进度。填写进度不参与运行，也不会取得审核或测试通过状态。","hint"));
    var save=button("保存填写进度",function(){saveProgress(isClient);});save.disabled=api.isReadOnly();box.appendChild(save);
    box.appendChild(button("预览位置、条件和处理要求",function(){previewEditor(isClient);}));
    var list=element("select");list.id="aw-"+prefix+"-progress-list";list.setAttribute("aria-label","载入未完成的"+(isClient?"客户":"公共")+"规则");option(list,"","请选择已保存的填写进度");box.appendChild(list);
    box.appendChild(button("刷新填写进度列表",function(){refreshProgress(isClient);}));
    box.appendChild(button("载入填写进度",function(){if(list.value){loadProgress(isClient,list.value);}}));
    var result=element("div");result.id="aw-"+prefix+"-preview";result.setAttribute("role","status");box.appendChild(result);container.appendChild(box);
    // The container is appended by the caller after this function returns.
    Promise.resolve().then(function(){refreshProgress(isClient);});
  }
  function progressQuery(isClient){var profile=api.clientState().profileId;if(isClient&&!profile){return null;}return isClient?"?client_profile_id="+encodeURIComponent(profile):"";}
  function refreshProgress(isClient){var query=progressQuery(isClient),box=$("aw-"+(isClient?"client":"public")+"-progress-list");if(query===null||!box){return;}api.call("GET","/api/v1/authoring/drafts"+query).then(function(r){if(query!==progressQuery(isClient)||box!==$(box.id)){return;}api.clear(box);option(box,"","请选择已保存的填写进度");if(!r.body.success){status($("aw-"+(isClient?"client":"public")+"-preview"),api.errorText(r.body,"读取填写进度失败："),true);return;}r.body.result.drafts.forEach(function(d){option(box,d.draft_id,d.title+" · "+schemas[d.kind].title+" · "+(d.validation.content_ready?"待审核":"待补内容"));});});}
  function saveProgress(isClient){
    var editor=isClient?clientEditor:publicEditor,record=isClient?clientProgress:publicProgress,s=api.clientState(),box=$("aw-"+(isClient?"client":"public")+"-preview");
    if(isClient&&!s.profileId){status(box,"请先选择客户。",true);return;}
    var state=isClient?{condition_intent:conditionIntent}:{operation:$("pp-op").value,target_rule_id:$("pp-target").value,title:$("pp-title").value,rationale:$("pp-rationale").value,source_refs:$("pp-sources").value,proposal_id:editingProposal&&editingProposal.proposal_id,proposal_content_sha256:editingProposal&&editingProposal.content_sha256};
    var request={kind:editor.meta.kind,draft:copy(editor.value),draft_id:record&&record.draft_id,expected_revision:record?record.revision:0,base_revision:isClient&&s.draft?s.draft.revision:null,editor_state:state};
    if(isClient){request.client_profile_id=s.profileId;request.draft.client_profile_id=s.profileId;}
    api.call("POST","/api/v1/authoring/drafts",request).then(function(r){if(r.body.success){document.dispatchEvent(new CustomEvent("studio-authoring-saved",{detail:{kind:editor.meta.kind,client_profile_id:isClient?s.profileId:null}}));}if(editor!==(isClient?clientEditor:publicEditor)){return;}if(!r.body.success){status(box,api.errorText(r.body,"保存填写进度失败："),true);return;}if(isClient){clientProgress=r.body.result;}else{publicProgress=r.body.result;}previewEditor(isClient,"填写进度已保存；尚未启用。");refreshProgress(isClient);});
  }
  function loadProgress(isClient,draftId){
    var query=progressQuery(isClient),originalEditor=isClient?clientEditor:publicEditor;if(query===null){return;}
    if(editorDirty(originalEditor,isClient)&&!window.confirm("载入填写进度将替换表单中尚未保存的内容，是否继续？")){return;}
    var token=beginEditorRead(isClient),box=$("aw-"+(isClient?"client":"public")+"-preview");
    api.call("GET","/api/v1/authoring/drafts/"+encodeURIComponent(draftId)+query).then(async function(r){
      if(!editorReadCurrent(token,box)){return;}
      if(!r.body.success){status(box,api.errorText(r.body,"载入失败："),true);return;}
      var p=r.body.result,st=p.editor_state||{};
      if(isClient){
        if(p.base_revision!==null&&p.base_revision!==undefined){
          var current=await api.call("GET","/api/v1/clients/"+encodeURIComponent(token.profileId)+"/drafts/"+encodeURIComponent(p.draft.client_rule_id));
          if(!editorReadCurrent(token,box)){return;}
          if(!current.body.success||current.body.result.draft.revision!==p.base_revision){status(box,"正式草稿已变化或已删除。为防止覆盖新内容，此填写进度未载入；请先核对最新草稿。",true);return;}
          api.fillRuleForm(current.body.result.draft);
        }else{api.clearRuleForm();}
        clientEditor.value=copy(p.draft);clientEditor.render();clientEditor.baseline=JSON.stringify(clientEditor.value);clientProgress=p;conditionIntent=st.condition_intent||"";$("aw-condition-intent").value=conditionIntent;
      }else{
        var storedProposal=null;
        if(st.proposal_id){var stored=await api.call("GET","/api/v1/public-proposals/"+encodeURIComponent(st.proposal_id));if(!editorReadCurrent(token,box)){return;}if(!stored.body.success||stored.body.result.content_sha256!==st.proposal_content_sha256){status(box,"公共建议已变化或已结束，请先核对最新内容；旧填写进度未覆盖表单。",true);return;}storedProposal=stored.body.result;}
        setPublic(p.kind,p.draft,false);publicEditor.value=copy(p.draft);publicEditor.render();publicEditor.baseline=JSON.stringify(publicEditor.value);publicProgress=p;
        editingProposal=storedProposal;
        $("pp-op").value=st.operation||"create";$("pp-target").value=st.target_rule_id||"";$("pp-title").value=st.title||"";$("pp-rationale").value=st.rationale||"";$("pp-sources").value=st.source_refs||"";
      }
      $(isClient?"client-manual-editor":"public-manual-editor").open=true;previewEditor(isClient,"已恢复填写进度；尚未启用。");
    }).finally(function(){endEditorRead(token);});
  }
  function previewEditor(isClient,message){
    var editor=isClient?clientEditor:publicEditor,body=editor.value,box=$("aw-"+(isClient?"client":"public")+"-preview");if(!box){return;}
    api.clear(box);if(message){box.appendChild(element("p",message));}
    var scope=body.scope||body,types=scope.type_ids||(body.type_id?[body.type_id]:[]),domains=scope.domain_codes||(body.domain_code?[body.domain_code]:[]),allTypes=framework.domains.flatMap(function(d){return d.types;});
    var names=types.map(function(t){return (allTypes.find(function(x){return x.type_id===t;})||{}).title||t;});
    names=names.concat(domains.map(function(d){return (framework.domains.find(function(x){return x.domain_code===d;})||{}).title||d;}));
    box.appendChild(element("h4",body.title||body.name||body.clause_group||"未命名规则"));
    box.appendChild(element("p","位置："+(scope.applies_to_all?"全部业务范围":names.join("；")||"尚未选择业务位置")));
    ["roles","scene_tags","contract_stages","clause_groups"].forEach(function(k){if((scope[k]||[]).length){var opts=vocab(editor.meta,k)||[];box.appendChild(element("p",editor.metadata(isClient?"scope."+k:k).label+"："+scope[k].map(function(x){return (opts.find(function(o){return o.value===x;})||{}).label||x;}).join("、")));}});
    if(isClient){
      box.appendChild(element("p","条件："+(conditionIntent===""?"尚未确定":conditionIntent==="semantic"?texts.semantic:conditionIntent==="unconditional"?"在上述范围内不另设事实条件":texts[body.condition.mode]+"下列条件")));
      if(body.condition.natural_language_note){box.appendChild(element("p",body.condition.natural_language_note));}
      (body.condition.clauses||[]).forEach(function(c){var f=editor.meta.vocabulary.fact_keys.find(function(f){return f.fact_key===c.fact_key;});box.appendChild(element("p",(f?f.description:c.fact_key)+" "+(texts[c.operator]||c.operator)+(c.operator==="exists"?"":" "+JSON.stringify(c.value))));});
      box.appendChild(element("p","适用性判断将以真实合同批注留痕；历史人工确认标记（"+(body.condition.human_confirmation_required?"是":"否")+"）不会要求重复点击，正式审核与启用另行进行。"));
      var target=editor.meta.vocabulary.effect_targets.find(function(t){return t.target_key===body.effect.target_key;});box.appendChild(element("p","处理："+(texts[body.effect.action]||body.effect.action)+" · "+(target?target.description:body.effect.target_key)));box.appendChild(element("pre",String(body.effect.value===undefined?"尚未填写":body.effect.value)));
    }else{["review_objective","review_objectives","action_guidance","variants"].forEach(function(k){if(body[k]){box.appendChild(element("strong",editor.metadata(k).label));box.appendChild(element("pre",typeof body[k]==="string"?body[k]:JSON.stringify(body[k],null,2)));}});}
    var referenceCatalog=editor.referenceCatalog();function referenceName(ref){var found=referenceCatalog.find(function(o){return o.value===ref;});return found?referenceLabel(found):"未登记的原有引用："+String(ref);}
    ["depends_on","dependencies","linked_module_ids","derived_module_ids","derived_principle_ids","conflicts_with"].forEach(function(k){if(Array.isArray(body[k])){box.appendChild(element("p",editor.metadata(k).label+"："+(body[k].length?body[k].map(referenceName).join("；"):"当前未声明")));}});
    if(body.dependencies_by_type&&typeof body.dependencies_by_type==="object"&&!Array.isArray(body.dependencies_by_type)){Object.keys(body.dependencies_by_type).forEach(function(t){var name=(allTypes.find(function(x){return x.type_id===t;})||{}).title||("未登记的原有合同类型："+t),refs=body.dependencies_by_type[t];box.appendChild(element("p",name+"的前置依赖："+(Array.isArray(refs)?(refs.length?refs.map(referenceName).join("；"):"明确无依赖"):"原有格式待核对，已保留")));});}
    var sources=body.source_refs||body.source_ids||[];box.appendChild(element("p","依据："+(sources.length?sources.map(function(s){return ((editor.meta.vocabulary.source_options||[]).find(function(o){return o.value===s;})||{}).label||s;}).join("；"):"尚未填写直接依据，请核对本类规则的来源要求")));
    box.appendChild(element("p","保存后仍未启用。公共建议须经过治理后更新正式资产；客户规则须人工确认、真实场景测试并显式发布。","hint"));
    var gaps=element("div");box.appendChild(gaps);api.call("POST","/api/v1/authoring/validate",{kind:editor.meta.kind,rule:body}).then(function(r){if(!gaps.isConnected){return;}if(!r.body.success){gaps.appendChild(element("p",api.errorText(r.body,"检查失败：")));return;}gaps.appendChild(element("strong",r.body.result.content_ready?"业务字段已填写，仍待审核和验证":"目前缺项或需核对内容"));r.body.result.issues.forEach(function(i){gaps.appendChild(element("p",i.label+"："+i.message+(i.kind==="governance"?"（由后续真实操作产生）":"")));});});
  }
  function renderCandidate(container,record,request){
    var meta=record.kind==="client_rule"&&clientEditor&&clientEditor.value.client_profile_id===record.client_profile_id?clientEditor.meta:schemas[record.kind],body=record.draft;
    if(!meta||!body){container.appendChild(element("p","候选内容尚未完整载入。","warning"));return;}
    function metadata(path){return meta.fields.find(function(f){return f.path===path;})||{};}
    function show(parent,value,path){
      if(value===null||value===undefined||value===""){parent.appendChild(element("span","尚未填写","hint"));return;}
      if(Array.isArray(value)){if(!value.length){parent.appendChild(element("span","未列条目","hint"));return;}var list=element("ul",null,"ai-value-list");value.forEach(function(v){var row=element("li");show(row,v,path+"[]");list.appendChild(row);});parent.appendChild(list);return;}
      if(typeof value==="object"){var dl=element("dl",null,"ai-read-fields");Object.keys(value).forEach(function(k){var child=path?path+"."+k:k,field=metadata(child);dl.appendChild(element("dt",field.label||k));var dd=element("dd");show(dd,value[k],child);dl.appendChild(dd);});parent.appendChild(dl);return;}
      var options=vocab(meta,path)||[];if(request&&["source_refs[]","source_ids[]"].includes(path)){options=options.concat((request.materials||[]).map(function(m){return {value:m.source_id,label:m.title};}),[{value:request.intent_source_id,label:"你的原始要求"}]);}var entry=options.find(function(o){return String(o.value)===String(value);});
      parent.appendChild(element("span",entry?entry.label:(texts[String(value)]||String(value))));
    }
    var important=record.kind==="client_rule"?["scope","condition","effect","validity","summary"]:["review_objective","review_objectives","roles","scene_tags","preconditions","failure_modes","variants","action_guidance","hard_boundaries"];
    var main=element("div",null,"ai-rule-content"),location=element("section",null,"ai-business-field"),scope=body.scope||body;
    location.appendChild(element("h5","在规则体系中的位置"));location.appendChild(element("p",meta.title));
    var dimensions=[["domain_codes",scope.domain_codes||(body.domain_code?[body.domain_code]:[])],["type_ids",scope.type_ids||(body.type_id?[body.type_id]:[])],["roles",scope.roles||[]],["scene_tags",scope.scene_tags||[]],["contract_stages",scope.contract_stages||[]],["clause_groups",scope.clause_groups||[]]];
    if(scope.applies_to_all){location.appendChild(element("p","明确适用于全部业务范围"));}
    var selected=false;dimensions.forEach(function(item){if(!item[1].length){return;}selected=true;var line=element("div");line.appendChild(element("strong",({domain_codes:"业务大类",type_ids:"合同类型",roles:"我方立场",scene_tags:"适用场景",contract_stages:"合同阶段",clause_groups:"条款范围"})[item[0]]+"："));show(line,item[1],record.kind==="client_rule"?"scope."+item[0]:item[0]);location.appendChild(line);});
    if(!scope.applies_to_all&&!selected){location.appendChild(element("p","适用范围尚未明确，需要继续核对。","hint"));}main.appendChild(location);
    important=important.filter(function(k){return !["scope","roles","scene_tags"].includes(k);});
    important.forEach(function(k){if(body[k]===undefined){return;}var section=element("section",null,"ai-business-field");section.appendChild(element("h5",metadata(k).label||k));
      if(k==="condition"){
        var intent=(record.editor_state||{}).condition_intent,clauses=body.condition.clauses||[];
        section.appendChild(element("p",semanticCondition(body)?texts.semantic:intent==="unconditional"&&!clauses.length?"在上述适用范围内，不另设事实条件。":clauses.length?(texts[body.condition.mode]||body.condition.mode)+"以下事实条件：":"事实条件尚未确认。"));
        clauses.forEach(function(c){var fact=meta.vocabulary.fact_keys.find(function(f){return f.fact_key===c.fact_key;});section.appendChild(element("p",(fact?fact.description:c.fact_key)+" "+(texts[c.operator]||c.operator)+(c.operator==="exists"?"":" "+JSON.stringify(c.value))));});
        if(body.condition.natural_language_note){section.appendChild(element("p",body.condition.natural_language_note));}if(semanticCondition(body)){section.appendChild(element("p","审查 AI 应根据合同全文给出适用性结论及依据，并在合同中留下真实批注；历史确认标记不产生重复确认步骤。正式规则仍须独立审核、测试与启用。"));}else{section.appendChild(element("p","上述业务条件仅供预筛；审查 AI 结合合同全文判断并以真实批注留痕。历史人工确认标记（"+(body.condition.human_confirmation_required?"是":"否")+"）不产生重复确认步骤，正式规则审核与启用独立保留。"));}
      }else{show(section,body[k],k);}main.appendChild(section);});container.appendChild(main);
    var full=element("details",null,"ai-full-rule");full.appendChild(element("summary","完整规则与框架关系（所有字段）"));
    meta.groups.forEach(function(g){var fields=meta.fields.filter(function(f){return !f.path.includes(".")&&!f.path.includes("[]")&&f.group===g.id&&body[f.path]!==undefined;});if(!fields.length){return;}var section=element("section");section.appendChild(element("h5",g.title));fields.forEach(function(f){section.appendChild(element("strong",f.label));var content=element("div",null,"ai-full-field");show(content,body[f.path],f.path);section.appendChild(content);});full.appendChild(section);});
    var unknown=Object.keys(body).filter(function(k){return !meta.schema.properties[k];});if(unknown.length){full.appendChild(element("p","以下扩展字段的业务说明尚未登记，原值完整保留：","hint"));unknown.forEach(function(k){full.appendChild(element("strong",k));full.appendChild(element("pre",JSON.stringify(body[k],null,2)));});}container.appendChild(full);
  }
  document.addEventListener("studio-author-rule",function(e){author(e.detail);});
  document.addEventListener("studio-ready",function(){api=window.Studio;Promise.all(["domain_principle","principle","clause_module","client_rule"].map(function(k){return api.call("GET","/api/v1/authoring/schema?kind="+k).then(function(r){if(!r.body.success){throw Error(api.errorText(r.body,"字段说明读取失败："));}schemas[k]=r.body.result;});}).concat([api.call("GET","/api/v1/knowledge/framework").then(function(r){if(!r.body.success){throw Error("规则框架读取失败");}framework=r.body.result;})])).then(function(){ready=true;window.AuthoringWorkbench.ready=true;$("rule-form").classList.add("aw-enhanced");$("rule-form").querySelectorAll("[required]").forEach(function(e){e.required=false;});loadClient(api.clientState().draft);initPublic();initPreviewExtras();if(pendingAuthor){author(pendingAuthor);pendingAuthor=null;}document.dispatchEvent(new CustomEvent("studio-authoring-ready"));if(api.isReadOnly()){document.body.classList.add("studio-readonly");$("session-state").textContent="只读模式：允许阅读和试运行，维护操作已禁用";$("rule-form").querySelectorAll("input,select,textarea,button").forEach(function(e){e.disabled=true;});$("proposal-form").querySelectorAll("input,select,textarea,button").forEach(function(e){e.disabled=true;});}}).catch(function(e){status($("rule-status"),e.message,true);status($("proposal-status"),e.message,true);});});
  window.AuthoringWorkbench={ready:false,renderCandidate:renderCandidate,referenceTitle:function(ref){var entry=(schemas.clause_module.vocabulary.rule_reference_options||[]).find(function(r){return r.value===ref;});return entry?entry.label:ref||"尚未明确";},fieldLabels:function(kind,paths){var meta=schemas[kind];return paths.map(function(path){var key=path.replace(/\.\d+(?=\.|$)/g,"[]"),f=meta&&meta.fields.find(function(f){return f.path===key;});return f?f.label:path;});},openClientProgress:function(draftId){if(!ready){return;}api.showView("clients");return loadProgress(true,draftId);},previewExtras:function(){return copy(previewExtraState);},loadClient:loadClient,getClientRule:getClientRule,beginClientRequest:beginClientRequest,clientRequestCurrent:clientRequestCurrent,endClientRequest:endClientRequest,applyClientResult:applyClientResult,saveProposal:saveProposal,runTests:runTests,showProposal:showProposal,
    openPublicProposal:function(proposalId){if(!ready){return;}api.showView("proposals");return showProposal(proposalId);},
    openPublicProgress:function(draftId){if(!ready){return;}api.showView("proposals");return loadProgress(false,draftId);}};
})();
