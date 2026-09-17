# AGENTS.md

AI 章节规划用例（V25-D-36）：无目录资料的章节边界识别。资料类型中立——输入是
`text_chunks` 块文本序列（PDF=真页码，TEXT/ZIP=伪页码），不含 PDF 专属概念；
本期仅 PDF 扫描器接线，TEXT/ZIP 推广时只换触发条件与输入来源。

- LLM 调用必须走本包的账本定式（`llm_call_attempts` stage='CHAPTER_PLANNING'、
  scope_type='MATERIAL'、调用前 STARTED 占位、预算 `ai_chapter_retry_limit`），
  禁止绕过账本直调 DeepSeek。
- 章节边界产出必须经 `validator.py` 确定性校验（页码落在段内、标题去重、数量上限），
  不信任模型自由发挥；红线 4：normalized_result 只存规范化边界 JSON。
