# 模块化知识路由

## 审查顺序

1. 形成合同画像并通过分类确认门。
2. 对已配置结构触发器的类型，先核对交易文件、资金、出资和程序事实；命中 P0 时停止自动改文并升级人工复核。
3. 调用 `scripts/knowledge/select_active_assets.py`，按主类型、辅类型、我方角色和场景标签取得 `active` 原则卡与模块。
4. 先读原则卡，明确审查目标、判断坐标、默认倾向、推翻因素和法律硬边界。
5. 再核对合同现有条款的机制，而不是只检索相似句子。
6. 形成 review plan 后，选择强保护、平衡或让步变体，并按具体事实重新起草。
7. 检查模块依赖、冲突和跨条款联动，再应用真实 Word 修订。
8. 完成 DOCX 工程门和语义一致性复核。

## 原则卡与条款模块分工

原则卡回答“为什么这样判断、什么会改变判断”；条款模块回答“在给定前提下可以如何落文”。两者不得合并成没有来源、没有前提的固定答案。

条款模块至少包含：

`module_id、version、type_id、clause_group、review_objective、failure_mode、roles、scene_tags、requiredness、assumptions、placeholders、variants、dependencies、conflicts、source_ids、source_family_ids、approval_status、reviewer、reviewed_at、legal_checked_at`。

## 联动组

下列机制默认成组审查：

- 付款、开票、交付与验收；
- 变更、工期、解除与违约；
- 保密、数据处理与知识产权；
- 责任限制、违约金、赔偿与保险；
- 到期、续期、退出、交接与数据迁移。

选择某一模块变体后，必须检查其 `dependencies` 和 `conflicts`。缺失依赖时补入配套机制或降低动作强度；冲突无法消除时升级人工判断。

候选框架中，`module_group_catalog.json` 仅记录联动顺序和一致性检查位；`template_catalog.json` 仅记录可填充的 DOCX 结构骨架。两者均不是已批准的审查内容，选择器不得加载。

股权转让候选试点另有 `trigger_catalog.json` 和 `experience_catalog.json`。两者当前仅供律师校验和回归测试，不得作为正式运行时资产。

## 没有 active 资产时

返回空资产集和 `blocked_need_approval`，继续使用现有原则型审查资料，不得读取候选模块正文、不得声称命中母版、不得自行提升资产状态。
