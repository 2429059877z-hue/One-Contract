# 修订策略兼容入口

本文件保留旧调用路径。新任务以 `redline-comment-policy.md` 为动作政策的单点真相。

## 核心口径

- 能在不虚构事实的前提下形成专业文本时，优先直接修订。
- 有合理主方案但存在变量时，修订加批注。
- 事实或授权会改变结构性方案时，才仅批注并升级。
- 能局部删除、补入或替换时，不整段重写；但“最小”应完整修复机制。
- 重大修订可附简短解释，纯格式修改不增加批注负担。

## 执行字段

在 review plan 显式写出 `evidence_state`、`action`、改文载荷、`assumptions`、`unknown_facts`、`comment` 和 `linked_clauses`。执行器不得根据风险描述中的关键词替代专业判断。

## DOCX 边界

- 插入、删除和批注必须是真实 OOXML 结构；
- 跨 run 定位须规范化并检测歧义；
- 历史修订不得非法嵌套；
- 执行后必须运行 `quality_gate.py`，生成接受/拒绝修订副本并渲染检查。

完整规则见 `redline-comment-policy.md` 与 `ooxml-quality-gates.md`。
