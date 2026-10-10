# ContractOps 统一工作台与知识库管理 MVP

更新：2026-10-09。用于本地运行和面试展示。已完成 1–4 项知识库基础能力，并扩展 5–8 项问答、合同台账、预审与人工审批，见 [合同预审与审批 MVP](CONTRACTOPS-WORKBENCH-MVP.md)。Inspector 直接接入仍限于管理知识资料；合同工作台引用这些资料，不代表原业务合同 Worker 已接入。OCR 当前未启用，真实模型未配置。

## 1. 已实现的范围

- 主入口与本地启动器共用应用工厂、工作台 HTML、管理/检索服务及 `/v1/knowledge` 接口；不再使用独立的演示上传接口。
- 创建有名称、说明的知识库；上传时选择合同资料、公司制度、标准模板分类。
- 文件名、分类、状态筛选与文档分页；查看真实元数据、失败原因、处理状态。
- “查看内容”提供分块与来源、完整解析正文、原始 PDF、表格与质量四个页签；保留多页来源、标题路径、片段类型和 SHA-256，可展开技术元数据。
- 上传修订版本并切换当前/历史版本；历史原件与正文保留，问答引用可定位固定版本的具体片段，再打开对应 PDF 来源页。
- 配置 Inspector 后，PDF 经主后端提交异步任务，持久化任务关联，保存正文、表格和质量信息；DOCX 保留原解析路径。未配置时 PDF 使用基础解析器。
- 失败或处理租约过期的资料可重试；归档排除检索但保留原件和记录；同库文件按 SHA-256 去重。
- 问答可以选择 `base_id` 限定库，使用管理页的同一数据，不再只是上传后提取的孤立页面。
- 支持文字型 PDF 与 DOCX，复用现有解析器。
- 查看当前文档、解析状态、片段数量和是否可检索。
- 英文词与中文短语/双字片段检索，可限定单份合同。
- 返回文件名、合同/版本/片段 ID、版本号、PDF 页码、DOCX 标题路径、文件和片段 SHA-256。
- 摘录保留原文字符范围 `[excerpt_start, excerpt_end)`；不是重新改写的文本。
- 问答默认组织检索原文，标记 `EXTRACTIVE`；可选配置模型后返回 `RAG`，无匹配或无法支持回答时明确提示依据不足。支持文档范围与当前页面短会话。
- 正式 API 在数据库内先按租户与 OWN / DEPARTMENT / TENANT 数据范围过滤，再排序、截取 top-k。保留原有 RLS，不把权限判断交给模型。
- 正式 API 只检索合同 `current_version_id` 指向的 `READY` 版本；新版本仍在解析时不会悄悄退回旧版本。

## 2. 立即本地运行：不需要 Docker

在仓库根目录打开 PowerShell，使用已有后端虚拟环境：

```powershell
& .\services\contract-api\.venv\Scripts\python.exe .\services\contract-api\scripts\knowledge_demo.py
```

然后打开 <http://127.0.0.1:8081>：

1. 将终端第三行临时令牌粘贴到页面，点击“连接后台”。
2. 新建“采购合同与制度”，填写说明，上传含可复制文字的 PDF 或 DOCX。
3. 点“查看内容”核对分块与完整解析正文；在底部上传修订文件，切换版本确认历史不被覆盖。接入 Inspector 后可打开对应版本的 PDF 原件页。
4. 进入“知识问答”，选择知识库，输入“付款期限”“Payment”等原文关键词。
5. 归档资料，再次检索确认不会返回该资料；上传损坏 DOCX，检查失败记录与重试。

端口占用时可加 `--port 8082`。终端按 Ctrl+C 停止服务。

如果没有虚拟环境，按 `services/contract-api/README.md` 的 Development 步骤安装后端依赖。

### 可选：启用本机 PDF Inspector 联动

需要已有 PDF Inspector 检出目录和能运行其 `backend`、Rust 扩展的 Python 环境；启动器不会自动安装、编译或修改另一个仓库。Inspector 版本必须支持幂等上传响应头 `X-Operation-ID` 和原件页预览接口。本机已用检出版本 `c549637ee4da75e75caedf3700ebec1748e592a0` 实测。

先在仓库根目录的一个终端运行：

```powershell
# 替换为自己的 Inspector 仓库和已配置的运行环境。
& 'F:\pdf inspector\.codex-tools\api-venv\Scripts\python.exe' `
  .\services\contract-api\scripts\inspector_local.py --repo 'F:\pdf inspector' --port 8090
```

再在另一个终端运行主工作台：

```powershell
& .\services\contract-api\.venv\Scripts\python.exe .\services\contract-api\scripts\knowledge_demo.py `
  --inspector-url http://127.0.0.1:8090 --inspector-key-file .\data\inspector-local\service-key.txt
```

