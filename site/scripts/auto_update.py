#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动更新动态（每小时一轮）。

唯一开关在仓库根的 auto_update.json（只能手动改文件，没有命令行开关）:
    {"enabled": true}    /= 开启
    {"enabled": false}   /= 关闭（定时任务照常触发，但什么都不做，只写一行日志）

每轮动作:
    1) 增量抓取 ARCHIVE_DIRS 登记的全部账号动态（爬虫自带增量模式，追平即停）
    2) node site\\scripts\\localize_images.js —— 新图下载 + 页面图片本地化
    3) 刷新 data.json 里动态页的「共 N 条」与更新时间
    4) git 提交 + 推送（只提交本轮会动的路径；推送失败只记日志，下轮自动重试）

用法（一般通过仓库根的 auto.cmd，不直接调本脚本）:
    auto.cmd            安装每小时计划任务（已装则刷新）+ 立即跑一轮
    auto.cmd once       立即跑一轮
    auto.cmd status     看开关 / 任务状态 / 最近日志
    auto.cmd log        打印完整日志
    auto.cmd loop       不要计划任务时：前台常驻每小时一轮
    auto.cmd uninstall  移除计划任务（不改开关）

脚本参数: --once（默认）/ --loop / --status / --install / --uninstall
日志: site\\auto_update.log（超 2MB 轮转为 .old；每步输出另存 site\\auto_update.step.log）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Windows 控制台默认 GBK，中文输出强制 UTF-8
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---- 目录基准（以脚本自身位置定位，不依赖运行时 cwd）----
SCRIPT_DIR = Path(__file__).resolve().parent     # ...\<仓库根>\site\scripts
SITE = SCRIPT_DIR.parent                         # ...\<仓库根>\site
ROOT = SITE.parent                               # 仓库根（index.html / data.json 所在）

CONFIG_FILE = ROOT / "auto_update.json"
LOCK_FILE = SITE / "auto_update.lock"
LOG_FILE = SITE / "auto_update.log"
STEP_FILE = SITE / "auto_update.step.log"
TASK_NAME = "xzy_archive_auto_update"

INTERVAL = 3600            # 固定每小时一轮
LOCK_STALE = 3 * 3600      # 超过 3 小时的锁视为残留
LOG_MAX = 2 * 1024 * 1024
STEP_TIMEOUT = 3000        # 单步最长 50 分钟
COMMIT_MSG = "auto update feeds"
PYTHONW = Path(sys.executable).with_name("pythonw.exe")


# ---------------------------------------------------------------- 日志
def _rotate() -> None:
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX:
            old = LOG_FILE.with_name(LOG_FILE.name + ".old")
            if old.exists():
                old.unlink()
            LOG_FILE.rename(old)
    except Exception:
        pass


def log(msg: str, echo: bool = True) -> None:
    _rotate()
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    if echo:
        try:
            print(line, flush=True)
        except Exception:
            pass


# ---------------------------------------------------------------- 开关
def load_config() -> dict:
    """读唯一开关：缺文件 → 默认开启并生成；解析失败 → 按开启处理。"""
    if not CONFIG_FILE.exists():
        try:
            CONFIG_FILE.write_text('{\n  "enabled": true\n}\n', encoding="utf-8")
            log(f"[config] 未找到配置文件，已生成默认开启: {CONFIG_FILE}")
        except Exception as e:
            log(f"[config] 生成默认配置失败: {e}")
        return {"enabled": True}
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
    except Exception as e:
        log(f"[config] 解析失败（按开启处理）: {e}")
        return {"enabled": True}
    if not isinstance(cfg, dict):
        return {"enabled": True}
    val = cfg.get("enabled", True)
    if isinstance(val, str):
        val = val.strip().lower() in ("true", "1", "yes", "on", "开启", "开")
    return {"enabled": bool(val)}


# ---------------------------------------------------------------- 并发锁
def acquire_lock() -> bool:
    try:
        if LOCK_FILE.exists():
            age = time.time() - LOCK_FILE.stat().st_mtime
            if age < LOCK_STALE:
                return False
            LOCK_FILE.unlink()
        LOCK_FILE.write_text(f"{os.getpid()} {int(time.time())}\n", encoding="utf-8")
        return True
    except Exception:
        return True


def release_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except Exception:
        pass


