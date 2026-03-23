# Schema

## 目标

Schema 用于替代当前偏“运行时缓存”的 SQLite 表结构，目标是同时满足：

- 运行时状态持久化
- 面向采集的数据落盘
- 统一支持 `sqlite` / `postgres`
- 为后续迁移保留稳定的逻辑模型

本版先定义逻辑 schema，不直接绑定具体数据库实现细节。

## 总体原则

- 不再使用按日期分库/分文件。
- 时间维度进入表字段，不进入文件名。
- 区分“运行状态表”和“采集数据表”。
- `sqlite` 按 `site` 分文件；`postgres` 使用单库。
- 两种后端尽量共用同一套逻辑表结构。
- 本项目只负责数据采集，不在项目内承担数据分析职能。
- 同一 `site` 在任一时刻只允许一个账号开启爬取。
  - 这是运行时约束，不由业务表结构本身保证。

## 维度与标识

远端的 `topic_id` / `post_id` 只在单个 forum site 内唯一，因此逻辑主键统一使用复合键：

- `site_key + topic_id`
- `site_key + post_id`
- `site_key + account_name + ...`

其中：

- `site_key`
  - 本地站点标识
  - 由 `base_url` 规范化得到
- `account_name`
  - 本地账号标识
  - 直接使用配置文件中的 `auth.name`
  - 不代表远端论坛用户资料

## 元数据表

### `sites`

站点维表。

字段：

- `site_key` PRIMARY KEY
- `base_url`
- `created_at`
- `updated_at`

说明：

- `sqlite` 按站点分文件时，这张表通常只有一行。
- 即使如此仍保留，便于统一 `sqlite` / `postgres` schema。

### `accounts`

本地逻辑账号维表。

字段：

- `site_key`
- `account_name`
- `created_at`
- `updated_at`
- PRIMARY KEY (`site_key`, `account_name`)

说明：

- `account_name` 直接来自配置文件中的 `auth.name`。
- 如果用户修改 `auth.name`，语义上等价于创建了新的本地账号标识。
- 该表不依赖远端论坛账号信息，不需要额外抓取 account profile。

## 运行状态表

### `topic_crawl_state`

记录 topic 的抓取进度，仅服务运行时。

字段：

- `site_key`
- `topic_id`
- `last_synced_post_number`
- `last_stream_len`
- `updated_at`
- PRIMARY KEY (`site_key`, `topic_id`)

说明：

- `last_read_post_number` 不进入该表。
- `last_read_post_number` 只以 `latest.json` / `unseen.json` 返回值为准。
- 该表替代当前 `topics` 表中与同步游标相关的字段。

### `notification_dedupe`

记录已发送通知的本地去重状态。

字段：

- `site_key`
- `account_name`
- `notification_id`
- `created_at`
- PRIMARY KEY (`site_key`, `account_name`, `notification_id`)

说明：

- 这是账号维度状态，不是站点全局状态。
- 非爬取模式下不落数据库，仍保持内存态。

### `plugin_daily_counters`

插件动作/触发器的每日计数。

字段：

- `site_key`
- `account_name`
- `plugin_id`
- `action`
- `day`
- `count`
- PRIMARY KEY (`site_key`, `account_name`, `plugin_id`, `action`, `day`)

### `plugin_once_marks`

插件的 once 语义标记。

字段：

- `site_key`
- `account_name`
- `plugin_id`
- `key`
- `created_at`
- PRIMARY KEY (`site_key`, `account_name`, `plugin_id`, `key`)

### `plugin_kv`

插件的键值状态存储。

字段：

- `site_key`
- `account_name`
- `plugin_id`
- `key`
- `value_json`
- `updated_at`
- PRIMARY KEY (`site_key`, `account_name`, `plugin_id`, `key`)

## 采集数据表

### `topics`

topic 当前状态表。

字段：

- `site_key`
- `topic_id`
- `category_id`
- `title`
- `slug`
- `tags_json`
- `reply_count`
- `views`
- `like_count`
- `highest_post_number`
- `unseen`
- `last_read_post_number`
- `created_at`
- `bumped_at`
- `last_posted_at`
- `first_post_updated_at`
- `first_seen_at`
- `synced_at`
- PRIMARY KEY (`site_key`, `topic_id`)

说明：

- `first_post_updated_at` 来自 `get_topic().post_stream.posts[0].updated_at`。
- topic 是否发生“内容更新”，优先使用 `first_post_updated_at` 判定。
- `synced_at` 表示本地最后一次看到该 topic 的时间。
  - 它不用于判定论坛内容是否更新。
  - 它用于采集链路排障和导出窗口切分。

