# 功能地图（转写后端 MVP + 纪要层）

本表是「功能 → 入口 → 关键状态与边界 → 验证名」的对照。改功能时先改这张表，再改代码；
验收时按本表的验证名去找证据。日期栏是该项最后一次实质变更的日期。

- 最近更新：2026-11-02
- 覆盖范围：`app/`（API + 网页 + worker + 引擎 + 纪要）与 `verify/`（试点验证脚本）
- 验证名口径：`verify/checks/*` 里的条目号（UI-EMPTY 等）与 `tests/` 里的 pytest 名

## 一、功能逐项

| # | 功能 | 入口 | 关键状态与边界 | 对应验证名 | 最近更新 |
| --- | --- | --- | --- | --- | --- |
| 1 | 上传导入 | `POST /jobs`（`multipart/form-data`，字段 `file` + `tier`）；网页 `POST /new`（`app/web.py`） | 落库即 `queued`；`tier` ∈ {`fast`,`precise`}，非法 400；空文件 400；超 `max_upload_bytes()`（默认 2GB）413；上传与链接同时提交 400；保存后交给 worker，接口不等转写 | `tests/test_api.py::test_b2_upload_reaches_done_and_lists_fields`、`::test_b5_oversize_upload_rejected`、`::test_invalid_tier_is_400`；`tests/test_pages.py::test_u2_new_form_creates_job_and_redirects`、`::test_w8_oversize_upload_lands_on_workbench`；`verify` UI-EMPTY、UI-WORKBENCH | 2026-11-02 |
| 2 | 链接导入 | `POST /jobs`（`application/json`，字段 `url` + `tier`）；网页同表单的 `url` 输入 | 落库即 `queued`；先过 `service.check_url_safety`（scheme 只收 http/https、拒 userinfo、解析出的地址必须是全局 IP、DNS 解析 5s 超时、并发解析上限 8），不安全 400 且不入库；下载失败的任务置 `error` 并留脱敏后的原因 | `tests/test_ssrf.py::test_ssrf_rejects_invalid_scheme_and_empty_host`、`::test_ssrf_rejects_userinfo`、`::test_ssrf_blocks_non_global_ips`、`::test_ssrf_mixed_public_private_resolution_rejected`、`::test_dns_resolution_timeout_returns_400`、`::test_dns_resolution_concurrency_never_exceeds_eight`、`::test_ssrf_public_ip_creates_job`、`::test_ssrf_via_api_and_web_routes`；`tests/test_api.py::test_b3_url_failure_becomes_error` | 2026-11-02 |
| 3 | 任务队列与状态 | worker 子进程 `app/worker.py::worker_main`（`store.claim_next()` / `store.claim_minutes()` 轮询 0.25s）；查询 `GET /jobs/{id}`（单条）、网页 `/` 账本（列表） | 状态机 `queued → running → done`，失败 `error`；`status` 与 `minutes_status`（`pending/running/done/error`）两条轴；任务串行（同一时刻只有一个 `running`）；不自动重试（`error` 不会被再次 claim）；worker 崩溃由 `WorkerSupervisor` 拉起并把遗留 `running` 置 `error`（「worker 进程异常退出，任务未完成」）；服务重启同样把 `running` 置 `error`；`created_at` 存 UTC、对外展示按 +8 时区 | `tests/test_api.py::test_b6_jobs_are_serial`、`::test_b7_restart_marks_running_as_error`、`::test_b21_killed_worker_does_not_kill_api`、`::test_error_job_is_not_retried`、`::test_f1_api_times_are_eight_hours_ahead_of_stored_utc`、`::test_list_recent_jobs_limit_clamp`；`verify` POLLING（轮询开停时序） | 2026-11-02 |
| 4 | 逐字稿 | `GET /jobs/{id}/transcript`（`transcript.md`）；`GET /jobs/{id}/segments`（`segments.json`，统一分段 `{start,end,speaker,text}`）；网页详情页 `/jobs/{id}/view` | 只有 `done` 可下载，否则 404 且带 `status`；快档 FunASR 出 speaker（SenseVoice-Small + fsmn-vad + cam++），精档 faster-whisper 输出经 OpenCC 繁→简、speaker 由 FunASR VAD+说话人时间段最大重叠贴上，无重叠保持 `null`；`transcript.md` 在 `speaker` 为空时不写说话人前缀 | `tests/test_api.py::test_b9_segments_schema`、`::test_b14_transcript_header_and_speaker_lines`、`::test_b14_precise_omits_speaker`、`::test_b15_download_before_done_is_not_500`、`::test_b15_download_after_done`；`tests/test_pages.py::test_u3_detail_has_minutes_and_speakers`；`verify` PLAYER-SYNC | 2026-11-02 |
| 5 | 纪要 | `GET /jobs/{id}/minutes`、`POST /jobs/{id}/minutes/regenerate`；worker 在 `done` 后接 `claim_minutes()` 生成 | `minutes_status` 独立于 `status`：转写完成后自动置 `pending` 并排队，失败单独置 `error` 且不影响逐字稿；`done` 前取纪要 404/409 而不是 500；`regenerate` 允许重跑；长逐字稿走 map-reduce 分段；provider 映射只写在 `app/config.py`；缺 key 报可读错误且不含密钥 | `tests/test_minutes.py::test_c2_minutes_generated_after_done`、`::test_c3_minutes_failure_isolated_then_regenerate`、`::test_c6_minutes_and_combined_order`、`::test_c6_minutes_before_done_is_not_500`、`::test_c5_long_transcript_map_reduce_call_count`、`::test_c1_provider_mapping_lives_only_in_config`；`tests/test_pages.py::test_pending_minutes_marks_the_workbench_live` | 2026-11-02 |
| 6 | 产物下载 | `GET /jobs/{id}/transcript`、`/segments`、`/minutes`、`/combined`（`combined.md`）、`/audio`（可选 `fmt=wav`） | 文件名只从 `ALLOWED_DOWNLOAD_FILENAMES` 白名单取，请求路径里的文件名不参与拼路径；解析后必须 `is_relative_to(job_dir)`，跨任务软链与越界软链一律 404；`/audio` 未 `done` 返回 409、`fmt` 只认 `wav` 或空；异常只记类型 + 脱敏摘要，不回堆栈 | `tests/test_download_security.py::test_download_endpoints_filenames_are_constant_whitelist`、`::test_download_cross_job_symlink_is_404`、`::test_download_jobs_root_traversal_symlink_is_404`、`::test_download_exception_logs_type_and_summary_without_traceback`；`tests/test_api.py::test_f1_audio_endpoint_range_and_full`、`::test_s1_audio_endpoint_symlink_traversal_rejected`、`::test_audio_not_done_returns_409_body_unchanged`；`tests/test_pages.py::test_u4_download_entries_include_combined` | 2026-11-02 |
| 7 | 详情页 / 播放器 | `GET /jobs/{id}/view`（`app/templates/detail.html`）、`/jobs/{id}/audio`；工作台 `/` 的轮询脚本 `app/static/app.js` | 页面只读 SQL 之外的数据、渲染时对文本转义；播放器双主题对齐 tokens，音频取不到时落到既有空态而不是报错条；工作台仅在有 `data-live-poll` 时轮询（初始 5s、失败指数退避上限 60s），全部完成后停轮询；删除按钮带确认 | `verify` PLAYER-BASIC、PLAYER-SYNC、PLAYER-EMPTY、UI-MOBILE、UI-FAILBANNER；`tests/test_player_playback_error.py::test_audio_error_shows_existing_empty_state`、`::test_play_rejection_shows_existing_empty_state`；`tests/test_pages.py::test_u7_processing_page_auto_refresh`、`::test_w7_idle_workbench_has_targets_without_polling`、`::test_w7_poll_whitelist_rejects_forbidden_writes`、`::test_u5_delete_button_has_confirm`；`tests/test_theme.py::test_theme_css_tokens_match_design_tokens`；`verify` TOKENS | 2026-11-02 |
| 8 | 健康检查 | `GET /health` | 只回 `{"ok": true}`，不查库、不碰引擎；供部署脚本与 `verify doctor` 探测 | `tests/test_api.py::test_b1_health`；`python -m verify.run doctor`（隔离实例 / 浏览器 / `/health` 三类结论，反例 exit 非 0） | 2026-11-02 |

