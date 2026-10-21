"""Start 4 local station processes, run scenarios A-E, print results. No Docker needed."""
import json
import sys

sys.path.insert(0, __file__.rsplit("scripts", 1)[0])
from dcn import scenarios
from dcn.controllers import LocalCluster


def main(keys):
    lc = LocalCluster()
    lc.start_all()
    try:
        for k in keys:
            res = scenarios.run(k, lc.cluster, lc)
            print(f"\n=== Scenario {res['key']}: {res['title']}  -> {'PASS' if res['passed'] else 'FAIL'} "
                  f"({res['duration_ms']} ms)")
            for s in res["steps"]:
                print(f"  [{'ok' if s['ok'] else 'XX'}] {s['text']}  {s['detail']}")
    finally:
        lc.stop_all()
        print("\ndata dir:", lc.data_dir)


if __name__ == "__main__":
    main(sys.argv[1:] or list("ABCDE"))
