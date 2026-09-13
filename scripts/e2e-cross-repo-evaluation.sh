#!/usr/bin/env bash
# 跨仓初筛 E2E(评审发布闸门 10,issue #168)——半自动,手工触发,不进 CI。
#
# 串三仓:Frontend 发起 → Backend 代理 → Agent 建 job → 状态 6 → 评分/题库
# → 状态回落 2 → 前端轮询。
#
# 前置:三仓本地服务已起(Backend: MySQL/Redis/RabbitMQ;Agent: PG + 模型 key)。
# 用法:
#   export BACKEND_URL=http://localhost:8080
#   export AGENT_TOKEN=<管理员 JWT>          # 需 resume:audit + evaluation:run
#   export CYCLE_ID=2026
#   ./scripts/e2e-cross-repo-evaluation.sh <resume_id>
#
# 退出码:0 全过;非 0 表示有场景失败(逐项打印)。
set -uo pipefail

BACKEND_URL="${BACKEND_URL:-http://localhost:8080}"
AGENT_TOKEN="${AGENT_TOKEN:?需要管理员 JWT(resume:audit + evaluation:run)}"
CYCLE_ID="${CYCLE_ID:?需要 CYCLE_ID(如 2026)}"
RESUME_ID="${1:?用法: $0 <resume_id>}"

PASS=0
FAIL=0
ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad()  { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

api() {  # api <method> <path> [json_body]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sS -X "$method" "$BACKEND_URL$path" \
      -H "Authorization: Bearer $AGENT_TOKEN" \
      -H "Content-Type: application/json" -d "$body"
  else
    curl -sS -X "$method" "$BACKEND_URL$path" \
      -H "Authorization: Bearer $AGENT_TOKEN"
  fi
}

echo "== 跨仓初筛 E2E:resume=$RESUME_ID cycle=$CYCLE_ID backend=$BACKEND_URL =="

# ── 场景 1:重复点击提交 → 只产生一个活跃 job(闸门2)──
echo "[1] 重复提交幂等"
A=$(api POST "/api/admin/agent/evaluation/run" \
  "{\"cycle_id\":$CYCLE_ID,\"items\":[{\"resume_id\":$RESUME_ID}]}")
B=$(api POST "/api/admin/agent/evaluation/run" \
  "{\"cycle_id\":$CYCLE_ID,\"items\":[{\"resume_id\":$RESUME_ID}]}")
ID_A=$(echo "$A" | jq -r '.data.job_ids[0] // empty')
ID_B=$(echo "$B" | jq -r '.data.job_ids[0] // empty')
if [ -n "$ID_A" ] && [ "$ID_A" = "$ID_B" ]; then
  ok "重复提交返回同一活跃 job($ID_A)"
else
  bad "重复提交产生不同 job(A=$ID_A B=$ID_B)"
fi

# ── 场景 2:处理中状态 6 可见,且终态后回落(闸门4)──
echo "[2] 处理中状态 6 → 终态回落"
STATUS=$(api GET "/api/resumes/admin/by-resume/$RESUME_ID" | jq -r '.data.status // empty')
# 状态 6 是瞬态,job 可能已完成;两次采样,宽松判定
sleep 2
STATUS2=$(api GET "/api/resumes/admin/by-resume/$RESUME_ID" | jq -r '.data.status // empty')
echo "  (status 采样: $STATUS → $STATUS2)"
if [ "$STATUS" = "6" ] || [ "$STATUS2" = "6" ] || [ "$STATUS2" = "2" ]; then
  ok "状态链符合预期(6 瞬态或已回落 2)"
else
  bad "状态异常:$STATUS → $STATUS2"
fi

# ── 场景 3:轮询 job 到终态(闸门4 前端逻辑的服务端对应)──
echo "[3] 轮询 job 至终态"
for _ in $(seq 1 60); do
  ST=$(api GET "/api/admin/agent/evaluation/jobs?cycleId=$CYCLE_ID" \
    | jq -r --arg rid "$RESUME_ID" \
      '.data.items[] | select(.resume_id == ($rid|tonumber)) | .status' | head -1)
  [ "$ST" = "succeeded" ] || [ "$ST" = "failed" ] && break
  sleep 5
done
if [ "$ST" = "succeeded" ]; then
  ok "job 终态 succeeded"
elif [ "$ST" = "failed" ]; then
  bad "job 终态 failed(查看 error 字段)"
else
  bad "轮询超时,未有终态(当前 $ST)"
fi

# ── 场景 4:题库状态可观测(闸门6)──
echo "[4] qbank_status 可见"
QB=$(api GET "/api/admin/agent/evaluation/jobs?cycleId=$CYCLE_ID" \
  | jq -r --arg rid "$RESUME_ID" \
    '.data.items[] | select(.resume_id == ($rid|tonumber)) | .qbank_status' | head -1)
case "$QB" in
  succeeded) ok "题库落库成功" ;;
  failed)    ok "题库失败但 job 可见 qbank_status=failed(部分成功语义生效)" ;;
  skipped)   bad "题库被跳过(评分阶段可能失败)" ;;
  *)         bad "qbank_status 缺失:$QB" ;;
esac

# ── 场景 5:Backend 不可用 → Agent job 失败(手工场景,提示)──
echo "[5] Backend 不可用(手工)"
echo "  NOTE: 停掉 Backend 后重复 [1]-[4],预期 job 失败、attempts 递增、"
echo "        达 _MAX_ATTEMPTS=3 后不再自动重排(转人工)。"

echo "== 结果:PASS=$PASS FAIL=$FAIL =="
[ "$FAIL" -eq 0 ] || exit 1