# ---------------------------------------------------------------- 外部步骤
def _tail(path: Path, n: int = 10) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    return [ln.rstrip() for ln in text.splitlines() if ln.strip()][-n:]


def _node_exe() -> str | None:
    """定位 node：PATH 优先，Windows 常见安装路径兜底。"""
    from shutil import which

    hit = which("node")
    if hit:
        return hit
    for cand in (
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Program Files (x86)\nodejs\node.exe",
    ):
        if Path(cand).exists():
            return cand
    return None


def run_step(name: str, cmd: list[str]) -> int:
    """跑一步外部命令（爬虫 / localize）：输出落盘进 step 日志，不占管道。"""
    t0 = time.time()
    try:
        STEP_FILE.write_text("$ " + " ".join(cmd) + "\n", encoding="utf-8")
    except Exception:
        pass
    rc = 1
    try:
        with open(STEP_FILE, "a", encoding="utf-8") as fh:
            p = subprocess.run(
                cmd, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT,
                timeout=STEP_TIMEOUT,
            )
        rc = p.returncode
    except Exception as e:
        log(f"[{name}] 启动失败: {e}")
    log(f"[{name}] {'完成' if rc == 0 else f'异常 rc={rc}'}（{int(time.time() - t0)}s）")
    for line in _tail(STEP_FILE):
        log("    " + line, echo=False)
    return rc


# ---------------------------------------------------------------- 索引同步
def archive_dirs() -> dict[str, str]:
    """uid → 存档目录，直接取自主爬虫的 ARCHIVE_DIRS（唯一事实来源）。"""
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        from crawl_bilibili_dynamic import ARCHIVE_DIRS  # type: ignore

        return dict(ARCHIVE_DIRS)
    except Exception as e:
        log(f"[config] 读取 ARCHIVE_DIRS 失败: {e}")
        return {}


def since_for(aid: str) -> str | None:
    """从 data.json 里该存档的 desc 推断抓取起点（如「…，2026-07-30 以来」）。

    存档页自己的说明就是唯一事实来源：desc 写了日期范围，抓取就只用该范围，
    避免自动化把范围外的老动态也灌进来；没写日期就是全时间。
    """
    p = ROOT / "data.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    it = next((x for x in (data.get("items") or []) if x.get("id") == aid), None)
    if not it:
        return None
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s*以来", it.get("desc") or "")
    return m.group(1) if m else None


def refresh_data_json(dirs: dict[str, str]) -> list[str]:
    """把动态页的「共 N 条」与时间刷成最新抓取结果，返回被改动的 aid 列表。"""
    p = ROOT / "data.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception as e:
        log(f"[index] data.json 解析失败: {e}")
        return []
    items = data.get("items") or []
    now = time.strftime("%Y-%m-%d %H:%M")
    touched: list[str] = []
    for uid, rel in dirs.items():
        aid = Path(rel).name
        jf = ROOT / rel / f"{uid}_dynamic.json"
        if not jf.exists():
            continue
        try:
            n = len(json.loads(jf.read_text(encoding="utf-8-sig")))
        except Exception as e:
            log(f"[index] 读取 {jf.name} 失败: {e}")
            continue
        it = next((x for x in items if x.get("id") == aid), None)
        if not it:
            continue
        old_desc = it.get("desc") or ""
        new_desc = re.sub(r"共\s*\d+\s*条", f"共 {n} 条", old_desc)
        if new_desc != old_desc:
            it["desc"] = new_desc
            it["time"] = now
            log(f"[index] {aid}: {old_desc} → {new_desc}")
            touched.append(aid)
    if touched:
        data["updated"] = time.strftime("%Y/%m/%d")
        try:
            p.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except Exception as e:
            log(f"[index] 写入 data.json 失败: {e}")
            return []
    return touched


# ---------------------------------------------------------------- 提交推送
def _git(args: list[str], timeout: int = 600):
    """git 调用：输出写临时文件（沙盒禁止管道捕获 stdout）。"""
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
    out = SITE / "_auto_update_git.out"
    try:
        with open(out, "w", encoding="utf-8", errors="replace") as fh:
            r = subprocess.run(
                ["git", *args], cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT,
                timeout=timeout, env=env,
            )
        text = out.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    finally:
        try:
            out.unlink()
        except Exception:
            pass
    return getattr(r, "returncode", 1), text


