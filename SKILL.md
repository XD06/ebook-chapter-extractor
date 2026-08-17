---
name: pdf-chapter-extractor
description: "PDF按章节按需解析与提取技能。针对大本工具书/教材/专著，通过目录定位、物理页计算与切片提取，按需解析目标章节，避免整本转换的巨大资源与时间开销。自动探测内嵌书签（get_toc）、自愈异常书签、无书签文本目录提取、偏移量校准（含扫描件漂移防御）；支持 PyMuPDF / MarkItDown（数字版）、Agent-Native 多模态视觉直读（扫描版/复杂排版/零OCR依赖）以及 MinerU VLM 精确解析（扫描版/文字LLM深度提取）。"
when_to_use: 当用户要求查看、读取、提取、解析、总结 PDF 书籍的某个章节/部分，或者给定大 PDF 并针对特定章节提问时必须触发。
  触发词："读取第X章"、"提取第X章"、"看看第X章"、"解析章节"、"PDF章节"、"只看这一章"、"提取目录"、"Contents"、"Million Dollar Weekend"、"计算机网络第2章"、"SQL必知必会"。
  不触发：小于5页的简短PDF、非PDF文件、整本不需要拆分的全面单页文档。
---

# PDF 按章节按需解析 (PDF Chapter Extractor)

本技能定义了从大本 PDF（数百至上千页）中**按需定位、切片与解析指定章节**的标准作业程序（SOP），避免整本转换的巨大资源消耗与延迟。

---

## 一、 核心决策树与分流体系

```text
                           用户请求 PDF 目标章节
                                     │
                 ┌───────────────────┴───────────────────┐
                 ▼                                       ▼
        【分支 1：数字版 PDF】                   【分支 2：扫描版 / 图像 PDF】
        (有清晰文字层，非图片)                    (扫描图像、双层PDF、无文字层)
                 │                                       │
                 ▼                                       ▼
     ┌───────────────────────┐               ┌───────────────────────┐
     │ 纯文本问答: PyMuPDF   │               │ 场景 A: 多模态视觉   │
     │ 结构化排版: MarkItDown│               │   -> 高清渲染直读(1s) │
     └───────────────────────┘               │ 场景 B: 纯文本 LLM    │
                                             │   -> 见下方扫描件流水线│
                                             └───────────────────────┘
```

---

## 二、 扫描版 + 纯文本 LLM 解析流水线（精准分流）

当驱动主模型为**纯文本 LLM（无法直接输入图片）**时，按任务精度与页面特征执行确定性分流：

```text
                     扫描版 PDF (已定位目标物理页)
                                  │
          ┌───────────────────────┴───────────────────────┐
          ▼                                               ▼
【场景 1：宏观总结 / 概念大意问答】              【场景 2：深度研读 / 表格 / 代码 / 公式】
· 优先提取双层 PDF 自带文字层 (0ms)              · 严禁使用普通扁平 OCR (表格/代码必碎)
· 纯图片使用 RapidOCR 本地快速提取 (0.3s/页)      · 切出目标 2~5 页小 PDF 切片
· 毫秒级返回，满足基础问答                       · 优先使用 MinerU VLM 精确模式解析
                                                          │
                                                          ▼
                                              检测环境 Token (MINERU_TOKEN / ~/.mineru/config.yaml)
                                              ├─ 已配置: 调用 extract --model vlm (高精还原) ⭐ 首选
                                              └─ 未配置:
                                                   ├─ 引导用户提供 Token
                                                   └─ 用户确实无法提供时，才降级 flash-extract (质量差、慢)
```

---

## 三、 标准执行 Core Loop（五步法）

### 阶段 1：快速探针（Probe）
执行 `probe_pdf.py` 输出文档特征 JSON（总页数、是否有文字层、书签状态、推荐路线）：
```bash
python skills/pdf-chapter-extractor/scripts/probe_pdf.py "<pdf_path>"
```

### 阶段 2：索引与物理页定位（Locate & Heal）
执行 `build_index.py`，内置**书签自愈算法**，自动修复大章页码为 `0/-1` 的脏数据：
```bash
python skills/pdf-chapter-extractor/scripts/build_index.py "<pdf_path>" --print
```
* **有书签**：书签页码绝大多数即为物理页（1-based），自愈后直接切片；
* **无书签**：提取印刷目录页逻辑页码 $\to$ 寻找正文 P1 物理页计算 Offset（`物理页 = 逻辑页 + Offset`）。

> ⚠️ **页面级图片检测（重要踩坑）**：探针判定为"数字版"不代表每页都有文字层。
> 个别页（尤其**目录页**）可能是**扫描图片**（如《只是为了好玩》实测：整本数字版，唯独目录页 27-28 是图片，`get_text` 返回 0 行）。
> **阶段 3 Sanity Check 若发现目标页文字为空，立即回退**：渲染该页为 PNG → 走「四、MinerU Token 优先」图片解析，**切勿误以为定位错误而跳过**。

