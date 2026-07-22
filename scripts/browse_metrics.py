#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
browse_metrics.py —— 用 agent-browser 打开公众号文章并截图，供视觉模型读取阅读/赞/在看。

这是 qianjin-wechat-analyzer 技能「§2.5 浏览器模式」的可执行入口：
  - 脚本只负责「打开文章 + 截全页」，产出的 PNG 交给带视觉的 agent 读数字
  - 想拿到阅读量/点赞/在看，需在同一个 agent-browser 会话里先登录微信

依赖（本机一次性安装）:
    npm install -g agent-browser
    agent-browser install        # 下载 Chromium

典型用法:
  1) 先登录微信（保留会话，不关闭）:
       agent-browser open https://mp.weixin.qq.com --headed --session wx
       # 在弹出的浏览器里扫码/登录，登录成功后保持窗口或只保持 daemon
  2) 批量截图（复用上面的 wx 会话，指标会渲染出来）:
       python scripts/browse_metrics.py --urls urls.txt --session wx --no-close
  3) 用完关掉会话:
       agent-browser close --session wx

单篇:
    python scripts/browse_metrics.py --url "https://mp.weixin.qq.com/s/xxxx"

输出:
    --out 目录下生成 <标识>.png（每篇一张全页截图）
    若带 --mode bottom，额外生成 <标识>_bottom.png（底部区域裁剪，数字更清晰）
    控制台打印 JSON: {"ok":[...], "failed":[{"url":..., "error":...}]}
    之后把生成的 PNG 交给 agent（视觉）即可读出 阅读/在看/点赞/评论 数字。
    注：阅读/在看通常直接渲染在文章页底部，不登录也可能读到；
        需精确/批量数字时用 --session wx（先登录）或 fetch_metrics.py（cookie）。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys


def _pkg_js(pkg_dir):
    """从包目录读 package.json 的 bin 入口，返回存在的 JS 绝对路径或 None。"""
    pkg_json = os.path.join(pkg_dir, "package.json")
    if not os.path.isfile(pkg_json):
        return None
    try:
        with open(pkg_json, "r", encoding="utf-8") as f:
            data = json.load(f)
        binrel = (data.get("bin") or {}).get("agent-browser")
        if not binrel:
            return None
        js = os.path.join(pkg_dir, binrel.lstrip("./\\"))
        return js if os.path.isfile(js) else None
    except Exception:  # noqa
        return None


def resolve_agent_browser():
    """返回 agent-browser 的调用方式（命令 base 列表），找不到返回 None。

    Windows 上 agent-browser 是 .cmd 包装脚本，Python subprocess 无法直接调用，
    因此改用 node 直接跑其 JS 入口（node 才是真正的 exe，参数走 argv 更安全）。
    不依赖 `npm` 命令（它在 Python 的 PATH 里常常缺失）。
    """
    node = shutil.which("node")
    # 方法1：从 which 找到的 agent-browser 二进制反推 node_modules 里的 JS 入口
    ab = shutil.which("agent-browser")
    if ab:
        npm_prefix = os.path.dirname(ab)            # 例如 .../npm
        pkg_dir = os.path.join(npm_prefix, "node_modules", "agent-browser")
        js = _pkg_js(pkg_dir)
        if node and js:
            return [node, js]
    # 方法2：常见全局 node_modules 位置
    for pkg_dir in (
        os.path.expanduser("~/AppData/Roaming/npm/node_modules/agent-browser"),
        os.path.expanduser("~/npm/node_modules/agent-browser"),
        "/usr/local/lib/node_modules/agent-browser",
        "/usr/lib/node_modules/agent-browser",
    ):
        js = _pkg_js(pkg_dir)
        if node and js:
            return [node, js]
    # 方法3：posix / Git Bash 下可直接调用 agent-browser
    if ab:
        return ["agent-browser"]
    return None


def run(cmd, timeout=60):
    """执行命令，返回 (rc, stdout, stderr)。失败不抛异常。"""
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="ignore",
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:  # noqa
        return 1, "", str(e)


def safe_name(url):
    """从 URL 提取可辨识文件名：优先 __biz，其次 mid，再次 url 哈希。"""
    m = re.search(r"__biz=([^&]+)", url)
    if m:
        return "biz_" + re.sub(r"[^A-Za-z0-9]", "", m.group(1))[:16]
    m = re.search(r"mid=(\d+)", url)
    if m:
        return "mid_" + m.group(1)
    m = re.search(r"/s/([A-Za-z0-9_-]+)", url)
    if m:
        return "s_" + m.group(1)
    return "art_" + str(abs(hash(url)) % 10**8)


