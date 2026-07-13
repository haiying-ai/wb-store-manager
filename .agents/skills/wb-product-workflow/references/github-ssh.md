# GitHub SSH 发布

用于本工作区的 GitHub 提交与推送。远程仓库为 `git@github.com:haiying-ai/wb-store-manager.git`，本地 Git 元数据位于 `E:\CodexWorks\WB\data\cache\local-git`，工作树为 `E:\CodexWorks\WB`。

本机 OpenSSH 配置将 `github.com` 绑定到 `C:/Users/孙兵/.ssh/id_ed25519_github_wb`。不要读取、复制、输出或提交私钥。推送前仅验证：

```powershell
ssh -T git@github.com
git --git-dir 'E:\CodexWorks\WB\data\cache\local-git' --work-tree 'E:\CodexWorks\WB' push -u origin main
```

先检查状态并只暂存本次请求的文件；工作树有无关改动时不得使用 `git add -A`。
