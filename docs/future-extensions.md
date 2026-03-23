# 后续拓展点

## 插件系统

目标是做“固定 hook，非固定行为”的插件运行时，不退回成一套写死字段的规则引擎。

### 目录结构

```text
config/
  app.json

plugins/
  hot_topics/
    manifest.json
    plugin.py
  auto_like/
    manifest.json
    plugin.py
```

### 文件职责

- `manifest.json`
  - 插件自描述
  - 第一版字段：`id`、`name`、`version`、`api_version`、`entry`、`hooks`、`permissions`、`default_priority`、`default_config`
- `plugin.py`
  - 插件行为实现
  - 由核心加载并分发 hook
- `config/app.json`
  - 项目级启停和配置覆盖
  - 不描述插件本体，只描述是否启用、优先级覆盖、插件配置覆盖、动作限额等运行时策略

### app.json 配置结构

建议最终结构：

```json
{
  "plugins": {
    "items": {
      "auto_like": {
        "enabled": true,
        "priority": 100,
        "limits": {
          "reply_per_day": 0,
          "like_per_day": 30
        },
        "config": {
          "min_like_count": 10
        }
      }
    }
  }
}
```

约定：

- `priority` 可覆盖 manifest 里的默认优先级
- `limits.reply_per_day` / `limits.like_per_day`
  - 按插件、按动作计数
  - `0` 表示不限制
- `config`
  - 作为插件自定义配置透传给插件

### manifest.json 示例

```json
{
  "id": "auto_like",
  "name": "Auto Like",
  "version": "0.1.0",
  "api_version": 1,
  "entry": "plugin.py",
  "hooks": ["post.fetched", "topic.after_crawl"],
  "permissions": ["post.like"],
  "default_priority": 100,
  "default_config": {
    "min_like_count": 10
  }
}
```

### 插件发现与加载

- 扫描 `plugins/*/manifest.json`
- 按 `manifest.id` 建立插件索引
- 用 `app.json.plugins.items[plugin_id]` 决定：
  - 是否启用
  - 是否覆盖默认优先级
  - 是否覆盖默认配置
  - 是否覆盖动作限额
- 最终配置：
  - `final_config = manifest.default_config + app.json 覆盖`

### Hook 集合

第一版固定 hook 名称，不开放任意事件名：

- `cycle.started`
- `topics.fetched`
- `topic.before_enter`
- `topic.after_enter`
- `topic.after_crawl`
- `post.fetched`
- `cycle.finished`

其中：

- `topic.after_enter`
  - 结构型 hook
  - 只表示“已经进入 topic”
  - 不提供帖子内容，避免与内容型 hook 重复工作
- `post.fetched`
  - 内容型 hook
  - 对本轮参与内容处理的每条帖子逐条触发
- `topic.after_crawl`
  - 内容型聚合 hook
  - 对本轮参与内容处理的帖子集合触发一次

映射到代码方法名：

- `topics.fetched` -> `on_topics_fetched`
- `topic.before_enter` -> `on_topic_before_enter`
- `topic.after_enter` -> `on_topic_after_enter`
- `topic.after_crawl` -> `on_topic_after_crawl`
- `post.fetched` -> `on_post_fetched`
- 其余同理

### plugin.py 入口

建议统一为：

```python
def create_plugin():
    return Plugin()
```

插件实例可选实现：

```python
class Plugin:
    def on_load(self, ctx, event=None): ...
    def on_cycle_started(self, ctx, event): ...
    def on_topics_fetched(self, ctx, event): ...
    def on_topic_before_enter(self, ctx, event): ...
    def on_topic_after_enter(self, ctx, event): ...
    def on_topic_after_crawl(self, ctx, event): ...
    def on_post_fetched(self, ctx, event): ...
    def on_cycle_finished(self, ctx, event): ...
```

### 事件对象

统一字段：

- `event.name`
- `event.ts`
- `event.cycle_id`
- `event.plugin_config`

补充：

- `on_load`
  - 兼容 `on_load(ctx)` 和 `on_load(ctx, event)` 两种签名
  - 若需要统一写法，推荐 `event=None`

按 hook 附加：

- `topics.fetched`
  - `event.topics`
- `topic.before_enter`
  - `event.topic_summary`
- `topic.after_enter`
  - `event.topic_summary`
  - `event.topic`
- `topic.after_crawl`
  - `event.topic_summary`
  - `event.topic`
  - `event.posts`
- `post.fetched`
  - `event.topic_summary`
  - `event.topic`
  - `event.post`

补充约定：

- `topic_summary` 必须保留：
  - `unseen`
  - `last_read_post_number`
- `event` 负载按只读约定使用，不应原地修改 `event.topics` / `event.topic_summary` / `event.topic` / `event.post` / `event.posts`
- `topic.after_enter`
  - 不提供 `posts`
- `topic.after_crawl.posts`
  - 表示本轮参与内容型 hook 的帖子集合

### 内容型 hook 的触发规则

定义：

- `entered_posts`
  - `get_topic()` 返回的 `post_stream.posts`
- `backfill_posts`
  - `get_posts_by_ids()` 补拉回来的帖子

