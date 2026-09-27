#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_selftest.py — prioritize skill 的自检用例（实现与验证分离）。

入口：
  python3 _selftest.py              # 从本目录跑
  python3 prioritize.py --selftest   # 由 prioritize.py 转发到这里

不联网、不依赖第三方库、不依赖外部 fixture 文件（全部内嵌在 _fixtures.py）。
任何一条 FAIL 返回退出码 1，可直接当 CI 门禁。

覆盖八组断言：
  1. YAML 子集解析（flow map / 嵌套标量 / 计数）
  2. compute_priority：公式正确性 + 人工先验来源标注
  3. 拒排路径：severity 无权重 / fixability 无系数（F99）/ 分母不可用
  4. rank：排序正确性、rank 连续、top_recommendation 只有一个
  5. F99 混入 → rejected_candidates，不进 ranked
  6. 黄金池 override hook：解析、生效、rationale 注明、幂等
  7. 分母口径一致（resolve_total_badcases）+ 纯函数性（不改写 pack）
  8. v0.2 联调补充：override payload 权威键（taxonomy_id / cluster_id target）、
     mismatch 不许静默、govern 投影 3 的 system_top/human_top 透传、pack 身份不写死
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import prioritize as impl
from _fixtures import SELFTEST_CLUSTERS, SELFTEST_F99, SELFTEST_THRESHOLDS


