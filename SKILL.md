---
name: ebook-chapter-extractor
description: "全格式电子书 (EPUB / MOBI / AZW3 / PDF) 按章节按需伴读与提取技能。为 AI Agent 量身定制，毫秒级快速定位并按需切片提取目标章节（0 Token、0 网络开销、拒绝整本读取）。支持流式电子书原生 Markdown 解析、代码块智能保护与混合 OCR 回填（百度飞桨 PaddleOCR-VL 批量模式 + RapidOCR 本地兜底，带原图路径与视觉自愈提示）；支持 PDF 书签自愈、印刷目录偏移量换算、扫描版自动智能降级与协同输出 (高清原图 + 离线 OCR 文本)、多模态视觉直读 (render_page.py) 与 MinerU VLM 高精云端解析 (check_token.py)；内置分级依赖自愈检测 (env_checker.py) 与只读路径缓存降级。"
when_to_use: 当用户要求查看、伴读、辅导、读取、提取、解析、切片或总结电子书或 PDF (EPUB/MOBI/AZW3/PDF) 的某个章节/小节/页码范围，或告知自己正在学习/阅读书籍的某个部分时使用。
---

# ebook-chapter-extractor (伴读与按需章节提取 SOP)

## 0. 脚本与参考文件清单 (Architecture Map)

| 文件路径 | 适用格式 | 阶段 / 角色 | 核心功能说明 |
|---|---|---|---|
| `scripts/probe.py` | 全格式 | 1. 探针 | 统一探针入口，嗅探格式、章节/总页数并推荐解析路线 |
| `scripts/probe_pdf.py` | PDF | 1. 探针 | PDF 专用深度探针，嗅探文字层、书签状态及扫描件特征 |
| `scripts/build_index.py` | 全格式 | 2. 索引 | 构建章节索引 `chapters.json`，内嵌 PDF 书签 `0/-1` 自愈算法 |
| `scripts/extract_chapter.py` | 全格式 | 3. 提取 | 按章节名/序号/范围提取 MD、导出 PDF 切片、OCR 批量回填、扫描件智能降级协同输出 (原图+OCR) 或 JSON 输出 |
| `scripts/render_page.py` | PDF | 3. 视觉 | 将 PDF 指定页/章节渲染为高清 PNG（支持 -c 模糊匹配与多种范围传参），供多模态 Agent 视觉直读 |
| `scripts/check_token.py` | 全格式 | 3. 鉴权 | 探测 MinerU Token 与 百度飞桨 PaddleOCR Token 状态，给出状态与持久化配置指引 |
| `scripts/env_checker.py` | 全格式 | 底层支撑 | 分级依赖自愈检测与极速自动安装（Tier 1 基础解析 / Tier 2 OCR 扩展） |
| `scripts/epub_parser.py` | EPUB | 解析引擎 | 纯 Python 流式解析，含章节合并、MathML 转 LaTeX、代码块等宽保留与 DOM 清洗 |
| `scripts/mobi_parser.py` | MOBI/AZW3 | 解析引擎 | PalmDOC LZ77 瞬时解压与 KF8 原生解包，提取内嵌图片、正则断章与代码识别 |
| `scripts/ocr_helper.py` | 全格式 | OCR 辅助 | 百度飞桨 PaddleOCR-VL 批量 API 与本地 RapidOCR 协同调度、启发式代码自愈与标点清洗、原图路径关联与缓存降级 |
| `scripts/mathml_helper.py` | 全格式 | 公式辅助 | MathML XML 标签向 LaTeX Markdown (`$...$`) AST 转换 |
| `references/epub-pipeline.md` | 流式规范 | 参考指南 | EPUB / MOBI / AZW3 流式电子书技术流水线与细节规范 |
| `references/min-pipeline.md` | 选型规范 | 参考指南 | 工具链选型全景矩阵与 MinerU / PaddleOCR Token 管理规范 |
| `references/offset-model.md` | PDF 规范 | 参考指南 | PDF 逻辑页/物理页体系、Offset 偏移量模型与扫描漂移防御 |
| `references/vision-pipeline.md` | 视觉规范 | 参考指南 | Agent-Native 多模态视觉直读工作流与 SOP 指南 |

---

## 1. 核心决策树 (Decision Tree)

```text
                           用户请求提取电子书/章节/页码
                                       │
                 ┌─────────────────────┴─────────────────────┐
                 ▼                                           ▼
      【流式电子书：EPUB/MOBI/AZW3】                     【固定版式：PDF】
                 │                                           │
         纯 Python 原生提取                           运行快速探针 probe.py
      (0.05s, 0 Token, 0 网络)                               │
                 │                         ┌─────────────────┴─────────────────┐
        extract_chapter.py                 ▼                                   ▼
                 │                   【数字版 PDF】                      【扫描版 / 图像 PDF】
        ┌────────┴────────┐                │                                   │
        ▼                 ▼          PyMuPDF / MarkItDown              ┌───────┴───────┐
    纯文本章节       含代码图/插图           (1秒提取结构化 MD)                 ▼               ▼
   (--format md)    (--ocr / --dump)                               【多模态视觉直读】  【纯文本 LLM 深度解析】
                          │                                         render_page.py     check_token.py ->
              自动导出原图+OCR批量回填                                     (1.5s 极速直读)    切片 -> MinerU VLM
          (首选 Paddle Batch, Rapid 兜底)
              带真实物理路径供 Agent 看图自愈
```

---

## 2. 流式电子书 SOP (EPUB / MOBI / AZW3)

