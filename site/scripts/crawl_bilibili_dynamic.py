#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
哔哩哔哩空间动态一键爬虫

目录约定（仓库根只放 index/editor/data.json/local_articles.json 四个入口文件）:
    * 本脚本以自身位置定位仓库根（site\\scripts\\ → site\\ → 仓库根），不依赖运行时 cwd；
      推荐在仓库根运行: python -u site\\scripts\\crawl_bilibili_dynamic.py <uid>
    * 运行期 state/缓存（{uid}_state.json 等）与未登记 uid 的输出 → site\\
    * 主账号 3706927737473467 默认输出 → site\\archive\\a3\\（见 ARCHIVE_DIRS）
    * local_articles.json 在仓库根，其 file 字段相对仓库根解析

用法:
    python site\\scripts\\crawl_bilibili_dynamic.py "https://space.bilibili.com/3706927737473467/dynamic"
    python site\\scripts\\crawl_bilibili_dynamic.py 3706927737473467                    # 直接输入 uid 也可以
    python site\\scripts\\crawl_bilibili_dynamic.py 3706927737473467 --since 2026-10-01
    python site\\scripts\\crawl_bilibili_dynamic.py 3706927737473467 --since 2026-10-01 --until 2026-10-09
                                                       # ^^^ 保存指定日期范围的动态
    python site\\scripts\\crawl_bilibili_dynamic.py 3706927737473467 --interval 30      # 风控严时加大间隔
    python site\\scripts\\crawl_bilibili_dynamic.py "https://space.bilibili.com/xxx/dynamic" --fresh

日期范围:
    * --since YYYY-MM-DD      起始日（含当天，北京时间）
    * --until YYYY-MM-DD      结束日（含当天，北京时间）
    * 只给 --since 时抓到起始日即翻页停止；只给 --until 时需翻完全史（建议两个都给）
    * 范围外的本地登记文章也会自动跳过
    * 输出文件名可用 -o 指定（相对路径按当前工作目录解析），如 -o site\\extra\\range.html

自动更新:
    * 首次运行 = 全量抓取，生成 {uid}_dynamic.{html,json}
    * 再次运行同一 uid = 自动进入增量更新：从最新页扫描，追平已知动态即停，
      然后重新渲染输出（无需任何参数）
    * 中断后重跑 = 从断点 offset 继续
    * local_articles.json 登记的本地简体整理版专栏会自动并入并按时间排序
      （feed 里已有同动态 id 的卡片则替换，没有则新增；--no-local 跳过）
    * 主账号输出默认落在 site\\archive\\a3\\，与 data.json 索引一致；
      其他 uid 输出到 site\\，可用 -o / --json 指定

支持的输入:
    * https://space.bilibili.com/{uid}/dynamic  (可带任意查询参数)
    * https://space.bilibili.com/{uid}
    * 纯数字 uid

功能:
    * 分页抓取全部动态（文字/图文/视频/专栏/转发，含转发的原文）
    * 绕过风控: spi 指纹 cookie、不带 origin、页间隔、-352/-412/空响应自动退避
    * 断点续抓: 每抓一页立即写入 site\\{uid}_state.json，中断后重跑自动从 offset 继续
    * 内容补全: 专栏动态抓全文与全部配图（feed 里是截断预览），视频抓完整简介
    * 输出: {uid}_dynamic.html（图文排版）+ {uid}_dynamic.json（结构化数据）
      主账号默认写入 site\\archive\\a3\\；重跑后执行 node site\\scripts\\localize_images.js 恢复图片本地化
    * 配套下载图片: python site\\scripts\\download_html_images.py site\\archive\\a3\\{uid}_dynamic.html

仅依赖 Python 标准库 (3.9+)。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Windows 控制台默认 GBK，中文输出强制 UTF-8
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---- 目录基准（以脚本自身位置定位，不依赖运行时 cwd）----
# 仓库根 = index.html / data.json / local_articles.json 所在目录（只放入口文件）；
# site\  = 存档页、运行期 state/缓存、extra\data\ 等一切工作内容。
SITE = Path(__file__).resolve().parent.parent   # ...\<仓库根>\site
ROOT = SITE.parent                              # ...\<仓库根>
# -o/--json 给出的相对路径仍按标准 CLI 语义相对当前工作目录解析。

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

FEED_URL = (
    "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
    "?host_mid={mid}&offset={offset}&features=itemOpusStyle"
)
SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
ARTICLE_URL = "https://api.bilibili.com/x/article/view?id={cv}"
VIDEO_URL = "https://api.bilibili.com/x/web-interface/view?bvid={bvid}"

# 归档目录映射：uid → 存档目录（相对仓库根，与 data.json 的 file 字段同基准）。
# 命中的 uid 默认输出到该目录，保证 data.json 的 file 链接在重跑后仍然有效；
# 未命中的 uid 输出到 site\ 工作区（仓库根只放 index/editor/data.json/local_articles.json）。
# 运行期 state/缓存不受影响，始终写在 site\。新增映射条目即可让其他 uid 落进 archive。
ARCHIVE_DIRS = {
    "3706927737473467": "site/archive/a3",  # 主账号动态页（索引 a3）
}

TYPE_NAME = {
    "DYNAMIC_TYPE_WORD": "文字动态",
    "DYNAMIC_TYPE_DRAW": "图片动态",
    "DYNAMIC_TYPE_AV": "视频投稿",
    "DYNAMIC_TYPE_ARTICLE": "专栏",
    "DYNAMIC_TYPE_OPUS": "图文/笔记",
    "DYNAMIC_TYPE_FORWARD": "转发",
    "DYNAMIC_TYPE_LIVE_RCMD": "直播分享",
    "DYNAMIC_TYPE_PGC": "番剧分享",
    "DYNAMIC_TYPE_MUSIC": "音乐分享",
    "DYNAMIC_TYPE_COURSES": "课程",
}


def https_fix(u: str | None) -> str:
    if isinstance(u, str) and u.startswith("http://"):
        return "https://" + u[7:]
    return u or ""


