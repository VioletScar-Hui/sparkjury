---
name: sj-push-and-deploy
description: 把改动提交推送到 VioletScar-Hui/sparkjury，并检查团队 DGX Spark 节点当前在跑什么、这次改动要不要部署上去。当你要在这个项目里提交代码、推分支、开 PR，或者想知道节点状态和要不要重新部署时使用。
whenToUse: 用户说提交、推送、开 PR、部署到节点、看看节点在跑什么，或者你刚改完代码准备交付时。
---

> 这一份和 `.agents/skills/sj-push-and-deploy/SKILL.md` 内容相同，改的时候两边一起改。

# 推送到远端 + 按需部署到节点

这个项目有两处「远端」：GitHub 上的仓库，和团队的 DGX Spark 节点。这个技能管两件事，先推到前者，再判断后者要不要重新部署。

配套的常驻规则在仓库根目录的 `AGENTS.md`，那里写的是「为什么」，这里写的是「怎么做」。

## 一次性准备

### 1. 拿到写权限

仓库是公开的，谁都能读，但推送需要写权限。把你的 GitHub 用户名给仓库 owner，他会执行：

```bash
gh api -X PUT repos/VioletScar-Hui/sparkjury/collaborators/<你的用户名> -f permission=push
```

你这边登录一次，然后验证权限确实下来了：

```bash
gh auth login
gh api repos/VioletScar-Hui/sparkjury --jq .permissions.push    # 返回 true 就是能推
```

### 2. 装本地依赖

```bash
uv sync --group ops        # ops 组里有 paramiko，连节点靠它
```

### 3. 把节点连接信息放到本机

在 `deploy/dgx/node.env` 写四行，密码找队里要：

```
SPARKJURY_NODE_HOST=61.172.235.130
SPARKJURY_NODE_PORT=6030
SPARKJURY_NODE_USER=asus_gx10
SPARKJURY_NODE_PASSWORD=...
```

这个文件在 `.gitignore` 里，不会进仓库。懒得写也行，跑的时候会提示你输密码。

## 第一步：推到远端

分支名带上你要做什么，提交信息用中文说清改了什么、为什么改。

```bash
git checkout -b fix/<简短描述>
git add -A
git commit -m "改了什么，为什么"
git push -u origin HEAD
```

推完直接开 PR，不需要绕 fork。PR 描述里先留一段「节点部署验证」，第二步的结果填进去。

main 开了分支保护，非管理员直接 `git push origin main` 会被拒，改动只能走分支加 PR，所以上面那条 `git checkout -b` 不是可选项。开 PR 时把模板带上，别用 `--fill`（那会用提交信息当正文，把模板覆盖掉）：

```bash
gh pr create --title "一句话说清这次改了什么" --body-file .github/pull_request_template.md
```

PR 描述里「节点部署验证」那一栏就是上面那条硬性规则的落地位置，第二步的结果填进去。PR 上会自动跑三平台测试（ubuntu、macOS、Windows），三个检查全绿才允许合并；红了先看日志，别急着合。不想用命令行就用网页开 PR，模板会自动填好。

## 第二步：看节点在跑什么、要不要部署

```bash
uv run --group ops python scripts/node.py check
```

一条命令给你：节点通不通、GPU 占用和统一内存、tmux 里谁在跑、四个 vLLM 端点和 API 起没起、节点上的代码是哪个 commit、你本地是哪个 commit，最后给出判断：需要部署、需要起服务、还是不需要部署。

它的输出是给 PR 用的，可以直接贴。

## 第三步：按判断行动

**不需要部署**：节点代码和你本地一致，服务都在跑。把 check 的输出贴进 PR 收工。

**需要起服务**：代码不用重推，是服务掉了。

```bash
uv run --group ops python scripts/node.py run "cd ~/sparkjury && bash deploy/dgx/start_judges.sh && bash deploy/dgx/status.sh"
```

**需要部署**：

```bash
uv run --group ops python scripts/node.py sync
```

sync 会把当前 commit 记到节点上的 `~/sparkjury/.synced-from`，下次 check 就是靠这行记录判断新旧的。

**节点不可达**：不许假装跳过。把失败原因写进 PR 描述，PR 留在草稿状态，不要合并。这一条是硬规矩。

**主树上有长任务在跑**：check 会提示 `tau2full`、`loop` 这类会话。这时候不要往 `~/sparkjury` 覆盖，先复制一份自己的树，在副本里折腾：

```bash
uv run --group ops python scripts/node.py run "cp -r ~/sparkjury ~/sparkjury-<你的名字>"
```

