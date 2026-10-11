# docflow-ai — 企业文档智能处理平台

> 把分散在 Excel、CSV、PDF、Word、扫描件里的异构数据，变成**可解析、可校验、可追溯、可流转**的结构化数据。

面向 **AI 应用实施 / 解决方案 / 企业数据自动化** 方向的作品集项目。它回答一个真实问题：

**企业里 80% 的业务数据散落在格式各异的表格、文档和扫描件里，怎么把它们变成能直接进业务系统的干净数据？**

答案不是「接个大模型让它读」，而是一条完整的工程链路：

接入 → 解析 → 字段映射 → 类型标准化 → 质量检测 → 置信度分流 → 人工复核 → 导出。

## 一图看懂

```
                         文件接入
        Excel / CSV ──┐
        文本 PDF    ──┤──▶ RawTable（原始二维表）
        Word 表格   ──┤
        扫描件/图片 ──┤        ↑ OCR 分级链路
        DXF 图纸 ⏳ ──┘
                              │  一级 RapidOCR（快）
                              │   └ 置信度不足 → 二级本地 VL 模型（准）
                              │
                       字段映射（三级策略）
        同义词精确 ──▶ 模糊匹配 ──▶ LLM 推断
                              │
                       类型标准化 + 统一数据模型
                              │
        ┌────────────┬────────┴────────┬────────────┐
        ▼            ▼                 ▼            ▼
    字段级校验   记录级校验        表级校验      跨源级校验
  （必填/类型）（跨字段逻辑）（主键重复/整列空）（多来源同实体取值一致）
                              │
                  去重扫描（精确主键 + 模糊多字段相似度）
                  模糊重复 ──▶ 强制人工确认，绝不自动合并
                              │
                       置信度三级分流
         ≥0.90 自动通过 ─ 0.70~0.90 建议检查 ─ <0.70 强制复核
                              │
                     人工复核队列 → 导出 / 入库
                    Excel · CSV · 复核流水
                              │
                    检索索引（RAG）→ 统一检索 / 带引用问答
```

## 里程碑与验收状态

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | 项目骨架 · 配置系统 · 统一数据模型 · doctor 体检 | ✅ 完成 |
| M1 | 核心链路：接入（Excel/CSV/PDF/Word）→ 字段映射 → 类型标准化 → 质量检测 → 导出 · CLI · Web UI | ✅ 完成 |
| M2 | OCR 分级链路：RapidOCR 一级 + 本地 VL 二级 + 置信度路由 + 表格重建 | ✅ 完成 |
| M3 | 人工复核工作流：置信度分流 · 复核队列 · 复核流水 · 运行指标刷新 | ✅ 完成 |
| M4 | 批处理与定时任务：收件目录扫描 · 内容指纹去重 · 归档 · watch 轮询 | ✅ 完成 |
| M5 | 质量体系闭环：跨源一致性校验 · 精确/模糊去重 · L3 LLM 字段映射 | ✅ 完成 |
| M6 | 平台化：Pipeline 可配置化（TOML）· Step 级断点续跑 · RBAC 权限 + 审计 · Docling 高级后端 | ✅ 完成 |
| M7 | 知识检索：对已处理数据建索引 · 跨来源统一检索 · 带引用问答（RAG） | ✅ 完成 |
| M8 | 图纸数据接入：DXF 属性块提取 → 图纸清单 → 校验异常 → 复核（口径已设计，待落地） | ⏳ 待落地 |

## 快速开始

前置：Python 3.11+。

```powershell
# 1. 安装（uv 或 pip 二选一）
uv venv --python 3.12; uv pip install -e ".[dev]"
#   pip 方式：python -m venv .venv; .venv\Scripts\pip install -e ".[dev]"

# 需要 OCR 时再加：uv pip install -e ".[dev,ocr]"

# 2. 体检：依赖、Schema、数据库、OCR 链路
.venv\Scripts\docflow.exe doctor
```

### 跑通完整链路

```powershell
# 看一眼有哪些内置 Schema
docflow schemas

# 处理一个目录 / 一批文件：接入 → 映射 → 质检 → 分流 → 落库
docflow run purchase_order .\samples\purchase_orders.csv

# 脏数据样例：会触发类型不匹配、跨字段冲突、主键重复，进人工复核
docflow run purchase_order .\samples\purchase_orders_messy.csv

# 查看历史运行 / 某次运行详情
docflow runs
docflow show <run_id>

# 待复核队列 → 复核
docflow pending
docflow review <record_id> --run-id <run_id> --approve --reviewer 张三 --note "已电话确认"

# 重新导出某次运行（不重跑处理）
docflow export <run_id> --out .\out.xlsx
docflow export <run_id> --out .\out_csv --csv

# Web UI：上传 → 处理 → 结果 → 复核 → 导出
docflow serve            # 浏览器打开 http://127.0.0.1:8000
```

