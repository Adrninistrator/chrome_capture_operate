"""验证脚本：访问 chrome_capture_operate 的 /health 接口，验证服务运行状态。

用于验证"快速执行脚本"页面与"定时执行脚本"功能是否正常：
- 服务未启动 / 端口不对 → 打印明确错误并以非 0 退出码退出
- 服务正常 → 打印服务返回的运行状态与当前时间，退出码 0
"""
import json
import os
import sys

import requests

# 服务默认端口；若能定位到本项目的 conf/conf.json 则以其中端口为准
DEFAULT_PORT = 33445
# 本脚本位于 chrome_capture_operate_python/python_scripts_example/health_check/，
# 向上三级即 chrome_capture_operate_python（conf/conf.json 所在项目根）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def service_port():
    try:
        with open(os.path.join(BASE_DIR, "conf", "conf.json"),
                  encoding="utf-8") as f:
            return int(json.load(f).get("port") or DEFAULT_PORT)
    except (OSError, ValueError):
        return DEFAULT_PORT


def main():
    url = "http://127.0.0.1:%d/health" % service_port()
    print("访问健康检查接口: %s" % url)
    try:
        r = requests.get(url, timeout=10, verify=False)
    except requests.RequestException as e:
        print("[失败] 服务不可达: %s" % e)
        print("请确认 chrome_capture_operate 服务已启动（start.bat），"
              "且参数配置页监听端口与实际一致")
        sys.exit(1)
    print("HTTP %d" % r.status_code)
    print(r.text)
    if r.status_code == 200:
        print("[成功] 服务运行正常")
    else:
        print("[失败] 健康检查返回非 200")
        sys.exit(1)


if __name__ == "__main__":
    main()
