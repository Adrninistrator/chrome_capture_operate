"""全局配置文件：C:\\Users\\%username%\\.chrome_capture_operate\\global_conf.json

对于需要在当前操作系统全局使用的配置参数（区别于 conf/conf.json 项目级配置），
写入全局配置文件，如固化脚本保存根目录 python_scripts_dir_path。

- 存储 JSON 格式
- 查询：文件或 key 不存在返回 ""，存在则返回对应值
- 修改：文件不存在则创建，写入指定 key=value

标量参数（如 python_scripts_dir_path）用 get_value/set_value；
结构化参数（如定时执行任务 schedules）用 get_json/set_json，同一把锁保护，
标量与结构化读写互不覆盖。
"""
import json
import os
import threading

GLOBAL_CONF_DIR = os.path.join(os.path.expanduser("~"),
                               ".chrome_capture_operate")
GLOBAL_CONF_PATH = os.path.join(GLOBAL_CONF_DIR, "global_conf.json")

# 全局配置已知的 key（校验/说明用；其他 key 按通用读写处理）
KEY_SCRIPTS_DIR = "python_scripts_dir_path"
KEY_SCHEDULES = "schedules"   # 定时执行任务（结构化，Scheduler 读写）
KEY_MULTI_PROFILE = "chrome_multi_profile"  # Chrome 多开实例（结构化，webapp 读写）
KEY_FAVORITE_PAGES = "favorite_pages"  # 收藏的页面记录（结构化，webapp 读写）

_lock = threading.Lock()


def _read_all(path):
    """读取全部配置；文件不存在或内容损坏时返回 {}。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_all(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_value(key):
    """查询某个 key 的值：文件不存在或 key 不存在返回 ""。"""
    v = _read_all(GLOBAL_CONF_PATH).get(key)
    return "" if v is None else v


def set_value(key, value):
    """修改指定 key 的 value（文件不存在则创建）。成功返回 True。"""
    with _lock:
        data = _read_all(GLOBAL_CONF_PATH)
        data[key] = value
        try:
            _write_all(GLOBAL_CONF_PATH, data)
            return True
        except OSError:
            return False


def get_json(key, default=None):
    """查询结构化配置（dict/list）：文件不存在或 key 不存在返回 default。"""
    v = _read_all(GLOBAL_CONF_PATH).get(key)
    return default if v is None else v


def set_json(key, value):
    """修改结构化配置（dict/list）。成功返回 True。"""
    with _lock:
        data = _read_all(GLOBAL_CONF_PATH)
        data[key] = value
        try:
            _write_all(GLOBAL_CONF_PATH, data)
            return True
        except OSError:
            return False
