"""Verify AI applicability explanations against real, anchored Word comments."""
from copy import deepcopy
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from scripts.review.delivery_checks import W, STORY, _text, _view


def _normalized(value):
    return ''.join(str(value or '').split())


def _paragraph_revision_identity(paragraph, kind):
    marker = paragraph.find(f'{{{W}}}pPr/{{{W}}}rPr/{{{W}}}{kind}')
    return tuple(sorted(marker.attrib.items())) if marker is not None else None


def anchored_comments(path, *, original_path=None):
    """Read comment text and closed ranges independently of execution claims."""
    original_insertions = {}; original_deletions = {}
    if original_path is not None:
        with ZipFile(original_path) as package:
            for name in package.namelist():
                if STORY.match(name):
                    paragraphs = list(ET.fromstring(package.read(name)).iter(f'{{{W}}}p'))
                    original_insertions[name] = {identity for paragraph in paragraphs
                        if (identity := _paragraph_revision_identity(paragraph, 'ins')) is not None}
                    original_deletions[name] = {identity for paragraph in paragraphs
                        if (identity := _paragraph_revision_identity(paragraph, 'del')) is not None}
    with ZipFile(path) as package:
        if 'word/comments.xml' not in package.namelist():
            return []
        comments = {}
        for node in ET.fromstring(package.read('word/comments.xml')).findall(f'{{{W}}}comment'):
            identity = node.get(f'{{{W}}}id')
            if identity in comments:
                return []  # Duplicate identities cannot prove a specific comment.
            comments[identity] = _text(node)
        ranges = []
        for name in package.namelist():
            if not STORY.match(name):
                continue
            original = ET.fromstring(package.read(name))
            for mode in (None, 'accept', 'reject'):
                root = deepcopy(original)
                if mode:
                    _view(root, mode)
                paragraphs = list(root.iter(f'{{{W}}}p'))
                paragraph_texts = [_text(p) for p in paragraphs]
                # New tracked paragraphs cannot be mistaken for input evidence
                # merely because they repeat its wording. Retain historical
                # insertion markers that were already present in the input.
                original_candidates = []
                for index, paragraph in enumerate(paragraphs):
                    inserted = _paragraph_revision_identity(paragraph, 'ins')
                    deleted = _paragraph_revision_identity(paragraph, 'del')
                    if original_path is not None:
                        if inserted is not None and inserted not in original_insertions.get(name, set()): continue
                        if deleted is not None and deleted in original_deletions.get(name, set()):
                            accepted = deepcopy(paragraph); _view(accepted, 'accept')
                            if not _normalized(_text(accepted)): continue
                    original_candidates.append(index)
                paragraph_ids = {node: index for index, node in enumerate(paragraphs)}
                parents = {child: parent for parent in root.iter() for child in parent}
                def paragraph_index(node):
                    while node is not None:
                        if node in paragraph_ids: return paragraph_ids[node]
                        node = parents.get(node)
                    return None
                active = {}; closed = {}; starts = {}; ends = {}; references = {}; offsets = {}
                for node in root.iter():
                    kind = node.tag.rsplit('}', 1)[-1]
                    identity = node.get(f'{{{W}}}id')
                    if kind == 'commentRangeStart':
                        starts[identity] = starts.get(identity, 0) + 1
                        active[identity] = {'chunks': [], 'paragraphs': set(), 'spans': []}
                    elif kind == 'commentRangeEnd':
                        ends[identity] = ends.get(identity, 0) + 1
                        if identity in active:
                            closed[identity] = active.pop(identity)
                    elif kind == 'commentReference':
                        references[identity] = references.get(identity, 0) + 1
                    elif kind in {'t', 'delText', 'instrText', 'delInstrText'}:
                        index = paragraph_index(node)
                        start = offsets.get(index, 0); end = start + len(_normalized(node.text))
                        offsets[index] = end
                        for record in active.values():
                            record['chunks'].append(node.text or '')
                            if index is not None:
                                record['paragraphs'].add(index)
                                if end > start: record['spans'].append((index, start, end))
                for identity, anchor in closed.items():
                    if identity in comments and starts.get(identity) == ends.get(identity) == references.get(identity) == 1:
                        ranges.append({'part': name, 'text': comments[identity],
                                       'anchor': ''.join(anchor['chunks']), 'mode': mode,
                                       'paragraph_indexes': sorted(anchor['paragraphs']),
                                       'spans': anchor['spans'],
                                       'paragraphs': paragraph_texts,
                                       'original_candidates': original_candidates})
        return ranges


