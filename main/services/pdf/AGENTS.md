# AGENTS.md

PDF 制卡用例：上传 → 分片 → 规划 → 生成。文件读写走 `../infra/storage/`。

- 样书夹具（`res/AI-Agents-in-Depth-zh-CN.pdf`）不得原地替换——测试硬断言（12 章节/318 页/首章起始页 = 9，1-based）按当前样书校准，变更须同步校准常量。
- 技术债（final review I-1 登记）：pypdf 解析（同步 `PdfReader` 读取）与 `storage.save`（同步文件写）阻塞事件循环；已按 Content-Length 头预检防大 body 放大，线程池化不在本修复范围。
