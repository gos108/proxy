#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_clash.py —— 抓取免费代理 → 校验存活 → 生成 Clash(Meta) 订阅 YAML

流程（对应 docs/ai/plan.md）:
    1) 用 pyfreeproxy 的各个 ProxiedSession 抓取免费 HTTP/SOCKS5 代理
    2) 协议过滤 / 去重 / 剔除内网地址 → 设置短超时 → 并行 TCP 存活+延迟测试 → 按延迟排序取 TOP N
    3) 输出 FlClash(Clash Meta 内核) 可直接订阅的 YAML，PROXY 分组用 url-test 自动选出最快存活节点

用法:
    python gen_clash.py                                # 默认源 -> clash.yaml
    python gen_clash.py --sources cn                   # 仅国内源（服务器在国内时更稳）
    python gen_clash.py --sources all --jobs 6         # 使用全部内置源
    python gen_clash.py --max-delay 2000 --top-n 80    # 更严格的延迟过滤 / 保留更多节点
    python gen_clash.py --max-http-delay 5000          # 额外校验 HTTPS(CONNECT) 隧道能力
    python gen_clash.py --output /var/www/html/guya/proxy/clash.yaml

说明:
    * 每个代理源在独立子进程里抓取并有硬超时（部分源会做大量 IP 归属地查询，很容易挂住），
      单源超时/失败只跳过该源，不影响整体生成。
    * 很多免费 HTTP 代理不支持 CONNECT 隧道（走不了 HTTPS），加 --max-http-delay 可提前淘汰它们。
    * 最终一个节点都没有时不会覆盖已有 clash.yaml（退出码 1），避免把线上订阅清空。
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

try:
    import yaml
    from freeproxy.modules import BuildProxiedSession, ProxiedSessionBuilder, ProxyInfo
except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确提示
    sys.exit(f"缺少依赖（{exc}），请先执行: pip install -r requirements.txt")


# ---------------------------------------------------------------- 配置默认值

# 计划里给出的 6 个源
DEFAULT_SOURCES = [
    "ProxiflyProxiedSession",
    "HProxyProxiedSession",
    "KuaidailiProxiedSession",
    "ProxyScrapeProxiedSession",
    "OpenProxyListProxiedSession",
    "GeonodeProxiedSession",
]

# 国内服务器优先使用（境外源本身可能抓不到）
CN_SOURCES = [
    "KuaidailiProxiedSession",
    "IP89ProxiedSession",
    "QiyunipProxiedSession",
    "KxdailiProxiedSession",
    "IP3366ProxiedSession",
    "SixSixDailiProxiedSession",
]

SOURCE_PRESETS = {
    "default": DEFAULT_SOURCES,
    "cn": CN_SOURCES,
    "all": list(ProxiedSessionBuilder.REGISTERED_MODULES.keys()),
}

ALLOWED_PROTOCOLS = ("http", "https", "socks5")  # Clash 的 type: http 同时支持 http/https，另有 socks5
HEALTH_CHECK_URL = "http://www.gstatic.com/generate_204"
HTTPS_TEST_URL = "https://www.baidu.com"
JSON_MARK_START = "###GEN_CLASH_JSON_START###"
JSON_MARK_END = "###GEN_CLASH_JSON_END###"


