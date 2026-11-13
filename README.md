# SmartReview 法律合同智能审查系统

面向企业法务的合同智能审查：上传合同 → 条款拆解 → 语义检索法条 → 双通道（确定性风控规则 + LLM）逐条审查 → Word 审查报告。

配套文档：[面试演示手册](面试演示手册.md) · [功能验收测试用例](SmartReview_功能验收测试用例.docx)

## 核心能力

- **双通道审查**：确定性风控规则兜底 + LLM 语义判断，单条失败不影响整单
- **法条可追溯**：BGE 检索 + reranker 重排，法条只来自知识库白名单，检索不到就留空
- **并发可视化**：逐条审查、完成即落库，前端实时看到进度与通道耗时
- **处置闭环**：采纳/修改/接受风险 → 回写原文 → 生成修订合同 → 自动复审 → 版本链
- **扫描件 OCR**：无文字层 PDF 自动渲染走 RapidOCR，单文件最多 20 页
- **安全与治理**：Prompt 注入检测与隔离、审计留痕（只追加）、版本管理、操作人标识
- **成本与质量**：token/费用逐条落库；标注集 + CI 卡口（召回/精确/注入检出）
- **模型可运营**：多 provider 降级链、失败冷却、灰度发布 A/B 指标

## 快速开始（本机开发）

```bash
# 0) 配置密钥（复制并填写）
cp backend/.env.example backend/.env
#   → SiliconFlow 云端：SMARTREVIEW_SF_API_KEY、SMARTREVIEW_SF_EMBEDDING_API_KEY
#   → 本地 vLLM（可选）：SMARTREVIEW_VLLM_BASE_URL / 模型名

# 1) Windows
scripts\start.bat
#    Linux/macOS
bash scripts/start.sh

# 2) （可选）重建法律知识库索引：python scripts/seed_kb.py
```

访问 `http://127.0.0.1:5174`（前端）/ `http://127.0.0.1:8002/docs`（后端 OpenAPI）。

后端未配置 LLM 密钥时自动降级为**纯规则通道**（仅命中风控规则的风险被标出，其余条款提示「人工复核」）。

## Docker 部署

```bash
cd deploy
cp ../backend/.env.example ../backend/.env   # 配置密钥
docker compose up -d --build                 # 前端 http://localhost:8080
docker compose --profile gpu up -d --build   # GPU 服务器：追加本地 vLLM
```

- `deploy/nginx.conf`：静态托管前端 dist + 按后端路由前缀反代。
- 后端数据（上传/报告/知识库/DB）绑定挂载 `backend/data`，与本地开发共用。
- 后端首启自动执行幂等知识库索引（md5 稳定 ID，无语料或嵌入 Key 时自动跳过）。

## 项目结构

```
smartreview/
├── backend/                  # FastAPI（8002）
│   ├── main.py               #   路由入口（upload/review/tasks/history/rules/knowledge/llm/health）
│   ├── config/               #   settings(.env) / prompts(审查提示词)
│   ├── db/                   #   SQLite：任务/结果/风控规则 + 运行时 LLM 配置仓储
│   ├── llm/                  #   多 provider 客户端（client） + 自定义模型注入/探测（registry）
│   ├── rag/                  #   RAG 精简复用（自 zhiqida-rag）：加载/切分/嵌入/检索/重排
│   ├── review/               #   拆解/元数据/上下文/判定解析/调度/报告
│   ├── rules.py              #   确定性风控规则引擎（纯函数，三类 matcher）
│   ├── data/                 #   运行时数据（.gitignore）
│   └── tests/                #   单测（pytest，36 passed）
├── frontend/                 # Vue3 + Vite + TS + Element Plus（5174）
│   └── src/views/            #   上传审查/审查中/结果/历史记录/规则配置/知识库管理/模型配置
├── samples/                  # 合同样例（docx/txt）+ generate_samples.py
├── scripts/                  # start.bat / start.sh / seed_kb.py
├── deploy/                   # Dockerfile×2 + docker-entrypoint.sh + nginx.conf + docker-compose.yml
└── docs/                     # 技术架构设计文档
```

## 测试

```bash
cd backend && python -m pytest tests -q      # 36 passed
cd frontend && npm run build                 # vue-tsc 类型检查 + 产物构建
```

## 主要 API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/upload` | 上传 PDF/DOCX/TXT（≤20MB），返回 `file_id/页数/预估条款数` |
| POST | `/review` | 按 `file_id` 创建后台审查任务 |
| GET | `/tasks/{id}/status` | 轮询进度/汇总/已发现风险 |
| GET | `/tasks/{id}/results` | 结果页明细（含法条依据/修改建议） |
| GET | `/tasks/{id}/report` | 下载 Word 审查报告 |
| GET / DELETE | `/history`、`/history/{id}` | 历史任务列表 / 删除（级联清理报告与上传文件） |
| GET / POST | `/rules` | 风控规则列表 / 手动新增 |
| PUT / DELETE | `/rules/{id}` | 更新规则 / 删除规则（重启不复活） |
| GET / POST / DELETE | `/knowledge/stats` `/knowledge/index` `/knowledge` | 知识库统计 / 幂等重建索引 / 清空 |
| GET / POST | `/knowledge/documents` | 文档列表（按 source+category 聚合）/ 上传语料 |
| GET / DELETE | `/knowledge/document` | 文档分块详情 / 删除文档（含源文件） |
| POST | `/knowledge/search` | 知识库语义检索测试 |
| GET / PUT | `/llm/config` | 运行时自定义 LLM 配置（OpenAI 兼容，DB 优先于 .env） |
| POST | `/llm/test` | LLM 连通性测试（不落库） |
| GET | `/health` | 健康检查（LLM provider / 知识库就绪） |

## 里程碑

- [x] I1 工程骨架 + backend/rag 精简复用 + 知识库 seed CLI
- [x] I2 合同拆解 → 元数据 → 双通道审查（规则 + LLM）并发调度
- [x] I3 Word 报告 + 历史 + Vue3 前端页面（含端到端联调）
- [x] I4 前端构建验证 + 后端单测回归（36 passed）
- [x] P2 管理增强：风控规则 CRUD（/rules）、知识库管理（/knowledge/*）、运行时模型配置（/llm/config）
- [ ] I5（下一轮）LoRA 微调数据集 → LLaMA-Factory 训练 → AWQ 量化 → vLLM 切换

## 安全

文件类型白名单 + 20MB 上限 + 随机 `file_id` 存储；上传文件审查完成后自动清理；密钥仅存 `backend/.env`（不入库、不入镜像）。
