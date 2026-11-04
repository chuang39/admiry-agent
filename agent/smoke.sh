#!/usr/bin/env bash
# Smoke-test a running agent service: health, docs, the browser pages, the assistant skill, and with AGENT_KEY the public tools and get_marketing_state.
#   bash agent/smoke.sh                       # http://localhost:8097, one host
#   AGENT_KEY=ak_... bash agent/smoke.sh https://agent.dev.adx.admiry.ai https://home.dev.adx.admiry.ai
#   AGENT_KEY=ak_... bash agent/smoke.sh https://agent.admiry.ai https://admiry.ai
# $1 BASE = AGENT_PUBLIC_BASE_URL: /health, the connector docs, the Stripe hook, /mcp.
# $2 WEB  = AGENT_WEB_BASE_URL (default BASE): the browser pages. When it differs, BASE must 302 them there
# (spec 2026-09-29 §4), so no single base passes on a split box.
set -euo pipefail
BASE="${1:-http://localhost:8097}"
WEB="${2:-$BASE}"
H=(-H "Accept: application/json, text/event-stream" -H "Content-Type: application/json")
trap 'echo "smoke FAILED at: $BASH_COMMAND" >&2' ERR   # set -e exits on the first failing check; say which

echo "== health";  curl -sf "$BASE/health" | grep '"ok":true' >/dev/null; echo ok
echo "== docs";    curl -sf "$BASE/connect/docs" | grep "$BASE/mcp" >/dev/null; echo ok
if [ "$WEB" != "$BASE" ]; then
  echo "== $BASE/connect -> $WEB/connect"; hop=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "$BASE/connect"); [ "$hop" = "302 $WEB/connect" ]; echo ok
fi
echo "== connect"; tmp=$(mktemp); code=$(curl -s -o "$tmp" -w '%{http_code}' "$WEB/connect"); [ "$code" = "200" ]; grep -qi "Sign in" "$tmp"; rm -f "$tmp"; echo ok
echo "== agreement"; tmp=$(mktemp); code=$(curl -s -o "$tmp" -w '%{http_code}' "$WEB/legal/management-agreement"); [ "$code" = "200" ]; grep -q "Google Ads Management Agreement" "$tmp"; grep -q 'content="noindex"' "$tmp"; rm -f "$tmp"; echo ok
echo "== addendum"; tmp=$(mktemp); code=$(curl -s -o "$tmp" -w '%{http_code}' "$WEB/legal/prepayment-addendum"); [ "$code" = "200" ]; grep -q "Prepayment Addendum" "$tmp"; grep -q 'content="noindex"' "$tmp"; rm -f "$tmp"; echo ok
echo "== llms";    curl -sf "$BASE/connect/llms.txt" | grep "^MCP: $BASE/mcp" >/dev/null; echo ok
echo "== skill";   curl -sf "$BASE/connect/skill.md" | grep '^name: admiry-marketing$' >/dev/null; echo ok
echo "== plan page 404"; code=$(curl -s -o /dev/null -w '%{http_code}' "$WEB/plan/nope"); [ "$code" = "404" ]; echo ok
echo "== old pay link → the conversation"; got=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "$WEB/pay/biz_smoke"); [ "$got" = "303 $WEB/?setup=biz_smoke" ]; echo ok
echo "== stripe hook refuses unsigned"; code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{}' "$BASE/hooks/stripe"); [ "$code" = "400" ] || [ "$code" = "503" ]; echo "ok ($code)"
echo "== a tool call without a key (expect 401)"
code=$(curl -s -o /dev/null -w '%{http_code}' "${H[@]}" -X POST "$BASE/mcp" -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_marketing_state","arguments":{}}}')
[ "$code" = "401" ]; echo "ok ($code)"
# every /mcp call needs a bearer (A1b-4b Task 2), tools/list included -- so the checks below need AGENT_KEY
if [ -n "${AGENT_KEY:-}" ]; then
  echo "== tools/list"
  curl -sf "${H[@]}" -H "Authorization: Bearer $AGENT_KEY" -X POST "$BASE/mcp" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
    | python3 -c 'import json,sys; names=[t["name"] for t in json.load(sys.stdin)["result"]["tools"]]; assert {"get_marketing_state","create_marketing_plan","update_marketing_plan","get_marketing_plan","launch_marketing_plan","get_campaigns","get_leads","contact_support"} <= set(names) and "whoami" not in names, names; print("ok", len(names), "tools")'
  echo "== get_marketing_state with AGENT_KEY"
  curl -sf "${H[@]}" -H "Authorization: Bearer $AGENT_KEY" -X POST "$BASE/mcp" \
    -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_marketing_state","arguments":{}}}' \
    | python3 -c 'import json,sys; r=json.load(sys.stdin)["result"]; assert not r["isError"], r; d=r["structuredContent"]["data"]; print("ok", d["caller"]["class"], d["surface_version"])'
  echo "== a retired name is answered with its replacement"
  curl -sf "${H[@]}" -H "Authorization: Bearer $AGENT_KEY" -X POST "$BASE/mcp" \
    -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"whoami","arguments":{}}}' \
    | python3 -c 'import json,sys; r=json.load(sys.stdin)["result"]; assert r["isError"] and "get_marketing_state" in r["content"][0]["text"], r; print("ok")'
fi
echo "smoke passed"
