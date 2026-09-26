# ContractOps：企业合同审批与履约风控后端服务

ContractOps 是一个面向企业法务、财务、采购和业务团队的后端服务项目，聚焦合同
台账、版本控制、可配置审批、履约义务、风险事件、到期提醒和全链路审计。PDF/DOCX
解析与 AI 风险提示属于辅助能力，不是项目的产品边界。

项目由原 EduMind 平台方案收缩而来。OpenMAIC 代码仍保留在仓库中，后续只作为
可选演示客户端；新的核心代码位于 `services/contract-api`。

## 当前骨架

```text
ContractOps/
├─ services/
│  ├─ contract-api/              # 合同、审批、履约与风控 API
│  │  ├─ src/contractops/
│  │  ├─ migrations/
│  │  └─ tests/
│  └─ model-gateway/             # Switchyard，作为可选模型路由层
├─ deploy/contractops/            # 独立本地 Compose
├─ architecture/
│  ├─ CONTRACTOPS-SCOPE.md
│  └─ CONTRACTOPS-IMPLEMENTATION-PLAN.md
├─ app/、components/、lib/        # 保留的 OpenMAIC 前端与运行时
└─ README-CONTRACTOPS.md
```

当前代码已经建立：

- 可注入配置的 FastAPI 应用工厂、健康检查、纯 ASGI 请求上下文和统一错误信封；
- 合同生命周期状态机及非法迁移测试，同时保留早期审查状态机作为历史兼容代码；
- 由 Alembic 管理的 PostgreSQL/pgvector 初始数据模型、升级与回滚脚本；
- 带一次性 `migrate` 服务的开发 Compose，以及隔离的测试 Compose；
- ContractOps API 专用 CI，执行 Ruff、mypy、pytest、空库迁移往返和 Compose 配置检查；
- HS256 JWT 租户上下文、角色与部门数据范围校验，以及稳定的认证/授权错误码；
- Contract、ContractVersion PostgreSQL Repository 与事务级 RLS 上下文；
- 合同创建、查询和不可变版本登记 API，支持租户级幂等键和对象键隔离；
- 多租户隔离、重复请求重放、版本不可覆盖的数据库集成测试；
- 产品边界、API 草案、里程碑、验收标准和持续更新记录。

当前已实现：审批策略版本与发布、受控条件匹配、策略快照、顺序审批、高金额财务加签、
个人待办、领取/审批/驳回/退回修改/转交、乐观并发控制和审批事件 Outbox。

尚未实现：履约调度、风险升级、对象存储直传、通知消费与审批人目录校验。
适配器。文档解析和模型调用安排在核心业务闭环完成之后。项目不会把规划能力表述为
已完成。

## 本地启动

需要 Python 3.12，或使用 Docker Compose。

### 直接启动 API

```bash
cd services/contract-api
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/uvicorn contractops.main:app --reload --port 8080
```

提交代码前可运行统一检查入口：

```bash
cd services/contract-api
.venv/Scripts/python scripts/check.py
```

访问：

- `GET http://localhost:8080/health/live`
- `GET http://localhost:8080/health/ready`
- `GET http://localhost:8080/v1/system/info`
- `POST http://localhost:8080/v1/contracts`
- `GET http://localhost:8080/v1/contracts/{contract_id}`
- `POST http://localhost:8080/v1/contracts/{contract_id}/versions`
- `GET http://localhost:8080/docs`

合同接口需要 HS256 JWT。令牌声明包括 `tenant_id`、`sub`、`roles`、
`department_ids` 和 `data_scope`；两个写接口还需要 `Idempotency-Key` 请求头。开发环境
示例配置见 `services/contract-api/.env.example`，部署前必须替换示例密钥。

### 启动本地依赖

```bash
docker compose -f deploy/contractops/docker-compose.yml up --build
```

Compose 会先运行 Alembic 迁移，迁移成功后才启动 API。完整隔离验证使用
`deploy/contractops/docker-compose.test.yml`。

默认端口：API `8080`、PostgreSQL `55432`、Redis `56379`、MinIO API `59000`、
MinIO Console `59001`。默认密码只用于本地开发。

## 设计文档

- [项目边界](architecture/CONTRACTOPS-SCOPE.md)
- [实施计划与基础架构](architecture/CONTRACTOPS-IMPLEMENTATION-PLAN.md)
- [原 EduMind 方案迁移说明](README-EDUMIND.md)

## 上游说明

- 根目录的 OpenMAIC 代码继续遵循其 MIT License。
- `services/model-gateway` 保留 Switchyard 的 Apache-2.0 License 和 NOTICE。
- ContractOps 新增代码当前沿用根目录 MIT License。
