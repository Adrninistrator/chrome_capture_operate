"""固化脚本执行器。

- 使用当前 Python 项目的虚拟环境执行，python -u（输出不被缓存）。
- 不传递参数；捕获 stdout/stderr。
- 超时（conf: exec_timeout_sec）后标记超时并保留"结束进程"按钮（人工决策），
  后台继续等待进程自然结束。

脚本可位于两个根目录（快速执行页"展示子目录"）：
- python_scripts_example（示例目录，随项目分发）
- 全局配置 global_conf.json key=python_scripts_dir_path（用户自定义目录）
"""
import asyncio
import logging
import os
import subprocess
import time
import uuid

from . import globalconf
from .config import BASE_DIR, SCRIPTS_EXAMPLE_DIR

log = logging.getLogger("app.exec")


def venv_python():
    p = os.path.join(BASE_DIR, ".venv", "Scripts", "python.exe")
    return p if os.path.exists(p) else "python"


def script_roots():
    """可执行脚本的根目录列表：示例目录在前，用户自定义目录在后。"""
    roots = [os.path.abspath(SCRIPTS_EXAMPLE_DIR)]
    user = globalconf.get_value(globalconf.KEY_SCRIPTS_DIR)
    if user:
        p = os.path.abspath(user)
        if p not in roots:
            roots.append(p)
    return roots


def validate_script_path(script_path):
    """脚本必须位于任一脚本根目录内且为 .py 文件。"""
    if not script_path:
        return None
    p = os.path.abspath(script_path)
    if not p.endswith(".py") or not os.path.isfile(p):
        return None
    for root in script_roots():
        if p.startswith(root + os.sep):
            return p
    return None


class Execution:
    def __init__(self, exec_id, script_path, timeout):
        self.id = exec_id
        self.script_path = script_path
        self.timeout = timeout
        self.started = time.time()
        self.finished = None
        self.exit_code = None
        self.timed_out = False
        self.killed = False
        self.stdout_lines = []
        self.stderr_lines = []
        self.proc = None
        self.error = None

    def to_dict(self):
        return {
            "id": self.id,
            "script_path": self.script_path,
            "running": self.finished is None,
            "timed_out": self.timed_out,
            "killed": self.killed,
            "exit_code": self.exit_code,
            "error": self.error,
            "elapsed_ms": int(((self.finished or time.time()) - self.started)
                              * 1000),
            "stdout": "\n".join(self.stdout_lines),
            "stderr": "\n".join(self.stderr_lines),
        }


class Executor:
    def __init__(self, config):
        self.config = config
        self.execs = {}

    def _timeout(self):
        return int(self.config.get("exec_timeout_sec", 300))

    async def start(self, script_path):
        """Web 快速执行：启动后由 get() 轮询状态。"""
        p = validate_script_path(script_path)
        if not p:
            return None
        ex = Execution(uuid.uuid4().hex[:12], p, self._timeout())
        self.execs[ex.id] = ex
        asyncio.create_task(self._run(ex))
        return ex

    async def _run(self, ex):
        log.info("执行脚本: %s", ex.script_path)
        # 子进程强制 UTF-8 输出，避免 Windows GBK 中文乱码；
        # 屏蔽 Python 告警（含固化脚本 verify=False 触发的
        # InsecureRequestWarning 等 HTTPS 相关告警，prompt.md 执行py要求）
        env = dict(os.environ, PYTHONIOENCODING="utf-8",
                   PYTHONWARNINGS="ignore")
        try:
            ex.proc = await asyncio.create_subprocess_exec(
                venv_python(), "-u", ex.script_path,
                cwd=os.path.dirname(ex.script_path),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # 服务以 pythonw（无控制台）运行，执行 console 程序
                # (python.exe) 时 Windows 会弹新控制台黑框——加该标志
                # 不创建控制台窗口，输出仍走管道正常捕获
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception as e:
            ex.error = "启动失败: %s" % e
            ex.finished = time.time()
            return

        async def read(stream, buf):
            while True:
                line = await stream.readline()
                if not line:
                    break
                buf.append(line.decode("utf-8", "replace").rstrip("\n"))

        readers = asyncio.gather(
            read(ex.proc.stdout, ex.stdout_lines),
            read(ex.proc.stderr, ex.stderr_lines),
        )
        try:
            await asyncio.wait_for(ex.proc.wait(), timeout=ex.timeout)
            await readers
        except asyncio.TimeoutError:
            ex.timed_out = True
            log.warning("脚本执行超时(%ds): %s", ex.timeout, ex.script_path)
            # 保留进程，等待人工结束；后台继续等待其自然结束
            async def wait_natural():
                await ex.proc.wait()
                await readers
                ex.exit_code = ex.proc.returncode
                ex.finished = time.time()
            asyncio.create_task(wait_natural())
            return
        ex.exit_code = ex.proc.returncode
        ex.finished = time.time()
        log.info("脚本结束: %s exit=%s", ex.script_path, ex.exit_code)

    async def _kill_proc(self, ex):
        if ex.proc and ex.proc.returncode is None:
            try:
                ex.proc.kill()
                ex.killed = True
                log.info("已终止脚本进程: %s", ex.script_path)
            except ProcessLookupError:
                pass

    async def kill(self, exec_id):
        ex = self.execs.get(exec_id)
        if not ex or ex.finished is not None:
            return False
        await self._kill_proc(ex)
        return True

    def get(self, exec_id):
        return self.execs.get(exec_id)


def list_scripts():
    """列出两个根目录（示例/用户自定义）下的脚本子目录及其 .py/.md 文件
    （含修改时间）；示例目录在前，category 标记子目录所属类别。"""
    result = []
    for root, category in [(SCRIPTS_EXAMPLE_DIR, "example"),
                           (globalconf.get_value(globalconf.KEY_SCRIPTS_DIR),
                            "user")]:
        if not root:
            continue
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            continue
        for d in sorted(os.listdir(root)):
            dpath = os.path.join(root, d)
            if not os.path.isdir(dpath):
                continue
            pys, docs, readme = [], [], None
            for f in sorted(os.listdir(dpath)):
                fp = os.path.join(dpath, f)
                if f.endswith(".py"):
                    pys.append({"path": fp, "mtime": _file_mtime(fp)})
                elif f.lower().endswith(".md"):
                    docs.append({"path": fp, "mtime": _file_mtime(fp)})
                    if f.lower() == "readme.md":
                        readme = fp
            result.append({"dir": dpath, "category": category,
                           "py_files": pys, "doc_files": docs,
                           "readme": readme})
    return result


def _file_mtime(path):
    """文件最近修改时间（本地时间字符串，读取失败返回空）。"""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(os.path.getmtime(path)))
    except OSError:
        return ""