def _source_locations(row, finding, original_paragraphs, original_path=None):
    """Bind the textual selector/occurrence to the cited original paragraphs."""
    selector = finding.get('selector') or {}
    target = selector.get('contains') if selector else finding.get('target_text') or finding.get('search')
    target = _normalized(target)
    citations = [e for e in row.get('evidence', []) if e.get('source') == 'input']
    raw_occurrence = selector.get('occurrence', finding.get('occurrence'))
    occurrence = int(raw_occurrence) if raw_occurrence is not None else None
    paragraph_selector = (finding.get('action') in {'comment', 'insert', 'delete_paragraph'}
        and selector.get('tag', finding.get('tag', 'w:p')) == 'w:p')
    locations = []
    for part in {e.get('part', 'word/document.xml') for e in citations}:
        texts = [_normalized(p) for p in original_paragraphs.get(part, [])]
        selected_input_indexes = None
        if original_path is not None and paragraph_selector and occurrence is not None and target:
            # Engine paragraph selectors count the preserved raw paragraphs,
            # including historical deleted ones; input citations use accepted
            # paragraphs. Convert between the two without calling a deleted
            # paragraph evidence for the identical surviving paragraph.
            with ZipFile(original_path) as package:
                raw_paragraphs = list(ET.fromstring(package.read(part)).iter(f'{{{W}}}p'))
            raw_matches = [index for index,p in enumerate(raw_paragraphs) if target in _normalized(_text(p))]
            selected_raw = raw_matches[occurrence-1:occurrence] if occurrence > 0 else []
            candidates = []
            for index,p in enumerate(raw_paragraphs):
                accepted = deepcopy(p); _view(accepted, 'accept')
                if _paragraph_revision_identity(p, 'del') is not None and not _normalized(_text(accepted)): continue
                candidates.append(index)
            mapping = _paragraph_bounds(texts, [_normalized(_text(raw_paragraphs[i])) for i in candidates])
            selected_input_indexes = {index for index in range(len(texts))
                if mapping is not None and candidates[mapping[0][index]] == candidates[mapping[1][index]]
                and candidates[mapping[0][index]] in selected_raw}
        offsets = []; cursor = 0
        for text in texts:
            offsets.append(cursor); cursor += len(text)
        matches = []
        for index, text in enumerate(texts):
            start = 0
            while target:
                start = text.find(target, start)
                if start < 0: break
                matches.append((index, start, start + len(target)))
                start += len(target)
                if paragraph_selector:
                    break  # Paragraph selectors count matching paragraphs.
        if selected_input_indexes is not None:
            matches = [match for match in matches if match[0] in selected_input_indexes]
        elif occurrence is not None:
            matches = matches[occurrence-1:occurrence] if occurrence > 0 else []
        for citation in citations:
            if citation.get('part', 'word/document.xml') != part: continue
            index = citation.get('paragraph_index'); quote = _normalized(citation.get('quote'))
            if type(index) is not int or not 0 <= index < len(texts) or not quote or quote not in texts[index]: continue
            if target:
                # A real quote from an unrelated sentence/paragraph is not
                # evidence for this finding's different selector.
                if target not in quote and quote not in target: continue
                selected = [m for m in matches if m[0] == index]
            else:
                selected = [(index, texts[index].index(quote), texts[index].index(quote)+len(quote))]
            for _, start, end in selected:
                locations.append((part, index, offsets[index]+start, offsets[index]+end))
    return locations


def _subsequence_bounds(original, reviewed):
    """Locate preserved original characters in the redline's raw text union.

    Tracked insertions/deletions retain the original characters. Earliest and
    latest possible alignments keep repeated wording conservative, and avoid
    comparing output paragraph numbers after paragraph insertion/deletion.
    """
    earliest = []; cursor = 0
    for character in original:
        cursor = reviewed.find(character, cursor)
        if cursor < 0: return None
        earliest.append(cursor); cursor += 1
    latest = [0] * len(original); cursor = len(reviewed)
    for index in range(len(original)-1, -1, -1):
        cursor = reviewed.rfind(original[index], 0, cursor)
        if cursor < 0: return None
        latest[index] = cursor
    return earliest, latest


def _paragraph_bounds(original, reviewed):
    """Use whole-paragraph context before character alignment across paragraphs."""
    def contains(source, target):
        cursor = 0
        for character in source:
            cursor = target.find(character, cursor)
            if cursor < 0: return False
            cursor += 1
        return True
    earliest = []; cursor = 0
    for text in original:
        while cursor < len(reviewed) and not contains(text, reviewed[cursor]): cursor += 1
        if cursor == len(reviewed): return None
        earliest.append(cursor); cursor += 1
    latest = [0] * len(original); cursor = len(reviewed)-1
    for index in range(len(original)-1, -1, -1):
        while cursor >= 0 and not contains(original[index], reviewed[cursor]): cursor -= 1
        if cursor < 0: return None
        latest[index] = cursor; cursor -= 1
    return earliest, latest


