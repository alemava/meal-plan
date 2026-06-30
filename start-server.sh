#!/bin/bash
cd "$(dirname "$0")"

# Start local HTTP server on port 8000 (serve.py sends no-cache for sw.js so
# iOS Safari picks up SW version bumps reliably)
python3 serve.py 8000 &
PY_PID=$!
echo "✓ HTTP server → http://localhost:8000 (PID $PY_PID)"

# Start ngrok tunnel if ~/ngrok exists
if [ -f ~/ngrok ]; then
  ~/ngrok http --url=regroup-affluent-bunkhouse.ngrok-free.dev 8000 > /tmp/ngrok.log 2>&1 &
  NGROK_PID=$!
  sleep 2
  echo "✓ ngrok tunnel → https://regroup-affluent-bunkhouse.ngrok-free.dev (PID $NGROK_PID)"
else
  echo "⚠ ~/ngrok not found — phone tunnel not started, only localhost available"
fi

echo ""
echo "Press Ctrl+C to stop everything"
trap "kill $PY_PID $NGROK_PID 2>/dev/null; exit" INT TERM
wait $PY_PID
