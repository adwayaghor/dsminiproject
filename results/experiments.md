# Measured experiment results

Generated 2026-10-21 18:05:10 UTC — mode: docker-compose — Linux 6.18.44-fc-v139, Python 3.13.16

## Scenarios

| Scenario | Passed | Steps | ms |
|---|---|---|---|
| A · Normal operation: report, replicate, verify | True | 6/6 | 821.3 |
| B · Network partition: Alpha isolated, local writes continue | True | 7/7 | 595.5 |
| C · Network recovery: reconnect Alpha and converge | True | 5/5 | 1404.1 |
| D · Node crash and restart: bravo | True | 10/10 | 4020.7 |
| E · Concurrent updates: deterministic conflict resolution | True | 6/6 | 1457.5 |

## Sync time vs records (median)

| Records | Runs | Median ms | Min | Max |
|---|---|---|---|---|
| 1 | 5 | 572.0 | 513.2 | 684.9 |
| 10 | 5 | 613.3 | 501.3 | 639.0 |
| 50 | 5 | 689.8 | 573.7 | 798.5 |
| 100 | 5 | 749.5 | 685.9 | 755.1 |
| 250 | 2 | 890.0 | 767.2 | 1012.9 |

## Partition recovery

```json
{
  "records_created_while_isolated": 20,
  "sync_attempts_during_partition": 12,
  "recovery_sync_ms": 532.4,
  "rounds": 2,
  "converged_after": true,
  "wall_ms": 550.2
}
```

## Crash and restart

```json
{
  "records_before_crash": 10,
  "records_created_during_outage": 15,
  "detected_down_after_s": 0.0,
  "container_restart_s": 0.3,
  "records_surviving_in_bravo_volume": 10,
  "failed_attempts_to_bravo_during_outage": 6,
  "catchup_sync_ms": 668.1,
  "records_after_catchup": 25,
  "converged_after": true
}
```

## Communication attempts during the crash-and-restart experiment

```json
{
  "success": 60,
  "failed": 6,
  "skipped_partition": 0,
  "other": 0,
  "by_outcome": {
    "success": 60,
    "unreachable": 6
  }
}
```

Medians over repeated runs. Times include sequential HTTP round trips between 4 stations on one machine plus fsync'd SQLite commits (synchronous=FULL); they are not WAN figures.
