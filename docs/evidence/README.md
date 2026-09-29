# 验收证据索引

各文件保留当时的代码基线与环境事实，不自动代表当前源码、现有服务或生产资源的状态。验收范围和交付口径见 [docs/acceptance.md](../acceptance.md) 与 [工程验证](../engineering.md)。

- [e2b-live-2026-09-28/](e2b-live-2026-09-28/README.md)：基线 `6c73023`，E2B 真沙箱 / 真模型 run、工作区 64 文件上限、真实 SDK timeout 参数与销毁、追问续接；含失败重试和按 id 清理事实。非多租户验收。
- [e2b-run.txt](e2b-run.txt)：2026-09-22 的历史 E2B 验收和后续更正；旧的单向 JSON / tee 路径不代表当前 RPC 实现。
- [docker-run.txt](docker-run.txt)：历史 Docker 面真实 run。
- 其余 `acp-*`、`browser-*`、`local-*` 等按各自文档/文件标注的基线与限制阅读。

不要向此目录复制 `.env`、完整宿主日志、运行期配置、会话文件或含出口代理 token 的 JSONL；本次 E2B live 仅保存脱敏、筛选后的记录与摘要，完整临时数据根在资源核验后删除。