### Pipeline 规范与断点续跑

```powershell
# 查看可用的 Pipeline 规范（步骤序列、是否支持断点续跑）
docflow pipelines

# 指定规范运行：fast 跳过去重与跨源校验，适合快速预览
docflow run purchase_order .\samples\purchase_orders.csv --pipeline fast

# 开启步骤级断点续跑：中断后重跑同一条命令，会从最近完成的步骤继续
docflow run purchase_order .\inbox --checkpoint .\data\state\ckpt
```

### 批处理与定时任务

面对「每天往共享目录里丢一批文件」的常态，`batch` 把收件目录一次性扫完，
`watch` 则持续轮询、只处理新文件：

```powershell
# 一次性批处理：扫描目录 → 去重 → 处理 → 落库，成功后把源文件归档
docflow batch purchase_order .\inbox --pattern "*.csv" --archive .\archive --export .\out

# 持续监听：每 60 秒扫一轮，发现新文件就处理（Ctrl+C 停止）
docflow watch purchase_order .\inbox --interval 60 --archive .\archive

# 单轮执行：适合交给系统计划任务（Windows 计划任务 / crontab）做真正的定时
docflow watch purchase_order .\inbox --once
```

**去重靠内容指纹而非文件名**：账本记录每个文件的 SHA-256（落在 `data/state/processed.json`），
改名、换目录都不会被重复处理；只有**成功解析**的文件才入账并归档，
解析失败的文件留在原地，下一轮自动重试——不需要人工清理失败队列。

> 定时调度刻意不内置 cron 解析：把「什么时候跑」交给操作系统，把「跑什么、跳过什么」交给 `batch`。
> 少一个常驻调度器，部署方式反而更灵活。

> 同一份文件重复处理不会互相覆盖：记录的 `record_id` 是**内容指纹**，
> 与 `run_id` 组成联合主键，历史运行可完整保留。

### 多供应商 BOM 合并清洗（`generic_record` 实战）

`samples/` 下三份字段风格各异的 BOM，对应「每天从不同供应商收 BOM，要合并成一份清单」的真实场景：

| 文件 | 字段名风格 | 故意埋的坑 |
|---|---|---|
| `bom_supplier_a.csv` | 中文（物料编号/物料描述/数量/单位/备注） | 标题前缀行、主键 A001 重复（100 与 50） |
| `bom_supplier_b.csv` | 英文（Item No./Part Name/Spec/Qty/UOM/Material/Unit Price/Lead Time） | B001/B004 同名同规格不同编号（模糊重复） |
| `bom_supplier_c.csv` | 混合（物料编码/名称/数量/单价/交期/备注） | A001 与 a 内容相同（内容指纹去重）、数量为空、数量为负、B002 跨源重复 |

一条命令合并清洗：

```powershell
$env:DOCFLOW_DATA_DIR=".\data_bom_demo"
docflow batch generic_record .\bom_inbox --pattern "bom_*.csv" `
  --archive .\bom_archive --export .\bom_out\bom_merged.xlsx --preview 6
```

期望结果（已验证）：
- 三份合并 13 条记录，字段映射三级策略自动对齐三种字段名风格
- 4 条待复核：跨源一致性告警 ×2（B002 在两家供应商中名称不一致）、数量为空、数量为负
- 去重组抓到 2 组共 3 条冗余（A001 精确重复 3 次、B002 跨源重复 2 次）
- 源文件自动归档，导出含 5 张工作表的合并 Excel

这条命令把「字段映射 → 类型标准化 → 四层质检 → 内容指纹去重 → 跨源校验 → 人工复核 → 归档 → 导出」整条链路一次性走完。

### 扫描件 OCR 演示

`samples/test_scanned_table.png` 是一张表格扫描件（模拟手机拍照 / 扫描仪输出），用于演示 OCR 分级链路：

```powershell
# 确保 .env 里配置了 OCR 开关和 VL 模型
$env:DOCFLOW_OCR_ENABLED="true"
docflow run generic_record .\samples\test_scanned_table.png --preview 5
```

期望结果：5 条记录全部置信度 1.00 自动通过，耗时约 4 秒。一级 RapidOCR 识别置信度高时直接通过；置信度低于 0.85 时自动升级到本地 VL 模型（qwen3-vl:8b）做二次识别。

### 知识检索（RAG）

对某次运行建立索引后，就能跨来源统一检索，并基于检索结果问答：

```powershell
# 为一次运行建立索引（把每条记录摊平成可检索片段，保留来源坐标）
docflow index <run_id>

