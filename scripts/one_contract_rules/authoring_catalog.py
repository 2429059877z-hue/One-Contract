"""Read-only, schema-backed business descriptions for Studio authoring.

This module describes existing fields. It does not invent legal content, grant
approval, alter a schema, or write any public knowledge asset.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from . import catalog_index, clause_groups, guard_catalog, paths

KINDS = {
    "domain_principle": ("大类审查原则", "assets/knowledge_v2/schemas/domain_principle.schema.json"),
    "principle": ("具体类型审查原则", "assets/knowledge_v2/schemas/principle_catalog.schema.json"),
    "clause_module": ("条款模块", "assets/knowledge_v2/schemas/clause_module.schema.json"),
    "client_rule": ("客户专属规则", "scripts/one_contract_rules/schemas/client-rule-v1.schema.json"),
}
GROUPS = [
    {"id": "identity", "title": "身份与定位", "description": "这是什么规则，放在哪个业务位置。"},
    {"id": "scope", "title": "适用范围", "description": "哪些类型、角色、阶段和场景适用。"},
    {"id": "conditions", "title": "条件与边界", "description": "触发前提、例外和人工确认要求。"},
    {"id": "requirements", "title": "审查要求与合同处理", "description": "查什么、怎么处理以及现有文本口径。"},
    {"id": "sources", "title": "依据与关联", "description": "依据来源、依赖规则及覆盖缺口。"},
    {"id": "validation", "title": "验证与发布", "description": "版本、内容完整性、实际审核和验证记录。"},
]

LABELS = {
    "domain_doctrine_id": "大类原则编号", "domain_code": "所属业务大类", "domain_name": "大类名称",
    "doctrine_id": "原则编号", "module_id": "模块编号", "client_rule_id": "客户规则编号",
    "type_id": "主要合同类型", "type_ids": "适用合同类型", "type_code": "合同类型编码", "type_codes": "合同类型编码",
    "name": "名称", "title": "规则名称", "version": "版本", "schema_version": "结构版本",
    "client_profile_id": "所属客户", "clause_group": "条款类别", "clause_groups": "条款类别",
    "review_objectives": "审查目标", "review_objective": "审查目标", "action_guidance": "行动指引",
    "judgment_coordinates": "判断维度", "review_sequence": "审查顺序", "common_failure_modes": "常见失败情形",
    "failure_mode": "失败情形", "default_tendencies": "默认处理倾向", "hard_boundaries": "不可突破的边界",
    "applicability_conditions": "适用条件", "override_factors": "调整因素与例外", "required_evidence": "所需证据",
    "knowledge_gaps": "知识缺口", "coverage": "覆盖信息", "total_type_count": "类型总数",
    "supported_type_count": "已支持类型数量", "coverage_status": "覆盖程度", "supported_type_ids": "已支持类型",
    "unsupported_type_ids": "未支持类型", "module_index": "议题与模块索引", "topic": "议题",
    "module_ids": "关联模块", "applicability_note": "适用说明", "derived_principle_ids": "关联类型原则",
    "derived_module_ids": "关联条款模块", "linked_module_ids": "关联条款模块", "dependencies": "前置依赖",
    "dependencies_by_type": "按合同类型区分的依赖", "conflicts": "冲突规则或情形", "depends_on": "前置依赖",
    "conflicts_with": "冲突规则", "source_ids": "依据来源", "source_refs": "依据来源", "source_family_ids": "样本家族",
    "required_source_slots": "需具备的来源类别", "source_status": "来源支撑情况", "sample_support_status": "样本支撑情况",
    "gold_family_count_local_summary": "本地高质量样本家族数", "representative_family_count_local_summary": "本地代表样本家族数",
    "roles": "我方角色", "scene_tags": "业务场景", "requiredness": "必需程度", "assumptions": "适用前提",
    "placeholders": "需要填写的参数", "variants": "合同处理口径", "strong_protection": "强保护口径",
    "balanced": "平衡口径", "concession": "可接受退让", "review_checks": "具体检查点",
    "review_checks_derivation": "检查点形成说明",
    "recommended_actions": "建议动作", "remediation_steps": "补救步骤", "legal_basis_notes": "法律依据说明",
    "risk_examples": "风险示例", "directed_block_policy": "定向阻断策略", "activation_condition": "触发条件",
    "allowed_outputs": "触发后仍允许的输出", "blocked_outputs": "触发后禁止的输出", "non_trigger": "不触发的情形",
    "scope": "适用范围", "applies_to_all": "全部适用", "domain_codes": "业务大类", "contract_stages": "合同阶段",
    "condition": "触发条件", "assessment_mode": "条件判断方式", "mode": "全部满足或任一满足", "clauses": "条件条目",
    "human_confirmation_required": "历史人工确认标记", "natural_language_note": "条件说明",
    "fact_key": "需核对的事实", "operator": "判断方式", "value": "取值", "effect": "处理动作",
    "action": "怎样处理", "target_key": "作用目标", "reason_template": "处理理由",
    "rule_kind": "规则用途", "normative_level": "执行力度", "summary": "补充说明",
    "priority_within_class": "同类规则内优先级", "validity": "适用有效期", "valid_from": "开始时间", "valid_until": "结束时间",
    "review": "审核记录", "approval_status": "审核状态", "activation_status": "启用状态",
    "content_status": "内容完备情况", "reviewer": "实际审核者", "reviewed_at": "实际审核日期",
    "legal_checked_at": "法律核验日期", "activated_at": "启用日期", "activation_basis": "启用依据",
    "migration_summary": "迁移处置说明", "lawyer_approval_evidence_id": "审核证据编号",
    "lawyer_approved_by": "实际审核者", "lawyer_approved_at": "实际审核时间", "legal_check_status": "法律核验状态",
    "regression_evidence_ids": "验证证据", "review_note": "审核说明", "supersedes": "替代的版本",
    "revision": "修订序号", "created_at": "创建时间", "updated_at": "修改时间", "created_by": "创建者",
    "updated_by": "修改者", "content_sha256": "内容指纹", "source_id": "来源编号", "source_kind": "来源种类",
    "author": "作者或发布机构", "published_at": "发布日期", "verified_at": "核验时间", "used_for": "支持的判断事项",
    "content_verification": "内容核验说明", "jurisdiction": "适用地区", "access_scope": "访问范围",
    "url": "来源链接", "knowledge_tags": "知识标签", "member_module_ids": "组成模块", "required_modules": "要求的模块",
}

_GROUP_FIELDS = {
    "identity": "domain_doctrine_id domain_code domain_name doctrine_id module_id client_rule_id type_id type_ids type_code type_codes name title client_profile_id clause_group",
    "scope": "roles scene_tags requiredness scope",
    "conditions": "applicability_conditions override_factors hard_boundaries assumptions directed_block_policy condition validity",
    "requirements": "review_objectives review_objective judgment_coordinates review_sequence common_failure_modes failure_mode default_tendencies action_guidance placeholders variants review_checks review_checks_derivation recommended_actions remediation_steps risk_examples rule_kind normative_level effect summary",
    "sources": "required_evidence knowledge_gaps coverage module_index derived_principle_ids derived_module_ids linked_module_ids dependencies dependencies_by_type conflicts depends_on conflicts_with source_ids source_refs source_family_ids required_source_slots source_status sample_support_status gold_family_count_local_summary representative_family_count_local_summary legal_basis_notes",
}
_GROUP_BY_FIELD = {field: group for group, fields in _GROUP_FIELDS.items() for field in fields.split()}
_SYSTEM = set("domain_doctrine_id doctrine_id module_id client_rule_id client_profile_id schema_version version type_code type_codes coverage revision created_at updated_at created_by updated_by content_sha256 approval_status activation_status activated_at".split())
_REVIEW = set("review reviewer reviewed_at legal_checked_at activation_basis".split())
DESCRIPTIONS = {
    "review_objectives": "逐条写清本规则要核对和解决的问题。", "review_objective": "写清本模块要解决的具体合同问题。",
    "type_id": "从已有合同类型目录选择；分类本身不代表规则已启用。",
    "dependencies_by_type": "共享模块按具体类型分别声明依赖，避免引入其他交易的前提。",
    "variants": "保留已有三种处理口径；处理原则不能冒充完整可直接使用的合同条款。",
    "source_ids": "选择实际存在的来源；来源关联不等于已完成法律核验。",
    "source_refs": "至少选择一个实际来源，不自动填入不相关的法律。",
    "required_source_slots": "说明应具备的来源类别，与已经具备的来源分开记录。",
    "condition": "可用完整自然语言交由 AI 结合合同全文判断，也可填写结构化条件或明确不限条件；未填写不能默认为不限条件。",
    "assessment_mode": "选择由 AI 结合合同全文判断时，完整填写条件说明并保留空条件条目；旧规则省略本项时仍使用结构化条件。判断须在实际合同中以批注留痕，不等于批准或启用规则。",
    "scope": "全部适用时各限定列表须为空；否则至少限定一个维度。",
    "clause_groups": "作用于哪些条款议题；整合同仍载入，定向审查才按类别筛选；缺失条款也可补写。公共模块的单数条款类别保留原议题，不自动映射。",
    "directed_block_policy": "仅在已有受控触发条件及明确作用范围时配置，五项子字段均须完整。",
    "review_checks": "逐条列出应实际检查的内容；格式可选不代表生产完整性可以省略。",
    "review_checks_derivation": "记录检查点如何从既有内容整理而来；保留已有形成说明，不据此推定新增法律依据或已经审核通过。",
    "review": "实际审核和测试行为的记录；不得通过新建表单编造批准或验证。",
    "coverage": "根据实际目录和资产关系计算；覆盖程度不等同法律结论可靠性。",
    "client_profile_id": "由当前明确选中的客户档案填入，切换客户后须重新核对归属。",
    "type_code": "由所选合同类型的目录记录带入，不手工编写编码。",
    "type_codes": "由所选适用合同类型的目录记录逐项带入。",
    "dependencies": "按名称选择前置条款模块；共同依赖须适用于本模块的所有类型，差异请在逐类型依赖中分别填写。",
    "linked_module_ids": "按名称选择适用于当前合同类型的条款模块；关联不授予启用资格。",
    "derived_module_ids": "按名称选择本大类关联的条款模块，并与议题索引逐项核对。",
    "derived_principle_ids": "按名称选择本业务大类中的类型原则；同一原则可有多个适用位置。",
    "module_ids": "按名称选择本议题关联的条款模块。",
    "depends_on": "按名称选择前置公共规则或当前客户规则；实际采用和发布仍须验证这些依赖。",
    "conflicts_with": "按名称选择存在冲突的公共规则或当前客户规则；不要选其他客户的规则。",
    "domain_code": "选择规则所属的业务大类，系统据此带入大类编号及覆盖目录。",
    "domain_name": "所选业务大类在目录中的中文名称；应与大类选择保持一致。",
    "name": "用业务名称概括这条原则，便于从目录和搜索结果识别。",
    "title": "用一句话概括本客户的具体要求，便于检索和核对。",
    "type_ids": "选择全部适用合同类型；共享模块保留同一身份，不为每个位置复制正文。",
    "judgment_coordinates": "列出形成判断时需要分别考虑的维度，而不是直接填写判断结论。",
    "review_sequence": "按实际执行先后列出审查步骤；条目顺序就是展示顺序。",
    "common_failure_modes": "记录本大类常见的约定缺漏、矛盾或履行失败情形。",
    "failure_mode": "记录本模块拟识别或预防的具体问题，便于检查点与处理口径对应。",
    "default_tendencies": "说明通常采用的处理方向；适用前提和例外应分别记录。",
    "applicability_conditions": "列出采用本大类原则之前须满足或核对的事实前提。",
    "override_factors": "说明哪些事实变化可能调整默认处理方向，避免把默认倾向当作无条件结论。",
    "hard_boundaries": "记录已有依据支持、不能由一般偏好突破的边界；缺少依据时先记为待核对。",
    "required_evidence": "列出作出判断需要查看的材料或记录，未取得材料不等于已经核实。",
    "module_index": "按业务议题组织模块：每项填写议题、关联模块及适用说明。",
    "topic": "本组模块共同处理的业务议题，用普通业务名称表述。",
    "applicability_note": "说明本议题下模块在什么情形使用，以及需要核对的限制。",
    "knowledge_gaps": "列出尚缺的资料、场景覆盖或判断依据；不得以空缺冒充已验证。",
    "content_status": "说明正文已具备与尚缺的内容；这段说明不能产生审核或启用资格。",
    "action_guidance": "逐条说明发现问题后的处理步骤，区分正文修改、补充材料和待确认事项。",
    "gold_family_count_local_summary": "填入已核实的本地高质量样本家族数量；无样本填零，不据此推定法律结论。",
    "representative_family_count_local_summary": "填入已核实的代表样本家族数量，避免把同一家族的多个文件重复计数。",
    "assumptions": "列出本处理口径依赖的事实或材料前提；实际不满足时应重新判断。",
    "clause_group": "用业务名称概括本公共模块处理的条款议题；此字段是自由议题，不是客户规则的受控类别编号。",
    "conflicts": "保留与本口径冲突的业务情形或既有标识；本字段允许情形文字，不强制改成规则引用。",
    "legal_basis_notes": "说明已引用来源怎样支持本处理要求，明确适用限制；不要填入未经核验的法律结论。",
    "placeholders": "列出合同处理文本仍需填写的业务参数，明确哪些事实尚待确认。",
    "requiredness": "选择本模块是必需、满足条件时需要，还是可选；条件应在前提说明中写清。",
    "recommended_actions": "列出本模块建议采取的具体动作，与检查点及处理口径对应。",
    "remediation_steps": "记录问题已发生时拟采取的补救步骤，区分实际可行措施与待确认事项。",
    "roles": "选择规则面向的我方身份；角色会影响实际采用，不能仅凭合同名称推定。",
    "risk_examples": "用情形示例解释风险，明确示例不等于本次合同已经发生该事实。",
    "scene_tags": "选择适用的业务场景；场景与角色、类型共同影响实际解析。",
    "source_family_ids": "保留实际样本家族编号以追溯材料；当前没有可确认的家族时不要虚构编号。",
    "source_status": "说明现有来源的完备程度和待核验限制，不等同已完成法律核验。",
    "balanced": "记录已有的平衡处理口径，并明确它是处理原则还是可直接使用的条款文本。",
    "concession": "记录已有依据支持的可接受退让及适用前提，不自动承诺对任何交易都可退让。",
    "strong_protection": "记录已有的强保护处理口径和前提；涉及待确认事实时应保留变量。",
    "summary": "补充标题之外的业务背景或限制，不替代正式适用条件和处理动作。",
    "rule_kind": "区分明确要求、优先做法、备用处理、人工判断和文书表达用途。",
    "normative_level": "选择规则执行力度：必须遵守、优先遵守或参考建议；不能覆盖更高层硬边界。",
    "applies_to_all": "选择是表示不按其他范围维度限定；同时必须清空各限定列表。",
    "domain_codes": "选择所限定的业务大类；未选择不表示已匹配任何具体合同。",
    "contract_stages": "选择本客户要求适用的合同阶段，留空表示不以阶段限定。",
    "mode": "结构化条件中，全部满足要求所有事实条件成立；任一满足表示至少一项成立。由 AI 判断自然语言条件时保留原值，不按此项计算。",
    "clauses": "结构化方式中逐条填写事实、判断方式和比较值；由 AI 判断自然语言条件时必须为空，不需要捏造事实字段。",
    "fact_key": "选择需要核对的事实，系统据此提供可用判断方式和取值类型。",
    "operator": "选择怎样比较事实；有该事实不填写比较值，属于所列值需要填写值列表。",
    "value": "按已选事实或处理目标填写对应类型的值，不把未知事实填成零或否。",
    "human_confirmation_required": "保留既有人工确认要求记录，不新增本次适用性确认步骤；客户业务条件由审查 AI 结合合同全文判断并以合同批注留痕。正式规则审核和启用仍独立进行。",
    "natural_language_note": "由 AI 结合合同全文判断时必填：完整写清主体、适用情形和边界，不必改成系统事实编号；结构化方式中本项仅补充说明，不替代条件条目。",
    "effect": "组合处理动作、受控目标和具体值，说明规则命中后应如何处理。",
    "action": "选择要求、优先、禁止、转人工或设定输出等处理方式。",
    "target_key": "选择允许客户规则作用的目标；目标决定具体值的数据类型。",
    "reason_template": "写出采用该处理方式时应说明的业务理由，勿预填本次尚未核实的事实。",
    "priority_within_class": "用于同层同类规则的排序比较；不能突破公共硬边界或替代冲突核对。",
    "validity": "限制本规则适用的时间范围；缺少某一端不自动补日期，结束时间不得早于开始时间。",
    "valid_from": "本规则允许开始适用的时点，须包含时区；未限定开始时点可不填写或保留原有 null。",
    "valid_until": "本规则适用截止时点，须包含时区且不得早于开始时间；未限定可不填写或保留原有 null。",
    "supersedes": "记录被本版本替代的既有版本说明；此字段本身不执行发布或停用操作。",
}

REFERENCE_FIELDS = {
    "dependencies": ["clause_module"], "dependencies_by_type": ["clause_module"],
    "linked_module_ids": ["clause_module"], "derived_module_ids": ["clause_module"],
    "module_index[].module_ids": ["clause_module"], "derived_principle_ids": ["principle"],
    "depends_on": ["clause_module", "principle", "domain_principle", "client_rule"],
    "conflicts_with": ["clause_module", "principle", "domain_principle", "client_rule"],
}


def derive_system_identity(kind: str, body: Mapping[str, Any], *, skill_root: Optional[Path] = None) -> Dict[str, Any]:
    """Derive catalog-owned fields without changing the requested business scope.

    Unknown locations remain unknown: never retain an AI-supplied catalog code
    or coverage claim that cannot be verified against this installation.
    """
    result = copy.deepcopy(dict(body))
    if kind not in {"principle", "clause_module", "domain_principle"}:
        return result
    index = catalog_index.build_catalog_index(skill_root=Path(skill_root) if skill_root else paths.skill_root())
    types = {item["type_id"]: item for item in index.taxonomy.get("catalog_types", ())}
    if kind == "principle":
        selected = result.get("type_id")
        code = types.get(selected, {}).get("type_code") if isinstance(selected, str) else None
        result.pop("type_code", None)
        if code:
            result["type_code"] = code
    elif kind == "clause_module":
        selected = result.get("type_ids", [result["type_id"]] if isinstance(result.get("type_id"), str) else [])
        if not isinstance(selected, list):
            selected = []
        result["type_codes"] = [types[tid]["type_code"] for tid in selected
                                if isinstance(tid, str) and tid in types and types[tid].get("type_code")]
    else:
        code = result.get("domain_code")
        domain = next((item for item in index.taxonomy.get("domains", ()) if item["code"] == code), None)
        result.pop("domain_doctrine_id", None)
        result.pop("coverage", None)
        if domain:
            result["domain_doctrine_id"] = "domain-doctrine-" + code.lower()
            result["domain_name"] = domain["name"]
            members = sorted((item for item in types.values() if item.get("domain_code") == code),
                             key=lambda item: (item.get("type_code") or "", item["type_id"]))
            active = set(index.manifest.get("active_type_ids", ()))
            supported = [item["type_id"] for item in members if item["type_id"] in active]
            unsupported = [item["type_id"] for item in members if item["type_id"] not in active]
            result["coverage"] = {"total_type_count": len(members), "supported_type_count": len(supported),
                                  "coverage_status": "strong" if members and not unsupported else "limited" if supported else "gap",
                                  "supported_type_ids": supported, "unsupported_type_ids": unsupported}
    return result


def reference_options(index) -> list:
    """Existing rule identities only; classifications and sources are not rules."""
    result = {}
    for kind, collection, key in (("clause_module", index.modules, "module_id"),
                                  ("principle", index.type_principles, "doctrine_id"),
                                  ("domain_principle", index.domain_principles, "domain_doctrine_id")):
        for asset in collection:
            identity = asset[key]
            types = asset.get("type_ids") or ([asset["type_id"]] if asset.get("type_id") else [])
            domains = {index.type_to_domain[t] for t in types if t in index.type_to_domain}
            if asset.get("domain_code"):
                domains.add(asset["domain_code"])
            entry = result.setdefault(identity, {
                "value": identity, "runtime_value": asset.get("domain_code") if kind == "domain_principle" else identity,
                "label": asset.get("name") or asset.get("title") or asset.get("clause_group") or asset.get("domain_name") or identity,
                "kind": kind, "type_ids": [], "domain_codes": [],
                "runtime_enabled": False,
            })
            entry["type_ids"] = sorted(set(entry["type_ids"]) | set(types))
            entry["domain_codes"] = sorted(set(entry["domain_codes"]) | domains)
            entry["runtime_enabled"] = entry["runtime_enabled"] or (
                asset.get("approval_status") == "active" and asset.get("activation_status") == "active")
    return sorted(result.values(), key=lambda entry: (entry["label"], entry["value"]))

ROLE_LABELS = dict(zip(
    "advertiser agency any buyer employee employer franchisee franchisor lessee lessor media other_shareholder supplier target_company transferee transferor".split(),
    ["广告主", "代理方", "不限角色", "买方／采购方", "劳动者", "用人单位", "被特许人", "特许人", "承租方", "出租方", "媒体方", "其他股东", "卖方／供应方", "目标公司", "受让方", "转让方"]))
STAGE_LABELS = dict(zip("drafting first_review negotiation signing performance termination settlement".split(),
                       ["起草", "首次审查", "谈判", "签署", "履行", "解除或终止", "结算"]))
SCENE_LABELS = dict(zip(
    "acceptance access_control advance_payment breach breach_remedy buyer_protection certificate charter_restriction closing compensation confidentiality control_transfer defective_contribution delivery delivery_logistics destruction disclosure_letter disguised_consideration dispute dispute_settlement dual_document_price_mismatch earnout eligibility equipment_purchase exit expiry external_transfer fixed_price framework_procurement handover historical_tax_risk information internal_transfer ip_ownership low_declared_price low_price management_handover material_purchase milestone_payment minority_investment monthly mutual_termination non_cash_contribution noncompete operation order_based overdue_contribution ownership_reservation partial_transfer price_adjustment publication region release remedy resignation return review schedule scope seller_protection severance signing social_insurance staged_payment tax_filing_mismatch term termination trade_secret transition_period trigger unpaid_not_due usage use_restriction warranty work_product".split(),
    ["验收", "访问控制", "预付款", "违约", "违约救济", "买方保护", "证明文件", "章程限制", "交割", "补偿", "保密", "控制权移交", "瑕疵出资", "交付", "交付与物流", "销毁", "披露函", "隐含对价", "争议", "争议解决", "多份文件价格不一致", "或有对价", "资格条件", "设备采购", "退出", "到期", "对外转让", "固定价格", "框架采购", "交接", "历史税务风险", "信息", "内部转让", "知识产权归属", "申报价格偏低", "低价", "管理交接", "原材料采购", "里程碑付款", "少数股权投资", "按月安排", "协商解除劳动关系", "非货币出资", "竞业限制", "运营", "订单交易", "逾期出资", "所有权保留", "部分转让", "价格调整", "发布", "地域", "解除限制", "救济", "辞职", "返还", "复核", "进度", "范围", "卖方保护", "离职补偿", "签署", "社会保险", "分期付款", "税务申报不一致", "期限", "解除或终止", "商业秘密", "过渡期", "触发条件", "未届期认缴", "使用", "使用限制", "质保", "工作成果"]))


def vocabulary_options(values, labels):
    # Preserve exact identifiers even if a new vocabulary item has no label yet.
    return [{"value": value, "label": labels.get(value, value)} for value in values]


def field_label(key: str) -> str:
    return LABELS.get(key, key)


def content_sections(body: Mapping[str, Any]) -> list:
    """Group original values without summarising or removing unknown fields."""
    groups = {g["id"]: {"id": g["id"], "title": g["title"], "fields": []} for g in GROUPS}
    for key, value in body.items():
        group = _GROUP_BY_FIELD.get(key, "validation")
        groups[group]["fields"].append({"key": key, "label": field_label(key), "value": copy.deepcopy(value)})
    return [group for group in groups.values() if group["fields"]]


def _resolve(schema: Mapping[str, Any], root: Mapping[str, Any]) -> Dict[str, Any]:
    current = dict(schema)
    ref = current.get("$ref", "")
    if ref.startswith("#/"):
        resolved = root
        for segment in ref[2:].split("/"):
            resolved = resolved[segment]
        return {**resolved, **{k: v for k, v in current.items() if k != "$ref"}}
    return current


def input_format(schema: Mapping[str, Any], *, key: str = "", reference: bool = False) -> str:
    if key == "dependencies_by_type":
        return "先按名称选择适用合同类型，再为该类型多选前置模块；不同类型分别保存。"
    if reference:
        return "按中文名称搜索选择已有规则；编号自动保存，原有未知引用保留待核对。"
    if schema.get("format") == "date-time":
        return "含时区的日期和时间（ISO 8601），例如 2026-10-19T09:00:00+08:00；不是仅填年月日。"
    if schema.get("format") == "date":
        return "日期，使用 YYYY-MM-DD，例如 2026-10-19。"
    if key == "value":
        return "按所选事实或目标的类型填写数值、文字或是/否；列表比较使用 JSON 数组，例如 [5,10]。"
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((v for v in kind if v != "null"), None)
    if schema.get("enum") or kind == "boolean":
        return "从列出的选项选择一项；既有未登记选项会明确标记，不自动替换。"
    if key in ("type_id", "type_ids", "domain_code", "domain_codes", "source_ids", "source_refs", "roles", "scene_tags", "contract_stages", "clause_groups", "fact_key", "target_key"):
        limits = "" if "minItems" not in schema else f"至少 {schema['minItems']} 项。"
        limits += "" if "maxItems" not in schema else f"最多 {schema['maxItems']} 项。"
        return "按中文名称选择，系统保存对应编号；不需要手填技术编号。" + ("可逐项添加多个选择。" if kind == "array" else "") + limits
    if kind == "array":
        limits = "" if "minItems" not in schema else f"至少 {schema['minItems']} 项。"
        limits += "" if "maxItems" not in schema else f"最多 {schema['maxItems']} 项。"
        return "逐项添加，按每项的控件填写。" + limits + ("条目不得重复。" if schema.get("uniqueItems") else "")
    if kind == "object":
        return "按下方子字段逐项填写；未识别的原有扩展字段保留原值，需另行核对。"
    if kind in ("integer", "number"):
        limits = "" if "minimum" not in schema else f"最小值 {schema['minimum']}。"
        limits += "" if "maximum" not in schema else f"最大值 {schema['maximum']}。"
        return ("整数，不填小数。" if kind == "integer" else "数值，使用数字，可含小数。") + limits
    limits = "" if "minLength" not in schema else f"至少 {schema['minLength']} 个字符。"
    limits += "" if "maxLength" not in schema else f"最多 {schema['maxLength']} 个字符。"
    return "文字内容，可分行表述。" + limits


def _example(key: str, schema: Mapping[str, Any], root=None) -> Any:
    schema = _resolve(schema, root or schema)
    if schema.get("format") == "date-time":
        return "2026-10-19T09:00:00+08:00"
    if schema.get("format") == "date":
        return "2026-10-19"
    examples = {
        "review_objectives": ["核对付款节点与合同约定的履行条件是否对应"],
        "review_objective": "核对付款节点与合同约定的履行条件是否对应",
        "action_guidance": ["先核对相关条款，再列出需确认的事实"],
        "judgment_coordinates": ["我方角色", "合同所处阶段"],
        "review_sequence": ["识别相关条款", "核对事实与附件", "确定修改方式"],
        "common_failure_modes": ["正文与附件的约定不一致"],
        "failure_mode": ["同一付款节点在不同条款中表述不一致"],
        "applicability_conditions": ["已识别对应的合同类型和我方角色"],
        "assumptions": ["已取得与该事项有关的合同附件"],
        "placeholders": ["【待确认：履行期限】"],
        "required_evidence": ["相关合同附件或履行记录"],
        "knowledge_gaps": ["尚缺少本情形的可核验来源"],
        "required_source_slots": ["official_law", "historical_revision_or_failure_case"],
        "sample_support_status": "尚无本地代表样本",
        "review_checks": ["核对正文、附件对同一事项是否一致"],
        "assessment_mode": "semantic",
        "natural_language_note": "客户在合同中为乙方且为收款方时适用；须结合合同全文核对主体及收付款安排。",
        "reason_template": "说明本次采用该处理方式的已核实理由",
        "type_id": "type-equipment-material-procurement",
        "type_ids": ["type-equipment-material-procurement"],
        "type_code": "EC-02-002", "type_codes": ["EC-02-002"],
        "domain_code": "EC-02", "domain_codes": ["EC-02"],
        "roles": ["buyer"], "scene_tags": ["acceptance"],
        "clause_groups": ["payment"],
        "dependencies": ["mod-delivery-risk"], "linked_module_ids": ["mod-payment-acceptance"],
        "module_ids": ["mod-payment-acceptance"],
        "clauses": [{"fact_key": "reservation.deposit.ratio", "operator": "gte", "value": 10}],
        "source_ids": ["src-prc-civil-code-2020"], "source_refs": ["src-prc-civil-code-2020"],
        "version": "0.1.0", "title": "本客户付款条件要求", "name": "本类型审查原则",
        "domain_name": "买卖、采购与供应", "topic": "付款与验收衔接",
        "applicability_note": "用于核对付款节点与验收材料之间的对应关系",
        "default_tendencies": ["先核对正文与附件约定的一致性"],
        "override_factors": ["附件中的实际履行安排发生变化时重新核对"],
        "hard_boundaries": ["待核对并列明已有依据支持的不可突破事项"],
        "balanced": "根据已确认的交易安排写明双方的履行配合要求",
        "strong_protection": "列明需优先保障的已确认利益及对应处理方式",
        "concession": "列明经核对可接受的调整，以及调整所需前提",
        "content_status": "已有处理原则，示范文本与材料支撑尚待补充",
        "review_checks_derivation": "由既有审查目标逐项整理，尚待复核覆盖是否充分",
        "clause_group": "付款与验收", "conflicts": ["正文与附件对同一事项作相反约定"],
        "legal_basis_notes": ["说明已引用来源与本检查点的对应关系和适用限制"],
        "recommended_actions": ["核对正文与附件并列明待确认差异"],
        "remediation_steps": ["补取与该事项有关的材料，再确定处理方式"],
        "risk_examples": ["某附件的金额与正文约定不一致，需要进一步核对"],
        "source_family_ids": ["请替换为已有材料的真实样本家族编号"],
        "summary": "本规则用于补充付款节点的材料核对要求",
        "priority_within_class": 50, "fact_key": "reservation.deposit.ratio", "target_key": "output.clause_instruction",
        "supersedes": "1.0.0", "source_status": "已关联来源，适用性与核验记录仍需逐项核对",
    }
    if key in examples:
        return copy.deepcopy(examples[key])
    if "enum" in schema:
        return copy.deepcopy(schema["enum"][0])
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((v for v in kind if v != "null"), None)
    if kind == "array":
        return [_example(key, schema.get("items", {}), root)]
    if kind == "object":
        return {child: _example(child, schema["properties"][child], root)
                for child in schema.get("required", []) if child in schema.get("properties", {})}
    if kind == "boolean":
        return False
    if kind in ("integer", "number"):
        return schema.get("minimum", 0)
    return "请说明该字段的已核实内容与待确认限制"


def get_authoring_schema(kind: str, *, skill_root: Optional[Path] = None) -> Dict[str, Any]:
    if kind == "type_principle":
        kind = "principle"
    if kind not in KINDS:
        raise ValueError("未知规则种类：" + str(kind))
    root = Path(skill_root) if skill_root else paths.skill_root()
    title, relative = KINDS[kind]
    schema = json.loads((root / relative).read_text(encoding="utf-8"))
    fields = []

    def walk(container: Mapping[str, Any], prefix: str = "", ancestors_required: bool = True):
        for key, original in container.get("properties", {}).items():
            item = _resolve(original, schema)
            path = prefix + key
            top = path.split(".")[0]
            local_required = key in container.get("required", ())
            required = ancestors_required and local_required
            system = top in _SYSTEM
            reviewer = top in _REVIEW
            conditions = []
            if prefix and local_required:
                conditions.append("填写“" + field_label(top) + "”时必须提供")
            if path == "scope":
                conditions.append("全部适用时其余范围必须为空；否则至少限定一个维度")
            if path == "condition.clauses":
                conditions.append("由 AI 判断自然语言条件时必须为空；结构化方式中每项须填事实和运算符，exists 不填值，其余必须填值")
            if path == "condition.natural_language_note":
                conditions.append("选择“由 AI 结合合同全文判断”时必填，不能仅填写空白")
            if path == "condition.clauses[].value":
                conditions.append("运算符不是 exists 时必填；exists 时不得填写")
            description = DESCRIPTIONS.get(key) or item.get("description") or "该字段含义尚未补充，请核对原始结构；原有值完整保留，不自动推定业务含义。"
            if system:
                description = DESCRIPTIONS.get(key, "由系统根据实际操作或目录生成，不可手工填写。")
            elif reviewer:
                description = "由真实审核或验证操作记录，不可通过填写表单编造。"
            if path == "directed_block_policy.scope":
                description = "限定本策略影响的业务动作或输出范围，不能扩大到无关合同条款。"
            fields.append({"path": path, "label": field_label(key), "description": description,
                           "example": _example(key, item, schema), "group": _GROUP_BY_FIELD.get(top, "validation"),
                           "input_format": "系统维护，不可手填。" if system or reviewer else input_format(item, key=key, reference=path in REFERENCE_FIELDS),
                           "required": required, "schema_required": local_required,
                           "conditional_required": conditions, "system_generated": system,
                           "managed_by": "system" if system else "reviewer" if reviewer else "user",
                           "allow_empty": item.get("minItems", item.get("minLength", 0)) == 0,
                           "reference_kinds": copy.deepcopy(REFERENCE_FIELDS.get(path, [])),
                           "schema": copy.deepcopy(item)})
            if "properties" in item:
                walk(item, path + ".", required)
            if item.get("type") == "array" and isinstance(item.get("items"), Mapping):
                child = _resolve(item["items"], schema)
                fields.append({"path": path + "[]", "label": field_label(key) + "条目",
                               "description": "填写一项" + field_label(key) + "，其他条目可继续添加。",
                               "example": _example(key, item, schema)[0] if isinstance(_example(key, item, schema), list) and _example(key, item, schema) else _example(key, child, schema),
                               "input_format": "系统维护，不可手填。" if system or reviewer else input_format(child, key=key),
                               "group": _GROUP_BY_FIELD.get(top, "validation"),
                               "required": False, "schema_required": False,
                               "conditional_required": ["添加此条目后应按其格式完整填写"],
                               "system_generated": system,
                               "managed_by": "system" if system else "reviewer" if reviewer else "user",
                               "allow_empty": child.get("minLength", 0) == 0, "schema": copy.deepcopy(child)})
                if "properties" in child:
                    walk(child, path + "[].", False)
    walk(schema)
    index = catalog_index.build_catalog_index(skill_root=root)
    catalog = guard_catalog.build_guard_catalog(None)
    client_schema = json.loads((root / KINDS["client_rule"][1]).read_text(encoding="utf-8"))
    stages = client_schema["properties"]["scope"]["properties"]["contract_stages"]["items"]["enum"]
    return {"kind": kind, "title": title, "schema": schema, "schema_path": relative,
            "groups": copy.deepcopy(GROUPS), "fields": fields,
            "draft_policy": "可保存未完成草稿；原结构缺项与生产内容缺口须分别列明，提交审核前核对。",
            "activation_policy": "维护阅读与保存草稿不授予正式运行资格；审核记录由真实操作产生。",
            "vocabulary": {"roles": sorted(index.valid_roles), "scene_tags": sorted(index.valid_scene_tags),
                           "clause_groups": list(clause_groups.CLAUSE_GROUP_IDS),
                           "clause_group_options": clause_groups.vocabulary_options(),
                           "contract_stages": stages, "fact_keys": copy.deepcopy(list(catalog.fact_keys)),
                           "effect_targets": copy.deepcopy(list(catalog.effect_targets)),
                           "rule_reference_options": reference_options(index),
                           "source_options": [{"value": s["source_id"], "label": s.get("title", s["source_id"])}
                                              for s in sorted(index.sources, key=lambda s: (s.get("title", ""), s["source_id"]))],
                           "role_options": vocabulary_options(sorted(index.valid_roles), ROLE_LABELS),
                           "scene_options": vocabulary_options(sorted(index.valid_scene_tags), SCENE_LABELS),
                           "stage_options": vocabulary_options(stages, STAGE_LABELS)}}
