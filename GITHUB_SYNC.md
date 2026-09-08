# GitHub 自动同步规则

本子项目的长期同步规则：

- 唯一远端 `origin` 为 `https://github.com/xiaobai156/dawei.git`，只同步本子项目。
- 每次本子项目修改完成并通过必要验证后，只提交本次任务明确产生的文件和行，并推送当前分支。
- 任务开始前已经存在的用户未提交改动必须保留，禁止混入提交、覆盖、还原、清理或强制推送。
- 凭据、密钥、令牌、`.env`、临时/锁文件、缓存、审计产物、生成 TXT、构建产物和无关项目不得提交或推送。
- 提交前检查暂存区 diff 与 `git diff --cached --check`；验证未通过不得提交或推送。
- 只允许普通 `git push origin <当前分支>`，禁止 force push；推送失败如实报告并保留本地提交。

`.gitignore` 负责拦截常见本地产物，`.githooks/pre-commit` 负责拦截敏感或临时路径，`.githooks/post-commit` 负责提交后自动推送。