def resolve_jump(ju: str) -> str:
    if not ju:
        return ""
    if ju.startswith(("http://", "https://")):
        return https_fix(ju)
    if ju.startswith("//"):
        return "https:" + ju
    return "https://" + ju


# ---------------------------------------------------------------- 网络层
class BiliSession:
    """带 buvid 指纹与自动退避的最小 B 站会话。"""

    def __init__(self, interval: float = 15.0, timeout: float = 30.0) -> None:
        self.interval = interval
        self.timeout = timeout
        self.cookie = ""
        self._last_req = 0.0
        self.init_cookies()

    # ---- cookie ----
    def init_cookies(self) -> None:
        try:
            status, body, _ = self._raw(SPI_URL, referer="https://www.bilibili.com/")
            j = json.loads(body)
            data = j.get("data") or {}
            if j.get("code") == 0 and data.get("b_3"):
                self.cookie = (
                    f"buvid3={data['b_3']}; buvid4={data.get('b_4', '')}; "
                    f"b_nut={int(time.time())}"
                )
                return
        except Exception:
            pass
        # 退化: 从首页拿 set-cookie
        try:
            _, _, set_cookies = self._raw("https://www.bilibili.com/", referer="")
            pairs = [c.split(";")[0] for c in set_cookies]
            if pairs:
                self.cookie = "; ".join(pairs)
        except Exception:
            pass

    def _raw(self, url: str, referer: str):
        """一次 HTTP GET。注意: 绝不能带 origin 头，否则服务端静默返回空列表。"""
        headers = {
            "User-Agent": UA,
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if referer:
            headers["Referer"] = referer
        if self.cookie:
            headers["Cookie"] = self.cookie
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            set_cookies = resp.headers.get_all("Set-Cookie") or []
            return resp.status, body, set_cookies

    def _pace(self) -> None:
        wait = self.interval + random.uniform(0, self.interval * 0.3) - self._last_req
        if wait > 0:
            time.sleep(wait)
        self._last_req = time.time()

    def get_json(self, url: str, referer: str, retries: int = 5) -> dict:
        """GET 并解析 JSON，处理 412 与 -352/-412/-509 风控（指数退避）。"""
        last_err = "unknown"
        for attempt in range(retries):
            self._pace()
            try:
                status, body, _ = self._raw(url, referer)
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}"
                if e.code in (412, 429):
                    time.sleep(8 * (attempt + 1))
                    continue
                time.sleep(5 * (attempt + 1))
                continue
            except Exception as e:  # 网络错误
                last_err = str(e)
                time.sleep(5 * (attempt + 1))
                continue
            if status in (412, 429):
                last_err = f"HTTP {status}"
                time.sleep(8 * (attempt + 1))
                continue
            try:
                j = json.loads(body)
            except Exception:
                last_err = "bad json"
                time.sleep(4 * (attempt + 1))
                continue
            code = j.get("code")
            if code in (-352, -412, -509, -353):
                last_err = f"code {code}"
                time.sleep(30 * (attempt + 1))  # 风控: 长退避
                self.init_cookies()
                continue
            return j
        raise RuntimeError(f"请求失败({last_err}): {url}")


# ---------------------------------------------------------------- 解析层
def major_info(item: dict) -> dict:
    md = (item.get("modules") or {}).get("module_dynamic") or {}
    major = md.get("major") or {}
    mtype = major.get("type") or ""
    out = {"kind": mtype, "title": "", "url": "", "pics": [], "extra": "", "bvid": ""}
    if mtype == "MAJOR_TYPE_DRAW":
        out["kind"] = "图片"
        out["pics"] = [https_fix(x.get("src")) for x in (major.get("draw") or {}).get("items", [])]
    elif mtype == "MAJOR_TYPE_OPUS":
        out["kind"] = "图文"
        o = major.get("opus") or {}
        out["title"] = o.get("title") or ""
        s = o.get("summary")
        out["extra"] = (s or {}).get("text", "") if isinstance(s, dict) else (s or "")
        out["pics"] = [https_fix(p.get("url") if isinstance(p, dict) else p) for p in (o.get("pics") or [])]
        out["url"] = resolve_jump(o.get("jump_url") or "")
    elif mtype == "MAJOR_TYPE_ARCHIVE":
        out["kind"] = "视频"
        a = major.get("archive") or {}
        out["bvid"] = a.get("bvid") or ""
        out["title"] = a.get("title") or ""
        if out["bvid"]:
            out["url"] = "https://www.bilibili.com/video/" + out["bvid"]
        if a.get("cover"):
            out["pics"] = [https_fix(a["cover"])]
        st = a.get("stat") or {}
        dur = a.get("duration_text") or ""
        out["extra"] = (
            f"{st.get('play', '')} 播放 / {st.get('danmaku', '')} 弹幕"
            + (f" · {dur}" if dur else "")
        )
    elif mtype == "MAJOR_TYPE_ARTICLE":
        out["kind"] = "专栏"
        art = major.get("article") or {}
        out["title"] = art.get("title") or ""
        if art.get("id"):
            out["url"] = f"https://www.bilibili.com/read/cv{art['id']}"
        out["pics"] = [https_fix(c) for c in (art.get("covers") or [])]
    elif mtype == "MAJOR_TYPE_LIVE":
        out["kind"] = "直播"
        lv = major.get("live") or {}
        out["title"] = lv.get("title") or ""
        out["url"] = lv.get("link") or ""
        if lv.get("cover"):
            out["pics"] = [https_fix(lv["cover"])]
    elif mtype == "MAJOR_TYPE_PGC":
        out["kind"] = "番剧"
        p = major.get("pgc") or {}
        out["title"] = p.get("title") or ""
        out["url"] = p.get("url") or ""
        if p.get("cover"):
            out["pics"] = [https_fix(p["cover"])]
    elif mtype == "MAJOR_TYPE_MUSIC":
        out["kind"] = "音乐"
        m = major.get("music") or {}
        out["title"] = m.get("title") or ""
        if m.get("cover"):
            out["pics"] = [https_fix(m["cover"])]
    elif mtype == "MAJOR_TYPE_LIVE_RCMD":
        out["kind"] = "直播推荐"
    elif not mtype:
        out["kind"] = "纯文本"
    return out


