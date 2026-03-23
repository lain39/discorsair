# CLI 说明

命令名：`discorsair`

## 设计原则

- 命令尽量短、语义清晰
- 先跑通核心流程，再扩展可选参数
- 所有命令默认读取 `config/app.json`

## 命令结构

```
discorsair <command> [options]
```

全局参数：

- `--config` 应用配置（默认 `config/app.json`）

输出约定：

- `status` / `daily` / `like` / `reply` / `export` / `import` / `notify test` 输出 JSON
- `run` / `watch` / `serve` 主要通过日志反映运行状态

### 1. `run`

执行 watch 循环；当前与 `watch` 使用同一实现，可视为兼容别名。

```
discorsair run --max-posts-per-interval 200
```

可选项

- `--interval 30` 轮询间隔（秒，必须 `>= 1`）
- `--once` 仅跑一轮即退出
- `--max-posts-per-interval` 每轮最多补抓的帖子数；只限制后续 `get_posts_by_ids()` 的补抓，不限制 `get_topic()` 首屏返回的帖子
- `run/watch` 不读取 `server.schedule`
- `run/watch` 的 `interval` 和 `max_posts_per_interval` 以 CLI 参数为准，不回退到 `server.interval_secs` / `server.max_posts_per_interval`

### 2. `daily`

轻量“日活”模式：优先选择 1 个 `unseen` 主题并上报 timings；如果没有 `unseen`，则回退到最新列表第一条。

```
discorsair daily --topic 123456
discorsair daily
```

可选项

- `--topic <id>` 指定主题 ID

输出：

- 成功：`{"ok": true, "topic_id": 123456}`
- 未找到主题：`{"ok": false, "topic_id": null, "reason": "no_topic_found"}`

### 3. `watch`

持续拉取信息流并输出/落盘，适合持续采集；当前行为与 `run` 相同。

```
discorsair watch --max-posts-per-interval 200
```

可选项

- `--interval 30` 轮询间隔（秒，必须 `>= 1`）
- `--once` 仅跑一轮即退出
- `--max-posts-per-interval` 每轮最多补抓的帖子数；只限制后续 `get_posts_by_ids()` 的补抓，不限制 `get_topic()` 首屏返回的帖子
- `run/watch` 不读取 `server.schedule`
- `run/watch` 的 `interval` 和 `max_posts_per_interval` 以 CLI 参数为准，不回退到 `server.interval_secs` / `server.max_posts_per_interval`

### 4. `like`

对指定帖子执行点赞/反应。

```
discorsair like --post 15018040 --emoji heart
```

输出：

- `{"ok": true, "post_id": 15018040, "emoji": "heart", ...}`

### 5. `reply`

对指定主题回帖。

```
discorsair reply --topic 719623 --raw "hello"
```

输出：

- `{"ok": true, "topic_id": 719623, "post_id": 123456, ...}`

### 6. `export`

按当前配置里的存储后端导出数据到 NDJSON 目录。

```
discorsair export --output ./export
```

说明：

- 当前只支持 NDJSON 格式
- 读取当前 `--config` 对应的 `storage.backend`
- SQLite 会导出当前库文件；PostgreSQL 会导出当前 DSN 对应库

输出：

- `{"ok": true, "action": "export", "format": "discorsair-ndjson-v1", "backend": "...", "output_dir": "...", "tables": {...}}`

### 7. `import`

把 NDJSON 导出目录导入到当前配置里的存储后端。

```
discorsair import --input ./export
```

说明：

- 当前只支持 NDJSON 格式
- 导入前会先按当前 `storage.backend` 初始化目标 schema
- 目标是 SQLite 时写入当前库文件；目标是 PostgreSQL 时写入当前 DSN 对应库
- 同一份导出重复导入时，主键表会收敛到最终状态，去重表和插件动作日志不会无限重复插入

输出：

- `{"ok": true, "action": "import", "format": "discorsair-ndjson-v1", "backend": "...", "input_dir": "...", "tables": {...}}`

### 8. `serve`

启动 HTTP 控制服务。

```
discorsair serve --host 0.0.0.0 --port 17880
```

说明：

- 如果绑定 `0.0.0.0` 或其他非回环地址，需先配置 `server.api_key`
- 如果 watch 线程或控制接口触发登录失效 / unresolved challenge，服务会自停并以非 0 退出
- `server.schedule`、`server.interval_secs`、`server.max_posts_per_interval` 只用于 `serve` 模式里的 watch 线程

可用接口：

- `POST /watch/start`（请求体可选：`{"use_schedule": true|false}`；返回：`{"ok": true|false}`）
- `POST /watch/config`（请求体：`{"use_unseen": true|false, "timings_per_topic": 30, "max_posts_per_interval": 200}`）
- `POST /watch/stop`（返回：`{"ok": true|false, "already_stopped": true|false}`）
- `GET /watch/status`
- `POST /like`（请求体：`{"post_id": 123, "emoji": "heart"}`）
- `POST /reply`（请求体：`{"topic_id": 123, "raw": "text", "category": 1}`）

鉴权：

- 如果配置了 `server.api_key`，请求需带 `X-API-Key: <key>` 头
- 默认监听地址建议保持 `127.0.0.1`
- 如果改成 `0.0.0.0` 或其他非回环地址，必须配置 `server.api_key`

