"""创建切片 0 的首个管理员账户。

运行：uv run python scripts/bootstrap_slice0.py
凭据来自模板 .env 的 FIRST_SUPERUSER / FIRST_SUPERUSER_PASSWORD，脚本不打印密码。
"""

from app.core.config import settings
from app.slice0_app import app as slice0_app


result = slice0_app.state.slice0.auth.ensure_bootstrap_admin(
    str(settings.FIRST_SUPERUSER), settings.FIRST_SUPERUSER_PASSWORD
)
print({"user_id": result["user_id"], "status": result["status"], "created": result["created"]})
