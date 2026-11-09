# 在本地 Phoenix 里看运行记录（OTel 导出）


## 导出什么

每个 `agent_run` 导出成 Phoenix 里的一条 trace：运行本身是根 span，每次模型调用、每次工具调用各是一个子 span。

导出的字段：
- id：调用 id、工具调用 id、用户 id；
- step_key、agent、purpose；
- 版本：模型、提供方、prompt 版本、手册包版本、代码版本；
- 哈希：输入、系统提示、工具定义；
- 花费、耗时、token 数；
- 结论：状态，以及停止或出错的类型（只导出类名或代码名，不导出消息）；
- 产物：这次运行产出的产物 id 和它们的父产物 id（等 artifact 表合并后才有）。

**不导出任何原文**：不导出提示词、消息、回复、工具参数和结果、subject、错误消息。

Phoenix 里也没有 EvoWiki 的原始 id：
- trace id 是 `sha256(run_id)` 的前 32 位；
- `session.id` 是 `s-` 加 `sha256(会话 id)` 的前 32 位。

要看原文，点根 span 上的 `evowiki.trace_url`。它指向 `GET /wiki/traces/otel/{trace_id}`，这个接口会核对用户，只返回本人的运行。删除请求还没完成时，接口直接返回 404。

## 删除

Phoenix 跟着 Postgres 删。删除的依据是账本，所以删除流程不需要另外交出被删的 id 列表。

| 时机 | 导出器做什么 |
|---|---|
| 用户有未完成的删除请求（`forget_request` 的 state 不是 committed） | 该用户的导出全部暂停；`hidden_of` 隐藏的那些运行（被删的会话和受牵连的轮次），已经发到 Phoenix 的 trace 立刻删掉，这个会话在 Phoenix 里的 session 也删掉 |
| 运行已经从 `agent_run` 里消失（删除请求已提交，或会话 C 清理了旧记录） | 删掉对应的 trace；如果某个 session 已经没有剩下的 trace，也一起删掉 |
| 超过导出保留期（默认 7 天） | 删掉，并且不再导出 |

Phoenix 删除失败时，账本里的那一行保留，下一轮再试（日志里有 warning）。不会出现删除失败却没有任何记录的情况。

用到的 Phoenix 接口已经对照 arize-phoenix 20.26.1 的 `schemas/openapi.json` 核实过：
- `DELETE /v1/traces/{otel trace_id}`：成功返回 204，trace 不存在返回 404，两种都算删掉了；
- `POST /v1/sessions/delete`，请求体是 `{"session_identifiers": [...]}`，会级联删除 trace 和 span。

两个接口都是按 id 精确删除，不是只靠保留期。Phoenix 服务端也另外设成 7 天保留，作为第二道保险。

## 启动 Phoenix（本机）

这一步需要从 PyPI 下载 arize-phoenix 到 uv 的缓存，不会做系统级安装。是否运行由用户决定：

```bash
PHOENIX_WORKING_DIR=D:/EvoWiki/.phoenix PHOENIX_DEFAULT_RETENTION_POLICY_DAYS=7 uvx --from arize-phoenix==20.26.1 phoenix serve
```

启动后：
- 界面在 http://127.0.0.1:6006；
- OTLP/HTTP 的接收地址是 http://127.0.0.1:6006/v1/traces；
- 数据存在 `PHOENIX_WORKING_DIR` 下的 SQLite 里。

## 打开导出

在 `项目目录的 .env` 里加一行，然后重启后端：

```
EVOWIKI_OTEL_ENDPOINT=http://127.0.0.1:6006
```

不设这一行，导出器就不会启动。可选配置：

| 变量 | 默认值 | 作用 |
|---|---|---|
| `EVOWIKI_OTEL_PROJECT` | `evowiki` | Phoenix 里的项目名 |
| `EVOWIKI_OTEL_RETENTION_DAYS` | `7` | 导出保留期 |
| `EVOWIKI_OTEL_LINK_BASE` | `http://127.0.0.1:8002` | 原文链接指向的后端地址 |
| `EVOWIKI_OTEL_EVERY_SECONDS` | `30` | 每隔多久导出一轮 |
| `EVOWIKI_PHOENIX_API_KEY` | 不设 | 只有 Phoenix 开启了认证才需要 |

导出器只在真实服务里运行，也就是有 DBOS 运行时的时候。它只读产品表，只写 `otel_export` 这一张表。

## 还没覆盖的

- 不在任何运行里的单次调用（`llm_call` 没有对应的 `agent_step`）暂时不导出：这些调用没有用户列，没法做按用户的暂停和权限检查。会话 A 的 `traced_step` 合并后，每次调用都会有所属的运行，那时自然就会导出。
- `step_key` 和 `code_version` 会在列存在时读取，会话 A 的 0090 合并后生效；产物在 `artifact` / `artifact_dependency` 表存在时读取，会话 B 的 0091 合并后生效。产物表的字段按方案里的写法读取：`artifact_id, kind, version_key, produced_by_run, created_at` 和 `child_id, parent_id, role`。