Inspector 的原件、任务和服务密钥保存在本仓库被忽略的 `data/inspector-local`，不写回 Inspector 仓库。两个进程均只监听本机。模板默认 `manual_query`，OCR 默认 `none`；不会伪装为已开启 OCR，也不会把扫描件无文本误判为质量通过。可选 `--ocr-provider rapidocr` 只有在 Inspector 环境已安装相应依赖时才能使用，本轮未验证该可选路径。

正式应用可通过 `CONTRACTOPS_INSPECTOR_URL`、`CONTRACTOPS_INSPECTOR_API_KEY`、`CONTRACTOPS_INSPECTOR_PROFILE_ID`、`CONTRACTOPS_INSPECTOR_TIMEOUT_SECONDS` 配置；非本机地址必须使用 HTTPS。密钥仅在后端使用，浏览器不直连 Inspector。

### 本机演示边界

- 仅监听 `127.0.0.1`，不提供公网部署模式。
- SQLite 保存原件、解析文本、元数据、版本、任务映射和引用，默认文件为 `data/knowledge-demo.sqlite3`，已被 Git 忽略。原件读取会重新校验 SHA-256；Inspector PDF 预览由主后端鉴权转发原件页面 PNG，不是将解析文字重新排版。
- 数据目录含原件与正文，不要提交或分享。重启保留文档，默认重新生成令牌；启动前可设置 `CONTRACTOPS_LOCAL_TOKEN` 保持令牌，不要写进源码。
- 令牌只在当前页面使用，不写入浏览器持久存储；修改令牌会清空旧身份已显示的数据。
- 单文件不超过 5 MiB；正文最多 500,000 字符 / 2,000 个块；每知识库最多 100 个版本记录（含历史、归档）。Inspector JSON 和单页 PNG 均限制为 8 MiB。
- 演示提供固定经办人与审批人身份切换；临时令牌仅解锁该演示，不代表企业登录或真实多租户账号管理。
- 同库重复上传返回已有记录（可能是历史或归档记录）；当前版本相同文件修订为重复请求，不创建新版本；与本库其他记录摘要相同的修订返回冲突。尚不支持历史版本回滚、取消归档或文档恢复。管理资料的“合同”只是分类，不自动创建业务合同或审批。
- 旧演示表一次性复制到“旧演示导入”，保留原 ID、片段和旧表；原来未保存的原件不能补回，这类记录不能重试。
- 页面与主应用共用，不是独立后端。SQLite 与 PostgreSQL 是不同物理存储，顶部明确显示模式。原有 OpenMAIC Next.js 前端未改造。
- 本地模式已开放持久化合同台账、预审与人工审批，复用现有应用服务和状态机，不生成假合同或假审批数据。新增详情和操作见 [5–8 项指南](CONTRACTOPS-WORKBENCH-MVP.md)。

**SQLite 演示不用于证明 PostgreSQL RLS。** 数据范围的真实数据库保证需要运行下述 PostgreSQL 集成测试。

## 3. 正式后端的接入点

正式应用仍是 `contractops.main:app`，使用原有 JWT、PostgreSQL、对象存储和业务 Worker。两种模式均提供工作台 `/`，不会挂载 `/demo/documents`。默认主入口不因数据库缺失而回退 SQLite。

### 知识库管理接口

| 接口 | 用途 |
| --- | --- |
| `GET /workspace/session` | 实际模式、身份和功能能力 |
| `GET /v1/knowledge/bases` | 有权访问的库及可见文档数量 |
| `POST /v1/knowledge/bases` | 创建名称、说明，可选可访问的 department_id |
| `GET /v1/knowledge/bases/{id}/documents` | 筛选和分页列表 |
| `POST /v1/knowledge/bases/{id}/documents` | 原始字节上传；查询参数指定文件名、媒体类型和分类 |
| `GET /v1/knowledge/documents/{id}` | 元数据、错误和允许操作 |
| `GET /v1/knowledge/documents/{id}/chunks` | 鉴权后的正文分页 |
| `GET /v1/knowledge/documents/{id}/text` | 完整解析正文，不代表 OCR 未识别部分也已提取 |
| `GET /v1/knowledge/documents/{id}/original` | 经过鉴权与摘要校验的原件 |
| `GET /v1/knowledge/documents/{id}/versions` | 同一文档当前与历史版本 |
| `POST /v1/knowledge/documents/{id}/versions` | 以原始字节上传修订文件，传 expected_version |
| `GET /v1/knowledge/documents/{id}/processing` | 本版本任务状态、阶段进度和错误码 |
| `POST /v1/knowledge/documents/{id}:sync` | 授权管理者同步外部任务并保存已就绪结果 |
| `GET /v1/knowledge/documents/{id}/pages/{page}.png` | 经过鉴权转发本版本 Inspector 原件页面 |
| `POST /v1/knowledge/documents/{id}:retry` | 重试失败或五分钟租约过期的解析 |
| `POST /v1/knowledge/documents/{id}:archive` | 归档并排除检索 |