def simplify(item: dict, is_orig: bool = False) -> dict:
    modules = item.get("modules") or {}
    author = modules.get("module_author") or {}
    md = modules.get("module_dynamic") or {}
    desc = md.get("desc") or {}
    major = major_info(item)
    text = desc.get("text") or ""
    if not text and major["extra"] and major["kind"] != "视频":
        text = major["extra"]  # 新版图文: desc 为 null，正文在 opus.summary
        major["extra"] = ""
    stat = modules.get("module_stat") or {}
    rec = {
        "id": item.get("id_str"),
        "type": item.get("type"),
        "is_orig": is_orig,
        "pub_ts": int(author.get("pub_ts") or 0),
        "author": author.get("name") or "",
        "face": https_fix(author.get("face") or ""),
        "rid": (item.get("basic") or {}).get("rid_str") or "",
        "bvid": major["bvid"],
        "text": text,
        "kind": major["kind"],
        "title": major["title"],
        "url": https_fix(major["url"]),
        "pics": [p for p in major["pics"] if p],
        "extra": major["extra"],
        "stats": {
            "fwd": (stat.get("forward") or {}).get("count", 0),
            "cmt": (stat.get("comment") or {}).get("count", 0),
            "like": (stat.get("like") or {}).get("count", 0),
        },
        "link": f"https://t.bilibili.com/{item['id_str']}" if item.get("id_str") else "",
    }
    if item.get("orig"):
        rec["orig"] = simplify(item["orig"], is_orig=True)
    return rec


def normalize_article(data: dict) -> dict:
    """专栏数据 → 有序 blocks（正文与图片按原文位置交错）。"""
    out = {"title": data.get("title") or "", "text": "", "blocks": []}
    paras = ((data.get("opus") or {}).get("content") or {}).get("paragraphs")
    if isinstance(paras, list) and paras:
        lines: list[str] = []
        for p in paras:
            ptype = p.get("para_type")
            if ptype in (1, 9) and (p.get("text") or {}).get("nodes"):
                t = "".join(
                    (n.get("word") or {}).get("words", "") for n in p["text"]["nodes"]
                )
                if t.strip():
                    out["blocks"].append({"type": "text", "value": t, "head": ptype == 9})
                    lines.append(t)
            elif ptype == 2 and (p.get("pic") or {}).get("pics"):
                for im in p["pic"]["pics"]:
                    out["blocks"].append(
                        {
                            "type": "pic",
                            "url": https_fix(im.get("url")),
                            "caption": im.get("comment") or "",
                        }
                    )
            elif ptype == 3:
                out["blocks"].append({"type": "text", "value": "————————————————"})
        out["text"] = "\n\n".join(lines)
    elif data.get("content"):
        t = (
            re.sub(r"<[^>]+>", "\n", str(data["content"]))
            .replace("\t", " ")
            .strip()
        )
        t = re.sub(r"\n{2,}", "\n", t)
        out["text"] = t
        if t:
            out["blocks"].append({"type": "text", "value": t})
    return out


