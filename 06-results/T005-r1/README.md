# T005-r1 托管 Codex 单题预检

2026 年 11 月 21 日，托管 Codex 在全新会话中调用官方 Accounting 1.0.1，仅凭这道开发题的材料交回了[原始答卷](response.json)和[工作底稿](workpaper.md)。独立评分端使用配套的 v1.2 评分器，客观分为 **80/80**。人工分和百分制总分留空。本次按开发题回归单列，48 题留出组的汇总见[盲测初步结果](../blind48/README.md)。

| 要核查的问题 | 本次证据 |
|---|---|
| 插件是否实际读取 | 受测会话确认调用了官方 Accounting。文件处理进程完整读取的指令与指定版本的指令摘要相同。 |
| 输入、交卷是否独立 | 会话未继承调度对话。文件处理进程只读本题 6 个输入文件和插件，禁止联网。6 次文件操作前后输入指纹一致。独占输出目录在前 5 次操作结束时均为空，第 6 次操作后首次出现两份交付件。 |
| 多候选时如何填写 | B003 标为 `REVIEW`，已确认账簿列表为 `[]`，差额为 `null`。底稿列出 L003 和 L099，说明两者的对手方、日期及金额相同，需补回单或付款指令才能确定归属。 |

[公开运行记录](record.json)包含原件与答卷的 SHA-256、插件指令读取摘要及评分器的完整客观报告。平台账号级记忆开关和精确后端模型快照无法独立核验。原版 T005 首答为 76.6667/80，r1 明确了多候选时已确认字段的写法。两次成绩对应不同版本题面。[字段裁定](../../03-validation/T005-adjudication.md)

## 用公开文件复算

在仓库根目录用 Bash 及 Python 3.10 或以上运行下列命令。评分器读取已公开的两份首次交付，结果应为 `auto_score = 80.0`，人工分和最终分均为 `null`。

```bash
run_dir=$(mktemp -d)
python3 -m zipfile -e 02-dataset/T005-r1_单题受测包.zip "$run_dir"
python3 -m zipfile -e 05-scoring/T005-r1_单题评分包_v1.2.zip "$run_dir"
mkdir -p "$run_dir/results/T005"
cp 06-results/T005-r1/{response.json,workpaper.md} "$run_dir/results/T005/"
python3 05-scoring/score.py --test-root "$run_dir/受测包" --gold-root "$run_dir/答案包" --results "$run_dir/results" --task T005
```
