#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_metrics.py —— 公众号文章指标抓取（阅读量 / 点赞 / 在看 / 留言数）

================ 前置条件 ================
微信文章的阅读量、点赞、在看、留言数由私有接口
    https://mp.weixin.qq.com/mp/getappmsgext
返回，该接口**必须携带微信登录态 cookie**（核心是 pass_ticket）才返回数字。
公开访问（无 cookie）必然返回空，这是微信的反爬限制，不是脚本问题。

因此使用前需要用户提供自己的微信登录 cookie：
  - 方法 A（推荐）：浏览器登录微信网页版 / 公众号后台后，
    打开任意一篇要抓的文章页，F12 → Network → 找到 getappmsgext 请求 →
    复制 Request Headers 里的 Cookie 整段，存成 cookies.txt
  - 方法 B：用浏览器自动化（agent-browser 技能）在已登录状态下直接抓取页面底部数字

cookie 文件两种格式都支持：
  1) 纯文本：  pass_ticket=xxx; appmsg_token=yyy; wxuin=zzz
  2) JSON：    [{"name":"pass_ticket","value":"xxx"}, ...] 或 {"pass_ticket":"xxx", ...}

================ 用法 ================
  python fetch_metrics.py --urls urls.txt --cookie cookies.txt --out metrics.json
  python fetch_metrics.py --url "https://mp.weixin.qq.com/s/xxxx" --cookie cookies.txt
  python fetch_metrics.py --url "..." --cookie cookies.txt --title "文章标题"

urls.txt：每行一个文章链接（# 开头为注释）。

输出 metrics.json 示例：
  [{"url": "...", "title": "...", "read_num": 12345,
    "like_num": 88, "old_like_num": 200, "comment_count": 12, "ok": true}, ...]

