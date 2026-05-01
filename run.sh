#!/bin/bash
while true; do
    echo "$(date) — Starting bot..."
    python main.py
    echo "$(date) — Bot stopped. Restarting in 5 seconds..."
    sleep 5
done
