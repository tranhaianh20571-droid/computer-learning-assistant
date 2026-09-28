"""产品入口（薄壳）：装配见 app/assembler.py（重构方案 O-A）。

启动：`uvicorn app.main:app`（server/backend 目录下）。
"""

from app.assembler import create_app

app = create_app()