# ---------------------------------------------------------------- 工具函数

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抓取免费代理并生成 Clash(Meta)/FlClash 订阅 YAML",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--sources", default="default",
                        help="源预设(default/cn/all) 或逗号分隔的会话类名，如 "
                             "KuaidailiProxiedSession,IP89ProxiedSession")
    parser.add_argument("--max-delay", type=int, default=3000, help="TCP 连接延迟上限(毫秒)")
    parser.add_argument("--max-http-delay", type=int, default=0,
                        help="HTTP/HTTPS 请求延迟上限(毫秒)，0 表示不做该项校验")
    parser.add_argument("--top-n", type=int, default=50, help="最终保留的节点数")
    parser.add_argument("--max-pages", type=int, default=1, help="每个源的翻页数")
    parser.add_argument("--per-source-limit", type=int, default=300,
                        help="单个源最多保留的候选数，0 表示不限制")
    parser.add_argument("--source-timeout", type=int, default=180, help="单个源的抓取超时(秒)")
    parser.add_argument("--timeout", type=float, default=5.0, help="单个节点测活超时(秒)")
    parser.add_argument("--jobs", type=int, default=8, help="并发抓取的源数量")
    parser.add_argument("--workers", type=int, default=100, help="节点测活并发数")
    parser.add_argument("--output", default="clash.yaml", help="输出的 Clash 配置文件路径")
    parser.add_argument("--port", type=int, default=7890, help="Clash 的 mixed-port")
    parser.add_argument("--test-url", default=HEALTH_CHECK_URL, help="PROXY 分组测速地址")
    parser.add_argument("--interval", type=int, default=180, help="PROXY 分组测速间隔(秒)")
    parser.add_argument("--tolerance", type=int, default=50, help="PROXY 分组切换容差(毫秒)")
    parser.add_argument("--keep-empty", action="store_true",
                        help="即使一个节点都没抓到也写出文件（默认不覆盖旧文件并退出码 1）")
    # 子进程模式（由主进程调用，等价于“只抓某一个源并把结果以 JSON 打印到 stdout”）
    parser.add_argument("--worker-source", default=None, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def resolve_sources(value: str) -> list[str]:
    """把 --sources 解析成具体的会话类名列表"""
    key = (value or "default").strip()
    if key in SOURCE_PRESETS:
        return list(SOURCE_PRESETS[key])
    registered = ProxiedSessionBuilder.REGISTERED_MODULES
    names = [item.strip() for item in key.split(",") if item.strip()]
    valid = [name for name in names if name in registered]
    for name in names:
        if name not in registered:
            log(f"[warn] 未知源，已忽略: {name}")
    return valid


# ---------------------------------------------------------------- 抓取

def worker_main(name: str, args: argparse.Namespace) -> int:
    """子进程：抓取单个源，把 ProxyInfo 列表以 JSON 输出到 stdout"""
    session = BuildProxiedSession({
        "max_pages": args.max_pages,
        "type": name,
        "disable_print": True,
        # 只在协议层面过滤（不在此处配 max_tcp_ms，避免用 ProxyInfo 默认的 60s 超时测活）
        "filter_rule": {"protocol": list(ALLOWED_PROTOCOLS)},
    })
    proxies = list(session.refreshproxies() or [])
    if args.per_source_limit and args.per_source_limit > 0:
        proxies = proxies[:args.per_source_limit]
    data = json.dumps([proxy.todict() for proxy in proxies], ensure_ascii=False)
    print(JSON_MARK_START, flush=True)
    print(data, flush=True)
    print(JSON_MARK_END, flush=True)
    return 0


def parse_worker_output(stdout: str) -> list:
    if JSON_MARK_START not in stdout or JSON_MARK_END not in stdout:
        return []
    payload = stdout.split(JSON_MARK_START, 1)[1].split(JSON_MARK_END, 1)[0].strip()
    try:
        return [ProxyInfo.fromdict(item) for item in json.loads(payload)]
    except Exception:
        return []


def fetch_source(name: str, args: argparse.Namespace) -> tuple[str, list, str]:
    """在独立子进程里抓取单个源（带硬超时），返回 (源名, 代理列表, 错误信息)"""
    started = time.monotonic()
    cmd = [
        sys.executable, os.path.abspath(__file__),
        "--worker-source", name,
        "--max-pages", str(args.max_pages),
        "--per-source-limit", str(args.per_source_limit),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=args.source_timeout)
    except subprocess.TimeoutExpired:
        return name, [], f"抓取超时(>{args.source_timeout}s)"
    except Exception as exc:
        return name, [], f"{type(exc).__name__}: {exc}"
    finally:
        log(f"  {name}: 用时 {time.monotonic() - started:.1f}s")

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return name, [], f"退出码 {proc.returncode}: {tail[-1][:160] if tail else ''}"
    proxies = parse_worker_output(proc.stdout or "")
    if not proxies and JSON_MARK_START not in (proc.stdout or ""):
        return name, [], "未取到结果输出"
    return name, proxies, ""


def fetch_all(args: argparse.Namespace) -> list:
    sources = resolve_sources(args.sources)
    if not sources:
        sys.exit("没有可用的代理源，请检查 --sources 参数")
    log(f"开始抓取 {len(sources)} 个源（单源超时 {args.source_timeout}s）: {', '.join(sources)}")

    proxies, stats = [], []
    with ThreadPoolExecutor(max_workers=max(1, min(args.jobs, len(sources)))) as executor:
        futures = [executor.submit(fetch_source, name, args) for name in sources]
        for future in as_completed(futures):
            name, result, error = future.result()
            stats.append((name, len(result), error))
            if error:
                log(f"[skip] {name}: {error}")
            else:
                log(f"[ok]   {name}: {len(result)} 个候选")
            proxies.extend(result)

    log("抓取结果统计:")
    for name, count, error in sorted(stats, key=lambda item: item[0]):
        flag = "FAIL" if error else "OK  "
        suffix = f"  ({error})" if error else ""
        log(f"  {flag} {name:<32}{count:>6} 个{suffix}")
    return proxies


# ---------------------------------------------------------------- 校验

def dedupe_and_normalize(proxies: list, args: argparse.Namespace) -> list:
    """协议校验 + 去重 + 剔除内网/非法地址，并统一设置短超时"""
    seen, result, dropped = set(), [], 0
    for proxy in proxies:
        protocol = (getattr(proxy, "protocol", "") or "").lower()
        if protocol not in ALLOWED_PROTOCOLS:
            dropped += 1
            continue
        try:
            ip_obj = ipaddress.ip_address(proxy.ip)
            port = int(float(proxy.port))
        except (ValueError, TypeError):
            dropped += 1
            continue
        if not ip_obj.is_global or not 1 <= port <= 65535:
            dropped += 1
            continue
        key = (proxy.ip, port)
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        proxy.protocol, proxy.port = protocol, str(port)
        proxy.test_timeout = args.timeout  # 关键：默认 60s 太慢
        result.append(proxy)
    log(f"去重/清洗后剩余 {len(result)} 个候选（丢弃 {dropped} 个）")
    return result


def measure_tcp(proxies: list, args: argparse.Namespace) -> list:
    """并行 TCP 存活+延迟测试，返回 [(proxy, delay_ms)]，按延迟升序且不超上限"""
    if not proxies:
        return []
    log(f"开始 TCP 测活，并发 {args.workers}，延迟上限 {args.max_delay}ms ...")
    alive = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(lambda p=proxy: p.tcp_connect_delay): proxy for proxy in proxies}
        for future in as_completed(futures):
            proxy = futures[future]
            try:
                delay = future.result()
            except Exception:
                continue
            if isinstance(delay, (int, float)) and delay <= args.max_delay:
                alive.append((proxy, int(delay)))
    alive.sort(key=lambda item: item[1])
    log(f"TCP 存活且延迟达标: {len(alive)}/{len(proxies)}")
    return alive


