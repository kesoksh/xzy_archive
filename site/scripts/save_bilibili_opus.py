#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
保存哔哩哔哩单条动态/专栏为独立 HTML 文件。

用法（推荐在仓库根运行，路径以脚本位置定位、不依赖 cwd）:
    python site\\scripts\\save_bilibili_opus.py https://www.bilibili.com/opus/1257802333947953158
    python site\\scripts\\save_bilibili_opus.py https://www.bilibili.com/read/cv53382393
    python site\\scripts\\save_bilibili_opus.py 1257802333947953158 --archive      # 一条龙（见下）
    python site\\scripts\\save_bilibili_opus.py 1257802333947953158 --archive a6    # 指定索引 id

--archive 归档一条龙（保证新存档结构不乱套）:
    * 自动分配下一个空闲 aN（或 --archive aN 指定），输出到 site\\archive\\<id>\\index.html
      （页名统一 index.html：站点访问 site/archive/<id>/ 目录地址即直达，无长串编码）
    * 自动把条目写进仓库根的 data.json（file = site/archive/<id>/index.html，zone=new）
    * 之后跑 node site\\scripts\\localize_images.js 自动发现新页面、
      下载全部图片并改写本地引用 —— 全程无需手改任何代码

默认输出（不加 --archive）:
    site\\archive\\_inbox\\{作者}-{标题}-opus{动态id}.html（草稿区，不登记索引；
    确认后重跑加 --archive 正式归档，或 -o 自定路径）
    （Windows 非法字符替换为空格；标题超 60 字截断加 ……）

支持的动态类型:
    * 专栏 (ARTICLE)   → article API 抓全文，正文/配图/图注按原文顺序
    * 图文/文字 (DRAW/WORD/OPUS) → detail 抓全文与全部配图
    * 视频 (AV)        → 标题 + 简介 + 封面（简介缺失时从 video API 补全）
    * 转发 (FORWARD)   → 附带被转发的原文
仅依赖 Python 标准库；复用 crawl_bilibili_dynamic 的会话与风控退避逻辑。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from pathlib import Path

from crawl_bilibili_dynamic import (
    ARTICLE_URL,
    ROOT,
    SITE,
    BiliSession,
    TYPE_NAME,
    VIDEO_URL,
    fmt_ts,
    https_fix,
    normalize_article,
    simplify,
)

DETAIL_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?id={id}&features=itemOpusStyle"


# ---------------------------------------------------------------- 输入解析
def parse_target(value: str) -> tuple[str | None, str | None]:
    """返回 (dyn_id, cv_id)。支持 opus / read.cv / t.bilibili.com / 纯数字 / cv数字。"""
    v = value.strip()
    for pat, key in (
        (r"opus/(\d+)", "dyn"),
        (r"t\.bilibili\.com/(\d+)", "dyn"),
        (r"read/cv(\d+)", "cv"),
        (r"^[Cc][Vv](\d+)$", "cv"),
        (r"^(\d{5,20})$", "dyn"),
    ):
        m = re.search(pat, v)
        if m:
            return (m.group(1), None) if key == "dyn" else (None, m.group(1))
    raise SystemExit(
        f"[error] 无法识别的链接: {value!r}\n"
        "支持: /opus/{id}、/read/cv{id}、t.bilibili.com/{id}、cv{id}、纯数字动态 id"
    )


def safe_part(s: str, limit: int) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", s or "")
    s = re.sub(r"\s+", " ", s).strip(" .")
    if len(s) > limit:
        s = s[:limit].rstrip() + "……"
    return s or "未命名"


# ---------------------------------------------------------------- 归档一条龙
ARCHIVE_ROOT = SITE / "archive"
DATA_JSON = ROOT / "data.json"
INBOX_DIR = ARCHIVE_ROOT / "_inbox"  # 草稿区：默认输出位置，不登记索引


def next_archive_id() -> str:
    """下一个空闲 aN：data.json 已用 id 与 site\\archive 下已有目录都不占用。"""
    used: set[str] = set()
    try:
        data = json.loads(DATA_JSON.read_text(encoding="utf-8"))
        used = {str(it.get("id") or "") for it in data.get("items") or []}
    except Exception:
        pass
    n = 1
    while True:
        aid = f"a{n}"
        if aid not in used and not (ARCHIVE_ROOT / aid).exists():
            return aid
        n += 1