# 关键词检索（纯 Python BM25：中文按字符二元组，英文/数字按词）
docflow search "螺栓 华信机械"
docflow search "PO-2024-001" --run-id <run_id> --top-k 5

# 基于检索结果问答：配置了文本 LLM 则生成带引用回答，否则给出证据片段
docflow ask "华信机械供了哪些物料？"
```

Web 端也有「知识检索」页（`/search`）。

> 检索是**只读**能力，索引按 run 落盘为 JSON（`data/state/retrieval/<run_id>.json`）。
> 刻意不引入向量库：BM25 确定性、离线可跑、可解释，对「按编号 / 术语找记录」这类
> 企业检索场景足够；问答生成只是锦上添花，模型不可用时自动退化为抽取式证据。

## 核心设计

### 1. 字段映射：三级策略，能用规则就不动模型

不同来源对同一个字段有无数种叫法——「物料编号」「料号」「Item No.」「part_no」。
三级匹配按成本从低到高逐级兜底：

| 级别 | 方法 | 置信度 | 说明 |
|---|---|---|---|
| L1 精确 | 归一化后命中字段名 / 同义词词典 | 1.0 | 覆盖绝大多数常见写法，零成本 |
| L2 模糊 | rapidfuzz 加权相似度 + 包含关系加成 | 0.55 ~ 0.95 | 处理拼写变体、中英混写 |
| L3 推断 | LLM 判断列名 + 样本数据的归属 | 0.5 ~ 0.9 | 前两级都拿不准时才调用，默认关闭（`DOCFLOW_LLM_MAPPING_ENABLED`） |

多列抢同一字段时，只保留置信度最高的那个，其余**显式降级并说明原因**——
静默丢弃会让用户误以为数据都映射上了。

每个映射结论都带 **理由**（`method` + `reason`），前端直接展示给用户确认——
可解释性是数据工具能不能被信任的前提。

### 2. 类型转换：保守优先，不猜

`coerce.py` 里所有转换失败都返回 `None` 并向上抛出「类型不匹配」，并把该字段置信度压到 `0.4`，
强制进入人工复核，而不是猜一个看起来合理的值。
在数据质量工具里，**一个悄悄猜错的数字比一条明确的告警危险得多**。

### 3. 置信度三级分流（Human-in-the-loop）

| 置信度 | 处理方式 |
|---|---|
| ≥ 0.90 且无 ERROR / WARNING | 自动通过，直接入库 |
| 0.70 ~ 0.90 | 进复核队列（提示「建议检查」） |
| < 0.70 | 强制人工复核 |
| 任意值但有 ERROR 级问题 | 强制人工复核 |
| 任意值但有 WARNING 级问题 | 进复核队列（提示「建议检查」，不自动放行） |

> 「有 WARNING 就不自动放行」是关键：格式不符、超出范围这类警告过去不参与分流，
> 高置信度的脏记录会被直接判为自动通过、悄悄入库。复核队列存在的意义就是兜住这类
> 「机器拿不准」的情况，所以警告级问题也会让记录停下来等人看一眼。

阈值全部走配置（`DOCFLOW_AUTO_APPROVE_THRESHOLD` / `DOCFLOW_REVIEW_THRESHOLD`），
不同企业对「多确定才算确定」的要求不一样。复核动作全部落 `review_actions` 流水，
「谁在什么时候改了什么」可追溯。

### 4. OCR 分级链路：先快后准，按需升级

扫描件/图片不是「一股脑丢给大模型」，而是按识别质量分层：

| 层级 | 引擎 | 定位 |
|---|---|---|
| 一级 | RapidOCR（ONNX Runtime） | 纯本地、CPU 可跑、体积小，吃掉大多数正常扫描件 |
| 二级 | 本地 VL 模型（OpenAI 兼容接口） | 一级平均置信度低于阈值时兜底，慢但能读懂版面 |

- 一级结果的平均置信度 ≥ `DOCFLOW_OCR_MIN_CONFIDENCE` → 直接采用，零额外成本；
- 低于阈值且配置了 VL 模型 → 升级到二级重识别，并在报告里注明「为什么慢」；
- 低于阈值但没配 VL → 保留一级结果并留下提示，不阻断流程。

启用方式：

```powershell
# 1. 装一级 OCR 依赖
uv pip install -e ".[ocr]"

