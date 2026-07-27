# finevision-to-sharegpt Design

## Goal

Build `finevision-to-sharegpt`, a container-first Python CLI tool that converts FineVision-style zip/parquet datasets into LLaMA-Factory-compatible ShareGPT multimodal training data.

The tool runs in a remote container environment. It does not provide a frontend and does not download datasets at runtime. It reads zip files mounted into the container, extracts single-image samples, calls an internal Qwen3-VL model API to translate English content into Chinese, writes images to a shared image directory, and writes one JSONL dataset per input zip.

## Runtime Constraints

- Runtime must not access the public internet.
- Runtime may read only mounted input files and write mounted output files.
- Runtime may call the internal OpenAI-compatible Qwen3-VL endpoint.
- FineVision data is provided by the user as local zip files, usually containing parquet files.
- Docker is the primary execution target.
- The project should also be runnable as a Python CLI inside the container for debugging.

Default model configuration:

```bash
JUDGE_MODEL="Qwen3-VL-235B-A22B-Instruct"
JUDGE_API_BASE="http://192.168.48.2:18180/v1/chat/completions"
JUDGE_API_KEY="${JUDGE_API_KEY:-${OPENAI_API_KEY:-sk-local}}"
```

The model endpoint follows OpenAI-compatible chat completions semantics. Images are sent as base64 data URLs in `image_url` content blocks.

## Inputs

The `translate` command supports both explicit zip files and directory scanning:

```bash
finevision-to-sharegpt translate \
  --input /input/okvqa.zip \
  --input /input/chartqa.zip \
  --output-dir /output
```

```bash
finevision-to-sharegpt translate \
  --input-dir /input \
  --glob "*.zip" \
  --output-dir /output
```

Each input zip is treated as an independent source dataset. The tool finds parquet files inside each zip and processes them row by row. It should avoid loading an entire large dataset into memory.

## Outputs

The default output layout is:

```text
/output/
  images/
    <sha256>.jpg
    <sha256>.png
    <sha256>.webp

  datasets/
    okvqa.jsonl
    chartqa.jsonl

  rejected/
    okvqa.rejected.jsonl
    chartqa.rejected.jsonl

  failed/
    okvqa.failed.jsonl
    chartqa.failed.jsonl

  suspicious/
    okvqa.suspicious.jsonl
    chartqa.suspicious.jsonl

  reports/
    okvqa.report.json
    chartqa.report.json
```

One zip produces one dataset JSONL by default. The tool may optionally export a JSON array file, but JSONL is the default because it supports large data and simple resume.

Images from all input datasets are stored in the shared `/output/images/` directory. Image file names are content hashes:

```text
images/<sha256>.<ext>
```

This avoids collisions across zips and allows repeated images to be reused.

## Main Sample Format

Default output is ShareGPT multimodal format:

```json
{
  "id": "okvqa:part-00001.parquet:562",
  "images": [
    "images/9046b731160cc0e6597228adf1b31e99826f5cba6a0b1271ce3f60b65a64ccd6.jpg"
  ],
  "conversations": [
    {
      "from": "human",
      "value": "<image>\n请说明这张图片中的内容。"
    },
    {
      "from": "gpt",
      "value": "这张图片展示了..."
    }
  ]
}
```

Rules:

- Top-level fields are `id`, `images`, and `conversations`.
- `images` is always an array.
- Only single-image samples are accepted in the main output.
- The total `<image>` token count must equal `len(images)`.
- For accepted samples, `len(images) == 1` and total `<image>` token count is `1`.
- The program, not the model, inserts `<image>\n`.
- `<image>\n` is inserted at the start of the first `human` message only.
- Roles are normalized to `human` and `gpt`.
- All original dialogue turns are preserved and translated.
- English source text is not kept in the final main output.

Version 1 writes ShareGPT `conversations` by default. It also includes an explicit `--schema messages` option for users who need OpenAI-style `messages` output.

## Filtering Rules

Rejected samples do not enter the main dataset.

Reject by default:

