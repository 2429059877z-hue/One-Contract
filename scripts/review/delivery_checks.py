"""Independent artifact checks: exact text, structure, edits and public output.

Reads DOCX XML itself; never trusts a producer's PASS. No files are modified.
"""
from scripts.review.plan_contract import get_findings
from collections import Counter
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
from xml.etree import ElementTree as ET
from zipfile import ZipFile

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
NS = {'w': W}
STORY = re.compile(r'^word/(document|header\d+|footer\d+|footnotes|endnotes)\.xml$')
INTERNAL = re.compile(r'\b(?:type-|mod-|prd-|trg-)[A-Za-z0-9_-]+|\bEC-\d{2}\b')
REFERENCE = re.compile(r'第\s*([0-9]+(?:[.．][0-9]+)*|[一二三四五六七八九十百千〇零]+)\s*条')
NUMERIC_FILL = re.compile(r'【待填\s*[:：]\s*\d+\s*】')


def _local(node):
    return node.tag.rsplit('}', 1)[-1]


def _surviving_payload(node):
    """Comments and formatting alone do not keep a removed paragraph alive."""
    name = _local(node)
    if name in {'pPr', 'rPr', 'commentRangeStart', 'commentRangeEnd', 'commentReference',
                'bookmarkStart', 'bookmarkEnd', 'proofErr'}:
        return False
    if name in {'p', 'r'}:
        return any(_surviving_payload(child) for child in node)
    if name in {'t', 'delText', 'instrText', 'delInstrText'}:
        return bool(node.text)
    return True  # Drawings, fields and unknown structures must be preserved.


def _view(node, mode):
    """Project revisions with an independent ElementTree traversal."""
    for child in list(node):
        name = _local(child)
        if name == 'p':
            marker = child.find('w:pPr/w:rPr/w:ins' if mode == 'reject' else 'w:pPr/w:rPr/w:del', NS)
            if marker is not None:
                # A revised paragraph mark joins surviving text to the next
                # paragraph; its formatting belongs to the surviving mark.
                _view(child, mode)
                siblings = list(node)
                index = siblings.index(child)
                cursor = index + 1
                boundary_markup = []
                while cursor < len(siblings) and _local(siblings[cursor]) in {
                    'commentRangeStart', 'commentRangeEnd', 'bookmarkStart', 'bookmarkEnd', 'proofErr'
                }:
                    boundary_markup.append(siblings[cursor])
                    cursor += 1
                following = siblings[cursor] if cursor < len(siblings) else None
                if following is not None and _local(following) == 'p':
                    offset = 1 if len(following) and _local(following[0]) == 'pPr' else 0
                    for content in list(child):
                        if _local(content) != 'pPr':
                            child.remove(content)
                            following.insert(offset, content)
                            offset += 1
                    for boundary in boundary_markup:
                        node.remove(boundary)
                        following.insert(offset, boundary)
                        offset += 1
                    node.remove(child)
                elif (not _surviving_payload(child)
                      and any(sibling is not child and _local(sibling) == 'p' for sibling in siblings)):
                    node.remove(child)
                continue
        if name in ({'ins','moveTo'} if mode == 'reject' else {'del','moveFrom'}):
            node.remove(child); continue
        if name in {'pPrChange','rPrChange','sectPrChange','tblPrChange','trPrChange','tcPrChange'}:
            if mode == 'reject' and len(child):
                prior = child[0]
                node.attrib.clear(); node.attrib.update(prior.attrib)
                for sibling in list(node): node.remove(sibling)
                node.extend(list(prior))
                break
            node.remove(child); continue
        _view(child, mode)


def _text(node):
    name = _local(node)
    if name in {'pPr','rPr','sectPr','tblPr','trPr','tcPr'}:
        return ''
    if name in {'t','delText','instrText','delInstrText'}:
        return node.text or ''
    symbols = {'tab':'\t','cr':'\n','br':'\n','noBreakHyphen':'\u2011','softHyphen':'\u00ad'}
    if name in symbols: return symbols[name]
    return ''.join(_text(child) for child in node)


def inspect_view(path, mode):
    paragraphs = {}; structures = {}; public = []
    with ZipFile(path) as z:
        names = sorted(z.namelist())
        for name in names:
            if not STORY.match(name): continue
            root = ET.fromstring(z.read(name)); _view(root, mode)
            paragraphs[name] = [_text(p) for p in root.iter(f'{{{W}}}p')]
            counts = Counter(_local(n) for n in root.iter())
            structures[name] = {k:counts[k] for k in ('p','cr','br','numPr','sectPr','commentRangeStart','commentRangeEnd','commentReference')}
            public.extend(paragraphs[name])
        if 'word/comments.xml' in names:
            root = ET.fromstring(z.read('word/comments.xml'))
            public.extend(_text(p) for p in root.iter(f'{{{W}}}p'))
    return {'paragraphs':paragraphs,'structures':structures,'parts':names,'public_text':'\n'.join(public), 'public_blocks':public}


