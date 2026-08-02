# Contributing

Thanks for improving the Job Hunt Board.

## Before opening a change

1. Run `./jobs setup`.
2. Install contributor tools with
   `./.venv/bin/python -m pip install -r requirements-dev.txt`.
3. Run `./.venv/bin/python -m pytest --cov=jobhunt --cov=jobs --cov-report=term-missing --cov-fail-under=60 -q`.
4. For the browser gate, run `npm ci`, `npx playwright install chromium`, then
   `npm run test:e2e`.
5. Keep fixtures synthetic and tests offline.
6. Never commit private search preferences, personal board data, connections,
   or full live job-description corpora.

Changes to an ATS adapter should include a fixture and parser test. Changes to
the board should be checked in a real browser at desktop and phone widths.
The browser smoke gate is `npm run test:e2e` after installing the Node
development dependencies and a local Chromium browser. It uses only the
fictional sample board and never calls a live ATS.

The most useful issues include the command you ran, the exact error, Python
version, operating system, and the output of `./jobs doctor`. Remove personal
information before posting logs.
