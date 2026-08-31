"""learning_projects 多资料化：materials 表 + chapters/text_chunks 改挂资料（V25-D-29~32）

不可逆迁移（V2.5 多资料增量，契约 3.2a/3.16/6.2；database-design 同步）：

- 新建 ``materials``：资料集合成员。PDF 资料行与 ``pdf_files`` 一对一
  （material_id == file_id，回填自 learning_projects×pdf_files 既有 1:1 归属），
  status 置 NULL（解析状态以 pdf_files 为权威，防第二套状态漂移）；
- ``chapters``：加 material_id（回填 = file_id），start/end_page 改可空（TEXT 章节无页码）；
- ``text_chunks``：加 material_id（回填 = file_id）与 chunk_seq（回填 = page_number），
  唯一键 (material_id, chunk_seq) 取代 (file_id, page_number)；
- ``learning_projects``：删除 file_id 唯一外键列（资料归属改经 materials.project_id），
  允许空项目（V25-D-29：删最后一份资料后项目存活）。

downgrade 不可实现：学习项目可能已无任何 PDF（空项目/纯文本），无法重建 1:1
file_id 归属；按部署纪律以备份恢复替代降级。
"""

import sqlalchemy as sa
from alembic import op

revision: str = "b7e4c2a91d50"
down_revision: str | None = "a3f8d21c9e47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "materials",
        sa.Column("material_id", sa.String(), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(),
            sa.ForeignKey("learning_projects.project_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_index("ix_materials_project_created", "materials", ["project_id", "created_at"])

    # 回填 PDF 资料行：既有 1:1 归属（learning_projects.file_id → pdf_files）
    op.execute(
        """
        INSERT INTO materials
            (material_id, project_id, type, name, status, error_code, size_bytes, char_count, created_at)
        SELECT p.file_id, lp.project_id, 'PDF', p.filename, NULL, p.error_code, p.size_bytes, NULL, p.created_at
        FROM learning_projects lp
        JOIN pdf_files p ON p.file_id = lp.file_id
        """
    )

    # chapters：加 material_id（回填）、file_id 可空（TEXT 章节无 PDF 行）、页码可空
    with op.batch_alter_table("chapters") as batch:
        batch.add_column(sa.Column("material_id", sa.String(), nullable=True))
        batch.alter_column("file_id", existing_type=sa.String(), nullable=True)
        batch.alter_column("start_page", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("end_page", existing_type=sa.Integer(), nullable=True)
    op.execute("UPDATE chapters SET material_id = file_id")
    with op.batch_alter_table("chapters") as batch:
        batch.alter_column("material_id", existing_type=sa.String(), nullable=False)
        batch.create_foreign_key(
            "fk_chapters_material",
            "materials",
            ["material_id"],
            ["material_id"],
            ondelete="CASCADE",
        )
        batch.create_index("ix_chapters_material_id", ["material_id"])

    # text_chunks：加 material_id/chunk_seq（回填）、file_id 可空（TEXT 块无 PDF 行）、
    # 唯一键换 (material_id, chunk_seq)
    with op.batch_alter_table("text_chunks") as batch:
        batch.add_column(sa.Column("material_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("chunk_seq", sa.Integer(), nullable=True))
        batch.alter_column("file_id", existing_type=sa.String(), nullable=True)
    op.execute("UPDATE text_chunks SET material_id = file_id, chunk_seq = page_number")
    with op.batch_alter_table("text_chunks") as batch:
        batch.alter_column("material_id", existing_type=sa.String(), nullable=False)
        batch.alter_column("chunk_seq", existing_type=sa.Integer(), nullable=False)
        batch.drop_constraint("uq_text_chunks_file_page", type_="unique")
        batch.drop_index("ix_text_chunks_file_page")
        batch.create_foreign_key(
            "fk_text_chunks_material",
            "materials",
            ["material_id"],
            ["material_id"],
            ondelete="CASCADE",
        )
        batch.create_unique_constraint("uq_text_chunks_material_seq", ["material_id", "chunk_seq"])
        batch.create_index("ix_text_chunks_material_seq", ["material_id", "chunk_seq"])

    # learning_projects：重建去掉 file_id（不可逆：空项目/纯文本项目无法回填 1:1 归属）
    with op.batch_alter_table("learning_projects") as batch:
        batch.drop_column("file_id")


def downgrade() -> None:
    raise NotImplementedError(
        "不可逆迁移：空项目/纯文本项目无法重建 learning_projects.file_id 1:1 归属；"
        "如需回退请从部署备份恢复（R25-09 纪律）。"
    )