def selftest() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        tp = td / "thresholds.yaml"
        tp.write_text(SELFTEST_THRESHOLDS, encoding="utf-8")
        thresholds = impl.load_thresholds(tp)

        p_cfg = thresholds["prioritize"]
        if p_cfg["severity_weights"] != {"3": 1.0, "2": 0.6, "1": 0.3}:
            failures.append(f"severity_weights 解析错: {p_cfg['severity_weights']}")
        if p_cfg["fixability_boost"]["F02"] != 1.2 or p_cfg["top_recommendation_count"] != 1:
            failures.append("fixability_boost / top_recommendation_count 解析错")

        # 2) compute_priority：公式正确性 + 人工先验来源标注
        f02 = next(c for c in SELFTEST_CLUSTERS if c["label"] == "F02")
        r = impl.compute_priority(f02, thresholds, 20)
        want = 7 / 20 * 0.6 * 1.2
        if not r["ok"] or abs(r["priority"] - want) > 1e-9:
            failures.append(f"F02 priority 错: {r.get('priority')} want {want}")
        if r.get("fixability_source") != impl.FIXABILITY_SOURCE:
            failures.append("缺 fixability 人工先验来源标注")

        # 3) 拒排路径
        r = impl.compute_priority(dict(f02, severity_max=5), thresholds, 20)
        if r["ok"] or "severity_weight 未在 pack 定义" not in r["reject_reason"]:
            failures.append("severity=5 应拒排")
        r = impl.compute_priority(SELFTEST_F99, thresholds, 20)
        if r["ok"] or "fixability_boost 未在 pack 定义" not in r["reject_reason"]:
            failures.append("F99 应拒排（不许自造系数）")
        if impl.compute_priority(f02, thresholds, 0)["ok"]:
            failures.append("分母 0 应拒排")

        # 4) rank：顺序 / rank 连续 / top 只一个
        res = impl.rank(SELFTEST_CLUSTERS, thresholds)
        if len(res["ranked"]) != 3:
            failures.append(f"ranked 数错: {len(res['ranked'])}")
        if [x["rank"] for x in res["ranked"]] != [1, 2, 3]:
            failures.append("rank 不连续")
        if res["top_recommendation_count"] != 1:
            failures.append("top_recommendation_count 应为 1")
        # F02: 0.35*0.6*1.2=0.252 ；F04: 0.15*1.0*0.8=0.12 ；F05: 0.5*0.3*0.6=0.09
        got_order = [x["category_id"] for x in res["ranked"]]
        if got_order != ["F02", "F04", "F05"]:
            failures.append(f"排序错: {got_order} want ['F02','F04','F05']")
        if res["top_recommendation"]["category_id"] != "F02":
            failures.append(f"top_recommendation 错: {res['top_recommendation']}")
        if not res["top_recommendation"]["one_line_reason"]:
            failures.append("one_line_reason 为空")

        # 5) F99 混入 → 只进 rejected_candidates
        res2 = impl.rank(SELFTEST_CLUSTERS + [SELFTEST_F99], thresholds)
        if any(x["category_id"] == "F99" for x in res2["ranked"]):
            failures.append("F99 不应进入 ranked")
        if not any(x["category_id"] == "F99" for x in res2["rejected_candidates"]):
            failures.append("F99 应进 rejected_candidates")

        # 6) override hook
        ov_path = td / "overrides.jsonl"
        ov_path.write_text("\n".join([
            json.dumps({"event_id": "ev-1", "event_type": "decision", "run_id": "r1",
                        "skill": "prioritize",
                        "payload": {"decision_kind": "override_priority", "actor": "pm-zhang",
                                    "target": "F04", "to_rank": 1,
                                    "rationale": "遗漏动作阻塞下单主链路"}}, ensure_ascii=False),
            # 非 decision / 非 override 的事件必须被忽略
            json.dumps({"event_id": "ev-2", "event_type": "checkpoint", "run_id": "r1",
                        "skill": "prioritize", "payload": {}}, ensure_ascii=False),
            json.dumps({"event_id": "ev-3", "event_type": "decision", "run_id": "r1",
                        "skill": "cluster",
                        "payload": {"decision_kind": "rename_cluster", "actor": "pm-zhang",
                                    "target": "F02"}}, ensure_ascii=False),
        ]) + "\n", encoding="utf-8")
        ovs = impl.load_overrides(ov_path)
        if len(ovs) != 1 or ovs[0]["target"] != "F04" or ovs[0]["to_rank"] != 1:
            failures.append(f"override 解析错（应只认 override_priority）: {ovs}")
        res3 = impl.rank(SELFTEST_CLUSTERS, thresholds, ovs)
        if [x["category_id"] for x in res3["ranked"]] != ["F04", "F02", "F05"]:
            failures.append(f"override 未生效: {[x['category_id'] for x in res3['ranked']]}")
        if res3["top_recommendation"]["category_id"] != "F04":
            failures.append("override 后 top_recommendation 应随之改变")
        f04 = next(x for x in res3["ranked"] if x["category_id"] == "F04")
        if "按历史人工决策修正" not in f04["rationale"]:
            failures.append("override 未写进 rationale")
        if not f04.get("override_applied"):
            failures.append("缺 override_applied 记录")
        res4 = impl.rank(SELFTEST_CLUSTERS, thresholds, ovs)
        if impl.sha256_obj(res3) != impl.sha256_obj(res4):
            failures.append("override 应用非幂等")

        # 7a) 分母口径：显式 > cluster_report.json > 累加
        cl_path = td / "clusters.json"
        cl_path.write_text(json.dumps({"clusters": SELFTEST_CLUSTERS}), encoding="utf-8")
        n, src = impl.resolve_total_badcases(cl_path, SELFTEST_CLUSTERS, None)
        if n != 20 or src != "sum_of_cluster_sizes":
            failures.append(f"无 cluster_report 时应累加得 20: got {n}/{src}")
        n, src = impl.resolve_total_badcases(cl_path, SELFTEST_CLUSTERS, 99)
        if n != 99 or src != "explicit_arg":
            failures.append(f"显式入参应优先: got {n}/{src}")
        (td / "cluster_report.json").write_text(json.dumps({"total_badcases": 25}),
                                               encoding="utf-8")
        n, src = impl.resolve_total_badcases(cl_path, SELFTEST_CLUSTERS, None)
        if n != 25 or "cluster_report" not in src:
            failures.append(f"应优先读 cluster_report.json 的 25: got {n}/{src}")
        # 这才是真实场景：F99 12 条不在 clusters 里，分母必须含它
        res5 = impl.rank(SELFTEST_CLUSTERS, thresholds, None, 25)
        got = {x["category_id"]: x["frequency"] for x in res5["ranked"]}
        if abs(got["F02"] - 7 / 25) > 1e-9:
            failures.append(f"frequency 未用 25 做分母: {got}")

        # 7b) 纯函数性：不得改写 pack
        before = tp.read_text(encoding="utf-8")
        impl.rank(SELFTEST_CLUSTERS, thresholds, ovs)
        if tp.read_text(encoding="utf-8") != before:
            failures.append("rank 改写了 pack 文件")

        # ------------------------------------------------------------------ #
        # 8) v0.2 联调补充：override payload 权威键 + 不静默 + pack 身份
        # ------------------------------------------------------------------ #

        # 8a) report 侧的完整事件形状：target=cluster_id（PM 语义），
        #     taxonomy_id=category_id（权威匹配键）+ govern 投影 3 要的两个 top。
        #     这是从卡到排序的端到端一条事件。
        full_path = td / "overrides_full.jsonl"
        full_path.write_text(json.dumps({
            "event_id": "evt-card-001", "event_type": "decision", "run_id": "r-full",
            "skill": "report",
            "payload": {"decision_kind": "override_priority", "actor": "pm-zhang",
                        "target": "CL-F04", "taxonomy_id": "F04", "to_rank": 1,
                        "system_top": "F02", "human_top": "F04",
                        "rationale": "遗漏动作阻塞下单主链路"}}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        full = impl.load_overrides(full_path)
        if len(full) != 1:
            failures.append(f"report 形状事件应被解析: {full}")
        else:
            e = full[0]
            if e["target"] != "CL-F04" or e["taxonomy_id"] != "F04":
                failures.append(f"target/taxonomy_id 解析错: {e}")
            if e["match_key"] != "F04" or e["match_key_source"] != "payload.taxonomy_id":
                failures.append(f"权威键应取 taxonomy_id: {e['match_key']}/{e['match_key_source']}")
            if (e["system_top"], e["human_top"]) != ("F02", "F04"):
                failures.append(f"system_top/human_top 未透传: {e}")
        res_full = impl.rank(SELFTEST_CLUSTERS, thresholds, full)
        if [x["category_id"] for x in res_full["ranked"]] != ["F04", "F02", "F05"]:
            failures.append("target=cluster_id + taxonomy_id=F04 未生效（这正是 v0.1 的失效点）")
        if res_full["top_recommendation"]["category_id"] != "F04":
            failures.append("override 后 top_recommendation 未跟着改")
        if not res_full["override_unmatched"] == []:
            failures.append(f"完整事件不该有未生效记录: {res_full['override_unmatched']}")

        # 8b) 缺 taxonomy_id 且 target 是 cluster_id → 必须进 unmatched，不许静默
        legacy_bad = td / "overrides_bad.jsonl"
        legacy_bad.write_text(json.dumps({
            "event_id": "evt-card-002", "event_type": "decision", "run_id": "r-full",
            "skill": "report",
            "payload": {"decision_kind": "override_priority", "actor": "pm-zhang",
                        "target": "CL-F04", "to_rank": 1,
                        "rationale": "老卡没写 taxonomy_id"}}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        res_bad = impl.rank(SELFTEST_CLUSTERS, thresholds, impl.load_overrides(legacy_bad))
        if [x["category_id"] for x in res_bad["ranked"]] != ["F02", "F04", "F05"]:
            failures.append("cluster_id target 不该被误匹配成 taxonomy id")
        un = res_bad["override_unmatched"]
        if len(un) != 1 or un[0]["reason"] != "override_payload_missing_taxonomy_id":
            failures.append(f"缺 taxonomy_id 必须记 unmatched: {un}")
        if un and (un[0].get("level") != "debug" or not un[0].get("detail")):
            failures.append("unmatched 记录缺 debug 级说明")

        # 8c) v0.1 兼容路径：target 直接写 F\d\d 仍然生效
        compat = td / "overrides_compat.jsonl"
        compat.write_text(json.dumps({
            "event_id": "evt-v01", "event_type": "decision", "run_id": "r-v01",
            "skill": "prioritize",
            "payload": {"decision_kind": "override_priority", "actor": "pm-qianfu",
                        "target": "F05", "to_rank": 1,
                        "rationale": "v0.1 遗物：target 直接写 taxonomy id"}},
            ensure_ascii=False) + "\n", encoding="utf-8")
        cv = impl.load_overrides(compat)
        if cv[0]["match_key"] != "F05" or cv[0]["match_key_source"] != "payload.target(legacy)":
            failures.append(f"v0.1 兼容路径未命中: {cv}")
        res_c = impl.rank(SELFTEST_CLUSTERS, thresholds, cv)
        if [x["category_id"] for x in res_c["ranked"]] != ["F05", "F02", "F04"]:
            failures.append(f"v0.1 兼容路径未生效: {[x['category_id'] for x in res_c['ranked']]}")

        # 8d) taxonomy_id 有值但本轮没这个类 → unmatched（可能已修好/进了 F99）
        gone = td / "overrides_gone.jsonl"
        gone.write_text(json.dumps({
            "event_id": "evt-gone", "event_type": "decision", "run_id": "r-full",
            "skill": "report",
            "payload": {"decision_kind": "override_priority", "actor": "pm-zhang",
                        "target": "CL-F07", "taxonomy_id": "F07", "to_rank": 1,
                        "system_top": "F02", "human_top": "F07",
                        "rationale": "上一轮的类，这一轮没了"}}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        res_g = impl.rank(SELFTEST_CLUSTERS, thresholds, impl.load_overrides(gone))
        if [x["category_id"] for x in res_g["ranked"]] != ["F02", "F04", "F05"]:
            failures.append("不可匹配的 override 不该改动排序")
        if res_g["override_unmatched"][0]["reason"] != "taxonomy_id_not_in_this_round":
            failures.append(f"类不在本轮应记 unmatched: {res_g['override_unmatched']}")

        # 8e) apply_overrides 返回三元组（新列表, 生效, 未生效），缺键事件全部进 unmatched
        base_ranked = impl.rank(SELFTEST_CLUSTERS, thresholds)["ranked"]
        r3, ap3, un3 = impl.apply_overrides(list(base_ranked), [
            {"event_id": "e-mk", "target": "CL-F02", "taxonomy_id": None,
             "match_key": None, "match_key_source": "missing", "to_rank": 1}])
        if ap3 or len(un3) != 1:
            failures.append(f"缺键事件应全部进 unmatched: applied={ap3} unmatched={un3}")
        elif [x["category_id"] for x in r3] != ["F02", "F04", "F05"]:
            failures.append("缺键事件不该改动排序")

        # 8f) pack 身份绑定：--pack 目录 > 显式传参 > UNKNOWN，绝不写死 FROZEN
        pack_copy = td / "mypack"
        pack_copy.mkdir()
        (pack_copy / "thresholds.yaml").write_text("prioritize: {}\n", encoding="utf-8")
        # frozen_hash 必须等于现场内容 hash（用权威算法算，不能手写一个假值）
        freeze = impl.load_pack_freeze()
        frozen = "0" * 64
        if freeze is not None:
            freeze.PACK_DIR = pack_copy  # 权威算法按目录算，必须先指向待测 pack
            frozen = freeze.compute_hash()
        else:
            failures.append("取不到 tools/pack_freeze.py 的 compute_hash")
        (pack_copy / "pack.manifest.json").write_text(json.dumps(
            {"pack_id": "mypack-x", "version": "9.9.9", "state": "FROZEN",
             "frozen_hash": frozen}, ensure_ascii=False), encoding="utf-8")
        b = impl.resolve_pack_binding(pack_copy, None)
        if b["pack"]["pack_id"] != "mypack-x" or b["pack"]["version"] != "9.9.9":
            failures.append(f"--pack 应读 manifest 的 pack_id/version: {b['pack']}")
        if b["pack"]["state_at_run"] != "FROZEN" or b["pack"]["pack_bound"] is not True:
            failures.append(f"--pack 应有 state_at_run 与 pack_bound=true: {b['pack']}")
        if b["pack"]["pack_hash"] != frozen:
            failures.append(f"--pack 未传 --pack-hash 时应取 manifest frozen_hash: {b['pack']}")
        if b["flags"]:
            failures.append(f"FROZEN + hash 齐全时不该有 flag: {b['flags']}")

        draft = td / "draftpack"
        draft.mkdir()
        (draft / "pack.manifest.json").write_text(json.dumps(
            {"pack_id": "mypack-draft", "version": "0.2.0", "state": "DRAFT"},
            ensure_ascii=False), encoding="utf-8")
        (draft / "thresholds.yaml").write_text("prioritize: {}\n", encoding="utf-8")
        bd = impl.resolve_pack_binding(draft, "deadbeef")
        if bd["pack"]["state_at_run"] != "DRAFT":
            failures.append(f"DRAFT pack 必须如实标 DRAFT（v0.1 在此写死 FROZEN）: {bd['pack']}")
        if impl.FLAG_PACK_NOT_FROZEN not in bd["flags"]:
            failures.append(f"DRAFT 应带 pack_not_frozen flag: {bd['flags']}")

        # --pack 给了但 manifest 没写 state：不是"没有 pack"，是"这份 manifest 缺字段"，
        # 统一写 UNKNOWN（下游只需认一个 unknown 词），仍然 pack_bound=true
        nostate = td / "nostatepack"
        nostate.mkdir()
        (nostate / "pack.manifest.json").write_text(json.dumps(
            {"pack_id": "p-nostate", "version": "1.0.0"}, ensure_ascii=False), encoding="utf-8")
        bns = impl.resolve_pack_binding(nostate, None)
        if bns["pack"]["state_at_run"] != impl.STATE_UNKNOWN:
            failures.append(f"manifest 缺 state 时应写 {impl.STATE_UNKNOWN}: {bns['pack']}")
        if bns["pack"]["pack_bound"] is not True:
            failures.append(f"有 manifest 就应 pack_bound=true: {bns['pack']}")
        if impl.FLAG_PACK_NOT_FROZEN not in bns["flags"]:
            failures.append(f"manifest 缺 state 应带 pack_not_frozen: {bns['flags']}")

        # FROZEN 但 frozen_hash 与现场不符 → 显式失败，不带着假 hash 往下跑
        tampered = td / "tamperedpack"
        tampered.mkdir()
        (tampered / "pack.manifest.json").write_text(json.dumps(
            {"pack_id": "p", "version": "1", "state": "FROZEN", "frozen_hash": "b" * 64},
            ensure_ascii=False), encoding="utf-8")
        (tampered / "thresholds.yaml").write_text("prioritize: {}\n", encoding="utf-8")
        try:
            impl.resolve_pack_binding(tampered, None)
            failures.append("FROZEN 且 hash 不符时必须抛错")
        except ValueError:
            pass

        # 无 --pack：不许冒充 FROZEN，必须自报未绑定（传了 --pack-hash 也一样 ——
        # 知道一个 hash 不等于知道它对应哪份 pack、那份 pack 是不是 FROZEN）
        bn = impl.resolve_pack_binding(None, "h" * 64, "retail-default", "0.1.0")
        if bn["pack"]["state_at_run"] != impl.STATE_UNKNOWN:
            failures.append(f"无 manifest 时 state_at_run 应为 {impl.STATE_UNKNOWN}: {bn['pack']}")
        if bn["pack"]["pack_bound"] is not False:
            failures.append(f"无 manifest 时 pack_bound 应为 false: {bn['pack']}")
        if impl.FLAG_PACK_UNBOUND not in bn["flags"]:
            failures.append(f"无 manifest 应带 {impl.FLAG_PACK_UNBOUND}: {bn['flags']}")
        if bn["pack"]["pack_hash"] != "h" * 64:
            failures.append("显式传的 pack_hash 应如实保留")

        # --pack 给了但目录里没有 manifest → 显式失败，不退化成写死常量
        try:
            impl.resolve_pack_binding(td / "no_such_pack", None)
            failures.append("--pack 缺 pack.manifest.json 时必须抛错")
        except ValueError:
            pass

    if failures:
        print("[prioritize --selftest] FAIL")
        for f in failures:
            print("  -", f)
        return 1
    print("[prioritize --selftest] PASS — 3 簇排序 F02>F04>F05，F99 拒排，"
          "override hook 生效（含 taxonomy_id 权威键 / cluster_id target mismatch 不静默 / "
          "govern 投影字段透传），分母口径一致，pack 身份不写死")
    return 0


if __name__ == "__main__":
    sys.exit(selftest())