def main():
    ap = argparse.ArgumentParser(description="用 agent-browser 打开公众号文章并截图")
    ap.add_argument("--url", help="单篇文章链接")
    ap.add_argument("--urls", help="包含多篇文章链接的文件，每行一个")
    ap.add_argument("--out", default="./shots", help="截图输出目录（默认 ./shots）")
    ap.add_argument("--session", default=None, help="复用指定的 agent-browser 会话（用于共享登录态）")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口（首次登录微信时用）")
    ap.add_argument("--no-close", action="store_true", help="结束后不关闭浏览器 daemon（便于复用登录态）")
    ap.add_argument("--wait", default="networkidle", choices=["networkidle", "load", "domcontentloaded"],
                    help="打开页面后的等待策略（默认 networkidle，卡住时降级为 load）")
    ap.add_argument("--mode", default="full", choices=["full", "bottom"],
                    help="截图模式：full=整页长图；bottom=额外裁出底部区域（阅读/在看数字更清晰）")
    ap.add_argument("--crop-h", type=int, default=600,
                    help="--mode bottom 时裁出的底部高度（像素），默认 600")
    ap.add_argument("--timeout", type=int, default=90, help="单条命令超时秒数")
    ap.add_argument("--args", action="append", default=[],
                    help="透传给浏览器的启动参数，例如 --args=--no-sandbox（沙箱/容器环境常需）")
    ap.add_argument("--no-sandbox", action="store_true",
                    help="以 --no-sandbox 启动浏览器（沙箱/容器/CI 环境常需，等价于 --args=--no-sandbox）")
    args = ap.parse_args()

    if not args.url and not args.urls:
        ap.error("必须提供 --url 或 --urls 之一")

    base = resolve_agent_browser()
    if not base:
        sys.stderr.write(
            "未找到 agent-browser。请先安装：\n"
            "  npm install -g agent-browser\n"
            "  agent-browser install\n"
        )
        return 2

    urls = []
    if args.url:
        urls.append(args.url.strip())
    if args.urls:
        with open(args.urls, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
    if not urls:
        sys.stderr.write("没有可处理的链接。\n")
        return 1

    os.makedirs(args.out, exist_ok=True)
    common = ["--session", args.session] if args.session else []

    ok, failed = [], []
    for i, url in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] 打开: {url}", file=sys.stderr)
        # 1) 打开
        open_cmd = base + ["open", url] + common
        if args.headed:
            open_cmd.append("--headed")
        if args.no_sandbox:
            open_cmd += ["--args", "--no-sandbox"]
        open_cmd += args.args
        rc, out, err = run(open_cmd, timeout=args.timeout)
        if rc != 0:
            failed.append({"url": url, "error": f"open failed: {err.strip() or out.strip()}"})
            continue
        # 2) 等待渲染（可降级）
        rc, out, err = run(base + ["wait", "--load", args.wait] + common, timeout=args.timeout)
        if rc != 0 and args.wait != "load":
            run(base + ["wait", "--load", "load"] + common, timeout=args.timeout)  # 降级一次
        # 3) 截全页
        out_path = os.path.join(args.out, f"{safe_name(url)}.png")
        rc, out, err = run(base + ["screenshot", "--full", out_path] + common, timeout=args.timeout)
        if rc != 0 or not os.path.exists(out_path):
            failed.append({"url": url, "error": f"screenshot failed: {err.strip() or out.strip()}"})
            continue
        print(f"    已保存: {out_path}", file=sys.stderr)
        ok.append(out_path)
        # 3b) 底部裁剪（阅读/在看/点赞数字更清晰）
        if args.mode == "bottom":
            crop_path = os.path.join(args.out, f"{safe_name(url)}_bottom.png")
            try:
                from PIL import Image
                with Image.open(out_path) as im:
                    w, h = im.size
                    top = max(0, h - args.crop_h)
                    im.crop((0, top, w, h)).save(crop_path)
                ok.append(crop_path)
                print(f"    底部裁剪: {crop_path}", file=sys.stderr)
            except Exception as e:  # noqa
                print(f"    底部裁剪失败（跳过）: {e}", file=sys.stderr)

    # 4) 关闭（除非保留会话）
    if not args.no_close:
        run(base + ["close"] + common, timeout=args.timeout)

    summary = {"ok": ok, "failed": failed}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
