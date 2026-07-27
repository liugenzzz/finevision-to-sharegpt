# 多图多后端翻译与 zip 抽取重构设计

## 目标

把当前项目从“单图、单模型、部分命令分散”的实现，重构成三个清晰功能：

1. 翻译已有 ShareGPT JSON/JSONL。
2. 从注册的数据集 zip/parquet 中抽取样本，并按比例生成中文翻译样本和英文原样样本。
3. 从注册的数据集 zip/parquet 中抽取样本，不翻译，直接生成英文 ShareGPT。

三个功能都要支持流式低内存处理、断点续跑、JSONL 和 JSON 双输出。涉及翻译的功能要支持多图、多线程、多卡、多模型、多 backend 固定并发调度。

## 非目标

- 不改已有 JSON 翻译链路中的图片路径。
- 不为 `translate-json` 复制、重命名或重新归档图片。
- 不继续把旧的 `sample` recipe 作为主路径扩展。
- 不使用“每个模型固定一个大文件桶”的静态分桶方式。
- 不在内存中加载完整数据集或完整输出。

## 最终功能

### 1. 翻译已有 JSON

命令建议保留为：

```bash
finevision-to-sharegpt translate-json --config /config/translate_json.json
```

功能：

- 输入已有 ShareGPT JSON 或 JSONL。
- 默认全量翻译。
- 支持单图和多图。
- 从原始记录的 `images` 字段读取图片路径，结合 `images_root` 读取图片字节。
- 输出时保留原始 `images` 路径，不改图片目录。
- 输出 JSONL 和 JSON。
- 支持断点续跑，兼容已经跑过的 10 万条数据。

输出：

```text
chinese.jsonl
chinese.json
chinese.done.jsonl
chinese.failed.jsonl
```

断点规则：

- 优先读取 `done_path` 中的完成记录。
- 如果没有 done 文件，则兼容扫描已有 `output_jsonl` 或 `output_json`。
- 已完成判断只看 `id`。
- 已完成记录不重新翻译。
- failed 不算完成，默认重跑时可以再次尝试。

### 2. 解压 zip 并按比例翻译

命令建议：

```bash
finevision-to-sharegpt translate-zips --config /config/translate_zips.json
```

功能：

- 从数据集注册表读取 zip。
- 解压 zip 中的 parquet。
- 流式读取 parquet row。
- 解析单图或多图样本。
- 图片写入统一图片目录，按数据集分子目录：

```text
images/<dataset_name>/<sha256>.<ext>
```

- 按每个数据集内部的比例决定中文和英文。
- 中文样本调用模型翻译。
- 英文样本不调用模型，直接输出原始 ShareGPT。
- 输出 JSONL 和 JSON。
- 输出失败和拒绝记录。
- 支持断点续跑。

比例规则：

- 默认每个数据集单独执行比例，例如 `chinese_ratio = 0.7` 表示每个数据集都尽量保持 70% 中文、30% 英文。
- 使用稳定 hash 分流：

```text
hash(seed + sample_id) < chinese_ratio => 中文
否则 => 英文
```

- 稳定 hash 的好处是断点重跑时同一个样本永远落到同一种语言。
- 比例是统计近似比例，不需要为了绝对精确比例做两遍全量扫描。
- 如后续确实需要精确条数比例，可以新增可选 `ratio_mode: exact`，但本次不作为默认。

输出：

```text
train.jsonl
train.json
images/
failed.jsonl
rejected.jsonl
report.json
```

### 3. 解压 zip 不翻译

命令建议：

```bash
finevision-to-sharegpt export-zips --config /config/export_zips.json
```

功能：

- 复用 zip/parquet 抽取链路。
- 支持单图和多图。
- 图片写入：

```text
images/<dataset_name>/<sha256>.<ext>
```

- 不调用模型。
- 直接输出英文 ShareGPT。
- 输出 JSONL 和 JSON。
- 输出拒绝记录。
- 支持断点续跑。

输出：

```text
train_en.jsonl
train_en.json
images/
rejected.jsonl
report.json
```