### `topic_snapshots`

topic 变化历史表，仅在 topic 发生变化时插入一条记录。

字段：

- `site_key`
- `topic_id`
- `captured_at`
- `first_post_updated_at`
- `title`
- `category_id`
- `tags_json`
- `raw_json`
- PRIMARY KEY (`site_key`, `topic_id`, `captured_at`)

变更判定：

- `first_post_updated_at`
- `title`
- `category_id`
- `tags_json`

即：每次 `get_topic()` 都更新 `topics` 当前表，但只有上述字段任一变化时才写入 `topic_snapshots`。

### `posts`

帖子事实表，不记录更新历史。

字段：

- `site_key`
- `post_id`
- `topic_id`
- `post_number`
- `reply_to_post_number`
- `username`
- `created_at`
- `updated_at`
- `fetched_at`
- `like_count`
- `reply_count`
- `reads`
- `score`
- `incoming_link_count`
- `current_user_reaction`
- `cooked`
- `raw_json`
- PRIMARY KEY (`site_key`, `post_id`)

说明：

- post 默认不做 snapshots。
- 同一 post 后续如再次抓到，会用 upsert 覆盖当前值，但不单独保留历史。
- `fetched_at` 当前定义为“本次写入时间”。

### `watch_cycles`

每轮 watch 的执行结果，用于运行观测、采集统计和导出对账。

字段：

- `cycle_id` PRIMARY KEY
- `site_key`
- `account_name`
- `started_at`
- `ended_at`
- `topics_fetched`
- `topics_entered`
- `posts_fetched`
- `notifications_sent`
- `success`
- `error_text`

说明：

- 不保存 `use_unseen`。
  - 后续如果运行中支持热更新，该值在一个长生命周期 watch 中可能变化。
- 不保存 `crawl_enabled`。
  - `crawl_enabled = false` 时本身不会进入数据库路径。

### `plugin_action_logs`

插件动作/结果日志。

字段：

- `id` PRIMARY KEY
- `cycle_id`
- `site_key`
- `account_name`
- `plugin_id`
- `hook_name`
- `action`
- `topic_id`
- `post_id`
- `status`
- `reason`
- `created_at`
- `extra_json`

说明：

- 这是采集过程中的结构化动作记录，不替代正常日志文件。
- `extra_json` 用于记录少量结构化补充信息，避免过早固定过多列。

## 原始 JSON 保留策略

### topic

`topic_snapshots.raw_json` 保留裁剪后的 `get_topic()` 返回值：

- 保留 topic 自身字段
- `post_stream.posts` 只保留 1 楼
- 丢弃 2 楼及以后帖子

原因：

- topic 重复抓取频率较高，保留一份精简后的原始结构有调试和补字段价值。
- 2 楼及以后帖子本身属于 `posts` 表职责，继续保留会造成明显重复。

### post

`posts.raw_json` 只保留单条 `post_stream.posts[i]` 对象。

原因：

- post 一般只抓一次
- 单条 post 原始结构体积相对可控
- 对后续补字段和排查接口差异有帮助

## SQLite 与 PostgreSQL 的落地约束

### SQLite

- 按 `site_key` 分文件
- 一个 `site` 只允许一个账号开启爬取
- 同一站点下多个账号如果都只做非爬取模式，不应共享数据库写路径
- 不再支持 `rotate_daily`

说明：

- `sqlite` 场景下，`sites` 表通常只有一行，但仍保留
- 这样后续迁移 `postgres` 时不需要再重构逻辑 schema

### PostgreSQL

- 全项目使用单库
- 多个站点、多账号共存于同一 schema
- 依靠 `site_key` / `account_name` 分区逻辑数据
- 同一 `site` 的单爬虫约束由运行时锁或 lease 机制保证，不由本文件定义

## 索引

当前第一版索引：

- `topics(site_key, category_id)`
- `topics(site_key, last_posted_at)`
- `topic_snapshots(site_key, topic_id, captured_at DESC)`
- `posts(site_key, topic_id, post_number)`
- `posts(site_key, created_at)`
- `watch_cycles(site_key, account_name, started_at DESC)`
- `plugin_action_logs(site_key, plugin_id, created_at DESC)`
- `notification_dedupe(site_key, account_name, created_at DESC)`