重试、归档传 `expected_version`，阻止旧页面覆盖新状态。列表、计数、正文读取和排序前，同时检查知识库与资料的租户、本人/部门/租户数据范围。写入需 `CONTRACT_OWNER` 或 `TENANT_ADMIN`；重试、归档还需文档所有者或有访问权限的租户管理员。

管理资料先提交 `PARSING` 记录。基础解析仍在工作线程同步执行；Inspector PDF 在工作线程内提交和查询一次，实际解析在 Inspector 异步执行。页面每五秒同步当前列表中有管理权限的处理中文档，亦可手动同步；关闭页面不会取消外部任务，但 ContractOps 不保证关页后自动写回，重新打开/同步会继续。当前没有新增常驻分布式 Worker。

每个版本先持久化唯一请求键与任务关联，再调用外部服务。提交响应丢失或临时不可用保留 `PARSING / SUBMISSION_UNKNOWN`，继续同步使用同一个键；失败后显式重试才开始新尝试。不会因为超时静默改用基础解析器。幂等能力依赖配套 Inspector 协议，不宣称跨系统“绝对只执行一次”。租约与条件版本更新保护正文，旧轮询不能把完成任务退回处理中。

文档 `READY` 表示解析结果已入库，`parse_details.quality_status=NEEDS_REVIEW` 表示仍需人工核对；扫描页未识别等信息保留在 `document/ocr`。零块结果不可检索。质量 `READY` 也不代表合同条款合法、符合制度或已获得审批。进度 `0/50/100` 是排队/处理/结束的阶段标记，不是实际计算百分比。

当前版本在独立事务中替换，不覆盖历史原件或片段；默认列表和检索只取当前版本。新版本解析未完成、失败或已归档时不回退历史版本。上传修订、原件读取、全文、版本历史、任务同步与页面预览均重新检查资料权限。原件暂存在数据库，不是新增对象存储平台。新增管理写入暂未接入业务审计流水，不宣称具有完整生产审计能力。

原有业务合同入库仍沿用：

1. 创建合同。
2. `POST /v1/contracts/{contract_id}/uploads` 获取上传票据。
3. 上传对象后 `POST /v1/contract-versions/{version_id}:complete-upload`。
4. Worker 校验 SHA-256 并完成解析，版本进入 `READY` 后参与检索。

知识库使用 `knowledge_bases`、`knowledge_documents`、`knowledge_document_chunks`、`knowledge_inspector_jobs` 四个表，合同工作台另有文档关联与预审报告两表；PostgreSQL 需升级到迁移 `20261009_0011` 并按既有部署流程重新执行 runtime grants，API 权限清单已更新。上述表均开启并强制租户 RLS。不把本地身份带入正式 API，不新增主后端第三方依赖。SQLite 启动时增量升级已有管理表，保留原件与旧表。

### 文档列表

`GET /v1/knowledge/documents?limit=20&offset=0`

返回有权访问的管理资料，以及有当前文件版本的业务合同。无文件合同不显示。未就绪资料显示状态但不参与检索；`READY` 且零片段仍不可检索。原有合同证据不复制到知识库表，也不会自动出现在新建库的管理列表。

### 检索

`GET /v1/knowledge/search?query=付款期限&limit=5`

可加 `contract_id` 限定单份合同，或 `base_id` 限定管理知识库。无权访问或不存在的对象返回空结果，不暴露其存在。最多 10 个片段，查询最多 300 字符、32 个匹配项；摘录最多 900 字符。管理资料的引用另有 `document_id`、`base_id`；旧兼容字段 `contract_id` 使用资料 ID，不代表业务合同，客户端应依 `document_id` 区分。

完整词/中文短语权重高于中文双字片段；`score` 是关键词得分，**不是置信度或法律风险概率**。检索为参数化字面匹配，不是向量检索；PostgreSQL 原合同查询超时 3 秒，管理资料查询超时 5 秒。

### 摘录问答

`POST /v1/knowledge/ask`

```json
{
  "question": "什么时候付款",
  "limit": 3
}
```

同样可传 `contract_id` 或 `base_id`，也可用 `document_id` 指定管理资料（不能同时传 `contract_id`）；`history` 最多 6 个历史问题，仅辅助理解追问。返回 `mode`、`status`、`answer`、`citations`、实际 `scope` 与 `fallback_reason`：

