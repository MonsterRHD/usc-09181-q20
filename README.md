# 跨境争议客服工作台

客户海外消费发起争议后，客服在本工作台把**交易、客户授权、证据请求、外部回执、时限**
组织成一条可审计的案件流程，协调收单行、商户与翻译团队推进结案。

## 运行

```bash
python -m service.main          # 或安装后使用 service 命令
# 环境变量：PORT（默认 8000）、DB_PATH（默认 ./workbench.db）
```

数据落在 SQLite 文件中，进程重启后案件、回执、审计链全部保留；
`GET /external-requests/pending` 可随时恢复跟踪未完成的外部请求（待补证、待处理的回执更正）。

## 核心规则

| 诉求 | 实现 |
| --- | --- |
| 回执重复到达不重复通知 | 回执按 `(case_id, idempotency_key)` 幂等；通知按 `dedup_key` 唯一去重 |
| 敏感信息按角色脱敏 | `X-Role` 头区分 agent / supervisor / merchant / translator / auditor，未知角色最严格 |
| 转派升级留责任链 | `assignments` 记录 from→to、原因、操作人；乐观锁（`expected_version`）保证并发转派只有一个生效，失败尝试也写入审计 |
| 原始回执不可改 | 回执落库只读，更正走 `POST /receipts/{id}/corrections`（必须带原因），原始内容不变 |
| 子流程各自有状态 | 部分退款、重复申诉、翻译版本、时区截止、客户撤回各有独立状态机（见 `service/domain.py`） |

## 主要接口

```
POST   /cases                                  建案（交易 + 客户 + 时区）
POST   /cases/{id}/authorization               客户授权/撤销授权
POST   /cases/{id}/evidence-requests           发证据请求（可带当地时区截止时间）
POST   /evidence-requests/{id}/submit|fulfill  补证 / 确认材料齐全
POST   /cases/{id}/receipts                    接收外部回执（幂等，重复返回 200）
POST   /receipts/{id}/corrections              对回执提交带原因的更正
POST   /cases/{id}/refunds                     提议部分退款 → approve → settle
POST   /cases/{id}/appeals                     申诉（同理由判重）；accept 后案件重开
POST   /cases/{id}/translations                登记翻译版本（新版本取代旧版本）
POST   /cases/{id}/withdraw                    客户撤回（取消未完成的请求与时限）
POST   /cases/{id}/transfer|escalate           转派/升级（需 expected_version 乐观锁）
POST   /cases/{id}/resolve|close               出结论（校验退款与证据状态）/ 归档
POST   /deadlines/check                        扫描超时时限并通知负责人
GET    /cases/{id}                             案件视图（按 X-Role 脱敏）
GET    /cases/{id}/notifications               通知列表（验证去重）
GET    /cases/{id}/audit                       审计导出（按序全量事件）
GET    /external-requests/pending              未完成的外部请求（重启恢复入口）
```

请求头：`X-Actor`（操作人，写入审计）、`X-Role`（角色，决定脱敏策略）。

## 测试

```bash
python -m unittest discover -s tests
```

`tests/test_acceptance.py` 为上线前验收：一笔交易完整经历补证 → 部分退款 →
跨日回执（含重复回执去重与更正）→ 并发转派 → 出结论，并核对最终结论、
通知去重、审计导出与重启后可追踪性。
