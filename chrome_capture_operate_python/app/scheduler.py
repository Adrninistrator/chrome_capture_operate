"""定时执行脚本：任务持久化 + 秒级调度循环 + 执行日志。

任务两种触发方式（prompt.md"定时执行脚本"节）：
- at（在指定时间执行）：每天 / 每个星期x / 每个月第x天 + 多个 时:分:秒
- interval（每隔一段时间执行）：从指定时间开始，每隔 N 秒执行一次；
  可指定允许执行的时间段——全部时间段（默认）或多个 起止 时:分:秒 时间段
  （调度点落在任一时间段内才执行，区间按自然日 00:00~24:00 理解，
  start<=end 为当天区间，start>end 视为跨零点区间）

任务保存在全局配置文件（global_conf.json key=schedules）；新增后默认启动；
暂停/恢复/删除/编辑只操作任务本身，不删除对应的 Python 脚本。

执行日志写到 log/scripts/{脚本所在子目录名}/{当前年份}/{脚本文件名}_{执行时间}.log，
每次执行生成单独的日志文件（stdout/stderr 逐行写入，含起止/退出码/耗时）。

调度语义：每秒 tick 一次，计算每个启动中任务的"<= 当前时刻的最近一次调度点"，
若该点晚于上次已触发的调度点（last_fired）且落在允许时间段内则触发——错过
多个周期只补触发最近一个（服务重启不重复执行历史任务），编辑任务后
last_fired 重置为当前时刻，只有未来的调度点会触发。
"""
import asyncio
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta

from . import globalconf
from .config import SCRIPT_LOG_DIR
from .executor import venv_python, validate_script_path

log = logging.getLogger("app.sched")

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def validate_schedule(sch):
    """校验 schedule 结构，返回错误信息（None 表示合法）。"""
    if not isinstance(sch, dict):
        return "定时配置不能为空"
    stype = sch.get("type")
    if stype == "at":
        mode = sch.get("mode")
        if mode not in ("daily", "weekly", "monthly"):
            return "指定时间执行的频率必须为 每天/每周/每月"
        if mode == "weekly":
            days = sch.get("weekdays") or []
            if not days or any(not isinstance(d, int) or d < 0 or d > 6
                               for d in days):
                return "每周执行需要选择星期几（周一~周日）"
        if mode == "monthly":
            days = sch.get("month_days") or []
            if not days or any(not isinstance(d, int) or d < 1 or d > 31
                               for d in days):
                return "每月执行需要指定 1~31 的日期"
        times = sch.get("times") or []
        if not times:
            return "至少指定一个执行时间（时:分:秒）"
        for t in times:
            if _parse_time(t) is None:
                return "执行时间格式需为 HH:MM:SS"
        return None
    if stype == "interval":
        if _parse(sch.get("start_at")) is None:
            return "开始时间格式需为 YYYY-MM-DD HH:MM:SS"
        try:
            iv = int(sch.get("interval_sec"))
        except (TypeError, ValueError):
            return "间隔时间必须为正整数秒"
        if iv <= 0:
            return "间隔时间必须为正整数秒"
        err = _validate_windows(sch)
        if err:
            return err
        return None
    return "定时方式必须为 指定时间执行 或 每隔一段时间执行"


def _validate_windows(sch):
    """校验允许执行的时间段（仅 interval 方式）。"""
    windows = sch.get("time_windows")
    if windows is None:
        return None  # 未配置 = 全部时间段
    if not isinstance(windows, list):
        return "允许执行的时间段格式非法"
    for w in windows:
        if not isinstance(w, dict):
            return "允许执行的时间段格式非法"
        if _parse_time(w.get("start")) is None \
                or _parse_time(w.get("end")) is None:
            return "时间段起止格式需为 HH:MM:SS"
    return None


def _parse_windows(sch):
    """时间段配置 -> [(start_time, end_time), ...]；None 表示全部时间段。"""
    windows = sch.get("time_windows")
    if windows is None:
        return None
    out = []
    for w in windows:
        s, e = _parse_time(w.get("start")), _parse_time(w.get("end"))
        if s and e:
            out.append((s, e))
    return out


