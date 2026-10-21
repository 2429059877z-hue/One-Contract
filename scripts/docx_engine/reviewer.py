#!/usr/bin/env python3
"""
合同审查文档操作工具

功能：
1. 添加批注（批注模式）
2. 标记删除（修订模式）
3. 建议插入（修订模式）
4. 读取合同文本

使用示例：
    from scripts import ContractReviewer

    # 初始化
    reviewer = ContractReviewer("workspace/unpacked")

    # 添加批注
    node = reviewer.find_text("甲方")
    reviewer.add_comment(node, "建议明确甲方的具体法律主体")

    # 修订模式删除
    reviewer.suggest_deletion(node)

    # 保存
    reviewer.save()

依赖：
    - defusedxml 库
"""

import html
import difflib
import re
import unicodedata

from .document import Document, DocxXMLEditor

MAX_VISIBLE_CHANGE_SEGMENTS = 6
MERGEABLE_EQUAL_SEGMENT_LENGTH = 1
MERGEABLE_EQUAL_SEGMENT_PATTERN = re.compile(
    r'^[\s，。；：、,.!?！？（）()“”"\'《》〈〉【】\[\]\-—/]+$'
)
INVISIBLE_TEXT_CHARS = {"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"}

#: \u6bb5\u843d/\u5b57\u7b26\u5c5e\u6027\u4e0e\u6279\u6ce8\u951a\u70b9\u7b49**\u975e\u6b63\u6587**\u5143\u7d20\u3002\u6587\u672c\u62bd\u53d6\u5fc5\u987b\u6574\u68f5\u8df3\u8fc7\uff1a
#: \u4e0d\u8df3\u8fc7 `w:pPr` \u4f1a\u628a `w:pPr/w:tabs/w:tab`\uff08\u5236\u8868\u4f4d**\u5b9a\u4e49**\uff09\u8bfb\u6210\u6b63\u6587\u5236\u8868\u7b26 `\t`\uff0c
#: \u6bb5\u843d\u91cd\u5199\u65f6\u53c8\u628a\u5b83\u4eec\u5f53\u6587\u672c\u5199\u56de\uff0c\u4f7f\u4fee\u8ba2\u540e\u7684\u6bb5\u843d\u51ed\u7a7a\u591a\u51fa\u5236\u8868\u7b26
#: \uff08\u5b9e\u6d4b\uff1a\u67d0\u6761\u88ab\u4fee\u8ba2\u7684\u6bb5\u843d\u6bb5\u9996\u591a\u51fa 2 \u4e2a `\t`\uff0c"\u62d2\u7edd\u5168\u90e8\u4fee\u8ba2"\u89c6\u56fe\u56e0\u6b64\u4e0e\u539f\u7a3f\u4e0d\u4e00\u81f4\uff09\u3002
_NON_CONTENT_ELEMENT_TAGS = frozenset(
    {
        "w:pPr",
        "w:rPr",
        "w:sectPr",
        "w:tblPr",
        "w:trPr",
        "w:tcPr",
        "w:commentRangeStart",
        "w:commentRangeEnd",
        "w:commentReference",
        "w:bookmarkStart",
        "w:bookmarkEnd",
        "w:proofErr",
        "w:lastRenderedPageBreak",
    }
)

#: 允许出现在插入容器内、随重建原样搬运的零宽标记（无文字、不参与接受视图）。
#: 批注锚点（commentRange*）**不在**此列——它们与批注线程绑定，位置必须严格保持，
#: 含批注锚点的容器一律不改写。
_PASSTHROUGH_REVISION_MARKUP = frozenset(
    {"w:bookmarkStart", "w:bookmarkEnd", "w:proofErr", "w:lastRenderedPageBreak"}
)


class _NestedEditNotApplicable(Exception):
    """目标不满足「在他人插入内嵌套改写」的安全条件。

    调用方捕获后应回退到既有拒绝路径（保持原报错提示不变），
    该异常本身不对外呈现。
    """