# ---------------------------------------------------------------- 爬取层
class Crawler:
    def __init__(self, mid: str, interval: float, fresh: bool, workdir: Path) -> None:
        self.mid = mid
        self.ses = BiliSession(interval=interval)
        self.workdir = workdir
        self.state_file = workdir / f"{mid}_state.json"
        self.art_cache_file = workdir / f"{mid}_article_cache.json"
        self.vid_cache_file = workdir / f"{mid}_video_cache.json"
        self.state = {"offset": "", "items": [], "done": False}
        self.update_mode = False
        if not fresh and self.state_file.exists():
            try:
                self.state = json.loads(self.state_file.read_text(encoding="utf-8"))
                print(
                    f"[state] 已有 {len(self.state.get('items', []))} 条原始数据, "
                    f"done={self.state.get('done')}"
                )
                if self.state.get("done"):
                    # 上次已抓完整 → 本次自动切增量更新：从最新页扫描，追平已知数据即停
                    self.update_mode = True
                    self.state["offset"] = ""
                    self.state["done"] = False
                    print("[state] 增量更新: 从最新页扫描，追平已知动态后自动停止")
            except Exception:
                pass

    def save_state(self) -> None:
        self.state_file.write_text(
            json.dumps(self.state, ensure_ascii=False), encoding="utf-8"
        )

    # ---- 分页抓取 ----
    def crawl(self, since_ts: int | None, max_pages: int) -> list[dict]:
        referer = f"https://space.bilibili.com/{self.mid}/dynamic"
        page = 0
        reached_old = False
        empty_retries = 0

        def has_old(items: list[dict]) -> bool:
            if not since_ts:
                return False
            for it in items:
                ts = int(((it.get("modules") or {}).get("module_author") or {}).get("pub_ts") or 0)
                if ts and ts < since_ts:
                    return True
            return False

        if has_old(self.state.get("items", [])):
            reached_old = True
        known = {it.get("id_str") for it in self.state.get("items", []) if it.get("id_str")}

        while not self.state.get("done") and page < max_pages:
            page += 1
            url = FEED_URL.format(mid=self.mid, offset=self.state.get("offset") or "")
            try:
                j = self.ses.get_json(url, referer)
            except RuntimeError as e:
                print(f"[crawl] 第{page}页失败: {e}（状态已保存，重跑可续抓）")
                break
            if j.get("code") != 0:
                print(f"[crawl] 第{page}页 code={j.get('code')} {j.get('message')}")
                break
            items = ((j.get("data") or {}).get("items")) or []
            if not items:
                # 空响应多半是风控静默拦截，退避重试
                empty_retries += 1
                if empty_retries <= 10:
                    wait = 30 * empty_retries
                    print(f"[crawl] 第{page}页返回空，退避 {wait}s 重试({empty_retries})")
                    time.sleep(wait)
                    self.ses.init_cookies()
                    continue
                print("[crawl] 持续空响应，停止（状态已保存）")
                break
            empty_retries = 0
            data = j.get("data") or {}
            if self.update_mode:
                fresh = [it for it in items if it.get("id_str") not in known]
                for it in fresh:
                    known.add(it.get("id_str"))
                self.state["items"].extend(fresh)
            else:
                fresh = items
                self.state["items"].extend(items)
            self.state["offset"] = data.get("offset") or ""
            page_old = has_old(items)
            if page_old:
                if reached_old:
                    self.state["done"] = True  # 已多抓一页兜底
                reached_old = True
            if not data.get("has_more"):
                self.state["done"] = True
            if self.update_mode and not fresh:
                self.state["done"] = True  # 本页全是已抓过的 → 已追平
            self.save_state()
            print(
                f"[crawl] 第{page}页: {len(items)} 条"
                + (f"（新增 {len(fresh)}）" if self.update_mode else "")
                + f", 累计 {len(self.state['items'])}, "
                f"has_more={data.get('has_more')}, 早于截止={page_old}"
            )
            if self.update_mode and not fresh:
                print("[crawl] 已追平到已知动态，增量更新完成")
                break
        # 增量更新中途失败（风控/网络）→ 恢复为"已抓完整"，
        # 下次运行会重新从最新页发起增量扫描，而不是退化成全量翻页
        if self.update_mode and not self.state.get("done"):
            self.state["done"] = True
            self.state["offset"] = ""
            self.save_state()
            print("[crawl] 增量更新未完成，状态已复位；重跑即可再次增量更新")
        return self.state.get("items", [])

    # ---- 内容补全 ----
    def enrich(self, records: list[dict], enabled: bool = True) -> None:
        art_cache = {}
        vid_cache = {}
        for f, cache in ((self.art_cache_file, art_cache), (self.vid_cache_file, vid_cache)):
            if f.exists():
                try:
                    cache.update(json.loads(f.read_text(encoding="utf-8")))
                except Exception:
                    pass

        art_miss: list[str] = []
        vid_miss: list[str] = []

        def walk(r: dict) -> None:
            if r.get("type") == "DYNAMIC_TYPE_ARTICLE" and r.get("rid") and r["rid"] not in art_cache:
                if r["rid"] not in art_miss:
                    art_miss.append(r["rid"])
            if r.get("bvid") and r["bvid"] not in vid_cache:
                if r["bvid"] not in vid_miss:
                    vid_miss.append(r["bvid"])
            if r.get("orig"):
                walk(r["orig"])

        for r in records:
            walk(r)

        if enabled and (art_miss or vid_miss):
            print(f"[enrich] 抓取专栏 {len(art_miss)} 篇, 视频简介 {len(vid_miss)} 个")
            for cv in art_miss:
                try:
                    j = self.ses.get_json(
                        ARTICLE_URL.format(cv=cv), f"https://www.bilibili.com/read/cv{cv}"
                    )
                    if j.get("code") == 0 and j.get("data"):
                        art_cache[cv] = normalize_article(j["data"])
                        self.art_cache_file.write_text(
                            json.dumps(art_cache, ensure_ascii=False, indent=1), encoding="utf-8"
                        )
                        pics = sum(1 for b in art_cache[cv]["blocks"] if b["type"] == "pic")
                        print(f"[enrich] cv{cv}: 全文 {len(art_cache[cv]['text'])} 字, 配图 {pics} 张")
                    else:
                        print(f"[enrich] cv{cv}: code={j.get('code')} {j.get('message')}")
                except Exception as e:
                    print(f"[enrich] cv{cv} 失败: {e}")
            for bv in vid_miss:
                try:
                    j = self.ses.get_json(VIDEO_URL.format(bvid=bv), f"https://www.bilibili.com/video/{bv}")
                    if j.get("code") == 0 and j.get("data"):
                        vid_cache[bv] = {
                            "title": j["data"].get("title") or "",
                            "desc": j["data"].get("desc") or "",
                        }
                        self.vid_cache_file.write_text(
                            json.dumps(vid_cache, ensure_ascii=False, indent=1), encoding="utf-8"
                        )
                        print(f"[enrich] {bv}: 简介 {len(vid_cache[bv]['desc'])} 字")
                    else:
                        print(f"[enrich] {bv}: code={j.get('code')} {j.get('message')}")
                except Exception as e:
                    print(f"[enrich] {bv} 失败: {e}")

        def apply(r: dict) -> None:
            if r.get("type") == "DYNAMIC_TYPE_ARTICLE" and r.get("rid") in art_cache:
                a = art_cache[r["rid"]]
                r["blocks"] = a["blocks"]
                if len(a["text"]) > len(r.get("text") or ""):
                    r["text"] = a["text"]
                r["fullContent"] = True
            if r.get("bvid") in vid_cache:
                v = vid_cache[r["bvid"]]
                if len(v.get("desc") or "") > len(r.get("text") or ""):
                    r["text"] = v["desc"]
                if not r.get("title") and v.get("title"):
                    r["title"] = v["title"]
            if r.get("orig"):
                apply(r["orig"])

        for r in records:
            apply(r)