## 二、高风险区（单列）

这四处出问题会直接变成安全事件或整机事故，改动时必须同时更新对应测试与 verify 检查。

| 风险区 | 代码位置 | 现有防线 | 验证方式 |
| --- | --- | --- | --- |
| 路径越界下载 | `app/main.py::_download`、`app/main.py::_serve_audio`、`app/service.py::minutes_payload`/`combined_payload` | 文件名只取 `ALLOWED_DOWNLOAD_FILENAMES` 白名单常量，请求侧文件名一律不拼路径；解析后要求 `is_relative_to(job_dir)`，且 `job_dir` 自己也要 `is_relative_to(root/jobs)`；`job_id` 先过 `^[0-9a-f]{32}$` | `uv run pytest tests/test_download_security.py tests/test_api.py -k "traversal or symlink or filename or audio_endpoint"`；断言覆盖跨任务软链、jobs 根越界软链、CRLF/`../` 文件名 |
| SSRF / 链接导入 | `app/service.py::check_url_safety`、`::_resolve_with_timeout`、`app/redact.py` | scheme 只收 http/https、拒 userinfo、`ipaddress.is_global` 过滤私有/回环/链路本地、DNS 5s 超时、并发解析 `BoundedSemaphore(8)`、失败原因脱敏 | `uv run pytest tests/test_ssrf.py`（含并发上限与闸满超时两例）、`tests/test_redact.py`；手工：`POST /jobs` 传 `http://127.0.0.1/...` 期望 400 |
| 脱敏读路径 | `app/redact.py`（全仓唯一实现）、`app/main.py` 两处 `except` 的日志、任务记录里的 `error`/`minutes_error` | `mask_url` 先结构化掩码 URL（query/fragment/userinfo 值），再关键词与长串规则；日志只写类型 + 摘要不写堆栈；历史任务读取路径同样过脱敏 | `uv run pytest tests/test_redact.py`（含历史任务回读与五处落点无密钥用例）；`uv run pytest tests/test_governance.py::test_b18_repo_has_no_secret_literals`；`python -m verify.run pack` 后 `verify/evidence_pack_check.py` 自检 |
| 并发闸 | `app/service.py::_DNS_RESOLUTION_GATE`（DNS 侧）；`app/jobs.py::claim_next`/`claim_minutes`（任务侧串行）；`app/worker.py::WorkerSupervisor`（单 worker 子进程） | DNS 解析并发上限 8、闸满直接超时 400；任务侧靠 SQLite 原子 `UPDATE ... WHERE status='queued'` 保证同一任务只被 claim 一次、且整体串行；worker 与 API 进程隔离，worker 崩溃不影响 API | `uv run pytest tests/test_ssrf.py -k "concurrency or gate or not_blocked"`、`tests/test_api.py::test_b6_jobs_are_serial`、`::test_b21_killed_worker_does_not_kill_api`、`tests/test_stress.py`；任务串行人工复核：连投两条后 `GET /jobs` 观察同一时刻只有一个 `running` |

## 三、验证命令入口

```bash
uv run mypy app          # 类型
uv run ruff check .      # lint
uv run pytest            # 测试：全绿，收集数与 AGENTS.md「测试基线」一致
uv run --group verify python -m verify.run doctor       # 隔离实例 / 浏览器 / health 体检
uv run --group verify python -m verify.run pack         # 九条 UI/行为证据包
```

`verify` 子命令的完整清单与 CLI 约定见 `verify/README.md`。
