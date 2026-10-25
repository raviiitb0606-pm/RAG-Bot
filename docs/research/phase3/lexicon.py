import re, sys, yaml
sys.path.insert(0, sys.argv[1])
from queries import *
UNSUPPORTED = re.compile(r"""\b(
  lock[\s-]?in | launch(ed)? | inception | (groww\s+)?rating | rated |
  statement | kyc | nomin(ee|ate|ation) | swp | stp | idcw | dividend |
  sharpe | sortino | standard\s+deviation | beta | alpha | turnover | p/?e\s+ratio | tracking\s+error |
  regular\s+plan | ceo | sip\s+dates? | mandate | elss | income\s+tax\s+return | itr |
  minimum\s+redemption | how\s+(do|can)\s+i\s+(redeem|withdraw|sell|switch|invest|buy|start)
)\b""", re.X | re.I)
golden = yaml.safe_load(open("tests/eval/golden.yaml"))
gq = [t["q"] for c in golden for t in c.get("turns", [c]) if t.get("expect_kind") == "answer"]
gn = [t["q"] for c in golden for t in c.get("turns", [c]) if t.get("expect_kind") == "not_found"]
inc = gq + [q for q, *_ in PARAPHRASES + HELDOUT + HOLDINGS_DEEP]
fp = [q for q in inc if UNSUPPORTED.search(q)]
print("in-corpus queries", len(inc), "false positives", len(fp), fp)
for name, qs in [("golden not_found", gn), ("ooc_scheme", [q for q, _ in OOC_SCHEME]), ("near_miss", [q for q, _ in OOC_NEAR_MISS]), ("general", [q for q, _ in OOC_GENERAL])]:
    miss = [q for q in qs if not UNSUPPORTED.search(q)]
    print(f"{name}: caught {len(qs)-len(miss)}/{len(qs)}; missed {miss}")
