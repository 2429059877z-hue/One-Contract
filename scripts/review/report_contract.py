"""Report input contract and bounded presentation cleanup.

Execution-only legacy plans remain loadable; rendering requires real risk text.
Never clean IDs, selectors, source text, replacements, or evidence references.
"""
from scripts.review.plan_contract import get_findings
from copy import deepcopy
import re

PRESENTATION_FIELDS = {
    'title', 'risk', 'description', 'comment', 'rationale', 'suggestion', 'fix',
    'tax_advice', 'core_conclusion', 'conclusion', 'overall_opinion',
    'key_recommendations', 'transaction_content', 'business_overview',
    'rights_obligations', 'price_overview', 'payment_terms', 'contract_type',
    'rights_allocation_changes', 'text',
}


def plain_presentation(value):
    if isinstance(value, str):
        return re.sub(r'\*\*(.+?)\*\*|__(.+?)__|`([^`]+)`',
                      lambda m: next(g for g in m.groups() if g is not None), value)
    if isinstance(value, list):
        return [plain_presentation(item) if isinstance(item, str) else item for item in value]
    return value


def prepare_report_plan(plan):
    result = deepcopy(plan)
    containers = [result.get('summary', {}), result.get('meta', {})]
    containers += get_findings(result)
    if isinstance(result.get('responses'), list):
        containers += result['responses']
    for container in containers:
        if isinstance(container, dict):
            for key in PRESENTATION_FIELDS & container.keys():
                container[key] = plain_presentation(container[key])
    return result


SUMMARY_FIELDS = frozenset(
    'amount business_model business_overview conclusion confirmed_domain_code contract_amount contract_name contract_summary contract_term contract_type core_conclusion core_rights_obligations coverage_assets coverage_level deal_overview duration key_milestones key_recommendations milestones missing_fields missing_information obligations_overview opinion overall_opinion overall_risk overview parties party_a party_a_name party_b party_b_name party_role payment_arrangement payment_terms pending_items price_overview price_terms pricing_terms primary_domain_code primary_type_id review_opinion review_version rights_allocation_changes rights_and_obligations rights_obligations rights_obligations_summary risk_level role signing_date stub_topics tax_advice tax_notes tax_recommendation tax_recommendations tax_suggestion tax_suggestions term timeline title to_fill_items total_amount transaction_content transaction_profile version 乙方 待填事项 涉税建议 甲方'.split()
)

def _has_text(value):
    if isinstance(value, str): return bool(value.strip())
    if isinstance(value, list): return bool(value) and all(_has_text(x) for x in value)
    return False


def validate_report_contract(plan):
    from scripts.review.module_checklist import validate_module_checklist
    errors = []
    summary = plan.get('summary', {})
    if not isinstance(summary, dict):
        errors.append('summary 必须为对象')
        summary = {}
    meta = plan.get('meta', {})
    if not isinstance(meta, dict):
        errors.append('meta 必须为对象')
        meta = {}
    try:
        validate_module_checklist(dict(plan, summary=summary, meta=meta))
    except (ValueError, TypeError, AttributeError) as exc:
        errors.append(str(exc))
    unknown = set(summary) - SUMMARY_FIELDS
    if unknown:
        errors.append('summary 存在不被渲染的字段：'+', '.join(sorted(unknown))+'；请使用 schema 中的合同概况字段')
    try:
        findings = get_findings(plan)
    except ValueError as exc:
        errors.append(str(exc))
        findings = []
    for index, item in enumerate(findings):
        if not isinstance(item, dict):
            errors.append(f'findings[{index}] 必须为对象'); continue
        if not any(_has_text(item.get(k)) for k in ('legal_basis','basis','source_references','sources')):
            errors.append(f'findings[{index}].legal_basis 必填依据或来源状态')
        if not any(_has_text(item.get(k)) for k in ('suggestion','fix','comment','rationale','tax_advice','replacement_text','recommended_text','insert_text')):
            errors.append(f'findings[{index}] 缺少审查意见或可执行修改方向')
        if item.get('rights_allocation_change') is True:
            changes = summary.get('rights_allocation_changes') or []
            if not isinstance(changes, list) or not any(isinstance(change,dict) and change.get('finding_id') == item.get('id')
                       for change in changes):
                errors.append(f'findings[{index}] 改变权利归属，rights_allocation_changes 须提供对应 finding_id 的结构化披露')
        # Tax-only entries are rendered through their own structured advice.
        if item.get('report_bucket') == 'tax':
            if not _has_text(item.get('tax_advice')):
                errors.append(f'findings[{index}].tax_advice 不得为空')
            continue
        if item.get('report_bucket') != 'tax':
            for names, label in [(('title', 'risk'), 'title'),
                                 (('risk', 'description'), 'risk（风险说明）'),
                                 (('clause', 'clause_position', 'target_text'), 'clause（条款位置；缺失条款须明示）')]:
                if not any(isinstance(item.get(k), str) and item[k].strip() for k in names):
                    errors.append(f'findings[{index}].{label} 不得为空')
        if item.get('rights_allocation_change') is True:
            changes = summary.get('rights_allocation_changes')
            if not changes:
                errors.append('改变权利归属时 summary.rights_allocation_changes 必填')
    changes = summary.get('rights_allocation_changes')
    if changes is not None:
        if not isinstance(changes, list):
            errors.append('summary.rights_allocation_changes 必须为数组')
        else:
            for change in changes:
                if isinstance(change, str) and change.strip():
                    continue  # 兼容未标记 finding 的历史首部提示。
                if not isinstance(change,dict) or not all(_has_text(change.get(k)) for k in ('finding_id','original','revised','reason','alternative')):
                    errors.append('rights_allocation_changes 须提供 finding_id/original/revised/reason/alternative')
                elif change['finding_id'] not in {f.get('id') for f in findings if isinstance(f,dict)}:
                    errors.append('rights_allocation_changes.finding_id 不存在')
    if errors:
        from scripts.review.preflight import PlanValidationError, diagnostic
        diagnostics = {'errors': [], 'warnings': []}
        for cause in errors:
            match = re.search(r'findings\[(\d+)\]', cause)
            index = int(match.group(1)) if match else None
            diagnostics['errors'].append(diagnostic('报告输入不完整：' + cause,
                index=index, finding=findings[index] if index is not None else None,
                code='REPORT_INPUT_INVALID',
                path=f'findings[{index}]' if index is not None else 'report'))
        raise PlanValidationError(diagnostics)


def rights_change_lines(summary):
    lines = []
    for item in summary.get('rights_allocation_changes', []):
        if isinstance(item, str):
            lines.append('- ' + plain_presentation(item))
        else:
            # IDs are for traceability only; only human-readable text is exposed.
            lines.append('- 原约定：' + plain_presentation(item['original']))
            lines.append('  - 改为：' + plain_presentation(item['revised']))
            lines.append('  - 理由：' + plain_presentation(item['reason']))
            alternative = plain_presentation(item['alternative']).strip()
            # Strip only a duplicated presentation label; a genuine “如甲方…”
            # condition must retain its legal meaning.
            alternative = re.sub(r'^(?:如不同意\s*[:：，,]\s*)+', '', alternative)
            label = '替代方案' if alternative.startswith(('如', '若', '如果')) else '如不同意'
            lines.append(f'  - {label}：' + alternative)
    return lines