# ---------------------------------------------------------------- 输出层
# ------------------------------------------------------- 本地文章合并（自动更新时并入）
def _strip_tags(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def _remove_meta_div(body: str) -> str:
    """删除 <div class="meta">…（平衡标签计数）。"""
    idx = body.find('<div class="meta">')
    if idx < 0:
        return body
    depth = 0
    i = idx
    while i < len(body) - 5:
        nxt_open = body.find("<div", i)
        nxt_close = body.find("</div>", i)
        if nxt_close < 0:
            break
        if 0 <= nxt_open < nxt_close:
            depth += 1
            i = nxt_open + 4
        else:
            depth -= 1
            i = nxt_close + 6
            if depth == 0:
                return body[:idx] + body[i:]
    return body


_ARTICLE_TOKEN = re.compile(
    r'<img class="cover" src="(?P<cover>[^"]+)"[^>]*>'
    r'|<h2 class="section">(?P<h2>.*?)</h2>'
    r"|<hr\s*/?>"
    r"|<figure>(?P<fig>.*?)</figure>"
    r'|<div class="stats">(?P<stats>.*?)</div>\s*</div>'
    r'|<p class="rule">(?P<rule>.*?)</p>'
    r'|<p class="[^"]*"[^>]*>(?P<pcls>.*?)</p>'
    r"|<p>(?P<p0>.*?)</p>",
    re.S,
)


def parse_article_file(path: Path) -> tuple[str, list[dict]]:
    """把简体整理版专栏 HTML 解析为 (标题, blocks)，保持正文/配图/小节的原文顺序。"""
    text = path.read_text(encoding="utf-8")
    start = text.find('<article class="card">')
    if start < 0:
        raise ValueError("找不到 <article class=\"card\">")
    end = text.find("</article>", start)
    body = _remove_meta_div(text[start + len('<article class="card">') : end])
    title = ""
    mt = re.search(r'<h1 class="title">(.*?)</h1>', body, re.S)
    if mt:
        title = _strip_tags(mt.group(1))
    blocks: list[dict] = []
    for m in _ARTICLE_TOKEN.finditer(body):
        if m.group("cover"):
            blocks.append({"type": "cover", "url": https_fix(m.group("cover"))})
        elif m.group("h2") is not None:
            blocks.append({"type": "head", "value": _strip_tags(m.group("h2"))})
        elif m.group(0).lower().startswith("<hr"):
            blocks.append({"type": "hr"})
        elif m.group("fig") is not None:
            fig = m.group("fig")
            im = re.search(r'<img src="([^"]+)"', fig)
            cap = re.search(r"<figcaption>(.*?)</figcaption>", fig, re.S)
            if im:
                blocks.append(
                    {
                        "type": "pic",
                        "url": https_fix(im.group(1)),
                        "caption": _strip_tags(cap.group(1)) if cap else "",
                    }
                )
        elif m.group("stats") is not None:
            blocks.append({"type": "stats", "raw": m.group(0)})
        elif m.group("rule") is not None:
            blocks.append({"type": "rule", "value": _strip_tags(m.group("rule"))})
        else:
            val = _strip_tags(m.group("pcls") or m.group("p0") or "")
            if val:
                blocks.append({"type": "text", "value": val})
    return title, blocks


def load_local_articles(mid: str, root: Path) -> list[dict]:
    """读取本地文章注册表: {uid}_local_articles.json 或 local_articles.json[{uid}]。

    注册表位于仓库根（root），file 字段也是相对仓库根的路径 —— 这里统一
    解析成绝对路径返回，下游解析/改写图片路径时不再依赖运行时 cwd。
    """
    per_uid = root / f"{mid}_local_articles.json"
    shared = root / "local_articles.json"
    entries: list[dict] = []
    try:
        if per_uid.exists():
            entries = json.loads(per_uid.read_text(encoding="utf-8"))
        elif shared.exists():
            data = json.loads(shared.read_text(encoding="utf-8"))
            entries = (data or {}).get(mid) or []
    except Exception as e:
        print(f"[local] 读取配置失败: {e}")
        return []
    out = []
    for en0 in entries or []:
        if not en0.get("file"):
            continue
        f = Path(en0["file"])
        if not f.is_absolute():
            f = root / f  # 相对仓库根解析
        if not f.exists():
            print(f"[local] 文件不存在，跳过: {en0['file']}")
            continue
        en = dict(en0)
        en["file"] = str(f)
        out.append(en)
    return out


def _beijing_to_ts(t: str) -> int:
    """'YYYY-MM-DD HH:MM'（北京时间）→ unix 秒。"""
    import calendar

    return calendar.timegm(time.strptime(t, "%Y-%m-%d %H:%M")) - 8 * 3600


def _local_src(url: str, article_path: Path, out_dir: Path) -> str:
    """本地文章里的相对图片路径 → 相对输出页目录的路径。

    存档页已按 data.json 索引整理进 archive\\<id>\\，本地文章（a1/a2）的
    <img src="images/…"> 相对其所在页面解析；并入动态页（如 archive/a3/）后必须
    改写为相对输出目录的路径（../a1/images/…），否则动态页会断图。
    远程/绝对/锚点链接原样返回。
    """
    if not url or url.startswith(("http://", "https://", "data:", "//", "/", "#")):
        return url
    try:
        abs_p = (article_path.parent / url).resolve()
        return Path(os.path.relpath(abs_p, out_dir.resolve())).as_posix()
    except Exception:
        return url


def build_local_record(cfg: dict, author: str, face: str, out_dir: Path | None = None) -> dict | None:
    page = Path(cfg["file"])
    try:
        title, blocks = parse_article_file(page)
    except Exception as e:
        print(f"[local] 解析失败 {cfg['file']}: {e}")
        return None
    if not blocks:
        print(f"[local] 未解析到内容，跳过: {cfg['file']}")
        return None
    if out_dir is not None:  # 相对图片路径改写为相对输出页目录
        for b in blocks:
            if b.get("type") in ("pic", "cover") and b.get("url"):
                b["url"] = _local_src(b["url"], page, out_dir)
    ts = 0
    if cfg.get("time"):
        try:
            ts = _beijing_to_ts(cfg["time"])
        except Exception:
            print(f"[local] 时间无法解析: {cfg['time']}")
    text = "\n\n".join(
        b["value"] for b in blocks if b.get("type") == "text" and b.get("value")
    )
    pics = [b["url"] for b in blocks if b.get("type") in ("pic", "cover")]
    cv = str(cfg.get("cv") or "")
    dyn_id = str(cfg.get("dyn_id") or "")
    return {
        "id": dyn_id or None,
        "type": "DYNAMIC_TYPE_ARTICLE",
        "is_orig": False,
        "pub_ts": ts,
        "author": author,
        "face": face,
        "rid": cv,
        "bvid": "",
        "text": text,
        "kind": "简体整理",
        "title": title,
        "url": f"https://www.bilibili.com/read/cv{cv}" if cv else "",
        "pics": pics,
        "extra": "",
        "stats": {},
        "link": f"https://t.bilibili.com/{dyn_id}" if dyn_id else "",
        "blocks": blocks,
        "local": True,
    }


def apply_local_articles(
    records: list[dict],
    cfgs: list[dict],
    since_ts: int | None = None,
    until_ts: int | None = None,
    out_dir: Path | None = None,
) -> int:
    """并入本地文章：feed 里已有同 id 卡片则替换（保留互动数据），否则新增。
    指定日期范围时，范围外的本地文章不并入。out_dir = 输出页所在目录
    （用于把本地文章的相对图片路径改写为相对输出页的路径）。"""
    if not cfgs:
        return 0
    author = next((r.get("author") for r in records if r.get("author")), "")
    face = next((r.get("face") for r in records if r.get("face")), "")
    added = 0
    for cfg in cfgs:
        # 按登记时间先过滤范围（替换场景以 feed 时间为准，feed 已过滤过）
        if cfg.get("time"):
            try:
                ts = _beijing_to_ts(cfg["time"])
                if since_ts and ts < since_ts:
                    print(f"[local] 跳过（早于 --since）: {cfg['file']}")
                    continue
                if until_ts and ts >= until_ts + 86400:
                    print(f"[local] 跳过（晚于 --until）: {cfg['file']}")
                    continue
            except Exception:
                pass
        rec = build_local_record(cfg, author, face, out_dir)
        if not rec:
            continue
        hit = next(
            (i for i, r in enumerate(records) if rec["id"] and r.get("id") == rec["id"]),
            None,
        )
        if hit is not None:
            old = records[hit]
            rec["stats"] = old.get("stats") or {}
            if old.get("pub_ts"):
                rec["pub_ts"] = old["pub_ts"]
            records[hit] = rec
            print(f"[local] feed 卡片 → 简体整理版: {rec['title']}")
        else:
            records.append(rec)
            when = fmt_ts(rec["pub_ts"]) if rec["pub_ts"] else "?"
            print(f"[local] 新增本地文章: {rec['title']} ({when})")
        added += 1
    if added:
        records.sort(key=lambda r: r.get("pub_ts") or 0, reverse=True)
    return added


def fmt_ts(ts: int) -> str:
    # gmtime(ts+8h) 读出的 UTC 字段即北京时间墙钟，与本机时区无关
    # （不能用 localtime(ts+8h)，在 UTC+8 机器上会双重偏移 +8h）
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts + 8 * 3600))


