# ContractOps 生产运行指南

## 1. 交付边界

仓库提供可复现的生产运行清单、启动前强校验、数据库迁移、健康检查、最小权限容器和
发布/回滚步骤。数据库、Redis、S3 兼容对象存储、OTLP Trace 后端、TLS 证书、域名和密钥
由目标环境提供；仓库不会提交或生成生产凭据。

生产清单只部署 ContractOps API、事件 Worker、文档 Worker、履约 Scheduler 和一次性迁移/
预检任务。PostgreSQL、Redis、对象存储和可观测后端应使用具备备份、TLS、监控和故障转移
能力的托管服务或等价基础设施。

每个容器只接收其角色所需凭据：API 获得 RLS 应用数据库、JWT 和对象存储凭据；事件
Worker 获得 Worker 数据库、Redis 和通知凭据；文档 Worker 获得 Worker 数据库和对象存储
凭据；Scheduler 只获得 Worker 数据库；migration 只获得迁移数据库。运行角色由生产清单
设置，不应在外部环境文件中覆盖。

## 2. 生产配置硬门禁

当 `CONTRACTOPS_ENVIRONMENT=production` 时，应用启动前会拒绝：

- 少于 32 字符、低熵或包含开发占位符的 JWT 密钥；
- 默认、过短或低熵的对象存储密钥；
- `localhost`、开发密码或未启用 TLS 的 PostgreSQL；
- 非 `rediss://` 的 Redis；
- 非 HTTPS 的对象存储、OTLP、模型端点和 Webhook；
- 生产 API 使用通配符 `Host` 白名单；
- 配置了 Webhook 但缺少至少 32 字符签名密钥；
- 缺少 OTLP Trace 端点。

仅当基础设施已经通过受控服务网格或等价传输层保护时，才可以显式设置
`CONTRACTOPS_ALLOW_INSECURE_DEPENDENCIES=true`。该开关不会放行开发凭据或 localhost。

## 3. 构建不可变镜像

Dockerfile 固定 Python 和 uv 官方镜像的 SHA-256 摘要；运行、测试与构建依赖均来自
`services/contract-api/uv.lock`。镜像使用 `uv sync --frozen` 和禁用构建隔离的第二次安装，
避免构建后端另行解析未锁定依赖。更新依赖时提交 pyproject 与锁文件并重新通过 CI。

从已经通过 CI 的提交构建并推送镜像，部署时使用 digest，不使用 `latest`：

```bash
docker build --target runtime -t registry.example/contractops-api:2026.09.29 services/contract-api
docker push registry.example/contractops-api:2026.09.29
docker inspect --format='{{index .RepoDigests 0}}' registry.example/contractops-api:2026.09.29
```

将最后一条命令得到的 `registry/repository@sha256:...` 写入生产环境文件的
`CONTRACTOPS_IMAGE`。

## 4. 准备环境

复制示例到仓库外的受限路径并替换全部 `replace-with`：

```bash
install -m 600 deploy/contractops/.env.production.example /etc/contractops/contractops.env
```

数据库使用三个不同身份：迁移管理员、受 RLS 约束的 API 运行角色，以及只拥有事件/
调度/文档处理表权限的跨租户 Worker 角色。三条连接串都必须启用 `sslmode=verify-full`
或组织批准的 TLS 校验模式。

由数据库管理员预先创建两个独立 LOGIN 角色（密码经密钥系统配置）：API 角色必须
`NOSUPERUSER NOCREATEROLE NOBYPASSRLS`，Worker 角色必须
`NOSUPERUSER NOCREATEROLE BYPASSRLS`。迁移角色拥有 public schema 的 CREATE 权限及业务表；
首次初始化前由管理员安装 `pgcrypto` 和 `vector` 扩展。不要把迁移凭据交给 API。
迁移完成后按下面的命令授予实际角色权限；自定义角色名也受支持。

对象存储 Bucket 必须预先创建，并开启版本控制、服务端加密、生命周期规则和拒绝公开访问。
Redis 只承载可重放的 Stream，不是业务事实源；仍应开启认证、TLS、持久化和内存告警。

## 5. 预检、迁移和启动

以下命令中的环境文件不应位于仓库内：

