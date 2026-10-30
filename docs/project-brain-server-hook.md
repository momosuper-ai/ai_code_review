# GitLab 服务端目录边界

应用路径检查不能替代 Token 的服务器权限。Self-Managed 管理员可安装 `scripts/gitlab/project_brain_pre_receive.py`，使发布机器人无论经 REST 或 Git 写入，都只能改变根目录 PROJECT_BRAIN.md 或 docs/project-brain/ 内普通文件。GL_ID 识别实际 GitLab 身份；作者名、提交标题不参与授权。

脚本检查最终差异和每个新引入提交，阻止“越界修改后再恢复”规避；拒绝非普通模式、标签、目标分支删除、force push 和独立历史。临时分支必须基于当前目标，分支创建不会把整个仓库当新增。其他人工身份正常使用 GitLab 权限。

安装要求 Python 3、Git，配置文件由管理员写入、Git 服务账号只读。先在测试仓库确认真实发布机器人的 GL_ID（user-N；若使用密钥身份还需记录 key-N），替换样例值，同步目标分支/临时前缀。不要依赖仓库内可被开发者修改的配置。

以下示例适用于 Linux 包安装；在 GitLab 管理员终端执行。先备份既有 hooks，将脚本合入已有 custom_hooks 包，避免 hooks set 替换其他团队规则：

```bash
sudo install -m 0644 project-brain-hook.example.json /etc/gitlab/project-brain-hook.json
sudoedit /etc/gitlab/project-brain-hook.json
mkdir -p custom_hooks/pre-receive.d
install -m 0755 project_brain_pre_receive.py custom_hooks/pre-receive.d/50-project-brain
tar -cf custom_hooks.tar custom_hooks
cat custom_hooks.tar | sudo -u git -- /opt/gitlab/embedded/bin/gitaly hooks set \
  --storage STORAGE --repository RELATIVE_REPOSITORY_PATH \
  --config /var/opt/gitlab/gitaly/config.toml
```

每个 Gitaly/Praefect 节点都需要对应脚本和配置。文件名不得以 ~ 结尾，脚本需要可执行。GL_ID、GL_REPOSITORY 等变量和 quarantine 对象环境由 GitLab 提供，不能在生产另用自行猜测的身份信息。[GitLab 官方安装与环境变量说明](https://docs.gitlab.com/administration/server_hooks/)。

验收必须涵盖：发布身份的允许目录自由文件名、目录外业务代码、路径/模式、中间提交恢复、目标删除/force push、临时分支首建/清理、REST commit、MR 自动合并、普通人工身份。若目标 GitLab 的操作链没有经过 hook，不能声称服务端保证已经生效。GitLab.com 无此安装能力。

本项目提供脚本及本地真实 Git 历史测试，未替你安装远端 hook。回滚使用预先备份的完整 hook 包和配置；不要通过空包移除其他团队规则。