# 2. 打开开关并配置本地 VL 模型（示例：Ollama）
$env:DOCFLOW_OCR_ENABLED="true"
$env:DOCFLOW_VL_BASE_URL="http://127.0.0.1:11434"
$env:DOCFLOW_VL_MODEL="qwen2.5vl:7b"
$env:DOCFLOW_VL_API_KEY="ollama"

docflow doctor   # 会分别报告一级 / 二级 OCR 是否就绪
```

扫描 PDF 会按页渲染成图片后走同一条链路，OCR 结果统一还原成二维矩阵，
再交给通用的表头识别逻辑（`ingestion/header.py`）——**OCR 与解析不重复造轮子**。

#### 提示词工程：把「请准确识别」拆成可执行的约束

7B 级 VL 模型的表格转写质量，几乎完全取决于约束给得够不够细。`ocr/vl.py` 的系统提示词
逐条对应一类真实的转写失败模式，而不是泛泛地要求「准确」：

| 约束 | 对应的失败模式 |
|---|---|
| 只输出表格、不要代码块围栏 | 回复裹着 ``` 和寒暄，下游解析器被噪声污染 |
| 第一行必须是表头、行列不得增删 | 合并表头被拆散，表头识别直接失败 |
| 每行列数一致、缺项留空 | 二维矩阵参差不齐，字段映射时串列 |
| 合并单元格重复填写其值 | 关键字段凭空消失 |
| 单元格内换行用空格、竖线转义为 `\|` | 单元格内容撑破 Markdown 表格 |
| 原文照抄、不翻译不改写 | 「顺手纠正」错别字导致与原文不符 |
| 数字保持原样、不重算不补位 | 金额、单价被模型篡改 |
| 非表格时退化为「字段：值」 | 对着非表格图片硬凑一张假表 |

解析侧同样做了防御：按**未转义的竖线**切分单元格、跳过 `\|---\|` 分隔行、把参差不齐的行补齐到同宽。

更进一步，管线会把当前 Schema 的字段名（label + 同义词）作为**软提示**传给 VL 模型，
让模型更可能把表头写成下游能精确匹配的写法，从而提升映射命中率：

```python
# pipeline/runner.py
tables, failures = ingest_paths(paths, self.settings, column_hint=self.schema.column_hint())
```

提示只是参考——图片里实际是什么就写什么，不强制模型照抄。

### 5. 四层质检：前三层看单条/单表，第四层看「来源之间」

| 层级 | 检查内容 | 作用域 | 实现位置 |
|---|---|---|---|
| 字段级 | 必填、类型、正则格式、取值范围 | 单个字段 | `quality/engine.py` |
| 记录级 | 跨字段逻辑（`compare` / `require_if` / `formula` 算术校验） | 单条记录 | `quality/engine.py` |
| 表级 | 主键重复、整行重复、整列全空 | 单张来源表 | `quality/engine.py` |
| **跨源级** | 同一实体在不同来源中的取值是否一致 | 整次运行的全部来源 | `quality/consistency.py`（独立管线步骤） |

> **实现边界（避免误解）**：前三层由同一个 `QualityEngine` 在一次 `quality` 步骤内完成；
> 跨源级由独立的 `CrossSourceChecker` 承担，在管线里是 `quality` 之后的另一个步骤。
> 之所以这样拆，是因为跨源检查需要「同时看到全部来源」，作用域与前三级完全不同；
> 「四层」是**质检体系的概念分层**，不等于单个类里的四个方法。

记录级除了关系比较（`compare`）与条件必填（`require_if`），还支持**算术校验**（`formula`）——
用来表达「总金额 = 数量 × 单价」这类派生字段一致性：

```toml
[[checks]]
type = "formula"
level = "warning"
target = "total_amount"
expression = "quantity * unit_price"   # 支持 + - * / 与括号
tolerance = 0.01                        # 允许 1% 相对误差，容忍小数舍入
message = "总金额与「数量 × 单价」不一致，请核对计价"
```

表达式只允许「字段名 + 数字常量 + 加减乘除」，在 Schema 加载期就用 AST 白名单校验完毕
（`formula.py`），**不使用 `eval`**，因此 Schema 文件即使被替换也无法注入代码。
参与计算的字段缺值或非数值时该规则自动跳过——这类问题交给必填 / 类型校验，不重复报错。

