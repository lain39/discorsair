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
- 存储：`storage.path`（SQLite，默认 `data/discorsair.db`）
- 存储隔离：`storage.auto_per_site`
- 按天分库：`storage.rotate_daily`
- 抓取：`crawl.enabled`（是否抓取帖子内容）
- 调试：`debug`（更详细日志）
- 日志文件：`logging.path`
- 请求节流：`request.min_interval_secs`（默认 1 秒）
- 未读优先：`watch.use_unseen`
- 队列：`queue.maxsize` / `queue.timeout_secs`
- 通知：`notify.enabled` + `notify.url` + `notify.chat_id`
- 通知前缀：`notify.prefix` / `notify.error_prefix`
- 服务：`discorsair serve` 启动 HTTP 控制服务
- 服务默认仅监听 `127.0.0.1`
- 如果 `server.host` 或 `--host` 使用非回环地址，必须配置 `server.api_key`
- 运行时只会在成功请求后写回 `auth.cookie`，不会用空 cookie 覆盖配置

## 结构

- `config/` 配置
- `src/` 源码
- `docs/` 文档
- `tests/` 测试
- 架构说明：`docs/architecture.md`

## 备注

- Cookie 建议新建一个隐私窗口来获取，获取后关闭窗口，以免会话冲突导致 cookie 失效。并建议第一次导入的时候删掉'_t'之外的Cookie， 不然无法过盾。
- `request.user_agent` 为空时，如需通过 FlareSolverr 探测 UA，探测请求不会携带现有 cookie，也不会持久化 probe 返回的 cookie。