流式电子书无页码偏移，100% 结构化还原，首选原生流式流水线。

```bash
# 1. 极速提取目标章节为 Markdown（自动包含下辖子节，0 Token）
python scripts/extract_chapter.py "book.epub" --chapter "Chapter 1" --format md

# 2. 伴读推荐：启用 OCR 智能回填（首选 PaddleOCR 批量高精 API，RapidOCR 本地离线秒级兜底）
python scripts/extract_chapter.py "book.mobi" --chapter "7.3.5" --ocr --ocr-engine paddleocr -o chapter.md

# 3. 临时传入 PaddleOCR API Token
python scripts/extract_chapter.py "book.mobi" --chapter "7.3.5" --ocr --paddle-token "your_token" -o chapter.md

# 4. 导出全部插图供多模态 Agent 工具直读
python scripts/extract_chapter.py "book.mobi" --chapter "2.1" --dump-images .cache/images/ -o chapter.md

# 5. Agent 结构化交互（输出包含字数、插图路径的 JSON 数据）
python scripts/extract_chapter.py "book.epub" --chapter "1.1" --json
```

---

## 3. PDF 按需切片 SOP（五步法）

### 阶段 1：快速探针 (Probe)
```bash
python scripts/probe.py "book.pdf"
```

### 阶段 2：索引与书签自愈 (Locate & Heal)
```bash
python scripts/build_index.py "book.pdf" --print
```
* **有书签**：书签页码大多数即为物理页（1-based），自愈修复异常 `0/-1` 页码后直接定位。
* **无书签**：从目录提取逻辑页码，通过正文 P1 物理页计算偏移量：
  $$\text{物理页} = \text{逻辑页} + \text{Offset} \quad (\text{Offset} = \text{正文 P1 物理页} - 1)$$

### 阶段 3：提取前自检 (Sanity Check)
校验物理页前 3 行文字是否匹配章节名。若未匹配，在 $\pm 2$ 页微调以防御扫描件漂移；若目标页（如目录页）为纯图片且 `get_text` 为空，立即回退至渲染/图片解析模式。

### 阶段 4：按需解析执行 (Execute)
1. **数字版 PDF（极速 Markdown）**：
   ```bash
   python scripts/extract_chapter.py "book.pdf" --chapter "第2章" --format md
   ```
2. **扫描版 PDF 智能降级（双通道协同输出，极速推荐）**：
   ```bash
   # 自动切片渲染原图供 Agent 视觉直读 + 输出离线 OCR 文本作为快速参考
   python scripts/extract_chapter.py "scanned_book.pdf" --chapter "2.3.2"
   ```
3. **扫描版 + 多模态 Agent 视觉直读**：
   ```bash
   # 支持按章节名模糊匹配或页码范围
   python scripts/render_page.py "book.pdf" -c "2.3.2" --dpi 150
   # 或
   python scripts/render_page.py "book.pdf" --pages "38-40" --dpi 150
   ```
4. **扫描版 + 纯文本 LLM（MinerU VLM 深度解析）**：
   * **Step 0 检查 Token**：`python scripts/check_token.py`
   * **Step 1 提取切片**：`python scripts/extract_chapter.py "book.pdf" --range "264-266" --format pdf -o slice.pdf`
   * **Step 2 高精解析**：`mineru-open-api extract slice.pdf --model vlm -f md -o ./output/`

### 阶段 5：沉淀与交付 (Deliver)
缓存索引至 `.cache/chapters.json`，提取结果缓存至 `.cache/chapter_xx.md`，避免同一会话重复解析。遇到只读路径时自动安全降级至系统临时目录。

---

## 4. API Token 管理与用户引导规范

> ⚠️ **严禁硬编码**：脚本与文档严禁固化真实 Token。

### 4.1 MinerU Token 配置
当纯文本 LLM 解析扫描件复杂表格/公式且未配置 Token 时：
1. 引导用户前往 [https://mineru.net/apiManage/token](https://mineru.net/apiManage/token) 获取免费 Token。
2. 配置方式：
   ```powershell
   # 方式 A（推荐，环境变量）：
   $env:MINERU_TOKEN="your_token_here"

   # 方式 B（持久化配置）：
   "your_token_here" | mineru-open-api auth
   mineru-open-api auth --verify
   ```

### 4.2 百度飞桨 PaddleOCR Token 配置
1. 引导用户前往百度飞桨 AI Studio 开放平台获取免费 API Access Token。
2. 配置方式：
   ```powershell
   # 方式 A（环境变量，推荐）：
   $env:PADDLEOCR_TOKEN="your_token_here"

   # 方式 B（持久化配置文件 ~/.paddleocr/config.yaml）：
   token: "your_token_here"
   ```

---

## 5. 黄金守则 (Guardrails)

1. **流式 0 Token 绝对优先**：遇到 EPUB / MOBI / AZW3 坚决走纯 Python 原生提取正文，严禁整本走 OCR 或大模型视觉。
2. **先切片后送 MinerU**：扫描件超过 20 页严禁一次性提交 MinerU，必须切出 2~5 页子切片。
3. **数字版常规页杜绝 MinerU/OCR**：数字版直接走 MarkItDown/PyMuPDF，秒级交付。
4. **Token 严格安全隔离**：Token 仅走环境变量与外部配置文件，严禁写入代码库。
5. **图文双轨原图校对**：OCR 遇到排版歧义时，多模态 Agent 依据 Markdown 附带的真实物理路径主动看图自愈。