跨源级是前三层覆盖不到的一类问题：同一个物料编号在 A、B 两家供应商的报价单里，
名称/规格/单价却对不上——这类冲突**必须同时看到多个来源**才能发现。
规则声明在 Schema 里，只在「同一实体确实出现在 ≥2 个来源」时才判定，
避免把单文件内的重复问题重复报一遍：

```toml
[[consistency]]
key = "material_id"                                    # 实体标识
fields = ["material_name", "spec", "unit", "supplier"] # 应当一致的字段
level = "warning"
```

### 6. 去重：只报告，不自动合并

多个来源常混着同一份数据的多个副本。去重分两阶段，且**只发现、不删改**：

1. **精确去重**：主键归一化后相同 → 指向同一实体；
2. **模糊去重**：主键缺失/不可靠时，用配置字段的 `token_sort_ratio` 均值兜底，
   超过阈值判定为疑似同一实体。

```toml
[dedupe]
enabled = true
fields = ["material_name", "spec", "supplier"]
threshold = 0.9
```

**模糊去重结果一律强制进人工复核**——合并错误的代价远高于多花几分钟确认，
所以管线只给记录打上 `possible_duplicate` 标记并拉回复核队列，
真正合并交给显式动作。去重组同时出现在 CLI 摘要、运行详情页与导出 Excel 的「去重组」Sheet 里。

> **精确重复的取舍**：主键完全相同的记录会被归入「精确」去重组，并触发表级
> `duplicate_key` 告警，但**不会**被强制拉回复核队列——因为它们是主键级的确凿重复，
> 不涉及「疑似同一实体」的误判风险。平台始终「只报告不合并」，重复行会原样出现在导出中，
> 由使用方依据「去重组」Sheet 与问题清单自行决定去留。若业务要求重复行默认不进正式产物，
> 可在导出后按 `复核状态` / 去重组过滤，或把它接成复核规则（见验收报告的后续优化项）。

### 7. 可追溯

每条记录都保留 `raw_fields`（标准化前的原始值）与 `source_column`（映射来源列），
任何一条输出数据都能倒推回「哪个文件的哪一行、哪一列」；
Excel 导出还会附带「问题清单」「字段映射」两个 Sheet，让「数据为什么少了/为什么被标红」在同一个文件里就能解释清楚。

### 8. Pipeline 可配置化：把「跑哪些步骤」从代码里抽出来

处理链路的步骤顺序不是随意的：`map` 依赖 `ingest`、`dedupe` 依赖 `triage`……
`pipeline/spec.py` 用一个规范（`PipelineSpec`）显式声明**规范顺序**与**硬依赖**，
加载 `pipelines/*.toml` 时校验：步骤名必须是已知步骤、顺序必须与规范一致、依赖必须满足。
任何一步不合法都在加载阶段就报错，而不是跑到一半才炸。

```toml
# pipelines/fast.toml
name = "fast"
label = "快速预览（跳过去重与跨源校验）"
checkpoint = false

[[steps]]
name = "ingest"

[[steps]]
name = "map"
# …按规范顺序声明；给某步 enabled = false 可临时关闭它
```

内置 `default` 规范覆盖全部步骤；`fast` 只跑到 `triage`，适合「先看看数据长什么样」。

### 9. Step 级断点续跑：从最近完成的步骤继续

批处理的去重账本解决的是「同一份文件别重复处理」，但一次运行**跑到一半失败**时，
重跑仍会从头再来。Step 级检查点解决的是后者：

- 每完成一个步骤，就把当时的中间状态（`PipelineState`）落盘一份检查点；
- 检查点用「Schema + 规范 + 步骤序列 + 输入文件指纹」算出的**签名**标识，
  输入变了就不会错误地续跑旧状态；
- 重跑同一命令时自动从「已完成步骤的下一步」继续，并在结果里注明从哪一步恢复；
- 跑完即清理检查点，不留下陈旧状态。

CLI 用 `--checkpoint <目录>` 开启，也可用 `DOCFLOW_CHECKPOINT_ENABLED` 设默认。

### 10. RBAC 与审计：谁能做什么，谁做过什么

交付层的访问控制刻意做得轻：不引入 JWT / OAuth，身份来源可插拔——默认从
`X-DocFlow-User` / `X-DocFlow-Role` 请求头读取（内网反向代理注入的常见形态），
RBAC 关闭时一律视为 admin，单机与本地演示不受影响。

| 角色 | 权限 |
|---|---|
| viewer | 只读：看运行结果与复核队列 |
| reviewer | 只读 + 人工复核 |
| operator | 复核 + 触发处理 + 导出 |
| admin | 全部，含审计查看 |