def _git_ok(args: list[str], timeout: int = 600) -> tuple[bool, str]:
    rc, text = _git(args, timeout)
    return (rc == 0), text


def publish(dirs: dict[str, str], extra: list[str]) -> None:
    """只提交本轮会动的路径；有未推提交就尝试推送（失败只记日志）。"""
    if not (ROOT / ".git").exists():
        log("[git] 不是 git 仓库，跳过发布")
        return
    paths = ["data.json"] + [rel for rel in dirs.values()] + extra
    _git_ok(["add", "-A", "--", *paths])
    ok, staged = _git_ok(["diff", "--cached", "--name-only", "--", *paths])
    files = [ln for ln in staged.splitlines() if ln.strip()] if ok else []
    if not files:
        log("[git] 本轮无改动")
        return
    ok, text = _git_ok(["commit", "-m", COMMIT_MSG])
    if not ok:
        last = (text.strip().splitlines() or ["未知"])[-1]
        log(f"[git] 提交失败（改动保留在工作区）: {last}")
        return
    log(f"[git] 已提交 {len(files)} 个文件（{COMMIT_MSG}）")
    ok, text = _git_ok(["rev-list", "--count", "@{u}..HEAD"])
    try:
        n = int(text.strip() or 0) if ok else 0
    except Exception:
        n = 0
    if n <= 0:
        log("[git] 没有待推送的提交")
        return
    ok, text = _git_ok(["push"], timeout=900)
    if ok:
        log(f"[git] 已推送 {n} 个提交")
    else:
        last = (text.strip().splitlines() or ["未知错误"])[-1]
        log(f"[git] 推送失败（提交保留，下轮自动重试）: {last}")


# ---------------------------------------------------------------- 一轮
def cycle() -> int:
    cfg = load_config()
    if not cfg["enabled"]:
        log("[skip] 自动更新已关闭（auto_update.json: enabled=false）")
        return 0
    if not acquire_lock():
        log("[skip] 上一轮仍在运行（site\\auto_update.lock）")
        return 0
    t0 = time.time()
    ok = True
    try:
        log("=== 自动更新开始 ===")
        dirs = archive_dirs()
        if not dirs:
            log("[crawl] 无可抓取账号（ARCHIVE_DIRS 为空）")
            return 0
        py = sys.executable
        for uid, rel in dirs.items():
            aid = Path(rel).name
            cmd = [py, "-u", str(SCRIPT_DIR / "crawl_bilibili_dynamic.py"), uid]
            since = since_for(aid)
            if since:
                cmd += ["--since", since]
                log(f"[crawl] {aid} 抓取范围: {since} 以来（取自 data.json 说明）")
            rc = run_step(f"crawl {aid}({uid})", cmd)
            ok = ok and rc == 0
        node = _node_exe()
        if node:
            rc = run_step("localize", [node, str(SCRIPT_DIR / "localize_images.js")])
            ok = ok and rc == 0
        else:
            log("[localize] 未找到 node，跳过图片本地化（新图会保留远程链接）")
            ok = False
        extra = ["local_articles.json"]
        touched = refresh_data_json(dirs)
        if touched:
            log(f"[index] 已刷新: {', '.join(sorted(set(touched)))}")
        publish(dirs, extra)
        log(f"=== 自动更新结束{'（有步骤异常，详见上方日志）' if not ok else ''}（{int(time.time() - t0)}s）===")
    finally:
        release_lock()
    return 0 if ok else 1


# ---------------------------------------------------------------- 计划任务
def _task_xml() -> str:
    """每小时整点触发、无窗口（pythonw）、错过则尽快补跑。"""
    tr = f'cmd /c ""{PYTHONW}" "{SCRIPT_DIR / "auto_update.py"}" --once"'
    start = time.strftime("%Y-%m-%dT%H:%M:%S")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>xzy_archive 自动更新动态（每小时一轮；唯一开关在 auto_update.json）</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
      <Repetition>
        <Interval>PT1H</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowHardTerminate>true</AllowHardTerminate>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{PYTHONW}</Command>
      <Arguments>"{SCRIPT_DIR / "auto_update.py"}" --once</Arguments>
      <WorkingDirectory>{ROOT}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _run_capture(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    """执行命令并捕获输出（沙盒禁止管道 → 写临时文件再读回）。"""
    out = SITE / "_auto_update_cmd.out"
    try:
        with open(out, "w", encoding="utf-8", errors="replace") as fh:
            r = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout)
        text = out.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"
    finally:
        try:
            out.unlink()
        except Exception:
            pass
    return getattr(r, "returncode", 1), text


