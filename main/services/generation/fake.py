"""fake.py：deterministic fake 生成器（V4 任务执行用；V5A 换真实 adapter，红线：fake 不代替生产）。"""

import hashlib
import uuid

_DIFFICULTY_LABEL = {"BASIC": "基础记忆", "UNDERSTANDING": "理解分析", "APPLICATION": "综合应用"}


def _stable_uuid(seed: str) -> str:
    return str(uuid.UUID(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]))


def generate_card(
    topic: str, chapter_name: str, difficulty: str, custom_requirements: str | None
) -> dict[str, object]:
    seed = f"{topic}|{chapter_name}|{difficulty}|{custom_requirements or ''}"
    card_id = _stable_uuid(f"card|{seed}")
    gen_item = _stable_uuid(f"gen|{seed}")
    label = _DIFFICULTY_LABEL.get(difficulty, difficulty)
    is_tf = difficulty == "APPLICATION"
    front = f"【{label}】{topic}（来自《{chapter_name}》）"
    back = f"参考答案：{topic} 的核心要点（{label} 口径）"
    return {
        "card_id": card_id,
        "source": "GENERATED",
        "front": front,
        "back": back,
        "card_type": "TRUE_FALSE" if is_tf else "QUESTION",
        "statement": front if is_tf else None,
        "answer_boolean": True if is_tf else None,
        "explanation": back if is_tf else None,
        "generation_item_id": gen_item,
        "target_difficulty": difficulty,
        "version": "v1",
    }
