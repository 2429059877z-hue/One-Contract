# One-Contract

面向律师、法务和合同高频处理团队的原则型合同审查与起草 Skill。它从交易目的、权利义务、失败模式、证据状态和条款联动出发，优先把能负责任落文的方案写成真实修订，并以批注承载事实、授权和重大选择。

> 目标不是堆叠规则命中或风险报告，而是交付可接受、可拒绝、可回滚、可验证的合同修改。

> 当前预览版本：**3.3-preview**（2026-10-21）。整合完整规则图谱、AI 整理与 Studio 核对、客户规则全文适用性判断，以及真实修订、批注和交付检查。完整变更与升级要求见 [CHANGELOG.md](CHANGELOG.md)。

> 本预览版包含完整规则导航与编辑、填写进度、真实场景测试、合同任务与交付验收。Windows 适配已提供代码及离线组装工具，原生 Windows、Word/WPS 与最终生产验收仍待完成；发布预览版不等于这些验收已经通过。

## 适合谁用

- 需要高频审查中文商业合同的律师、法务和律所团队
- 希望统一审查口径、风险分级和 Word 交付格式的团队
- 需要从零起草合同，并先固定交易文件包、程序前提和待补事实的人

## 典型场景

```text
用户：请站在乙方立场，以常规口径审查这份设计委托合同，重点关注验收、付款和违约责任。
AI：我会先确认审查立场和口径，再按交易结构、合同形式、条款语言三层扫描。
    交付物包括审查报告，以及带修订和批注的 Word 文件。
```

## 它能产出什么

- 风险清单：风险等级、后果、判别标准、相关条款、整改建议
- 推荐措辞：可直接谈判或落入合同正文的条款文本
- 审查报告：可签结论、先决事项、谈判优先级和复核要点
- Word 修订批注版：在 `.docx` 中直接显示修改痕迹和批注
- 起草工作台：主合同类型、配套协议、必带附件、待补事实和条款骨架

## 当前覆盖范围

当前开发目录共 **216 类**，其中 **92 类可进入正式类型路由**。第二层共 10 张大类原则卡：EC-01 至 EC-09 已启用，EC-10 为知识缺口卡并保持候选。类型层共 109 条“原则—合同类型”映射（75 个可复用原则 ID，其中 95 条映射已启用）和 371 个条款模块（311 个已启用）。存在内容缺口的议题须如实披露，不能因为状态已启用就表述为具有完整检查项。全部 20 份 DOCX 骨架仍为独立候选资产，不会因规则激活而自动获得母版批准。数量会随维护变化，以 Studio 实际目录和资产状态为准。

其他类型仍可使用通用原则和已启用的大类原则卡进行审查，但不声称已具备成熟的专项原则卡或母版。旧版 12 类、64 份合同类型资料已退出运行时路由，仅在项目内作为迁移来源归档，不进入对外 Skill 包。

> 发布说明：为满足当前可用版本的交付安排，产品负责人决定九张大类卡与 17 个本轮修改模块继续保持启用；其律师复核与更新后审批证据尚待补齐。系统不会把这一发布决定表述为已经完成律师逐条复核，对外结果仍须律师终审。

支持：

- 甲方、乙方、中立等审查立场
- 克制、常规、强势等审查口径
- 仅批注、直接修订、批注与修订结合等落痕策略
- DOCX 原生批注、修订和 Word 审查意见书输出

## 安装方式

