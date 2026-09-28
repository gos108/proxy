#!/usr/bin/env bash
# 本地/服务器定时更新 clash.yaml 的入口脚本（备选方案，配合 cron 使用）
#   crontab -e  ->  0 */3 * * * /var/www/html/guya/proxy/update.sh >> /var/log/clash-sub.log 2>&1
# 如需指定 pip 镜像：export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

if ! ./.venv/bin/python -c "import freeproxy, yaml, socks" >/dev/null 2>&1; then
  if ! ./.venv/bin/pip install -q -r requirements.txt; then
    echo "[warn] 默认源安装失败，改用清华镜像重试" >&2
    ./.venv/bin/pip install -q -r requirements.txt \
      -i "${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
  fi
fi

# 不传参数时使用与 GitHub Actions 一致的默认参数（含 HTTPS 隧道校验）
if [ "$#" -eq 0 ]; then
  set -- --output clash.yaml --max-http-delay 8000
fi

exec ./.venv/bin/python gen_clash.py "$@"
