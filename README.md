# Discorsair

Discourse 自动巡帖与信息流分析工具。

## 运行方式

CLI 命令名：`discorsair`

## CLI

- `discorsair run`：默认等价于 `watch`
- `discorsair watch`：持续拉取最新主题
- `discorsair daily`：从最新列表选 `unseen` 的帖子阅读并上报 timings
- `discorsair like --post <id> --emoji heart`
- `discorsair reply --topic <id> --raw "text"`
- `run/watch` 支持 `--max-posts-per-interval` 控制每轮抓取上限
- `discorsair status`
- `discorsair notify test`
- `discorsair init --path config/app.json`

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

## 结构

- `config/` 配置
- `src/` 源码
- `docs/` 文档
- `tests/` 测试

## 备注

- Cookie 建议新建一个隐私窗口来获取，获取后关闭窗口，以免会话冲突导致 cookie 失效。