def install() -> int:
    xml = SITE / "_auto_update_task.xml"
    try:
        xml.write_text(_task_xml(), encoding="utf-16")
    except Exception as e:
        print(f"[error] 写入任务定义失败: {e}")
        return 1
    rc, text = _run_capture(
        ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml), "/F"]
    )
    try:
        xml.unlink()
    except Exception:
        pass
    if rc != 0:
        print("[error] 注册计划任务失败:")
        print(text.strip())
        print("提示: 可用 `auto.cmd loop` 以前台常驻方式代替计划任务。")
        return 1
    print(f"[ok] 已注册计划任务 {TASK_NAME}（每小时整点触发，静默运行）")
    print(f"     开关: {CONFIG_FILE} → enabled")
    return 0


def uninstall() -> int:
    rc, text = _run_capture(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    if rc != 0:
        print(f"[warn] 删除计划任务失败（可能本来就没有）: {text.strip()}")
        return 0
    print(f"[ok] 已移除计划任务 {TASK_NAME}（auto_update.json 的开关未改动）")
    return 0


def task_info() -> str:
    rc, text = _run_capture(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"])
    if rc != 0:
        return "未注册（用 auto.cmd 安装，或 auto.cmd loop 常驻）"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    keep = [
        ln for ln in lines
        if re.search(r"(Next Run Time|Status|下次运行时间|状态)", ln)
    ]
    return " | ".join(keep) if keep else "已注册"


# ---------------------------------------------------------------- 状态
def status() -> int:
    cfg = load_config()
    print(f"开关文件: {CONFIG_FILE}")
    try:
        print(f"  内容 : {' '.join(CONFIG_FILE.read_text(encoding='utf-8-sig').split())}")
    except Exception:
        print("  内容 : （读取失败）")
    print(f"  状态 : {'开启' if cfg['enabled'] else '关闭'}    间隔: 每 {INTERVAL // 60} 分钟")
    print(f"计划任务: {task_info()}")
    if LOCK_FILE.exists():
        print(f"当前: 正在跑一轮（lock {int(time.time() - LOCK_FILE.stat().st_mtime)}s）")
    else:
        print("当前: 空闲")
    print(f"日志: {LOG_FILE}")
    if LOG_FILE.exists():
        try:
            lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
            print("最近日志:")
            for ln in lines[-15:]:
                print("  " + ln)
        except Exception:
            pass
    return 0


def show_log() -> int:
    if not LOG_FILE.exists():
        print(f"[log] 还没有日志: {LOG_FILE}")
        return 0
    text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    print(text)
    print(f"（共 {len(text.splitlines())} 行，{LOG_FILE}）")
    return 0


ALIASES = {
    "init": "--init", "install": "--install", "setup": "--install",
    "once": "--once", "now": "--once", "run": "--once",
    "status": "--status", "st": "--status",
    "log": "--log",
    "loop": "--loop",
    "uninstall": "--uninstall", "remove": "--uninstall", "rm": "--uninstall",
}


def main() -> int:
    raw = sys.argv[1] if len(sys.argv) > 1 else "--once"
    arg = ALIASES.get(raw.strip().lower().lstrip("-"), raw)
    if arg == "--init":  # 双击 auto.cmd：装计划任务 + 立刻跑一轮
        rc = install()
        cfg = load_config()
        if not cfg["enabled"]:
            log("[skip] 自动更新已关闭（auto_update.json: enabled=false），仅注册了计划任务")
            return rc
        return cycle() or rc
    if arg == "--status":
        return status()
    if arg == "--log":
        return show_log()
    if arg == "--install":
        return install()
    if arg == "--uninstall":
        return uninstall()
    if arg == "--loop":
        log(f"[loop] 常驻模式，每 {INTERVAL // 60} 分钟一轮（Ctrl+C 退出）")
        try:
            while True:
                t0 = time.time()
                cycle()
                wait = max(INTERVAL - (time.time() - t0), 300)
                log(f"[loop] 下一轮 {int(wait)} 秒后")
                time.sleep(wait)
        except KeyboardInterrupt:
            log("[loop] 已中断，退出")
            return 0
    return cycle()


if __name__ == "__main__":
    sys.exit(main())
