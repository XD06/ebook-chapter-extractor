---
name: ebook-chapter-extractor
description: "全格式电子书 (EPUB / MOBI / AZW3 / PDF) 与无目录学术 PDF 的按需伴读、章节提取技能。为 AI Agent 定制：毫秒级定位并按需切片，0 Token、0 网络开销、拒绝整本读取。支持流式电子书原生 Markdown 解析、代码块保护与混合 OCR 回填 (PaddleOCR-VL 批量 + RapidOCR 本地兜底)；PDF 书签 0/-1 自愈、印刷目录 Offset 换算；学术论文/期刊/报告/长文等无目录 PDF 自动进入 Paper Mode（布局分析重建虚拟章节索引、双栏阅读顺序重排、可选文末参考文献剥离）；扫描版智能降级为高清原图 + 离线 OCR 双通道输出、多模态视觉直读 (render_page.py) 与 MinerU VLM 高精解析。内置分级依赖自愈 (env_checker.py) 与只读路径缓存降级。"
when_to_use: 当用户要求查看、伴读、辅导、读取、提取、解析、切片或总结电子书或 PDF (EPUB/MOBI/AZW3/PDF) 的某个章节/小节/页码范围；或处理没有目录的学术论文、期刊、学位论文、技术报告、白皮书、长篇文章；或告知自己正在阅读某本书/某篇论文的某个部分时使用。
---

# ebook-chapter-extractor (伴读与按需章节提取 SOP)

> **核心原则**：永远「先定位、再切片、后读取」，绝不整本送入上下文。
> **两类文档**：有目录的电子书走「书签/目录索引」；无目录的论文走「布局分析虚拟索引 (Paper Mode)」。

---

## 0. 脚本与参考文件清单 (Architecture Map)

| 文件路径 | 适用格式 | 阶段 / 角色 | 核心功能说明 |
|---|---|---|---|
| `scripts/probe.py` | 全格式 | 1. 探针 | 统一探针入口，嗅探格式、章节/总页数并推荐解析路线 |
| `scripts/probe_pdf.py` | PDF | 1. 探针 | PDF 深度探针：文字层、书签状态、扫描件特征、文档形态（论文/书籍、单双栏）、Paper Mode 触发提示 |
| `scripts/paper_parser.py` | PDF | 1. 探针 / 2. 索引 | **无目录 PDF 布局分析引擎**：正文基准字号相对判定、整行聚合、页眉页脚过滤、虚拟章节索引、双栏重排、严格文末参考文献剥离 |
| `scripts/build_index.py` | 全格式 | 2. 索引 | 构建 `chapters.json`；PDF 三级回退：书签自愈 → 文本目录 + Offset → 布局分析虚拟索引 |
| `scripts/extract_chapter.py` | 全格式 | 3. 提取 | 按章节名/序号/范围提取 MD、PDF 切片、高清图、OCR 回填、扫描件双通道降级、JSON 输出 |
| `scripts/render_page.py` | PDF | 3. 视觉 | 将指定页/章节渲染为高清 PNG，供多模态 Agent 视觉直读 |
| `scripts/check_token.py` | 全格式 | 3. 鉴权 | 探测 MinerU / 百度飞桨 PaddleOCR Token 状态与持久化配置指引 |
| `scripts/env_checker.py` | 全格式 | 底层支撑 | 分级依赖自愈检测与极速自动安装（Tier 1 基础解析 / Tier 2 OCR 扩展） |
| `scripts/console_safe.py` | 全格式 | 底层支撑 | Windows 窄编码控制台保护：文件名/标题含非 GBK 字符（`・`、emoji、生僻字）时不再 `UnicodeEncodeError` 崩溃 |
| `scripts/epub_parser.py` | EPUB | 解析引擎 | 纯 Python 流式解析，章节合并、MathML 转 LaTeX、代码块等宽保留、DOM 清洗 |
| `scripts/mobi_parser.py` | MOBI/AZW3 | 解析引擎 | PalmDOC LZ77 解压与 KF8 解包，内嵌图片、正则断章、代码识别 |
| `scripts/ocr_helper.py` | 全格式 | OCR 辅助 | PaddleOCR-VL 批量 API + RapidOCR 本地兜底、代码自愈、标点清洗、原图路径关联 |
| `scripts/mathml_helper.py` | 全格式 | 公式辅助 | MathML → LaTeX Markdown (`$...$`) AST 转换 |
| `references/epub-pipeline.md` | 流式规范 | 参考指南 | EPUB / MOBI / AZW3 流式电子书技术流水线 |
| `references/min-pipeline.md` | 选型规范 | 参考指南 | 工具链选型矩阵与 MinerU / PaddleOCR Token 管理 |
| `references/offset-model.md` | PDF 规范 | 参考指南 | 逻辑页/物理页体系、Offset 偏移量模型与扫描漂移防御 |
| `references/vision-pipeline.md` | 视觉规范 | 参考指南 | Agent-Native 多模态视觉直读工作流与 SOP |

