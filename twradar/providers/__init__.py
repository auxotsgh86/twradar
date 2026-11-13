"""資料源。新增來源：在此資料夾新增一個檔案，繼承 Provider 並加上 @register，再於下方 import。"""
from . import finmind, tdcc, tpex, twse  # noqa: F401  註冊內建資料源
from .base import DATASET_TABLE, REGISTRY, Batch, Provider, Transport, build, register  # noqa: F401