class ContractReviewer:
    """合同审查操作封装类

    封装 docx 技能的 Document 类，提供合同审查专用 API。
    支持批注模式和修订模式两种操作方式。

    Attributes:
        doc: 底层 Document 实例
    """

    def __init__(self, unpacked_dir, author="合同审查助手", initials="CA"):
        """
        初始化合同审查器

        Args:
            unpacked_dir: 解包后的 DOCX 目录路径
            author: 批注/修订的作者名称（默认"合同审查助手"）
            initials: 作者缩写（默认"CA"）

        Raises:
            ValueError: 如果目录不存在或不是有效的解包 DOCX 目录
        """
        self.doc = Document(
            unpacked_dir,
            track_revisions=True,
            author=author,
            initials=initials,
        )

    def find_text(self, text, tag="w:p", occurrence=None):
        """
        查找包含指定文本的节点

        Args:
            text: 要查找的文本
            tag: XML 标签类型（默认段落 w:p）

        Returns:
            找到的 DOM 节点

        Raises:
            ValueError: 如果未找到匹配节点或找到多个匹配

        Example:
            node = reviewer.find_text("甲方应承担")
            node = reviewer.find_text("金额", tag="w:r")
        """
        try:
            return self.find_node(tag=tag, contains=text, occurrence=occurrence)
        except ValueError:
            if tag != "w:p":
                raise
            matches = self._locate_text_occurrences_in_paragraphs(text)
            if not matches:
                raise
            if occurrence is None and len(matches) != 1:
                raise ValueError(
                    "Multiple normalized paragraph matches found. Add occurrence or selector."
                )
            index = 1 if occurrence is None else int(occurrence)
            if index < 1 or index > len(matches):
                raise ValueError(
                    f"Occurrence {index} out of range. Found {len(matches)} normalized matches."
                )
            return matches[index - 1]["paragraph"]

    def find_node(
        self,
        tag="w:p",
        attrs=None,
        line_number=None,
        contains=None,
        occurrence=None,
    ):
        """
        通用节点查询（支持 attrs / line_number / contains 组合过滤）

        Args:
            tag: XML 标签类型
            attrs: 属性字典
            line_number: 行号或 range
            contains: 文本包含条件

        Returns:
            DOM 节点
        """
        return self.doc["word/document.xml"].get_node(
            tag=tag,
            attrs=attrs,
            line_number=line_number,
            contains=contains,
            occurrence=occurrence,
        )

    def find_by_line(self, line_number, tag="w:p", occurrence=None):
        """
        按行号查找节点

        Args:
            line_number: XML 文件中的行号（1-indexed）
            tag: XML 标签类型

        Returns:
            找到的 DOM 节点

        Raises:
            ValueError: 如果未找到匹配节点

        Example:
            node = reviewer.find_by_line(42)
            node = reviewer.find_by_line(range(40, 50))
        """
        return self.doc["word/document.xml"].get_node(
            tag=tag, line_number=line_number, occurrence=occurrence
        )

    def find_by_attrs(self, tag, attrs, occurrence=None):
        """
        按属性查找节点

        Args:
            tag: XML 标签类型
            attrs: 属性字典

        Returns:
            找到的 DOM 节点

        Raises:
            ValueError: 如果未找到匹配节点

        Example:
            node = reviewer.find_by_attrs("w:p", {"w14:paraId": "12345678"})
        """
        return self.doc["word/document.xml"].get_node(
            tag=tag, attrs=attrs, occurrence=occurrence
        )

    def add_comment(self, node, text, end_node=None):
        """
        添加批注（批注模式）

        在指定节点范围添加批注。适用于指出风险、提供建议等场景。

        Args:
            node: 批注起始节点
            text: 批注内容
            end_node: 批注结束节点（可选，默认与起始相同）

        Returns:
            int: 批注 ID

        Example:
            node = reviewer.find_text("甲方承担全部责任")
            reviewer.add_comment(node, "P0风险：责任条款过重，建议限定损失范围")
        """
        end = end_node if end_node else node
        return self.doc.add_comment(start=node, end=end, text=text)

    def add_comment_by_text(self, target_text, comment_text, tag="w:p", occurrence=None):
        """
        按文本定位并添加批注

        Args:
            target_text: 目标文本（要求唯一匹配）
            comment_text: 批注内容
            tag: 匹配标签类型，默认段落 w:p

        Returns:
            int: 批注 ID
        """
        node = self.find_text(target_text, tag=tag, occurrence=occurrence)
        return self.add_comment(node, comment_text)

    def reply_to_comment(self, parent_comment_id, text):
        """
        回复已有批注

        Args:
            parent_comment_id: 父批注的 ID
            text: 回复内容

        Returns:
            int: 新批注 ID

        Example:
            reviewer.reply_to_comment(0, "已确认修改")
        """
        return self.doc.reply_to_comment(parent_comment_id=parent_comment_id, text=text)

    def suggest_deletion(self, node):
        """
        建议删除（修订模式）

        将节点标记为删除，使用 Track Changes 样式。
        适用于明确错误需要删除的场景。

        Args:
            node: 要标记删除的节点（w:r 或 w:p）

        Returns:
            修改后的节点

        Raises:
            ValueError: 如果节点已包含跟踪修改

        Example:
            run = reviewer.find_text("违约金为全部合同金额", tag="w:r")
            reviewer.suggest_deletion(run)
        """
        return self.doc["word/document.xml"].suggest_deletion(node)

    def delete_paragraph(self, node, comment_text=None):
        """Track removal of a complete plain paragraph, including its mark.

        The following paragraph keeps its own properties. Terminal paragraphs,
        section breaks, existing revisions and structural/anchored content need
        explicit handling and are rejected before the document is changed.
        """
        if getattr(node, "tagName", None) != "w:p":
            raise ValueError("delete_paragraph 只接受 w:p 段落")
        following = node.nextSibling
        while following is not None and following.nodeType != following.ELEMENT_NODE:
            following = following.nextSibling
        if following is None or following.tagName != "w:p":
            raise ValueError("delete_paragraph 需要紧邻的后续段落；末段或表格边界请用文字删除并人工确认段落结构")
        from .revision_views import REVISION_TAGS
        if self._has_tracked_changes(node) or any(node.getElementsByTagName(tag) for tag in REVISION_TAGS):
            raise ValueError("delete_paragraph 暂不支持已有修订的段落；请先 accept-existing 或改用批注")
        self._assert_plain_rewrite(node)
        protected = {"w:sectPr", "w:bookmarkStart", "w:bookmarkEnd", "w:commentRangeStart", "w:commentRangeEnd", "w:commentReference"}
        if any(node.getElementsByTagName(tag) for tag in protected):
            raise ValueError("delete_paragraph 不支持节分隔、书签或已有批注锚点，请先结构化处理或仅添加批注")
        for child in node.childNodes:
            if child.nodeType != child.ELEMENT_NODE:
                continue
            if child.tagName not in {"w:pPr", "w:r", "w:proofErr"}:
                raise ValueError("delete_paragraph 不支持该段落的复杂结构")
            if child.tagName == "w:r" and any(
                item.nodeType == item.ELEMENT_NODE and item.tagName not in {"w:rPr", "w:t", "w:tab"}
                for item in child.childNodes
            ):
                raise ValueError("delete_paragraph 不支持该段落的复杂行内结构")
        clone = node.cloneNode(True)
        ppr = self._get_direct_child(clone, "w:pPr")
        if ppr is None:
            ppr = clone.ownerDocument.createElement("w:pPr")
            clone.insertBefore(ppr, clone.firstChild)
        rpr = self._get_direct_child(ppr, "w:rPr")
        if rpr is None:
            rpr = clone.ownerDocument.createElement("w:rPr")
            # pPr rPr precedes sectPr/pPrChange; both are excluded above.
            ppr.appendChild(rpr)
        rpr.insertBefore(clone.ownerDocument.createElement("w:del"), rpr.firstChild)
        for text in list(clone.getElementsByTagName("w:t")):
            replacement = clone.ownerDocument.createElement("w:delText")
            for index in range(text.attributes.length):
                attr = text.attributes.item(index)
                replacement.setAttribute(attr.name, attr.value)
            while text.firstChild:
                replacement.appendChild(text.firstChild)
            text.parentNode.replaceChild(replacement, text)
        deletion = clone.ownerDocument.createElement("w:del")
        for child in list(clone.childNodes):
            if child is not ppr:
                deletion.appendChild(child)
        clone.appendChild(deletion)
        nodes = self.doc["word/document.xml"].replace_node(node, clone.toxml())
        if comment_text:
            self.add_comment(nodes[0], comment_text)
        return {"deleted": nodes[0], "fallback": "paragraph_mark"}

    def suggest_insertion(self, node, xml_content):
        """
        建议插入（修订模式）

        在节点后插入新内容，使用 Track Changes 样式。
        适用于需要添加新内容的场景。

        Args:
            node: 插入位置参考节点
            xml_content: 要插入的 XML 内容（段落格式）

        Returns:
            list: 插入的新节点列表

        Example:
            para = reviewer.find_text("第一条")
            new_content = '<w:p><w:r><w:t>新增条款内容</w:t></w:r></w:p>'
            reviewer.suggest_insertion(para, new_content)
        """
        wrapped = DocxXMLEditor.suggest_paragraph(
            xml_content, namespace_root=self.doc["word/document.xml"].dom.documentElement
        )
        return self.doc["word/document.xml"].insert_after(node, wrapped)

    def _clone_context_formatting(self, node):
        """Return ``(pPr_xml, rPr_xml)`` cloned from the anchor paragraph.

        An inserted paragraph should inherit the surrounding formatting —
        font, size, indentation, spacing and list numbering — instead of
        falling back to the document default. Only the nearest ``w:p``
        ancestor is used, and any section break is stripped from the cloned
        paragraph properties so the insertion does not create a nested
        section.

        Args:
            node: 插入锚点节点（通常是 ``w:p``）

        Returns:
            tuple[str, str]: 锚点段落的 ``w:pPr`` 与首个可见 run 的 ``w:rPr``
        """
        para = node
        if getattr(node, "tagName", None) != "w:p":
            cursor = node
            for _ in range(10):
                cursor = getattr(cursor, "parentNode", None)
                if cursor is None:
                    break
                if getattr(cursor, "tagName", None) == "w:p":
                    para = cursor
                    break

        ppr_xml = ""
        rpr_xml = ""
        if getattr(para, "tagName", None) != "w:p":
            return ppr_xml, rpr_xml

        for child in getattr(para, "childNodes", ()):
            if getattr(child, "tagName", None) == "w:pPr":
                cloned = child.cloneNode(True)
                for sub in list(cloned.childNodes):
                    if getattr(sub, "tagName", None) == "w:sectPr":
                        cloned.removeChild(sub)
                for tag in ("w:pPrChange", "w:rPrChange", "w:ins", "w:del"):
                    for old in list(cloned.getElementsByTagName(tag)):
                        old.parentNode.removeChild(old)
                ppr_xml = cloned.toxml()
                break

        for run in para.getElementsByTagName("w:r"):
            for child in getattr(run, "childNodes", ()):
                if getattr(child, "tagName", None) == "w:rPr":
                    cloned = child.cloneNode(True)
                    for old in list(cloned.getElementsByTagName("w:rPrChange")):
                        old.parentNode.removeChild(old)
                    rpr_xml = cloned.toxml()
                    break
            if rpr_xml:
                break

        return ppr_xml, rpr_xml

    def insert_text_after(self, node, text, as_paragraph=False):
        """
        在指定节点后插入文本（带修订痕迹）

        Args:
            node: 插入锚点节点
            text: 要插入的纯文本内容
            as_paragraph: 是否按段落插入（默认 False，按 run 插入）

        Returns:
            list: 插入的新节点列表
        """
        if as_paragraph:
            if getattr(node, "tagName", None) != "w:p":
                raise ValueError("段落插入必须以 w:p 为锚点")
            properties = self._get_direct_child(node, "w:pPr")
            mark_properties = self._get_direct_child(properties, "w:rPr") if properties is not None else None
            if mark_properties is not None and self._get_direct_child(mark_properties, "w:del") is not None:
                # This physical paragraph merges with its successor in the
                # accepted view. Inserting here would split that logical
                # paragraph instead of adding after the visible anchor.
                raise ValueError("插入锚点含待删除的段落标记，接受视图会与后段合并；请先 accept-existing 固定段落边界，再执行段落插入")
            ppr_xml, rpr_xml = self._clone_context_formatting(node)
            paragraphs = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            # Prepare the entire batch before inserting; retain deliberate blank lines.
            xml = "".join(
                DocxXMLEditor.suggest_paragraph(
                    f"<w:p>{ppr_xml}<w:r>{rpr_xml}{self._build_text_element_xml(value)}</w:r></w:p>",
                    namespace_root=self.doc["word/document.xml"].dom.documentElement,
                )
                for value in paragraphs
            )
            return self.doc["word/document.xml"].insert_after(node, xml)

        insertion_xml = f"<w:ins><w:r>{self._build_text_element_xml(text)}</w:r></w:ins>"
        return self.doc["word/document.xml"].insert_after(node, insertion_xml)

    def _get_document_text(self, node):
        return self.doc["word/document.xml"]._get_element_text(node)

    def _get_accepted_text(self, node):
        """Return the final/accepted text view for a node containing revisions.

        **只收正文**：段落属性、字符属性与批注锚点等元素整棵跳过
        （见 `_NON_CONTENT_ELEMENT_TAGS`）。否则 `w:pPr/w:tabs` 里的制表位定义
        会被读成正文制表符，并在段落重写时写回成字面 `\\t`。
        """
        if getattr(node, "nodeType", None) != node.ELEMENT_NODE:
            return ""
        if getattr(node, "tagName", "") in _NON_CONTENT_ELEMENT_TAGS:
            return ""
        if getattr(node, "tagName", "") in {"w:del", "w:moveFrom", "w:delText"}:
            return ""
        if getattr(node, "tagName", "") in {"w:t", "w:instrText"}:
            return "".join(
                child.data
                for child in node.childNodes
                if child.nodeType == child.TEXT_NODE
            )
        inline = {"w:tab": "\t", "w:cr": "\n", "w:br": "\n",
                  "w:noBreakHyphen": "\u2011", "w:softHyphen": "\u00ad"}
        if getattr(node, "tagName", "") in inline:
            return inline[node.tagName]
        return "".join(
            self._get_accepted_text(child)
            for child in node.childNodes
            if child.nodeType == child.ELEMENT_NODE
        )

    def _get_direct_child(self, node, tag_name):
        for child in node.childNodes:
            if child.nodeType == child.ELEMENT_NODE and child.nodeName == tag_name:
                return child
        return None

    def _find_ancestor(self, node, tag_name):
        current = node
        while current is not None:
            if getattr(current, "tagName", None) == tag_name:
                return current
            current = getattr(current, "parentNode", None)
        return None

    def _assert_plain_rewrite(self, node):
        # Flattening these nodes would lose layout or non-text content. Keep the
        # original XML untouched and require an explicit structural edit first.
        protected = {"w:cr", "w:br", "w:noBreakHyphen", "w:softHyphen",
                     "w:drawing", "w:object", "w:pict", "w:fldChar", "w:instrText"}
        found = sorted(tag for tag in protected if node.getElementsByTagName(tag))
        if found:
            raise ValueError("目标包含不可扁平重写的行内结构 " + ", ".join(found)
                             + "；请先结构化处理并核对原文，或仅添加批注。")

    def _has_tracked_changes(self, node):
        revision_tags = {"w:ins", "w:del", "w:moveFrom", "w:moveTo"}
        if getattr(node, "tagName", "") in revision_tags:
            return True
        ancestor = getattr(node, "parentNode", None)
        while ancestor is not None:
            if getattr(ancestor, "tagName", "") in revision_tags:
                return True
            ancestor = getattr(ancestor, "parentNode", None)
        return any(node.getElementsByTagName(tag) for tag in revision_tags)

    @staticmethod
    def _normalize_text_with_map(text):
        """Normalize Word-visible text while retaining offsets into the source string.

        Word frequently splits visually continuous text across runs and represents
        spaces using NBSP/full-width spaces. Locators compare a normalized view but
        edits still slice the original paragraph text with the returned offsets.
        """
        normalized = []
        source_indexes = []
        for source_index, char in enumerate(text):
            if char in INVISIBLE_TEXT_CHARS:
                continue
            expanded = unicodedata.normalize("NFKC", char)
            for normalized_char in expanded:
                if normalized_char.isspace():
                    normalized_char = " "
                    if normalized and normalized[-1] == " ":
                        continue
                normalized.append(normalized_char)
                source_indexes.append(source_index)
        return "".join(normalized), source_indexes

    def _get_first_text_run(self, paragraph_node):
        for run in paragraph_node.getElementsByTagName("w:r"):
            if self._get_document_text(run):
                return run
        return None

    def _get_run_properties_xml(self, run_node):
        if run_node is None:
            return ""
        rpr = self._get_direct_child(run_node, "w:rPr")
        if rpr is None:
            return ""
        clone = rpr.cloneNode(True)
        # Splitting one run copies its property history. Allocate a fresh
        # annotation ID for each copy while retaining author/date and values.
        for change in clone.getElementsByTagName("w:rPrChange"):
            if change.hasAttribute("w:id"):
                change.removeAttribute("w:id")
        return clone.toxml()

    def _build_text_element_xml(self, text, *, deleted=False):
        escaped = html.escape(text, quote=False)
        tag_name = "w:delText" if deleted else "w:t"
        preserve_space = text[:1].isspace() or text[-1:].isspace()
        space_attr = ' xml:space="preserve"' if preserve_space else ""
        return f"<{tag_name}{space_attr}>{escaped}</{tag_name}>"

    def _build_run_xml(self, text, template_run=None, *, deleted=False):
        if not text:
            return ""
        rpr_xml = self._get_run_properties_xml(template_run)
        text_xml = self._build_text_element_xml(text, deleted=deleted)
        return f"<w:r>{rpr_xml}{text_xml}</w:r>"

    def _append_revision_segment(self, segments, kind, text):
        if not text:
            return
        if segments and segments[-1][0] == kind:
            previous_kind, previous_text = segments[-1]
            segments[-1] = (previous_kind, previous_text + text)
            return
        segments.append((kind, text))

    def _is_change_segment(self, kind):
        return kind in {"delete", "insert"}

    def _is_mergeable_equal_segment(self, text):
        if not text:
            return False
        if len(text) <= MERGEABLE_EQUAL_SEGMENT_LENGTH:
            return True
        return bool(MERGEABLE_EQUAL_SEGMENT_PATTERN.fullmatch(text))

    def _project_revision_block_text(self, segments, *, include_equal, include_delete, include_insert):
        parts = []
        for kind, text in segments:
            if kind == "equal" and include_equal:
                parts.append(text)
            elif kind == "delete" and include_delete:
                parts.append(text)
            elif kind == "insert" and include_insert:
                parts.append(text)
        return "".join(parts)

    def _merge_revision_change_blocks(self, segments, equal_index):
        left = equal_index - 1
        while left - 1 >= 0 and self._is_change_segment(segments[left - 1][0]):
            left -= 1

        right = equal_index + 1
        while right + 1 < len(segments) and self._is_change_segment(segments[right + 1][0]):
            right += 1

        block = segments[left : right + 1]
        old_text = self._project_revision_block_text(
            block,
            include_equal=True,
            include_delete=True,
            include_insert=False,
        )
        new_text = self._project_revision_block_text(
            block,
            include_equal=True,
            include_delete=False,
            include_insert=True,
        )

        replacement = []
        if old_text == new_text:
            self._append_revision_segment(replacement, "equal", old_text)
        else:
            self._append_revision_segment(replacement, "delete", old_text)
            self._append_revision_segment(replacement, "insert", new_text)

        compacted = []
        for kind, text in segments[:left] + replacement + segments[right + 1 :]:
            self._append_revision_segment(compacted, kind, text)
        return compacted

    def _count_visible_change_segments(self, segments):
        return sum(1 for kind, _ in segments if self._is_change_segment(kind))

    def _compact_revision_segments(self, segments):
        compacted = list(segments)

        merged = True
        while merged:
            merged = False
            for index in range(1, len(compacted) - 1):
                kind, text = compacted[index]
                if kind != "equal":
                    continue
                if not self._is_change_segment(compacted[index - 1][0]):
                    continue
                if not self._is_change_segment(compacted[index + 1][0]):
                    continue
                if not self._is_mergeable_equal_segment(text):
                    continue
                compacted = self._merge_revision_change_blocks(compacted, index)
                merged = True
                break

        while self._count_visible_change_segments(compacted) > MAX_VISIBLE_CHANGE_SEGMENTS:
            candidates = []
            for index in range(1, len(compacted) - 1):
                kind, text = compacted[index]
                if kind != "equal":
                    continue
                if not self._is_change_segment(compacted[index - 1][0]):
                    continue
                if not self._is_change_segment(compacted[index + 1][0]):
                    continue
                candidates.append((len(text), index))
            if not candidates:
                break
            _, merge_index = min(candidates)
            compacted = self._merge_revision_change_blocks(compacted, merge_index)

        return compacted

    def _build_revision_segments(self, old_text, new_text):
        matcher = difflib.SequenceMatcher(a=old_text, b=new_text, autojunk=False)
        segments = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                self._append_revision_segment(segments, "equal", old_text[i1:i2])
            elif tag == "delete":
                self._append_revision_segment(segments, "delete", old_text[i1:i2])
            elif tag == "insert":
                self._append_revision_segment(segments, "insert", new_text[j1:j2])
            elif tag == "replace":
                self._append_revision_segment(segments, "delete", old_text[i1:i2])
                self._append_revision_segment(segments, "insert", new_text[j1:j2])
        return self._compact_revision_segments(segments)

    def _surround_revision_segments(self, segments, before_text="", after_text=""):
        combined = []
        self._append_revision_segment(combined, "equal", before_text)
        for kind, text in segments:
            self._append_revision_segment(combined, kind, text)
        self._append_revision_segment(combined, "equal", after_text)
        return combined

    def _build_change_fragment_xml(self, segments, template_run=None):
        parts = []
        for kind, text in segments:
            if kind == "equal":
                parts.append(self._build_run_xml(text, template_run))
            elif kind == "delete":
                parts.append(
                    f"<w:del>{self._build_run_xml(text, template_run, deleted=True)}</w:del>"
                )
            elif kind == "insert":
                parts.append(f"<w:ins>{self._build_run_xml(text, template_run)}</w:ins>")
        return "".join(parts)

    def _find_first_change_node(self, nodes):
        queue = list(nodes)
        while queue:
            node = queue.pop(0)
            if getattr(node, "tagName", None) in {"w:ins", "w:del", "w:p", "w:r"}:
                if getattr(node, "tagName", None) in {"w:ins", "w:del"}:
                    return node
            for child in getattr(node, "childNodes", []):
                if getattr(child, "nodeType", None) == child.ELEMENT_NODE:
                    queue.append(child)
        return nodes[0] if nodes else None

    def _build_paragraph_xml(self, paragraph_node, text):
        """整段重写：保留段落属性与**首个正文 run 的字符格式**。

        此前这里产出的是裸 `<w:r>`，会丢掉原段落的字体/字号等字符格式；
        段落属性里的制表位定义也一度被当成正文制表符（见 `_get_accepted_text`）。
        """
        ppr = self._get_direct_child(paragraph_node, "w:pPr")
        ppr_xml = ppr.toxml() if ppr is not None else ""
        template_run = self._get_first_text_run(paragraph_node)
        return f"<w:p>{ppr_xml}{self._build_run_xml(text, template_run)}</w:p>"

    def _build_fragment_paragraph_xml(
        self,
        paragraph_node,
        *,
        segments,
        template_run=None,
    ):
        ppr = self._get_direct_child(paragraph_node, "w:pPr")
        ppr_xml = ppr.toxml() if ppr is not None else ""
        body_xml = self._build_change_fragment_xml(segments, template_run=template_run)
        comment_starts = "".join(
            node.toxml()
            for node in paragraph_node.getElementsByTagName("w:commentRangeStart")
        )
        comment_ends = "".join(
            node.toxml()
            for node in paragraph_node.getElementsByTagName("w:commentRangeEnd")
        )
        reference_runs = []
        seen_reference_ids = set()
        for reference in paragraph_node.getElementsByTagName("w:commentReference"):
            comment_id = reference.getAttribute("w:id")
            if comment_id in seen_reference_ids:
                continue
            run = self._find_ancestor(reference, "w:r")
            if run is not None:
                reference_runs.append(run.toxml())
                seen_reference_ids.add(comment_id)
        return (
            f"<w:p>{ppr_xml}{comment_starts}{body_xml}"
            f"{comment_ends}{''.join(reference_runs)}</w:p>"
        )

    # ------------------------------------------------------------------
    # 嵌套修订：目标完全落在他人未接受的插入（w:ins）内时做外科式改写
    # ------------------------------------------------------------------

    def _ins_shell_open_tag(self, ins_node):
        """序列化 w:ins 的开始标签并剥掉 w:id——拆分出的片段由注入器分配新 id。"""
        attrs = []
        for index in range(ins_node.attributes.length):
            attribute = ins_node.attributes.item(index)
            if attribute.name == "w:id":
                continue
            attrs.append(
                f'{attribute.name}="{html.escape(attribute.value, quote=True)}"'
            )
        return "<w:ins " + " ".join(attrs) + ">" if attrs else "<w:ins>"

    def _collect_accepted_pieces(self, paragraph_node):
        """按文档序收集段落接受视图的文字片段，并记录各片段所属的插入容器。

        返回 (pieces, supported)。supported=False 表示存在无法忠实映射或无法安全
        重建的结构（行内换行等已由 _assert_plain_rewrite 先行拦截；此处针对修订
        结构）：多层插入、批注锚点或无法保留的非 run 子元素，
        或段落含 w:moveTo / 制表符等无法映射的节点。书签及单层历史删除可保留。
        """
        pieces = []
        supported = [True]
        container = [None]

        def visit(node):
            if not supported[0] or node.nodeType != node.ELEMENT_NODE:
                return
            tag = node.tagName
            if tag in _NON_CONTENT_ELEMENT_TAGS:
                return
            if tag in {"w:del", "w:moveFrom", "w:delText"}:
                return
            if tag in {"w:moveTo", "w:cr", "w:br", "w:tab"}:
                supported[0] = False
                return
            if tag == "w:ins":
                if container[0] is not None:
                    supported[0] = False
                    return
                for child in node.childNodes:
                    if child.nodeType != child.ELEMENT_NODE:
                        continue
                    if child.tagName == "w:r":
                        continue
                    if child.tagName == "w:del":
                        # 源文件自带的跨作者嵌套（如 A 的插入里删 B 的文字）：
                        # 零宽、不贡献接受视图文本；仅当内部无再嵌套修订时放行。
                        if child.getElementsByTagName("w:ins") or child.getElementsByTagName("w:del"):
                            supported[0] = False
                            return
                        continue
                    if child.tagName not in _PASSTHROUGH_REVISION_MARKUP:
                        supported[0] = False
                        return
                container[0] = node
                for child in node.childNodes:
                    visit(child)
                container[0] = None
                return
            if tag == "w:r":
                for child in node.childNodes:
                    if (
                        child.nodeType == child.ELEMENT_NODE
                        and child.tagName not in {"w:rPr", "w:t"}
                    ):
                        supported[0] = False
                        return
                for text_node in node.getElementsByTagName("w:t"):
                    text = "".join(
                        part.data
                        for part in text_node.childNodes
                        if part.nodeType == part.TEXT_NODE
                    )
                    pieces.append(
                        {"text": text, "container": container[0], "run": node}
                    )
                return
            supported[0] = False

        for child in paragraph_node.childNodes:
            visit(child)
        return pieces, supported[0]

    def _replace_within_insertion(self, match, new_text, comment_text=None):
        """目标完全落在单一历史插入（w:ins）内时的嵌套改写。

        产出结构（W=目标所在的既有插入，T=目标文本，R=替换文本）：

            前缀 w:ins(W) ｜ w:ins(W){ w:del(本轮){ T } } ｜ w:ins(本轮){ R } ｜ 后缀 w:ins(W)

        全部接受＝前缀＋R＋后缀；拒绝 W 移除历史插入，但保留本轮独立的 R；
        拒绝本轮＝w:del 还原为 T、移除 R（回到 W 的原始插入文本）。
        w:ins(W) 片段保留原作者 author/date，仅由注入器分配新的 w:id。

        不满足安全条件时抛出 _NestedEditNotApplicable，由调用方回退原拒绝提示。
        """
        paragraph = match["paragraph"]
        pieces, supported = self._collect_accepted_pieces(paragraph)
        if not supported:
            raise _NestedEditNotApplicable()
        if "".join(piece["text"] for piece in pieces) != match["paragraph_text"]:
            raise _NestedEditNotApplicable()

        start, end = match["start"], match["end"]
        if start >= end:
            raise _NestedEditNotApplicable()

        container = None
        offset = 0
        for piece in pieces:
            piece_start, piece_end = offset, offset + len(piece["text"])
            offset = piece_end
            if piece_end <= start or piece_start >= end:
                continue
            if piece["container"] is None or (
                container is not None and piece["container"] is not container
            ):
                raise _NestedEditNotApplicable()
            container = piece["container"]
        if container is None:
            raise _NestedEditNotApplicable()
        container_author = container.getAttribute("w:author")
        if not container_author or container_author == self.doc.author:
            raise _NestedEditNotApplicable()

        ins_start = ins_end = None
        offset = 0
        for piece in pieces:
            if piece["container"] is container:
                if ins_start is None:
                    ins_start = offset
                ins_end = offset + len(piece["text"])
            offset += len(piece["text"])
        if ins_start is None or start < ins_start or end > ins_end:
            raise _NestedEditNotApplicable()
        inner_start, inner_end = start - ins_start, end - ins_start

        prepared = self._prepare_insertion_container(container)
        if prepared["length"] != (ins_end - ins_start):
            raise _NestedEditNotApplicable()
        if any(kind == "shadow" for kind, _ in prepared["trailing"]):
            raise _NestedEditNotApplicable()
        shell = prepared["shell"]
        fragments = []
        prefix_xml = self._render_insertion_range(prepared, 0, inner_start)
        if prefix_xml:
            fragments.append(f"{shell}{prefix_xml}</w:ins>")
        fragments.append(
            f"{shell}{self._render_insertion_deletion(prepared, inner_start, inner_end)}</w:ins>"
        )
        if new_text:
            template_run = next(
                (
                    run
                    for run, run_start, run_text in prepared["runs"]
                    if run_start <= inner_start < run_start + len(run_text)
                ),
                prepared["runs"][0][0],
            )
            fragments.append(
                f"<w:ins>{self._build_run_xml(new_text, template_run)}</w:ins>"
            )
        suffix_xml = self._render_insertion_range(
            prepared, inner_end, prepared["length"]
        )
        suffix_xml += "".join(xml for _, xml in prepared["trailing"])
        if suffix_xml:
            fragments.append(f"{shell}{suffix_xml}</w:ins>")

        nodes = self.doc["word/document.xml"].replace_node(
            container, "".join(fragments)
        )
        anchor = self._find_first_del_node(nodes)
        if comment_text and anchor is not None:
            self.add_comment(anchor, comment_text)
        return {"deleted": anchor, "inserted": nodes, "fallback": "nested_insertion"}

    def _prepare_insertion_container(self, container):
        """为一个可安全重建的插入容器准备材料（run 文字切片 + 零宽标记相对位置）。"""
        run_slices = []
        markup_before_run = {}
        run_index = 0
        cursor = 0
        for child in container.childNodes:
            if child.nodeType != child.ELEMENT_NODE:
                continue
            if child.tagName in _PASSTHROUGH_REVISION_MARKUP:
                markup_before_run.setdefault(run_index, []).append(
                    ("markup", child.toxml())
                )
                continue
            if child.tagName == "w:del":
                # 源文件自带的跨作者嵌套删除：零宽影子结构，重建时须保留在
                # 原插入容器内、其文档序位置附近（不可进入本轮 w:del 内部）。
                markup_before_run.setdefault(run_index, []).append(
                    ("shadow", child.toxml())
                )
                continue
            if child.tagName != "w:r":
                raise _NestedEditNotApplicable()
            text = "".join(
                "".join(
                    part.data
                    for part in text_node.childNodes
                    if part.nodeType == part.TEXT_NODE
                )
                for text_node in child.getElementsByTagName("w:t")
            )
            if not text:
                markup_before_run.setdefault(run_index, []).append(("markup", child.toxml()))
                continue
            run_slices.append((child, cursor, text))
            cursor += len(text)
            run_index += 1
        if not run_slices:
            raise _NestedEditNotApplicable()
        trailing = markup_before_run.pop(len(run_slices), [])
        return {
            "container": container,
            "runs": run_slices,
            "length": cursor,
            "markup_before_run": markup_before_run,
            "trailing": trailing,
            "emitted": set(),
            "shell": self._ins_shell_open_tag(container),
        }

    @staticmethod
    def _take_pending_markup(prepared, index):
        pending = prepared["markup_before_run"].get(index)
        if pending and index not in prepared["emitted"]:
            prepared["emitted"].add(index)
            return "".join(xml for _, xml in pending)
        return ""

    def _render_insertion_range(
        self, prepared, range_start, range_end
    ):
        rendered = []
        for index, (run, run_start, run_text) in enumerate(prepared["runs"]):
            run_end = run_start + len(run_text)
            overlap_start = max(range_start, run_start)
            overlap_end = min(range_end, run_end)
            if overlap_start >= overlap_end:
                continue
            rendered.append(
                self._take_pending_markup(prepared, index)
            )
            rendered.append(
                self._build_run_xml(
                    run_text[overlap_start - run_start : overlap_end - run_start],
                    run,
                )
            )
        return "".join(rendered)

    def _render_insertion_deletion(self, prepared, range_start, range_end):
        # Keep historical zero-width deletions at their exact run boundary. A
        # single outer deletion would swallow them; moving them would change
        # the result of rejecting both this reviewer and their original author.
        rendered = []
        for index, (run, run_start, run_text) in enumerate(prepared["runs"]):
            start = max(range_start, run_start)
            end = min(range_end, run_start + len(run_text))
            if start >= end:
                continue
            rendered.append(self._take_pending_markup(prepared, index))
            rendered.append(
                f"<w:del>{self._build_run_xml(run_text[start-run_start:end-run_start], run, deleted=True)}</w:del>"
            )
        return "".join(rendered)

    def _find_first_del_node(self, nodes):
        for node in nodes:
            candidates = ([node] if node.tagName == "w:del" else []) + list(node.getElementsByTagName("w:del"))
            for candidate in candidates:
                if candidate.getAttribute("w:author") == self.doc.author:
                    return candidate
        return None

    @staticmethod
    def _iter_elements(node):
        for child in node.childNodes:
            if child.nodeType != child.ELEMENT_NODE:
                continue
            yield child
            yield from ContractReviewer._iter_elements(child)

    def _replace_with_nested_edits(self, match, new_text, comment_text=None):
        """含历史修订段落的改写入口（两种受支持情形）。

        - 目标完全落在单一历史插入（`w:ins`）内：交由 `_replace_within_insertion`
          生成标准嵌套（前缀/中段/后缀 + 并列替换插入）。
        - 目标跨越插入与普通文字的边界（含多段插入与普通文字交替、或段落他处有修订
          而目标位于普通文字）：按上下文分段重建——普通文字段以本轮 `w:del` 删除；
          插入内部分在保留原作者前缀/后缀片段的同时嵌套本轮 `w:del`；替换文本以本轮
          `w:ins` 插在最后一段被删内容之后。四种视图均可正确还原。
        其余情形（同一作者插入、位于历史删除内、容器含批注锚点或结构不可安全重建）
        抛 `_NestedEditNotApplicable`，由调用方回退原拒绝提示。
        """
        paragraph = match["paragraph"]
        pieces, supported = self._collect_accepted_pieces(paragraph)
        if not supported:
            raise _NestedEditNotApplicable()
        if "".join(piece["text"] for piece in pieces) != match["paragraph_text"]:
            raise _NestedEditNotApplicable()
        start, end = match["start"], match["end"]
        if start >= end:
            raise _NestedEditNotApplicable()

        offsets = []
        offset = 0
        for piece in pieces:
            piece_start = offset
            offset += len(piece["text"])
            offsets.append((piece, piece_start, offset))

        overlap = [
            (piece, piece_start, piece_end, max(piece_start, start), min(piece_end, end))
            for piece, piece_start, piece_end in offsets
            if piece_end > start and piece_start < end
        ]
        if not overlap:
            raise _NestedEditNotApplicable()

        for piece, _, _, _, _ in overlap:
            container = piece["container"]
            if container is None:
                continue
            author = container.getAttribute("w:author")
            if not author or author == self.doc.author:
                raise _NestedEditNotApplicable()

        first_piece = overlap[0][0]
        if (
            first_piece["container"] is not None
            and all(
                item[0]["container"] is first_piece["container"] for item in overlap
            )
            and overlap[0][3] == start
            and overlap[-1][4] == end
        ):
            return self._replace_within_insertion(
                match, new_text, comment_text=comment_text
            )

        template_rpr_xml = self._get_run_properties_xml(first_piece["run"])

        # 受影响元素（文档序）：被覆盖的普通 run，与被覆盖的插入容器
        affected = []
        seen = set()
        for node in self._iter_elements(paragraph):
            tag = node.tagName
            if tag == "w:ins":
                hit = any(item[0]["container"] is node for item in overlap)
            elif tag == "w:r":
                hit = any(
                    item[0]["container"] is None and item[0]["run"] is node
                    for item in overlap
                )
            else:
                continue
            if hit and id(node) not in seen:
                seen.add(id(node))
                affected.append(node)
        if not affected:
            raise _NestedEditNotApplicable()

        plans = []
        for element in affected:
            if element.tagName == "w:ins":
                prepared = self._prepare_insertion_container(element)
                ins_start = ins_end = None
                for piece, piece_start, piece_end in offsets:
                    if piece["container"] is element:
                        if ins_start is None:
                            ins_start = piece_start
                        ins_end = piece_end
                if ins_start is None or prepared["length"] != (ins_end - ins_start):
                    raise _NestedEditNotApplicable()
                local_start = max(start, ins_start) - ins_start
                local_end = min(end, ins_end) - ins_start
                if local_start >= local_end:
                    raise _NestedEditNotApplicable()
                if any(kind == "shadow" for kind, _ in prepared["trailing"]):
                    raise _NestedEditNotApplicable()
                shell = prepared["shell"]
                parts = []
                prefix_xml = self._render_insertion_range(prepared, 0, local_start)
                if prefix_xml:
                    parts.append(f"{shell}{prefix_xml}</w:ins>")
                parts.append(
                    f"{shell}{self._render_insertion_deletion(prepared, local_start, local_end)}</w:ins>"
                )
                if element is affected[-1] and new_text:
                    parts.append(
                        f"<w:ins><w:r>{template_rpr_xml}"
                        f"{self._build_text_element_xml(new_text)}</w:r></w:ins>"
                    )
                suffix_xml = self._render_insertion_range(
                    prepared, local_end, prepared["length"]
                )
                suffix_xml += "".join(xml for _, xml in prepared["trailing"])
                if suffix_xml:
                    parts.append(f"{shell}{suffix_xml}</w:ins>")
                plans.append((element, "".join(parts)))
            else:
                run_pieces = [
                    (piece, piece_start, piece_end)
                    for piece, piece_start, piece_end in offsets
                    if piece["container"] is None and piece["run"] is element
                ]
                if not run_pieces:
                    raise _NestedEditNotApplicable()
                run_text = "".join(piece["text"] for piece, _, _ in run_pieces)
                run_base = run_pieces[0][1]
                local_start = max(start, run_base) - run_base
                local_end = min(end, run_pieces[-1][2]) - run_base
                if local_start >= local_end:
                    raise _NestedEditNotApplicable()
                parts = []
                prefix_text = run_text[:local_start]
                if prefix_text:
                    parts.append(self._build_run_xml(prefix_text, element))
                parts.append(
                    f"<w:del>{self._build_run_xml(run_text[local_start:local_end], element, deleted=True)}</w:del>"
                )
                if element is affected[-1] and new_text:
                    parts.append(
                        f"<w:ins><w:r>{template_rpr_xml}"
                        f"{self._build_text_element_xml(new_text)}</w:r></w:ins>"
                    )
                suffix_text = run_text[local_end:]
                if suffix_text:
                    parts.append(self._build_run_xml(suffix_text, element))
                plans.append((element, "".join(parts)))

        # Parse every fragment before mutation (including replacement text), so
        # malformed XML in the final fragment cannot leave a partial edit.
        for _, xml in plans:
            self.doc["word/document.xml"]._parse_fragment(xml)
        # 全部片段渲染成功后再统一落笔，避免中途拒绝留下半改文档。
        inserted_nodes = []
        first_change_anchor = None
        for element, xml in plans:
            nodes = self.doc["word/document.xml"].replace_node(element, xml)
            del_node = self._find_first_del_node(nodes)
            if del_node is None:
                raise _NestedEditNotApplicable()
            if first_change_anchor is None:
                first_change_anchor = del_node
            inserted_nodes.extend(nodes)

        if comment_text and first_change_anchor is not None:
            self.add_comment(first_change_anchor, comment_text)
        return {
            "deleted": first_change_anchor,
            "inserted": inserted_nodes,
            "fallback": "nested_span",
        }

    def _replace_within_run(
        self,
        run_node,
        *,
        old_text,
        new_text,
        comment_text=None,
    ):
        self._assert_plain_rewrite(run_node)
        if self._has_tracked_changes(run_node):
            raise ValueError("run already contains tracked changes")

        run_text = self._get_document_text(run_node)
        start = run_text.find(old_text)
        if start < 0:
            raise ValueError("target text not found inside run")

        revision_segments = self._build_revision_segments(old_text, new_text)
        replacement_xml = self._build_change_fragment_xml(
            self._surround_revision_segments(
                revision_segments,
                before_text=run_text[:start],
                after_text=run_text[start + len(old_text) :],
            ),
            template_run=run_node,
        )
        nodes = self.doc["word/document.xml"].replace_node(run_node, replacement_xml)
        anchor = self._find_first_change_node(nodes)
        if comment_text and anchor is not None:
            self.add_comment(anchor, comment_text)
        return {
            "deleted": anchor,
            "inserted": nodes,
            "fallback": "run_fragment",
        }

    def _locate_text_occurrences_in_paragraphs(self, text):
        normalized_target, _ = self._normalize_text_with_map(str(text))
        if not normalized_target:
            raise ValueError("target text is empty after normalization")
        matches = []
        for paragraph in self.get_paragraphs():
            # Locate against the accepted view. This keeps later findings stable
            # after an earlier finding has inserted w:del/w:ins segments.
            paragraph_text = self._get_accepted_text(paragraph)
            normalized_text, source_indexes = self._normalize_text_with_map(paragraph_text)
            start = 0
            while True:
                index = normalized_text.find(normalized_target, start)
                if index < 0:
                    break
                raw_start = source_indexes[index]
                raw_end = source_indexes[index + len(normalized_target) - 1] + 1
                matches.append(
                    {
                        "paragraph": paragraph,
                        "paragraph_text": paragraph_text,
                        "start": raw_start,
                        "end": raw_end,
                        "normalized_target": normalized_target,
                    }
                )
                start = index + len(normalized_target)
        return matches

    def replace_text_via_paragraph_rewrite(
        self,
        old_text,
        new_text,
        occurrence=None,
        comment_text=None,
    ):
        matches = self._locate_text_occurrences_in_paragraphs(old_text)
        if not matches:
            raise ValueError(
                f"Node not found: <w:p> containing '{old_text}'. "
                "Text may be split across elements or use different wording."
            )
        if occurrence is None and len(matches) > 1:
            raise ValueError(
                "Multiple paragraph matches found for spanning text. "
                "Add occurrence or selector to narrow the search."
            )

        index = 1 if occurrence is None else int(occurrence)
        if index < 1 or index > len(matches):
            raise ValueError(
                f"Occurrence {index} out of range for spanning text. "
                f"Only {len(matches)} match(es) found."
            )

        match = matches[index - 1]
        paragraph = match["paragraph"]
        self._assert_plain_rewrite(paragraph)
        if self._has_tracked_changes(paragraph):
            try:
                return self._replace_with_nested_edits(
                    match, new_text, comment_text=comment_text
                )
            except _NestedEditNotApplicable:
                raise ValueError(
                    "目标段落含修订，可能来自本轮第一次改写；同一段落的多处修改须合并为一次。"
                    "若确属输入稿的历史修订，可先 accept-existing 固定基线；也可仅添加批注。"
                ) from None
        paragraph_text = match["paragraph_text"]
        start = match["start"]
        end = match["end"]
        updated_text = (
            paragraph_text[:start]
            + new_text
            + paragraph_text[end:]
        )
        replacement_xml = self._build_paragraph_xml(paragraph, updated_text)
        deleted = self.suggest_deletion(paragraph)
        inserted = self.suggest_insertion(deleted, replacement_xml)

        if comment_text and inserted:
            self.add_comment(inserted[0], comment_text)

        return {
            "deleted": deleted,
            "inserted": inserted,
            "fallback": "paragraph_rewrite",
        }

    def replace_text_via_paragraph(
        self,
        old_text,
        new_text,
        occurrence=None,
        comment_text=None,
    ):
        matches = self._locate_text_occurrences_in_paragraphs(old_text)
        if not matches:
            raise ValueError(
                f"Node not found: <w:p> containing '{old_text}'. "
                "Text may be split across elements or use different wording."
            )
        if occurrence is None and len(matches) > 1:
            raise ValueError(
                "Multiple paragraph matches found for spanning text. "
                "Add occurrence or selector to narrow the search."
            )

        index = 1 if occurrence is None else int(occurrence)
        if index < 1 or index > len(matches):
            raise ValueError(
                f"Occurrence {index} out of range for spanning text. "
                f"Only {len(matches)} match(es) found."
            )

        match = matches[index - 1]
        paragraph = match["paragraph"]
        self._assert_plain_rewrite(paragraph)
        if self._has_tracked_changes(paragraph):
            try:
                return self._replace_with_nested_edits(
                    match, new_text, comment_text=comment_text
                )
            except _NestedEditNotApplicable:
                raise ValueError(
                    "目标段落含修订，可能来自本轮第一次改写；同一段落的多处修改须合并为一次。"
                    "若确属输入稿的历史修订，可先 accept-existing 固定基线；也可仅添加批注。"
                ) from None

        paragraph_text = match["paragraph_text"]
        start = match["start"]
        end = match["end"]
        # Match normalization locates the span; the deletion must retain the
        # exact source slice, including NBSP and repeated spaces.
        revision_segments = self._build_revision_segments(paragraph_text[start:end], new_text)
        template_run = self._get_first_text_run(paragraph)
        replacement_xml = self._build_fragment_paragraph_xml(
            paragraph,
            segments=self._surround_revision_segments(
                revision_segments,
                before_text=paragraph_text[:start],
                after_text=paragraph_text[end:],
            ),
            template_run=template_run,
        )
        nodes = self.doc["word/document.xml"].replace_node(paragraph, replacement_xml)
        anchor = self._find_first_change_node(nodes)
        if comment_text and anchor is not None:
            self.add_comment(anchor, comment_text)

        return {
            "deleted": anchor,
            "inserted": nodes,
            "fallback": "paragraph_fragment",
        }

    def replace_text(
        self,
        old_text,
        new_text,
        tag="w:r",
        comment_text=None,
        occurrence=None,
    ):
        """
        替换文本（删除旧文本 + 插入新文本）

        Args:
            old_text: 需替换的原文本（唯一匹配）
            new_text: 替换后的新文本
            tag: 匹配标签，默认 w:r；段落替换可用 w:p
            comment_text: 可选，替换后在新节点添加批注

        Returns:
            dict: {"deleted": 删除节点, "inserted": 插入节点列表}
        """
        try:
            target = self.find_text(old_text, tag=tag, occurrence=occurrence)
            if tag == "w:r":
                return self._replace_within_run(
                    target,
                    old_text=old_text,
                    new_text=new_text,
                    comment_text=comment_text,
                )
            if tag == "w:p":
                return self.replace_text_via_paragraph(
                    old_text=old_text,
                    new_text=new_text,
                    occurrence=occurrence,
                    comment_text=comment_text,
                )
        except ValueError as exc:
            if tag == "w:r":
                return self.replace_text_via_paragraph(
                    old_text=old_text,
                    new_text=new_text,
                    occurrence=occurrence,
                    comment_text=comment_text,
                )
            raise exc
        deleted = self.suggest_deletion(target)

        anchor = deleted
        if (
            tag == "w:r"
            and getattr(deleted, "parentNode", None) is not None
            and getattr(deleted.parentNode, "tagName", "") == "w:del"
        ):
            anchor = deleted.parentNode

        inserted = self.insert_text_after(anchor, new_text, as_paragraph=(tag == "w:p"))

        if comment_text and inserted:
            self.add_comment(inserted[0], comment_text)

        return {"deleted": deleted, "inserted": inserted}

    def delete_text(self, target_text, tag="w:r", comment_text=None, occurrence=None):
        try:
            node = self.find_text(target_text, tag=tag, occurrence=occurrence)
            if tag == "w:r":
                return self._replace_within_run(
                    node,
                    old_text=target_text,
                    new_text="",
                    comment_text=comment_text,
                )
            if tag == "w:p":
                return self.replace_text_via_paragraph(
                    old_text=target_text,
                    new_text="",
                    occurrence=occurrence,
                    comment_text=comment_text,
                )
            deleted = self.suggest_deletion(node)
            if comment_text:
                self.add_comment(node, comment_text)
            return {"deleted": deleted, "fallback": None}
        except ValueError as exc:
            if tag == "w:r":
                return self.replace_text_via_paragraph(
                    old_text=target_text,
                    new_text="",
                    occurrence=occurrence,
                    comment_text=comment_text,
                )
            raise exc

    def replace_node(self, node, new_text, tag="w:r", comment_text=None):
        """
        按节点替换文本（删除目标节点 + 插入新文本）

        Args:
            node: 要替换的目标节点
            new_text: 替换后的新文本
            tag: 目标标签类型，用于判断插入模式（w:p 段落 / w:r run）
            comment_text: 可选，替换后在新节点添加批注

        Returns:
            dict: {"deleted": 删除节点, "inserted": 插入节点列表}
        """
        deleted = self.suggest_deletion(node)

        anchor = deleted
        if (
            tag == "w:r"
            and getattr(deleted, "parentNode", None) is not None
            and getattr(deleted.parentNode, "tagName", "") == "w:del"
        ):
            anchor = deleted.parentNode

        inserted = self.insert_text_after(anchor, new_text, as_paragraph=(tag == "w:p"))

        if comment_text and inserted:
            self.add_comment(inserted[0], comment_text)

        return {"deleted": deleted, "inserted": inserted}

    def revert_insertion(self, node):
        """
        撤销插入（修订模式）

        拒绝已有的插入，将其转为删除标记。

        Args:
            node: 要处理的 w:ins 节点或包含 w:ins 的容器

        Returns:
            list: 处理后的节点列表

        Raises:
            ValueError: 如果节点不包含 w:ins 元素
        """
        return self.doc["word/document.xml"].revert_insertion(node)

    def revert_deletion(self, node):
        """
        撤销删除（修订模式）

        拒绝已有的删除，恢复被删除的内容。

        Args:
            node: 要处理的 w:del 节点或包含 w:del 的容器

        Returns:
            list: 处理后的节点列表

        Raises:
            ValueError: 如果节点不包含 w:del 元素
        """
        return self.doc["word/document.xml"].revert_deletion(node)

    def save(self, destination=None, validate=True):
        """
        保存修改后的文档

        Args:
            destination: 输出目录（可选，默认覆盖原目录）
            validate: 是否验证文档有效性（默认 True）

        Raises:
            ValueError: 如果验证失败
        """
        self.doc.save(destination=destination, validate=validate)

    def get_full_text(self):
        """
        获取合同全文文本

        Returns:
            str: 文档的纯文本内容

        Example:
            text = reviewer.get_full_text()
            print(f"合同共 {len(text)} 字")
        """
        text_parts = []
        for elem in self.doc["word/document.xml"].dom.getElementsByTagName("w:t"):
            if elem.firstChild:
                text_parts.append(elem.firstChild.data)
        return "".join(text_parts)

    def get_paragraphs(self):
        """
        获取所有段落节点

        Returns:
            list: w:p 节点列表

        Example:
            for para in reviewer.get_paragraphs():
                # 处理每个段落
                pass
        """
        return list(self.doc["word/document.xml"].dom.getElementsByTagName("w:p"))

    def validate(self):
        """
        验证文档有效性

        Raises:
            ValueError: 如果验证失败
        """
        self.doc.validate()

    def set_operation_timestamp(self, timestamp):
        self.doc.set_operation_timestamp(timestamp)

    def clear_operation_timestamp(self):
        self.doc.clear_operation_timestamp()
