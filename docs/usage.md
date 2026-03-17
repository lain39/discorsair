# 使用说明（草案）

## 启动

- 命令：`discorsair`

## 配置

- `config/app.json` 写入站点与请求相关配置
- `config/app.json` 写入站点与账号配置
- `config/app.json.template` 作为模板参考
- `auth.proxy` 可设置代理（可留空）
- `auth.name` 账号标识（用于通知前缀）
- `request.user_agent` 为空时，运行期自动探测并填充（默认通过 `data:,`）
- `flaresolverr.ua_probe_url` 可省略，不填时默认使用 `data:,`
- `storage.path` 指定 SQLite 存储路径（默认 `data/discorsair.db`）
- `storage.auto_per_site` 按站点自动区分数据库文件
- `storage.rotate_daily` 按天分库（文件名追加日期）
- `crawl.enabled` 控制是否启用帖子内容抓取（默认 `true`）
- `debug` 启用详细日志（请求地址、请求头、响应等，敏感字段会脱敏）
- `logging.path` 日志文件路径（空表示不写文件）
- `request.min_interval_secs` 每次请求的最小间隔（默认 `1` 秒）
- `watch.use_unseen` 优先使用 `/unseen.json`（空则回退到 `/latest.json`）
- `watch.timings_per_topic` 每次刷多少楼层（默认 30）
- `queue.maxsize` 请求队列长度（0 为无限）
- `queue.timeout_secs` 队列任务超时时间
- `notify.enabled` 启用通知
- `notify.interval_secs` 通知轮询间隔（默认 600 秒）
- `notify.url` 通知接口地址（类似 Telegram `sendMessage`）
- `notify.chat_id` 通知目标
- `notify.prefix` 消息前缀（默认 `[Discorsair]`）
- `notify.error_prefix` 错误消息前缀（默认 `[Discorsair][error]`）
- `server.host` / `server.port` 监听地址
- `server.schedule` 运行时段（如 `08:00-12:00`）
- `server.auto_restart` watch 线程自动重启
- `server.restart_backoff_secs` 自动重启间隔
- `server.max_restarts` 最大重启次数（0 为不限制）
- `server.same_error_stop_threshold` 连续相同错误次数达到阈值后自动停止（0 为关闭）
- `server.api_key` HTTP 服务鉴权（为空则不启用）
- `time.timezone` 时区（用于今日统计与运行时段）
- 模板文件为 JSONC（允许 `//` 注释），程序也支持读取 JSONC

## FlareSolverr

- 需提前在 Docker 中部署 FlareSolverr
- 如果 `auth.proxy` 使用回环地址（如 `http://127.0.0.1:7890`），传给 FlareSolverr 的代理需要转换为 `http://host.docker.internal:7890`
- 该转换由 `src/core/` 处理
- `cf_clearance` 可按代理 IP 做本地缓存，下次同 IP 先尝试复用

## CLI

- 参见 `docs/cli.md`
