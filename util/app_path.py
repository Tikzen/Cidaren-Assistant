import os
import sys


def app_root():
    """程序根目录: 源码运行取仓库根目录, 打包后取 exe 所在目录(而非临时解包目录)"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def config_dir():
    path = os.path.join(app_root(), 'config')
    os.makedirs(path, exist_ok=True)
    return path