#!/bin/bash
while true; do
    echo "$(date) — Starting bot..."
    python main.py
    EXIT_CODE=$?
    echo "$(date) — Bot stopped (exit=$EXIT_CODE). Restarting in 15 seconds..."
    sleep 15
done
