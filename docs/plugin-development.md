# 插件开发

本文档面向“要自己写插件的人”。

如果你只是想启用现成插件，先看 [`config/app.json.template`](../config/app.json.template) 和 [`plugins/sample_forum_ops/`](../plugins/sample_forum_ops/)。

## 目标

Discorsair 的插件系统是“固定 hook，插件自定义行为”。

- 核心负责：
  - 发现插件
  - 校验 manifest
  - 在固定时机分发 hook
  - 控制权限、动作限额、超时、失败熔断
- 插件负责：
  - 决定是否排序主题
  - 决定是否跳过主题
  - 决定何时回复
  - 决定何时点赞
  - 保存自己的轻量状态

第一版不是规则引擎，也不是热重载系统。

## 目录结构

每个插件一个目录：

```text
plugins/
  my_plugin/
    manifest.json
    plugin.py
```

约定：

- 目录名建议与插件 `id` 一致
- `manifest.json` 描述插件元信息
- `plugin.py` 实现插件逻辑

## 最小示例

`manifest.json`:

```json
{
  "id": "my_plugin",
  "name": "My Plugin",
  "version": "0.1.0",
  "api_version": 1,
  "entry": "plugin.py",
  "hooks": ["topics.fetched"],
  "permissions": ["topics.reorder"],
  "default_priority": 100,
  "default_config": {}
}
```

`plugin.py`:

```python
class Plugin:
    def on_topics_fetched(self, ctx, event):
        for topic in event.topics:
            title = str(topic.get("title", ""))
            if "important" in title.lower():
                ctx.prioritize_topic(int(topic["id"]), 100)


def create_plugin():
    return Plugin()
```

`config/app.json`:

```json
{
  "plugins": {
    "items": {
      "my_plugin": {
        "enabled": true
      }
    }
  }
}
```

## manifest.json

第一版支持这些字段：

- `id`
  - 插件唯一标识
  - 必须和 `app.json.plugins.items.<id>` 对应
- `name`
  - 展示名
- `version`
  - 插件版本号
- `api_version`
  - 当前必须是 `1`
- `entry`
  - 入口文件，通常写 `plugin.py`
- `hooks`
  - 订阅的 hook 列表
  - 不能为空
- `permissions`
  - 申请的权限列表
- `default_priority`
  - 默认优先级
  - 数值越大越先执行
- `default_config`
  - 插件默认配置
  - 必须是对象

示例：

```json
{
  "id": "sample_forum_ops",
  "name": "Sample Forum Ops",
  "version": "0.1.0",
  "api_version": 1,
  "entry": "plugin.py",
  "hooks": ["topics.fetched", "topic.after_crawl", "post.fetched"],
  "permissions": ["topics.reorder", "topics.skip", "reply.create", "post.like"],
  "default_priority": 100,
  "default_config": {
    "min_like_count": 10
  }
}
```

## app.json 中怎么启用插件

插件本体信息放在 `manifest.json`，项目级启停和覆盖放在 `config/app.json`：

```json
{
  "plugins": {
    "dir": "plugins",
    "hook_timeout_secs": 10,
    "max_consecutive_failures": 3,
    "items": {
      "my_plugin": {
        "enabled": true,
        "priority": 200,
        "permissions": ["topics.reorder"],
        "limits": {
          "reply_per_day": 0,
          "like_per_day": 0
        },
        "config": {
          "keyword": "important"
        }
      }
    }
  }
}
```

字段说明：

- `plugins.dir`
  - 插件目录，默认 `plugins`
- `plugins.hook_timeout_secs`
  - 默认 hook 超时秒数
  - `0` 表示不设超时
- `plugins.max_consecutive_failures`
  - 默认连续失败熔断阈值
  - `0` 表示不自动禁用
- `plugins.items.<plugin_id>.enabled`
  - 是否启用该插件
- `priority`
  - 覆盖 manifest 默认优先级
- `permissions`
  - 只能缩小，不能超出 manifest 申请的权限
- `limits.reply_per_day`
  - 每日回复上限
- `limits.like_per_day`
  - 每日点赞上限
- `config`
  - 透传给插件，合并到 `ctx.config`

## plugin.py 入口

