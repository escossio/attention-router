# CI Agent V0 status

State: CANDIDATE / NOT ACTIVE ON DEFAULT BRANCH

Branch: `feat/github-ci-agent-v0`

Activation condition: merge the dedicated CI Agent PR into `main` after all existing required checks are green.

Until that merge, `.github/workflows/ci-agent.yml` is validated as repository content but `workflow_run` does not act as the repository's event-driven CI observer.
