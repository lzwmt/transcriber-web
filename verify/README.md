# verify —— 转写试点验证脚本包

在仓库根目录运行（Playwright 只在 `verify` 依赖分组）：

```
uv run --group verify python -m verify.run <子命令>
```

## 前置依赖

- 宿主机自带 **ffmpeg**（造数用 lavfi 正弦波短音频，verify 不负责安装）。
- **Playwright chromium**（脚本只查 `INSTALLATION_COMPLETE`，不在 Python 里下载）。解析顺序：仓库根 `.verify-browsers/` 优先，宿主 `~/.cache/ms-playwright` 兜底；命中哪处就把 `PLAYWRIGHT_BROWSERS_PATH` 指到哪处。宿主缓存会被磁盘清理删掉，整包不应只靠它，项目自带目录是首选。两处都缺时停下，并在报错里给出两条路径。补装（只写到项目目录）：

  ```
  PLAYWRIGHT_BROWSERS_PATH="$PWD/.verify-browsers" uv run --group verify playwright install chromium
  ```

  若没有随装 `chromium_headless_shell`，同一环境变量再执行 `playwright install chromium-headless-shell`。

## 子命令一览

| 子命令 | 作用 |
| --- | --- |
| `self-check` | 起停隔离实例空跑，并核对三类造数（空实例 / 多状态 / 24h 内外失败） |
| `v2a` | 实跑 UI-EMPTY 与 UI-WORKBENCH 两查 |
| `v2b` | 实跑 UI-FAILBANNER 与 UI-MOBILE 两查 |
| `polling` | 实跑 POLLING 轮询时序查 |
| `tokens` | 实跑 TOKENS 变量对账与非变量实测 |
| `player-basic` | 实跑 PLAYER-BASIC 播放器双主题对齐 |
| `player-sync` | 实跑 PLAYER-SYNC 逐字稿联动 |
| `player-empty` | 实跑 PLAYER-EMPTY 无音频占位态 |
| `player` | 实跑 PLAYER-BASIC、PLAYER-SYNC、PLAYER-EMPTY |
| `pack` | 一键实跑全部九查，组装证据包到 `verify-out/<pack_id>/` 并自检 |
| `doctor` | 体检隔离实例起停、浏览器（`.verify-browsers` 优先）、`/health` 三类结论 |
| `seed` | 显式跑三类造数（空实例 / 多状态 / 24h 内外失败）并报告造了什么 |
| `snapshot` | 对隔离实例取可访问性快照 + 整页截图，默认页面 `/` |
| `wait-settle` | 等首页轮询计数稳定（1s 间隔、连续 2 次一致、默认 30s 超时） |
| `flag <name> on\|off` | 带 `TRANSCRIBER_FEAT_<NAME>` 重起隔离实例并回读 `/proc/<pid>/environ` |

单条证据默认写到 `verify-out/items/<条目号>/`；每个子命令结束会清理隔离实例与临时目录。

## CLI 约定（LZW-56 A8）

- 每个子命令在 `verify/run.py` 里有一条**显式分发分支**；分支走完落到兜底 `return 2`
  并打一行错误，不会静默去跑别的检查。未知子命令仍由 argparse `exit 2`。
- 五个新子命令（`doctor`、`seed`、`snapshot`、`wait-settle`、`flag`）首行输出
  `VERIFY-CMD <name>`，便于从日志确认跑的是哪条。
- 退出码：`0` 通过；`1` 体检/取证失败（`doctor`、`snapshot`、`flag`）；`2` 分发兜底或
  参数错误；`3` `wait-settle` 超时（同时打印最后一次快照计数）。

## doctor（体检）

三类结论，任一项失败即 exit 1 并在末行「失败项」点名：

| 项 | 查什么 | 相关参数 |
| --- | --- | --- |
| 隔离实例 | 端口从 8911 起避让生产 8900、只监听回环、`/health` 返回 `{"ok":true}`、停止后进程/临时目录/端口不留残 | 无 |
| 浏览器 | `chromium-<revision>` 与 `chromium_headless_shell-<revision>` 的 `INSTALLATION_COMPLETE`；默认 `.verify-browsers` 优先、宿主缓存兜底 | `--browsers <目录>` 强制只看该目录（反例取证用） |
| `/health` | 目标地址 200 且响应体 `ok=true`；默认打隔离实例自身 | `--health-url <url>` 换目标（反例取证用） |

报告落盘 `verify-out/doctor-<UTC>.txt`（同秒重跑加 `-2` 后缀），内容与标准输出一致。
反例冒烟：`--browsers /tmp/不存在` 与 `--health-url http://127.0.0.1:<未监听端口>/health`
都必须 exit 非 0 且指明对应失败项。

## snapshot / wait-settle / flag 口径

- `snapshot [--page <路径>]`：默认 `/`，`--page` 可指定任意站内路径；产物目录
  `verify-out/snapshot-<UTC>/`，含 `page.txt`、`aria.txt`（可访问性快照）、`shot.png`、
  `meta.json`（页面、最终 URL、HTTP 状态、标题、字节数）。
- `wait-settle [--page] [--interval] [--stable-reads] [--timeout]`：默认 1s 间隔、连续 2 次
  读数一致判稳、30s 超时；超时 exit 3 并打印最后一次快照计数。计数逻辑**只 import**
  `verify/checks/polling.py` 里的函数，不改其源文件。
- `flag <name> on|off`：隔离实例在 `Popen(env=...)` 时确定环境，运行中改不了开关，
  所以语义是「带目标变量重起隔离实例」；起好后回读 `/proc/<pid>/environ` 校验
  `TRANSCRIBER_FEAT_<NAME>` 是否与所设一致（`on`=1、`off`=未设）。只碰隔离实例，
  绝不碰生产 8900 与生产容器；生产侧的翻开关前后对比由协调者验收时做。

## pack 证据包

- `pack_id` 格式 `verify-pilot-<UTC yyyymmddhhmm>`；同一分钟重跑自动加 `-2` 等后缀，保证各次产物独立。
- 包内：`items/` 九个条目 + `results.md` + `manifest.json`（evidence-pack/1 口径），组装后自动用入仓版 `verify/evidence_pack_check.py` 自检，退出码 0 才算成包。
- 九条：UI-EMPTY、UI-WORKBENCH、UI-FAILBANNER、UI-MOBILE、POLLING、TOKENS、PLAYER-BASIC、PLAYER-SYNC、PLAYER-EMPTY。
- 对应表：UI-EMPTY→V2a、UI-WORKBENCH→V2a、UI-FAILBANNER→V2b、UI-MOBILE→V2b、POLLING→V3、TOKENS→V4、PLAYER-BASIC / PLAYER-SYNC / PLAYER-EMPTY→player；条目 type：UI-* 与 PLAYER-BASIC、PLAYER-EMPTY 为 `ui`，POLLING、TOKENS、PLAYER-SYNC 为 `behavior`。
- 校验脚本也可单独复跑：`uv run python verify/evidence_pack_check.py verify-out/<pack_id>`（exit 0 通过）。

## UI-FAILBANNER 视口说明

该查用 **1280×360** 视口：1280×800 下失败条页面不溢出、点击提示条后 `scrollY` 恒为 0，「scrollY 增大」「上传区 top 落入 [0, 96]」的数值判据无法成立；压到 360 高后页面可滚动，判据才有意义。