入口函数固定为：

```python
def create_plugin():
    return Plugin()
```

插件实例可按需实现这些方法：

```python
class Plugin:
    def on_load(self, ctx, event): ...
    def on_cycle_started(self, ctx, event): ...
    def on_topics_fetched(self, ctx, event): ...
    def on_topic_before_enter(self, ctx, event): ...
    def on_topic_after_enter(self, ctx, event): ...
    def on_post_fetched(self, ctx, event): ...
    def on_topic_after_crawl(self, ctx, event): ...
    def on_cycle_finished(self, ctx, event): ...
```

说明：

- 所有 hook 统一使用 `(ctx, event)`
- 你不需要继承基类
- 不实现的方法会被直接跳过

## Hook 一览

### `on_load`

插件被加载时调用一次。

适合做：

- 读取配置
- 写初始化日志
- 初始化 KV

不适合做：

- 长时间阻塞
- 发大量网络请求
- 扫全量历史数据

### `on_cycle_started`

每轮 watch 开始时触发一次。

`event` 额外字段：

- `crawl_enabled`
- `use_unseen`

### `on_topics_fetched`

拿到 `latest.json` 或 `unseen.json` 的主题列表后触发。

`event` 额外字段：

- `topics`

适合做：

- 按标题关键词重排主题
- 根据主题元数据跳过部分主题

### `on_topic_before_enter`

准备进入某个主题前触发。

`event` 额外字段：

- `topic_summary`

适合做：

- 在进入前判断要不要跳过

### `on_topic_after_enter`

已经进入主题、拿到了 `get_topic()` 返回结果后触发。

`event` 额外字段：

- `topic_summary`
- `topic`

注意：

- 这是结构型 hook
- 不提供 `posts`
- 不建议在这里做帖子内容分析

### `on_post_fetched`

内容型 hook。对本轮参与内容处理的每条帖子逐条触发。

`event` 额外字段：

- `topic_summary`
- `topic`
- `post`

适合做：

- 检查帖子内容后点赞
- 对单条帖子命中规则时累计触发次数

### `on_topic_after_crawl`

内容型聚合 hook。对本轮参与内容处理的帖子集合触发一次。

`event` 额外字段：

- `topic_summary`
- `topic`
- `posts`

适合做：

- 汇总分析整批帖子
- 根据整批帖子内容决定是否回复

### `on_cycle_finished`

每轮 watch 结束时触发一次。

`event` 额外字段：

- `topics`

## 事件对象

所有 hook 的 `event` 至少都有：

- `event.name`
- `event.ts`
- `event.cycle_id`
- `event.plugin_config`

补充说明：

- `topic_summary` 来自 `latest/unseen` 的主题摘要
- `topic` 来自 `get_topic()`
- `post` 来自 `get_topic()` 或 `get_posts_by_ids()`
- `event` 负载默认按只读约定使用；不要原地修改 `event.topics` / `event.topic_summary` / `event.topic` / `event.post` / `event.posts`
- `topic_summary` 中要特别关注：
  - `unseen`
  - `last_read_post_number`

## 内容型 hook 的触发规则

这部分很重要，插件行为经常会被这里影响。

定义：

- `entered_posts`
  - `get_topic()` 首屏返回的 `post_stream.posts`
- `backfill_posts`
  - `get_posts_by_ids()` 补拉回来的帖子

无爬取模式：

- 不开数据库
- 不补拉帖子
- 仅当 `topic_summary.unseen == true` 时触发内容型 hook
- `post.fetched` 逐条处理 `entered_posts`
- `topic.after_crawl.posts == entered_posts`

爬取模式：

- 会开数据库
- `entered_posts` 仅在 `unseen == true` 时参与内容型 hook
- `backfill_posts` 只要本轮实际拉到，就参与内容型 hook
- `backfill_posts` 会先按 `post_number` 排序
- `post.fetched` 顺序为：
  - `entered_posts(if unseen)` + `backfill_posts(sorted by post_number)`
- `topic.after_crawl.posts` 与上面的顺序一致

这意味着：

- 如果一个主题不是 `unseen`，插件通常不会重复处理它首屏已有的帖子
- 但在爬取模式下，真正补拉到的新帖子仍然会触发内容型 hook

