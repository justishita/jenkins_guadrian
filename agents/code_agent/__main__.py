"""Entry point. ``TRIGGER_MODE`` decides who starts an investigation, never both.

* ``rabbitmq``: consume ``incident.created`` from RabbitMQ (the original behaviour).
* ``a2a`` (default, matching the Coordinator): serve the A2A task endpoint; the Coordinator triggers
  the investigation and the RabbitMQ consumer is not started, so one incident is never processed twice.
"""

import os

mode = os.getenv("TRIGGER_MODE", "rabbitmq").strip().lower()

if mode == "a2a":
    from agents.code_agent.a2a_app import serve

    serve()
elif mode in ("rabbitmq", "rabbit"):
    from agents.code_agent.agent import main

    main()
else:
    raise SystemExit(f"Unknown TRIGGER_MODE {mode!r}; expected 'a2a' or 'rabbitmq'")
