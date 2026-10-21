# Windows 离线运行包

当前提供可重复执行的离线组装脚本，不随仓库分发解释器或未知软件，也不自动下载。构建结果明确标为 `assembled_unverified`。本轮验证环境为 macOS，尚未完成原生 Windows 验收，不能把生成目录称为已经通过生产验收的 Windows 发行版。

## 准备输入

由部署负责人通过可信渠道准备并核验：

1. 一个明确补丁版本的 CPython Windows x64 嵌入式 ZIP，文件名为 `python-3.<minor>.<patch>-embed-amd64.zip`，以及可信来源提供的 SHA256。产品范围为 Python 3.10–3.13；这不代表每个补丁版本都已验收。
2. 与该 Python/Windows x64 匹配的完整离线 wheelhouse。直接依赖以 `scripts/requirements.txt` 为准，包括 `defusedxml`、`lxml`、`python-docx`、`jsonschema`、`referencing`，也须提供其全部适用的传递依赖。每个包只放一个版本；不接受源码包、URL 依赖或其他平台的 wheel。
3. 构建机已有 Python 与 `packaging`，或已有 pip 附带的 `packaging`。构建器不安装它们，不运行输入 ZIP 中的解释器。

嵌入式 Python 用于应用随附运行环境，与常规 Python 安装不同；本方案直接随应用提供已验证元数据的 wheel 内容，不在目标机向嵌入式环境安装 pip。[CPython 官方说明](https://docs.python.org/3.12/using/windows.html#the-embeddable-package)及 [wheel 格式规范](https://packaging.python.org/en/latest/specifications/binary-distribution-format/)是此组装方式的依据。

## 离线构建

在产品目录执行，下列版本仅为命令格式示例，不是要求使用该旧补丁：

```powershell
python scripts/build_windows_studio.py `
  --python-zip C:\Offline\python-3.12.10-embed-amd64.zip `
  --python-version 3.12.10 `
  --python-sha256 <由可信来源核对的64位SHA256> `
  --wheelhouse C:\Offline\wheelhouse `
  --output C:\Build\OneContract-Studio
```

输出必须是不存在、位于源产品目录以外的新目录。构建器核对解释器声明版本、x64 PE 架构及 ZIP 哈希；核对 wheel 的平台标签、Python 约束、包名/版本、RECORD 文件校验、适用 Windows 条件与 extras 的传递依赖。冲突、缺包、链接、路径越界、同名覆盖会失败并清理未完成输出。仅支持普通 wheel 根目录安装布局；特殊 `.data` 安装方案明确拒绝，不偷偷遗漏脚本或数据。

输出包含 `runtime/`、`one-contract/`、`Start-Studio.cmd`、`verify_windows.py` 和 `windows-kit-manifest.json`。清单记录解释器 ZIP、每个依赖 wheel 和每个产物文件的 SHA256。清单用于完整性复核，并非软件来源的数字签名。产品文件与外置客户数据分离，构建器不打包客户档案、合同任务、凭证或工作目录中的审查日志。

## 在 Windows 上检查与启动

将整个输出目录迁移到 Windows 用户可读写的位置，在未接入真实客户数据之前运行：

```powershell
.\runtime\python.exe .\verify_windows.py
.\Start-Studio.cmd diagnose
.\Start-Studio.cmd start -DataDir C:\Users\User\OneContractData
.\Start-Studio.cmd status
.\Start-Studio.cmd stop
```

原生检查复核实际解释器的完整版本、Windows x64 环境、文件哈希、依赖导入和公共规则载入，并生成 `windows-environment-check.json`。即使检查通过，`production_accepted` 仍为 `false`；这只是环境检查。启动包装器每次先执行该检查，再使用固定随包解释器调用现有启动器；不会落回另一套系统 Python。

合同逐页渲染还需要由部署负责人准备 LibreOffice、Poppler `pdfinfo` 和 `pdftoppm`，以及合同所用中文字体；它们不包含在本构建器中。工具发现优先使用当前 PATH 中的 `soffice`（或 `libreoffice`）、`pdfinfo`、`pdftoppm`。Windows 未从 PATH 找到 LibreOffice 时，会检查 Program Files 和本地用户程序目录下明确的 LibreOffice 安装位置，优先使用 `program/soffice.com`；不会遍历磁盘、自动下载或改写系统 PATH。自定义安装目录及 Poppler 仍须由部署负责人配置到启动工作台的 PATH。

“合同任务”和“工作区与备份”显示已找到的工具、具体路径和缺项，允许重新检查。命令行诊断的 `ready` 仅表示工作台启动条件，`ready_scope` 为 `studio_startup`；独立的 `rendering` 项说明工具发现结果。工具齐备不证明字体、动态库、实际转换或版式通过。缺工具时任务在文档生成前明确阻断，原件和修改安排保留，不产生新的执行批次；补齐后按当前任务版本重试。规则浏览和维护仍可使用。

## 原生验收尚待完成

须在隔离 Windows 数据目录中实际完成：中文/空格路径、依赖导入、首次启动及停止/重启、浏览器本机入口、只读模式、私有 ACL、客户发布与恢复、备份还原、客户任务固定规则、实际修订和渲染，以及审阅件/接受版/拒绝版/报告的逐页人工确认。既有客户数据只能通过备份与新目录恢复迁移，不覆盖旧目录；公共资产指纹冲突仍须明确处理。

本轮已用明确标记为合成数据的 PE/ZIP/wheel 测试组装清单、Windows 条件依赖、extras、版本冲突、缺失依赖、错误哈希、越界路径、覆盖保护和非 Windows 原生检查拒绝。另已从 Python 官方取得 CPython 3.13.16 Windows x64 嵌入式包，并按官方 SHA256 核对；从 PyPI 取得匹配的 9 项依赖，校验 wheel 的平台、版本、完整依赖闭包及文件内容。真实输入和组装证据保存在包外 QA 目录。该工作不等于原生执行验证，尚未在本机执行 Windows 二进制，也未形成生产验收结论。
