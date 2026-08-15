# finevision-to-sharegpt 镜像依赖刷新与离线交付设计

## 目标

基于当前源码的真实导入关系重新整理 Python 运行依赖，刷新 Docker 构建配置，生成可在 Linux x86_64 环境通过 `docker load` 离线导入的镜像包。旧 `requirements.txt` 不作为依赖判断依据。

本次同时给出项目接口化方案，但不实现或修改任何接口代码。

## 当前状态

- 项目是 Python 3.11 命令行工具，入口为 `finevision-to-sharegpt`。
- 源码实际使用的第三方运行依赖为 `httpx`、`pyarrow` 和 `tqdm`。
- 当前 Dockerfile 通过 `pyproject.toml` 安装项目，却复制了另一份 requirements，存在依赖来源不一致的风险。
- 本机 Docker Desktop 使用 Linux 容器，支持构建 `linux/amd64` 镜像。
- 数据集、临时文件和输出结果不应进入镜像。

## 方案选择

采用单阶段 `python:3.11-slim` 运行镜像。多阶段 wheel 构建不会显著减少由 `pyarrow` 二进制依赖带来的体积，却会增加维护成本；从当前本机环境直接冻结依赖则容易引入无关包和平台差异。

## 依赖管理

1. 完全根据当前源码导入关系重写 `requirements.txt`，只声明直接运行依赖。
2. 将 `pyproject.toml` 的项目依赖同步为同一组包，防止容器安装和普通包安装产生差异。
3. Docker 构建先安装 `requirements.txt`，再使用 `pip install --no-deps .` 安装本项目，确保镜像只采用已经审核的依赖集合。
4. 依赖版本使用 Python 3.11 可安装并经测试验证的明确版本约束；最终镜像中的精确版本会写入交付说明。

## 镜像结构与运行方式

- 基础镜像：`python:3.11-slim`，目标平台 `linux/amd64`。
- 工作目录：`/app`。
- 包含内容：项目包、默认 prompts、示例及当前 configs、运行所需元数据。
- 排除内容：Git 元数据、测试缓存、临时目录、数据集、生成结果、归档包及本地虚拟环境。
- 容器入口保持 `finevision-to-sharegpt`，默认参数为 `--help`。
- 输入数据、任务配置和输出目录在实际运行时通过 bind mount 或 volume 提供，不烘焙进镜像。
- 镜像不内置模型；翻译任务继续访问配置的 OpenAI 兼容后端。

## 交付物

- 更新后的 `requirements.txt`、`pyproject.toml`、`Dockerfile` 和必要的构建辅助文件。
- Docker 镜像标签：`finevision-to-sharegpt:latest`。
- 离线镜像包：`finevision-to-sharegpt_linux-amd64.tar`。
- 镜像包 SHA-256 校验值及导入、运行示例。

## 验证

1. 运行完整 Python 测试套件。
2. 构建 `linux/amd64` 镜像。
3. 在容器中运行 CLI `--help`。
4. 在容器中导入三个直接运行依赖和项目包。
5. 导出镜像 tar，确认文件非空并计算 SHA-256。
6. 使用导出的 tar 执行一次 `docker load`，确认离线包可被 Docker 识别。

任何验证失败都先定位原因，不把未验证的镜像作为最终交付物。

## 接口化建议（本次不实现）

接口化本身难度中等偏低，主要复杂度不在 HTTP 层，而在长任务、超大文件、断点续跑、进度和失败恢复。推荐采用异步任务模型：

1. `POST /v1/jobs` 提交转换任务并返回任务 ID。
2. `GET /v1/jobs/{id}` 查询排队、运行、成功或失败状态，以及处理统计。
3. `POST /v1/jobs/{id}/cancel` 请求取消任务。
4. `GET /v1/jobs/{id}/artifacts` 获取结果文件清单或下载地址。
5. `GET /healthz` 和 `GET /readyz` 提供存活与就绪检查。

首版可以使用 FastAPI、SQLite 和独立 worker 进程，复用现有 Python 业务函数；单机任务量增大后再替换为 Redis 加 Celery/RQ，并把产物存储迁移到 MinIO/S3。API 进程只负责校验、登记和查询，worker 负责实际转换，避免长任务占住 HTTP 请求。上传文件需设置大小限制并采用流式落盘，后端 API key 通过环境变量或运行时 secret 注入，不写入请求记录或镜像。

## 非目标

- 本次不新增 FastAPI、任务队列或数据库代码。
- 本次不修改转换算法、数据格式或 CLI 行为。
- 本次不把模型权重、数据集或生成结果打进镜像。
- 本次不发布镜像到远端镜像仓库。
