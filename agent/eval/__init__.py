"""Admiry's evaluation (spec 2026-10-07-admiry-capability-layer-design §7): the golden set (`cases`, the flawed copies
`flaws`), the runner that builds each case's plan through the real commands against a dedicated database and judges it
(`runner`), the bundles that carry frozen plans and grades between the dev box and the case files (`bundle`), and its
command line, `python -m agent.eval`."""
