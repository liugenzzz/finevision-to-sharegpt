# finevision-to-sharegpt

`finevision-to-sharegpt` 是一个容器优先的 Python 命令行工具，用于把 FineVision 风格的 `zip/parquet` 数据包或已有 ShareGPT JSON/JSONL 转换成 LLaMA-Factory 兼容的 ShareGPT 多模态训练数据。

当前主路径是三个配置化流水线，另有一个直接使用参数的合并命令：

1. `translate-json`：翻译已有英文 ShareGPT JSON/JSONL。
2. `translate-zips`：从注册的 zip/parquet 数据集中抽取样本，并按比例生成中文翻译样本和英文原样样本。
3. `export-zips`：从注册的 zip/parquet 数据集中抽取样本，不调用模型，直接导出英文 ShareGPT。
4. `merge`：按输入顺序合并中间 JSONL，并按 `id` 保留首次出现的记录。

另有一组可选的 MySQL 命令（`db-init` / `db-scan` / `db-export` / `db-status`），用于跨任务增量抽取，见下方“可选：接入 MySQL”。

详细运行说明见 [docs/运行说明.md](docs/运行说明.md)。
受限服务器（根目录不可写、只有 conda）的从零部署见 [docs/服务器部署说明.md](docs/服务器部署说明.md)。

## 主要特性

- 支持单图和多图样本。
- 支持输出 JSONL 和 JSON 数组。
- 支持已有 JSON 翻译断点续跑。
- 支持 zip/parquet 流式读取，避免全量加载数据集。
- 数据集支持两种形式：zip 压缩包，或 FineVision 风格的 parquet 目录（就地读取，不占临时空间）。
- 目录形式支持自动发现，几百个数据集不用逐个登记。
- 支持多线程、多卡、多模型、多 backend 固定并发翻译。
- 两级进度条：外层按数据集显示总进度和累计写入，内层按 parquet 分片显示行进度（跑完自动收起）。
- 支持 backend 请求超时、重试、连续失败后临时禁用。
- 支持数据集注册表，zip 任务只需要写数据集名字。
- zip 抽取图片按数据集分目录保存：`images/<dataset_name>/<hash>.<ext>`。
- `translate-zips` 默认保留每个数据集翻译前的 `raw.jsonl` 中间层。
- 支持稳定合并多个 JSONL，同时生成 JSONL 和 JSON 数组。
- 已有 JSON 翻译不复制图片、不改 `images` 路径。
- 可选接入 MySQL 做消费账本，跨任务增量抽取（用多少抽多少），不连库也能正常运行。

## 输出格式

输出记录使用 ShareGPT 多模态格式：

