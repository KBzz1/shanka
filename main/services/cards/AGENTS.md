# AGENTS.md

卡片用例：position 分配 / 创建 / 列表 / 导入原子 / 单卡重写。

- 卡片创建同事务插入初始 review_states（state=NEW、difficulty=1.0，满足 ORM CHECK 1~10）；position = 牌组内 max+1（UNIQUE(deck_id, position) 并发兜底）。
- 单卡重写（FR-13）按 C-05：原地替换（同 card_id）、复习状态重置、新 `generation_item_id`（见 docs/Architecture/AGENTS.md）。