1. 打开 [One-Contract 的 3.3-preview 发布页](https://github.com/2429059877z-hue/One-Contract/releases/tag/v3.3-preview)。
2. 下载 [Skill 安装包](https://github.com/2429059877z-hue/One-Contract/releases/download/v3.3-preview/one-contract-3.3-preview.zip)。Windows 工作台用户可同时下载 [Windows x64 运行包](https://github.com/2429059877z-hue/One-Contract/releases/download/v3.3-preview/one-contract-studio-3.3-preview-windows-x64.zip)。
3. 解压后将 `one-contract/` 文件夹放入你的 skill 目录。
4. 在支持 `SKILL.md` 的 Agent / Claude 环境中启用该 skill。
5. 如需生成 Word 批注、修订或报告，安装脚本依赖：

```bash
pip install -r scripts/requirements.txt
```

首次生成审查文件时，按 Agent 提示确认审查人姓名；机构可留空；配置会写入本地 `config/`，不会作为公开发布内容使用。

### 本地合同规则工作台

本开发树中的中文规则工作台提供“规则总览、试运行、客户规则、公共规则建议、合同任务、工作区”六个页面。规则总览包含全部 216 个类型，未启用与尚无专项内容的类型也可定位；共享模块可以从多个位置进入。日常操作无需手写内部编号。完整操作流程见 [Studio 操作说明](references/studio-user-guide.md)。

```bash
./start.sh --data-dir "/请改为/存放客户规则的独立目录" --idle-timeout-seconds 3600
```

启动后会自动打开浏览器。客户规则数据必须存放在 Skill 目录之外；草稿不会自动生效，须按“送交复核 → 律师确认 → 测试通过 → 正式启用”完成。当明确选择某客户后，客户规则版本缺失、损坏或不兼容时，系统会停止处理，不会静默退回公共规则。

当前支持范围以 macOS 技术验证为基础；Windows/Linux、律师 UAT 签署和独立多轮稳定性验收尚未完成。生成文档的视觉终版仍需实际渲染与逐页复核。

Windows x64 运行包已包含固定 Python 与脚本依赖，完整解压后双击包根目录的 `Start-Studio.cmd`。仅下载 Skill 源码的用户可使用 `start.cmd`，并按 [Windows 组装及验收说明](references/studio-windows.md) 准备依赖。LibreOffice、Poppler 和合同所用字体另行安装；组装成功不等于生产验收通过。

## 可以怎么用

- “站在甲方立场，以强势口径审查这份买卖合同”
- “帮我把服务合同里的验收、付款和违约责任条款补齐”
- “请输出审查报告，并把确定性问题直接修订进 Word”
- “我要起草一份技术开发合同，请先列起草信息清单和待确认问题”

## 使用边界

这个 skill 适合：

- 中文商业合同的常规审查、起草和交付格式统一
- 批量合同的一轮风险扫描和修订底稿生成
- 律师或法务复核前的结构化整理

这个 skill 不适合：

- 替代律师对重大交易作最终签署意见
- 处理未提供事实背景、交易安排或业务目标的合同
- 覆盖所有涉外法域、外文合同和高监管行业专项审查
- 自动完成需要线下审批、登记、备案或主体授权核验的事项

## 核心设计

### 分层四步审查

先识别交易目标和可能失效方式，再核对事实与证据状态，形成专业默认和推翻因素，最后选择直接修订、修订加批注、仅批注或不输出。示例和案例用于校准判断，不作为机械 if-then 规则。

企业合同知识按全局规则、大类原则卡、具体类型原则卡与条款模块三层继承。具体类型可在明确范围内深化大类默认，但不能突破全局硬边界；候选大类卡只返回影子元数据，不返回正文或自动激活其索引模块。

### 最终文件质量门

DOCX 执行后必须重新打开最终包，检查全部 XML/RELS、编码、关系、评论锚点和修订属性，并生成接受版与拒绝版。执行器返回成功不等于完成；任一 finding、结构门、语义视图或渲染门失败都应阻断正式交付并保留失败证据。

### 分析与文档操作分工

Agent 负责识别风险、形成审查计划和推荐措辞；脚本负责在 DOCX 中稳定写入批注、修订和报告，减少复制粘贴导致的格式错误。

### 记忆但不静默代填

本地配置可记住审查人、客户和审查口径，但新环境或缺少关键身份信息时仍会要求确认，避免把历史配置误用到正式交付。

## 关键文件

- [SKILL.md](./SKILL.md)：执行入口和合同审查规则
- [references/review-doctrine.md](./references/review-doctrine.md)：原则型审查方法
- [references/enterprise-taxonomy-v2.md](./references/enterprise-taxonomy-v2.md)：v2 合同分类与稳定编码
- [references/modular-knowledge-routing.md](./references/modular-knowledge-routing.md)：正式原则卡与条款模块路由
- [references/domain-layer-status.md](./references/domain-layer-status.md)：第二层十大类逐类覆盖、成熟度、核验缺口与批准状态
- [assets/knowledge_v2/domain_principle_catalog.json](./assets/knowledge_v2/domain_principle_catalog.json)：十大类第二层原则卡目录
- [references/common-clause-doctrine.md](./references/common-clause-doctrine.md)：跨类型通用条款原则
- [references/redline-comment-policy.md](./references/redline-comment-policy.md)：证据状态、批注与修订策略
- [references/ooxml-quality-gates.md](./references/ooxml-quality-gates.md)：最终 DOCX 质量门
- [references/employment-termination-doctrine.md](./references/employment-termination-doctrine.md)：协商解除劳动合同专项原则
- [scripts/README.md](./scripts/README.md)：DOCX 自动化脚本说明

## 许可证

本作品采用 [CC BY-NC 4.0](./LICENSE.txt) 许可证。商用授权联系方式以 [LICENSE.txt](./LICENSE.txt) 为准。

## 关于作者 / 咨询与交流

马也

如需就合同审查、合同起草、企业内部合同流程落地、复杂法律问题或商用授权进一步沟通，欢迎联系。
