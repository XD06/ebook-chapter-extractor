# 流式电子书解析流水线指南 (EPUB / MOBI / AZW3)

## 1. 为什么流式电子书对 Agent 极度友好？

传统固定版面 PDF 在提取大章节时常常遇到：
- 双栏排版文字穿插；
- 物理页与印刷逻辑页存在 Offset 偏移；
- 扫描版需要 OCR / VLM 或视觉直读，耗时且消耗资源。

而 **EPUB / AZW3 / MOBI** 这类流式电子书（Reflowable eBooks）本质上是**经过打包的标准化 XHTML / HTML 文档集合**，对 AI Agent 具有天然优势：

| 维度 | EPUB / AZW3 / MOBI 流水线 | 传统 PDF 流水线 |
|---|---|---|
| **解析速度** | **0.05 秒（毫秒级）** | 1.5s（视觉）~ 30s（VLM） |
| **外部依赖** | **纯 Python 内置库**（`zipfile` + `xml` + `bs4`） | PyMuPDF / MinerU / OCR / Node CLI |
| **Token 开销** | **0 Token（纯本地离线）** | 需消耗云端 Token 或多模态 Token |
| **格式与排版** | **100% 结构化精准还原**（表格、代码块高亮） | 可能出现换行粘连或漏字 |
| **Offset 问题** | **无需计算任何 Offset** | 需探测正文 P1 物理页并防御漂移 |

---

## 2. 核心架构与解析原理

### 2.1 EPUB (EPUB 2 / EPUB 3)
```text
ebook.epub (ZIP 容器)
  ├─ META-INF/container.xml ──────> 定位 content.opf 路径
  └─ OEBPS/
      ├─ content.opf ─────────────> 提取书名、作者、manifest 清单与 spine 顺序
      ├─ toc.ncx / nav.xhtml ─────> 提取层级目录树 (标题 -> 文件名#锚点)
      ├─ Text/*.xhtml ────────────> 各章节独立 HTML 源码
      └─ Images/*.png/jpg ────────> 章节内嵌插图/代码图
```

1. **目录索引定位**：
   - **EPUB 2**：解析 `toc.ncx` 中的 `<navPoint>` 节点；
   - **EPUB 3**：解析 `nav.xhtml` 中的 `<nav epub:type="toc">`；
   - **Spine 兜底**：若目录损坏，按 `<spine>` 中定义的线性阅读顺序解析每个文档的 `<title>` 或 `<h1>`。
2. **章节内容转换与代码块智能识别**：
   - 提取指定章节对应的 XHTML 片段；
   - 预处理 `<pre>` / `<code>` 标签与等宽字体样式，规范化为标准 Markdown Fenced Code Blocks，防止换行折叠；
   - 支持 MathML 标签递归转换为 LaTeX 行内/行间公式 (`$...$` / `$$...$$`)；
   - 使用 `html2text` 或 `markdownify` 转换为结构化 Markdown，保留原生表格与代码缩进。

---

## 3. MOBI / AZW / AZW3 (KF8)

```text
book.mobi / book.azw3 (Palm Database 格式)
  ├─ PDB Header & Record List ───> 记录块定位
  ├─ Record 0 (Mobi Header) ─────> 提取元数据、压缩算法与 KF8 Boundary / 首张图片索引
  └─ Records 1..N:
      ├─ KF8 (AZW3) ─────────────> 内部嵌入 EPUB 归档 -> 直接复用 EPUB 引擎
      ├─ PalmDOC (Mobi6) ────────> LZ77 算法纯 Python 瞬时解压 -> HTML 正则断章
      └─ Image Records ──────────> PDB 记录中提取二进制图片 (通过 recindex 映射)
```

- **AZW3 (KF8)**：结构内嵌完整 EPUB 归档，直接采用 EPUB 解析流水线处理，目录抽取时支持粗体标题正则嗅探兜底；
- **Mobi6**：采用纯 Python 原生实现的 LZ77 解压算法，零外部 C 拓展依赖，基于标题标签与锚点特征智能聚类断章。

---

## 4. 插图与代码图处理流水线 (Vision vs OCR)

某些书籍制作时将代码、表格制作成了插图嵌入电子书中。流水线支持两种分流策略：

```text
                               ┌──> 策略 A: --dump-images (保存高清插图供视觉多模态大模型直读)
[章节内嵌插图/代码图 (IMG)] ──┤
                               └──> 策略 B: --ocr (调用 RapidOCR 识别图内文本与代码，格式化回填 Markdown)
```

### 4.1 视觉大模型模式 (`--dump-images`)
将该章节涉及的插图提取并保存在 `.cache/images/`，多模态 LLM 直接查看原图：
```bash
python scripts/extract_chapter.py "book.mobi" --chapter "2.1 进入C++" --dump-images
```

### 4.2 纯文字大模型模式 (`--ocr`)
自动通过 `RapidOCR`（ONNXRuntime 轻量高精度引擎）识别插图中的代码和文字，并通过 `clean_code_ocr` 进行代码启发式自愈（修复括号、赋值符与注释），自动包装成代码块或引用块插入 Markdown 原位置：
```bash
python scripts/extract_chapter.py "book.mobi" --chapter "2.1 进入C++" --ocr
```

---

## 5. 标准 CLI 调用命令

```bash
# 1. 探针检查（支持 .epub / .mobi / .azw3 / .pdf）
python scripts/probe.py "book.epub"

# 2. 生成结构化章节索引
python scripts/build_index.py "book.epub" --print

# 3. 按章节名或序号提取 Markdown
python scripts/extract_chapter.py "book.epub" --chapter "第1章" --format md
python scripts/extract_chapter.py "book.epub" --index 2 --format md
python scripts/extract_chapter.py "book.mobi" --range "1-3" --format md -o output.md

# 4. 提取 Markdown 同时导出插图与执行 OCR
python scripts/extract_chapter.py "book.mobi" --chapter "2.1 进入C++" --ocr --dump-images -o chapter.md
```
