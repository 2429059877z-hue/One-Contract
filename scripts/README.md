# One-Contract 执行器

执行器负责实现已经完成专业判断的 review plan，不根据风险描述中的关键词推断法律结论、商业授权或风险级别。

## 模块化企业合同知识

- `knowledge/route_enterprise_contract.py`：输出 v2 稳定合同画像；只有标题、交易结构、旧大类证据和我方角色一致时才解除人工确认门。
- `knowledge/select_active_assets.py`：按主辅类型、角色和场景只返回 `active` 原则卡及条款模块；本候选版会返回空资产集和 `blocked_need_approval`。
- `knowledge/validate_assets.py`：检查资产状态、模块字段、manifest一致性以及原始路径、合同族标识和长哈希泄露。
- `knowledge/detect_equity_transfer_triggers.py`：对结构化股权转让事实检查多文本价格不一致、无独立商业实质的对价改名、出资瑕疵和优先购买权程序异常；只用于候选校验。

上述脚本不读取本地合同库，也不能把资产从候选状态自动晋级。

股权转让触发器示例：

```bash
python scripts/knowledge/detect_equity_transfer_triggers.py --facts /path/to/equity-facts.json
```

事实文件可包含 `documents`、`declared_consideration`、`actual_total_consideration`、`payments`、`capital_status` 和 `preemption`。脚本不从关键词直接推定违法；材料缺失时要求人工确认，P0 命中时阻断自动改文。

## 依赖与测试

```bash
python3 -m pip install -r scripts/requirements.txt
PYTHONPATH=. python3 -m unittest discover -s scripts/tests -v
```

盲测、CI 或并行评估必须隔离运行态配置，避免测试身份与审查记忆写入 Skill 本体或串入其他 case：

```bash
export CONTRACT_COPILOT_CONFIG_DIR=/path/to/current-case/runtime-config
export CONTRACT_COPILOT_REVIEW_MEMORY=/path/to/current-case/runtime-config/review-memory.json
```

每个 case 使用独立目录，并把该目录随本轮输入、输出和失败记录一并留存。正式使用若需要跨次保留审查偏好，可以继续使用 Skill 默认配置目录。

## 标准执行

每次使用新的输出目录，避免覆盖旧结果：

```bash
python scripts/review/apply_review_plan.py \
  --input /path/original.docx \
  --plan /path/review-plan.json \
  --output /path/round-01/reviewed.docx \
  --quality-dir /path/round-01/quality \
  --author "审查人" \
  --organization "机构"
```

执行器默认：

- 不覆盖既有输出、报告或日志；
- 将所有被编辑的 XML/RELS 统一序列化为 UTF-8；
- 生成含真实 `w:ins` / `w:del` / Word 批注的审阅件；
- 运行严格质量门并生成接受版、拒绝版与 `quality-gate.json`；
- 任一 finding 或质量门失败时仍尽量保留产物和日志，但以非零状态退出。

`--no-validate` 只关闭编辑过程中的轻量检查，不关闭最终严格质量门，不应用于交付。

## 历史修订

默认 `--existing-revisions reject`：目标段落含历史修订时不直接改写，该 finding 失败并记录原因；可将计划改为批注。

只有明确决定以“接受历史修订后的文本”为本轮基线时，才使用：

```bash
--existing-revisions accept-existing
```

此策略会先在临时副本中接受历史修订，再应用本轮修订；原件不变，执行日志记录历史修订数量和基线策略，拒绝本轮修订后的语义与原件的接受视图比较。

## Review plan

```json
{
  "meta": {
    "contract_name": "协商解除劳动合同协议",
    "party_role": "用人单位",
    "review_intensity": "常规",
    "edit_policy": "revise-first"
  },
  "findings": [
    {
      "id": "ET-001",
      "risk_level": "P1",
      "evidence_state": "reasonable_assumption",
      "action": "replace",
      "target_text": "原条款中的唯一连续文本",
      "replacement_text": "可直接进入合同的主方案文本",
      "assumptions": ["当前采用的事实假设"],
      "unknown_facts": ["需要确认的变量"],
      "comment": "请确认变量；若结论相反，按备选方向调整。",
      "linked_clauses": ["结算条款", "权利终结条款"],
      "basis_type": "legal_boundary"
    }
  ]
}
```

证据状态与默认动作：

| `evidence_state` | 动作 |
|---|---|
| `confirmed` | 有改文载荷时真实修订 |
| `reasonable_assumption` | 真实修订并附假设/待确认批注 |
| `major_choice` | 仅批注；用户选择后改为 `confirmed` |
| `not_applicable` | 跳过并记录 |

`action` 支持 `auto/comment/report-only/delete/insert/replace/none/skip`。`auto` 只读取显式证据状态和改文载荷。若 `assertions_unverified` 为真，必须同时明确 `uses_placeholders_or_conditions=true`，否则直接改文会降为批注。

定位支持：

- `target_text` / `search`；
- `occurrence`（从 1 开始）；
- `selector.tag / attrs / line_number / contains / occurrence`。

跨 run 定位会对全角/不换行空格、连续空白和不可见字符作规范化，但同一规范化文本多次出现时拒绝猜测，必须提供 occurrence 或更窄 selector。

## 独立质量门

```bash
python scripts/docx_engine/quality_gate.py reviewed.docx \
  --original original.docx \
  --output-dir qa \
  --baseline-view reject \
  --require-revisions
```

质量门检查 ZIP、全部 XML/RELS、UTF-8/16 编码声明、内部关系、content types、评论锚点、修订属性，重新打开最终 DOCX，并生成接受/拒绝副本。它不替代 LibreOffice/Microsoft Word 渲染与逐页检查。

## 目录

```text
scripts/
  review/apply_review_plan.py   主流程
  review/plan_loader.py         结构化证据状态补全
  review/action_executor.py     动作执行
  docx/reviewer.py              定位、修订、批注
  docx/revision_views.py        接受/拒绝副本
  docx/quality_gate.py          严格 OOXML 质量门
  report/                       Markdown/DOCX 审查意见书
  tests/                        回归测试
```
