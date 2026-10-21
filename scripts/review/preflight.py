"""Collect ordered edit diagnostics on a disposable, transactional source copy."""
from copy import deepcopy
from scripts.review.plan_contract import get_findings
from pathlib import Path
import shutil
import tempfile
import zipfile
from defusedxml.minidom import parseString
from scripts.docx_engine.revision_views import _safe_extract, resolve_unpacked_revisions
from scripts.docx_engine.reviewer import ContractReviewer
from scripts.review.action_executor import apply_finding
from scripts.review.comment_hygiene import resolve_orphan_comments


class PlanValidationError(ValueError):
    """A compatible ValueError carrying all machine-readable plan diagnostics."""

    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        lines = []
        for error in diagnostics['errors']:
            location = error.get('path') or 'plan'
            if error.get('finding_id'):
                location += f" ({error['finding_id']})"
            lines.append(f"{location}：{error['cause']}；建议：{error['suggestion']}")
        super().__init__('审查计划校验/预检失败；未生成交付文件：\n- ' + '\n- '.join(lines))


def diagnostic(cause, *, index=None, finding=None, code='PLAN_VALIDATION_ERROR',
               path=None, suggestion=None):
    return {
        'code': code,
        'finding_id': str(finding.get('id') or '') if isinstance(finding, dict) else None,
        'finding_index': index,
        'path': path or (f'findings[{index}]' if index is not None else 'plan'),
        'cause': str(cause),
        'suggestion': suggestion or repair_suggestion(str(cause)),
    }


def repair_suggestion(cause):
    lower = cause.lower()
    if 'occurrence' in lower or 'multiple' in lower or '不唯一' in cause:
        return ('在执行时的接受视图核对剩余匹配数，补充或调整 occurrence/selector；'
                '前一项已改掉第一处后，下一项通常仍应使用 occurrence=1。')
    if 'not found' in lower or 'no ' in lower or '未找到' in cause or '找不到' in cause:
        return ('核对接受视图中的 target_text/selector 和执行顺序；'
                '若锚点由前项生成，先修复该前项后重新预检。')
    if '修订' in cause or 'revision' in lower or 'insertion' in lower:
        return ('缩小 target_text 至可安全定位的范围；涉及历史修订时核对作者和结构，'
                '按委托要求选择 accept-existing，或保留原文并批注。')
    if 'pending_kind' in cause:
        return '将 pending_kind 改为 fill、verify、authorization 之一，或删除不适用的字段。'
    if 'risk_level' in cause:
        return '将 risk_level 改为 P0、P1、P2 之一。'
    if 'evidence_state' in cause:
        return '核对事实与授权后使用 confirmed、reasonable_assumption、major_choice 或 not_applicable。'
    return '按原因补齐或更正该字段；修复全部错误后重新预检。'


def replacement_range_warnings(plan):
    """Advisory only: common unchanged edges do not need wholesale redlining."""
    from scripts.review.action_executor import (
        infer_auto_action, normalize_action, resolve_delivery_action, resolve_replacement_text,
    )
    warnings = []
    policy = plan.get('meta', {}).get('edit_policy', 'revise-first')
    for index, finding in enumerate(get_findings(plan)):
        if not isinstance(finding, dict):
            continue
        try:
            requested = normalize_action(finding.get('action'), default='auto')
            action = infer_auto_action(finding, edit_policy=policy) if requested == 'auto' else requested
            action = resolve_delivery_action(finding, requested_action=requested, action=action, edit_policy=policy)
            if action != 'replace':
                continue
            target = finding.get('target_text') or finding.get('search')
            replacement = resolve_replacement_text(finding, edit_policy=policy)
            if not isinstance(target, str) or replacement is None or len(target) < 32:
                continue
            prefix = 0
            while prefix < min(len(target), len(replacement)) and target[prefix] == replacement[prefix]:
                prefix += 1
            suffix = 0
            while (suffix < min(len(target), len(replacement)) - prefix
                   and target[-1-suffix] == replacement[-1-suffix]):
                suffix += 1
            unchanged = prefix + suffix
            if unchanged < 24 or unchanged / len(target) < .65:
                continue
            warning = diagnostic(
                f'target_text 共 {len(target)} 字，其中 {unchanged} 字为未变化的公共前后缀，修订痕迹可能过宽',
                index=index, finding=finding, code='EXCESSIVE_REPLACE_RANGE',
                suggestion=('保留必要的唯一定位上下文，缩小 target_text 与 replacement_text 的共同前后缀；'
                            '追加内容可使用短尾锚。须在执行时接受视图核对唯一性，不自动改写计划。'))
            warning.update(target_length=len(target), common_prefix_length=prefix,
                           common_suffix_length=suffix, changed_source_length=len(target)-unchanged,
                           changed_replacement_length=len(replacement)-unchanged)
            warnings.append(warning)
        except (TypeError, ValueError):
            continue  # Invalid strategy fields are reported by validation.
    return warnings