权限决定动作能不能发生，审计记录动作确实发生过：处理、复核、导出，以及**被拦截的越权尝试**
都会落一条流水（`docflow audit` 或 Web 的「审计日志」页）。两者都只在改变状态的动作上生效，读操作不落流水。

> **部署定位（请按此边界使用）**：Web 层定位为**内网 / 单机工具**，由反向代理做前置认证并注入身份头，
> 因此刻意没有引入 JWT / OAuth、也没有给复核 POST 表单加 CSRF token。它**不适合直接暴露到公网**。
> 若确需公网部署，至少要补：会话/令牌认证、CSRF 防护、HTTPS 终止、速率限制与独立的文件存储隔离。
> 把定位写清楚，比假装它是面向公网的产品更负责。

### 11. Docling 可插拔后端：复杂版面时的更优解析

内置解析走 PyMuPDF / python-docx，轻量且覆盖大多数文档。遇到复杂排版的 PDF/Word 时，
可切到 Docling 高级后端（版面分析 + 阅读顺序还原 + 表格结构识别）：

```powershell
uv pip install -e ".[docling]"     # 依赖较重（含 torch 等）
$env:DOCFLOW_DOCUMENT_BACKEND="docling"
```

后端通过 `document_backend` 配置切换，接入层按配置路由，**默认关闭、不改变现有行为**；
对 Docling 的 API 做防御式调用——不同版本方法签名有差异时宁可退化为「没有表格」，
也不让整份文档解析失败。`docflow doctor` 会报告后端是否就绪。

### 12. 知识检索：先建索引，再统一检索与问答

检索是「只读」能力，建立在已有处理结果之上：把每条记录摊平成一段「字段: 值」文本，
保留来源坐标（文件 / 位置 / 记录 ID），用纯 Python 的 **BM25** 排序。
中文按字符二元组切词、英文数字按词，不引入分词库与向量库——
对企业里「按编号 / 术语找记录」这类检索，确定性、离线可跑、可解释比语义泛化更重要。

- `docflow index <run_id>` 为一次运行建索引，落盘为 JSON；
- `docflow search` 跨多个已建索引的运行统一检索（结构化记录与文档文本最终都摊平成片段，天然统一）；
- `docflow ask` 把检索片段交给文本 LLM 生成**带引用**的回答；未配置模型时退化为抽取式证据，绝不报错。

### DXF 图纸接入（M8 口径，待落地）

> 典型场景：用 AI 把机械图纸里的标题栏 / 明细栏属性块批量提取成清单，并做基础校验、识别数据异常。

**为什么融入 docflow-ai 而不单独建项目**：DXF 解析后只是「新接入源」，字段映射、四层质检、置信度分流、复核、导出、批处理、RAG 检索**全部复用**——单独脚本要重写一整套质检与复核工作流，融入后零额外成本。

**实现路线**（Python + ezdxf，开源 DXF 原生支持）：

1. 新增接入器 `src/docflow/ingestion/dxf.py`，让 `detector.py` 识别 `.dxf` 后缀分发
2. **首选属性块提取**：遍历 INSERT 实体读取其 ATTRIB 的 tag/value——非标设备图纸的标题栏 / 明细栏通常是属性块，每个图块实例 = 一条记录
3. **兜底文字提取**：属性块缺失时按图层名规则（TITLE/NOTE/BOM 类）筛选 TEXT/MTEXT
4. 每条记录带来源坐标（源文件 / 图层 / 块名 / 图元句柄），对接 RAG 带引用检索
5. 内置 Schema `drawing_bom`：图号必填 + 唯一、物料编码格式正则、数量正整数——覆盖「基础校验、识别数据异常」需求

**DWG 处理**：ezdxf 只支持开放的 DXF；DWG 是闭源二进制格式，行业常规用 ODA File Converter（Open Design Alliance 官方免费工具）批量 DWG → DXF。v1 仅在 README 写清此流程；可选增强是接入器检测到 `.dwg` 且系统装有 ODA 时自动调用转换。

**为什么没做**：ezdxf 是新增依赖，图纸场景需要真实样例才能跑稳。优先把已验证的主线打磨到位，架构已留好接口，后续接 ezdxf 即可。

> 对应《Codex For CAD 工程应用评测报告》四路径分类：docflow 的 DXF 接入对应「脚本方式看逻辑」，
> 批处理 + 复核 + Schema 沉淀对应「Skill/流程复用」——个人经验变组织资产。

## 项目结构

