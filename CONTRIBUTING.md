# 参与贡献

感谢你对本项目感兴趣！在开始之前，请先阅读：

- `README.md` — 功能概览与快速开始
- `CLAUDE.md` / `AGENTS.md` — 架构说明、工作流约定与配置参考（两个文件内容同步，改动时需同时维护）
- `SECURITY.md` — 安全边界与漏洞报告方式
- `CHANGELOG.md` — 版本历史

## 开发环境搭建

```bash
# 1. 创建虚拟环境并安装依赖（含开发依赖 pytest）
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install ".[dev]"

# 2. 准备环境变量
cp .env.example .env   # 填入你的实际配置（数据库、GitLab、LLM 等）

# 3. （可选）启动依赖服务
docker-compose up -d postgres redis
```

> `.env` 含敏感信息，已在 `.gitignore` 中排除，**永不提交**。

## 运行测试

```bash
# 全量测试
pytest tests/

# 单个测试文件
pytest tests/unit/test_agent_tools.py

# 单个测试用例
pytest tests/unit/test_agent_tools.py::TestReadFile::test_read_existing_file
```

测试全部为离线单元测试（无网络/外部服务依赖）。`search_code` 工具依赖 POSIX `grep`，请在 Linux/macOS 或 WSL 下运行相关用例。

## 代码规范

- 使用绝对导入（`from src.core.xxx import ...`），组间空一行
- 类型标注使用 Python 3.10+ 语法（`str | None`、`list[str]`）
- 命名遵循 PEP 8：函数/变量 `snake_case`、类 `PascalCase`、常量 `UPPER_CASE`、私有成员 `_` 前缀
- 字符串统一双引号，拼接优先 f-string；logger 使用 `%`-formatting
- 所有公开类/函数必须有 docstring
- 面向用户的输出（LLM prompt、审查报告、日志）使用中文

## 提交规范

- 提交信息使用 Conventional Commits 风格：`feat:` / `fix:` / `docs:` / `refactor:` / `chore:` / `test:` 前缀
- 提交前确保相关测试通过
- 功能或配置变更后，同步更新 `CLAUDE.md` / `AGENTS.md` / `.env.example` / `README.md`

## 硬纪律

| 禁止事项 | 说明 |
|----------|------|
| 提交 `.env` 或任何真实密钥 | 密钥只应存在于本地 `.env`（已被 .gitignore 排除） |
| 提交真实服务器地址 / 内部域名 / 本机路径 | 示例一律使用 `example.com`、`localhost`、`127.0.0.1` |
| 提交真实人名 / 邮箱 / 客户信息 | 测试与文档使用脱敏占位 |
| 绕过隐私过滤链路 | 敏感数据保护是核心特性，改动线需自带测试 |
| 引入未同步的配置项 | 新增环境变量必须同步 `.env.example` 并更新配置参考 |

## 提交流程

1. 新建分支：`git checkout -b feat/your-feature`
2. 小步提交，每步可独立验证
3. 运行全量测试，确认通过
4. 发起 Pull Request，说明变更内容与测试结果
