# proxy —— 免费代理 → Clash / FlClash 订阅

抓取公开的免费 HTTP / SOCKS5 代理，逐级校验（存活 → HTTPS 隧道 → AI 服务连通性）并打质量分，
生成 FlClash（Clash Meta 内核）可直接订阅的 `clash.yaml`，由 GitHub Actions 每 2 小时自动更新。

**订阅地址**

```
https://gos108.github.io/proxy/clash.yaml
```

> 公开仓库 + GitHub Pages，无需登录可直接导入 FlClash。刷新不出新版时加个查询串绕过 CDN：
> `https://gos108.github.io/proxy/clash.yaml?v=2`

---

## 特性

- **多源抓取**：基于 [`pyfreeproxy`](https://github.com/CharlesPikachu/freeproxy)，默认 6 个源，每个源跑独立子进程 + 硬超时（部分源会做大量 IP 归属地查询而挂死，单源失败只跳过自己）。
- **三级校验**：TCP 存活与延迟 → HTTP(S) CONNECT 隧道（免费 HTTP 代理大多不支持，必须淘汰）→ AI 服务连通性探测。
- **质量评分**：0–100 分写进节点名，选点按分数排序。
- **按国家/地区分组**：`🇺🇸 美国`、`🇭🇰 中国香港` … 每组独立 `url-test`，可手动锁定地区。
- **AI 服务专用分组**：`🤖 ChatGPT` / `🎭 Claude` / `✨ Gemini`，并把对应域名写进 rules 自动分流。
- **自动更新**：GitHub Actions 每 2 小时重新生成并提交；本地 cron 亦可（`update.sh`）。
- **不会清空订阅**：一个节点都没抓到时不覆盖已有 `clash.yaml`（退出码 1）。

## 订阅结构

```
PROXY (select)                 ← 规则走这里；默认选中「♻️ 自动选择」，也可手动锁定地区/AI 分组
├── ♻️ 自动选择 (url-test)      ← 全部节点，自动选最快
├── 🤖 ChatGPT (url-test)      ← 实测能连通对应 AI 服务的节点
├── 🎭 Claude  (url-test)
├── ✨ Gemini  (url-test)
├── 🇺🇸 美国 / 🇭🇰 中国香港 / 🇯🇵 日本 … (url-test)   ← 各地区节点
└── DIRECT

rules:
  - DOMAIN-SUFFIX,openai.com,🤖 ChatGPT
  - DOMAIN-SUFFIX,claude.ai,🎭 Claude
  - DOMAIN-SUFFIX,generativelanguage.googleapis.com,✨ Gemini
  - GEOIP,CN,DIRECT
  - MATCH,PROXY
```

**节点名**：`🇺🇸 US-01 · 95 🤖`

| 部分 | 含义 |
|---|---|
| `🇺🇸 US-01` | 地区国旗 + 国家码 + 组内序号（组内按质量分排序） |
| `95` | 质量分（0–100） |
| `🤖` / `💬` | AI 服务全通 / 仅部分通（都不通则不显示标记） |

用 `--name-style region` 可只保留 `🇺🇸 US-01`，用 `--name-with-ip` 可追加 `ip:port`。

## 质量评分

```
score = 40 × (1 − TCP延迟 / --max-delay)
      + 40 × (1 − HTTP(S)延迟 / --max-http-delay)     # 未开 --max-http-delay 时该项权重并入 TCP（70/0/30）
      + 20 × AI 服务通过比例
```

分数只用于**生成时刻**的排序与挑选，FlClash 里的延迟数字仍是客户端实测值。

## AI 连通性探测

走代理（而不是从本机）请求各家的官方 API，拿到 `< 500` 的响应即视为可达，`403 + unsupported_country` 判为不可用：

| 服务 | 探测端点 |
|---|---|
| 🤖 ChatGPT | `https://api.openai.com/v1/models` |
| 🎭 Claude | `https://api.anthropic.com/v1/messages` |
| ✨ Gemini | `https://generativelanguage.googleapis.com/v1beta/models` |

只探测延迟较优的候选（`--ai-probe-limit`，默认 150）以控制耗时；
没有节点命中某个服务时，对应的分组与 rules 都不会生成（否则 mihomo 会因引用不存在的分组报错）。

> 标记是生成时刻的快照：FlClash 每 180 秒只重测延迟，不重测 AI 连通性，标记最长可能陈旧约 2 小时（下一次订阅刷新）。

## 快速开始

```bash
cd /var/www/html/guya/proxy
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt        # 国内可加 -i https://pypi.tuna.tsinghua.edu.cn/simple
./.venv/bin/python gen_clash.py                    # 结果写入 ./clash.yaml
```

或直接用封装脚本（自动建 venv、装依赖，默认参数与 CI 一致）：

```bash
./update.sh                                        # 等价于 --output clash.yaml --max-http-delay 8000
./update.sh --sources cn --max-http-delay 8000     # 服务器在国内时更稳
```

需要 Python 3.10+。

## 部署

### 方案 A：GitHub Actions 自动更新（推荐，当前使用）

`.github/workflows/update-clash.yml` 每 2 小时（`0 */2 * * *`）抓取一次，生成后自动 commit + push；
也支持 `push` 到 `main`（忽略 `clash.yaml`，避免自循环）和手动 `workflow_dispatch` 触发。

启用 GitHub Pages：仓库 `Settings → Pages → Source: Deploy from a branch → main / (root)`。

> 公开仓库的 Actions 标准 runner 分钟数不限；私有仓库 Pages 需要付费套餐。

### 方案 B：本地 / 服务器 cron

```bash
crontab -e
0 */2 * * * /var/www/html/guya/proxy/update.sh >> /var/log/clash-sub.log 2>&1
```

再用任意静态服务对外提供 `clash.yaml`：

```bash
python3 -m http.server 8080 -d /var/www/html/guya/proxy
```

订阅地址即 `http://<服务器IP>:8080/clash.yaml`。

## FlClash 配置

1. 订阅 → 右上角 `+` → 粘贴 `https://gos108.github.io/proxy/clash.yaml` → 确认导入。
2. 打开该订阅的自动更新，间隔填 **120 分钟**（与 Actions 的 2 小时对齐）。
3. 启用订阅，系统代理模式选「规则」。
4. 想手动指定地区：在 `PROXY` 组里点 `🇯🇵 日本` 等分组；想全自动就保持 `♻️ 自动选择`。

## 参数速查

| 参数 | 默认 | 说明 |
|---|---|---|
| `--sources` | `default` | 源预设 `default`/`cn`/`all`，或逗号分隔的会话类名 |
| `--max-delay` | `3000` | TCP 延迟上限（毫秒） |
| `--max-http-delay` | `0` | HTTP(S) 延迟上限，`0` 表示跳过 HTTPS 隧道校验 |
| `--top-n` | `80` | 最终节点总数上限 |
| `--per-region-top` | `6` | 单个国家/地区最多保留节点数 |
| `--exclude-regions` | `CN` | 丢弃的地区码（国内节点延迟最低会被 url-test 长期选中，却没有出境能力） |
| `--name-style` | `full` | `region` / `score` / `full`（是否带质量分与 AI 标记） |
| `--name-with-ip` | 关 | 节点名追加 `ip:port` |
| `--ai-services` | `chatgpt,claude,gemini` | 要探测的 AI 服务，`none` 关闭 |
| `--ai-probe-limit` | `150` | 最多探测多少个候选节点 |
| `--ai-timeout` | `8.0` | AI 探测超时（秒） |
| `--no-region-groups` | 关 | 退回「单个 PROXY url-test」结构 |
| `--source-timeout` | `180` | 单源抓取硬超时（秒） |
| `--jobs` / `--workers` | `8` / `100` | 并发抓取源数 / 并发测活数 |
| `--timeout` | `5.0` | 单节点测活超时（秒） |
| `--interval` / `--tolerance` | `180` / `50` | url-test 测速间隔（秒）/ 切换容差（毫秒） |
| `--output` / `--port` | `clash.yaml` / `7890` | 输出路径 / mixed-port |
| `--keep-empty` | 关 | 没抓到节点时也写出空订阅（默认保留旧文件并退出码 1） |

`./.venv/bin/python gen_clash.py --help` 可查看全部参数。

## 常见问题

- **FlClash 里还是上一版**：Pages 有 10 分钟 CDN 缓存，把订阅地址改成 `...clash.yaml?v=2`（数字随便换）。
- **节点全是 Timeout**：免费代理寿命就是小时级，等下一次自动更新，或在 FlClash 里对 `♻️ 自动选择` 触发一次延迟测试。
- **某些 HTTP 节点连不上 HTTPS 站点**：`--max-http-delay` 就是为淘汰这类节点而生的，去掉该参数会放进大量"能连不能用"的节点。
- **国内服务器抓不到源**：用 `--sources cn`（快代理/IP89/齐云等），境外源本身可能访问不了。
- **CI 时间**：单次约 5–7 分钟，其中大头是某个源必然超时（`--source-timeout`），属预期行为。
- **推送被拒（non-fast-forward）**：bot 每 2 小时会往 `main` 提交 `clash.yaml`，本地改完先 `git pull --rebase origin main` 再 push。

## 风险提示

- 免费公共代理速度差、寿命短（小时级）、随时全灭，只适合爬虫 / 临时用途，**不要指望稳定体验**。
- 流量经过陌生人的服务器，**可能被窃听或篡改**：切勿在这些代理下登录银行、邮箱等敏感账号，HTTPS 站点也别输入隐私信息。
- 本仓库与 Pages 是公开的，订阅地址任何人拿到都能使用你的节点列表。

## 目录

| 路径 | 说明 |
|---|---|
| `gen_clash.py` | 抓取 + 校验 + 生成订阅的主脚本 |
| `clash.yaml` | 生成的订阅文件（由 Actions/脚本覆盖，勿手工改） |
| `update.sh` | 本地 cron 入口（自动建 venv、装依赖） |
| `requirements.txt` | pyfreeproxy / PyYAML / PySocks |
| `.github/workflows/update-clash.yml` | 每 2 小时自动更新 |
| `docs/ai/plan.md` | 最初的方案设计文档 |