def scan_external_text(text, finding_ids=(), *, source_text=''):
    """Only new occurrences are findings; unchanged source text is not generated metadata."""
    errors = []
    patterns = [('EXTERNAL_INTERNAL_CODE', INTERNAL),
                ('UNNAMED_FILL_PLACEHOLDER', NUMERIC_FILL),
                ('RAW_MARKDOWN', re.compile(r'\*\*[^\n]+\*\*'))]
    for fid in finding_ids:
        if fid:
            patterns.append(('EXTERNAL_FINDING_ID', re.compile(r'(?<![\w-])'+re.escape(fid)+r'(?![\w-])')))
    for code, pattern in patterns:
        if Counter(m.group() for m in pattern.finditer(text)) - Counter(m.group() for m in pattern.finditer(source_text)):
            errors.append(code)
    return sorted(set(errors))


def changed_blocks(before, after):
    for tag, a, b, c, d in SequenceMatcher(a=before, b=after, autojunk=False).get_opcodes():
        if tag in {'replace', 'insert'}:
            yield '\n'.join(before[a:b]), '\n'.join(after[c:d])


def _normalize_number(value):
    value = value.replace('．','.')
    if re.fullmatch(r'[0-9.]+',value): return value
    digits = dict(zip('零〇一二三四五六七八九', [0,0,1,2,3,4,5,6,7,8,9]))
    units = {'十':10,'百':100,'千':1000}
    total = digit = 0
    for char in value:
        if char in digits: digit = digits[char]
        elif char in units: total += (digit or 1)*units[char]; digit = 0
        else: return value
    return str(total + digit)


def contract_references(text):
    # Laws and contracts both use “第X条”. A named legal instrument scopes
    # following article references until a sentence/semicolon or explicit
    # contract reference. Do not claim to validate law numbers here.
    for clause in re.split(r'[。；;\n]',text):
        for match in REFERENCE.finditer(clause):
            prefix = clause[:match.start()]
            laws = list(re.finditer(r'《[^》]+》|(?:民法典|公司法|劳动法|司法解释)',prefix))
            if laws:
                instrument = laws[-1].group()
                tail = prefix[laws[-1].end():]
                is_contract = any(k in instrument for k in ('合同','协议','执行单','订单')) and not any(k in instrument for k in ('合同法','合同编','司法解释'))
                if not is_contract and not re.search(r'本合同|本协议|合同第|协议第',tail):
                    continue
            yield match.group(1)


def source_clause_references(source_view):
    """Return valid normalized numbers and human-readable original locators."""
    refs = set()
    labels = []
    for paragraphs in source_view['paragraphs'].values():
        for paragraph in paragraphs:
            for match in REFERENCE.finditer(paragraph):
                refs.add(_normalize_number(match.group(1)))
                if match.group() not in labels:
                    labels.append(match.group())
            match = re.match(r'^\s*(\d+(?:\.\d+)*)(?![\d.])(?:[、．.\[【（(]|\s|(?=[\u4e00-\u9fff]))', paragraph)
            if match:
                refs.add(_normalize_number(match[1]))
                label = match[1]
                if label not in labels:
                    labels.append(label)
            # Chinese enumeration is an available *literal* locator, not an
            # authorization to silently rewrite it to a fabricated 第X条.
            match = re.match(r'^\s*([一二三四五六七八九十百千〇零]+、)', paragraph)
            if match and match[1] not in labels:
                labels.append(match[1])
    return refs, labels


def _text_leaves(value, path):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _text_leaves(item, f'{path}.{key}')
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _text_leaves(item, f'{path}[{index}]')


def report_reference_diagnostics(plan, source_view, *, report_text=None):
    """Scan every summary leaf; optionally check the actual rendered report.

    “新增第X条” is still a proposal, not proof that a source clause exists.
    Keep the guard conservative and offer a concrete unnumbered rewrite.
    """
    refs, labels = source_clause_references(source_view)
    diagnostics = []
    values = list(_text_leaves(plan.get('summary', {}), 'summary'))
    if report_text is not None:
        values.append(('report', report_text))
    for path, text in values:
        for ref in dict.fromkeys(contract_references(text)):
            if _normalize_number(ref) not in refs:
                diagnostics.append({
                    'code': 'UNKNOWN_REPORT_CLAUSE', 'reference': ref,
                    'finding_id': None, 'finding_index': None, 'path': path,
                    'cause': f'UNKNOWN_REPORT_CLAUSE:{ref}；原文未定义“第{ref}条”',
                    'available_clause_references': labels,
                    'suggestion': ('使用原文的条款标题或编号（可用引用：' + ('、'.join(labels) or '无显式条款编号')
                                   + '）；新增条款改写为“拟新增的‘条款名称’条款”。'
                                   '检查 summary 全部字段；法律引文须在同一分句内先写明《法律名称》。'),
                })
    return diagnostics


