# Agents

Agent packages are independently owned: P1 owns `jenkins_agent`, P2 owns `metrics_agent`, and P3 owns `code_agent`. Agents must not call or import one another; shared contracts belong in `common/`.