# 📖 ebook-chapter-extractor (全格式电子书按章节按需解析)

> **针对大本工具书/教材/专著的按需章节提取技能** —— 通过目录定位、物理页/章节计算与切片提取，只解析目标章节，避免整本转换的巨大资源与时间开销。支持 **EPUB / MOBI / AZW3 / PDF** 等全格式。

一个面向 **AI Agent / LLM** 的全格式电子书按章节按需解析标准作业程序（SOP）技能。适用于数百至上千页的电子书籍，自动判别文档格式与最优解析路线。

---

## ✨ 核心特性

- **EPUB / MOBI / AZW3 原生极速解析**：纯 Python 毫秒级提取（~0.05s），零 Token 开销，零网络依赖，100% 结构化精准还原原生表格与代码块。
- **PDF 按需切片，拒绝整本转换**：通过书签/目录定位目标章节，只切出 2~5 页小切片，资源消耗降低数十倍。
- **书签自愈算法**：自动修复 PDF 大章页码为 `0 / -1` 的脏书签数据，稳定构建章节索引。
- **Offset 偏移量模型**：PDF 无书签时用印刷目录逻辑页 + Offset 换算物理页，含扫描件漂移防御。
- **全格式自动分流**：
  - **流式格式 (EPUB/MOBI/AZW3)**：原生 ZIP + XHTML 解构，0.05s 输出精美 Markdown；
  - **数字版 PDF**：MarkItDown / PyMuPDF 极速提取；
  - **扫描版 PDF**：优先 Agent 原生视觉直读（~1.5s），深度研读走 MinerU VLM 精确解析。
- **Token 安全隔离**：MinerU Token 仅走环境变量或外部配置，绝不硬编码进代码。

---

## 📊 决策树分流体系

```text
                     用户请求电子书目标章节（如："读取《xxx》第3章"）
                                         │
                                         ▼
                               阶段 1：快速探针 (Probe)
                       运行 scripts/probe.py 或 probe_pdf.py
                                         │
             ┌───────────────────────────┴───────────────────────────┐
             ▼                                                       ▼
 【流式电子书：EPUB / MOBI / AZW3】                           【固定版式：PDF】
             │                                                       │
             ▼                                                       ▼
 阶段 2：目录索引与锚点提取                                  阶段 2：书签自愈与物理页定位
   - 解析 toc.ncx / nav.xhtml / spine                          - get_toc 自愈修复异常 0/-1 页码
   - 生成 chapters.json                                        - 无书签时提取印刷目录 + 计算 Offset
             │                                                       │
             ▼                                                       ▼
 阶段 3：零 Token 原生提取                                  阶段 3：按需分流解析
   - 直接抽取对应 XHTML 片段                                   ├─ 数字版: PyMuPDF / MarkItDown
   - 纯 Python 0.05s 转结构化 Markdown                         ├─ 扫描版 A: render_page 视觉直读(1.5s)
   - 100% 结构保真(表格/代码/公式)                             └─ 扫描版 B: 切片 2~5 页 -> MinerU VLM
```

---

## 🛠️ 工具与脚本

| 脚本 | 适用格式 | 阶段 | 作用 |
|---|---|---|---|
| `probe.py` / `probe_pdf.py` | 全格式 | 1. 探针 | 自动嗅探格式，秒级输出元数据、章节/页数与推荐路线 |
| `build_index.py` | 全格式 | 2. 索引 | 解析章节目录，构建并保存 `chapters.json`（含 PDF 书签 0/-1 自愈） |
| `extract_chapter.py` | 全格式 | 3. 提取 | 按章节名/序号/页码提取为 Markdown、纯文本、PDF 切片、OCR 回填或 JSON |
| `render_page.py` | PDF | 3. 视觉 | 将 PDF 页面渲染为高清 PNG，供 Agent 视觉模型直读 |
| `check_token.py` | 扫描版 PDF | 3. 检测 | 检测 MinerU Token 状态，决定走精确 VLM 还是 flash-extract |
| `env_checker.py` | 全格式 | 支撑 | 分级依赖自愈检测与极速自动安装（Tier 1 核心包 / Tier 2 OCR） |

### 依赖

- Python 3.8+
- [PyMuPDF](https://pymupdf.readthedocs.io/)（`pip install pymupdf`，PDF 核心依赖）
- 可选：`pip install "markitdown[pdf]"`（数字版 PDF Markdown 转换）
- 可选：`npm install -g mineru-open-api`（扫描版 PDF VLM 精确解析）

---

## 🚀 快速开始

### 阶段 1：探针（Probe）

```bash
python scripts/probe.py "book.epub"
# 或
python scripts/probe.py "book.pdf"
```

### 阶段 2：索引与定位（Locate & Heal）

```bash
python scripts/build_index.py "book.epub" --print
# 或
python scripts/build_index.py "book.pdf" --print
```

### 阶段 3：按需解析（Execute）

```bash
# EPUB / MOBI / AZW3 -> 毫秒级结构化 Markdown (0 Token)
python scripts/extract_chapter.py "book.epub" --chapter "Chapter 1" --format md

# 数字版 PDF -> 极速 Markdown
python scripts/extract_chapter.py "book.pdf" --chapter "第2章" --format md

# 扫描版 PDF + Agent 原生视觉（最快，~1.5s）
python scripts/render_page.py "book.pdf" --pages "264" --dpi 150

# 扫描版 PDF + 纯文本 LLM（先切小切片，再送 MinerU，Token 优先）
python scripts/check_token.py                      # 检测 Token
python scripts/extract_chapter.py "book.pdf" --range "264-265" --format pdf --output slice.pdf
mineru-open-api extract slice.pdf --model vlm -f md   # ⭐ Token 精确模式（首选）
```

---

## 📚 参考资料（references/）

| 文档 | 内容 |
|---|---|
| `epub-pipeline.md` | EPUB / MOBI / AZW3 流式电子书解析流水线指南 |
| `min-pipeline.md` | 工具链选型全景矩阵与 MinerU 规范 |
| `offset-model.md` | PDF 页码体系与 Offset 偏移量模型（含扫描漂移防御） |
| `vision-pipeline.md` | Agent-Native 多模态视觉直接解析流水线 |

---

## ⚠️ 黄金守则（Guardrails）

1. **流式格式零 Token 优先**：遇到 EPUB / MOBI / AZW3 直接纯 Python 提取，严禁走 OCR 或大模型视觉。
2. **先切后送，严禁全本提交**：扫描件超过 20 页严禁一次性提交 MinerU，必须切出 2~5 页子段。
3. **数字版常规页杜绝 MinerU/OCR**：数字版正文直接走 MarkItDown/PyMuPDF，1 秒搞定。
4. **多模态视觉优先**：环境支持图像输入时，优先 `render_page.py` 视觉直读。
5. **Token 严格隔离**：Token 仅走环境变量/外部配置，绝不写入版本库。