副本不是复制完就能用的，下面两条不处理，就会出现"测了等于没测"：

- 副本 `.venv` 里的 `_editable_impl_sparkjury.pth` 写的是**绝对路径**，还指向主树的 `src`，`import sparkjury` 拿到的仍是主树代码。把它改成副本自己的 `src`。
- 更隐蔽的一条：`cp -r` 出来的 console script（`pytest` 这些）shebang 也是绝对路径，仍然指向**主树**那份 `.venv/bin/python`。于是 `uv run pytest` 是用主树的解释器在主树代码上跑测试，副本里的改动一行都没跑到（副本自带的 `sparkjury` 命令反而可能是对的，因为 uv 重装过它，别拿它当参照）。烟测第一步因此写成 `uv run python -m pytest`，走副本自己的解释器。

改完先确认一句话再往下走：`uv run python -c "import sparkjury;print(sparkjury.__file__)"`，路径里得是副本的名字。用完删掉：`rm -rf ~/sparkjury-<你的名字>`。

## 第四步：部署完的冒烟验证

三样，缺一样都不算部署通过。

```bash
# 1. 测试（在副本里必须写成 python -m pytest，理由见上面那两条）
uv run --group ops python scripts/node.py run "cd ~/sparkjury && ~/.local/bin/uv run python -m pytest -q"
# 2. 服务与监听地址
uv run --group ops python scripts/node.py run "cd ~/sparkjury && bash deploy/dgx/status.sh"
# 3. 一条真实 run
uv run --group ops python scripts/node.py run "cd ~/sparkjury && ~/.local/bin/uv run sparkjury run --demo"
```

第三条只想确认链路通用 `--demo` 就够了（离线 mock 裁判，两秒）。改动大的话跑 `--config deploy/run.toml`，然后打开 `runs/<run_id>/manifest.json` 看 `status` 和 `degradations`。

三样的实际输出摘要贴进 PR 的「节点部署验证」段。降级项照实写：StepFun 没配 key 会退化成 mock 裁判，Jev 没配 key 会退化成本地仲裁，这两种都会出现在 `degradations` 里。

## 常见卡点

- `gh pr create` 之后立刻 `gh pr checks --watch`，有时会在检查还没注册时报错并以 1 退出，那不是测试失败。隔半分钟再 watch，或者直接盯 workflow：`gh run watch $(gh run list --branch <分支> --limit 1 --json databaseId --jq '.[0].databaseId') --exit-status`。
- **合并成功之前不要删分支**。head 分支一删，GitHub 会把这个 PR 直接关掉；要恢复就把分支推回来再 `gh pr reopen <号>`。
- 必需检查还在 pending 时合并会被拒，提示里会建议 `--admin` 绕过。别绕，那是分支保护在正常工作——等检查跑完。

`uv: command not found`：macOS 上 uv 可能装在 `/opt/homebrew/bin/uv`，`~/.local/bin/uv` 有时是个坏掉的包装脚本；节点上 uv 在 `~/.local/bin/uv`，非登录 shell 里不在 PATH，要写全路径或者用 `bash -lc` 包一层。

`ModuleNotFoundError: paramiko`：跑 `uv sync --group ops`。

想在节点上 `git pull`：不行。实测那台机器访问 github.com 会超时，同步一律走 `node.py sync`。

check 说「节点上没有同步记录」：说明节点上那份代码是别人用老版 sync 推的，或者从来没同步过。跑一次 `sync` 就有记录了。

## 红线

以 `AGENTS.md` 的「红线」一节为准。这里是同一批规则的逐条复述——只加载本技能、没读到
`AGENTS.md` 正文的 Agent 也要看全，所以不能压成一句摘要：原来的那一句就漏掉了 `scp` 的
1GB 上限、8888/9000 必须鉴权、以及节点活动结束会清盘这三条。

- 禁止 `reboot`、`shutdown`、`poweroff`，禁止改系统级配置（密码、SSH 配置、防火墙、路由、用户权限）。
- 禁止探测内网 `192.168.110.0/24`。
- 超过 1GB 的文件禁止 `scp`，模型一律在节点内下载，上行带宽是 50 支队共用的。
- 长任务必须跑在 `tmux` 里，占着前台会被断连带走。
- 8888 和 9000 上对外提供的服务必须有鉴权。
- 节点在活动结束后会被清空：代码要及时 push，跑出来的产物要及时拷出来。
- 不提交密钥，不把节点密码写进代码或 PR 描述。
