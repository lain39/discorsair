# CLI 设计草案

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

### 1. `run`

执行预设的“挂机流程”，用于长期运行（默认等价于 `watch`）。

```
discorsair run --max-posts-per-interval 200
```

可选项（后续）

- `--interval 30` 轮询间隔（秒）
- `--once` 仅跑一轮即退出
- `--max-posts-per-interval` 每轮最多抓取的帖子数

### 2. `daily`

轻量“日活”模式：登录后阅读 1 个未读帖子并上报 timings，然后退出。

```
discorsair daily --topic 123456
discorsair daily
```

可选项

- `--topic <id>` 指定主题 ID

### 3. `watch`

持续拉取信息流并输出/落盘，适合分析数据。

```
discorsair watch --max-posts-per-interval 200
```

可选项（后续）

- `--since <topic_id>` 只抓取大于该 ID 的主题
- `--output <path>` 输出到文件
- `--max-posts-per-interval` 每轮最多抓取的帖子数

### 4. `like`

对指定帖子执行点赞/反应。

```
discorsair like --post 15018040 --emoji heart
```

### 5. `reply`

对指定主题回帖。

```
discorsair reply --topic 719623 --raw "hello"
```

### 6. `serve`

启动 HTTP 控制服务。

```
discorsair serve --host 0.0.0.0 --port 8080
```

可用接口：

- `POST /watch/start`（可选 `{"use_schedule": true|false}`）
- `POST /watch/config`（`{"use_unseen": true|false, "timings_per_topic": 30, "max_posts_per_interval": 200}`）
- `POST /watch/stop`
- `GET /watch/status`
- `POST /like`（`{"post_id": 123, "emoji": "heart"}`）
- `POST /reply`（`{"topic_id": 123, "raw": "text", "category": 1}`）

鉴权：

- 如果配置了 `server.api_key`，请求需带 `X-API-Key: <key>` 头

**接口返回（示例）**

- `GET /watch/status`

```json
{
  "running": true,
  "started_at": "2026-03-17T01:00:00Z",
  "last_tick": "2026-03-17T01:10:00Z",
  "next_run": "2026-03-17T08:00:00",
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
  }
}
```

### 7. `status`

查看统计状态。

```
discorsair status
```

### 8. `notify test`

发送一条测试通知。

```
discorsair notify test
```

### 9. `init`

写入配置模板。

```
discorsair init --path config/app.json
```

## 说明

- `daily` 的“未读”判断严格使用 `unseen` 字段
