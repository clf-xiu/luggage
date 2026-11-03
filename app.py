"""仓库根目录兼容入口：让 `gunicorn app:app` 也能启动。

实际实现位于 contral/app.py，两种写法等价：
- gunicorn contral.app:app --bind 0.0.0.0:$PORT
- gunicorn app:app --bind 0.0.0.0:$PORT
"""
from contral.app import app
