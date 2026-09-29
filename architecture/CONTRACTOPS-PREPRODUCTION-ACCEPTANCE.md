# ContractOps 预生产业务验收

此文件是执行清单，不是通过报告。CI 的隔离数据库测试、发布脚本单元测试和公共健康
检查不能代替目标环境的业务验收。每次发布保存提交 SHA、镜像 digest、Compose 项目名、
测试时间、请求 ID、资源 ID 和结果；不要保存 JWT、签名上传 URL、连接串或合同正文。

## 前置条件

- 已按生产运行指南完成部署；API、两个 Worker、Scheduler 均运行。
- 已配置 PostgreSQL、Redis、对象存储 Bucket、OTLP 和真实 HTTPS 入口；获得监控访问权。
- 使用两个专用测试租户，不使用真实客户数据。租户 A 准备管理员、合同经办人、两名法务
  和审计员；租户 B 准备能访问自己数据的经办人。令牌必须有效，具有对应角色和数据范围。
- 每个写请求使用新的 `Idempotency-Key`（至少 8 字符），每条场景使用可辨识的
  `X-Request-ID`；幂等重放场景刻意复用原键和原请求。
- 故障演练只操作隔离环境的 Redis/测试通知接收端，不能暂停共享生产依赖。

## 执行顺序与通过标准

以下路径统一以 `/v1` 开头，健康检查除外。记录实际结果，不得将“待执行”填为通过。

| 场景 | 操作 | 通过标准 |
| --- | --- | --- |
| 入口 | GET `/health/live` 和 `/health/ready` | HTTPS 证书有效，无跳转，分别返回 `ok`、`ready` |
| 建档/重放 | POST `/contracts`，再用相同键和正文重发 | 两次资源 ID 相同，第二次 `X-Idempotent-Replay=true` |
| 上传 | POST `/contracts/{id}/uploads`，向返回的 URL PUT 文件，再 POST `/contract-versions/{id}:complete-upload` | 文件字节数与 SHA-256 一致，完成请求 202；重复完成不新增任务 |
| 解析/证据 | 轮询 GET `/contract-versions/{id}/chunks`、`/findings` | 限时内得到非空证据块，金额/日期来自测试原文；不能只凭 202 判定解析成功 |
| 版本差异 | 上传修改金额的第二版本，GET `/contracts/{id}/version-diff?from_version_id=...&to_version_id=...` | 差异定位到修改的证据；分别覆盖有效 DOCX 和 PDF |
| 策略 | 管理员 POST `/approval-policies` 后 POST `/approval-policies/{id}:publish` | 测试策略只匹配本次唯一 contract_type，避免覆盖其他合同 |
| 审批/转交 | POST `/contracts/{id}:submit`；法务一 claim→transfer；法务二 claim→decide | 每次使用响应中的 state_version；转交后原审批人不能操作，最终合同 APPROVED |
| 并发冲突 | 旧 expected_version 配合新的幂等键再次 decide | 返回 409，审批事实不被覆盖；不要用原幂等键掩盖版本冲突 |
| 激活/履约 | POST `/contracts/{id}:activate`；POST `/contracts/{id}/obligations` | 使用最新合同版本激活；专用义务设置到期时间为过去、宽限期至少 60 秒 |
| 提醒/风险 | 等待 Scheduler，GET `/risk-events`，检查测试通知接收端 | 出现对应 obligation_id 的风险与提醒；接收端按投递幂等键去重 |
| 履约完成 | POST `/obligations/{id}:complete`，附最新 expected_version 和测试证据引用 | 状态 COMPLETED；结合审计与后续调度周期确认不再重复调度该义务 |
| 审计关联 | 审计员 GET `/audit-events?request_id=...`，在 OTLP 后端查 trace_id | 能关联本次关键业务操作及异步链路；证据不含正文/令牌/密钥 |
| 租户隔离 | 租户 B 查询 A 的合同、chunks/findings、审批实例，并尝试审批操作 | 拒绝访问，不泄露资源内容；同时正向验证 B 自己的数据可读 |
| Redis 故障 | 仅在隔离环境暂停 Redis，执行业务操作，然后恢复 Redis | 业务提交不丢失，Outbox 待重试，恢复后消费完成；记录恢复耗时 |
| 通知重试/死信 | 测试接收端返回失败直至产生死信，再恢复；管理员 GET `/event-dead-letters` 并 POST `/{id}:replay` | 人工重放后成功，死信解决；按实际接收记录证明没有重复业务通知 |

发现 5xx、超时、跨租户泄露或后台无进展时停止验收，保存脱敏请求 ID 和失败阶段。
不得通过直接改业务表、跳过 RLS、手工标记任务成功来“跑通”。轮询使用明确超时，至少
覆盖已配置的调度周期与退避周期；超过窗口视为失败而非无限等待。

## 仍需独立确认的生产门禁

- 从公共网络不能直接访问 `/metrics` 和 Worker 端口，可信监控网络可抓取。
- OTLP 后端确实收到 Trace；仅配置 URL 或看到健康接口 200 不算验证。
- 在目标规格执行至少 5 分钟稳态及峰值压测；CI 30 秒基线不算容量结论。
- 完成 PostgreSQL 和对象存储备份恢复演练，记录 RPO/RTO；在隔离恢复环境重复租户隔离
  与审批闭环，验证恢复后可写。
- 上一个镜像与当前 schema 兼容，演练应用回滚。发布脚本不会自动执行降级迁移。

## 现有自动化证据定位

这些测试提供回归证据，不声明上述真实环境场景已经完成：

- `test_postgres_approval_workflow.py`：审批策略、转交、冲突与任务分页。
- `test_postgres_reliable_events.py`、`test_m7_resilience_security.py`：可靠事件、失败恢复及安全拒绝。
- `test_postgres_obligation_scheduler.py`：到期调度、风险及履约状态。
- `test_postgres_preflight.py`：运行身份、权限及 schema 预检。
- `test_preproduction_release.py`：发布顺序、默认 dry-run、失败中断及输出脱敏。

完整结果以当前提交的 ContractOps API 工作流及实际环境验收记录为准。
