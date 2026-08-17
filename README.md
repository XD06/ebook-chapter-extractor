# 📄 PDF 按章节按需解析 (PDF Chapter Extractor)

> **针对大本工具书/教材/专著的按需章节提取技能** —— 通过目录定位、物理页计算与切片提取，只解析目标章节，避免整本转换的巨大资源与时间开销。

一个面向 **AI Agent / LLM** 的 PDF 按章节按需解析标准作业程序（SOP）技能。适用于数百至上千页的 PDF 书籍，支持数字版、扫描版、复杂排版等多种文档形态，自动判别最优解析路线。

---

## ✨ 核心特性

- **按需切片，拒绝整本转换**：通过书签/目录定位目标章节，只切出 2~5 页小切片，资源消耗降低数十倍
- **书签自愈算法**：自动修复大章页码为 `0 / -1` 的脏书签数据，稳定构建章节索引
- **Offset 偏移量模型**：无书签时用印刷目录逻辑页 + Offset 换算物理页，含扫描件漂移防御
- **四路解析引擎**：PyMuPDF / MarkItDown / Agent-Native 多模态视觉直读 / MinerU VLM 精确解析，自动分流
- **Token 安全隔离**：MinerU Token 仅走环境变量或外部配置，绝不硬编码进代码

---

## 📊 决策树分流体系

```
                   用户请求 PDF 目标章节
                             │
           ┌─────────────────┴─────────────────┐
           ▼                                   ▼
  【分支 1：数字版 PDF】               【分支 2：扫描版/图像 PDF】
  (有清晰文字层，非图片)                (扫描图像、双层PDF、无文字层)
           │                                   │
           ▼                                   ▼
  PyMuPDF 纯文本问答            ┌──────────────┴──────────────┐
  MarkItDown 结构化排版         ▼                             ▼
                       场景A 多模态视觉直读        场景B 纯文本LLM
                       (高清渲染1s, 零OCR)        (切小切片→MinerU VLM)
```

---

## 🛠 工具与脚本

| 脚本 | 阶段 | 作用 |
|---|---|---|
| `probe_pdf.py` | 1. 探针 | 输出文档特征（总页数、数字/扫描、书签状态、推荐路线） |
| `build_index.py` | 2. 索引 | 书签自愈 + 构建章节物理页索引，导出 `chapters.json` |
| `extract_chapter.py` | 4. 提取 | 按章节/页范围提取为文本、Markdown、子 PDF 切片 |
| `render_page.py` | 4. 视觉 | 扫描页渲染高清 PNG，供 Agent 多模态视觉直读 |
| `check_token.py` | 4. 检测 | 检测 MinerU Token 配置状态，决定走精确 VLM 还是兜底 flash-extract |

### 依赖

- Python 3.8+
- [PyMuPDF](https://pymupdf.readthedocs.io/)（`pip install pymupdf`，核心依赖）
- 可选：`pip install "markitdown[pdf]"`（数字版结构化提取）
- 可选：`npm install -g mineru-open-api`（扫描版 VLM 精确解析）

---

## 🚀 快速开始

### 阶段 1：探针（Probe）

```bash
python scripts/probe_pdf.py "<pdf_path>"
```

### 阶段 2：索引与定位（Locate & Heal）

```bash
python scripts/build_index.py "<pdf_path>" --print
```

### 阶段 3：按需解析（Execute）

```bash
# 数字版 → 秒级转结构化 Markdown
python scripts/extract_chapter.py "<pdf_path>" --chapter "第2章" --format md

# 扫描版 + Agent 原生视觉（最快，~1.5s）
python scripts/render_page.py "<pdf_path>" --pages "264" --dpi 150

# 扫描版 + 纯文本 LLM（先切小切片，再送 MinerU，Token 优先）
python scripts/check_token.py                      # 检测 Token
python scripts/extract_chapter.py "<pdf_path>" --range "264-265" --format pdf --output slice.pdf
mineru-open-api extract slice.pdf --model vlm -f md   # ⭐ Token 精确模式（首选）
# 无 Token 才降级：mineru-open-api flash-extract slice.pdf
```

---

## 📚 参考资料（references/）

| 文档 | 内容 |
|---|---|
| `min-pipeline.md` | MinerU 与工具链选型流水线全景矩阵 |
| `offset-model.md` | 页码体系与 Offset 偏移量模型（含扫描漂移防御） |
| `vision-pipeline.md` | Agent-Native 多模态视觉直接解析流水线 |

---

## 🔐 MinerU Token 规范

本技能**严禁硬编码任何真实 API Token**。统一通过以下优先级解析：

1. `--token <token>` 命令行参数
2. `MINERU_TOKEN` 环境变量
3. `~/.mineru/config.yaml` 配置文件

> 免费申请 Token：<https://mineru.net/apiManage/token>

**Token 优先原则**：扫描件深度解析时**默认使用 Token 精确模式**（`extract --model vlm`，高保真还原表格/公式/代码），仅在用户确实无法提供 Token 时才降级 `flash-extract`（质量差、排队慢）。

```bash
# 检测 Token 状态
python scripts/check_token.py

# 推荐：环境变量（临时生效）
$env:MINERU_TOKEN="your_token_here"

# 或：管道输入写入配置（持久化，避免卡交互）
"your_token_here" | mineru-open-api auth

# 注：`mineru-open-api auth --token "..."` 在 v0.5.9 会卡在交互输入，勿用
```

---

## ⚠️ 黄金守则（Guardrails）

1. **先切后送，严禁全本提交**：扫描件超过 20 页严禁一次性提交 MinerU，必须切出 2~5 页子段
2. **数字版常规页杜绝 MinerU/OCR**：数字版正文直接走 MarkItDown/PyMuPDF，1 秒搞定；**但个别图片页（如目录页）`get_text` 为空时，例外回退到 MinerU Token 解析**
3. **多模态视觉优先**：环境支持图像输入时，优先 `render_page.py` 视觉直读
4. **Token 优先**：扫描件深度解析默认用 Token 精确模式，仅用户无法提供 Token 时才降级 flash-extract
5. **Token 安全隔离**：Token 仅走环境变量/外部配置，绝不写入版本库

---

## 📄 License

MIT

---

## 🙋 使用场景

- 对大型 PDF 教材、工具书、专著按章节提问（"读取第 X 章"、"提取第 X 章"）
- 处理扫描版书籍的复杂表格、代码、数学公式
- 需要快速定位物理页与逻辑页偏移的场景
- AI Agent 为主模型提供 PDF 按需解析能力