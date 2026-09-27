"""切片 1：能力配置、外发确认与本机连接器。"""

__version__ = "0.1.0"

from . import models as _models  # noqa: F401 - register tables on Base.metadata
from .errors import Slice1Error, error  # noqa: F401
