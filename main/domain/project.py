"""LearningProject / ProjectStudySettings（structure-contract 3.16/3.17；V2.5 新增）。

一个项目恰好对应一份当前 PDF（learning_projects.file_id 唯一外键权威）；
项目状态由 PDF 状态与 chapters_confirmed_at 确定，不建立可漂移的第二套状态列。
"""

# 项目状态由 PDF 状态与本列确定（database-design 2.17）——chapters_confirmed_at
# 为 READY 判定所需列；项目状态枚举见 domain/enums.ProjectStatus。
