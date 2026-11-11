# 📖 ebook-chapter-extractor (全格式电子书 & 无目录学术 PDF 按章节按需解析)

> **针对大本工具书/教材/专著 + 无目录学术论文/报告的按需章节提取技能** —— 通过书签目录定位、印刷目录 Offset 换算或布局分析重建虚拟索引，只解析目标章节，避免整本转换的巨大资源与时间开销。支持 **EPUB / MOBI / AZW3 / PDF** 等全格式。

一个面向 **AI Agent / LLM** 的全格式电子书按章节按需解析标准作业程序（SOP）技能。适用于数百至上千页的电子书籍，自动判别文档格式与最优解析路线。对**没有书签、也没有印刷目录**的 PDF（期刊论文、学位论文、Supplementary Materials、技术报告、白皮书、长篇文章），自动进入 **Paper Mode**：用字体度量重建虚拟章节索引，同样支持按标题名按需切片。

---

## ✨ 核心特性

- **EPUB / MOBI / AZW3 原生极速解析**：纯 Python 毫秒级提取（~0.05s），零 Token 开销，零网络依赖，100% 结构化精准还原原生表格、数学公式（MathML 转 LaTeX）与代码块（等宽字体与缩进智能识别）。
- **无目录 PDF 自动 Paper Mode**：无需书签、无需目录页、无需 OCR 或大模型。基于「正文基准字号相对判定」的布局分析重建虚拟章节索引（自动过滤页眉页脚、参考文献条目与表格碎片），支持 `--chapter "Introduction"` 模糊匹配、`--strip-refs` 剥离文末参考文献、`--column-aware` 双栏阅读顺序重排。
- **混合多后端 OCR 与代码自愈**：
  - **首选/高精 API**：百度飞桨 **PaddleOCR-VL-1.6** API，支持章节内多图内存打包（单次批量 Batch 请求，彻底杜绝 429 报错，提速 400%），高精度解析代码、特殊符号与多栏排版；
  - **离线兜底引擎**：本地轻量 **RapidOCR (ONNXRuntime)**，在无网络、无 Token 或 API 偶发异常时秒级自动兜底；
  - **代码启发式自愈**：内置 `clean_ocr_text` 与 `clean_code_ocr`，自动修复注释符（`//`、`/*`）、赋值符（`:=`）、全角符号与断行，并保留高清原图相对路径锚点。
- **PDF 按需切片，拒绝整本转换**：通过书签/目录/虚拟索引定位目标章节，只切出目标页，资源消耗降低数十倍。
- **扫描版智能降级与协同输出 (Dual-Channel Output)**：遇扫描版/无文字层 PDF 时，自动按需渲染为高清 PNG 供 Agent 多模态视觉直读，并附带离线 OCR 文本作为快速参考通道。
- **书签自愈算法**：自动修复 PDF 大章节码为 `0 / -1` 的脏书签数据，向下回溯定位首个有效子节点页码。
- **三级索引回退**：书签自愈 → 印刷目录逻辑页 + Offset 换算 → 布局分析虚拟索引（含扫描件 $\pm 2$ 页漂移防御）。
- **命令行传参容错**：`render_page.py` 与 `extract_chapter.py` 支持章节名模糊匹配（`-c "2.3.2"`）、多种页码范围格式（`38 40`、`38-40`、`12,15`）。
- **只读路径缓存自动降级**：目标电子书位于只读介质或无写入权限时，自动将渲染与提取缓存重定向至系统临时目录。
- **API Token 安全隔离**：百度飞桨与 MinerU Token 仅走环境变量、外部配置或命令行参数传入，绝不硬编码进代码库。

---

## 📊 决策树分流体系

