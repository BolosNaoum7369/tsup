---
name: deploy
description: 将 go-tunnel 的客户端文件及 .github 上传到本地 GH_PAT 所属账号的 gost 仓库，按需 fork 上游并配置同名 Actions 密钥。用于部署或更新这套隧道客户端。
---

# 部署 gost

使用随 skill 附带的 `scripts/deploy.py`，需要 Python 3.10+ 和 GitHub CLI (`gh`)。脚本直接调用 GitHub.com API，不依赖当前 Git remote 或 `gh auth login` 的账号。

上传范围固定为项目根目录的 `client.go`、`client.yaml`、`common.go`、`go.mod`、`go.sum`，以及 `.github` 内全部普通文件。从项目根目录的 `GH_PAT` 文件读取令牌，以该令牌调用 `/user` 确定目标账号。`GH_PAT` 只用于鉴权和同名 Actions 仓库密钥，不属于上传文件。

## 执行

将下列命令的脚本路径替换为本 skill 内脚本的绝对路径，`--source` 明确指定用户要部署的项目目录：

```powershell
python <skill-dir>/scripts/deploy.py --source D:/workspace/go-tunnel --dry-run
python <skill-dir>/scripts/deploy.py --source D:/workspace/go-tunnel
```

部署前先检查并优先使用本机已有的 Python，不要为了这个无额外依赖的脚本默认创建或配置新环境。用 PowerShell 定位解释器：

```powershell
Get-Command python, py -ErrorAction SilentlyContinue | Select-Object Name, Source
```

若找到 `python`，运行 `python --version`；否则使用 `py --version`。版本为 3.10+ 时直接运行上面的 dry-run，通过后再部署。只有本机没有解释器或版本过低时，才配置/安装 Python。若配置工具无响应或返回 `cancelled`，不要据此声称用户取消，也不要连续重试同一个卡住的调用；如实说明工具未完成、部署脚本尚未运行及远端尚未修改。版本无法确认时停止，不要猜测部署状态。

`--dry-run` 只校验本地文件并列出清单，不访问网络，令牌缺失或为空时仍可预览。实际部署前需有效的 `GH_PAT`。不要输出令牌、把令牌放进命令参数，或从 Git remote、其他已登录账号替代读取。若文件为空，完成工具和本地检查后告知用户在本机填写，无需让用户在聊天中发送令牌。

目标仓库已存在时直接更新。若不存在，按用户已选定的默认来源 `go-gost/gost` fork 为其个人账号下的 `gost`；用户要求其他来源时通过 `--upstream OWNER/REPO` 覆盖。用户已明确要求部署时，可直接执行其授权范围内的上传和密钥设置，无需重复确认。

脚本先写入 Actions 仓库密钥 `GH_PAT`，再向目标默认分支创建单次提交，信息精确为 `Add files via upload`。保留远端其他文件；同名路径更新，`.github` 中未被本地覆盖的远端文件也保留。文件内容完全相同时不创建空提交，但仍更新密钥。不会 force push；并发更新、权限不足或网络失败时报错并停止，已完成的 fork 或密钥设置可能保留。先检查输出和远端状态，再决定是否重跑。

部署结束检查脚本输出的仓库、分支、提交及密钥元数据验证结果，再报告成功。密钥值不可读回，验证只证明同名密钥存在且写入接口成功。部署成功只表示文件已上传，不证明 workflow 已运行或定时任务已经触发。上传工作流不等于启动工作流；本项目 `ci.yml` 仅有 `workflow_dispatch`，运行需用户另外要求。新 fork 的 Actions 可能需要启用；对定时任务只说明已部署及其前置条件，不要宣称已验证触发。

令牌必须能访问目标仓库、写入内容/工作流和 Actions Secrets。权限不足时按 GitHub 的具体错误补齐权限；不要擅自切换账号或放宽仓库设置。

API 行为参考：[fork](https://docs.github.com/en/rest/repos/forks#create-a-fork)、[Git trees](https://docs.github.com/en/rest/git/trees#create-a-tree)、[更新引用](https://docs.github.com/en/rest/git/refs#update-a-reference)、[gh secret set](https://cli.github.com/manual/gh_secret_set)。