def check_archive_id(aid: str) -> None:
    """显式指定的 id 必须格式合法且未被 data.json 或目录占用。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", aid):
        raise SystemExit(f"[error] 非法索引 id: {aid!r}（仅限字母/数字/_/-，≤32 字符）")
    try:
        data = json.loads(DATA_JSON.read_text(encoding="utf-8"))
        if any(str(it.get("id")) == aid for it in data.get("items") or []):
            raise SystemExit(f"[error] data.json 已存在 id={aid}，换一个或先删除旧条目")
    except SystemExit:
        raise
    except Exception:
        pass
    if (ARCHIVE_ROOT / aid).exists() and any((ARCHIVE_ROOT / aid).iterdir()):
        raise SystemExit(f"[error] 目录 site\\archive\\{aid}\\ 已存在且非空，换一个 id")


def register_entry(
    aid: str, title: str, file_rel: str, time_str: str, desc: str, tag: str
) -> None:
    """把新存档条目插到 data.json 顶部（index.html 按数组序渲染，新条目排最前）。"""
    try:
        data = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        raise SystemExit(f"[error] data.json 读取失败，未登记: {e}")
    items = data.get("items") or []
    if any(str(it.get("id")) == aid for it in items):
        raise SystemExit(f"[error] data.json 已存在 id={aid}，未登记")
    items.insert(
        0,
        {
            "id": aid,
            "title": title,
            "file": file_rel,
            "time": time_str,
            "desc": desc,
            "tag": tag,
            "zone": "new",
        },
    )
    data["items"] = items
    data["updated"] = time_str[:10]  # YYYY-MM-DD（北京时间）
    DATA_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _beijing_now() -> str:
    """北京时间 'YYYY-MM-DD HH:MM'（gmtime(ts+8h)，不能用 localtime）。"""
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 8 * 3600))


# ---------------------------------------------------------------- 取数
def fetch_article(ses: BiliSession, cv: str) -> dict | None:
    j = ses.get_json(ARTICLE_URL.format(cv=cv), f"https://www.bilibili.com/read/cv{cv}")
    if j.get("code") == 0 and j.get("data"):
        return j["data"]
    print(f"[error] 专栏 cv{cv}: code={j.get('code')} {j.get('message')}")
    return None


def fetch_video_desc(ses: BiliSession, bvid: str) -> dict | None:
    j = ses.get_json(VIDEO_URL.format(bvid=bvid), f"https://www.bilibili.com/video/{bvid}")
    if j.get("code") == 0 and j.get("data"):
        return j["data"]
    return None


def pick_cover(art: dict | None, pics: list[str]) -> str:
    if art:
        urls = art.get("image_urls") or []
        if not urls and art.get("banner_url"):
            urls = [art["banner_url"]]
        if urls:
            return https_fix(urls[0])
    return https_fix(pics[0]) if pics else ""


# ---------------------------------------------------------------- 渲染
def blocks_to_html(blocks: list[dict]) -> str:
    e = html.escape
    out: list[str] = []
    for b in blocks:
        t = b.get("type")
        if t == "text":
            if b.get("head"):
                out.append(f'<h2 class="section">{e(b["value"])}</h2>')
            else:
                out.append(f'<p>{e(b.get("value") or "").replace(chr(10), "<br>")}</p>')
        elif t == "pic":
            cap = b.get("caption") or ""
            out.append(
                f'<figure><a href="{e(b["url"])}" target="_blank">'
                f'<img src="{e(b["url"])}" loading="lazy" alt=""></a>'
                + (f"<figcaption>{e(cap)}</figcaption>" if cap else "")
                + "</figure>"
            )
        elif t == "hr":
            out.append("<hr>")
    return "".join(out)


def stats_line(art_stats: dict, dyn_stats: dict) -> str:
    pieces: list[str] = []

    def add(label: str, v) -> None:
        try:
            n = int(v)
        except (TypeError, ValueError):
            return
        if n:
            pieces.append(f"{label} {n}")

    if art_stats:
        add("阅读", art_stats.get("view"))
        add("点赞", art_stats.get("like"))
        add("评论", art_stats.get("reply"))
        add("收藏", art_stats.get("favorite"))
        add("投币", art_stats.get("coin"))
        add("分享", art_stats.get("share"))
    add("转发动态", dyn_stats.get("fwd"))
    if not art_stats:
        add("评论", dyn_stats.get("cmt"))
        add("点赞", dyn_stats.get("like"))
    return " · ".join(pieces)


def render_page(
    *,
    title: str,
    author: str,
    face: str,
    pub_ts: int,
    type_label: str,
    cover: str,
    body_html: str,
    orig_html: str,
    stats_html: str,
    source_note: str,
    links: list[tuple[str, str]],
    back_href: str = "index.html",
) -> str:
    e = html.escape
    cover_html = f'<img class="cover" src="{e(cover)}" alt="封面">' if cover else ""
    avatar = f'<img class="avatar" src="{e(face)}" alt="{e(author)}的头像">' if face else ""
    time_text = fmt_ts(pub_ts) if pub_ts else ""
    links_html = " ".join(
        f'<a class="d-link" href="{e(href)}" target="_blank">{e(label)}</a>'
        for href, label in links
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)} - {e(author)}</title>
<style>
  :root {{ --text:#2f3238; --muted:#61666d; --line:#e3e5e7; --accent:#fb7299; --blue:#00a1d6; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:#f6f7f8; color:var(--text);
    font-family:"PingFang SC","Microsoft YaHei","Noto Sans SC",sans-serif;
    font-size:16px; line-height:1.8; }}
  .container {{ max-width:860px; margin:0 auto; padding:32px 20px 64px; }}
  .card {{ background:#fff; border-radius:8px; padding:32px 40px; }}
  .cover {{ width:100%; border-radius:6px; margin-bottom:20px; border:1px solid var(--line); }}
  h1.title {{ font-size:28px; line-height:1.4; margin:0 0 16px; }}
  .meta {{ display:flex; align-items:center; gap:12px; color:var(--muted); font-size:14px;
    padding-bottom:20px; border-bottom:1px solid var(--line); margin-bottom:24px; }}
  .meta .avatar {{ width:48px; height:48px; border-radius:50%; object-fit:cover;
    background:#e3e5e7; flex-shrink:0; }}
  .meta .name {{ color:var(--accent); font-size:16px; font-weight:600; }}
  p {{ margin:0 0 16px; }}
  h2.section {{ font-size:22px; margin:40px 0 16px; padding-top:24px; border-top:1px solid var(--line); }}
  figure {{ margin:20px auto; text-align:center; }}
  figure img {{ max-width:100%; height:auto; border-radius:6px; border:1px solid var(--line); background:#fff; }}
  figcaption {{ margin-top:8px; font-size:13px; color:var(--muted); line-height:1.6; text-align:left;
    background:#f6f7f8; border-left:3px solid var(--accent); padding:6px 10px; border-radius:0 4px 4px 0; }}
  hr {{ border:none; border-top:1px solid var(--line); margin:32px 0; }}
  .orig {{ margin-top:28px; border-left:3px solid var(--line); background:#fafbfc;
    padding:16px 20px; border-radius:0 4px 4px 0; }}
  .orig-tag {{ display:inline-block; font-size:12px; color:var(--muted); background:#f1f2f3;
    border-radius:3px; padding:1px 7px; margin-bottom:8px; }}
  .stats {{ margin-top:32px; padding-top:16px; border-top:1px solid var(--line);
    color:var(--muted); font-size:14px; }}
  .stats .source {{ color:var(--muted); font-size:13px; margin-top:6px; }}
  .src-links {{ margin-top:12px; }}
  .d-link {{ color:var(--blue); font-size:13px; word-break:break-all; margin-right:14px; }}
  .back-home {{ position:fixed; top:14px; left:14px; z-index:99; display:inline-block;
    padding:7px 16px; background:#fff; color:var(--text); font-size:14px; line-height:1.4;
    text-decoration:none; border:1px solid var(--line); border-radius:18px;
    box-shadow:0 2px 8px rgba(0,0,0,.08); transition:.15s; }}
  .back-home:hover {{ color:var(--accent); border-color:var(--accent); transform:translateY(-1px); }}
  @media print {{ .back-home {{ display:none; }} }}
</style>
</head>
<body>
<a class="back-home" href="{back_href}">← 返回主页</a>
<div class="container">
<article class="card">
  {cover_html}
  <h1 class="title">{e(title)}</h1>
  <div class="meta">{avatar}<div>
      <div class="name">{e(author)}</div>
      <div>{e(time_text)}{(' · ' + e(type_label)) if type_label else ''}</div>
    </div>
  </div>
  {body_html}
  {orig_html}
  <div class="stats">{stats_html}<div class="source">{e(source_note)}</div></div>
  <div class="src-links">{links_html}</div>
</article>
</div>
</body>
</html>
"""


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(
        description="保存哔哩哔哩单条动态/专栏为独立 HTML",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("url", help="动态链接（/opus/{id}、/read/cv{id}、t.bilibili.com/{id}）或纯数字 id")
    ap.add_argument("-o", "--out", help="输出文件路径（相对路径按当前目录解析；与 --archive 互斥）")
    ap.add_argument("--archive", nargs="?", const="auto", default=None, metavar="ID",
                    help="归档一条龙：输出到 site\\archive\\<id>\\ 并自动登记 data.json；"
                         "省略 id（--archive）自动分配下一个空闲 aN")
    ap.add_argument("--interval", type=float, default=12.0, help="请求间隔秒数，默认 12")
    ap.add_argument("--print-name", action="store_true", help="只打印将要输出的路径，不下载保存")
    args = ap.parse_args()
    if args.archive and args.out:
        raise SystemExit("[error] --archive 与 -o 不能同时使用")

    dyn_id, cv_id = parse_target(args.url)
    ses = BiliSession(interval=args.interval)

    rec: dict | None = None
    art: dict | None = None
    item: dict | None = None

    # 1) 动态 detail
    if dyn_id:
        j = ses.get_json(DETAIL_URL.format(id=dyn_id), f"https://www.bilibili.com/opus/{dyn_id}")
        if j.get("code") != 0 or not (j.get("data") or {}).get("item"):
            print(f"[error] 动态 opus{dyn_id}: code={j.get('code')} {j.get('message')}")
            return 1
        item = j["data"]["item"]
        rec = simplify(item)
        cv_id = cv_id or (rec.get("rid") or None)

    # 2) 专栏全文
    if cv_id and (rec is None or rec.get("type") == "DYNAMIC_TYPE_ARTICLE"):
        art = fetch_article(ses, cv_id)

    # 3) 视频简介补全
    if rec and rec.get("bvid") and len(rec.get("text") or "") < 30:
        v = fetch_video_desc(ses, rec["bvid"])
        if v and len(v.get("desc") or "") > len(rec.get("text") or ""):
            rec["text"] = v["desc"]
        if v and not rec.get("title") and v.get("title"):
            rec["title"] = v["title"]

    # 4) 组装记录
    if rec is None:
        # 纯 cv 输入且专栏存在
        if not art:
            return 1
        author_obj = art.get("author") or {}
        rec = {
            "type": "DYNAMIC_TYPE_ARTICLE",
            "author": author_obj.get("name") if isinstance(author_obj, dict) else str(author_obj or ""),
            "face": https_fix(
                (author_obj.get("upic") or author_obj.get("head_url") or "")
                if isinstance(author_obj, dict)
                else ""
            ),
            "pub_ts": int(art.get("publish_time") or 0),
            "title": art.get("title") or "",
            "text": "",
            "pics": [],
            "stats": {},
            "link": "",
            "url": f"https://www.bilibili.com/read/cv{cv_id}",
            "rid": cv_id,
            "orig": None,
        }
        dyn_id = str(art.get("dyn_id_str") or "") or None

    # 5) 内容 blocks
    title = (art.get("title") if art else "") or rec.get("title") or ""
    cover = pick_cover(art, rec.get("pics") or [])
    if art:
        norm = normalize_article(art)
        blocks = norm["blocks"]
        if not title:
            title = norm["title"]
    else:
        blocks = []
        if rec.get("text"):
            blocks.append({"type": "text", "value": rec["text"]})
        for u in rec.get("pics") or []:
            blocks.append({"type": "pic", "url": u, "caption": ""})
    if not title:  # 图文无标题 → 取正文首行
        first = next((b["value"] for b in blocks if b["type"] == "text"), "")
        title = first.split("\n")[0][:40] or "动态"
    body_html = blocks_to_html(blocks)

    # 6) 转发原文
    orig_html = ""
    if rec.get("orig"):
        o = rec["orig"]
        otitle = o.get("title") or ""
        oblocks = []
        if o.get("text"):
            oblocks.append({"type": "text", "value": o["text"]})
        for u in o.get("pics") or []:
            oblocks.append({"type": "pic", "url": u, "caption": ""})
        head = f'<p><strong>{html.escape(otitle)}</strong></p>' if otitle else ""
        orig_html = (
            f'<div class="orig"><span class="orig-tag">被转发的原文</span>'
            f"<p><em>{html.escape(o.get('author') or '')} · {fmt_ts(o.get('pub_ts') or 0)}</em></p>"
            f"{head}{blocks_to_html(oblocks)}</div>"
        )
        if not cover:
            cover = pick_cover(None, o.get("pics") or [])

    # 7) 统计 / 来源 / 链接
    art_stats = (art or {}).get("stats") or {}
    dyn_stats = rec.get("stats") or {}
    stats_html = stats_line(art_stats, dyn_stats)
    type_label = TYPE_NAME.get(rec.get("type", ""), "")
    if cv_id:
        source_note = (
            f"来源：哔哩哔哩专栏 cv{cv_id}"
            + (f"（动态 opus {dyn_id}）" if dyn_id else "")
            + f"，保存于 {time.strftime('%Y-%m-%d')}"
        )
    else:
        source_note = f"来源：哔哩哔哩动态 opus {dyn_id}，保存于 {time.strftime('%Y-%m-%d')}"
    links: list[tuple[str, str]] = []
    if cv_id:
        links.append((f"https://www.bilibili.com/read/cv{cv_id}", f"专栏 cv{cv_id}"))
    if dyn_id:
        links.append((f"https://t.bilibili.com/{dyn_id}", "原动态链接"))
    if rec.get("url") and rec["url"] not in [h for h, _ in links]:
        links.append((rec["url"], "内容链接"))

    # 8) 输出路径 + 归档登记
    suffix = f"opus{dyn_id}" if dyn_id else f"cv{cv_id}"
    fname = f"{safe_part(rec.get('author') or '未知作者', 24)}-{safe_part(title, 60)}-{suffix}.html"
    aid = None
    if args.out:
        out_path = Path(args.out)  # 相对路径按 cwd 解析（标准 CLI 语义）
    elif args.archive is not None:
        aid = next_archive_id() if args.archive == "auto" else args.archive
        check_archive_id(aid)
        # 页名统一 index.html：站点访问 site/archive/<id>/ 目录即直达
        out_path = ARCHIVE_ROOT / aid / "index.html"
    else:
        out_path = INBOX_DIR / fname  # 默认草稿区，不登记索引

    if args.print_name:
        try:
            print(out_path.resolve().relative_to(ROOT).as_posix())
        except ValueError:
            print(out_path.resolve())
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 返回主页链接随输出目录深度自适应
    back = os.path.relpath(ROOT / "index.html", out_path.resolve().parent).replace("\\", "/")
    page = render_page(
        title=title,
        author=rec.get("author") or "未知作者",
        face=rec.get("face") or "",
        pub_ts=rec.get("pub_ts") or 0,
        type_label=type_label,
        cover=cover,
        body_html=body_html,
        orig_html=orig_html,
        stats_html=stats_html,
        source_note=source_note,
        links=links,
        back_href=back,
    )
    out_path.write_text(page, encoding="utf-8")

    pics_n = sum(1 for b in blocks if b["type"] == "pic") + (1 if cover else 0)
    text_n = sum(len(b.get("value") or "") for b in blocks if b["type"] == "text")
    print(f"标题: {title}")
    print(f"作者: {rec.get('author')}  时间: {fmt_ts(rec.get('pub_ts') or 0)}  类型: {type_label}")
    print(f"正文 {text_n} 字 · 配图 {pics_n} 张 · 原文段落 {len(blocks)} 段")
    try:
        rel = out_path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        rel = out_path.resolve().as_posix()
    print(f"已保存: {rel}")

    if aid:
        file_rel = f"site/archive/{aid}/index.html"
        desc = (
            f"cv{cv_id} by {rec.get('author')}" if cv_id
            else f"{suffix} by {rec.get('author')}"
        )
        tag = "B站专栏" if cv_id else "B站动态"
        register_entry(aid, title or "未命名", file_rel, _beijing_now(), desc, tag)
        print(f"[索引] 已登记 data.json: id={aid}  file={file_rel}  zone=new")
        print(f"[访问] 站内目录地址即直达: site/archive/{aid}/")
        print("下一步（自动发现本页、下载图片并改本地引用）: node site\\scripts\\localize_images.js")
    elif not args.out:
        print(f"[草稿] 未登记索引；确认后重跑加 --archive 正式归档: "
              f"python site\\scripts\\save_bilibili_opus.py {args.url} --archive")
        print("图片本地化: node site\\scripts\\localize_images.js（自动扫描 site\\archive\\ 含 _inbox）")
    else:
        print(f'下载图片: python site\\scripts\\download_html_images.py "{rel}" '
              f'-o "<页面目录>\\images\\<名>"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
