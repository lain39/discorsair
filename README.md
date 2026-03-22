# Discorsair

Discourse 自动巡帖与信息流分析工具。

## 运行方式

CLI 命令名：`discorsair`

## CLI

- 常用命令：`run` / `watch` / `daily` / `like` / `reply` / `status` / `notify test` / `init` / `serve`
- `run` 和 `watch` 当前共用同一套 watch 循环实现与参数
- `status` / `daily` / `like` / `reply` / `notify test` 默认输出 JSON，便于脚本处理
- 详细命令说明、参数与输出示例见 `docs/cli.md`

## 配置

- 主配置：`config/app.json`
- 账号配置：`config/app.json` 内的 `auth`
- 模板参考：`config/app.json.template`
- 必填：`site.base_url`、`auth.cookie`
- 敏感字段支持环境变量覆盖：`DISCORSAIR_AUTH_COOKIE`、`DISCORSAIR_AUTH_NAME`、`DISCORSAIR_AUTH_KEY`、`DISCORSAIR_NOTIFY_URL`
- 存储：`storage.path`（SQLite，默认 `data/discorsair.db`）
- 存储隔离：`storage.auto_per_site`
- 按天分库：`storage.rotate_daily`
- 抓取：`crawl.enabled`（是否抓取帖子内容）
- 调试：`debug`（更详细日志）
- 日志文件：`logging.path`
- 请求节流：`request.min_interval_secs`（默认 1 秒）
- 未读优先：`watch.use_unseen`
- 队列：`queue.maxsize`
- 通知：`notify.enabled` + `notify.url` + `notify.chat_id`
- 通知自动已读：`notify.auto_mark_read`（默认关闭；当当前未读通知都已在本地去重状态中时，调用 mark-read 全部标记为已读）
- 通知前缀：`notify.prefix` / `notify.error_prefix`
- 服务：`discorsair serve` 启动 HTTP 控制服务
- 控制接口超时：`server.action_timeout_secs`（`0` 表示不设超时）
- 服务默认仅监听 `127.0.0.1`
- 如果 `server.host` 或 `--host` 使用非回环地址，必须配置 `server.api_key`
- 运行时只会在成功请求后写回 `auth.cookie` 中的 `_t`，不会持久化其他 cookie，也不会用空 cookie 覆盖配置
- `serve` 模式下如果遇到登录失效或 unresolved challenge，会停止 watch、关闭 HTTP 服务，并以非 0 退出

## 结构

- `config/` 配置
- `src/` 源码
- `docs/` 文档
- `tests/` 测试
- 架构说明：`docs/architecture.md`

## 备注

- Cookie 建议新建一个隐私窗口来获取，获取后关闭窗口，以免会话冲突导致 cookie 失效。首次导入时建议只保留 `_t`。
