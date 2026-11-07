# transcriber

自用转写后端：上传/链接导入 → 预处理 → 双档 ASR 转写 → 分段 JSON、Markdown 逐字稿与纪要，带网页列表/提交/详情。

仅供内网或 Tailscale 使用，没有账号鉴权，禁止公网暴露。第 3 步规格见 docs/STEP3-SPEC.md，清单见 docs/STEP3-CHECKLIST.md。

## 启动

需要本机已有 `ffmpeg`、`ffprobe`，以及 uv。Python 用仓库里的 3.12 虚拟环境，不要往系统 Python 装包。

```bash
uv sync
./run.sh
```

`run.sh` 用 `uv run uvicorn` 启动，默认只听 `127.0.0.1:8900`。不要把这个端口绑到公网网卡，也不要加内网穿透。同一台机器上的 Tailscale 访问走本机或 tailnet 自己的限制，不要额外发布到公网。

浏览器打开 `http://127.0.0.1:8900/` 看任务列表，`/new` 提交，`/jobs/{id}/view` 看详情。JSON API 仍走 `/jobs`。

环境变量：

- `TRANSCRIBER_HOST`：监听地址，默认 `127.0.0.1`
- `TRANSCRIBER_PORT`：端口，默认 `8900`
- `TRANSCRIBER_DATA_DIR`：任务库和产物目录，默认 `./data`
- `TRANSCRIBER_LLM_PROVIDER`：纪要通道，四条之一：`nous`（默认）、`ollama`、`spark`、`custom`
- `TRANSCRIBER_LLM_API_KEY`：纪要接口密钥，也可写在 `~/.transcriber/transcriber.env`（nous/spark 必填；ollama/custom 不要求）
- `TRANSCRIBER_LLM_MODEL` / `TRANSCRIBER_LLM_BASE_URL`：覆盖通道表里的模型和地址，对**所有通道**生效

### 纪要通道

通道表在 `app/config.py` 的 `LLM_PROVIDERS`，四个通道都走 OpenAI 兼容接口：

| 通道 | 默认模型 | 默认地址 | 需要密钥 |
| --- | --- | --- | --- |
| `nous`（默认） | `stepfun/step-3.7-flash:free` | `https://inference-api.nousresearch.com/v1` | 是 |
| `ollama` | `qwen2.5:7b` | `http://127.0.0.1:11434/v1` | 否 |
| `spark` | `spark-x2.6` | `https://maas-api.cn-huabei-1.xf-yun.com/v2` | 是 |
| `custom` | 无默认（必填） | 无默认（必填） | 否 |

`custom` 用来接任意 OpenAI 兼容端点（讯飞、智谱、OpenRouter、本地网关等），没有默认地址与模型，必须自己填；不填会在纪要那一步报错（只影响纪要，已完成的转写不受影响），改好配置重启后重新生成纪要即可。地址必须是 `http`/`https` 且带主机名。

用 `custom` 的完整示例（写进 `~/.transcriber/transcriber.env`，或直接给进程环境）：

```bash
TRANSCRIBER_LLM_PROVIDER=custom
TRANSCRIBER_LLM_BASE_URL=https://api.example.com/v1
TRANSCRIBER_LLM_MODEL=magpie-chat
# 端点需要密钥时才填；不需要就整行不写（无密钥端点不会被拦）
TRANSCRIBER_LLM_API_KEY=sk-xxx
```

请求打到 `{BASE_URL 去掉尾部斜杠}/chat/completions`，`/v1` 这类后缀原样保留。

### 切回 ollama（两步回退）

`MODEL`/`BASE_URL` 是**对所有通道生效的全局覆盖**，地址不会随 provider 自动回落，所以只改 provider 不够：

1. `TRANSCRIBER_LLM_PROVIDER=ollama`
2. 把 `TRANSCRIBER_LLM_BASE_URL` 改回 ollama 的生效地址、`TRANSCRIBER_LLM_MODEL` 改回 ollama 模型（或两者都清空，回落到通道表默认）

然后重启。**Docker 部署要注意**：容器里的 ollama 是另一个容器，`BASE_URL` 必须显式写成容器地址 `http://ollama:11434/v1`，且**只能写进生产 secrets env_file**（`~/.transcriber/transcriber.env`）——`docker-compose.yml` 的 `environment:` 已不再出现这个键，宿主 shell 里 export 的同名变量**不会**进容器，唯一入口就是 env_file。「清空 `BASE_URL` 回落到默认」只适用于原生部署，在 Docker 下清空会回落到 `127.0.0.1:11434`，容器内连不上 ollama。

提交一条本地音频：

```bash
curl -s -F "file=@tests/fixtures/s2_60.wav" -F "tier=fast" http://127.0.0.1:8900/jobs
```

服务已经在跑时，用样本做一次 fast 冒烟（会打印逐字稿）：

```bash
uv run python scripts/smoke.py tests/fixtures/s2_60.wav
```

门禁：

```bash
uv run mypy app
uv run ruff check .
uv run pytest
```