```json
{
  "id": "okvqa:nested/part.parquet:0",
  "images": [
    "images/okvqa/abc123.jpg",
    "images/okvqa/def456.png"
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

多图规则：

- `images` 数组里有几张图，第一条 human 里就插入几个 `<image>`。
- `<image>` token 连续放在第一条 human 开头。
- 单图样本仍然是一个 `<image>`。

## 构建镜像

```bash
docker build -t finevision-to-sharegpt:latest .
```

导出镜像：

```bash
docker save finevision-to-sharegpt:latest -o finevision-to-sharegpt_latest.tar
```

导入镜像：

```bash
docker load -i finevision-to-sharegpt_latest.tar
```

运行阶段不会下载数据集。翻译功能只需要能访问你配置的内网大模型接口。

## 怎么运行

**两步：改配置 → 跑脚本。** 不用 `cd`、不用 `export`、不用装包，脚本全帮你处理好了。

1. 改 `configs/` 下对应的配置文件（见下方各功能说明）。
2. 跑对应脚本，无参即可：

```bash
bash scripts/in_translate_json.sh   # 翻译已有 JSON   → 读 configs/translate_json.json
bash scripts/in_translate_zips.sh   # 解压+按比例翻译 → 读 configs/translate_zips.json
bash scripts/in_export_zips.sh      # 解压+直接导出   → 读 configs/export_zips.json
```

就这样。想临时换个配置，把路径当第一个参数传进去：`bash scripts/in_translate_zips.sh 别的配置.json`。

<details>
<summary>脚本背后做了什么 / 不想用脚本怎么办</summary>

脚本会自动 `cd` 到项目根目录、设置 `PYTHONPATH=src`、再用 `python -m finevision_to_sharegpt <命令>` 调起，所以你不需要手动配环境。配置里的相对路径都相对项目根解析。

如果你想直接敲命令（容器里包已装好，可直接用；裸 checkout 则先 `pip install -e .` 或自行 `export PYTHONPATH=src`）：

```bash
python -m finevision_to_sharegpt translate-zips --config configs/translate_zips.json
```

</details>

## 公共配置

### backend 配置

`api_base` 必须是完整的 `/v1/chat/completions`，模型清单给的 Base URL 通常只到 `/v1`。

推理型模型（会输出 `<think>` 块）需要额外注意：解析前会自动剥掉 `<think>` 内容，
所以不配也能跑对；但那些推理 token 是白花的，建议用 `extra_body` 直接关掉：

```json
{ "extra_body": { "chat_template_kwargs": { "enable_thinking": false } } }
```

`extra_body` 会原样合并进请求体，也可以用来设 `temperature`、`max_tokens` 等。

填好后先探活：

```bash
python scripts/check_backends.py configs/backend_config.json
```

会分别用纯文本和带图请求探测每个 backend，报告耗时，并区分「连不上」和
「连上了但没在超时内回复」。慢的后端用 `--timeout 600 --only <名字>` 单独重试。

涉及翻译的功能都需要 backend 配置，直接编辑：

```text
configs/backend_config.json
```

填入你的接口地址和 key。示例：

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
- `max_retries=2` 表示单条样本首次失败后最多再重试 2 次。
- `disable_backend_after_failures=20` 表示某个 backend 连续失败 20 次后临时停用。
- 多卡可以注册多个 backend。
- 多模型也可以注册多个 backend。

### 提示词

默认提示词：

```text
prompts/translate_sample_zh.txt
prompts/translate_utterance_zh.txt
```

任务配置里可以覆盖：

```json
{
  "sample_prompt_file": "prompts/translate_sample_zh.txt",
  "utterance_prompt_file": "prompts/translate_utterance_zh.txt"
}
```

## 功能一：翻译已有 JSON

用于翻译已经准备好的英文 ShareGPT JSON/JSONL。

直接编辑：

```text
configs/translate_json.json
```

默认内容（相对路径都相对项目根）：

```json
{
  "input": "data/input/english.jsonl",
  "output_jsonl": "output/chinese.jsonl",
  "output_json": "output/chinese.json",
  "done_path": "output/chinese.done.jsonl",
  "failed_path": "output/chinese.failed.jsonl",
  "images_root": "data/input",
  "backend_config": "configs/backend_config.json",
  "sample_prompt_file": "prompts/translate_sample_zh.txt",
  "utterance_prompt_file": "prompts/translate_utterance_zh.txt",
  "resume": true
}
```

`output_json`、`images_root`、`failed_path`、`rejected_path`、`report_path` 可省略；默认会按 `output_jsonl` 所在目录推导为 `train.json`、`images/`、`failed.jsonl`、`rejected.jsonl`、`report.json`。

运行：

```bash
bash scripts/in_translate_json.sh
```

输入：

- `input`：英文 ShareGPT JSON 或 JSONL。
- `images_root`：图片相对路径根目录。

输出：

```text
output/chinese.jsonl
output/chinese.json
output/chinese.done.jsonl
output/chinese.failed.jsonl
```

注意：

- 这个功能不复制图片。
- 这个功能不改原 JSON 里的 `images` 路径。
- `resume: true` 时会根据 `id` 跳过已完成样本，兼容已跑过的数据。

## 功能二：解压 zip 并按比例翻译

用于从 zip/parquet 数据集中直接抽样并生成训练数据。可以设置每个数据集内部中文和英文比例。

直接编辑：

```text
configs/datasets.json
configs/translate_zips.json
```

数据集注册表（`data_root` 相对项目根）。两种形式，可混用：

```json
{
  "data_root": "data/zips",
  "datasets": {
    "okvqa": { "zip": "okvqa.zip" },
    "chartqa": { "zip": "chartqa.zip" },
    "CoSyn_400k_chart": { "dir": "CoSyn_400k_chart" }
  }
}
```

- `zip`：压缩包，运行时解压到临时目录。
- `dir`：目录，里面是裸 parquet（可嵌套子目录）。**就地读取，不解压，不占临时空间。**

FineVision 那种「一个数据集一个目录、几百个目录」的结构，用自动发现：

```json
{
  "data_root": "/mnt/data/FineVision",
  "auto_discover": true
}
```

`data_root` 下每个含有 parquet 的子目录都会被注册成数据集，目录名即数据集名；`README.md` 这类散文件自动忽略。也可以筛选：

```json
{
  "data_root": "/mnt/data/FineVision",
  "auto_discover": { "include": ["arxivqa", "chartqa"] }
}
```

`include` 和 `exclude` 都按目录名精确匹配。显式写在 `datasets` 里的条目会覆盖同名的自动发现结果。

### 先勘察：哪些集合读得了

一堆下载回来的集合里，不是每个本工具都读得了。先看格式和体积：

```bash
python scripts/survey_roots.py /mnt/data/mm_general
```

按目录统计扩展名和体积，分成「含 parquet/zip」和「需要先转格式」两组。**这只是按文件名分类，不是结论**：zip 里可能装的是散图不是 parquet，parquet 里可能只存了图片路径不存图片字节，两种都会被分到错的一组。所以再打开数据看一眼：

```bash
python scripts/probe_dataset.py /mnt/data/mm_general/OmniScience   # 单个
python scripts/probe_dataset.py /mnt/data/mm_general --all         # 整个根目录
```

它取每个集合的第一个 parquet（或 zip 里的第一个 parquet 成员），用**流水线真正在用的那个行解析器**跑前几行，所以给出的判断就是实际跑起来的判断：

| 判断 | 含义 | 怎么办 |
| --- | --- | --- |
| ✅ 可注册 | 图片字节和对话字段都在 | 直接写进 `datasets.json` |
| ⚠️ 缺图 | 有文本，但图片是路径不是字节 | 先把图片合进 parquet |
| ⚠️ 缺文本 | 有图片，但没有 `conversations`/`texts`/caption | 先补对话字段 |
| ❌ 读不了 | 压根没有 parquet（纯视频、散图、模型权重） | 转格式，或者跳过 |

会一并打印列名、前几行解析出几条、每条几张图，以及一条对话示例。`--rows` 控制看几行（默认 5）。

### 批量注册（把发现结果固化下来）

`auto_discover` 是运行时扫描。想把结果固化成一份可编辑、可审查的注册表，用脚本生成：

```bash
python scripts/register_datasets.py /mnt/data/FineVision --dry-run   # 先看
python scripts/register_datasets.py /mnt/data/FineVision -o configs/datasets.json
```

会逐个打印数据集的 parquet 分片数和体积，然后写出显式的 `datasets.json`。固化的好处是**锁定范围**：之后目录里多出或少掉什么，都不会悄悄改变任务覆盖的数据集。支持 `--include` / `--exclude` / `--min-files` / `--relative`。

跑之前先确认注册表解析成了什么：

```bash
python -m finevision_to_sharegpt list-datasets --config configs/translate_zips.json --limit 10
```

会列出每个数据集的名字、形式、parquet 分片数和体积。**如果 `total` 只有 1 而你预期几百个，说明 `data_root` 指高了一层** —— 父目录被当成了一个巨型数据集。

### 按类别配额抽样

169 个数据集大小差 8600 倍（objects365_qa 166 万，funsd 194 条），统一的 `limit_per_dataset` 会让两者出一样多，配比失真。用类别配额来分：

```bash
# 不接 MySQL，直接从行数清单规划
python scripts/plan_sampling.py --counts configs/fv_counts.txt \
    --config configs/translate_zips.json \
    --plan configs/sampling_plan_5m.json --chinese-ratio 0.7 \
    -o configs/translate_5m.json

