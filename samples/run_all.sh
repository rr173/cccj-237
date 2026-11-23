#!/usr/bin/env bash
# 可再跑的批处理：清掉旧产物，对全部样例重跑一遍。
# 用法：bash samples/run_all.sh
set -u
cd "$(dirname "$0")/.."
rm -rf out
for f in samples/flow_main.json samples/flow_legacy.csv \
         samples/flow_breakpoints.json samples/flow_seq_conflict.json \
         samples/flow_million.json; do
    name="$(basename "$f")"
    echo "================================================================"
    echo ">> $f"
    python3 -I run_slicer.py run --input "$f" --out-dir "out/${name%.*}" || true
done
for limit in 1 2; do
    echo "================================================================"
    echo ">> handover_dispute (publish-limit $limit)"
    python3 -I run_slicer.py handover \
        --flow samples/handover_dispute_flow.json \
        --handover samples/handover_dispute.json \
        --publish-limit "$limit" --out "out/handover_dispute_${limit}.json" || true
done
echo "================================================================"
echo ">> handover_main (publish-limit 4)"
python3 -I run_slicer.py handover \
    --flow samples/handover_flow.json --handover samples/handover_main.json \
    --publish-limit 4 --out out/handover_main.json || true
echo "================================================================"
echo ">> billing (period [3,12))"
python3 -I run_slicer.py billing \
    --flow samples/billing_flow.json --prices samples/billing_prices.json \
    --start 3 --end 12 --out out/billing.json || true
echo
echo "全部产物见 out/ ；断点样例退出码为 1 属预期（errors.json 内含断点说明）。"
