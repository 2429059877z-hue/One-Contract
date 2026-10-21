# 计划与交付完整性（3.3-preview）

这些检查处理已发生的静默缺陷。报错须回到计划或源稿修正，不可用 `--no-validate` 绕过。
本预览版纳入批量预检与段落动作等完整性修复，具体变更及验证边界见变更日志。

## 报告输入

`assets/knowledge_v2/schemas/review_plan.schema.json` 声明报告字段。
加载时统一把历史 `risks` 归为 `findings`；两者同时非空且不同会报冲突。空 `findings` 不会遮住非空 `risks`。
此格式归一化与 `--no-enrich-plan` 无关，预检、编辑、报告、交付检查使用同一意见集合。
每个进入正文意见的 finding 必须提供 `title`、`clause`（或 `clause_position` / `target_text`）、
`risk`（或 `description`）；缺失条款的位置可明确写“合同未约定”。不允许缺键后自动输出“待补充”。
合同概况写在 `summary.transaction_content`、`price_overview` 或 `contract_amount` + `payment_terms`、
`rights_obligations` 等声明字段；未知 summary 字段报错，内部补充资料放入 `meta.internal`。
`party_role` 使用身份枚举，描述性说明放入交易概况；未知值报错。
机构名称可留空；报告只显示审查人，不打印“未设置”。

纯税务条目用 `report_bucket=tax` + `tax_advice`（字符串或数组）；混合条目用 `mixed`。
自由文本偶然提到税务不会改变分类。显示字段里的 Markdown 加粗或行内代码标记会清理；
ID、原文锚点、替代正文和证据不做递归清洗。正文确实需要特殊格式时应使用对应 DOCX 操作。
内部编码保留在计划和日志，不输出到客户文件。

## 模块检查

将选择器输出的 `selection_context` 原样写为计划顶层 `knowledge_selection`。
凡声明主类型，都必须给出上下文，即使填的是 `domain_only` / `global_only` 或未填写覆盖层级。
计划及报告均根据该上下文重新调用选择器，不采信手填的模块名单；声明层级与实际不一致会报错，
未填层级但上下文完整时由选择结果生成报告深度。旧计划缺上下文须先补齐，不得伪造人工确认或删除主类型以规避。
每个选中模块在 `module_checklist` 中须有同名键，值为：

```json
{"status":"checked","reason":"逐项核查了风险及应有而未有的约定，说明结论与依据","finding_ids":["R001"]}
```

无问题时 `finding_ids=[]`，仍须解释已核查结论；不适用用 `not_applicable` + 具体理由。
不允许缺模块、虚构 finding 引用或空理由。选中模块为空壳或缺检查项时，报告自动披露议题，
不能手写“无”消除覆盖缺口。28 个历史空数组已由模块既有 `failure_mode` 逐项转为检查问题；
这不代表新增法律核验或独立质量验收。

分类确认结果：`accepted` / `accepted_with_fallback` 可按相应深度继续；
`needs_human_review` 须等待单案明示确认；确认未激活依赖后返回 `accepted_with_reduced_coverage`，
须把 `human_confirmation`、`cross_check`、`coverage_notice` 与 `verdict` 留在分类溯源，报告披露覆盖不足。
`data_error` 表示引用不存在的模块，应修正数据；`rejected` 表示类型无效或未启用，人工确认不能激活资产。

## 修改与源批注

`validate_plan(..., source_docx=..., existing_revisions=...)` 会在临时副本按顺序试执行，
正式执行入口自动调用。多个同名锚点须补从 1 开始的 `occurrence` 或精确 `selector`；
插入后的新段落可作为后续插入锚点。预检收集可检测错误并统一返回，失败动作先回滚再继续；
后续错误以“之前成功动作已应用、失败动作未应用”的临时状态为准，修正前序后应整份重跑。
`occurrence` 是本条执行时接受视图中的剩余匹配序号，不是原始位置编号。
执行入口将 finding ID、位置、原因与修正提示写入质量目录的 `preflight.json`；
可用 `--preflight-report` 指定路径，`--preflight-only` 仅检查而不出稿。
同段多次直接改写应先合并为一次；接受历史修订不能解除本轮修订防覆盖限制。
含 `w:cr`、`w:br`、特殊连字符、域或图片的扁平重写会被阻断，先结构化处理或改为批注。

`replace` 取最小充分区间，详见 `redline-comment-policy.md` 1.7；预检范围提示不自动缩短目标。
`insert` 默认在锚点所在段落后新增（`tag=w:p`），文本换行生成多个真实段落；显式 `tag=w:r` 不拆段。多条 finding 使用同一锚点仍是
后插在前；可合并成一个含换行的 `insert_text`，或以后续段落作为下一条的锚点。
`delete` 只删文字；完整编号项用 `delete_paragraph` 同时修订删除文字与段落标记。
段落删除须完整定位，目标后紧邻可合并段落；节分隔、末段、复杂结构会被明确拒绝。

