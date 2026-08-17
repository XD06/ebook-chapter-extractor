# MinerU 与工具链选型流水线指南

## 1. 工具选型全景矩阵

| 工具 | 适用场景 | 平均速度 | 核心优势 | 局限性 / 劣势 | 依赖与配置 |
|---|---|---|---|---|---|
| **Agent-Native 视觉直读** | **扫描版/复杂排版/首选方案** | **~1.5秒** | 零网络依赖、零排队、保真度极高，代码缩进与 LaTeX 完美还原 | 仅适用于支持多模态图像的模型 | `pip install pymupdf` |
| **MarkItDown** | **原生数字版 PDF 最佳选择** | **~1秒** | 毫秒级提取，保留标题、表格、代码块，输出整洁 Markdown | 不支持纯图片扫描件 OCR | `pip install "markitdown[pdf]"` |
| **MinerU (`extract --model vlm`)** | **扫描版/纯文本LLM深度解析首选** | **~20-30秒** | **高精 VLM 驱动**，表格重建 HTML，公式 LaTeX，代码自动缩进 | 需配置 `MINERU_TOKEN`，需切片 | `npm install -g mineru-open-api` |
| **MinerU (`flash-extract`)** | 扫描版/纯文本LLM（无Token备选） | ~60秒 | 免登录、免 Token、快速验证 | 排队时间较长，公式可能误识（如将 ∞ 识别为 8） | `npm install -g mineru-open-api` |
| **PyMuPDF (`get_text`)** | 双层 PDF 快速概念问答 | **0.01秒** | 极致速度，零延迟 | 丢失复杂表格与代码排版 | `pip install pymupdf` |

---

## 2. 扫描件在纯文本 LLM 下的黄金两级解析策略

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
                                              检测环境 Token (MINERU_TOKEN)
                                              ├─ 已配置: 调用 extract --model vlm (高精还原)
                                              └─ 未配置:
                                                   ├─ 引导用户获取 Token
                                                   └─ 降级执行 flash-extract
```

---

## 3. MinerU Token 管理与用户引导规范

### 3.1 绝不硬编码 Token 原则
技能代码、文档或脚本中严禁包含真实的 API Token。统一通过以下优先级解析：
1. `--token <token>` 命令行参数
2. `MINERU_TOKEN` 操作系统环境变量
3. `~/.mineru/config.yaml` 配置文件

### 3.2 引导用户获取与配置 Token 的标准流程
当检测到需要高精度解析复杂表格/公式但本地尚未配置 Token 时，按以下指引提示用户：

1. **获取免费 Token**：
   访问官网：[https://mineru.net/apiManage/token](https://mineru.net/apiManage/token)（注册后即可免费创建 Token）。
2. **配置 Token**：
   ```bash
   # Windows PowerShell 设置环境变量（临时生效）
   $env:MINERU_TOKEN="your_token_here"

   # 或使用 CLI 一键写入配置（永久生效）
   mineru-open-api auth --token "your_token_here"
   ```

---

## 4. MinerU 最佳实战命令集

### (1) 先切片（严禁整本上传）
```bash
python skills/pdf-chapter-extractor/scripts/extract_chapter.py "large_book.pdf" --range "264-266" --format pdf --output slice.pdf
```

### (2) 精确 VLM 解析（有 Token，速度快、效果最好）
```bash
mineru-open-api extract slice.pdf --model vlm -f md -o ./output/
```

### (3) 免 Token 快速模式（无 Token 时的备用降级）
```bash
mineru-open-api flash-extract slice.pdf -o ./output/
```