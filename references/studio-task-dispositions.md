# Studio 客户规则与合同处理记录

本契约适用于 Studio 中明确选择客户、固定客户快照的合同任务。未选择客户的旧审查计划保持兼容。Agent 交接文件中的 `client_rule_requirements` 提供该快照的全部规则及真实解析结果。审查 Agent 须结合合同全文判断每条客户要求的适用性；目录、角色、场景及机器解析提示是判断输入，不能代替全文判断。不能只在 `resolution_provenance` 写上规则版本便宣布已落实。

## 整份合同与定向审查

创建任务时，`context.review_scope` 省略即为 `whole_contract`，并固定 `clause_groups: []`。此模式审查整份合同。合同中缺少付款章等内容，不构成排除客户付款规则的理由，可能恰好需要新增条款。

只有明确采用 `review_scope: "clause_groups"`，同时提供非空受控 `clause_groups`，才进行定向审查；规则限定组与所选组无交集时，解析器给出真实 `scope_mismatch[clause_groups]` 排除理由。未知类别、重复类别或整份审查搭配非空子集均会被拒绝，不按猜测补齐。

受控类别由 `one_contract_rules.clause_groups` 提供，交接文件 `client_rule_disposition_contract.clause_group_options` 含中文名称。不要以公共知识资产中名称相似的字段自动猜测映射。

## 逐规则处理记录

审查计划的 `meta.client_rule_dispositions` 须逐条覆盖固定客户快照，不能重复或引用其他客户规则。

| 字段 | 内容 |
| --- | --- |
| `rule_id` / `content_hash` | 直接使用交接文件中的规则编号及内容哈希 |
| `status` | `applied`、`already_satisfied`、`commented`、`not_applicable`；旧 `requires_confirmation` 兼容为批注处理流程 |
| `explanation` | 本合同如何处理该规则的具体说明 |
| `finding_ids` | 关联的真实审查事项编号；已有满足、或正式启用/有效期/用户限定范围等确定排除时可为空，其余批注处理须引用真实 finding |
| `clause_group` | 对限定 `scope.clause_groups` 的规则，落实或已有满足时须明确一个受控类别，属于规则范围；定向审查时还须属于本次所选范围 |
| `evidence` | 下表中的一项或多项可复核证据 |

| `evidence.source` | 必填内容与核验方式 |
| --- | --- |
| `input` | `part`（默认 `word/document.xml`）、从 0 开始的 `paragraph_index`、`quote`；引用原件接受现有修订后的段落，必须精确匹配。用于证明特定组已满足或处理既有条款时，另填一致的 `clause_group` |
| `context` | `pointer`、`value`；JSON pointer 如 `/our_role` 或 `/facts/...`，值须与创建任务时固定的上下文相同。普通角色、地址等事实不能当作放弃适用规则的授权 |
| `resolution` | `reason`；须与交接文件的 `exclusion_reason` 完全一致 |
| `finding` | `finding_id`、`field`、`quote`；字段允许 `target_text`、`replacement_text`、`insert_text`、`comment`、`risk`。处理限定组时，证据、关联 finding 和 disposition 均填写同一 `clause_group` |
| `task_confirmation` | `evidence_id`；引用当前任务内用户明确记录的人工决定，后台核验全部绑定与用途 |

示例中的合同内容、规则编号和哈希须替换为真实值：

```json
{
  "rule_id": "<交接文件中的规则编号>",
  "content_hash": "sha256:<交接文件中的真实哈希>",
  "status": "applied",
  "clause_group": "payment",
  "explanation": "客户要求将付款期限明确为十五日，已在 R1 修改正文。",
  "finding_ids": ["R1"],
  "evidence": [
    {"source": "input", "paragraph_index": 0, "quote": "甲方应于十日内付款。", "clause_group": "payment"},
    {"source": "finding", "finding_id": "R1", "field": "replacement_text", "quote": "十五日", "clause_group": "payment"}
  ]
}
```

关联 `R1` 也须填 `clause_group: "payment"`。缺少归属或证据未明确归属时，任务仍可保存为兼容状态 `awaiting_confirmation`，表示处理依据待补全，并非要求人工逐条审批。Agent 应补齐实际归属，不会由系统自动猜组；填写超出规则或本次审查范围的类别会被拒绝。

`applied` 的正文事项必须以 `evidence_state=confirmed` 为基础，并关联真实 `replace`、`insert`、`delete` 或 `delete_paragraph`。交付时重新比对实际接受版、独立编辑核验结果和执行状态。不能用仅报告或批注代替应落实的正文修改。缺少原条款时，可引用真实原件锚点及同组的新增 `insert` finding 证据，不要求虚构原合同已有该组内容。删除引用 `target_text`，并由独立全文投影核验。

`already_satisfied` 须引用归属该组的真实原文，且这段证据在最终接受版仍存在。不能仅说“已满足”而不提供条款依据。

## 全文判断与真实合同批注