`accept-existing` 先接受正文修订，但延迟删除失去锚点的源批注；随后由所选 orphan 策略
统一留痕、计数和隔离。源稿保持不变；隔离批注不能声称已经回复。

改变权利归属的 finding 设置 `rights_allocation_change=true`，并在
`summary.rights_allocation_changes` 为该 finding 填对象：`finding_id`、`original`（原约定）、
`revised`（改为）、`reason`（理由）、`alternative`（如不同意可改回的方案）。
报告在合同概况前显示这些说明，不显示 finding ID。
具体是否改变归属仍须审查人判断，不以关键词检测代替法律判断。

## 出稿后检查

执行入口自动运行 `scripts/review/delivery_checks.py`，结果写入日志及 `delivery-checks.json`：

- 独立读取 DOCX，比较拒绝本轮修改后的逐字文本、段落及行内换行、编号、节属性和结构件清单。
  `accept-existing` 以接受历史修订后的输入为基线；否则以拒绝历史修订后的输入为基线。
- 按目标位置、occurrence 与计划顺序独立计算完整预期段落，再与接受视图逐段核对。
  原文别处出现新字、只删除旧字、改错同名位置均不能冒充成功。
  支持文字锚点及段落/run 的 contains/attrs 选择器；仅行号或其他结构节点无法独立定位时明确失败，
  须补可核对的文字锚点或先结构化处理，不得退回全文计数放行。
- 扫描实际修订稿、批注和报告中的内部编码、finding ID、裸 Markdown、无事项名的数字占位符。
  修订稿按实际变化区段与基线比较，原文已有设备编号不会因形似内部码而误报；在其他位置新增同码仍会检查。
- 实际落文及报告中的“第 X 条”引用与输入基线核对；输入没有的新增条号须人工核对并修正计划。
  只存在于计划、已定向阻断或未采用的替代文字不当作已输出内容。
  该检查不证明引用的法律语义正确，也不替代法条检索。

失败将交付级别改为 blocked，执行命令非零退出。报告在最终检查后同步失败提示；
如报告因此更新，重查实际最终版本，每一版渲染证据分别保存，记录最终报告哈希。
不能收敛到一致状态时撤回本轮报告草稿。结构检查不替代 Word/LibreOffice 逐页视觉检查。
法条清单仍是待复核清单；当前版本不会凭离线材料声称已完成现行法检索。

## 报告字段速查

| 项目 | 写法与校验范围 |
|---|---|
| 条款引用 | `findings` 的输出及 `summary` 所有字段（含 `pending_items`、`core_conclusion`、`rights_allocation_changes`）均须核对。错误诊断附源合同可用引用集合 |
| 中文序号合同 | 原文仅有“一、二、”时不自行改称“第一条、第二条”；用原编号或条款主题定位 |
| 新增条款自指 | “新增第七条”也可能不在源引用集合中；写“拟新增的保密条款”等主题描述，再核对最终编号，不作无条件豁免 |
| 引用法律条文 | 《法律名称》须在同一“。／；／换行”分句内位于条号之前；引用放行不证明法条有效性 |
| 新增内容报告标签 | `insert` 显示“插入位置（其后）／建议新增”，锚点不是被替换的原条款 |
| 风险及待办枚举 | `risk_level` 仅 `P0/P1/P2`；`pending_kind` 仅 `fill/verify/authorization` |
| 权利归属备选方案 | `alternative` 直接写方案正文；避免重复“如不同意：”。条件句开头的“如甲方…”仍是有效内容 |
| 概况 | `summary.parties` 使用对象；`overall_risk` 用简短风险词；不使用未声明且不渲染的 `main_risks` |
| 对外文本 | 不输出内部编码、finding ID 或裸 `**`；DOCX 以实际产物检查为准 |


## 七项约束的回归映射

| 约束 | 检查阶段 / 实现 | 反例与合法近似例（scripts/tests/test_review_fixes.py） |
|---|---|---|
| RV-01 目标位置修改真实完成 | 最终 DOCX / expected_edits + delivery_checks | 只删未插、改错 occurrence；正确替换、空白规范化、连续插入、节点选择器 |
| RV-02 报告状态与最终检查一致 | 报告最终版本 / finalize_report | 交付失败、报告渲染失败；最终报告哈希一致 |
| RV-03 分类确认不能因缺字段跳过 | 计划及报告前 / module_checklist | 三类依赖缺上下文、未确认；人工确认后的覆盖披露 |
| RV-04 各入口使用同一意见集合 | 加载、预检、报告、交付 / plan_contract | risks 旧格式、空 findings、冲突列表；不补全时归一化 |
| RV-05 覆盖声明等于实际资产 | 计划及报告 / selected_assets | 手填高层级；未填层级时采用真实结果 |
| RV-06 只检查实际输出的引用 | 最终 DOCX 与报告 / delivery_checks | 实际写入未知条号；未输出的阻断建议 |
| RV-07 不把原文当新增内部码 | 最终 DOCX 变化区段 / delivery_checks | 新增同码；原有产品编号 |

此表用于维护和定位回归，不等于多轮稳定性或 evaluator 签名验收证据。