- No image.
- More than one image.
- No usable text or conversation.
- Image extraction failure.
- Unsupported image payload.
- Invalid role structure that cannot be normalized.

Rejected records are written to `rejected/<source>.rejected.jsonl` with enough metadata to debug:

```json
{
  "source": "okvqa.zip",
  "parquet": "part-00001.parquet",
  "row_index": 562,
  "reason": "multi_image_not_supported",
  "image_count": 2
}
```

## Source Text Parsing

The parser should handle FineVision-style fields conservatively:

- Prefer existing dialogue fields such as `texts`, `messages`, or `conversations`.
- Accept both `content` and `value` text fields.
- Normalize roles such as `user`/`assistant` to `human`/`gpt`.
- Preserve all turns.
- If a sample has only caption-like text, create one ShareGPT pair:
  - `human`: `<image>\n请描述这张图片。`
  - `gpt`: Chinese translation of the caption text.

The caption prompt is configurable:

```bash
--caption-prompt "请描述这张图片。"
```

## Translation Behavior

Translation mode is the primary mode. The model should translate original English content into Chinese while preserving meaning and structure. Version 1 must not invent replacement captions or answers when source text exists.

The default strategy:

1. Send the full single-image sample and all text turns in one model request.
2. Ask the model to return strict JSON containing translated turns only.
3. Validate the returned JSON.
4. Retry on malformed output or API failure.
5. If still failing, fall back to per-turn translation.
6. If fallback also fails, write the sample to `failed/<source>.failed.jsonl`.

The translation prompt should be stored in editable prompt files:

```text
prompts/
  translate_sample_zh.txt
  translate_utterance_zh.txt
```

The default sample prompt should enforce:

- Translate only readable English natural language.
- Keep original meaning.
- Use the image for visual context.
- Preserve options, numbering, tables, lists, code blocks, formulas, units, variable names, field names, file paths, URLs, special tokens, and placeholders.
- Do not output English source text.
- Do not add explanations, disclaimers, comments, or extra fields.
- Keep the same number of dialogue turns.
- Keep role order.
- Return legal JSON.

## Model Request

The Qwen3-VL request uses the OpenAI-compatible chat completions endpoint. Image content is encoded as a base64 data URL:

```json
{
  "type": "image_url",
  "image_url": {
    "url": "data:image/jpeg;base64,..."
  }
}
```

Configuration sources:

- CLI flags should override environment variables.
- Environment variables should provide defaults.
- `JUDGE_MODEL`, `JUDGE_API_BASE`, and `JUDGE_API_KEY` are supported.

The tool should include timeout, retry, and backoff controls:

```bash
--timeout 120
--max-retries 3
--retry-backoff 2.0
```

## Concurrency

The tool supports automatic and fixed concurrency:

```bash
--concurrency auto
--max-concurrency 4
```

```bash
--concurrency 2
```

Auto mode starts conservatively, increases concurrency when requests are stable, and backs off when timeouts, connection errors, 429 responses, or 5xx responses occur. Manual mode fixes concurrency to the provided integer. Auto mode must respect `--max-concurrency`.

## Resume Behavior

No database is used.

Resume is based on existing JSONL output:

- Main output is appended line by line.
- On startup, the tool scans the existing output JSONL and records completed `id` values.
- If an incoming sample ID already exists, it is skipped.
- Images are content-hashed, so existing image files can be reused.
- Failed, rejected, and suspicious files are independent append-only JSONL files.

ID generation must avoid collisions across input zips and parquet files. The default ID strategy is source-row based:

```text
<zip-stem>:<parquet-path>:<row-index>
```

Version 1 supports `--id-strategy source_row`, `--id-strategy field`, and `--id-strategy hash`. The default is `source_row`.

## Quality Checks

Quality checks run after translation and before writing the main output.

Required checks:

