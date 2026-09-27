# BENCHMARK — sparkjury-arbitrate

> 本文件的命令在 sparkjury 仓根目录、macOS (darwin 25.5.0)、Python 3.12、uv 环境下**逐条实跑通过**。
> 阈值口径引用 `standards/scenario-pack/thresholds.yaml` 的 `arbitration` 段
> 与 `standards/scenario-pack/judges.yaml` 的 `arbiter` 段（FROZEN pack，
> `frozen_hash = 86325dc1（v0.2，2026-09-27 冻结；历史背书跑在 v0.1@6ecbd667 上）`）。

## 0. 环境前提交代（macOS 必做，本坑实测踩中）

本机设有**大写** `ALL_PROXY=socks5://127.0.0.1:7897`。`sparkjury arbitrate` 在
`--jev auto`（默认）下会构造 `JevClient`，而 `httpx.Client()` 默认 `trust_env=True`
会读这个 socks 变量，然后抛：

```
ImportError: Using SOCKS proxy, but the 'socksio' package is not installed.
```

只 unset 小写三个**不够**，必须四个一起：

```bash
env -u http_proxy -u https_proxy -u all_proxy -u ALL_PROXY uv run sparkjury arbitrate ...
```

影响面：`--jev auto` + 未配 key 也会踩（`JevClient()` 在判 `configured` 之前就建好了
httpx client）。`--jev off` 完全不构造 `JevClient`，不受影响。
`sparkjury run --demo` 的 ARBITRATE 阶段同样受影响（实测：不 unset `ALL_PROXY`
则 `stage_end ARBITRATE failed`，整轮 run 失败）。

## 1. 冒烟：离线仲裁（Jev 关掉）

先造一个带 panel results 的 store：

```bash
env -u http_proxy -u https_proxy -u all_proxy -u ALL_PROXY \
  uv run sparkjury run --demo --db runs/skill-check.db --run-id arb-bench \
  --stages INGEST,PRECHECK,SCORE,ARBITRATE
```

通过标准：退出码 0；`ARBITRATE ok  n_traces=<N> n_degraded=<K>`；
`by_source` 里**没有 `jev`**（无 key），`local` 或 `panel_fallback` 承接全部分歧；
`run manifest` 的 `degradations[]` 有一条 `stage=ARBITRATE, component=jev` 的记录。

### 单独跑仲裁

```bash
env -u http_proxy -u https_proxy -u all_proxy -u ALL_PROXY \
  uv run sparkjury arbitrate --db runs/skill-check.db --jev off --json > /tmp/arb.json
```

通过标准：退出码 0；`/tmp/arb.json` 顶层键恰好为 `summary` / `decisions`。

### 前置门：没有 panel results 时必须报错

```bash
env -u http_proxy -u https_proxy -u all_proxy -u ALL_PROXY \
  uv run sparkjury arbitrate --db /tmp/empty.db --jev off; echo "rc=$?"
```

通过标准：退出码 **1**，stdout/stderr 含
`no panel results in store; run sparkjury score first`。

### wrapper 入口

```bash
uv run python skills-governance/sparkjury-arbitrate/scripts/run.py --help
```

通过标准：退出码 **0**，stdout 含 `Usage`。
对应 `tests/test_m9_skills_nat.py::test_skill_wrapper_scripts_invoke_cli_help[sparkjury-arbitrate]`。

## 2. 形状与自洽校验

