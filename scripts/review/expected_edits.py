"""Independent plain-text projection of ordered edits; no editor imports.

Compare the complete expected paragraph sequence, not global string counts.
Selectors this checker cannot resolve are explicitly unverified, never PASS.
"""
import re
import unicodedata
from xml.etree import ElementTree as ET
from zipfile import ZipFile


def _normalized(text):
    chars, offsets = [], []
    for i, char in enumerate(text):
        if char in {'\u200b','\u200c','\u200d','\u2060','\ufeff'}:
            continue
        for normalized in unicodedata.normalize('NFKC', char):
            normalized = ' ' if normalized.isspace() else normalized
            if normalized == ' ' and chars and chars[-1] == ' ':
                continue
            chars.append(normalized); offsets.append(i)
    return ''.join(chars), offsets


def _spans(text, needle):
    text, offsets = _normalized(text)
    needle, _ = _normalized(needle)
    if not needle:
        return []
    return [(offsets[m.start()], offsets[m.end()-1]+1)
            for m in re.finditer(re.escape(needle), text)]


def expected_paragraphs(source, findings, statuses, baseline_view):
    # Shared XML projection only; locating and editing are independent of the producer.
    from scripts.review.delivery_checks import W, _view, _text
    with ZipFile(source) as z:
        root = ET.fromstring(z.read('word/document.xml'))
    _view(root, baseline_view)
    records = []
    for p in root.iter(f'{{{W}}}p'):
        runs, offset = [], 0
        for run in p.iter(f'{{{W}}}r'):
            text = _text(run)
            runs.append({'start':offset, 'end':offset+len(text), 'attrs':run.attrib})
            offset += len(text)
        records.append({'text':_text(p), 'attrs':p.attrib, 'runs':runs})
    checks = []
    for finding in findings:
        result = statuses.get(finding.get('id'), {}) if statuses is not None else finding
        if statuses is not None and result.get('status') != 'applied':
            continue
        action = result.get('action', finding.get('action'))
        if action not in {'replace', 'insert', 'delete', 'delete_paragraph'}:
            continue
        check = {'id':finding.get('id'), 'passed':False}
        checks.append(check)
        try:
            selector = finding.get('selector') or {}
            tag = selector.get('tag') or finding.get('tag') or ('w:p' if action in {'insert', 'delete_paragraph'} else 'w:r')
            old = finding.get('target_text') or finding.get('search')
            occurrence = selector.get('occurrence', finding.get('occurrence'))
            node_mode = action in {'insert', 'delete_paragraph'} or not old
            if tag not in {'w:p', 'w:r'} or (node_mode and selector.get('line_number') is not None):
                raise ValueError('该 selector 无法独立验证，请改用 target_text 或 contains/attrs 与 occurrence')
            matches = []
            for pi, record in enumerate(records):
                text = record['text']
                if not node_mode:
                    matches.extend((pi, a, b) for a,b in _spans(text, str(old)))
                    continue
                needle = selector.get('contains') if selector else old
                attrs = selector.get('attrs') or {}
                attrs = {('{'+W+'}'+k[2:] if k.startswith('w:') else k):v for k,v in attrs.items()}
                nodes = [dict(start=0,end=len(text),attrs=record['attrs'])] if tag == 'w:p' else record['runs']
                for node in nodes:
                    if any(node['attrs'].get(k) != v for k,v in attrs.items()):
                        continue
                    if needle is not None and not _spans(text[node['start']:node['end']],str(needle)):
                        continue
                    matches.append((pi,node['start'],node['end']))
            if not matches or (occurrence is None and len(matches) != 1):
                raise ValueError('目标位置不存在或不唯一')
            index = 1 if occurrence is None else int(occurrence)
            if index < 1 or index > len(matches):
                raise ValueError('occurrence 超出目标范围')
            pi, a, b = matches[index-1]
            text = records[pi]['text']
            if action in {'delete', 'delete_paragraph'}:
                new = ''
            elif action == 'insert':
                new = finding.get('replacement_text') or finding.get('insert_text')
            else:
                new = finding.get('replacement_text',finding.get('recommended_text'))
            if new is None:
                raise ValueError('缺少替代文字')
            new = str(new)
            def record(value):
                return {'text':value,'attrs':{},'runs':[{'start':0,'end':len(value),'attrs':{}}]}
            if action == 'delete_paragraph':
                if tag != 'w:p' or (old and _normalized(str(old))[0] != _normalized(text)[0]):
                    raise ValueError('delete_paragraph 必须定位完整段落')
                records.pop(pi)
            elif action == 'insert' and tag == 'w:p':
                values = new.replace('\r\n', '\n').replace('\r', '\n').split('\n')
                records[pi+1:pi+1] = [record(value) for value in values]
            elif node_mode and tag == 'w:p':
                # Node-level deletion keeps the original paragraph container.
                records[pi] = record('')
                if action == 'replace': records.insert(pi+1, record(new))
            else:
                if action == 'insert': a = b
                records[pi] = record(text[:a]+new+text[b:])
            check['passed'] = True
        except (ValueError, TypeError, IndexError) as exc:
            check['reason'] = str(exc)
    return [r['text'] for r in records], checks