依赖：仅 Python 标准库（urllib / http.cookiejar / json / re）。
"""

import argparse
import json
import re
import sys
import time
import urllib.request
import urllib.parse
import http.cookiejar

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

API = "https://mp.weixin.qq.com/mp/getappmsgext"


def load_cookies(path):
    """读取 cookie 文件，返回 http.cookiejar.CookieJar。"""
    jar = http.cookiejar.CookieJar()
    text = open(path, "r", encoding="utf-8").read().strip()
    pairs = []
    try:
        data = json.loads(text)
        if isinstance(data, list):
            for item in data:
                pairs.append((item.get("name"), item.get("value")))
        elif isinstance(data, dict):
            for k, v in data.items():
                pairs.append((k, v))
    except json.JSONDecodeError:
        # 纯文本 k=v; k=v 形式
        for chunk in text.split(";"):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            k, v = chunk.split("=", 1)
            pairs.append((k.strip(), v.strip()))
    for name, value in pairs:
        if not name or value is None:
            continue
        c = http.cookiejar.Cookie(
            version=0, name=name, value=str(value),
            port=None, port_specified=False,
            domain="mp.weixin.qq.com", domain_specified=True, domain_initial_dot=False,
            path="/", path_specified=True,
            secure=False, expires=None, discard=False,
            comment=None, comment_url=None, rest={}, rfc2109=False,
        )
        jar.set_cookie(c)
    return jar


def extract_params(url, html):
    """从文章 URL + 页面 HTML 提取 getappmsgext 所需参数。"""
    params = {}
    # 1) 优先从 URL 查询串取
    q = urllib.parse.urlparse(url).query
    for key in ("__biz", "mid", "idx", "sn"):
        m = re.search(r"(?:^|&)" + key + r"=([^&]+)", q)
        if m:
            params[key] = urllib.parse.unquote(m.group(1))
    # 2) 从 HTML 取 appmsg_token、以及 URL 没取到的字段
    m = re.search(r"(?:window\.|var\s+)?appmsg_token\s*=\s*[\"']([^\"']*)[\"']", html)
    if m and m.group(1):
        params["appmsg_token"] = m.group(1)
    for key, pat in (
        ("mid", r"var\s+mid\s*=\s*[\"']?([0-9]+)"),
        ("idx", r"var\s+idx\s*=\s*[\"']?([0-9]+)"),
        ("sn", r"var\s+sn\s*=\s*[\"']([^\"']+)"),
        ("__biz", r"var\s+biz\s*=\s*[\"']([^\"']+)"),
    ):
        if key not in params:
            m = re.search(pat, html)
            if m:
                params[key] = m.group(1)
    # 标题兜底：<title> 常由 JS 后填为空，优先取 og:title / msg_title
    og = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)', html)
    if og:
        params["_title"] = og.group(1)
    else:
        mt = re.search(r'(?:var\s+msg_title|var\s+title)\s*=\s*["\']([^"\']+)', html)
        if mt:
            params["_title"] = mt.group(1)
    return params


def fetch_html(url, jar):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    handler = urllib.request.HTTPCookieProcessor(jar)
    opener = urllib.request.build_opener(handler)
    with opener.open(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "ignore")


def fetch_metrics(url, jar, title=None):
    """抓取单篇文章指标，返回 dict。"""
    result = {"url": url, "title": title, "ok": False,
              "read_num": None, "like_num": None,
              "old_like_num": None, "comment_count": None, "error": None}
    try:
        html = fetch_html(url, jar)
        if not title:
            mt = re.search(r"<title>(.*?)</title>", html, re.S)
            if mt and mt.group(1).strip():
                result["title"] = mt.group(1).strip()
        p = extract_params(url, html)
        if not result.get("title") and p.get("_title"):
            result["title"] = p["_title"]
        missing = [k for k in ("mid", "idx", "sn", "appmsg_token") if k not in p]
        if missing:
            result["error"] = "缺少参数: " + ",".join(missing) + "（页面可能不是标准文章页）"
            return result
        biz = p.get("__biz", "")
        q = {
            "action": "appmsg_update",
            "f": "json",
            "mid": p["mid"],
            "idx": p["idx"],
            "sn": p["sn"],
            "appmsg_token": p["appmsg_token"],
            "x": "1", "y": "1", "wxtoken": "", "count": "1",
            "is_need_ad": "0", "uin": "", "key": "",
        }
        if biz:
            q["__biz"] = biz
        api_url = API + "?" + urllib.parse.urlencode(q)
        req = urllib.request.Request(api_url, headers={
            "User-Agent": UA,
            "Referer": url,
        })
        handler = urllib.request.HTTPCookieProcessor(jar)
        opener = urllib.request.build_opener(handler)
        with opener.open(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "ignore")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            result["error"] = "接口返回非 JSON（可能 cookie 失效/被风控）: " + body[:200]
            return result
        if data.get("ret") != 0:
            result["error"] = "接口 ret=%s: %s" % (data.get("ret"), data.get("msg", ""))
            return result
        stat = data.get("appmsgstat", {})
        result["read_num"] = stat.get("read_num")
        result["like_num"] = stat.get("like_num")          # 点赞
        result["old_like_num"] = stat.get("old_like_num")  # 在看
        result["comment_count"] = stat.get("comment_count")
        result["ok"] = True
    except Exception as e:  # noqa: BLE001
        result["error"] = str(e)
    return result


def main():
    ap = argparse.ArgumentParser(description="公众号文章指标抓取（需微信登录态 cookie）")
    ap.add_argument("--urls", help="含多篇文章链接的文件，每行一个")
    ap.add_argument("--url", help="单篇文章链接")
    ap.add_argument("--cookie", required=True, help="cookie 文件路径（文本或 JSON）")
    ap.add_argument("--out", default="metrics.json", help="输出 json 路径")
    ap.add_argument("--title", help="单篇文章标题（可选）")
    ap.add_argument("--delay", type=float, default=1.5, help="每篇间隔秒数，避免风控")
    args = ap.parse_args()

    urls = []
    if args.urls:
        for line in open(args.urls, "r", encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append((line, None))
    if args.url:
        urls.append((args.url, args.title))

    if not urls:
        print("未提供任何文章链接（--url 或 --urls）", file=sys.stderr)
        sys.exit(1)

    jar = load_cookies(args.cookie)
    results = []
    for i, (u, t) in enumerate(urls):
        print("[%d/%d] 抓取 %s" % (i + 1, len(urls), u), file=sys.stderr)
        r = fetch_metrics(u, jar, t)
        print("  -> %s" % (json.dumps({k: r[k] for k in
              ("title", "read_num", "like_num", "old_like_num", "comment_count", "ok", "error")},
              ensure_ascii=False)), file=sys.stderr)
        results.append(r)
        if i < len(urls) - 1:
            time.sleep(args.delay)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("已写入 %s，共 %d 篇，成功 %d 篇" %
          (args.out, len(results), sum(1 for r in results if r["ok"])), file=sys.stderr)


if __name__ == "__main__":
    main()
