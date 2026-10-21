"""Recompute selected modules; do not trust a caller-supplied coverage list."""
from scripts.review.plan_contract import get_findings
from scripts.knowledge.select_active_assets import select_assets


def selected_assets(plan):
    context = plan.get('knowledge_selection')
    provenance = plan.get('meta', {}).get('classification_provenance') or {}
    expected = provenance.get('primary_type_id') or provenance.get('proposed_type_id') or plan.get('meta', {}).get('primary_type_id') or plan.get('summary', {}).get('primary_type_id')
    if context is None:
        if expected or (provenance.get('coverage_level') or plan.get('summary',{}).get('coverage_level')) == 'domain_and_type':
            raise ValueError('knowledge_selection 缺失；已声明主类型时必须提供选择上下文并通过分类确认，不得用覆盖层级绕过')
        return None
    if not isinstance(context, dict):
        raise ValueError('knowledge_selection 必须为对象')
    if not context.get('our_role') or context['our_role'] in {'unknown','mixed'}:
        raise ValueError('knowledge_selection.our_role 必须为明确角色')
    if not isinstance(context.get('scene_tags'), list) or not context['scene_tags']:
        raise ValueError('knowledge_selection.scene_tags 必须为非空场景数组')
    if expected and expected != context.get('primary_type_id'):
        raise ValueError('knowledge_selection.primary_type_id 与分类溯源不一致')
    selection = select_assets(primary_type_id=context.get('primary_type_id',''),
        secondary_type_ids=context.get('secondary_type_ids',[]),our_role=context['our_role'],
        scene_tags=context['scene_tags'],classification_status=context.get('classification_status',''),
        confirmed_domain_code=context.get('confirmed_domain_code',''))
    domain_only = provenance.get('review_method') == 'domain_only'
    if domain_only:
        # The review method is explicit provenance, not an asset-enable switch.
        # Recompute first: even one selected specific asset makes this path invalid.
        if (selection['status'] != 'ok' or selection['coverage_level'] != 'domain_only'
                or not selection['domain_principle'] or selection['principles'] or selection['modules']):
            raise ValueError('domain_only 方法与实际选择不符；仅允许公共方法和已启用大类原则，不得混入专项资产或绕过待确认状态')
        if provenance.get('coverage_level') != 'domain_only' or provenance.get('verdict') != 'accepted_with_fallback':
            raise ValueError('domain_only 分类溯源须记录 accepted_with_fallback 和实际覆盖层级，不得复用 rejected 记录')
        if provenance.get('primary_domain_code') and provenance['primary_domain_code'] != selection['primary_domain_code']:
            raise ValueError('domain_only 分类溯源的大类与实际目录不一致')
    from scripts.knowledge.confirm_contract_type import confirm, _load
    confirmed = (provenance.get('human_confirmation') or {}).get('confirmed') is True or provenance.get('human_confirmed') is True
    decision = confirm(title=plan.get('meta',{}).get('contract_name',''),
        proposed=context.get('primary_type_id',''), basis=provenance.get('basis',''),
        our_role=context['our_role'], human_confirmed=confirmed,
        domain_only=domain_only,
        router_initial=provenance.get('router_initial') or {'classification_status':'low'})
    if decision['verdict'] in {'rejected','data_error','needs_human_review'}:
        raise ValueError('knowledge_selection 分类门未放行：'+decision['verdict'])
    if decision['verdict'] == 'accepted_with_reduced_coverage' and provenance.get('verdict') != decision['verdict']:
        raise ValueError('分类溯源须记录 accepted_with_reduced_coverage 并披露未启用依赖')
    related = [m for m in _load('module_catalog.json')['assets']
               if context['primary_type_id'] in m.get('type_ids',[m.get('type_id')])
               and m.get('activation_status') == 'active']
    roles = {role for m in related for role in m.get('roles',[])}
    scenes = {scene for m in related for scene in m.get('scene_tags',[])}
    if not domain_only and roles and 'any' not in roles and context['our_role'] not in roles:
        raise ValueError('knowledge_selection.our_role 未命中该类型角色，不能声明已加载专项模块')
    if not domain_only and scenes and not scenes.intersection(context['scene_tags']):
        raise ValueError('knowledge_selection.scene_tags 未命中该类型场景，不能声明已加载专项模块')
    if context.get('confirmed_domain_code') and context['confirmed_domain_code'] != selection['primary_domain_code']:
        raise ValueError('knowledge_selection.confirmed_domain_code 与主类型不一致')
    for container in (provenance, plan.get('summary', {})):
        if container.get('coverage_level') and container['coverage_level'] != selection['coverage_level']:
            raise ValueError('coverage_level 与实际选择结果不一致：'+selection['coverage_level'])
    return selection


def selected_modules(plan):
    selection = selected_assets(plan)
    return selection['modules'] if selection else []


def validate_module_checklist(plan):
    modules = selected_modules(plan)
    checklist = plan.get('module_checklist')
    if not modules and checklist is None:
        return
    if not isinstance(checklist, dict):
        raise ValueError('module_checklist 缺失；每个选中模块须登记检查结论')
    expected = {m['module_id'] for m in modules}
    if set(checklist) != expected:
        raise ValueError('module_checklist 与实际选中模块不一致；缺少 '+str(sorted(expected-set(checklist)))+'；多出 '+str(sorted(set(checklist)-expected)))
    findings = {f.get('id') for f in get_findings(plan) if isinstance(f,dict)}
    for mid, entry in checklist.items():
        if not isinstance(entry,dict) or entry.get('status') not in {'checked','not_applicable'}:
            raise ValueError(f'module_checklist.{mid}.status 必须为 checked/not_applicable')
        if not isinstance(entry.get('reason'),str) or not entry['reason'].strip():
            raise ValueError(f'module_checklist.{mid}.reason 必填（包括已检查但未发现问题的理由）')
        ids = entry.get('finding_ids')
        if not isinstance(ids,list) or any(not isinstance(x,str) or x not in findings for x in ids):
            raise ValueError(f'module_checklist.{mid}.finding_ids 必须为存在的 finding ID 数组；无问题填 []')


def coverage_gaps(plan):
    return [m.get('clause_group') or '尚无具体检查项的议题' for m in selected_modules(plan)
            if not m.get('review_checks') or str(m.get('content_status','')).startswith('stub_')]
