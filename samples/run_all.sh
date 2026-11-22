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
echo
echo "全部产物见 out/ ；断点样例退出码为 1 属预期（errors.json 内含断点说明）。"
