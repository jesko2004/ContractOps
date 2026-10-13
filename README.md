# ContractOps

面向企业合同审批与履约管理的后端工程项目，持续扩展文档知识库与证据驱动的 AI 辅助能力。

由 [jesko2004](https://github.com/jesko2004) 开发 ContractOps 业务模块，用于本地演示、面试展示与后端工程实践。本仓库保留部分开源项目代码，来源与职责见文末，不将上游成果计为个人原创。

[项目详细介绍](architecture/CONTRACTOPS-PROJECT-INTRODUCTION.md) · [API 文档](services/contract-api/README.md) · [演示流程](architecture/CONTRACTOPS-DEMO.md) · [运行手册](architecture/CONTRACTOPS-RUNBOOK.md)

## 项目解决什么问题

ContractOps 不只是上传合同文件，而是把合同建档、版本管理、条件审批、履约提醒与审计追溯串成一条可恢复、可验证的业务流程。

- 不同类型、金额和部门的合同，如何匹配不同审批步骤？
- 多人操作同一审批任务，如何防止重复处理和陈旧请求覆盖？
- 合同获批后，如何跟踪付款、交付、验收、续约与终止通知？
- 后台进程或通知服务故障后，如何恢复已提交但尚未完成的工作？
- 多租户共用服务时，如何控制数据范围并留下审计证据？

业务主线：

```text
合同建档 → 文件上传与版本登记 → 解析与辅助检查
         → 条件审批与人工决定 → 合同激活
         → 履约跟踪、到期提醒与风险处理 → 审计追溯
```

## 版本状态

> 本 README 更新于 2026-10-13。本次仅更新首页文档，没有把本地开发中的代码一并发布。

| 范围 | 状态与边界 |
| --- | --- |
| GitHub `main` | 合同、审批、履约、风险、文档入库、权限及异步处理等核心后端代码；提供数据库迁移、部署模板和测试流程 |
| 本地开发扩展 | 统一工作台、知识库管理、知识问答、PDF Inspector 解析联动、人工复核等；尚未同步至本次核对的 `main` |
| 当前待修复 | 本地问答存在项目经历汇总超时、检索上下文混杂及模型引用校验失败；不宣称完整 RAG 链路已验收 |
| 交付定位 | 个人工程实践与本地面试展示；不是已服务真实企业的商业 SaaS，也不把部署模板等同于生产上线验收 |

## 主分支核心能力

### 合同台账与不可变版本

登记合同编号、类型、交易方、金额、币种、部门和有效期。合同文件通过对象存储上传，校验大小与 SHA-256；修改文件新增版本，不覆盖历史证据。

### 条件审批与并发控制

按合同类型、部门、金额及币种匹配审批策略，支持任务领取、转交、批准、驳回与要求修改。审批实例保留策略快照；幂等键处理重复请求，状态和版本校验拒绝陈旧操作。

这是条件驱动的审批流程，不宣称已提供通用拖拽式流程设计器。

### 文档解析与辅助预审

后台解析 PDF / DOCX，保留页码或标题路径等证据定位信息，提供金额、日期等规则检查及版本差异。模型辅助默认关闭；开启后也只产生待人工确认的候选建议，不自动批准合同或生成履约决定。

### 履约义务与风险事件

管理付款、交付、验收、续约和终止通知等事项，记录负责人、到期时间及完成证据。调度器生成到期提醒与风险，支持确认、延期和解决；合同终止时取消关联有效任务。

### 多租户权限与审计

JWT 携带租户、角色、部门与数据范围；应用层权限检查配合 PostgreSQL 行级安全策略。操作记录关联请求与追踪标识，日志避免直接写入令牌、密钥或合同正文。

### 可靠异步处理

业务变更与 Outbox 事件同事务落库，通过 Redis Streams 投递。后台任务具备租约、接管、幂等消费、失败重试与死信重放能力；外部通知仍需接收方按幂等键去重，不承诺跨网络绝对“仅执行一次”。

## 知识库与 PDF Inspector 联动方向

本地扩展的工作台分为知识库管理、知识问答、合同管理和审批中心，目标是让审核人先看到资料与证据，再作业务决定。

- 知识库管理：上传、分类、筛选、查看正文与来源、修订版本和归档。
- 知识问答：在授权资料范围内检索，展示回答与可展开的参考原文；模型失败时明确降级。
- 合同管理：关联知识资料、查看版本差异与预审建议。
- 审批中心：人工处理审批任务；文档解析复核与合同批准是两个独立动作。

**当前接入边界：**本地 ContractOps 调用 PDF Inspector 的异步解析任务，后续入库、检索和模型回答仍使用 ContractOps 自身实现。使用相同 Ollama 模型或同为 BM25 + 向量检索，并不意味着已复用 PDF Inspector 桌面版完整知识库 / RAG 链路。

下一阶段重点是对齐解析、分块、索引、上下文组织和引用校验，并用字节级相同 PDF、相同问题及权限范围进行回归。不得绕过租户隔离，也不能把人工确认解析可用当成问答正确性验收。

以上为本地开发进展与后续方向，不是从当前 `main` 克隆即可使用的功能承诺。

## 技术栈与代码结构

| 技术 | 用途 |
| --- | --- |
| Python / FastAPI / Pydantic | API、数据校验与业务用例 |
| PostgreSQL / SQLAlchemy / Alembic | 事务、持久化、租户隔离与数据库迁移 |
| Redis Streams | 事件投递、消费者组和故障恢复 |
| MinIO / S3 兼容存储 | 原始文件与临时上传票据 |
| OpenTelemetry / Prometheus | 请求追踪、指标与运行诊断 |
| pytest / Ruff / mypy / Locust | 回归测试、静态检查和性能基线 |
| Docker Compose / GitHub Actions | 依赖编排、持续集成和交付验证 |

```text
services/contract-api/   核心业务 API、Worker、Scheduler、迁移与测试
services/model-gateway/  保留的 Switchyard，可选模型路由组件
deploy/contractops/      开发、测试与生产部署模板
architecture/           业务范围、设计、演示和运行文档
app/ components/ lib/   保留的 OpenMAIC 前端及相关代码，非合同工作台
```

API、事件 Worker、文档 Worker 与履约 Scheduler 是同一项目中的职责拆分，不意味着已经搭建复杂的微服务平台。

## 启动主分支后端

需要 Git 和可运行 Docker Compose 的容器环境。从仓库根目录执行：

```powershell
git clone https://github.com/jesko2004/ContractOps.git
cd ContractOps
docker compose -f deploy/contractops/docker-compose.yml up --build -d
```

- API 交互文档：<http://127.0.0.1:8080/docs>
- 存活检查：<http://127.0.0.1:8080/health/live>
- 业务请求需要符合服务配置的 JWT；接口文档不等于已登录业务系统。

开发 Compose 使用本地开发凭据，不要直接暴露公网。生产部署需另行配置密钥、TLS、外部依赖与备份恢复，见[生产部署说明](architecture/CONTRACTOPS-PRODUCTION.md)。

本地开发工作台使用 `http://127.0.0.1:8081/`，但仅适用于包含相关启动器的开发代码。它不是公共演示站点，也不是当前 `main` 的默认入口；仓库根目录的 `pnpm dev` 启动的是保留的 OpenMAIC 应用。

## 验证与工程重点

从仓库根目录运行隔离后端检查：

```powershell
docker compose -f deploy/contractops/docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from checks checks
```

重点覆盖状态机、幂等与并发冲突、租户隔离、Worker 租约、通知恢复、文档输入安全及数据库迁移。测试和性能结果以对应提交的实际运行产物为准，不把某次通过数当作当前全部能力或生产容量的证明。

本项目希望展示的是业务建模、一致性、安全边界与异常恢复能力，而不是仅增加接口数量或让模型返回一段文字。

## 文档导航

- [业务后端与架构概览](README-CONTRACTOPS.md)
- [个人项目介绍与开发工作](architecture/CONTRACTOPS-PROJECT-INTRODUCTION.md)
- [API 开发说明](services/contract-api/README.md)
- [业务范围](architecture/CONTRACTOPS-SCOPE.md)
- [演示流程](architecture/CONTRACTOPS-DEMO.md)
- [故障处置手册](architecture/CONTRACTOPS-RUNBOOK.md)
- [生产部署要求](architecture/CONTRACTOPS-PRODUCTION.md)

部分历史文档记录的是当时版本的范围和测试结果，不能据此推断本地扩展已发布或当前模型问答已验收。

## 来源与许可

ContractOps 业务后端与集成工作由仓库维护者开发；仓库保留的 [OpenMAIC](https://github.com/THU-MAIC/OpenMAIC) 是上游交互式课堂项目，其功能、论文、社区与统计数据不属于 ContractOps 的个人成果。原课堂项目中文说明保留在 [README-zh.md](README-zh.md)。Switchyard 位于 `services/model-gateway`，作为可选模型路由组件，不是自动作出合同审批决定的业务模型。

许可见 [LICENSE](LICENSE)。保留上游版权及各组件原有许可声明；项目仅提供工程演示与辅助信息，不构成法律或合规意见。