```bash
export CONTRACTOPS_ENV_FILE=/etc/contractops/contractops.env

docker compose --env-file "$CONTRACTOPS_ENV_FILE" \
  -f deploy/contractops/docker-compose.prod.yml config --quiet

docker compose --env-file "$CONTRACTOPS_ENV_FILE" \
  -f deploy/contractops/docker-compose.prod.yml run --rm preflight-migration

docker compose --env-file "$CONTRACTOPS_ENV_FILE" \
  -f deploy/contractops/docker-compose.prod.yml run --rm migrate

# 替换为已由管理员创建的实际角色名。首次部署及升级后均执行。
docker compose --env-file "$CONTRACTOPS_ENV_FILE" \
  -f deploy/contractops/docker-compose.prod.yml run --rm migrate \
  python scripts/provision_production_database.py \
  --app-role contractops_app --worker-role contractops_worker

docker compose --env-file "$CONTRACTOPS_ENV_FILE" \
  -f deploy/contractops/docker-compose.prod.yml run --rm preflight-api

docker compose --env-file "$CONTRACTOPS_ENV_FILE" \
  -f deploy/contractops/docker-compose.prod.yml run --rm preflight-event-worker

docker compose --env-file "$CONTRACTOPS_ENV_FILE" \
  -f deploy/contractops/docker-compose.prod.yml run --rm preflight-ingestion-worker

docker compose --env-file "$CONTRACTOPS_ENV_FILE" \
  -f deploy/contractops/docker-compose.prod.yml run --rm preflight-scheduler

docker compose --env-file "$CONTRACTOPS_ENV_FILE" \
  -f deploy/contractops/docker-compose.prod.yml up -d \
  contract-api contract-worker ingestion-worker obligation-scheduler
```

运行角色的依赖预检会验证 schema 版本、逐表权限、强制 RLS 和数据库角色属性。
迁移预检只验证 DDL 所需权限，允许首次初始化空库；其他预检等待迁移完成。
OTLP 可填写 collector 基础地址或完整 `/v1/traces` 地址，前缀路径会被保留。

文档 Worker 每隔租约时长的三分之一续期。写回时同时检查 Worker 身份、领取次数和
有效租约，过期或被接管的尝试不能写入成功、失败、证据或审计结果。

幂等键保留 24 小时：有效期内继续重放，过期后的同键请求按新请求处理，业务唯一约束
仍然生效。Scheduler 每分钟分批清理最多 1000 条过期记录；清理失败记日志并在下一周期重试。
`GET /v1/approval-tasks` 默认每页 50 条，`limit` 范围 1–200，返回仍是数组；有下一页时
响应头提供 `X-Next-Cursor`，下一次请求将它原样传入 `cursor`。没有该响应头表示已到末页。
排序为 `(created_at, id)`，游标绑定租户和用户，每页重新执行当前权限过滤。

清单默认只把 API 绑定到 `127.0.0.1:8080`，必须通过受信任的 TLS 反向代理或入口网关
暴露。Worker 指标同样默认只绑定到主机回环地址的 9101–9103 端口，供本机监控代理抓取。
仅把代理地址加入 `CONTRACTOPS_FORWARDED_ALLOW_IPS`，不要使用不受限的 `*`。

## 6. 发布验收

发布后依次确认：

1. `/health/live` 返回 200，`/health/ready` 返回 200 且数据库可用；
2. `/metrics` 只能被监控网络访问，Worker/Scheduler 指标持续更新；
3. 使用测试租户完成建档、策略匹配、审批、义务登记和审计查询；
4. 使用另一个租户确认跨租户读取被拒绝；
5. 上传一份测试 DOCX/PDF，确认对象校验、证据块和版本 Diff；
6. 暂停测试通知端点并恢复，确认重试、死信和人工重放；
7. 在目标规格环境执行至少 5 分钟稳态和峰值压测，记录 QPS、P95/P99、失败率、数据库
   CPU/连接/存储、Redis 延迟和单合同资源成本。

CI 的 M7 数字只用于回归基线，不能替代目标环境容量验收。

## 7. 备份与恢复

上线前必须由基础设施负责人提供并演练：

- PostgreSQL 自动备份与时间点恢复，RPO/RTO 有明确数值；
- 对象存储版本控制、跨故障域复制和删除保护；
- 配置与密钥的独立备份及轮换流程；
- 至少一次恢复到隔离环境，并运行迁移状态、租户隔离和完整审批链路验证。

Redis Stream 可以从 PostgreSQL Outbox 重建，恢复时不得清空 Outbox、审计、幂等、投递和
死信记录。数据库恢复后的第一个写流量必须等迁移版本、RLS 策略和 Worker 权限验证完成。

## 8. 回滚

保留上一个已验证镜像 digest。应用回滚只能切换到兼容当前数据库 schema 的版本；需要
数据库 down migration 时，必须先备份并在隔离副本验证。回滚后重新执行五个角色的预检，
再验证就绪、租户隔离、幂等重放和一条完整审批链路。

故障处置、死信恢复和安全事件分级见 [运行手册](CONTRACTOPS-RUNBOOK.md)。
