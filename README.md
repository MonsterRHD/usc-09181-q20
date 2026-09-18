# 跨境争议客服工作台

面向海外消费争议的客服工作台：把交易、客户授权、证据请求、外部回执和时限组织成**案件流程**，
协调收单行（ACQUIRER）、商户（MERCHANT）与翻译团队（TRANSLATOR）。零三方依赖，
仅使用 Python 3.11 标准库 + SQLite。

## 启动

```bash
python3 -m service.main          # 默认 0.0.0.0:8000，DATABASE_PATH=./workbench.db
PORT=8000 DATABASE_PATH=/data/wb.db python3 -m service.main
```

启动时自动恢复未完成的外部请求（PENDING 重发、DISPATCHED 向外部核对），
因此程序再次运行后在途请求继续可追踪。

健康检查：`GET /health`

## 角色与脱敏

角色经 `X-Role` 请求头传递：`AGENT`（一线）、`SUPERVISOR`（主管）、`BANK`、`MERCHANT`、`TRANSLATOR`。

- 库内数据始终为明文（仅存 last4，禁止完整卡号）；脱敏只发生在**视图与通知文本**上。
- 主管可见明文；一线邮箱局部掩码；收单行/商户只见姓氏首字母；翻译角色敏感字段全部 `***`。
- 银行原始回执的 JSON 正文同样按角色脱敏；非结构化原文对翻译角色整体遮蔽。

## 核心业务规则

| 主题 | 规则 |
|---|---|
| 案件 | 状态机 OPEN → EVIDENCE_REQUESTED → IN_REVIEW → ESCALATED → RESOLVED |
| 重复申诉 | 同一原始交易号 + 同一争议码且已有未结案件时，新案件 `duplicate_of` 关联，不重复立案 |
| 证据 | REQUESTED → SUBMITTED → ACCEPTED/REJECTED；记录材料是否齐全、缺件清单；驳回后另开补证请求，旧请求留痕 |
| 部分退款 | PROPOSED → APPROVED → SENT_TO_BANK → CONFIRMED；不得超过原交易金额；CONFIRMED 需银行 REFUND 回执 |
| 翻译 | 按版本追加（v1、v2…），新版本交付后旧版本标记 SUPERSEDED，旧译文不被覆盖 |
| 客户撤回 | REQUESTED → CONFIRMED/REJECTED；确认后案件以 WITHDRAWN 终结；重复撤回被拒 |
| 时区截止 | 截止时间统一换算为 UTC 绝对时刻存储，另存本地时区标签；`POST /deadlines/sweep` 标记 BREACHED |
| 银行回执 | **只追加、不可修改**；编号相同内容相同 → 幂等忽略且不重复通知；编号相同内容不同 → 冲突拒绝 |
| 回执更正 | 客服不能改原始回执，只能 `POST /receipts/{uid}/corrections` 追加**带原因**的更正 |
| 通知去重 | `UNIQUE(receipt_uid, channel, target_role)`，相同回执对同一角色只通知一次，正文按角色脱敏 |
| 转派/升级 | 每次写入责任链（序号、类型、前后责任人、原因、操作人）；支持 `expected_owner` 乐观并发控制，过期转派返回 409 |
| 外部请求 | PENDING → DISPATCHED → ACKNOWLEDGED/FAILED；带幂等键、尝试次数与错误；重启后可恢复 |
| 审计 | 所有动作写 `audit_events`；`GET /audit?case_ref=...` 导出案件全貌 + 有序事件流 |

## HTTP API 摘要

- `POST /cases` 立案；`GET /cases`、`GET /cases/{ref}`（按角色脱敏）
- `POST /cases/{ref}`，body 中 `action` 取值：
  `transfer`（转派/升级，可带 `expected_owner`、`escalate`）、`authorization`、
  `request_evidence`（`deadline_hours` + `timezone_name`）、`propose_refund`、
  `request_translation`、`withdraw`、`receipt`（银行回执）、`resolve`
- `POST /evidence/{ref}/submit`、`POST /evidence/{ref}/decision`
- `POST /refunds/{ref}/approve`、`POST /refunds/{ref}/send`
- `POST /cases/{ref}/translations/{version}/delivery`
- `POST /withdrawals/{ref}/confirm`、`POST /authorizations/{ref}/revoke`
- `POST /receipts/{uid}/corrections`
- `GET /notifications`、`POST /notifications/deliver`
- `GET /requests`（未完成外部请求）、`POST /recovery/recover`
- `POST /deadlines/sweep`、`GET /audit[?case_ref=...]`

错误码：400 校验失败 / 403 原始回执不可变 / 404 不存在 / 409 状态非法、并发冲突或回执内容冲突；
重复回执返回 200 `{"duplicate": true, "notified": false}`。

## 代码结构

```
service/
  models.py     角色、参与方、状态枚举
  db.py         SQLite schema 与访问封装（事务、WAL）
  timeutil.py   UTC 绝对时刻 + 本地时区标签
  masking.py    按角色字段级脱敏
  errors.py     领域错误
  workbench.py  核心领域服务（案件全流程、回执幂等、外部请求恢复、审计导出）
  app.py        HTTP 适配层
  main.py       启动入口（启动时恢复在途请求）
tests/          单元/集成/端到端演练（含可控时钟与故障网关）
```

## 测试与上线前演练

```bash
python3 -m unittest discover -s tests -v
```

`tests/test_end_to_end.py` 让一笔交易完整经历**补证（材料不足→驳回→补齐）→
部分退款（提议→批准→发送银行）→ 跨日回执（UTC 次日到达并确认退款）→
并发转派（乐观锁拦截过期转派 + 升级）**，并核对：最终结论为 PARTIAL_REFUND、
重复回执零增量通知、责任链三段有序、审计事件齐全，以及“程序再次运行”后
无在途请求丢失、库中只有一条回执。
