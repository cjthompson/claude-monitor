#!/bin/bash
while true; do
    claude-monitor
    echo "Restarting..."
    sleep 0.5
done