# 或者已经灌过库，行数从账本取
python scripts/plan_sampling.py --config configs/db_scan.json --dump
python scripts/plan_sampling.py --config configs/db_scan.json \
    --plan configs/sampling_plan_5m.json -o configs/translate_5m.json
```

`--counts` 读 `<行数> <数据集名>` 的文本（就是 `--dump` 的输出格式，带逗号和括号都行），所以**规划不必等 db-scan 跑完**——几百万行灌完库才发现某个类别配额够不着，是很贵的等待。

计划文件里每个类别有两个数：`share` 是该类占 `total` 的比例，`max_share_per_dataset` 是单个数据集在本类配额里的上限。上限是必需的——不设的话 densefusion_1m 一家 105 万会吃掉 caption 类将近一半，同样的配额摊到十几个来源上混合质量更好。

`--chinese-ratio 0.7` 表示 70% 翻成中文、30% 保留英文。挑中哪些是按 `seed + 样本 id` 哈希定的，同样的 seed 重跑挑中的是同一批。

没有匹配到任何类别的数据集会被列出来并排除，不会悄悄混进去。

### 先跑通链路再谈量

百万条的规划建立在「管道跑得通、译文能看」之上，而这两件事只有真后端能回答。`configs/translate_smoke.json` 就是干这个的：每个数据集取 2 条，覆盖尽量多的 schema 变体，不接 MySQL，`resume` 关掉（重跑就是真重跑）。

```bash
python scripts/check_backends.py configs/backend_config.json      # 先探活，别拿几小时试错
python -m finevision_to_sharegpt translate-zips --config configs/translate_smoke.json
head -3 output/smoke.jsonl
```

`check_backends.py` 查三件事：`api_base` 是不是完整的 `/v1/chat/completions`、key 认不认、模型收不收图片——翻译永远带图发，纯文本模型会条条失败。冒烟跑完看 `output/rejected.jsonl`（哪些行解析不了）和 `emit_raw` 留下的原文，确认 `<think>` 剥离和 JSON 解析在真实模型上没问题，再定总量和并发。

任务配置：

```json
{
  "dataset_registry": "configs/datasets.json",
  "datasets": ["okvqa", "chartqa"],
  "output_jsonl": "output/train.jsonl",
  "chinese_ratio": 1.0,
  "seed": 42,
  "limit_per_dataset": null,
  "backend_config": "configs/backend_config.json",
  "sample_prompt_file": "prompts/translate_sample_zh.txt",
  "utterance_prompt_file": "prompts/translate_utterance_zh.txt",
  "emit_raw": true,
  "resume": true
}
```

`output_json`、`images_root`、`rejected_path`、`report_path` 同样可省略，默认从 `output_jsonl` 推导。

运行：

```bash
bash scripts/in_translate_zips.sh
```

输出分为原始层、翻译层和汇总层：

```text
output/train.jsonl                    # 汇总层：所有数据集的翻译层
output/train.json
output/<dataset_name>/raw.jsonl       # 原始层：翻译前英文记录
output/<dataset_name>/train.jsonl     # 翻译层：中文与保留英文的混合
output/<dataset_name>/train.json
output/images/<dataset_name>/<hash>.<ext>
output/failed.jsonl
output/rejected.jsonl
output/report.json
```

比例说明：

- `chinese_ratio: 1.0` 表示全部翻成中文（默认配置）；设为 `0.7` 则每个数据集内部约 70% 中文、30% 英文。
- 英文样本不调用模型，直接输出原始英文 ShareGPT。
- 中文样本调用模型翻译。
- 使用稳定 hash 分流，同一个 `id` 重跑时会落到同一种语言。
- `emit_raw: true` 默认开启原始层；设为 `false` 时不写 `raw.jsonl`。
- raw 与 train 复用同一批图片路径，图片只落盘一次；raw 只生成 JSONL，不生成 `raw.json`。
- 如果旧任务已完成一部分后才开启 raw，`resume: true` 不会回填已跳过的英文原文；需要完整 raw 时请用 `resume: false` 从头运行。

可以对单个数据集覆盖比例和数量：

```json
{
  "datasets": [
    "okvqa",
    {
      "name": "captcha",
      "chinese_ratio": 1.0,
      "limit": 5000
    }
  ]
}
```

## 功能三：解压 zip 不翻译

用于从 zip/parquet 数据集中抽取图片和英文文本，直接生成英文 ShareGPT，不调用模型。

直接编辑：

```text
configs/export_zips.json
```

任务配置：

```json
{
  "dataset_registry": "configs/datasets.json",
  "datasets": ["*"],
  "output_jsonl": "output/train_en.jsonl",
  "limit_per_dataset": null,
  "resume": true
}
```

运行：

```bash
bash scripts/in_export_zips.sh
```

输出（每个数据集单独一份 + 一份汇总）：

```text
output/train_en.jsonl                 # 汇总
output/train_en.json
output/<dataset_name>/train_en.jsonl  # 每个数据集
output/<dataset_name>/train_en.json
output/images/<dataset_name>/<hash>.<ext>
output/rejected.jsonl
output/report.json
```

这个功能不需要 `backend_config.json`。

## 可选：接入 MySQL

**不配置就完全用不到。** 没有 `mysql` 段时，上面三条流水线的行为和以前一模一样，也不需要装 `PyMySQL`。

### 解决什么问题

文件模式的 `resume` 只认**本次** `output_jsonl` 里的 `id`。换一个输出目录跑第二批，就会从头重新抽到同一批样本。接上 MySQL 后：

- **跨任务增量**：换输出目录、换机器都不会重复抽到已经用过的样本。
- **跳读**：记录每个 parquet 扫到哪一行，重跑时按 row group 整组跳过，不再空转前面几百万行。
- **换版隔离**：zip 换一版（内容变了）自动算作新版本，历史消费记录不会被误判。
- **源数据与译文分表**：源数据进 `sample_source`，译文进 `sample_translation`，靠 `source_id` 关联；每个数据集另有一个 `v_sample_source_<数据集名>` 视图，查起来跟小表一样。

### 配置

编辑 `configs/mysql.json`（可参考 `configs/mysql.example.json`），在任务配置里加一个 `mysql` 段：

```json
{
  "mysql": {
    "host": "10.0.0.5",
    "port": 3306,
    "user": "fv",
    "password": "${FV_MYSQL_PASSWORD}",
    "database": "finevision",
    "batch_size": 200,
    "claim_ttl_seconds": 3600,
    "on_connect_error": "fallback"
  }
}
```

说明：

- `password` 支持 `${环境变量}` 展开，不用把密码写进 git。
- `on_connect_error: "fallback"` 表示连不上就打个告警、降级回文件模式继续跑；设成 `"fail"` 则直接报错退出，避免“以为在记账其实没记”。
- `batch_size` 是攒批写入的条数，默认 200 条或 5 秒先到先 flush。
- `claim_ttl_seconds` 是一条样本被抽走后的认领有效期；进程崩了以后，超过这个时间的认领会在下次运行时自动回收重抽。
- 目标是 MySQL 8.0。若服务端没有 `utf8mb4_0900_ai_ci`，用 `collation` 字段改掉。

把这个 `mysql` 段加进 `configs/translate_zips.json` 或 `configs/export_zips.json`，原来的命令就自动带上账本了，用法不变。

### 建表

```bash
bash scripts/in_db_init.sh
```

幂等，重复跑没有副作用。会建四张表和每个已注册数据集的视图。

### 用多少抽多少

`limit_per_dataset` 的语义是**本次新增 N**，不是累计上限 N。所以配好 `limit_per_dataset: 5000` 之后：

```bash
bash scripts/in_translate_zips.sh   # 第一次：抽 5000 条
bash scripts/in_translate_zips.sh   # 第二次：再抽 5000 条全新的
```

两次的输出目录可以不同，也不会重复。

### 其他命令

```bash
bash scripts/in_db_scan.sh      # 只把源数据灌进库（status=pending），不调模型
bash scripts/in_db_status.sh    # 看各数据集各状态的计数
bash scripts/in_db_export.sh    # 从库里导出 ShareGPT JSONL
```

`db-export` 可以按条件筛选，多余的参数直接透传：

```bash
bash scripts/in_db_export.sh configs/mysql.json --dataset okvqa --lang zh
```

同一条源数据允许有多条译文（换模型或换提示词重翻会各存一条，带上 `model_name` 和 `prompt_version`），导出时取最新的一条。

### 表结构

| 表 | 作用 |
| --- | --- |
| `dataset_version` | zip 指纹（大小 + 头尾采样 hash），一个 zip 版本一行 |
| `sample_source` | 源数据大表：英文原文、图片路径、消费状态 |
| `sample_translation` | 译文表，记 `backend_name` / `model_name` / `prompt_version` / `latency_ms` |
| `dataset_cursor` | 每个 parquet 扫到哪一行的水位线 |

图片**不进库**，仍然按 `images/<数据集名>/<hash>.<ext>` 落盘，表里只存相对路径。

### 本地起一个测试库

```bash
docker compose up -d mysql
```

跑集成测试（不设这个环境变量就自动跳过）：

```bash
FV_TEST_MYSQL='{"host":"127.0.0.1","port":3306,"user":"fv","password":"fv","database":"finevision"}' \
  pytest tests/test_db_integration.py