def _clone_reviewer(reviewer, destination):
    """Isolate the backing files as well as DOM and lazy editor state.

    Document owns its temp directory and removes it in __del__. A plain deep
    copy would share that directory and both comments and cleanup would leak
    across transactions. Remap paths *during* copying, before any destructor.
    """
    old_root = Path(reviewer.doc.temp_dir)
    shutil.copytree(old_root, destination)
    memo = {id(reviewer.doc.temp_dir): str(destination)}
    for owner in (reviewer.doc, *reviewer.doc._editors.values()):
        for value in vars(owner).values():
            if isinstance(value, Path) and value.is_relative_to(old_root):
                memo[id(value)] = destination / value.relative_to(old_root)
    # minidom has parent/previous/next links: deepcopy recurses through sibling
    # chains and overflows on normal 100+ paragraph contracts. Reparse each DOM
    # once and memoize it, preserving original selector line positions.
    for editor in reviewer.doc._editors.values():
        cloned_dom = parseString(editor.dom.toxml(encoding='utf-8'))
        memo[id(editor.dom)] = cloned_dom
        for original, cloned in zip(editor.dom.getElementsByTagName('*'), cloned_dom.getElementsByTagName('*')):
            memo[id(original)] = cloned
            if hasattr(original, 'parse_position'):
                cloned.parse_position = original.parse_position
    return deepcopy(reviewer, memo)


def preflight_plan(plan, source_docx, *, existing_revisions='reject', author=None,
                   skip_indices=(), raise_on_error=True):
    diagnostics = {'errors': [], 'warnings': replacement_range_warnings(plan)}
    with tempfile.TemporaryDirectory(prefix='one-contract-preflight-') as temporary:
        root = Path(temporary)/'source'
        with zipfile.ZipFile(source_docx) as archive:
            _safe_extract(archive, root)
        if existing_revisions == 'accept-existing':
            resolve_unpacked_revisions(root, 'accept', prune_comments=False)
        resolve_orphan_comments(root, export_path=Path(temporary)/'orphans.json')
        reviewer = ContractReviewer(root, author=author or plan.get('meta', {}).get('reviewer') or '预检', initials='PF')
        failed_ids = []
        for index, finding in enumerate(get_findings(plan)):
            if index in skip_indices:
                failed_ids.append(str(finding.get('id') or index) if isinstance(finding, dict) else str(index))
                continue
            try:
                if not isinstance(finding, dict):
                    raise ValueError('finding 必须是对象')
                for location in (finding, finding.get('selector')):
                    if not isinstance(location, dict):
                        continue
                    occurrence = location.get('occurrence')
                    if occurrence is not None and (type(occurrence) is not int or occurrence < 1):
                        raise ValueError('occurrence 必须为从 1 开始的整数')
                # Commit DOM, comment IDs and caches only after success. No save.
                trial = _clone_reviewer(reviewer, Path(temporary) / f'action-{index}')
                result = apply_finding(trial, finding,
                    edit_policy=plan.get('meta',{}).get('edit_policy','revise-first'))
                if result.get('status') == 'failed':
                    raise ValueError(result.get('message','执行失败'))
                reviewer = trial
            except Exception as exc:
                error = diagnostic(exc, index=index, finding=finding, code='PREFLIGHT_ACTION_FAILED')
                if failed_ids:
                    error['prior_failed_findings'] = list(failed_ids)
                    error['suggestion'] += ' 前序失败项未应用，若本项依赖前项生成的内容，请一并修复后重跑。'
                diagnostics['errors'].append(error)
                failed_ids.append(str(finding.get('id') or index) if isinstance(finding, dict) else str(index))
    if diagnostics['errors'] and raise_on_error:
        raise PlanValidationError(diagnostics)
    return diagnostics