def render_card(r: dict) -> str:
    e = html.escape
    label = '<span class="orig-tag">被转发的原文</span>' if r.get("is_orig") else ""
    title = f'<div class="d-title">{e(r["title"])}</div>' if r.get("title") else ""
    st = r.get("stats") or {}
    stat_line = ""
    if not r.get("is_orig") and any(st.get(k) for k in ("fwd", "cmt", "like")):
        stat_line = (
            f'<div class="d-stat">转发 {st.get("fwd", 0)} · 评论 {st.get("cmt", 0)} '
            f'· 点赞 {st.get("like", 0)}</div>'
        )
    link = (
        f'<a class="d-link" href="{e(r["link"])}" target="_blank">原动态链接</a>'
        if r.get("link") and not r.get("is_orig")
        else ""
    )
    media = (
        f'<a class="d-link" href="{e(r["url"])}" target="_blank">{e(r["url"])}</a>'
        if r.get("url")
        else ""
    )
    if r.get("blocks"):
        parts: list[str] = []
        for b in r["blocks"]:
            t = b.get("type")
            if t == "pic":
                parts.append(
                    f'<div class="pic"><a href="{e(b["url"])}" target="_blank">'
                    f'<img src="{e(b["url"])}" loading="lazy" alt=""></a>'
                    + (
                        f'<div class="pic-cap">{e(b["caption"])}</div>'
                        if b.get("caption")
                        else ""
                    )
                    + "</div>"
                )
            elif t == "cover":
                parts.append(f'<img class="cover" src="{e(b["url"])}" alt="">')
            elif t == "head":
                parts.append(f'<h2 class="section">{e(b["value"])}</h2>')
            elif t == "rule":
                parts.append(f'<p class="rule">{e(b["value"])}</p>')
            elif t == "hr":
                parts.append("<hr>")
            elif t == "stats":
                parts.append(b.get("raw") or "")  # 本地文章脚注（可信本地文件的原始 HTML）
            else:
                parts.append(
                    f'<p class="d-text{" d-head" if b.get("head") else ""}">'
                    f'{e(b.get("value") or "").replace(chr(10), "<br>")}</p>'
                )
        return f"{label}{title}{stat_line}{''.join(parts)}{media}{link}"
    text = (
        f'<p class="d-text">{e(r["text"]).replace(chr(10), "<br>")}</p>' if r.get("text") else ""
    )
    extra = (
        f'<p class="d-extra">{e(r["extra"]).replace(chr(10), "<br>")}</p>'
        if r.get("extra") and r["extra"] != r.get("text")
        else ""
    )
    pics = "".join(
        f'<a href="{e(u)}" target="_blank"><img src="{e(u)}" loading="lazy" alt=""></a>'
        for u in r.get("pics") or []
    )
    pics_html = f'<div class="pics">{pics}</div>' if pics else ""
    return f"{label}{title}{stat_line}{text}{extra}{pics_html}{media}{link}"