## 与当前 schema 的主要差异

- 废弃按日期分库/分文件
- 当前 `topics` 表中的同步游标字段拆分到 `topic_crawl_state`
- `last_read_post_number` 不再依赖数据库作为读帖起点来源
- 新增 topic 当前表与 topic 变化历史表
- post 从“缓存表”提升为采集事实表
- 插件状态表补上 `site_key` / `account_name`
- 新增 `watch_cycles` 与 `plugin_action_logs`

## 旧库处理

本次 schema 调整不提供旧 SQLite 库的迁移脚本。

处理原则：

- 如果当前 SQLite 文件仍是旧 schema，直接删除后由运行时按新 schema 重建
- 运行时发现旧表结构时，应明确报错并提示删除旧库
- 这条规则只针对“旧 runtime/cache schema -> 当前 schema”

原因：

- 当前项目定位已经从“运行时缓存”转为“数据采集”
- 旧库字段语义与新 schema 差异较大，做自动迁移收益不高
- 直接重建比维护一次性迁移脚本更稳

## SQLite 到 PostgreSQL 的导出/导入方案

当前按“逻辑表级导出导入”处理，不做数据库文件级转换。

### 目标

- 把单站点 SQLite 采集数据迁入 PostgreSQL
- 允许多份 SQLite 库汇总到一个 PostgreSQL 库
- 保持当前逻辑主键与幂等语义

### 导出粒度

按表导出，不按整库二进制转换。

导出的表：

- `sites`
- `accounts`
- `topic_crawl_state`
- `topics`
- `topic_snapshots`
- `posts`
- `notification_dedupe`
- `plugin_daily_counters`
- `plugin_once_marks`
- `plugin_kv`
- `watch_cycles`
- `plugin_action_logs`
- `stats_total`
- `stats_daily`

### 推荐交换格式

首选 NDJSON。

原因：

- JSON 字段和长文本字段更容易原样保留
- 比 CSV 更不容易在 `raw_json` / `cooked` / `extra_json` 上踩转义坑
- 便于分表、分批、断点导入

目录结构：

```text
export/
  meta.json
  sites.ndjson
  accounts.ndjson
  topic_crawl_state.ndjson
  topics.ndjson
  topic_snapshots.ndjson
  posts.ndjson
  notification_dedupe.ndjson
  plugin_daily_counters.ndjson
  plugin_once_marks.ndjson
  plugin_kv.ndjson
  watch_cycles.ndjson
  plugin_action_logs.ndjson
  stats_total.ndjson
  stats_daily.ndjson
```

其中 `meta.json` 包含：

- `format`
- `exported_at`
- `source_backend`
- `source_site_key`
- `source_account_name`
- `source_path`
- `tables`

### 导入原则

- PostgreSQL 端只需要先建库；导入时会自动初始化当前 schema
- 每张表独立导入
- 导入时统一使用 upsert / ignore 冲突
- 以逻辑主键去重，而不是相信导出文件无重复

规则：

- 维表与状态表：
  - 存在即 upsert
- `topic_snapshots` / `watch_cycles` / `plugin_action_logs`：
  - 保留原始记录，按主键或唯一键去重
- `posts` / `topics`：
  - 允许重复导入，最终以主键 upsert 收敛

### 多 SQLite 库汇总到一个 PostgreSQL 库

这是方案设计时必须支持的主路径。

因为：

- SQLite 按 `site` 分文件
- PostgreSQL 使用单库
- 后续很可能把多个站点、多个账号历史数据汇总到同一个 PostgreSQL 库

因此导入器必须：

- 依赖数据里的 `site_key` / `account_name`
- 不从目标 DSN 推断站点
- 允许重复导入不同来源文件

### 不建议做的方案

- 不做 `.db -> .sql` 的原始 sqlite dump 直灌 postgres
- 不做整库字符串替换式转换
- 不做跳过逻辑主键校验的裸插入

### 当前实现

当前已经按两步命令落地：

1. `discorsair export --output ./export`
2. `discorsair import --input ./export`

这样把“导出”和“导入”解耦：

- 便于先备份，再迁移
- 便于线下清洗或抽样检查
- 便于多份 SQLite 导出结果汇总后统一导入 PostgreSQL

## 暂未在本版解决的问题

- `site_key` 的最终规范化算法
- `fetched_at` 在 post upsert 场景下是“首次抓取时间”还是“最近抓取时间”的最终取舍
- 是否需要进一步拆分更细的板块/category 维表
