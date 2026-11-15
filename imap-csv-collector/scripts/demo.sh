#!/usr/bin/env bash
# 端到端演示：docker compose up -d 后执行本脚本。
# 场景覆盖：
#   1. 多附件坏邮件 -> 停在该邮件、不留部分结果、返回邮件标识
#   2. 移除坏邮件后续采 -> 断点继续、无新邮件即完成
#   3. 并发投递多封不同 Message-ID 的仪器 CSV -> 每封独立 UID、各入库一次
#   4. 从 /deliveries 与 /results 追溯采集结果和邮件来源
#   5. 原样重投 -> Message-ID + 附件摘要识别，不重复入库
#   6. 邮箱重建（UIDVALIDITY 改变、UID 重排）-> 整箱重扫，重投全部跳过
#   7. 同 Message-ID 但附件被改 -> 停采待核实，原结果保留
# 用法： bash scripts/demo.sh
set -euo pipefail

API="${API:-http://localhost:8000}"
CTL=(docker compose run --rm mailctl)

collect() {
  curl -sS -X POST "$API/collect" -H 'Content-Type: application/json' -d '{
    "host": "imap", "port": 10143, "folder": "INBOX",
    "username": "instrument@lab.local", "password": "labpass",
    "use_ssl": false
  }'
}

show() { python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin), ensure_ascii=False, indent=2))"; }
line() { printf '\n========== %s ==========\n' "$1"; }

line "1. 首轮采集：停在 UID3（两个 CSV 之一损坏），前两封共 5 条记录入库"
collect | show

line "2. 按样品查询 S-1001；坏邮件里的 S-1005 未部分入库（404）"
curl -sS "$API/results/S-1001" | show
curl -sS -o /dev/null -w 'GET /results/S-1005 -> HTTP %{http_code}\n' "$API/results/S-1005"

line "3. 采集进度 last_uid=2，重启后从 UID3 续采"
curl -sS "$API/ingest/state" | show

line "4. 运维移除坏邮件 UID3，再次采集：无新邮件，正常完成"
"${CTL[@]}" list
docker compose exec -T imap rm -f /srv/mail/INBOX/3.eml
collect | show

line "5. 并发投递：3 个进程同时各投 5 封不同 Message-ID 的仪器 CSV"
# 三个 mailctl 进程共享同一邮箱卷，各自再并发写 5 封：
# 若 UID 分配不是原子的，会出现同一 UID 被后写邮件覆盖。
docker compose run --rm mailctl deliver-csv \
  --count 5 --batch demo-a --sample-base 2001 &
docker compose run --rm mailctl deliver-csv \
  --count 5 --batch demo-b --sample-base 2101 &
docker compose run --rm mailctl deliver-csv \
  --count 5 --batch demo-c --sample-base 2201 &
wait
"${CTL[@]}" list

line "6. 采集并发投递的 15 封：每封独立 UID，各入库一次"
collect | show

line "7. 从 /docs 提供的接口追溯结果与邮件来源"
curl -sS "$API/results/S-2001" | show
curl -sS "$API/deliveries/demo-a-001@lab-instrument" | show
curl -sS "$API/deliveries" | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print('已入库邮件数:', d['count']); \
   [print(' ', x['source']['message_id'], 'UID', x['source']['uid'], '->', x['subject']) for x in d['deliveries'][-5:]]"

line "8. 原样重投 UID1 后采集：同一投递，跳过且不重复入库"
"${CTL[@]}" redeliver 1
collect | show

line "9. 邮箱重建（UIDVALIDITY 改变、UID 从 1 重排，内容不变）"
"${CTL[@]}" rebuild
"${CTL[@]}" list
line "9a. 重建后采集：整箱重扫，全部按重投跳过，零新增"
collect | show

line "10. 同 Message-ID 但附件被修改后重投：停采待核实，原结果保留"
cat > /tmp/changed.csv <<'CSV'
样品编号,检测项目,结果,单位
S-1001,铅Pb,9.999,mg/kg
CSV
# 把宿主机 CSV 挂进 mailctl 容器后再执行修改重投
docker compose run --rm -v /tmp/changed.csv:/tmp/changed.csv mailctl \
  redeliver-modified 1 \
  --attachment batch_b1.csv --csv-path /tmp/changed.csv
collect | show

line "11. 复查 S-1001：仍为原始结果，伪造值 9.999 未入库"
curl -sS "$API/results/S-1001" | show

line "演示完成（可用 curl -X POST $API/admin/reset 重置结果库）"
