/**
 * 页面图片本地化：把归档页 <img src> 从远程图床改写为本地相对路径（离线自包含）。
 *
 * 用法（以脚本自身位置定位归档根，不依赖运行时 cwd）:
 *     node site\scripts\localize_images.js
 *
 * 处理范围:
 *   1) JOBS 显式清单（a1~a5 五个已知存档页，含顺序编号与 manifest 模式）；
 *   2) 自动扫描 site\archive\*\ 顶层的全部 HTML（含 _inbox 草稿与 save --archive
 *      新建的 aN 目录）—— 新存档无需改任何代码，跑一次本脚本即完成:
 *      首次备份到 backup_original\ → 查 images\ 下的 manifest 做 url→文件 映射 →
 *      缺失的图自动下载 → 改写 src 为本地引用。
 *
 * 说明:
 *   * 存档页页名统一为 index.html（站点访问 …/site/archive/<aid>/ 目录即直达）；
 *     backup_original\ 里的老备份保留原描述性文件名，勿改名。
 *   * crawl_bilibili_dynamic.py 每次重跑都会把 site\archive\a3\index.html
 *     重新渲染成远程图床链接 —— 更新后请再跑一次本脚本恢复本地化。
 *   * manifest 查找顺序: images\manifest.json → images\*\manifest.json（含
 *     download_html_images.py 生成的清单）；完全没有清单时新建 images\manifest.json。
 *   * order 模式（小章鱼交代_cv53304354）: 按 01_ 02_… 编号与页面 <img> 顺序一一对应。
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const SITE = path.resolve(__dirname, '..'); // ...\<仓库根>\site
const ROOT = path.resolve(SITE, '..');      // 仓库根（index.html / data.json 所在）
const ARCHIVE = path.join(SITE, 'archive');

// JOBS = 已知存档页的显式任务。aid=索引 id；页名统一为 index.html（站点访问
// …/site/archive/<aid>/ 目录即直达）；img 是图片目录（相对页面所在目录），
// 也是写回 <img src> 的相对前缀。
const JOBS = [
  { aid: 'a1', html: 'index.html', img: 'images/小章鱼交代_cv53304354', mode: 'order' },
  { aid: 'a2', html: 'index.html', img: 'images/cv53316951', mode: 'manifest' },
  { aid: 'a3', html: 'index.html', img: 'images/主账号动态页_37069', mode: 'manifest', fetchMissing: true },
  { aid: 'a4', html: 'index.html', img: 'images/旧号动态_11930395', mode: 'manifest' },
  { aid: 'a5', html: 'index.html', img: 'images/黑嘴酱_cv53382393', mode: 'manifest' },
];

async function download(url, dirAbs) {
  const res = await fetch(url, {
    headers: { 'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0' },
  });
  if (!res.ok) throw new Error('HTTP ' + res.status);
  const buf = Buffer.from(await res.arrayBuffer());
  const m = url.split('?')[0].match(/\.(png|jpe?g|gif|webp)$/i);
  const ext = m ? (m[1].toLowerCase() === 'jpe' ? '.jpg' : '.' + m[1].toLowerCase()) : '.jpg';
  const hash = crypto.createHash('sha1').update(url).digest('hex').slice(0, 10);
  const name = `图_${hash}${ext}`; // ext 含点号
  fs.writeFileSync(path.join(dirAbs, name), buf);
  return { name, bytes: buf.length };
}

/** 读 JSON：容忍 UTF-8 BOM（记事本/PowerShell 保存的 manifest 常带 BOM）。 */
function readJson(file) {
  const txt = fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, '');
  return JSON.parse(txt);
}

/** 收集页面目录下所有 manifest.json（images\ 一层及其子目录一层）。 */
function findManifests(pageDir) {
  const out = [];
  const imgRoot = path.join(pageDir, 'images');
  const add = (dir, relPrefix) => {
    const mfPath = path.join(dir, 'manifest.json');
    if (fs.existsSync(mfPath)) out.push({ dir, relPrefix, mfPath });
  };
  if (!fs.existsSync(imgRoot)) return out;
  add(imgRoot, 'images');
  for (const name of fs.readdirSync(imgRoot)) {
    const sub = path.join(imgRoot, name);
    if (fs.statSync(sub).isDirectory()) add(sub, `images/${name}`);
  }
  return out;
}