- Output JSON is parseable.
- Conversation turn count matches the source.
- Role order matches the normalized source.
- `images` exists and has one item.
- Total `<image>` token count is one.
- Image file exists under the output root.
- `conversations` is non-empty.
- Each message has `from` and `value`.
- `from` is either `human` or `gpt`.
- The output does not contain obvious model refusal or boilerplate such as "as an AI".
- The output does not retain large amounts of English source text.

Suspicious samples are written to `suspicious/<source>.suspicious.jsonl`. By default, suspicious samples still enter the main output. Version 1 supports both keeping and rejecting suspicious samples:

```bash
--quality-action keep
--quality-action reject
```

## Validate Command

The `validate` command checks an existing ShareGPT JSON or JSONL dataset. It includes the behavior from the user's prior validation scripts.

Example:

```bash
finevision-to-sharegpt validate \
  --input /output/datasets/okvqa.jsonl \
  --images-root /output \
  --rejects /output/rejected/okvqa.format_rejected.jsonl
```

Checks:

- Supports JSON array and JSONL input.
- Supports `conversations` and `messages`.
- Supports `value` and `content`.
- Counts `<image>` tokens across all messages.
- Counts `<video>` tokens across all messages.
- Compares token counts to `images`/`image` and `videos`/`video`.
- Rejects empty messages/conversations.
- Optionally verifies local image paths exist.
- Writes valid output and rejected output when requested.

## Mix Command

The `mix` command is a post-processing command. It does not translate, read images, or call the model. It combines already generated JSONL files by ratio or count.

Ratio example:

```bash
finevision-to-sharegpt mix \
  --inputs /output/datasets/okvqa.jsonl:0.3 \
           /output/datasets/chartqa.jsonl:0.7 \
  --total 100000 \
  --output /output/mixed/train_100k.jsonl \
  --seed 42
```

Count example:

```bash
finevision-to-sharegpt mix \
  --inputs /output/datasets/okvqa.jsonl:30000 \
           /output/datasets/chartqa.jsonl:70000 \
  --output /output/mixed/train.jsonl \
  --seed 42
```

Rules:

- Input files are not modified.
- Output remains JSONL.
- Samples are shuffled.
- `--seed` makes sampling reproducible.
- Image paths are left unchanged because all datasets share the output image directory.

## Docker Usage

Example runtime command:

```bash
docker run --rm \
  --network host \
  -e JUDGE_API_BASE="http://192.168.48.2:18180/v1/chat/completions" \
  -e JUDGE_MODEL="Qwen3-VL-235B-A22B-Instruct" \
  -e JUDGE_API_KEY="sk-local" \
  -v /remote/FineVision:/input:ro \
  -v /remote/output:/output \
  finevision-to-sharegpt:latest \
  translate \
  --input-dir /input \
  --glob "*.zip" \
  --output-dir /output \
  --output-format jsonl \
  --concurrency auto \
  --max-concurrency 4
```

`--network host` is recommended when the internal model API must be reached directly from the container.

The README should explain that if the remote build environment is also offline, dependencies must be prepared ahead of time with a wheelhouse or the image must be built on a machine with dependency access and then exported/imported.

## Proposed Project Structure

```text
finevision-to-sharegpt/
  Dockerfile
  README.md
  requirements.txt
  prompts/
    translate_sample_zh.txt
    translate_utterance_zh.txt
  src/finevision_to_sharegpt/
    __init__.py
    cli.py
    archive.py
    parquet_reader.py
    image_store.py
    sample_parser.py
    qwen_client.py
    translator.py
    validator.py
    mixer.py
```

## Out of Scope

- Frontend UI.
- Runtime dataset download.
- Public internet access at runtime.
- Database-backed job tracking.
- Multi-image training sample output.
- Automatic full dataset upload.
- Human review UI.

## Implementation Notes

- The first implementation should include a small local fixture test rather than requiring real FineVision data.
- The code should isolate archive reading, sample parsing, image storage, model calling, translation orchestration, validation, and mixing into separate modules.
- The model client should be easy to replace if the internal API differs slightly from OpenAI-compatible behavior.
- The first version should prioritize stable JSONL output and LLaMA-Factory validation over aggressive throughput.
