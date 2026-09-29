# ContractOps 运行与故障处置手册

## 1. 使用范围

本手册覆盖 ContractOps API、PostgreSQL、Redis、MinIO、事件 Worker、文档 Worker、履约
Scheduler 和通知适配器。所有处置必须保留 `request_id`、`trace_id`、事件 ID 和操作时间，
禁止把合同正文、JWT、Webhook 密钥或对象存储密钥复制到工单和聊天记录。

## 2. 发布前门禁

1. `python scripts/check.py --with-migrations` 全部通过。
2. GitHub `M7 production-readiness evidence` 通过：QPS ≥ 10、P95 ≤ 1,000 ms、失败率 ≤ 1%。
3. `docker compose -f deploy/contractops/docker-compose.prod.yml config --quiet` 通过。
4. API、事件 Worker、文档 Worker、Scheduler 和 migration 五个角色的
   `contractops-preflight --check-dependencies` 全部通过。
5. 生产环境已替换 JWT、数据库、对象存储、Grafana 和 Webhook 密钥。
6. Webhook 使用公网 HTTPS 域名；如确需专网地址，只在受控网络显式开启
   `CONTRACTOPS_NOTIFICATION_ALLOW_PRIVATE_NETWORKS=true` 并配置出口白名单。
7. 数据库备份、迁移回滚窗口、值班人和业务回退负责人已经确认。

## 3. 健康检查和观测入口

| 目标 | 检查 | 正常信号 |
| --- | --- | --- |
| API 存活 | `GET /health/live` | HTTP 200 |
| API 就绪 | `GET /health/ready` | HTTP 200，数据库可用 |
| 指标 | `GET /metrics` | Prometheus 文本可读取 |
| Worker | 9101 `/metrics` | publish/delivery 成功计数增长 |
| Scheduler | 9102 `/metrics` | processed 计数增长，failed 不持续增加 |
| 文档 Worker | 9103 `/metrics` | ingestion 成功或可解释失败 |
| 运行失败 | `GET /v1/admin/runtime/failures` | 无持续新增同类失败 |
| 死信 | `GET /v1/event-dead-letters` | 无未处理死信 |

先按 `request_id` 或 `trace_id` 查询审计事件，再关联 Outbox、Worker 和通知结果。日志只
用于定位组件和资源标识，不应成为合同正文的副本。

## 4. 故障演练

### 4.1 Worker 崩溃恢复

1. 创建一条会产生通知的审批或履约事件，记录事件 ID。
2. `docker compose -f deploy/contractops/docker-compose.yml kill contract-worker`。
3. 确认 Outbox 或 Redis pending 中仍保留任务，没有写入成功通知结果。
4. `docker compose -f deploy/contractops/docker-compose.yml up -d contract-worker`。
5. 等待租约或 pending idle 超时，确认新 Worker 领取任务。
6. 验证同一 `(tenant, event, channel, destination)` 只有一条成功发送记录。

验收：任务最终完成、恢复率 100%、重复通知率 0%。若未恢复，先查租约时间和 Worker
数据库角色，再人工重放死信；不要直接改业务表状态。

### 4.2 Redis 暂停与恢复

1. `docker compose -f deploy/contractops/docker-compose.yml pause redis`。
2. 触发业务事件，确认 API 事务成功且 Outbox 保留为待发布或重试状态。
3. `docker compose -f deploy/contractops/docker-compose.yml unpause redis`。
4. 确认 Publisher 重试成功，事件进入 Stream 并被确认。

验收：业务事务不回滚、不丢事件、不提前标记已发布。若 Redis 长时间不可用，限制非必要
写流量并扩展 Outbox 告警窗口，不要删除积压记录。

### 4.3 通知端点失败

1. 将测试 Webhook 指向返回 503 的受控公网端点。
2. 触发通知并观察指数退避、失败计数和死信。
3. 恢复端点，通过死信 API 发起人工重放。
4. 使用稳定的 `Idempotency-Key` 在接收方确认没有重复副作用。

验收：达到重试上限后进入死信，恢复后可审计重放。Webhook 302/307 跳转和私网地址应
被拒绝，避免 SSRF 绕过。

### 4.4 Scheduler 租约恢复

在 Scheduler 领取到期义务后立即停止进程，等待 `lease_until` 过期，再启动另一个
Scheduler。确认义务重新被领取，`obligation_reminders` 唯一键阻止重复提醒，终止合同
不再产生后续任务。

## 5. 常见事故分级

| 级别 | 示例 | 首要动作 |
| --- | --- | --- |
| P1 | 跨租户读取、审计可篡改、密钥或合同正文泄漏 | 立即隔离入口、保留证据、轮换密钥并通知安全负责人 |
| P2 | 审批决定丢失、大量任务不恢复、重复外部通知 | 停止相关消费者、保护 Outbox、评估人工重放 |
| P3 | 单租户通知失败、文档解析积压、P95 持续超阈值 | 限流或降级非核心能力，扩容并排查依赖 |

模型辅助始终可以关闭；关闭后合同建档、审批、履约和审计主链路必须保持可用。

## 6. 回滚原则

- 应用先回滚到兼容当前数据库结构的前一镜像。
- 迁移只使用已经验证的 down 路径；含业务数据的破坏性回滚必须先备份并单独审批。
- Outbox、审计、通知投递和幂等记录不得清空。
- 回滚后运行存活、就绪、租户隔离、幂等重放和一条完整审批链路冒烟测试。
- 记录开始/结束时间、影响租户、积压数量、恢复数量和重复副作用数量。

## 7. 压测复现

CI 运行 20 个并发用户、每秒 5 个用户启动、持续 30 秒，覆盖合同创建/读取、审批待办和
审计查询。原始 CSV、HTML、API 日志、JSON 和 Markdown 报告位于工作流产物
`contractops-m7-evidence`。生产发布前应在同规格预发布环境重复 5 分钟以上，并分别记录
QPS、P50、P95、P99、失败率、数据库 CPU/连接/存储、Redis 延迟和单合同资源成本。
