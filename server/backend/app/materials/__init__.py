"""切片 2：资料上传、逐页解析、原图裁切与内置检索。

T02 只落地与 OCR 外部契约无关的地基：数据模型、上传校验、配额账本、
PDF 渲染封装与文本可信判定。OCR 写路径（T03/T04/T05）以 T01 真实样本
spike 为门禁，未通过前不启动。
"""

__version__ = "0.1.0"

from . import models as _models  # noqa: F401 - register tables on Base.metadata
from .errors import MaterialsError, error  # noqa: F401
