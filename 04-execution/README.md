# 从一次试评到批量执行

**复核首轮 12 题**只需要 Python 标准库。在仓库根目录运行下面的命令，可核对 27 份答卷原件、按现行客观评分基线重算各题，并比对首轮存档。T002 的人工分取自匿名历史复核记录，T053 的同卷重评分已反映公式核验裁定。

```bash
python3 06-results/replay.py
```

**本轮留出题在托管 Codex 中作答。** 管理员按题建立全新会话，只发本题材料，调用官方 Accounting 1.0.1，交卷后由会话外的评审端评分。T005-r1 先跑通同一流程，客观分为 80/80。随后 48 题各自首次交卷，原始输入、插件指令读取和各题输出均有文件指纹。平台账号级记忆设置与底层模型精确快照无法独立核验。[T005-r1 预检记录](../06-results/T005-r1/README.md) · [48 题运行结果](../06-results/blind48/README.md)

**在本机通过 Codex CLI 新起测评**，可使用本目录的 Docker 工具。工具按题拆分输入，每题启动新容器，宿主机收卷评分。本地路径已分步检查分题准备、插件安装识别和离线评分。选用该路径时，在目标宿主机另用一道公开题核验容器连接模型、加载插件、隔离收卷及评分。

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

公开开发题采用仓库的 [v1.2 客观评分器](../05-scoring/score.py)，配套的开发题参考答案沿用 v1.1。私有批次将 `--answers` 指向评审端的完整 v1.2 评分包，程序会选用包内的全题型评分器。开发批次也可单独选择 T005-r1 的 v1.2 配套评分包。程序先核对完整答案包与配套评分器指纹，再核对题号范围及每题输入清单。首轮评分将版本写入 `scores/grading.json`；同一批次再次评分时须匹配初次记录。答案真值改动、材料错配或同批换包会中止，既有成绩保留。未确认交卷的题记录为 `NO_VERIFIED_RUN`、分数留空。

## 本机 CLI 的运行记录与隔离

每题文件处理进程只挂载本题输入、固定官方插件、本题输出目录及模型请求代理。输入和插件只读，每次创建新配置与会话，容器禁用直接联网。宿主机代理将模型请求转发到固定的 OpenAI Responses 接口，密钥留在宿主机。模型 ID、镜像与插件指纹以及执行脚本指纹记入批次记录。正式开跑前由管理员登记选定的超时、基础设施故障的重试上限和人工介入规则，并检查受测端与评分端隔离。

检查 `attempts/<题号>/<尝试编号>/receipt.json` 可查看运行状态，`out/` 保留交付文件。模型回合完成、插件已启用、成功读取技能文本、独立会话编号及输出指纹共同构成运行证据。基础设施故障、超时、激活未核验均保留空分。经核验的会话若漏交文件则保留其真实交付情况。基础设施失败可用原命令加 `--retry` 开始新尝试；已交卷的题会跳过。换模型或研究同题重复性时另起批次。

新生成的原始日志供本地复核。对外提供材料时，应先筛除账号、会话链接、私有路径及个人签名。示例结果的数据发布范围见 `../06-results/README.md`。