def has_pysocks() -> bool:
    try:
        import socks  # noqa: F401  (PySocks)
        return True
    except ImportError:
        return False


def strict_http_filter(pairs: list, args: argparse.Namespace) -> list:
    """按需校验代理能否正常发 HTTP(S) 请求（HTTP 代理即验证 CONNECT 隧道能力）"""
    if args.max_http_delay <= 0 or not pairs:
        return pairs
    socks_available = has_pysocks()
    if not socks_available:
        log("[warn] 未安装 PySocks，socks5 节点将跳过 HTTP 校验")

    keep, testable = [], []
    for proxy, delay in pairs:
        if proxy.protocol.startswith("socks") and not socks_available:
            keep.append((proxy, delay))
            continue
        proxy.test_url = HTTPS_TEST_URL
        testable.append((proxy, delay))

    log(f"开始 HTTP(S) 校验（上限 {args.max_http_delay}ms），共 {len(testable)} 个节点 ...")
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 30))) as executor:
        futures = {executor.submit(lambda p=proxy: p.http_connect_delay): (proxy, delay)
                   for proxy, delay in testable}
        for future in as_completed(futures):
            pair = futures[future]
            try:
                http_delay = future.result()
            except Exception:
                continue
            if isinstance(http_delay, (int, float)) and http_delay <= args.max_http_delay:
                keep.append(pair)
    keep.sort(key=lambda item: item[1])
    log(f"HTTP(S) 校验通过: {len(keep)} 个")
    return keep


