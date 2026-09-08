"""CLI: research idea registry (register / freeze / approve / queue dry-run / link-result).

    python -m jobs.research_ideas validate --spec idea.json
    python -m jobs.research_ideas register --spec idea.json --actor alice
    python -m jobs.research_ideas freeze --idea idea_xxx --actor alice
    python -m jobs.research_ideas approve --idea idea_xxx --approved-by alice --version 1 --spec-hash <hash>
    python -m jobs.research_ideas queue --idea idea_xxx --actor alice --contract-out contract.json   # dry-run only

Nothing here launches QuantConnect, backtests, or deployments.
"""

from __future__ import annotations

import sys

from market_intelligence.ideas import main

if __name__ == "__main__":
    sys.exit(main())
