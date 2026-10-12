# deploy/ —— 生产级部署（进阶）

⚠️ **普通用户请忽略本目录**，直接看项目根目录的 [README](../README.md)，
双击 `start-mac.command` / `start-windows.bat` 即可。

---

## 这个目录是什么

根目录的 `docker-compose.yml` 是**简易版**：只起「协议网关 + 主程序」两个容器，
配置存 JSON、消息存本地文件，**不需要数据库** —— 适合个人和小团队。

本目录是**进阶版**，当你出现以下需求时再切换过来：

| 需求 | 简易版 | 本目录 |
|---|---|---|
| 多实例水平扩展 | ❌ | ✅ K8s / 多 Worker |
| 消息全量归档与查询 | ⚠️ 本地文件 | ✅ PostgreSQL |
| 长期记忆 / RAG | ❌ | ✅ pgvector |
| 监控告警 | ❌ | ✅ Prometheus + Grafana |
| 会话粘性路由 | ❌ | ✅ 按 chat_id 哈希分区 |

## 目录内容

| 文件 | 用途 |
|---|---|
| `docker-compose.yml` | 完整栈：网关 + 编排 + Redis + PostgreSQL + Prometheus + Grafana |
| `Dockerfile` | 编排服务镜像 |
| `init.sql` | 数据库表结构（含 pgvector 向量表） |
| `prometheus.yml` | 指标采集配置 |
| `alerts.yml` | 告警规则（登录态失效、消息积压、单群发言过密等） |
| `.env.example` | 环境变量模板 |

## 使用

```bash
cd deploy
cp .env.example .env      # 填入密钥
docker compose up -d
```

启动后：
- 管理界面： `http://localhost:8000`
- Prometheus： `http://localhost:9090`
- Grafana： `http://localhost:3000`

## 注意

当前 `src/` 下的代码默认使用内存存储（`store_memory.py`）。
要启用本目录的 Redis / PostgreSQL，需要把 `MemoryStore` 替换为对应的持久化实现
（接口签名一致，见 `src/store_memory.py` 顶部的说明）。

完整设计说明见 [`docs/微信LLM机器人技术方案.md`](../docs/微信LLM机器人技术方案.md) 第 7、8 章。
