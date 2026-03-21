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

- `status` / `daily` / `like` / `reply` / `notify test` 输出 JSON
- `run` / `watch` / `serve` 主要通过日志反映运行状态

### 1. `run`

执行 watch 循环；当前与 `watch` 使用同一实现，可视为兼容别名。

```
discorsair run --max-posts-per-interval 200
```

可选项

- `--interval 30` 轮询间隔（秒）
- `--once` 仅跑一轮即退出
- `--max-posts-per-interval` 每轮最多抓取的帖子数

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

持续拉取信息流并输出/落盘，适合分析数据；当前行为与 `run` 相同。

```
discorsair watch --max-posts-per-interval 200
```

可选项

- `--interval 30` 轮询间隔（秒）
- `--once` 仅跑一轮即退出
- `--max-posts-per-interval` 每轮最多抓取的帖子数

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

### 6. `serve`

启动 HTTP 控制服务。

```
discorsair serve --host 0.0.0.0 --port 8080
```

说明：

- 如果绑定 `0.0.0.0` 或其他非回环地址，需先配置 `server.api_key`
- 如果 watch 线程或控制接口触发登录失效 / unresolved challenge，服务会自停并以非 0 退出

可用接口：

- `POST /watch/start`（请求体可选：`{"use_schedule": true|false}`；返回：`{"ok": true|false}`）
- `POST /watch/config`（请求体：`{"use_unseen": true|false, "timings_per_topic": 30, "max_posts_per_interval": 200}`）
- `POST /watch/stop`（返回：`{"ok": true|false}`）
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
  "started_at": "2026-03-17T01:00:00Z",
  "last_tick": "2026-03-17T01:10:00Z",
  "last_error": null,
  "last_error_at": null,
  "next_run": "2026-03-17T08:00:00",
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
  "schedule": [
    "08:00-12:00",
    "14:00-23:00"
  ],
  "use_unseen": false,
  "timings_per_topic": 30,
  "max_posts_per_interval": 200,
  "timezone": "Asia/Shanghai"
}
```

- `POST /watch/config` 成功示例：

```json
{
  "ok": true,
  "use_unseen": true,
  "timings_per_topic": 30,
  "max_posts_per_interval": 200
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

### 7. `status`

查看统计状态。

```
discorsair status
```

输出：

- `{"stats_total": {...}, "stats_today": {...}, "storage_path": "..."}`

### 8. `notify test`

发送一条测试通知。

```
discorsair notify test
```

输出：

- 已配置通知：`{"ok": true|false, "action": "notify_test"}`
- 未配置通知：`{"ok": false, "action": "notify_test", "reason": "notify_not_configured"}`

### 9. `init`

写入配置模板。

```
discorsair init --path config/app.json
```

## 说明

- `run` 和 `watch` 共用同一套运行逻辑与参数
- `daily` 的“未读”判断严格使用 `unseen` 字段