## 多图格式

统一使用用户确认的格式 A：

- `images` 字段保存多张图片路径。
- 第一条 human 消息开头插入与图片数量相同的连续 `<image>` token。

示例：

```json
{
  "id": "okvqa:nested/part-000.parquet:12",
  "images": [
    "images/okvqa/a.jpg",
    "images/okvqa/b.jpg"
  ],
  "conversations": [
    {
      "from": "human",
      "value": "<image>\n<image>\n问题"
    },
    {
      "from": "gpt",
      "value": "答案"
    }
  ]
}
```

旧单图记录仍合法。

## 多 backend 加速设计

新增 `TranslationBackendPool`。

它负责：

- 读取 backend 配置。
- 为每个 backend 创建独立 client。
- 按固定并发启动 worker。
- 从共享任务队列动态领取任务。
- 快 backend 自动处理更多任务，慢 backend 自动处理更少任务。
- 单条请求失败后按配置重试。
- backend 连续失败过多后临时禁用。
- 不使用失败自动降并发。

解决的问题：

- 多线程：每个 backend 内部固定并发。
- 多卡：每张卡或每组卡暴露为一个 backend。
- 多模型：不同模型也注册为不同 backend。
- 快模型不等待慢模型：共享队列动态领取。
- 慢模型卡住：单请求最多等待 `request_timeout`，不会卡住全局任务。

默认失败参数：

```json
{
  "request_timeout": 120,
  "max_retries": 2,
  "disable_backend_after_failures": 20
}
```

含义：

- `max_retries=2`：单条样本首次失败后最多再重试 2 次，总共最多尝试 3 次。
- `disable_backend_after_failures=20`：某个 backend 连续失败 20 次后临时停用，剩余任务由其他 backend 继续处理。

## 配置文件

### 数据集注册表

文件：

```text
configs/datasets.example.json
```

示例：

```json
{
  "data_root": "/data/zips",
  "datasets": {
    "okvqa": {
      "zip": "okvqa.zip"
    },
    "chartqa": {
      "zip": "chartqa.zip"
    },
    "captcha": {
      "zip": "captcha/captcha.zip"
    }
  }
}
```

规则：

- `data_root` 是相对 zip 路径的根目录。
- 任务配置只需要写数据集名字。
- 支持 `"datasets": ["*"]` 跑全部注册数据集。
- dataset name 同时用于图片目录名。

### backend 配置

文件：

```text
configs/backend_config.example.json
```

示例：

```json
{
  "request_timeout": 120,
  "max_retries": 2,
  "disable_backend_after_failures": 20,
  "backends": [
    {
      "name": "gpu0",
      "api_base": "http://10.0.0.1:18180/v1/chat/completions",
      "model": "Qwen3-VL",
      "api_key": "sk-local",
      "concurrency": 16,
      "weight": 1
    },
    {
      "name": "gpu1",
      "api_base": "http://10.0.0.2:18180/v1/chat/completions",
      "model": "Qwen3-VL",
      "api_key": "sk-local",
      "concurrency": 24,
      "weight": 1
    }
  ]
}
```

说明：

- `concurrency` 是该 backend 的固定并发。
- `weight` 预留用于后续加权调度；当前动态队列下，固定并发已经能体现大部分吞吐差异。
- 不传 backend 配置时，CLI 继续兼容当前单模型参数。

### 翻译已有 JSON 配置

文件：

```text
configs/translate_json.example.json
```

示例：

```json
{
  "input": "/data/input/english.jsonl",
  "output_jsonl": "/data/output/chinese.jsonl",
  "output_json": "/data/output/chinese.json",
  "done_path": "/data/output/chinese.done.jsonl",
  "failed_path": "/data/output/chinese.failed.jsonl",
  "images_root": "/data/input",
  "backend_config": "/config/backend_config.json",
  "sample_prompt_file": "/prompts/translate_sample_zh.txt",
  "utterance_prompt_file": "/prompts/translate_utterance_zh.txt",
  "resume": true
}
```

### zip 翻译配置

文件：