# ---------------------------------------------------------------- 生成

def to_clash(pairs: list, args: argparse.Namespace) -> dict:
    proxies, names = [], []
    for proxy, _ in pairs:
        protocol = proxy.protocol.lower()
        name = f"{protocol}-{proxy.ip}:{proxy.port}"
        node = {
            "name": name,
            "type": "socks5" if protocol == "socks5" else "http",
            "server": proxy.ip,
            "port": int(proxy.port),
        }
        if protocol == "socks5":
            node["udp"] = True
        if protocol == "https":
            node["tls"] = True
        proxies.append(node)
        names.append(name)

    return {
        "mixed-port": args.port,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "proxies": proxies,
        "proxy-groups": [
            {
                "name": "PROXY",
                "type": "url-test",          # 免费代理死得快，靠 url-test 自动淘汰、切换
                "proxies": names,
                "url": args.test_url,
                "interval": args.interval,
                "tolerance": args.tolerance,
            }
        ],
        "rules": ["GEOIP,CN,DIRECT", "MATCH,PROXY"],
    }


def dump_yaml(config: dict, pairs: list, args: argparse.Namespace) -> str:
    header = [
        "# Clash(Meta) 订阅 - 由 gen_clash.py 自动生成，请勿手工修改",
        f"# 生成时间(UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}",
        f"# 节点数: {len(pairs)}  延迟上限: {args.max_delay}ms  策略: TCP 延迟排序取 TOP {args.top_n}",
        "",
    ]
    body = yaml.safe_dump(config, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return "\n".join(header) + body


def atomic_write(path: str, text: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".clash.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp_path, 0o644)  # mkstemp 默认 0600，Web 服务器可能读不到
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def print_summary(pairs: list) -> None:
    log(f"最终节点数: {len(pairs)}，延迟最优 10 个:")
    for proxy, delay in pairs[:10]:
        log(f"  {proxy.protocol:<7} {proxy.ip}:{proxy.port:<6} {delay:>6}ms  "
            f"来源 {proxy.source}  匿名度 {proxy.anonymity or '-'}")


# ---------------------------------------------------------------- 主流程

def main(argv=None) -> int:
    args = parse_args(argv)
    if args.worker_source:
        return worker_main(args.worker_source, args)

    started = time.monotonic()
    candidates = dedupe_and_normalize(fetch_all(args), args)
    alive = measure_tcp(candidates, args)
    if args.max_http_delay > 0:
        alive = strict_http_filter(alive, args)

    pairs = alive[:max(0, args.top_n)]
    if not pairs and not args.keep_empty:
        log("[error] 没有抓到任何可用节点；为保留上一份可用订阅，本次不写出文件（退出码 1）")
        return 1

    config = to_clash(pairs, args)
    atomic_write(args.output, dump_yaml(config, pairs, args))

    print_summary(pairs)
    log(f"生成完成: {len(pairs)} 个节点 -> {os.path.abspath(args.output)}"
        f"（总用时 {time.monotonic() - started:.1f}s）")
    if not pairs:
        log("[warn] 本次写出的是空订阅（--keep-empty），FlClash 中会显示没有可用节点")
    return 0


if __name__ == "__main__":
    sys.exit(main())
