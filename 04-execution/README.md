# 从一次试评到批量执行

**复核已有六题**只需要 Python 标准库。在仓库根目录运行下面的命令，即可核验原始答卷文件指纹并重新计算六题自动分。此步骤不调用模型，也不需要 API 密钥。

```bash
python3 06-results/replay.py
```

**发起新的 Codex CLI 测评**使用本目录的工具。工具按题号拆分输入，每题启动一个新容器。作答完成后，由宿主机读取答案包并计算 80 分客观项。历史记录中有三道网页试评、三道托管 Codex 试评；下面的本地 Docker 工具提供后续批量运行方式。本项目发布时已验证分题准备、官方插件的本地安装识别、结果验证和离线重算。本地 Docker 容器到真实模型的全链路待部署实测。

## 在 Linux 或 WSL 上新建一批试评

准备 Python 3.10 或以上、本地 Docker Engine、能使用目标模型的 OpenAI API 项目以及你依法取得的 Codex Accounting 1.0.1 官方插件。使用 WSL 时，把仓库放在 Linux 文件系统内。Docker 守护进程需要与 Python 进程共享本地 Unix socket；远程 Docker 端点不适用。

1. 构建工作器，找到你自己的 Accounting 安装目录。安装可在 Codex CLI 的 `/plugins` 内完成。若安装位置不同，修改下方 `acct_plugin_dir`。脚本核对完整包与原试验指纹，版本不一致会停止。

```bash
cd 04-execution
docker build -t accounting-codex-pilot:1 .
acct_plugin_dir="$HOME/.codex/plugins/cache/openai-curated-remote/accounting/1.0.1"
ls "$acct_plugin_dir/.codex-plugin/plugin.json"
```

2. 准备一批三个案例变体。要扩展到更多题，在 `--tasks` 后列出公开受测包中存在的题号。商业项目要保护未曝光的测试题，同批输入与专家答案应保存在宿主机的不同目录。

```bash
python3 runner.py prepare --input ../02-dataset/development_inputs_v1.0.zip \
  --tasks T005 T050 T053 --run-dir runs/pilot-01
```

3. 从安全环境配置宿主机的 `OPENAI_API_KEY`；不要将值写入文件、命令历史或提交到仓库。指定项目可用的精确模型 ID。新执行会实际产生 API 费用。

```bash
read -rsp "OpenAI API key: " OPENAI_API_KEY && export OPENAI_API_KEY && echo
read -rp "OpenAI model ID: " acct_model_id
python3 runner.py run --run-dir runs/pilot-01 --model "$acct_model_id" --plugin-dir "$acct_plugin_dir"
```

4. 在宿主机上评分。逐题结果和汇总保存在 `runs/pilot-01/scores/`。

```bash
python3 runner.py score --run-dir runs/pilot-01 --answers ../05-scoring/development_reference_v1.1.zip
```

## 运行记录与隔离

每题文件处理进程只能读取本题输入、固定官方插件和自己的输出目录。输入和插件只读，每次创建新配置与会话，容器禁用网络。宿主机代理将模型请求转发到固定的 OpenAI Responses 接口，密钥不挂载到容器。执行器只接受同一批次的固定模型、镜像及执行配置。

检查 `attempts/<题号>/<尝试编号>/receipt.json` 可查看运行状态，`out/` 保留交付文件。模型回合完成、插件已启用、成功读取技能文本、独立会话编号及输出指纹共同构成运行证据。基础设施故障、超时、激活未核验均保留空分。经核验的会话若漏交文件则保留其真实交付情况。基础设施失败可用原命令加 `--retry` 开始新尝试；已交卷的题会跳过。换模型或研究同题重复性时另起批次。

新生成的原始日志供本地复核。对外提供材料时，应先筛除账号、会话链接、私有路径及个人签名。示例结果的数据发布范围见 `../06-results/README.md`。