---

## 1. 核心决策树 (Decision Tree)

```text
              用户请求提取 电子书 / 论文 / 章节 / 页码范围
                                │
          ┌─────────────────────┴─────────────────────┐
          ▼                                           ▼
【流式电子书 EPUB/MOBI/AZW3】                  【固定版式 PDF】
          │                                           │
  纯 Python 原生提取                          运行探针 probe.py
  (0.05s, 0 Token, 0 网络)                             │
          │                       ┌───────────────────┴───────────────────┐
          │                       ▼                                       ▼
          │              【有书签 / 有印刷目录】              【无书签且无目录 → Paper Mode】
          │                       │                                       │
          │            build_index.py                    paper_parser.py 布局分析
          │        书签 0/-1 自愈 / Offset                 字体度量重建虚拟章节索引
          │                       │                       自动过滤参考文献条目
          │                       └───────────────┬───────────────────────┘
          │                                       ▼
          │                    ┌──────────────────┴──────────────────┐
          │                    ▼                                     ▼
          │           【数字版：有文本层】                 【扫描版：无文本层】
          │        PyMuPDF / MarkItDown                 双通道：高清原图 + 离线 OCR
          │        (1 秒结构化 MD)                    render_page.py / MinerU VLM
          │                    │
          │         --chapter / --range / --index
          ▼                    ▼
  extract_chapter.py 按需切片输出 (md / text / pdf / image / json)
```

---

## 2. 流式电子书 SOP (EPUB / MOBI / AZW3)

流式电子书无页码偏移，100% 结构化还原，首选原生流式流水线。

```bash
# 1. 极速提取目标章节为 Markdown（自动包含下辖子节，0 Token）
python scripts/extract_chapter.py "book.epub" --chapter "Chapter 1" --format md

# 2. 伴读推荐：启用 OCR 智能回填（PaddleOCR 批量高精 API，RapidOCR 本地离线兜底）
python scripts/extract_chapter.py "book.mobi" --chapter "7.3.5" --ocr --ocr-engine paddleocr -o chapter.md

# 3. 临时传入 PaddleOCR API Token
python scripts/extract_chapter.py "book.mobi" --chapter "7.3.5" --ocr --paddle-token "your_token" -o chapter.md

# 4. 导出全部插图供多模态 Agent 直读
python scripts/extract_chapter.py "book.mobi" --chapter "2.1" --dump-images .cache/images/ -o chapter.md

# 5. Agent 结构化交互（输出含字数、插图路径的 JSON）
python scripts/extract_chapter.py "book.epub" --chapter "1.1" --json
```

---

## 3. 有目录 PDF 按需切片 SOP（五步法）

### 阶段 1：快速探针 (Probe)
```bash
python scripts/probe.py "book.pdf"
```
输出文档形态（论文 / 书籍、单双栏）、文字层、书签状态与决策分类 A/B/C/D。

### 阶段 2：索引与书签自愈 (Locate & Heal)
```bash
python scripts/build_index.py "book.pdf" --print
```
* **有书签**：书签页码多为物理页（1-based），自愈修复异常 `0/-1` 页码后直接定位。
* **有印刷目录无书签**：从目录提取逻辑页码，通过正文 P1 物理页计算偏移量：
  $$\text{物理页} = \text{逻辑页} + \text{Offset} \quad (\text{Offset} = \text{正文 P1 物理页} - 1)$$

### 阶段 3：提取前自检 (Sanity Check)
校验目标物理页前 3 行是否匹配章节名。未匹配时在 $\pm 2$ 页微调防御扫描件漂移；若目标页 `get_text()` 为空（纯图片），立即回退渲染 / 图片解析模式。

### 阶段 4：按需解析执行 (Execute)
```bash
# 1) 数字版 PDF（极速 Markdown）
python scripts/extract_chapter.py "book.pdf" --chapter "第2章" --format md

# 2) 扫描版 PDF 智能降级（双通道：高清原图 + 离线 OCR 文本）
python scripts/extract_chapter.py "scanned_book.pdf" --chapter "2.3.2"

# 3) 扫描版 + 多模态视觉直读
python scripts/render_page.py "book.pdf" -c "2.3.2" --dpi 150
python scripts/render_page.py "book.pdf" --pages "38-40" --dpi 150

# 4) 扫描版 + MinerU VLM 高精解析（复杂表格 / 公式）
python scripts/check_token.py
python scripts/extract_chapter.py "book.pdf" --range "264-266" --format pdf -o slice.pdf
mineru-open-api extract slice.pdf --model vlm -f md -o ./output/
```