/**
 * 首次处理页面时把远程原版备份到 backup_original\。
 * 判定从「同名文件是否存在」放宽为「目录里是否已有任何 .html」——页名统一改为
 * index.html 后，老备份仍保留描述性文件名，避免同一目录出现重复备份。
 */
function backupOnce(pageDir, htmlFile, content) {
  const bakDir = path.join(pageDir, 'backup_original');
  fs.mkdirSync(bakDir, { recursive: true });
  const has = fs.readdirSync(bakDir).some((f) => /\.html$/i.test(f));
  if (has) return;
  fs.writeFileSync(path.join(bakDir, htmlFile), content, 'utf8');
}

/**
 * 处理一个页面：urls=页面上的远程图片地址；map 回调负责产出 url→本地相对路径。
 * 返回 [newHtml, replaced, notFound]。改写 + 备份 + 写回对两种模式共用。
 */
function rewrite(html, htmlPath, pageDir, htmlFile, map) {
  backupOnce(pageDir, htmlFile, html);
  let replaced = 0;
  let notFound = 0;
  const out = html.replace(/src="(https?:[^"]+)"/g, (m, u) => {
    const local = map.get(u);
    if (local) {
      replaced++;
      return `src="${local}"`;
    }
    notFound++;
    return m; // 无本地副本 → 保留远程兜底
  });
  fs.writeFileSync(htmlPath, out, 'utf8');
  return [replaced, notFound];
}

