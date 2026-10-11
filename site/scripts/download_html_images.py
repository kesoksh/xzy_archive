#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键下载指定 HTML 文件中的所有图片。

用法（不依赖运行时 cwd，推荐在仓库根运行）:
    python site\\scripts\\download_html_images.py <html文件> [选项]

示例:
    python site\\scripts\\download_html_images.py 文章.html
    python site\\scripts\\download_html_images.py site\\archive\\a6\\文章.html
    python site\\scripts\\download_html_images.py 文章.html -o ./图片 --base https://example.com/ --referer https://example.com/page

功能:
    * 从 <img src> / srcset、<source srcset>、<video poster>、<link rel="image_src">、
      <meta property="og:image">、内联样式 url(...) 中提取图片地址
    * 自动解析相对路径（相对 HTML 文件位置，或用 --base 指定网页地址）
    * 跳过 data: URI；去重；按出现顺序编号命名
    * 并发下载（默认 8 线程），带重试；缺失扩展名时根据 Content-Type 补全
    * 生成 manifest.json 记录 URL 与本地文件的对应关系（-o 相对路径按当前目录解析；
      默认输出到 HTML 同目录的 images\\<html名>\\，localize_images.js 可直接接管改写 src）
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import mimetypes
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote
from urllib.request import Request, urlopen

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Windows 控制台默认 GBK，中文路径/输出会乱码，强制 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".avif", ".ico"}

CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.I)
SRCSET_SPLIT_RE = re.compile(r"\s*,\s*")


