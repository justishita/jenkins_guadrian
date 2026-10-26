"""Entry point. ``TRIGGER_MODE`` decides who starts an investigation, never both.

* ``rabbitmq`` (default, the original behaviour): consume ``incident.created`` from RabbitMQ.
* ``a2a``: serve the A2A task endpoint; the Coordinator triggers the investigation and the
  RabbitMQ consumer is not started, so one incident is never processed by both paths.
"""

import os

mode = os.getenv("TRIGGER_MODE", "rabbitmq").strip().lower()

if mode == "a2a":
    from .a2a_app import serve

    serve()
elif mode in ("rabbitmq", "rabbit"):
    from .consumer import main

    main()
else:
    raise SystemExit(f"Unknown TRIGGER_MODE {mode!r}; expected 'a2a' or 'rabbitmq'")
