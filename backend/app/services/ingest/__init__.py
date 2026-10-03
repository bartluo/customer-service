"""知识导入流水线：解析 → 入库 → 标记待复核。

把原始法规文本变成数据库里的条文库，走这条路。
采集负责"把文件弄到手"，本包负责"把文件变成数据"。
"""

from app.services.ingest.importer import (
    DocumentInput,
    ImportReport,
    import_documents,
    import_one,
)
from app.services.ingest.collector import (
    Collector,
    InboxCollector,
    collect_from_directory,
    read_text_file,
)

__all__ = [
    "DocumentInput",
    "ImportReport",
    "import_documents",
    "import_one",
    "Collector",
    "InboxCollector",
    "collect_from_directory",
    "read_text_file",
]