/** JOBS 显式任务（manifest / order 两种既有模式）。 */
async function processJob(job) {
  const pageDir = path.join(ARCHIVE, job.aid);
  const htmlPath = path.join(pageDir, job.html);
  const dirAbs = path.join(pageDir, job.img.replace(/\//g, '\\'));
  if (!fs.existsSync(htmlPath) || !fs.existsSync(dirAbs)) {
    console.log(`[跳过] 缺少 site/archive/${job.aid}/${job.html} 或 ${job.img}`);
    return;
  }
  let html = fs.readFileSync(htmlPath, 'utf8');
  const urls = [...new Set([...html.matchAll(/src="(https?:[^"]+)"/g)].map((m) => m[1]))];
  if (!urls.length) {
    console.log(`[无需处理] site/archive/${job.aid}/${job.html}（已是本地引用）`);
    return;
  }
  const map = new Map();

  if (job.mode === 'manifest') {
    const mfPath = path.join(dirAbs, 'manifest.json');
    const mf = readJson(mfPath);
    for (const it of mf.items || []) {
      if (it.ok && it.file) map.set(it.url, `${job.img}/${it.file}`);
    }
    const missing = urls.filter((u) => !map.has(u));
    if (missing.length && job.fetchMissing) {
      const mfList = (mf.items = mf.items || []);
      let ni = mfList.reduce((m, x) => Math.max(m, x.index || 0), 0);
      for (const u of missing) {
        try {
          const { name, bytes } = await download(u, dirAbs);
          ni += 1;
          mfList.push({ index: ni, url: u, file: name, bytes, ok: true, _added_by: 'localize_images.js' });
          map.set(u, `${job.img}/${name}`);
          console.log(`  [补下载] ${name} (${bytes} B)`);
        } catch (e) {
          console.log(`  [下载失败] ${u} : ${e.message}`);
        }
      }
      fs.writeFileSync(mfPath, JSON.stringify(mf, null, 2), 'utf8');
    } else if (missing.length) {
      missing.forEach((u) => console.log(`  [缺图] 本地无此图: ${u}`));
    }
  } else {
    const files = fs
      .readdirSync(dirAbs)
      .filter((f) => /\.(png|jpe?g|gif|webp)$/i.test(f))
      .sort((a, b) => parseInt(a, 10) - parseInt(b, 10));
    if (files.length !== urls.length) {
      console.log(`[跳过] site/archive/${job.aid}/${job.html}: 页面图片 ${urls.length} 张 != 本地 ${files.length} 张，顺序映射不可靠`);
      return;
    }
    urls.forEach((u, i) => map.set(u, `${job.img}/${files[i]}`));
  }

  const [replaced, notFound] = rewrite(html, htmlPath, pageDir, job.html, map);
  console.log(`[完成] site/archive/${job.aid}/${job.html}: 本地化 ${replaced} 处，保留远程 ${notFound} 处`);
}

/** 自动发现: site\archive\ 下每个目录顶层的 HTML（JOBS 已处理的跳过）。 */
async function processDiscovered(pageDir, htmlFile, label) {
  const htmlPath = path.join(pageDir, htmlFile);
  let html = fs.readFileSync(htmlPath, 'utf8');
  const urls = [...new Set([...html.matchAll(/src="(https?:[^"]+)"/g)].map((m) => m[1]))];
  if (!urls.length) {
    console.log(`[无需处理] ${label}（已是本地引用）`);
    return;
  }

  // 1) 已有 manifest → url→文件 映射（覆盖 download_html_images.py 的产物）
  const map = new Map();
  const manifests = findManifests(pageDir);
  for (const { dir, relPrefix, mfPath } of manifests) {
    try {
      const mf = readJson(mfPath);
      for (const it of mf.items || []) {
        if (it.ok && it.file) map.set(it.url, `${relPrefix}/${it.file}`);
      }
    } catch (e) {
      console.log(`  [warn] manifest 解析失败 ${mfPath}: ${e.message}`);
    }
  }

  // 2) 缺失的图 → 下载进 images\（首个已有 manifest 的目录，否则 images\ 根）
  const missing = urls.filter((u) => !map.has(u));
  let targetDir = path.join(pageDir, 'images');
  let mfPath = path.join(targetDir, 'manifest.json');
  if (manifests.length) {
    targetDir = manifests[0].dir;
    mfPath = manifests[0].mfPath;
  }
  if (missing.length) {
    fs.mkdirSync(targetDir, { recursive: true });
    let mf;
    try {
      mf = readJson(mfPath);
    } catch (e) {
      mf = { html: htmlFile, items: [] };
    }
    const mfList = (mf.items = mf.items || []);
    // 已失败/已有的条目不重复计数；按现有最大 index 续号
    let ni = mfList.reduce((m, x) => Math.max(m, x.index || 0), 0);
    for (const u of missing) {
      try {
        const { name, bytes } = await download(u, targetDir);
        ni += 1;
        mfList.push({ index: ni, url: u, file: name, bytes, ok: true, _added_by: 'localize_images.js' });
        const relPrefix = manifests.length
          ? manifests[0].relPrefix
          : 'images';
        map.set(u, `${relPrefix}/${name}`);
        console.log(`  [下载] ${name} (${bytes} B)`);
      } catch (e) {
        console.log(`  [下载失败] ${u} : ${e.message}`);
      }
    }
    fs.writeFileSync(mfPath, JSON.stringify(mf, null, 2), 'utf8');
  }

  const [replaced, notFound] = rewrite(html, htmlPath, pageDir, htmlFile, map);
  console.log(`[完成] ${label}: 本地化 ${replaced} 处，保留远程 ${notFound} 处`);
}

async function main() {
  const handled = new Set();

  for (const job of JOBS) {
    handled.add(path.join(ARCHIVE, job.aid, job.html).toLowerCase());
    await processJob(job);
  }

  if (!fs.existsSync(ARCHIVE)) {
    console.log('[跳过] 不存在 site\\archive\\');
    return;
  }
  for (const name of fs.readdirSync(ARCHIVE)) {
    const pageDir = path.join(ARCHIVE, name);
    if (!fs.statSync(pageDir).isDirectory()) continue;
    for (const f of fs.readdirSync(pageDir)) {
      if (!/\.html$/i.test(f)) continue; // 只扫目录顶层（backup_original 等子目录不动）
      const full = path.join(pageDir, f);
      if (handled.has(full.toLowerCase())) continue;
      await processDiscovered(pageDir, f, `site/archive/${name}/${f}`);
    }
  }
}

main().catch((e) => {
  console.error('FATAL', e);
  process.exit(1);
});
