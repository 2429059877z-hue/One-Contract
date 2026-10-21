"""Contract task bridge: pinned rules -> Agent plan -> real engine -> visual acceptance.

This repository never invents a review plan or treats a rules preview as a
completed contract review. Delivery checks are recomputed on stored bytes.
"""
from __future__ import annotations
import copy
from datetime import datetime, timezone
from functools import wraps
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
import zipfile

from one_contract_rules import models, paths, private_fs, resolver
from one_contract_rules import platform_fs
from one_contract_rules.transaction_lock import profile_lock
from render_tools import diagnose_rendering_tools

SKILL_ROOT = Path(__file__).resolve().parents[2]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))
TASK_ID = re.compile(r'^task_[a-f0-9]{32}$')
MAX_DOCX_BYTES = 64 * 1024 * 1024
VISUAL_CHECKS = ('no_clipping', 'no_overlap', 'no_missing_text', 'fonts_checked',
                 'tables_headers_numbering_checked', 'non_target_format_preserved', 'accepted_rejected_checked')


class TaskError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _hash_file(path):
    return _hash_bytes(path.read_bytes())


def _write_bytes(path, value):
    private_fs.secure_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        private_fs.secure_path(path)
        handle.write(value); handle.flush(); os.fsync(handle.fileno())


def _mutating(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        if self.read_only:
            raise TaskError('工作台以只读模式运行，未修改任务。')
        with profile_lock(self.root):
            return method(self, *args, **kwargs)
    return call


def _docx_bytes(value):
    if not isinstance(value, bytes) or not value or len(value) > MAX_DOCX_BYTES:
        raise TaskError('请提供不超过 64 MiB 的真实 DOCX 文件。')
    try:
        with zipfile.ZipFile(io.BytesIO(value)) as package:
            infos = package.infolist()
            names = [x.filename for x in infos]
            if len(names) != len(set(names)) or len(names) > 10000 or sum(x.file_size for x in infos) > 256 * 1024 * 1024:
                raise TaskError('DOCX 包内容异常或解压体积过大。')
            if any(name.startswith('/') or '\\' in name or '..' in name.split('/') for name in names):
                raise TaskError('DOCX 包含不安全路径。')
            if not {'[Content_Types].xml', 'word/document.xml'}.issubset(names):
                raise TaskError('文件缺少 DOCX 正文部件。')
            if any('vbaproject' in n.lower() for n in names):
                raise TaskError('此任务入口仅接收不含宏的 DOCX。')
    except zipfile.BadZipFile as exc:
        raise TaskError('文件不是可读取的 DOCX。') from exc


class TaskRepository:
    def __init__(self, *, data_root, public_repository, client_repository, read_only=False):
        self.data_root = Path(data_root).expanduser().resolve()
        self.root = self.data_root / 'tasks'
        if os.path.lexists(self.root) and (self.root.is_symlink() or getattr(self.root.lstat(),'st_file_attributes',0) & 0x400):
            raise TaskError('任务根目录不能是符号链接或重解析点。')
        self.public = public_repository
        self.clients = client_repository
        self.read_only = bool(read_only)
        if not read_only:
            private_fs.secure_dir(self.root)
            # All execution holds the task-root lock; an executing record left after
            # acquiring it belongs to an interrupted process, never a live worker.
            with profile_lock(self.root):
                for directory in self.root.iterdir():
                    if TASK_ID.fullmatch(directory.name) and (directory/'task.json').is_file():
                        task = self._load(directory.name)
                        if task['state'] == 'executing':
                            task.update(state='blocked', errors=['上次执行中断，保留中间文件；请重新执行产生新的尝试。'])
                            self._save(task)

    def _directory(self, task_id):
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            raise TaskError('任务编号无效。')
        try:
            return paths.contained_path(self.root, task_id)
        except paths.PathContractError as exc:
            raise TaskError('任务路径越界。') from exc

    def _file(self, directory, relative):
        if not isinstance(relative, str) or '\\' in relative:
            raise TaskError('任务文件路径无效。')
        parts = relative.split('/')
        try:
            return paths.contained_path(directory, *parts)
        except paths.PathContractError as exc:
            raise TaskError('任务文件路径越界。') from exc

    def _save(self, task):
        task['revision'] = task.get('revision', 0) + 1
        task['updated_at'] = _now()
        task['content_sha256'] = models.content_hash(task)
        private_fs.atomic_write_json(self._directory(task['task_id'])/'task.json', task)
        return copy.deepcopy(task)

    def _load(self, task_id):
        directory = self._directory(task_id)
        path = self._file(directory, 'task.json')
        if not path.is_file(): raise TaskError('任务不存在。')
        task = json.loads(path.read_text(encoding='utf-8'))
        if task.get('task_id') != task_id or task.get('content_sha256') != models.content_hash(task):
            raise TaskError('任务记录的归属或内容校验失败。')
        for item in (task['input'], task['context_file'], task['effective_rules_file'], *task.get('confirmations', [])):
            if _hash_file(self._file(directory, item['path'])) != item['sha256']:
                raise TaskError('任务原件或固定规则内容已改变，不能继续交付。')
        if task.get('plan') and _hash_file(self._file(directory, task['plan']['path'])) != task['plan']['sha256']:
            raise TaskError('任务审查计划校验失败。')
        if task.get('state') == 'ready':
            self._verify_delivery_files(task)
        return task

    def _check_revision(self, task, expected_revision):
        if type(expected_revision) is not int or task['revision'] != expected_revision:
            raise TaskError('任务已改变，请重新载入当前版本后操作。')

    def _check_pin(self, task):
        effective = json.loads((self._directory(task['task_id'])/task['effective_rules_file']['path']).read_text(encoding='utf-8'))
        if resolver.resolution_has_blockers(effective):
            raise TaskError('规则来源、启用状态或硬边界检查尚未通过；请按具体原因处理后重新创建任务。')
        pin = task['pin']
        if pin['public_asset_fingerprint'] != self.public.public_asset_fingerprint:
            raise TaskError('公共规则已更新；本任务保留原固定版本，请重新创建并明确选择新版本。')
        if pin.get('client_profile_id'):
            snapshot = self.clients.load_snapshot(pin['client_profile_id'], pin['client_policy_snapshot_id'])
            if snapshot['content_sha256'] != pin['client_policy_snapshot_sha256']:
                raise TaskError('固定客户快照已损坏或不匹配。')

    @_mutating
    def create_task(self, *, input_name, input_bytes, context, title, actor):
        _docx_bytes(input_bytes)
        if not isinstance(input_name, str) or Path(input_name).name != input_name or '\\' in input_name or not input_name.lower().endswith('.docx'):
            raise TaskError('原件名称须为不含路径的 .docx 文件名。')
        if not isinstance(context, dict) or not context.get('primary_type_id') or not context.get('our_role'):
            raise TaskError('请明确合同类型与我方角色；客户档案必须由用户明确选择。')
        context = copy.deepcopy(context)
        from one_contract_rules.clause_groups import validate_review_scope
        review_scope, selected_groups = validate_review_scope(context)
        context.update(review_scope=review_scope, clause_groups=list(selected_groups))
        engine = resolver.Resolver(public_repository=self.public, client_repository=self.clients)
        effective = engine.resolve_effective_rules(context).to_dict()
        snapshot = None
        if effective.get('client_profile_id') and effective.get('client_policy_snapshot_id'):
            snapshot = self.clients.load_snapshot(effective['client_profile_id'], effective['client_policy_snapshot_id'])
            context['client_policy_snapshot_id'] = snapshot['snapshot_id']
            effective = engine.resolve_effective_rules(context).to_dict()
        pin = {'public_snapshot_id': self.public.public_snapshot_id,
               'public_asset_fingerprint': self.public.public_asset_fingerprint,
               'resolver_semantics_version': self.public.resolver_semantics_version,
               'client_profile_id': effective.get('client_profile_id'),
               'client_policy_snapshot_id': effective.get('client_policy_snapshot_id'),
               'client_policy_snapshot_sha256': snapshot.get('content_sha256') if snapshot else None,
               'result_hash': effective['result_hash'], 'context_hash': effective['context_hash']}
        task_id = 'task_' + uuid.uuid4().hex
        directory = self._directory(task_id)
        private_fs.secure_dir(directory)
        try:
            _write_bytes(directory/'input.docx', input_bytes)
            from scripts.docx_engine.quality_gate import inspect_docx
            source_check = inspect_docx(directory/'input.docx')
            if source_check['status'] != 'PASS':
                raise TaskError('原件未通过 DOCX 结构检查：' + '; '.join(source_check.get('errors', [])))
            private_fs.atomic_write_json(directory/'context.json', context)
            private_fs.atomic_write_json(directory/'effective-rules.json', effective)
            blocked = resolver.resolution_has_blockers(effective)
            task = {'schema_version': 1, 'task_id': task_id, 'revision': 0,
                    'title': str(title or input_name)[:200], 'created_by': str(actor)[:80], 'created_at': _now(),
                    'state': 'awaiting_confirmation' if blocked else 'awaiting_review', 'resolution_blocked': blocked,
                    'input': {'name': input_name, 'path': 'input.docx', 'sha256': _hash_bytes(input_bytes)},
                    'context_file': {'path': 'context.json', 'sha256': _hash_file(directory/'context.json')},
                    'effective_rules_file': {'path': 'effective-rules.json', 'sha256': _hash_file(directory/'effective-rules.json')},
                    'pin': pin, 'pin_sha256': models.content_hash(pin), 'plan': None,
                    'attempts': [], 'confirmations': [], 'delivery': None, 'errors': []}
            return self._save(task)
        except Exception:
            shutil.rmtree(directory)
            raise

    def get_task(self, task_id):
        return copy.deepcopy(self._load(task_id))

    def list_tasks(self):
        if not self.root.exists(): return []
        tasks = []
        for directory in self.root.iterdir():
            if TASK_ID.fullmatch(directory.name) and (directory/'task.json').is_file():
                task = self._load(directory.name)
                tasks.append({key: task[key] for key in ('task_id', 'title', 'state', 'revision', 'created_at', 'updated_at')})
        return sorted(tasks, key=lambda x: x['created_at'], reverse=True)

    def _binding(self, task):
        return {'task_id': task['task_id'], 'input_sha256': task['input']['sha256'],
                'pin_sha256': task['pin_sha256'],
                'plan_sha256': task['plan']['sha256'] if task.get('plan') else None}

    def _client_rules(self, task):
        """Read every rule in the exact verified snapshot, including exclusions."""
        pin = task['pin']
        if not pin.get('client_profile_id') or not pin.get('client_policy_snapshot_id'):
            return []
        snapshot = self.clients.load_snapshot(pin['client_profile_id'], pin['client_policy_snapshot_id'])
        if snapshot['content_sha256'] != pin['client_policy_snapshot_sha256']:
            raise TaskError('固定客户快照校验失败。')
        engine = resolver.Resolver(public_repository=self.public, client_repository=self.clients)
        rules = engine._clients_snapshot_rules(snapshot)
        effective = json.loads((self._directory(task['task_id'])/'effective-rules.json').read_text(encoding='utf-8'))
        included = {r['rule_id'] for r in effective['effective_rules'] if r['source_layer'] == 'client'}
        excluded = {r['rule_id']:r.get('reason', '') for r in effective['excluded_rules']}
        notices = [c for c in effective.get('rule_conflicts', []) if resolver.is_application_review_notice(c)]
        return [{'rule_id':r['client_rule_id'], 'content_hash':r['content_sha256'],
                 'rule_payload':r, 'included':r['client_rule_id'] in included,
                 'application_review':[n for n in notices if r['client_rule_id'] in n['rule_ids']],
                 'exclusion_reason':excluded.get(r['client_rule_id'])} for r in rules]

    @staticmethod
    def _context_value(context, pointer):
        if not isinstance(pointer, str) or not pointer.startswith('/'):
            raise TaskError('上下文证据须提供 JSON pointer（例如 /our_role）。')
        value = context
        try:
            for key in pointer[1:].split('/'):
                key = key.replace('~1', '/').replace('~0', '~')
                value = value[int(key)] if isinstance(value, list) else value[key]
        except (ValueError, KeyError, IndexError, TypeError):
            raise TaskError('上下文证据不存在：' + pointer) from None
        return value

    def _confirmation_binding(self, task, rule, purpose):
        return {'rule_id':rule['rule_id'], 'content_hash':rule['content_hash'],
                'client_profile_id':task['pin']['client_profile_id'],
                'client_policy_snapshot_id':task['pin']['client_policy_snapshot_id'],
                'input_sha256':task['input']['sha256'], 'purpose':purpose}

    def _confirmation_matches(self, task, rule, purpose, value):
        if not isinstance(value,dict): return False
        return (value.get('confirmed') is True
                and all(value.get(key)==expected for key,expected in self._confirmation_binding(task,rule,purpose).items())
                and all(isinstance(value.get(k),str) and value[k].strip() for k in ('confirmed_by','confirmed_at','decision')))

    def _task_confirmation(self, task, evidence_id):
        item=next((i for i in task.get('confirmations',[]) if i['evidence_id']==evidence_id),None)
        if item is None: raise TaskError('人工确认记录不属于当前任务。')
        path=self._file(self._directory(task['task_id']),item['path'])
        if _hash_file(path)!=item['sha256']: raise TaskError('人工确认记录已改变。')
        value=json.loads(path.read_text(encoding='utf-8'))
        if value.get('task_id')!=task['task_id'] or value.get('evidence_id')!=evidence_id:
            raise TaskError('人工确认记录归属不匹配。')
        return value

    @_mutating
    def record_rule_confirmation(self, task_id, rule_id, *, purpose, decision, confirmed_by,
                                 confirmation, expected_revision, actor):
        task=self._load(task_id); self._check_revision(task,expected_revision); self._check_pin(task)
        rules={r['rule_id']:r for r in self._client_rules(task)}
        if rule_id not in rules: raise TaskError('规则不属于此任务固定客户快照。')
        if not isinstance(purpose,str) or purpose not in {'not_applicable','escalation'}: raise TaskError('人工确认用途无效。')
        if (confirmation is not True or not isinstance(decision,str) or not decision.strip()
                or not isinstance(confirmed_by,str) or not confirmed_by.strip()):
            raise TaskError('须由用户填写真实确认人、具体决定并明确勾选确认。')
        if purpose=='escalation' and (rules[rule_id]['rule_payload'].get('effect') or {}).get('action')!='escalate':
            raise TaskError('该规则没有人工升级审核效果，不能记录升级完成来替代正文落实。')
        evidence_id='confirmation_'+uuid.uuid4().hex
        value={**self._confirmation_binding(task,rules[rule_id],purpose),
               'evidence_id':evidence_id,'task_id':task_id,'confirmed':True,
               'confirmed_by':confirmed_by.strip(),'confirmed_at':_now(),'decision':decision.strip(),
               'recorded_by':str(actor)[:80]}
        relative='confirmations/'+evidence_id+'.json';path=self._directory(task_id)/relative
        _write_bytes(path,(json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode('utf-8'))
        task.setdefault('confirmations',[]).append({'evidence_id':evidence_id,'rule_id':rule_id,'purpose':purpose,'path':relative,'sha256':_hash_file(path)})
        task.update(state='awaiting_confirmation',delivery=None,
                    errors=['已保存人工确认；请更新并重新提交引用该证据的审查计划，不会自动改变处置结论。'])
        return {'confirmation':value,'task':self._save(task)}

    def _check_dispositions(self, task, plan, *, execution=None, delivery=None, reviewed=None, report=None):
        """Verify traceability and exact evidence; legal meaning remains a lawyer judgment."""
        rules = {r['rule_id']:r for r in self._client_rules(task)}
        rows = (plan.get('meta') or {}).get('client_rule_dispositions', [])
        if not isinstance(rows, list): raise TaskError('client_rule_dispositions 须为逐条处理记录列表。')
        if not rules and not rows: return {'records':[], 'pending_rule_ids':[], 'pending_reasons':{}}
        if any(not isinstance(r, dict) or not isinstance(r.get('rule_id'), str) for r in rows):
            raise TaskError('每条客户处理记录须有 rule_id。')
        if len({r['rule_id'] for r in rows}) != len(rows) or {r['rule_id'] for r in rows} != set(rules):
            raise TaskError('客户规则处理记录须完整覆盖固定快照，且不得重复或引用其他规则：' + ', '.join(sorted(rules)))
        from scripts.review.delivery_checks import inspect_view
        root = self._directory(task['task_id'])
        original = inspect_view(root/'input.docx', 'accept')['paragraphs']
        accepted = inspect_view(reviewed, 'accept')['paragraphs'] if reviewed else None
        report_text = inspect_view(report, 'accept')['public_text'] if report else None
        context = json.loads((root/'context.json').read_text(encoding='utf-8'))
        findings = {f['id']:f for f in plan['findings']}
        outcomes = {r.get('id'):r for r in (execution or {}).get('results', []) if isinstance(r, dict)}
        edits = {c['id']:c for c in (delivery or {}).get('edit_checks', [])}
        from one_contract_rules.clause_groups import CLAUSE_GROUP_IDS, validate_review_scope
        review_scope, selected_groups = validate_review_scope(context)
        pending = {}
        def needs_confirmation(rule_id, message):
            pending.setdefault(rule_id, []).append(message)
        for row in rows:
            rule_id = row['rule_id']; rule = rules[rule_id]
            status = row.get('status'); prefix = '客户规则 ' + rule_id + '：'
            if row.get('content_hash') != rule['content_hash']: raise TaskError(prefix + '规则内容校验值不匹配。')
            if not isinstance(status,str) or status not in {'applied','already_satisfied','commented','requires_confirmation','not_applicable'}:
                raise TaskError(prefix + '处理状态无效。')
            if not isinstance(row.get('explanation'), str) or not row['explanation'].strip():
                raise TaskError(prefix + '须解释本合同中如何处理该规则。')
            ids = row.get('finding_ids')
            if not isinstance(ids,list) or any(not isinstance(i,str) or i not in findings for i in ids) or len(set(ids)) != len(ids):
                raise TaskError(prefix + 'finding_ids 必须引用计划中真实且不重复的审查事项。')
            evidence = row.get('evidence')
            if not isinstance(evidence,list) or not evidence or any(not isinstance(e,dict) for e in evidence):
                raise TaskError(prefix + '须提供可复核的原文、固定上下文或解析证据。')
            group=row.get('clause_group')
            if isinstance(group,str) and not group.strip(): group=None
            scoped_groups=(rule['rule_payload'].get('scope') or {}).get('clause_groups') or []
            if group is not None and (group not in CLAUSE_GROUP_IDS or (scoped_groups and group not in scoped_groups)
                    or (review_scope=='clause_groups' and group not in selected_groups)):
                raise TaskError(prefix+'条款组不属于受控类别、规则范围或本次定向审查范围。')
            if scoped_groups and status in {'applied','already_satisfied'} and not group:
                needs_confirmation(rule_id,'尚未明确该规则实际落实的条款组。')
            source_quotes = []; group_source_quotes = []; finding_evidence = {}; context_evidence = []; confirmations=[]
            group_finding_evidence=set()
            for item in evidence:
                source = item.get('source')
                if source == 'input':
                    part = item.get('part', 'word/document.xml'); index = item.get('paragraph_index'); quote = item.get('quote')
                    if (not isinstance(part,str) or type(index) is not int or index < 0 or index >= len(original.get(part, []))
                            or not isinstance(quote,str) or not quote.strip() or quote not in original[part][index]):
                        raise TaskError(prefix + '原文证据与指定正文位置不一致。')
                    source_quotes.append((part, quote))
                    if group and item.get('clause_group')==group: group_source_quotes.append((part,quote))
                elif source == 'context':
                    value = self._context_value(context, item.get('pointer'))
                    if 'value' not in item or json.dumps(value, sort_keys=True) != json.dumps(item['value'], sort_keys=True):
                        raise TaskError(prefix + '上下文证据与任务创建时的固定事实不同。')
                    context_evidence.append((item.get('pointer'),value))
                elif source == 'resolution':
                    if rule['included'] or not rule['exclusion_reason'] or item.get('reason') != rule['exclusion_reason']:
                        raise TaskError(prefix + '排除原因与固定解析结果不同。')
                elif source == 'finding':
                    fid = item.get('finding_id'); field = item.get('field'); quote = item.get('quote')
                    if (fid not in ids or not isinstance(field,str) or field not in {'target_text','replacement_text','insert_text','comment','risk'}
                            or not isinstance(quote,str) or not quote.strip() or quote not in str(findings[fid].get(field) or '')):
                        raise TaskError(prefix + '处理证据没有对应到真实审查事项的指定内容。')
                    finding_evidence.setdefault(fid, []).append((field,quote))
                    if group and item.get('clause_group')==group and findings[fid].get('clause_group')==group:
                        group_finding_evidence.add(fid)
                elif source == 'task_confirmation':
                    value=self._task_confirmation(task,item.get('evidence_id'))
                    latest=next((i for i in reversed(task.get('confirmations',[])) if i.get('rule_id')==value['rule_id']),None)
                    if latest and latest['evidence_id']!=value['evidence_id']:
                        raise TaskError(prefix+'人工确认已有更新决定，不能继续使用旧记录。')
                    confirmations.append(value)
                else:
                    raise TaskError(prefix + '未知证据来源；仅支持 input/context/resolution/finding/task_confirmation。')
            if not any(e.get('source') in {'input','context','resolution','task_confirmation'} for e in evidence):
                raise TaskError(prefix + '不能仅用计划中的建议文字证明原合同或事实。')
            if not rule['included'] and status not in {'not_applicable','commented','requires_confirmation'}:
                raise TaskError(prefix + '解析已排除该规则，应说明不适用原因或保留待确认。')
            purpose='not_applicable' if status=='not_applicable' else 'escalation'
            legacy=[value for pointer,value in context_evidence if pointer=='/client_rule_confirmations/'+rule_id]
            if legacy and any(i.get('rule_id')==rule_id for i in task.get('confirmations',[])):
                raise TaskError(prefix+'已有任务内更新决定，请引用最新人工确认记录。')
            confirmations.extend(legacy)
            matching_confirmation=any(self._confirmation_matches(task,rule,purpose,value) for value in confirmations)
            if confirmations and not matching_confirmation:
                raise TaskError(prefix+'人工确认未绑定本规则、内容、客户快照、合同原件或所需用途。')
            comment_needed = status in {'commented', 'requires_confirmation'} or bool(rule.get('application_review'))
            if status in {'commented', 'requires_confirmation'}:
                # Unknown business facts remain unknown. A real anchored Word
                # comment records them without inventing a human decision.
                pass
            elif status == 'not_applicable':
                if rule['included']:
                    comment_needed = True
                if not rule['included'] and not any(e.get('source')=='resolution' for e in evidence) and not matching_confirmation:
                    needs_confirmation(rule_id,'请引用固定解析的排除原因。')
            elif status == 'already_satisfied':
                if not source_quotes: raise TaskError(prefix + '已有满足须引用合同原文。')
                if scoped_groups and group and not group_source_quotes:
                    needs_confirmation(rule_id,'已有满足的合同原文尚未明确归属本条款组。')
                if accepted is not None and any(not any(q in p for p in accepted.get(part, [])) for part,q in source_quotes):
                    raise TaskError(prefix + '宣称已满足的原文未保留在最终合同中。')
            elif status == 'applied':
                if not ids: raise TaskError(prefix + '落实记录须关联具体审查事项。')
                escalate = (rule['rule_payload'].get('effect') or {}).get('action') == 'escalate'
                body_finding_seen = False
                for fid in ids:
                    finding = findings[fid]; action = finding.get('action'); links = finding_evidence.get(fid, [])
                    if scoped_groups and group:
                        if finding.get('clause_group')!=group or fid not in group_finding_evidence:
                            needs_confirmation(rule_id,'审查事项及处理证据尚未明确归属本条款组：'+fid)
                        if action!='insert' and not (escalate and action in {'comment','report-only'}) and not group_source_quotes:
                            needs_confirmation(rule_id,'既有条款处理尚未绑定本条款组的原文证据：'+fid)
                    if action in {'replace','insert','delete','delete_paragraph'}:
                        body_finding_seen = True
                        if finding.get('evidence_state') != 'confirmed': raise TaskError(prefix + '落实事项必须以已确认事实为依据。')
                        required = {'target_text'} if action in {'delete','delete_paragraph'} else {'replacement_text','insert_text'}
                        if not source_quotes or not any(field in required for field,quote in links):
                            raise TaskError(prefix + '正文落实须同时引用原件锚点和该修改事项的实际处理文字。')
                        if execution is not None:
                            if outcomes.get(fid,{}).get('status') != 'applied' or not edits.get(fid,{}).get('passed'):
                                raise TaskError(prefix + '关联的正文修改未通过实际交付核验：' + fid)
                            if action in {'replace','insert'} and any(not any(q in p for ps in accepted.values() for p in ps) for field,q in links if field in required):
                                raise TaskError(prefix + '关联的落实文字未出现在最终合同中：' + fid)
                    elif action == 'comment':
                        comment_needed = True
                    elif escalate and action == 'report-only' and matching_confirmation:
                        # Keep genuine historical human decisions readable;
                        # new AI-only treatment must be a Word comment.
                        if not any(field in {'comment','risk'} for field,quote in links):
                            raise TaskError(prefix + '历史人工处理须有真实报告留痕。')
                        if execution is not None and (outcomes.get(fid,{}).get('status') != 'report_only'
                                or not any(q in (report_text or '') for field,q in links if field in {'comment','risk'})):
                            raise TaskError(prefix + '历史人工处理留痕未出现在实际交付报告中。')
                    elif escalate and action == 'report-only':
                        comment_needed = True
                    else:
                        raise TaskError(prefix + '需要正文落实的规则不能用仅报告或批注替代；已有满足可据原文明确说明。')
                if not escalate and not body_finding_seen:
                    raise TaskError(prefix + '需要正文落实的规则不能用仅报告或批注替代；已有满足可据原文明确说明。')
            if comment_needed:
                from rule_studio.task_comments import verify_disposition_comments
                gaps = verify_disposition_comments(row, findings, source_quotes, finding_evidence,
                    reviewed=reviewed, outcomes=outcomes if execution is not None else None,
                    original_paragraphs=original, original_path=root/'input.docx')
                for message in gaps:
                    needs_confirmation(rule_id, message)
        return {'records':copy.deepcopy(rows), 'pending_rule_ids':sorted(pending), 'pending_reasons':pending,
                'commented_rule_ids':[row['rule_id'] for row in rows if row['status'] in {'commented','requires_confirmation'}]}

    def export_handoff(self, task_id):
        task = self._load(task_id)
        directory = self._directory(task_id)
        from one_contract_rules.clause_groups import vocabulary_options
        return {'task_id': task_id, 'state': task['state'], 'binding': self._binding(task),
                'input_path': str(directory/'input.docx'), 'input_name': task['input']['name'],
                'context': json.loads((directory/'context.json').read_text(encoding='utf-8')),
                'pin': task['pin'], 'client_rule_requirements': self._client_rules(task),
                'client_rule_disposition_contract': {'meta_field':'client_rule_dispositions',
                    'statuses':['applied','already_satisfied','commented','not_applicable','requires_confirmation'],
                    'required_fields':['rule_id','content_hash','status','explanation','finding_ids','evidence'],
                    'evidence_sources':['input','context','resolution','finding','task_confirmation'],
                    'clause_group_options':vocabulary_options(),
                    'confirmation_purposes':['not_applicable','escalation'],
                    'application_review':'由 AI 依据合同全文逐条判断；不适用、业务待判断事项及预筛分歧须以真实合同批注说明，不要求逐项人工确认。',
                    'comment_evidence':'引用真实 input 锚点及 finding.comment；交付时核对批注全文、对应锚点及实际执行，不接受仅报告。'},
                'rule_confirmations':[self._task_confirmation(task,item['evidence_id']) for item in task.get('confirmations',[])],
                'effective_rule_set': json.loads((directory/'effective-rules.json').read_text(encoding='utf-8')),
                'requirements': ['以固定规则和合同原件完成实质审查，提交真实 review plan。',
                                 '不得把规则预览或结构验证版宣称为已完成合同交付。',
                                 '需要真实正文修订、有效批注、报告以及逐页视觉验收。']}

    @_mutating
    def attach_plan(self, task_id, plan, *, expected_revision, actor):
        task = self._load(task_id); self._check_revision(task, expected_revision); self._check_pin(task)
        if not isinstance(plan, dict): raise TaskError('审查计划须为 JSON 对象。')
        plan = copy.deepcopy(plan)
        meta = plan.setdefault('meta', {})
        if not isinstance(meta, dict): raise TaskError('审查计划 meta 须为对象。')
        supplied = meta.get('resolution_provenance')
        if supplied is not None and (not isinstance(supplied,dict) or any(supplied.get(k) != v for k,v in task['pin'].items())):
            raise TaskError('审查计划的规则来源与本任务固定版本不同。')
        meta['resolution_provenance'] = copy.deepcopy(task['pin'])
        meta['client_selection'] = {key: task['pin'][key] for key in ('client_profile_id','client_policy_snapshot_id')}
        meta['client_selection']['mode'] = 'explicit' if task['pin']['client_profile_id'] else 'none'
        binding = self._binding(task); binding.pop('plan_sha256')
        if 'studio_task_binding' in meta and meta['studio_task_binding'] != binding:
            raise TaskError('计划绑定的合同原件或任务不同。')
        meta['studio_task_binding'] = binding
        from scripts.review.plan_loader import enrich_plan, validate_plan
        plan = enrich_plan(plan)
        validate_plan(plan, source_docx=self._directory(task_id)/'input.docx')
        dispositions = self._check_dispositions(task, plan)
        relative = 'plans/plan_' + uuid.uuid4().hex + '.json'
        path = self._directory(task_id)/relative
        private_fs.atomic_write_json(path, plan)
        task.update(plan={'path': relative, 'sha256': _hash_file(path), 'attached_by':str(actor)[:80]},
                    state='awaiting_confirmation' if dispositions['pending_rule_ids'] else 'plan_ready',
                    client_rule_dispositions=dispositions, delivery=None,
                    errors=[rule_id+'：'+'；'.join(reasons) for rule_id,reasons in dispositions['pending_reasons'].items()])
        return self._save(task)

    def _new_attempt(self, task, kind):
        attempt_id = 'attempt_' + uuid.uuid4().hex
        directory = self._directory(task['task_id']) / 'attempts' / attempt_id
        private_fs.secure_dir(directory)
        attempt = {'attempt_id': attempt_id, 'kind': kind, 'path': 'attempts/'+attempt_id,
                   'binding': self._binding(task), 'created_at': _now(), 'status': 'executing'}
        task['attempts'].append(attempt); task.update(state='executing', delivery=None, errors=[])
        self._save(task)
        return attempt, directory

    def _secure_tree(self, directory):
        for path in directory.rglob('*'):
            if path.is_symlink(): raise TaskError('执行产物包含符号链接，拒绝交付。')
            private_fs.secure_path(path)

    @_mutating
    def execute_plan(self, task_id, *, expected_revision, reviewer, timeout=300):
        task = self._load(task_id); self._check_revision(task, expected_revision); self._check_pin(task)
        if not task.get('plan'): raise TaskError('等待 Agent 提交审查计划；系统不会自行猜测修改。')
        self._require_dispositions_resolved(task)
        if not isinstance(reviewer, dict) or not str(reviewer.get('author') or '').strip():
            raise TaskError('执行前须明确审查人姓名。')
        if type(timeout) not in (int,float) or not 1 <= timeout <= 1800: raise TaskError('执行等待时间须为 1–1800 秒。')
        rendering = diagnose_rendering_tools()
        task['rendering'] = rendering
        if not rendering['ready']:
            task.update(state='blocked', delivery=None,
                        errors=['合同尚未执行：缺少渲染工具 '+', '.join(rendering['missing_tools'])+'。输入和审查计划已保留。']
                               + rendering['remediation'])
            return self._save(task)
        attempt, directory = self._new_attempt(task, 'local_execution')
        root = self._directory(task_id)
        context = json.loads((root/'context.json').read_text(encoding='utf-8'))
        env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1',
                   CONTRACT_COPILOT_CONFIG_DIR=str(directory/'runtime-config'),
                   CONTRACT_COPILOT_REVIEWER_PROFILE=str(directory/'runtime-config/reviewer.json'),
                   CONTRACT_COPILOT_REVIEW_MEMORY=str(directory/'runtime-config/memory.json'))
        command = [sys.executable, str(SKILL_ROOT/'scripts/review/apply_review_plan.py'),
                   '--input', str(root/'input.docx'), '--plan', str(root/task['plan']['path']),
                   '--output', str(directory/'reviewed.docx'), '--report', str(directory/'report.md'),
                   '--report-docx', str(directory/'report.docx'), '--log', str(directory/'execution.json'),
                   '--quality-dir', str(directory/'engine-quality'), '--no-archive', '--no-enrich-plan',
                   '--visual-policy', 'require', '--author', str(reviewer['author']),
                   '--organization', str(reviewer.get('organization') or ''),
                   '--party-role', str(reviewer.get('party_role') or context['our_role']),
                   '--review-intensity', str(reviewer.get('review_intensity') or '常规')]
        try:
            completed = subprocess.run(command, cwd=SKILL_ROOT, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
            _write_bytes(directory/'engine.log', completed.stdout)
            self._secure_tree(directory)
            if completed.returncode != 0:
                raise TaskError('合同执行或质量检查未通过；中间文件已保留，尚不可交付。')
            execution = json.loads((directory/'execution.json').read_text(encoding='utf-8'))
            return self._evaluate(task, attempt, directory, execution)
        except subprocess.TimeoutExpired:
            task.update(state='blocked', errors=['执行超时；中间文件已保留，请检查后重试。'])
        except (ValueError, OSError) as exc:
            task.update(state='blocked', errors=[str(exc)])
        except Exception as exc:
            task.update(state='blocked', errors=[f'执行检查未完成（{type(exc).__name__}），请检查中间记录后重试。'])
        attempt['status'] = 'blocked'
        self._secure_tree(directory)
        return self._save(task)

    @_mutating
    def import_delivery(self, task_id, *, reviewed_bytes, report_bytes, execution, binding, expected_revision, actor):
        task = self._load(task_id); self._check_revision(task, expected_revision); self._check_pin(task)
        if not task.get('plan') or binding != self._binding(task): raise TaskError('交付包与本任务原件、计划或固定规则不匹配。')
        self._require_dispositions_resolved(task)
        _docx_bytes(reviewed_bytes); _docx_bytes(report_bytes)
        if not isinstance(execution, dict): raise TaskError('请提供执行记录。')
        attempt, directory = self._new_attempt(task, 'imported_delivery')
        try:
            _write_bytes(directory/'reviewed.docx', reviewed_bytes); _write_bytes(directory/'report.docx', report_bytes)
            private_fs.atomic_write_json(directory/'execution.json', execution)
            return self._evaluate(task, attempt, directory, execution)
        except (ValueError, OSError) as exc:
            task.update(state='blocked', errors=[str(exc)]); attempt['status']='blocked'
            self._secure_tree(directory)
            return self._save(task)
        except Exception as exc:
            task.update(state='blocked', errors=[f'交付包检查未完成（{type(exc).__name__}），不能交付。']); attempt['status']='blocked'
            self._secure_tree(directory)
            return self._save(task)

    def _evaluate(self, task, attempt, directory, execution):
        root = self._directory(task['task_id'])
        plan = json.loads((root/task['plan']['path']).read_text(encoding='utf-8'))
        if execution.get('source_sha256') != task['input']['sha256']:
            raise TaskError('执行记录的输入文件校验值与任务原件不同。')
        if (execution.get('plan_meta') or {}).get('studio_task_binding') != plan['meta']['studio_task_binding']:
            raise TaskError('执行记录未绑定此任务的计划。')
        findings = plan.get('findings', [])
        outcomes = {r.get('id'):r.get('status') for r in execution.get('results',[]) if isinstance(r,dict)}
        if any(outcomes.get(f.get('id')) not in ('applied','report_only','directed_blocked') for f in findings):
            raise TaskError('存在未执行或失败的审查事项，不能交付。')
        from scripts.review.delivery_checks import check_delivery
        from scripts.docx_engine.quality_gate import run_quality_gate, render_docx_evidence, inspect_docx
        delivery = check_delivery(root/'input.docx',directory/'reviewed.docx',plan,report=directory/'report.docx')
        dispositions = self._check_dispositions(task, plan, execution=execution, delivery=delivery,
                                                reviewed=directory/'reviewed.docx', report=directory/'report.docx')
        if dispositions['pending_rule_ids']: raise TaskError('客户规则的批注或处理证据尚未补齐，不能交付。')
        structural = run_quality_gate(directory/'reviewed.docx',original_docx=root/'input.docx',output_dir=directory/'verified-quality',
                         require_revisions=any(f.get('action') in ('replace','delete','delete_paragraph','insert') for f in findings),
                         visual_policy='require')
        report_structure = inspect_docx(directory/'report.docx')
        report_visual = render_docx_evidence(directory/'report.docx',output_dir=directory/'verified-quality/render/report',visual_policy='require')
        errors = list(delivery.get('errors',[])) + list(structural.get('errors',[])) + list(report_structure.get('errors',[])) + list(report_visual.get('errors',[]))
        views = dict(structural.get('visual_validation') or {}); views['report'] = report_visual
        pages = []
        for view in ('reviewed','accepted','rejected','report'):
            evidence = views.get(view) or {}
            if evidence.get('status') != 'PASS' or not evidence.get('pages'):
                errors.append('缺少可逐页检查的渲染证据：'+view)
            for number,page in enumerate(evidence.get('pages') or [],1):
                path=Path(page['path'])
                relative=path.relative_to(root).as_posix()
                pages.append({'view':view,'page':number,'path':relative,'sha256':_hash_file(path)})
        artifacts = {path.relative_to(root).as_posix():_hash_file(path) for path in directory.rglob('*') if path.is_file()}
        manifest = {'binding':attempt['binding'],'artifacts':artifacts,'pages':pages}
        validation = {'client_rule_dispositions':dispositions,'delivery_checks':delivery,'quality_gate':structural,'report_structure':report_structure,'report_visual':report_visual,
                      'render_manifest':manifest,'render_manifest_sha256':models.content_hash(manifest),'errors':sorted(set(errors))}
        private_fs.atomic_write_json(directory/'delivery-validation.json',validation)
        self._secure_tree(directory)
        attempt.update(status='blocked' if errors else 'awaiting_visual_review',validation_path=attempt['path']+'/delivery-validation.json',
                       validation_sha256=_hash_file(directory/'delivery-validation.json'))
        task.update(state=attempt['status'],errors=validation['errors'],delivery={'attempt_id':attempt['attempt_id'],
                    'render_manifest_sha256':validation['render_manifest_sha256'],'pages':pages,'ready':False,
                    'artifacts':[{'path':attempt['path']+'/'+name,'label':label,'sha256':artifacts[attempt['path']+'/'+name]}
                                 for name,label in (('reviewed.docx','修订合同'),('report.docx','审查报告'))]})
        return self._save(task)

    def delivery(self, task_id):
        task = self._load(task_id)
        return {'task_id':task_id,'state':task['state'],'binding':self._binding(task),'delivery':task.get('delivery'),'errors':task['errors']}

    def get_artifact(self, task_id, relative_path):
        """Return a verified server-local path from an explicit task artifact allowlist."""
        task = self._load(task_id)
        allowed = {task['input']['path']:task['input']['sha256']}
        allowed.update({item['path']:item['sha256'] for item in task.get('confirmations',[])})
        if task.get('plan'): allowed[task['plan']['path']] = task['plan']['sha256']
        for attempt in task['attempts']:
            for name in ('engine.log','execution.json'):
                allowed[attempt['path']+'/'+name] = None
            if attempt.get('validation_path'):
                path = self._file(self._directory(task_id), attempt['validation_path'])
                if _hash_file(path) != attempt['validation_sha256']: raise TaskError('验收记录已被修改。')
                validation = json.loads(path.read_text(encoding='utf-8'))
                allowed.update(validation['render_manifest']['artifacts'])
        if relative_path not in allowed: raise TaskError('该文件不属于可读取的任务产物。')
        path = self._file(self._directory(task_id),relative_path)
        if not path.is_file(): raise TaskError('任务产物尚未生成。')
        digest = _hash_file(path)
        if allowed[relative_path] is not None and allowed[relative_path] != digest: raise TaskError('任务产物已改变。')
        content_type = {'.docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                        '.png':'image/png','.pdf':'application/pdf','.json':'application/json; charset=utf-8',
                        '.md':'text/markdown; charset=utf-8','.log':'text/plain; charset=utf-8'}.get(path.suffix,'application/octet-stream')
        return {'path':str(path),'filename':path.name,'media_type':content_type,'sha256':digest}

    def _require_dispositions_resolved(self, task):
        plan = json.loads((self._directory(task['task_id'])/task['plan']['path']).read_text(encoding='utf-8'))
        result = self._check_dispositions(task, plan)
        if result['pending_rule_ids']: raise TaskError('客户规则的批注或处理证据尚未补齐，请更新审查计划。')
        return result

    def _verify_delivery_files(self, task):
        self._require_dispositions_resolved(task)
        attempt = next(a for a in task['attempts'] if a['attempt_id']==task['delivery']['attempt_id'])
        root = self._directory(task['task_id'])
        path = self._file(root, attempt['validation_path'])
        if _hash_file(path) != attempt['validation_sha256']:
            raise TaskError('验收证据已被修改。')
        validation = json.loads(path.read_text(encoding='utf-8'))
        for relative,digest in validation['render_manifest']['artifacts'].items():
            if _hash_file(self._file(root,relative)) != digest:
                raise TaskError('最终文件或渲染页面已改变，需要重新验收。')
        if task['delivery'].get('acceptance_path') and _hash_file(self._file(root,task['delivery']['acceptance_path'])) != task['delivery']['acceptance_sha256']:
            raise TaskError('逐页验收记录已被修改。')
        plan = json.loads((root/task['plan']['path']).read_text(encoding='utf-8'))
        execution = json.loads((root/attempt['path']/'execution.json').read_text(encoding='utf-8'))
        dispositions = self._check_dispositions(task, plan, execution=execution, delivery=validation['delivery_checks'],
                                                reviewed=root/attempt['path']/'reviewed.docx', report=root/attempt['path']/'report.docx')
        if dispositions['pending_rule_ids']:
            raise TaskError('最终合同的客户规则批注或处理证据未通过复核。')
        return validation

    @_mutating
    def accept_delivery(self, task_id, *, visual_acceptance, expected_revision, actor):
        task = self._load(task_id); self._check_revision(task, expected_revision); self._check_pin(task)
        if task['state'] != 'awaiting_visual_review' or not task.get('delivery'):
            raise TaskError('结构、语义和渲染检查尚未通过，不能确认交付。')
        attempt = next(a for a in task['attempts'] if a['attempt_id']==task['delivery']['attempt_id'])
        root=self._directory(task_id)
        validation=self._verify_delivery_files(task)
        manifest=validation['render_manifest']
        for relative,digest in manifest['artifacts'].items():
            if _hash_file(self._file(root,relative))!=digest: raise TaskError('最终文件或渲染页面已改变，需要重新验收。')
        approval=visual_acceptance
        if (not isinstance(approval,dict) or approval.get('status')!='PASS'
                or approval.get('render_manifest_sha256')!=validation['render_manifest_sha256']
                or not str(approval.get('inspected_by') or '').strip()
                or any((approval.get('checklist') or {}).get(k) is not True for k in VISUAL_CHECKS)):
            raise TaskError('请对本版本完成全部视觉检查并填写检查人。')
        checked=approval.get('pages')
        expected={(p['path'],p['sha256']) for p in manifest['pages']}
        if (not isinstance(checked,list) or len(checked)!=len(expected)
                or any(not isinstance(p,dict) or p.get('status')!='PASS' for p in checked)
                or {(p.get('path'),p.get('sha256')) for p in checked}!=expected):
            raise TaskError('须逐页确认审阅件、接受版、拒绝版和报告的全部页面，且校验值一致。')
        acceptance_path = attempt['path']+'/visual-acceptance.json'
        private_fs.atomic_write_json(root/acceptance_path,
                                     {**approval,'recorded_at':_now(),'recorded_by':str(actor)[:80]})
        task['delivery'].update(ready=True,accepted_by=approval['inspected_by'],accepted_at=_now(),
                                acceptance_path=acceptance_path,acceptance_sha256=_hash_file(root/acceptance_path))
        task.update(state='ready',errors=[]); attempt['status']='ready'
        return self._save(task)
