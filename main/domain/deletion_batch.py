"""CardDeletionBatch（structure-contract 3.18；V2.5 新增）。

撤销窗口常量：服务端接受最后一次追加后 10 秒（database-design 2.20）。
"""

UNDO_WINDOW_SECONDS = 10