def _in_windows(dt, windows):
    """时刻是否落在允许时间段内（start<=end 当天区间；start>end 跨零点）。"""
    if windows is None:
        return True
    t = dt.time()
    for s, e in windows:
        if s <= e:
            if s <= t <= e:
                return True
        else:  # 跨零点区间（如 22:00~06:00）
            if t >= s or t <= e:
                return True
    return False


def _parse_time(t):
    try:
        return datetime.strptime(t, "%H:%M:%S").time()
    except (TypeError, ValueError):
        return None


def _day_matches(d, sch):
    mode = sch.get("mode")
    if mode == "weekly":
        return d.weekday() in (sch.get("weekdays") or [])
    if mode == "monthly":
        return d.day in (sch.get("month_days") or [])
    return True  # daily


def next_run(task, now):
    """计算任务的下次执行时刻；暂停或配置非法返回 None。"""
    if not task.get("enabled"):
        return None
    sch = task.get("schedule") or {}
    if sch.get("type") == "interval":
        start = _parse(sch.get("start_at"))
        try:
            iv = int(sch.get("interval_sec", 0))
        except (TypeError, ValueError):
            return None
        if start is None or iv <= 0:
            return None
        windows = _parse_windows(sch)
        if start > now:
            cand = start
        else:
            k = int((now - start).total_seconds() // iv) + 1
            cand = start + timedelta(seconds=k * iv)
        # 有时间段限制时向后找第一个落在时间段内的调度点（上限找 1000 个，
        # 覆盖最坏情况：间隔远小于每日窗口长度，如 1s 间隔 + 每天 1 秒窗口）
        for _ in range(1000):
            if _in_windows(cand, windows):
                return cand
            cand += timedelta(seconds=iv)
        return None
    times = sorted(sch.get("times") or [])
    if not times:
        return None
    # 最多向后找一年（monthly 最坏 62 天无命中，370 天足够）
    for offset in range(0, 370):
        d = now.date() + timedelta(days=offset)
        if not _day_matches(d, sch):
            continue
        for t in times:
            tm = _parse_time(t)
            if tm is None:
                continue
            dt = datetime.combine(d, tm)
            if dt > now:
                return dt
    return None


def latest_due(task, now):
    """<= now 的最近一次调度点（用于判断是否需要触发）；无则 None。

    interval 带时间段限制时：向前找最近一个落在允许时间段内的调度点——
    时间段外的调度点视为"不存在"（不触发也不推进 last_fired，等进入
    时间段后从窗口内的下一个点继续）。
    """
    sch = task.get("schedule") or {}
    if sch.get("type") == "interval":
        start = _parse(sch.get("start_at"))
        try:
            iv = int(sch.get("interval_sec", 0))
        except (TypeError, ValueError):
            return None
        if start is None or iv <= 0 or now < start:
            return None
        k = int((now - start).total_seconds() // iv)
        cand = start + timedelta(seconds=k * iv)
        windows = _parse_windows(sch)
        if windows is None:
            return cand
        # 向前找（含当前点）最近一个在时间段内的调度点；跨零点窗口下
        # 调度点必然密集（iv 小于窗口跨度才会被配置出来），向前 1000 个足够
        for _ in range(1000):
            if _in_windows(cand, windows):
                return cand
            cand -= timedelta(seconds=iv)
            if cand < start:
                return None
        return None
    times = sorted(sch.get("times") or [], reverse=True)
    if not times:
        return None
    # 向前找最近一个命中的调度日（含当天已过的时间点）
    for offset in range(0, 370):
        d = now.date() - timedelta(days=offset)
        if not _day_matches(d, sch):
            continue
        for t in times:
            tm = _parse_time(t)
            if tm is None:
                continue
            dt = datetime.combine(d, tm)
            if dt <= now:
                return dt
    return None


def describe_schedule(sch):
    """任务的定时方式描述（列表展示用）。"""
    if not isinstance(sch, dict):
        return ""
    if sch.get("type") == "interval":
        start = sch.get("start_at", "")
        desc = "从 %s 起，每隔 %s 执行一次" % (
            start, _fmt_interval(sch.get("interval_sec")))
        windows = sch.get("time_windows")
        if windows:
            desc += "（仅 %s）" % "、".join(
                "%s~%s" % (w.get("start"), w.get("end")) for w in windows)
        else:
            desc += "（全部时间段）"
        return desc
    mode = sch.get("mode")
    times = "、".join(sch.get("times") or [])
    if mode == "weekly":
        days = "、".join(WEEKDAY_NAMES[d] for d in sorted(sch.get("weekdays") or []))
        return "每%s %s 执行" % (days or "?", times or "?")
    if mode == "monthly":
        days = "、".join("%d日" % d for d in sorted(sch.get("month_days") or []))
        return "每月%s %s 执行" % (days or "?", times or "?")
    return "每天 %s 执行" % (times or "?")


def _fmt_interval(sec):
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return "?"
    if sec % 86400 == 0:
        return "%d 天" % (sec // 86400)
    if sec % 3600 == 0:
        return "%d 小时" % (sec // 3600)
    if sec % 60 == 0:
        return "%d 分钟" % (sec // 60)
    return "%d 秒" % sec


class Scheduler:
    """任务存储 + 调度循环。线程安全（读写锁），执行不阻塞 tick。

    任务保存在全局配置文件 global_conf.json 的 schedules key 中（prompt.md
    "定时执行任务存储位置"），与脚本保存目录等全局参数同文件；conf_key
    可覆盖（测试隔离用）。
    """

    def __init__(self, config, conf_path=None, log_root=None,
                 conf_key=None):
        self.config = config
        self.conf_path = conf_path or globalconf.GLOBAL_CONF_PATH
        self.conf_key = conf_key or globalconf.KEY_SCHEDULES
        self.log_root = log_root or SCRIPT_LOG_DIR
        self.tasks = {}          # id -> task dict
        self.last_fired = {}     # id -> "YYYY-MM-DD HH:MM:SS"（最近触发的调度点）
        self._running = set()    # 正在执行的 task id
        self._results = {}       # id -> {last_run, last_result}
        self._lock = threading.Lock()
        self._loop_task = None
        self.load()

    # ---------- 持久化 ----------
    def _read_conf(self):
        """读取全局配置文件的 schedules 结构。"""
        try:
            with open(self.conf_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        sched = data.get(self.conf_key)
        return sched if isinstance(sched, dict) else {}

    def load(self):
        data = self._read_conf()
        with self._lock:
            self.tasks = {t["id"]: t for t in data.get("tasks", [])
                          if isinstance(t, dict) and t.get("id")}
            self.last_fired = {k: v for k, v in
                               (data.get("last_fired") or {}).items()
                               if k in self.tasks}
            self._results = {k: v for k, v in
                             (data.get("results") or {}).items()
                             if k in self.tasks}

    def save(self):
        with self._lock:
            data = {"tasks": list(self.tasks.values()),
                    "last_fired": dict(self.last_fired),
                    "results": self._results}
        try:
            with globalconf._lock:  # 与标量配置写入共用一把文件锁
                all_conf = globalconf._read_all(self.conf_path)
                all_conf[self.conf_key] = data
                globalconf._write_all(self.conf_path, all_conf)
        except OSError as e:
            log.error("保存定时任务失败: %s", e)

    # ---------- 任务管理 ----------
    def list_tasks(self, now=None):
        """任务列表（含运行状态/下次执行/最近执行结果）。"""
        now = now or datetime.now()
        with self._lock:
            ids = list(self.tasks.keys())
        out = []
        for tid in ids:
            with self._lock:
                t = dict(self.tasks.get(tid) or {})
                t["running"] = tid in self._running
                t["next_run"] = (_fmt(next_run(t, now))
                                 if t.get("enabled") else "")
                t.update(self._results.get(tid) or {})
            out.append(t)
        return out

    def add(self, script_path, schedule):
        err = validate_schedule(schedule)
        if err:
            return None, err
        p = validate_script_path(script_path)
        if not p:
            return None, "脚本不存在或不在允许的脚本目录内"
        task = {"id": uuid.uuid4().hex[:8],
                "script_path": p,
                "schedule": schedule,
                "enabled": True,   # 新增后默认启动
                "created_at": _fmt(datetime.now())}
        now = _fmt(datetime.now())
        with self._lock:
            self.tasks[task["id"]] = task
            # 新任务从当前时刻起算，只有未来的调度点触发（不补跑历史）
            self.last_fired[task["id"]] = now
        self.save()
        return task, None

    def update(self, tid, script_path, schedule):
        with self._lock:
            task = self.tasks.get(tid)
        if not task:
            return None, "任务不存在"
        err = validate_schedule(schedule)
        if err:
            return None, err
        p = validate_script_path(script_path)
        if not p:
            return None, "脚本不存在或不在允许的脚本目录内"
        now = _fmt(datetime.now())
        with self._lock:
            task["script_path"] = p
            task["schedule"] = schedule
            # 编辑后重置触发基准：只触发未来的调度点
            self.last_fired[tid] = now
        self.save()
        return task, None

    def delete(self, tid):
        with self._lock:
            if tid not in self.tasks:
                return False
            del self.tasks[tid]
            self.last_fired.pop(tid, None)
            self._results.pop(tid, None)
        self.save()
        return True

    def pause(self, tid):
        with self._lock:
            task = self.tasks.get(tid)
            if not task:
                return None, "任务不存在"
            task["enabled"] = False
        self.save()
        return task, None

    def resume(self, tid):
        with self._lock:
            task = self.tasks.get(tid)
            if not task:
                return None, "任务不存在"
            task["enabled"] = True
            # 恢复时以当前时刻为基准，避免立即补跑暂停期间错过的调度点
            self.last_fired[tid] = _fmt(datetime.now())
        self.save()
        return task, None

    # ---------- 调度循环 ----------
    async def run_loop(self):
        while True:
            try:
                await self.tick()
            except Exception as e:  # 单次异常不终止循环
                log.exception("定时调度 tick 异常: %s", e)
            await asyncio.sleep(1)

    async def tick(self, now=None):
        now = now or datetime.now()
        with self._lock:
            due = [(tid, dict(t)) for tid, t in self.tasks.items()
                   if t.get("enabled") and tid not in self._running]
        for tid, task in due:
            point = latest_due(task, now)
            if point is None:
                continue
            with self._lock:
                last = _parse(self.last_fired.get(tid))
            if last is not None and point <= last:
                continue
            with self._lock:
                if tid in self._running:
                    continue
                self._running.add(tid)
                self.last_fired[tid] = _fmt(point)
            # 后台执行不阻塞调度循环（长脚本运行期间其他任务照常触发）
            asyncio.create_task(self._fire_wrapped(tid, task, point))

    async def _fire_wrapped(self, tid, task, point):
        try:
            await self._fire(tid, task, point)
        except Exception as e:
            log.exception("定时任务 %s 执行异常: %s", tid, e)
            self._set_result(tid, time.time(), "error")
        finally:
            with self._lock:
                self._running.discard(tid)
                # 执行期间已过去的调度点不再补触发（上一次未结束则跳过）
                self.last_fired[tid] = _fmt(datetime.now())
            self.save()

    # ---------- 执行 ----------
    def log_path(self, script_path, when):
        """log/scripts/{子目录名}/{年份}/{脚本文件名}_{执行时间}.log"""
        subdir = os.path.basename(os.path.dirname(os.path.abspath(script_path)))
        stem = os.path.splitext(os.path.basename(script_path))[0]
        name = "%s_%s.log" % (stem, when.strftime("%Y%m%d_%H%M%S"))
        return os.path.join(self.log_root, subdir, str(when.year), name)

    def _timeout(self):
        try:
            return int(self.config.get("exec_timeout_sec", 300))
        except (TypeError, ValueError):
            return 300

    async def _fire(self, tid, task, point):
        script = task["script_path"]
        log_path = self.log_path(script, datetime.now())
        started = time.time()
        # prompt.md 执行日志要求：触发定时执行时在主日志中打印
        # （任务名/脚本路径/独立日志文件路径）
        log.info("定时任务触发: %s 脚本=%s 日志=%s",
                 task.get("name") or tid, script, log_path)
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
        except OSError as e:
            log.error("创建日志目录失败 %s: %s", log_path, e)
            self._set_result(tid, started, "error")
            return

        def w(line):
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

        w("[任务 %s] 脚本: %s" % (tid, script))
        w("[触发时刻: %s] [开始执行: %s]" % (_fmt(point), _fmt(datetime.now())))
        p = validate_script_path(script)
        if not p:
            w("[错误] 脚本不存在或不在允许的脚本目录内，跳过执行")
            log.warning("定时任务 %s 脚本失效: %s", tid, script)
            self._set_result(tid, started, "error")
            return
        # UTF-8 输出 + 屏蔽告警（InsecureRequestWarning 等，见 executor._run）
        env = dict(os.environ, PYTHONIOENCODING="utf-8",
                   PYTHONWARNINGS="ignore")
        try:
            proc = await asyncio.create_subprocess_exec(
                venv_python(), "-u", p,
                cwd=os.path.dirname(p), env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # 同 executor：pythonw 父进程执行 console 程序时
                # 防止弹出控制台黑框（输出仍走管道）
                creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception as e:
            w("[错误] 启动失败: %s" % e)
            self._set_result(tid, started, "error")
            return

        async def drain(stream, prefix):
            while True:
                line = await stream.readline()
                if not line:
                    break
                w(prefix + line.decode("utf-8", "replace").rstrip("\n"))

        readers = asyncio.gather(drain(proc.stdout, ""),
                                 drain(proc.stderr, "[stderr] "))
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=self._timeout())
        except asyncio.TimeoutError:
            timed_out = True
            # 无人值守场景：超时即终止进程，避免僵尸进程堆积
            proc.kill()
            await proc.wait()
            w("[超时] 执行超过 %d 秒，已终止进程" % self._timeout())
        await readers
        elapsed = int((time.time() - started) * 1000)
        w("[结束: %s] [exit=%s] [耗时 %dms]" % (
            _fmt(datetime.now()), proc.returncode, elapsed))
        if timed_out:
            result = "timeout"
        elif proc.returncode == 0:
            result = "success"
        else:
            result = "failed"
        log.info("定时任务 %s 执行完成: %s result=%s exit=%s 耗时%dms",
                 tid, script, result, proc.returncode, elapsed)
        self._set_result(tid, started, result)

    def _set_result(self, tid, started_ts, result):
        with self._lock:
            self._results[tid] = {
                "last_run": _fmt(datetime.now()),
                "elapsed_ms": int((time.time() - started_ts) * 1000),
                "last_result": result,
            }

    # ---------- 日志查询 ----------
    def list_logs(self, subdir, date=None):
        """列出某脚本子目录的全部执行日志（按日期可选过滤，新在前）。

        date: "YYYY-MM-DD"，按文件名中的执行时间戳过滤。
        """
        import re
        out = []
        root = os.path.join(self.log_root, subdir)
        if not os.path.isdir(root):
            return out
        for year in os.listdir(root):
            ydir = os.path.join(root, year)
            if not os.path.isdir(ydir):
                continue
            try:
                names = os.listdir(ydir)
            except OSError:
                continue
            for name in names:
                if not name.endswith(".log"):
                    continue
                # {脚本名}_{YYYYMMDD_HHMMSS}.log：取最后一段时间戳
                m = re.search(r"_(\d{8}_\d{6})\.log$", name)
                if not m:
                    continue
                if date and not m.group(1).startswith(
                        date.replace("-", "")):
                    continue
                fp = os.path.join(ydir, name)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                out.append({"name": name, "path": fp,
                            "time": m.group(1), "year": year,
                            "size": st.st_size,
                            "mtime": time.strftime(
                                "%Y-%m-%d %H:%M:%S",
                                time.localtime(st.st_mtime))})
        out.sort(key=lambda x: x["time"], reverse=True)
        return out
