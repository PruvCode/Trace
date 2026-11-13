# TRACE preview scorecard

TRACE rates **8.2/10 overall**, with **5.5/10 for coding effectiveness** and **8.8/10 for envisioned potential**. The overall score gives equal weight to the eight criteria below. Ratings are editorial assessments based on the verified preview evidence.

| Criterion | Previous | Current | Basis |
| --- | ---: | ---: | --- |
| Ideation | 8.5 | 8.5 | Local memory links past decisions, failed checks, code and evidence. |
| Usability | 9.0 | 9.0 | Verified Windows installer and Obsidian navigation. Task binding and Obsidian refresh involve manual steps. |
| Scalability | 7.7 | 7.7 | Prior tests covered about 10,000 graph entities and 50,000 messages. Larger repositories and cold indexing need more measurement. |
| Engineering quality | 9.0 | 9.0 | Regression tests, migration checks, isolated installation and native integration checks. |
| Differentiation | 8.5 | 8.5 | Durable engineering decisions and evidence are accessible across sessions and source revisions. |
| Coding effectiveness | 4.0 | 5.5 | A matched synthetic win and one successful browser replay. The repository-function trials tied notes. |
| Safety and privacy | 9.0 | 9.0 | Opt-in capture, redaction, project and task isolation, and checks for file-specific recall. |
| Release readiness | 8.0 | 8.0 | Windows preview and clean installations verified. Broader platform and effectiveness gates remain open. |

## Coding evidence

In eight fresh two-file synthetic tasks, TRACE passed seven and maintained notes passed three with the same model, editor, and retry limits. Median input fell from 597 to 361 tokens and median elapsed time from 62.7 to 45.4 seconds. On six separate tasks using functions from Rubaru, WISIT, and TRACE, each arm passed one.

The Rubaru events and archive trial built the fixture site and checked navigation in the browser. On the fresh attempt TRACE passed two of three checks and notes passed one. With the same precise retry feedback in each arm, TRACE passed three and notes passed two. A separate rebuild reproduced all final verdicts. The browser tests covered navigation and preserved content. They did not grade visual styling, and the generated archive link used a different CSS class than requested.

The broader effectiveness gate calls for fresh repository tasks, repeated trials, two qualified models and equal budgets. The second local model passed one of its two qualification cases. The Rubaru browser task used one model and the replay reused the diagnosed task. See the [effectiveness protocol](../../research/preview/EFFECTIVENESS.md) for the test setup and remaining evidence requirements.