```text
configs/translate_zips.example.json
```

示例：

```json
{
  "dataset_registry": "/config/datasets.json",
  "datasets": ["okvqa", "chartqa"],
  "output_jsonl": "/output/train.jsonl",
  "output_json": "/output/train.json",
  "images_root": "/output/images",
  "failed_path": "/output/failed.jsonl",
  "rejected_path": "/output/rejected.jsonl",
  "report_path": "/output/report.json",
  "language": {
    "mode": "mixed",
    "chinese_ratio": 0.7,
    "seed": 42
  },
  "limits": {
    "limit_per_dataset": null
  },
  "backend_config": "/config/backend_config.json",
  "sample_prompt_file": "/prompts/translate_sample_zh.txt",
  "utterance_prompt_file": "/prompts/translate_utterance_zh.txt",
  "resume": true
}
```

可选每个数据集覆盖比例：

```json
{
  "dataset_registry": "/config/datasets.json",
  "datasets": [
    "okvqa",
    {
      "name": "captcha",
      "chinese_ratio": 1.0,
      "limit": 5000
    }
  ],
  "language": {
    "mode": "mixed",
    "chinese_ratio": 0.7,
    "seed": 42
  }
}
```

### zip 英文导出配置

文件：

```text
configs/export_zips.example.json
```

示例：

```json
{
  "dataset_registry": "/config/datasets.json",
  "datasets": ["*"],
  "output_jsonl": "/output/train_en.jsonl",
  "output_json": "/output/train_en.json",
  "images_root": "/output/images",
  "rejected_path": "/output/rejected.jsonl",
  "report_path": "/output/report.json",
  "limits": {
    "limit_per_dataset": null
  },
  "resume": true
}
```

## 容器内脚本

保留三个容器内脚本，不再以外部 `docker run` 包装脚本作为主路径：

```text
scripts/in_translate_json.sh
scripts/in_translate_zips.sh
scripts/in_export_zips.sh
```

用法：

```bash
scripts/in_translate_json.sh /config/translate_json.json
scripts/in_translate_zips.sh /config/translate_zips.json
scripts/in_export_zips.sh /config/export_zips.json
```

脚本只做：

- 接收配置文件路径。
- 调用对应 CLI。
- 不硬编码外部挂载路径。
- 不负责 `docker run`。

## 提示词

提示词继续独立放在：

```text
prompts/translate_sample_zh.txt
prompts/translate_utterance_zh.txt
```

命令和配置都支持覆盖：

```json
{
  "sample_prompt_file": "/prompts/custom_sample.txt",
  "utterance_prompt_file": "/prompts/custom_utterance.txt"
}
```

## 流式低内存设计

必须保持全链路流式：

- zip 只解 parquet，不加载完整 zip 数据集。
- parquet 使用 batch 流式读取。
- 任务队列只保留小窗口，例如 `total_concurrency * 2` 或 `total_concurrency * 4`。
- 翻译完成立即 append 到 JSONL。
- failed/rejected/report 增量写入或低内存统计。
- 最终 JSON 从 JSONL 二次流式生成，不把 JSONL 全量读入内存。

JSONL 转 JSON 的方式：

```text
写入 [
逐行读取 JSONL
按需写逗号和记录
写入 ]
```

## 断点续跑

### 已有 JSON 翻译

用户已经跑过 10 万条，属于 `translate-json` 链路。

兼容策略：

- 保留原记录 `id`。
- 保留原图片路径。
- 优先扫描 `done_path`。
- 兼容扫描已有 `output_jsonl` 和 `output_json`。
- 已有 id 直接跳过。
- 不把旧数据重新写图片。

### zip 翻译和 zip 导出

保持旧 zip 样本 id 规则：

```text
{zip_stem}:{parquet_name}:{row_index}
```

续跑策略：

- 扫描目标 `output_jsonl` 中已有 id。
- 已完成 id 跳过。
- failed 默认不算完成，重跑时再次尝试。
- rejected 可按 id/reason 保留记录；后续多图支持后，旧的多图 rejected 如果重新跑会被正常处理。