def render_html(records: list[dict], mid: str, since_text: str) -> str:
    e = html.escape
    nick = next((r.get("author") for r in records if r.get("author")), "（未知）")
    face = next((r.get("face") for r in records if r.get("face")), "")
    counts: dict[str, int] = {}
    for r in records:
        counts[r["type"]] = counts.get(r["type"], 0) + 1
    count_text = " · ".join(f"{TYPE_NAME.get(k, k)} {v}" for k, v in counts.items())
    cards = []
    for r in records:
        tname = TYPE_NAME.get(r["type"], r["type"])
        kind = (
            f'<span class="badge kind">{e(r["kind"])}</span>'
            if r.get("kind") and r["kind"] != "纯文本"
            else ""
        )
        orig = f'<div class="orig">{render_card(r["orig"])}</div>' if r.get("orig") else ""
        cards.append(
            f'<article class="dyn">\n'
            f'  <div class="dyn-head"><span class="time">{fmt_ts(r["pub_ts"]) if r.get("pub_ts") else ""}</span>'
            f'<span class="badge">{e(tname)}</span>{kind}</div>\n'
            f"  {render_card(r)}\n  {orig}\n</article>"
        )
    avatar = f'<img class="avatar" src="{e(face)}" alt="{e(nick)}的头像">' if face else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(nick)} 的动态（{e(since_text)}）</title>