常见错误响应：

- `401 {"error":"unauthorized"}`：缺少或错误的 `X-API-Key`
- `401 {"error":"not_logged_in"}`：请求命中登录失效；服务会触发 fatal stop 并关闭
- `503 {"error":"challenge_unresolved"}`：过盾后仍被 Cloudflare 阻断；服务会触发 fatal stop 并关闭
- `504 {"error":"timeout"}`：控制接口执行超时
- `500 {"error":"internal"}`：其他未处理异常

**接口返回（示例）**

- `GET /watch/status`

```json
{
  "running": true,
  "stop_requested": false,
  "stopping": false,
  "started_at": "2026-03-17T01:00:00Z",
  "last_tick": "2026-03-17T01:10:00Z",
  "last_error": null,
  "last_error_at": null,
  "next_run": "2026-03-17T08:00:00",
  "storage_enabled": true,
  "storage_path": "data/discorsair.example.db",
  "stats_total": {
    "topics_seen": 120,
    "posts_fetched": 340,
    "timings_sent": 58,
    "notifications_sent": 7
  },
  "stats_today": {
    "topics_seen": 20,
    "posts_fetched": 60,
    "timings_sent": 10,
    "notifications_sent": 1
  },
  "plugins": {
    "enabled": true,
    "count": 1,
    "backend": "sqlite",
    "runtime_live": true,
    "items": []
  },
  "schedule": [
    "08:00-12:00",
    "14:00-23:00"
  ],
  "use_schedule": true,
  "use_unseen": false,
  "timings_per_topic": 30,
  "max_posts_per_interval": 200,
  "timezone": "Asia/Shanghai"
}
```

说明：

- `use_schedule` 表示当前运行中的 watch 线程是否真的按 `server.schedule` 控制
- `stop_requested == true` 表示已经收到过停止请求；线程可能仍在收尾，也可能已经停完
- `stopping == true` 表示已经收到 `POST /watch/stop`，且当前 watch 线程还没完全退出
- 只有 `use_schedule == true` 时，`next_run` 才有值；手动用 `POST /watch/start {"use_schedule": false}` 启动时，`next_run` 为 `null`
- `POST /watch/stop` 是幂等的：线程正在运行时会返回 `{"ok": true, "already_stopped": false}`；线程在处理这次 stop 请求之前就已经停完时，会返回 `{"ok": true, "already_stopped": true}`
- `POST /watch/stop` 返回 `{"ok": false}` 只表示当前没有可停止的 watch 实例
- `POST /watch/stop` 返回 `{"ok": true}` 不等于线程已经完全停止；是否还在收尾看 `stopping`
- `storage_enabled` 表示当前 watch/status 背后的存储是否已打开
- `storage_path` 在 SQLite 下是库文件路径；在 PostgreSQL 下是脱敏后的 DSN
- `plugins` 是当前 watch 线程里插件管理器的实时快照；其中运行态字段不是静态推断值

- `POST /watch/config` 成功示例：

```json
{
  "ok": true,
  "use_unseen": true,
  "timings_per_topic": 30,
  "max_posts_per_interval": 200
}
```

- `POST /watch/stop` 成功示例：

```json
{
  "ok": true,
  "already_stopped": true
}
```

- `POST /like` 成功示例：

```json
{
  "ok": true,
  "result": {
    "post_action_id": 1,
    "acted": true
  }
}
```

- `POST /reply` 成功示例：

```json
{
  "ok": true,
  "result": {
    "topic_id": 123,
    "post_id": 456
  }
}
```

### 9. `status`

查看统计状态。

```
discorsair status
```

输出：

- `{"storage_enabled": true|false, "stats_total": {...}|null, "stats_today": {...}|null, "storage_path": "..."|null, "plugins": {...}}`

其中 `plugins` 会包含：

- `backend`
  - 启用插件时为 `sqlite`、`postgres` 或 `memory`
  - 未启用任何插件时为 `null`
- `runtime_live`
  - `discorsair status` 下固定为 `false`
  - 表示这里只是静态/持久态快照，不是运行中插件管理器的实时内存态
- `items[*].daily_counts`
  - 插件今日动作计数摘要，例如 `reply` / `like` / `trigger:*`
- `items[*].once_mark_count`
  - 插件累计 `once_key` / `mark_done` 标记数
- `items[*].kv_keys`
  - 插件已写入的 KV key 列表

说明：

- `discorsair status` 不会导入或实例化插件代码
- 因此其中的运行态字段，例如 `hook_successes` / `hook_failures` / `disabled`，在 CLI `status` 下会是 `null`
- 如果要看运行中 watch 线程里的实时插件运行态，用 HTTP `GET /watch/status`

### 10. `notify test`

发送一条测试通知。

```
discorsair notify test
```

输出：

- 已配置通知：`{"ok": true|false, "action": "notify_test"}`
- 未配置通知：`{"ok": false, "action": "notify_test", "reason": "notify_not_configured"}`

### 11. `init`

写入配置模板。

```
discorsair init --path config/app.json
```

## 说明

- `run` 和 `watch` 共用同一套运行逻辑与参数
- `daily` 的“未读”判断严格使用 `unseen` 字段