## ctx API

约定上，插件应该通过 `ctx` 做动作与读写状态。

实现边界说明：

- 第一版插件运行在主进程内，没有 Python 级沙箱
- 因此这里的 `ctx` / 权限模型是运行时约束与接口约定，不是安全隔离
- `event` 里的对象也是主流程共享数据，不是隔离副本；即使技术上可以修改，也应视为只读输入
- 只应加载你自己信任的本地插件代码

### 基础字段

- `ctx.plugin_id`
- `ctx.config`
- `ctx.logger`
- `ctx.now()`

### 主题控制

`ctx.prioritize_topic(topic_id, score)`

- 需要权限：`topics.reorder`
- 只能在 `topics.fetched` 中调用
- 多个插件/多次调用会累加分数
- hook 成功返回后才会真正提交到本轮排序结果

`ctx.skip_topic(topic_id, reason="")`

- 需要权限：`topics.skip`
- 只能在 `topics.fetched` 或 `topic.before_enter` 中调用
- hook 成功返回后才会真正提交
- 主题会从本轮后续处理中移除

### 持久化状态

`ctx.get_kv(key, default=None)`

- 需要权限：`storage.read`

`ctx.set_kv(key, value)`

- 需要权限：`storage.write`
- 值会序列化存储

`ctx.was_done(key)`

- 需要权限：`storage.read`

`ctx.mark_done(key)`

- 需要权限：`storage.write`

### 自定义触发计数

`ctx.check_limit(key, daily_limit)`

- 需要权限：`storage.read`
- 检查 `trigger:<key>` 今日次数是否达到上限

`ctx.record_trigger(key)`

- 需要权限：`storage.write`
- 记录一次 `trigger:<key>` 今日次数

### 回复

`ctx.reply(topic_id, content, *, once_key=None, category=None)`

- 需要权限：`reply.create`
- 实际走现有队列客户端，所以继承节流、429、重试、CSRF 刷新
- 受 `limits.reply_per_day` 限制

返回值常见情况：

- 成功发送：`{"ok": true, "acted": true, ...}`
- 因 `once_key` 已存在被跳过：`{"ok": true, "acted": false, "reason": "once_key_exists"}`
- 因日限额超出失败：`{"ok": false, "reason": "daily_limit_exceeded"}`

建议：

- 任何“同类回复只想发一次”的场景都带 `once_key`

### 点赞

`ctx.like(post, *, emoji="heart")`

- 需要权限：`post.like`
- 只接受 `post`，不接受 `post_id`
- 实际走现有队列客户端，所以也继承节流、429、重试
- 受 `limits.like_per_day` 限制

轻量幂等规则：

- 仅当传入的 `post.current_user_reaction` 非空时直接跳过
- 这不是“同一轮 watch / 同一帖子”的强幂等保证
- 因为底层调用的是 `toggle_reaction`
- 如果同一帖子在同一轮里被多个插件、多个 hook，或同一插件重复调用 `ctx.like(...)`
- 后一次调用可能把前一次点赞切回去
- 如果你要做插件内“同帖只点赞一次”，需要自己用 `ctx.was_done(...)` / `ctx.mark_done(...)` 做去重

返回值常见情况：

- 成功点赞：`{"ok": true, "acted": true, ...}`
- 已有 reaction 被跳过：`{"ok": true, "acted": false, "reason": "already_reacted"}`
- 因日限额超出失败：`{"ok": false, "reason": "daily_limit_exceeded"}`

## 权限模型

第一版固定权限如下：

- `topics.reorder`
- `topics.skip`
- `reply.create`
- `post.like`
- `storage.read`
- `storage.write`

建议原则：

- 能不给就不给
- 只申请你真正需要的权限
- `storage.write` 通常意味着插件已经有状态副作用
- 权限检查只用于规范插件行为，不提供安全隔离
- 不要把第三方未知插件当作“被权限系统完全限制”的代码来运行

## 每日限额

动作限额由 `app.json` 控制，不由插件运行时临时决定：