```bash
python3 - <<'PY'
import json
d = json.load(open("/tmp/arb.json"))
ok = True
def chk(cond, msg):
    global ok
    print(("PASS  " if cond else "FAIL  ") + msg); ok = ok and bool(cond)

chk(set(d) == {"summary", "decisions"}, "顶层键恰为 summary/decisions")
s, decs = d["summary"], d["decisions"]
arbs = [a for dec in decs for a in dec["arbitrations"]]
chk(s["n_traces"] == len(decs), "summary.n_traces == decisions 条数")
chk(s["n_dimensions"] == len(arbs), "summary.n_dimensions == 仲裁条数")
chk(sum(s["by_source"].values()) == len(arbs), "by_source 计数之和 == 仲裁条数")
chk(set(s["by_source"]) <= {"panel","jev","local","panel_fallback"},
    "by_source 的键落在 DecisionSource 枚举内")
chk(s["n_degraded"] == sum(1 for a in arbs if a["degraded"]),
    "n_degraded == degraded=true 的条数")
chk(all(a["degraded"] == (a["source"] in ("local","panel_fallback")) for a in arbs),
    "degraded 当且仅当 source ∈ {local, panel_fallback}（pack 红线 2 的可机检形式）")
chk(all(a["final_label"] is None for a in arbs if a["dimension"] != "outcome"),
    "非 outcome 维 final_label 恒为 null")
chk(all(a["final_label"] in ("pass","fail") for a in arbs
        if a["dimension"] == "outcome" and a["final_label"] is not None),
    "outcome 维标签只有 pass/fail")
chk(all((a["source"] == "jev") == (a["jev_raw_score"] is not None) for a in arbs),
    "jev_raw_score 非 null 当且仅当 source == jev")
chk(all(a["error"] is None or a["source"] == "panel_fallback" for a in arbs),
    "error 只在 panel_fallback 通道非 null")
chk(all(a["rationale"].startswith("[degraded:") == a["degraded"] for a in arbs),
    "降级裁决的 rationale 带 [degraded: ...] 前缀")
for a in arbs:
    if a["source"] == "panel":
        sc = [v for v in a["panel_scores"].values() if v is not None]
        chk(max(sc) - min(sc) <= 1, f"{a['trace_id']}/{a['dimension']}: panel 通道 spread ≤ 1")
chk(all(0 <= a["final_score"] <= 4 for a in arbs if a["final_score"] is not None),
    "final_score 落在 0..4")
print("\nALL PASS" if ok else "\nHAS FAIL")
PY
```

通过标准：逐行 `PASS`，末行 `ALL PASS`。

## 3. pack 门禁（引用 standards/scenario-pack/）

| 项 | pack 出处 | 期望值 | sparkjury 状态 |
|---|---|---|---|
| `arbitration.unanimous` | `thresholds.yaml` | `3:0 → 直接记分` | **部分实现**：outcome 看标签一致，其余维看 spread ≤ 1，非字面 3:0 |
| `arbitration.majority_with_golden` | 同上 | 2:1 + gold → gold 裁决 | **未实现**。无 gold 路由 |
| `arbitration.majority_without_golden` | 同上 | 2:1 无 gold → 升级 Jev | **已实现**（且 1:1:1 同样升级） |
| `arbitration.split` | 同上 | `1:1:1 → 升级 Jev` | **已实现** |
| `arbitration.jev_fallback` | 同上 | calibrate 画像 lead judge 裁决，flag `jev_unreachable` | **部分实现**：单级本地裁判兜底；**不产出 `jev_unreachable` 字串** |
| `arbitration.jev_choice_limit` | 同上 | `255` | arbitrate 不用 choice；FailureLabel 10 类，远离上限 |
| `arbitration.sample_audit_rate` | 同上 | `0.05` | **已实现**，判据为 `sha1("audit\|<trace_id>")` 确定性抽样 |
| `scoring.judge_repeats` | `thresholds.yaml:scoring` | `2` | **未实现**。每裁判每维一票，无 repeats 归约 |
| `judges.yaml` 红线 1（异族裁判） | `judges.yaml` | agent 与裁判不同族 | mock panel 三族（qwen/gemma/step）；真实 panel 由部署方保证 |
| `judges.yaml` 红线 2（降级进 manifest） | `judges.yaml` | `degraded_flags` | **字段名不同**：落在 `degradations[]`，见 `references/arbitration-rules.md` §5 |

## 4. 测试门禁

```bash
env -u http_proxy -u https_proxy -u all_proxy -u ALL_PROXY uv run pytest tests/test_m4_arbiter.py -q
```

通过标准：全绿。

## 5. 已知不覆盖项（不要当成已验证）

- Jev 的真实调用（含 `jev` source 分支、`legend` 偏移、超时路径）：**未验证**——
  本机无 `TYPESAFE_API_KEY`，且代理需绕开
- `$0.04/百万输入 token` 成本口径：**pack 记载，非实测**；sparkjury 也不记录 `tokens_in`
- 降级通道（Judge A）与 Jev 通道的系统性偏差：**未验证**
- spread ≤ 1 一致判据在真实裁判上的误一致率：**未验证**
- 本机环境（`ALL_PROXY` 存在）下 `--jev auto` 的可用性：**已确认不可用**，
  见 §0；这不是 sparkjury 的缺陷，是本机代理配置与其交互的结果