```
docflow-ai/
├── src/docflow/
│   ├── models/         # 统一数据模型：Schema 定义 · RawTable · UnifiedRecord · 质量问题
│   ├── ingestion/      # 接入层：类型检测 · 表头识别 · Excel/CSV/PDF/Word 解析 · Docling 后端
│   ├── ocr/            # OCR 分级链路：RapidOCR · 本地 VL 模型 · 表格重建
│   ├── mapping/        # 映射层：同义词词典 · 模糊匹配 · 冲突消解 · L3 LLM 兜底
│   ├── quality/        # 质检层：字段级 / 记录级 / 表级 / 跨源级 + 去重 + 置信度分流
│   ├── pipeline/       # 编排层：规范（spec）· 运行器 · 中间状态 · 步骤级检查点
│   ├── retrieval/      # 检索层：切词 · BM25 索引 · 跨来源统一检索 · 带引用问答
│   ├── security/       # 交付层安全：RBAC 角色-权限矩阵 · 审计事件
│   ├── batch.py        # 批处理：收件目录扫描 · 内容指纹去重 · 归档 · 定时轮询
│   ├── store/          # 存储层：SQLAlchemy + SQLite（含旧库主键迁移）
│   ├── export/         # 导出层：Excel（多 Sheet）/ CSV（多文件）
│   ├── web/            # FastAPI + 服务端渲染模板（无前端构建链，断网可用）
│   ├── cli/            # typer 命令
│   ├── coerce.py       # 类型转换（保守策略）
│   ├── formula.py      # 算术表达式安全求值（AST 白名单，供 formula 规则）
│   ├── text.py         # 归一化与清洗工具
│   └── config.py       # pydantic-settings 配置
├── pipelines/          # Pipeline 规范（TOML）：default / fast
├── schemas/            # Schema 模板（TOML）
├── samples/            # 演示数据（干净 / 脏数据各一份）
└── tests/              # 单元测试
```

## 测试

```powershell
make test          # 等价于 pytest
# 或
.venv\Scripts\python.exe -m pytest -q
```

覆盖范围（140 个用例，全部离线、不依赖真实模型）：

- **端到端链路**：CSV 接入 → 映射 → 质检 → 分流，验证干净数据自动通过、脏数据进复核；
- **字段映射**：精确 / 同义词 / 模糊三级策略 + 多列抢同一字段的冲突消解 + L3 LLM 兜底（可插拔 advisor）；
- **质检规则**：格式告警、取值区间、跨字段冲突、主键重复、整列为空、**算术校验（`formula`：命中 / 容差内通过 / 缺值跳过）**；
- **表达式安全**：`formula` 只允许字段名与加减乘除，幂运算 / 函数调用 / 属性访问等一律在加载期拒绝；
- **跨源一致性**：多来源同实体取值冲突才告警、单来源不误报；
- **去重**：精确主键分组、模糊多字段相似度分组、模糊重复强制进复核；
- **OCR 分级**：置信度路由（一级直用 / 升级二级 / 无 VL 时降级保留）、表格坐标重建、VL 回复解析与转义竖线、列名提示透传；
- **Pipeline 规范**：步骤顺序 / 硬依赖校验、禁用步骤不执行、规范驱动执行；
- **断点续跑**：检查点签名随输入变化、落盘 / 恢复往返、中断后续跑只执行剩余步骤、跑完即清理；
- **RBAC 与审计**：角色-权限矩阵、Web 越权 403 与越权审计、审计流水写入与查询、身份头 HMAC 签名校验、fail-closed（未配密钥且不信任代理时忽略伪造身份头）、开放重定向防护、超大请求体早拒；
- **Docling 后端**：依赖缺失安全报错、表格抽取与清洗、无表格退化为键值、接入层按配置路由；
- **知识检索**：切词、BM25 排序、跨运行检索、索引落盘往返、抽取式 / 生成式问答、Web 检索页；
- **持久化**：**同一文件重复处理不撞主键（回归用例）**、复核流水与运行指标刷新、旧库主键自动迁移、去重结果落库与还原；
- **批处理**：目录收集与递归、内容指纹去重（跨轮次 + 同轮改名副本）、账本持久化与损坏兜底、归档、导出、失败文件留待重试；
- **导出**：Excel 多 Sheet（含去重组）与 CSV 多文件的完整性。

#### 模型依赖路径的测试边界

以下能力依赖外部模型或重型依赖，自动化测试用假引擎覆盖编排逻辑，真实路径已在本机验证：