无爬取模式：

- 仅当 `topic_summary.unseen == true` 时触发内容型 hook
- `post.fetched`
  - 对 `entered_posts` 逐条触发
- `topic.after_crawl.posts`
  - 等于 `entered_posts`

爬取模式：

- `entered_posts`
  - 仅当 `topic_summary.unseen == true` 时参与内容型 hook
- `backfill_posts`
  - 只要本轮实际补拉到，就参与内容型 hook
- `backfill_posts` 在进入内容型 hook 前按 `post_number` 排序
- `post.fetched`
  - 顺序为 `entered_posts(if unseen)` + `backfill_posts(sorted by post_number)`
- `topic.after_crawl.posts`
  - 与 `post.fetched` 使用相同顺序

### ctx API

插件不直接拿底层 client，只走受控上下文：

- `ctx.reply(topic_id, content, *, once_key=None, category=None)`
- `ctx.like(post, *, emoji="heart")`
- `ctx.prioritize_topic(topic_id, score)`
- `ctx.skip_topic(topic_id, reason="")`
- `ctx.get_kv(key, default=None)`
- `ctx.set_kv(key, value)`
- `ctx.check_limit(key, daily_limit)`
- `ctx.record_trigger(key)`
- `ctx.was_done(key)`
- `ctx.mark_done(key)`
- `ctx.now()`
- `ctx.logger`
- `ctx.config`
- `ctx.plugin_id`

补充约定：

- 约定上插件应通过 `ctx` 访问能力，而不是直接操作底层 `client` / `store`
- 插件动作统一走现有 `QueuedDiscourseClient`
  - 天然继承请求队列、429 等待、节流、重试与 CSRF 重试
- `ctx.like(post, ...)`
  - 只接受 `post`
  - 不开放仅传 `post_id` 的调用方式
- `ctx.like(post, ...)` 幂等规则：
  - 仅当传入的 `post.current_user_reaction` 非空时直接跳过
  - 这是轻量保护，不是跨 hook / 跨插件 / 同轮次的强幂等
  - 因为底层是 `toggle_reaction`，重复调用可能把前一次点赞切回去
- `ctx.reply(...)` 幂等规则：
  - 由 `once_key` 控制

### 权限模型

第一版建议固定权限字符串：

- `topics.reorder`
- `topics.skip`
- `reply.create`
- `post.like`
- `storage.read`
- `storage.write`

规则：

- 插件在 `manifest.json` 声明自己需要的权限
- 核心最终按 manifest 和 app 配置裁剪权限
- 无权限调用直接拒绝，不拖垮整个 watch
- 但第一版没有进程级或 Python 级沙箱
- 所以权限模型主要是运行时约束，不等于安全隔离
- 默认前提仍然是“只加载受信任插件”

### 执行顺序与隔离

- 同一 hook 下按 `priority` 降序执行
- 同分时按 `plugin_id` 排序
- 单插件异常不影响其他插件
- 单插件单 hook 有超时
- 超时和异常都写运行日志
- 可做连续失败熔断

### 幂等与限额

由核心统一提供：

- `once_key`
  - 例如同一主题同一天只回复一次
- `daily_limit`
  - 例如某插件每天最多回复 5 次
  - 例如某插件每天最多点赞 30 次

第一版约定：

- `ctx.like` 不再单独要求 `once_key`
  - 直接依赖 `post.current_user_reaction` 判定是否跳过
- 每日限额不由插件调用时传参
  - 统一由 `app.json.plugins.items[plugin_id].limits` 决定
  - `reply_per_day`
  - `like_per_day`
  - `0` 表示不限制

建议存储维度：

- `plugin_id`
- `action`
- `scope_key`
- `day`

### 数据落点

爬取模式下建议新增：

- `plugin_runs`
  - hook 执行记录、耗时、错误
- `plugin_daily_counters`
  - 每日动作计数
- `plugin_once_marks`
  - 幂等去重键
- `plugin_kv`
  - 插件少量状态

## 模式边界

### 无爬取模式

- 不开数据库
- `last_read_post_number` 只来自 `latest/unseen` 返回值
- 仍会读取 topic 列表并进入 topic
- 仍会发送 `topics/timings`
- 不补拉帖子
- 仅当 `topic_summary.unseen == true` 时，才用 `get_topic()` 返回的首屏帖子触发内容型 hook
- 通知去重保留为进程内内存态
- 插件系统在该模式下也不能依赖数据库

### 爬取模式

- 开数据库
- `last_read_post_number` 仍以 `latest/unseen` 为准
- 数据库存储帖子内容与抓取同步状态
- `get_topic()` 首屏帖子仅在 `unseen == true` 时参与内容型 hook
- `get_posts_by_ids()` 补拉帖子参与内容型 hook
- 仅该模式下提供依赖数据库的插件持久化能力

## 其他拓展

- 通知标记为已读
- 失败重试与指数退避策略
- 代理池/节点切换
- 统一请求日志与指标统计
- 任务调度与时段策略
- 多账号轮换与风控策略
- 数据落盘与导出管线
