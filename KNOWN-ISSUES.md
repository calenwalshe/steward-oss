# Known issues (0.1.0)

- **Merge decisions are the owner's.** With `acceptance = "human"`, an
  "Incorporate?" card must be answered by you (an agent's approve is refused);
  `steward incorporate <job>` merges by hand.
- **Acceptance holds still sometimes need a human.** An approver role that
  resolves them is in design.
- **A job whose worker never starts can look healthy.** A proof-of-life check
  is planned; until then, glance at the pane if a new job shows no progress.
- **The full test suite takes about 2-3 minutes** with pytest-xdist
  (`-n 8`), and much longer serially.