- `not_applicable`：Agent 根据合同全文判断本规则不适用，须引用 `input` 原文及关联 finding 的 `comment`，说明具体条款、交易事实和不适用理由。一般角色或上下文值本身不能代替这一判断，不再要求本规则专属人工审批。
- `commented`：适用前提尚不确定、事实不足、存在商业选择，或规则要求提示进一步核实时，用真实合同批注保留问题、理由和待核实事项。须同时引用 `input` 锚点和关联 finding 的 `comment`。旧 `requires_confirmation` 按同一批注流程接收，不因其名称要求先取得人工点击。
- 正式启用、有效期、行政约束或用户明确限定条款组等确定排除，可只引用匹配的 `resolution` 理由。不为定向审查范围外的规则强加合同批注，也不得把业务适用性疑问伪装成这种排除。

可用的关联动作包括带有真实 `comment` 的 `comment`、`replace`、`insert`、`delete`、`delete_paragraph`。单纯 `report-only`、只有风险描述、报告中的文字或文首汇总，均不能替代对应条款上的批注。事实和选择尚未确定时，不得在正文中把它们写成确定事实；其他已有依据的正文修改应继续完成。

例如，计划可保留确定的付款期限修改，同时另设一条批注核实验收前提。下例只展示格式，规则哈希、原文位置及措辞须使用本任务真实值：

```json
{
  "rule_id": "<固定规则编号>",
  "content_hash": "<固定规则哈希>",
  "status": "commented",
  "clause_group": "payment",
  "explanation": "现有付款条款未明确验收前提，已在 C1 批注中说明需核实的安排。",
  "finding_ids": ["C1"],
  "evidence": [
    {"source": "input", "paragraph_index": 0, "quote": "甲方应于十日内付款。", "clause_group": "payment"},
    {"source": "finding", "finding_id": "C1", "field": "comment", "quote": "请核实本项付款是否以验收为前提；现有条款未明确该安排。", "clause_group": "payment"}
  ]
}
```

关联 `C1` 必须是计划中的真实事项，在原文位置安排上述批注；`status` 和说明本身不会生成批注。执行后及最终验收时，系统重新检查实际审阅 DOCX 的批注内容、锚点、关联执行结果与原文绑定。批注缺失、错位或仅存在于报告中时不能通过。补齐有效批注后，语义判断或商业选择尚未解决本身不再阻止其他修订和文件交付；最终产物应明确保留该事项，而不是声称已获批准或已解决。

规则效果为 `effect.action=escalate` 时，真实合同批注可以完成提示或升级事项的留痕，不强制先填写批准人。若确定规则要求实质正文修改，则仍按 `applied` 落实；不得为了避免修改把确定适用的要求一律标成 `commented`。

## 可选：补充已经取得的人工决定

人工记录用于保存已经实际取得的业务决定，不是逐条适用性判断或生成合同的必经步骤。无需伪造初始上下文或确认人；用户确已作出决定时可记录：

```python
repo.record_rule_confirmation(
    task_id, rule_id,
    purpose="not_applicable",  # 或 escalation
    decision="具体事实、核验结果及决定；不得由系统代填",
    confirmed_by="真实确认人",
    confirmation=True,
    expected_revision=current_revision,
    actor=local_user,
)
```

返回 `{"confirmation": {...}, "task": {...}}`。后台自动绑定 `task_id`、`rule_id`、`content_hash`、客户与快照、合同输入 SHA256 及用途，记录确认人、时间、决定，写入不可变证据文件；调用者不能自己填写这些身份哈希。缺少真实姓名、决定或明确勾选会被拒绝。

计划通过 `{"source": "task_confirmation", "evidence_id": "confirmation_..."}` 引用证据。记录确认后仍须重新提交明确引用新证据的计划，不自动改变处置结论，也不自动变成可交付。新决定使当前交付失效，但历史尝试保留；同一规则的新决定会阻止继续引用旧决定。交接文件的 `rule_confirmations` 可用于核对记录。

`purpose=escalation` 仅用于规则效果为 `effect.action=escalate` 的实际升级决定。人工记录不能替代规则所需的正文修改或真实批注；普通联系地址确认、其他规则、其他合同或不同用途的决定不能复用。仅为读取历史记录保留旧 `escalate` + `report-only` 路径：必须有真实匹配的人工决定及经核验的实际报告留痕；新的 AI 处理默认使用真实合同批注，不能借兼容路径省略批注。

兼容旧任务的固定上下文确认时，只接受 `/client_rule_confirmations/<rule_id>` 下严格绑定 `rule_id`、`content_hash`、`client_profile_id`、`client_policy_snapshot_id`、`input_sha256`、`purpose` 的对象，另须有 `confirmed: true`、真实 `confirmed_by`、`confirmed_at`、`decision`。一旦任务内记录更新决定，旧上下文确认不能覆盖它。

解析器的 `CLIENT_APPLICATION_REVIEW` 是交给全文 AI 的业务判断提示，`blocking=false`，不因事实未填、自然语言条件或尚无人工点击而阻断整个合同任务。快照损坏、身份/版本不匹配、硬边界冲突等独立错误仍须按具体提示处理；可选人工记录不能悄悄改变固定版本或消除这类错误。

这些检查证明规则版本、处理说明、文本证据和实际产物之间有可复核关系，并不自动证明法律判断正确或人工记录的现实身份。逐页检查与最终交付验收仍须真实完成；不得伪填确认。执行、导入产物、最终验收及读取已完成交付时均重新检查正文与批注证据，不能跳过。
