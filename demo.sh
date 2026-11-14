#!/usr/bin/env bash
# 本地（非 Docker）一键演示：建临时库 -> 启动服务 -> 上传样例 -> 追溯查询。
set -euo pipefail

cd "$(dirname "$0")"
DB_DIR="$(pwd)/.local-data"
mkdir -p "$DB_DIR"
export TRACE_DB_PATH="$DB_DIR/demo_trace.db"
rm -f "$TRACE_DB_PATH" "$TRACE_DB_PATH-wal" "$TRACE_DB_PATH-shm"

PYTHON="${PYTHON:-python3}"
PORT="${PORT:-8000}"

echo ">> 启动 API: http://127.0.0.1:$PORT  (文档 /docs, 演示指引 /help)"
"$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT

for _ in $(seq 1 40); do
  curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && break
  sleep 0.5
done

echo
echo ">> 1) 上传合法事件日志"
curl -s -X POST "http://127.0.0.1:$PORT/uploads" \
  -F "file=@samples/events_valid.csv;type=text/csv"
echo; echo
echo ">> 2) 09:30 追溯缺陷批次 B20261101（命中 P-1001，多级事件路径）"
curl -s "http://127.0.0.1:$PORT/trace?child_part_no=EC-100&batch=B20261101&at_time=2026-11-01T09:30:00Z"
echo; echo
echo ">> 3) 10:30 拆出后再次追溯（不再命中）"
curl -s "http://127.0.0.1:$PORT/trace?child_part_no=EC-100&batch=B20261101&at_time=2026-11-01T10:30:00Z"
echo; echo
echo ">> 4) 同内容重传（幂等，0 新增）"
curl -s -X POST "http://127.0.0.1:$PORT/uploads" \
  -F "file=@samples/events_valid.csv;type=text/csv"
echo; echo
echo ">> 5) 结构非法上传（整份拒收，HTTP 422，旧数据不变）"
curl -s -X POST "http://127.0.0.1:$PORT/uploads" \
  -F "file=@samples/invalid_two_parents.csv;type=text/csv"
echo; echo
echo ">> 6) 歧义：同型号 SA-600 两批次分别装在两台成品，板件批次无法唯一归属"
curl -s -X POST "http://127.0.0.1:$PORT/uploads" \
  -F "file=@samples/ambiguous_same_part_two_products.csv;type=text/csv" >/dev/null
curl -s "http://127.0.0.1:$PORT/trace?child_part_no=EC-600&batch=B600&at_time=2026-11-06T09:30:00Z"
echo; echo
echo ">> 7) 拆出后按查询时刻重判：LOT-B 拆出后只剩 P-6001 确定受影响"
curl -s -X POST "http://127.0.0.1:$PORT/uploads" \
  -F "file=@samples/ambiguity_remove_later.csv;type=text/csv" >/dev/null
curl -s "http://127.0.0.1:$PORT/trace?child_part_no=EC-600&batch=B600&at_time=2026-11-06T12:00:00Z"
echo; echo
echo ">> 8) 汇合：SA-601 两批次都装进同一成品 P-6010，仍为确定受影响"
curl -s -X POST "http://127.0.0.1:$PORT/uploads" \
  -F "file=@samples/convergent_same_product.csv;type=text/csv" >/dev/null
curl -s "http://127.0.0.1:$PORT/trace?child_part_no=EC-601&batch=B601&at_time=2026-11-07T09:30:00Z"
echo; echo
echo ">> 9) 从未装入的批次：batch_found=false，空结果（不误报）"
curl -s "http://127.0.0.1:$PORT/trace?child_part_no=EC-600&batch=B-NOT-IN-LOG&at_time=2026-11-06T09:30:00Z"
echo; echo
echo ">> 浏览器打开 http://127.0.0.1:$PORT/docs 可交互演示；Ctrl-C 退出。"
wait $SERVER_PID