```json
{
  "plugins": {
    "items": {
      "my_plugin": {
        "enabled": true,
        "limits": {
          "reply_per_day": 1,
          "like_per_day": 30
        }
      }
    }
  }
}
```

约定：

- `0` 表示不限制
- 计数按“插件 + 动作 + 日期”统计
- 回复和点赞分别计数

如果你还想限制插件自己的业务触发次数，可以自己用：

- `ctx.check_limit("my_rule", 5)`
- `ctx.record_trigger("my_rule")`

## 状态后端

无爬取模式：

- 插件状态是内存态
- 进程退出后消失
- 不开 SQLite

爬取模式：

- 插件状态落 SQLite
- `kv`、`once`、每日计数都会持久化

## 超时与失败熔断

每个 hook 都受超时和连续失败阈值控制：

```json
{
  "plugins": {
    "hook_timeout_secs": 10,
    "max_consecutive_failures": 3,
    "items": {
      "my_plugin": {
        "enabled": true,
        "hook_timeout_secs": 3,
        "max_consecutive_failures": 1
      }
    }
  }
}
```

规则：

- hook 超时算一次失败
- 抛异常也算一次失败
- 连续失败达到阈值后，插件会被自动禁用
- 之后的 hook 分发会跳过该插件

## 状态与调试

`discorsair status` 和 HTTP `GET /watch/status` 都会返回插件状态摘要。

你可以看到：

- `plugin_id`
- `hooks`
- `permissions`
- `limits`
- `disabled`
- `consecutive_failures`
- `hook_successes`
- `hook_failures`
- `hook_timeouts`
- `action_counts`
- `last_error`
- `last_error_at`
- `daily_counts`
- `once_mark_count`
- `kv_keys`

需要区分两种来源：

- `discorsair status`
  - 只读、静态
  - 不会导入或实例化插件代码
  - `runtime_live == false`
  - 运行态字段会是 `null`
- HTTP `GET /watch/status`
  - 来自正在运行的 watch 进程
  - `runtime_live == true`
  - 可以看到实时内存态，例如连续失败次数、hook 成功/失败次数、最后错误等

插件日志建议统一使用：

```python
ctx.logger.info("something happened: topic_id=%s", topic_id)
```

运行时本身也会记录结构化动作日志，例如：

- `prioritize_topic`
- `skip_topic`
- `reply`
- `like`
- `set_kv`
- `mark_done`

## 开发建议

建议这样分层：

- `on_topics_fetched`
  - 只做主题级决策
- `on_topic_after_enter`
  - 只做进入主题后的结构性判断
- `on_post_fetched`
  - 只做单帖规则
- `on_topic_after_crawl`
  - 做整批帖子分析和汇总动作

尽量避免：

- 在多个 hook 上对同一批帖子重复做重计算
- 不带 `once_key` 就直接回复
- 忽略 `current_user_reaction`
- 在插件里自己维护一套网络请求

## 一个更实际的例子

下面这个插件会：

- 标题命中关键词时优先处理主题
- 帖子点赞数达到阈值时点赞
- 本轮帖子里命中关键词时回帖一次

```python
def _text(value):
    return str(value or "").strip()


class Plugin:
    def on_topics_fetched(self, ctx, event):
        for topic in event.topics:
            title = _text(topic.get("title")).lower()
            if "python" in title:
                ctx.prioritize_topic(int(topic["id"]), 100)

    def on_post_fetched(self, ctx, event):
        post = event.post
        if not _text(post.get("cooked")):
            return
        if int(post.get("like_count", 0) or 0) >= 20:
            ctx.like(post)

    def on_topic_after_crawl(self, ctx, event):
        topic_id = int(event.topic_summary.get("id", 0) or 0)
        if topic_id <= 0:
            return
        text = "\n".join(_text(post.get("cooked")) for post in event.posts)
        if "help needed" not in text.lower():
            return
        ctx.reply(
            topic_id,
            "可以补充一下复现步骤和报错信息。",
            once_key=f"reply:{topic_id}:help-needed",
        )


def create_plugin():
    return Plugin()
```

## 参考

- 示例插件：[`plugins/sample_forum_ops/`](../plugins/sample_forum_ops/)
- 设计说明：[`docs/future-extensions.md`](./future-extensions.md)