```

## 合并中间 JSONL

`merge` 按参数顺序拼接一个或多个 JSONL；相同 `id` 只保留首次出现的记录，无 `id` 的记录全部保留。它不抽样、不排序、不打乱，也不修改记录内容。

```bash
python -m finevision_to_sharegpt merge \
  --inputs output/okvqa/train.jsonl output/chartqa/train.jsonl \
  --output output/merged.jsonl
```

命令会覆盖生成：

```text
output/merged.jsonl
output/merged.json
```

终端会打印 `read`、`written`、`duplicates` 和输出路径统计。

## 校验输出

校验 ShareGPT JSON/JSONL：

```bash
python -m finevision_to_sharegpt validate \
  --input output/train.jsonl \
  --output output/train.clean.jsonl \
  --rejects output/train.format_rejected.jsonl
```

检查行数：

```bash
wc -l output/train.jsonl
wc -l output/failed.jsonl
cat output/report.json
```

## 本地开发测试

```bash
pytest -q
```

当前测试覆盖：

- 多图解析和输出。
- Qwen 多图 payload。
- JSON/JSONL 流式读写和汇总。
- raw 中间层的开关、truncate 和失败重试去重。
- merge 稳定去重、无 id 保留与 CLI 统计。
- backend 固定并发、重试和失败禁用。
- translate-json 配置模式和旧参数模式。
- zip 导出和 zip 按比例翻译。
- parquet 按 row group 跳读与断点续扫。
- 目录形式数据集的注册、自动发现、就地读取与目录指纹。
- MySQL 配置解析、zip 指纹、批量写入与降级回文件模式。
- 流水线对账本的调用（认领、完成、失败、译文归属）。
- 连真实数据库的端到端用例（未配置 `FV_TEST_MYSQL` 时自动跳过）。