| 能力 | 自动测试覆盖 | 真实路径验证 |
|---|---|---|
| RapidOCR 一级识别 | 置信度路由、表格重建、列名提示透传 | ✅ 扫描件表格 5 行全部识别，置信度 1.00 |
| VL 二级升级 | 低置信度触发升级、回复解析与转义 | ✅ qwen3-vl:8b 整张表格识别完整，含 45#钢/Q235 等专业代号 |
| L3 LLM 字段映射 | 调用时机与结果合并 | 未验证（默认关闭，需配置 LLM 接口） |
| Docling 后端 | 依赖缺失报错、表格抽取、无表格退化 | 未验证（需安装 docling 依赖） |
| RAG 生成式问答 | 检索、证据拼装、无模型时退化 | 未验证（需配置文本 LLM） |

> RapidOCR 和 VL 模型的真实路径已在本机跑通（`samples/test_scanned_table.png` → 5 条结构化记录）。
> L3 LLM 映射、Docling 后端、RAG 生成式问答三条路径需自行配置接口后验证。

```powershell
make lint          # ruff check
make typecheck     # mypy（strict）
```

## 配置项

全部 `DOCFLOW_` 前缀环境变量或 `.env`，见 [.env.example](.env.example)：

| 变量 | 说明 | 默认 |
|---|---|---|
| `DOCFLOW_DATA_DIR` | 工作目录：上传件、产物、数据库 | `./data` |
| `DOCFLOW_SCHEMAS_DIR` | Schema 模板目录 | `./schemas` |
| `DOCFLOW_PIPELINES_DIR` | Pipeline 规范目录（`*.toml`） | `./pipelines` |
| `DOCFLOW_DATABASE_URL` | 数据库连接串（可切 PostgreSQL） | 空（用 SQLite） |
| `DOCFLOW_CHECKPOINT_ENABLED` | 默认启用步骤级断点续跑 | false |
| `DOCFLOW_FUZZY_THRESHOLD` | 模糊匹配接受阈值 | 0.75 |
| `DOCFLOW_FUZZY_SUGGEST_THRESHOLD` | 模糊匹配建议阈值 | 0.55 |
| `DOCFLOW_AUTO_APPROVE_THRESHOLD` | 自动通过阈值 | 0.90 |
| `DOCFLOW_REVIEW_THRESHOLD` | 强制复核阈值 | 0.70 |
| `DOCFLOW_DOCUMENT_BACKEND` | PDF/Word 解析后端：builtin / docling | builtin |
| `DOCFLOW_DOCLING_DO_OCR` | Docling 是否对扫描件做 OCR | false |
| `DOCFLOW_PDF_TEXT_MIN_CHARS` | 判定为扫描件的文本层字符阈值 | 40 |
| `DOCFLOW_PDF_RENDER_DPI` | 扫描 PDF 渲染 DPI | 200 |
| `DOCFLOW_OCR_ENABLED` | 是否启用 OCR 链路 | false |
| `DOCFLOW_OCR_MIN_CONFIDENCE` | 升级到 VL 模型的置信度阈值 | 0.85 |
| `DOCFLOW_VL_BASE_URL` / `DOCFLOW_VL_MODEL` | 本地 VL 模型地址与模型名 | 空 |
| `DOCFLOW_RBAC_ENABLED` | 是否启用 RBAC 权限校验 | false |
| `DOCFLOW_RBAC_DEFAULT_ROLE` | 未携带角色时的兜底角色 | viewer |
| `DOCFLOW_RBAC_HEADER_SECRET` | 身份头 HMAC-SHA256 签名密钥（未配置且不信任代理时 fail-closed） | 空 |
| `DOCFLOW_RBAC_TRUST_PROXY` | 是否信任反向代理注入的身份头（可信代理模式） | false |
| `DOCFLOW_MAX_UPLOAD_BYTES` | 单个上传文件大小上限（字节） | 26214400（25MB） |
| `DOCFLOW_MAX_REQUEST_BYTES` | 单次上传请求体上限（字节，按 Content-Length 早拒） | 104857600（100MB） |
| `DOCFLOW_LLM_MAPPING_ENABLED` | 是否启用 L3 LLM 字段映射兜底 | false |
| `DOCFLOW_LLM_BASE_URL` / `DOCFLOW_LLM_MODEL` | 文本 LLM 地址与模型名（L3 / RAG 用） | 空 |
| `DOCFLOW_RAG_ENABLED` | 是否启用知识检索 | false |
| `DOCFLOW_RAG_TOP_K` | 检索返回的片段数 | 5 |
| `DOCFLOW_RAG_CHUNK_CHARS` | 单个检索片段最大字符数 | 600 |

## License

个人项目，用于学习交流。