### 阶段 5：沉淀与交付 (Deliver)
索引缓存至 `.cache/<stem>_chapters.json`，提取结果缓存至 `.cache/chapter_xx.md`，避免同一会话重复解析。只读路径自动降级至系统临时目录。

---

## 4. 无目录学术论文 SOP（Paper Mode）★

**适用**：期刊论文、学位论文、Supplementary Materials、技术报告、白皮书、长篇文章 —— 即**没有任何书签，也没有印刷目录页**的 PDF。

### 4.1 自动触发条件
`probe.py` / `build_index.py` / `extract_chapter.py` 在「数字版 + 无书签 + 无文本目录」时自动启用，无需手动开关：

| 层级 | 行为 |
|---|---|
| `probe_pdf.py` | 报告 `Paper Mode`、虚拟标题数、论文/书籍形态、单双栏 |
| `build_index.py` | 三级回退第 3 级调用 `paper_parser.build_virtual_index()` 生成 `chapters.json`（`is_virtual: true`） |
| `extract_chapter.py` | `--chapter` 在书签为空时自动回退虚拟索引做模糊匹配 |

### 4.2 标准操作流
```bash
# Step 1 探针：确认 Paper Mode 与识别到的标题数
python scripts/probe.py "paper.pdf"

# Step 2 查看虚拟目录（务必先看，标题是启发式识别的）
python scripts/build_index.py "paper.pdf" --print
# 或只看布局分析结果
python scripts/paper_parser.py "paper.pdf" --print

# Step 3 按虚拟章节名提取（支持模糊 / 归一化 / 章节号前缀匹配）
python scripts/extract_chapter.py "paper.pdf" --chapter "Introduction" -o intro.md
python scripts/extract_chapter.py "paper.pdf" --chapter "3.2" --format md -o method.md

# Step 4 学术论文增强选项
python scripts/extract_chapter.py "paper.pdf" --chapter "Results" --strip-refs -o results.md
python scripts/extract_chapter.py "paper.pdf" --range "1-3" --format text --column-aware -o head.txt

# Step 5 强制走布局索引（即便文件有书签，或书签质量差时）
python scripts/build_index.py "paper.pdf" --force-layout --print
python scripts/extract_chapter.py "paper.pdf" --paper-mode --chapter "Methods" -o methods.md
```

### 4.3 布局分析引擎原理 (`paper_parser.py`)
1. **按行聚合 span**：避免一个标题被拆成多个碎片。
2. **正文基准字号**：字符加权众数（0.5pt 分桶），而非绝对阈值 —— 因此 9pt 小字期刊与 14pt 大字报告都适用。
3. **标题判据（满足其一）**：字号 ≥ 基准 ×1.22 / 加粗短行 / 编号标题（`1`、`2.3`、`Chapter 2`、`第1章`，要求后缀首字母大写以排除表格数字）/ 学术固定标题词整行（`Abstract`、`Introduction`、`References`…）。
4. **噪声过滤**：跨页重复的页眉页脚、`doi:`、`et al.`、`(2016)` 等引文特征、表头、正文长句；**文末参考文献区之后不再收集标题**。
5. **页级区间**：同页多标题不会产生 0 页 / 负页区间；索引字段与书签索引完全一致，下游无需区分。

### 4.4 学术增强能力
| 能力 | 开关 | 说明 |
|---|---|---|
| 文末参考文献剥离 | `--strip-refs` | **显式开启**，仅剥离文档后 40% 中最后一次「独占一行」的 References/Bibliography 标题之后的内容；不会误伤正文中提及或 DOI。**默认关闭**，避免影响普通电子书。 |
| 双栏阅读顺序重排 | `--column-aware` | `--format text` 时按「先左栏自上而下，再右栏自上而下」重建语序，解决双栏 PDF 线性读取串列问题。 |
| 强制布局索引 | `--paper-mode` / `--force-layout` | 书签缺失、书签质量差或想统一按视觉标题切分时使用。 |

### 4.5 已知边界（重要）
* 虚拟索引是**启发式**结果：字号层次不明显的文档可能漏识别，表格 / 图注可能被识别为标题。**提取前先用 `build_index.py --print` 人工确认**，不确定时改用 `--range "start-end"` 按物理页提取。
* 切片粒度是**页级**：若同一页内包含多个小节，提取结果会含同页后续小节内容；这是「到下一节之前」的正常语义。需要更细粒度时用 `--range` 或 `render_page.py` 视觉直读。
* 扫描版无目录 PDF **不走**布局分析（无文字层），按 §3 阶段 4 的 OCR / MinerU 路线处理。
* 复杂公式、跨页表格仍建议 `render_page.py` 视觉直读或 MinerU VLM，不要指望纯文本提取 100% 还原。