## 输出顺序

翻译任务使用并发后，完成顺序可能与输入顺序不同。

默认接受并发完成顺序，原因：

- 可立即写 JSONL。
- 内存占用低。
- 不需要等待慢请求保持原序。

如果后续必须保持输入顺序，需要增加排序缓冲或外部排序，内存和磁盘复杂度都会提高。本次默认不保证输出顺序，只保证 id 稳定和不重复。

## 重构计划范围

现有代码中 `cli.py` 过重，建议拆分。

新增模块：

```text
backend_pool.py
config_loader.py
dataset_registry.py
json_io.py
translation_job.py
zip_pipeline.py
```

调整模块：

```text
models.py
qwen_client.py
translator.py
sample_parser.py
image_store.py
validator.py
cli.py
```

模块职责：

- `backend_pool.py`：多 backend 固定并发、共享队列、重试、禁用坏 backend。
- `config_loader.py`：读取三个任务配置和 backend 配置。
- `dataset_registry.py`：读取数据集注册表，解析 dataset name 到 zip 路径。
- `json_io.py`：JSON/JSONL 流式读写、append、done id 扫描、JSONL 汇总 JSON。
- `translation_job.py`：通用翻译任务执行器，供 `translate-json` 和 `translate-zips` 复用。
- `zip_pipeline.py`：zip/parquet 到 SourceSample、图片保存、比例分流、report 统计。
- `qwen_client.py`：只负责 OpenAI-compatible 请求，支持多图。
- `translator.py`：单样本翻译、解析模型输出、fallback、构造 ShareGPT record。
- `sample_parser.py`：row 到 SourceSample，支持多图。
- `image_store.py`：hash 保存图片，支持 dataset 子目录。
- `validator.py`：校验多图时 `<image>` token 数量等于 `images` 数量。
- `cli.py`：只做命令解析和调用对应服务。

## 向后兼容

- 旧单模型 CLI 参数继续可用。
- 不传 `--config` 时可保留当前参数模式，减少破坏。
- 旧 `translate-json` 已完成数据可续跑。
- 旧单图 JSON 输出仍合法。
- 旧图片路径不移动。
- 旧 `images/<hash>.jpg` 不迁移，新 zip 抽取才写 `images/<dataset_name>/<hash>.<ext>`。

## 测试要求

至少补充这些测试：

- `translate-json` 支持多图输入，并保留原图片路径。
- `qwen_client` 多图 payload 包含多条 `image_url`。
- `translator` 按图片数量插入多个 `<image>` token。
- `sample_parser` 不再拒绝多图。
- `image_store` 支持 dataset 子目录。
- `zip_pipeline` 按稳定 hash 分配中文/英文。
- `translate-zips` 每个数据集都应用相同比例。
- `export-zips` 不调用模型。
- `backend_pool` 固定并发、重试、连续失败禁用 backend。
- JSONL 到 JSON 汇总不全量加载。
- `translate-json` 从 done/output 扫描 id 后跳过已完成记录。
- validator 校验多图 token 数量。

## 风险和处理

- 多模型质量不一致：建议默认注册同类模型；混用不同模型由配置决定。
- 某个 backend 挂掉：连续失败后禁用，不拖垮全局。
- 输出顺序变化：默认接受并发完成顺序，用 id 保证可追踪。
- 比例不是绝对精确：采用稳定 hash 保证重跑一致和低内存，统计上接近目标比例。
- 旧数据图片路径混合：这是预期，旧数据不迁移，新 zip 输出按数据集目录写图。

## 默认决策

- 三个主 CLI 名称固定为 `translate-json`、`translate-zips`、`export-zips`。
- 旧 `sample` 命令保留为兼容入口，但不再作为主文档入口继续扩展。
- `weight` 字段第一版保留在配置中，但主要吞吐控制使用每个 backend 的固定 `concurrency`；后续需要更复杂调度时再启用权重逻辑。
- 输出顺序默认按并发完成顺序，不强制保持输入顺序；稳定性由 `id`、resume 扫描和去重保证。