def _location_matches(item, finding, locations, original_paragraphs, cache):
    if item['mode'] is not None: return False
    part = item['part']; candidate_indexes = item['original_candidates']
    paragraphs = [_normalized(item['paragraphs'][index]) for index in candidate_indexes]
    if part not in cache:
        original = [_normalized(p) for p in original_paragraphs.get(part, [])]
        cache[part] = (_paragraph_bounds(original, paragraphs), _subsequence_bounds(''.join(original), ''.join(paragraphs)))
    paragraph_mapping, mapping = cache[part]
    if paragraph_mapping is None and mapping is None: return False
    bounds = []; cursor = 0
    for paragraph in paragraphs:
        bounds.append((cursor, cursor+len(paragraph))); cursor += len(paragraph)
    actual_indexes = set(item['paragraph_indexes'])
    for source_part, source_index, start, end in locations:
        if source_part != part or end <= start: continue
        if paragraph_mapping is not None:
            source_indexes = {candidate_indexes[paragraph_mapping[0][source_index]],
                              candidate_indexes[paragraph_mapping[1][source_index]]}
        else:
            left, right = mapping[0][start], mapping[1][end-1]+1
            source_indexes = {candidate_indexes[index] for index,(a,b) in enumerate(bounds) if a < right and b > left}
        if not source_indexes: continue
        if finding.get('action') == 'insert':
            # Paragraph insertion comments attach to the first newly inserted
            # paragraph immediately after the original anchor; inline insertion
            # stays in the original paragraph. No arbitrary later paragraph.
            paragraph_insert = (finding.get('selector') or {}).get('tag', finding.get('tag', 'w:p')) == 'w:p'
            if paragraph_insert and len(source_indexes) != 1: continue
            expected = {max(source_indexes)+1} if paragraph_insert else source_indexes
            if actual_indexes and actual_indexes.issubset(expected): return True
        elif source_indexes.issubset(actual_indexes):
            if finding.get('action') in {'replace', 'delete'} and len(source_indexes) == 1:
                output_index = next(iter(source_indexes))
                original = [_normalized(p) for p in original_paragraphs[part]]
                source_offset = sum(map(len, original[:source_index]))
                local_start, local_end = start-source_offset, end-source_offset
                raw = _normalized(item['paragraphs'][output_index])
                alignment = _subsequence_bounds(original[source_index], raw)
                if alignment is None: continue
                # Include inserted characters immediately adjacent to the old
                # target (a minimal edit can anchor only the new character),
                # but never accept another occurrence in the same paragraph.
                left = alignment[0][local_start-1]+1 if local_start else 0
                right = alignment[1][local_end] if local_end < len(original[source_index]) else len(raw)
                spans = item['spans']
                if not spans or not all(index == output_index and a >= left and b <= right
                                        for index, a, b in spans): continue
            return True
    return False


def verify_disposition_comments(row, findings, source_quotes, finding_evidence, *, reviewed=None, outcomes=None,
                                original_paragraphs=None, original_path=None):
    """Return gaps, never synthesize consent or change unresolved business facts."""
    gaps = []
    if not source_quotes:
        gaps.append('适用性判断批注须引用合同原文锚点。')
    linked = []
    for identity in row['finding_ids']:
        finding = findings[identity]
        if not any(field == 'comment' for field, quote in finding_evidence.get(identity, [])):
            continue
        if (finding.get('action') not in {'comment', 'replace', 'insert', 'delete', 'delete_paragraph'}
                or not str(finding.get('comment') or '').strip()):
            gaps.append('适用性判断须安排真实 Word 批注，不能仅写入报告：' + identity)
            continue
        linked.append((identity, finding))
    if not linked:
        gaps.append('须关联说明本规则判断依据或待明确事项的 finding.comment，并写入合同批注。')
    actual = anchored_comments(reviewed, original_path=original_path) if reviewed is not None else None
    location_cache = {}
    for identity, finding in linked:
        locations = _source_locations(row, finding, original_paragraphs, original_path) if original_paragraphs is not None else None
        if locations is not None and not locations:
            gaps.append('批注事项的目标或 occurrence 未对应引用的原文段落：' + identity)
        if outcomes is not None and outcomes.get(identity, {}).get('status') != 'applied':
            gaps.append('适用性判断批注未实际执行：' + identity)
        if actual is None:
            continue
        selector = finding.get('selector') or {}
        anchors = [finding.get('target_text'), finding.get('search'), selector.get('contains'),
                   finding.get('replacement_text'), finding.get('insert_text')]
        if finding.get('action') == 'insert':
            inserted = str(finding.get('replacement_text') or finding.get('insert_text') or '')
            anchors.extend(inserted.splitlines()[:1])
        if finding.get('action') == 'replace':
            before = str(finding.get('target_text') or finding.get('search') or '')
            after = str(finding.get('replacement_text') or '')
            prefix = 0
            while prefix < min(len(before), len(after)) and before[prefix] == after[prefix]: prefix += 1
            suffix = 0
            while suffix < min(len(before), len(after))-prefix and before[-suffix-1] == after[-suffix-1]: suffix += 1
            anchors.extend([before[prefix:len(before)-suffix if suffix else len(before)],
                            after[prefix:len(after)-suffix if suffix else len(after)]])
        anchors = [_normalized(value) for value in anchors if _normalized(value)]
        if not anchors:
            anchors = [_normalized(quote) for _, quote in source_quotes if _normalized(quote)]
        parts = {part for part, _ in source_quotes}
        content = _normalized(finding['comment'])
        if not any(item['part'] in parts and content in _normalized(item['text'])
                   and any(anchor in _normalized(item['anchor']) for anchor in anchors)
                   and (locations is None or _location_matches(item, finding, locations, original_paragraphs, location_cache))
                   for item in actual):
            gaps.append('最终合同缺少与本事项对应且锚点闭合的完整批注：' + identity)
    return gaps