```text
        用户请求目标章节（如：“读取《xxx》第3章” / “提取这篇论文的 Methods”）
                                    │
                                    ▼
                          阶段 1：快速探针 (Probe)
                  运行 scripts/probe.py 或 probe_pdf.py
                                    │
        ┌───────────────────────────┴───────────────────────────┐
        ▼                                                       ▼
【流式电子书：EPUB / MOBI / AZW3】                     【固定版式：PDF】
        │                                                       │
        ▼                                       ┌─────────────────┴─────────────────┐
阶段 2：目录索引与锚点提取                      ▼                                   ▼
  - 解析 toc.ncx / nav.xhtml / spine      【有书签 / 有印刷目录】          【无书签且无印刷目录】
  - 生成 chapters.json                  - get_toc 自愈 0/-1 页码        → Paper Mode 自动启用
        │                               - 印刷目录 + Offset 换算        - paper_parser.py 布局分析
        ▼                                       │                      - 重建虚拟章节索引
阶段 3：零 Token 原生提取                       └─────────────┬───────────────────┘
  - 直接抽取对应 XHTML 片段                                   ▼
  - 纯 Python 0.05s 转结构化 Markdown              阶段 3：按需分流解析
  - 100% 结构保真(表格/代码/公式)                      ├─ 数字版: PyMuPDF / MarkItDown (秒级)
  - 启发式代码保护与 OCR 插图回填                      │   --chapter / --range / --strip-refs
    (首选 PaddleOCR Batch API, RapidOCR 兜底)        ├─ 扫描版 A (优先): extract_chapter 智能降级
                                                    │   ├─ 自动渲染高清切片供 Agent 视觉直读
                                                    │   └─ PaddleOCR 批量 / RapidOCR 文本快速通道
                                                    └─ 扫描版 B (深度): 切片 2~5 页 -> MinerU VLM
```

---

## 🛠️ 工具与脚本

| 脚本 | 适用格式 | 阶段 | 作用 |
|---|---|---|---|
| `probe.py` / `probe_pdf.py` | 全格式 | 1. 探针 | 自动嗅探格式，秒级输出元数据、文档形态（论文/书籍、单双栏）、章节/总页数与推荐路线 |
| `paper_parser.py` | PDF | 1. 探针 / 2. 索引 | **无目录 PDF 布局分析引擎**：相对字号标题识别、页眉页脚过滤、虚拟章节索引、双栏重排、文献剥离 |
| `build_index.py` | 全格式 | 2. 索引 | 解析章节目录构建 `chapters.json`（书签自愈 → 印刷目录 Offset → 布局索引三级回退，支持 `--force-layout`） |
| `extract_chapter.py` | 全格式 | 3. 提取 | 按章节名/序号/页码提取为 Markdown、纯文本、PDF 切片、OCR 回填或 JSON；扫描件自动降级协同输出 |
| `render_page.py` | PDF | 3. 视觉 | 将 PDF 页面渲染为高清 PNG，支持章节名匹配与多种范围参数，供 Agent 视觉模型直读 |
| `check_token.py` | 全格式 | 3. 鉴权 | 智能探测 MinerU 与 百度飞桨 PaddleOCR Token 配置状态，给出持久化配置指引 |
| `env_checker.py` | 全格式 | 支撑 | 分级依赖自愈检测与极速自动安装（Tier 1 核心包 / Tier 2 OCR 扩展） |
| `console_safe.py` | 全格式 | 支撑 | Windows GBK 控制台输出保护：文件名含「・」/ emoji / 生僻字时不再 `UnicodeEncodeError` 崩溃，所有 CLI 入口自动生效 |
| `ocr_helper.py` | 全格式 | 辅助 | PaddleOCR-VL 批量 API 与本地 RapidOCR 调度、标点与代码启发式清洗自愈、缓存降级 |

### 依赖说明

