"""字段名映射:中文 Excel 字段名 <-> 英文 ORM 列名。

数据库 ORM 列名使用英文(避免 Python 保留字和特殊字符),
但 API 和 Excel 对外统一使用清单第 4 节定义的 13 个中文/拉丁文字段名。
"""

# 中文/拉丁文字段名 -> ORM 列名
FIELD_TO_COLUMN: dict[str, str] = {
    "中名": "zhongming",
    "Phylum": "phylum",
    "纲": "gang",
    "Class": "klass",
    "Order": "order_field",
    "中文科名": "zhongwen_ke",
    "科名": "ke",
    "属名": "shu",
    "种名": "zhong",
    "产地3": "chandi3",
    "图像": "tuxiang",
    "采集人": "caijiren",
    "采集日期": "caiji_riqi",
}

# ORM 列名 -> 中文/拉丁文字段名(反向映射)
COLUMN_TO_FIELD: dict[str, str] = {v: k for k, v in FIELD_TO_COLUMN.items()}

# 5 个图片提取字段
IMAGE_EXTRACTED_FIELDS: list[str] = [
    "中名",
    "产地3",
    "图像",
    "采集人",
    "采集日期",
]

# 8 个分类补全字段
TAXONOMY_FIELDS: list[str] = [
    "Phylum",
    "纲",
    "Class",
    "Order",
    "中文科名",
    "科名",
    "属名",
    "种名",
]

# 全部 13 个字段(顺序与清单第 4 节一致)
ALL_TARGET_FIELDS: list[str] = list(FIELD_TO_COLUMN.keys())