class ImageCollector(HTMLParser):
    """收集 HTML 中所有图片地址（含 srcset、poster、og:image、内联 css url()）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def _add(self, raw: str | None) -> None:
        if raw:
            raw = raw.strip()
            if raw:
                self.urls.append(raw)

    def handle_starttag(self, tag: str, attrs) -> None:
        d = dict(attrs)
        tag = tag.lower()
        if tag == "img":
            self._add(d.get("src"))
            self._add_srcset(d.get("srcset"))
        elif tag == "source":
            self._add_srcset(d.get("srcset"))
        elif tag == "video":
            self._add(d.get("poster"))
        elif tag == "link":
            rel = (d.get("rel") or "").lower()
            if "image_src" in rel or "icon" in rel:
                self._add(d.get("href"))
        elif tag == "meta":
            prop = (d.get("property") or d.get("name") or "").lower()
            if prop in ("og:image", "og:image:url", "twitter:image", "thumbnail"):
                self._add(d.get("content"))
        # 任何标签的内联样式里都可能有 url(...)
        style = d.get("style")
        if style:
            for m in CSS_URL_RE.finditer(style):
                self._add(m.group(1))

    def _add_srcset(self, srcset: str | None) -> None:
        if not srcset:
            return
        for cand in SRCSET_SPLIT_RE.split(srcset):
            cand = cand.strip()
            if cand:
                self._add(cand.split()[0])  # "url 2x" -> url

    def handle_data(self, data: str) -> None:
        # <style> 块内的 url(...)
        if "url(" in data:
            for m in CSS_URL_RE.finditer(data):
                self._add(m.group(1))


def collect_from_html(html_path: Path) -> list[str]:
    text = html_path.read_text(encoding="utf-8", errors="replace")
    parser = ImageCollector()
    try:
        parser.feed(text)
    except Exception as exc:  # 解析失败则退化为正则
        print(f"[warn] HTML 解析异常，改用正则: {exc}", file=sys.stderr)
        parser.urls = CSS_URL_RE.findall(text) + re.findall(
            r'<img[^>]+src=["\']([^"\']+)["\']', text, re.I
        )
    return parser.urls


def resolve_urls(raw_urls: list[str], html_path: Path, base: str | None) -> list[str]:
    """去重、跳过 data:/javascript: 等、解析相对地址。保持出现顺序。"""
    if base:
        # 以网页地址为基准
        base_url = base if base.endswith("/") else base
        def resolve(u: str) -> str | None:
            return urljoin(base_url, u)
    else:
        file_uri = html_path.resolve().as_uri()  # file:///...
        def resolve(u: str) -> str | None:
            if u.startswith(("http://", "https://", "file://")):
                return u
            return urljoin(file_uri, u)

    seen: set[str] = set()
    out: list[str] = []
    for raw in raw_urls:
        u = raw.strip()
        if not u or u.startswith(("data:", "javascript:", "about:", "blob:")):
            continue
        full = resolve(u)
        if not full:
            continue
        if not full.startswith(("http://", "https://", "file://")):
            continue
        if full in seen:
            continue
        seen.add(full)
        out.append(full)
    return out


def ext_from_url(url: str) -> str:
    path = unquote(urlparse(url).path)
    ext = Path(path).suffix.lower()
    if ext in IMG_EXTS:
        return ext
    return ""


def filename_for(index: int, url: str, content_type: str | None) -> str:
    path = unquote(urlparse(url).path)
    stem = Path(path).stem
    stem = re.sub(r"[^\w\-一-鿿]+", "_", stem).strip("_")[:60] or "image"
    ext = ext_from_url(url)
    if not ext and content_type:
        guess = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guess:
            ext = guess
            if ext == ".jpe":
                ext = ".jpg"
    if not ext:
        ext = ".jpg"
    return f"{index:03d}_{stem}{ext}"


def download(url: str, dest_dir: Path, index: int, referer: str | None, timeout: float,
             retries: int) -> dict:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            headers = {"User-Agent": UA, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
            if referer:
                headers["Referer"] = referer
            req = Request(url, headers=headers)
            with urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                ctype = resp.headers.get("Content-Type")
            if not data:
                raise RuntimeError("empty response")
            name = filename_for(index, url, ctype)
            dest = dest_dir / name
            if dest.exists():  # 重名时加短哈希
                short = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
                dest = dest_dir / f"{index:03d}_{short}{dest.suffix}"
            dest.write_bytes(data)
            return {
                "index": index,
                "url": url,
                "file": dest.name,
                "bytes": len(data),
                "ok": True,
            }
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < retries:
                time.sleep(1.5 * attempt)
    return {"index": index, "url": url, "file": None, "ok": False, "error": str(last_err)}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="下载指定 HTML 文件里的所有图片",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("html", help="HTML 文件路径")
    ap.add_argument("-o", "--out", default=None,
                    help="图片保存目录（默认：<html 所在目录>\\images\\<html名>，"
                         "与归档约定一致，localize_images.js 可直接发现其 manifest）")
    ap.add_argument("--base", default=None,
                    help="相对图片地址的基准 URL（默认以 HTML 文件所在目录为基准）")
    ap.add_argument("--referer", default=None,
                    help="下载时发送的 Referer 头（防盗链站点需要，如 https://www.bilibili.com/）")
    ap.add_argument("--workers", type=int, default=8, help="并发线程数，默认 8")
    ap.add_argument("--timeout", type=float, default=30.0, help="单张超时秒数，默认 30")
    ap.add_argument("--retries", type=int, default=3, help="每张图重试次数，默认 3")
    ap.add_argument("--manifest", action="store_true",
                    help="额外输出 manifest.json（默认也生成）")
    args = ap.parse_args()

    html_path = Path(args.html)
    if not html_path.is_file():
        print(f"[error] 找不到 HTML 文件: {html_path}", file=sys.stderr)
        return 1

    raw = collect_from_html(html_path)
    urls = resolve_urls(raw, html_path, args.base)
    print(f"HTML: {html_path}")
    print(f"提取到 {len(raw)} 处图片引用，去重后 {len(urls)} 个地址")

    if not urls:
        print("没有可下载的图片。")
        return 0

    # 默认落到 HTML 同目录的 images\<html名>\：既不污染运行时 cwd（仓库根只放入口文件），
    # 又符合 archive 约定，localize_images.js 扫描 images\*\manifest.json 时能直接接管。
    out_dir = (
        Path(args.out)
        if args.out
        else html_path.parent / "images" / html_path.with_suffix("").name
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"保存目录: {out_dir.resolve()}")

    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(download, u, out_dir, i + 1, args.referer, args.timeout, args.retries): u
            for i, u in enumerate(urls)
        }
        for fut in concurrent.futures.as_completed(futures):
            r = fut.result()
            results.append(r)
            mark = "ok " if r["ok"] else "FAIL"
            detail = f"{r['bytes']} B" if r["ok"] else r.get("error", "")
            print(f"  [{mark}] {r['index']:03d} {r['file'] or ''} <- {r['url']}  {detail}")

    results.sort(key=lambda r: r["index"])
    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {"html": str(html_path), "total": len(urls), "ok": len(ok), "failed": len(bad),
             "items": results},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n完成: 成功 {len(ok)} / 失败 {len(bad)}，清单已写入 {manifest_path}")
    return 0 if not bad else 2


if __name__ == "__main__":
    sys.exit(main())
