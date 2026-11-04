## Working rules (cost-aware)
- Run only the tests for files you changed; let CI run the full suite. Run the full suite locally only before a release.
- No synthetic large-scale datasets, load tests or agent "new-hire" simulations unless the owner asks.
- One PR per feature. Bundle small fixes into it.
- Comments and docstrings: 3 lines max, explaining why, not history. AUDIT.md entries: 10 lines max.
- Commit messages: a subject plus up to 5 lines.
- Reports to the owner: 15 lines max. What shipped (PR + commit), what needs the owner, what's next.
- Ask before starting anything estimated at more than 2 hours of work.