<style>
  :root {{ --text:#2f3238; --muted:#61666d; --line:#e3e5e7; --accent:#fb7299; --blue:#00a1d6; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:#f6f7f8; color:var(--text);
    font-family:"PingFang SC","Microsoft YaHei","Noto Sans SC",sans-serif; font-size:15px; line-height:1.75; }}
  .container {{ max-width:820px; margin:0 auto; padding:28px 16px 64px; }}
  header.page h1 {{ font-size:24px; margin:0 0 6px; }}
  header.page .head {{ display:flex; align-items:center; gap:14px; margin-bottom:6px; }}
  header.page .avatar {{ width:56px; height:56px; border-radius:50%; object-fit:cover;
    background:#e3e5e7; flex-shrink:0; border:1px solid var(--line); }}
  header.page .sub {{ color:var(--muted); font-size:14px; margin-bottom:20px; }}
  .dyn {{ background:#fff; border-radius:8px; padding:18px 22px; margin-bottom:16px; }}
  .dyn-head {{ display:flex; gap:8px; align-items:center; margin-bottom:8px; flex-wrap:wrap; }}
  .time {{ color:var(--muted); font-size:13px; }}
  .badge {{ font-size:12px; background:#f1f2f3; color:var(--muted); border-radius:3px; padding:1px 7px; }}
  .badge.kind {{ background:#eaf4fb; color:var(--blue); }}
  .d-title {{ font-weight:600; margin:4px 0; }}
  .d-text {{ margin:4px 0 8px; }}
  .d-text.d-head {{ font-weight:600; font-size:16px; margin-top:14px; }}
  .d-extra {{ color:var(--muted); font-size:13px; margin:0 0 8px; }}
  .d-stat {{ color:var(--muted); font-size:12px; margin:2px 0 6px; }}
  .pics {{ display:flex; flex-wrap:wrap; gap:8px; margin:8px 0; }}
  .pics img {{ width:100%; max-width:480px; height:auto; border-radius:4px; border:1px solid var(--line); }}
  .pic {{ margin:10px 0; text-align:center; }}
  .pic img {{ max-width:100%; border-radius:4px; border:1px solid var(--line); }}
  .pic-cap {{ font-size:12px; color:var(--muted); text-align:left; margin-top:4px;
    background:#f1f2f3; border-left:3px solid var(--accent); padding:4px 8px; border-radius:0 4px 4px 0; }}
  .d-link {{ color:var(--blue); font-size:13px; word-break:break-all; margin-right:12px; }}
  .orig {{ margin-top:12px; border-left:3px solid var(--line); background:#fafbfc;
    padding:10px 14px; border-radius:0 4px 4px 0; }}
  .orig-tag {{ display:inline-block; font-size:12px; color:var(--muted); background:#f1f2f3;
    border-radius:3px; padding:1px 7px; margin-bottom:6px; }}
  article.dyn p {{ margin:0 0 14px; }}
  .cover {{ width:100%; border-radius:6px; margin-bottom:16px; border:1px solid var(--line); }}
  h2.section {{ font-size:20px; margin:32px 0 14px; padding-top:20px; border-top:1px solid var(--line); }}
  .rule {{ color:var(--muted); letter-spacing:2px; overflow:hidden; white-space:nowrap; margin:16px 0; }}
  article.dyn figure {{ margin:16px auto; text-align:center; }}
  article.dyn figure img {{ max-width:100%; height:auto; border-radius:6px; border:1px solid var(--line); background:#fff; }}
  figcaption {{ margin-top:8px; font-size:13px; color:var(--muted); line-height:1.6; text-align:left;
    background:#f1f2f3; border-left:3px solid var(--accent); padding:6px 10px; border-radius:0 4px 4px 0; }}
  article.dyn hr {{ border:none; border-top:1px solid var(--line); margin:24px 0; }}
  .stats {{ margin-top:24px; padding-top:14px; border-top:1px solid var(--line);
    color:var(--muted); font-size:13px; }}
  .stats .source {{ color:var(--muted); font-size:12px; margin-top:6px; }}
</style>
</head>
<body>
<div class="container">
<header class="page">
  <div class="head">{avatar}<h1>{e(nick)}（UID {mid}）的动态</h1></div>
  <div class="sub">时间范围：{e(since_text)} · 共 {len(records)} 条 · {e(count_text)}</div>
</header>
{chr(10).join(cards)}
</div>
</body>
</html>
"""


# ---------------------------------------------------------------- 主流程
def parse_mid(value: str) -> str:
    value = value.strip()
    m = re.search(r"space\.bilibili\.com/(\d+)", value)
    if m:
        return m.group(1)
    if value.isdigit() and 4 <= len(value) <= 20:
        return value
    raise SystemExit(
        f"[error] 无法从输入解析 uid: {value!r}\n"
        "支持格式: https://space.bilibili.com/{{uid}}/dynamic 或纯数字 uid"
    )


def parse_since(text: str | None) -> int | None:
    if not text:
        return None
    try:
        dt = time.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise SystemExit(f"[error] --since 格式应为 YYYY-MM-DD，收到: {text}")
    # 以 UTC+8 的当天 00:00 为界（timegm 与时区无关，避免受本机时区影响）
    import calendar

    return calendar.timegm(dt) - 8 * 3600


def main() -> int:
    ap = argparse.ArgumentParser(
        description="爬取哔哩哔哩空间动态（输入链接即可）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("url", help="空间动态链接或 uid，如 https://space.bilibili.com/xxx/dynamic")
    ap.add_argument("--since", help="起始日期（YYYY-MM-DD，北京时间），只保留该日及之后的动态")
    ap.add_argument("--until", help="结束日期（YYYY-MM-DD，北京时间，含当天），与 --since 组成日期范围")
    ap.add_argument("--limit", type=int, default=0, help="最多保留 N 条（0=不限）")
    ap.add_argument("--max-pages", type=int, default=50, help="最多翻页数，默认 50")
    ap.add_argument("--interval", type=float, default=15.0,
                    help="请求间隔秒数（默认 15，风控严格时建议 30~45）")
    ap.add_argument("--fresh", action="store_true", help="忽略断点，从头抓取")
    ap.add_argument("--no-enrich", action="store_true", help="跳过专栏全文/视频简介补全")
    ap.add_argument("--no-local", action="store_true",
                    help="跳过 local_articles.json 里登记的本地简体整理版文章")
    ap.add_argument("-o", "--out", default=None,
                    help=f"输出 HTML 路径（默认：命中 ARCHIVE_DIRS 的 uid 写入其存档目录，如 {ARCHIVE_DIRS.get('3706927737473467', '')}/{{uid}}_dynamic.html；否则当前目录）")
    ap.add_argument("--json", dest="json_out", default=None, help="输出 JSON 路径（默认与 HTML 同目录）")
    args = ap.parse_args()

    mid = parse_mid(args.url)
    since_ts = parse_since(args.since)
    until_ts = parse_since(args.until)  # 当天 00:00（北京时间），内容保留到该天结束
    if since_ts and until_ts and since_ts > until_ts:
        raise SystemExit("[error] --since 不能晚于 --until")
    if args.since and args.until:
        since_text = f"{args.since} ~ {args.until}"
    elif args.since:
        since_text = f"{args.since}以来"
    elif args.until:
        since_text = f"至 {args.until}"
    else:
        since_text = "全部时间"
    archive_dir = ARCHIVE_DIRS.get(mid)  # 命中归档站映射 → 默认写入 site/archive/<id>/
    default_base = (ROOT / archive_dir) if archive_dir else SITE
    out_html = Path(args.out) if args.out else default_base / f"{mid}_dynamic.html"
    out_json = Path(args.json_out) if args.json_out else default_base / f"{mid}_dynamic.json"

    print(f"[target] uid={mid}  截止={since_text}  间隔={args.interval}s")
    print(f"[out] HTML={out_html.as_posix()}  JSON={out_json.as_posix()}")

    crawler = Crawler(mid, interval=args.interval, fresh=args.fresh, workdir=SITE)
    raw_items = crawler.crawl(since_ts, args.max_pages)

    # 过滤 + 转换 + 去重 + 排序（日期范围: since <= 发布时间 <= until 当天结束）
    until_end_ts = (until_ts + 86400) if until_ts else None
    records = []
    for it in raw_items:
        modules = it.get("modules") or {}
        author = modules.get("module_author") or {}
        ts = int(author.get("pub_ts") or 0)
        if since_ts and ts and ts < since_ts:
            continue
        if until_end_ts and ts and ts >= until_end_ts:
            continue
        records.append(simplify(it))
    records.sort(key=lambda r: r.get("pub_ts") or 0, reverse=True)
    seen, deduped = set(), []
    for r in records:
        if r.get("id") and r["id"] in seen:
            continue
        if r.get("id"):
            seen.add(r["id"])
        deduped.append(r)

    crawler.enrich(deduped, enabled=not args.no_enrich)

    # 自动并入 local_articles.json 登记的本地简体整理版专栏
    if not args.no_local:
        local_cfgs = load_local_articles(mid, ROOT)
        if local_cfgs:
            print(f"[local] 登记的本地文章: {len(local_cfgs)} 篇")
        apply_local_articles(deduped, local_cfgs, since_ts, until_ts, out_html.parent)

    if args.limit and len(deduped) > args.limit:
        deduped = deduped[: args.limit]

    for _p in (out_html, out_json):
        _p.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(deduped, ensure_ascii=False, indent=2), encoding="utf-8")
    out_html.write_text(render_html(deduped, mid, since_text), encoding="utf-8")

    types: dict[str, int] = {}
    for r in deduped:
        types[r["type"]] = types.get(r["type"], 0) + 1
    pics_total = 0
    for r in deduped:
        if r.get("blocks"):  # 有 blocks 时渲染只走 blocks，避免与 pics 重复计数
            pics_total += sum(1 for b in r["blocks"] if b["type"] in ("pic", "cover"))
        else:
            pics_total += len(r.get("pics") or [])
    print(
        f"\n完成: 共 {len(deduped)} 条 {json.dumps(types, ensure_ascii=False)}，"
        f"图片 {pics_total} 张"
    )
    print(f"  HTML: {out_html.resolve()}")
    print(f"  JSON: {out_json.resolve()}")
    print(f"  下载图片: python site\\scripts\\download_html_images.py \"{out_html.as_posix()}\"")
    print(f"  图片本地化（重跑后必做）: node site\\scripts\\localize_images.js")
    return 0


if __name__ == "__main__":
    sys.exit(main())