---

## 5. CLI 参数速查 (`extract_chapter.py`)

| 参数 | 作用 |
|---|---|
| `--chapter / -c <name>` | 章节标题 / 编号 / 关键词（模糊匹配，无书签时自动查虚拟索引） |
| `--index / -i <n>` | 章节序号（主要用于 EPUB/MOBI） |
| `--range / -r <a-b>` | PDF 物理页范围 / EPUB 章节范围 |
| `--format / -f` | `text` / `md`(默认) / `pdf` / `image` / `html` |
| `--output / -o` | 输出文件或目录 |
| `--json` | 输出含元数据的 JSON（Agent 友好） |
| `--ocr` / `--ocr-engine` / `--paddle-token` | OCR 回填与引擎选择 |
| `--dump-images <dir>` | 导出章节内嵌图片供视觉直读 |
| `--dpi` | 渲染 DPI（默认 150） |
| `--strip-refs` | Paper Mode：剥离文末参考文献（默认关闭） |
| `--column-aware` | Paper Mode：`text` 格式双栏语序重排 |
| `--paper-mode` | Paper Mode：强制使用布局虚拟索引 |
| `--force-layout` | `build_index.py` 专用：强制布局索引 |

---

## 6. API Token 管理与用户引导规范

> ⚠️ **严禁硬编码**：脚本与文档严禁固化真实 Token，只走环境变量 / 外部配置文件。

### 6.1 MinerU Token
当纯文本 LLM 解析扫描件复杂表格 / 公式且未配置 Token 时，先检查 `python scripts/check_token.py`：
1. 引导用户前往 [https://mineru.net/apiManage/token](https://mineru.net/apiManage/token) 获取免费 Token。
2. 配置：
   ```powershell
   # 方式 A（推荐，环境变量）
   $env:MINERU_TOKEN="your_token_here"

   # 方式 B（持久化配置）
   "your_token_here" | mineru-open-api auth
   mineru-open-api auth --verify
   ```

### 6.2 百度飞桨 PaddleOCR Token
1. 引导用户前往百度飞桨 AI Studio 开放平台获取免费 API Access Token。
2. 配置：
   ```powershell
   # 方式 A（环境变量，推荐）
   $env:PADDLEOCR_TOKEN="your_token_here"

   # 方式 B（持久化配置 ~/.paddleocr/config.yaml）
   token: "your_token_here"
   ```

---

## 7. 黄金守则 (Guardrails)

1. **流式 0 Token 绝对优先**：EPUB / MOBI / AZW3 坚决走纯 Python 原生提取，严禁整本走 OCR 或大模型视觉。
2. **先切片后送 MinerU**：扫描件超过 20 页严禁一次性提交，必须切出 2~5 页子切片。
3. **数字版常规页杜绝 MinerU/OCR**：直接走 PyMuPDF / MarkItDown，秒级交付。
4. **Token 严格安全隔离**：仅走环境变量与外部配置文件，严禁写入代码库。
5. **图文双轨原图校对**：OCR 遇排版歧义时，多模态 Agent 依据 Markdown 附带的真实物理路径主动看图自愈。
6. **虚拟索引先验证再提取**：Paper Mode 的章节标题是启发式识别，先 `build_index.py --print` 确认；不确定就用 `--range` 按物理页提取，不要盲目信任自动匹配结果。
7. **参考文献剥离是可选操作**：`--strip-refs` 默认关闭，仅在处理学术论文且用户明确不需要文献时开启；严禁对普通电子书自动剥离任何内容。
8. **拒绝整本读取**：任何情况下都不要把全书 / 全篇 PDF 一次性送入上下文，坚持按需切片。

---

## 8. 常见故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `Could not find chapter matching '...' in TOC or virtual layout index` | 先跑 `build_index.py <pdf> --print` 看实际识别到的标题；或改用 `--range "3-5"` |
| 虚拟标题过多 / 全是正文 | 文档字号层次不明显；用 `--paper-mode` 配合 `--range`，或 `render_page.py` 视觉直读 |
| 探针无输出 / 依赖报错 | 用装有 PyMuPDF 的解释器执行（如 `py -3.14`），或让 `env_checker.py` 自动安装 `pymupdf` |
| 双栏 PDF 文本左右串列 | 加 `--column-aware --format text` |
| 扫描版 Paper Mode 无标题 | 正常：无文字层，改走 §3 阶段 4 的 OCR / MinerU 路线 |