- Python 3.8+
- [PyMuPDF](https://pymupdf.readthedocs.io/)（`pip install pymupdf`，核心依赖，Paper Mode 亦仅依赖此包）
- 可选：`pip install requests`（百度飞桨 PaddleOCR-VL API 批量支持）
- 可选：`pip install rapidocr_onnxruntime`（本地离线轻量 OCR 支持）
- 可选：`pip install "markitdown[pdf]"`（数字版 PDF Markdown 转换）
- 可选：`npm install -g mineru-open-api`（扫描版 PDF VLM 深度解析）

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
# 1. EPUB / MOBI / AZW3 -> 毫秒级结构化 Markdown (0 Token，保留代码缩进与公式)
python scripts/extract_chapter.py "book.epub" --chapter "Chapter 1" --format md

# 2. 开启 OCR 图片代码与文字高精识别（首选 PaddleOCR 批量解析，自动 RapidOCR 本地兜底）
python scripts/extract_chapter.py "book.mobi" --chapter "7.3.5" --ocr --ocr-engine paddleocr -o "ch7_3_5.md"

# 3. 临时传入 PaddleOCR Token（亦可使用环境变量 PADDLEOCR_TOKEN 或 ~/.paddleocr/config.yaml）
python scripts/extract_chapter.py "book.mobi" --chapter "7.3.5" --ocr --paddle-token "your_token" -o "ch7_3_5.md"

# 4. 数字版 PDF -> 极速 Markdown / 纯文本
python scripts/extract_chapter.py "book.pdf" --chapter "第2章" --format md

# 5. 扫描版 PDF -> 自动智能降级 (高清切片图像 + 离线 OCR 双通道输出，推荐)
python scripts/extract_chapter.py "scanned_book.pdf" --chapter "2.3.2"

# 6. 指定章节/页码高清视觉渲染（Agent 原生多模态视觉直读）
python scripts/render_page.py "book.pdf" -c "2.3.2" --dpi 150
# 或按页码范围容错传参
python scripts/render_page.py "book.pdf" 38 40 --dpi 150

# 7. 检测 Token 配置状态 (check_token.py)
python scripts/check_token.py
```

### 无目录学术论文 / 报告（Paper Mode）

无需书签、无需目录页，**自动触发**；也可 `--force-layout` / `--paper-mode` 强制启用。

```bash
# 探针确认 Paper Mode 与识别到的虚拟标题数
python scripts/probe.py "paper.pdf"

# 查看布局分析重建的虚拟目录（提取前务必先确认，标题是启发式识别的）
python scripts/build_index.py "paper.pdf" --print
python scripts/paper_parser.py "paper.pdf" --print

# 按虚拟章节名 / 编号提取（支持模糊匹配）
python scripts/extract_chapter.py "paper.pdf" --chapter "Introduction" -o intro.md
python scripts/extract_chapter.py "paper.pdf" --chapter "3.2" --format md -o method.md

# 学术增强选项
python scripts/extract_chapter.py "paper.pdf" --chapter "Results" --strip-refs -o results.md
python scripts/extract_chapter.py "paper.pdf" --range "1-3" --format text --column-aware -o head.txt

# 强制走布局索引（书签质量差时）
python scripts/build_index.py "paper.pdf" --force-layout --print
python scripts/extract_chapter.py "paper.pdf" --paper-mode --chapter "Methods" -o methods.md
```

> ⚠️ 虚拟索引是启发式结果：字号层次不明显的文档可能漏识别，表格 / 图注可能被识别为标题。**提取前先 `--print` 确认**，不确定就改用 `--range "start-end"` 按物理页提取。切片为页级粒度，同页多个小节会一起带出。扫描版无文字层不走此路径，按上方第 5/6 条处理。

完整 SOP、参数速查与边界说明见 [SKILL.md](SKILL.md)。

---

## 📚 参考资料（references/）

| 文档 | 内容 |
|---|---|
| `epub-pipeline.md` | EPUB / MOBI / AZW3 流式电子书解析流水线、代码块提取与 PaddleOCR/RapidOCR 回填指南 |
| `vision-pipeline.md` | Agent-Native 多模态视觉直接解析流水线与扫描版协同工作流 |
| `offset-model.md` | PDF 页码体系与 Offset 偏移量模型（含扫描漂移防御） |
| `min-pipeline.md` | 工具链选型全景矩阵与 MinerU / PaddleOCR 规范 |

---

## ⚠️ 黄金守则（Guardrails）

1. **流式格式零 Token 优先**：遇到 EPUB / MOBI / AZW3 直接纯 Python 提取正文，严禁整本走 OCR 或大模型视觉。
2. **先切后送，严禁全本提交**：扫描件超过 20 页严禁一次性提交 MinerU，必须切出 2~5 页子段。
3. **数字版常规页杜绝 MinerU/OCR**：数字版正文直接走 MarkItDown/PyMuPDF，1 秒搞定。
4. **多模态视觉优先**：环境支持图像输入时，优先 `render_page.py` 或 `extract_chapter.py` 视觉直读。
5. **Token 严格隔离**：Token 仅走环境变量/外部配置文件，绝不写入版本库。
6. **虚拟索引先验证再提取**：Paper Mode 的章节标题是启发式识别，先 `build_index.py --print` 确认；不确定就用 `--range`，不要盲目信任自动匹配。
7. **参考文献剥离是可选操作**：`--strip-refs` 默认关闭，仅在处理学术论文且用户明确不需要文献时开启；严禁对普通电子书自动剥离任何内容。
8. **拒绝整本读取**：任何情况下都不要把全书 / 全篇 PDF 一次性送入上下文，坚持按需切片。
