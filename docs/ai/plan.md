
一、整体思路

freeproxy 抓取+验证免费代理 → 生成 Clash 格式订阅文件(YAML) → 托管到一个 URL(自动定时更新) → FlClash 添加该 URL 作为订阅

关键点：FlClash 用的是 Clash Meta 内核，原生支持 type: http / type: socks5 的普通代理节点，所以 freeproxy 抓到的 HTTP/SOCKS 免费代理可以直接转成 Clash 节点。"自动更新"则靠 GitHub Actions（或本地 cron）定时重新生成 YAML，FlClash 按订阅 URL 定时拉取。

二、生成 Clash 订阅的脚本

安装：pip install pyfreeproxy

gen_clash.py — 抓取免费代理并生成 Clash YAML
import yaml
from freeproxy.modules import BuildProxiedSession

SOURCES = ["ProxiflyProxiedSession", "HProxyProxiedSession",
           "KuaidailiProxiedSession", "ProxyScrapeProxiedSession",
           "OpenProxyListProxiedSession", "GeonodeProxiedSession"]
MAX_DELAY_MS = 3000   # 延迟过滤
TOP_N = 50            # 最终保留节点数

def fetch_all():
    seen, result = set(), []
    for src in SOURCES:
        try:
            sess = BuildProxiedSession({"max_pages": 1, "type": src, "disable_print": True})
            for p in sess.refreshproxies():
                ok, _ = p.selfcheck()
                if not ok: continue
                if p.protocol.lower() not in ("http", "https", "socks5"): continue
                key = (p.ip, p.port)
                if key in seen: continue
                seen.add(key)
                p.test_timeout = 5
                if p.tcp_connect_delay <= MAX_DELAY_MS:   # 顺手测活+测速
                    result.append(p)
        except Exception as e:
            print(f"[skip] {src}: {e}")
    return result

def to_clash(proxies):
    proxies.sort(key=lambda p: p.tcp_connect_delay)
    proxies = proxies[:TOP_N]
    nodes, names = [], []
    for p in proxies:
        name = f"{p.protocol}-{p.ip}:{p.port}"
        names.append(name)
        node = {"name": name, "server": p.ip, "port": int(p.port)}
        if p.protocol.lower() == "socks5":
            node["type"] = "socks5"; node["udp"] = True
        else:
            node["type"] = "http"
        nodes.append(node)
    return {
        "mixed-port": 7890, "mode": "rule", "allow-lan": False,
        "proxies": nodes,
        "proxy-groups": [{
            "name": "PROXY", "type": "url-test", "proxies": names,
            "url": "http://www.gstatic.com/generate_204",
            "interval": 300, "tolerance": 50,
        }],
        "rules": ["GEOIP,CN,DIRECT", "MATCH,PROXY"],
    }

if name == "main":
    cfg = to_clash(fetch_all())
    with open("clash.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    print(f"生成 {len(cfg['proxies'])} 个节点 -> clash.yaml")

说明：
url-test 分组会自动在节点间测速切换——免费代理死得快，这是必须的兜底。
很多免费 HTTP 代理不支持 CONNECT 隧道（走不了 HTTPS），FlClash 里连不上属正常，靠 url-test 淘汰即可；想更严可在脚本里用 p.http_connect_delay 做二次过滤。

三、实现"自动更新"（推荐 GitHub 方案）

Fork 你自己的仓库，把脚本放进去，加一个 workflow（该项目自己的 update-proxies.yml 就是每 3 小时跑一次，照抄思路即可）：

.github/workflows/update-clash.yml
name: update clash.yaml
on:
  schedule: [{cron: "0 */3 * * *"}]
  workflow_dispatch: {}
permissions: {contents: write}
jobs:
  update:
    runs-on: ubuntu-latest
    steps:
      uses: actions/checkout@v4
      uses: actions/setup-python@v5
        with: {python-version: "3.x"}
      run: pip install pyfreeproxy pyyaml
      run: python gen_clash.py
      run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+users.noreply.github.com"
          git diff --quiet clash.yaml || { git add clash.yaml; git commit -m "update proxies"; git push; }

然后在仓库 Settings → Pages 开启 GitHub Pages（选 master/main 分支根目录），得到固定地址：

https://<你的用户名>.github.io/<仓库名>/clash.yaml

备选：不想用 GitHub 就在自己服务器/NAS 上 cron 定时跑脚本 + python3 -m http.server 8080 提供下载，FlClash 订阅 http://<内网IP>:8080/clash.yaml。

四、FlClash 中配置

打开 FlClash → 订阅（机场） → 右上角 + → 粘贴上面的 clash.yaml URL → 确认导入。
订阅设置里打开自动更新，间隔建议设 180 分钟（和 Actions 的 3 小时对齐）。
启用该订阅，系统代理模式选"规则"，FlClash 会通过 mixed-port: 7890 接管流量，PROXY 分组自动 url-test 选最快存活节点。

五、必须知道的风险提示

免费公共代理速度差、寿命短（几小时级）、随时全灭，这套方案适合爬虫/临时用途，别指望稳定的"翻墙"体验。
安全警告：流量经过陌生人的服务器，可被窃听篡改。切勿在走这些代理时登录银行、邮箱等敏感账号，HTTPS 站点也别输入隐私信息。
在国内服务器跑抓取脚本时，部分境外代理源本身可能访问不了，可精简 SOURCES 保留国内源（快代理、齐云、IP89 等）。
