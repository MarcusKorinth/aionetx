# Benchmark Harnesses

This directory contains small local measurement tools for contributors. They
are not part of the public API, do not define performance guarantees, and
should not be used as CI thresholds.

## TCP Event Delivery Baseline

Run the TCP event-delivery baseline before and after changes that affect event
dispatch, receive loops, queueing, or backpressure behavior:

```bash
python bench/event_delivery_baseline.py --json
```

The script starts a local loopback TCP server, sends a configurable number of
fixed-size payloads, and reports local context plus observed metrics:

- Python version, implementation, platform, and event-loop class
- dispatch mode and backpressure policy
- payload count, payload size, and queue capacity
- elapsed time and throughput
- received bytes and payload event count
- dispatcher counters, including queue peak and backpressure drops

Example commands:

```bash
python bench/event_delivery_baseline.py
python bench/event_delivery_baseline.py --payload-count 5000 --payload-size 512 --json
python bench/event_delivery_baseline.py --backpressure-policy drop_newest --max-pending-events 8 --handler-delay-seconds 0.001
```

Use the output for same-machine before/after comparisons. Results depend on
the local OS, Python build, event-loop policy, CPU load, and transport settings.
Do not compare the numbers as cross-platform claims.