def check_delivery(source, reviewed, plan, *, execution=None, report=None, baseline_view='reject'):
    before = inspect_view(source, baseline_view)
    original_accepted = inspect_view(source, 'accept')
    accepted = inspect_view(reviewed, 'accept')
    rejected = inspect_view(reviewed, 'reject')
    errors = []; findings = get_findings(plan)
    if before['paragraphs'] != rejected['paragraphs']:
        errors.append('REJECT_TEXT_MISMATCH')
    for name, counts in before['structures'].items():
        after = rejected['structures'].get(name, {})
        for key in ('p','cr','br','numPr','sectPr'):
            if counts[key] != after.get(key): errors.append(f'STRUCTURE_LOSS:{name}:{key}')
    for name in before['parts']:
        if name in {'word/styles.xml','word/numbering.xml'} and name not in rejected['parts']:
            errors.append(f'MISSING_PART:{name}')
    statuses = {x.get('id'):x for x in (execution or {}).get('results',[])} if execution is not None else None
    before_text = '\n'.join(p for ps in before['paragraphs'].values() for p in ps)
    from scripts.review.expected_edits import expected_paragraphs
    # The editor locates against accepted text even when historical revisions
    # remain in the source. Reject restoration still uses baseline_view above.
    expected, edit_checks = expected_paragraphs(source, findings, statuses, 'accept')
    expected_stories = dict(original_accepted['paragraphs'])
    expected_stories['word/document.xml'] = expected
    matches = expected_stories == accepted['paragraphs']
    for check in edit_checks:
        check['passed'] = check['passed'] and matches
        if not check['passed']:
            errors.append('EDIT_NOT_CONFIRMED:'+str(check['id']))
    if not matches:
        errors.append('ACCEPTED_TEXT_MISMATCH')
    source_refs, available_refs = source_clause_references(before)
    ids=[f.get('id','') for f in findings]
    # Validate emitted text, including actual comments/replies. Blocked proposals
    # and unused replacement fields are not part of the delivery artifact.
    output_changes = list(changed_blocks(original_accepted['public_blocks'], accepted['public_blocks']))
    for previous, current in output_changes:
        previous_refs = Counter(contract_references(previous))
        for ref, count in (Counter(contract_references(current)) - previous_refs).items():
            if count and _normalize_number(ref) not in source_refs:
                errors.append('UNKNOWN_SOURCE_CLAUSE:'+ref)
        errors.extend(scan_external_text(current, ids, source_text=previous))
    report_text = None
    if report:
        path=Path(report)
        report_text = inspect_view(path,'accept')['public_text'] if path.suffix.lower()=='.docx' else path.read_text(encoding='utf-8')
        errors.extend(scan_external_text(report_text,ids))
    reference_diagnostics = report_reference_diagnostics(plan, before, report_text=report_text)
    errors.extend('UNKNOWN_REPORT_CLAUSE:'+item['reference'] for item in reference_diagnostics)
    return {'status':'FAIL' if errors else 'PASS','errors':sorted(set(errors)),
            'available_clause_references':available_refs, 'clause_reference_diagnostics':reference_diagnostics,
            'source_sha256':hashlib.sha256(Path(source).read_bytes()).hexdigest(),
            'reviewed_sha256':hashlib.sha256(Path(reviewed).read_bytes()).hexdigest(),
            'report_sha256':hashlib.sha256(Path(report).read_bytes()).hexdigest() if report else None,
            'baseline_view':baseline_view, 'baseline':before['structures'],
            'rejected':rejected['structures'],'accepted':accepted['structures'],
            'parts_before':before['parts'],'parts_after':accepted['parts'], 'edit_checks':edit_checks}


def main():
    import argparse
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',required=True);p.add_argument('--reviewed',required=True)
    p.add_argument('--plan',required=True);p.add_argument('--report');p.add_argument('--execution')
    p.add_argument('--baseline-view',choices=['accept','reject'],default='reject')
    a=p.parse_args()
    result=check_delivery(a.source,a.reviewed,json.loads(Path(a.plan).read_text()),
        report=a.report, execution=json.loads(Path(a.execution).read_text()) if a.execution else None,baseline_view=a.baseline_view)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['status']=='PASS' else 1

if __name__=='__main__': raise SystemExit(main())
