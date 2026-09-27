# 贡献指南

感谢关注本项目。

## 范围
本仓库主要由 @xiongweilin 维护，欢迎提交 issue 和 pull request。

## 工作流
1. 较大改动先开 issue 讨论。
2. Fork 仓库或从 `main` 创建分支。
3. 提交前运行本地检查：
   - 遵循现有风格和 `README` 说明。
   - 如果仓库提供测试/lint/build，按仓库约定运行（例如 `make`、`ruff`、`pytest`、`npm run build`）。
4. 提交目标为 `main` 的 PR，并填写 PR 模板。
5. CI 必须通过；维护者随后审查。

## Commit 风格
- 使用小而聚焦的 commit；subject 使用简短祈使句（例如 “Add health check”）。
- 不要提交 secret、`.env` 值或 PII；使用占位符。

## 本地验证
如果存在 `.github/scripts/validate_structure.py`，请在本地运行。

## 行为准则
参与本项目需遵循 `CODE_OF_CONDUCT.md`。

## 许可证
提交贡献即表示同意贡献内容按照仓库 `LICENSE` 授权。
