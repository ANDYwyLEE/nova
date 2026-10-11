#!/bin/bash

# 1. Set Search Pattern
# Matches 'python' (case-insensitive) and 'nova.py' in the same line.
# This covers 'Python' on Darwin and 'python3.exe' on Windows.
SEARCH_PATTERN="python.*nova\.py"

# 2. Extract PIDs
# -i: Ignore case (crucial for Darwin's 'Python' vs 'python')
# -E: Use extended regular expressions
PIDS=$(ps -ef | grep -iE "$SEARCH_PATTERN" | grep -v "grep" | awk '{print $2}')

SIGNAL="-2" # SIGINT (Equivalent to Ctrl+C, allows for clean cleanup)

if [ -z "$PIDS" ]; then
    echo "No Nova processes found running."
    exit 0
fi

echo "Found PIDs: $PIDS"
echo "Sending signal $SIGNAL to stop processes..."

for pid in $PIDS
do
    # Validate that PID is a number
    if [[ $pid =~ ^[0-9]+$ ]]; then
        if kill -0 "$pid" 2>/dev/null; then
            echo "Stopping PID $pid..."
            kill "$SIGNAL" "$pid"
        fi
    fi
done

# 3. Final Verification
sleep 1
if ! ps -ef | grep -iE "$SEARCH_PATTERN" | grep -v "grep" > /dev/null; then
    echo "✅ All Nova processes have been stopped successfully."
else
    echo "⚠️ Some processes are still active. You might need 'kill -9'."
fi
