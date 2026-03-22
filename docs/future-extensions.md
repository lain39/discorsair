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
  - 字段建议：`id`、`name`、`version`、`api_version`、`entry`、`hooks`、`permissions`、`default_priority`、`default_config`
- `plugin.py`
  - 插件行为实现
  - 由核心加载并分发 hook
- `config/app.json`
  - 项目级启停和配置覆盖
  - 不描述插件本体，只描述是否启用、优先级覆盖、插件配置覆盖、全局超时

### 插件发现与加载

- 扫描 `plugins/*/manifest.json`
- 按 `manifest.id` 建立插件索引
- 用 `app.json.plugins.items[plugin_id]` 决定：
  - 是否启用
  - 是否覆盖默认优先级
  - 是否覆盖默认配置
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
    def on_load(self, ctx): ...
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
  - `event.new_posts`
- `post.fetched`
  - `event.topic_summary`
  - `event.topic`
  - `event.post`

### ctx API

插件不直接拿底层 client，只走受控上下文：

- `ctx.reply(topic_id, content, *, once_key=None, daily_limit=None, category=None)`
- `ctx.like(post_id, *, emoji="heart", once_key=None, daily_limit=None)`
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
  - 例如同一帖子只点赞一次
- `daily_limit`
  - 例如某插件每天最多回复 5 次
  - 例如某插件每天最多点赞 30 次

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
- 不补拉帖子，不触发依赖帖子抓取结果的事件
- 通知去重保留为进程内内存态
- 插件系统在该模式下也不能依赖数据库

### 爬取模式

- 开数据库
- `last_read_post_number` 仍以 `latest/unseen` 为准
- 数据库存储帖子内容与抓取同步状态
- 仅该模式下提供依赖数据库的插件持久化能力

## 其他拓展

- 通知标记为已读
- 失败重试与指数退避策略
- 代理池/节点切换
- 统一请求日志与指标统计
- 任务调度与时段策略
- 多账号轮换与风控策略
- 数据落盘与分析管线
