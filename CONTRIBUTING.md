# 贡献指南

项目作者与维护者：[Sirius-Peng（Yuhan Peng）](https://github.com/Sirius-Peng)。当前已确认的贡献者见[CONTRIBUTORS.md](CONTRIBUTORS.md)。

## 提交身份

每位贡献者使用**自己的GitHub账号**与该账号关联的邮箱；不要照抄维护者的身份。先在自己的克隆中配置（只影响本仓库，不改全局）：

```sh
git config --local user.name "YOUR_GITHUB_USERNAME"
git config --local user.email "YOUR_ID+YOUR_GITHUB_USERNAME@users.noreply.github.com"
git var GIT_AUTHOR_IDENT
```

使用GitHub邮箱设置页提供的真实noreply地址，不把上面的占位符提交到历史中。推送后可在提交页面确认头像和账号链接。GitHub通过邮箱关联提交，Git的用户名与认证用的账号不是同一项配置。

已有提交的历史邮箱若尚未关联，可在[GitHub邮箱设置](https://github.com/settings/emails)添加并完成验证，保留原提交哈希。`.mailmap`只用于Git展示别名；不承诺它能替代GitHub账户验证或立即刷新贡献统计。涉及历史重写时先备份并协商分支，已发布的Release标签与二进制不随意移动或替换。

参考：[GitHub提交邮箱说明](https://docs.github.com/en/account-and-profile/how-tos/email-preferences/setting-your-commit-email-address)、[贡献者显示排查](https://docs.github.com/en/repositories/viewing-activity-and-data-for-your-repository/viewing-a-projects-contributors)。

## 修改与验证

1. 描述问题、复现步骤和改动范围；不提交API Key、登录凭据、聊天记录或个人数据目录。
2. 功能变化补充对应回归，更新相关文档，保留已有配置与数据迁移兼容性。
3. 运行Python回归、相关Node前端/桌面测试及文档检查，再提交Pull Request。

```sh
python -m unittest discover -s tests
node tests/test_proactive_frontend.cjs
python tools/check_docs.py
```

署名/仓库资料检查：`python -m unittest discover -s tests -p test_repository_attribution.py`。Windows与macOS安装包使用对应原生构建环境；发布平台与测试范围以[验证矩阵](docs/validation.md)为准。

## 仓库内容

运行依赖只维护根目录 `requirements.txt`。不要提交构建产物、缓存、用户资料或重复生成的人格语料；需要训练格式时运行 `python tools/export_persona.py`。人格示例修改后运行 `tests/test_persona.py` 与 `tests/test_repository_hygiene.py` 的回归。测试、诊断工具、独立人格验证集与历史发布清单均有实际用途，继续保留。
