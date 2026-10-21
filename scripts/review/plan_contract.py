"""Canonical finding list, independent of optional strategy enrichment."""
from copy import deepcopy


def get_findings(plan):
    for key in ('findings', 'risks'):
        if key in plan and not isinstance(plan[key], list):
            raise ValueError(f'{key} 必须是数组')
    current, legacy = plan.get('findings', []), plan.get('risks', [])
    if current and legacy and current != legacy:
        raise ValueError('findings 与 risks 同时存在且内容不同；请保留一份审查意见')
    return current or legacy


def canonical_plan(plan):
    findings = get_findings(plan)
    result = deepcopy(plan)
    result['findings'] = deepcopy(findings)
    result.pop('risks', None)
    return result
