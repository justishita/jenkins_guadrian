"""Failure taxonomy, copied verbatim from docs/decisions.md.

Do not add aliases. Replace this module with the shared one in `common/` once P3
publishes it (agents must not import from each other, but may import `common`).
"""

from enum import StrEnum


class FailureTaxonomy(StrEnum):
    CODE_TEST_FAILURE = "code_test_failure"
    BUILD_COMPILATION_FAILURE = "build_compilation_failure"
    DEPENDENCY_REGRESSION = "dependency_regression"
    TIMEOUT = "timeout"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    INFRA_NETWORK_FAILURE = "infra_network_failure"
    CONFIG_ERROR = "config_error"
    AUTH_FAILURE = "auth_failure"
    FLAKY_TEST = "flaky_test"
    DEPLOYMENT_FAILURE = "deployment_failure"
    UNKNOWN = "unknown"