- `EXTRACTIVE / EVIDENCE_FOUND`：找到关键词匹配原文，不保证这些片段已完整回答自然语言问题。
- `EXTRACTIVE / INSUFFICIENT_EVIDENCE`：未找到可访问且匹配的原文；不是“合同不存在此条款”的判断。
- `RAG / MODEL_ANSWER`：已配置模型组织带引用回答，逐字引用验证不等于语义正确性保证；`RAG / INSUFFICIENT_EVIDENCE` 表示模型明确表示现有片段不足。未配置或模型异常显示原文降级原因。
- DOCX 页码为 `null`；不虚构排版页码。PDF 页码从 1 开始。
- 每次请求重新检索，无共享答案缓存；响应使用 `Cache-Control: no-store`。已有下载或复制的文本无法因撤权而自动收回。
- 问题可能敏感，优先使用 POST 问答。代理和访问日志也应避免记录完整查询参数。

## 4. 验证方式与限制

在 `services/contract-api` 目录运行：

```powershell
& .\.venv\Scripts\python.exe scripts/check.py
```

该命令执行 Ruff、mypy 和完整 pytest 回归。新增测试覆盖实际 PDF/DOCX 解析入库、重开 SQLite 后的数据读取、英文/中文检索、带引用问答、无匹配、角色校验、租户/部门/本人范围、原文偏移及正式应用不挂载演示接口。

本机 Ruff、mypy 通过；完整 pytest 结果见后续工作台指南的验收记录。测试覆盖知识库管理、正文鉴权分页、作用域隔离、失败重试、归档、去重、过期解析 fencing、旧数据导入，以及原件摘要、版本历史、当前版本检索、提交响应丢失后的同键恢复、多页来源、质量信息、无效证据拒绝和迟到进度防覆盖。PostgreSQL 知识库用例共 6 项，因本机缺少数据库而跳过。

`tests/test_postgres_knowledge.py` 还覆盖真实 PostgreSQL 的作用域过滤、当前版本切换、归档排除和 SQL 参数化。它需要配置现有测试环境变量 `CONTRACTOPS_INTEGRATION_DATABASE_URL`、`CONTRACTOPS_INTEGRATION_ADMIN_DATABASE_URL`，并使用迁移后的专用测试数据库；**未配置时跳过，不能把跳过当成通过**。现有 GitHub Actions 质量检查任务配置了这些变量；本地验收记录不代表 CI 结论，远端结果以对应 PR 的实际检查为准。默认 Compose 检查未配置这两个变量，需要显式接入测试角色才能运行这些用例。

SQLite 和 HTTP 测试不能替代真实 PostgreSQL/Redis 集成。本机无这些服务，尚未验证新增迁移实际升级/回滚与真实 RLS，未做新增检索负载测试。

页面脚本 `scripts/check_knowledge_ui.cjs` 已用独立临时 SQLite 库验收：连接、创建、上传、完整正文、修订版本、历史浏览、当前版本引用定位、筛选、归档排除、失败重试、修改身份清空数据，以及桌面/手机布局，并检查截图。通过 `KNOWLEDGE_UI_PDF_FIXTURE` 指定已有 PDF 后，还用真实本地 Inspector 验证了异步入库、任务状态和原件页面渲染，并将预览与独立 PDF 渲染结果作视觉对照。没有向用户库添加验收资料。

脚本默认 8082，会写入测试数据，只能对可丢弃的本地库运行；需 `CONTRACTOPS_LOCAL_TOKEN`，可用 `PLAYWRIGHT_MODULE`、`KNOWLEDGE_UI_BROWSER` 指向已有环境，`KNOWLEDGE_UI_SCREENSHOT` 保存截图，不自动安装浏览器。响应丢失/恢复等故障用例使用模拟适配器；真实 Inspector 验收不等于完整分布式故障压测。

另已对真实 Inspector 重复提交同一已完成文件及持久化请求键，确认返回原任务 ID，没有创建第二个任务。

## 5. 明确未做的功能

- OCR 的实际启用与扫描件效果验证、Inspector 合同专用提取模板、业务合同 Worker 的 Inspector 接入。合同台账现已复用金额/日期规则候选并保存核对标记，不自动填写业务字段。
- 向量检索、重排序和真实 LLM 效果评测。已实现可选生成客户端与引用校验，当前未配置真实模型。
- 自动审批或自动创建履约义务。规则预审报告与人工审批已在本地闭环。
- DOCX 排版预览、知识库编辑删除、历史版本回滚、取消归档、多用户登录、常驻分布式任务调度。未配置 Inspector 的 PDF 依赖浏览器原生查看器，兼容性不作保证。

后续优先补真实模型效果验收和 PostgreSQL 迁移/RLS 验证，再考虑向量检索或扩大部署范围；当前面试演示路径见 [5–8 项指南](CONTRACTOPS-WORKBENCH-MVP.md)。