### 阶段 3：提取前自检（Sanity Check）
提取物理第 $P$ 页前 3 行文字，核验是否包含目标章节标题关键字。若不匹配，前后滑动 1~2 页校准，防御扫描件 Offset 漂移。

### 阶段 4：按需解析与执行（Execute）

1. **数字版**：
   ```bash
   # 秒级转结构化 Markdown（推荐喂 AI）
   python skills/pdf-chapter-extractor/scripts/extract_chapter.py "<pdf_path>" --chapter "<章节名>" --format md
   ```

2. **扫描版 + Agent 原生视觉（速度最快，~1.5秒）**：
   ```bash
   python skills/pdf-chapter-extractor/scripts/render_page.py "<pdf_path>" --pages "264" --dpi 150
   # Agent 直接读取生成的 PNG 图片进行视觉理解与转写
   ```

3. **扫描版 + 纯文本 LLM（深度结构化提取）**：
   * **第 0 步：检测 Token**（决定走精确还是兜底）：
     ```bash
     python skills/pdf-chapter-extractor/scripts/check_token.py
     ```
   * **第 1 步：切出目标小切片**：
     ```bash
     python skills/pdf-chapter-extractor/scripts/extract_chapter.py "<pdf_path>" --range "264-265" --format pdf --output slice.pdf
     ```
   * **第 2 步：Token 优先的 MinerU 提取**（**⭐ 默认走 Token 精确模式，绝不优先 flash-extract**）：
     * **若已配置 Token**（`MINERU_TOKEN` 环境变量 或 `~/.mineru/config.yaml`）——**首选**：
       ```bash
       mineru-open-api extract slice.pdf --model vlm -f md -o ./output/
       ```
     * **若未配置 Token**：先按「四、MinerU Token 引导规范」引导用户提供 Token；
       **仅当用户确实无法/不愿提供 Token 时**，才降级为 `flash-extract`（质量差、排队慢、公式易误识，仅作兜底）：
       ```bash
       mineru-open-api flash-extract slice.pdf -o ./output/
       ```

### 阶段 5：沉淀与交付（Cache & Deliver）
* 索引缓存保存至 `.cache/chapters.json`；
* 提取出的章节 Markdown 缓存至 `.cache/chapter_xx.md`，避免重复请求。

---

## 四、 MinerU Token 引导规范（禁止硬编码 Token）

在技能文档、脚本和代码中，**严禁固化/硬编码任何真实的 API Token**。统一通过环境变量或 CLI 鉴权。

### Token 配置与用户引导标准话术
当纯文本 LLM 需要高精度解析表格/公式且未检测到 Token 时，按以下规范引导用户：

> 💡 **提示**：检测到当前解析涉及复杂表格/公式/代码段。为了获得最高精度的 VLM 结构化解析，建议配置 MinerU API Token（免费申请）：
> 1. 前往官网获取 Token：[https://mineru.net/apiManage/token](https://mineru.net/apiManage/token)
> 2. 执行命令配置（**注意：`mineru-open-api auth --token "..."` 在 v0.5.9 会卡在交互输入，勿用**）：
>    ```bash
>    # 方式 A (推荐，临时生效，仅当前会话)：设置环境变量
>    $env:MINERU_TOKEN="your_token_here"
>
>    # 方式 B (持久化，写入 ~/.mineru/config.yaml)：管道输入，避免卡交互
>    "your_token_here" | mineru-open-api auth
>
>    # 验证配置是否生效
>    mineru-open-api auth --verify
>    ```

---

## 五、 黄金守则（Guardrails）

1. **先切后送，严禁全本提交**：扫描件超过 20 页严禁一次性提交 MinerU，必须切出目标 2~5 页子段后再提交。
2. **数字版常规页杜绝 MinerU/OCR**：数字版正文直接走 MarkItDown/PyMuPDF，1 秒搞定，不产生任何网络开销与排队；**但若个别页（如目录页）是图片、`get_text` 为空，则例外回退到 MinerU Token 解析**（见阶段 2 提示）。
3. **多模态视觉优先**：当运行环境支持图像输入时，优先采用 `render_page.py` 视觉直读，速度比云端 API 快数十倍。
4. **Token 优先于 flash-extract**：扫描件深度解析时，**默认使用 Token 精确模式**（`extract --model vlm`）；仅当用户明确无法提供 Token 时才降级 `flash-extract`（质量差、慢）。
5. **Token 安全隔离**：Token 仅通过环境变量或外部配置读取，绝不写入版本库或持久化 Skill 文件。